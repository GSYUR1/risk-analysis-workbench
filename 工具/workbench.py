"""工作台CLI：路由当前分析事项、项目归档和知识索引命令。"""

import argparse
import json
import sys
from pathlib import Path

from core import GateError, Workbench, read


def main():
    parser = argparse.ArgumentParser(description='风控分析工作台：事项记录、项目归档与知识检索')
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[1]))
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'index', 'maintenance'):
        sub.add_parser(name)
    command = sub.add_parser('init-project')
    command.add_argument('--project-root', default='.')
    command = sub.add_parser('new-analysis')
    command.add_argument('--project-root', default='.')
    command.add_argument('--spec', required=True)
    command.add_argument('--thread-id', default='')
    command = sub.add_parser('link-thread')
    command.add_argument('--project-root', default='.')
    command.add_argument('--analysis-id', required=True)
    command.add_argument('--thread-id', required=True)
    command = sub.add_parser('archive-draft')
    command.add_argument('--project-root', default='.')
    command.add_argument('--analysis-id', required=True)
    command.add_argument('--spec', required=True)
    command = sub.add_parser('finalize-archive')
    command.add_argument('--project-root', default='.')
    command.add_argument('--analysis-id', required=True)
    command.add_argument('--reviewer', required=True)
    command.add_argument('--evidence', required=True)
    command = sub.add_parser('search-projects')
    command.add_argument('--query', default='')
    command = sub.add_parser('search')
    command.add_argument('--country', required=True)
    command.add_argument('--product', required=True)
    command.add_argument('--query', default='')
    command.add_argument('--task-type')
    command.add_argument('--as-of')
    args = parser.parse_args()
    workbench = Workbench(args.root)
    try:
        if args.command == 'init':
            result = workbench.initialize()
        elif args.command == 'index':
            result = workbench.index()
        elif args.command == 'maintenance':
            result = workbench.maintenance()
        elif args.command == 'init-project':
            result = workbench.init_project(args.project_root)
        elif args.command == 'new-analysis':
            result = workbench.new_analysis(args.project_root, read(args.spec), args.thread_id)
        elif args.command == 'link-thread':
            result = workbench.link_thread(args.project_root, args.analysis_id, args.thread_id)
        elif args.command == 'archive-draft':
            result = workbench.draft_archive(args.project_root, args.analysis_id, read(args.spec))
        elif args.command == 'finalize-archive':
            result = workbench.finalize_archive(args.project_root, args.analysis_id, args.reviewer, args.evidence)
        elif args.command == 'search-projects':
            result = workbench.search_projects(args.query)
        else:
            result = workbench.search(args.country, args.product, args.query, task_type=args.task_type, as_of=args.as_of)
        print(json.dumps(result or {'ok': True}, ensure_ascii=False, indent=2))
        return 0
    except (GateError, OSError, ValueError, KeyError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
