# -*- coding: utf-8 -*-
"""
ph_data_analysis.py — 菲律宾版三方数据/征信分析指标工具包

数据底表：
    - demo.order_trace : 新客授信全链路宽表（主取数表，含订单级明细列
      is_approve / is_order / is_payout / credit_amount_after / payout_principal / dpd{期}_{观察天} / ...）
    - demo.indicator   : 指标聚合表（指标列 cre_cnt / apprv_cnt / t0_order_fz /
      is_order_fz / payout_cnt / overdue_amt_{期}_{观察天} / ...）
    两表同粒度（user_id * credit_apply_no * loan_apply_no，一笔授信申请对应首笔动支订单）。
    本模块函数默认按 order_trace 明细列计算；若传入 indicator 指标列，会在
    _normalize_ph_base 中自动映射为明细列，两种取数均可直接使用。

说明：
    1. 分析起点由 generate_indicator / generate_pivot 的 stage 参数指定（三个环节的起点不同，指标集不同，
       具体指标见工作台指标口径文件的 ph_newcust_cre_pivot_v1）：
           stage='credit'   授信开始（默认）：授信申请->通过->动支->成交 全漏斗
           stage='withdraw' 动支开始：仅保留有动支订单的样本（is_order=1）
           stage='payout'   放款开始：仅保留已放款（成交）样本（is_payout=1）
    2. RISK_MOBS = ['1_7', '2_7', '3_7', '4_7', '5_7', '6_7']
       风险表现点记作 {期数}_{观察天数}，源自 demo.risk_observations（tag='new'）：
           金额逾期率   dpd{p}_{d}    = Σoverdue_amt_{p}_{d} / Σexpire_amt_{p}_{d}
           人头逾期率   dpd{p}_{d}cnt = Σoverdue_cnt_{p}_{d} / Σexpire_cnt_{p}_{d}
           出账（到期件数） 出账{p}_{d}  = Σexpire_cnt_{p}_{d}
       观察点已出数（overdue_amt 非 null）即视为到期：expire_amt=该单 repay_principal、expire_cnt=1。
    3. 币种：底表金额单位为 PHP，($) 换算使用 FX_PHP_PER_USD=61。
    4. 老客复借 ever 逾期（generate_old_pivot）：人头口径，
           cycle2_ever3 = Σoverdue_cycle2_ever3 / Σexpire_cycle2_ever3
           同口径还有 cycle2_ever7 / cycle4_ever3 / cycle4_ever7。
"""

import numpy as np
import pandas as pd
import re
import warnings
try:
    from IPython.display import display
except ImportError:  # 非notebook环境兜底
    display = print
# optbinning / toad 仅分箱函数需要，放到使用处再 import，避免 reload(pda) 每次都卡几秒


# 忽略所有警告
warnings.filterwarnings("ignore")

# 设定展示最大行列数
pd.options.display.max_rows = 1000
pd.options.display.max_columns = 500

# ===================== 菲律宾口径常量 =====================
# 默认观察的风险表现点（期数_观察天数）
RISK_MOBS = ['1_7', '2_7', '3_7', '4_7', '5_7', '6_7']

# 全部32个风险表现点：期数1~2观察点[1,2,3,4,5,7,10,15]，期数3~6观察点[1,3,7,15]
RISK_POINTS = {1: [1, 2, 3, 4, 5, 7, 10, 15],
               2: [1, 2, 3, 4, 5, 7, 10, 15],
               3: [1, 3, 7, 15],
               4: [1, 3, 7, 15],
               5: [1, 3, 7, 15],
               6: [1, 3, 7, 15]}
ALL_RISK_MOBS = [f'{p}_{d}' for p, ds in RISK_POINTS.items() for d in ds]

# 老客复借 ever 逾期表现点（人头：Σoverdue_cycle* / Σexpire_cycle*）
OLD_RISK_MOBS = ['cycle2_ever3', 'cycle2_ever7', 'cycle4_ever3', 'cycle4_ever7']

# PHP -> USD 汇率（口径文件中的 FX=61 PHP/USD）
FX_PHP_PER_USD = 61

# 合法的分析起点（stage）：授信 / 动支 / 放款
STAGES = ('credit', 'withdraw', 'payout')


def _validate_stage(stage):
    """校验分析起点参数。"""
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}, got {stage!r}")


def _validate_lift_base(lift_base):
    """校验风险lift分母基准参数。"""
    valid_lift_bases = {'overall', 'row'}
    if lift_base not in valid_lift_bases:
        raise ValueError(f"lift_base must be one of {valid_lift_bases}, got {lift_base!r}")


def _validate_risk_mob(mob):
    """校验风险表现点是否在全部32个表现点内。"""
    if mob not in ALL_RISK_MOBS:
        raise ValueError(f"risk_ind must be one of the 32 risk points, e.g. '1_7'/'2_7', got {mob!r}")


def _validate_old_risk_mob(mob):
    """校验老客复借 ever 逾期表现点。"""
    if mob not in OLD_RISK_MOBS:
        raise ValueError(f"old risk_ind must be one of {OLD_RISK_MOBS}, got {mob!r}")


def _old_risk_cols(mob):
    """老客 ever 表现点对应的逾期/到期列名，如 cycle2_ever3 -> overdue_cycle2_ever3 / expire_cycle2_ever3。"""
    _validate_old_risk_mob(mob)
    return (f'overdue_{mob}', f'expire_{mob}')


def _risk_cols(mob):
    """风险表现点 mob（如 '1_7'）对应的四个基础列名。"""
    _validate_risk_mob(mob)
    return (f'overdue_amt_{mob}', f'expire_amt_{mob}',
            f'overdue_cnt_{mob}', f'expire_cnt_{mob}')


def _build_lift_assignments_ph(df_result, lift_base='overall', row_group_levels=None):
    """
    为indicator类输出生成dpd*_lift列赋值字典（菲律宾口径：逾期金额/到期金额）。

    Args:
        df_result (pd.DataFrame): 已包含m*与overdue_amt/expire_amt汇总列的结果表
        lift_base (str): 'overall' 或 'row'
        row_group_levels (list/int/None): row基准时用于汇总行风险的索引层级
    """
    _validate_lift_base(lift_base)
    lift_assignments = {}

    for mob in RISK_MOBS:
        risk_col = f'dpd{mob}'
        overdue_col = f'overdue_amt_{mob}'
        expire_col = f'expire_amt_{mob}'
        lift_col = f'dpd{mob}lift'

        if lift_base == 'row' and row_group_levels is not None:
            row_overdue = df_result.groupby(level=row_group_levels)[overdue_col].transform('sum')
            row_expire = df_result.groupby(level=row_group_levels)[expire_col].transform('sum')
            row_risk = row_overdue / row_expire.replace(0, np.nan)
            lift_values = (df_result[risk_col] / row_risk).replace([np.inf, -np.inf], np.nan)
            lift_assignments[lift_col] = lift_values
        else:
            overall_expire = df_result[expire_col].sum()
            overall_risk = df_result[overdue_col].sum() / overall_expire if overall_expire != 0 else np.nan
            lift_assignments[lift_col] = df_result[risk_col] / overall_risk if not np.isnan(overall_risk) else np.nan

    return lift_assignments


def _calculate_pivot_risk_lift_ph(m_risk, risk_pivot1, mob, margins=True, lift_base='overall',
                                  overdue_key=None, expire_key=None):
    """
    计算pivot结果下的风险lift（菲律宾口径，支持overall/row基准）。

    Args:
        m_risk (pd.DataFrame): 格子级逾期率
        risk_pivot1 (pd.DataFrame): 含逾期分子 / 到期分母两个汇总块的pivot结果
        mob (str): 风险表现点，如 '1_7' 或 'cycle2_ever3'
        overdue_key / expire_key: 分子分母块名，默认 overdue_amt_{mob} / expire_amt_{mob}
    """
    _validate_lift_base(lift_base)

    overdue_tbl = risk_pivot1[overdue_key or f'overdue_amt_{mob}']
    expire_tbl = risk_pivot1[expire_key or f'expire_amt_{mob}']

    if lift_base == 'overall':
        if margins:
            total_overdue = overdue_tbl.iloc[-1, -1]
            total_expire = expire_tbl.iloc[-1, -1]
        else:
            total_overdue = overdue_tbl.to_numpy().sum()
            total_expire = expire_tbl.to_numpy().sum()

        overall_m_risk = total_overdue / total_expire if total_expire != 0 else 0
        m_risklift = m_risk / overall_m_risk if overall_m_risk != 0 else m_risk * 0
        return m_risklift.fillna(0)

    # row基准：每个格子风险 / 该行汇总风险
    if margins:
        row_overdue = overdue_tbl.iloc[:, :-1].sum(axis=1)
        row_expire = expire_tbl.iloc[:, :-1].sum(axis=1)
    else:
        row_overdue = overdue_tbl.sum(axis=1)
        row_expire = expire_tbl.sum(axis=1)

    row_risk = row_overdue / row_expire.replace(0, np.nan)
    m_risklift = m_risk.div(row_risk, axis=0).replace([np.inf, -np.inf], np.nan)
    return m_risklift.fillna(0)


# ===================== indicator表/宽表列名兼容 =====================
# indicator 指标列 -> order_trace 明细flag列 的映射（值>0视为1）
_FLAG_FROM_INDICATOR = [
    ('is_approve', 'apprv_cnt'),
    ('t0_order', 't0_order_fz'),
    ('t1_order', 't1_order_fz'),
    ('t3_order', 't3_order_fz'),
    ('t7_order', 't7_order_fz'),
    ('is_order', 'is_order_fz'),
    ('is_order_approve', 'order_approve_cnt'),
    ('is_order_pending', 'order_pending_cnt'),
    ('is_payout', 'payout_cnt'),
]

# 需要强制数值化的基础列（存在才处理）
_NUMERIC_BASE_COLS = ['credit_amount_after', 'order_apprv_credits', 'order_apprv_principal',
                      'payout_principal', 'payout_credits', 'payout_prin_t0', 'payout_prin_t1', 'payout_prin_t3',
                      'available_amount_before', 'available_amount_after',
                      'repay_principal', 'repay_interest', 'repay_principal_add_interest', 'deal_amt_dt',
                      'price', 'period', 'days_per_period', 'real_duration', 'apr_duration']


def _as_numeric(s):
    """已是数值则原样返回，避免对大列重复 to_numeric。"""
    if pd.api.types.is_numeric_dtype(s):
        return s
    return pd.to_numeric(s, errors='coerce')


def _normalize_ph_base(df_input, copy=True):
    """
    将输入数据统一为 order_trace 明细列口径（不修改原df）：
    1. indicator 指标列（apprv_cnt/t0_order_fz/...）-> 明细flag列（is_approve/t0_order/...）
    2. indicator 无 credit_amount_after 时用 total_credits 兜底
    3. 基础数值列与风险列强制数值化（odps导出常为字符串）；cnt/amt风险列空值视为0（未到期）
    4. order_trace 的 dpd{期}_{观察天}（逾期金额别名）-> overdue_amt_{期}_{观察天}
    """
    df = df_input.copy() if copy else df_input

    # 1) indicator 指标列 -> 明细flag列
    for flag_col, src_col in _FLAG_FROM_INDICATOR:
        if flag_col not in df.columns and src_col in df.columns:
            src_vals = _as_numeric(df[src_col]).fillna(0)
            df[flag_col] = (src_vals > 0).astype(int)

    # 2) 授信额度兜底
    if 'credit_amount_after' not in df.columns and 'total_credits' in df.columns:
        df['credit_amount_after'] = _as_numeric(df['total_credits'])

    # 3) 数值化（已是数值则跳过 to_numeric）
    flag_cols = [c for c, _ in _FLAG_FROM_INDICATOR if c in df.columns]
    for c in [c for c in _NUMERIC_BASE_COLS if c in df.columns] + flag_cols:
        df[c] = _as_numeric(df[c])
    for c in flag_cols:
        df[c] = df[c].fillna(0)

    # 4) 风险列：dpd别名 -> overdue_amt；风险列数值化、空值(未到期/未匹配订单)视为0
    for p, ds in RISK_POINTS.items():
        for d in ds:
            oa_col = f'overdue_amt_{p}_{d}'
            dpd_col = f'dpd{p}_{d}'
            if oa_col not in df.columns and dpd_col in df.columns:
                df[oa_col] = _as_numeric(df[dpd_col])
            for prefix in ('overdue_amt', 'expire_amt', 'overdue_cnt', 'expire_cnt'):
                rc = f'{prefix}_{p}_{d}'
                if rc in df.columns:
                    df[rc] = _as_numeric(df[rc]).fillna(0)

    return df


