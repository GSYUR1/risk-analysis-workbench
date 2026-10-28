# 工作台工具

本地审阅候选，不包含真实数据库接入。工具调用顺序见 `工作约定/工具调用与分析输出.md`。

| 文件 | 职责 |
|---|---|
| workbench.py | 分析事项、窗口关联、归档与检索CLI；通过--root定位工作台 |
| core.py | 文件状态、项目与事项生命周期、归档及知识索引 |
| ph_data_analysis.py | 原有指标、分箱、透视计算；保留算法和常量，须审核共享权限 |
| excel_export.py | 标准指标/透视结果的格式化导出，不承担取数 |
| pivot_base_sql.py | 基于profile生成SQL及检查SQL，不连接数据库 |
| analysis_base.py | 保存共用Parquet底表、SQL和数据口径清单，离线补充交叉；操作见analysis_base使用说明.md |
| tests/ | 合成数据与临时目录离线验证 |

计算仍优先使用现成函数，标准表格仍调用 `merge_formatted_pivot_to_excel` / `merge_formatted_indicator_to_excel`。ph数据口径目录完整保留；函数缺口再读取对应定义，不自行编造指标。

明细使用generate_pivot，可加总聚合组件使用generate_grouped_pivot；同底数先保存共用数据包再出报告，后续补表先复用底表。保存顺序、边界和验收以《工具调用与分析输出》为准。

运行 `python -X utf8 -m unittest discover -s 工具/tests -v`。

基础依赖见requirements；最优分箱可选依赖optbinning单独安装。真实ODPS接入依赖外部Skill。当前工具不直接导入pyodps/chardet，发布版基础依赖不捆绑这些包；原工作台依赖文件不变。

SQL使用示例：`python -X utf8 工具/pivot_base_sql.py --spec 已确认的spec.json --output 新输出.sql`。默认profile原样保留源表定义；接入前仍须确认实时字段、唯一性、时点与完整明细公式证据。
