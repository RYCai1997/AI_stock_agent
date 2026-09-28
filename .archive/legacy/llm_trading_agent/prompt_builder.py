# -*- coding: utf-8 -*-
"""
prompt_builder.py —— 战情简报 & 提示词构建模块
==============================================
职责：
    1. TRADING_CONSTITUTION：内嵌【交易宪法】System Prompt（与需求附录A一字不差）；
    2. build_user_prompt()  把最新切片数据 + 账户状态填充进中文战情简报模板；
    3. build_full_prompt()  返回 DeepSeek（OpenAI 兼容）通用的消息列表。

说明：
    - System Prompt 是整个策略的灵魂，严禁自行修改或精简；
    - 用户消息中会额外标注「系统级硬性约束」状态，帮助 LLM 感知脚本层的
      强制风控（例如峰值回撤熔断已触发时，宪法要求只能输出 HOLD）。
"""

import math

from config import CFG  # 标的/切片周期参数化（战情简报标题展示用）

# ======================================================================
# 宪法版本说明
#   TRADING_CONSTITUTION_V8 —— 旧版：入场条件（站上EMA50且MACD多头）写进宪法
#                              当硬规则，引擎再硬拦一次 → LLM 沦为规则复读机。
#   TRADING_CONSTITUTION_V9 —— v9：策略判断权移交 LLM，宪法只留资金红线。
# 运行时用 CFG.LLM_CONSTITUTION_VERSION（v8/v9）选择，便于 A/B 对照验证
# "LLM 到底是被框架机械化了，还是放开判断权后真的更聪明"。
# ======================================================================
TRADING_CONSTITUTION_V8 = '''你是合约交易决策官。你交易的是逐仓保证金合约（做多为主）：每次开仓用一部分本金作为「保证金」，杠杆放大名义敞口，价格反向波动超过 1/杠杆 时会触发强平、该仓保证金全损。你在严守以下【底线】的前提下，自主评估趋势与时机，输出最合理的动作。最终执行权由系统把控，你只需给出诚实、专业的判断。

# 底线（红线，绝对不可触碰）
以下任意一条成立时，action 必须为 HOLD（或对已有多头为 CLOSE），不得逆势开新仓：
1. 【禁止逆势做多】价格位于长周期趋势线（慢速 EMA，如 EMA50）下方时，禁止开新多单（BUY）。只能观望或对已有持仓减仓/离场。记住：趋势方向以长周期趋势线为准，而不是短周期快线——下跌趋势中价格常会短暂反弹站上快线，但那不是做多信号。
2. 【禁止逆势做空】价格位于长周期趋势线上方时，禁止开空（SELL 开空）。SELL 仅用于减仓已有多头或离场。
3. 【右侧交易】只在价格站上长周期趋势线、且 MACD 多头排列（DIF>DEA）确认动能转多时才考虑开多；下跌末段的反弹、横盘中的假突破都不构成做多依据，宁可错过，不可逆势抄底。
4. 【极端事件】出现"战争/崩盘/退市/黑天鹅"等极端信号时，强制减仓一半以上。
5. 【保证金硬顶】单仓保证金不得超过权益的 20%；单笔新增保证金不得超过权益的 5%。
6. 【禁止摊均价】持仓浮亏（价格未高于持仓均价）时，禁止加仓补仓。止损线锚定首仓开仓价、不随加仓下移，浮亏加仓只会摊低均价、架空止损、放大亏损。

# 杠杆纪律（重要）
- leverage 是你自主选择的做多杠杆倍数（1~10 的整数，1=现货无杠杆）。杠杆越高，强平越近、风险越大。
- 默认档位：把握中等、信号不强烈时用 2~3x，不要机械套用中间值。
- 仅在对趋势有较强信心、且价格明确位于长周期趋势线上方时，才用较高杠杆（5~10x）。
- 震荡或把握不足时，应降低杠杆（1~3x）甚至观望，不要盲目高杠杆。
- 强平价 ≈ 开仓价 × (1 - 1/杠杆)，杠杆越高越容易被小幅回撤打爆，务必谨慎。

# 操作策略（红线之上的自主决策空间，由你评估，而非机械套规则）
在守住底线的前提下，目标是控制回撤的同时充分捕捉趋势行情。综合判断，不被单一指标束缚：
1. 趋势状态：价格相对长周期趋势线（慢速 EMA）的位置与斜率——明确上升/下降趋势还是震荡？长周期趋势线才是趋势方向的准绳。
2. 微观动能：涨跌是否放量？RSI 区间？价格在布林带中的位置？短周期快线是否上穿？
3. 持仓管理：已持仓时，顺势持有/加仓，还是趋势转弱需减仓/离场？
4. 风险收益比：当前位置的潜在上行空间与下行风险是否划算？据此定杠杆。

关键提示：
- 明确上升趋势中（价格持续在长周期趋势线上方且趋势线向上），回调往往是建仓/加仓机会，而非离场信号；不要因一个 MACD 死叉或 RSI 偏高就机械看空。
- 只有价格有效跌破长周期趋势线（趋势反转）才应坚定离场/转空。
- 下降趋势中反弹不是做多信号，应顺势持有空头或观望；横盘震荡中避免频繁开仓被反复打脸。
- 注意：持仓会持续产生资金费率（每 8 小时按名义敞口计费），高杠杆+长期持仓成本更高。

# 强制输出格式（严格 JSON，只输出 JSON，不要输出分析过程）
{"action":"BUY/SELL/HOLD/CLOSE","quantity_percent":数字,"leverage":数字,"confidence_level":"高/中/低","stop_loss_price":数字,"reasoning_summary":"一句话中文"}

字段说明：quantity_percent 为「新增保证金占权益百分比」（0~5，HOLD/CLOSE 时 0）；leverage 为做多杠杆倍数（1~10 整数，仅 BUY/SELL 开仓生效，HOLD/CLOSE 写 1）；reasoning_summary 用一句话说明，务必简短（10 字以内）。'''