def _metric_number_format(name):
    """单个指标名对应的数字格式（indicator / pivot 共用）。"""
    name = str(name)
    if re.match(r'^(dpd\d+_\d+|cycle\d+_ever\d+)lift$', name):
        return '{:.2f}'
    if (name in ('IRR', 'APR') or re.search(r'率|占比', name)
            or re.match(r'^(dpd\d+_\d+(cnt)?|cycle\d+_ever\d+)$', name)):
        return '{:.2%}'
    if name.endswith('($)') or name in ('久期', '名义期限'):
        return '{:,.2f}'
    return '{:,.0f}'


def _build_format_dict(cols):
    """按列名规则生成style.format用的格式字典；兼容多层列（按level0取指标名）。"""
    fmt = {}
    for c in cols:
        name = c[0] if isinstance(c, tuple) else c
        fmt[c] = _metric_number_format(name)
    return fmt


# generate_indicator 数据条：浅色低饱和，衬在黑字下不抢内容
_BAR_COLOR_COUNT = '#AED0EA'       # 雾蓝 — 申请/件数
_BAR_COLOR_PASS_RATE = '#B5D8A8'   # 抹茶 — 通过率/转化率
_BAR_COLOR_DRAW_RATE = '#F3C89A'   # 浅杏 — 动支率/定价
_BAR_COLOR_AMOUNT = '#E8D89A'      # 香槟金 — 金额/件均
_BAR_COLOR_RISK = '#EBB0B0'        # 浅珊瑚 — 风险

_RE_RISK_COL = re.compile(
    r'^(dpd\d+_\d+(lift|cnt)?|出账\d+_\d+|\d+_\d+出账|cycle\d+_ever\d+(lift)?|cycle\d+_ever\d+出账)$'
)
_RE_DPD_RATE = re.compile(r'^(dpd\d+_\d+(cnt)?|cycle\d+_ever\d+)$')
_RE_DPD_LIFT = re.compile(r'^(dpd\d+_\d+|cycle\d+_ever\d+)lift$')


def _default_risk_output_cols(mobs=None):
    """风险列默认顺序：全部dpd → 全部出账 → 全部dpd lift → 全部dpd cnt。"""
    mobs = RISK_MOBS if mobs is None else mobs
    return (
        [f'dpd{m}' for m in mobs]
        + [f'出账{m}' for m in mobs]
        + [f'dpd{m}lift' for m in mobs]
        + [f'dpd{m}cnt' for m in mobs]
    )


def _classify_indicator_bar_cols(cols):
    """按指标语义把列分到数据条颜色组。"""
    groups = {
        'count': [],
        'pass_rate': [],
        'draw_rate': [],
        'amount': [],
        'risk': [],
    }
    for c in cols:
        name = str(c)
        if _RE_RISK_COL.match(name):
            groups['risk'].append(c)
        elif '动支率' in name or name == '动支pending率' or name in ('久期', '名义期限', 'IRR', 'APR'):
            groups['draw_rate'].append(c)
        elif re.search(r'通过率|转化率', name):
            groups['pass_rate'].append(c)
        elif re.search(r'件均|效率|金额|额度', name):
            groups['amount'].append(c)
        else:
            groups['count'].append(c)
    return groups


def _is_all_row_label(label):
    """判断是否为汇总行：indicator 的 ALL，或 pivot margins 的 All。"""
    if isinstance(label, tuple):
        tokens = [str(x) for x in label]
        return any(t in ('ALL', 'All') for t in tokens) and all(t in ('ALL', 'All', '') for t in tokens)
    return str(label) in ('ALL', 'All')


def _is_margin_col(col):
    """判断透视表列是否为 All 汇总列（跳过 level0 指标名）。"""
    parts = col if isinstance(col, tuple) else (col,)
    check = parts[1:] if len(parts) > 1 else ()
    return any(str(x) == 'All' for x in check)


def _pivot_risk_cmaps():
    """dpd/cnt：绿-黄-红（越大越红）；lift：白-红（越大越红）。"""
    try:
        from matplotlib.colors import LinearSegmentedColormap
        dpd_cmap = LinearSegmentedColormap.from_list(
            'ph_dpd_ryg', ['#7CBC7C', '#F2E6A8', '#DE7A7A']
        )
        lift_cmap = LinearSegmentedColormap.from_list(
            'ph_dpd_lift', ['#FFFFFF', '#F6D0D0', '#D96A6A']
        )
        return dpd_cmap, lift_cmap
    except ImportError:
        return 'RdYlGn_r', 'Reds'


def _metric_level0(columns):
    """取出列的指标名（多层列用 level0）。"""
    if isinstance(columns, pd.MultiIndex):
        return [c[0] for c in columns]
    return list(columns)


def _style_pivot_df(df, fmt=None):
    """
    透视表格式：数字格式对齐 generate_indicator；
    普通指标数据条（汇总行/列不画条）；
    dpd / dpd cnt 绿-黄-红色阶，dpd lift 白-红色阶（越大越红）。
    """
    if fmt is None:
        fmt = _build_format_dict(df.columns)
    styler = df.style.format(fmt)

    bar_rows = [i for i in df.index if not _is_all_row_label(i)]
    if not bar_rows:
        return styler

    level0_names = list(dict.fromkeys(_metric_level0(df.columns)))
    groups = _classify_indicator_bar_cols(level0_names)
    dpd_names = [n for n in level0_names if _RE_DPD_RATE.match(str(n))]
    lift_names = [n for n in level0_names if _RE_DPD_LIFT.match(str(n))]
    # 数据条不含色阶指标
    scale_names = set(dpd_names + lift_names)

    def _paint_cols(metric_names):
        names = set(metric_names)
        cols = []
        for c in df.columns:
            name = c[0] if isinstance(c, tuple) else c
            if name in names and not _is_margin_col(c):
                cols.append(c)
        return cols

    color_map = {
        'count': _BAR_COLOR_COUNT,
        'pass_rate': _BAR_COLOR_PASS_RATE,
        'draw_rate': _BAR_COLOR_DRAW_RATE,
        'amount': _BAR_COLOR_AMOUNT,
        'risk': _BAR_COLOR_RISK,
    }
    # 每个指标单独一块、axis=None：整块共用比例尺（与逾期色阶一致），不是按列各自缩放
    name_to_color = {}
    for key, color in color_map.items():
        for n in groups[key]:
            if n not in scale_names:
                name_to_color[n] = color
    for name, color in name_to_color.items():
        subset = _paint_cols([name])
        if subset:
            styler = styler.bar(
                subset=pd.IndexSlice[bar_rows, subset],
                color=color,
                align='left',
                vmin=0,
                axis=None,
            )

    dpd_cmap, lift_cmap = _pivot_risk_cmaps()
    for name in dpd_names:
        subset = _paint_cols([name])
        if subset:
            styler = styler.background_gradient(
                subset=pd.IndexSlice[bar_rows, subset],
                cmap=dpd_cmap,
                axis=None,
            )
    for name in lift_names:
        subset = _paint_cols([name])
        if subset:
            styler = styler.background_gradient(
                subset=pd.IndexSlice[bar_rows, subset],
                cmap=lift_cmap,
                axis=None,
            )
    return styler


def _style_indicator_df(df, fmt=None):
    """为indicator结果表按列类型刷数字格式 + 数据条（汇总行只保留数字格式，不画数据条）。"""
    if fmt is None:
        fmt = _build_format_dict(df.columns)
    groups = _classify_indicator_bar_cols(df.columns)
    color_map = {
        'count': _BAR_COLOR_COUNT,
        'pass_rate': _BAR_COLOR_PASS_RATE,
        'draw_rate': _BAR_COLOR_DRAW_RATE,
        'amount': _BAR_COLOR_AMOUNT,
        'risk': _BAR_COLOR_RISK,
    }
    styler = df.style.format(fmt)
    bar_rows = [i for i in df.index if not _is_all_row_label(i)]
    if not bar_rows:
        return styler
    for key, color in color_map.items():
        subset = [c for c in groups[key] if c in df.columns and df[c].notna().any()]
        if subset:
            styler = styler.bar(
                subset=pd.IndexSlice[bar_rows, subset],
                color=color,
                align='left',
                vmin=0,
            )
    return styler


def _as_jupyter_styled_frame(df, styled):
    """
    返回仍是 DataFrame 的对象，但 Jupyter 只渲染带格式的 Styler，
    避免 display(styler) + return df 在单元格末行打出两张表。
    """
    out = df.copy()
    html = styled.to_html()

    def _repr_html_(*args, **kwargs):
        return html

    def _ipython_display_(*args, **kwargs):
        display(styled)

    out._repr_html_ = _repr_html_
    out._ipython_display_ = _ipython_display_
    return out


def _append_indicator_all_row(df, df_agg, bin_name, other_grp, aggregate_fn):
    """按全量数据再聚合一行 ALL，接到分组结果末尾。"""
    keys = [bin_name] + list(other_grp)
    df_all = df.copy()
    for col in keys:
        if col in df_all.columns and isinstance(df_all[col].dtype, pd.CategoricalDtype):
            df_all[col] = df_all[col].astype(object)
        df_all[col] = 'ALL'
    df_all_agg = aggregate_fn(df_all)

    out = df_agg
    if isinstance(out.index, pd.CategoricalIndex):
        out = out.copy()
        out.index = out.index.astype(object)
    elif isinstance(out.index, pd.MultiIndex) and any(
        isinstance(out.index.levels[i], pd.CategoricalIndex) for i in range(out.index.nlevels)
    ):
        out = out.copy()
        out.index = pd.MultiIndex.from_tuples(out.index.tolist(), names=out.index.names)
    return pd.concat([out, df_all_agg])

# 数据清理函数，上传odps使用
def odps_data_cleaning(df):
    """
    全维度数据清理函数：统一处理空值表示、自动适配数据类型，为后续ODPS导入/数据分析做准备。

    核心清理逻辑（分两步）：
    1. 空值标准化：将各种格式的空值（nan/NaN/None/NULL等）统一转换为空字符串
    2. 类型自适应：先转为字符串处理空值，再尝试转回数值类型（能转则转，不能转则保留字符串）

    Args:
        df (pd.DataFrame): 待清理的原始DataFrame

    Returns:
        pd.DataFrame: 清理后的DataFrame，空值统一为''，列类型适配为数值/字符串（无混合类型）
    """
    df_clean = df.copy()

    for col in df_clean.columns:
        df_clean[col] = df_clean[col].astype(str)
        df_clean[col] = df_clean[col].replace({
            'nan': '', 'NaN': '', 'None': '', 'NULL': '', 'null': '',
            'NaT': '', 'nat': '', '<NA>': '', '': ''
        })
        try:
            numeric_vals = pd.to_numeric(df_clean[col], errors='coerce')
            if not numeric_vals.isna().all():
                df_clean[col] = numeric_vals
            else:
                df_clean[col] = df_clean[col].astype(str)
        except Exception:
            df_clean[col] = df_clean[col].astype(str)

    return df_clean


