# -*- coding: utf-8 -*-
"""等口径判决实验（#49）：LLM 是否带来增量？—— 三腿同窗对打

设计（backtest-ab-validation skill「判 LLM 增量」三步法）：
  尺子已修（账本守恒 + 成本 + next_open + 年化修正），冻结基线已立（baseline_trend）。
  本脚本把三条腿放在同一数据窗口、同成本、同成交假设下：
    L_BASELINE：机械冻结基线（EMA50+斜率+ATR 止损，风险2%定仓）——隔离目录自带账本
    L_RULES  ：主项目规则引擎（engine 档：规则决策 + 引擎宪法拦截）——零 API
    L_LLM    ：主项目真实 LLM（llm 档 = GUI 实盘配置）——逐根烧 API
  窗口 = 主项目规则腿实际回放区间（EMA50 预热自动跳过，读报告回放起点），
  保证三腿回放区间逐日一致。

用法：
  python verdict_llm_vs_baseline.py baseline   # 只跑冻结基线腿（零 API）
  python verdict_llm_vs_baseline.py rules      # 主项目规则引擎腿（零 API）
  python verdict_llm_vs_baseline.py llm        # 真 LLM 腿（烧 API，~120+/窗）
  python verdict_llm_vs_baseline.py all        # 全部（按 baseline→rules→llm 顺序）

参数化窗口（任意标的/区间，报告文件名带窗口标签，不覆盖 #49 默认存档）：
  python verdict_llm_vs_baseline.py {baseline|rules|llm} SYMBOL START END
  # 例：熊市复验 SOL_熊22（2022 全年）先跑规则腿拿回放起点：
  #   python verdict_llm_vs_baseline.py rules SOLUSDT 2021-11-01 2022-12-31
  # 窗口参数缺省 = #49 判决默认窗（SOLUSDT 2024-08-01 → 2025-01-18）。
"""
import os
import re
import shutil
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "baseline_trend"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "llm_trading_agent"))

# ---------------- 判决窗口（三腿共用） ----------------
# 主对比 = SOL 2024-08-01→2025-01-18 主升浪（_bt_reentry_sol 同窗，规则腿历史报告可对照）；
# START_DATE 提前供 EMA50 预热，实际回放起点以主项目 first_valid 为准（三腿取同一起点）。
SYMBOL = "SOLUSDT"
INTERVAL = "1d"
START_DATE = "2024-08-01"
BACKTEST_END = "2025-01-18"
RUN_SUFFIX = ""      # 参数化窗口时置为 _SYMBOL_START_END（报告/日志文件名带后缀，防覆盖 #49 存档）

from config import CFG  # noqa: E402  必须取单例实例


def _set_window():
    CFG.SYMBOL = SYMBOL
    CFG.INTERVAL = INTERVAL
    CFG.START_DATE = START_DATE
    CFG.BACKTEST_END = BACKTEST_END
    CFG.MAX_BACKTEST_BARS = 1000
    eff = (CFG.SYMBOL, CFG.INTERVAL, CFG.START_DATE, CFG.BACKTEST_END)
    assert eff == (SYMBOL, INTERVAL, START_DATE, BACKTEST_END), f"窗口覆写未生效: {eff}"


def run_baseline_leg(replay_start: str) -> dict:
    """冻结基线腿：与规则/LLM 腿相同回放区间（replay_start 来自主项目报告）。"""
    import baseline_risk_scan as brs
    d = brs.fetch_df(SYMBOL, START_DATE, BACKTEST_END)   # 预热区已含
    r = brs.run_one(d, replay_start, BACKTEST_END, risk=0.02, gate_pct=None)
    if "error" in r:
        raise RuntimeError(f"baseline 腿失败: {r['error']}")
    return {
        "leg": "L_BASELINE 冻结基线(risk2%)",
        "ret": r["ret"], "max_dd": r["max_dd"], "calmar": r["calmar"],
        "rounds": r["rounds"], "pf": r["pf"], "win_rate": r["win_rate"],
    }


