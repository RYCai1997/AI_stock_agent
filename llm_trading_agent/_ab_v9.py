# -*- coding: utf-8 -*-
"""A/B 实测：v8 机械版 vs v9 自主版（真实 LLM，同区间对照）。

回答用户问题「LLM 是否被框架机械化？放开判断权后是否更聪明」：
  - A 组（v8/engine，旧行为基线）：宪法把入场条件写成硬规则 + 引擎 EMA50/MACD
    硬拦 + 无 K 线窗口（每根只看单行快照）——即用户当前"套话复读机"状态。
  - B 组（v9/llm，新行为）：宪法只留资金红线 + 引擎不拦趋势 + 24 根 K 线窗口，
    LLM 像交易员一样看图自主判断趋势/时机。

同区间、同 LLM 模型、同本金；唯一差异 = 决策自由度。窗口 ~240 根 30m 控成本。
日志落盘 runs/_ab_v9.log；报告存 runs/ab_v8_report.md 与 runs/ab_v9_report.md。
"""
import logging
import shutil
import types
from pathlib import Path

from config import CFG  # 必须取单例实例：参数都是 Config 类属性，模块级覆写对 from config import CFG 的消费方无效（项目经典坑）

# ---- 与用户 GUI 实测相同的标的/区间（窗口截最近 240 根控成本）----
CFG.SYMBOL = "SOLUSDT"
CFG.INTERVAL = "30m"
CFG.START_DATE = "2026-05-01"      # 与用户 GUI 相同起点
CFG.BACKTEST_END = "2026-08-01"    # 与用户 GUI 相同终点
CFG.MAX_BACKTEST_BARS = 240        # 截最近 240 根 ≈ 5 天 30m（每根一次 API，控费）
CFG.BACKTEST_USE_LLM = True        # 真实 LLM
print(f"SYMBOL={CFG.SYMBOL} {CFG.INTERVAL} 窗口=最近{CFG.MAX_BACKTEST_BARS}根  BACKTEST_USE_LLM={CFG.BACKTEST_USE_LLM}", flush=True)

import main

LOG = Path(CFG.BASE_DIR) / "runs" / "_ab_v9.log"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    filename=str(LOG), encoding="utf-8", filemode="w",
                    datefmt="%H:%M:%S")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

BASE = Path(CFG.BASE_DIR)
OUT = BASE / "runs"
OUT.mkdir(exist_ok=True)
args = types.SimpleNamespace(mode="backtest", once=False, use_llm=True, plot=False)

GROUPS = [
    # (组名, 宪法版本, 趋势过滤, K线窗口, 说明)
    ("v8",  "v8", "engine", 0,  "旧：入场规则硬编码+引擎EMA/MACD硬拦+单根快照"),
    ("v9",  "v9", "llm",    24, "新：判断权交LLM+24根K线窗口（红线仍引擎硬性）"),
]

for tag, const_ver, trend_level, kwin, note in GROUPS:
    print(f"\n######## {tag} 组：{note} ########", flush=True)
    CFG.LLM_CONSTITUTION_VERSION = const_ver
    CFG.TREND_FILTER_LEVEL = trend_level
    CFG.LLM_KLINE_WINDOW = kwin
    # 生效性自检：确认参数真的打到了单例上（类属性默认值会掩盖覆写失败，白烧 API）
    eff = (CFG.LLM_CONSTITUTION_VERSION, CFG.TREND_FILTER_LEVEL, CFG.LLM_KLINE_WINDOW)
    want = (const_ver, trend_level, kwin)
    print(f"  生效参数 const={eff[0]} trend={eff[1]} kwin={eff[2]}", flush=True)
    assert eff == want, f"{tag} 参数覆写未生效: {eff} != {want} —— config 单例坑！"
    main.run_backtest(args)
    src = BASE / "backtest_report.md"
    if src.exists():
        shutil.copy(src, OUT / f"ab_{tag}_report.md")
        print(f"  -> runs/ab_{tag}_report.md 已存", flush=True)
    else:
        print(f"  !! 报告缺失（{tag}）", flush=True)

print(f"\n完整 INFO 日志：{LOG}", flush=True)
