# -*- coding: utf-8 -*-
"""Offline same-signal counterfactual: replay saved LLM decisions through fixed sizing."""
import json
import logging
import shutil
import statistics
import types
from pathlib import Path

import config as m
import main
import run_strategy_ab as ab


BASE = Path(m.CFG.BASE_DIR)
OUT = BASE / "runs"
SOURCE_PREFIX = "ab_spot_phase3_llm5_BTCUSDT_2023-01-01_2023-06-30_spot_confidence"
TARGET = 10.26


def main_run():
    m.CFG.SYMBOL = "BTCUSDT"
    m.CFG.INTERVAL = "1d"
    m.CFG.START_DATE = "2023-01-01"
    m.CFG.BACKTEST_END = "2023-06-30"
    m.CFG.BACKTEST_USE_LLM = False
    m.CFG.TRADING_MODE = "spot"
    m.CFG.SPOT_SIZING_MODE = "fixed"
    m.CFG.SPOT_FIXED_TARGET_PERCENT = TARGET
    main.setup_logging()
    logging.getLogger().setLevel(logging.WARNING)

    rows = []
    deltas = []
    for rep in range(1, 6):
        suffix = f"r{rep:02d}"
        src_repro = OUT / f"{SOURCE_PREFIX}_{suffix}_repro.json"
        src_report = OUT / f"{SOURCE_PREFIX}_{suffix}_report.md"
        data = json.loads(src_repro.read_text(encoding="utf-8"))
        decisions = {str(x["ts"])[:16]: dict(x["decision"])
                     for x in data.get("llm_calls", []) if isinstance(x.get("decision"), dict)}
        args = types.SimpleNamespace(
            mode="backtest", once=False, use_llm=False, plot=False, v10=None,
            decision_replay=decisions,
            decision_label="离线重放同一组LLM信号（零API）")
        main.run_backtest(args)

        stem = f"ab_spot_phase3_shadow_same_signal_BTCUSDT_2023-01-01_2023-06-30_fixed_{suffix}"
        for src, ending in [
            (BASE / "backtest_report.md", "_report.md"),
            (BASE / "backtest_repro.json", "_repro.json"),
            (BASE / "backtest_equity.csv", "_equity.csv"),
            (OUT / "curve_backtest.csv", "_curve.csv"),
        ]:
            shutil.copy2(src, OUT / f"{stem}{ending}")

        conf = ab.extract(src_report.read_text(encoding="utf-8"))
        fixed_text = (OUT / f"{stem}_report.md").read_text(encoding="utf-8")
        fixed = ab.extract(fixed_text)
        cr, fr = ab.metric_number(conf.get("总收益率")), ab.metric_number(fixed.get("总收益率"))
        cd, fd = ab.metric_number(conf.get("最大回撤")), ab.metric_number(fixed.get("最大回撤"))
        ca, fa = ab.metric_number(conf.get("平均名义敞口")), ab.metric_number(fixed.get("平均名义敞口"))
        deltas.append(cr - fr)
        rows.append(f"| {rep} | {cr:+.2f}% | {fr:+.2f}% | {cr-fr:+.2f}pp | "
                    f"{cd:.2f}% | {fd:.2f}% | {ca:.2f}% | {fa:.2f}% |")

    lines = [
        "# 同一LLM信号流：信心仓位 vs 固定仓位离线反事实", "",
        "- 信号来源：付费实验中每轮信心仓位腿已保存的原始LLM决策。",
        "- 反事实腿：不调用API，把完全相同时间戳的动作/信心送入10.26%固定仓位执行器。",
        "- 若固定腿状态导致源信号当时不存在，则该时点HOLD；不生成新信号。", "",
        "| 轮次 | 信心收益 | 同信号固定收益 | 收益差 | 信心DD | 固定DD | 信心平均敞口 | 固定平均敞口 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|", *rows, "",
        f"- 收益差均值/中位数/标准差：{statistics.mean(deltas):+.3f} / "
        f"{statistics.median(deltas):+.3f} / {statistics.pstdev(deltas):.3f} pp", "",
    ]
    path = OUT / "ab_spot_phase3_shadow_same_signal_summary.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"汇总已保存：{path}")


if __name__ == "__main__":
    main_run()