def run_main_leg(tag: str, trend_level: str, use_llm: bool, note: str,
                 v10: dict | None = None) -> dict:
    """主项目腿（规则引擎 / 真 LLM）：跑 run_backtest，读报告指标。
    v10：引擎红线开关 dict（见 main.run_backtest），None=关闭。"""
    import logging
    import main
    _set_window()
    CFG.TREND_FILTER_LEVEL = trend_level
    CFG.BACKTEST_USE_LLM = use_llm
    CFG.REENTRY_GATE_MODE = "trend"
    eff = (CFG.TREND_FILTER_LEVEL, CFG.BACKTEST_USE_LLM)
    assert eff == (trend_level, use_llm), f"{tag} 参数覆写未生效: {eff}"
    if use_llm and v10:
        print(f"[v10 透传] {v10}", flush=True)

    # 独立日志文件
    for h in logging.root.handlers[:]:
        logging.root.removeHandler(h)
    OUT = CFG.BASE_DIR / "runs"
    OUT.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        filename=str(OUT / f"verdict_{tag}{RUN_SUFFIX}.log"),
                        encoding="utf-8", filemode="w")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    print(f"\n######## {tag}：{note} ########", flush=True)
    args = types.SimpleNamespace(mode="backtest", once=False,
                                 use_llm=use_llm, plot=False, v10=v10)
    main.run_backtest(args)

    # 读报告与权益曲线
    md = (CFG.BASE_DIR / "backtest_report.md").read_text(encoding="utf-8")
    def grab(pat, default=None):
        m = re.search(pat, md)
        return m.group(1) if m else default
    # 报告 markdown 表格行：| 总收益率 | x% | 等
    ret = grab(r"\| 总收益率 \| ([+\-0-9.]+)% \|")
    mdd = grab(r"\| 最大回撤 \| ([+\-0-9.]+)% \|")
    cal = grab(r"\| 收益/最大回撤 \| ([+\-0-9.∞]+) \|")
    trades = grab(r"\| 交易次数 \| (\d+)")
    wr = grab(r"\| 胜率 \| ([+\-0-9.]+)% \|")
    pf = grab(r"\| 盈利因子 \| ([+\-0-9.∞]+) \|")
    rng = grab(r"- 区间：(.+?) → (.+)")
    # 规范产物挪到 runs 存档
    for src, suffix in ((CFG.BASE_DIR / "backtest_report.md", "_report.md"),
                        (CFG.BASE_DIR / "backtest_equity.csv", "_equity.csv"),
                        (CFG.BASE_DIR / "backtest_repro.json", "_repro.json")):
        if src.exists():
            shutil.copy(src, OUT / f"verdict_{tag}{RUN_SUFFIX}{suffix}")
    replay_start = rng.strip() if rng else None
    return {
        "leg": f"L_{tag.upper()} 主项目{'真LLM' if use_llm else '规则引擎'}",
        "ret": float(ret) if ret else None,
        "max_dd": float(mdd) if mdd else None,
        "calmar": (float("inf") if cal and cal.strip() == "∞" else
                   float(cal) if cal else None),
        "rounds": int(trades) if trades else None,
        "pf": (float("inf") if pf and pf.strip() == "∞" else
               float(pf) if pf else None),
        "win_rate": float(wr) if wr else None,
        "replay_start": replay_start,
    }


def fmt_pf(p):
    return "∞" if p in (float("inf"), None) else f"{p:.2f}"


