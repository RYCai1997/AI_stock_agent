# A股现货择时选股系统

一个供人工决策使用的A股现货选股与持仓复核工具。当前应用版本为 **1.2.1**；正式策略 `A_CSI300_QVM_TIMING_V1` 的参数版本仍是 **1.0.0**。

输入筛选日期后，程序从该日期的沪深300成分股出发，下载截至该节点可获得的财务与行情，输出完整指标表、Q/V/M观察名单、趋势确认名单、最多5只的新建仓计划，以及已有持仓的复核建议。工具不会连接券商或自动下单。

本仓库当前只有一个正式策略：`A_CSI300_QVM_TIMING_V1`。它以沪深300历史成分为股票池，使用Quality、行业内Value和中期Momentum筛选标的，再以沪深300及个股EMA200确认入场时机。

详细的因子、仓位和退出规则见[策略说明](OFFICIAL_STRATEGY.md)。历史节点研究不能替代连续账户回测，也不构成收益保证。

## 安装与启动（Windows）

需要可用的 Python、网络连接，以及 `pandas`、`numpy`、`baostock`。首次使用在项目根目录双击：

```text
Install_Stock_System.bat
Launch_Stock_System.bat
```

第一步建立本地虚拟环境并安装[依赖](stock_selector/requirements.txt)；以后只需运行启动脚本。若提示依赖缺失，重新运行安装脚本。中文桌面页面可以选择“当前账户”或“历史研究”、日期和持仓CSV；历史研究不会更新当前持仓。

## 一次筛选怎么读

1. 选择日期并开始筛选。“当前账户”使用今天的日期；过去的节点选择“历史研究”。
2. 查看“筛选汇报”。它分别列出取数成功与错误、Q/V/M观察名单、趋势过滤后的可执行候选、建仓计划和实际行情日期。
3. 已有仓位先查看“持仓复核”，再查看“今日交易指导”及新建仓计划；执行前人工核对数据节点、公司事件、账户余额和实际成交价格。

点击“当前持仓…”可手工记录股票代码、实际买入日期、均价和股数，也可从最近一次Top 5计划导入后补充实际成交信息。记录按时间保存于本地且不纳入Git；以后打开GUI会自动选择最新记录，每次筛选后更新月度确认状态并给出退出复核节点。

“账户设置”可填总资产、现金和仓位上限；“登记实际成交”用于记录交易及费用。完整操作和数据口径见[交易指导工具使用说明](TRADING_GUIDANCE.md)。

筛选时显示5个步骤的真实进度，例如“2/5 获取行情与财务 · 当前步骤64/300，21.3%”。进度条按当前步骤完成量推进，切换步骤时重新计数，并非耗时估计。缓存和失败项也计入已处理数量，成功/错误数量另列；30秒无新进度会提示等待，不会伪造进度。首次完整取数仍可能需要较长时间。

完成后自动打开“筛选汇报”：说明取数成功与错误、Q/V/M观察名单、市场过滤、可执行候选、建仓计划、具体建议股数与实际行情日期。零计划可能是市场过滤或个股趋势不满足，不能简单理解为没有值得观察的公司。停止或失败会明确提示结果不完整。

GUI与命令行调用同一个正式策略入口；GUI只是展示与操作层，不改变任何选股、仓位或退出参数。

正式运行链：

```text
GUI / CLI → Official strategy → Point-in-time provider → Scoring
          → Timing → Portfolio plan → Holding review / account guidance
```

离线历史研究可把正式策略生成的月度节点快照交给 `stock_selector/run_backtest.py`，经同一筛选和持仓复核代码形成信号，再由连续账户引擎模拟次日开盘成交、停牌、涨跌停、止损、费用和每日净值。该研究入口不修改正式筛选器，也不连接券商。

运行前需备齐逐日**未复权** OHLC CSV、每月正式运行的 `raw_metrics.csv` 与 `official_run_metadata.json`、公司行动 CSV、日期化费率 JSON，以及说明价格口径和公司行动覆盖状态的输入 manifest。缺少这些历史数据时不能得出连续回测收益。命令格式：

```powershell
python stock_selector\run_backtest.py --bars bars.csv --actions actions.csv `
  --snapshots-dir monthly_snapshots --fee-config fees.json `
  --input-manifest input_manifest.json --output reports\continuous_backtest
```

加 `--all-variants --benchmark-bars csi300.csv` 可固定运行 A–H 并输出 `variant_comparison.csv`。A/B 使用指数开收盘和前一日 EMA200 构造理论敞口，不是可直接交易的沪深300订单，也不含券商费用；C–H 使用相同股票行情、账户、费率和成交引擎。各版本都是拆解研究，不能据此自动修改冻结的 H/V1 参数。

提供同日期的基准 CSV 后，报告还会输出满仓沪深300与“前一日策略仓位 × 当日沪深300收益”的动态敞口匹配基准、超额收益、跟踪误差、信息比率和 Beta。现金收益按0，指数价格是否含分红取决于输入来源。

公司行动 CSV 至少有 `date,ticker,kind` 表头；空表仅表示未提供事件，不证明历史期间没有事件。输入 manifest 必须写明 `bars_price_basis: "unadjusted"`，以及 `corporate_actions_status: "audited"` 或 `"unverified"`。输出的 `data_quality_report.md` 会披露未核对的事件和覆盖状态。

## 命令行与结果

从项目根目录也可以运行：

```powershell
python stock_selector\run_official_strategy.py --as-of 2026-09-09
```

复核已有持仓时增加：

```powershell
python stock_selector\run_official_strategy.py --as-of 2026-09-09 `
  --holdings stock_selector\examples\holdings_template.csv
```

命令行默认结果保存至`stock_selector/outputs/official/<日期>/`；GUI会按模式、日期和运行时间建立独立目录。主要文件包括：

- `candidates.csv`：全部股票、指标、排名和排除原因；
- `selected.csv`：Q/V/M合格池；
- `actionable.csv`：同时通过市场和个股趋势确认的候选；
- `portfolio_plan.csv`：最多5只、每只6%的人工审批计划；
- `holding_review.csv`：已有持仓的止损线、资格和月度退出复核；
- `account_guidance.csv`：结合账户限制得出的建议股数及原因；
- `official_run_metadata.json`：固定策略版本、参数和数据日期。
- `research_manifest.json`：策略、应用、Git版本、数据源、因子和执行假设；若存在未提交代码，明确标记工作树状态。
- `run_summary.txt`：与页面一致的中文筛选汇报。

## 使用边界与本地数据

- A股现货long/flat，不做杠杆、合约或卖空；
- 不连接券商，不自动下单；
- 系统为确定性规则程序，不依赖LLM；
- 所有建议都要求人工确认；
- 任何晚于筛选日期的数据都会被拒绝。
- 报告中的实际行情日期可能早于所选日期；取数完成不表示财务因子齐全或行情实时。
- 本地账户设置、持仓快照、成交记录、数据缓存和运行结果在Git忽略目录内，不纳入代码提交。

运行测试：

```powershell
$env:PYTHONPATH = (Resolve-Path stock_selector)
python -m unittest discover -s stock_selector/tests
```

## 目录

- `stock_selector/`：唯一正式运行系统和测试；
- `.archive/legacy/`：旧LLM、币圈和纯价格策略，只用于历史追溯；
- `.archive/research/`：A股实验脚本、组合宽度比较、美股适配和旧跨市场工具；
- `.archive/planning/`：已经完成的历史重构计划；
- `_ref_daily_stock_analysis/`：本地外部参考，不纳入本仓库追踪。

旧策略不会被正式入口导入或调用。Git历史保留完整回退能力。
