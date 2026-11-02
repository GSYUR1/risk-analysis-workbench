"""用工作台标准导出器生成公式化预估表；Python快照缓存不冒充Excel重算。"""

import math
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.workbook.properties import CalcProperties

try:
    from .excel_export import merge_formatted_indicator_to_excel, merge_formatted_pivot_to_excel
except ImportError:
    from excel_export import merge_formatted_indicator_to_excel, merge_formatted_pivot_to_excel


LABELS = {
    'quota_factor': '额度变化倍数', 'approved_count': '授信通过件数',
    'old_quota': '原平均额度', 'new_quota': '预估平均额度',
    'old_draw_count': '原动支件数', 'new_draw_count': '预估动支件数',
    'old_draw_rate': '原动支率', 'new_draw_rate': '预估动支率',
    'draw_multiplier': '动支变化倍数', 'draw_capped': '动支封顶标记',
    'old_payout_count': '原成交件数', 'new_payout_count': '预估成交件数',
    'old_payout_amount': '原成交金额', 'new_payout_amount': '预估成交金额',
    'old_ticket': '原成交件均', 'new_ticket': '预估成交件均',
    'old_quota_total': '原授信总额度', 'new_quota_total': '预估授信总额度',
    'draw_capped_groups': '动支封顶组数', 'quota_change': '平均额度变化率',
    'draw_rate_change': '动支率相对变化率', 'payout_count_change': '成交件数变化率',
    'ticket_change': '成交件均变化率', 'payout_amount_change': '成交金额变化率',
}


def label(field):
    if '__' not in field:
        return LABELS.get(field, field)
    identifier, suffix = field.split('__', 1)
    names = {'old_rate': '原风险率', 'new_rate': '预估风险率', 'old_denominator': '原风险分母',
             'new_denominator': '预估风险分母', 'old_numerator': '原风险分子', 'new_numerator': '预估风险分子',
             'relative_change': '风险相对变化率', 'pp_change': '风险变化百分点',
             'missing_weight_groups': '缺风险权重组数', 'unmatured_groups': '未成熟组数', 'weight_mode': '风险汇总模式'}
    return identifier + '-' + names[suffix]


def _cell_map(fields, index_count, row, start_col=1):
    return {field: f'{get_column_letter(start_col + index_count + offset)}{row}' for offset, field in enumerate(fields)}


def _cache_formulas(path, caches, sheet_names):
    namespace = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    temporary = path.with_suffix('.cached.xlsx')
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as target:
        for entry in source.infolist():
            content = source.read(entry.filename)
            if entry.filename.startswith('xl/worksheets/sheet') and entry.filename.endswith('.xml'):
                number = int(entry.filename.split('sheet')[-1].split('.')[0]) - 1
                values = caches.get(sheet_names[number], {})
                if values:
                    tree = ET.fromstring(content)
                    for cell in tree.iter(f'{{{namespace}}}c'):
                        address = cell.attrib['r']
                        if address in values and cell.find(f'{{{namespace}}}f') is not None:
                            cached = cell.find(f'{{{namespace}}}v')
                            if cached is None:
                                cached = ET.SubElement(cell, f'{{{namespace}}}v')
                            value = values[address]
                            if pd.isna(value):
                                cell.set('t', 'e')
                                cached.text = '#N/A'
                            elif isinstance(value, str):
                                cell.set('t', 'str')
                                cached.text = value
                            else:
                                cell.set('t', 'n')
                                cached.text = str(float(value))
                    content = ET.tostring(tree, encoding='utf-8', xml_declaration=True)
            target.writestr(entry, content)
    temporary.replace(path)


