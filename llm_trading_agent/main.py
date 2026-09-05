# -*- coding: utf-8 -*-
"""
main.py —— 程序主入口（调度器 + 状态机）
=========================================
运行方式：
    python main.py --mode live        # 模式1：实时模拟（默认，每4小时自动运行一次）
    python main.py --mode backtest    # 模式2：一次性回测（START_DATE → 现在）
    python main.py --mode live --once # 只跑一个切片就退出（联调用）

核心循环 run_slice()（每个4小时切片执行一次）：
    取数据 → 算指标 → 取最新切片 → 构建战情简报 → 调LLM决策
    → 执行层强制风控撮合 → 记录日志 → 持久化状态
"""

import argparse
import hashlib
import json
import logging
import platform
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pandas as pd  # 回测绩效计算需要

from config import CFG
import data_fetcher
import prompt_builder
import llm_client
import spot_policy
from order_executor import OrderExecutor

# 交易日按北京时间；K线时间已由 data_fetcher 转换为北京时区
logger = logging.getLogger("agent")


# ======================================================================
# 日志配置：控制台 + 文件（trading.log，10MB 轮转保留3份）
# ======================================================================
def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    # 控制台
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    # 文件（UTF-8，轮转）
    try:
        file_h = RotatingFileHandler(CFG.LOG_FILE, maxBytes=10 * 1024 * 1024,
                                     backupCount=3, encoding="utf-8")
        file_h.setFormatter(fmt)
        root.addHandler(file_h)
    except OSError as e:
        logger.warning("日志文件创建失败(%s)，仅输出到控制台", e)

    # 降低第三方库的噪音
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def _recent_rows(df, end_idx: int | None = None, n: int | None = None) -> list:
    """
    提取最近 n 根K线的紧凑上下文（供战情简报的 K 线窗口渲染）。
    字段：time/close/low/change_pct —— low 让 LLM 能看到每根低点（支撑/止损参考）。

    参数:
        df:       含 open_time/close/low/change_pct 列的行情 DataFrame
        end_idx:  截至的行索引（含）；None=取末尾
        n:        取最近几根；None=取 CFG.LLM_KLINE_WINDOW（0 则返回空）
    """
    if n is None:
        n = int(getattr(CFG, "LLM_KLINE_WINDOW", 24) or 0)
    if n <= 0 or df is None or len(df) == 0:
        return []
    end = len(df) - 1 if end_idx is None else int(end_idx)
    start = max(0, end - n + 1)
    out = []
    for _, r in df.iloc[start:end + 1].iterrows():
        chg = r.get("change_pct")
        out.append({
            "time": str(r["open_time"])[:16],
            "close": float(r["close"]),
            "low": float(r["low"]) if pd.notna(r["low"]) else None,
            "change_pct": round(float(chg), 2) if pd.notna(chg) else None,
        })
    return out


def make_decision(row_ctx: dict, snap: dict) -> dict:
    """
    决策分派：有 API 密钥 → 真实 LLM；无密钥 → 内置规则引擎（离线可跑）。

    密钥缺失时只在首次给出 WARNING（避免每4小时重复刷屏）。
    """
    if CFG.has_llm_key():
        messages = prompt_builder.build_full_prompt(
            row_ctx,
            position=snap.get("position_pct", 0.0),
            pnl=snap.get("pnl_pct", 0.0),
            total_trades=snap.get("n_trades", 0),
            daily_pnl=snap.get("daily_pnl_pct", 0.0),
            equity=snap.get("equity"),
            cash=snap.get("cash"),
            entry_price=snap.get("entry_price"),
            force_hold_reason=_hard_block_text(row_ctx, snap),
            max_leverage=getattr(CFG, "MAX_LEVERAGE", 10),
            cur_leverage=snap.get("leverage"),
            liq_price=snap.get("liq_price"),
            stop_price=snap.get("stop_price"),
        )
        return llm_client.call_llm(messages, provider=CFG.LLM_PROVIDER,
                                   max_position_pct=CFG.MAX_POSITION_PERCENT,
                                   max_leverage=getattr(CFG, "MAX_LEVERAGE", 10))

    if not getattr(make_decision, "_warned", False):
        logger.warning("未配置 %s 的 API 密钥，决策使用内置规则引擎（非LLM）；"
                       "如需真实LLM请在 .env 填入密钥", CFG.LLM_PROVIDER)
        make_decision._warned = True
    return llm_client.rule_based_decision(row_ctx, snap)


def apply_position_sizing(decision: dict, ex: OrderExecutor, price: float,
                          ema_slow=None, macd_dif=None, macd_dea=None,
                          market: dict | None = None) -> dict:
    """Apply the selected instrument's engine-owned sizing policy."""
    if not spot_policy.is_spot_mode():
        return decision
    return spot_policy.apply_spot_sizing(
        decision, ex, price, ema_slow=ema_slow,
        macd_dif=macd_dif, macd_dea=macd_dea, market=market)


# ======================================================================
# 系统级硬约束提示（供战情简报告知 LLM）
# ======================================================================
def _hard_block_text(row, snapshot: dict) -> str | None:
    """
    计算「系统级硬约束」提示文本（用于战情简报中告知 LLM）。
    覆盖两类禁止条款：长周期趋势线下方禁做多、趋势线上方禁开空（后者仅当
    ALLOW_SHORT=true 时才会出现 SELL 开空请求，故需提示宪法禁止）；
    以及两类熔断：当日亏损熔断（当天有效、次日清零）、峰值回撤熔断（可恢复）。
    并附带告知当前交易模式（现货 or 允许做空），避免 LLM 输出与模式不符。
    趋势方向判据统一用长周期慢线 ema_slow（EMA_TREND_SLOW_PERIOD），
    而非快线 ema_trend——慢线才是「趋势方向判官」，快线仅供微观动能参考。
    """
    price = row.get("close")
    ema = row.get("ema_slow") or row.get("ema_trend") or row.get("ema_200")
    daily = snapshot.get("daily_pnl_pct", 0.0) or 0.0
    dd = snapshot.get("drawdown_pct", 0.0) or 0.0
    allow_short = bool(getattr(CFG, "ALLOW_SHORT", False))
    trend_level = str(getattr(CFG, "TREND_FILTER_LEVEL", "engine")).strip().lower()
    blocks = []
    # ---- 趋势方向约束：engine=硬约束（历史）；llm=交 LLM 判断（战情简报指标行已含
    #      EMA 位置，这里不再重复强制，让 LLM 有机会用 K 线窗口做自己的趋势判断）----
    if trend_level == "engine":
        try:
            if float(price) < float(ema):
                blocks.append("价格低于长周期趋势线（宪法禁止做多）")
            elif allow_short and float(price) > float(ema):
                blocks.append("价格高于长周期趋势线（宪法禁止开空）")
        except (TypeError, ValueError):
            pass
    if daily <= -CFG.DAILY_LOSS_LIMIT_PERCENT:
        blocks.append(f"当日亏损{daily:.2f}%触发熔断")
    if dd <= -CFG.OVERALL_LOSS_LIMIT_PERCENT:
        blocks.append(f"峰值回撤{dd:.2f}%触发熔断")
    if blocks:
        return "；".join(blocks) + " → 今日建议只输出 HOLD/CLOSE"
    if not allow_short:
        return "合约模式（ALLOW_SHORT=false）：SELL 仅用于减多，禁止 SELL 开空"
    return None


# ======================================================================
# 模式1：实时模拟（4小时切片）
# ======================================================================
def _load_or_create_state() -> OrderExecutor:
    """加载 state.json；不存在则从默认本金新建（并把初始状态落盘）。"""
    ex = OrderExecutor(capital=CFG.CAPITAL, state_path=CFG.STATE_FILE)
    ex.load_state()
    if not CFG.STATE_FILE.exists():
        ex.save_state()
    return ex


