# -*- coding: utf-8 -*-
"""Spot-only sizing policies.

The LLM/rule engine decides direction and confidence.  This module owns sizing,
so an uncalibrated model cannot turn an arbitrary quantity or leverage number
into a large position.
"""
from __future__ import annotations

from copy import deepcopy

from config import CFG


EPS = 1e-9


def is_spot_mode() -> bool:
    return str(getattr(CFG, "TRADING_MODE", "spot")).strip().lower() == "spot"


def _confidence(value) -> str:
    value = str(value or "低").strip()
    return value if value in {"低", "中", "高"} else "低"


def _current_exposure(executor, price: float) -> float:
    if executor.position is None:
        return 0.0
    equity = executor.mark_to_market(price)
    if equity <= EPS:
        return 0.0
    return abs(float(executor.position["qty"]) * float(price)) / equity * 100.0


def _next_high_step(current: float, steps: tuple[float, ...]) -> float:
    for step in steps:
        if step > current + EPS:
            return step
    return steps[-1]


def _number(market: dict, key: str) -> float | None:
    """Read a finite numeric market field without importing pandas/numpy."""
    try:
        value = float(market.get(key))
    except (AttributeError, TypeError, ValueError):
        return None
    return value if value == value and abs(value) != float("inf") else None


def _confirmed_risk_cap(price: float, market: dict, executor,
                        max_exp: float) -> float:
    """Maximum exposure whose structural-stop loss fits the equity risk budget."""
    atr = _number(market, "atr14")
    atr_pct = _number(market, "atr_pct")
    if atr is None and atr_pct is not None:
        atr = price * atr_pct / 100.0
    stop_candidates = []
    low10 = _number(market, "low_10_prev")
    if low10 is not None and 0 < low10 < price:
        stop_candidates.append(low10)
    ema_slow = _number(market, "ema_slow")
    if ema_slow is not None and atr is not None:
        structural = ema_slow - float(getattr(CFG, "SPOT_DEEP_BREAK_ATR", 1.0)) * atr
        if 0 < structural < price:
            stop_candidates.append(structural)
    existing_stop = None
    if executor.position is not None:
        try:
            existing_stop = float(executor.position.get("stop_price"))
        except (TypeError, ValueError):
            existing_stop = None
    if existing_stop is not None and 0 < existing_stop < price:
        stop_candidates.append(existing_stop)
    if not stop_candidates:
        return max_exp
    stop_distance_pct = (price - min(stop_candidates)) / price * 100.0
    if stop_distance_pct <= EPS:
        return max_exp
    budget = max(0.0, float(getattr(CFG, "SPOT_RISK_BUDGET_PERCENT", 2.0)))
    return max(0.0, min(max_exp, budget / stop_distance_pct * 100.0))


