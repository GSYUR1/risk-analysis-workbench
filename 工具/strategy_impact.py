"""额度调整的本地静态情景预估；不取数、不发布、不改变原指标定义。"""

import argparse
import copy
import hashlib
import json
import math
import re
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RULE_PATH = Path('分析功能/策略影响预估/规则/quota_elasticity.v1.json')
VERSION = '1.0.0'
BASE_FIELDS = ('approved_count', 'avg_quota', 'payout_count', 'payout_amount')
SCENARIO_FIELDS = {'apply': ('coefficient',), 'remove': ('coefficient',),
                   'migrate': ('old_coefficient', 'new_coefficient')}


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resolve_spec(spec):
    if not isinstance(spec, dict):
        raise ValueError('spec 必须是JSON对象')
    resolved = copy.deepcopy(spec)
    if resolved.get('schema_version') != 1:
        raise ValueError('spec.schema_version 必须为1')
    rule = read_json(ROOT / RULE_PATH)
    if rule.get('schema_version') != 1 or rule.get('draw_upper_bound') != 1 or rule.get('risk_out_of_range') != 'error':
        raise ValueError('规则边界与当前模型不兼容，不能静默忽略规则修改')
    supplied = resolved.get('parameters', {})
    if not isinstance(supplied, dict) or set(supplied) - set(rule['defaults']):
        raise ValueError('parameters 包含未知参数')
    parameters = {**rule['defaults'], **supplied}
    if not isinstance(parameters['draw_enabled'], bool):
        raise ValueError('draw_enabled 必须是布尔值')
    for field in ('risk_elasticity', 'draw_elasticity'):
        value = parameters[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f'{field} 必须是非负有限数')
    resolved['parameters'] = parameters
    resolved['rule'] = {'id': rule['rule_id'], 'version': rule['version'], 'sha256': file_hash(ROOT / RULE_PATH)}
    dimensions = resolved.get('dimensions')
    if not isinstance(dimensions, list) or not dimensions or not all(isinstance(item, str) and item for item in dimensions):
        raise ValueError('dimensions 必须列明互斥的最细分组字段')
    if len(set(dimensions)) != len(dimensions) or any('__' in item or item.startswith(('old_', 'new_')) for item in dimensions):
        raise ValueError('维度重复或与结果保留字段冲突')
    if resolved.get('scenario') not in SCENARIO_FIELDS:
        raise ValueError('scenario 必须是apply、remove或migrate')
    if not isinstance(resolved.get('context'), dict) or not isinstance(resolved.get('assumptions'), dict):
        raise ValueError('context与assumptions必须是对象')
    for field in ('country', 'product', 'currency', 'sample_window', 'draw_window'):
        value = resolved.get('context', {}).get(field)
        if not isinstance(value, str) or not value.strip() or value.startswith('待填写'):
            raise ValueError(f'context.{field} 必须填写；不适用时显式说明')
    assumptions = resolved.get('assumptions', {})
    for field in ('approved_population_unchanged', 'post_draw_conversion_unchanged', 'within_group_composition_unchanged'):
        if assumptions.get(field) is not True:
            raise ValueError(f'必须明确采用假设: {field}')
    risks = resolved.get('risk_metrics')
    if not isinstance(risks, list) or not risks:
        raise ValueError('必须明确至少一个风险指标，不能猜测风险点')
    identifiers = set()
    for risk in risks:
        if not isinstance(risk, dict):
            raise ValueError('risk_metrics中的每项必须是对象')
        identifier = risk.get('id', '')
        if not re.fullmatch(r'[a-z][a-z0-9_]*', identifier) or identifier in identifiers:
            raise ValueError('风险ID无效或重复')
        identifiers.add(identifier)
        if risk.get('kind') not in ('amount', 'count') or risk.get('weight_mode') not in ('mature', 'unavailable', 'transaction_proxy'):
            raise ValueError(f'{identifier}: 风险类型或汇总权重无效')
        if not risk.get('definition') or str(risk['definition']).startswith('待填写') or not risk.get('rate'):
            raise ValueError(f'{identifier}: 必须指定风险定义及原风险率字段')
        if risk['weight_mode'] == 'mature':
            if not risk.get('denominator') or assumptions.get('maturity_structure_unchanged') is not True:
                raise ValueError('成熟分母模式必须指定分母并确认成熟度/期限结构不变')
        if risk['weight_mode'] == 'transaction_proxy' and risk['kind'] != 'amount':
            raise ValueError('成交金额近似加权仅适用于金额风险，且不是成熟口径')
    groups = resolved.setdefault('summary_groups', [[dimension] for dimension in dimensions])
    if not isinstance(groups, list) or any(not isinstance(group, list) or not group or len(set(group)) != len(group) or not set(group) <= set(dimensions) for group in groups):
        raise ValueError('summary_groups 必须是非空维度子集的列表；整体自动生成')
    if len({tuple(group) for group in groups}) != len(groups):
        raise ValueError('summary_groups 重复')
    column_dimension = resolved.get('column_dimension')
    if column_dimension is not None and (column_dimension not in dimensions or len(dimensions) < 2):
        raise ValueError('column_dimension 必须是维度之一，且须有剩余行维度')
    return resolved