def run_slice(ex: OrderExecutor) -> None:
    """执行一个完整的战情切片（数据→简报→决策→执行→落盘→实时曲线）。周期=CFG.INTERVAL。"""
    slice_start = time.time()
    logger.info("=" * 70)
    logger.info("开始新的%s战情切片(%s) @ %s", _interval_cn(CFG.INTERVAL), CFG.SYMBOL,
                time.strftime("%Y-%m-%d %H:%M:%S"))

    # ---- 1) 获取最近 ~500 根4小时K线（确保趋势线预热充分）----
    try:
        df = data_fetcher.fetch_klines(limit=500)
        df = data_fetcher.closed_klines_only(df, CFG.INTERVAL)
        if df.empty:
            raise RuntimeError("行情数据为空")
        df = data_fetcher.calculate_indicators(df)
        # 丢弃趋势线预热期之前的数据（以长周期慢线 ema_slow 为准，确保趋势判据有效）
        df = df.dropna(subset=["ema_slow"]).reset_index(drop=True)
        row = df.iloc[-1]
    except Exception as e:
        logger.error("[切片] 数据获取/指标计算失败: %s —— 本轮按HOLD处理", e)
        return

    # 最新价与切片时间（北京时间）
    signal_price = float(row["close"])
    ts = row["open_time"]
    bar_key = str(ts)
    if ex.last_processed_bar == bar_key:
        logger.info("[切片] 闭合K线 %s 已处理，跳过重复决策", str(ts)[:16])
        return
    # 信号只使用完整收盘K线；成交使用此刻可执行的最新价，不能倒填历史收盘价。
    try:
        price = float(data_fetcher.fetch_latest_price())
    except Exception as e:
        logger.warning("[切片] 最新成交价获取失败(%s)，本轮HOLD，避免按历史收盘价虚拟成交", e)
        return
    logger.info("[切片] 信号K线收盘 %.2f；当前可执行价 %.2f", signal_price, price)

    # ---- 2) 账户快照 & 战情简报 & LLM 决策 ----
    snap = ex.snapshot(price)

    # 把最近 N 根K线明细附加给 LLM（v9：CFG.LLM_KLINE_WINDOW 根，LLM 可看走势形态）
    row_ctx = row.to_dict()
    row_ctx["_recent"] = _recent_rows(df)

    # ---- 3) 决策（有密钥→LLM；无密钥→规则引擎）----
    decision = make_decision(row_ctx, snap)
    decision = apply_position_sizing(
        decision, ex, price,
        ema_slow=float(row["ema_slow"]) if pd.notna(row["ema_slow"]) else None,
        macd_dif=float(row["macd_dif"]) if pd.notna(row["macd_dif"]) else None,
        macd_dea=float(row["macd_dea"]) if pd.notna(row["macd_dea"]) else None,
        market=row.to_dict())
    logger.info("[决策] action=%s qty=%.1f%% conf=%s reason=%s",
                decision["action"], decision.get("quantity_percent", 0.0),
                decision.get("confidence_level", "?"), decision.get("reasoning_summary", ""))

    # ---- 4) 执行（含强制风控：长周期趋势线拦截/熔断/仓位硬顶/单笔上限）----
    result = ex.execute_decision(
        decision, price, ts,
        ema_slow=float(row["ema_slow"]) if pd.notna(row["ema_slow"]) else None,
        macd_dif=float(row["macd_dif"]) if pd.notna(row["macd_dif"]) else None,
        macd_dea=float(row["macd_dea"]) if pd.notna(row["macd_dea"]) else None,
        atr_pct=float(row["atr_pct"]) if pd.notna(row["atr_pct"]) else None)

    # ---- 4.5) 逐仓强平 & 资金费率（切片粒度）----
    ex.check_liquidation(price, ts)
    ex.accrue_funding(ts, price)

    # ---- 5) 止损 / 移动止盈巡检（切片粒度）----
    sl_result = ex.check_stop_loss(price, ts)
    if sl_result and sl_result.get("action") == "CLOSE":
        result = sl_result
    tp_result = ex.check_trailing_tp(price, ts)
    if tp_result and tp_result.get("action") == "CLOSE":
        result = tp_result
    # ---- 5.5) 刷新历史峰值权益（供下一切片回撤熔断计算）----
    ex.update_peak(price)

    # ---- 6) 落盘 + 摘要日志 ----
    snap_after = ex.snapshot(price)
    ex.last_processed_bar = bar_key
    ex.save_state(price)
    logger.info("[成交] %s @ %.2f | 持仓 %.2f%% | 权益 %.2f | 累计盈亏 %+.2f%% | 当日 %+.2f%%",
                result["action"], price,
                snap_after["position_pct"], snap_after["equity"],
                snap_after["pnl_pct"], snap_after["daily_pnl_pct"])
    logger.info("切片耗时 %.1fs", time.time() - slice_start)

    # ---- 7) 实时曲线追加（供 GUI 图表轮询，买入持有线由 GUI 以首行价为基准绘制）----
    try:
        _append_live_curve(ts, snap_after["equity"], price)
    except Exception as e:                       # noqa: BLE001 —— 曲线写入失败不影响主流程
        logger.debug("实盘曲线写入失败: %s", e)


def run_stop_loss_check(ex: OrderExecutor) -> None:
    """高频止损巡检（默认每2分钟）：只取最新价，不调用 LLM。"""
    try:
        price = data_fetcher.fetch_latest_price()
        result = ex.check_stop_loss(price, pd.Timestamp.now())
        if result and result.get("action") == "CLOSE":
            logger.warning("[巡检] 止损在两次切片之间被触发 @ %.2f", price)
            ex.save_state(price)
    except Exception as e:
        logger.debug("[巡检] 最新价获取失败: %s", e)


# ======================================================================
# 切片周期 & 实时曲线（供 GUI 图表）
# ======================================================================
def _interval_cn(interval: str) -> str:
    """'30m'->'30分钟'；'4h'->'4小时'；'1d'->'1天'（日志/文案展示用）。"""
    interval = (interval or "4h").strip().lower()
    try:
        n = int(interval[:-1]); u = interval[-1]
    except ValueError:
        n, u = 4, "h"
    return {"m": f"{n}分钟", "h": f"{n}小时", "d": f"{n}天"}.get(u, f"{n}小时")


def _interval_schedule(interval: str):
    """把切片周期转成 schedule 的 (n, unit)：'15m'->(15,'minutes')、'1d'->(1,'days')。"""
    interval = (interval or "4h").strip().lower()
    try:
        n = int(interval[:-1]); u = interval[-1]
    except ValueError:
        n, u = 4, "h"
    if u == "m":
        return n, "minutes"
    if u == "d":
        return n, "days"
    return n, "hours"


def _curve_csv_path(name: str):
    """runs/ 下的曲线 CSV 路径（目录不存在则创建）。"""
    runs = CFG.BASE_DIR / "runs"
    try:
        runs.mkdir(exist_ok=True)
    except OSError:
        pass
    return runs / name


def _append_live_curve(ts, equity: float, price: float) -> None:
    """实盘每切片向 runs/live_curve.csv 追加一行（文件不存在则先写表头）。"""
    path = _curve_csv_path("live_curve.csv")
    line = "%s,%.2f,%.2f\n" % (str(ts)[:16], float(equity), float(price))
    if not path.exists():
        path.write_text("time,equity,price\n", encoding="utf-8")
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def _preheat_live(ex_old: OrderExecutor, start_date: str) -> OrderExecutor:
    """
    实盘时间起点预热：用**规则引擎**把 [start_date, 最新K线) 快速回放，
    把权益/价格曲线直接铺满到当前窗口。

    - 只影响模拟账本与图表显示，**不调用 LLM**（避免预热烧 token；实时节点
      仍按用户选择的引擎决策）；
    - 旧 state.json 先备份到 runs/state_backup_<时间戳>.json（模拟盘从起点重演）；
    - 预热止于最新一根K线之前，最新一根交给随后的 run_slice 处理 → 无缝衔接。
    """
    # ---- 备份现有账本 ----
    try:
        if CFG.STATE_FILE.exists():
            bak = CFG.BASE_DIR / "runs" / ("state_backup_%s.json"
                                           % time.strftime("%Y%m%d_%H%M%S"))
            bak.parent.mkdir(exist_ok=True)
            import shutil
            shutil.copy2(CFG.STATE_FILE, bak)
            logger.warning("预热将重建模拟账本，旧状态已备份: %s", bak)
    except Exception as e:                       # noqa: BLE001
        logger.warning("旧状态备份失败: %s", e)

    try:
        df = data_fetcher.fetch_klines(start_date=start_date)
        if df.empty:
            raise RuntimeError("预热数据为空")
        df = data_fetcher.calculate_indicators(df)
        # 截断到切片上限，避免预热过久
        if len(df) > CFG.MAX_BACKTEST_BARS:
            logger.warning("预热区间共 %d 根，超过上限 %d，自动截取最近 %d 根"
                           "（实际起点 %s）",
                           len(df), CFG.MAX_BACKTEST_BARS, CFG.MAX_BACKTEST_BARS,
                           df.iloc[-CFG.MAX_BACKTEST_BARS]["open_time"])
            df = df.tail(CFG.MAX_BACKTEST_BARS).reset_index(drop=True)
        # 最新一根留给随后 run_slice 处理 → 与实时无缝衔接
        if len(df) > 1:
            df = df.iloc[:-1].reset_index(drop=True)

        first_valid = df["ema_slow"].first_valid_index()
        if first_valid is None:
            raise RuntimeError("预热区间不足，无法形成趋势线")
        start_idx = df.index.get_loc(first_valid)

        # 全新账本（从起点以本金重新模拟）
        ex = OrderExecutor(capital=CFG.CAPITAL, state_path=CFG.STATE_FILE)
        _replay_rules(df, start_idx, ex, "预热")
        ex.save_state()
        logger.warning("预热完成：%s → %s 共回放 %d 根（规则引擎，未调用LLM）",
                       df.iloc[start_idx]["open_time"], df.iloc[-1]["open_time"],
                       len(df) - start_idx)
        return ex
    except Exception as e:                       # noqa: BLE001
        logger.error("预热失败（%s），跳过预热，按现有状态直接进入实盘。", e)
        return ex_old


_EXECUTION_MODELS = ("close", "next_open", "intrabar")


def _execution_model() -> str:
    """返回校验后的执行成交模型（EXECUTION_MODEL，见 config.py 注释）。

    close/intrabar/next_open 三档决定止损/止盈/开平仓的「触发识别时点」与「成交价」，
    只作用于回测与预热（run_backtest/_replay_rules）；实盘 run_slice 始终按最新收盘价
    即时执行（真实延迟≈0，等价 close 语义），不受此参数影响。
    """
    model = str(getattr(CFG, "EXECUTION_MODEL", "intrabar")).strip().lower()
    if model not in _EXECUTION_MODELS:
        raise SystemExit(
            f"EXECUTION_MODEL 非法值 {model!r}，可选：{'/'.join(_EXECUTION_MODELS)}"
            "（close=收盘价成交 / next_open=次根开盘成交 / intrabar=盘中极值触发成交）")
    return model