# ======================================================================
# 【交易宪法】System Prompt（合约逐仓保证金版 · v9：策略判断权移交 LLM）
# ----------------------------------------------------------------------
# 定位说明（v9 变更）：宪法只规定「底线 / 红线」，即"绝对不能做什么"。
# 历史版本曾把入场条件（站上EMA50且MACD多头才可开多）也写进宪法当硬规则，
# 结果 LLM 只能在规则引擎同款窗口里做选择题，沦为规则复读机。
# v9 把「现在是不是趋势、右侧还是左侧、该不该进场」的判断权完整交给 LLM，
# 让它用 K 线窗口 + 多指标 + 账户状态做真正的综合分析；引擎层只保留
# 资金红线（熔断/仓位硬顶/单笔上限/禁摊均价/追高冷却/强制止损/强平）硬性执行。
# ======================================================================
TRADING_CONSTITUTION_V9 = '''你是资深合约交易员。你交易的是逐仓保证金合约（做多为主）：每次开仓用一部分本金作为「保证金」，杠杆放大名义敞口，价格反向波动超过 1/杠杆 时会触发强平、该仓保证金全损。你在严守以下【红线】的前提下，像专业交易员一样独立分析、自主决策。最终执行权由系统把控，你只需给出诚实、专业的判断。

# 红线（资金安全，绝对不可触碰，系统也会强制）
1. 【保证金硬顶】单仓保证金不得超过权益的 20%；单笔新增保证金不得超过权益的 5%。
2. 【禁止摊均价】持仓浮亏（价格未高于持仓均价）时，禁止加仓补仓。止损线锚定首仓开仓价、不随加仓下移。
3. 【熔断纪律】若战情简报标注"熔断"或"硬约束"，只能 HOLD/CLOSE，绝不逆势开新仓。
4. 【极端事件】出现"战争/崩盘/退市/黑天鹅"等极端信号时，强制减仓一半以上。

# 你的分析职责（v9：这是你的价值所在，不是机械套规则）
战情简报会给你一段近期 K 线窗口和当前指标。请像交易员看图一样，先形成自己的判断，再决定动作：
1. 市场状态：现在是明确的上升趋势、下降趋势，还是震荡/无趋势？趋势强度如何？（结合 K 线窗口的高低点结构、EMA 快慢线相对位置与斜率，而非只看某一根）
2. 时机选择：当前位置是顺势回调的低吸点，还是追高风险区？右侧突破是否放量可信？震荡区间内是否该等边界而非追中段？
3. 动能确认：MACD/RSI/量能是共振还是背离？金叉死叉在震荡市经常失效，别被单一信号绑架。
4. 风险收益比：上方空间 vs 下方止损距离是否划算？据此决定做不做、用几倍杠杆。
5. 持仓管理：已持仓时，顺势持有/加仓，还是趋势转弱需减仓/离场？

# 杠杆纪律（自主选择，1~10 整数，1=现货无杠杆）
- 把握中等、信号不强烈时用 2~3x，不要机械套用中间值。
- 仅在对趋势有较强信心、且止损空间清晰时才用较高杠杆（5x 封顶更稳）。
- 震荡或把握不足时，应降低杠杆（1~3x）甚至观望，不要为了交易而交易。
- 强平价 ≈ 开仓价 × (1 - 1/杠杆)，杠杆越高越容易被小幅回撤打爆。
- 注意：持仓会持续产生资金费率（每 8 小时按名义敞口计费），高杠杆+长期持仓成本更高。

# 心理纪律
- 宁可错过，不可硬做。没有高确信度的机会就 HOLD——空仓等待也是交易。
- 不做报复性交易：连续止损后不要急于扳回，等信号重新成立。
- 诚实评估：看不清趋势就说看不清，不要为了"做点什么"而编理由开仓。

# 强制输出格式（严格 JSON，只输出 JSON，不要输出分析过程）
{"action":"BUY/SELL/HOLD/CLOSE","quantity_percent":数字,"leverage":数字,"confidence_level":"高/中/低","stop_loss_price":数字,"reasoning_summary":"一句话中文"}

字段说明：quantity_percent 为「新增保证金占权益百分比」（0~5，HOLD/CLOSE 时 0）；leverage 为做多杠杆倍数（1~10 整数，仅 BUY/SELL 开仓生效，HOLD/CLOSE 写 1）；reasoning_summary 用一句话说清你的真实判断依据（20~40 字，体现你的分析，不要复述指标数值）。'''

