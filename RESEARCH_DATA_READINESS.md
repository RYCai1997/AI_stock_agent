# 真实历史回测输入状态（2026-09-29）

本轮从旧本机研究缓存中读取九个节点的 Baostock 原始数据，并用当前正式入口重新生成 `raw_metrics.csv` 与 `official_run_metadata.json`。九个节点均为 300/300 只构建成功、0 个取数错误、300 个缓存命中。这是节点输入覆盖检查，不是中证指数官方历史成分核验。

节点日期：2020-03-16、2020-07-15、2021-05-17、2021-12-15、2022-05-16、2022-10-17、2023-03-15、2024-05-15、2025-07-15。首尾覆盖 65 个自然月，只有 9 个月有正式信号快照，缺 **56 个月**。因此不能把这些节点连接起来称为完整月度 V1 连续回测。

Baostock 抽查了 2022-06-30、2023-06-30、2024-06-28、2025-06-30 的成分查询，均返回 300 只；短区间未复权行情及单只股票分红接口可查询。这些检查不能证明全部日期的成分、公司行动或成交价格完整。随后对单只股票的跨多年日线请求等待超过约两分钟、单年度请求等待超过约一分半钟，均未完成；批量采集未通过可行性验证，已停止请求，没有据此填造价格或收益。

东方财富公开日线接口的单只股票跨多年未复权查询已能快速返回；抽查浦发银行 2025-06-23 的开高低收与 Baostock 一致。这只是公开价格采集的可行性信号，尚未完成逐股覆盖、停牌、复权和公司行动审计。

`stock_selector/collect_public_bars.py` 可从已有正式节点列出的股票并集中采集东方财富未复权日线和沪深300指数日线，逐只保存原始响应与 SHA-256，重启时复用缓存；同时输出价格 CSV 和 `source_audit.json`。只有全部序列成功时才命名为 `bars.csv`、`benchmark_raw.csv`。未返回的交易日不会被补造，原始响应也不等于已核实停牌、ST、涨跌停和公司行动。采集结束须检查 `complete_tickers`、各只首尾日期及跨源抽查，再决定是否用于诊断回放。

首轮实采 472 条（471 只股票加沪深300指数）中只有 358 条成功；后续 114 条被接口断开连接。采集器会把未齐数据写为 `bars.partial.csv`、`benchmark_raw.partial.csv`，并在审计文件中标记 `collection_complete=false`；**不得把部分行情送入正式连续回测**。`--offline` 可在不重新请求接口的情况下从已存响应重新生成覆盖报告。即使全部补齐，前述 56 个缺失信号月份及公司行动问题仍未解决。

加入单 ticker 有界重试、指数退避与请求节流后，续传达到 **465/472**，沪深300指数已取得。仍失败的七只为 `sz.002812`、`sz.002821`、`sz.002841`、`sz.300014`、`sz.300760`、`sz.300832`、`sz.300866`；审计文件逐只保留错误。缩短其中一只的请求区间仍被断开；结果继续保持 `collection_complete=false`，文件仍标为 partial。

```powershell
$env:PYTHONPATH = (Resolve-Path stock_selector).Path
python stock_selector/collect_public_bars.py `
  --snapshots-dir stock_selector/outputs/research_data `
  --start 2020-03-16 --end 2025-08-15 `
  --output stock_selector/outputs/public_bars `
  --max-attempts 3 --request-interval 2
```

当前缺少完整逐月正式快照、逐日未复权 OHLC、已核对公司行动、日期化实际券商费率和中证指数公告的历史成分对照。完整研究的输入预检命令：

```powershell
$env:PYTHONPATH = (Resolve-Path stock_selector).Path
python stock_selector/audit_backtest_readiness.py `
  --snapshots-dir <月度快照目录> --bars <未复权行情.csv> `
  --actions <公司行动.csv> --benchmark-bars <沪深300行情.csv>
```

预检显示 `replay_inputs_present=true` 只说明这些文件和首尾节点之间的逐月快照存在；还须核查数据日期、停牌与涨跌停、分红送转和配股、退市、官方历史成分、费率来源及缺失行情。完成这些核对后，按 README 中的命令运行 A–H 连续回放；报告仍应把历史结果标为 retrospective validation，不能当作严格前瞻样本外证据。