def _replay_rules(df, start_idx: int, ex: OrderExecutor, tag: str = "回测") -> None:
    """
    规则引擎逐根回放内核（供回测与实盘预热共用）：
    止损近似判断（多/空方向自适应）→ 决策 → 执行 → 记录权益曲线到 live_curve.csv。
    预热模式 (tag='预热') 额外把曲线逐行写入 runs/live_curve.csv，供 GUI 绘制。
    """
    _model = _execution_model()
    equity_curve: list = []
    for i in range(start_idx, len(df)):
        row = df.iloc[i]
        ts = row["open_time"]
        price = float(row["close"])
        ema = float(row["ema_trend"]) if pd.notna(row["ema_trend"]) else None
        ema_slow = float(row["ema_slow"]) if pd.notna(row["ema_slow"]) else None
        macd_dif = float(row["macd_dif"]) if pd.notna(row["macd_dif"]) else None
        macd_dea = float(row["macd_dea"]) if pd.notna(row["macd_dea"]) else None
        atr_pct = float(row["atr_pct"]) if pd.notna(row["atr_pct"]) else None

        # ① 日期滚动（当日亏损熔断按北京时间日）
        ex.roll_day(ts, price)

        # ①.5 资金费率计提
        ex.accrue_funding(ts, price)

        # ② next_open：次根开盘先结算上根收盘挂起的动作（先平仓、后开/平仓指令）。
        #     模拟「收盘信号/收盘识别 → 次根开盘价才成交」的延迟交易者（含隔夜跳空损耗）。
        if _model == "next_open":
            _sr = ex.settle_pending(float(row["open"]), ts)
            if _sr and _sr["action"] not in ("HOLD", "CLOSE"):
                logger.info("[%s] %s @ %.2f（次根开盘） | %s | 保证金占比->%.2f%%",
                            str(ts)[:16], _sr["action"], float(row["open"]),
                            _sr["reason"], ex.position_pct(float(row["open"])))

        # ②.5 逐仓强平检查
        ex.check_liquidation(price, ts)

        # ③ 止损检查（按执行模型三档：intrabar=盘中极值击穿按线价即时成交；
        #     close/next_open=仅收盘价识别，close 按收盘价即时、next_open 挂起次根开盘）
        sl = ex.stop_level()
        if sl is not None and ex.position is not None:
            _long = ex.position["side"] == "long"
            if _model == "intrabar":
                _hit = (float(row["low"]) <= sl) if _long else (float(row["high"]) >= sl)
                if _hit:
                    ex.force_close(sl, ts, reason="止损触发（盘中极值击穿，按止损线价成交）")
            else:
                _hit = (price <= sl) if _long else (price >= sl)
                if _hit:
                    if _model == "close":
                        ex.force_close(price, ts, reason="止损触发（收盘价击穿止损线）")
                    else:  # next_open：收盘识别 → 次根开盘离场
                        ex.set_pending_exit(ts, "止损触发（收盘价击穿止损线）")

        # ③.5 移动止盈（intrabar=盘中击穿按触发线价；close/next_open=收盘回撤识别，
        #     next_open 命中挂起、次根开盘离场）＋ 权益回撤止损（同样 defer 化）
        if _model == "intrabar":
            ex.check_trailing_tp_intrabar(float(row["high"]), float(row["low"]), ts)
            ex.check_trailing_tp(price, ts)
        else:
            ex.check_trailing_tp(price, ts, defer=(_model == "next_open"))
        ex.check_stop_loss(price, ts, defer=(_model == "next_open"))

        # ④ 决策（预热/回测默认规则引擎）
        if _model == "next_open" and ex.pending_exit is not None:
            # 已有待执行平仓：本根暂停决策，避免与次根开盘的离场叠加反向新单
            result = {"action": "HOLD", "filled_qty_pct": 0.0,
                      "reason": "次根开盘待平仓，本根暂停决策", "price": price}
        else:
            snap = ex.snapshot(price)
            decision = llm_client.rule_based_decision(row.to_dict(), snap)
            decision = apply_position_sizing(
                decision, ex, price, ema_slow=ema_slow,
                macd_dif=macd_dif, macd_dea=macd_dea, market=row.to_dict())
            result = ex.execute_decision(decision, price, ts,
                                         ema_slow=ema_slow,
                                         macd_dif=macd_dif, macd_dea=macd_dea,
                                         atr_pct=atr_pct,
                                         defer=(_model == "next_open"))
        # ⑤ 收盘后再查一次强平与止损
        ex.check_liquidation(price, ts)
        ex.check_stop_loss(price, ts, defer=(_model == "next_open"))
        ex.check_trailing_tp(price, ts, defer=(_model == "next_open"))
        # ⑤.5 刷新历史峰值权益
        ex.update_peak(price)

        # 成交日志：CLOSE 已在 force_close 内部统一打印（含本次/累计盈亏），此处避免重复
        if result["action"] not in ("HOLD", "CLOSE") and not result.get("deferred"):
            logger.info("[%s] %s @ %.2f | %s | 保证金占比->%.2f%%",
                        str(ts)[:16], result["action"], price,
                        result["reason"], ex.position_pct(price))

        # ⑤ 记录权益曲线（预热模式实时落盘供 GUI 轮询；回测模式由调用方收集）
        eq = ex.mark_to_market(price)
        equity_curve.append((str(ts)[:16], eq))
        if tag == "预热":
            try:
                _append_live_curve(ts, eq, price)
            except Exception:                    # noqa: BLE001
                pass
    return equity_curve


def mode_live(args) -> None:
    """实盘模拟模式：可选预热回放 → 立即执行一个切片 → 按 CFG.INTERVAL 周期调度。"""
    logger.info("启动实盘模拟 | 标的=%s 周期=%s | 提供方=%s | 本金=%.0f USDT",
                CFG.SYMBOL, CFG.INTERVAL, CFG.LLM_PROVIDER, CFG.CAPITAL)
    engine = f"真实LLM({CFG.llm_model})" if CFG.has_llm_key() else "内置规则引擎（未配置密钥，离线可用）"
    logger.info("决策引擎: %s", engine)

    ex = _load_or_create_state()

    # ① 实盘时间起点预热（仅循环模式且显式设置了 LIVE_START；规则引擎不烧token）
    if CFG.LIVE_START and not args.once:
        logger.warning("检测到实盘起点 %s → 先快速预热补齐历史曲线", CFG.LIVE_START)
        ex = _preheat_live(ex, CFG.LIVE_START)

    # ② 立即执行一次（保证程序一启动就有决策输出；若已预热则衔接最新一根）
    run_slice(ex)

    if args.once:
        logger.info("--once 模式：单切片执行完毕，退出。")
        ex.save_state()
        return

    import schedule  # 延迟导入：仅在进入调度循环时才需要

    # ③ 按所选切片周期自动运行
    n, unit = _interval_schedule(CFG.INTERVAL)
    getattr(schedule.every(n), unit).do(run_slice, ex)
    logger.info("已按切片周期调度：每 %s 自动决策一次", _interval_cn(CFG.INTERVAL))

    # ④ 可选：每 N 分钟巡检一次止损（弥补切片之间的止损盲区）
    if CFG.ENABLE_FREQUENT_STOP_CHECK:
        schedule.every(CFG.STOP_CHECK_MINUTES).minutes.do(run_stop_loss_check, ex)
        logger.info("已启用止损高频巡检：每 %d 分钟一次", CFG.STOP_CHECK_MINUTES)

    logger.info("进入调度循环：每 %s 一个切片（Ctrl+C 退出）...", _interval_cn(CFG.INTERVAL))
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("收到退出信号，保存状态后关闭。")
        try:
            ex.save_state()
        except Exception:
            pass


# ======================================================================
# 模式2：一次性回测
# ======================================================================
_INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120,
                     "4h": 240, "6h": 360, "8h": 480, "12h": 720, "1d": 1440,
                     "3d": 4320, "1w": 10080}

def _interval_minutes(iv) -> float:
    """把周期字符串转成分钟（年化 Sharpe 用）；未知周期回退 240（=4h 旧口径）。"""
    key = str(iv or "").strip().lower()
    return float(_INTERVAL_MINUTES.get(key, 240.0))


def compute_performance(equity_curve: list) -> dict:
    """
    根据逐K线权益曲线计算绩效指标。
    equity_curve: [(time_str, equity), ...]
    """
    eq = pd.Series([e for _, e in equity_curve], dtype=float)
    rets = eq.pct_change().dropna()

    total_return = (eq.iloc[-1] / eq.iloc[0] - 1.0) * 100.0 if len(eq) > 1 else 0.0

    # 最大回撤（峰值到谷底）
    running_max = eq.cummax()
    drawdown = (eq / running_max - 1.0) * 100.0
    max_dd = float(drawdown.min()) if len(drawdown) else 0.0

    # 年化因子按真实周期（Codex #2 修复）：每周期根数/年 = 365×1440/周期分钟。
    # 旧实现固定按 4h（√2190）年化 → 日线 Sharpe 被放大 √6、30m 被缩小 √8。
    minutes = _interval_minutes(getattr(CFG, "INTERVAL", "4h"))
    bars_per_year = 365.0 * 1440.0 / minutes
    annual_factor = bars_per_year ** 0.5
    if len(rets) > 1 and float(rets.std()) > 1e-12:
        sharpe = float(rets.mean() / rets.std() * annual_factor)
    else:
        sharpe = None

    # 收益/最大回撤（Calmar 口径，用户目标：用风险换高收益 → 看风险调整后的收益而非胜率）
    calmar = total_return / abs(max_dd) if abs(max_dd) > 1e-9 else (0.0 if total_return <= 0 else float("inf"))

    return {
        "total_return": total_return,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "annual_bars": bars_per_year,
        "calmar": calmar,
        "bars": len(equity_curve),
    }


def compute_path_diagnostics(equity_curve: list, prices: list,
                             exposure_curve: list, ex: OrderExecutor) -> dict:
    """Diagnostics tied to the stated goal: catch upside, avoid deep down bars and wear."""
    if len(equity_curve) < 2 or len(prices) < 2:
        return {
            "buy_hold_return_pct": 0.0, "excess_vs_buy_hold_pp": 0.0,
            "up_bar_capture_pct": 0.0, "down_bar_capture_pct": 0.0,
            "worst_5pct_bar_avg_exposure_pct": 0.0,
            "turnover_on_initial_capital_pct": 0.0, "cost_drag_pct": 0.0,
        }
    eq = pd.Series([float(v) for _, v in equity_curve])
    px = pd.Series([float(v) for v in prices])
    sr = eq.pct_change().iloc[1:].reset_index(drop=True)
    mr = px.pct_change().iloc[1:].reset_index(drop=True)
    up = mr > 0
    down = mr < 0
    up_den = float(mr[up].sum())
    down_den = float(mr[down].sum())
    up_capture = float(sr[up].sum()) / up_den * 100.0 if abs(up_den) > 1e-12 else 0.0
    down_capture = float(sr[down].sum()) / down_den * 100.0 if abs(down_den) > 1e-12 else 0.0
    n_worst = max(1, int(len(mr) * 0.05))
    worst_idx = list(mr.nsmallest(n_worst).index)
    exp = list(exposure_curve[1:])
    worst_exp = (sum(exp[i] for i in worst_idx if i < len(exp)) / len(worst_idx)
                 if worst_idx else 0.0)
    buy_hold = (px.iloc[-1] / px.iloc[0] - 1.0) * 100.0
    strategy_return = (eq.iloc[-1] / eq.iloc[0] - 1.0) * 100.0
    turnover = sum(abs(float(t.get("notional_usdt") or 0.0)) for t in ex.trades)
    return {
        "buy_hold_return_pct": buy_hold,
        "excess_vs_buy_hold_pp": strategy_return - buy_hold,
        "up_bar_capture_pct": up_capture,
        "down_bar_capture_pct": down_capture,
        "worst_5pct_bar_avg_exposure_pct": worst_exp,
        "turnover_on_initial_capital_pct": turnover / ex.base_capital * 100.0,
        "cost_drag_pct": float(ex.stats.get("fees_paid", 0.0)) / ex.base_capital * 100.0,
    }


