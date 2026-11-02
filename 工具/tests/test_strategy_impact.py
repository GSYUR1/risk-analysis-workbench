import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategy_impact import estimate_quota_impact, read_input, resolve_spec, run_impact, summarize_impact


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / '分析功能/策略影响预估/示例'


def fixture():
    return pd.read_csv(EXAMPLE / 'synthetic.csv'), json.loads((EXAMPLE / 'spec.json').read_text(encoding='utf-8'))


class StrategyImpactTests(unittest.TestCase):
    def test_uniform_ten_percent_example_and_risk_weights(self):
        base, spec = fixture()
        detail = estimate_quota_impact(base, spec)
        summary = summarize_impact(detail, [], spec).iloc[0]
        self.assertAlmostEqual(summary['quota_change'], .1)
        self.assertAlmostEqual(summary['payout_amount_change'], .122)
        self.assertAlmostEqual(summary['payout_count_change'], .02)
        self.assertAlmostEqual(summary['dpd3_7__relative_change'], .05)
        self.assertAlmostEqual(detail.iloc[0]['dpd3_7__new_rate'], .0525)
        self.assertAlmostEqual(detail.iloc[0]['dpd3_7__new_denominator'], 2000000 * 1.122)
        self.assertAlmostEqual(detail.iloc[0]['dpd3_7cnt__new_denominator'], 200 * 1.02)
        self.assertAlmostEqual(summary['dpd3_7__pp_change'], (summary['dpd3_7__new_rate'] - summary['dpd3_7__old_rate']) * 100)

    def test_disabled_draw_and_unchanged_scenario(self):
        base, spec = fixture()
        spec['parameters']['draw_enabled'] = False
        del spec['fields']['draw_count']
        detail = estimate_quota_impact(base, spec)
        summary = summarize_impact(detail, [], spec).iloc[0]
        self.assertAlmostEqual(summary['payout_amount_change'], .1)
        self.assertEqual(summary['payout_count_change'], 0)
        self.assertTrue(pd.isna(summary['old_draw_rate']))
        base['额度系数'] = 1.0
        detail = estimate_quota_impact(base, spec)
        summary = summarize_impact(detail, [], spec).iloc[0]
        for field in ('quota_change', 'payout_amount_change', 'dpd3_7__relative_change'):
            self.assertEqual(summary[field], 0)

    def test_migration_and_removal(self):
        base, spec = fixture()
        del spec['fields']['coefficient']
        spec['fields'].update(old_coefficient='旧系数', new_coefficient='新系数')
        base['旧系数'], base['新系数'] = .8, .88
        spec['scenario'] = 'migrate'
        self.assertAlmostEqual(estimate_quota_impact(base, spec).iloc[0]['quota_factor'], 1.1)
        spec['scenario'] = 'remove'
        spec['fields'] = {name: column for name, column in spec['fields'].items() if name not in ('old_coefficient', 'new_coefficient')}
        spec['fields']['coefficient'] = '旧系数'
        self.assertEqual(estimate_quota_impact(base, spec).iloc[0]['quota_factor'], 1.25)

    def test_draw_cap_changes_amount_multiplier(self):
        base, spec = fixture()
        base['动支件数'] = base['授信通过件数'] * .99
        base['额度系数'] = 2.0
        detail = estimate_quota_impact(base, spec)
        self.assertTrue(detail['draw_capped'].all())
        self.assertTrue(detail['new_draw_rate'].eq(1).all())
        self.assertAlmostEqual(detail.iloc[0]['new_payout_amount'], 4000000 * 2 / .99)

    def test_zero_draw_zero_payout_and_zero_risk(self):
        base, spec = fixture()
        base.loc[0, ['动支件数', '成交件数', '成交金额', '到期金额', '到期件数', '风险率', '件数风险率']] = 0
        detail = estimate_quota_impact(base, spec)
        self.assertEqual(detail.iloc[0]['new_payout_amount'], 0)
        self.assertTrue(pd.isna(detail.iloc[0]['old_ticket']))
        self.assertTrue(pd.isna(detail.iloc[0]['dpd3_7__old_rate']))
        base.loc[1:, '风险率'] = 0
        summary = summarize_impact(estimate_quota_impact(base, spec), [], spec).iloc[0]
        self.assertEqual(summary['dpd3_7__new_rate'], 0)
        self.assertTrue(pd.isna(summary['dpd3_7__relative_change']))

    def test_missing_weight_not_partial_summary(self):
        base, spec = fixture()
        base.loc[0, '到期金额'] = np.nan
        detail = estimate_quota_impact(base, spec)
        summary = summarize_impact(detail, [], spec).iloc[0]
        self.assertTrue(pd.isna(summary['dpd3_7__old_rate']))
        self.assertEqual(summary['dpd3_7__missing_weight_groups'], 1)
        self.assertFalse(pd.isna(detail.iloc[0]['dpd3_7__old_rate']))

    def test_all_unmatured_and_rate_only(self):
        base, spec = fixture()
        base['到期金额'], base['风险率'] = 0, np.nan
        summary = summarize_impact(estimate_quota_impact(base, spec), [], spec).iloc[0]
        self.assertTrue(pd.isna(summary['dpd3_7__new_rate']))
        self.assertEqual(summary['dpd3_7__unmatured_groups'], 4)
        base, spec = fixture()
        risk = spec['risk_metrics'][0]
        risk['weight_mode'] = 'unavailable'
        del risk['denominator']
        detail = estimate_quota_impact(base, spec)
        self.assertAlmostEqual(detail.iloc[0]['dpd3_7__new_rate'], .0525)
        self.assertTrue(pd.isna(summarize_impact(detail, [], spec).iloc[0]['dpd3_7__new_rate']))

    def test_mature_weight_differs_from_transaction_proxy(self):
        base, spec = fixture()
        base.loc[0, '到期金额'] = 1000
        mature = summarize_impact(estimate_quota_impact(base, spec), [], spec).iloc[0]
        spec['risk_metrics'][0]['weight_mode'] = 'transaction_proxy'
        proxy = summarize_impact(estimate_quota_impact(base, spec), [], spec).iloc[0]
        self.assertNotAlmostEqual(mature['dpd3_7__old_rate'], proxy['dpd3_7__old_rate'])

    def test_mixed_factors_independent_weighted_aggregation(self):
        base, spec = fixture()
        base['额度系数'] = [1.2, .8, 1.05, 1]
        detail = estimate_quota_impact(base, spec)
        summary = summarize_impact(detail, [], spec).iloc[0]
        amounts = base['到期金额'].to_numpy()
        coefficients = base['额度系数'].to_numpy()
        weights = amounts * coefficients * (1 + .2 * (coefficients - 1))
        rates = base['风险率'].to_numpy() * (1 + .5 * (coefficients - 1))
        self.assertAlmostEqual(summary['dpd3_7__new_rate'], np.dot(weights, rates) / weights.sum())
        grouped = summarize_impact(detail, ['月份'], spec)
        self.assertAlmostEqual(grouped['new_payout_amount'].sum(), summary['new_payout_amount'])

    def test_invalid_numeric_and_funnel_inputs(self):
        cases = [('额度系数', 0), ('额度系数', -1), ('额度系数', np.inf), ('风险率', 5), ('风险率', -.1),
                 ('风险率', np.nan), ('成交金额', 0), ('授信通过件数', 0), ('动支件数', 1001), ('动支件数', 0), ('到期件数', 401)]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                base, spec = fixture()
                base.loc[0, field] = value
                with self.assertRaises(ValueError):
                    estimate_quota_impact(base, spec)

    def test_duplicate_totals_and_missing_dimensions_rejected(self):
        base, spec = fixture()
        for candidate in (pd.concat([base, base.iloc[:1]], ignore_index=True), base.assign(评级='All'), base.assign(月份=None), base.drop(columns=['月份'])):
            with self.subTest():
                with self.assertRaises(ValueError):
                    estimate_quota_impact(candidate, spec)

    def test_bad_parameters_and_assumptions(self):
        base, spec = fixture()
        for params in ({'draw_enabled': 'false'}, {'risk_elasticity': -1}, {'risk_elastcity': .5}, {'draw_elasticity': float('nan')}, {'risk_elasticity': 1000}):
            with self.subTest(params=params):
                changed = copy.deepcopy(spec)
                changed['parameters'] = params
                with self.assertRaises(ValueError):
                    estimate_quota_impact(base, changed)
        spec['assumptions']['post_draw_conversion_unchanged'] = False
        with self.assertRaises(ValueError):
            estimate_quota_impact(base, spec)

    def test_rule_defaults_and_template_not_ready(self):
        _, spec = fixture()
        spec['parameters'] = {}
        resolved = resolve_spec(spec)
        self.assertFalse(resolved['parameters']['draw_enabled'])
        self.assertEqual(resolved['parameters']['risk_elasticity'], .5)
        self.assertIn('sha256', resolved['rule'])
        template = json.loads((ROOT / '分析功能/策略影响预估/模板/strategy_impact_spec.json').read_text(encoding='utf-8'))
        with self.assertRaises(ValueError):
            resolve_spec(template)

    def test_cli_and_outputs_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'run'
            command = [sys.executable, '-X', 'utf8', str(ROOT / '工具/strategy_impact.py'), '--input', str(EXAMPLE / 'synthetic.csv'), '--spec', str(EXAMPLE / 'spec.json'), '--output-dir', str(output)]
            result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
            self.assertIn('影响预估.xlsx', manifest['artifacts'])
            self.assertIn('分析功能/策略影响预估/方法说明.md', manifest['versions'])
            self.assertFalse(manifest['excel_checks']['excel_engine_recalculated'])
            with self.assertRaises(FileExistsError):
                run_impact(EXAMPLE / 'synthetic.csv', EXAMPLE / 'spec.json', output)

    def test_csv_duplicate_headers_rejected(self):
        _, spec = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'duplicate.csv'
            path.write_text('a,a\n1,2\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                read_input(path, spec)


if __name__ == '__main__':
    unittest.main()
