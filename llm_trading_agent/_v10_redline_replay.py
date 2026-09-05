# -*- coding: utf-8 -*-
"""_v10_redline_replay.py —— v10 红线①②离线重放验证（零 API）

背景（2026-09-04）：LLM 熊市止损拖累诊断（llm_bear_stoploss_diagnosis.md）给出 v10 方向：
  红线① 引擎级结构止损：持仓浮亏 ≥ rl1_dd%（默认 5）且收盘 < EMA_SLOW → 引擎强制平
        （把规则引擎「价格<趋势线→离场」的结构锚搬到 LLM 档共用；红线不进 prompt）
  红线② 熊市降杠杆：BUY 决策当根收盘 < EMA_SLOW → 杠杆 cap 到 1x（对齐规则引擎 1x）

本脚本把 LLM 决策源替换为已烧的 repro json（verdict_llm*_repro.json 的 llm_calls[i].decision），
引擎执行逻辑（OrderExecutor / main 主循环语义）原样复用 → 验证红线①②**不需要再调 LLM API**。

用法：
  python _v10_redline_replay.py                          # 默认牛市窗 SOLUSDT 2024-08-01→2025-01-18
  python _v10_redline_replay.py SOLUSDT 2021-10-01 2022-12-31 --rl1 --rl2
  # 红线开关：--rl1（结构止损）/ --rl2（熊市降杠杆）/ 缺省 = 无红线（供 --verify 对拍存档）
  # --verify：跑无红线并逐点对拍 runs/verdict_llm*_equity.csv（校验重放器忠实性）
  # --rl1-dd 8：自定红线①浮亏门槛（默认 5，单位 %）
  # --tag xxx：产物文件名标签

产物：runs/v10rl_{tag}_{SYM}_{S}_{E}.log / _report.md / _result.json
校验通过判据：无红线跑 equity 与存档逐点一致 + 注入决策数 == repro llm_calls 数。
"""
import argparse
import json
import logging
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 第三方库降噪（须在 basicConfig 前设置，否则可能重复打印）
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

import pandas as pd  # noqa: E402

from config import CFG  # noqa: E402  单例实例！覆写必须走实例属性
import data_fetcher  # noqa: E402
import main as M  # noqa: E402
from order_executor import OrderExecutor  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs")
CACHE_DIR = os.path.join(OUT_DIR, "_kline_cache")

# repro cfg 快照中不覆写的键（网络/密钥/路径保持当前环境）
_SKIP_CFG_KEYS = {
    "BASE_DIR", "LLM_BASE_URL", "DEEPSEEK_BASE_URL", "LLM_MODEL_ENV",
    "LLM_PROVIDER", "llm_model", "STATE_FILE", "LOG_FILE",
    "BINANCE_BASE_URLS", "FETCH_TIMEOUT", "DATA_RETRY", "LIVE_START",
}

DEFAULT_WIN = ("SOLUSDT", "2024-08-01", "2025-01-18")


def resolve_win(sym, start, end):
    """repro/equity 存档文件名：参数化窗带后缀；#49 默认牛市窗无后缀。"""
    if (sym, start, end) == DEFAULT_WIN:
        return "", "verdict_llm_repro.json", "verdict_llm_equity.csv"
    sfx = f"_{sym}_{start}_{end}"
    return sfx, f"verdict_llm{sfx}_repro.json", f"verdict_llm{sfx}_equity.csv"


