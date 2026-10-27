"""Generate grouped pivot-base SQL and read-only checks without remote execution."""

import argparse
import hashlib
import json
import re
from pathlib import Path


DEFAULT_PROFILE = Path(__file__).resolve().parents[1] / '工作约定/数据口径/ph/pivot_base_profile.json'


def identifier(value, qualified=False):
    pattern = r'[A-Za-z_][A-Za-z0-9_]*'
    if qualified:
        pattern += r'(?:\.[A-Za-z_][A-Za-z0-9_]*)?'
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f'Invalid SQL identifier: {value!r}')
    return value


def fragment(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('SQL expression must be nonempty')
    if any(token in value for token in (';', '--', '/*', '*/')):
        raise ValueError('SQL fragments must not contain statements or comments')
    return value.strip()


def condition(filters):
    if not isinstance(filters, list):
        raise ValueError('filters must be a list of SQL predicates')
    return ' AND '.join(f'({fragment(item)})' for item in filters) or '1 = 1'


def unique_names(names, label):
    for name in names:
        identifier(name)
    if len({name.lower() for name in names}) != len(names):
        raise ValueError(f'Duplicate {label}')


def build_pivot_base_sql(spec, profile_path=DEFAULT_PROFILE):
    profile_path = Path(profile_path)
    profile_bytes = profile_path.read_bytes()
    profile = json.loads(profile_bytes.decode('utf-8-sig'))
    metrics = profile['metrics']
    unique_names(metrics, 'metrics')
    source_kind = spec.get('source_kind')
    if source_kind not in ('indicator', 'detail'):
        raise ValueError('source_kind must explicitly be indicator or detail')
    source_table = identifier(spec.get('source_table', profile[f'{source_kind}_table']), True)
    if source_table == profile['detail_table'] and source_kind != 'detail':
        raise ValueError('order_trace requires detail expressions, not SUM(indicator columns)')
    filters = spec.get('filters', [])
    if not filters and spec.get('allow_all_dates') is not True:
        raise ValueError('Provide confirmed filters or explicitly set allow_all_dates=true')
    dimensions = spec.get('dimensions', profile['default_dimensions'])
    if not isinstance(dimensions, list):
        raise ValueError('dimensions must be an ordered list')
    dimension_items = []
    for item in dimensions:
        if isinstance(item, str):
            name = identifier(item)
            expression = f'b.{name}'
        else:
            name = identifier(item['name'])
            expression = fragment(item['expression'])
        dimension_items.append((name, expression))
    dimension_names = [name for name, _ in dimension_items]
    unique_names(dimension_names + metrics, 'output columns')
    expressions = spec.get('metric_expressions', {})
    if source_kind == 'indicator':
        if expressions:
            raise ValueError('indicator mode preserves SUM for every metric; do not override formulas')
        expressions = {name: f'SUM(b.{name})' for name in metrics}
    else:
        missing = [name for name in metrics if name not in expressions]
        extra = sorted(set(expressions) - set(metrics))
        if missing or extra:
            raise ValueError(f'detail requires every confirmed metric expression; missing={missing}, extra={extra}')
        if not spec.get('metric_expression_evidence'):
            raise ValueError('detail requires metric_expression_evidence for formula provenance')
        expressions = {name: fragment(expressions[name]) for name in metrics}

    base = f'base AS (\n    SELECT *\n    FROM {source_table}\n    WHERE {condition(filters)}\n)'
    ctes = [base]
    joins = []
    checks = []
    aliases = {'b', 'base', 'enriched'}
    grain_keys = spec.get('grain_keys', profile['grain_keys'])
    if not isinstance(grain_keys, list) or not grain_keys:
        raise ValueError('grain_keys must be a nonempty list')
    unique_names(grain_keys, 'grain keys')
    key_sql = ', '.join(grain_keys)
    null_keys = ' OR '.join(f'{key} IS NULL' for key in grain_keys)
    checks.append(('base_null_keys', f'WITH {base}\nSELECT COUNT(*) AS null_key_rows FROM base WHERE {null_keys};'))
    checks.append(('base_duplicate_keys', f'WITH {base}\nSELECT {key_sql}, COUNT(*) AS row_cnt FROM base GROUP BY {key_sql} HAVING COUNT(*) > 1;'))
    coverage = []
    for join in spec.get('joins', []):
        alias = identifier(join['alias'])
        if alias.lower() in aliases or alias.lower().startswith('__pivot_'):
            raise ValueError(f'Reserved or repeated join alias: {alias}')
        aliases.add(alias.lower())
        table = identifier(join['table'], True)
        keys = join['keys']
        if not isinstance(keys, dict) or not keys:
            raise ValueError('joins.keys must map base keys to dimension keys')
        unique_names(list(keys), 'base join keys')
        unique_names(list(keys.values()), 'dimension join keys')
        dim_cte = f'__pivot_{alias}'
        ctes.append(f'{dim_cte} AS (\n    SELECT * FROM {table}\n    WHERE {condition(join.get("filters", []))}\n)')
        on_clause = ' AND '.join(f'b.{left} = {alias}.{right}' for left, right in keys.items())
        joins.append(f'LEFT JOIN {dim_cte} {alias} ON {on_clause}')
        right_keys = ', '.join(keys.values())
        nonnull = ' AND '.join(f'{key} IS NOT NULL' for key in keys.values())
        checks.append((f'{alias}_duplicate_keys', f'WITH {ctes[-1]}\nSELECT {right_keys}, COUNT(*) AS row_cnt FROM {dim_cte} WHERE {nonnull} GROUP BY {right_keys} HAVING COUNT(*) > 1;'))
        marker = next(iter(keys.values()))
        coverage.append(f'SUM(CASE WHEN {alias}.{marker} IS NULL THEN 1 ELSE 0 END) AS {alias}_unmatched_rows')

    dimension_projection = [f'{expression} AS __pivot_dim_{index}' for index, (_, expression) in enumerate(dimension_items)]
    selection = ',\n        '.join(['b.*'] + dimension_projection)
    join_sql = '\n    '.join(joins)
    ctes.append(f'enriched AS (\n    SELECT {selection}\n    FROM base b\n    {join_sql}\n)')
    prefix = 'WITH ' + ',\n'.join(ctes)
    select_columns = [f'b.__pivot_dim_{index} AS {name}' for index, (name, _) in enumerate(dimension_items)]
    select_columns += [f'{expressions[name]} AS {name}' for name in metrics]
    group_by = ''
    if dimension_items:
        group_by = '\nGROUP BY ' + ', '.join(f'b.__pivot_dim_{index}' for index in range(len(dimension_items)))
    sql = prefix + '\nSELECT\n    ' + ',\n    '.join(select_columns) + '\nFROM enriched b' + group_by + ';\n'
    counts = f'{prefix}\nSELECT \'before\' AS stage, COUNT(*) AS row_cnt FROM base\nUNION ALL\nSELECT \'after\' AS stage, COUNT(*) AS row_cnt FROM enriched;'
    checks.append(('join_row_counts', counts))
    if coverage:
        checks.append(('join_coverage', 'WITH ' + ',\n'.join(ctes[:-1]) + '\nSELECT COUNT(*) AS joined_rows,\n    ' + ',\n    '.join(coverage) + '\nFROM base b\n' + '\n'.join(joins) + ';'))
    source_columns = spec.get('source_columns')
    if source_columns is not None:
        if not isinstance(source_columns, list):
            raise ValueError('source_columns must be a list from a verified schema')
        required = set(grain_keys)
        if source_kind == 'indicator':
            required.update(metrics)
        for _, expression in dimension_items:
            required.update(re.findall(r'\bb\.([A-Za-z_][A-Za-z0-9_]*)', expression))
        for expression in expressions.values():
            required.update(re.findall(r'\bb\.([A-Za-z_][A-Za-z0-9_]*)', expression))
        for join in spec.get('joins', []):
            required.update(join['keys'])
        missing = sorted(required - set(source_columns))
        if missing:
            raise ValueError(f'Source schema missing fields: {missing}')
    return {
        'sql': sql,
        'checks': checks,
        'metadata': {
            'profile_id': profile['profile_id'],
            'profile_sha256': hashlib.sha256(profile_bytes).hexdigest(),
            'spec_sha256': hashlib.sha256(json.dumps(spec, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest(),
            'source_table': source_table,
            'source_kind': source_kind,
            'output_columns': dimension_names + metrics,
            'dimension_count': len(dimension_names),
            'metric_count': len(metrics),
            'schema_supplied': source_columns is not None,
            'remote_validated': False,
            'execution_status': 'not_executed',
            'metric_expression_evidence': spec.get('metric_expression_evidence'),
            'checks': [name for name, _ in checks],
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--profile', type=Path, default=DEFAULT_PROFILE)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text(encoding='utf-8-sig'))
    result = build_pivot_base_sql(spec, args.profile)
    paths = [args.output, args.output.with_suffix('.checks.sql'), args.output.with_suffix('.manifest.json')]
    if len(set(paths)) != 3 or any(path.exists() for path in paths):
        raise FileExistsError('Choose a new output filename; SQL, checks and manifest must not overwrite existing evidence')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    checks_sql = '\n\n'.join(f'-- {name}\n{sql}' for name, sql in result['checks']) + '\n'
    payloads = [result['sql'], checks_sql, json.dumps(result['metadata'], ensure_ascii=False, indent=2) + '\n']
    for path, payload in zip(paths, payloads):
        with path.open('x', encoding='utf-8') as output:
            output.write(payload)
    print(json.dumps({'files': [str(path.resolve()) for path in paths], **result['metadata']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
