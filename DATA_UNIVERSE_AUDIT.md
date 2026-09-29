# 沪深300历史成分审计

当前 Baostock 接口 `query_hs300_stocks(as_of)` 接受历史日期，但这本身不证明历史成分完整。研究回放必须检查成分遗漏、临时调整、退市标的及幸存者偏差。

## 当前证据状态

本仓库不包含已核验的中证指数历史全量成分表，也没有可复核的逐日期公告比对结果。当前默认 Python 环境未直接配置 Baostock；工作区另有可用的 Baostock 依赖目录，但尚未收集并核验完整月度历史快照。因此历史成分完整性仍为 **UNRESOLVED**；不能把 `membership_snapshot=max(updateDate)` 当作整池一致更新日期或无幸存者偏差证明。

正式 provider 现在另外保存 `membership_update_min`、`membership_update_max`、`membership_update_unique_count`、逐股票 `membership_update_date` 及 `membership_updates_by_ticker`。这些是数据源返回状态，不等于中证指数公告的生效日期。

## 抽查计划

每年选择半年调样前后两个节点，先覆盖 2022、2023、2024、2025 年的 5 月末／6 月末与 11 月末／12 月末。另根据实际公告补充临时调整前后日期，特别记录退市、合并、特别处理和指数样本临时替换。具体生效日须以当年中证指数公告为准，不能从日历月末推断。

1. 对每个目标日期运行正式节点，保存 `raw_metrics.csv` 和 `official_run_metadata.json`。
2. 从中证指数公告或官方历史成分文件核对生效日期与完整股票代码，整理为 CSV，表头 `as_of,ticker,notice_url`，同一日期每只股票一行。保留原始公告 URL 与下载时间。
3. 运行 `python stock_selector/audit_universe.py --snapshots-dir monthly_snapshots --official-membership official_membership.csv`。
4. 查看 `universe_audit.csv` 的缺失和多余股票；调查退市、临时调整、代码变更及数据源延迟。没有官方名单时状态只能是 `pending_official_notice`。
5. 对每个异常附公告、数据源响应和处理决定。即使抽查吻合，也不能证明未经抽查的全部历史日期无幸存者偏差。

此工具只比对已保存的节点与用户提供的官方名单，不自动抓取或推断公告。历史收益报告应引用审计状态及未解决差异。
