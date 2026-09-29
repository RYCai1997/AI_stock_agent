# V1 历史 PIT 回测任务执行报告

**核对日期：**2026-09-29  
**项目：**`RYCai1997/AI_stock_agent`  
**正式策略：**`A_CSI300_QVM_TIMING_V1`，版本 `1.0.0`  
**研究区间：**2020-03 至 2025-07，预先冻结的 65 个逐月信号日，2025-07-31 收盘终止估值。

## 结论

**最初需求尚未满足。**已建立固定日历、离线历史数据结构、停牌和价格采集防护、输入预检、费用假设、严格嵌套归因阶梯及多期限随机选股诊断。但公开数据尚不足以生成 65 个可审计 PIT 信号，也不足以核对完整交易和公司行动。此次**没有运行第一次完整真实 V1 连续账户回测**，因此没有真实累计收益、CAGR、回撤、基准差值或归因收益可报告。任何九节点连线或部分股票回放都不能代替它。

## SUPPORTED：已核实的工作和输出

| 阶段 | 已完成事项 | 可复核输出 |
| --- | --- | --- |
| 1 | 在新增月份结果出现前冻结机械月度信号规则：每月首个不早于 15 日的市场交易日收盘后计算信号。 | `RESEARCH_PROTOCOL.md`；`stock_selector/data_history/schema/signal_calendar_2020-03_2025-07.csv`；日历哈希清单。 |
| 2 | 建立本地 PIT Store 的价格规范化及来源记录；缺少正式因子所需字段时明确拒绝完整 V1 输入。 | `stock_selector/data_history/`；本地忽略 Git 的 `stock_selector/outputs/history/pit_store/`。 |
| 3 | 固定 1316 个市场交易日；持仓停牌期间仅结转最近有效收盘用于估值，禁止结转价成交；复牌涨跌停参考最近真实交易收盘。 | `stock_selector/data_history/schema/market_sessions_2020-03_2025-07.csv` 和回归测试。 |
| 4 | 公开未复权价格采集支持独立股票缓存、SHA-256、有界重试、退避和节流。部分结果只写 `*.partial.csv`，正式回放入口拒绝它。 | `stock_selector/outputs/public_bars/source_audit.json`（本地，忽略 Git）。 |
| 5 | 预检逐月对照冻结的精确日期，检查逐行财务、价格和成分更新时间是否越过信号日。 | `stock_selector/audit_backtest_readiness.py`；现有 9 个节点无预检错误。 |
| 6 | 冻结日期化费用假设；分红按税前入账，历史股息税未模拟且会降低结论置信度。 | `research_fee_assumptions.json`；`RESEARCH_DATA_READINESS.md`。 |
| 7 | 保留原 A–H，另加 R0–R7 单机制嵌套阶梯和相邻差值表。完整 Q/V/M 阶段复用正式筛选及组合计划，R7 委托正式 H 信号适配器；测试验证 R7 与 H 的账户路径一致。 | `stock_selector/backtest/nested_attribution.py`；`--nested-attribution` 输出 `nested_variant_comparison.csv` 和 `nested_incremental_attribution.csv`。 |
| 8 | 固定随机种子，保留 20 交易日诊断并增加 63、126 交易日；各期限分别记录收益、随机均值/中位数、百分位和样本数。样本不足明确标记未运行。 | `stock_selector/backtest/monte_carlo.py`；未来完整回放输出 `monte_carlo_selection.csv`。 |

本次实际运行 `audit_backtest_readiness`：**9/65** 个冻结信号日存在快照，**56** 个缺失；九个现有节点逐只 300/300 构建成功且未发现快照日期错误。价格审计显示 **465/472** 条请求成功，包括沪深300指数；`collection_complete=false`，仍为部分数据。七只失败股票为 `sz.002812`、`sz.002821`、`sz.002841`、`sz.300014`、`sz.300760`、`sz.300832`、`sz.300866`。

## TENTATIVE：已取得但尚不能用于完整结论的证据

