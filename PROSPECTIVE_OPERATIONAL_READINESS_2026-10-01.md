# PUBLIC_V1 首次前瞻信号运行前实测

**结论：`NOT_READY`。** 2026-09-30 的两次真实公开数据 research dry run 都被核心数据门禁拒绝，没有生成 retrospective prediction package，也没有生成或登记 prospective primary。V1 策略参数未改。下一个正式日期由 Baostock 公开交易日历计算为 **2026-10-15**，上海时间收盘后。

## 真实数据与覆盖

使用 `python stock_selector/run_prospective.py --research-date 2026-09-30`，真实调用 Baostock 交易日历、沪深300成分、财务、行业和复权行情。首次返回 300 只成分、31 行建成、269 只错误；第二次返回 300 只成分、214 行建成、86 只错误。第二次逐股错误从 `sz.000858` 等开始，内容为 `用户未登录`，说明长批量取数时 SDK 会话中断。忽略追踪的逐股审计留在 `stock_selector/outputs/retrospective/dry_run_diagnostics/2026-09-30_83212ff292fa48bebae84b755545a526/`。

在第二次建成的 214 行中，财报披露日、成分更新日、最新价格、Momentum、EMA200、行业、ST/交易状态分别为 **214/214**；核心 Q/V/M 因子为 **209/214**。缺失字段均为 `eps_growth_std`，涉及 `sh.600930`、`sh.601059`、`sh.603296`、`sh.688472`、`sh.688506`。这些缺口不能通过删除股票或降低策略门槛来绕过。300 只成分更新日、214 行披露日和价格日期没有晚于研究日期；行业字符串未见 U+FFFD 替换字符。

这次归档的审计 JSON 把“官方 parser 未配置”误计为 2999 次 fallback；这不是 2999 次真实官网失败。随后代码改为单列 `source_selection_reason=official_parser_not_configured`，真实失败后回退数为 **0**。修正后又用 1 只股票的真实 Baostock 请求验证了该字段和计数；没有重写前两次原始审计。

## 原始链条抽查

固定随机种子 `20261001` 抽取普通股票 `sh.601898`、`sh.600050`、`sh.601633`、`sh.600010`、`sh.601816`；另取金融股 `sh.600000`、近期披露股 `sh.601868`，以及沪深300 `sh.000300`。共 **106 个**相关 SDK 表的字段/行到规范化记录逐项相等；七只股票的归档原始/规范化 SHA-256、成分更新日、披露日、行情日期与最终指标匹配。沪深300基准归档哈希通过，2026-09-30 收盘价为 `4357.6155`，EMA200 可计算。这里的“原始”指 Baostock SDK 返回的表，并非底层网络报文字节。

## 门禁、封存与账户演练

- 以实际公开日历计算的接下来三个信号日为 **2026-10-15、2026-11-16、2026-12-15**。在该日 14:59、15:01、次日收盘后、历史日期重跑四种情况中，仅 15:01 当日运行判为 prospective primary。
- 当前缺数据，**未能以真实数据验证 retrospective package 的 `SEALED` 和重跑纪律**。独立合成输入的 CLI 检查为原包 `SEALED`、篡改测试副本 `INVALID`；同一研究日期产生 `rerun_001`、`rerun_002`，首份仍 `SEALED`，prospective registry 不存在。这仅验证封存机制，不能替代真实数据验收。
- 干净 Git 树允许合成 primary；脏树默认拒绝；`--allow-dirty` 只产生降级 `research_only`，不进入 prospective registry。
- 合成行情的首月影子账户从 100 万现金产生挂单，次日开盘按费用和滑点成交，形成持仓、费用、NAV；第二个月继续同一账户。独立开盘核对覆盖正常、停牌、买入涨停、买入跌停、止损跳空及卖出跌停；原预测保持封存。这些是执行逻辑验证，不是实际未来账户收益。

## 本机公开源健康

2026-10-01 小规模探测：中证指数、上交所、深交所、巨潮和 Baostock 首页为 `degraded`（首页可访问，正式 dated parser 未验证）；东方财富行情 API 为 `unavailable`（`RemoteDisconnected`）。Baostock 的实际 dated SDK 日历、成分、财务和行情接口已调用，但 300 只批量读取不稳定。中证指数与巨潮的正式解析器尚未配置，manifest 不应写成“官方端点失败后回退”。

## 最小阻塞与复核

最小阻塞是 **Baostock 长批量会话中断造成 86 只缺行，以及 5 只股票缺少正式 EPS 稳定性输入**。二者任一存在都阻止 300 只完整正式快照。应先证明稳定取到 300 行，再为确实缺少年度 EPS 的个股取得有披露日的公开数据，或依据预先审定的数据协议说明处理方式；不能临时修改 V1 参数或删除这些股票。只有重新完成真实 dry run、得到合格的非 primary package 且通过 seal / tamper / rerun 检查，才能改为 `READY_FOR_FIRST_PROSPECTIVE_PRIMARY`。

运行清单见 `PROSPECTIVE_RUN_CHECKLIST.md`。本地完整 unittest 为 **143 项通过**；测试通过不替代真实公开数据覆盖。