def generate_eda_report(df, list_eda_vars, special_values=[-1, -1111, -999, -9999, 999, 9999]):
    """
    生成探索性数据诊断（eda）报告，按字段类型（数值型/字符串型）计算多维度统计指标，
    核心用于数据质量校验（空值、特殊值、0值、分布等）。

    核心逻辑：
    1. 筛选指定待诊断的字段列，避免用户ID/时间列干扰
    2. 区分字段类型（object/数值型）计算差异化指标：
       - 字符串型（object）：仅计算计数、唯一值、空值、特殊值相关指标
       - 数值型：额外计算0值、-1值、统计分布（min/max/均值/标准差/分位数）
    3. 生成结构化eda报告，包含空值率、查得率、有效值率等核心数据质量指标

    Args:
        df (pd.DataFrame): 原始数据集（包含待诊断的所有字段）
        list_eda_vars (list): 需要进行eda诊断的字段名称列表
        special_values (list, optional): 需要监控的特殊值列表，默认[-1,-1111,-999,-9999,999,9999]

    Returns:
        pd.DataFrame: 以字段为行的eda诊断报告
    """
    df_eda_raw = df[list_eda_vars].copy()

    eda_results = []

    for col in df_eda_raw.columns:
        col_dtype = df_eda_raw[col].dtype
        total_records = len(df_eda_raw)

        count = df_eda_raw[col].count()
        unique = df_eda_raw[col].nunique()
        null_count = df_eda_raw[col].isnull().sum()
        null_rate = null_count / total_records if total_records > 0 else 0
        valid_rate = count / total_records if total_records > 0 else 0
        special_count = df_eda_raw[col].isin(special_values).sum()
        special_rate = special_count / total_records if total_records > 0 else 0

        if col_dtype == 'object':
            zero_count = None
            zero_rate = None
            min_value = None
            max_value = None
            mean = None
            std = None
            quantiles = {0.25: None, 0.5: None, 0.75: None}
        else:
            zero_count = (df_eda_raw[col] == 0).sum()
            zero_rate = zero_count / total_records if total_records > 0 else 0
            min_value = df_eda_raw[col].min()
            max_value = df_eda_raw[col].max()
            mean = df_eda_raw[col].mean()
            std = df_eda_raw[col].std()
            quantiles = df_eda_raw[col].quantile([0.25, 0.5, 0.75]).to_dict()

        eda_result = {
            '字段': col,
            '数据类型': col_dtype,
            'count': count,
            'unique': unique,
            '有效值率': valid_rate - special_rate if (pd.notna(special_rate) and special_rate is not None) else valid_rate,
            '查得率': valid_rate,
            '空值数量': null_count,
            '空值率': null_rate,
            '0值数量': zero_count,
            '0值率': zero_rate,
            '特殊值数量': special_count,
            '特殊值率': special_rate,
            'min': min_value,
            'max': max_value,
            'mean': mean,
            'std': std,
            '25%分位点': quantiles.get(0.25),
            '50%分位点': quantiles.get(0.5),
            '75%分位点': quantiles.get(0.75)
        }
        eda_results.append(eda_result)

    df_eda_result = pd.DataFrame(eda_results)

    return df_eda_result


def filtered_corr_and_iv(df, feats, df_iv_result, threshold=0.7, special_values=[-1, -9999, -999]):
    """
    基于相关性阈值筛选特征：相关性绝对值超过阈值的特征对中，仅保留IV值更高者，
    最终按IV值降序返回。

    Args:
        df (pd.DataFrame): 原始数据集，包含待筛选的特征列
        feats (list): 需要进行筛选的特征名称列表
        df_iv_result (pd.DataFrame): 特征IV值表，需含 'name' 与 'iv' 两列
        threshold (float, optional): 相关性阈值，默认0.7
        special_values (list, optional): 需视为缺失值的特殊值列表，默认[-1,-9999,-999]

    Returns:
        list: 按IV值降序排列的最终筛选特征列表
    """
    print('原始变量数：' + str(len(feats)))

    df_corr_base = df.loc[:, feats].apply(pd.to_numeric, errors='coerce')
    if special_values:
        df_corr_base = df_corr_base.mask(df_corr_base.isin(special_values))

    df_corr = df_corr_base.corr(min_periods=500)
    iv_map = df_iv_result.set_index('name')['iv']

    list_final_vars = []

    for var1 in feats:
        add_var = True
        iv1 = iv_map[var1]
        for var2 in feats:
            if var1 == var2:
                continue
            if abs(df_corr.at[var1, var2]) > threshold and iv1 < iv_map[var2]:
                add_var = False
                break
        if add_var:
            list_final_vars.append(var1)

    vars_with_iv = {var: iv_map[var] for var in list_final_vars}
    sorted_vars = sorted(vars_with_iv.items(), key=lambda x: x[1], reverse=True)
    sorted_final_vars = [var for var, _ in sorted_vars]

    print('筛选后变量数：' + str(len(sorted_final_vars)))

    return sorted_final_vars


def _format_bin_labels(bin_col, ind_col):
    """向量化生成分箱标识：普通箱 '01.[a, b)'，特殊/缺失 '99.Special'/'99.Missing'。"""
    bin_s = pd.Series(bin_col, copy=False).astype('string')
    mask_special = bin_s.isin(['Special', 'Missing'])
    idx_str = pd.to_numeric(ind_col, errors='coerce').astype('Int64').add(1).astype('string').str.zfill(2)
    labels = idx_str + '.' + bin_s
    return labels.mask(mask_special, '99.' + bin_s).fillna('').astype(str)


def _infer_bin_digits(s, special_codes=None):
    """
    按字段取值推断 show_digits：
      整数（整型 dtype，或浮点但有效值全为整数）→ 1
      浮点且最大值 <= 1 → 3
      浮点且最大值 > 1 → 2
    类别/无法数值化 → 0。
    """
    special_codes = special_codes or []
    if not pd.api.types.is_numeric_dtype(s):
        numeric = pd.to_numeric(s, errors='coerce')
        if numeric.notna().sum() == 0:
            return 0
        s = numeric
    clean = s.dropna()
    if special_codes:
        clean = clean[~clean.isin(special_codes)]
    if len(clean) == 0:
        return 0
    if pd.api.types.is_integer_dtype(s):
        return 1
    vals = clean.to_numpy(dtype='float64', copy=False)
    if np.isfinite(vals).all() and np.allclose(vals, np.round(vals), rtol=0, atol=1e-9):
        return 1
    if float(np.nanmax(vals)) <= 1:
        return 3
    return 2


# 获取添加了分箱结果的原始df、分箱后的ref_table、各变量iv值表、binningProcess分箱器
def optb_get_lift_df(df, list_vars, target='is_ovd_3_7', max_n_bins=10, min_n_bins=2, min_bin_size=0.05, cat_feats=[], special_codes=[], split_digits=3, var_digits=None):
    """
    使用optbinning库的BinningProcess进行卡方分箱，生成包含分箱结果的数据集、
    各变量分箱的统计信息表（含坏样本浓度lift）、各变量iv值表、binningProcess分箱器。
    注意：若target存在空值会自动剔除。

    Args:
        df (pd.DataFrame): 输入数据集，必须包含待分箱特征列（list_vars）和目标列（target）
        list_vars (list): 需要进行分箱的特征名称列表（数值型/类别型均可）
        target (str, optional): 目标变量名（二分类），默认'is_ovd_3_7'
            （菲律宾常用坏账口径，可由 generate_ovd 基于逾期金额列生成）
        max_n_bins (int, optional): 每个特征的最大分箱数，默认10
        min_n_bins (int, optional): 每个特征的最小分箱数，默认2
        min_bin_size (float, optional): 每个分箱的最小占比（0-1），默认0.05
        cat_feats (list, optional): 显式指定的类别特征列表，默认空列表（自动识别）
        special_codes (list, optional): 需要单独处理的特殊值列表，默认空列表
        split_digits (int, optional): 默认切点小数位，默认3
        var_digits (dict, optional): 指定字段的小数位，如 {'cibi_trade_plt_d90': 2}。
            命中字段的 split_digits 与 show_digits 都用该值；其余字段切点用 split_digits，
            展示用 _infer_bin_digits（整数1 / 浮点max<=1为3 / 浮点max>1为2）。

    Returns:
        df_binned (pd.DataFrame): 包含原始数据+分箱结果的数据集
        ref_table (pd.DataFrame): 各变量分箱的统计信息表（含lift）
        df_iv_result (pd.DataFrame): 各变量的IV值结果表
        bp (optbinning.BinningProcess): optbinning分箱器
    """

    if df[target].isnull().any():
        print("目标变量target存在空值，已自动剔除相关行以防报错")
        df = df.dropna(subset=[target]).reset_index(drop=True)

    # 只复制待分箱列做类型转换，避免对整张宽表 copy
    X = df[list_vars].copy()
    for var in list_vars:
        if X[var].dtype == 'object' and var not in cat_feats:
            numeric_col = pd.to_numeric(X[var], errors='coerce')
            if numeric_col.notna().any():
                print(f"字段 {var} 为文本格式，已自动转换为数值")
                X[var] = numeric_col

    # ==================== 防报错拦截：剔除全为空/特殊值的特征 ====================
    valid_vars = []
    for var in list_vars:
        col_data = X[var].dropna()
        clean_data = col_data[~col_data.isin(special_codes)]
        if len(clean_data) == 0:
            print(f"警告: 特征 '{var}' 全为空值或特殊值，无有效分箱数据，已自动剔除")
        else:
            valid_vars.append(var)

    if len(valid_vars) == 0:
        raise ValueError("所有特征都被剔除，请检查数据")

    list_vars = valid_vars
    X = X[list_vars]
    cat_set = set(cat_feats or [])
    var_digits = var_digits or {}

    split_map, show_map = {}, {}
    for var in list_vars:
        if var in cat_set:
            split_map[var] = show_map[var] = 0
        elif var in var_digits:
            split_map[var] = show_map[var] = int(var_digits[var])
        else:
            split_map[var] = int(split_digits)
            show_map[var] = _infer_bin_digits(X[var], special_codes)
    print('各字段切点小数位:', split_map)
    print('各字段展示小数位:', show_map)

    from optbinning import BinningProcess

    # ==== 1. 分箱器初始化与训练 ====
    binning_fit_params = {
        var: {'split_digits': split_map[var]}
        for var in list_vars if var not in cat_set
    }
    bp = BinningProcess(
        variable_names=list_vars,
        max_n_bins=max_n_bins,
        min_n_bins=min_n_bins,
        min_bin_size=min_bin_size,
        categorical_variables=cat_feats,
        special_codes=special_codes,
        binning_fit_params=binning_fit_params or None,
    )

    print("开始训练分箱器...")
    bp.fit(X, df[target])

    df_iv_result = bp.summary().sort_values(by='iv', ascending=False)

    # ==== 2. 转换数据生成分箱结果 ====
    print("转换分箱数据...")
    df_bins = bp.transform(X, metric="bins")
    df_indices = bp.transform(X, metric="indices")

    # ==== 3. 拼接分箱标识（向量化，一次组表，避免逐列 insert 碎片化） ====
    df_bin_labels = pd.DataFrame(
        {f'{var}_bin': _format_bin_labels(df_bins[var], df_indices[var]) for var in list_vars},
        index=df.index
    )
    df_binned = pd.concat([df, df_bin_labels], axis=1)

    # ==== 4. 计算分箱lift（坏样本浓度提升度） ====
    global_rate = df[target].mean()
    print(f"整体目标变量浓度(坏样本率): {global_rate:.4%}")

    table_list = []

    for f in list_vars:
        op = bp.get_binned_variable(f)
        try:
            tb_df = op.binning_table.build(show_digits=show_map[f], add_totals=False)
            tb_df = tb_df.reset_index().rename(columns={'index': 'Bin ID'})
            tb_df.insert(0, 'Var Name', f)
            table_list.append(tb_df)
        except Exception as e:
            print(f"特征 {f} 生成统计表失败: {str(e)}")

    if table_list:
        ref_table = pd.concat(table_list, axis=0, ignore_index=True)
        ref_table['lift'] = ref_table['Event rate'] / global_rate
    else:
        ref_table = pd.DataFrame()

    return df_binned, ref_table, df_iv_result, bp