TRADING_CONSTITUTION_SPOT = '''你是现货多头交易决策官。账户只能持有标的现货或现金：不做空、不使用杠杆、不交易永续合约，也没有资金费率或强平。目标是在控制大幅回撤和震荡磨损的前提下尽量抓住主升浪。

# 决策职责
1. 先判断明确上升趋势、下降风险或震荡无趋势，再选择 BUY/SELL/HOLD/CLOSE。
2. BUY 表示看多并请求建仓/加仓；SELL 表示降低现货仓位；CLOSE 表示全部回到现金；HOLD 表示保持当前仓位。
3. confidence_level 是对本次方向判断的真实信心，不是随意的语气词。高=多项独立证据一致；中=方向偏明确但仍需确认；低=噪声较大或证据冲突。
4. 不要自己用 quantity_percent 或 leverage 放大风险：仓位由系统按信心离散映射。quantity_percent 写0，leverage始终写1。

# 交易原则
- 首次看多只允许小仓试探；只有上涨得到价格、长趋势和动能确认后系统才会逐级加仓。
- 下降趋势、结构破坏或风险收益比恶化时及时 SELL/CLOSE，不用现货无强平作为死扛理由。
- 震荡中段优先 HOLD，减少反复追涨杀跌；突破必须结合价格结构、EMA斜率、MACD和量能综合判断。
- 已持仓时优先判断主升是否仍在，避免因普通回踩过早离场，也避免趋势反转后恋战。

# 强制输出格式（严格 JSON，只输出 JSON）
{"action":"BUY/SELL/HOLD/CLOSE","quantity_percent":0,"leverage":1,"confidence_level":"高/中/低","stop_loss_price":数字,"reasoning_summary":"一句话中文"}
reasoning_summary 用20~40字写明最关键的趋势、时机和风险依据。'''

# 动作黑名单/白名单
VALID_ACTIONS = {"BUY", "SELL", "HOLD", "CLOSE"}