def plot_equity_curve(equity_curve: list, out_path, trades: list = None) -> str | None:
    """
    绘制权益曲线 + 回撤子图（可选，依赖 matplotlib）。

    参数:
        equity_curve: [(time_str, equity), ...]
        out_path:     PNG 保存路径
        trades:       成交记录列表（含 time/action），用于在曲线上标注
                      BUY（红▲）/ SELL/CLOSE（蓝▼）动作点；可为 None

    返回: 保存成功返回路径，matplotlib 缺失时返回 None（不影响回测主流程）。
    """
    try:
        import matplotlib
        matplotlib.use("Agg")          # 无界面后端，双击运行不会弹窗
        import matplotlib.pyplot as plt
    except ImportError:
        logger.warning("未安装 matplotlib，跳过权益曲线绘图（pip install matplotlib 可启用）")
        return None

    # 中文字体（Windows: 微软雅黑优先）
    try:
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS"]
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass

    df = pd.DataFrame(equity_curve, columns=["time", "equity"])
    df["time"] = pd.to_datetime(df["time"])
    rets = df["equity"].pct_change()
    dd = (df["equity"] / df["equity"].cummax() - 1.0) * 100.0   # 回撤(%)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(11, 7), sharex=True,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.08})
    fig.suptitle(f"LLM量化Agent 回测权益曲线  |  初始本金 {CFG.CAPITAL:,.0f} USDT",
                 fontsize=13, fontweight="bold")

    # 上：权益曲线
    ax1.plot(df["time"], df["equity"], lw=1.2, color="#c0392b", label="总权益")
    ax1.axhline(CFG.CAPITAL, color="#7f8c8d", ls="--", lw=0.8, alpha=0.7, label="初始本金")
    ax1.fill_between(df["time"], df["equity"].min(), df["equity"],
                     where=(df["equity"] < CFG.CAPITAL), color="#27ae60",
                     alpha=0.12, label="水下区间")
    ax1.set_ylabel("总权益 (USDT)")
    ax1.grid(alpha=0.25)

    # 上：叠加买卖动作点（BUY=红▲，SELL/CLOSE=蓝▼）
    if trades:
        eq_by_time = {t: v for t, v in zip(df["time"], df["equity"])}
        buy_t, buy_v, sell_t, sell_v = [], [], [], []
        for tr in trades:
            try:
                tm = pd.to_datetime(tr["time"])
            except (TypeError, ValueError):
                continue
            if tm not in eq_by_time:
                continue                       # 未命中权益曲线时点则跳过
            if tr.get("action") == "BUY":
                buy_t.append(tm); buy_v.append(eq_by_time[tm])
            elif tr.get("action") in ("SELL", "CLOSE"):
                sell_t.append(tm); sell_v.append(eq_by_time[tm])
        if buy_t:
            ax1.scatter(buy_t, buy_v, marker="^", s=42, color="#d63031",
                        edgecolors="white", linewidths=0.5, zorder=5, label=f"BUY ×{len(buy_t)}")
        if sell_t:
            ax1.scatter(sell_t, sell_v, marker="v", s=42, color="#0984e3",
                        edgecolors="white", linewidths=0.5, zorder=5, label=f"SELL/CLOSE ×{len(sell_t)}")

    ax1.legend(loc="upper left", fontsize=9, ncols=2)

    # 下：回撤
    ax2.fill_between(df["time"], dd, 0, color="#8e44ad", alpha=0.35, label="回撤")
    ax2.set_ylabel("回撤 (%)")
    ax2.legend(loc="lower left", fontsize=9)
    ax2.grid(alpha=0.25)

    try:
        fig.autofmt_xdate()
        fig.savefig(out_path, dpi=130, bbox_inches="tight")
        plt.close(fig)
        return str(out_path)
    except OSError as e:
        logger.error("权益曲线图保存失败: %s", e)
        return None


def _cfg_snapshot() -> dict:
    """配置快照：dump CFG 全部生效参数（排除密钥），供回测复现（Codex #5）。"""
    skip_sub = ("KEY", "SECRET", "TOKEN", "PASSWORD")
    out: dict = {}
    for k in vars(type(CFG)):                     # 类属性 + property
        if k.startswith("_"):
            continue
        if any(s in k.upper() for s in skip_sub):
            continue                              # 绝不落盘密钥
        try:
            val = getattr(CFG, k)                 # 实例优先 → 拿生效值（非类默认）
        except Exception:
            continue
        if callable(val):
            continue
        if isinstance(val, Path):
            val = str(val)
        elif isinstance(val, (list, tuple)):
            val = [str(x) for x in val]
        elif isinstance(val, float):
            val = round(val, 6)
        out[k] = val
    return out


def _data_fingerprint(df, start_idx: int) -> dict:
    """行情指纹：完整 OHLCV sha256 + 尾部收盘 sha1，供严格比对数据一致性。"""
    tail = df.tail(50)
    h = hashlib.sha1()
    h_full = hashlib.sha256()
    fp_cols = [c for c in ("open_time", "open", "high", "low", "close", "volume") if c in df.columns]
    for row in df[fp_cols].itertuples(index=False, name=None):
        h_full.update(("|".join(str(v) for v in row) + "\n").encode("utf-8"))
    for t, c in zip(tail["open_time"].astype(str), tail["close"].astype(str)):
        h.update(f"{t}|{c}".encode("utf-8"))
    return {
        "source_rows": int(len(df)),
        "warm_skipped": int(start_idx),
        "replayed_rows": int(len(df) - start_idx),
        "first_ts": str(df.iloc[start_idx]["open_time"]),
        "last_ts": str(df.iloc[-1]["open_time"]),
        "close_sha1_tail50": h.hexdigest(),
        "ohlcv_sha256_full": h_full.hexdigest(),
        "cols": [str(c) for c in df.columns],
    }


def _load_historical_funding(symbol: str, start_date: str,
                             end_date: str | None) -> tuple[pd.DataFrame, dict]:
    """读取/下载历史 funding，并返回可复现元数据；缓存写入 runs/_funding_cache。"""
    cache_dir = CFG.BASE_DIR / "runs" / "_funding_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe_end = end_date or f"now_{pd.Timestamp.now(tz=CFG.TIMEZONE).strftime('%Y-%m-%d')}"
    cache_path = cache_dir / f"v2_1hmark_{symbol}_{start_date}_{safe_end}.csv"
    source = "cache"
    if cache_path.exists():
        df = pd.read_csv(cache_path, encoding="utf-8-sig")
        df["funding_time"] = pd.to_datetime(df["funding_time"])
    else:
        source = "binance_futures_api"
        df = data_fetcher.fetch_funding_rates(symbol, start_date, end_date)
        tmp = cache_path.with_suffix(".csv.tmp")
        df.to_csv(tmp, index=False, encoding="utf-8-sig")
        tmp.replace(cache_path)
    required = {"funding_time", "funding_rate", "mark_price"}
    if not required.issubset(df.columns):
        raise ValueError(f"funding缓存缺列: {sorted(required - set(df.columns))}")
    df["funding_time"] = pd.to_datetime(df["funding_time"])
    df["funding_rate"] = pd.to_numeric(df["funding_rate"], errors="coerce")
    df["mark_price"] = pd.to_numeric(df["mark_price"], errors="coerce")
    df = (df.dropna(subset=["funding_time", "funding_rate"])
          .drop_duplicates(subset="funding_time", keep="last")
          .sort_values("funding_time").reset_index(drop=True))
    h = hashlib.sha256()
    for r in df.itertuples(index=False):
        h.update(f"{r.funding_time}|{r.funding_rate}|{r.mark_price}\n".encode("utf-8"))
    meta = {
        "mode": "historical",
        "source": source,
        "cache_file": str(cache_path),
        "rows": int(len(df)),
        "first_ts": str(df.iloc[0]["funding_time"]) if len(df) else None,
        "last_ts": str(df.iloc[-1]["funding_time"]) if len(df) else None,
        "sha256": h.hexdigest(),
    }
    return df, meta


def _dump_repro_json(repro: dict) -> str | None:
    """把可复现 JSON 写到项目根目录 backtest_repro.json；失败返回 None。"""
    try:
        p = CFG.BASE_DIR / "backtest_repro.json"
        p.write_text(json.dumps(repro, ensure_ascii=False, indent=1), encoding="utf-8")
        return str(p)
    except OSError as e:
        logger.error("backtest_repro.json 写入失败: %s", e)
        return None


