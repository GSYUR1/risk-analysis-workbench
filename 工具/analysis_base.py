"""Persist a reusable analysis base and reproduce local supplementary pivots."""

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


TOOL_DIR = Path(__file__).resolve().parent
ENGINE_FILES = ('analysis_base.py', 'ph_data_analysis.py', 'excel_export.py')


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def _names(value, label):
    if not isinstance(value, list) or not value or any(not isinstance(name, str) or not name for name in value):
        raise ValueError(f'{label} must be a nonempty list of column names')
    if len(set(value)) != len(value):
        raise ValueError(f'{label} contains duplicates')
    return value


def _validate_base(frame, contract):
    for key in ('base_id', 'sample', 'time_window', 'snapshot', 'grain', 'definition_evidence', 'retention', 'storage_authorization'):
        if not contract.get(key):
            raise ValueError(f'Missing contract field: {key}')
    if frame.empty or not frame.columns.is_unique or any(not isinstance(name, str) for name in frame.columns):
        raise ValueError('Base must have rows and unique string column names')
    dimensions = _names(contract.get('dimensions'), 'dimensions')
    grain_keys = _names(contract.get('grain_keys'), 'grain_keys')
    required = set(dimensions + grain_keys + _names(contract.get('required_columns'), 'required_columns'))
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f'Missing columns: {sorted(missing)}')
    if frame.duplicated(grain_keys).any():
        raise ValueError('Duplicate grain keys; do not hide joined or mixed-level rows')
    for dimension in dimensions:
        if frame[dimension].isin(['All', '__MISSING__']).any():
            raise ValueError(f'Reserved dimension label in {dimension}: All / __MISSING__')
    if contract.get('kind') == 'grouped':
        if set(grain_keys) != set(dimensions):
            raise ValueError('Grouped base grain_keys must equal dimensions; no mixed grouping sets')
        components = _names(contract.get('additive_columns'), 'additive_columns')
        if set(components) & set(dimensions):
            raise ValueError('Additive components cannot also be dimensions')
        evidence = contract.get('additivity_evidence', {})
        if not isinstance(evidence, dict) or any(not evidence.get(name) for name in components):
            raise ValueError('Each additive column needs evidence, especially counts of distinct people')
        for name in components:
            if name not in frame or not pd.api.types.is_numeric_dtype(frame[name]):
                raise ValueError(f'Missing or non-numeric component: {name}')
        if not contract.get('metrics'):
            raise ValueError('Grouped base needs explicit metric definitions')
        for name, definition in contract['metrics'].items():
            operation = definition.get('op')
            if operation == 'sum':
                used = [definition.get('column')]
            elif operation == 'ratio':
                used = [definition.get('numerator'), definition.get('denominator')]
            else:
                raise ValueError(f'{name}: only additive sum / ratio are supported; distinct counts are not summable by default')
            if any(column not in components for column in used) or not definition.get('evidence'):
                raise ValueError(f'{name}: components must be declared additive and definition evidence is required')
    elif contract.get('kind') == 'detail':
        options = contract.get('pivot_options', {})
        id_col = options.get('id_col')
        if id_col not in frame or frame[id_col].isna().any() or frame[id_col].duplicated().any():
            raise ValueError('Detail ph pivot requires a non-null unique id_col at application/order grain')
        for key in ('stage', 'risk_ind', 'indicator', 'lift_base'):
            if key not in options:
                raise ValueError(f'Detail pivot_options must explicitly specify {key}')
        _names(options['indicator'], 'indicator')
        if not isinstance(options['risk_ind'], list):
            raise ValueError('risk_ind must be an explicit list (empty means no risk metrics)')
        for column in ('cre_cnt', 'apprv_cnt', 'is_order_fz', 'payout_cnt'):
            if column in frame and not frame[column].dropna().isin([0, 1]).all():
                raise ValueError(f'{column} is aggregated; cannot feed grouped counts to detail generate_pivot')
    else:
        raise ValueError('kind must be grouped or detail')