def main():
    argv = sys.argv[1:]
    mode = argv[0].lower() if argv else "all"
    global SYMBOL, START_DATE, BACKTEST_END, RUN_SUFFIX
    RUN_SUFFIX = ""
    if len(argv) >= 4:                       # mode SYMBOL START END → 参数化窗口
        SYMBOL, START_DATE, BACKTEST_END = argv[1].upper(), argv[2], argv[3]
        RUN_SUFFIX = f"_{SYMBOL}_{START_DATE}_{BACKTEST_END}"
    # v10 引擎红线开关（--v10-rl1 {breach|dd} [--v10-rl1-dd F] [--v10-rl1-cool N] [--v10-rl2]）
    rest = (argv[4:] if (len(argv) >= 4 and not argv[1].startswith("-"))
            else argv[1:])
    v10 = None
    if "--v10-rl1" in rest:
        _i = rest.index("--v10-rl1")
        _m = rest[_i + 1]
        assert _m in ("breach", "dd"), "--v10-rl1 须为 breach|dd"
        v10 = {"rl1": _m}
        for _flag, _key, _conv in (("--v10-rl1-dd", "rl1_dd", float),
                                   ("--v10-rl1-cool", "rl1_cool", int)):
            if _flag in rest:
                v10[_key] = _conv(rest[rest.index(_flag) + 1])
        v10["rl2"] = "--v10-rl2" in rest
    elif "--v10-rl2" in rest:
        # 纯 rl2（仅熊市降杠杆 cap 1x，无强平/无冷却）——独立可开关
        v10 = {"rl2": True}
    results = []
    replay_start = None

    # 1) 规则腿先跑（零 API）→ 拿实际回放起点，供基线/LLM 对齐
    if mode in ("rules", "all"):
        r = run_main_leg("rules", trend_level="engine", use_llm=False,
                         note="主项目规则引擎（engine 档，零 API）")
        replay_start = r.pop("replay_start")
        results.append(r)

    # 2) 基线腿（需 replay_start；若单独跑 baseline 则先用保守起点 = 报告历史值）
    if mode in ("baseline", "all"):
        if replay_start is None:
            # 单独跑 baseline 时无法得知主项目实际起点 → 用上一轮报告（若有）
            prev = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "runs", f"verdict_rules{RUN_SUFFIX}_report.md")
            if os.path.exists(prev):
                m = re.search(r"- 区间：(.+?) →", open(prev, encoding="utf-8").read())
                replay_start = m.group(1).strip() if m else None
        if replay_start is None:
            replay_start = START_DATE      # 兜底：用窗口起点（EMA50 预热自动跳过无效段）
        results.append(run_baseline_leg(replay_start))

    # 3) LLM 腿（烧 API，须显式）。带 v10 红线时按配置指纹分 tag，产物独立不互相覆盖：
    #    rl2-only（纯熊市降杠杆）→ llm_v10_rl2；含 rl1（结构止损/冷却）→ llm_v10
    if mode == "llm":
        _tag = "llm"
        if v10:
            _tag = "llm_v10" if v10.get("rl1") else "llm_v10_rl2"
        r = run_main_leg(_tag, trend_level="llm", use_llm=True, v10=v10,
                         note="真实 LLM + llm 档（GUI 实盘配置，逐根烧 API）"
                              + (" + v10引擎红线 " + str(v10) if v10 else ""))
        replay_start = r.pop("replay_start")
        results.append(r)

    if not results:
        print(__doc__)
        return 1

    # ---- 汇总 ----
    def _s(v, fmt="{:.2f}", suffix=""):
        return "" if v is None else fmt.format(v) + suffix

    print("\n" + "=" * 62)
    print(f"判决窗口：{SYMBOL} {INTERVAL.upper()} 回放 {replay_start or '?'} → {BACKTEST_END}"
          "（EMA50 预热已跳过，三腿同起点同终点）")
    print(f"{'腿':<34}{'收益':>8}{'最大回撤':>9}{'Calmar':>8}{'回合':>6}{'胜率':>7}{'PF':>6}")
    print("-" * 62)
    for r in results:
        print(f"{r['leg']:<34}"
              f"{_s(r['ret'], '{:+.2f}', '%'):>8}"
              f"{_s(r['max_dd'], '{:.2f}', '%'):>9}"
              f"{_s(r['calmar'], '{:.2f}'):>8}"
              f"{_s(r['rounds'], '{:d}'):>6}"
              f"{_s(r['win_rate'], '{:.0f}', '%'):>7}"
              f"{fmt_pf(r.get('pf')):>6}")
    print("=" * 62)
    # 写判决文档：读旧表 → 本轮的腿覆盖同名 → 未跑的腿保留（防单腿模式覆盖丢行）
    here = os.path.dirname(os.path.abspath(__file__))
    report_name = f"verdict_report{RUN_SUFFIX}.md"
    report_path = os.path.join(here, "runs", report_name)
    merged = {}                                   # leg → 表格行文本
    if os.path.exists(report_path):
        for ln in open(report_path, encoding="utf-8"):
            if ln.startswith("| L_"):
                leg = ln.split("|")[1].strip()
                merged[leg] = ln.rstrip("\n")
    for r in results:                             # 本轮结果覆盖同名
        merged[r["leg"]] = (
            f"| {r['leg']} | {_s(r['ret'], '{:+.2f}', '%')} | "
            f"{_s(r['max_dd'], '{:.2f}', '%')} | "
            f"{_s(r['calmar'], '{:.2f}')} | "
            f"{_s(r['rounds'], '{:d}')} | {_s(r['win_rate'], '{:.0f}', '%')} | "
            f"{fmt_pf(r.get('pf'))} |")

    order = {"L_BASELINE": 0, "L_RULES": 1, "L_LLM": 2}
    def _key(leg):
        return order.get(leg.split()[0], 9)

    # ---- 解读节：由已存行数值自动生成（窗口无关，重跑不丢）----
    def _row_metrics(row_text):
        cells = [c.strip() for c in row_text.strip().strip("|").split("|")]
        # cells: [leg, ret, mdd, calmar, rounds, winrate, pf]
        def _f(x):
            try:
                return float(x.rstrip("%"))
            except (ValueError, AttributeError):
                return None
        return {"leg": cells[0], "ret": _f(cells[1]), "mdd": _f(cells[2]),
                "calmar": _f(cells[3]), "rounds": int(cells[4]) if cells[4].isdigit() else None,
                "pf": _f(cells[6])}

    conclusion = []
    met = {k.split()[0]: _row_metrics(v) for k, v in merged.items()}
    if set(met) >= {"L_BASELINE", "L_RULES", "L_LLM"}:
        def _mdd(k):                       # max_dd 为负值：越接近 0 = 回撤越小
            return met[k]["mdd"] if met[k]["mdd"] is not None else -9.0
        def _cal(k): return met[k]["calmar"] if met[k]["calmar"] is not None else -9.0
        def _pf(k):  return met[k]["pf"] if met[k]["pf"] is not None else -9.0
        def _ret(k): return met[k]["ret"] if met[k]["ret"] is not None else -9.0
        best_cal = max(met, key=_cal)
        best_pf = max(met, key=_pf)
        best_ret = max(met, key=_ret)
        min_dd_leg = max(met, key=_mdd)    # 回撤最小（最接近 0）
        max_dd_leg = min(met, key=_mdd)    # 回撤最大（最负）
        # 高换手/低质量腿：真实「回合最多」与「PF 最低」的腿（可能不是 L_RULES，动态判定）
        most_rounds = max(met, key=lambda k: met[k]["rounds"] or -1)
        worst_pf = min(met, key=_pf)
        positive_legs = [k for k in met if _ret(k) > 0]
        if positive_legs:
            best_cal = max(positive_legs, key=_cal)
            calmar_line = (
                f"- **{met[best_cal]['leg']}** 在正收益方案中 Calmar "
                f"{met[best_cal]['calmar']:.2f} 最高；回撤最小者为 "
                f"**{met[min_dd_leg]['leg']}**（{met[min_dd_leg]['mdd']:.2f}%）。")
        else:
            calmar_line = (
                f"- 本窗口所有方案均为负收益，**不使用负 Calmar 排名**；"
                f"收益最高为 **{met[best_ret]['leg']}**（{met[best_ret]['ret']:+.2f}%），"
                f"回撤最小为 **{met[min_dd_leg]['leg']}**（{met[min_dd_leg]['mdd']:.2f}%）。")
        trade_wear_line = ""
        if most_rounds == worst_pf:
            trade_wear_line = (f"- **{met[most_rounds]['leg']}** 回合 {met[most_rounds]['rounds']} 最多、"
                               f"PF {met[most_rounds]['pf']:.2f} 三腿最低 → 高换手磨损，"
                               f"盈亏质量垫底。")
        else:
            trade_wear_line = (f"- **{met[most_rounds]['leg']}** 回合 {met[most_rounds]['rounds']} 最多；"
                               f"PF 最低为 **{met[worst_pf]['leg']}**（{met[worst_pf]['pf']:.2f}）→ "
                               f"高换手/低质量未集中在同一腿，逐腿解读。")
        conclusion += [
            "", f"## 判决解读{RUN_SUFFIX or '（#49 闭环）'}", "",
            "**目标函数 = 肥尾 + 有界回撤：Calmar(收益/最大回撤) 与 PF 优先于胜率/Sharpe。**",
            "",
            calmar_line,
            f"- **{met[best_pf]['leg']}** PF {met[best_pf]['pf']:.2f} 最高 → 盈亏质量最优"
            + ("，与正收益 Calmar 最优腿一致。" if positive_legs and best_pf == best_cal else "。"),
            f"- **{met[best_ret]['leg']}** 收益 {met[best_ret]['ret']:+.2f}% 最高（绝对收益视角）"
            + (f"，但回撤 {met[best_ret]['mdd']:.2f}% 亦最大 → 高收益由高回撤换来，"
               "需结合回撤预算判断。"
               if best_ret == max_dd_leg else "。"),
            trade_wear_line,
            "",
            "> ⚠️ 样本警告：单窗口、单次 LLM 运行只作探索；API 调用量约等于实际回放根数。"
            "下一步应对每条腿重复运行并报告离散度，不能用单次负 Calmar 判冠军。",
        ]
    else:
        missing = [{"L_BASELINE": "冻结基线", "L_RULES": "规则引擎", "L_LLM": "真LLM"}[k]
                   for k in ("L_BASELINE", "L_RULES", "L_LLM") if k not in met]
        conclusion += ["", f"> 提示：已存 {len(met)}/3 腿（缺 {'、'.join(missing)}）。"
                           "跑对应模式补齐后自动生成解读：`python verdict_llm_vs_baseline.py {baseline|rules|llm}`"]

    lines = [
        f"# LLM 增量判决实验（#49）{RUN_SUFFIX or ''}",
        "",
        f"- 窗口：{SYMBOL} {INTERVAL.upper()}，回放 {replay_start or '?'} → {BACKTEST_END}",
        "- 口径：三腿同区间/同成本(taker0.04%+滑点2bp)/同 next_open/同账本修复语义",
        "- 冻结基线（隔离目录）风险2%定仓 ≤3x 名义；主项目保证金≤20% 杠杆自主。",
        "",
        "| 腿 | 收益 | 最大回撤 | Calmar | 回合 | 胜率 | PF |",
        "|---|---|---|---|---|---|---|",
    ]
    lines += [merged[k] for k in sorted(merged, key=_key)]
    lines += conclusion + [""]
    body = "\n".join(lines)
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "runs", f"verdict_report{RUN_SUFFIX}.md"), "w",
              encoding="utf-8") as f:
        f.write(body)
    print(f"\n判决文档已写 runs/verdict_report{RUN_SUFFIX}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
