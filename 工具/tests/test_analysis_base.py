import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis_base import build_views, load_base, run_views, save_base
from ph_data_analysis import generate_grouped_pivot


class AnalysisBaseTests(unittest.TestCase):
    def setUp(self):
        self.frame = pd.DataFrame({
            'month': ['01', '02', '01', '02'], 'rating': ['A', 'A', 'B', None],
            'channel': ['x', 'x', 'y', 'y'], 'cre_cnt': [10, 90, 20, 30],
            'bad': [1, 9, 0, 0], 'due': [2, 90, 0, 0],
        })
        self.contract = {
            'base_id': 'synthetic_v1', 'kind': 'grouped', 'sample': 'synthetic only',
            'time_window': 'synthetic', 'snapshot': 'synthetic', 'grain': 'month/rating/channel',
            'grain_keys': ['month', 'rating', 'channel'], 'dimensions': ['month', 'rating', 'channel'],
            'required_columns': list(self.frame.columns), 'definition_evidence': 'synthetic definition',
            'retention': 'temporary test', 'storage_authorization': 'synthetic offline test',
            'additive_columns': ['cre_cnt', 'bad', 'due'],
            'additivity_evidence': {name: 'disjoint synthetic applications' for name in ['cre_cnt', 'bad', 'due']},
            'metrics': {
                '计数': {'op': 'sum', 'column': 'cre_cnt', 'evidence': 'synthetic'},
                'dpd1_7': {'op': 'ratio', 'numerator': 'bad', 'denominator': 'due', 'evidence': 'synthetic'},
            },
        }
        self.views = [{'name': 'rating-channel', 'ind': ['rating'], 'col': ['channel']}]

    def save(self, root):
        sql = root / 'extract.sql'
        sql.write_text('SELECT synthetic_only;', encoding='utf-8')
        directory = root / '中文底表_v1'
        save_base(self.frame, directory, self.contract, [sql])
        return directory

    def test_weighted_ratio_nulls_and_all(self):
        result = build_views(self.frame, self.contract, self.views)['rating-channel']
        self.assertEqual(result.loc['All', ('计数', 'All')], 150)
        self.assertEqual(result.loc['A', ('计数', 'x')], 100)
        self.assertAlmostEqual(result.loc['A', ('dpd1_7', 'x')], 10 / 92)
        self.assertAlmostEqual(result.loc['All', ('dpd1_7', 'All')], 10 / 92)
        self.assertTrue(pd.isna(result.loc['B', ('dpd1_7', 'y')]))
        self.assertEqual(result.loc['__MISSING__', ('计数', 'y')], 30)

    def test_multi_axes_and_missing_components(self):
        result = generate_grouped_pivot(self.frame, ['rating'], ['month', 'channel'], self.contract['metrics'])
        self.assertEqual(result.loc['All', ('计数', 'All', '')], 150)
        result = generate_grouped_pivot(self.frame, ['rating', 'month'], ['channel'], self.contract['metrics'])
        self.assertEqual(result.loc[('All', ''), ('计数', 'All')], 150)
        changed = self.frame.copy()
        changed['due'] = float('nan')
        result = generate_grouped_pivot(changed, ['rating'], ['channel'], self.contract['metrics'])
        self.assertTrue(pd.isna(result.loc['All', ('dpd1_7', 'All')]))

    def test_filter_and_missing_dimension(self):
        view = dict(self.views[0], filters={'month': ['01']})
        result = build_views(self.frame, self.contract, [view])['rating-channel']
        self.assertEqual(result.loc['All', ('计数', 'All')], 30)
        with self.assertRaisesRegex(ValueError, 'absent'):
            build_views(self.frame, self.contract, [dict(view, col=['not_saved'])])
        with self.assertRaisesRegex(ValueError, 'empty'):
            build_views(self.frame, self.contract, [dict(view, filters={'month': ['missing']})])

    def test_reuse_for_another_cross_and_freeze_bin_types(self):
        frame = self.frame.copy()
        frame['rating'] = pd.Categorical(frame['rating'], categories=['A', 'B', 'C'], ordered=True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sql = root / 'extract.sql'
            sql.write_text('SELECT synthetic_only;', encoding='utf-8')
            directory = root / 'base'
            save_base(frame, directory, self.contract, [sql])
            restored, manifest = load_base(directory)
            pd.testing.assert_frame_equal(frame, restored)
            views = self.views + [{'name': 'month-channel', 'ind': ['month'], 'col': ['channel']}]
            original = build_views(frame, self.contract, views)
            repeated = build_views(restored, manifest['contract'], views)
            for title in original:
                pd.testing.assert_frame_equal(original[title], repeated[title])
                self.assertEqual(repeated[title].loc['All', ('计数', 'All')], 150)

    def test_tool_version_change_blocks_without_remote_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = self.save(root)
            with patch('analysis_base.ENGINE_FILES', ('changed.py',)):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    run_views(directory, directory / 'views.example.json', root / 'blocked-run')
            self.assertFalse((root / 'blocked-run').exists())

    def test_roundtrip_and_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = self.save(Path(temporary))
            restored, manifest = load_base(directory)
            pd.testing.assert_frame_equal(self.frame, restored)
            self.assertEqual(manifest['rows'], 4)
            self.assertTrue((directory / 'supplement.py').is_file())
            with self.assertRaises(FileExistsError):
                self.save(Path(temporary))
            (directory / 'sql' / '00_extract.sql').write_text('changed', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'changed'):
                load_base(directory)

    def test_reject_duplicate_grain_and_nonadditive(self):
        duplicate = pd.concat([self.frame, self.frame.iloc[:1]], ignore_index=True)
        with self.assertRaisesRegex(ValueError, 'Duplicate grain'):
            build_views(duplicate, self.contract, self.views)
        contract = copy.deepcopy(self.contract)
        contract['metrics']['users'] = {'op': 'nunique', 'column': 'cre_cnt', 'evidence': 'synthetic'}
        with self.assertRaisesRegex(ValueError, 'distinct'):
            build_views(self.frame, contract, self.views)
        contract = copy.deepcopy(self.contract)
        contract['additivity_evidence'].pop('cre_cnt')
        with self.assertRaisesRegex(ValueError, 'evidence'):
            build_views(self.frame, contract, self.views)

    def test_local_run_preserves_export_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = self.save(root)
            output = root / 'supplement_run'
            with patch('socket.create_connection', side_effect=AssertionError('No network')):
                run_views(directory, directory / 'views.example.json', output)
            record = json.loads((output / 'run.json').read_text(encoding='utf-8'))
            self.assertFalse(record['remote_query'])
            workbook = load_workbook(output / 'supplement.xlsx')
            self.assertTrue(workbook.active.merged_cells.ranges)
            self.assertTrue(len(workbook.active.conditional_formatting))
            values = [cell.value for row in workbook.active for cell in row]
            self.assertIn(150, values)
            self.assertIn('__MISSING__', values)
            self.assertTrue(any(isinstance(value, (float, int)) and abs(value - 10 / 92) < 1e-12 for value in values))
            workbook.close()
            with self.assertRaises(FileExistsError):
                run_views(directory, directory / 'views.example.json', output)
            request = json.loads((directory / 'views.example.json').read_text(encoding='utf-8'))
            request['base_manifest_sha256'] = 'wrong'
            request_path = root / 'wrong.json'
            request_path.write_text(json.dumps(request), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'different base'):
                run_views(directory, request_path, root / 'wrong-run')

    def test_generated_launcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = self.save(root)
            result = subprocess.run([
                sys.executable, '-X', 'utf8', str(directory / 'supplement.py'),
                '--views', str(directory / 'views.example.json'), '--output-dir', str(root / 'cli-run'),
            ], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / 'cli-run' / 'supplement.xlsx').is_file())

    def test_detail_reuses_ph_and_masks_unmatured(self):
        frame = pd.DataFrame({
            'id': ['001', '002', '003'], 'rating': ['A', None, 'B'], 'channel': ['x', 'x', 'y'],
            'overdue_amt_1_7': [0., 0., 2.], 'expire_amt_1_7': [0., 0., 10.], 'expire_cnt_1_7': [0, 0, 1],
        })
        contract = dict(self.contract, kind='detail', grain='application', grain_keys=['id'],
                        dimensions=['rating', 'channel'], required_columns=list(frame.columns),
                        pivot_options={'id_col': 'id', 'stage': 'credit', 'risk_ind': ['1_7'],
                                       'indicator': ['计数'], 'lift_base': 'overall'})
        result = build_views(frame, contract, self.views)['rating-channel']
        self.assertEqual(result.loc['All', ('计数', 'All')], 3)
        self.assertTrue(pd.isna(result.loc['A', ('dpd1_7', 'x')]))
        self.assertEqual(result.loc['__MISSING__', ('计数', 'x')], 1)
        with tempfile.TemporaryDirectory() as temporary:
            sql = Path(temporary) / 'extract.sql'
            sql.write_text('SELECT synthetic_only;', encoding='utf-8')
            directory = Path(temporary) / 'detail'
            save_base(frame, directory, contract, [sql])
            restored, _ = load_base(directory)
            self.assertEqual(restored['id'].iloc[0], '001')


if __name__ == '__main__':
    unittest.main()
