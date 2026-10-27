import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pivot_base_sql import DEFAULT_PROFILE, build_pivot_base_sql


class PivotBaseSqlTests(unittest.TestCase):
    def setUp(self):
        self.profile = json.loads(DEFAULT_PROFILE.read_text(encoding='utf-8'))
        self.metrics = self.profile['metrics']
        self.dimensions = self.profile['default_dimensions']
        self.connection = sqlite3.connect(':memory:')
        columns = ['credit_apply_no TEXT']
        columns += [f'{name} TEXT' for name in self.dimensions]
        columns += [f'{name} REAL' for name in self.metrics]
        self.connection.execute('CREATE TABLE fact (' + ', '.join(columns) + ')')
        for credit, month, amount in [('a', '2026-05', 2), ('b', '2026-05', 3), ('c', '2026-06', 5)]:
            values = [credit, month] + ['same'] * (len(self.dimensions) - 1) + [amount] * len(self.metrics)
            self.connection.execute('INSERT INTO fact VALUES (' + ', '.join('?' for _ in values) + ')', values)
        self.spec = {
            'source_kind': 'indicator',
            'source_table': 'fact',
            'filters': ["trace_mth >= '2026-05'"],
        }

    def tearDown(self):
        self.connection.close()

    def execute(self, spec):
        result = build_pivot_base_sql(spec)
        cursor = self.connection.execute(result['sql'])
        return [column[0] for column in cursor.description], cursor.fetchall(), result

    def test_complete_field_contract_and_additive_counts(self):
        columns, rows, result = self.execute(self.spec)
        self.assertEqual(len(self.dimensions), 9)
        self.assertEqual(len(self.metrics), 140)
        self.assertEqual(columns, self.dimensions + self.metrics)
        self.assertEqual(self.metrics[0], 'cre_cnt')
        self.assertEqual(self.metrics[-1], 'deal_cibi_trade_cnt_cnt')
        self.assertIn('deal_amobilewalla_multi_norecord_cnt', columns)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row[9:], (5.0,) * 140)
        self.assertFalse(result['metadata']['remote_validated'])

    def test_only_requested_risk_points(self):
        expected_points = ['1_1', '1_3', '1_7', '1_15', '2_1', '2_3', '2_7', '2_15',
                           '3_7', '3_15', '4_7', '4_15', '5_7', '5_15', '6_7', '6_15']
        expected = [f'{prefix}_{point}' for point in expected_points
                    for prefix in ('overdue_amt', 'overdue_cnt', 'expire_amt', 'expire_cnt')]
        self.assertEqual(self.metrics[27:91], expected)

    def test_subtotal_conservation_and_no_dimensions(self):
        grouped_spec = {**self.spec, 'dimensions': ['trace_mth']}
        _, grouped, _ = self.execute(grouped_spec)
        columns, totals, _ = self.execute({**self.spec, 'dimensions': []})
        self.assertEqual(columns, self.metrics)
        self.assertEqual(totals, [(10.0,) * 140])
        self.assertEqual(tuple(sum(row[index + 1] for row in grouped) for index in range(140)), totals[0])

    def test_null_dimensions_and_unmatured_metrics_preserved(self):
        self.connection.execute('UPDATE fact SET cre_rating_sub = NULL, expire_amt_1_7 = NULL, overdue_amt_1_7 = NULL')
        columns, rows, _ = self.execute({**self.spec, 'dimensions': ['cre_rating_sub']})
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0][0])
        self.assertIsNone(rows[0][columns.index('expire_amt_1_7')])
        self.assertIsNone(rows[0][columns.index('overdue_amt_1_7')])

    def join_spec(self):
        self.connection.execute('CREATE TABLE dim (credit_apply_no TEXT, bucket TEXT, pt TEXT)')
        self.connection.executemany('INSERT INTO dim VALUES (?, ?, ?)', [('a', 'hit', 'current'), ('b', 'old', 'old')])
        return {
            **self.spec,
            'dimensions': ['trace_mth', {'name': 'bucket', 'expression': 'extra.bucket'}],
            'joins': [{'table': 'dim', 'alias': 'extra', 'keys': {'credit_apply_no': 'credit_apply_no'},
                       'filters': ["pt = 'current'"]}],
        }

    def test_left_join_preserves_unmatched_and_filters_dimension(self):
        columns, rows, result = self.execute(self.join_spec())
        self.assertEqual(columns, ['trace_mth', 'bucket'] + self.metrics)
        self.assertEqual(sum(row[2] for row in rows), 10)
        self.assertEqual(sum(row[1] is None for row in rows), 2)
        checks = dict(result['checks'])
        self.assertEqual(self.connection.execute(checks['extra_duplicate_keys']).fetchall(), [])
        self.assertEqual(self.connection.execute(checks['join_row_counts']).fetchall(), [('before', 3), ('after', 3)])
        self.assertEqual(self.connection.execute(checks['join_coverage']).fetchone(), (3, 2))

    def test_duplicate_join_is_detected_not_silently_deduplicated(self):
        spec = self.join_spec()
        self.connection.execute("INSERT INTO dim VALUES ('a', 'second', 'current')")
        _, _, result = self.execute(spec)
        checks = dict(result['checks'])
        self.assertEqual(self.connection.execute(checks['extra_duplicate_keys']).fetchall(), [('a', 2)])
        self.assertEqual(self.connection.execute(checks['join_row_counts']).fetchall(), [('before', 3), ('after', 4)])

    def test_base_duplicate_and_null_keys_detected(self):
        self.connection.execute("UPDATE fact SET credit_apply_no = 'a' WHERE credit_apply_no = 'b'")
        self.connection.execute("UPDATE fact SET credit_apply_no = NULL WHERE credit_apply_no = 'c'")
        checks = dict(build_pivot_base_sql(self.spec)['checks'])
        self.assertEqual(self.connection.execute(checks['base_duplicate_keys']).fetchall(), [('a', 2)])
        self.assertEqual(self.connection.execute(checks['base_null_keys']).fetchone(), (1,))

    def test_complete_schema_and_missing_metric_rejection(self):
        spec = {**self.spec, 'source_columns': ['credit_apply_no'] + self.dimensions + self.metrics}
        self.assertTrue(build_pivot_base_sql(spec)['metadata']['schema_supplied'])
        spec['source_columns'].remove('deal_cibi_trade_cnt_sum')
        with self.assertRaisesRegex(ValueError, 'deal_cibi_trade_cnt_sum'):
            build_pivot_base_sql(spec)

    def test_detail_requires_all_formulas_and_evidence(self):
        spec = {**self.spec, 'source_kind': 'detail', 'dimensions': ['trace_mth']}
        with self.assertRaisesRegex(ValueError, 'every confirmed metric'):
            build_pivot_base_sql(spec)
        spec['metric_expressions'] = {name: f'SUM(b.{name})' for name in self.metrics}
        spec['metric_expressions']['cre_cnt'] = 'COUNT(DISTINCT b.credit_apply_no)'
        with self.assertRaisesRegex(ValueError, 'evidence'):
            build_pivot_base_sql(spec)
        spec['metric_expression_evidence'] = 'synthetic test formulas, not production definitions'
        _, rows, _ = self.execute(spec)
        self.assertEqual(sorted(row[1] for row in rows), [1, 2])

    def test_computed_dimension_and_composite_join(self):
        self.connection.execute('CREATE TABLE dim (credit_apply_no TEXT, trace_mth TEXT, bucket TEXT)')
        self.connection.executemany('INSERT INTO dim VALUES (?, ?, ?)', [('a', '2026-05', 'hit'), ('a', '2026-06', 'wrong')])
        spec = {**self.spec,
                'dimensions': [{'name': 'matched', 'expression': "CASE WHEN extra.bucket IS NULL THEN 'missing' ELSE extra.bucket END"}],
                'joins': [{'alias': 'extra', 'table': 'dim', 'keys': {'credit_apply_no': 'credit_apply_no', 'trace_mth': 'trace_mth'}}]}
        _, rows, result = self.execute(spec)
        self.assertEqual({row[0]: row[1] for row in rows}, {'hit': 2, 'missing': 8})
        for name, query in result['checks']:
            actual = self.connection.execute(query).fetchall()
            if name.endswith('duplicate_keys'):
                self.assertEqual(actual, [])

    def test_invalid_specs_fail_closed(self):
        variants = [
            {'source_kind': 'unknown'},
            {'source_table': self.profile['detail_table']},
            {'filters': []},
            {'filters': ['1=1; DROP TABLE fact']},
            {'dimensions': ['cre_cnt']},
            {'dimensions': ['trace_mth', 'TRACE_MTH']},
            {'grain_keys': []},
            {'metric_expressions': {'cre_cnt': 'COUNT(*)'}},
            {'joins': [{'alias': 'b', 'table': 'dim', 'keys': {'credit_apply_no': 'credit_apply_no'}}]},
            {'joins': [{'alias': 'extra', 'table': 'dim', 'keys': {}}]},
        ]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_pivot_base_sql({**copy.deepcopy(self.spec), **changes})
        build_pivot_base_sql({**self.spec, 'filters': [], 'allow_all_dates': True})

    def test_cli_outputs_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / '中文目录'
            root.mkdir()
            spec_path = root / 'spec.json'
            spec_path.write_text(json.dumps(self.spec), encoding='utf-8-sig')
            output = root / 'output.sql'
            command = [sys.executable, '-X', 'utf8', str(Path(__file__).resolve().parents[1] / 'pivot_base_sql.py'),
                       '--spec', str(spec_path), '--output', str(output)]
            result = subprocess.run(command, capture_output=True, encoding='utf-8', check=True)
            self.assertEqual(json.loads(result.stdout)['metric_count'], 140)
            self.connection.execute(output.read_text(encoding='utf-8'))
            self.connection.executescript(output.with_suffix('.checks.sql').read_text(encoding='utf-8'))
            manifest = json.loads(output.with_suffix('.manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['execution_status'], 'not_executed')
            original = output.read_bytes()
            retry = subprocess.run(command, capture_output=True, encoding='utf-8')
            self.assertNotEqual(retry.returncode, 0)
            self.assertEqual(output.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
