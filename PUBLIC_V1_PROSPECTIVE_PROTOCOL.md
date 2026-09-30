# PUBLIC_V1 前瞻研究协议

策略 `A_CSI300_QVM_TIMING_V1` v1.0.0 参数冻结；公开数据提供方 `PUBLIC_V1`、执行模型 1.0、预测 schema 1 各自独立。Tushare 是可选、非活动的历史研究适配器，不参与本协议，也不需要 token。

## 机械信号与证据分层

每月首个日期不早于 15 日的 A 股正常交易日收盘后运行 `python stock_selector/run_prospective.py`。交易日由公开 Baostock 日历查询，不在代码里硬编码。程序按上海实际生成时间判断；过去日期的 `--research-date` 只能进入 `stock_selector/outputs/retrospective/`，不能被事后提升为 prospective。非信号日默认跳过 primary。`stock_selector/outputs/prospective/` 和 `retrospective/` 各自保存 manifest 与证据标签；两者不能合并统计。

正式预测要求完整的 300 只成分、每只的 PIT 财报披露日、Q/V/M 因子、动量历史、EMA200、最新价、证券状态和行业。任一核心项缺失则 fail closed。公司行动与交叉来源检查显示在数据质量明细中；后续账户执行必须单独审计公司行动。缺少辅助二次核对只形成警告。当前优先级写于 `stock_selector/data_public/policy.py`，实际自动采集仍以 Baostock SDK 为主；中证指数和巨潮等官网只有连通性探测，尚无经验证的正式解析器。manifest 会把这种情况明确写作“官方 parser 未配置 → Baostock”，不能误读为官网请求失败后的实测回退。中证指数/交易所/巨潮与 Baostock 冲突时，应停止封存并人工核对官方记录，不能默默择一。

## 封存与核验

每次 Baostock 原始表响应及规范化表保留请求名称、参数、抓取时间、parser 版本、原始和规范化 SHA-256。正式预测包含股票池、财务、行情、候选、精选、可执行和计划文件、来源与策略 manifest、预测记录和 seal。`python stock_selector/verify_prediction.py <预测目录>` 核验文件哈希；已有 primary 不覆盖，再次运行生成另一个非 primary 记录。primary 记录需干净 Git 工作树；`--allow-dirty` 只允许降级研究记录。`prospective_registry.csv` 只追加首个封存预测；评价另写 `evaluation_registry.csv`，不会修改旧预测。哈希链能发现普通本地改动，但未接入外部时间戳或远程不可变存储，不能宣称防恶意篡改。

实际行情和财报源的网页内容、披露时间、行业编码、复权定义、成分调整必须在首次正式运行前抽样实测。健康检查 `python stock_selector/check_public_sources.py` 只做少量请求；站点首页可达不等于对应 dated API/parser 可用。

## 后续独立评价与账户

20、63、126 个交易日到期后，`python stock_selector/run_outcome_evaluation.py <预测目录> --stock-prices <复权收盘CSV> --benchmark-prices <指数复权收盘CSV> --source-manifest <哈希清单JSON> --horizon 20` 生成选择结果诊断。它比较信号收盘至未来收盘，与次日开盘执行收益不同；实际价格文件由输入清单哈希核对，评价运行时间必须晚于目标收盘。每个评价只追加登记一次。

观察到执行日开盘后，`python stock_selector/run_execution_reconciliation.py <预测目录> --observations <按ticker键控的JSON> --source-manifest <含source/raw_sha256的JSON>` 单独输出开盘核对，不改预测，也不下单。需要连续影子账户时，`python stock_selector/run_prospective_shadow.py --bars <每日未复权OHLC与证券状态JSON> --actions <公司行动JSON> --calendar <交易日数组JSON> --source-manifest <哈希及覆盖声明JSON>` 对所有 primary 预测从首日重放同一账户；历史前缀变化即拒绝。输入清单须有 source、retrieved_at、price_basis=`unadjusted`、bars/actions/calendar 的 SHA-256、`corporate_actions_complete=true`。这一覆盖声明必须由实际来源审计支持，空事件数组本身不构成证明。执行源收集、公司行动核验及逐日数据目前尚未自动化，因此未来连续 NAV 尚未产生。

`stock_selector/outputs/prospective/prospective_performance.csv` 是可再生状态表；样本不足 12 个正式月度信号时标 `insufficient_sample`。无有效基准或成交数据的指标保持空值，不补造收益、命中率或换手率。真实人工成交继续保存在 Git 忽略的 `stock_selector/user_data/`，不得反向改写预测或影子账户。

## 当前边界（2026-09-30）

尚无策略冻结后的正式前瞻信号。官方成分/公告解析器、独立交叉核验、可靠中文历史行业、自动收集已审计未复权执行价与公司行动仍缺失。当前网络环境的六个公开来源探测均返回 `URLError`；这是本机连通性观察，不证明服务本身不可用。因此当前不能承诺下一个信号日一定自动封存 primary。缺口应使系统 fail closed，并保留失败原因；不能通过历史补录或放松策略参数制造记录。