# 将已拟合的 optbinning.BinningProcess 应用于新的 df_new，生成与训练阶段一致的分箱标识
def optb_apply_bins(df_new, bp, list_vars, bin_suffix="_bin", keep_other_cols=True, return_intermediate=False):
    """
    将已拟合的 optbinning.BinningProcess 应用于新的 df_new，生成与训练阶段一致的分箱标识：
      - 普通分箱：两位数1-based索引 + "." + 区间字符串，例如 "01.[0.1, 0.5)"
      - 特殊/缺失：固定前缀 "99." + "Special"/"Missing"，例如 "99.Missing"

    Args:
        df_new (pd.DataFrame): 新数据，需包含 list_vars 列
        bp (optbinning.BinningProcess): 训练阶段返回的分箱器
        list_vars (list): 需要应用分箱的特征列表（与训练一致）
        bin_suffix (str): 最终分箱标识列的后缀，默认 "_bin"
        keep_other_cols (bool): True 则把分箱结果合并回原 df_new；False 仅返回分箱结果列
        return_intermediate (bool): 是否同时返回中间列 {var}_range 和 {var}_ind

    Returns:
        pd.DataFrame: 包含分箱标识列（以及可选的中间列）
    """

    missing = [c for c in list_vars if c not in df_new.columns]
    if missing:
        raise ValueError(f"df_new 缺少以下特征列：{missing}")

    X = df_new[list_vars].copy()
    for var in list_vars:
        if X[var].dtype == 'object':
            numeric_col = pd.to_numeric(X[var], errors='coerce')
            if numeric_col.notna().any():
                print(f"字段 {var} 为文本格式，已自动转换为数值")
                X[var] = numeric_col

    bins_df = bp.transform(X, metric="bins")
    ind_df = bp.transform(X, metric="indices")

    out_data = {f'{var}{bin_suffix}': _format_bin_labels(bins_df[var], ind_df[var]) for var in list_vars}
    if return_intermediate:
        for var in list_vars:
            bin_col = bins_df[var].astype('string')
            mask_special = bin_col.isin(['Special', 'Missing'])
            out_data[f'{var}_range'] = bin_col
            out_data[f'{var}_ind'] = np.where(
                mask_special, '99',
                pd.to_numeric(ind_df[var], errors='coerce').astype('Int64').add(1).astype('string').str.zfill(2)
            )
    out = pd.DataFrame(out_data, index=df_new.index)

    if keep_other_cols:
        return pd.concat([df_new.reset_index(drop=True), out.reset_index(drop=True)], axis=1)
    return out


def _sql_split(x):
    """digitize 切点写成 SQL 数值，保留 float64 有效精度。"""
    x = float(x)
    if not np.isfinite(x):
        return 'inf' if x > 0 else '-inf'
    return format(x, '.12g')


def _bp_fitted_names(bp):
    """已拟合字段，保持 variable_names 原顺序。"""
    names = list(getattr(bp, 'variable_names', []) or [])
    binned = getattr(bp, '_binned_variables', None)
    return [n for n in names if n in binned] if binned else names


def optb_generate_bin_sql(bp, list_vars=None, special_codes=None, bin_suffix='_bin',
                          col_expr=None, print_sql=True):
    """
    根据已拟合的 BinningProcess 生成分箱 CASE WHEN SQL（ODPS/Hive）。
    切点取自 OptimalBinning.splits（与 digitize 一致），不用 ref_table 的展示文本。
    未传 list_vars 时输出全部已分箱字段。

    Args:
        bp: optb_get_lift_df 返回的分箱器
        list_vars: 要输出的字段；None 表示全部。带 _bin 后缀会自动去掉
        special_codes: 覆盖特殊值；None 则用该变量自己的 special_codes。
            list → '99.Special'；dict → '99.{组名}'
        bin_suffix: 输出列后缀，默认 '_bin'
        col_expr: 字段名 -> SQL 表达式（COALESCE/CAST 等）
        print_sql: 是否 print

    Returns:
        str: 可贴进 SELECT 列表的 CASE 片段
    """
    if bp is None or not getattr(bp, '_is_fitted', False):
        raise ValueError("bp 未拟合，无法生成分箱 SQL")

    all_vars = _bp_fitted_names(bp)
    if not all_vars:
        raise ValueError("bp 中没有已分箱字段")

    if list_vars is None:
        target_vars = all_vars
    else:
        if isinstance(list_vars, str):
            list_vars = [list_vars]
        target_vars = []
        for v in list_vars:
            name = str(v)
            if bin_suffix and name.endswith(bin_suffix):
                name = name[:-len(bin_suffix)]
            target_vars.append(name)
        unknown = [v for v in target_vars if v not in set(all_vars)]
        if unknown:
            print(f"警告: 以下字段不在 bp 已分箱列表中，已跳过：{unknown}")
        target_vars = [v for v in target_vars if v in set(all_vars)]
    if not target_vars:
        raise ValueError("没有可输出 SQL 的已分箱字段")

    col_expr = col_expr or {}
    pieces = []
    for var in target_vars:
        optb = bp.get_binned_variable(var)
        col = col_expr.get(var, var)
        codes = special_codes if special_codes is not None else (optb.special_codes or bp.special_codes)

        lines = [f"    WHEN {col} IS NULL THEN '99.Missing'"]
        if codes:
            groups = codes.items() if isinstance(codes, dict) else [('Special', list(codes))]
            for gname, gvals in groups:
                gvals = list(gvals) if isinstance(gvals, (list, tuple, np.ndarray, set)) else [gvals]
                in_list = ', '.join(
                    _sql_split(v) if isinstance(v, (int, float, np.number)) and not isinstance(v, (bool, np.bool_))
                    else "'" + str(v).replace("'", "''") + "'"
                    for v in gvals
                )
                pred = f'{col} = {in_list}' if len(gvals) == 1 else f'{col} IN ({in_list})'
                lines.append(f"    WHEN {pred} THEN '99.{gname}'")

        if getattr(optb, 'dtype', 'numerical') == 'categorical':
            for i, cats in enumerate(optb.splits if optb.splits is not None else []):
                vals = list(cats) if isinstance(cats, (list, tuple, np.ndarray, set, pd.Index)) else [cats]
                if not vals:
                    continue
                in_list = ', '.join("'" + str(v).replace("'", "''") + "'" for v in vals)
                pred = f'{col} = {in_list}' if len(vals) == 1 else f'{col} IN ({in_list})'
                lines.append(f"    WHEN {pred} THEN '{i + 1:02d}.{'|'.join(map(str, vals))}'")
            lines.append("    ELSE '99.Missing'")
        else:
            toks = [_sql_split(s) for s in np.asarray(optb.splits if optb.splits is not None else [], dtype='float64')]
            if not toks:
                lines.append("    ELSE '01.(-inf, inf)'")
            else:
                lines.append(f"    WHEN {col} < {toks[0]} THEN '01.(-inf, {toks[0]})'")
                for i in range(len(toks) - 1):
                    lines.append(f"    WHEN {col} < {toks[i + 1]} THEN '{i + 2:02d}.[{toks[i]}, {toks[i + 1]})'")
                lines.append(f"    ELSE '{len(toks) + 1:02d}.[{toks[-1]}, inf)'")

        pieces.append('CASE\n' + '\n'.join(lines) + f'\n END AS {var}{bin_suffix}')

    sql = ',\n'.join(pieces)
    if print_sql:
        print(sql)
    return sql


# ===================== 指标生成（by bin 聚合表） =====================
# 菲律宾没有动支宽表，统一用 stage 参数选择分析起点：
#   stage='credit'   授信开始：授信申请->授信通过->动支->成交 全漏斗（默认）
#   stage='withdraw' 动支开始：仅保留有动支订单的样本（is_order=1）
#   stage='payout'   放款开始：仅保留已放款（成交）样本（is_payout=1）

_DEFAULT_INDICATOR_OUTPUTS = {
    'credit': ['授信申请件数', '件数占比', '授信通过件数', '授信通过率',
               '授信通过总额度(PHP)', '授信件均(PHP)', '授信额度($)', '授信效率(PHP)',
               '动支率T0', '动支率T1', '动支率T3', '动支率T7', '动支率',
               '动支通过件数', '动支通过率', '成交件数', '成交转化率', '成交金额(PHP)', '成交件均(PHP)', '放款件均($)',
               '久期', '名义期限', 'IRR', 'APR'],
    'withdraw': ['动支件数', '件数占比', '动支通过件数', '动支通过率', '动支pending率',
                 '动支通过金额(PHP)', '成交件数', '成交转化率', '成交金额(PHP)', '成交件均(PHP)', '放款件均($)',
                 '久期', '名义期限', 'IRR', 'APR'],
    'payout': ['成交件数', '件数占比', '成交金额(PHP)', '金额占比',
               '成交件均(PHP)', '放款件均($)', '成交-授信件均($)',
               '久期', '名义期限', 'IRR', 'APR'],
}


def _prepare_ph_indicator_df(df_input, stage, copy=True):
    """stage筛选 + 构造数据口径的分子列（不修改原df）。"""
    df = _normalize_ph_base(df_input, copy=copy)

    if 'is_credit' not in df.columns:
        credit_amt = _as_numeric(df['credit_amount_after']) if 'credit_amount_after' in df.columns else None
        df['is_credit'] = np.where(credit_amt > 0, 1, 0) if credit_amt is not None else 0

    # 定价分子/分母列兜底（order_trace明细列 -> indicator口径的聚合分子列）
    if 'repay_principal_add_interest' not in df.columns and {'repay_principal', 'repay_interest'} <= set(df.columns):
        df['repay_principal_add_interest'] = df['repay_principal'].fillna(0) + df['repay_interest'].fillna(0)
    if 'sum_real_duration_principal' not in df.columns and {'repay_principal', 'real_duration'} <= set(df.columns):
        df['sum_real_duration_principal'] = df['repay_principal'] * df['real_duration']
    if 'dt_amt_sum_apr' not in df.columns and {'repay_principal', 'apr_duration'} <= set(df.columns):
        df['dt_amt_sum_apr'] = df['repay_principal'] * df['apr_duration']
    if 'sum_irr_principal' not in df.columns and {'repay_principal', 'price', 'real_duration'} <= set(df.columns):
        df['sum_irr_principal'] = df['repay_principal'] * df['price'] * df['real_duration']

    # 按 is_approve 门控的分子列（对齐指标口径中的 sql_expression: sum(case when is_approve=1 then ... end)）
    is_apprv = df['is_approve'].fillna(0) if 'is_approve' in df.columns else pd.Series(1, index=df.index)
    if 'credit_amount_after' in df.columns:
        df['_total_credits'] = np.where((is_apprv == 1) & (df['is_credit'] == 1),
                                        df['credit_amount_after'].fillna(0), 0)
    for t in ['t0', 't1', 't3', 't7']:
        if f'{t}_order' in df.columns:
            df[f'_{t}_order_fz'] = is_apprv * df[f'{t}_order'].fillna(0)
    for new_col, src in [('_is_order_fz', 'is_order'),
                         ('_order_approve_cnt', 'is_order_approve'),
                         ('_order_pending_cnt', 'is_order_pending'),
                         ('_payout_cnt', 'is_payout')]:
        if src in df.columns:
            df[new_col] = is_apprv * df[src].fillna(0)

    # 分析起点过滤
    if stage == 'withdraw':
        if 'is_order' not in df.columns:
            raise ValueError("stage='withdraw' 需要 is_order 列（或 indicator 表的 is_order_fz 列）")
        df = df[df['is_order'] == 1]
    elif stage == 'payout':
        if 'is_payout' not in df.columns:
            raise ValueError("stage='payout' 需要 is_payout 列（或 indicator 表的 payout_cnt 列）")
        df = df[df['is_payout'] == 1]

    return df.reset_index(drop=True)


