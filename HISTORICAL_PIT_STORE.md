# Historical PIT Store 状态

本地历史资料位于忽略 Git 的 `stock_selector/outputs/history/pit_store/`。代码入口在 `stock_selector/data_history/`；正式 V1 参数不受影响。该目录目前是**未完成的数据集**，不能驱动正式完整回测。

`builders/ingest_public_prices.py` 从东方财富逐股原始响应及其 SHA-256 审计文件，离线生成 `prices/unadjusted/<ticker>.csv`。每行保留日期、未复权 OHLC、原始成交量、成交额、来源口径与仅“接口返回了行情行”的状态。成交量按来源的“手”保存，不擅自换算为股。没有行情行的市场交易日不在此阶段补造。`pit_store_manifest.json` 明确列出成功覆盖与缺口，并将 `usable_for_full_v1` 设为 `false`。

当前实际产物是 358/472 条未复权序列；调整后指标价、完整财务披露、历史成分、行业时点和证券状态尚无完整资料。2020–2025 的财务记录必须保存 `statDate` 与 `pubDate`，离线建节点时只能使用 `pubDate <= signal_date`；`data_history.store.disclosures_available` 已用回归测试锁定这一限制。没有披露日期的记录会被拒绝。

本轮测得 Baostock 2025 年第一季度单股行情可返回 57 行，但对 2024 年的年度、季度、短日期请求均等待超过 30 秒未返回，已中止；东方财富剩余 114 条（含沪深300指数）返回连接断开。不能假定这些失败等于股票退市或停牌，也不能删去股票凑齐覆盖。因此尚不能从当前公开来源稳定构造全部 V1 所需的历史 PIT 数据。后续应在不改变日历和参数的条件下恢复或更换公开来源，逐字段保留原始响应与哈希，并与官方公告抽查。

离线复建已有部分未复权资料：

```powershell
$env:PYTHONPATH = (Resolve-Path stock_selector).Path
python stock_selector/data_history/builders/ingest_public_prices.py `
  --public-bars-dir stock_selector/outputs/public_bars `
  --output stock_selector/outputs/history/pit_store
```
