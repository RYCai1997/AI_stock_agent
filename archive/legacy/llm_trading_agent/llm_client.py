# -*- coding: utf-8 -*-
"""
llm_client.py —— LLM API 调用封装模块
=====================================
职责：
    1. call_llm()           调用 DeepSeek Chat Completions 接口（OpenAI 兼容协议，
                            直接用 requests 裸调 REST 端点，无需安装 openai SDK）；
    2. 温度固定 0.1、max_tokens 300，返回解析后的标准决策字典；
    3. 防御性编程：JSON 解析失败 / API 异常 → 一律兜底为 HOLD，绝不中断程序；
    4. rule_based_decision()  内置「宪法机械执行引擎」：当未配置 API 密钥或
       回测未开启真实 LLM 时，用确定性规则顶替 LLM，保证整条流水线可离线跑通。

决策字典统一结构:
    {"action": str,              # BUY / SELL / HOLD / CLOSE
     "quantity_percent": float,  # 建议保证金占权益百分比（0-20）
     "leverage": int,            # 建议杠杆倍数（1~10，LLM 自主选择，仅 BUY/SELL 开仓生效）
     "confidence_level": str,    # 高 / 中 / 低
     "stop_loss_price": float,   # 建议止损价（可能为 None）
     "reasoning_summary": str}   # 决策理由（中文，精简）
"""

import json
import logging
import re
import time

from config import CFG

logger = logging.getLogger(__name__)

VALID_ACTIONS = {"BUY", "SELL", "HOLD", "CLOSE"}
VALID_CONFIDENCE = {"高", "中", "低"}


# ----------------------------------------------------------------------
# 防御性默认值 / JSON 解析
# ----------------------------------------------------------------------
def default_decision(reason: str = "未知原因") -> dict:
    """构造一个安全的 HOLD 决策（LLM 抽风/失败时的兜底）。"""
    return {
        "action": "HOLD",
        "quantity_percent": 0.0,
        "leverage": 1,
        "confidence_level": "低",
        "stop_loss_price": None,
        "reasoning_summary": f"防御性HOLD：{reason}",
    }