def _risk_sum_cols():
    """RISK_MOBS对应的风险基础列名列表（存在才聚合）。"""
    cols = []
    for mob in RISK_MOBS:
        cols.extend(_risk_cols(mob))
    return cols


def generate_indicator(df_input, bin_name, other_grp=[], stage='credit', show=False,
                       highlight=None, lift_base='overall', columns_output=None,
                       add_all_row=True):
    """
    菲律宾口径：输出 by bin 的漏斗指标 + 风险表现（对齐工作台数据口径/metric_registry.yaml）。

    底表：demo.order_trace（明细列）或
          demo.indicator（指标列，自动映射）。

    Args:
        df_input (pd.DataFrame): 底表数据（粒度 user_id*credit_apply_no*loan_apply_no）
        bin_name (str): 主分组维度（如分箱列）
        other_grp (list, optional): 其他分组维度
        stage (str, optional): 分析起点，'credit'授信（默认）/'withdraw'动支/'payout'放款。
            三个环节的起点不同、指标集不同：
            - credit:   授信申请->通过->动支->成交 全漏斗（授信件数/通过率/额度/动支率T0~T7/成交/风险）
            - withdraw: 仅动支样本（动支件数/通过率/pending率/成交转化/风险）
            - payout:   仅成交样本（成交件数/金额/件均/定价/风险）
        show (bool, optional): True 时 Jupyter 以条件格式（数据条）渲染返回的表。
            返回值仍是 DataFrame，可继续计算；单元格末行不会再多打出一张无格式表。
        highlight: 兼容旧参数，已不再使用（改为按列类型统一着色）
        lift_base (str, optional): 风险lift分母基准，'overall'全局/'row'按首层分组行汇总
        columns_output (list, optional): 输出列，默认按stage取 _DEFAULT_INDICATOR_OUTPUTS + 风险列
            （风险列顺序：全部dpd → 全部出账 → 全部dpd lift → 全部dpd cnt）
        add_all_row (bool, optional): 是否在末尾追加ALL汇总行，默认True

    Returns:
        pd.DataFrame: 以分组维度为索引的指标表
    """
    _validate_stage(stage)
    _validate_lift_base(lift_base)
    _ = highlight  # 兼容旧调用，按列类型着色后不再单独高亮

    if columns_output is None:
        columns_output = _DEFAULT_INDICATOR_OUTPUTS[stage] + _default_risk_output_cols()

    keep_cols = _collect_needed_cols(df_input, [bin_name] + list(other_grp), RISK_MOBS)
    df = _prepare_ph_indicator_df(_take_columns(df_input, keep_cols), stage, copy=False)
    count_col = 'credit_apply_no' if 'credit_apply_no' in df.columns else df.columns[0]

    def _aggregate(data):
        """对一份数据做全量聚合，返回含基础指标的DataFrame（一行或多行）。"""
        list_grp = [bin_name] + other_grp
        grp = data.groupby(list_grp)
        sum_candidates = ['is_approve', '_total_credits',
                          '_t0_order_fz', '_t1_order_fz', '_t3_order_fz', '_t7_order_fz',
                          '_is_order_fz', '_order_approve_cnt', '_order_pending_cnt',
                          '_payout_cnt', 'order_apprv_credits', 'order_apprv_principal',
                          'payout_credits', 'payout_principal',
                          'available_amount_before', 'available_amount_after',
                          'deal_amt_dt', 'sum_real_duration_principal', 'dt_amt_sum_apr',
                          'sum_irr_principal', 'repay_principal', 'repay_interest',
                          'repay_principal_add_interest']
        sum_candidates += _risk_sum_cols()
        sum_cols = [c for c in sum_candidates if c in data.columns]
        agg = grp[sum_cols].sum()
        agg['授信申请件数'] = grp[count_col].count()
        return agg

    df_agg = _aggregate(df)
    list_grp = [bin_name] + other_grp
    if add_all_row:
        df_agg = _append_indicator_all_row(df, df_agg, bin_name, other_grp, _aggregate)

    # ---------------------- 分母列0替换为nan，避免除零 ----------------------
    df_agg_safe = df_agg.copy()
    # 兼容 indicator 表取数：缺失的源列补NaN，保证除法不报NoneType错误
    required_src_cols = ['is_approve', '_total_credits',
                         '_t0_order_fz', '_t1_order_fz', '_t3_order_fz', '_t7_order_fz',
                         '_is_order_fz', '_order_approve_cnt', '_order_pending_cnt',
                         '_payout_cnt', 'order_apprv_principal', 'payout_principal', 'payout_credits',
                         'sum_real_duration_principal', 'dt_amt_sum_apr', 'sum_irr_principal',
                         'repay_principal', 'repay_interest']
    required_src_cols += [f'{pre}_{mob}' for mob in RISK_MOBS for pre in ('overdue_amt', 'expire_amt', 'overdue_cnt', 'expire_cnt')]
    for c in required_src_cols:
        if c not in df_agg_safe.columns:
            df_agg_safe[c] = np.nan
    denominator_cols = ['授信申请件数', 'is_approve', '_is_order_fz', '_order_approve_cnt',
                        '_payout_cnt', 'payout_principal', 'repay_principal',
                        'sum_real_duration_principal', 'dt_amt_sum_apr']
    denominator_cols += [f'expire_amt_{mob}' for mob in RISK_MOBS]
    denominator_cols += [f'expire_cnt_{mob}' for mob in RISK_MOBS]
    for c in denominator_cols:
        if c in df_agg_safe.columns:
            df_agg_safe[c] = df_agg_safe[c].astype('float').replace(0, np.nan)

    # 占比分母不含ALL行，避免把汇总行加进合计后占比被腰斩
    apply_cnt = df_agg_safe['授信申请件数']
    payout_amt = df_agg_safe['payout_principal'] if 'payout_principal' in df_agg_safe.columns else None
    if add_all_row and len(apply_cnt) > 0:
        apply_den = apply_cnt.iloc[:-1].sum()
        payout_den = payout_amt.iloc[:-1].sum() if payout_amt is not None else np.nan
    else:
        apply_den = apply_cnt.sum()
        payout_den = payout_amt.sum() if payout_amt is not None else np.nan

    # ---------------------- 衍生指标 ----------------------
    df_result = df_agg_safe.assign(
        件数占比=apply_cnt / apply_den,
        授信通过件数=df_agg_safe.get('is_approve'),
        授信通过率=df_agg_safe.get('is_approve') / df_agg_safe['授信申请件数'],
        授信通过总额度PHP=df_agg_safe.get('_total_credits'),
        授信件均PHP=df_agg_safe.get('_total_credits') / df_agg_safe.get('is_approve'),
        授信额度USD=df_agg_safe.get('_total_credits') / df_agg_safe.get('is_approve') / FX_PHP_PER_USD,
        授信效率PHP=df_agg_safe.get('_total_credits') / df_agg_safe['授信申请件数'],
        动支率T0=df_agg_safe.get('_t0_order_fz') / df_agg_safe.get('is_approve'),
        动支率T1=df_agg_safe.get('_t1_order_fz') / df_agg_safe.get('is_approve'),
        动支率T3=df_agg_safe.get('_t3_order_fz') / df_agg_safe.get('is_approve'),
        动支率T7=df_agg_safe.get('_t7_order_fz') / df_agg_safe.get('is_approve'),
        动支率=df_agg_safe.get('_is_order_fz') / df_agg_safe.get('is_approve'),
        动支件数=df_agg_safe.get('_is_order_fz'),
        动支通过件数=df_agg_safe.get('_order_approve_cnt'),
        动支通过率=df_agg_safe.get('_order_approve_cnt') / df_agg_safe.get('_is_order_fz'),
        动支pending率=df_agg_safe.get('_order_pending_cnt') / df_agg_safe.get('_is_order_fz'),
        动支通过金额PHP=df_agg_safe.get('order_apprv_principal'),
        成交件数=df_agg_safe.get('_payout_cnt'),
        成交转化率=df_agg_safe.get('_payout_cnt') / df_agg_safe.get('is_approve'),
        成交金额PHP=df_agg_safe.get('payout_principal'),
        金额占比=(payout_amt / payout_den) if payout_amt is not None else np.nan,
        成交件均PHP=df_agg_safe.get('payout_principal') / df_agg_safe.get('_payout_cnt'),
        放款件均USD=df_agg_safe.get('payout_principal') / df_agg_safe.get('_payout_cnt') / FX_PHP_PER_USD,
        成交授信件均USD=df_agg_safe.get('payout_credits') / df_agg_safe.get('_payout_cnt') / FX_PHP_PER_USD,
        久期=df_agg_safe.get('sum_real_duration_principal') / df_agg_safe.get('repay_principal'),
        名义期限=df_agg_safe.get('dt_amt_sum_apr') / df_agg_safe.get('repay_principal'),
        IRR=df_agg_safe.get('sum_irr_principal') / df_agg_safe.get('sum_real_duration_principal'),
        APR=df_agg_safe.get('repay_interest') / df_agg_safe.get('dt_amt_sum_apr'),
    )

    # withdraw起点：成交转化率分母改为动支件数（对齐国内版 放款转化率=放款人数/动支人数）
    if stage == 'withdraw':
        df_result['成交转化率'] = df_agg_safe.get('_payout_cnt') / df_agg_safe.get('_is_order_fz')

    # 风险指标：金额逾期率 / 人头逾期率 / 到期件数
    for mob in RISK_MOBS:
        df_result[f'dpd{mob}'] = df_agg_safe.get(f'overdue_amt_{mob}') / df_agg_safe.get(f'expire_amt_{mob}')
        df_result[f'dpd{mob}cnt'] = df_agg_safe.get(f'overdue_cnt_{mob}') / df_agg_safe.get(f'expire_cnt_{mob}')
        df_result[f'出账{mob}'] = df_agg_safe.get(f'expire_cnt_{mob}')

    # lift（ALL 为各组加总，overall 风险比值不变，ALL 行 lift 自然为 1）
    row_group_levels = [0] if lift_base == 'row' and len(list_grp) > 1 else None
    risk_present = [f'dpd{mob}' for mob in RISK_MOBS if f'dpd{mob}' in df_result.columns]
    if risk_present:
        df_result = df_result.assign(**_build_lift_assignments_ph(
            df_result=df_result,
            lift_base=lift_base,
            row_group_levels=row_group_levels
        ))

    # ---------------------- 列名规整 + 输出 ----------------------
    rename_map = {'授信通过总额度PHP': '授信通过总额度(PHP)', '授信件均PHP': '授信件均(PHP)',
                  '授信额度USD': '授信额度($)', '授信效率PHP': '授信效率(PHP)',
                  '动支通过金额PHP': '动支通过金额(PHP)',
                  '成交金额PHP': '成交金额(PHP)', '成交件均PHP': '成交件均(PHP)',
                  '放款件均USD': '放款件均($)', '成交授信件均USD': '成交-授信件均($)'}
    df_result = df_result.rename(columns=rename_map)

    out = df_result[columns_output]
    if show:
        styled = _style_indicator_df(out, _build_format_dict(columns_output))
        return _as_jupyter_styled_frame(out, styled)
    return out

