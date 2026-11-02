# 策略影响预估

第一期内置额度调整的静态情景模型。支持新增系数、旧新系数迁移、取消已生效系数；可选额度对动支率的影响。方法与边界见[方法说明](方法说明.md)，默认假设的唯一配置见[规则](规则/quota_elasticity.v1.json)。

## 输入与准备

- 在现有分析事项确认国家、产品、币种、样本窗、动支窗、风险定义和基准额度含义。
- 提供互斥叶子组的CSV，或明确sheet/range的单层表头XLSX。不是读取任意原生透视布局，也不保存原工作簿。
- 输入包括授信通过件数、平均额度、成交件数/金额、系数字段和所选风险率；启用动支影响必须提供同窗动支件数。
- 风险汇总须提供对应成熟分母，或显式选择不可汇总/成交金额近似模式；禁止静默改分母。
- 优先复用已有共用底表。聚合数据不作为明细flag重新计数；本工具不取数、不实现认证。

## 使用

把[专属模板](模板/strategy_impact_spec.json)复制到当前事项并填写。`parameters`只覆盖本次改变的参数，其余从规则解析；运行时保存完整resolved快照。不是读取本机外部Skill。

在工作台根目录运行以下合成示例，输出必须指定尚不存在的新目录：

```powershell
python -X utf8 .\工具\strategy_impact.py --input '.\分析功能\策略影响预估\示例\synthetic.csv' --spec '.\分析功能\策略影响预估\示例\spec.json' --output-dir '当前事项的新运行目录'
```

Python接口：`validate_impact_input`、`resolve_quota_factor`、`estimate_quota_impact`、`summarize_impact`。脚本仅承担该计算，不替代workbench事项管理。

## 输出

新运行目录保存原配置、解析后配置、逐组CSV、整体/指定维度汇总CSV、checks.json、公式化Excel及manifest.json。manifest记录输入、代码、规则、方法和所引用数据口径文件的SHA-256；不会把原客户明细另复制到工作台。

Excel保留Summary同页汇总、横向指标过程表、参数、逐组计算和基准输入。`column_dimension`指定一个列维度，其余维度作为行；不指定则使用标准单维/多层行索引表。Summary按summary_groups独立汇总，整体不重复参与加总。

标准表格调用excel_export。源输入为只读；结果另存。参数弹性与系数可在Excel里修改，公式随之重算；改变分组、数据范围、风险模式或口径必须重新执行Python校验。初始公式缓存由已校验Python结果写入，交付明确区分缓存核验与Excel引擎实际重算，禁止宣称前者等于后者。

真实业务报告仍按工作台规范完成视觉与业务验收；合成示例通过不代表真实效果验证。
