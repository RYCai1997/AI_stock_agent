# A股现货择时选股系统

本仓库当前只有一个正式策略：`A_CSI300_QVM_TIMING_V1`。它以沪深300历史成分为股票池，使用Quality、行业内Value和中期Momentum筛选标的，再以沪深300及个股EMA200确认入场时机。

完整规则见 [OFFICIAL_STRATEGY.md](OFFICIAL_STRATEGY.md)。

## 运行

Windows可双击：

```text
Install_Stock_System.bat   （首次安装）
Launch_Stock_System.bat
```

或运行：

```powershell
python stock_selector\run_official_strategy.py --as-of 2026-09-09
```

复核已有持仓时增加：

```powershell
python stock_selector\run_official_strategy.py --as-of 2026-09-09 `
  --holdings stock_selector\examples\holdings_template.csv
```

默认结果保存至`stock_selector/outputs/official/<日期>/`，包括：

- `candidates.csv`：全部股票、指标、排名和排除原因；
- `selected.csv`：Q/V/M合格池；
- `actionable.csv`：同时通过市场和个股趋势确认的候选；
- `portfolio_plan.csv`：最多3只、每只10%的人工审批计划；
- `holding_review.csv`：已有持仓的止损线、资格和月度退出复核；
- `official_run_metadata.json`：固定策略版本、参数和数据日期。

## 安全边界

- A股现货long/flat，不做杠杆、合约或卖空；
- 不连接券商，不自动下单；
- LLM不参与评分、排名、仓位或订单；
- 所有建议都要求人工确认；
- 任何晚于筛选日期的数据都会被拒绝。

## 目录

- `stock_selector/`：唯一正式运行系统及其回顾测试；
- `llm_trading_agent/`：已停用的旧LLM/币圈实验，仅保留历史研究证据；旧启动入口会转向或提示正式系统；
- `baseline_trend/`：已停用的价格策略研究证据，不属于当前运行路径；
- `_ref_daily_stock_analysis/`：本地外部参考，不纳入本仓库追踪。

旧策略不会被正式入口导入或调用。Git历史保留完整回退能力。