def export_impact_workbook(base, detail, summaries, spec, output_path):
    output = Path(output_path)
    if output.exists():
        raise FileExistsError('Excel输出已存在，禁止覆盖')
    dimensions = spec['dimensions']
    fields = spec['fields']
    workbook = Workbook()
    workbook.remove(workbook.active)
    caches = {}
    parameters = pd.DataFrame({'值': [spec['parameters']['risk_elasticity'], int(spec['parameters']['draw_enabled']), spec['parameters']['draw_elasticity']]},
                              index=pd.Index(['风险额度弹性系数', '启用动支影响(0或1)', '动支额度弹性系数'], name='参数'))
    merge_formatted_indicator_to_excel({'情景参数': parameters}, output, '参数', workbook=workbook)
    parameter_sheet = workbook['参数']
    parameter_sheet['B3'].number_format = parameter_sheet['B5'].number_format = '0.00'
    parameter_sheet['B4'].number_format = '0'
    switch_validation = DataValidation(type='list', formula1='"0,1"', allow_blank=False)
    switch_validation.showErrorMessage = True
    parameter_sheet.add_data_validation(switch_validation)
    switch_validation.add('B4')
    parameter_sheet['A8'] = '修改系数或弹性会重算；修改分组、数据范围、口径或风险模式必须重新运行Python校验。'
    parameter_sheet['A9'] = '风险/动支率越界返回#N/A；动支率上限100%，封顶数量在汇总披露。'
    source_columns = list(dict.fromkeys(list(fields.values()) + [risk['rate'] for risk in spec['risk_metrics']] + [risk['denominator'] for risk in spec['risk_metrics'] if risk['weight_mode'] == 'mature']))
    source = base.set_index(dimensions)[source_columns]
    merge_formatted_indicator_to_excel({'基准输入（原文件只读）': source}, output, '基准输入', workbook=workbook)
    for row_number in range(3, len(base) + 3):
        for column_number in range(1, len(dimensions) + 1):
            cell = workbook['基准输入'].cell(row_number, column_number)
            cell.value = str(cell.value)
            cell.data_type = 's'
    calculation = detail.copy()
    for period in ('old', 'new'):
        calculation[period + '_quota_total'] = calculation[period + '_quota'] * calculation['approved_count']
    calculation_fields = [field for field in calculation if field not in dimensions]
    numeric_calculation = calculation.set_index(dimensions)[calculation_fields]
    merge_formatted_indicator_to_excel({'逐组计算（基准→倍数→规模→风险）': numeric_calculation.rename(columns=label)}, output, '计算', workbook=workbook)
    calc_sheet = workbook['计算']
    caches['计算'] = {}
    for position in range(len(calculation)):
        row_number = position + 3
        input_cells = _cell_map(source_columns, len(dimensions), row_number)
        calc_cells = _cell_map(calculation_fields, len(dimensions), row_number)

        def source_ref(name):
            return "'基准输入'!" + input_cells[fields[name]]

        def raw_ref(column):
            return "'基准输入'!" + input_cells[column]

        def calc_ref(name):
            return calc_cells[name]

        scenario = spec['scenario']
        if scenario == 'migrate':
            factor = f"{source_ref('new_coefficient')}/{source_ref('old_coefficient')}"
            positive = f"AND({source_ref('new_coefficient')}>0,{source_ref('old_coefficient')}>0)"
        else:
            factor = source_ref('coefficient') if scenario == 'apply' else f"1/{source_ref('coefficient')}"
            positive = f"{source_ref('coefficient')}>0"
        old_draw = source_ref('draw_count') if 'draw_count' in fields else 'NA()'
        raw_draw = f"{calc_ref('old_draw_rate')}*(1+'参数'!$B$5*({calc_ref('quota_factor')}-1))"
        formulas = {
            'quota_factor': f'IF({positive},{factor},NA())',
            'approved_count': source_ref('approved_count'), 'old_quota': source_ref('avg_quota'),
            'new_quota': f"{calc_ref('old_quota')}*{calc_ref('quota_factor')}",
            'old_draw_count': old_draw,
            'old_draw_rate': f"{calc_ref('old_draw_count')}/{calc_ref('approved_count')}",
            'new_draw_rate': f"IF('参数'!$B$4=0,{calc_ref('old_draw_rate')},IF(AND('参数'!$B$4=1,'参数'!$B$5>=0,{raw_draw}>=0),MIN(1,{raw_draw}),NA()))",
            'draw_capped': f"IF('参数'!$B$4=0,0,--({raw_draw}>1))",
            'draw_multiplier': f"IF('参数'!$B$4=0,1,IF({calc_ref('old_draw_rate')}=0,1,{calc_ref('new_draw_rate')}/{calc_ref('old_draw_rate')}))",
            'new_draw_count': f"{calc_ref('new_draw_rate')}*{calc_ref('approved_count')}",
            'old_payout_count': source_ref('payout_count'),
            'new_payout_count': f"{calc_ref('old_payout_count')}*{calc_ref('draw_multiplier')}",
            'old_payout_amount': source_ref('payout_amount'),
            'new_payout_amount': f"{calc_ref('old_payout_amount')}*{calc_ref('quota_factor')}*{calc_ref('draw_multiplier')}",
            'old_ticket': f"IF({calc_ref('old_payout_count')}>0,{calc_ref('old_payout_amount')}/{calc_ref('old_payout_count')},NA())",
            'new_ticket': f"{calc_ref('old_ticket')}*{calc_ref('quota_factor')}",
        }
        for period in ('old', 'new'):
            formulas[period + '_quota_total'] = f"{calc_ref(period + '_quota')}*{calc_ref('approved_count')}"
        for risk in spec['risk_metrics']:
            prefix = risk['id'] + '__'
            rate_source = raw_ref(risk['rate'])
            rate = f'IF(ISNUMBER({rate_source}),{rate_source},NA())'
            if risk['weight_mode'] == 'mature':
                denominator = raw_ref(risk['denominator'])
                weight = f'IF(ISNUMBER({denominator}),{denominator},NA())'
                rate = f'IF(AND(ISNUMBER({denominator}),{denominator}=0),NA(),{rate})'
            elif risk['weight_mode'] == 'transaction_proxy':
                weight = calc_ref('old_payout_amount')
            else:
                weight = 'NA()'
            projected = f"{calc_ref(prefix + 'old_rate')}*(1+'参数'!$B$3*({calc_ref('quota_factor')}-1))"
            multiplier = calc_ref('draw_multiplier') + ('*' + calc_ref('quota_factor') if risk['kind'] == 'amount' else '')
            formulas[prefix + 'old_rate'] = rate
            formulas[prefix + 'new_rate'] = f"IF(AND('参数'!$B$3>=0,{projected}>=0,{projected}<=1),{projected},NA())"
            formulas[prefix + 'old_denominator'] = weight
            formulas[prefix + 'new_denominator'] = f"{calc_ref(prefix + 'old_denominator')}*{multiplier}"
            for period in ('old', 'new'):
                weight_cell = calc_ref(prefix + period + '_denominator')
                formulas[prefix + period + '_numerator'] = f"IF({weight_cell}=0,0,{weight_cell}*{calc_ref(prefix + period + '_rate')})"
        for field, expression in formulas.items():
            address = calc_cells[field]
            calc_sheet[address] = '=' + expression
            caches['计算'][address] = calculation.iloc[position][field]
        for column_number in range(1, len(dimensions) + 1):
            cell = calc_sheet.cell(row_number, column_number)
            cell.value = str(cell.value)
            cell.data_type = 's'

    summary_tables = {}
    groupings = {'overall': []} | {f'group_{number}': group for number, group in enumerate(spec['summary_groups'], start=1)}
    for name, summary in summaries.items():
        group = groupings[name]
        table = summary.set_index(group) if group else summary.set_axis(pd.Index(['All'], name='汇总'))
        summary_tables['整体影响' if not group else '按' + '×'.join(group) + '汇总'] = table.rename(columns=label)
    merge_formatted_indicator_to_excel(summary_tables, output, 'Summary', workbook=workbook, start_row=25, start_col=4)
    summary_sheet = workbook['Summary']
    caches['Summary'] = {}
    overall_cells = {}
    start_row = 25
    calc_columns = _cell_map(calculation_fields, len(dimensions), 3)
    last_row = len(calculation) + 2
    for name, summary in summaries.items():
        group = groupings[name]
        summary_fields = [field for field in summary if field not in group]
        index_count = len(group) or 1
        for position in range(len(summary)):
            row_number = start_row + 2 + position
            cells = _cell_map(summary_fields, index_count, row_number, 4)
            if name == 'overall':
                overall_cells = cells
            for offset in range(index_count):
                index_cell = summary_sheet.cell(row_number, 4 + offset)
                index_cell.value = str(index_cell.value)
                index_cell.data_type = 's'

            def total(field):
                column = ''.join(character for character in calc_columns[field] if character.isalpha())
                span = f"'计算'!${column}$3:${column}${last_row}"
                if not group:
                    return f'SUM({span})'
                criteria = []
                for offset, dimension in enumerate(group):
                    dim_column = get_column_letter(dimensions.index(dimension) + 1)
                    value_cell = f'{get_column_letter(4 + offset)}{row_number}'
                    escaped = f'SUBSTITUTE(SUBSTITUTE(SUBSTITUTE({value_cell},"~","~~"),"*","~*"),"?","~?")'
                    criteria.extend([f"'计算'!${dim_column}$3:${dim_column}${last_row}", f'"="&{escaped}'])
                return f'SUMIFS({span},' + ','.join(criteria) + ')'

            expressions = {'approved_count': total('approved_count'), 'draw_capped_groups': total('draw_capped')}
            for period in ('old', 'new'):
                expressions[period + '_quota'] = total(period + '_quota_total') + '/' + cells['approved_count']
                for field in ('draw_count', 'payout_count', 'payout_amount'):
                    expressions[period + '_' + field] = total(period + '_' + field)
                expressions[period + '_draw_rate'] = cells[period + '_draw_count'] + '/' + cells['approved_count']
                expressions[period + '_ticket'] = f"IF({cells[period + '_payout_count']}>0,{cells[period + '_payout_amount']}/{cells[period + '_payout_count']},NA())"
            for field in ('quota', 'draw_rate', 'payout_count', 'ticket', 'payout_amount'):
                expressions[field + '_change'] = f"IF({cells['old_' + field]}>0,{cells['new_' + field]}/{cells['old_' + field]}-1,NA())"
            for risk in spec['risk_metrics']:
                prefix = risk['id'] + '__'
                for period in ('old', 'new'):
                    for component in ('numerator', 'denominator'):
                        expressions[prefix + period + '_' + component] = total(prefix + period + '_' + component)
                    denominator = cells[prefix + period + '_denominator']
                    expressions[prefix + period + '_rate'] = f"IF({denominator}>0,{cells[prefix + period + '_numerator']}/{denominator},NA())"
                expressions[prefix + 'relative_change'] = f"IF({cells[prefix + 'old_rate']}>0,{cells[prefix + 'new_rate']}/{cells[prefix + 'old_rate']}-1,NA())"
                expressions[prefix + 'pp_change'] = f"({cells[prefix + 'new_rate']}-{cells[prefix + 'old_rate']})*100"
            for field, expression in expressions.items():
                summary_sheet[cells[field]] = '=' + expression
                caches['Summary'][cells[field]] = summary.iloc[position][field]
        start_row += len(summary) + 5

    display_fields = ['old_quota', 'quota_factor', 'new_quota', 'old_draw_rate', 'new_draw_rate', 'old_payout_count', 'new_payout_count', 'old_ticket', 'new_ticket', 'old_payout_amount', 'new_payout_amount']
    display_fields += [risk['id'] + '__' + period + '_rate' for risk in spec['risk_metrics'] for period in ('old', 'new')]
    view = detail[dimensions + display_fields].copy().astype({field: object for field in display_fields})
    for position in range(len(view)):
        addresses = _cell_map(calculation_fields, len(dimensions), position + 3)
        for field in display_fields:
            view.loc[position, field] = "='计算'!" + addresses[field]
    view = view.set_index(dimensions)[display_fields].rename(columns=label)
    if spec.get('column_dimension'):
        view = view.unstack(spec['column_dimension'])
        merge_formatted_pivot_to_excel({'影响预估过程表': view}, output, sheet_name='过程表', workbook=workbook)
    else:
        merge_formatted_indicator_to_excel({'影响预估过程表': view}, output, '过程表', workbook=workbook)
    caches['过程表'] = {}
    process_data_row = 4 if spec.get('column_dimension') else 3
    process_index_count = len(dimensions) - (1 if spec.get('column_dimension') else 0)
    for row in workbook['过程表']:
        for cell in row:
            if cell.row >= process_data_row and cell.column > process_index_count and cell.data_type == 'f' and cell.value.startswith("='计算'!"):
                caches['过程表'][cell.coordinate] = caches['计算'][cell.value.split('!')[1]]
                source_cell = calc_sheet[cell.value.split('!')[1]]
                cell.number_format = source_cell.number_format
    context = spec['context']
    summary_sheet['B3'] = '【分析背景】'
    summary_sheet['C4'] = '给定假设下的额度调整情景，不代表实测策略收益。'
    summary_sheet['B6'] = '【分析思路】'
    summary_sheet['C7'] = '1.逐组调整额度与件均；2.可选动支变化；3.重算成交规模与风险分子分母。'
    summary_sheet['B9'] = '【分析样本与口径】'
    summary_sheet['C10'] = f"{context['country']} / {context['product']}；样本窗：{context['sample_window']}；币种：{context['currency']}"
    summary_sheet['C11'] = '动支窗：' + context['draw_window'] + '；授信通过规模、动支后转化与组内构成不变。'
    summary_sheet['C12'] = '风险口径：' + '；'.join(risk['id'] + ':' + risk['definition'] + ' [' + risk['weight_mode'] + ']' for risk in spec['risk_metrics'])
    summary_sheet['C13'] = '成交金额近似权重不等于成熟口径；缺分母时不做部分样本加权冒充整体。'
    summary_sheet['B14'] = '【基本结论】'
    conclusion_parts = [('平均额度相对变化：', 'quota_change'), ('成交件数相对变化：', 'payout_count_change'), ('成交金额相对变化：', 'payout_amount_change')]
    risk_parts = [(risk['id'] + '风险相对变化：', risk['id'] + '__relative_change') for risk in spec['risk_metrics']]
    for address, parts in [('C15', conclusion_parts), ('C16', risk_parts)]:
        expressions = [f'"{title}"&IFERROR(TEXT({overall_cells[field]},"0.00%"),"NA")' for title, field in parts]
        summary_sheet[address] = '=' + '&"；"&'.join(expressions)
        caches['Summary'][address] = '；'.join(title + ('NA' if pd.isna(summaries['overall'].iloc[0][field]) else f"{summaries['overall'].iloc[0][field]:.2%}") for title, field in parts)
    summary_sheet['C17'] = '修改参数后由Excel重算。文件初始缓存为Python快照，未声称Excel引擎已重算。'
    summary_sheet['C18'] = '缺权重/未成熟组数为本次输入校验快照；改变源数据后必须重新运行。'
    summary_sheet['C19'] = '风险#N/A表示不可计算或假设越界，不代表零风险。'
    summary_sheet['A22'] = '详细数据'
    summary_sheet['B23'] = '1. 额度策略影响汇总'
    summary_sheet['B24'] = '在统一样本和假设下比较原值与预估值；按指定维度及整体独立汇总。'
    for sheet in workbook:
        sheet.freeze_panes = 'E27' if sheet.title == 'Summary' else 'B3'
        for row in sheet:
            for cell in row:
                if cell.data_type == 'f' and cell.coordinate not in caches.get(sheet.title, {}):
                    cell.data_type = 's'
                cell.font = Font(name='Calibri' if not isinstance(cell.value, str) or cell.data_type == 'f' or cell.value.isascii() else '华文楷体', size=11, bold=cell.font.bold)
                cell.alignment = Alignment(horizontal=cell.alignment.horizontal, vertical='center', wrap_text=False)
                if cell.data_type == 'f' and len(cell.value) > 8192:
                    raise ValueError('Excel公式超过长度限制')
        for column in range(1, sheet.max_column + 1):
            sheet.cell(1, column).fill = PatternFill('solid', fgColor='4472C4')
            sheet.cell(1, column).font = Font(name='华文楷体', size=11, bold=True, color='FFFFFF')
            sheet.column_dimensions[get_column_letter(column)].width = min(32, max(14, sheet.column_dimensions[get_column_letter(column)].width or 14))
        if sheet.title == 'Summary':
            sheet['A1'] = '策略影响预估（静态情景）'
            for column in range(1, sheet.max_column + 1):
                sheet.cell(22, column).fill = PatternFill('solid', fgColor='DDEBF7')
        for row in sheet:
            for cell in row:
                if cell.fill.fgColor.rgb in ('00DCE6F1', '00DDEBF7'):
                    cell.fill = PatternFill('solid', fgColor='DDEBF7')
    for address in ('C15', 'C16'):
        summary_sheet[address].font = Font(name='华文楷体', size=11)
    workbook._sheets = [workbook[name] for name in ('Summary', '过程表', '参数', '计算', '基准输入')]
    workbook.calculation = CalcProperties(calcId=0, fullCalcOnLoad=True, forceFullCalc=True, calcMode='auto')
    workbook.save(output)
    _cache_formulas(output, caches, workbook.sheetnames)
    formula_book = load_workbook(output, data_only=False)
    value_book = load_workbook(output, data_only=True)
    count = 0
    try:
        for sheet, cells in caches.items():
            for address, expected in cells.items():
                if formula_book[sheet][address].data_type != 'f':
                    raise ValueError(f'公式丢失: {sheet}!{address}')
                actual = value_book[sheet][address].value
                if pd.isna(expected):
                    if actual != '#N/A':
                        raise ValueError('NA缓存不一致')
                elif isinstance(expected, str):
                    if actual != expected:
                        raise ValueError('结论文本缓存不一致')
                elif not isinstance(actual, (int, float)) or not math.isclose(actual, float(expected), rel_tol=1e-10, abs_tol=1e-9):
                    raise ValueError('Python快照与Excel缓存不一致')
                count += 1
    finally:
        formula_book.close()
        value_book.close()
    return {'formula_cells_checked': count, 'cache_source': 'python_snapshot', 'excel_engine_recalculated': False, 'visual_review': 'not_per_run'}