def apply_spot_sizing(decision: dict, executor, price: float,
                      ema_slow: float | None = None,
                      macd_dif: float | None = None,
                      macd_dea: float | None = None,
                      market: dict | None = None) -> dict:
    """Return a spot-safe decision whose quantity is determined by confidence.

    BUY uses target exposure rather than trusting the model's raw quantity:
    - flat + medium/high -> 10% probe;
    - additions require profit, price above EMA50 and bullish MACD;
    - medium tops out at 25%; high advances one step through 25/50/75%.
    SELL reduces faster: low 10pp, medium 25pp, high closes completely.
    """
    out = deepcopy(decision)
    out["leverage"] = 1
    action = str(out.get("action", "HOLD")).strip().upper()
    conf = _confidence(out.get("confidence_level"))
    out["confidence_level"] = conf
    current = _current_exposure(executor, price)
    max_exp = max(0.0, min(100.0, float(getattr(CFG, "SPOT_MAX_EXPOSURE_PERCENT", 75.0))))
    probe = max(0.0, min(max_exp, float(getattr(CFG, "SPOT_PROBE_PERCENT", 10.0))))
    medium_cap = max(probe, min(max_exp, float(getattr(CFG, "SPOT_MEDIUM_TARGET_PERCENT", 25.0))))
    high_steps = tuple(sorted({probe, medium_cap,
                               min(max_exp, float(getattr(CFG, "SPOT_HIGH_STEP_PERCENT", 50.0))),
                               max_exp}))
    min_change = max(0.0, float(getattr(CFG, "SPOT_MIN_REBALANCE_PERCENT", 5.0)))
    stats = executor.stats

    def hold(reason: str, stat: str | None = None) -> dict:
        if stat:
            stats[stat] = stats.get(stat, 0) + 1
        out["action"] = "HOLD"
        out["quantity_percent"] = 0.0
        out["reasoning_summary"] = reason
        out["spot_target_percent"] = round(current, 2)
        return out

    sizing_mode = str(getattr(CFG, "SPOT_SIZING_MODE", "confidence")).strip().lower()
    if sizing_mode not in {"confidence", "fixed", "confirmed"}:
        raise ValueError(f"SPOT_SIZING_MODE 非法: {sizing_mode!r}")
    if sizing_mode == "fixed":
        if action == "BUY":
            target = min(max_exp, max(0.0, float(getattr(CFG, "SPOT_FIXED_TARGET_PERCENT", 10.0))))
            delta = target - current
            if delta < min_change - EPS:
                return hold("现货固定仓位对照：已达到目标仓位", "spot_hysteresis_hold")
            stats["spot_fixed_entry"] = stats.get("spot_fixed_entry", 0) + 1
            out["action"] = "BUY"
            out["quantity_percent"] = round(delta, 4)
            out["spot_target_percent"] = round(target, 2)
            out["reasoning_summary"] = f"固定仓位对照：目标{target:.1f}%"
            return out
        if action in {"SELL", "CLOSE"}:
            if current <= EPS:
                return hold("现货固定仓位对照：空仓不卖", "spot_short_block")
            out["action"] = "CLOSE"
            out["quantity_percent"] = 0.0
            out["spot_target_percent"] = 0.0
            out["leverage"] = 1
            out["reasoning_summary"] = "固定仓位对照：方向离场信号全清"
            return out
        return hold("现货固定仓位对照：保持仓位")

    if sizing_mode == "confirmed":
        market = dict(market or {})
        market.setdefault("close", price)
        if ema_slow is not None:
            market.setdefault("ema_slow", ema_slow)
        if macd_dif is not None:
            market.setdefault("macd_dif", macd_dif)
        if macd_dea is not None:
            market.setdefault("macd_dea", macd_dea)

        close = _number(market, "close") or float(price)
        prev_close = _number(market, "prev_close")
        ema_fast = _number(market, "ema_trend")
        ema_fast_prev = _number(market, "ema_trend_prev")
        slow = _number(market, "ema_slow")
        slow_prev = _number(market, "ema_slow_prev")
        dif = _number(market, "macd_dif")
        dea = _number(market, "macd_dea")
        hist = _number(market, "macd_hist")
        hist_prev = _number(market, "macd_hist_prev")
        atr = _number(market, "atr14")
        if atr is None:
            atr_pct = _number(market, "atr_pct")
            atr = close * atr_pct / 100.0 if atr_pct is not None else None
        low10 = _number(market, "low_10_prev")
        low5 = _number(market, "low_5_prev")
        high20 = _number(market, "high_20_prev")
        high_prev = _number(market, "high_prev")

        def rebalance(target: float, reason: str, stat: str) -> dict:
            target = max(0.0, min(max_exp, target))
            delta = target - current
            if abs(delta) < min_change - EPS:
                return hold(reason + "；变化小于再平衡门槛", "spot_hysteresis_hold")
            stats[stat] = stats.get(stat, 0) + 1
            out["action"] = "BUY" if delta > 0 else "SELL"
            out["quantity_percent"] = round(abs(delta), 4)
            out["spot_target_percent"] = round(target, 2)
            out["reasoning_summary"] = reason
            return out

        if current > EPS:
            deep_break = ((low10 is not None and close < low10)
                          or (slow is not None and atr is not None
                              and close < slow - float(getattr(
                                  CFG, "SPOT_DEEP_BREAK_ATR", 1.0)) * atr))
            slow_break = (slow is not None and slow_prev is not None
                          and prev_close is not None
                          and close < slow and prev_close < slow_prev)
            fast_break = (ema_fast is not None and ema_fast_prev is not None
                          and prev_close is not None and hist is not None
                          and hist_prev is not None and close < ema_fast
                          and prev_close < ema_fast_prev and hist < hist_prev)
            if deep_break:
                stats["spot_confirmed_exit"] = stats.get("spot_confirmed_exit", 0) + 1
                out.update(action="CLOSE", quantity_percent=0.0,
                           spot_target_percent=0.0,
                           reasoning_summary="确认式仓位：10日结构低点/EMA50-ATR失守，清仓")
                return out
            if slow_break and current > probe + min_change - EPS:
                return rebalance(probe, "确认式仓位：连续两根低于EMA50，降至试探仓",
                                 "spot_confirmed_reduce_slow")
            if fast_break and current > probe + min_change - EPS:
                lower_steps = [s for s in high_steps if s < current - EPS]
                target = lower_steps[-1] if lower_steps else probe
                return rebalance(target, "确认式仓位：快线连续失守且MACD柱减弱，降一级",
                                 "spot_confirmed_reduce_fast")
            if action == "CLOSE":
                stats["spot_confirmed_exit"] = stats.get("spot_confirmed_exit", 0) + 1
                out.update(action="CLOSE", quantity_percent=0.0,
                           spot_target_percent=0.0,
                           reasoning_summary="确认式仓位：原策略方向离场信号，清仓")
                return out
            # A source SELL is commonly only an RSI-overheat suggestion. In confirmed
            # mode it must not pre-empt a valid breakout or liquidate a healthy trend;
            # the mechanical fast/slow/deep-break rules above own de-risking.

        if current <= EPS:
            if action != "BUY":
                return hold("确认式仓位：空仓等待原策略BUY")
            risk_cap = _confirmed_risk_cap(close, market, executor, max_exp)
            target = min(probe, risk_cap)
            if target < probe - EPS:
                stats["spot_confirmed_risk_cap"] = stats.get("spot_confirmed_risk_cap", 0) + 1
            if target < min_change - EPS:
                return hold("确认式仓位：结构止损过远，风险预算不足")
            stats["spot_probe"] = stats.get("spot_probe", 0) + 1
            out.update(action="BUY", quantity_percent=round(target, 4),
                       spot_target_percent=round(target, 2),
                       reasoning_summary=f"确认式仓位：原策略BUY建立{target:.1f}%试探仓")
            return out

        entry = float((executor.position or {}).get("entry_price") or close)
        profitable = close > entry
        trend_ok = (slow is not None and slow_prev is not None
                    and ema_fast is not None and dif is not None and dea is not None
                    and close > slow and slow > slow_prev and ema_fast > slow and dif > dea)
        atr_confirm = atr is not None and close >= entry + (
            float(getattr(CFG, "SPOT_ADD_CONFIRM_ATR", 1.0)) * atr)
        last_add = int((executor.position or {}).get("last_add_bar", -10**9))
        bars_ok = (int(getattr(executor, "_bar_counter", 0)) - last_add
                   >= max(0, int(getattr(CFG, "SPOT_ADD_MIN_BARS", 2))))
        if not profitable:
            stats["spot_confirmed_block_profit"] = stats.get("spot_confirmed_block_profit", 0) + 1
        elif not trend_ok:
            stats["spot_confirmed_block_trend"] = stats.get("spot_confirmed_block_trend", 0) + 1
        elif not atr_confirm:
            stats["spot_confirmed_block_atr"] = stats.get("spot_confirmed_block_atr", 0) + 1
        elif not bars_ok:
            stats["spot_confirmed_block_interval"] = stats.get("spot_confirmed_block_interval", 0) + 1
        if not profitable or not trend_ok or not atr_confirm or not bars_ok:
            return hold("确认式仓位：趋势/盈利/ATR/间隔条件未全部满足", "spot_confirm_block")

        high_target = float(getattr(CFG, "SPOT_HIGH_STEP_PERCENT", 50.0))
        if current < (probe + medium_cap) / 2.0:
            raw_target, stat = medium_cap, "spot_confirmed_add_25"
            reason = "确认式仓位：趋势与1ATR盈利确认，升至25%"
        elif current < (medium_cap + high_target) / 2.0:
            if high20 is None or close <= high20:
                stats["spot_confirmed_block_breakout"] = stats.get("spot_confirmed_block_breakout", 0) + 1
                return hold("确认式仓位：尚未突破前20根高点", "spot_confirm_block")
            raw_target, stat = high_target, "spot_confirmed_add_50"
            reason = "确认式仓位：突破前20根高点，升至50%"
        elif current < (high_target + max_exp) / 2.0:
            pullback_near_fast = (low5 is not None and ema_fast is not None and atr is not None
                                  and low5 <= ema_fast + 0.5 * atr)
            recovered = high_prev is not None and close > high_prev
            if not pullback_near_fast or not recovered:
                stats["spot_confirmed_block_pullback"] = stats.get("spot_confirmed_block_pullback", 0) + 1
                return hold("确认式仓位：等待EMA快线附近回踩后重新突破", "spot_confirm_block")
            raw_target, stat = max_exp, "spot_confirmed_add_75"
            reason = "确认式仓位：回踩快线后重新突破，升至75%"
        else:
            return hold("确认式仓位：已在最高仓位级别")

        risk_cap = _confirmed_risk_cap(close, market, executor, max_exp)
        target = min(raw_target, risk_cap)
        if target < raw_target - EPS:
            stats["spot_confirmed_risk_cap"] = stats.get("spot_confirmed_risk_cap", 0) + 1
            reason += f"；风险预算压缩至{target:.1f}%"
        if target <= current + min_change - EPS:
            return hold(reason + "，无足够加仓空间")
        return rebalance(target, reason, stat)

    if action == "BUY":
        if conf == "低":
            return hold("现货仓位引擎：低信心不新开/加仓", "spot_low_conf_block")
        if current < min_change:
            target = probe
            stats["spot_probe"] = stats.get("spot_probe", 0) + 1
        else:
            entry = float((executor.position or {}).get("entry_price") or price)
            confirmed = (float(price) > entry
                         and ema_slow is not None and float(price) >= float(ema_slow)
                         and macd_dif is not None and macd_dea is not None
                         and float(macd_dif) > float(macd_dea))
            if not confirmed:
                return hold("现货仓位引擎：加仓确认不足，保持原仓位", "spot_confirm_block")
            target = medium_cap if conf == "中" else _next_high_step(current, high_steps)
            stats["spot_add"] = stats.get("spot_add", 0) + 1
        target = min(target, max_exp)
        delta = target - current
        if delta < min_change - EPS:
            return hold("现货仓位引擎：目标变化小于再平衡门槛", "spot_hysteresis_hold")
        out["action"] = "BUY"
        out["quantity_percent"] = round(delta, 4)
        out["spot_target_percent"] = round(target, 2)
        out["reasoning_summary"] = (
            f"现货信心仓位：{conf}信心，目标{target:.0f}%（原{current:.1f}%）；"
            f"{str(decision.get('reasoning_summary', ''))}"
        )[:200]
        return out

    if action == "SELL":
        if current <= EPS:
            return hold("现货模式空仓，SELL不允许开空", "spot_short_block")
        if conf == "高":
            out["action"] = "CLOSE"
            out["quantity_percent"] = 0.0
            out["spot_target_percent"] = 0.0
            stats["spot_conf_close"] = stats.get("spot_conf_close", 0) + 1
        else:
            reduce_pp = (float(getattr(CFG, "SPOT_MEDIUM_REDUCE_PERCENT", 25.0))
                         if conf == "中" else
                         float(getattr(CFG, "SPOT_LOW_REDUCE_PERCENT", 10.0)))
            reduce_pp = min(current, max(0.0, reduce_pp))
            if reduce_pp < min_change - EPS:
                return hold("现货仓位引擎：减仓变化小于再平衡门槛", "spot_hysteresis_hold")
            out["quantity_percent"] = round(reduce_pp, 4)
            out["spot_target_percent"] = round(max(0.0, current - reduce_pp), 2)
            stats["spot_reduce"] = stats.get("spot_reduce", 0) + 1
        out["leverage"] = 1
        out["reasoning_summary"] = (
            f"现货信心减仓：{conf}信心，目标{out['spot_target_percent']:.0f}%；"
            f"{str(decision.get('reasoning_summary', ''))}"
        )[:200]
        return out

    if action == "CLOSE":
        out["quantity_percent"] = 0.0
        out["spot_target_percent"] = 0.0
        return out

    out["action"] = "HOLD"
    out["quantity_percent"] = 0.0
    out["spot_target_percent"] = round(current, 2)
    return out