# ===================== 透视表（pivot） =====================
_DEFAULT_PIVOT_INDICATORS = {
    'credit': ['计数', '占比', '授信通过率', '授信件均($)', '动支率T1', '动支率', '成交转化率'],
    'withdraw': ['计数', '占比', '动支通过率', '成交转化率', '放款件均($)'],
    'payout': ['计数', '占比', '金额占比', '放款件均($)'],
}

# generate_indicator / generate_pivot / quick_dim_cross 会读到的金额/定价列（存在才取）
_ANALYSIS_METRIC_COLS = (
    'credit_apply_no', 'user_id', 'loan_apply_no', 'is_credit',
    'credit_amount_after', 'total_credits',
    'order_apprv_credits', 'order_apprv_principal',
    'payout_principal', 'payout_credits',
    'available_amount_before', 'available_amount_after',
    'deal_amt_dt',
    'repay_principal', 'repay_interest', 'repay_principal_add_interest',
    'real_duration', 'apr_duration', 'price',
    'sum_real_duration_principal', 'sum_irr_principal', 'dt_amt_sum_apr',
)


def _as_name_list(names):
    """把 id/ind/col 统一成列名列表。"""
    if names is None:
        return []
    if isinstance(names, (list, tuple, set, pd.Index)):
        return list(names)
    return [names]


def _collect_needed_cols(df, extra_cols, risk_mobs, extra_wanted=()):
    """
    收集分析函数实际会用到的列（含 indicator 表别名），避免对整张宽表 copy。
    风险列只保留传入的 risk_mobs；extra_wanted 用于老客 overdue/expire_cycle* 等额外列。
    """
    wanted = set(_as_name_list(extra_cols))
    wanted.update(_as_name_list(extra_wanted))
    for flag_col, src_col in _FLAG_FROM_INDICATOR:
        wanted.add(flag_col)
        wanted.add(src_col)
    wanted.update(_ANALYSIS_METRIC_COLS)
    for mob in risk_mobs:
        wanted.update(_risk_cols(mob))
        wanted.add(f'dpd{mob}')
    return [c for c in df.columns if c in wanted]


def _collect_generate_pivot_cols(df, id_col, ind, col, risk_ind_list):
    """generate_pivot 用到的列：维度 + 漏斗/定价 + 本次 risk_ind。"""
    return _collect_needed_cols(df, _as_name_list(id_col) + _as_name_list(ind) + _as_name_list(col), risk_ind_list)


def _take_columns(df, cols):
    """按列用 ndarray 抽出新表，避免对分箱后碎片化宽表做 BlockManager 重整。"""
    return pd.DataFrame({c: df[c].to_numpy(copy=True) for c in cols})


def _add_sum_margins(pt):
    """对 sum/count 透视补 All 行/列，结果与 pivot_table(margins=True) 一致。"""
    if pt is None or pt.empty:
        return pt
    out = pt.copy()
    out['All'] = out.sum(axis=1)
    if isinstance(out.index, pd.MultiIndex):
        # 必须带列indexer，否则 .loc[元组] 会被当成 (行, 列) 定位而多建一列
        all_idx = tuple(['All'] + [''] * (out.index.nlevels - 1))
        out.loc[all_idx, :] = out.sum(axis=0)
    else:
        out.loc['All'] = out.sum(axis=0)
    return out


def _series_to_pivot(s, col_list, margins=True):
    """groupby 后的 Series unstack 成与 pivot_table 相同的二维表。"""
    level = col_list if len(col_list) > 1 else col_list[0]
    pt = s.unstack(level)
    if margins:
        pt = _add_sum_margins(pt)
    return pt


_PIVOT_FUNNEL_SUM_CANDIDATES = [
    'is_approve', 'credit_amount_after',
    't0_order', 't1_order', 't3_order', 't7_order',
    'is_order', 'is_order_approve', 'is_order_pending',
    'is_payout', 'payout_principal',
    'sum_real_duration_principal', 'repay_principal',
    'sum_irr_principal', 'repay_interest', 'dt_amt_sum_apr',
]


def _filter_pivot_stage(dfw, stage):
    """按分析起点过滤透视样本。"""
    if stage == 'withdraw':
        if 'is_order' not in dfw.columns:
            raise ValueError("stage='withdraw' 需要 is_order 列（或 indicator 表的 is_order_fz 列）")
        dfw = dfw[dfw['is_order'] == 1]
    elif stage == 'payout':
        if 'is_payout' not in dfw.columns:
            raise ValueError("stage='payout' 需要 is_payout 列（或 indicator 表的 payout_cnt 列）")
        dfw = dfw[dfw['is_payout'] == 1]
    if len(dfw) == 0:
        raise ValueError(
            f"stage={stage!r} 过滤后样本为空，无法透视。"
            "请检查筛选条件（odps 导出的列常为字符串，如 == 1 需写成 == '1'）"
        )
    return dfw.reset_index(drop=True)


def _build_pivot_funnel_indicators(dfw, id_col, col_list, keys, stage, margins, extra_sum_cols=()):
    """一次 groupby 后生成漏斗/定价类透视指标，供 generate_pivot / generate_old_pivot 共用。"""
    sum_candidates = list(_PIVOT_FUNNEL_SUM_CANDIDATES) + list(extra_sum_cols)
    sum_cols = [c for c in sum_candidates if c in dfw.columns]
    g = dfw.groupby(keys, dropna=True, observed=False, sort=True)
    cnt_s = g[id_col].count()
    sums = g[sum_cols].sum() if sum_cols else None

    def _pt(metric_s):
        return _series_to_pivot(metric_s, col_list, margins)

    def _has(name):
        return sums is not None and name in sums.columns

    available_indicators = {}
    cnt_pivot = _pt(cnt_s)
    if margins:
        total_count = cnt_pivot.iloc[:-1, :-1].sum().sum()
    else:
        total_count = cnt_pivot.sum().sum()
    available_indicators['计数'] = cnt_pivot
    available_indicators['占比'] = cnt_pivot.div(total_count)

    if _has('is_approve'):
        sx_pivot = _pt(sums['is_approve'])
        available_indicators['授信通过率'] = (sx_pivot / cnt_pivot).fillna(0)
    if _has('credit_amount_after'):
        credit_pivot = _pt(sums['credit_amount_after'])
        available_indicators['授信效率($)'] = (credit_pivot / cnt_pivot / FX_PHP_PER_USD).fillna(0)
        if '授信通过率' in available_indicators:
            available_indicators['授信件均($)'] = (
                available_indicators['授信效率($)'] / available_indicators['授信通过率'].replace(0, np.nan)
            ).fillna(0)
        else:
            pos = dfw['credit_amount_after'].to_numpy() > 0
            g_pos = dfw.loc[pos].groupby(keys, dropna=True, observed=False, sort=True)['credit_amount_after']
            avg_credit = _pt(g_pos.sum()) / _pt(g_pos.count()).replace(0, np.nan)
            available_indicators['授信件均($)'] = (avg_credit / FX_PHP_PER_USD).fillna(0)

    if '授信通过率' in available_indicators:
        sx_pivot = available_indicators['授信通过率'] * cnt_pivot
    else:
        sx_pivot = cnt_pivot
    for t in ['t0', 't1', 't3', 't7']:
        if _has(f'{t}_order'):
            t_pivot = _pt(sums[f'{t}_order'])
            available_indicators[f'动支率{t.upper()}'] = (t_pivot / sx_pivot.replace(0, np.nan)).fillna(0)
    order_pivot = None
    if _has('is_order'):
        order_pivot = _pt(sums['is_order'])
        available_indicators['动支率'] = (order_pivot / sx_pivot.replace(0, np.nan)).fillna(0)
        if _has('is_order_approve'):
            available_indicators['动支通过率'] = (
                _pt(sums['is_order_approve']) / order_pivot.replace(0, np.nan)
            ).fillna(0)
        if _has('is_order_pending'):
            available_indicators['动支pending率'] = (
                _pt(sums['is_order_pending']) / order_pivot.replace(0, np.nan)
            ).fillna(0)

    if _has('is_payout'):
        deal_cnt_pivot = _pt(sums['is_payout'])
        available_indicators['成交件数'] = deal_cnt_pivot
        base_pivot = order_pivot if order_pivot is not None and stage != 'credit' else sx_pivot
        available_indicators['成交转化率'] = (deal_cnt_pivot / base_pivot.replace(0, np.nan)).fillna(0)
        if _has('payout_principal'):
            deal_amt_pivot = _pt(sums['payout_principal'])
            available_indicators['成交金额'] = deal_amt_pivot
            if margins:
                total_amt = deal_amt_pivot.iloc[:-1, :-1].sum().sum()
            else:
                total_amt = deal_amt_pivot.sum().sum()
            available_indicators['金额占比'] = deal_amt_pivot.div(total_amt) if total_amt != 0 else deal_amt_pivot * np.nan
            available_indicators['放款件均($)'] = (deal_amt_pivot / deal_cnt_pivot.replace(0, np.nan) / FX_PHP_PER_USD).fillna(0)

    if _has('sum_real_duration_principal') and _has('repay_principal'):
        available_indicators['久期'] = (
            _pt(sums['sum_real_duration_principal']) / _pt(sums['repay_principal']).replace(0, np.nan)
        ).fillna(0)
    if _has('sum_irr_principal') and _has('sum_real_duration_principal'):
        available_indicators['IRR'] = (
            _pt(sums['sum_irr_principal']) / _pt(sums['sum_real_duration_principal']).replace(0, np.nan)
        ).fillna(0)
    if _has('repay_interest') and _has('dt_amt_sum_apr'):
        available_indicators['APR'] = (
            _pt(sums['repay_interest']) / _pt(sums['dt_amt_sum_apr']).replace(0, np.nan)
        ).fillna(0)
    if _has('credit_amount_after') and 'payout_credits' in dfw.columns and _has('is_payout'):
        paid = dfw['is_payout'].to_numpy() == 1
        g_paid = dfw.loc[paid].groupby(keys, dropna=True, observed=False, sort=True)['credit_amount_after']
        avg_credit_out = _pt(g_paid.sum()) / _pt(g_paid.count()).replace(0, np.nan)
        available_indicators['平均授信额度($)'] = avg_credit_out / FX_PHP_PER_USD

    return available_indicators, sums, _pt, _has


