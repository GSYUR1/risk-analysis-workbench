import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capability_catalog import build_catalog


ROOT = Path(__file__).resolve().parents[2]


class CapabilityCatalogTests(unittest.TestCase):
    def test_real_catalog_statuses_and_no_external_mining(self):
        catalog = build_catalog(ROOT)
        items = {item['id']: item for item in catalog['capabilities']}
        self.assertEqual(len(items), 4)
        self.assertEqual(items['strategy_impact']['status'], 'available')
        self.assertEqual(items['feature_mining']['status'], 'planned')
        self.assertEqual(items['feature_mining']['code'], [])
        self.assertEqual(items['feature_mining']['requires'], [])

    def test_reject_duplicate_broken_and_escape_references(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / '分析功能/mining'
            directory.mkdir(parents=True)
            source = json.loads((ROOT / '分析功能/特征挖掘/capability.json').read_text(encoding='utf-8'))
            source['entry'] = '分析功能/mining/README.md'
            (directory / 'README.md').write_text('# test', encoding='utf-8')
            manifest = directory / 'capability.json'
            manifest.write_text(json.dumps(source), encoding='utf-8')
            self.assertEqual(build_catalog(root, write=True), build_catalog(root))
            self.assertTrue((root / '索引/capabilities.json').is_file())
            source['entry'] = '../outside.md'
            manifest.write_text(json.dumps(source), encoding='utf-8')
            with self.assertRaises(ValueError):
                build_catalog(root)
            source['entry'] = '分析功能/mining/README.md'
            source['requires'] = ['nonexistent']
            manifest.write_text(json.dumps(source), encoding='utf-8')
            with self.assertRaises(ValueError):
                build_catalog(root)
            source['requires'] = ['feature_mining']
            manifest.write_text(json.dumps(source), encoding='utf-8')
            with self.assertRaises(ValueError):
                build_catalog(root)
            source['requires'] = []
            manifest.write_text(json.dumps(source), encoding='utf-8')
            shutil.copytree(directory, root / '分析功能/duplicate')
            with self.assertRaises(ValueError):
                build_catalog(root)


if __name__ == '__main__':
    unittest.main()