def _numeric(frame, column, nullable=False):
    if not isinstance(column, str) or column not in frame:
        raise ValueError(f'缺少输入字段: {column}')
    values = pd.to_numeric(frame[column], errors='coerce').astype(float)
    if ((frame[column].notna() & values.isna()) | np.isinf(values)).any() or (not nullable and values.isna().any()):
        raise ValueError(f'{column} 含缺失、非数值或无穷值')
    if (values.dropna() < 0).any():
        raise ValueError(f'{column} 不能为负')
    return values


def resolve_quota_factor(base, spec):
    fields = spec['fields']
    scenario = spec['scenario']
    coefficients = {name: _numeric(base, fields.get(name)) for name in SCENARIO_FIELDS[scenario]}
    if any((values <= 0).any() for values in coefficients.values()):
        raise ValueError('额度系数必须大于0；零额度/拒绝策略不属于本模型')
    if scenario == 'migrate':
        return coefficients['new_coefficient'] / coefficients['old_coefficient']
    if scenario == 'remove':
        return 1 / coefficients['coefficient']
    return coefficients['coefficient']


def validate_impact_input(base, spec):
    resolved = resolve_spec(spec)
    if base.empty or not base.columns.is_unique:
        raise ValueError('输入不能为空或包含重复列名')
    dimensions = resolved['dimensions']
    reserved = {'quota_factor', 'approved_count', 'draw_multiplier', 'draw_capped'}
    if set(dimensions) & reserved:
        raise ValueError('分组维度与内部指标字段冲突')
    if not set(dimensions) <= set(base.columns):
        raise ValueError('缺少分组维度')
    for dimension in dimensions:
        labels = base[dimension].astype(str).str.strip()
        if base[dimension].isna().any() or labels.eq('').any():
            raise ValueError('缺失维度须先明确标记为独立组，不能静默丢弃')
        if labels.isin(['ALL', 'All', '总计', '合计', '小计']).any():
            raise ValueError('输入含汇总行；先移除汇总行，只提供互斥叶子组')
    if base.duplicated(dimensions).any():
        raise ValueError('输入分组重复，不能直接相加或自动去重')
    fields = resolved.get('fields', {})
    if not isinstance(fields, dict):
        raise ValueError('fields 必须为字段映射')
    allowed = set(BASE_FIELDS) | {'draw_count', 'coefficient', 'old_coefficient', 'new_coefficient'}
    if set(fields) - allowed or len(set(fields.values())) != len(fields):
        raise ValueError('字段映射存在未知key或重复来源')
    values = {name: _numeric(base, fields.get(name)) for name in BASE_FIELDS}
    if (values['approved_count'] <= 0).any() or (values['avg_quota'] <= 0).any():
        raise ValueError('授信通过件数和平均额度必须大于0；空组不进入预估')
    if (values['payout_count'] > values['approved_count']).any():
        raise ValueError('成交件数大于授信通过件数；请核对粒度与观察窗')
    if ((values['payout_count'] == 0) != (values['payout_amount'] == 0)).any():
        raise ValueError('零成交件数与零成交金额不一致')
    if resolved['parameters']['draw_enabled'] and 'draw_count' not in fields:
        raise ValueError('启用动支影响需要同口径动支件数')
    if 'draw_count' in fields:
        draws = _numeric(base, fields['draw_count'])
        if ((draws > values['approved_count']) | (draws < values['payout_count'])).any():
            raise ValueError('要求成交件数 <= 动支件数 <= 授信通过件数')
    factor = resolve_quota_factor(base, resolved)
    if not np.isfinite(factor).all():
        raise ValueError('额度变化倍数溢出')
    for risk in resolved['risk_metrics']:
        rates = _numeric(base, risk['rate'], nullable=True)
        if (rates > 1).any():
            raise ValueError('风险率使用0到1的小数，不接受整百分数')
        projected = rates * (1 + resolved['parameters']['risk_elasticity'] * (factor - 1))
        if ((projected < 0) | (projected > 1) | np.isinf(projected)).any():
            raise ValueError('预估风险率越界；线性假设不适用，请调整情景而非静默截断')
        if risk['weight_mode'] == 'mature':
            denominator = _numeric(base, risk['denominator'], nullable=True)
            if ((denominator > 0) & rates.isna()).any():
                raise ValueError('有成熟分母但风险率缺失，不能当作零风险')
            if ((denominator == 0) & (rates.fillna(0) != 0)).any():
                raise ValueError('成熟分母为0却有非零风险率')
            if ((values['payout_count'] == 0) & (denominator.fillna(0) > 0)).any():
                raise ValueError('无成交却有成熟分母，样本口径不一致')
            if risk['kind'] == 'count' and (denominator > values['payout_count']).any():
                raise ValueError('成熟件数超过成交件数')
    return resolved


