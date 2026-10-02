# 首次及每月 prospective signal day 检查表

正式策略 `A_CSI300_QVM_TIMING_V1` v1.0.0 与 `PUBLIC_V1` 的门槛均不得因候选名单或事后收益而改变。正式日期由公开交易日历计算：每月首个不早于 15 日的 A 股交易日。

## 运行前

- [ ] 在项目目录执行 `git pull`，确认使用预定版本。
- [ ] 执行 `git status --short`，正式 primary 要求输出为空；记录 `git rev-parse HEAD`。
- [ ] 进入 `stock_selector` 目录执行 `python -m unittest discover -s tests`，确认全部通过，然后返回项目根目录。
- [ ] 执行 `python stock_selector/check_public_sources.py`；分别判断首页连通性和真正的 dated endpoint / parser，不把 HTTP 200 当作数据有效。
- [ ] 检查网络、Baostock SDK 实际登录和公开日历查询；核对系统时钟、日期与 `Asia/Shanghai` 时区。
- [ ] 检查上次 operational dry run 的 300 只覆盖、逐股错误、财报披露日、行业编码、价格与证券状态。若核心项不完整，预期正式运行会 fail closed。

## 信号日收盘后

- [ ] 上海时间 15:00 后执行 `python stock_selector/run_prospective.py`，不要用过去日期补造 primary。
- [ ] 对输出目录执行 `python stock_selector/verify_prediction.py <prediction_dir> --registry stock_selector/outputs/prospective/prospective_registry.csv`，结果必须为 `SEALED`。
- [ ] 查看 `run_summary.md`、`selected.csv`、`actionable.csv`、`portfolio_plan.csv` 和 `prediction_record.json`，记录候选与计划，不因名单外观重跑挑选。
- [ ] 查看 `source_manifest.json` 的来源、原始哈希、披露日、fallback / `official_parser_not_configured` 及数据质量明细。
- [ ] 确认 `prospective_registry.csv` 只追加一次，`prospective_primary=true`，Git 提交正确，工作树为干净状态。首次封存后不删除、不覆盖、不调参。

## 次日及未来

- [ ] 执行日真实开盘后，用有来源哈希的行情运行 `run_execution_reconciliation.py`；结果单独保存，不修改预测。
- [ ] 逐日核验未复权行情、证券状态和公司行动覆盖，再运行 `run_prospective_shadow.py`，保留同一账户的现金、挂单、持仓、费用及 NAV。
- [ ] 20、63、126 个真实交易日分别到期且收盘后，使用经哈希核验的复权价格运行 `run_outcome_evaluation.py`；结果进入独立评价 registry。
- [ ] 实际人工成交只记入本地 `user_data`，不得反写 prediction 或 shadow。

任何核心数据、日期、来源或封存核验失败时，保留错误证据，标记 `NOT_READY`；不得降低冻结策略门槛来制造第一份预测。