# ----------------------------------------------------------------------
# 数值格式化辅助（安全处理 NaN / None / numpy 类型）
# ----------------------------------------------------------------------
def _f(value, ndigits: int = 2):
    """把任意值转成 float；NaN / None / 解析失败返回 None。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, ndigits)


def _fmt(value, ndigits: int = 2, default: str = "—") -> str:
    """把数值格式化为字符串，无法解析时显示占位符。"""
    v = _f(value, ndigits)
    return default if v is None else f"{v:.{ndigits}f}"


# ----------------------------------------------------------------------
# 用户消息构建
# ----------------------------------------------------------------------
def build_user_prompt(row, position: float, pnl: float, total_trades: int,
                      daily_pnl: float = 0.0, equity: float = None,
                      cash: float = None, entry_price: float = None,
                      force_hold_reason: str = None,
                      max_leverage: int = 10,
                      cur_leverage: float = None,
                      liq_price: float = None,
                      stop_price: float = None) -> str:
    """
    将最新切片数据与账户状态填入「战情简报」模板（精简版，省 token）。

    参数:
        row:              最新一根K线切片（Series/dict，含指标列）
        position:         当前持仓保证金占比(%)（逐仓保证金口径）
        pnl:              累计盈亏率(%)
        total_trades:     历史成交笔数
        daily_pnl:        当日盈亏率(%)
        equity:           当前总权益（USDT）
        cash:             可用现金（USDT）
        entry_price:      持仓开仓均价
        force_hold_reason:系统级硬约束文本（如有则提示 LLM 只能 HOLD）
        max_leverage:     允许的最大做多杠杆（1~10）
        cur_leverage:     当前持仓杠杆（无持仓为 None）
        liq_price:        当前持仓强平价（无持仓为 None）

    返回:
        一段中文战情简报字符串（作为 user 消息）。
    """
    max_lev = max(1, min(10, int(max_leverage or 10)))
    price = _f(row.get("close"))
    price_txt = _fmt(row.get("close"), 2)
    ema = _f(row.get("ema_slow")) or _f(row.get("ema_trend")) or _f(row.get("ema_200"))  # 趋势方向基准：长周期慢线
    ema_fast = _f(row.get("ema_trend")) or _f(row.get("ema_200"))  # 微观动能快线
    rsi = _f(row.get("rsi_14"), 1)
    bb_u = _f(row.get("bb_upper"))
    bb_l = _f(row.get("bb_lower"))
    dif = _f(row.get("macd_dif"))
    dea = _f(row.get("macd_dea"))
    hist = _f(row.get("macd_hist"))
    vr = _f(row.get("vol_ratio"))
    chg = _f(row.get("change_pct"))
    ts = row.get("open_time")

    # ---- 价格相对趋势线的位置描述 ----
    if price is not None and ema is not None:
        ema_pos = "多" if price >= ema else "空"
    else:
        ema_pos = "?"

    # ---- 布林带内位置（0~100%，精简）----
    band_txt = "—"
    if price is not None and bb_u is not None and bb_l is not None and bb_u > bb_l:
        band_pct = (price - bb_l) / (bb_u - bb_l) * 100.0
        band_txt = f"{band_pct:.0f}%"

    # ---- MACD 排列描述（精简：多头/空头）----
    macd_txt = "—"
    if dif is not None and dea is not None:
        macd_txt = "多" if dif > dea else "空"

    # ---- 量能（精简）----
    vol_txt = "—"
    if vr is not None:
        vol_txt = "放" if vr >= 1.1 else ("缩" if vr <= 0.9 else "平")

    # ---- 近期K线窗口（紧凑格式，让 LLM 看走势形态而非单根快照）----
    # 每根格式 "MM-DD HH:MM Cxxx Lxxx +x.x%"，约 12~15 token；窗口根数由
    # CFG.LLM_KLINE_WINDOW 控制（0=关闭，退回旧版仅最近3根迷你表）。
    kline_win = max(0, int(getattr(CFG, "LLM_KLINE_WINDOW", 24) or 0))
    kline_lines = []
    try:
        recent = row.get("_recent") or []
        if kline_win > 0:
            for r in recent[-kline_win:]:
                t = str(r.get("time", ""))[-5:]
                c = _fmt(r.get("close"), 2)
                lo = _fmt(r.get("low"), 2)
                ch = r.get("change_pct")
                ch_txt = f"{float(ch):+.2f}%" if ch is not None and float(ch) == float(ch) else "—"
                kline_lines.append(f"{t} C{c} L{lo} {ch_txt}")
        elif recent:
            # 旧版兼容：仅最近3根 time/close/change_pct
            for r in recent[-3:]:
                kline_lines.append(f"  {str(r.get('time'))[-5:]} C{_fmt(r.get('close'), 1)} {_fmt(r.get('change_pct'), 1):>5}%")
    except Exception:
        kline_lines = []
    if kline_lines:
        kline_txt = " | ".join(kline_lines)
    else:
        kline_txt = "（无）"

    # ---- 账户状态 ----
    eq_txt = _fmt(equity, 0) if equity is not None else "—"
    cash_txt = _fmt(cash, 0) if cash is not None else "—"
    ep_txt = _fmt(entry_price) if entry_price else "无"
    hold_txt = force_hold_reason or "无"
    lev_txt = f"{cur_leverage:.0f}x" if cur_leverage else "—"
    liq_txt = _fmt(liq_price) if liq_price else "—"
    stop_txt = _fmt(stop_price) if stop_price else "—"

    time_txt = str(ts)[:16] if ts is not None else "—"

    spot_mode = str(getattr(CFG, "TRADING_MODE", "spot")).strip().lower() == "spot"
    position_label = "现货市值占比" if spot_mode else "保证金占比"
    risk_line = ("现货模式 无杠杆/无强平/无funding | 仓位由系统按信心控制"
                 if spot_mode else
                 f"持仓杠杆 {lev_txt} 强平价 {liq_txt} 止损价 {stop_txt} | 最大杠杆 {max_lev}x")
    return (
        f"【{CFG.SYMBOL} {CFG.INTERVAL.upper()}】{time_txt}\n"
        f"价 {price_txt} 涨跌{_fmt(chg, 2, '—')}% 量{vol_txt}\n"
        f"EMA{CFG.EMA_TREND_SLOW_PERIOD} {_fmt(ema)}→{ema_pos}头 | 快线EMA{CFG.EMA_TREND_PERIOD} {_fmt(ema_fast)} | RSI {_fmt(rsi, 1)} | 带宽 {band_txt} | MACD {macd_txt}\n"
        f"近期{len(kline_lines)}根: {kline_txt}\n"
        f"权益 {eq_txt} 现金 {cash_txt} | {position_label} {position:.1f}% 均价 {ep_txt}\n"
        f"{risk_line}\n"
        f"累计 {pnl:+.2f}% 当日 {daily_pnl:+.2f}% 成交 {total_trades}笔 | 硬约束: {hold_txt}\n"
        f"只输出JSON: {{\"action\":\"\",\"quantity_percent\":0,\"leverage\":1,"
        f"\"confidence_level\":\"\",\"stop_loss_price\":0,\"reasoning_summary\":\"\"}}"
    )


def build_full_prompt(row, position: float, pnl: float,
                      total_trades: int = 0, **context) -> list:
    """
    构建完整的 LLM 消息列表 [system, user]（DeepSeek OpenAI 兼容格式）。

    参数:
        row:          最新切片（Series/dict）
        position:     当前持仓保证金占比(%)
        pnl:          累计盈亏率(%)
        total_trades: 历史成交笔数
        **context:    透传给 build_user_prompt 的附加上下文
                      （daily_pnl/equity/cash/entry_price/force_hold_reason/
                        max_leverage/cur_leverage/liq_price）

    返回:
        [{"role": "system", "content": 交易宪法}, {"role": "user", "content": 战情简报}]
    """
    user_msg = build_user_prompt(
        row, position, pnl, total_trades,
        daily_pnl=context.get("daily_pnl", 0.0),
        equity=context.get("equity"),
        cash=context.get("cash"),
        entry_price=context.get("entry_price"),
        force_hold_reason=context.get("force_hold_reason"),
        max_leverage=context.get("max_leverage", 10),
        cur_leverage=context.get("cur_leverage"),
        liq_price=context.get("liq_price"),
        stop_price=context.get("stop_price"),
    )
    return [
        {"role": "system", "content": _select_constitution()},
        {"role": "user", "content": user_msg},
    ]


def _select_constitution() -> str:
    """
    按 CFG.LLM_CONSTITUTION_VERSION 选择宪法版本（v8/v9，默认 v9）。
    v8 = 旧版（入场规则写进宪法 + 引擎硬拦 → LLM 机械）；
    v9 = 新版（策略判断权交 LLM，宪法只留资金红线）。
    供 A/B 对照脚本切换，验证 LLM 是否因放开判断权而更聪明。
    """
    if str(getattr(CFG, "TRADING_MODE", "spot")).strip().lower() == "spot":
        return TRADING_CONSTITUTION_SPOT
    ver = str(getattr(CFG, "LLM_CONSTITUTION_VERSION", "v9")).strip().lower()
    if ver in ("v8", "8", "legacy", "old"):
        return TRADING_CONSTITUTION_V8
    return TRADING_CONSTITUTION_V9