def estimate_quota_impact(base, spec):
    base = base.reset_index(drop=True).copy()
    spec = validate_impact_input(base, spec)
    fields, parameters = spec['fields'], spec['parameters']
    detail = base[spec['dimensions']].copy()
    detail['quota_factor'] = resolve_quota_factor(base, spec)
    detail['approved_count'] = _numeric(base, fields['approved_count'])
    detail['old_quota'] = _numeric(base, fields['avg_quota'])
    detail['new_quota'] = detail['old_quota'] * detail['quota_factor']
    detail['old_draw_count'] = _numeric(base, fields['draw_count']) if 'draw_count' in fields else np.nan
    detail['old_draw_rate'] = detail['old_draw_count'] / detail['approved_count']
    raw_draw_rate = detail['old_draw_rate'] * (1 + parameters['draw_elasticity'] * (detail['quota_factor'] - 1)) if parameters['draw_enabled'] else detail['old_draw_rate']
    if (raw_draw_rate < 0).any():
        raise ValueError('预估动支率小于0，线性假设不适用')
    detail['draw_capped'] = raw_draw_rate.gt(1)
    detail['new_draw_rate'] = raw_draw_rate.clip(upper=1)
    detail['draw_multiplier'] = (detail['new_draw_rate'] / detail['old_draw_rate']).where(detail['old_draw_rate'] > 0, 1.0)
    detail['new_draw_count'] = detail['new_draw_rate'] * detail['approved_count']
    detail['old_payout_count'] = _numeric(base, fields['payout_count'])
    detail['new_payout_count'] = detail['old_payout_count'] * detail['draw_multiplier']
    detail['old_payout_amount'] = _numeric(base, fields['payout_amount'])
    detail['old_ticket'] = detail['old_payout_amount'] / detail['old_payout_count'].replace(0, np.nan)
    detail['new_ticket'] = detail['old_ticket'] * detail['quota_factor']
    detail['new_payout_amount'] = detail['old_payout_amount'] * detail['quota_factor'] * detail['draw_multiplier']
    for risk in spec['risk_metrics']:
        prefix = risk['id'] + '__'
        detail[prefix + 'old_rate'] = _numeric(base, risk['rate'], nullable=True)
        if risk['weight_mode'] == 'mature':
            denominator = _numeric(base, risk['denominator'], nullable=True)
            detail.loc[denominator.eq(0), prefix + 'old_rate'] = np.nan
        elif risk['weight_mode'] == 'transaction_proxy':
            denominator = detail['old_payout_amount'].copy()
        else:
            denominator = pd.Series(np.nan, index=detail.index)
        detail[prefix + 'new_rate'] = detail[prefix + 'old_rate'] * (1 + parameters['risk_elasticity'] * (detail['quota_factor'] - 1))
        detail[prefix + 'old_denominator'] = denominator
        multiplier = detail['draw_multiplier'] * (detail['quota_factor'] if risk['kind'] == 'amount' else 1)
        detail[prefix + 'new_denominator'] = denominator * multiplier
        for period in ('old', 'new'):
            weight = detail[prefix + period + '_denominator']
            detail[prefix + period + '_numerator'] = (detail[prefix + period + '_rate'] * weight).where(weight.ne(0), 0.0)
    numeric = detail.select_dtypes(include='number')
    if np.isinf(numeric.to_numpy()).any():
        raise ValueError('计算溢出，请核对数量级与系数')
    detail.attrs['resolved_spec'] = spec
    return detail


