# Tushare PIT 数据阶段执行报告

**日期：**2026-09-30  
**策略：**`A_CSI300_QVM_TIMING_V1` v1.0.0，参数未变  
**当前状态：**`blocked_by_missing_data_credentials`

## 结论

本轮已按附件 Phase 14 的无凭据分支完成 Tushare Provider、原始响应与哈希缓存、九节点等价性审计、预先冻结的漂移门槛、历史行业来源审计、首信号预热期检查、PIT schema 与所需接口/权限清单。**没有 Tushare 令牌或已验证的接口权限，不能构造候选历史数据，因此没有生成 65/65 PIT 快照，也没有运行第一次真实连续 V1 账户回测。**当前结果不能回答收益或增量归因问题。

## SUPPORTED

| 工作 | 输出及验证 |
| --- | --- |
| Tushare 原始数据适配 | `stock_selector/data_history/providers/tushare.py`；请求按 endpoint/参数/字段确定性缓存，原始响应与元数据分别保存，含 SHA-256、UTC 获取时间、行数及未审计覆盖标记；不写入 token。测试覆盖离线读取、缓存篡改、权限错误和缺令牌。原 Baostock、Eastmoney 代码保留。 |
| 九节点等价性审计 | `DATA_PROVIDER_EQUIVALENCE.md`、`provider_equivalence_summary.csv`、`provider_equivalence_by_ticker.csv`。九个参考节点各 300 行；Tushare 候选节点 0 个，因此九行均为 `blocked_by_missing_data_credentials`，数值比较为空。审计程序在有候选数据时比较成分、价格、因子、分数、通过集合和 Top 5，并拒绝未来成分日期。 |
| 事前漂移门槛 | `DATA_PROVIDER_DRIFT_POLICY.json` 固定数值阈值、九节点完整性及五项语义门槛；`tushare_candidate_v1` 是独立数据提供方版本。数值接近而语义或行业未核对时不能认定等价。 |
| 行业口径 | `INDUSTRY_SOURCE_AUDIT.md`：现存九节点 2,700 行，471 只不同股票；17 只在稀疏节点间有不同 L2 行业。保存的中文行业文本含替换字符，不能据此认证原始分类或变化日期。申万/Tushare 分类未替换正式 V1 的 Baostock 行业体系。 |
| 预热期 | `stock_selector/data_history/warmup.py`：首个 2020-03-16 信号要求至少 250 个有效复权交易日、13 个月动量锚点、至少五份信号日前已披露的年度 EPS；不足则逐股 `fail_closed`。建议采集从 2018-01-01 开始的价格及从 2012-01-01 开始的财务，账户区间没有改变。 |
| 数据契约与权限 | `stock_selector/data_history/schema/historical_pit_schema.json`、`REQUIRED_TUSHARE_ENDPOINTS.json`、`TUSHARE_DATA_ACCESS.md`。列出原始价格、复权、披露、成分、行业、证券状态、公司行动的必需字段和时间约束，以及仍需逐项探测的接口。 |

完整 `unittest` 在各逻辑提交前运行；最终 **123 项通过**。这些测试使用人工构造的接口响应和价格/披露样本，不是 Tushare 实际数据质量或历史收益验证。现有数据门禁复核为 `complete_snapshots=9/65`、`monthly_signal_coverage_complete=false`、`replay_inputs_present=false`；旧公开价格缓存为 `465/472`、`collection_complete=false`。

## TENTATIVE

- Tushare `fina_indicator`、`daily_basic`、财报字段可能提供构造正式 Q/V/M 所需的候选原料；ROE、CFO/收入、年度 EPS 稳定性、PE、净现金流估值和 PB 的定义、单位、期间及披露日语义尚未实测。名称相近不等于等价。
- `index_weight` 属于月度成分候选来源；必须仅取 `trade_date <= signal_date` 的记录，并单独抽查中证指数临时调样公告。当前无实际 Tushare 成分记录可比较。
- Provider 的 HTTPS Pro 请求路径已通过注入式本地测试；实际接口连通性、额度、权限、分页表现和历史深度仍待持令牌验证。

## UNRESOLVED

Phase 6–9 的完整未复权/复权价格、暂停交易及精确涨跌停、上市退市/ST、公司行动、历史行业、65/65 正式快照和逐月逐证券覆盖审计均未完成。Phase 10–13 的每日 NAV、现金、持仓、成交、费用、股息、绩效、CSI300 对照、A–H、R0–R7、Monte Carlo 及稳健性实证结果均**不存在**；不能据此判断 V1 是否盈利、是否跑赢基准或哪个机制贡献收益。当前股息税仍未模拟，未来主回测须单列税前分红及保守税敏感性。

最小后续动作是：在本机进程环境中提供有权限的 `TUSHARE_TOKEN`（不要写进 Git 或聊天），逐项探测 `REQUIRED_TUSHARE_ENDPOINTS.json` 所列接口，并保存实际权限/字段结果；随后先重建九个候选节点、完成语义和行业等价审计，再批量采集预热期至 2025-07-31 的历史记录并离线生成 65 个快照。若必需接口权限缺失，维持阻塞状态并指出具体接口，不拼接未经审计的网页数据。只有完整数据审计通过后才能运行正式 H 回测。全部 2020–2025 结果届时仍只属于 `retrospective historical validation`。

## 本轮分阶段提交

1. `9ade793` — Tushare 原始响应 Provider。
2. `53858a4` — 九节点等价性审计与实际阻塞 CSV。
3. `6ed65f9` — 事先冻结漂移容差与语义门槛。
4. `fbde398` — 历史行业口径与编码审计。
5. `a46fe17` — 历史数据预热期 fail-closed 检查。
6. `d38b52c` — 接口权限清单与 PIT schema。

本地复核命令：

```powershell
cd D:\CodexWorkSpace\Stock\AI_stock_agent
$env:PYTHONPATH = (Resolve-Path stock_selector).Path
python -m unittest discover -s stock_selector/tests
python -m data_history.provider_equivalence `
  --reference stock_selector/outputs/research_data `
  --candidate stock_selector/outputs/tushare_snapshots `
  --output .
python stock_selector/audit_backtest_readiness.py --snapshots-dir stock_selector/outputs/research_data
```
