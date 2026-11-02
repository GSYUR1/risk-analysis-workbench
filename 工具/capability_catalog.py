"""读取功能清单并生成可重建的索引，不导入或执行功能代码。"""

import hashlib
import json
import re
from pathlib import Path


REFERENCE_FIELDS = ('documents', 'rules', 'templates', 'code', 'tests')
STATUSES = {'planned', 'partial', 'available', 'workflow'}


def _local_file(root, relative):
    if not isinstance(relative, str) or not relative:
        raise ValueError('功能引用必须是非空相对路径')
    path = Path(relative)
    target = (root / path).resolve()
    if path.is_absolute() or not target.is_relative_to(root) or not target.is_file():
        raise ValueError(f'功能引用缺失或越界: {relative}')
    return target


def build_catalog(root, write=False):
    root = Path(root).resolve()
    manifests = sorted((root / '分析功能').glob('*/capability.json'))
    if not manifests:
        raise ValueError('没有功能清单；不能把空目录视为已有能力')
    capabilities = []
    identifiers = set()
    for manifest in manifests:
        capability = json.loads(manifest.read_text(encoding='utf-8-sig'))
        identifier = capability.get('id', '')
        if not re.fullmatch(r'[a-z][a-z0-9_]*', identifier) or identifier in identifiers:
            raise ValueError(f'功能ID无效或重复: {identifier}')
        identifiers.add(identifier)
        if capability.get('schema_version') != 1 or capability.get('status') not in STATUSES:
            raise ValueError(f'功能版本或状态无效: {identifier}')
        for field in ('name', 'version', 'execution', 'validation'):
            if not isinstance(capability.get(field), str) or not capability[field]:
                raise ValueError(f'{identifier} 缺少 {field}')
        references = [capability.get('entry')]
        for field in REFERENCE_FIELDS + ('requires', 'limitations'):
            values = capability.get(field)
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ValueError(f'{identifier}.{field} 必须是字符串列表')
            if field in REFERENCE_FIELDS:
                references.extend(values)
        if capability['status'] == 'available' and not (capability['code'] and capability['tests']):
            raise ValueError(f'{identifier} 可运行状态缺少实现或测试')
        if capability['status'] == 'planned' and capability['code']:
            raise ValueError(f'{identifier} 待实施能力不得登记代码入口')
        hashes = {}
        for relative in references:
            target = _local_file(root, relative)
            hashes[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
            if target.suffix == '.md':
                for link in re.findall(r'\]\(([^)]+)\)', target.read_text(encoding='utf-8-sig')):
                    if '://' not in link and not link.startswith('#'):
                        linked = (target.parent / link.split('#')[0]).resolve()
                        if not linked.is_relative_to(root) or not linked.is_file():
                            raise ValueError(f'{relative} 的文档链接缺失或越界: {link}')
        capability['manifest'] = manifest.relative_to(root).as_posix()
        hashes[capability['manifest']] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        capability['file_hashes'] = hashes
        capabilities.append(capability)
    graph = {item['id']: item['requires'] for item in capabilities}
    visited = set()

    def visit(identifier, pending):
        if identifier not in graph:
            raise ValueError(f'未知功能依赖: {identifier}')
        if identifier in pending:
            raise ValueError(f'循环功能依赖: {identifier}')
        if identifier not in visited:
            for dependency in graph[identifier]:
                visit(dependency, pending | {identifier})
            visited.add(identifier)

    for identifier in graph:
        visit(identifier, set())
    result = {'schema_version': 1, 'generated': True, 'capabilities': capabilities}
    if write:
        directory = root / '索引'
        directory.mkdir(exist_ok=True)
        temporary = directory / 'capabilities.json.tmp'
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(directory / 'capabilities.json')
    return result