def _ratio(numerator, denominator):
    return numerator / denominator if pd.notna(denominator) and denominator > 0 else np.nan


def summarize_impact(detail, group_by, spec):
    if len(set(group_by)) != len(group_by) or not set(group_by) <= set(spec['dimensions']):
        raise ValueError('汇总维度不是输入维度的合法子集')
    grouped = detail.groupby(group_by, dropna=False, observed=True, sort=False) if group_by else [((), detail)]
    rows = []
    for keys, block in grouped:
        keys = keys if isinstance(keys, tuple) else (keys,)
        record = dict(zip(group_by, keys))
        record['approved_count'] = block['approved_count'].sum()
        record['draw_capped_groups'] = int(block['draw_capped'].sum())
        for period in ('old', 'new'):
            record[period + '_quota'] = (block[period + '_quota'] * block['approved_count']).sum() / record['approved_count']
            for field in ('draw_count', 'payout_count', 'payout_amount'):
                record[period + '_' + field] = block[period + '_' + field].sum(skipna=False)
            record[period + '_draw_rate'] = record[period + '_draw_count'] / record['approved_count']
            record[period + '_ticket'] = _ratio(record[period + '_payout_amount'], record[period + '_payout_count'])
        for field in ('quota', 'draw_rate', 'payout_count', 'ticket', 'payout_amount'):
            record[field + '_change'] = _ratio(record['new_' + field], record['old_' + field]) - 1
        for risk in spec['risk_metrics']:
            prefix = risk['id'] + '__'
            missing = block[prefix + 'old_denominator'].isna()
            record[prefix + 'missing_weight_groups'] = int(missing.sum())
            record[prefix + 'unmatured_groups'] = int(block[prefix + 'old_denominator'].eq(0).sum())
            for period in ('old', 'new'):
                denominator = block[prefix + period + '_denominator'].sum(skipna=False)
                numerator = block[prefix + period + '_numerator'].sum(skipna=False)
                record[prefix + period + '_denominator'] = denominator
                record[prefix + period + '_numerator'] = numerator
                record[prefix + period + '_rate'] = _ratio(numerator, denominator)
            record[prefix + 'relative_change'] = _ratio(record[prefix + 'new_rate'], record[prefix + 'old_rate']) - 1
            record[prefix + 'pp_change'] = (record[prefix + 'new_rate'] - record[prefix + 'old_rate']) * 100
            record[prefix + 'weight_mode'] = risk['weight_mode']
        rows.append(record)
    return pd.DataFrame(rows)


def read_input(path, spec):
    path = Path(path)
    if path.suffix.lower() == '.csv':
        import csv
        with path.open(encoding='utf-8-sig', newline='') as stream:
            header = next(csv.reader(stream), [])
        if len(set(header)) != len(header):
            raise ValueError('CSV存在重复表头')
        return pd.read_csv(path, encoding='utf-8-sig', dtype={dimension: str for dimension in spec['dimensions']})
    if path.suffix.lower() == '.xlsx':
        from openpyxl import load_workbook
        from openpyxl.utils.cell import range_boundaries
        source = spec.get('source', {})
        if not source.get('sheet') or not source.get('range'):
            raise ValueError('Excel输入必须明确sheet与range；只支持单层表头的矩形底表，不猜透视布局')
        workbook = load_workbook(path, read_only=True, data_only=True)
        formulas = load_workbook(path, read_only=True, data_only=False)
        try:
            bounds = range_boundaries(source['range'])
            if not all(isinstance(value, int) and value > 0 for value in bounds):
                raise ValueError('Excel range必须明确到单元格')
            left, top, right, bottom = bounds
            if right < left or bottom <= top:
                raise ValueError('Excel range必须包含表头和数据')
            rows = list(workbook[source['sheet']].iter_rows(min_row=top, max_row=bottom, min_col=left, max_col=right, values_only=True))
            formula_rows = formulas[source['sheet']].iter_rows(min_row=top, max_row=bottom, min_col=left, max_col=right)
            for cached_row, formula_row in zip(rows, formula_rows):
                if any(cell.data_type == 'f' and value is None for value, cell in zip(cached_row, formula_row)):
                    raise ValueError('Excel输入公式缺少缓存值，请先完成重算')
            header = rows[0]
            if any(not isinstance(value, str) or not value for value in header) or len(set(header)) != len(header):
                raise ValueError('Excel需要唯一的非空单层表头')
            return pd.DataFrame(rows[1:], columns=header)
        finally:
            workbook.close()
            formulas.close()
    raise ValueError('输入仅支持UTF-8 CSV或明确范围的XLSX；不修改源文件')


