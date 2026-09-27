"""Structured work counters and deterministic, human-readable run summaries."""
from __future__ import annotations

import json
from typing import Callable

PROGRESS_PREFIX = "@@SELECTOR_PROGRESS@@"
STAGES = ("连接与准备股票池", "获取行情与财务", "Q/V/M筛选与建仓计划", "持仓复核与账户指导", "保存结果与汇报")
ProgressCallback = Callable[[int, int, int, str], None]


def emit_progress(stage: int, completed: int, total: int, detail: str) -> None:
    print(PROGRESS_PREFIX + json.dumps({
        "stage": stage, "completed": completed, "total": total, "detail": detail,
    }, ensure_ascii=False), flush=True)


def parse_progress(line: str) -> dict | None:
    if not line.startswith(PROGRESS_PREFIX):
        return None
    try:
        event = json.loads(line[len(PROGRESS_PREFIX):])
        if not isinstance(event, dict):
            return None
        stage, completed, total = (event[k] for k in ("stage", "completed", "total"))
        if any(type(v) is not int for v in (stage, completed, total)):
            return None
        if not 1 <= stage <= len(STAGES) or total <= 0 or not 0 <= completed <= total:
            return None
        if not isinstance(event.get("detail"), str):
            return None
        return event
    except (ValueError, KeyError, TypeError):
        return None


def progress_percent(event: dict) -> float:
    return 100 * event["completed"] / event["total"]


def progress_label(event: dict) -> str:
    return (f'{event["stage"]}/{len(STAGES)} {STAGES[event["stage"] - 1]} · '
            f'当前步骤 {event["completed"]}/{event["total"]}，{progress_percent(event):.1f}%'
            f' · {event["detail"]}')


def format_run_summary(metadata: dict) -> str:
    provider = metadata["provider"]
    selector = metadata["selector"]
    counts = selector["counts"]
    plan = metadata["plan"]
    requested, built = provider["requested_members"], provider["built_rows"]
    errors = provider.get("errors", {})
    if built == requested and not errors:
        coverage = f"{requested}只股票都已完成取数，没有记录下载错误。"
    else:
        coverage = (f"股票池{requested}只，成功构建{built}只，记录取数错误{len(errors)}只。"
                    "数据不完整时不能把空结果理解为没有投资机会。")
    lines = [f'筛选完成｜节点：{selector["as_of"]}', "", coverage,
             f'其中复用缓存{provider.get("cache_hits", 0)}只；取数成功不等于所有因子齐全。',
             f'质量指标齐全{counts["quality_complete"]}只，质量通过{counts["quality_pass"]}只，'
             f'质量及估值通过{counts["value_pass"]}只。', "",
             f'1. {counts["fundamental_candidates"]}只通过Q/V/M筛选，可以进入观察名单。']
    trend = provider.get("market_trend", "unknown")
    if trend == "down":
        gate = "沪深300未高于EMA200，触发大盘过滤条件，暂停新建仓。"
    elif trend == "up":
        gate = "沪深300高于EMA200，大盘过滤通过；个股仍需通过自身趋势条件。"
    else:
        gate = "大盘趋势数据不足或未知，不能确认新建仓条件。"
    lines += [f'2. {gate}可执行候选{counts["actionable_candidates"]}只，建仓计划{plan["ideas"]}只。']
    if not plan["ideas"] and trend == "up":
        lines.append("本次无建仓计划：" + (
            "没有股票通过Q/V/M筛选。" if not counts["fundamental_candidates"] else
            "Q/V/M观察名单中没有股票通过个股趋势确认。" if not counts["actionable_candidates"] else
            "请查看建仓计划限制及完整候选表。"))
    if plan.get("overheated"):
        lines.append(f'大盘过热：计划需等待{plan["entry_delay_sessions"]}个交易日并重新复核，不代表立即买入。')
    account_guidance = metadata.get("account_guidance")
    if account_guidance is not None:
        lines.append(f'3. 建议股数大于0的计划{account_guidance["positive_quantity_plans"]}只（与筛选候选数不同）。')
        for reason, number in account_guidance.get("zero_quantity_reasons", {}).items():
            lines.append(f'   {number}只未生成买入股数：{reason}。')
    if errors:
        lines.append("股票池取数不完整，所有建议买入股数已置零；错误明细见official_run_metadata.json。")
    review = metadata.get("holding_review", {})
    lines += ["", f'持仓复核{review.get("holdings_reviewed", 0)}只，退出复核信号{review.get("exit_signals", 0)}只；'
              f'持仓／计划报价错误{len(metadata.get("quote_errors", {}))}只。',
              "暂停新建仓不等于全部卖出已有持仓；请单独查看持仓复核。"]
    dates = selector.get("data_date_ranges", {}).get("price_as_of", {})
    benchmark = provider.get("benchmark", {})
    lines += ["", f'个股行情日期范围：{dates.get("min", "未知")} ～ {dates.get("max", "未知")}；'
              f'沪深300行情日期：{benchmark.get("price_as_of", "未知")}。',
              f'股票池快照日期：{provider.get("membership_snapshot", "未知")}。',
              "以上为数据源实际返回日期，并非实时行情；系统未核实是否覆盖最近交易日。", "",
              "本工具只提供规则筛选与人工复核建议，不保证收益，也不会自动下单。"]
    return "\n".join(lines) + "\n"
