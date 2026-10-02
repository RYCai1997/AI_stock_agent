# PUBLIC_V1 首次正式 prospective primary 运行准备报告

**结论：`READY_FOR_FIRST_PROSPECTIVE_PRIMARY`。** 截至 2026-10-02，冻结策略 `A_CSI300_QVM_TIMING_V1` v1.0.0 与 `PUBLIC_V1` 的运行链已用真实公开数据完成首次正式日之前的验收。下一个正式信号日由 Baostock 公开交易日历计算为 **2026-10-15**；只有上海时间该日收盘后、干净 Git 树、实际数据再通过相同门禁，运行才会登记第一份 prospective primary。现在没有 primary，也没有 prospective registry。

## 真实公开数据 dry run

对 2026-09-30 执行 `--research-date`，真实调用 Baostock 交易日历、CSI300 成分、行业、财务、年度 EPS、日线和沪深300基准。最终在干净提交 `c242c09278b803234aef96587ca2f924b5ed77f0` 上生成回溯包 `stock_selector/outputs/retrospective/2026-09-30/rerun_004`，仅标记 `retrospective_reconstruction`，`prospective_primary=false`。该包没有选出买入股票；这是冻结 V1 的实际结果，没有据此改参数或重跑挑版本。

| 核验项 | 结果 |
|---|---:|
| CSI300 成分 / 请求 / 建成行 | 300 / 300 / 300 |
| Provider errors | 0 |
| 财报披露日、成分更新日、当日最新价格 | 各 300/300 |
| Q/V/M 原始因子齐全 | 293/300；另 7 只合法历史不足 |
| Momentum 原始齐全 | 299/300；缺 12-1 历史的股票未进入需要 Momentum 的排名集合 |
| EMA200、行业、ST/交易状态 | 各 300/300 |
| `quality_complete` | 241/300；金融股及历史不足按冻结 V1 排除 |
| 无法解释的缺失 / provider failure | 0 / 0 |
| 当次登录 / 重连 / 逐股重试 | 1 / 0 / 0 |
| 当前 manifest 的真实 fallback | 0 |

7 只年度 EPS 不足股票的上市日期、年度、`statDate/pubDate` 与处理方式见 [EPS 审计](PUBLIC_V1_EPS_CONTINUITY_AUDIT_2026-10-02.md)。另发现 `sz.302132` 的早期年报挂在前代码 `sz.300114` 下；用第二公开来源确认报告存在后，按 Baostock 代码承继和披露日补全同一 `epsTTM` 字段，公式未变。旧缓存中 2422 条“官方 parser 未配置”标签被明确更正为 `source_selection_reason`，最终 manifest 的 fallback 为 0；没有声称调用失败的官方 dated endpoint。

最终包含 4523 条来源记录。逐条重新计算包内原始/规范化 SHA-256，**0 不匹配**；300 行中成分更新日、财报披露日、价格日均 **0 条晚于 2026-09-30**。行业与证券状态无空值。`sh.601059` 的真实当日 `tradestatus=0`、`security_eligible=false`，按停牌状态处理，没有混为 provider 失败。

## 封存与重跑纪律

- `verify_prediction.py` 对最终真实包 `rerun_004` 返回 **`SEALED`**。
- 只复制最终包并改动副本 `portfolio_plan.csv`，验证返回 **`INVALID`**；原包再次验证仍 **`SEALED`**。
- 同一研究日期的包按 `rerun_001` 至 `rerun_004` 分别保留，早期封存包未覆盖。最终核验以已更正来源措辞、干净提交生成的 `rerun_004` 为准。
- 没有创建 `stock_selector/outputs/prospective/prospective_registry.csv`；所有研究日期运行均未登记 primary。
- 收盘时点、非信号日、历史 research date、脏 Git 树与 `--allow-dirty` 降级行为，以及首月到次月的影子账户连续性和次日执行核对，沿用现有针对性测试与 [前次实测报告](PROSPECTIVE_OPERATIONAL_READINESS_2026-10-01.md) 的结果。本轮没有把合成账户输入说成真实成交。

## 公开源和验证状态

本机 `check_public_sources.py` 在 2026-10-02 真实运行：Baostock 的 dated SDK 日历端点为 `available`，并已完成上述 300 股真实抓取；中证指数、上交所、深交所、巨潮均为 `degraded`（首页可访问，正式 dated parser 未验证）；东方财富行情端点为 `unavailable`（`RemoteDisconnected`）。用于 EPS 代码承继审计的东方财富财报元数据端点单独实测可用，不能推断其行情端点可用。

本地 `python -m unittest discover -s tests` **149 项通过**。Public GitHub `main` 的提交 `c242c09` 已推送，[Windows CI 第 12 次运行](https://github.com/RYCai1997/AI_stock_agent/actions/runs/36959834421) **completed successfully**。仓库公开网页无需登录可访问；可见性未更改。

2026-10-15 前按 [正式运行检查表](PROSPECTIVE_RUN_CHECKLIST.md) 确认 Git 干净、测试、时钟、网络与公开源健康。正式日收盘后重新获取当天的 300 股真实数据并通过门禁，才封存第一次 primary；若当天源失效或任何核心门禁不通过，应 fail closed，不把本次 retrospective 包升格为 prospective，也不因候选结果改冻结策略。