def _extract_json_obj(text: str):
    """
    从 LLM 输出中稳健提取第一个完整 JSON 对象。

    兼容情况：
    - markdown 代码块 ```json ... ```
    - 解释性文字前后缀
    - 模型「话痨」输出多个重复 JSON 对象（截断重发导致）——只取第一个，
      避免 rfind('}') 抓到最后一个、中间夹两个对象导致 json.loads 失败。

    返回: dict 或 None（找不到/解析失败）。
    """
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    start = cleaned.find("{")
    if start == -1:
        return None
    # 用括号配对扫描，取「第一个」完整平衡的 JSON 对象
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = cleaned[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    # 第一个对象解析失败，尝试去掉内部换行再解析
                    try:
                        return json.loads(re.sub(r"[\r\n]+", " ", candidate))
                    except json.JSONDecodeError:
                        return None
    return None


def _parse_decision(content: str, max_position_pct: float = None,
                    max_leverage: int = None) -> dict:
    """
    把 LLM 返回的原始文本解析为结构化决策字典。

    任何字段缺失/非法都会就地降级（action 非法 → HOLD，百分比越界 → 截断），
    解析彻底失败则抛出异常，由 call_llm 兜底为 HOLD。
    """
    raw = _extract_json_obj(content)
    if raw is None:
        raise ValueError(f"LLM输出中未找到合法JSON: {content[:200]!r}")

    # ---- action：白名单校验 ----
    action = str(raw.get("action", "HOLD")).strip().upper()
    if action not in VALID_ACTIONS:
        logger.warning("[LLM] 非法action=%r，降级为HOLD", raw.get("action"))
        action = "HOLD"

    # ---- quantity_percent：0~上限 硬截断（上限 = 保证金占权益%，缺省 20）----
    cap = float(max_position_pct) if max_position_pct is not None else float(CFG.MAX_POSITION_PERCENT)
    try:
        qty = float(raw.get("quantity_percent", 0))
    except (TypeError, ValueError):
        qty = 0.0
    qty = max(0.0, min(qty, cap))
    if action in ("HOLD", "CLOSE"):
        qty = 0.0

    # ---- leverage：LLM 自主选择的杠杆倍数，收敛到 [1, max_leverage] ----
    max_lev = int(max_leverage) if max_leverage is not None else int(getattr(CFG, "MAX_LEVERAGE", 10))
    try:
        lev = float(raw.get("leverage", 1))
    except (TypeError, ValueError):
        lev = 1.0
    if lev <= 0 or lev != lev:  # NaN/非法 → 1x
        lev = 1.0
    leverage = max(1, min(max_lev, int(round(lev))))
    if action in ("HOLD", "CLOSE"):
        leverage = 1

    # ---- confidence_level 归一化 ----
    conf = str(raw.get("confidence_level", "低")).strip()
    conf = conf if conf in VALID_CONFIDENCE else "低"

    # ---- stop_loss_price：非法则置 None ----
    sl = raw.get("stop_loss_price")
    try:
        sl = float(sl)
        if sl <= 0:
            sl = None
    except (TypeError, ValueError):
        sl = None

    reason = str(raw.get("reasoning_summary", "")).strip() or "（LLM未给出理由）"

    return {
        "action": action,
        "quantity_percent": round(qty, 2),
        "leverage": leverage,
        "confidence_level": conf,
        "stop_loss_price": sl,
        "reasoning_summary": reason[:200],
    }


# ----------------------------------------------------------------------
# 主入口：调用 LLM
# ----------------------------------------------------------------------
def call_llm(messages: list, provider: str = None, max_position_pct: float = None,
             max_leverage: int = None, capture: dict | None = None) -> dict:
    """
    根据 provider 调用对应 LLM API，返回解析后的决策字典。

    参数:
        messages: [{"role": "system"|"user", "content": ...}, ...]
        provider: 预留参数；当前仅支持 "deepseek"（缺省读 CFG.LLM_PROVIDER）
        max_position_pct: 保证金占权益百分比硬上限；None 时取 CFG.MAX_POSITION_PERCENT
        max_leverage:    允许的最大杠杆倍数；None 时取 CFG.MAX_LEVERAGE
        capture: 可选 dict（可复现快照用，Codex #5）——非 None 时把每次尝试的
                 原始响应 content/错误 与最终解析 decision 写进 capture["attempts"]/
                 capture["decision"]，供回测落盘（prompt sha1 由调用方记录）。

    返回:
        结构化决策字典（详见模块 docstring）。任何失败 → 防御性 HOLD。
    """
    provider = (provider or CFG.LLM_PROVIDER).strip().lower()
    if provider != "deepseek":
        logger.warning("[LLM] 未知 provider=%r，仍按 OpenAI 兼容协议调用", provider)

    # ---- 密钥检查：缺失则不发起请求，直接规则引擎兜底 ----
    if not CFG.has_llm_key():
        logger.warning("[LLM] 未检测到 API 密钥，本次决策使用内置规则引擎（非LLM）")
        return default_decision("未配置API密钥，已回退到规则引擎")

    last_err: Exception | None = None

    for attempt in range(1, CFG.MAX_API_RETRY + 1):
        try:
            content = _call_deepseek(messages)

            if not content or not content.strip():
                raise ValueError("LLM返回了空内容")
            decision = _parse_decision(content, max_position_pct=max_position_pct,
                                       max_leverage=max_leverage)
            if capture is not None:
                capture.setdefault("attempts", []).append({"ok": True, "content": content})
                capture["decision"] = decision
            return decision

        except Exception as e:  # noqa: BLE001 —— 任何异常都不允许击穿主流程
            last_err = e
            if capture is not None:
                capture.setdefault("attempts", []).append(
                    {"ok": False, "error": f"{type(e).__name__}: {e}"})
            logger.warning("[LLM] 第 %d/%d 次调用失败: %s", attempt, CFG.MAX_API_RETRY, e)
            if attempt < CFG.MAX_API_RETRY:
                time.sleep(2 ** attempt)   # 指数退避：2s, 4s

    logger.error("[LLM] 重试 %d 次后仍失败(%s)，降级为防御性HOLD", CFG.MAX_API_RETRY, last_err)
    return default_decision(f"API调用失败: {type(last_err).__name__}")


def _call_deepseek(messages: list) -> str:
    """
    调用 OpenAI 兼容的 Chat Completions 接口，返回原始文本。

    端点和密钥均由用户在 GUI/.env 指定（CFG.LLM_BASE_URL / CFG.LLM_API_KEY），
    模型名取 CFG.llm_model。任何 OpenAI 兼容服务（DeepSeek / 通义 / 本地 Ollama /
    自定义网关等）只要走 /chat/completions 协议即可直接使用，无需装 SDK。
    """
    import requests

    base = (CFG.LLM_BASE_URL or "https://api.deepseek.com/v1").rstrip("/")
    payload = {
        "model": CFG.llm_model,
        "messages": messages,          # system/user/assistant 角色原生支持
        "temperature": CFG.LLM_TEMPERATURE,
        "max_tokens": CFG.LLM_MAX_TOKENS,
        "stream": False,
    }
    # 推理模型默认开启思考，思考过程会消耗 max_tokens 预算导致最终 JSON 被截断/为空。
    # 显式关闭思考，把预算全部留给最终 JSON 输出。但「关闭思考」的字段名因服务商而异：
    #   - DeepSeek 官方： payload["thinking"] = {"type": "disabled"}
    #   - 硅基流动(SiliconFlow)： payload["enable_thinking"] = False
    # 两者互不通用（未知字段会被静默忽略，导致思考仍开启、输出被截断），故按 provider 区分。
    if CFG.LLM_PROVIDER in ("siliconflow", "silicon"):
        payload["enable_thinking"] = False
    else:
        payload["thinking"] = {"type": "disabled"}
    resp = requests.post(
        base + "/chat/completions",
        headers={
            "Authorization": "Bearer " + CFG.LLM_API_KEY,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=CFG.LLM_TIMEOUT,
    )
    resp.raise_for_status()                # 非 2xx → HTTPError，由 call_llm 重试/兜底
    data = resp.json()
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise ValueError(f"LLM 返回结构异常: {str(data)[:200]}")
    # 优先取正式答案 content；若为空（思考耗尽了预算），回退到 reasoning_content
    content = (msg.get("content") or "").strip()
    if content:
        return content
    reasoning = (msg.get("reasoning_content") or "").strip()
    if reasoning:
        return reasoning
    raise ValueError("LLM返回了空内容")


# ----------------------------------------------------------------------
# 内置规则引擎（宪法机械执行）
# ----------------------------------------------------------------------
def _num(value):
    """把 pandas/numpy 数值安全转成 float；NaN 返回 None。"""
    import math
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or math.isinf(v)) else v


def rule_based_decision(row, snapshot: dict) -> dict:
    """
    宪法机械执行引擎（无 LLM 时的确定性兜底，也可用于快速验证流水线）。

    规则（全部只依赖切片数据，无未来函数）：
        1. 熔断检查：当日亏损 / 峰值回撤超 -2% → HOLD；
        2. 长周期趋势线（EMA_SLOW，默认50）定方向（对称宪法，默认 ALLOW_SHORT=false 只走多头半边）：
           - 价格 < 长周期趋势线（空头区域）：持多则 CLOSE 离场；
             持空则持有（空头趋势延续，直至价格收复长周期趋势线或触发止损，
             不在超卖/金叉时回补，避免下跌途中被小反弹反复打出 whipsaw）；
             空仓时——若 ALLOW_SHORT=true 且 MACD 死叉/RSI 偏弱（RSI≤28
             超卖贴地不追空）→ SELL 开空 5%；否则 HOLD（禁止做多）。
           - 价格 > 长周期趋势线（多头区域）：持空则 CLOSE 离场（空单逆势）；空仓/持多
             按多头趋势跟随执行（见 3）。
        3. 多头区域规则（ALLOW_SHORT=false 时的完整行为）：
           - 空仓 + MACD多头排列(DIF>DEA) + RSI∈[30,68] → 试探性 BUY（5%）；
           - 持仓 + RSI≥72（过热）→ SELL 分批止盈 5%；
           - 持仓 + MACD死叉绿柱且RSI>58（动能转弱）→ SELL 减仓 3%；
           - 持仓 + 多头排列延续且仓位<15% → BUY 顺势加仓 5%；
        4. 其余情况一律 HOLD（宁可不赚，不可大亏）。

    兼容性保证：ALLOW_SHORT=false（默认）时本函数产生的决策序列
    与旧版（纯现货做多）完全一致——做空分支全部不可达。

    参数:
        row:      最新K线切片（含指标列）
        snapshot: 账户快照 dict，需含 position_pct / pnl_pct / daily_pnl_pct /
                  side("long"/"short"/"flat") 与 btc（side 缺失时按 btc 符号推断）

    返回: 与 LLM 相同的决策字典结构。
    """
    price = _num(row.get("close"))
    ema = _num(row.get("ema_slow")) or _num(row.get("ema_trend")) or _num(row.get("ema_200"))  # 趋势方向判官：优先长周期慢线 ema_slow
    rsi = _num(row.get("rsi_14"))
    hist = _num(row.get("macd_hist"))
    dif = _num(row.get("macd_dif"))
    dea = _num(row.get("macd_dea"))
    vol_ratio = _num(row.get("vol_ratio"))

    pos = snapshot.get("position_pct", 0.0) or 0.0
    pnl = snapshot.get("pnl_pct", 0.0) or 0.0
    daily = snapshot.get("daily_pnl_pct", 0.0) or 0.0
    dd = snapshot.get("drawdown_pct", 0.0) or 0.0

    # ---- 持仓方向判定（优先显式 side，缺失时按 btc 符号回退）----
    side = str(snapshot.get("side", "flat")).strip().lower()
    if side not in ("long", "short", "flat"):
        btc = snapshot.get("btc")
        if btc is not None:
            btc = _num(btc)
            side = "short" if (btc or 0) < 0 else ("long" if (btc or 0) > 0 else "flat")
        else:
            side = "long" if pos > 0.001 else "flat"   # 兼容无 side 信息的旧快照
    short = (side == "short")

    # ---- 熔断（宪法第1条 + 账户级双重熔断）----
    if daily <= -CFG.DAILY_LOSS_LIMIT_PERCENT:
        return _hold(f"当日亏损{daily:.2f}%已达熔断线")
    if dd <= -CFG.OVERALL_LOSS_LIMIT_PERCENT:
        return _hold(f"峰值回撤{dd:.2f}%已达熔断线")

    if price is None or ema is None:
        return _hold("技术指标数据不足（预热期）")

    bull = (dif is not None and dea is not None and dif > dea)   # MACD 多头排列
    bear = (dif is not None and dea is not None and dif < dea)   # MACD 空头排列
    overheat = rsi is not None and rsi >= 72                     # 多头过热
    oversold = rsi is not None and rsi <= 28                     # 空头超卖（反弹风险）

    # ==================== 空头区域（价格 < 长周期趋势线） ====================
    if price < ema:
        # 持多：趋势转空，按宪法离场（与旧版一致）
        if side == "long":
            return {
                "action": "CLOSE",
                "quantity_percent": 0.0,
                "leverage": 1,
                "confidence_level": "高",
                "stop_loss_price": None,
                "reasoning_summary": f"价格{price:.0f}<长周期趋势线 {ema:.0f}，趋势转空，按宪法离场",
            }
        # 持空：对称宪法——价格收复长周期趋势线（下方多头区域分支）或统一止损触发才离场；
        # 不在超卖/金叉时回补，避免下跌途中被小反弹反复打脸（whipsaw）
        # （A/B实测：浮盈+RSI≤25深度超卖回补一半的V2胜率虚高但总利润少一个量级，弃用）
        if short:
            return _hold("价格位于长周期趋势线下方，空单持有至收复长周期趋势线或触发止损离场")
        # 空仓：
        if not getattr(CFG, "ALLOW_SHORT", False):      # 未开启做空：禁止做多（原文案）
            return _hold("价格位于长周期趋势线下方，禁止做多")
        # 已开启做空：空头排列/弱势信号 → 试探性开空；RSI≤28 超卖贴地不追空，
        # 避免在局部底部开空后立刻被反弹打出止损
        if (bear or (rsi is not None and rsi < 45)) and not oversold:
            return {
                "action": "SELL",
                "quantity_percent": 5.0,
                "confidence_level": "中",
                "stop_loss_price": None,
                "reasoning_summary": f"价格{price:.0f}<长周期趋势线 {ema:.0f}且"
                                     f"{'MACD空头排列' if bear else f'RSI={rsi:.0f}偏弱'}，试探性开空",
            }
        return _hold("空头区域但无明确做空信号（或RSI超卖贴地），观望")

    # ==================== 多头区域（价格 > 长周期趋势线） ====================
    if short:                                            # 持空却回到多头区：空单离场
        return {
            "action": "CLOSE",
            "quantity_percent": 0.0,
            "confidence_level": "高",
            "stop_loss_price": None,
            "reasoning_summary": f"价格{price:.0f}上穿长周期趋势线 {ema:.0f}，空单逆势离场",
        }

    weak = bear and (hist is not None and hist < 0)      # 动能转弱（死叉+绿柱）

    if pos <= 0.001:                                     # 空仓
        if bull and rsi is not None and 30 <= rsi <= 68 and (vol_ratio is None or vol_ratio >= 0.8):
            return {
                "action": "BUY",
                "quantity_percent": 5.0,
                "leverage": 1,
                "confidence_level": "中",
                "stop_loss_price": None,
                "reasoning_summary": f"趋势线上方且MACD多头排列、RSI={rsi:.0f}适中，试探性建仓",
            }
        return _hold("多头区域但无明确做多信号，观望")
    else:                                                # 持仓：先风控、后加减仓
        if overheat:
            return {
                "action": "SELL",
                "quantity_percent": min(pos, 5.0),
                "confidence_level": "高",
                "stop_loss_price": None,
                "reasoning_summary": f"RSI={rsi:.0f}过热，分批止盈",
            }
        if weak and rsi is not None and rsi > 58:
            return {
                "action": "SELL",
                "quantity_percent": min(pos, 3.0),
                "confidence_level": "中",
                "stop_loss_price": None,
                "reasoning_summary": "MACD死叉且绿柱、RSI偏高，动能转弱减仓",
            }
        if bull and pos < 15.0 and rsi is not None and rsi < 60:
            return {
                "action": "BUY",
                "quantity_percent": 5.0,
                "leverage": 1,
                "confidence_level": "中",
                "stop_loss_price": None,
                "reasoning_summary": f"多头排列延续（RSI={rsi:.0f}），顺势加仓至15%以内",
            }
        return _hold("持仓中且无明确加减仓信号，继续持有")


def _hold(reason: str) -> dict:
    """规则引擎的常规观望 HOLD（非故障兜底，不带『防御性』前缀）。"""
    return {
        "action": "HOLD",
        "quantity_percent": 0.0,
        "leverage": 1,
        "confidence_level": "低",
        "stop_loss_price": None,
        "reasoning_summary": reason,
    }
