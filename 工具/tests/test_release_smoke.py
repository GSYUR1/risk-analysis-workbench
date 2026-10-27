import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from excel_export import merge_formatted_indicator_to_excel
from ph_data_analysis import generate_indicator


class ReleaseSmokeTests(unittest.TestCase):
    def test_synthetic_indicator_and_export(self):
        frame = pd.DataFrame({
            'credit_apply_no': ['synthetic-a', 'synthetic-b', 'synthetic-c'],
            'cohort': ['A', 'A', 'B'],
            'is_approve': [1, 0, 1],
            'is_order': [1, 0, 1],
            'is_payout': [1, 0, 0],
        })
        result = generate_indicator(frame, 'cohort', columns_output=['授信申请件数'], show=False)
        self.assertEqual(result.loc['A', '授信申请件数'], 2)
        self.assertEqual(result.loc['B', '授信申请件数'], 1)
        self.assertEqual(result.iloc[-1]['授信申请件数'], 3)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'synthetic.xlsx'
            merge_formatted_indicator_to_excel({'synthetic': result}, str(output))
            workbook = load_workbook(output)
            self.assertGreater(len(workbook.active.conditional_formatting), 0)
            values = [cell.value for row in workbook.active for cell in row]
            self.assertIn('授信申请件数', values)
            self.assertIn(3, values)
            workbook.close()

    def test_safe_config_and_profile_contract(self):
        root = Path(__file__).resolve().parents[2]
        config = json.loads((root / '工作约定/config.example.json').read_text(encoding='utf-8'))
        self.assertFalse(config['external_enabled'])
        self.assertEqual(config['ssh_hosts'], {})
        self.assertEqual(config['approved_profiles'], [])
        self.assertFalse((root / '工作约定/config.json').exists())
        profile = json.loads((root / '工作约定/数据口径/ph/pivot_base_profile.json').read_text(encoding='utf-8'))
        self.assertEqual(len(profile['metrics']), 140)
        self.assertEqual(len(profile['default_dimensions']), 9)
        self.assertNotEqual(profile['indicator_table'], 'demo.indicator')
        self.assertNotEqual(profile['detail_table'], 'demo.order_trace')

    def test_skill_keeps_original_governance_and_workflow_sections(self):
        root = Path(__file__).resolve().parents[2]
        skill = (root / 'skills/risk-analysis-agent/SKILL.md').read_text(encoding='utf-8')
        for heading in ('## 强制启动阅读', '## 执行路径', '## 新客授信宽表 → 透视聚合底表', '## 文档治理'):
            self.assertIn(heading, skill)
        self.assertIn('业务和工作台经验留在工作台，不自动改全局memory或其他Skill。', skill)
        self.assertIn('不维护第二套副本。', skill)


if __name__ == '__main__':
    unittest.main()
