"""风控分析工作台状态引擎：事项、项目、归档和知识索引。"""

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


class GateError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def file_hash(path):
    hasher = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(block)
    return hasher.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, value, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if exclusive:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(text)
        return
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    os.replace(temporary, path)


def identifier(prefix):
    return prefix + '-' + datetime.now().strftime('%Y%m%d') + '-' + uuid.uuid4().hex[:12]


def inside(root, relative):
    relative = str(relative)
    if not relative or '\\' in relative or ':' in relative or relative.startswith('/') or '..' in Path(relative).parts:
        raise GateError('必须提供安全的相对路径')
    root = Path(root).resolve()
    result = root / relative
    cursor = root
    for part in Path(relative).parts:
        cursor = cursor / part
        if cursor.is_symlink() or (hasattr(cursor, 'is_junction') and cursor.is_junction()):
            raise GateError('不接受链接路径')
    if not result.resolve().is_relative_to(root):
        raise GateError('路径越界')
    return result


def required(value, fields):
    missing = [field for field in fields if field not in value or value[field] in (None, '', [], {})]
    if missing:
        raise GateError('缺少字段: ' + ', '.join(missing))


def safe_text(text, allow_identifiers=False):
    patterns = (r'(?i)(?:access[_-]?key|secret[_-]?key|api[_-]?key|password)[\s"\x27]*[=:]\s*["\x27]?[^\s"\x27]{6,}',
                r'\bLTAI[A-Za-z0-9]{12,}\b', r'-----BEGIN .*PRIVATE KEY-----', r'\b1[3-9]\d{9}\b',
                r'\b\d{17}[\dXx]\b')
    if not allow_identifiers:
        patterns += (r'(?i)\b(id_card_number|mobile_number|phone_number|cust_no)\b',)
    if any(re.search(pattern, text) for pattern in patterns):
        raise GateError('疑似敏感信息；禁止回传，需人工脱敏')


def clean_notebook(notebook):
    for cell in notebook.get('cells', []):
        if cell.get('outputs') or cell.get('execution_count') is not None or cell.get('attachments'):
            raise GateError('Notebook 必须清除输出、执行计数和附件')
    if notebook.get('metadata', {}).get('widgets'):
        raise GateError('Notebook 含 widget 状态')
    safe_text(json.dumps(notebook, ensure_ascii=False), allow_identifiers=True)


