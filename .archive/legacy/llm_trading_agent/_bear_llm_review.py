# -*- coding: utf-8 -*-
"""熊市 LLM 决策复盘（零 API）：诊断 LLM 熊市止损拖累的具体诱因。

输入：两熊市窗 repro json（含逐根 decision+reasoning_summary）+ equity csv + 日志。
输出：① LLM 持仓期行为统计（开/持/平决策分布）；② 亏损回合止损路径（谁主动平 vs 引擎强平）；
③ reasoning 关键词模式（"等反弹/观望/企稳" vs "止损/离场"）；④ 与规则引擎止损时点的错位量。
"""
import json, os, re, sys, csv
from collections import Counter

HERE = r"D:\Coding\AI_stock_agent\llm_trading_agent\runs"
SYMS = {
    "SOL": ("SOLUSDT_2021-10-01_2022-12-31", "SOLUSDT"),
    "ETH": ("ETHUSDT_2021-10-01_2022-12-31", "ETHUSDT"),
}

def load_repro(tag):
    with open(os.path.join(HERE, f"verdict_llm_{tag}_repro.json"), encoding="utf-8") as f:
        return json.load(f)

def load_log(tag):
    p = os.path.join(HERE, f"verdict_llm_{tag}.log")
    if not os.path.exists(p):
        return ""
    return open(p, encoding="utf-8").read()

def load_equity(tag):
    """从主项目 backtest_equity.csv 读——但熊市窗的在 runs/verdict_llm_{tag}_equity.csv？"""
    for cand in (os.path.join(HERE, f"verdict_llm_{tag}_equity.csv"),
                 os.path.join(HERE, f"verdict_llm_{tag.replace('_report','')}_equity.csv")):
        if os.path.exists(cand):
            return cand
    return None

WAIT_PAT = re.compile(r"观望|等待|企稳|反弹|止跌|蓄势|回调|再观察|不动|暂缓|等等")
EXIT_PAT = re.compile(r"止损|离场|平仓|退出|清仓|规避|风险|破位|止盈")
OPEN_PAT = re.compile(r"开仓|买入|做多|建仓|BUY|加仓")

for sym, (tag, _) in SYMS.items():
    print("=" * 70)
    print(f"### {sym} 熊市窗 LLM 决策复盘（{tag}）")
    d = load_repro(tag)
    calls = d.get("llm_calls", [])
    log = load_log(tag)
    print(f"LLM 调用 {len(calls)} 根")
    if not calls:
        continue

    # ① 决策动作分布
    acts = Counter(c.get("action", "?") for c in calls)
    print("决策动作分布:", dict(acts))

    # ② 持仓中(有仓时段)行为：需要结合 equity 或日志状态。
    #    近似：用 equity csv 找仓位期；但 equity 若不存在，用 action=BUY/SELL 之间的 HOLD 段。
    # 直接统计连续决策序列中的理由模式
    hold_wait = 0; hold_exit = 0; open_cnt = 0
    wait_examples, exit_examples = [], []
    for c in calls:
        dec = c.get("decision", {})
        a = dec.get("action", "?")
        rs = dec.get("reasoning_summary", "") or ""
        if a == "HOLD":
            if WAIT_PAT.search(rs):
                hold_wait += 1
                if len(wait_examples) < 3: wait_examples.append((c["ts"], rs[:120]))
        if a in ("SELL", "CLOSE"):
            if EXIT_PAT.search(rs): hold_exit += 1
            if len(exit_examples) < 3: exit_examples.append((c["ts"], rs[:120]))
        if a in ("BUY", "OPEN"):
            open_cnt += 1

    total_hold = sum(1 for c in calls if c.get("decision", {}).get("action") == "HOLD")
    print(f"\nHOLD {total_hold} 次：其中理由含【观望/等反弹/企稳】类 {hold_wait} 次 ({hold_wait/max(total_hold,1)*100:.0f}%)")
    for ts, ex in wait_examples:
        print(f"   例 {ts}: {ex}")
    print(f"\n平仓/减仓决策中理由含【止损/离场】类 {hold_exit} 次；BUY/OPEN {open_cnt} 次")
    for ts, ex in exit_examples:
        print(f"   例 {ts}: {ex}")

    # ③ 从日志提取实际买卖与强平事件，重建回合级止损路径
    print("\n--- 日志关键事件（BUY/SELL/强平/止损）---")
    ev = re.findall(r"\[([0-9\-]+ [0-9:]+)\].{0,4}(BUY|SELL|CLOSE|强平|止损)[^|]{0,90}", log)
    for ts, kind, rest in ev[:30]:
        print(f"  {ts} {kind} {rest.strip()[:90]}")

# ④ 规则引擎对照（从两窗 rules 日志拿回合与止损点）
print("\n" + "=" * 70)
print("### 规则引擎止损时点对照（同一熊市窗）")
for sym, (tag, _) in SYMS.items():
    log = load_log(tag.replace("_report.md", "")) if False else ""
    rp = os.path.join(HERE, f"verdict_rules_{tag.replace('_report.md','')}.log")
    rp = os.path.join(HERE, f"verdict_rules_{tag}.log")
    if os.path.exists(rp):
        lg = open(rp, encoding="utf-8").read()
        ev = re.findall(r"\[([0-9\-]+ [0-9:]+)\].{0,4}(BUY|SELL|CLOSE|强平|止损)[^|]{0,90}", lg)
        print(f"\n{sym} 规则引擎事件 {len(ev)} 条，前 20：")
        for ts, kind, rest in ev[:20]:
            print(f"  {ts} {kind} {rest.strip()[:90]}")
    else:
        print(f"{sym}: {rp} 不存在")