def save_base(frame, directory, contract, sql_paths, supporting_paths=()):
    """Save an already-authorized DataFrame in its execution environment; never query a service."""
    _validate_base(frame, contract)
    target = Path(directory).resolve()
    if target.exists():
        raise FileExistsError(target)
    sql_paths = [Path(path).resolve() for path in sql_paths]
    supporting_paths = [Path(path).resolve() for path in supporting_paths]
    if not sql_paths or any(not path.is_file() or path.suffix.lower() != '.sql' for path in sql_paths):
        raise ValueError('At least one existing extraction SQL file is required')
    if any(not path.is_file() for path in supporting_paths):
        raise ValueError('Missing preprocessing / binning evidence file')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.base-', dir=target.parent) as temporary:
        staging = Path(temporary) / 'bundle'
        staging.mkdir()
        frame.to_parquet(staging / 'base.parquet', index=False, engine='pyarrow')
        files = {'base.parquet': file_hash(staging / 'base.parquet')}
        for category, paths in (('sql', sql_paths), ('support', supporting_paths)):
            for position, source in enumerate(paths):
                relative = f'{category}/{position:02d}_{source.name}'
                destination = staging / relative
                destination.parent.mkdir(exist_ok=True)
                shutil.copyfile(source, destination)
                files[relative] = file_hash(destination)
        manifest = {
            'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
            'contract': contract, 'rows': len(frame),
            'columns': {name: str(dtype) for name, dtype in frame.dtypes.items()},
            'files': files, 'tools': {name: file_hash(TOOL_DIR / name) for name in ENGINE_FILES},
            'runtime': {'python': sys.version.split()[0], 'pandas': pd.__version__},
        }
        write_json(staging / 'manifest.json', manifest)
        dimensions = contract['dimensions']
        views = [] if len(dimensions) < 2 else [{'name': 'supplement', 'ind': [dimensions[0]], 'col': [dimensions[1]]}]
        write_json(staging / 'views.example.json', {
            'base_manifest_sha256': file_hash(staging / 'manifest.json'), 'views': views,
        })
        launcher = (
            'import argparse\nimport sys\nfrom pathlib import Path\n\n'
            'parser = argparse.ArgumentParser()\n'
            f'parser.add_argument("--tools-dir", default={str(TOOL_DIR)!r})\n'
            'parser.add_argument("--views", required=True)\n'
            'parser.add_argument("--output-dir", required=True)\n'
            'args = parser.parse_args()\n'
            'sys.path.insert(0, args.tools_dir)\n'
            'from analysis_base import run_views\n'
            'run_views(Path(__file__).resolve().parent, args.views, args.output_dir)\n'
        )
        (staging / 'supplement.py').write_text(launcher, encoding='utf-8')
        staging.rename(target)
    return target / 'manifest.json'


def load_base(directory):
    """Load a validated snapshot for CLI or notebook use; no remote fallback."""
    root = Path(directory).resolve()
    manifest = read_json(root / 'manifest.json')
    if manifest.get('schema_version') != 1 or 'base.parquet' not in manifest['files']:
        raise ValueError('Unsupported or incomplete manifest')
    for relative, expected in manifest['files'].items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or file_hash(path) != expected:
            raise ValueError(f'Base evidence changed or invalid path: {relative}')
    frame = pd.read_parquet(root / 'base.parquet', engine='pyarrow')
    if len(frame) != manifest['rows'] or {name: str(dtype) for name, dtype in frame.dtypes.items()} != manifest['columns']:
        raise ValueError('Base schema / row count changed')
    _validate_base(frame, manifest['contract'])
    return frame, manifest