- 九个历史节点的原始输入来自旧 Baostock 研究缓存，并已用当前入口重算。这证明节点流程可以复现，不证明其间 56 个月的数据完整，也不证明全部历史 CSI300 成分正确。
- 东方财富公开日线覆盖大部分请求；单日跨源抽查一致。这不足以证明所有股票、停牌、复权口径及公司行动正确。
- R0–R7 的结构和测试数据上的运行路径已验证。由于真实全量输入缺失，**没有真实的逐层收益归因**；相邻收益差也只能解释为按固定阶梯顺序加入该机制后的增量，不能自动解释为统计因果效应。
- Monte Carlo 的 20/63/126 日算法已经接入；没有完整历史价格与真实 H 成交样本时，其百分位不是当前策略的实证结论，也不能称为显著性。

## UNRESOLVED：阻塞完整回测的具体输入

| 门槛 | 当前状态 | 所需最小解决方案 |
| --- | --- | --- |
| 65 个月 PIT 信号 | 9/65；缺 56 个精确日期 | 批量取得每只历史成分股带 `statDate`、`pubDate` 的正式 Q/V/M 全字段，以及对应复权技术序列，在本地一次性建库后离线生成 56 个快照。不得以报告期代替披露日。 |
| 未复权执行价格 | 465/472 条请求成功；七只失败；数据仍为 partial | 对七只股票换用可追溯的第二公开源补齐、逐日交叉核验并重建 SHA-256 覆盖审计；保留失败记录。 |
| CSI300 历史成分与行业 | 少数抽查日期获得 300 只，尚无逐次官方公告及历史行业口径核对 | 收集中证指数历史调整公告及生效日期，与按日成分记录逐次比对；获取日期化行业归属。 |
| 公司行动与证券状态 | 分红、送转、配股、退市、ST/停牌尚未全量审计 | 从交易所/公司公告或可追溯公开记录生成逐证券日期化事件表，验证数量、现金和除权附近价格；无法可靠处理的事件必须标为置信度下降。 |
| 基准及账户结果 | 指数原始日线已采集，但完整输入门槛失败 | 前述门槛通过后，才运行 H、A–H、R0–R7、基准和稳健性并生成 daily NAV、订单、成交、现金、持仓、费用、股息、暴露及指标。 |

**Phase 9–11 状态：**未达到真实回测前置条件，因此 Full V1 的累计收益、CAGR、波动率、最大回撤、Sharpe、Sortino、Calmar、换手、胜率、平均持有期、交易成本、平均/最大暴露、100% 与暴露匹配 CSI300 对照，以及真实归因和稳健性结果均为 **UNRESOLVED**。研究区间、信号日历、V1 参数和费用假设没有因结果而调整。未来即使补齐数据，这段历史也只能称为 `retrospective historical validation`，不能称严格样本外。

## 验证与提交

本轮相关提交按阶段分别为：

1. `25ceef5` — 冻结机械月度信号日历。
2. `0794158` — 建立可拒绝不完整输入的本地 PIT 价格层。
3. `ba5d38c` — 修复停牌估值及复牌涨跌停参考。
4. `7562869` — 公开价格重试、缓存与部分数据阻断。
5. `bb90cc1` — 冻结日期及未来数据预检。
6. `cad5fd2` — 日期化费用与税前分红披露。
7. `f9ba593` — 嵌套单机制归因阶梯。
8. `0ada6af` — 20/63/126 日 Monte Carlo 诊断。
9. `388668c` — 首版完整执行报告。

每个逻辑阶段在提交前运行完整 `python -m unittest discover -s stock_selector/tests`；最近一次 **113 项通过**。单测验证的是实现与测试输入，不构成真实历史收益证据。`origin/main` 已核实指向 `388668c54bac64fc98901db720d08925ac591b60`；公开 GitHub 页面可匿名读取。[Deterministic unittest suite #5](https://github.com/RYCai1997/AI_stock_agent/actions/runs/36532562327) 对 `388668c` 显示 **completed success**。

本地复核：

```powershell
cd D:\CodexWorkSpace\Stock\AI_stock_agent
$env:PYTHONPATH = (Resolve-Path stock_selector).Path
python -m unittest discover -s stock_selector/tests
python stock_selector/audit_backtest_readiness.py --snapshots-dir stock_selector/outputs/research_data
Get-Content stock_selector/outputs/public_bars/source_audit.json -Raw | ConvertFrom-Json | Select-Object requested_tickers,complete_tickers,collection_complete
```

预期预检显示 `monthly_signal_coverage_complete=false`、`replay_inputs_present=false`、价格 `collection_complete=false`。这些状态未转为通过前，不能出具“第一次完整真实 V1 历史账户路径”。