class Workbench:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def initialize(self):
        import sys
        for folder in ('工作约定', '知识库/变更', '知识库/正式', '知识库/历史', '分析模板', '索引'):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        config = self.root / '工作约定/config.json'
        if not config.exists():
            write(config, {'schema_version': 1, 'external_enabled': False, 'ssh_hosts': {},
                           'odps_python': sys.executable,
                           'odps_skill': str(Path.home() / '.codex/skills/odps-query'),
                           'approved_profiles': [], 'knowledge_limit': 5,
                           'backup_status': '未配置', 'maintenance_reviewed_count': 0})
        self.index()
        return {'ok': True, 'root': str(self.root)}

    def project_paths(self, project_root):
        project_root = Path(project_root).expanduser().resolve()
        if not project_root.is_dir():
            raise GateError('项目根目录不存在或不是目录')
        return project_root, project_root / 'risk-analysis'

    def init_project(self, project_root):
        project_root, analysis_root = self.project_paths(project_root)
        project_id = project_root.name
        if not (analysis_root / 'project.json').exists():
            write(analysis_root / 'project.json', {'schema_version': 1, 'project_id': project_id,
                'project_root': str(project_root), 'created_at': now()}, True)
        if not (analysis_root / 'analysis-index.json').exists():
            write(analysis_root / 'analysis-index.json', {'schema_version': 1, 'analyses': []}, True)
        thread_index = analysis_root / 'threads-index.json'
        if not thread_index.exists():
            write(thread_index, {'schema_version': 1, 'threads': []}, True)
        return {'project_id': project_id, 'project_root': str(project_root), 'path': str(analysis_root)}

    def _project_state(self, project_root):
        project_root, analysis_root = self.project_paths(project_root)
        files = [analysis_root / 'project.json', analysis_root / 'analysis-index.json', analysis_root / 'threads-index.json']
        if not all(path.exists() for path in files):
            raise GateError('项目尚未初始化，请先执行 init-project')
        return project_root, analysis_root, read(files[0]), read(files[1]), read(files[2])

    def new_analysis(self, project_root, specification, thread_id=''):
        required(specification, ('title', 'goal', 'country', 'product', 'task_type'))
        safe_text(json.dumps(specification, ensure_ascii=False))
        if thread_id and any(char in thread_id for char in '\\/:'):
            raise GateError('thread_id 不能包含路径分隔符')
        self.init_project(project_root)
        project_root, analysis_root, project, state, _ = self._project_state(project_root)
        analysis_id = identifier('ANA')
        title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', specification['title']).strip(' .')[:60] or 'analysis'
        directory_name = datetime.now().strftime('%Y%m%d') + '_' + title + '_' + analysis_id.rsplit('-', 1)[1]
        analysis_path = 'risk-analysis/analyses/' + directory_name
        analysis_directory = inside(project_root, analysis_path)
        entry = {**specification, 'analysis_id': analysis_id, 'project_id': project['project_id'],
                 'thread_ids': [], 'status': 'in_progress', 'created_at': now(), 'updated_at': now(),
                 'analysis_path': analysis_path, 'layout_version': 3}
        write(analysis_directory / 'analysis.json', entry, True)
        state['analyses'].append(entry)
        write(analysis_root / 'analysis-index.json', state)
        if thread_id:
            self.link_thread(project_root, analysis_id, thread_id)
        return {'analysis_id': analysis_id, 'project_id': project['project_id'],
                'path': str(analysis_directory), 'analysis_path': analysis_path}

    def link_thread(self, project_root, analysis_id, thread_id):
        if not thread_id or any(char in thread_id for char in '\\/:'):
            raise GateError('thread_id 不能为空且不能包含路径分隔符')
        project_root, analysis_root, _, state, threads = self._project_state(project_root)
        analysis = next((item for item in state['analyses'] if item['analysis_id'] == analysis_id), None)
        if analysis is None:
            raise GateError('analysis_id 不存在')
        if thread_id not in analysis['thread_ids']:
            analysis['thread_ids'].append(thread_id)
        thread = next((item for item in threads['threads'] if item['thread_id'] == thread_id), None)
        if thread is None:
            thread = {'thread_id': thread_id, 'analysis_ids': []}
            threads['threads'].append(thread)
        if analysis_id not in thread['analysis_ids']:
            thread['analysis_ids'].append(analysis_id)
        analysis['updated_at'] = thread['updated_at'] = now()
        write(analysis_root / 'analysis-index.json', state)
        write(analysis_root / 'threads-index.json', threads)
        if analysis.get('analysis_path'):
            write(inside(project_root, analysis['analysis_path']) / 'analysis.json', analysis)
        return {'analysis_id': analysis_id, 'thread_id': thread_id, 'thread_count': len(analysis['thread_ids'])}

    @staticmethod
    def _archive_directory(project_root, analysis_root, analysis):
        if analysis.get('analysis_path'):
            return inside(project_root, analysis['analysis_path']) / 'review'
        return inside(analysis_root, 'archives/' + analysis['analysis_id'])

    def draft_archive(self, project_root, analysis_id, summary):
        project_root, analysis_root, project, state, _ = self._project_state(project_root)
        analysis = next((item for item in state['analyses'] if item['analysis_id'] == analysis_id), None)
        if analysis is None:
            raise GateError('analysis_id 不存在')
        required(summary, ('data_sources', 'metrics', 'method', 'conclusions', 'deliverables', 'lessons'))
        safe_text(json.dumps(summary, ensure_ascii=False))
        files = []
        for relative in summary.get('files', []):
            path = inside(project_root, relative)
            if not path.is_file():
                raise GateError('归档文件不存在: ' + str(relative))
            files.append({'path': str(relative), 'sha256': file_hash(path), 'size': path.stat().st_size})
        archive = {'schema_version': 1, 'status': 'draft', 'project_id': project['project_id'],
                   'analysis_id': analysis_id, 'thread_ids': analysis['thread_ids'], 'analysis': analysis,
                   **{key: value for key, value in summary.items() if key != 'files'}, 'files': files, 'created_at': now()}
        destination = self._archive_directory(project_root, analysis_root, analysis)
        destination.mkdir(parents=True, exist_ok=True)
        write(destination / 'archive.json', archive)
        (destination / 'archive.md').write_text(self._archive_markdown(archive), encoding='utf-8')
        return {'analysis_id': analysis_id, 'status': 'draft', 'path': str(destination)}

    @staticmethod
    def _archive_markdown(archive):
        def render(value):
            if isinstance(value, list):
                return '\n'.join('- ' + str(item) for item in value) or '- 无'
            if isinstance(value, dict):
                return '\n'.join('- **' + str(key) + '**：' + str(item) for key, item in value.items()) or '- 无'
            return str(value or '无')
        files = render([item['path'] + ' sha256=' + item['sha256'] for item in archive['files']])
        return (f'# 分析事项归档\n\n- 项目：`{archive["project_id"]}`\n- 分析事项：`{archive["analysis_id"]}`\n'
                f'- 关联窗口：\n{render(archive["thread_ids"])}\n\n'
                f'## 数据与口径\n{render(archive["data_sources"])}\n\n'
                f'## 指标与分析方法\n{render(archive["metrics"])}\n{render(archive["method"])}\n\n'
                f'## 主要结论\n{render(archive["conclusions"])}\n\n'
                f'## 交付物\n{render(archive["deliverables"])}\n\n'
                f'## 可复用经验\n{render(archive["lessons"])}\n\n## 文件清单\n{files}\n')

    def finalize_archive(self, project_root, analysis_id, reviewer, evidence):
        if not reviewer or not evidence:
            raise GateError('正式归档必须提供审核人和审核证据')
        project_root, analysis_root, project, state, _ = self._project_state(project_root)
        analysis = next((item for item in state['analyses'] if item['analysis_id'] == analysis_id), None)
        if analysis is None:
            raise GateError('analysis_id 不存在')
        source = self._archive_directory(project_root, analysis_root, analysis)
        archive = read(source / 'archive.json')
        if archive.get('status') != 'draft':
            raise GateError('归档不是待审核草稿')
        archive.update(status='approved', reviewer=reviewer, review_evidence=evidence, reviewed_at=now())
        write(source / 'archive.json', archive)
        (source / 'archive.md').write_text(self._archive_markdown(archive), encoding='utf-8')
        destination = self.root / '项目归档' / project['project_id'] / 'analyses' / analysis_id
        destination.mkdir(parents=True, exist_ok=True)
        write(destination / 'archive.json', archive, True)
        (destination / 'archive.md').write_text((source / 'archive.md').read_text(encoding='utf-8'), encoding='utf-8')
        project_index = destination.parent.parent / 'project-index.json'
        existing = read(project_index) if project_index.exists() else {'schema_version': 1, 'project_id': project['project_id'], 'analyses': []}
        existing['analyses'] = [item for item in existing['analyses'] if item['analysis_id'] != analysis_id]
        existing['analyses'].append({'analysis_id': analysis_id, 'title': archive['analysis'].get('title'),
            'status': archive['status'], 'path': str(destination.relative_to(self.root))})
        existing['updated_at'] = now()
        write(project_index, existing)
        return {'project_id': project['project_id'], 'analysis_id': analysis_id, 'status': 'approved', 'path': str(destination)}

    def search_projects(self, query=''):
        results = []
        for path in (self.root / '项目归档').glob('*/project-index.json'):
            index = read(path)
            for item in index.get('analyses', []):
                archive_path = self.root / item['path'] / 'archive.json'
                archive = read(archive_path) if archive_path.exists() else item
                if not query or query.casefold() in json.dumps(archive, ensure_ascii=False).casefold():
                    results.append({**item, 'project_id': index['project_id']})
        return results

    def index(self):
        entries = []
        for folder in ('变更', '正式', '历史'):
            for path in (self.root / '知识库' / folder).glob('*.json'):
                entry = read(path)
                entries.append({**entry, '_location': folder})
        projects = self.search_projects()
        catalog = {'knowledge': entries, 'projects': projects, 'updated_at': now()}
        write(self.root / '索引/catalog.json', catalog)
        return catalog

    def search(self, country, product, query='', task_type=None, as_of=None):
        self.index()
        today = as_of or datetime.now().date().isoformat()
        matches = []
        for path in (self.root / '知识库/正式').glob('*.json'):
            entry = read(path)
            if (entry.get('country') == country and entry.get('product') == product
                    and entry.get('status') == 'approved'
                    and entry.get('valid_from', '0000-00-00') <= today
                    and (not entry.get('valid_until') or today <= entry['valid_until'])
                    and (not task_type or entry.get('task_type') in (task_type, '*'))
                    and (not query or query.casefold() in (entry.get('topic', '') + entry.get('summary', '')).casefold())):
                matches.append(entry)
        matches.sort(key=lambda entry: entry.get('last_verified', ''), reverse=True)
        config = read(self.root / '工作约定/config.json')
        limit = min(10, max(1, config.get('knowledge_limit', 5)))
        return matches[:limit]

    def maintenance(self):
        catalog = self.index()
        groups = {}
        for entry in catalog['knowledge']:
            key = (entry.get('country'), entry.get('product'), entry.get('topic', '').strip().casefold())
            groups.setdefault(key, []).append(entry)
        candidates = []
        for key, entries in groups.items():
            if len(entries) > 1:
                candidates.append({'country': key[0], 'product': key[1], 'topic': key[2],
                                   'locations': [entry.get('_location') for entry in entries],
                                   'note': '仅生成整理建议，需人工核实后处理'})
        today = datetime.now().date().isoformat()
        expired = [entry for entry in catalog['knowledge'] if entry.get('valid_until') and entry['valid_until'] < today]
        result = {'generated_at': now(), 'knowledge_count': len(catalog['knowledge']),
                  'duplicate_or_expired_candidates': candidates, 'expired': expired,
                  'action': '仅生成整理建议，不合并、不废弃；审核后记录维护检查点'}
        write(self.root / '索引/maintenance.json', result)
        return result