def run_backtest(args) -> None:
    """从 START_DATE（可选 BACKTEST_END）逐根K线回放；超过 1000 切片自动截取最近部分。"""
    end_txt = CFG.BACKTEST_END or "现在"
    logger.info("=" * 70)
    logger.info("启动回测 | 标的=%s 周期=%s | 区间 %s → %s（切片上限 %d）",
                CFG.SYMBOL, CFG.INTERVAL, CFG.START_DATE, end_txt,
                CFG.MAX_BACKTEST_BARS)
    decision_replay = getattr(args, "decision_replay", None)
    replay_mode = isinstance(decision_replay, dict)
    use_llm = False if replay_mode else (args.use_llm or CFG.BACKTEST_USE_LLM)
    if replay_mode:
        logger.warning("回测决策引擎：离线重放 %d 条已保存 LLM 信号；本次不会调用 API。",
                       len(decision_replay))
    elif use_llm:
        if not CFG.has_llm_key():
            logger.error("--use-llm 已指定但缺少 API 密钥，请先配置 .env")
            sys.exit(1)
        logger.warning("回测将逐根K线调用真实LLM（约每%s1次API），耗时与费用请自行评估！",
                       _interval_cn(CFG.INTERVAL))
    else:
        logger.info("回测决策引擎：内置规则引擎（宪法机械执行）。"
                    "如需真实LLM请加 --use-llm（费用自负）。")

    # ---- 1) 拉取历史数据并计算指标 ----
    try:
        df = data_fetcher.fetch_klines(start_date=CFG.START_DATE,
                                        end_date=CFG.BACKTEST_END)
        df = data_fetcher.closed_klines_only(df, CFG.INTERVAL)
    except Exception as e:
        logger.error("回测数据获取失败: %s\n提示：请检查网络是否能访问 Binance"
                     "（程序已内置备用域名自动切换）。", e)
        sys.exit(1)
    if df.empty:
        logger.error("回测数据为空，无法回测")
        sys.exit(1)
    df = data_fetcher.calculate_indicators(df)

    # ---- 1.5) 切片上限保护：超过 MAX_BACKTEST_BARS 自动截取最近部分 ----
    truncated = len(df) > CFG.MAX_BACKTEST_BARS
    if truncated:
        kept = df.tail(CFG.MAX_BACKTEST_BARS).reset_index(drop=True)
        logger.warning("所选区间共 %d 个切片，超过上限 %d → 自动截取最近 %d 个，"
                       "实际起点调整为 %s（如需更早区间请改小切片间隔或缩短范围）",
                       len(df), CFG.MAX_BACKTEST_BARS, CFG.MAX_BACKTEST_BARS,
                       kept.iloc[0]["open_time"])
        df = kept

    # 定位首个趋势线有效行（以长周期慢线 ema_slow 为准，确保趋势方向判据
    # 在整段回放内都有效——快线 ema_trend 仅需 6 根预热，慢线需 EMA_TREND_SLOW_PERIOD 根）
    first_valid = df["ema_slow"].first_valid_index()
    if first_valid is None:
        logger.error("历史数据不足，无法形成趋势线，请扩大回测区间")
        sys.exit(1)
    start_idx = df.index.get_loc(first_valid)
    logger.info("共 %d 根K线（预热跳过 %d，实际回放 %d），起点 %s",
                len(df), start_idx, len(df) - start_idx,
                df.iloc[start_idx]["open_time"])

    # ---- 2) 逐根回放 ----
    ex = OrderExecutor(capital=CFG.CAPITAL, state_path=None)   # 回测不污染实盘 state.json
    equity_curve: list = []
    prices: list = []
    exposure_curve: list[float] = []
    leverage_curve: list[float] = []
    _model = _execution_model()

    # ---- 可复现快照（Codex #5）：配置快照 + 行情指纹 + LLM prompt hash/原始响应 ----
    repro: dict = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "runtime": f"python {platform.python_version()} {platform.system()}",
        "use_llm": bool(use_llm),
        "decision_mode": "offline_llm_replay" if replay_mode else ("live_llm" if use_llm else "rules"),
        "cfg": None,        # 循环结束后填入（含全量生效参数，密钥除外）
        "data": None,       # 行情指纹（行数/首末时间/收盘sha1）
        "funding": None,    # historical/fixed 口径与历史费率指纹
        "llm_calls": [],    # use_llm 时逐根记录（prompt sha1 + 原始响应 + 解析决策）
    }

    funding_mode = ("none_spot" if ex.trading_mode == "spot" else
                    str(getattr(CFG, "BACKTEST_FUNDING_MODE", "historical")).strip().lower())
    if funding_mode not in ("none_spot", "historical", "fixed"):
        raise ValueError(f"BACKTEST_FUNDING_MODE 非法: {funding_mode!r}，仅支持 historical/fixed")
    funding_df = pd.DataFrame()
    funding_idx = 0
    if funding_mode == "none_spot":
        repro["funding"] = {"mode": "none", "reason": "spot has no perpetual funding"}
        logger.info("现货模式：不下载、不计提永续合约 funding")
    elif funding_mode == "historical":
        try:
            funding_df, funding_meta = _load_historical_funding(
                CFG.SYMBOL, CFG.START_DATE, CFG.BACKTEST_END)
        except Exception as e:
            logger.error("历史 funding 获取/缓存失败：%s。为避免静默退回错误口径，本次回测终止。", e)
            raise
        repro["funding"] = funding_meta
        logger.info("历史 funding：%d 个结算点，来源=%s，sha256=%s",
                    len(funding_df), funding_meta["source"], funding_meta["sha256"][:12])
    else:
        repro["funding"] = {
            "mode": "fixed", "rate_per_8h": ex.funding_rate_8h,
            "warning": "固定费率仅用于敏感性对照，不代表历史真实成本",
        }

    # ---- v10 引擎红线（默认全关）：给 LLM 档补「结构止损 + 熊市降杠杆 + 离场冷却」----
    # 配置源优先级：CLI --v10-*（args.v10）> .env V10_*（CFG.V10_*）。两源都缺=关闭，旧路径零影响。
    _v10 = getattr(args, "v10", None) or {}
    if not _v10 and CFG.V10_ENABLED:
        _v10 = {"rl1": CFG.V10_RL1_MODE}
        if CFG.V10_RL1_COOL:
            _v10["rl1_cool"] = CFG.V10_RL1_COOL
        if CFG.V10_RL2:
            _v10["rl2"] = True
    rl1_mode = _v10.get("rl1")        # None / "breach"(收盘<EMA50即走) / "dd"(浮亏≥rl1_dd且破线)
    rl1_dd = float(_v10.get("rl1_dd", 5.0))
    rl1_cool = int(_v10.get("rl1_cool", 0))   # 红线①离场后禁 BUY 根数（0=不启用冷却）
    rl2_on = bool(_v10.get("rl2", False))     # 收盘<EMA50 的 BUY 压杠杆至 1x
    redline_cool_until = -1                   # 冷却截止根序号（含）
    if rl1_mode or rl2_on:
        logger.warning("v10 引擎红线开启：rl1_mode=%s rl1_dd=%.1f rl1_cool=%d rl2=%s",
                       rl1_mode, rl1_dd, rl1_cool, rl2_on)

    for i in range(start_idx, len(df)):
        row = df.iloc[i]
        ts = row["open_time"]
        price = float(row["close"])
        ema = float(row["ema_trend"]) if pd.notna(row["ema_trend"]) else None
        ema_slow = float(row["ema_slow"]) if pd.notna(row["ema_slow"]) else None
        macd_dif = float(row["macd_dif"]) if pd.notna(row["macd_dif"]) else None
        macd_dea = float(row["macd_dea"]) if pd.notna(row["macd_dea"]) else None
        atr_pct = float(row["atr_pct"]) if pd.notna(row["atr_pct"]) else None

        # ① 日期滚动（当日亏损熔断按北京时间日）
        ex.roll_day(ts, price)

        # ①.5 资金费率计提：历史模式逐结算点应用真实 rate；fixed 仅作敏感性对照。
        if funding_mode == "historical":
            while (funding_idx < len(funding_df)
                   and funding_df.iloc[funding_idx]["funding_time"] <= pd.Timestamp(ts)):
                fr = funding_df.iloc[funding_idx]
                mark = float(fr["mark_price"]) if pd.notna(fr["mark_price"]) and float(fr["mark_price"]) > 0 else price
                ex.apply_funding_rate(float(fr["funding_rate"]), mark, fr["funding_time"])
                funding_idx += 1
        elif funding_mode == "fixed":
            ex.accrue_funding(ts, price)

        # ② next_open：次根开盘先结算上根收盘挂起的动作（先平仓、后开/平仓指令）。
        #     模拟「收盘信号/收盘识别 → 次根开盘价才成交」的延迟交易者（含隔夜跳空损耗）。
        if _model == "next_open":
            _sr = ex.settle_pending(float(row["open"]), ts)
            if _sr and _sr["action"] not in ("HOLD", "CLOSE"):
                logger.info("[%s] %s @ %.2f（次根开盘） | %s | 保证金占比->%.2f%%",
                            str(ts)[:16], _sr["action"], float(row["open"]),
                            _sr["reason"], ex.position_pct(float(row["open"])))

        # ②.5 逐仓强平检查（价格反向波动超过 1/杠杆 → 全额保证金亏损）
        ex.check_liquidation(price, ts)

        # ③ 止损检查（按执行模型三档：intrabar=盘中极值击穿按止损线价即时成交；
        #     close/next_open=仅收盘价识别，close 按收盘价即时、next_open 挂起次根开盘）
        #    多头止损线在开仓价下方 → 看最低价 low；空头止损线在开仓价上方 → 看最高价 high。
        sl = ex.stop_level()
        if sl is not None and ex.position is not None:
            _long = ex.position["side"] == "long"
            if _model == "intrabar":
                _hit = (float(row["low"]) <= sl) if _long else (float(row["high"]) >= sl)
                if _hit:
                    ex.force_close(sl, ts, reason="止损触发（盘中极值击穿，按止损线价成交）")
            else:
                _hit = (price <= sl) if _long else (price >= sl)
                if _hit:
                    if _model == "close":
                        ex.force_close(price, ts, reason="止损触发（收盘价击穿止损线）")
                    else:  # next_open：收盘识别 → 次根开盘离场
                        ex.set_pending_exit(ts, "止损触发（收盘价击穿止损线）")
        # ③.5 移动止盈（intrabar=盘中击穿按触发线价；close/next_open=收盘回撤识别，
        #     next_open 命中挂起、次根开盘离场）＋ 权益回撤止损（同样 defer 化）
        if _model == "intrabar":
            ex.check_trailing_tp_intrabar(float(row["high"]), float(row["low"]), ts)
            ex.check_trailing_tp(price, ts)
        else:
            ex.check_trailing_tp(price, ts, defer=(_model == "next_open"))
        ex.check_stop_loss(price, ts, defer=(_model == "next_open"))

        # ③.7 v10 红线①：结构止损（默认关）—— 把规则引擎「破趋势线即离场」搬到 LLM 档引擎层。
        #     breach=收盘<EMA50 即走（无浮亏门槛，牛市顺势持仓价格恒在线上 → 零误伤）；
        #     dd=浮亏≥rl1_dd% 且 收盘<EMA50（宽松版，供对照）。命中→set_pending_exit，
        #     与 ④ 的「待平仓暂停决策」自然衔接（本根 LLM 不再开新单）。
        if (rl1_mode and ex.position is not None and ex.pending_exit is None
                and ema_slow is not None):
            _p = ex.position
            if _p["side"] == "long":
                _dd = (price - _p["entry_price"]) / _p["entry_price"] * 100.0
                _hit = ((rl1_mode == "breach" and price < float(ema_slow))
                        or (rl1_mode == "dd" and _dd <= -rl1_dd
                            and price < float(ema_slow)))
                if _hit:
                    _why = ("收盘<EMA50 结构转空" if rl1_mode == "breach"
                            else f"浮亏{_dd:.1f}%≥{rl1_dd}%且收盘<EMA50")
                    ex.set_pending_exit(ts, f"v10红线①:{_why}，引擎强制离场")
                    ex.stats["v10_rl1"] += 1
                    if rl1_cool > 0:
                        redline_cool_until = i + rl1_cool
            # 空头镜像（预留：ALLOW_SHORT 场景）
            else:
                _dd = (_p["entry_price"] - price) / _p["entry_price"] * 100.0
                _hit = ((rl1_mode == "breach" and price > float(ema_slow))
                        or (rl1_mode == "dd" and _dd <= -rl1_dd
                            and price > float(ema_slow)))
                if _hit:
                    _why = ("收盘>EMA50 结构转空" if rl1_mode == "breach"
                            else f"空头浮亏{_dd:.1f}%≥{rl1_dd}%且收盘>EMA50")
                    ex.set_pending_exit(ts, f"v10红线①:{_why}，引擎强制离场")
                    ex.stats["v10_rl1"] += 1
                    if rl1_cool > 0:
                        redline_cool_until = i + rl1_cool

        # ④ 决策（LLM 或规则引擎）
        if _model == "next_open" and ex.pending_exit is not None:
            # 已有待执行平仓：本根暂停决策（也省一次 LLM 调用），避免与次根开盘离场叠加新单
            result = {"action": "HOLD", "filled_qty_pct": 0.0,
                      "reason": "次根开盘待平仓，本根暂停决策", "price": price}
        else:
            snap = ex.snapshot(price)
            rec: dict | None = None
            if replay_mode:
                key = str(ts)[:16]
                saved = decision_replay.get(key)
                decision = (dict(saved) if isinstance(saved, dict) else
                            {"action": "HOLD", "quantity_percent": 0.0, "leverage": 1,
                             "confidence_level": "低", "stop_loss_price": None,
                             "reasoning_summary": "源信号流该时点无决策，离线重放HOLD"})
                rec = {"ts": key, "source": "offline_saved_llm_decision",
                       "decision": dict(decision), "source_signal_present": isinstance(saved, dict)}
                repro["llm_calls"].append(rec)
            elif use_llm:
                row_ctx = row.to_dict()
                row_ctx["_recent"] = _recent_rows(df, end_idx=i)
                messages = prompt_builder.build_full_prompt(
                    row_ctx,
                    position=snap["position_pct"],
                    pnl=snap["pnl_pct"],
                    total_trades=snap["n_trades"],
                    daily_pnl=snap["daily_pnl_pct"],
                    equity=snap["equity"], cash=snap["cash"],
                    entry_price=snap["entry_price"],
                    force_hold_reason=_hard_block_text(row_ctx, snap),
                    max_leverage=ex.max_leverage,
                    cur_leverage=snap["leverage"],
                    liq_price=snap["liq_price"],
                    stop_price=snap.get("stop_price"),
                )
                # 可复现快照：prompt 全文 sha1（prompt 不落盘省空间，hash 可校验复现一致性）
                if repro is not None:
                    psha = hashlib.sha1(
                        json.dumps(messages, ensure_ascii=False).encode("utf-8")).hexdigest()
                    rec = {"ts": str(ts)[:16], "prompt_sha1": psha}
                    repro["llm_calls"].append(rec)
                decision = llm_client.call_llm(messages, provider=CFG.LLM_PROVIDER,
                                               max_position_pct=CFG.MAX_POSITION_PERCENT,
                                               max_leverage=ex.max_leverage,
                                               capture=rec)
            else:
                decision = llm_client.rule_based_decision(row.to_dict(), snap)

            # ④.5 v10 红线（默认关）：冷却期禁 BUY（堵「红线砍后 LLM 重新接刀」）
            #     + 收盘<EMA50 的 BUY 压杠杆至 1x（与规则引擎熊市 1x 对齐）。原地改 decision。
            if (rl1_cool > 0 or rl2_on) and decision.get("action") == "BUY":
                if rl1_cool > 0 and i <= redline_cool_until:
                    ex.stats["v10_cool_block"] += 1
                    logger.info("[%s] v10冷却吞BUY：%s", str(ts)[:16],
                                str(decision.get("reasoning_summary", ""))[:60])
                    decision["action"] = "HOLD"
                    decision["quantity_percent"] = 0.0
                    decision["filled_qty_pct"] = 0.0
                    decision["reasoning_summary"] = (
                        f"v10红线①冷却期（离场后{rl1_cool}根内禁再开仓），BUY→HOLD"
                        f"（{decision.get('reasoning_summary', '')}）")
                elif (rl2_on and ema_slow is not None
                      and price < float(ema_slow)):
                    _lev = decision.get("leverage")
                    if _lev not in (None, 1):
                        ex.stats["v10_rl2"] += 1
                        logger.info("[%s] v10红线②压杠杆 %s→1x（收盘<EMA50）",
                                    str(ts)[:16], _lev)
                        decision["leverage"] = 1

            decision = apply_position_sizing(
                decision, ex, price, ema_slow=ema_slow,
                macd_dif=macd_dif, macd_dea=macd_dea, market=row.to_dict())
            if (use_llm or replay_mode) and rec is not None:
                rec["post_guardrail_decision"] = dict(decision)

            result = ex.execute_decision(decision, price, ts,
                                         ema_slow=ema_slow,
                                         macd_dif=macd_dif, macd_dea=macd_dea,
                                         atr_pct=atr_pct,
                                         defer=(_model == "next_open"))
        # ⑤ 收盘后再查一次强平与止损（同一根K线内建仓后立即校验）
        ex.check_liquidation(price, ts)
        ex.check_stop_loss(price, ts, defer=(_model == "next_open"))
        ex.check_trailing_tp(price, ts, defer=(_model == "next_open"))
        # ⑤.5 刷新历史峰值权益（供下一根K线的回撤熔断计算）
        ex.update_peak(price)

        # 成交日志：CLOSE 已在 force_close 内部统一打印（含本次/累计盈亏），此处避免重复；
        # deferred 结果仅为占位（真实成交已由次根 settle_pending 打日志），跳过避免假成交。
        if result["action"] not in ("HOLD", "CLOSE") and not result.get("deferred"):
            logger.info("[%s] %s @ %.2f | %s | 保证金占比->%.2f%%",
                        str(ts)[:16], result["action"], price,
                        result["reason"], ex.position_pct(price))

        # ⑥ 记录权益曲线
        equity_curve.append((str(ts)[:16], ex.mark_to_market(price)))
        prices.append(price)
        eq_now = ex.mark_to_market(price)
        if ex.position is not None and eq_now > 0:
            exposure_curve.append(abs(ex.position["qty"] * price) / eq_now * 100.0)
            leverage_curve.append(float(ex.position.get("leverage", 0.0)))
        else:
            exposure_curve.append(0.0)

    # ---- 3) 绩效报告 ----
    if repro is not None:
        repro["cfg"] = _cfg_snapshot()
        repro["data"] = _data_fingerprint(df, start_idx)
        active_exp = [x for x in exposure_curve if x > 0]
        exposure_stats = {
            "time_in_market_pct": (len(active_exp) / len(exposure_curve) * 100.0
                                   if exposure_curve else 0.0),
            "avg_notional_exposure_pct": (sum(exposure_curve) / len(exposure_curve)
                                           if exposure_curve else 0.0),
            "avg_exposure_when_in_market_pct": (sum(active_exp) / len(active_exp)
                                                 if active_exp else 0.0),
            "max_notional_exposure_pct": max(exposure_curve) if exposure_curve else 0.0,
            "avg_leverage_when_in_market": (sum(leverage_curve) / len(leverage_curve)
                                             if leverage_curve else 0.0),
        }
        repro["exposure"] = {k: round(v, 6) for k, v in exposure_stats.items()}
        ex.backtest_exposure_stats = exposure_stats
        path_diag = compute_path_diagnostics(
            equity_curve, prices, exposure_curve, ex)
        repro["path_diagnostics"] = {k: round(v, 6) for k, v in path_diag.items()}
        ex.backtest_path_diagnostics = path_diag
    decision_label = getattr(args, "decision_label", None)
    report = build_backtest_report(df, ex, equity_curve, start_idx, use_llm,
                                   truncated=truncated, repro=repro,
                                   decision_label=decision_label)
    logger.info("\n%s", report["text"])

    # 保存报告与权益曲线到项目目录
    try:
        out_md = CFG.BASE_DIR / "backtest_report.md"
        out_md.write_text(report["markdown"], encoding="utf-8")
        if repro is not None:
            rp = _dump_repro_json(repro)
            if rp:
                logger.info("可复现快照已保存: %s（LLM 原始响应 %d 条）",
                            rp, len(repro["llm_calls"]))
        pd.DataFrame(equity_curve, columns=["time", "equity"]).to_csv(
            CFG.BASE_DIR / "backtest_equity.csv", index=False, encoding="utf-8-sig")
        # 供 GUI 内嵌图表：权益 + 价格（买入持有线由 GUI 以首行价为基准绘制）
        curve_path = _curve_csv_path("curve_backtest.csv")
        pd.DataFrame({"time": [t for t, _ in equity_curve],
                      "equity": [e for _, e in equity_curve],
                      "price": prices}).to_csv(
            curve_path, index=False, encoding="utf-8-sig")
        logger.info("报告已保存: %s\n权益曲线: %s\nGUI曲线: %s",
                    out_md, CFG.BASE_DIR / "backtest_equity.csv", curve_path)
    except OSError as e:
        logger.error("报告文件保存失败: %s", e)

    # 可选：绘制权益曲线图（需 matplotlib，缺失时自动跳过）
    if args.plot:
        png_path = plot_equity_curve(equity_curve, CFG.BASE_DIR / "backtest_equity_chart.png",
                                     trades=ex.trades)
        if png_path:
            logger.info("权益曲线图已保存: %s", png_path)


