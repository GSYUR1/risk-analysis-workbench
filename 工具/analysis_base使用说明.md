# 保存共用底表与离线补充交叉

适用于同一组底数生成多张交叉表，也可在Notebook中继续操作。流程规则只在《工作约定/工具调用与分析输出》维护；本页说明工具接口。数据包放在实际业务项目的事项目录，不放在工作台公共工具目录。

聚合路径的数据流是：源端明细 → 取数SQL按待分析N维联合GROUP BY并SUM指标组件 → 已聚合df落盘 → Python从该df重组交叉维度。save_base只保存传入的df，不执行第一次源端聚合；generate_grouped_pivot只负责后续交叉重聚合，不要求先下载原始明细。明细路径仍可按任务需要选用，不强制所有分析都预聚合。

## 1. 首次分析：保存已经取得的df

依赖：pandas、numpy、pyarrow，以及现有标准Excel导出器的依赖。此工具不连接数据库；下载仍经odps-query及原有审批。明细在远程时将工具部署到原执行环境，在那里保存和运行，不必下载到本机。

在原分析脚本/Notebook完成取数、清洗和分箱后，调用：

```python
from analysis_base import save_base

save_base(
    df,
    analysis_dir / 'artifacts/bases/base_v1',
    contract,
    sql_paths=[analysis_dir / 'sql/extract.sql'],
    supporting_paths=[analysis_dir / 'scripts/prepare_base.py'],
)
```

`analysis_dir`是实际事项目录的Path，`contract`是已填写的数据口径字典；导入前将实际工作台的`工具`目录加入sys.path。上例不是自动执行的业务代码。没有预处理则可省略supporting_paths；有多段取数SQL、分箱配置或映射文件时全部传入，SQL不是清洗/分箱脚本的替代品。若已有Parquet：

```text
python analysis_base.py save --data base.parquet --contract contract.json --sql extract.sql --support prepare_base.py --output-dir <事项目录>/artifacts/bases/base_v1
```

`--sql`、`--support`均可重复传入。CSV须先显式指定dtype读入，再调用save_base，避免001被推断成1、NULL维度丢失。保存df全部列，不因当前交叉只用两维而裁掉其余已确认维度和指标。

数据包生成：

```text
base_v1/
  base.parquet          实际参与交叉计算的数据，非最终交叉结果
  manifest.json         样本/快照/粒度/字段/指标/哈希/工具版本
  sql/                  完整取数SQL副本
  support/              提供的预处理代码、切点等（如有）
  views.example.json    绑定此底表的交叉配置示例
  supplement.py         人工补表入口
```

## 2. 填写数据口径

聚合底表输入结构见`分析模板/base_contract.grouped.json`。所有示例值必须换为当前已核实口径；不得将示例指标集合当作任务完整指标集合。`required_columns`填全部约定字段，`additive_columns`填可加组件，`metrics`按原报告顺序完整配置，不默认缩减成少数风险指标。

- `kind=grouped`：`grain_keys`与`dimensions`相同，N维组合唯一；一个数据包只含一个最细分组层级。
- `additivity_evidence`：逐组件记录为何可以跨这些维度加总。普通申请件数在申请不跨组时可加，去重人数未必可加。工具只检查声明存在，不能替代业务证明。
- `metrics`支持`sum`和`ratio`，引用已声明组件，保留各指标定义证据。ratio是先加总再相除，零分母为空；不支持率的平均、直接跨箱SUM去重人数。
- 币种、换汇、成熟条件、分箱版本、来源表/分区、关联时点等保存在`definition_evidence`或补充字段及support文件中；工具不会自行推断或换算币种。
- 缺失维度保留为`__MISSING__`；`All`和`__MISSING__`为保留标签，原始真实类别不能使用。空输入、重复粒度、缺字段均报错。

明细路径的共同元数据相同，将`kind`改为`detail`，`grain_keys`设置为实际唯一申请/订单键，并配置：

```json
{
  "pivot_options": {
    "id_col": "credit_apply_no",
    "stage": "credit",
    "risk_ind": ["1_7"],
    "indicator": ["计数", "占比"],
    "lift_base": "overall"
  }
}
```

此代码块仅展示差异字段，不是完整contract；不需要聚合路径的additive_columns/additivity_evidence/metrics。明细仍用原`generate_pivot`，需唯一且非空id_col；风险点及指标列表按任务完整填写，risk_ind=[]表示不生成风险指标。底表须包含原函数所需全部原始组件。补表入口另外保留NULL维度，并对零成熟分母的风险指标置空。这个入口的“计数”仍是申请/订单件数，不将其冒充客户去重人数。

## 3. 人工检查：只改交叉配置，再运行脚本

复制数据包的`views.example.json`为本次`views.json`，保留base_manifest_sha256，只改views：

```json
[
  {"name": "评级_渠道", "ind": ["rating"], "col": ["channel"]},
  {"name": "月份_渠道", "ind": ["month"], "col": ["channel"], "filters": {"rating": ["A", null]}}
]
```

这是views数组示例；字段名替换成已保存维度。ind/col均支持多个字段，filters是等值集合筛选，null保留缺失值。省略metrics表示沿用完整指标列表；只有用户明确要求指标子集时才加metrics。不会自动遍历所有维度两两组合。

```text
python <数据包>/supplement.py --views <本次>/views.json --output-dir <事项目录>/runs/supplement_01
```

输出`supplement.xlsx`、`cross_00.csv`等结构化表、`views.json`和`run.json`。Excel使用工作台既有标准导出器，同次交叉表在同一Sheet纵向排列；不重写原报告，不代表自动嵌回原Summary。每次使用新目录，不能覆盖旧交付。移动工作台后可加`--tools-dir <当前工具目录>`，代码哈希须匹配。

## 4. Notebook继续探索

```python
from analysis_base import load_base, build_views

df, manifest = load_base(base_dir)
tables = build_views(df, manifest['contract'], [
    {'name': '补充交叉', 'ind': ['rating'], 'col': ['channel']},
])
tables['补充交叉']
```

CLI自动检查工具哈希并写运行证据；Notebook探索调用不自动记运行记录，正式交付回到supplement.py/run_views，或显式保存等价证据。缺维度、原始去重键、分母组件或改变分箱时不能仅修改views蒙混过关；需按流程补齐或新建base版本。未支持的复杂指标先检查现有ph函数，再查指标定义，为该数据包保留相应计算脚本和测试，不以简化公式替代。
