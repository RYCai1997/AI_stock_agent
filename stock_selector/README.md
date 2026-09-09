# 跨市场择时选股器 V1

> **正式状态（2026-09-09）**：A股入口已固定为`A_CSI300_QVM_TIMING_V1`，完整规则见根目录`OFFICIAL_STRATEGY.md`。正式运行使用`run_official_strategy.py`；其余市场适配和回测脚本只作为研究工具。

这是新项目主线。V1接收某一历史时点已经准备好的股票池与指标CSV，输出完整候选表；它不会连接券商、不会下单，也不会让LLM改变评分或仓位。

## 使用方式

正式A股运行：

```powershell
python run_official_strategy.py --as-of 2026-09-09
```

可选传入`--holdings examples\holdings_template.csv`，同时生成已有持仓月度复核。

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

使用 Baostock 重建历史沪深300节点：

```powershell
python run_a_snapshot.py --as-of 2025-07-15 `
  --output outputs\a_csi300_2025-07-15
```

A股使用独立的 `a_share_v1` 因子口径：Quality 为 ROE、CFO/营收和 EPS
增长稳定性；Value 为盈利收益率、净现金流收益率和账面市值比。这里的净现金流
收益率来自 Baostock `pcfNcfTTM` 的倒数，不等同于经营现金流收益率或 FCF Yield。
所有财务记录必须满足 `pubDate <= --as-of`，历史沪深300成分、行情与财务日期都会
写入元数据供审计。

多节点回顾测试：

```powershell
python backtest_a_nodes.py `
  --nodes 2022-04-29,2023-02-01,2024-02-05,2024-10-08,2025-07-15 `
  --output outputs\a_multinode_demo `
  --snapshot-cache outputs\a_provider_cache
```

程序以信号后首个交易日开盘价模拟买入，并把未来1、3、6个月收益单独保存为结果
标签。Baostock登录状态不支持本项目并行下载多个节点，应保持串行运行；断点缓存会
避免重复查询成功记录，瞬时网络或登录错误则会自动重试。

在已经固定的Q/V/M候选上测试止损与过热入场规则：

```powershell
python test_a_risk_rules.py `
  --snapshot-root outputs\a_multinode_demo\snapshots `
  --nodes-file outputs\a_multinode_demo\nodes.csv `
  --output outputs\a_risk_rules_demo
```

该脚本不会重新选股。默认过热条件为沪深300截至信号日的20个交易日涨幅超过10%；
测试立即买入、固定-10%盘中止损、过热时延后5个交易日、50/50分批及延后后止损。
跳空跌破止损价按更差的开盘价成交，但尚未模拟跌停无法成交。

测试持仓月度复核与确认式退出：

```powershell
python test_a_requalification_exit.py `
  --snapshot-root outputs\a_multinode_demo_v2\snapshots `
  --nodes-file outputs\a_multinode_demo_v2\nodes.csv `
  --output outputs\a_requalification_exit_demo
```

月度跌出Q/V/M名单只产生预警；只有个股EMA200也确认破坏时才退出。这一确认式退出
已纳入正式V1，但补充节点没有证明它能增加收益，因此应视为风险控制和执行纪律，
而不是收益来源。实验结果和方法边界见`A_REQUALIFICATION_EXIT_DEMO.md`。

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
- `portfolio_plan.csv`：最多3只、每只10%，并明确立即进入人工审批或等待5个交易日；
- `holding_review.csv`：已有持仓的止损线、资格状态、退出动作和连续确认状态；
- `official_run_metadata.json`：正式策略版本、固定参数、市场20日涨幅和过热等待状态。
- 多节点测试另输出 `nodes.csv`、`forward_outcomes.csv` 和 `portfolio_summary.csv`。
- 月度复核实验另输出逐股票退出结果、逐月决策、复核漏斗、汇总和元数据。

## 当前边界

- Quality为全市场横截面排名；Value为行业内排名；Momentum为Q+V合格池内排名。
- 默认沿用已回测的6-1M与12-1M各50%；行业相对强弱会输出，但在完成独立对照前不加入正式分数。
- 金融公司默认保留在输出中但不参加Q/V/M筛选，因为普通公司的ROIC与FCF口径不适合银行、保险和券商。
- 技术价格信息只负责趋势与风险提示，不进入Q/V/M总分。
- 已接入A股 Baostock 历史沪深300提供器和美股本地历史缓存提供器；港股尚未接入。
- 美股本地历史缓存提供器已经接入；自动补齐缺失文件仍未实现。
- `audit_us_coverage.py`可将每个历史成分代码归类并核对总数；`update_us_cache.py`默认只做dry-run，只有显式添加 `--execute`才下载缺失文件。