def build_backtest_report(df, ex: OrderExecutor, equity_curve: list,
                          start_idx: int, use_llm: bool,
                          truncated: bool = False,
                          repro: dict | None = None,
                          decision_label: str | None = None) -> dict:
    """汇总回测绩效并生成文本/Markdown 报告。"""
    perf = compute_performance(equity_curve)
    exposure = getattr(ex, "backtest_exposure_stats", {})
    path_diag = getattr(ex, "backtest_path_diagnostics", {})
    spot = ex.trading_mode == "spot"
    spot_sizing = str(getattr(CFG, "SPOT_SIZING_MODE", "confidence"))
    mode_text = (f"现货 long/flat（无杠杆；目标仓位≤{ex.max_position_percent():.0f}%；仓位模式={spot_sizing}）"
                 if spot else
                 f"合约杠杆: 1~{ex.max_leverage}倍做多（逐仓保证金，LLM自主选择；单仓保证金≤{CFG.MAX_POSITION_PERCENT}%）")
    engine_text = decision_label or ("真实LLM" if use_llm else "内置规则引擎")
    symbol, interval = CFG.SYMBOL, CFG.INTERVAL.upper()   # 报告标题标注标的/周期
    wins = [t for t in ex.closed_trades if (t.get("pnl_pct") or 0) > 0]
    losses = [t for t in ex.closed_trades if (t.get("pnl_pct") or 0) <= 0]
    gross_win = sum(t.get("realized_usdt") or 0 for t in wins)
    gross_loss = abs(sum(t.get("realized_usdt") or 0 for t in losses))
    win_rate = len(wins) / len(ex.closed_trades) * 100 if ex.closed_trades else 0.0
    profit_factor = gross_win / gross_loss if gross_loss > 1e-9 else (float("inf") if gross_win > 0 else 0.0)

    lines = [
        "",
        "================ 回测绩效报告 ================",
        f"区间: {df.iloc[start_idx]['open_time']}  →  {df.iloc[-1]['open_time']}",
        f"K线总数: {len(df)}（预热跳过 {start_idx}，回放 {perf['bars']}）",
        f"决策引擎: {engine_text}",
        f"交易模式: {mode_text}",
        f"初始本金: {CFG.CAPITAL:,.0f} USDT",
        f"期末权益: {equity_curve[-1][1]:,.2f} USDT",
        f"总收益率: {perf['total_return']:+.2f}%",
        f"收益/最大回撤: {perf['calmar']:.2f}" if perf["calmar"] != float("inf")
        else "收益/最大回撤: ∞（零回撤正收益）",
        f"最大回撤: {perf['max_drawdown']:.2f}%",
        f"夏普比率: {perf['sharpe']:.2f}（年化基准 {perf['annual_bars']:.0f} 根/年）"
        if perf["sharpe"] is not None else "夏普比率: —（权益无波动）",
        f"成交动作: {len(ex.trades)} 笔"
        f"（BUY {ex.stats['buys']} / SELL+CLOSE {ex.stats['sells']}）",
        f"平仓回合: {len(ex.closed_trades)}（胜 {len(wins)} / 负 {len(losses)}）",
        f"胜率: {win_rate:.1f}%",
        f"盈利因子: {profit_factor:.2f}" if profit_factor != float("inf") else "盈利因子: ∞（无亏损回合）",
        f"累计已实现盈亏: {ex.realized_pnl:+,.2f} USDT",
        f"累计交易成本(手续费+滑点): {ex.stats['fees_paid']:,.2f} USDT",
        f"累计资金费率净成本: {ex.stats['funding_paid']:,.2f} USDT（正=支付，负=收取）",
        f"买入持有收益: {path_diag.get('buy_hold_return_pct', 0.0):+.2f}%",
        f"相对买入持有超额: {path_diag.get('excess_vs_buy_hold_pp', 0.0):+.2f}pp",
        f"上涨/下跌K线捕获率: {path_diag.get('up_bar_capture_pct', 0.0):.1f}% / "
        f"{path_diag.get('down_bar_capture_pct', 0.0):.1f}%",
        f"最差5% K线平均仓位: {path_diag.get('worst_5pct_bar_avg_exposure_pct', 0.0):.2f}%",
        f"累计换手/成本磨损: {path_diag.get('turnover_on_initial_capital_pct', 0.0):.1f}% / "
        f"{path_diag.get('cost_drag_pct', 0.0):.3f}%本金",
        f"时间在场率: {exposure.get('time_in_market_pct', 0.0):.1f}%",
        f"平均名义敞口: {exposure.get('avg_notional_exposure_pct', 0.0):.2f}%（全时段）",
        f"持仓时平均/最大敞口: {exposure.get('avg_exposure_when_in_market_pct', 0.0):.2f}% / "
        f"{exposure.get('max_notional_exposure_pct', 0.0):.2f}%",
        f"持仓时平均杠杆: {exposure.get('avg_leverage_when_in_market', 0.0):.2f}x",
        f"强平次数: {ex.stats['liquidated']}",
        "",
        "---- 风控拦截统计（防LLM幻觉防线）----",
        f"宪法拦截(长周期趋势线下方做多): {ex.stats['ema_intercept']}",
        f"LLM逆势做多审计(llm档): {ex.stats.get('llm_countertrend_buy', 0)}",
        f"当日亏损熔断拦截: {ex.stats['daily_block']}",
        f"峰值回撤熔断拦截: {ex.stats['overall_block']}",
        f"仓位硬顶拦截: {ex.stats['max_pos_block']}",
        f"单笔上限截断: {ex.stats['qty_capped']}",
        f"止损触发: {ex.stats['stop_loss']}",
        f"移动止盈触发: {ex.stats['take_profit']}",
        f"再入场闸门(拦截/顺势放行/乖离衰减): {ex.stats['reentry_block']} / "
        f"{ex.stats.get('reentry_trend_allow', 0)} / {ex.stats.get('chase_dev_decay', 0)}",
        f"禁止摊均价拦截: {ex.stats.get('avg_down_block', 0)}",
        f"保证金不足拦截: {ex.stats['no_cash_block']}",
        f"v10护栏(RL1/冷却吞BUY/RL2降杠杆): {ex.stats.get('v10_rl1', 0)} / "
        f"{ex.stats.get('v10_cool_block', 0)} / {ex.stats.get('v10_rl2', 0)}",
        "=============================================",
    ]
    if truncated:
        lines.insert(2, f"⚠️ 区间超过 {CFG.MAX_BACKTEST_BARS} 切片上限，已自动截取最近"
                        f" {CFG.MAX_BACKTEST_BARS} 根（起点自动后移）")
    text = "\n".join(lines)

    md = "\n".join([
        f"# 回测绩效报告 · {symbol} {interval}",
        "",
        f"- 区间：{df.iloc[start_idx]['open_time']} → {df.iloc[-1]['open_time']}",
        f"- K线：{len(df)} 根（预热 {start_idx}，回放 {perf['bars']}）",
        f"- 决策引擎：{engine_text}",
        f"- 交易模式：{mode_text}",
        f"- 初始本金：{CFG.CAPITAL:,.0f} USDT",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 期末权益 | {equity_curve[-1][1]:,.2f} USDT |",
        f"| 总收益率 | {perf['total_return']:+.2f}% |",
        f"| 收益/最大回撤 | {perf['calmar']:.2f} |" if perf["calmar"] != float("inf")
        else "| 收益/最大回撤 | ∞（零回撤正收益） |",
        f"| 最大回撤 | {perf['max_drawdown']:.2f}% |",
        f"| 夏普比率 | {perf['sharpe']:.2f}（年化基准 {perf['annual_bars']:.0f} 根/年） |"
        if perf["sharpe"] is not None else "| 夏普比率 | — |",
        f"| 成交动作 | {len(ex.trades)}（BUY {ex.stats['buys']} / SELL+CLOSE {ex.stats['sells']}） |",
        f"| 平仓回合 | {len(ex.closed_trades)}（胜 {len(wins)} / 负 {len(losses)}） |",
        f"| 期末持仓 | {'有' if ex.position is not None else '无'} |",
        f"| 胜率 | {win_rate:.1f}% |",
        f"| 盈利因子 | {profit_factor:.2f} |" if profit_factor != float("inf") else "| 盈利因子 | ∞ |",
        f"| 累计已实现盈亏 | {ex.realized_pnl:+,.2f} USDT |",
        f"| 累计交易成本(费+滑点) | {ex.stats['fees_paid']:,.2f} USDT |",
        f"| 累计资金费率净成本 | {ex.stats['funding_paid']:,.2f} USDT（正=支付，负=收取） |",
        f"| 买入持有收益 | {path_diag.get('buy_hold_return_pct', 0.0):+.2f}% |",
        f"| 相对买入持有超额 | {path_diag.get('excess_vs_buy_hold_pp', 0.0):+.2f}pp |",
        f"| 上涨 / 下跌K线捕获率 | {path_diag.get('up_bar_capture_pct', 0.0):.1f}% / "
        f"{path_diag.get('down_bar_capture_pct', 0.0):.1f}% |",
        f"| 最差5% K线平均仓位 | {path_diag.get('worst_5pct_bar_avg_exposure_pct', 0.0):.2f}% |",
        f"| 累计换手 / 成本磨损 | {path_diag.get('turnover_on_initial_capital_pct', 0.0):.1f}% / "
        f"{path_diag.get('cost_drag_pct', 0.0):.3f}%本金 |",
        f"| 时间在场率 | {exposure.get('time_in_market_pct', 0.0):.1f}% |",
        f"| 平均名义敞口 | {exposure.get('avg_notional_exposure_pct', 0.0):.2f}%（全时段） |",
        f"| 持仓时平均 / 最大敞口 | {exposure.get('avg_exposure_when_in_market_pct', 0.0):.2f}% / "
        f"{exposure.get('max_notional_exposure_pct', 0.0):.2f}% |",
        f"| 持仓时平均杠杆 | {exposure.get('avg_leverage_when_in_market', 0.0):.2f}x |",
        f"| 强平次数 | {ex.stats['liquidated']} |",
        f"| 移动止盈触发 | {ex.stats.get('take_profit', 0)} |",
        f"| 确认式加仓25 / 50 / 75 | {ex.stats.get('spot_confirmed_add_25', 0)} / "
        f"{ex.stats.get('spot_confirmed_add_50', 0)} / {ex.stats.get('spot_confirmed_add_75', 0)} |",
        f"| 确认式快线 / 慢线 / 信号减仓 | {ex.stats.get('spot_confirmed_reduce_fast', 0)} / "
        f"{ex.stats.get('spot_confirmed_reduce_slow', 0)} / "
        f"{ex.stats.get('spot_confirmed_reduce_signal', 0)} |",
        f"| 确认式清仓 / 风险预算压缩 | {ex.stats.get('spot_confirmed_exit', 0)} / "
        f"{ex.stats.get('spot_confirmed_risk_cap', 0)} |",
        f"| 确认式加仓阻断 盈利/趋势/ATR/间隔 | {ex.stats.get('spot_confirmed_block_profit', 0)} / "
        f"{ex.stats.get('spot_confirmed_block_trend', 0)} / "
        f"{ex.stats.get('spot_confirmed_block_atr', 0)} / "
        f"{ex.stats.get('spot_confirmed_block_interval', 0)} |",
        f"| 确认式加仓阻断 突破/回踩 | {ex.stats.get('spot_confirmed_block_breakout', 0)} / "
        f"{ex.stats.get('spot_confirmed_block_pullback', 0)} |",
        "",
        "## 风控拦截统计",
        "",
        f"- 宪法拦截（长周期趋势线下方做多）：**{ex.stats['ema_intercept']}** 次",
        f"- LLM逆势做多审计（llm档）：**{ex.stats.get('llm_countertrend_buy', 0)}** 次",
        f"- 当日亏损熔断：**{ex.stats['daily_block']}** 次",
        f"- 峰值回撤熔断：**{ex.stats['overall_block']}** 次",
        f"- 账户级回撤闸门（拦截 / 强制清仓）：**{ex.stats.get('account_dd_block', 0)}** / "
        f"**{ex.stats.get('account_dd_force', 0)}** 次（高水位永不因平仓重置，Codex #6）",
        f"- 仓位硬顶 / 单笔上限截断：**{ex.stats['max_pos_block']}** / **{ex.stats['qty_capped']}** 次",
        f"- 止损触发：**{ex.stats['stop_loss']}** 次",
        f"- 移动止盈触发：**{ex.stats['take_profit']}** 次",
        f"- 再入场闸门（拦截 / 顺势放行 / 乖离仓位衰减）：**{ex.stats['reentry_block']}** / "
        f"**{ex.stats.get('reentry_trend_allow', 0)}** / **{ex.stats.get('chase_dev_decay', 0)}** 次",
        f"- 禁止浮亏摊均价：**{ex.stats.get('avg_down_block', 0)}** 次",
        f"- 保证金不足：**{ex.stats['no_cash_block']}** 次",
        f"- 逐仓强平：**{ex.stats['liquidated']}** 次",
        f"- v10 护栏（RL1 / 冷却吞BUY / RL2降杠杆）：**{ex.stats.get('v10_rl1', 0)}** / "
        f"**{ex.stats.get('v10_cool_block', 0)}** / **{ex.stats.get('v10_rl2', 0)}** 次",
        f"- 现货信心仓位（试探 / 确认加仓 / 确认不足 / 低信心拒绝）："
        f"**{ex.stats.get('spot_probe', 0)}** / **{ex.stats.get('spot_add', 0)}** / "
        f"**{ex.stats.get('spot_confirm_block', 0)}** / **{ex.stats.get('spot_low_conf_block', 0)}** 次",
        f"- 现货降仓（部分减仓 / 高信心清仓 / 空仓禁开空）："
        f"**{ex.stats.get('spot_reduce', 0)}** / **{ex.stats.get('spot_conf_close', 0)}** / "
        f"**{ex.stats.get('spot_short_block', 0)}** 次",
        f"- 确认式加仓（25 / 50 / 75）：**{ex.stats.get('spot_confirmed_add_25', 0)}** / "
        f"**{ex.stats.get('spot_confirmed_add_50', 0)}** / "
        f"**{ex.stats.get('spot_confirmed_add_75', 0)}** 次",
        f"- 确认式降仓（快线 / 慢线 / 信号 / 清仓 / 风险预算压缩）："
        f"**{ex.stats.get('spot_confirmed_reduce_fast', 0)}** / "
        f"**{ex.stats.get('spot_confirmed_reduce_slow', 0)}** / "
        f"**{ex.stats.get('spot_confirmed_reduce_signal', 0)}** / "
        f"**{ex.stats.get('spot_confirmed_exit', 0)}** / "
        f"**{ex.stats.get('spot_confirmed_risk_cap', 0)}** 次",
        "",
        "> ⚠️ 本报告由模拟回测生成，仅用于策略研究，不构成投资建议。",
        "",
    ])
    if repro is not None:
        fp = repro.get("data") or {}
        cfg = repro.get("cfg") or {}
        n_llm = len(repro.get("llm_calls", []))
        funding = repro.get("funding") or {}
        md += "\n".join([
            "## 可复现信息（Codex #5）",
            "",
            f"- 复现 JSON：`backtest_repro.json`（配置快照全量 + 行情指纹 + LLM prompt hash/原始响应），"
            f"由 {repro.get('runtime', '?')} 于 {repro.get('generated_at', '?')} 生成",
            f"- 行情指纹：源 {fp.get('source_rows', '?')} 根（预热 {fp.get('warm_skipped', '?')}，"
            f"回放 {fp.get('replayed_rows', '?')}），{fp.get('first_ts', '?')} → {fp.get('last_ts', '?')}，"
            f"完整OHLCV sha256 = `{fp.get('ohlcv_sha256_full', '?')}`；"
            f"收盘价 sha1(尾50) = `{fp.get('close_sha1_tail50', '?')}`",
            f"- LLM 调用：{n_llm} 根已记录（规则引擎回测为 0）",
            f"- 执行模型：`{cfg.get('EXECUTION_MODEL', '?')}`（成交假设声明，Codex #2）",
            (f"- Funding：历史结算点 {funding.get('rows', 0)} 条，来源 `{funding.get('source', '?')}`，"
             f"sha256 = `{funding.get('sha256', '?')}`"
             if funding.get("mode") == "historical"
             else ("- Funding：无（现货模式不产生永续资金费率）"
                   if funding.get("mode") == "none"
                   else f"- Funding：固定费率 `{funding.get('rate_per_8h', '?')}` / 8h（仅敏感性对照）")),
            "",
        ])
    if truncated:
        md = md.replace(f"# 回测绩效报告 · {symbol} {interval}",
                        f"# 回测绩效报告 · {symbol} {interval}\n\n"
                        f"> ⚠️ 所选区间超过 {CFG.MAX_BACKTEST_BARS} 切片上限，"
                        f"已自动截取最近 {CFG.MAX_BACKTEST_BARS} 根（起点自动后移）。")
    return {"text": text, "markdown": md}


