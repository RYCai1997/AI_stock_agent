# 跨市场择时选股器 V1

这是新项目主线。V1接收某一历史时点已经准备好的股票池与指标CSV，输出完整候选表；它不会连接券商、不会下单，也不会让LLM改变评分或仓位。

## 使用方式

```powershell
python run_selector.py --market US --as-of 2025-07-15 --input examples\input_template.csv --output outputs\us_2025-07-15
```

支持市场代码：`A`、`HK`、`US`。`--market-trend` 可取 `up`、`down` 或 `unknown`，表示该市场宽基指数在筛选日是否位于EMA200上方。

已有SEC／Yahoo历史缓存时，可直接运行真实美股节点：

```powershell
python run_us_cached_snapshot.py --as-of 2025-07-15 `
  --history D:\path\sp500_historical_components.csv `
  --ticker-map D:\path\sec_company_tickers.json `
  --fact-cache D:\path\sec_compact_facts `
  --submission-cache D:\path\sec_submissions `
  --price-cache D:\path\yahoo_prices `
  --output outputs\us_2025-07-15
```

## 输入

输入字段模板见 `examples/input_template.csv`。每一行必须是筛选日当时股票池中的一只股票，并明确记录：

- 股票池、财务和价格数据的截止日期；
- 公司与行业；
- Quality、Value、Momentum原始指标；
- EMA200、波动率、回撤和流动性指标。

任何数据日期晚于 `--as-of`，程序都会直接终止，防止无意间使用未来数据。

## 输出

- `candidates.csv`：所有股票及其指标、分数、排名、择时状态和排除原因；
- `selected.csv`：通过Q/V/M两阶段筛选的候选池，不受市场趋势参数影响；
- `actionable.csv`：在Q/V/M入选基础上，市场与个股趋势均确认的候选；
- `metadata.json`：参数、数据日期范围、数量漏斗及模型边界。

## 当前边界

- Quality为全市场横截面排名；Value为行业内排名；Momentum为Q+V合格池内排名。
- 默认沿用已回测的6-1M与12-1M各50%；行业相对强弱会输出，但在完成独立对照前不加入正式分数。
- 金融公司默认保留在输出中但不参加Q/V/M筛选，因为普通公司的ROIC与FCF口径不适合银行、保险和券商。
- 技术价格信息只负责趋势与风险提示，不进入Q/V/M总分。
- 尚未接入A股、港股、美股的自动数据提供器；V1先固定评分语义和审计边界。
- 美股本地历史缓存提供器已经接入；自动补齐缺失文件仍未实现。