def fetch_cached(sym: str, interval: str, start: str, end: str) -> pd.DataFrame:
    """拉 K 线（带 csv 缓存），返回原始行情 df（未算指标）。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_p = os.path.join(CACHE_DIR, f"{sym}_{interval}_{start}_{end}.csv")
    if os.path.exists(cache_p):
        df = pd.read_csv(cache_p)
        df["open_time"] = pd.to_datetime(df["open_time"])
        return df
    df = data_fetcher.fetch_klines(symbol=sym, interval=interval,
                                   start_date=start, end_date=end)
    if df is None or df.empty:
        raise RuntimeError(f"K线获取失败/为空：{sym} {start}→{end}（网络或代理问题）")
    df.to_csv(cache_p, index=False, encoding="utf-8")
    return df


def apply_cfg_snapshot(cfg_snap: dict) -> None:
    """用 repro 的 cfg 快照覆写 CFG 实例属性，保证重放参数与真跑逐项一致。"""
    if not isinstance(cfg_snap, dict):
        return
    for k, v in cfg_snap.items():
        if k in _SKIP_CFG_KEYS or v is None or not isinstance(v, (str, int, float, bool)):
            continue
        try:
            setattr(CFG, k, v)
        except Exception:
            pass
    CFG.BACKTEST_USE_LLM = False          # 本脚本永远不真调 LLM
    CFG.SYMBOL = cfg_snap.get("SYMBOL", CFG.SYMBOL)
    CFG.INTERVAL = cfg_snap.get("INTERVAL", CFG.INTERVAL)
    eff = (CFG.SYMBOL, CFG.START_DATE, CFG.BACKTEST_END, CFG.BACKTEST_USE_LLM)
    assert eff[3] is False and eff[0] == cfg_snap.get("SYMBOL"), f"cfg 覆写未生效: {eff}"


def replay(sym, start, end, rl1: bool, rl2: bool, rl1_dd: float,
           rl1_mode: str = "dd", rl1_cool: int = 0,
           gate: str = "none", gate_k: int = 10,
           verify: bool = False, tag: str = "") -> dict:
    """离线重放：决策源 = repro json；可选叠加 v10 红线①② + 市况闸门。

    gate=slope：EMA_SLOW 较 gate_k 根前下降 → gate_on（下行市武装红线；
    上行段解除 → 冷却禁闭同步解除，放行反弹 BUY，防震荡市误伤）。
    """
    sfx, repro_name, equity_name = resolve_win(sym, start, end)
    repro_p = os.path.join(OUT_DIR, repro_name)
    if not os.path.exists(repro_p):
        raise SystemExit(f"找不到 repro：{repro_p}\n（该窗口未跑过 LLM 腿，无法离线重放）")
    repro = json.load(open(repro_p, encoding="utf-8"))
    calls = repro.get("llm_calls") or []
    if not calls:
        raise SystemExit(f"repro 无 llm_calls：{repro_p}")
    by_ts = {}
    for rec in calls:
        dec = rec.get("decision")
        if dec is not None:
            by_ts[rec["ts"]] = dec
    print(f"repro：{len(calls)} 条 LLM 决策（{calls[0]['ts']} ~ {calls[-1]['ts']}）")
    apply_cfg_snapshot(repro.get("cfg") or {})

    # 数据 + 指标
    df = fetch_cached(sym, CFG.INTERVAL, start, end)
    df = data_fetcher.calculate_indicators(df)
    truncated = len(df) > CFG.MAX_BACKTEST_BARS
    if truncated:
        df = df.tail(CFG.MAX_BACKTEST_BARS).reset_index(drop=True)
        print(f"⚠️ 超过切片上限，截取最近 {CFG.MAX_BACKTEST_BARS} 根")
    first_valid = df["ema_slow"].first_valid_index()
    if first_valid is None:
        raise SystemExit("历史数据不足，无法形成 ema_slow 趋势线")
    start_idx = df.index.get_loc(first_valid)
    print(f"K线 {len(df)} 根（预热跳过 {start_idx}，回放 {len(df)-start_idx}），"
          f"起点 {df.iloc[start_idx]['open_time']}")

    _model = M._execution_model()
    if _model != "next_open":
        raise SystemExit(f"当前 EXECUTION_MODEL={_model}，replay 仅支持 next_open（repro 生成口径）")

    # 独立日志（filemode=w 覆盖旧文件）
    for h in logging.root.handlers[:]:
        logging.root.removeHandler(h)
    if not tag:
        gtag = f"_g{gate}{gate_k}" if gate != "none" else ""
        if rl1 and rl2:
            tag = f"rl_{rl1_mode}" + (f"dd{int(rl1_dd)}" if rl1_mode == "dd" else "") \
                  + (f"c{rl1_cool}" if rl1_cool > 0 else "") + "_rl2" + gtag
        elif rl1:
            tag = f"rl_{rl1_mode}" + (f"dd{int(rl1_dd)}" if rl1_mode == "dd" else "") \
                  + (f"c{rl1_cool}" if rl1_cool > 0 else "") + gtag
        elif rl2:
            tag = "rl2" + gtag
        else:
            tag = "none" + gtag
    tag0 = tag
    log_p = os.path.join(OUT_DIR, f"v10rl_{tag0}{sfx}.log")
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        filename=log_p, encoding="utf-8", filemode="w")

    ex = OrderExecutor(capital=CFG.CAPITAL, state_path=None)
    equity_curve: list = []
    n_inject = 0
    n_miss = 0
    rl1_hits = []     # 红线①每次触发的记录
    rl2_caps = []     # 红线②每次压杠杆记录
    rl1_cool_blocks = []   # 红线①冷却期吞掉的 BUY
    redline_cool_until = -1   # 红线①离场后禁 BUY 的截止根序号（含）

    for i in range(start_idx, len(df)):
        row = df.iloc[i]
        ts = row["open_time"]
        ts16 = str(ts)[:16]
        price = float(row["close"])
        ema_slow = float(row["ema_slow"]) if pd.notna(row["ema_slow"]) else None
        macd_dif = float(row["macd_dif"]) if pd.notna(row["macd_dif"]) else None
        macd_dea = float(row["macd_dea"]) if pd.notna(row["macd_dea"]) else None
        atr_pct = float(row["atr_pct"]) if pd.notna(row["atr_pct"]) else None

        # ① 日期滚动 / ①.5 资金费率 / ② next_open 结算上根挂起动作
        ex.roll_day(ts, price)
        ex.accrue_funding(ts, price)
        if _model == "next_open":
            _sr = ex.settle_pending(float(row["open"]), ts)
            if _sr and _sr["action"] not in ("HOLD", "CLOSE"):
                logging.info("[%s] %s @ %.2f（次根开盘） | %s | 保证金占比->%.2f%%",
                             str(ts)[:16], _sr["action"], float(row["open"]),
                             _sr["reason"], ex.position_pct(float(row["open"])))
        # ②.5 / ③ 强平 + 价格止损（next_open：收盘识别 → 挂起次根开盘离场）
        ex.check_liquidation(price, ts)
        sl = ex.stop_level()
        if sl is not None and ex.position is not None:
            _long = ex.position["side"] == "long"
            _hit = (price <= sl) if _long else (price >= sl)
            if _hit:
                ex.set_pending_exit(ts, "止损触发（收盘价击穿止损线）")
        # ③.5 移动止盈 + 组合权益止损（均 defer 化）
        ex.check_trailing_tp(price, ts, defer=True)
        ex.check_stop_loss(price, ts, defer=True)

        # ===== v10 市况闸门（regime gate）=====
        #  slope：EMA_SLOW 较 gate_k 根前下降 → gate_on（确认下行才武装红线）。
        #  牛市 EMA50 全程上行→gate 恒关；熊市阴跌→gate 恒开；震荡市下行段开/上行段关。
        gate_on = True
        if gate == "slope" and ema_slow is not None:
            if i - gate_k >= 0:
                ema_prev = float(df.iloc[i - gate_k]["ema_slow"])
                gate_on = ema_slow < ema_prev
            else:
                gate_on = False   # 预热段不足 k 根：不武装（宁可漏救不误伤）

        # ============ v10 红线①：结构止损（触发即挂起，次根开盘强制离场） ============
        #  mode=dd：    浮亏≥rl1_dd% 且 收盘<EMA50（滞后：快跌市 EMA50 下移慢，常砍在深坑）
        #  mode=breach：收盘<EMA50 即走，无浮亏门槛（与规则引擎「破趋势线即离场」同构）
        if (rl1 and gate_on and ex.position is not None
                and ex.pending_exit is None and ema_slow is not None):
            p = ex.position
            if p["side"] == "long":
                dd_pct = (price - p["entry_price"]) / p["entry_price"] * 100.0
                hit = ((rl1_mode == "breach" and price < float(ema_slow))
                       or (rl1_mode == "dd" and dd_pct <= -rl1_dd and price < float(ema_slow)))
                if hit:
                    why = ("收盘<EMA50" if rl1_mode == "breach"
                           else f"浮亏{dd_pct:.1f}%≥{rl1_dd}%且收盘<EMA50")
                    ex.set_pending_exit(ts, f"v10红线①:{why}，强制离场")
                    rl1_hits.append({"trigger_ts": ts16, "trigger_price": price,
                                     "entry_price": p["entry_price"],
                                     "dd_pct": dd_pct,
                                     "ema_slow": float(ema_slow)})
                    if rl1_cool > 0:
                        redline_cool_until = i + rl1_cool
            # 空头镜像（预留：ALLOW_SHORT 场景）
            else:
                dd_pct = (p["entry_price"] - price) / p["entry_price"] * 100.0
                hit = ((rl1_mode == "breach" and price > float(ema_slow))
                       or (rl1_mode == "dd" and dd_pct <= -rl1_dd and price > float(ema_slow)))
                if hit:
                    why = ("收盘>EMA50" if rl1_mode == "breach"
                           else f"空头浮亏{dd_pct:.1f}%≥{rl1_dd}%且收盘>EMA50")
                    ex.set_pending_exit(ts, f"v10红线①:{why}，强制离场")
                    rl1_hits.append({"trigger_ts": ts16, "trigger_price": price,
                                     "entry_price": p["entry_price"],
                                     "dd_pct": dd_pct,
                                     "ema_slow": float(ema_slow)})
                    if rl1_cool > 0:
                        redline_cool_until = i + rl1_cool

        # ④ 决策：待平仓短路 or repro 注入（可选红线②压杠杆）
        if _model == "next_open" and ex.pending_exit is not None:
            result = {"action": "HOLD", "filled_qty_pct": 0.0,
                      "reason": "次根开盘待平仓，本根暂停决策", "price": price}
        else:
            dec = by_ts.get(ts16)
            if dec is None:
                n_miss += 1
                if n_miss <= 3:
                    print(f"⚠️ 无 repro 决策 @ {ts16}（重放与真跑状态分叉？）")
                result = {"action": "HOLD", "filled_qty_pct": 0.0,
                          "reason": "repro缺决策→HOLD", "price": price}
            else:
                decision = dict(dec)
                # ===== v10 红线①冷却：红线离场后 N 根内禁 BUY（堵「砍后接刀」） =====
                if (rl1_cool > 0 and i <= redline_cool_until
                        and decision.get("action") == "BUY"):
                    rl1_cool_blocks.append({"ts": ts16, "price": price,
                                            "orig_reason": str(decision.get("reasoning_summary", ""))[:60]})
                    decision["action"] = "HOLD"
                    decision["quantity_percent"] = 0.0
                    decision["reasoning_summary"] = (f"v10红线①冷却期（离场后{rl1_cool}根内禁再开仓），"
                                                     f"BUY→HOLD（{decision.get('reasoning_summary','')}）")
                # ===== v10 红线②：BUY 且 收盘<EMA50 → 杠杆 cap 1x =====
                if (rl2 and decision.get("action") == "BUY" and ema_slow is not None
                        and price < float(ema_slow)):
                    orig_lev = decision.get("leverage")
                    if orig_lev not in (None, 1):
                        decision["leverage"] = 1
                        rl2_caps.append({"ts": ts16, "price": price,
                                         "orig_leverage": orig_lev,
                                         "ema_slow": float(ema_slow)})
                n_inject += 1
                result = ex.execute_decision(decision, price, ts,
                                             ema_slow=ema_slow,
                                             macd_dif=macd_dif, macd_dea=macd_dea,
                                             atr_pct=atr_pct, defer=True)
        # ⑤ 收盘后再查强平/止损/止盈 + ⑤.5 峰值刷新
        ex.check_liquidation(price, ts)
        ex.check_stop_loss(price, ts, defer=True)
        ex.check_trailing_tp(price, ts, defer=True)
        ex.update_peak(price)
        if result["action"] not in ("HOLD", "CLOSE") and not result.get("deferred"):
            logging.info("[%s] %s @ %.2f | %s | 保证金占比->%.2f%%",
                         str(ts)[:16], result["action"], price,
                         result["reason"], ex.position_pct(price))
        equity_curve.append((ts16, ex.mark_to_market(price)))

    # ---- 对拍存档（仅无红线 + verify）----
    eq_ok = None
    if verify and not rl1 and not rl2:
        arch_p = os.path.join(OUT_DIR, equity_name)
        if os.path.exists(arch_p):
            arch = pd.read_csv(arch_p)
            mine = pd.DataFrame(equity_curve, columns=["time", "equity"])
            # 以存档 time 为基准对齐（重放窗口应与真跑完全一致）
            merged = arch.merge(mine, on="time", suffixes=("_arch", "_replay"))
            if len(merged) == 0:
                eq_ok = "NO_TIME_MATCH"
            else:
                diff = (merged["equity_replay"] - merged["equity_arch"]).abs()
                max_rel = (diff / merged["equity_arch"].abs().replace(0, float("nan"))).max()
                eq_ok = (f"{len(merged)}/{len(arch)} 点对齐，"
                         f"最大相对差 {max_rel * 100:.6f}%") if max_rel < 1e-4 else (
                    f"❌ 最大相对差 {max_rel * 100:.4f}% 超限")
                if max_rel >= 1e-4:
                    bad = merged[diff > 1e-6].head(5)
                    eq_ok += "\n  首个偏差点：\n" + bad.to_string(index=False)
        else:
            eq_ok = f"存档缺失：{arch_p}"

    # ---- 绩效报告（复用 main 口径，与真跑逐项可比）----
    report = M.build_backtest_report(df, ex, equity_curve, start_idx,
                                     use_llm=False, truncated=truncated, repro=None)
    md = report["markdown"]
    # 追加 v10 红线统计节
    extra = ["", "## v10 红线统计", ""]
    gdesc = "关" if gate == "none" else f"slope（EMA_SLOW<{gate_k}根前才武装）"
    extra.append(f"- 市况闸门：{gdesc}")
    if rl1:
        mlabel = {"dd": f"浮亏≥{rl1_dd}%且收盘<EMA50", "breach": "收盘<EMA50 即走"}[rl1_mode]
        extra.append(f"- 红线①（结构止损，mode={rl1_mode}：{mlabel}，"
                     f"冷却{rl1_cool}根禁BUY）触发：**{len(rl1_hits)}** 次"
                     f"{'，冷却吞 BUY **' + str(len(rl1_cool_blocks)) + '** 次' if rl1_cool > 0 else ''}")
        for h in rl1_hits:
            extra.append(f"  - {h['trigger_ts']} 触发价 {h['trigger_price']:.2f} "
                         f"(入场 {h['entry_price']:.2f}，浮亏 {h['dd_pct']:.1f}%，"
                         f"EMA50 {h['ema_slow']:.0f})")
        for b in rl1_cool_blocks[:10]:
            extra.append(f"  - [冷却吞] {b['ts']} 价 {b['price']:.2f}（LLM原由：{b['orig_reason']}）")
    else:
        extra.append("- 红线①：关")
    if rl2:
        extra.append(f"- 红线②（收盘<EMA50 时 BUY 杠杆 cap 1x）：**{len(rl2_caps)}** 次")
        for c in rl2_caps[:15]:
            extra.append(f"  - {c['ts']} 价 {c['price']:.2f}：{c['orig_leverage']}x → 1x")
    else:
        extra.append("- 红线②：关")
    extra += ["", f"- 注入决策：{n_inject}/{len(calls)}（repro 全量）| 缺决策：{n_miss}",
              f"- 对拍存档：{eq_ok if eq_ok is not None else '（非 verify 模式，跳过）'}"]
    md_full = md + "\n".join(extra) + "\n"
    rep_p = os.path.join(OUT_DIR, f"v10rl_{tag0}{sfx}_report.md")
    open(rep_p, "w", encoding="utf-8").write(md_full)

    # ---- 结果 json（供聚合对比）----
    res = {
        "tag": tag0, "sym": sym, "start": start, "end": end,
        "rl1": rl1, "rl2": rl2, "rl1_dd": rl1_dd,
        "rl1_mode": rl1_mode, "rl1_cool": rl1_cool,
        "replay_start": str(df.iloc[start_idx]["open_time"])[:10],
        "bars": len(equity_curve), "n_inject": n_inject, "n_repro": len(calls),
        "n_miss": n_miss, "eq_verify": eq_ok,
        "n_rl1": len(rl1_hits), "n_rl2": len(rl2_caps),
        "n_rl1_cool_block": len(rl1_cool_blocks),
        "final_equity": equity_curve[-1][1], "init_capital": float(CFG.CAPITAL),
        "ret_pct": (equity_curve[-1][1] / float(CFG.CAPITAL) - 1.0) * 100.0,
        "stats": dict(ex.stats),
        "n_closed": len(ex.closed_trades),
    }
    # 已实现盈亏额口径（与报告一致）
    res["realized_usdt"] = ex.realized_pnl
    res_p = os.path.join(OUT_DIR, f"v10rl_{tag0}{sfx}_result.json")
    open(res_p, "w", encoding="utf-8").write(json.dumps(res, ensure_ascii=False, indent=1))
    print(report["text"])
    print(f"\n[v10] rl1={rl1}({rl1_mode},dd={rl1_dd},cool={rl1_cool}) rl2={rl2} "
          f"触发 rl1={len(rl1_hits)} rl2={len(rl2_caps)} "
          f"冷却吞BUY={len(rl1_cool_blocks)} | 注入 {n_inject}/{len(calls)} 缺 {n_miss}")
    if eq_ok:
        print(f"[对拍] {eq_ok}")
    print(f"产物：v10rl_{tag0}{sfx}_report.md / _result.json")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pos", nargs="*", help="SYMBOL START END（缺省=牛市窗）")
    ap.add_argument("--rl1", action="store_true", help="红线①：结构止损")
    ap.add_argument("--rl2", action="store_true", help="红线②：熊市降杠杆")
    ap.add_argument("--rl1-dd", type=float, default=5.0, help="红线①浮亏门槛%（dd mode）")
    ap.add_argument("--rl1-mode", default="dd", choices=["dd", "breach"],
                    help="红线①触发模式：dd=浮亏≥门槛且破EMA50；breach=破EMA50即走")
    ap.add_argument("--rl1-cool", type=int, default=0,
                    help="红线①离场后 N 根内禁 BUY（堵再入场磨损；0=关）")
    ap.add_argument("--gate", default="none", choices=["none", "slope", "reentry"],
                    help="市况闸门：slope=EMA_SLOW 下行才武装；reentry=价格站回EMA50即解除冷却禁闭")
    ap.add_argument("--gate-k", type=int, default=10,
                    help="slope 闸门斜率比较窗口（根）")
    ap.add_argument("--verify", action="store_true", help="无红线跑并逐点对拍存档 equity")
    ap.add_argument("--tag", default="", help="产物标签")
    a = ap.parse_args()
    if a.pos:
        if len(a.pos) != 3:
            raise SystemExit("参数应为 SYMBOL START END 或留空（默认牛市窗）")
        sym, start, end = a.pos[0].upper(), a.pos[1], a.pos[2]
    else:
        sym, start, end = DEFAULT_WIN
    if a.verify and (a.rl1 or a.rl2):
        raise SystemExit("--verify 用于无红线对拍；请勿与 --rl1/--rl2 同用")
    t0 = time.time()
    replay(sym, start, end, rl1=a.rl1, rl2=a.rl2, rl1_dd=a.rl1_dd,
           rl1_mode=a.rl1_mode, rl1_cool=a.rl1_cool,
           verify=a.verify, tag=a.tag)
    print(f"耗时 {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