# ======================================================================
# 命令行入口
# ======================================================================
def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"LLM驱动的量化交易Agent（{CFG.SYMBOL} {CFG.INTERVAL.upper()} 切片，Paper Trading）")
    parser.add_argument("--mode", choices=["live", "backtest"], default="live",
                        help="运行模式：live=实时模拟(默认)；backtest=回测")
    parser.add_argument("--once", action="store_true",
                        help="live 模式下只执行一个切片后退出（用于联调）")
    parser.add_argument("--use-llm", action="store_true",
                        help="回测时逐根K线调用真实LLM（注意费用与耗时）")
    parser.add_argument("--plot", action="store_true",
                        help="回测结束后绘制权益曲线图（需安装matplotlib）")
    # ---- 通用化回测参数（generalized 策略自测入口，仅 backtest 档生效）----
    parser.add_argument("--symbol", default=None,
                        help="交易对（如 BTCUSDT/ETHUSDT/SOLUSDT，覆盖 .env SYMBOL，仅回测档）")
    parser.add_argument("--interval", default=None,
                        help="K线周期（如 4h/1d，覆盖 .env INTERVAL，仅回测档）")
    parser.add_argument("--start", default=None,
                        help="回测起始日 YYYY-MM-DD（覆盖 .env START_DATE）")
    parser.add_argument("--end", default=None,
                        help="回测截止日 YYYY-MM-DD（覆盖 .env BACKTEST_END；空=到最新）")
    # ---- v10 引擎护栏开关（generalized 策略护栏，见 STRATEGY_v10_guardrail.md）----
    parser.add_argument("--v10-rl1", choices=["breach", "dd"], default=None,
                        help="v10红线①结构止损模式：breach=收盘<EMA50 即强平（推荐，实跑验证）；"
                             "dd=浮亏≥阈值且破线才走（宽松对照，已证伪）")
    parser.add_argument("--v10-rl1-dd", type=float, default=None,
                        help="dd 模式浮亏触发阈值 %%（默认 5.0，仅 --v10-rl1 dd 时用）")
    parser.add_argument("--v10-rl1-cool", type=int, default=None,
                        help="红线①离场后禁 BUY 根数（默认 0=不冷却；实跑验证配置=20）")
    parser.add_argument("--v10-rl2", action="store_true",
                        help="v10红线②：收盘<EMA50 的 BUY 强制压杠杆至 1x（只许试探、禁止重仓抄底）")
    args = parser.parse_args()

    # 回测档：CLI 覆盖 CFG（⚠️ 须写实例属性 CFG.X=，config 为类属性+单例结构，改类不生效）
    if args.mode == "backtest":
        if args.symbol:
            CFG.SYMBOL = args.symbol.strip().upper()
        if args.interval:
            CFG.INTERVAL = args.interval.strip().lower()
        if args.start:
            CFG.START_DATE = args.start.strip()
        if args.end is not None:
            CFG.BACKTEST_END = args.end.strip() or None

    # v10 护栏组装为 run_backtest 认识的 dict（默认全关，与旧路径零影响）
    if args.v10_rl1:
        args.v10 = {"rl1": args.v10_rl1}
        if args.v10_rl1_dd is not None:
            args.v10["rl1_dd"] = args.v10_rl1_dd
        if args.v10_rl1_cool is not None:
            args.v10["rl1_cool"] = args.v10_rl1_cool
        args.v10["rl2"] = args.v10_rl2
    elif args.v10_rl2:
        args.v10 = {"rl2": True}

    setup_logging()

    try:
        if args.mode == "backtest":
            run_backtest(args)
        else:
            mode_live(args)
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception:
        logger.exception("程序发生未捕获异常")


if __name__ == "__main__":
    main()
