import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import Workbench, read


class CurrentWorkbenchTests(unittest.TestCase):
    def test_analysis_lifecycle_and_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'workbench'
            project = Path(tmp) / 'project'
            root.mkdir()
            project.mkdir()
            workbench = Workbench(root)
            workbench.initialize()
            result = workbench.new_analysis(project, {
                'title': 'synthetic analysis',
                'goal': 'validate archive flow',
                'country': 'ph',
                'product': '不限产品',
                'task_type': 'risk',
            }, thread_id='thread-a')
            analysis_dir = Path(result['path'])
            self.assertTrue((analysis_dir / 'analysis.json').is_file())
            self.assertFalse((analysis_dir / 'task.json').exists())
            workbench.link_thread(project, result['analysis_id'], 'thread-b')
            archive = workbench.draft_archive(project, result['analysis_id'], {
                'data_sources': ['synthetic'],
                'metrics': ['count'],
                'method': ['pivot'],
                'conclusions': ['ok'],
                'deliverables': ['report.xlsx'],
                'lessons': ['reuse'],
            })
            self.assertEqual(read(Path(archive['path']) / 'archive.json')['status'], 'draft')
            final = workbench.finalize_archive(project, result['analysis_id'], 'TEST', 'synthetic approval')
            self.assertEqual(final['status'], 'approved')
            self.assertTrue(Path(final['path'], 'archive.md').is_file())
            self.assertEqual(len(workbench.search_projects('synthetic')), 1)

    def test_index_and_maintenance_do_not_create_removed_task_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbench = Workbench(root)
            workbench.initialize()
            catalog = read(root / '索引/catalog.json')
            self.assertNotIn('tasks', catalog)
            result = workbench.maintenance()
            self.assertIn('knowledge_count', result)


if __name__ == '__main__':
    unittest.main()
