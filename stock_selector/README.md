# 正式A股选股器

这里是`A_CSI300_QVM_TIMING_V1`的唯一运行代码。正式版只支持A股沪深300现货选股，不包含港股、美股、币圈、合约、杠杆、LLM或券商下单功能。

## 运行

普通使用可从项目根目录双击`Launch_Stock_System.bat`进入中文GUI。点击“当前持仓…”可以手工新增持仓，或导入最近一次Top 5计划后填写实际成交均价和股数。每次保存都会在`user_data/holdings/`生成带时间戳的本地快照；GUI以后自动使用最新快照进行复核。页面只生成研究计划，不会连接券商或下单。

命令行审计入口继续保留：

从项目根目录运行：

```powershell
python stock_selector\run_official_strategy.py --as-of YYYY-MM-DD
```

复核已有持仓时增加：

```powershell
--holdings stock_selector\examples\holdings_template.csv
```

程序输出完整候选池、Q/V/M合格池、趋势确认池、Top 5人工审批计划、持仓复核和审计元数据。每只计划仓位6%，单窗口最多30%。

完整策略见根目录`OFFICIAL_STRATEGY.md`，第一版说明见`RELEASE_NOTES_v1.0.0.md`，组合宽度证据见`PORTFOLIO_BREADTH_VALIDATION.md`。

## 正式代码

- `run_official_strategy.py`：唯一入口；
- `gui.pyw`：中文桌面操作与结果展示层；
- `selector/strategy.py`：冻结参数；
- `selector/providers/a_baostock.py`：历史沪深300、行情和财务数据；
- `selector/scoring.py`：Q/V/M评分；
- `selector/pipeline.py`：日期验证、趋势确认和候选输出；
- `selector/portfolio_plan.py`：Top 5、固定6%计划；
- `selector/holding_review.py`：已有持仓月度复核；
- `selector/holdings_store.py`：本地持仓快照、校验和复核状态延续；
- `tests/test_selector.py`：正式版测试。

旧系统和研究工具已经移动到根目录`archive/`，不会被正式入口导入。