def build_views(frame, contract, views):
    from ph_data_analysis import generate_grouped_pivot, generate_pivot

    _validate_base(frame, contract)
    if not isinstance(views, list) or not views:
        raise ValueError('At least one explicit cross view is required')
    results = {}
    for view in views:
        if set(view) - {'name', 'ind', 'col', 'filters', 'metrics'}:
            raise ValueError('Views may only change axes, equality filters and metric subset, not the base definition')
        title = view.get('name')
        if not isinstance(title, str) or not title.strip() or title in results:
            raise ValueError('View names must be nonempty and unique')
        rows = _names(view.get('ind'), 'ind')
        columns = _names(view.get('col'), 'col')
        if set(rows) & set(columns) or not set(rows + columns) <= set(contract['dimensions']):
            raise ValueError('Axes overlap or dimension absent from the frozen base; extend base explicitly, never auto-query')
        selected = frame.copy()
        for dimension, values in view.get('filters', {}).items():
            if dimension not in contract['dimensions'] or not isinstance(values, list):
                raise ValueError('Filters must map saved dimensions to lists of exact values')
            mask = selected[dimension].isin([value for value in values if value is not None])
            if None in values:
                mask |= selected[dimension].isna()
            selected = selected.loc[mask]
        if selected.empty:
            raise ValueError(f'{title}: empty filtered sample')
        for dimension in rows + columns:
            selected[dimension] = selected[dimension].astype(object).where(selected[dimension].notna(), '__MISSING__')
        if contract['kind'] == 'grouped':
            metric_names = view.get('metrics', list(contract['metrics']))
            _names(metric_names, 'metrics')
            if not set(metric_names) <= set(contract['metrics']):
                raise ValueError('Unknown grouped metric')
            metrics = {name: contract['metrics'][name] for name in metric_names}
            result = generate_grouped_pivot(selected, rows, columns, metrics)
        else:
            options = dict(contract['pivot_options'])
            if view.get('metrics') is not None:
                _names(view['metrics'], 'metrics')
                if not set(view['metrics']) <= set(options['indicator']):
                    raise ValueError('Unknown detail metric')
                options['indicator'] = view['metrics']
            result = generate_pivot(selected, ind=rows, col=columns, margins=True, show=False, **options)
            missing = set(options['indicator']) - set(result.columns.get_level_values(0))
            if missing:
                raise ValueError(f'Metrics unavailable from saved columns: {sorted(missing)}')
            for mob in options['risk_ind']:
                from ph_data_analysis import _filter_pivot_stage, _normalize_ph_base

                normalized = _filter_pivot_stage(_normalize_ph_base(selected), options['stage'])
                components = {'amount': f'expire_amt_{mob}', 'count': f'expire_cnt_{mob}'}
                if any(column not in normalized for column in components.values()):
                    raise ValueError(f'Missing maturity components for {mob}')
                denominators = generate_grouped_pivot(normalized, rows, columns, {
                    name: {'op': 'sum', 'column': column} for name, column in components.items()
                })
                for suffix, component in (('', 'amount'), ('cnt', 'count'), ('lift', 'amount')):
                    metric = f'dpd{mob}{suffix}'
                    if metric in result.columns.get_level_values(0):
                        result[metric] = result[metric].where(denominators[component].gt(0))
        results[title] = result
    return results


def run_views(directory, views_path, output_directory):
    from excel_export import merge_formatted_pivot_to_excel

    root = Path(directory).resolve()
    target = Path(output_directory).resolve()
    if target.exists():
        raise FileExistsError(target)
    request = read_json(views_path)
    if request.get('base_manifest_sha256') != file_hash(root / 'manifest.json'):
        raise ValueError('View request points to a different base definition / manifest')
    frame, manifest = load_base(root)
    for name in ENGINE_FILES:
        if manifest['tools'].get(name) != file_hash(TOOL_DIR / name):
            raise ValueError(f'Tool version changed: {name}; review and create a new base version')
    results = build_views(frame, manifest['contract'], request['views'])
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.cross-', dir=target.parent) as temporary:
        staging = Path(temporary) / 'run'
        staging.mkdir()
        write_json(staging / 'views.json', request)
        for position, result in enumerate(results.values()):
            result.to_csv(staging / f'cross_{position:02d}.csv', encoding='utf-8-sig')
        merge_formatted_pivot_to_excel(results, str(staging / 'supplement.xlsx'))
        write_json(staging / 'run.json', {
            'created_at': datetime.now(timezone.utc).isoformat(), 'base_id': manifest['contract']['base_id'],
            'base_directory': str(root), 'base_manifest_sha256': request['base_manifest_sha256'],
            'remote_query': False, 'tools': manifest['tools'], 'runtime': {'python': sys.version.split()[0], 'pandas': pd.__version__},
            'tables': {title: f'cross_{position:02d}.csv' for position, title in enumerate(results)},
            'outputs': {path.name: file_hash(path) for path in staging.iterdir()},
        })
        staging.rename(target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    save = commands.add_parser('save')
    save.add_argument('--data', required=True)
    save.add_argument('--contract', required=True)
    save.add_argument('--sql', action='append', required=True)
    save.add_argument('--support', action='append', default=[])
    save.add_argument('--output-dir', required=True)
    run = commands.add_parser('run')
    run.add_argument('--base', required=True)
    run.add_argument('--views', required=True)
    run.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    if args.command == 'save':
        data = Path(args.data)
        if data.suffix.lower() != '.parquet':
            parser.error('CLI requires Parquet with explicit types; for CSV read with explicit dtype then call save_base(df, ...)')
        print(save_base(pd.read_parquet(data), args.output_dir, read_json(args.contract), args.sql, args.support))
    else:
        print(run_views(args.base, args.views, args.output_dir))


if __name__ == '__main__':
    main()