def _assemble_pivot_result(available_indicators, indicator, extra_names, show):
    """
    按 indicator + 风险列顺序拼接多层列透视，可选 Jupyter 条件格式。
    指标名去重：风险指标同时出现在 indicator 和自动追加的风险列时只保留一份，
    否则同名指标块会被拼两遍（导出后表头合并成一块，分箱看起来被打散）。
    """
    selected_indicators = []
    selected_keys = []
    for ind_name in indicator:
        if ind_name in selected_keys:
            continue
        if ind_name in available_indicators:
            selected_indicators.append(available_indicators[ind_name])
            selected_keys.append(ind_name)
        else:
            print(f"警告: 指标 '{ind_name}' 不存在，已跳过")
    for ind_name in extra_names:
        if ind_name in selected_keys:
            continue
        if ind_name in available_indicators:
            selected_indicators.append(available_indicators[ind_name])
            selected_keys.append(ind_name)
    if not selected_indicators:
        raise ValueError("没有选中任何有效指标")
    final_result = pd.concat(selected_indicators, axis=1, keys=selected_keys)
    if show:
        styled = _style_pivot_df(final_result)
        return _as_jupyter_styled_frame(final_result, styled)
    return final_result


def generate_pivot(df, id_col, ind, col, margins=True, stage='credit', risk_ind=None,
                   indicator=None, lift_base='overall', show=True):
    """
    生成菲律宾授信业务多维度透视表，涵盖件数计数、授信/动支/成交转化、风险表现等指标。

    Args:
        df (pd.DataFrame): order_trace/indicator 底表数据
        id_col (str): 唯一标识列名（如'credit_apply_no'），用于计数
        ind (str/list): 透视表的行维度
        col (str/list): 透视表的列维度
        margins (bool, optional): 是否显示总计行/列，默认True
        stage (str, optional): 分析起点 'credit'（默认）/'withdraw'/'payout'，
            决定样本范围与默认可选指标集
        risk_ind (str/list, optional): 风险表现点（如 '1_7'/'2_7'），
            默认取 RISK_MOBS 中该stage常见的点：
            credit -> ['1_7']，withdraw/payout -> ['1_7','2_7','3_7']
        indicator (list, optional): 需展示的核心指标列表，默认按stage取
            _DEFAULT_PIVOT_INDICATORS。可选指标：
            '计数','占比','授信通过率','授信效率($)','授信件均($)','动支率T0/T1/T3/T7','动支率',
            '动支通过率','动支pending率','成交转化率','成交件数','成交金额','放款件均($)',
            '金额占比','平均授信额度($)','久期','IRR','APR'
        lift_base (str, optional): 风险lift分母基准，'overall'全局/'row'行汇总
        show (bool, optional): True（默认）时 Jupyter 以条件格式渲染返回的表；
            返回值仍是 DataFrame。数字格式对齐 generate_indicator；
            dpd / dpd cnt 为绿-黄-红色阶，dpd lift 为白-红色阶（越大越红）。

    Returns:
        pd.DataFrame: 多层列索引的透视表
    """
    _validate_stage(stage)
    _validate_lift_base(lift_base)

    if risk_ind is None:
        risk_ind = ['1_7'] if stage == 'credit' else ['1_7', '2_7', '3_7']
    risk_ind_list = [risk_ind] if isinstance(risk_ind, str) else list(dict.fromkeys(risk_ind))
    for r in risk_ind_list:
        _validate_risk_mob(r)
    if indicator is None:
        indicator = _DEFAULT_PIVOT_INDICATORS[stage]

    # 只抽出透视用到的列（ndarray，避免碎片化宽表重整）
    keep_cols = _collect_generate_pivot_cols(df, id_col, ind, col, risk_ind_list)
    dfw = _filter_pivot_stage(_normalize_ph_base(_take_columns(df, keep_cols), copy=False), stage)

    ind_list = _as_name_list(ind)
    col_list = _as_name_list(col)
    keys = ind_list + col_list

    ovd_flag_cols = {}
    extra_sum_cols = []
    for r_ind in risk_ind_list:
        oa = f'overdue_amt_{r_ind}'
        extra_sum_cols.extend([oa, f'expire_amt_{r_ind}', f'expire_cnt_{r_ind}'])
        if oa in dfw.columns:
            flag = f'_ovd_flag_{r_ind}'
            dfw[flag] = (dfw[oa].to_numpy() > 0).astype(np.int8)
            ovd_flag_cols[r_ind] = flag
            extra_sum_cols.append(flag)

    available_indicators, sums, _pt, _ = _build_pivot_funnel_indicators(
        dfw, id_col, col_list, keys, stage, margins, extra_sum_cols=extra_sum_cols
    )

    # 每个 risk_ind 的风险指标（金额/件数/lift/出账）
    all_risk_indicator_names = []
    for r_ind in risk_ind_list:
        overdue_pt = _pt(sums[f'overdue_amt_{r_ind}'])
        expire_pt = _pt(sums[f'expire_amt_{r_ind}'])
        m_risk = (overdue_pt / expire_pt.replace(0, np.nan)).fillna(0)
        risk_pivot1 = pd.concat(
            {f'overdue_amt_{r_ind}': overdue_pt, f'expire_amt_{r_ind}': expire_pt},
            axis=1
        )
        m_risklift = _calculate_pivot_risk_lift_ph(
            m_risk=m_risk,
            risk_pivot1=risk_pivot1,
            mob=r_ind,
            margins=margins,
            lift_base=lift_base
        )

        risk_pivot2 = _pt(sums[f'expire_cnt_{r_ind}'])
        flag_col = ovd_flag_cols.get(r_ind)
        if flag_col is None:
            numerator_pivot = risk_pivot2 * 0
        else:
            numerator_pivot = _pt(sums[flag_col])
        m_riskcnt = (numerator_pivot / risk_pivot2.replace(0, np.nan)).fillna(0)

        available_indicators[f'dpd{r_ind}'] = m_risk
        available_indicators[f'dpd{r_ind}lift'] = m_risklift
        available_indicators[f'dpd{r_ind}cnt'] = m_riskcnt
        available_indicators[f'{r_ind}出账'] = risk_pivot2
        all_risk_indicator_names.extend([f'dpd{r_ind}', f'dpd{r_ind}lift', f'dpd{r_ind}cnt', f'{r_ind}出账'])

    return _assemble_pivot_result(available_indicators, indicator, all_risk_indicator_names, show)


def generate_grouped_pivot(df, ind, col, metrics):
    """Reaggregate declared additive components; compute ratios after sums, including All."""
    row_keys = _as_name_list(ind)
    column_keys = _as_name_list(col)
    keys = row_keys + column_keys
    if not row_keys or not column_keys or len(set(keys)) != len(keys) or not metrics:
        raise ValueError('Provide disjoint row/column dimensions and explicit metrics')
    components = []
    for definition in metrics.values():
        if definition.get('op') == 'sum':
            components.append(definition['column'])
        elif definition.get('op') == 'ratio':
            components.extend([definition['numerator'], definition['denominator']])
        else:
            raise ValueError('Only additive sums and ratios are supported; no mean-of-rates or distinct summation')
    components = list(dict.fromkeys(components))
    if set(components) & set(keys):
        raise ValueError('Components cannot also be dimensions')
    missing = set(keys + components) - set(df.columns)
    if missing:
        raise ValueError(f'Missing grouped columns: {sorted(missing)}')
    work = df[keys + components].copy()
    if work.empty or any(not pd.api.types.is_numeric_dtype(work[name]) for name in components):
        raise ValueError('Nonempty numeric additive components required')
    for key in keys:
        if work[key].isin(['All']).any():
            raise ValueError('All is reserved for calculated margins, not an input grouping level')
        work[key] = work[key].astype(object).where(work[key].notna(), '__MISSING__')
    cells = work.groupby(keys, observed=True, dropna=False, sort=False)[components].sum(min_count=1)
    row_totals = work.groupby(row_keys, observed=True, dropna=False, sort=False)[components].sum(min_count=1)
    column_totals = work.groupby(column_keys, observed=True, dropna=False, sort=False)[components].sum(min_count=1)
    all_row = 'All' if len(row_keys) == 1 else tuple(['All'] + [''] * (len(row_keys) - 1))
    all_column = 'All' if len(column_keys) == 1 else tuple(['All'] + [''] * (len(column_keys) - 1))
    tables = {}
    for component in components:
        table = cells[component].unstack(column_keys, fill_value=0)
        table[all_column] = row_totals[component].reindex(table.index)
        totals = column_totals[component].reindex(table.columns).copy()
        totals.loc[all_column] = work[component].sum(min_count=1)
        margin_index = pd.Index([all_row], name=table.index.name) if len(row_keys) == 1 else pd.MultiIndex.from_tuples([all_row], names=row_keys)
        margin = pd.DataFrame([totals.to_numpy()], index=margin_index, columns=table.columns)
        tables[component] = pd.concat([table, margin])
    output = {}
    for name, definition in metrics.items():
        if definition['op'] == 'sum':
            output[name] = tables[definition['column']]
        else:
            output[name] = tables[definition['numerator']] / tables[definition['denominator']].replace(0, np.nan)
    return pd.concat(output, axis=1)


def generate_old_pivot(df, id_col, ind, col, margins=True, stage='payout', risk_ind=None,
                       indicator=None, lift_base='overall', show=True):
    """
    生成带格式的老客复借风险透视表：漏斗/定价指标与 generate_pivot 相同，
    风险改为 ever 逾期人头口径。

        cycle2_ever3 = Σoverdue_cycle2_ever3 / Σexpire_cycle2_ever3

    Args:
        df (pd.DataFrame): order_trace/indicator 底表（需含 overdue_cycle* / expire_cycle*）
        id_col (str): 唯一标识列名（如'user_id'/'credit_apply_no'），用于计数
        ind (str/list): 透视表的行维度
        col (str/list): 透视表的列维度
        margins (bool, optional): 是否显示总计行/列，默认True
        stage (str, optional): 分析起点，默认'payout'（老客风险通常看成交样本）
        risk_ind (str/list, optional): 老客 ever 表现点，默认 ['cycle2_ever3']。
            可选 OLD_RISK_MOBS：cycle2_ever3 / cycle2_ever7 / cycle4_ever3 / cycle4_ever7
        indicator (list, optional): 漏斗指标，默认按stage取 _DEFAULT_PIVOT_INDICATORS
        lift_base (str, optional): 风险lift分母基准，'overall'全局/'row'行汇总
        show (bool, optional): True（默认）时 Jupyter 以条件格式渲染；
            cycle*_ever* 为绿-黄-红色阶，lift 为白-红色阶（越大越红）。

    Returns:
        pd.DataFrame: 多层列索引的透视表。风险块为
            {risk_ind}（ever逾期率）、{risk_ind}lift、{risk_ind}出账（到期件数）
    """
    _validate_stage(stage)
    _validate_lift_base(lift_base)

    if risk_ind is None:
        risk_ind = ['cycle2_ever3']
    risk_ind_list = [risk_ind] if isinstance(risk_ind, str) else list(dict.fromkeys(risk_ind))
    for r in risk_ind_list:
        _validate_old_risk_mob(r)
    if indicator is None:
        indicator = _DEFAULT_PIVOT_INDICATORS[stage]

    extra_wanted = []
    for r in risk_ind_list:
        extra_wanted.extend(_old_risk_cols(r))
    keep_cols = _collect_needed_cols(
        df,
        _as_name_list(id_col) + _as_name_list(ind) + _as_name_list(col),
        risk_mobs=[],
        extra_wanted=extra_wanted,
    )
    dfw = _filter_pivot_stage(_normalize_ph_base(_take_columns(df, keep_cols), copy=False), stage)

    missing = [c for c in extra_wanted if c not in dfw.columns]
    if missing:
        raise ValueError(f"generate_old_pivot 缺少列: {missing}")
    for c in extra_wanted:
        dfw[c] = _as_numeric(dfw[c]).fillna(0)

    ind_list = _as_name_list(ind)
    col_list = _as_name_list(col)
    keys = ind_list + col_list

    available_indicators, sums, _pt, _ = _build_pivot_funnel_indicators(
        dfw, id_col, col_list, keys, stage, margins, extra_sum_cols=extra_wanted
    )

    all_risk_indicator_names = []
    for r_ind in risk_ind_list:
        overdue_col, expire_col = _old_risk_cols(r_ind)
        overdue_pt = _pt(sums[overdue_col])
        expire_pt = _pt(sums[expire_col])
        m_risk = (overdue_pt / expire_pt.replace(0, np.nan)).fillna(0)
        risk_pivot1 = pd.concat({overdue_col: overdue_pt, expire_col: expire_pt}, axis=1)
        m_risklift = _calculate_pivot_risk_lift_ph(
            m_risk=m_risk,
            risk_pivot1=risk_pivot1,
            mob=r_ind,
            margins=margins,
            lift_base=lift_base,
            overdue_key=overdue_col,
            expire_key=expire_col,
        )
        available_indicators[r_ind] = m_risk
        available_indicators[f'{r_ind}lift'] = m_risklift
        available_indicators[f'{r_ind}出账'] = expire_pt
        all_risk_indicator_names.extend([r_ind, f'{r_ind}lift', f'{r_ind}出账'])

    return _assemble_pivot_result(available_indicators, indicator, all_risk_indicator_names, show)


