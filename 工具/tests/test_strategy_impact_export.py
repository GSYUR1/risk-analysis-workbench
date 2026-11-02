import copy
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from openpyxl import Workbook, load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategy_impact import estimate_quota_impact, read_input, summarize_impact
from strategy_impact_excel import export_impact_workbook, label
from test_strategy_impact import fixture


class ImpactExportTests(unittest.TestCase):
    def export(self, output, base=None, spec=None):
        if base is None:
            base, spec = fixture()
        detail = estimate_quota_impact(base, spec)
        spec = detail.attrs['resolved_spec']
        summaries = {'overall': summarize_impact(detail, [], spec)}
        summaries.update({f'group_{number}': summarize_impact(detail, group, spec) for number, group in enumerate(spec['summary_groups'], start=1)})
        return export_impact_workbook(base, detail, summaries, spec, output)

    def test_formulas_caches_formats_source_and_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'impact.xlsx'
            checks = self.export(output)
            self.assertGreater(checks['formula_cells_checked'], 100)
            formulas = load_workbook(output, data_only=False)
            values = load_workbook(output, data_only=True)
            self.assertEqual(formulas.sheetnames, ['Summary', '过程表', '参数', '计算', '基准输入'])
            self.assertTrue(formulas.calculation.fullCalcOnLoad)
            summary = formulas['Summary']
            headers = {cell.value: cell.column for cell in summary[26] if cell.value}
            result_cell = summary.cell(27, headers[label('payout_amount_change')])
            self.assertEqual(result_cell.data_type, 'f')
            self.assertAlmostEqual(values['Summary'][result_cell.coordinate].value, .122)
            risk_cell = summary.cell(27, headers[label('dpd3_7__old_rate')])
            self.assertIn('IF(', risk_cell.value)
            self.assertEqual(risk_cell.number_format, '0.00%')
            for rule in summary.conditional_formatting:
                for region in rule.sqref.ranges:
                    self.assertFalse(region.min_row <= 27 <= region.max_row)
            self.assertGreater(len(formulas['过程表'].merged_cells.ranges), 0)
            self.assertGreater(len(formulas['过程表'].conditional_formatting), 0)
            calc_formulas = [cell.value for row in formulas['计算'] for cell in row if cell.data_type == 'f']
            self.assertTrue(any("'基准输入'!" in value for value in calc_formulas))
            self.assertTrue(any("'参数'!$B$3" in value for value in calc_formulas))
            self.assertTrue(any("'参数'!$B$5" in value for value in calc_formulas))
            self.assertEqual(formulas['参数']['B3'].number_format, '0.00')
            self.assertTrue(all(not cell.alignment.wrap_text for row in summary for cell in row if cell.value is not None))
            self.assertEqual(summary['B3'].value, '【分析背景】')
            self.assertEqual(summary['A22'].value, '详细数据')
            self.assertIn('12.20%', values['Summary']['C15'].value)
            self.assertEqual(summary['C15'].data_type, 'f')
            formulas.close()
            values.close()

    def test_unavailable_and_unmatured_are_na_not_zero(self):
        base, spec = fixture()
        spec['risk_metrics'][0]['weight_mode'] = 'unavailable'
        spec['risk_metrics'][0]['denominator'] = 'not_used_or_required'
        base['到期件数'] = 0
        base['件数风险率'] = float('nan')
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'impact.xlsx'
            self.export(output, base, spec)
            values = load_workbook(output, data_only=True)
            headers = {cell.value: cell.column for cell in values['Summary'][26] if cell.value}
            for identifier in ('dpd3_7', 'dpd3_7cnt'):
                self.assertEqual(values['Summary'].cell(27, headers[label(identifier + '__new_rate')]).value, '#N/A')
            values.close()

    def test_plain_layout_and_no_draw_data(self):
        base, spec = fixture()
        spec.pop('column_dimension')
        spec['parameters']['draw_enabled'] = False
        del spec['fields']['draw_count']
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'impact.xlsx'
            self.export(output, base, spec)
            values = load_workbook(output, data_only=True)
            headers = {cell.value: cell.column for cell in values['Summary'][26] if cell.value}
            self.assertAlmostEqual(values['Summary'].cell(27, headers[label('payout_amount_change')]).value, .1)
            self.assertEqual(values['Summary'].cell(27, headers[label('old_draw_rate')]).value, '#N/A')
            values.close()
            with self.assertRaises(FileExistsError):
                self.export(output, base, spec)

    def test_excel_input_requires_explicit_flat_range_and_caches(self):
        _, spec = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'input.xlsx'
            workbook = Workbook()
            workbook.active.title = 'data'
            workbook.active.append(['a', 'b'])
            workbook.active.append([1, '=1+1'])
            workbook.save(path)
            with self.assertRaises(ValueError):
                read_input(path, spec)
            spec['source'] = {'sheet': 'data', 'range': 'A1:B2'}
            with self.assertRaises(ValueError):
                read_input(path, spec)
            workbook.active['B2'] = 2
            workbook.save(path)
            self.assertEqual(read_input(path, spec).iloc[0]['b'], 2)

    def test_dimension_text_is_not_executed_as_formula(self):
        base, spec = fixture()
        base['评级'] = base['评级'].replace({'A': '=1+1', 'B': 'a*?'})
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'impact.xlsx'
            self.export(output, base, spec)
            workbook = load_workbook(output, data_only=False)
            matched = []
            for sheet in workbook:
                for row in sheet:
                    for cell in row:
                        if cell.value == '=1+1':
                            matched.append(cell)
                            self.assertEqual(cell.data_type, 's')
            self.assertGreater(len(matched), 0)
            workbook.close()


if __name__ == '__main__':
    unittest.main()
