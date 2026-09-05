# -*- coding: utf-8 -*-
"""主升浪吃浪验证：REENTRY_GATE_MODE price(旧静态锚) vs trend(动态趋势锚)

背景：用户 1d 回测「止盈平仓后 95 天零交易、2024 主升浪一口没吃到」——
旧 _reentry_block 只看上次平仓价 ×(1+prem)，牛市里价格永远高于它 → 永久锁死。
改造后 trend 档：价格≥EMA50(趋势线)=顺势区放行回补，EMA 动态跟随天然自回归。

本脚本 = Task #40 真实数据验证：
  Phase 1（确定性，零 API）：SOLUSDT/1d 2024-08-01→2025-01-18 主升浪窗口，
    规则引擎下 price vs trend 同窗 A/B——若 trend 组在止盈后能再进场吃浪而
    price 组锁死，机制即被真实数据证实。
  Phase 2（真实 LLM）：trend 闸门 + TREND_FILTER_LEVEL=llm + 真 LLM，复现用户
    GUI 实际配置（.env 现已是 gate=trend + llm），确认整条链路吃到主升浪。
    用法：python _bt_reentry_sol.py llm   （不带参数只跑 Phase 1，控费）
"""
import logging
import shutil
import sys
import types
from pathlib import Path

from config import CFG  # 必须取单例实例（config 单例覆写坑：模块级覆写对 from config import CFG 的消费方无效）

# ---- 定向窗口：SOL 2024-10→2025-01 主升浪（150→270）----
CFG.SYMBOL = "SOLUSDT"
CFG.INTERVAL = "1d"
CFG.START_DATE = "2024-08-01"      # 提前 ~50 根供 EMA50 预热，实际回放从趋势线有效行开始
CFG.BACKTEST_END = "2025-01-18"
CFG.MAX_BACKTEST_BARS = 1000       # 该窗口仅 ~170 根，不会截断

BASE = Path(CFG.BASE_DIR)
OUT = BASE / "runs"
OUT.mkdir(exist_ok=True)

args = types.SimpleNamespace(mode="backtest", once=False, use_llm=False, plot=False)


def _fresh_log(tag: str):
    """每组独立日志文件，便于按组提取成交事件。"""
    for h in logging.root.handlers[:]:
        logging.root.removeHandler(h)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(message)s",
                        filename=str(OUT / f"bt_{tag}.log"),
                        encoding="utf-8", filemode="w", datefmt="%m-%d %H:%M")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def run_group(tag: str, gate: str, trend_level: str, use_llm: bool, note: str):
    CFG.REENTRY_GATE_MODE = gate
    CFG.TREND_FILTER_LEVEL = trend_level
    CFG.BACKTEST_USE_LLM = use_llm
    # 生效性自检：确认参数真的打到单例上（类属性默认值会掩盖覆写失败 → 白跑）
    eff = (CFG.SYMBOL, CFG.INTERVAL, CFG.START_DATE, CFG.BACKTEST_END,
           CFG.REENTRY_GATE_MODE, CFG.TREND_FILTER_LEVEL, CFG.BACKTEST_USE_LLM)
    want = ("SOLUSDT", "1d", "2024-08-01", "2025-01-18",
            gate, trend_level, use_llm)
    print(f"\n######## {tag}：{note} ########", flush=True)
    print(f"  生效参数 SYMBOL={eff[0]} {eff[1]} 区间 {eff[2]}→{eff[3]} | "
          f"gate={eff[4]} trend={eff[5]} BACKTEST_USE_LLM={eff[6]}", flush=True)
    assert eff == want, f"{tag} 参数覆写未生效: {eff} != {want} —— config 单例坑！"

    _fresh_log(tag)
    import main
    main.run_backtest(args)

    src_md = BASE / "backtest_report.md"
    src_csv = BASE / "backtest_equity.csv"
    for src, suffix in ((src_md, "_report.md"), (src_csv, "_equity.csv")):
        if src.exists():
            shutil.copy(src, OUT / f"bt_{tag}{suffix}")
    print(f"  -> runs/bt_{tag}_report.md / bt_{tag}_equity.csv 已存", flush=True)


if __name__ == "__main__":
    phase = sys.argv[1].lower() if len(sys.argv) > 1 else "engine"
    if phase == "engine":
        # Phase 1：确定性规则引擎 A/B（零 API），TREND_FILTER_LEVEL 固定 engine，
        # 唯一差异 = 再入场闸门模式。直击「止盈后能否再进场吃浪」机制。
        run_group("price", gate="price", trend_level="engine",
                  use_llm=False, note="旧静态价锚：平仓价×1.02 永久锁死（对照）")
        run_group("trend", gate="trend", trend_level="engine",
                  use_llm=False, note="动态趋势锚：站上EMA50顺势放行（修复后）")
        print("\nPhase 1 完成（规则引擎对照，零 API）。"
              "\n如需真实 LLM 链路确认（~100 次 API）：python _bt_reentry_sol.py llm")
    elif phase == "llm":
        # Phase 2：真实 LLM + trend 闸门 + 趋势判断权交 LLM（=用户 GUI 配置）
        run_group("llm_trend", gate="trend", trend_level="llm",
                  use_llm=True, note="真实LLM+动态趋势锚：全链路吃浪确认")
        print("\nPhase 2 完成。")
    else:
        print(f"未知参数 {phase}：无参=规则引擎A/B；llm=真实LLM链路确认")
        sys.exit(1)
