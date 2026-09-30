# PUBLIC_V1 前瞻研究阶段执行报告

**项目：** `RYCai1997/AI_stock_agent`  
**日期：** 2026-09-30  
**策略：** `A_CSI300_QVM_TIMING_V1` v1.0.0，参数未改  
**数据提供方：** `PUBLIC_V1`；Tushare 为 optional / inactive  
**代码：** `8c4e660` 已推送公开仓库 `main`

## 完成内容与输出

| 目标 | 本轮结果 |
| --- | --- |
| 公开数据主线 | Baostock SDK 提供交易日历、沪深300成分、行业、结构化财务、行情；原始响应和规范化结果保留请求参数、时间、parser 版本及 SHA-256。东方财富仅做小规模行情 API 健康探测；中证指数、巨潮、沪深交易所仅做站点探测，未实现正式 dated parser。官方源优先级与 Baostock 回退原因写入 manifest，未伪称已经从官方端点取到正式数据。 |
| 预测 | `run_prospective.py` 按上海真实生成时间和公开交易日历判定每月首个不早于 15 日的交易日；历史 `--research-date` 归入 retrospective。核心成分、披露日期、因子、动量、EMA200、最新价、资格、行业不完整时拒绝 primary。 |
| 封存 | 预测包含输入、筛选结果、计划订单、来源/策略 manifest、prediction record、文件 seal；`verify_prediction.py` 检查封存。primary 按日期只认首次，重跑不能覆盖。预测 registry 追加记录，Git 提交和工作树状态进入预测。 |
| 后续核对 | `run_execution_reconciliation.py` 独立核对未来开盘与涨跌停；`run_outcome_evaluation.py` 在 20/63/126 个交易日到期后计算股票、组合和沪深300的复权收盘诊断，并追加独立评价 registry。二者不改预测。 |
| 连续账户 | `run_prospective_shadow.py` 接受带文件哈希、未复权口径和公司行动覆盖声明的每日执行数据，从首个封存预测起在同一账户重放。现金、持仓、挂单、费用、分红、已实现损益和 NAV 有跨月检查点；旧历史漂移会在追加前拒绝。真实人工成交仍归本地 `user_data`，不反写影子策略。 |
| 状态数据 | `prospective_performance.csv` 可生成信号数、实际进入窗口数、影子 NAV、固定 horizon 评价数与可得账户指标；少于 12 个正式信号标 `insufficient_sample`，无数据的基准、命中率或换手率保持空值。 |
| 文档 | `PUBLIC_V1_PROSPECTIVE_PROTOCOL.md` 明确运行命令、三层账户区分、数据门禁与当前限制；`TUSHARE_DATA_ACCESS.md` 改为可选历史适配器。 |

## 验证和实际观察

- 提交前逐阶段执行完整 `python -m unittest discover -s stock_selector/tests`；最终本地 **141 项通过**。测试涵盖日历、PIT 门禁、来源哈希、封存篡改、重跑、后验评价、评价 registry、跨月影子账户及旧订单漂移。
- 本机历史审计为 **9/65** 个 2020-03 至 2025-07 冻结月度节点；缺少逐月完整历史信号及公司行动，不能公布真实连续历史收益。这是 retrospective 缺口，不阻塞未来 prospective 的制度设计。
- 本机六个公开站点/API 的小规模健康探测均返回 `URLError`。这是当前运行环境的网络访问结果，不能推断各网站对普通用户不可用，也不能算作正式 dated 数据读取成功。
- 截至报告时尚无策略冻结后的正式 prospective 信号、后验窗口或真实连续影子 NAV，因此没有收益、超额收益或显著性结论。
- GitHub `main` 与本地提交一致；公开 [Actions 第 9 次运行](https://github.com/RYCai1997/AI_stock_agent/actions/runs/36710638299) 显示 `8c4e660` 的确定性 unittest 工作流成功。仓库保持 Public。

## 是否满足最初需求

**研究框架和证据分层已建立，但“未来信号日可无人干预地使用真实公开数据自动生成正式 primary prediction”尚未被验证，因此本轮未完全满足这一目标。** 代码会在核心数据缺失时拒绝封存，而不是补造结果。当前实际主数据只有 Baostock；官方成分与披露公告解析、跨来源冲突审计、中文行业编码、公开源连通性、逐日未复权执行行情与公司行动自动采集仍需完成或实测。`corporate_actions_complete=true` 是外部审计声明，不可由空事件文件推断。封存哈希及本地 registry 可发现普通修改，尚无外部时间戳或不可变远端锚定，不能称为绝对不可篡改。

## 用户可复核

```powershell
cd D:\CodexWorkSpace\Stock\AI_stock_agent
$env:PYTHONPATH = (Resolve-Path stock_selector).Path
python -m unittest discover -s stock_selector/tests
python stock_selector/check_public_sources.py
python stock_selector/audit_backtest_readiness.py --snapshots-dir stock_selector/outputs/research_data
git status --short
git rev-parse HEAD
```

未来正常信号日收盘后运行 `python stock_selector/run_prospective.py`。仅在所有核心数据和来源记录通过检查时，才会在 `stock_selector/outputs/prospective/` 生成正式预测；否则保留 fail-closed 错误，不把研究重跑冒充未来预测。
