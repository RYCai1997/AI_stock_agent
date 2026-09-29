# AI_stock_agent 重构任务执行报告

**报告日期：**2026-09-29  
**仓库：**https://github.com/RYCai1997/AI_stock_agent  
**正式策略：**`A_CSI300_QVM_TIMING_V1`，策略版本 `1.0.0`  
**报告依据：**截至提交 `f98eea5` 的代码、测试、公开数据采集和本地输入预检。本文将已实现的能力与已完成的真实历史验证分开说明。

## 1. 是否满足最初需求

**尚未完全满足。**最初要求的仓库整理、模块化连续账户回测框架、A–H 消融与研究诊断、自动测试、CI、正式文档分级和分阶段提交已完成；正式 V1 参数没有为提高历史收益而修改。**最初明确要求“运行完整 V1 连续组合回测”及基于真实历史数据回答的绩效和策略贡献问题尚未完成。**因此本项目目前是具备回测能力的研究系统，不是一份已经验证真实历史收益的研究结论。

原因是所需输入还不完整：2020-03 至 2025-07 共 65 个自然月只有 9 个完整正式信号节点，缺 56 个月；471 只已知股票加沪深300指数的公开未复权日线，首轮只取得 358/472 条序列；公司行动、官方历史成分和实际历史券商费率也未完成核验。不能用九个稀疏节点冒充逐月 V1，不能将部分行情或空公司行动表当成完整历史数据，也不能据此报告真实累计收益、最大回撤或 alpha。

## 2. 做了什么

| 工作 | 已完成的实现 | 当前证据边界 |
| --- | --- | --- |
| 仓库整理 | 审计正式运行引用，将历史实验、旧实现和规划材料迁入 `.archive/`，修复正式路径与文档引用。 | `.archive/` 用于追溯，不参与正式运行。 |
| 研究可复现性 | 每次研究输出包含策略、程序、Git、数据与执行假设的 manifest。 | manifest 能记录来源，不能自动证明来源数据正确。 |
| 连续账户引擎 | 分离信号、目标、订单、实际成交、账户台账和每日估值；维护现金、持仓、盈亏、费用、分红和净值。 | 已通过确定性合成数据测试，尚无完整真实历史输入。 |
| A 股成交与成本 | 模拟次日可交易开盘、停牌、开盘涨跌停、跳空止损、待成交订单、日期化费率和滑点。 | 无盘口排队数据，成交约束是保守代理；券商实际费率待核。 |
| 公司行动 | 处理现金分红、送转、拆并及显式配股；无法可靠处理的情形标为需人工审计。 | 历史事件流尚未完整取得与核对。 |
| 正式策略接入 | 连续回放复用正式筛选、组合计划和持仓复核逻辑，冻结 V1 参数。 | 9 个现有节点不足以回放完整月度 V1。 |
| 研究比较 | 实现 A–H 固定消融、满仓及动态敞口匹配沪深300、逐窗口移除、赢家集中度、MAE/MFE、参数邻域和固定随机种子的 Monte Carlo。 | 工具已就绪；真实历史比较结果均未成立。 |
| 数据与前瞻审计 | 增加历史成分审计流程、输入预检、缺月标记及 shadow/paper-trading 记录和事后核对。 | 未取得逐期官方完整成分对照；短期影子运行不能证明 alpha。 |
| CI 与文档 | 保持 `unittest`，建立 Windows GitHub Actions；将离散历史节点结果与连续账户证据分级。 | 测试通过证明代码契约，不证明策略有效。 |

修复 bug 时先做最小失败复现，再修复和回归测试。其中涉及已平仓盈亏口径、未来数据阻断、止损参考价、订单受阻历史、缺月报告及部分行情防误用。历史研究结果没有被改写为“表现改善”。

## 3. 产生了什么输出

### 已写入并推送仓库

- `stock_selector/backtest/`：账户、引擎、成交、费用、公司行动、正式 V1 适配、variants、benchmarks、robustness、metrics、report 等模块。
- `stock_selector/run_backtest.py`：离线真实输入回放 CLI；输入齐备后可运行 A–H。
- `stock_selector/audit_backtest_readiness.py`：只读检查正式节点数量、元数据、缺月和回放文件是否存在。
- `stock_selector/collect_public_bars.py`：逐股采集公开未复权日线，保存原始响应、SHA-256 与覆盖审计；未齐数据只输出 `*.partial.csv`。
- `stock_selector/audit_universe.py`、`stock_selector/reconcile_shadow.py`：历史成分与影子记录核对工具。
- `.github/workflows/tests.yml`、`stock_selector/tests/`：离线确定性测试和 CI。
- `README.md`、`OFFICIAL_STRATEGY.md`、`DATA_UNIVERSE_AUDIT.md`、`RESEARCH_DATA_READINESS.md`、`CHANGELOG.md`：正式使用说明、证据层级及未解决风险。

### 已在本机生成、未纳入 Git

- `stock_selector/outputs/research_data/<日期>/`：9 个节点经当前正式入口重建的 `raw_metrics.csv`、`official_run_metadata.json` 等。每个节点均为 300/300 构建、0 个取数错误；这不等于官方成分已核验。
- `stock_selector/outputs/research_cache/`：上述节点的 provider 缓存；另一次 2025-06-16 缺月试采只缓存了 16/300 只，**没有形成正式完整节点**。
- `stock_selector/outputs/public_bars/responses/`：已取得的公开原始日线响应。
- `stock_selector/outputs/public_bars/source_audit.json`：逐只来源哈希、日期范围和失败状态；`collection_complete=false`，358/472 条成功。
- `stock_selector/outputs/public_bars/bars.partial.csv` 与 `benchmark_raw.partial.csv`：部分价格数据。沪深300指数序列尚未取得；这些文件不能用于完整 V1 绩效报告。