# ===================== 维度交叉（crosstab） =====================
def quick_dim_cross(df, ind, col, mob, credit_out=False, stage='payout'):
    """
    生成维度交叉的风险表现汇总表（菲律宾口径），涵盖金额风险、件数风险、到期件数及占比。

    Args:
        df (pd.DataFrame): 原始数据（需含 overdue_amt_{mob}/expire_amt_{mob}/expire_cnt_{mob} 等列）
        ind (str): 透视表行维度字段名
        col (str): 透视表列维度字段名
        mob (str): 风险表现点标识（如 '1_7'、'3_7'）
        credit_out (bool): 是否输出额度字段（平均授信额度($)）
        stage (str): 样本范围，'payout'（默认，仅成交样本）/'withdraw'/'credit'（全量）

    Returns:
        pd.DataFrame: 多层列索引（外层=指标名，内层=列维度），包含：
            - dpd{mob}: 金额风险（逾期金额/到期金额）
            - dpd{mob}cnt: 件数风险（逾期件数/到期件数）
            - 出账{mob}: 到期件数（出账件数）
            - 出账{mob}占比: 到期件数占全量比例
            - 平均授信额度($)（可选）
        含All汇总（margins=True）
    """
    _validate_stage(stage)
    keep_cols = _collect_needed_cols(df, [ind, col], [mob])
    dfw = _normalize_ph_base(_take_columns(df, keep_cols), copy=False)
    if stage == 'withdraw':
        dfw = dfw[dfw['is_order'] == 1]
    elif stage == 'payout':
        dfw = dfw[dfw['is_payout'] == 1]
    dfw = dfw.reset_index(drop=True)

    overdue_col, expire_amt_col, overdue_cnt_col, expire_cnt_col = _risk_cols(mob)
    sum_cols = [c for c in (overdue_col, expire_amt_col, overdue_cnt_col, expire_cnt_col) if c in dfw.columns]
    g = dfw.groupby([ind, col], dropna=True, observed=False, sort=True)
    sums = g[sum_cols].sum()

    def _pt(name):
        return _series_to_pivot(sums[name], [col], margins=True)

    # 金额风险 = 逾期金额/到期金额
    df_amt_risk = _pt(overdue_col) / _pt(expire_amt_col).replace(0, np.nan)
    # 件数风险 = 逾期件数/到期件数
    df_cnt_risk = _pt(overdue_cnt_col) / _pt(expire_cnt_col).replace(0, np.nan)
    # 到期（出账）件数
    df_performance = _pt(expire_cnt_col)

    # 出账占比（仅对 ind、col 非NaN的样本计算分母）
    total_cnt = dfw.loc[dfw[ind].notna() & dfw[col].notna(), expire_cnt_col].sum()
    df_performance_pct = df_performance / total_cnt if total_cnt != 0 else df_performance * np.nan

    pieces = [df_amt_risk, df_cnt_risk, df_performance, df_performance_pct]
    keys = [f'dpd{mob}', f'dpd{mob}cnt', f'出账{mob}', f'出账{mob}占比']
    if credit_out and 'credit_amount_after' in dfw.columns:
        g_credit = dfw.groupby([ind, col], dropna=True, observed=False, sort=True)['credit_amount_after']
        df_credit = (
            _series_to_pivot(g_credit.sum(), [col], margins=True)
            / _series_to_pivot(g_credit.count(), [col], margins=True).replace(0, np.nan)
            / FX_PHP_PER_USD
        )
        pieces.append(df_credit)
        keys.append('平均授信额度($)')

    return pd.concat(pieces, axis=1, keys=keys)


# 为crosstab/quick_dim_cross生成的DataFrame刷格式
def styled_dim_cross(df_result):
    """
    为crosstab/quick_dim_cross生成的DataFrame整块数据区域刷格式：
    1. 整块数值列统一百分比格式
    2. 整块数值列统一数据条（同比例尺）

    Args:
        df_result (pd.DataFrame): 交叉表(crosstab)或quick_dim_cross结果

    Returns:
        pd.io.formats.style.Styler: 整块美化后的Styler对象
    """
    pattern_ratio = re.compile(r'^dpd\d+_\d+$')
    pattern_cnt = re.compile(r'^dpd\d+_\d+cnt$')
    pattern_pct = re.compile(r'^出账\d+_\d+占比$')
    pattern_perform = re.compile(r'^出账\d+_\d+$')

    cols = df_result.columns
    level0 = cols.get_level_values(0)

    format_pct_cols = [col for col in list(set(level0)) if (pattern_ratio.match(col) or pattern_cnt.match(col) or pattern_pct.match(col))]
    format_bar_cols = [col for col in list(set(level0)) if (pattern_ratio.match(col) or pattern_cnt.match(col))]
    format_int_cols = [col for col in list(set(level0)) if pattern_perform.match(col)] + ['平均授信额度($)']
    format_int_cols = [c for c in format_int_cols if c in set(level0)]

    data_min = df_result[format_bar_cols].min().min()
    data_max = df_result[format_bar_cols].max().max()

    styled_df = (
        df_result.style
        .format('{:.2%}', subset=pd.IndexSlice[:, format_pct_cols])
        .bar(
            subset=pd.IndexSlice[:, format_bar_cols],
            color='#FFD966',
            align='left',
            vmin=data_min,
            vmax=data_max
        )
        .format('{:,.0f}', subset=pd.IndexSlice[:, format_int_cols])
    )

    return styled_df


# 生成ovd target（基于菲律宾逾期金额列）
def generate_ovd(df, mob, stage='payout'):
    """
    基于逾期金额列生成二分类风险target：
        is_ovd_{mob} = 1 当 到期且逾期金额>0；0 当 到期且未逾期；到期前/未到期为 NaN。

    菲律宾口径与国内版差异：国内版用 出账{mob}应还本金/剩余应还本金 判断，
    菲律宾用 expire_amt_{mob}（到期本金）与 overdue_amt_{mob}（逾期金额）。

    Args:
        df (pd.DataFrame): 含逾期金额/到期金额列的数据（会就地新增 is_ovd_{mob} 列）
        mob (str/list): 风险表现点，如 '3_7'，支持列表
        stage (str, optional): 保留参数，默认'payout'（未放款样本无风险字段，自然为NaN）

    Returns:
        pd.DataFrame: 原df（就地新增 is_ovd_{mob} 列）
    """
    mob_list = [mob] if isinstance(mob, str) else list(mob)
    for m in mob_list:
        overdue_col, expire_amt_col, _, _ = _risk_cols(m)
        if f'is_ovd_{m}' not in df.columns:
            df[f'is_ovd_{m}'] = np.select([(df[expire_amt_col] > 0) & (df[overdue_col] > 0),
                                           (df[expire_amt_col] > 0) & (df[overdue_col] <= 0)],
                                          [1, 0],
                                          default=np.nan)
    return df


# 规则组合挖掘
def high_lift_combinations_categorical(df, creditlevel_col='cre_rating', target_col='is_ovd_3_7',
                                       var_cols=None, var_values_dict=None,
                                       min_lift=1.5, min_coverage=0.05,
                                       max_combination=2):
    """
    枚举类别变量取特定值的组合，计算对目标变量的提升度（Lift）。
    默认参数为菲律宾口径：按授信评级(cre_rating)分组、目标为 is_ovd_3_7。

    规则形式：var1 == 'value1' & var2 == 'value2' & ...

    Parameters
    ----------
    df : pandas.DataFrame
        输入数据集。
    creditlevel_col : str
        信用等级列名（默认'cre_rating'，菲律宾授信评级），函数将按该列分组计算lift。
    target_col : str
        目标变量列名（二值 0/1，默认'is_ovd_3_7'，可用 generate_ovd 生成）。
    var_cols : list of str, optional
        用于组合的类别变量名列表。若为 None，则自动使用除 creditlevel_col 和 target_col 之外的所有列。
    var_values_dict : dict, optional
        指定每个变量要考虑的取值列表，格式：{var: [value1, value2, ...]}。
        若不提供，则每个变量将使用其在该信用等级分组中出现的所有唯一值（缺失值会被忽略）。
    min_lift : float
        提升度阈值。
    min_coverage : float
        覆盖率阈值。
    max_combination : int
        组合的最大长度（即规则中包含多少个原子条件）。

    Returns
    -------
    pandas.DataFrame
        满足条件的规则，按 lift 降序排列。
    """
    from itertools import combinations

    if var_cols is None:
        var_cols = [col for col in df.columns if col not in [creditlevel_col, target_col]]

    results = []

    for creditlevel in df[creditlevel_col].unique():
        creditlevel_df = df[df[creditlevel_col] == creditlevel]
        n_total = len(creditlevel_df)
        base_rate = creditlevel_df[target_col].mean()

        if not base_rate or np.isnan(base_rate):
            continue

        local_values_dict = {}
        for var in var_cols:
            if var_values_dict and var in var_values_dict:
                values = var_values_dict[var]
            else:
                values = creditlevel_df[var].dropna().unique().tolist()
            local_values_dict[var] = values

        atomic_conds = []
        for var, vals in local_values_dict.items():
            for v in vals:
                atomic_conds.append((var, v))

        for combo_size in range(1, max_combination + 1):
            for combo in combinations(atomic_conds, combo_size):
                vars_in_combo = [cond[0] for cond in combo]
                if len(set(vars_in_combo)) != len(vars_in_combo):
                    continue

                mask = pd.Series([True] * len(creditlevel_df), index=creditlevel_df.index)
                for var, val in combo:
                    mask &= (creditlevel_df[var] == val)

                hit_cnt = mask.sum()
                if hit_cnt == 0:
                    continue

                hit_rate = creditlevel_df.loc[mask, target_col].mean()
                coverage = hit_cnt / n_total
                lift = hit_rate / base_rate

                if lift >= min_lift and coverage >= min_coverage:
                    rule_str = ' & '.join([f"{var} == '{val}'" for var, val in combo])
                    results.append({
                        'creditlevel': creditlevel,
                        'rule': rule_str,
                        'base_rate': base_rate,
                        'rule_target_rate': hit_rate,
                        'lift': lift,
                        'coverage': coverage,
                        'hit_count': hit_cnt
                    })

    if results:
        return pd.DataFrame(results).sort_values('lift', ascending=False)
    else:
        print('无符合条件的规则')
        return pd.DataFrame()

