# 冻结的 V1 历史研究协议

**冻结日期：**2026-09-29。本协议在新月份 PIT 信号生成及其回测结果出现之前确定。正式策略 `A_CSI300_QVM_TIMING_V1` 版本 `1.0.0` 的因子和参数不因本协议改变。

## 历史信号运行日历

研究区间预先固定为 2020-03 至 2025-07，每个自然月恰好一次完整筛选。运行日为**当月第一个日期大于或等于 15 日的 Baostock A 股市场交易日**，于该日收盘后计算信号；成交只能从后续可交易时点模拟。选择规则只读市场开市日，不读取收益、因子或候选股票。不得逐月人工改变日期，也不得根据回测结果重新选择起止月份。

完整 65 个月的预定日期保存在 [`stock_selector/data_history/schema/signal_calendar_2020-03_2025-07.csv`](stock_selector/data_history/schema/signal_calendar_2020-03_2025-07.csv)。账户回放的预定终点为 **2025-07-31 收盘**；末期未平仓头寸继续按当日市值计入账户，后续持有路径不在本次固定样本区间内。[`market_sessions_2020-03_2025-07.csv`](stock_selector/data_history/schema/market_sessions_2020-03_2025-07.csv) 保存 1316 个市场交易日，包括个别股票可能无行情的日子。[`signal_calendar_manifest.json`](stock_selector/data_history/schema/signal_calendar_manifest.json) 记录原始交易日响应和两个冻结 CSV 的 SHA-256。原始 Baostock `query_trade_dates` 响应保存在忽略 Git 的 `stock_selector/outputs/history/baostock_trade_dates_2020-03_2025-07.csv`，可用以下命令重新取得并离线生成：

```powershell
$env:PYTHONPATH = (Resolve-Path stock_selector).Path
python stock_selector/data_history/builders/fetch_market_calendar.py `
  --start 2020-03-01 --end 2025-07-31 `
  --output stock_selector/outputs/history/baostock_trade_dates_2020-03_2025-07.csv
python stock_selector/data_history/calendar.py `
  --source-csv stock_selector/outputs/history/baostock_trade_dates_2020-03_2025-07.csv `
  --output-csv stock_selector/data_history/schema/signal_calendar_2020-03_2025-07.csv `
  --market-sessions-csv stock_selector/data_history/schema/market_sessions_2020-03_2025-07.csv `
  --manifest stock_selector/data_history/schema/signal_calendar_manifest.json
```

Baostock 市场日历被用于本轮机械运行日。它尚未逐日与沪、深交易所官方日历核对，因此“共同交易日”口径的独立核验仍在 data quality audit 中列为待完成；若发现来源错误，应作为有证据的资料修订单独记录，不得因收益结果而改日。已有九个历史节点恰好落在此规则选定的日期，但仍需用本轮离线 PIT Store 和同一日历重新审计。

## 输入与结论边界

每个信号只能读取当日或更早的市场价格、当时有效的指数成分与行业信息，以及 `pubDate <= signal_date` 的财务披露。执行价格使用未复权日线；技术指标使用与正式 V1 一致的复权口径。缺失行情、停牌、公司行动和数据来源冲突必须显式披露。所有 2020–2025 历史研究只称为 **retrospective historical validation**，不称为严格样本外。