**尚未生成：**基于完整、核验过的真实月度信号与逐日账户路径的 `reports/continuous_backtest/` 绩效和 A–H 研究结论。引擎在输入齐备时设计为输出 `daily_nav.csv`、`orders.csv`、`trades.csv`、`positions.csv`、`performance.csv`、比较与诊断 CSV、`summary.md`、`data_quality_report.md` 和 `research_manifest.json`；这里列的是能力，不是声称已有真实历史结果。

## 4. 测试、发布与复核

截至 `f98eea5`，本地执行 `python -m unittest discover -s stock_selector/tests`：**91 项通过、0 失败**。对应 GitHub Actions [第 3 次运行](https://github.com/RYCai1997/AI_stock_agent/actions/runs/36513912477)成功。远端 `main` 与本地 `f98eea57bd7365f07f020166a28383784bc0f3b4` 一致；仓库页面可匿名访问，保持 Public。本地研究数据被 `.gitignore` 排除。

复核方法（在 `D:\CodexWorkSpace\Stock\AI_stock_agent` 执行）：

```powershell
$env:PYTHONPATH = (Resolve-Path stock_selector).Path
python -m unittest discover -s stock_selector/tests
python stock_selector/audit_backtest_readiness.py --snapshots-dir stock_selector/outputs/research_data
git status --short
git rev-parse HEAD
```

预检目前应显示 `complete_snapshots=9`、`expected_signal_months_between_first_and_last=65`、56 个 `missing_signal_months`、`replay_inputs_present=false`。`git status --short` 应为空。完整输入集齐并完成来源核验后，才能按 README 的离线回放命令运行并解释绩效。

## 5. 研究结论

- **SUPPORTED：**代码已建立可审计的连续账户回测流程；正式策略参数仍冻结；确定性测试与最新 CI 通过；已知历史输入存在明确缺口。
- **TENTATIVE：**东方财富公开接口可提供部分跨年未复权日线；单只浦发银行在 2025-06-23 的 OHLC 与 Baostock 抽查一致。这不证明其余证券、日期或公司行动正确。
- **UNRESOLVED：**完整 V1 真实净值、成本后收益、最大回撤、与沪深300比较、EMA/Momentum/Quality/Value/止损/月度退出的真实增量贡献、参数稳定性、selection skill、历史成分完整性和严格前瞻样本外表现。

剩余风险包括：缺失的 56 个月正式信号、114 条未取得的公开价格序列、停牌与 ST/涨跌停标记、现金分红和送转配股/退市事件、官方历史成分变更与幸存者偏差、盘口不可观察造成的成交误差、样本窗口集中，以及未来 prospective 验证不足。2020–2025 的回顾数据即使补齐，也只能称为 **retrospective historical validation**，不能重新标为严格 OOS。

## 6. 本轮提交记录

下表从本轮开始前的 `80a484c` 之后列出截至报告依据提交的全部 29 个提交；每个主要逻辑阶段分别提交。

| 提交 | 内容及目的 |
| --- | --- |
| `5f61d66` | 归档历史研究并清理正式仓库。 |
| `96cfc9c` | 增加可复现研究 manifest。 |
| `02a410b` | 建立连续账户回测引擎。 |
| `ac2f85d` | 加入保守 A 股成交约束。 |
| `5a677c8` | 加入历史费率和成交成本模型。 |
| `206f949` | 加入公司行动账务。 |
| `fb9c489` | 将正式策略接入连续回测。 |
| `74f7327` | 增加 A–H 策略消融。 |
| `3282551` | 增加动态敞口匹配基准。 |
| `f9a087c` | 增加逐窗口影响诊断。 |
| `c652e7e` | 修复已平仓交易盈亏统计口径。 |
| `0523200` | 增加赢家集中度诊断。 |
| `4414cb5` | 增加 MAE/MFE 交易诊断。 |
| `4b19f01` | 增加参数稳定性研究。 |
| `393358f` | 增加固定种子的选股 Monte Carlo。 |
| `b15ddbe` | 增加沪深300历史成分审计工具与元数据。 |
| `a1a4970` | 增加前瞻 shadow 记录。 |
| `f7f48a6` | 增加确定性 `unittest` CI。 |
| `c27e490` | 阻断未来日期基准与成分元数据进入回放。 |
| `0b3dbc3` | 扩充确定性回测测试。 |
| `715f4bf` | 增加连续研究报告汇总。 |
| `535724d` | 修订证据层级与研究文档。 |
| `0873d61` | 修复止损价对成交价与佣金的处理。 |
| `0f6138b` | 保留逐日订单受阻历史。 |
| `58eff3f` | 澄清历史成分数据可得性。 |
| `aa710cd` | 修复 Windows CI 中文输出编码。 |
| `c6f2607` | 报告缺失月度信号，防止稀疏结果被误称完整 V1。 |
| `c98637f` | 增加真实历史输入准备度预检。 |
| `f98eea5` | 增加可审计公开日线采集与部分结果防误用。 |

本报告本身是后续文档提交，不包含在上述 29 个实现与修复提交中。
