# 风控分析工作台 · 本地审阅候选

本目录由唯一源工作台自动生成，不在此手工修改代码。当前状态是 **LOCAL_REVIEW_ONLY**，不是已批准的公开发行版；没有随附许可证授权。

## 包含与不包含

- 包含：分析事项生命周期、窗口关联、归档流程、指标与透视计算工具、标准Excel导出器、SQL生成器、合成测试、空模板和路由Skill。
- 不包含：真实接入配置、凭证、业务数据、项目归档、独立历史审批档案及索引。
- `工作约定/数据口径/ph/` 经用户明确确认无需脱敏，全部原样复制，包含完整指标/维度/来源表YAML、默认口径、profile和透视聚合说明。表名、公式、原文中的授权说明和路径引用均不替换；原文引用的外部历史文件不随包提供。
- 数据口径目录以外的代码算法和业务常量保留以避免改变计算，代码来源及共享权限仍需最终发布审核；不对已确认的ph目录重复索取脱敏确认。
- 完整复制口径不代表已经验证实时表结构或取得数据库访问权限；真实接入、任务适用范围和明细公式证据仍按现行规范确认。

## 离线快速开始

在当前目录运行，建议Python 3.11。安装依赖需要使用者自行确认联网行为；构建工具本身不安装依赖或访问远程服务。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\工具\requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s .\工具\tests -v
```

用于日常运行时，复制到另一个使用目录后初始化；不要在受保护的生成目录中运行会写本地配置或索引的命令。

```powershell
Copy-Item -LiteralPath '.\工作约定\config.example.json' -Destination '.\工作约定\config.json'
python -X utf8 .\工具\workbench.py init
python -X utf8 .\工具\workbench.py init-project --project-root '..\合成示例项目'
python -X utf8 .\工具\workbench.py new-analysis --project-root '..\合成示例项目' --spec '.\examples\analysis.json' --thread-id 'demo-thread'
```

`--root`可覆盖工作台位置，必须放在子命令前；默认从CLI文件位置推导。初始化生成的config、索引、项目产物只留本地，不提交。

## 可选能力与限制

- 取数依赖外部 `odps-query` Skill，仓库不捆绑该Skill、任何认证或数据库权限。无取数能力时停止，不能另造认证客户端。
- 复杂工作簿能力依赖外部 `spreadsheets` Skill；它不替代现成计算与标准导出器。
- 最优分箱功能需另装 `optbinning`，基础离线测试不验证该可选功能。
- `ph_data_analysis.py`保留原观察点、成熟度语义及固定换算常量；不表示当前汇率或对任意客群都适用。真实分析先确认数据口径。
- `pivot_base_profile.json`与源文件逐字节一致，包含原表名、140指标及9个默认维度。SQL生成不等于执行或线上验证。
- Skill在 `skills/risk-analysis-agent/`，从原Skill直接生成，完整保留强制启动、执行路径、透视聚合底表和文档治理要求；只替换机器固定路径的定位方式。单独安装时必须明确工作台根路径。

## 审核与维护

`release-manifest.json`仅包含候选文件哈希和通用待审核项，不包含原始绝对路径或内部扫描明细。发布控制程序、转换映射、审核日志保留在唯一源工作台，不随此包复制。

ph数据口径目录的原样保留已获用户明确确认。外部发布前仍需确认其余代码来源及依赖许可、指定许可证，并批准最终提交与上传；自动检查和离线测试不能代替最终确认。