def run_impact(input_path, spec_path, output_directory):
    input_path, spec_path = Path(input_path).resolve(), Path(spec_path).resolve()
    output = Path(output_directory).resolve()
    if output.exists():
        raise FileExistsError(f'输出目录已存在，禁止覆盖: {output}')
    source_hashes = {'input': file_hash(input_path), 'spec': file_hash(spec_path)}
    original = read_json(spec_path)
    resolved = resolve_spec(original)
    frame = read_input(input_path, resolved)
    detail = estimate_quota_impact(frame, resolved)
    resolved = detail.attrs['resolved_spec']
    summaries = {'overall': summarize_impact(detail, [], resolved)}
    for number, group in enumerate(resolved['summary_groups'], start=1):
        summaries[f'group_{number}'] = summarize_impact(detail, group, resolved)
    references = {}
    for reference in resolved.get('definition_files', []):
        relative = Path(reference)
        definition = (ROOT / relative).resolve()
        if relative.is_absolute() or not definition.is_relative_to(ROOT) or not definition.is_file():
            raise ValueError(f'口径文件引用缺失或越界: {reference}')
        references[relative.as_posix()] = file_hash(definition)
    tracked = [Path('工具/strategy_impact.py'), Path('工具/strategy_impact_excel.py'),
               Path('工具/excel_export.py'), RULE_PATH,
               Path('分析功能/策略影响预估/方法说明.md'),
               Path('分析功能/策略影响预估/capability.json')]
    versions = {path.as_posix(): file_hash(ROOT / path) for path in tracked}
    checks = {
        'input_validated': True,
        'draw_capped_groups': int(detail['draw_capped'].sum()),
        'zero_draw_groups': int(detail['old_draw_count'].eq(0).sum()),
        'zero_payout_groups': int(detail['old_payout_count'].eq(0).sum()),
        'risk_weights': {risk['id']: {
            'mode': risk['weight_mode'],
            'missing_groups': int(detail[risk['id'] + '__old_denominator'].isna().sum()),
            'unmatured_groups': int(detail[risk['id'] + '__old_denominator'].eq(0).sum()),
        } for risk in resolved['risk_metrics']},
        'validation_scope': 'local_scenario_not_causal_validation',
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.impact-', dir=output.parent) as temporary:
        staging = Path(temporary) / 'result'
        staging.mkdir()
        for name, payload in [('spec.original.json', original), ('spec.resolved.json', resolved), ('checks.json', checks)]:
            (staging / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        detail.to_csv(staging / 'detail.csv', index=False, encoding='utf-8-sig')
        for name, summary in summaries.items():
            summary.to_csv(staging / f'{name}.csv', index=False, encoding='utf-8-sig')
        try:
            from .strategy_impact_excel import export_impact_workbook
        except ImportError:
            from strategy_impact_excel import export_impact_workbook
        excel_checks = export_impact_workbook(frame, detail, summaries, resolved, staging / '影响预估.xlsx')
        if source_hashes != {'input': file_hash(input_path), 'spec': file_hash(spec_path)}:
            raise ValueError('运行中输入或配置改变，禁止交付混合版本')
        if any(file_hash(ROOT / path) != digest for path, digest in versions.items()):
            raise ValueError('运行中方法或代码改变，禁止交付混合版本')
        if any(file_hash(ROOT / path) != digest for path, digest in references.items()):
            raise ValueError('运行中数据口径改变')
        manifest = {
            'schema_version': 1, 'tool_version': VERSION, 'status': 'completed_local_scenario',
            'input': {'path': str(input_path), 'sha256': source_hashes['input']},
            'spec': {'path': str(spec_path), 'sha256': source_hashes['spec']},
            'versions': versions, 'definition_files': references,
            'context': resolved['context'], 'excel_checks': excel_checks,
            'summary_groups': dict(zip([f'group_{number}' for number in range(1, len(resolved['summary_groups']) + 1)], resolved['summary_groups'])),
            'artifacts': {path.name: file_hash(path) for path in staging.iterdir() if path.is_file()},
        }
        (staging / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        staging.rename(output)
    return {'ok': True, 'output': str(output), 'checks': checks, 'excel_checks': excel_checks}


def main():
    parser = argparse.ArgumentParser(description='本地额度策略影响预估；只写新的输出目录')
    parser.add_argument('--input', required=True)
    parser.add_argument('--spec', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    try:
        result = run_impact(args.input, args.spec, args.output_dir)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
