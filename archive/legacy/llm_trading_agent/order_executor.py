# -*- coding: utf-8 -*-
"""
order_executor.py —— 订单执行与风控模块（合约逐仓保证金模型）
==============================================================
职责：
    1. 解析 LLM/规则引擎给出的决策（BUY / SELL / HOLD / CLOSE + leverage）；
    2. 执行前强制风控（无论 LLM 说什么，脚本拥有最终执行权）：
       - 单次下单保证金 ≤ SINGLE_ORDER_PERCENT(5%)
       - 单仓保证金 ≤ MAX_POSITION_PERCENT(20%)
       - 峰值回撤 < -5% 时强制忽略 BUY（相对历史峰值，可恢复）
       - 当日亏损 < -2% 时熔断当天所有 BUY
       - 价格 < 长周期趋势线(EMA_SLOW) 且 action=BUY → 宪法拦截，改为 HOLD
       - MACD 空头排列(DIF≤DEA) 时拒绝开多（右侧交易确认，过滤诱多假反弹）
    3. check_stop_loss() 统一止损（默认 2%，按浮亏占权益比例触发）；
    3.5 check_trailing_tp() 移动止盈：持仓期记录最高价，从最高点回撤
        TRAILING_TP_RATIO(5%) 即止盈，浮盈达 TRAILING_TP_ACTIVATE(2%) 才激活；
    4. 逐仓强平：价格反向波动 ≥ (1/杠杆) 时该仓全额保证金亏损、逐仓隔离；
    5. 资金费率：每 8 小时按名义敞口计提固定费率，多头支付；
    6. 状态持久化到 state.json（现金/仓位/保证金/交易历史/统计）。

交易语义（合约逐仓保证金模型，重要）：
    - 每笔开仓使用「逐仓保证金」隔离，仓位用名义敞口（notional）= 保证金 × 杠杆 计量。
    - 保证金不占用其余账户现金的盈亏，但强平只影响本仓保证金（不穿仓、不影响其它仓位）。
    - 未实现盈亏 = 名义敞口 × 价格变动率，等价于保证金 × 杠杆 × 变动率。
    - 权益 equity = cash + Σ(逐仓未实现盈亏) + 已实现盈亏。
    - 单仓多头强平价 = 开仓价 × (1 - 1/杠杆 + LIQUIDATION_BUFFER)。
      价格跌破强平价 → 本仓全额保证金亏损（简化模型，不回收残值），仓位归零。

    默认配置（ALLOW_SHORT=false）为「合约多头」模型：
    - BUY  = 以指定杠杆开多 / 加仓（LLM 决定倍数，1~MAX_LEVERAGE）
    - SELL = 减多（平掉部分多头，释放保证金与盈亏）
    - CLOSE= 平掉全部多头仓位
    - 仓位百分比按「保证金占权益」计量（逐仓隔离，每仓保证金 ≤ 20% 权益）。

    可选做空（.env 设 ALLOW_SHORT=true 后）：
    - 空仓时 SELL = 开空（逐仓保证金同样隔离，方向反转），BUY = 回补平空。
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from config import CFG

logger = logging.getLogger(__name__)

VALID_ACTIONS = {"BUY", "SELL", "HOLD", "CLOSE"}
EPS = 1e-9   # 浮点比较容差


class OrderExecutor:
    """合约逐仓保证金模拟执行器：撮合、风控、保证金账本、强平、资金费率与持久化。"""

    def __init__(self, capital: float = None, state_path=None):
        """
        参数:
            capital:    模拟本金（USDT）；缺省取 CFG.CAPITAL
            state_path: 状态文件路径；None 表示不持久化（回测模式常用）
        """
        self.base_capital: float = float(capital if capital is not None else CFG.CAPITAL)
        # 兼容 str / Path 两种传参
        self.state_path = Path(state_path) if state_path is not None else None

        # ---- 交易选项 ----
        self.trading_mode: str = str(getattr(CFG, "TRADING_MODE", "spot")).strip().lower()
        if self.trading_mode not in ("spot", "futures"):
            raise ValueError(f"TRADING_MODE 非法: {self.trading_mode!r}，仅支持 spot/futures")
        # 是否允许做空：False = 纯合约多头；True = 允许 SELL 开空 / BUY 回补。
        self.allow_short: bool = (self.trading_mode == "futures"
                                  and bool(getattr(CFG, "ALLOW_SHORT", False)))

        # ---- 杠杆边界（LLM 在 [1, max_leverage] 内自主选择）----
        self.max_leverage: int = (1 if self.trading_mode == "spot" else
                                  max(1, min(10, int(getattr(CFG, "MAX_LEVERAGE", 10)))))
        self.min_leverage: int = int(getattr(CFG, "MIN_LEVERAGE", 1))

        # ---- 资金费率参数 ----
        self.funding_rate_8h: float = float(getattr(CFG, "FUNDING_RATE_PER_8H", 0.0001))
        self._last_funding_ts = None                  # 上次计提资金费率的时间戳

        # ---- 账户账本（现金 + 逐仓头寸）----
        self.cash: float = self.base_capital           # 可用现金（含未占用保证金）
        self.position = None                           # 当前单一逐仓头寸（dict 或 None）
        #   position 结构：
        #   {
        #     "side": "long"|"short",
        #     "margin": float,          # 逐仓保证金（USDT）
        #     "leverage": float,        # 杠杆倍数
        #     "entry_price": float,     # 开仓加权均价
        #     "qty": float,             # 合约数量（张数，= notional/entry_price）
        #     "notional": float,        # 名义敞口（USDT）= margin × leverage
        #     "liq_price": float,       # 强平价
        #     "stop_price": float,      # 止损线（锚定首仓开仓价，加仓不重置）
        #     "highest_price": float,   # 持仓期最高价（移动止盈跟踪基准）
        #     "tp_activated": bool,     # 移动止盈是否已激活（浮盈达到激活阈值后置 True）
        #   }
        self.realized_pnl: float = 0.0                # 已实现盈亏（USDT，累计）

        # ---- 止损冷却 / 追高拦截状态（同向平仓后禁止立刻追回）----
        self.last_exit: dict | None = None            # 最近一次同向离场记录 {"side","price","bar"}
        self._bar_counter: int = 0                    # 回测/预热中 execute_decision 每根K线调用一次的计时器

        # ---- next_open 执行模型（EXECUTION_MODEL=next_open）：当根收盘识别 → 次根开盘成交 ----
        self.pending_exit: dict | None = None         # 待执行平仓（止损/移动止盈/权益止损 收盘识别后挂起，等次根开盘价）
        self.pending_action: dict | None = None       # 待撮合的开/平仓决策（评估已通过，仅差成交时点）
        self.last_processed_bar: str | None = None    # live 防重复：最后一次完成决策的闭合K线 open_time

        # ---- 峰值权益（用于「回撤熔断」，替代永久性的累计亏损熔断）----
        self.peak_equity: float = self.base_capital   # 历史最高权益（滚动更新，用于回撤计算）
        # 账户级高水位（Codex #6，2026-09-03）：与 peak_equity 解耦。交易门控峰值在
        # 平仓/强平后会重置（防旧峰值永久锁死交易）；账户级回撤闸门用另一个永不因
        # 平仓重置的高水位 —— 连续亏损不会把账户回撤清零，真正实现「有界回撤」。
        self.account_peak_equity: float = self.base_capital

        # ---- 当日统计（按北京时间自然日滚动）----
        self.day_key: str | None = None
        self.day_start_equity: float = self.base_capital

        # ---- 交易记录 ----
        self.trades: list = []
        self.closed_trades: list = []                  # 每次平仓/减仓的已实现盈亏明细
        self.liquidations: list = []                   # 强平记录

        # ---- 风控拦截统计（用于回测报告）----
        self.stats = {
            "orders": 0,          # 实际成交笔数
            "ema_intercept": 0,   # 宪法拦截（engine 档：长周期趋势线下方做多 / 上方做空 + MACD空头确认）
            "llm_countertrend_buy": 0,  # llm 档：LLM 在价格<长周期趋势线时仍选择做多的次数（自主权审计）
            "daily_block": 0,     # 当日熔断拦截
            "overall_block": 0,   # 峰值回撤熔断拦截
            "account_dd_block": 0,  # 账户级回撤闸门拦截（新开仓被拒；Codex #6）
            "account_dd_force": 0,  # 账户级回撤闸门强制清仓次数
            "max_pos_block": 0,   # 仓位已满拦截
            "qty_capped": 0,      # 单笔超限被截断
            "no_cash_block": 0,   # 保证金不足拦截
            "avg_down_block": 0,  # 禁止浮亏摊均价拦截（加仓被拒）
            "no_pos_block": 0,    # 无持仓却要卖出
            "invalid_action": 0,  # 非法动作
            "stop_loss": 0,       # 止损触发次数
            "take_profit": 0,     # 移动止盈触发次数
            "reentry_block": 0,   # 止损冷却/追高拦截次数（同向平仓后禁止立刻追回）
            "reentry_trend_allow": 0,  # trend 档：顺势（价在趋势线上）回补被放行的次数（审计：主升浪未被误拦）
            "chase_dev_decay": 0,  # 高乖离追高仓位衰减次数（乖离>CHASE_DEV_START_PCT 时强制降敞口）
            "deferred": 0,        # next_open 模式下延迟到次根开盘成交的决策笔数
            "v10_rl1": 0,         # v10 结构止损触发
            "v10_cool_block": 0,  # v10 冷却期吞掉 BUY
            "v10_rl2": 0,         # v10 弱市降杠杆
            "spot_probe": 0,      # 现货首次试探仓
            "spot_add": 0,        # 现货确认后逐级加仓
            "spot_confirm_block": 0,  # 加仓确认不足
            "spot_low_conf_block": 0, # 低信心 BUY 被拒
            "spot_hysteresis_hold": 0,# 仓位变化过小不交易
            "spot_reduce": 0,     # 信心减仓
            "spot_conf_close": 0, # 高信心 SELL 直接清仓
            "spot_short_block": 0,# 空仓 SELL 禁开空
            "spot_fixed_entry": 0,# 固定仓位对照入场
            "spot_confirmed_add_25": 0,
            "spot_confirmed_add_50": 0,
            "spot_confirmed_add_75": 0,
            "spot_confirmed_reduce_fast": 0,
            "spot_confirmed_reduce_slow": 0,
            "spot_confirmed_reduce_signal": 0,
            "spot_confirmed_exit": 0,
            "spot_confirmed_risk_cap": 0,
            "spot_confirmed_block_profit": 0,
            "spot_confirmed_block_trend": 0,
            "spot_confirmed_block_atr": 0,
            "spot_confirmed_block_interval": 0,
            "spot_confirmed_block_breakout": 0,
            "spot_confirmed_block_pullback": 0,
            "liquidated": 0,      # 强平触发次数
            "funding_paid": 0.0,  # 资金费率净成本（USDT；正=支付，负=收取）
            "fees_paid": 0.0,     # 累计交易成本（手续费+滑点，USDT；Codex #3）
            "buys": 0,
            "sells": 0,
        }

    # ==================================================================
    # 保证金 / 估值账本
    # ==================================================================
    def _unrealized_pnl(self, price: float) -> float:
        """当前逐仓头寸的未实现盈亏（USDT）。无头寸返回 0。"""
        if self.position is None:
            return 0.0
        p = self.position
        if p["side"] == "long":
            return (price - p["entry_price"]) * p["qty"]
        else:
            return (p["entry_price"] - price) * p["qty"]

    def _trading_cost(self, notional: float) -> float:
        """单边交易成本 = (taker手续费 + 滑点) × 名义敞口；SIMULATE_COSTS=False 返回 0。

        由各成交现金点调用并计入 stats['fees_paid']。成本直接扣现金 → 权益即时反映，
        与逐仓账本自洽（开仓瞬间权益 = 本金 - 开仓成本，不再凭空守恒）。
        """
        if not getattr(CFG, "SIMULATE_COSTS", True) or notional <= 0:
            return 0.0
        fee_key = "SPOT_FEE_TAKER_PCT" if self.trading_mode == "spot" else "FEE_TAKER_PCT"
        fee_default = 0.001 if self.trading_mode == "spot" else 0.0004
        rate = float(getattr(CFG, fee_key, fee_default)) \
             + float(getattr(CFG, "SLIPPAGE_PCT", 0.0002))
        cost = notional * rate
        if cost > 0:
            self.stats["fees_paid"] += cost
        return cost

    def mark_to_market(self, price: float) -> float:
        """权益 = 现金 + 逐仓保证金 + 未实现盈亏（realized 不在此加）。

        账本守恒（Codex #1 修复，2026-09-03）：
        - 开仓时 cash -= margin（+手续费），保证金本金仍在头寸里，估值时加回 margin；
        - 平仓时 force_close 已把 margin + realized（-平仓费）并入 cash；
        - 因此 realized_pnl 若再计入权益，会被重复计算一次（曾致权益虚高 =
          本金 + 2×累计已实现）。realized_pnl 现在只作统计用途，不进权益。
        - 开仓瞬间（price=entry、无费）权益守恒：cash+margin = 初始本金。
        """
        if self.position is None:
            return self.cash
        p = self.position
        return self.cash + p["margin"] + self._unrealized_pnl(price)

    def position_pct(self, price: float) -> float:
        """当前逐仓「保证金」占总权益的百分比（0~100，逐仓隔离口径）。"""
        if self.position is None:
            return 0.0
        eq = self.mark_to_market(price)
        if eq <= EPS:
            return 0.0
        return self.position["margin"] / eq * 100.0

    def notional_pct(self, price: float) -> float:
        """当前名义敞口占权益的百分比（用于风控与展示）。"""
        if self.position is None:
            return 0.0
        eq = self.mark_to_market(price)
        if eq <= EPS:
            return 0.0
        return self.position["notional"] / eq * 100.0

    def exposure_pct(self, price: float) -> float:
        """当前按市价计算的真实名义敞口占权益百分比。"""
        if self.position is None:
            return 0.0
        eq = self.mark_to_market(price)
        if eq <= EPS:
            return 0.0
        return abs(float(self.position["qty"]) * float(price)) / eq * 100.0

    def max_position_percent(self) -> float:
        return (float(getattr(CFG, "SPOT_MAX_EXPOSURE_PERCENT", 75.0))
                if self.trading_mode == "spot" else float(CFG.MAX_POSITION_PERCENT))

    def single_order_percent(self) -> float:
        return (float(getattr(CFG, "SPOT_SINGLE_ORDER_PERCENT", 25.0))
                if self.trading_mode == "spot" else float(CFG.SINGLE_ORDER_PERCENT))

    def pnl_pct(self, price: float) -> float:
        """累计盈亏率(%) = (当前权益 / 初始本金 - 1) * 100。"""
        return (self.mark_to_market(price) / self.base_capital - 1.0) * 100.0

    def drawdown_pct(self, price: float) -> float:
        """回撤率(%) = (当前权益 / 历史峰值权益 - 1) * 100（恒 ≤ 0）。

        这是「回撤熔断」的判据：相对峰值回撤，而非相对初始本金。
        关键区别：账户创新高后，峰值上移，之前的亏损不再构成永久锁死，
        回撤熔断天然「可恢复」——这正是替代旧「累计亏损熔断」的原因。
        """
        eq = self.mark_to_market(price)
        if self.peak_equity <= EPS:
            return 0.0
        return (eq / self.peak_equity - 1.0) * 100.0

    def account_dd_pct(self, price: float) -> float:
        """账户级回撤率(%)：相对「永不因平仓重置的高水位」的回撤（恒 ≤ 0）。

        与 drawdown_pct 的区别：后者的峰值在平仓/强平后会被重置（交易门控，
        防旧峰值永久锁死交易），连续亏损之间回撤被清零、5% 熔断形同虚设
        （Codex #6 指出的漏洞）。本方法 = 账户级风控闸门判据，高水位只随
        创新高上移，绝不因平仓/强平重置。
        """
        eq = self.mark_to_market(price)
        if self.account_peak_equity <= EPS:
            return 0.0
        return (eq / self.account_peak_equity - 1.0) * 100.0

    def update_peak(self, price: float) -> None:
        """滚动刷新历史峰值权益（在每根K线收盘后调用，供回撤熔断计算）。

        双轨：peak_equity（交易门控，平仓可重置）+ account_peak_equity（账户级
        高水位，只随创新高上移，永不因平仓重置——Codex #6）。
        """
        eq = self.mark_to_market(price)
        if eq > self.peak_equity:
            self.peak_equity = eq
        if eq > self.account_peak_equity:
            self.account_peak_equity = eq

    def daily_pnl_pct(self, price: float) -> float:
        """当日盈亏率(%) = (当前权益 / 日初权益 - 1) * 100。"""
        if self.day_start_equity <= EPS:
            return 0.0
        return (self.mark_to_market(price) / self.day_start_equity - 1.0) * 100.0

    def stop_level(self) -> float | None:
        """返回当前止损触发价；无头寸返回 None。

        止损线锚定首仓开仓价（NO_AVERAGE_DOWN=true 时加仓不重置），
        避免「浮亏补仓摊均价 → 止损线被越摊越低 → 止损永不触发」的死循环。
        """
        if self.position is None:
            return None
        p = self.position
        if p.get("stop_price") is not None:
            return p["stop_price"]
        if p["side"] == "long":
            return p["entry_price"] * (1.0 - CFG.STOP_LOSS_RATIO)
        return p["entry_price"] * (1.0 + CFG.STOP_LOSS_RATIO)

    def _stop_price(self, side: str, price: float, atr_pct: float | None = None) -> float:
        """止损线双指标：max(固定 STOP_LOSS_RATIO, ATR_STOP_MULT × ATR_pct)。

        固定 2% 价格止损在日线上会被单日噪声反复扫损（追高买入后次日插针即 -2%）；
        ATR 随波动率伸缩——高波动期自动放宽止损距离避免噪声扫损，低波动期保持 2%
        保底不放大单笔风险。ATR_STOP_ENABLED=false 时退回纯固定止损。
        """
        ratio = CFG.STOP_LOSS_RATIO
        if getattr(CFG, "ATR_STOP_ENABLED", True) and atr_pct and float(atr_pct) > 0:
            atr_ratio = float(getattr(CFG, "ATR_STOP_MULT", 1.5)) * (float(atr_pct) / 100.0)
            ratio = max(ratio, atr_ratio)
        if side == "long":
            return price * (1.0 - ratio)
        return price * (1.0 + ratio)

    def _reentry_block(self, side: str, price: float,
                       ema_slow: float | None = None,
                       ema_slow_prev: float | None = None) -> str | None:
        """止损冷却 + 追高拦截：同向平仓后，冷却期内或追高幅度过大时禁止再开仓。

        现象根因：LLM「止损→当天/次日更高价追回→再止损」形成锯齿磨损（如 08-23
        止损@196.68 后当日追高 BUY@204.14）。此拦截强制系统平仓后先冷静 N 根K线，
        且现价不得显著高于上次离场价，打断「低卖高买」循环。

        REENTRY_GATE_MODE 两种语义（对齐 daily_stock_analysis「乖离分档+强势放宽」哲学）：
          price（旧/静态价锚，默认关闭）：现价 vs 上次平仓价 ×(1+prem)，永久锁定。
             缺陷=牛市里「止盈离场即永久禁入」，主升浪一口吃不到。
          trend（动态趋势锚）：判断核心从「上次平仓价」换成「趋势线 EMA_SLOW」——
             * 价格 ≥ 趋势线（顺势/强势区）：不拦，允许顺势回补吃主升浪
               （EMA50 动态跟随，价格涨均线涨，乖离不会永远超标，天然自回归）；
             * 价格 < 趋势线（弱势/逆势区）且高于上次平仓价 prem：仍拦，
               防「弱势反弹里追回」的锯齿磨损——只有当价格重新站上趋势线
               才是真正的右侧确认。
            冷却期两种模式都生效（POST_LOSS_COOLDOWN_BARS）。

        返回拦截原因字符串；放行返回 None。
        """
        le = self.last_exit
        if le is None or le.get("side") != side:
            return None
        cn = "多" if side == "long" else "空"
        bars_since = self._bar_counter - int(le.get("bar", 0))
        cds = int(getattr(CFG, "POST_LOSS_COOLDOWN_BARS", 0))
        if cds > 0 and bars_since < cds:
            return (f"止损冷却：距上次{cn}仓平仓仅 {bars_since}/{cds} 根K线，暂缓再开{cn}仓"
                    f"（上次离场价 {le['price']:.2f}）")
        mode = str(getattr(CFG, "REENTRY_GATE_MODE", "trend")).lower()
        prem = float(getattr(CFG, "REENTRY_MAX_PREMIUM", 0.02))

        if mode == "trend" and ema_slow is not None and ema_slow > 0:
            # 动态趋势锚：价格站上趋势线 = 顺势区 → 放行（无论上次离场价多低）
            if side == "long":
                if price >= ema_slow:
                    self.stats["reentry_trend_allow"] += 1
                    return None
            else:  # short：价格跌破趋势线 = 顺势区 → 放行
                if price <= ema_slow:
                    self.stats["reentry_trend_allow"] += 1
                    return None
            # 逆势区：仍用静态价锚防锯齿（弱势反弹追回）
            if prem > 0:
                if side == "long" and price > le["price"] * (1.0 + prem):
                    return (f"追高拦截：现价 {price:.2f} 仍在趋势线 {ema_slow:.2f} 下方"
                            f"（弱势区），且高于上次平仓价 {le['price']:.2f} 超 {prem*100:.0f}%，"
                            f"暂缓追{cn}——等重新站上趋势线再顺势回补")
                if side == "short" and price < le["price"] * (1.0 - prem):
                    return (f"追空拦截：现价 {price:.2f} 仍在趋势线 {ema_slow:.2f} 上方"
                            f"（弱势区），且低于上次平仓价 {le['price']:.2f} 超 {prem*100:.0f}%，"
                            f"暂缓追{cn}——等重新跌破趋势线再顺势回补")
            return None

        # 旧静态价锚模式（price / 无趋势线数据时回退）
        if prem > 0:
            if side == "long" and price > le["price"] * (1.0 + prem):
                return (f"追高拦截：现价 {price:.2f} 高于上次平仓价 {le['price']:.2f} "
                        f"超 {prem*100:.0f}%，暂缓追{cn}")
            if side == "short" and price < le["price"] * (1.0 - prem):
                return (f"追空拦截：现价 {price:.2f} 低于上次平仓价 {le['price']:.2f} "
                        f"超 {prem*100:.0f}%，暂缓追{cn}")
        return None

    def liq_price(self, side: str, entry: float, leverage: float) -> float:
        """计算强平价（简化模型：维持保证金率缓冲 = LIQUIDATION_BUFFER）。"""
        buf = float(getattr(CFG, "LIQUIDATION_BUFFER", 0.0))
        if side == "long":
            return entry * (1.0 - 1.0 / leverage + buf)
        return entry * (1.0 + 1.0 / leverage - buf)

    def _chase_dev_decay(self, side: str, price: float, qty: float,
                         leverage: int, ema_slow: float | None
                         ) -> tuple[float, int, bool]:
        """高乖离追高仓位衰减：现价相对趋势线乖离超阈值时不拦截、但强制降敞口。

        校准结论（2026-09，SOL 1d 主升浪实测 13 个真实买点）：乖离硬顶无法区分
        赢家/输家（11-15 @乖离+25% 仍赚 +5.4%，11-27 @+21% 深亏 -9.8%）——拦截
        会误杀最大赢单。真正共性是「宽止损 × 满仓」放大深亏，故按参考仓库
        daily_stock_analysis「强势可追但轻仓」哲学改为敞口衰减：
          side=long  : dev = price/ema_slow - 1      （追高：价格远高于趋势线）
          side=short : dev = 1 - price/ema_slow      （追空：价格远低于趋势线）
        dev 超 CHASE_DEV_START_PCT → 单仓保证金 × CHASE_DEV_DECAY_FACTOR、
        杠杆压到 ≤ CHASE_DEV_MAX_LEVERAGE，把宽止损下的深亏按比例缩小。

        返回 (qty, leverage, decayed)。只在新开仓决策根调用（skip_reentry=False），
        next_open 的 settle 不再二次衰减（存的就是衰减后尺寸）。
        """
        # 现货由 spot_policy 的离散目标仓位和确认式加仓统一控制，避免在此二次缩仓。
        if self.trading_mode == "spot":
            return qty, 1, False
        if not ema_slow or float(ema_slow) <= 0:
            return qty, leverage, False
        dev = (price / float(ema_slow) - 1.0) if side == "long" \
            else (1.0 - price / float(ema_slow))
        start = float(getattr(CFG, "CHASE_DEV_START_PCT", 8.0)) / 100.0
        if dev <= start:
            return qty, leverage, False
        factor = float(getattr(CFG, "CHASE_DEV_DECAY_FACTOR", 0.5))
        max_lev = int(getattr(CFG, "CHASE_DEV_MAX_LEVERAGE", 2))
        qty2 = max(0.0, float(qty) * factor)
        lev2 = min(int(leverage), max_lev)
        self.stats["chase_dev_decay"] += 1
        return qty2, lev2, True

    # ==================================================================
    # 日期滚动 / 资金费率计提
    # ==================================================================
    def roll_day(self, ts, price: float) -> None:
        """若 ts 的日期与当前记账日不同，则滚动新的一天并快照日初权益。"""
        day = self._day_str(ts)
        if self.day_key is None:
            self.day_key = day
            self.day_start_equity = self.mark_to_market(price)
            return
        if day != self.day_key:
            prev = self.day_key
            self.day_key = day
            self.day_start_equity = self.mark_to_market(price)
            logger.info("[账本] 交易日滚动 %s -> %s，日初权益 %.2f",
                        prev, day, self.day_start_equity)

    def accrue_funding(self, ts, price: float) -> None:
        """按持仓时长计提资金费率（每 8 小时一次，多头支付、空头收取）。

        简化：以「距上次计提经过的 8 小时整段数」计费，费率固定。
        """
        if self.trading_mode == "spot" or self.position is None or abs(self.funding_rate_8h) <= EPS:
            return
        if self._last_funding_ts is None:
            self._last_funding_ts = ts
            return
        dt = self._as_datetime(ts)
        dt0 = self._as_datetime(self._last_funding_ts)
        if dt is None or dt0 is None:
            return
        hours = (dt - dt0).total_seconds() / 3600.0
        periods = int(hours // 8)
        if periods <= 0:
            return
        p = self.position
        # 资金费按结算时点仓位市值计算；fee 可为负（负费率时收付方向反转）。
        fee = abs(p["qty"] * float(price)) * self.funding_rate_8h * periods
        if p["side"] == "long":
            self.cash -= fee                 # 多头支付
            self.stats["funding_paid"] += fee
        else:
            self.cash += fee                 # 空头收取（简化：同费率反向）
            self.stats["funding_paid"] -= fee
        self._last_funding_ts = dt0 + timedelta(hours=8 * periods)
        logger.debug("[资金费率] %d 个周期，%s %.4f USDT（名义 %.2f）",
                     periods, "支付" if p["side"] == "long" else "收取",
                     fee, p["notional"])

    def apply_funding_rate(self, rate: float, price: float, ts=None) -> float:
        """按一个真实结算时点应用历史 funding rate；返回账户净成本。

        返回值正数表示账户支付，负数表示账户收取。空仓时返回 0。
        """
        if self.trading_mode == "spot" or self.position is None:
            return 0.0
        rate = float(rate)
        if abs(rate) <= EPS:
            return 0.0
        p = self.position
        notional = abs(p["qty"] * float(price))
        signed_payment = notional * rate * (1.0 if p["side"] == "long" else -1.0)
        self.cash -= signed_payment
        self.stats["funding_paid"] += signed_payment
        logger.debug("[历史资金费率] %s rate=%+.8f，%s %.4f USDT（名义 %.2f）",
                     self._time_str(ts), rate,
                     "支付" if signed_payment >= 0 else "收取",
                     abs(signed_payment), notional)
        return signed_payment

    @staticmethod
    def _as_datetime(ts):
        """把 datetime / pd.Timestamp / str 归一化为 datetime。"""
        if isinstance(ts, datetime):
            return ts
        if hasattr(ts, "to_pydatetime"):
            try:
                return ts.to_pydatetime()
            except Exception:
                pass
        try:
            s = str(ts)[:19]
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _day_str(ts) -> str:
        if isinstance(ts, datetime):
            return ts.strftime("%Y-%m-%d")
        return str(ts)[:10]

    @staticmethod
    def _time_str(ts) -> str:
        if isinstance(ts, datetime):
            return ts.strftime("%Y-%m-%d %H:%M")
        return str(ts)[:16]

    # ==================================================================
    # 账户快照
    # ==================================================================
    def snapshot(self, price: float) -> dict:
        """返回当前账户状态快照 dict（供战情简报与决策引擎读取）。"""
        eq = self.mark_to_market(price)
        side = "flat"
        if self.position is not None:
            side = self.position["side"]
        return {
            "equity": eq,
            "cash": self.cash,
            "position_pct": (self.exposure_pct(price) if self.trading_mode == "spot"
                             else self.position_pct(price)),
            "margin_pct": self.position_pct(price),
            "notional_pct": self.notional_pct(price),
            "leverage": self.position["leverage"] if self.position else None,
            "liq_price": self.position["liq_price"] if self.position else None,
            "stop_price": self.stop_level(),
            "pnl_pct": self.pnl_pct(price),
            "daily_pnl_pct": self.daily_pnl_pct(price),
            "drawdown_pct": self.drawdown_pct(price),
            "account_dd_pct": self.account_dd_pct(price),
            "peak_equity": self.peak_equity,
            "account_peak_equity": self.account_peak_equity,
            "entry_price": self.position["entry_price"] if self.position else None,
            "btc": self.position["qty"] if self.position else 0.0,
            "side": side,
            "n_trades": len(self.trades),
        }

    # ==================================================================
    # 决策执行主入口（含全部强制风控）
    # ==================================================================
    def execute_decision(self, decision: dict, current_price: float,
                         timestamp, ema_200: float | None = None,
                         ema_slow: float | None = None,
                         macd_dif: float | None = None,
                         macd_dea: float | None = None,
                         atr_pct: float | None = None,
                         defer: bool = False,
                         skip_reentry: bool = False) -> dict:
        """
        执行一条决策（BUY/SELL/HOLD/CLOSE），前置执行全部强制风控。

        参数:
            decision:     决策字典（llm_client 标准结构，可含 leverage 字段）
            current_price: 当前成交价（最新K线收盘价）
            timestamp:     当前K线时间
            ema_200:      当前 EMA200 值（向后兼容，可为 None）
            ema_slow:     当前长周期趋势线值（EMA_TREND_SLOW_PERIOD，趋势方向判官）
            macd_dif:     当前 MACD DIF 值（右侧交易确认：多头排列要求 DIF>DEA）
            macd_dea:     当前 MACD DEA 值
            atr_pct:      当前 ATR14 占价格百分比（开仓止损线双指标：max(固定2%, ATR_MULT×ATR%)）
            defer:        True=next_open 执行模型：决策评估（含冷却/追高拦截）完成后不实际
                          撮合，把决策与指标上下文挂进 pending_action，由次根 settle_pending
                          按开盘价成交（模拟「收盘信号 → 次根才执行」的延迟交易者）。
            skip_reentry: True=结算已挂起的 pending_action 时跳过冷却/追高拦截（决策当根已
                          判定过，成交阶段不再重复拦——但隔夜跳空会如实反映为开盘价滑点）。

        返回:
            执行结果 dict：{"action", "filled_qty_pct", "reason", "price"}
        """
        self.roll_day(timestamp, current_price)
        if not defer:
            self._bar_counter += 1                   # 根计数器：defer 调用不计数（次根 settle 时 +1），保证每根恰好一次
        price = float(current_price)
        action = str(decision.get("action", "HOLD")).strip().upper()
        qty = float(decision.get("quantity_percent", 0.0) or 0.0)
        qty = max(0.0, qty)
        leverage = self._clamp_leverage(decision.get("leverage"))
        if self.trading_mode == "spot":
            leverage = 1
        reason = str(decision.get("reasoning_summary", "")).strip() or "无说明"

        if action not in VALID_ACTIONS:
            self.stats["invalid_action"] += 1
            logger.warning("[风控] 非法动作 %r 已降级为 HOLD", action)
            return self._result("HOLD", 0.0, f"非法动作{action}，降级为HOLD", price)

        # ==================== 熔断即强制清仓（脚本层硬风控，优先于任何动作） ====================
        # 累计或当日亏损触及熔断线时，不再只是拦截 BUY，而是主动平掉全部持仓，
        # 杜绝「浮亏仓被无限期死扛」导致回测失效。清仓后本轮动作直接返回，不继续撮合。
        forced = self._force_close_on_circuit_breaker(price, timestamp)
        if forced is not None:
            return forced

        # ==================== BUY：开多 / 回补平空 ====================
        if action == "BUY":
            if self.position is not None and self.position["side"] == "short":
                if defer:
                    return self._stash_pending("BUY", qty, leverage, reason, price,
                                               ema_slow, macd_dif, macd_dea, atr_pct)
                return self._cover_short(price, qty, timestamp, reason)
            if self.position is None and not skip_reentry:   # 仅新开多受冷却/追高拦截，加仓不受限
                blk = self._reentry_block("long", price, ema_slow=ema_slow)
                if blk is not None:
                    self.stats["reentry_block"] += 1
                    return self._result("HOLD", 0.0, blk, price)
                # 高乖离追高仓位衰减（放行≠满仓：乖离超阈值强制降敞口，不拦方向只缩风险）
                qty, leverage, _dec = self._chase_dev_decay(
                    "long", price, qty, leverage, ema_slow)
                if _dec:
                    reason = (f"{reason}（乖离过大，追高仓位自动衰减为 {qty:.1f}%"
                              f" / ≤{leverage}x）").strip()
            if defer:
                return self._stash_pending("BUY", qty, leverage, reason, price,
                                           ema_slow, macd_dif, macd_dea, atr_pct)
            return self._open_long(price, qty, leverage, timestamp, reason,
                                   ema_slow, macd_dif, macd_dea, atr_pct)

        # ==================== SELL：减多 / 开空 ====================
        if action == "SELL":
            if self.position is not None and self.position["side"] == "long":
                if defer:
                    return self._stash_pending("SELL", qty, leverage, reason, price,
                                               ema_slow, macd_dif, macd_dea, atr_pct)
                return self._reduce_long(price, qty, timestamp, reason)
            if self.position is None and not skip_reentry:   # 仅新开空受冷却/追空拦截
                blk = self._reentry_block("short", price, ema_slow=ema_slow)
                if blk is not None:
                    self.stats["reentry_block"] += 1
                    return self._result("HOLD", 0.0, blk, price)
                # 高乖离追空仓位衰减（空头镜像：价格远低于趋势线的追空同样降敞口）
                qty, leverage, _dec = self._chase_dev_decay(
                    "short", price, qty, leverage, ema_slow)
                if _dec:
                    reason = (f"{reason}（乖离过大，追空仓位自动衰减为 {qty:.1f}%"
                              f" / ≤{leverage}x）").strip()
            if defer:
                return self._stash_pending("SELL", qty, leverage, reason, price,
                                           ema_slow, macd_dif, macd_dea, atr_pct)
            return self._open_short(price, qty, leverage, timestamp, reason,
                                    ema_slow, atr_pct)

        # ==================== CLOSE：清仓 ====================
        if action == "CLOSE":
            if defer:
                return self._stash_pending("CLOSE", 0.0, leverage, reason, price,
                                           ema_slow, macd_dif, macd_dea, atr_pct)
            return self.force_close(price, timestamp, reason=f"CLOSE指令（{reason}）")

        # ==================== HOLD ====================
        return self._result("HOLD", 0.0, f"HOLD（{reason}）", price)

    # ==================================================================
    # next_open 执行模型：待办挂起与次根开盘结算
    # ==================================================================
    def _stash_pending(self, action: str, qty: float, leverage: int,
                       reason: str, price: float, ema_slow, macd_dif,
                       macd_dea, atr_pct) -> dict:
        """把已通过全部风控的决策挂进 pending_action，次根 settle 撮合。

        返回带 deferred 标记的占位 result（filled_qty_pct 仅提示，未成交）。
        """
        self.pending_action = {
            "action": action, "qty": qty, "leverage": leverage, "reason": reason,
            "ema_slow": ema_slow, "macd_dif": macd_dif,
            "macd_dea": macd_dea, "atr_pct": atr_pct,
        }
        self.stats["deferred"] += 1
        r = self._result(action, qty, f"{reason}（次根开盘执行）", price)
        r["deferred"] = True
        return r

    def set_pending_exit(self, timestamp, reason: str) -> None:
        """next_open：收盘识别到止损/止盈/权益止损 → 挂起，次根开盘价离场。已有待办则忽略。"""
        if self.pending_exit is None:
            self.pending_exit = {"ts": timestamp, "reason": reason}

    def settle_pending(self, open_price: float, timestamp) -> dict | None:
        """next_open：次根开盘结算上根挂起的动作（先平仓、后开/平决策）。每根调用一次。"""
        self._bar_counter += 1                        # next_open 根计数器：每根恰好一次
        res = None
        if self.pending_exit is not None:
            pe = self.pending_exit
            self.pending_exit = None
            if self.pending_action is not None:       # main 层短路保证不共存；残留时防御性丢弃
                logger.warning("[settle] 待平仓结算时发现残留开仓指令，已丢弃：%s",
                               self.pending_action.get("reason", "?"))
                self.pending_action = None
            res = self.force_close(open_price, timestamp,
                                   reason=pe["reason"] + "（次根开盘成交）")
        elif self.pending_action is not None:
            pa = self.pending_action
            self.pending_action = None
            # 决策根已跑过冷却/追高等全部拦截 → skip_reentry 不再重复拦；
            # 但熔断仍会重查（隔夜跳空若触发硬风控，真实也会放弃/平仓）。
            # _stash_pending 按字段存，execute_decision 要 dict → 这里重建决策结构。
            decision = {
                "action": pa["action"],
                "quantity_percent": pa["qty"],
                "leverage": pa["leverage"],
                "reasoning_summary": pa["reason"],
            }
            res = self.execute_decision(
                decision, open_price, timestamp,
                ema_slow=pa.get("ema_slow"), macd_dif=pa.get("macd_dif"),
                macd_dea=pa.get("macd_dea"), atr_pct=pa.get("atr_pct"),
                defer=False, skip_reentry=True)
        return res

    def _clamp_leverage(self, lev) -> int:
        """把 LLM 给出的杠杆倍数收敛到 [min_leverage, max_leverage] 的整数。"""
        try:
            lev = float(lev)
        except (TypeError, ValueError):
            return self.max_leverage
        if lev <= 0 or not (lev == lev):  # 非法/NaN → 默认 1x
            return 1
        lev = int(round(lev))
        return max(self.min_leverage, min(self.max_leverage, lev))

    # ==================================================================
    # 多头开仓 / 加仓
    # ==================================================================
    def _open_long(self, price: float, qty: float, leverage: int,
                   timestamp, reason: str, ema_slow: float | None,
                   macd_dif: float | None = None,
                   macd_dea: float | None = None,
                   atr_pct: float | None = None) -> dict:
        daily = self.daily_pnl_pct(price)
        dd = self.drawdown_pct(price)

        # ① 回撤熔断（相对历史峰值回撤，可恢复，替代旧的永久性累计亏损熔断）
        if dd <= -CFG.OVERALL_LOSS_LIMIT_PERCENT:
            self.stats["overall_block"] += 1
            return self._result("HOLD", 0.0,
                                f"峰值回撤{dd:.2f}%触发熔断，BUY→HOLD（回撤收窄后自动恢复）", price)
        # ①.5 账户级回撤闸门（Codex #6：高水位永不因平仓重置，连续亏损不会被清零）
        add = self.account_dd_pct(price)
        if add <= -float(getattr(CFG, "EQUITY_DRAWDOWN_LIMIT_PERCENT", 15.0)):
            self.stats["account_dd_block"] += 1
            return self._result("HOLD", 0.0,
                                f"账户级回撤{add:.2f}%触达永久停机闸门，BUY→HOLD（需人工审查后复位）", price)
        # ② 当日亏损熔断
        if daily <= -CFG.DAILY_LOSS_LIMIT_PERCENT:
            self.stats["daily_block"] += 1
            return self._result("HOLD", 0.0,
                                f"当日亏损{daily:.2f}%达熔断线，今日禁止开新仓", price)
        # ③ 宪法拦截：价格 < 长周期趋势线（EMA_SLOW）禁止做多
        #    用长周期均线（默认 EMA50）作为趋势方向判官，而非快线 EMA6——
        #    快线在下跌趋势中会被每次小反弹站上，导致「禁止逆势做多」失效、
        #    LLM 一路接飞刀。长周期均线平滑掉噪音，只在价格真正站上趋势基准
        #    上方才放行（右侧交易，不抄左侧）。
        #    v9 起该拦截按 TREND_FILTER_LEVEL 分支：
        #      - "engine"：引擎硬拦（历史行为，LLM 只能在规则窗口内做选择题）；
        #      - "llm"   ：引擎放行，趋势/右侧判断交给 LLM（战情简报含 K 线窗口）。
        #                  红线（熔断/仓位/摊均价/冷却/止损/强平）仍在本函数硬性执行。
        trend_level = str(getattr(CFG, "TREND_FILTER_LEVEL", "engine")).strip().lower()
        if trend_level == "engine":
            if ema_slow is not None and price < float(ema_slow):
                self.stats["ema_intercept"] += 1
                return self._result("HOLD", 0.0,
                                    f"宪法拦截：价格低于长周期趋势线({float(ema_slow):.0f})，禁止做多（右侧交易，不逆势抄底）", price)
            # ③.5 右侧交易确认：MACD 空头排列（DIF≤DEA）时，即使价格在趋势线上方
            #    也拒绝开多，过滤下跌末段的假突破/诱多反弹。
            if macd_dif is not None and macd_dea is not None and macd_dif <= macd_dea:
                self.stats["ema_intercept"] += 1
                return self._result("HOLD", 0.0,
                                    "右侧交易确认：MACD空头排列（DIF≤DEA），动能未转多，拒绝开多", price)
        else:
            # "llm"：引擎不拦趋势，但记录 LLM 在趋势线下方/动能未转多时仍选择做多的次数
            #        （供报告审计 LLM 是否滥用自主权——若次数畸高说明 LLM 判断不可靠）。
            if ema_slow is not None and price < float(ema_slow):
                self.stats.setdefault("llm_countertrend_buy", 0)
                self.stats["llm_countertrend_buy"] += 1
        # ④ 仓位硬顶：现货按实时市值敞口，合约沿用逐仓保证金口径。
        eq = self.mark_to_market(price)
        max_pct = self.max_position_percent()
        max_margin = eq * max_pct / 100.0
        if self.position is not None:
            current_for_cap = (abs(self.position["qty"] * price)
                               if self.trading_mode == "spot" else self.position["margin"])
            if current_for_cap >= max_margin - EPS:
                self.stats["max_pos_block"] += 1
                return self._result("HOLD", 0.0,
                                    f"本仓位已达{max_pct:.0f}%上限，拒加仓", price)
        # ⑤ 单笔下单上限。现货 quantity_percent 表示新增现货市值占权益比例。
        current_pct = self.exposure_pct(price) if self.trading_mode == "spot" else self.position_pct(price)
        allow = min(qty, self.single_order_percent(), max_pct - current_pct)
        if allow <= EPS:
            return self._result("HOLD", 0.0, "计算后可开仓保证金空间为0，拒买", price)
        if qty > allow + EPS:
            self.stats["qty_capped"] += 1
            logger.info("[风控] 建议%.1f%%，单笔仓位上限截断为%.1f%%", qty, allow)

        margin_add = allow / 100.0 * eq                 # 新增逐仓保证金
        if margin_add > self.cash + EPS:                # 保证金不足拦截
            self.stats["no_cash_block"] += 1
            return self._result("HOLD", 0.0, "可用保证金不足，拒买", price)

        # 计算新增名义敞口与合约数量
        notional_add = margin_add * leverage
        qty_add = notional_add / price

        if self.position is None:
            # 新开仓
            self.position = {
                "side": "long",
                "margin": margin_add,
                "leverage": float(leverage),
                "entry_price": price,
                "qty": qty_add,
                "notional": notional_add,
                "liq_price": self.liq_price("long", price, leverage),
                "stop_price": self._stop_price("long", price, atr_pct),
                "highest_price": price,
                "tp_activated": False,
                "last_add_bar": self._bar_counter,
            }
            # 新仓从实际开仓时刻开始累计，不能把此前空仓期计入 funding。
            self._last_funding_ts = timestamp
        else:
            # 加仓：禁止摊均价（NO_AVERAGE_DOWN）
            p = self.position
            if getattr(CFG, "NO_AVERAGE_DOWN", True) and price <= p["entry_price"]:
                # 当前价不高于持仓均价 → 浮亏补仓，拒绝（防止摊低成本架空止损）
                self.stats["avg_down_block"] += 1
                return self._result("HOLD", 0.0,
                                    f"禁止浮亏摊均价：当前价{price:.2f}≤持仓均价{p['entry_price']:.2f}，拒加仓",
                                    price)
            new_margin = p["margin"] + margin_add
            new_qty = p["qty"] + qty_add
            p["entry_price"] = (p["entry_price"] * p["qty"] + price * qty_add) / new_qty
            p["qty"] = new_qty
            p["margin"] = new_margin
            p["notional"] = p["notional"] + notional_add
            p["leverage"] = p["notional"] / new_margin
            p["last_add_bar"] = self._bar_counter
            p["liq_price"] = self.liq_price("long", p["entry_price"], p["leverage"])
            # 关键：止损线保持锚定首仓开仓价（不随加仓下移）
            if p.get("stop_price") is None:
                p["stop_price"] = p["entry_price"] * (1.0 - CFG.STOP_LOSS_RATIO)

        self.cash -= margin_add                          # 保证金从现金划出
        self.cash -= self._trading_cost(notional_add)    # 开仓成本（费+滑点，Codex #3）
        filled_pct = margin_add / eq * 100.0
        self._record_trade("BUY", price, filled_pct, notional_add, timestamp,
                           f"{reason}（{leverage}x）", qty_override=qty_add)
        return self._result("BUY", filled_pct,
                            f"开多{margin_add:.2f}保证金@{leverage}x（{reason}）", price)

    # ==================================================================
    # 多头减仓 / 平仓
    # ==================================================================
    def _reduce_long(self, price: float, qty: float, timestamp, reason: str) -> dict:
        p = self.position
        eq = self.mark_to_market(price)
        # 减仓：quantity_percent 表示「平掉名义敞口占权益的比例」，最多全平
        current_notional = (abs(p["qty"] * price)
                            if self.trading_mode == "spot" else p["notional"])
        reduce_notional = min(qty / 100.0 * eq, current_notional)
        reduce_fraction = reduce_notional / current_notional if current_notional > EPS else 0.0
        reduce_qty = p["qty"] * reduce_fraction
        if reduce_qty <= EPS:
            return self._result("HOLD", 0.0, "无可减多头", price)

        realized = (price - p["entry_price"]) * reduce_qty   # 本段已实现盈亏
        self.realized_pnl += realized
        block_pnl_pct = (price / p["entry_price"] - 1.0) * 100.0
        entry_before = p["entry_price"]

        # 释放等比例保证金回现金
        release_margin = p["margin"] * reduce_fraction
        self.cash += release_margin + realized
        self.cash -= self._trading_cost(price * reduce_qty)   # 减仓成本（按平仓名义额）

        p["qty"] -= reduce_qty
        p["margin"] -= release_margin
        p["notional"] = p["margin"] * p["leverage"]
        lev_backup = p["leverage"]
        if p["qty"] <= EPS:
            self.position = None
            self._last_funding_ts = None
        else:
            p["liq_price"] = self.liq_price("long", p["entry_price"], p["leverage"])

        self.stats["sells"] += 1
        self.closed_trades.append({
            "time": self._time_str(timestamp),
            "action": "SELL",
            "price": round(price, 2),
            "entry": round(entry_before, 2),
            "leverage": round(lev_backup, 1),
            "pnl_pct": round(block_pnl_pct, 3),
            "realized_usdt": round(realized, 2),
        })
        self._record_trade("SELL", price, qty, reduce_notional, timestamp, reason,
                           qty_override=reduce_qty)
        return self._result("SELL", qty, f"减多成交（{reason}）", price)

    # ==================================================================
    # 做空辅助（仅 ALLOW_SHORT=true 时可达）
    # ==================================================================
    def _open_short(self, price: float, qty: float, leverage: int,
                    timestamp, reason: str, ema_slow: float | None,
                    atr_pct: float | None = None) -> dict:
        if not self.allow_short:
            self.stats["no_pos_block"] += 1
            return self._result("HOLD", 0.0,
                                "无多头可减，SELL→HOLD（ALLOW_SHORT=false 未开启做空）", price)

        dd = self.drawdown_pct(price)
        daily = self.daily_pnl_pct(price)
        if dd <= -CFG.OVERALL_LOSS_LIMIT_PERCENT:
            self.stats["overall_block"] += 1
            return self._result("HOLD", 0.0,
                                f"峰值回撤{dd:.2f}%触发熔断，SELL开空→HOLD（回撤收窄后自动恢复）", price)
        # ①.5 账户级回撤闸门（空头镜像）
        add = self.account_dd_pct(price)
        if add <= -float(getattr(CFG, "EQUITY_DRAWDOWN_LIMIT_PERCENT", 15.0)):
            self.stats["account_dd_block"] += 1
            return self._result("HOLD", 0.0,
                                f"账户级回撤{add:.2f}%触达永久停机闸门，SELL开空→HOLD（需人工审查后复位）", price)
        if daily <= -CFG.DAILY_LOSS_LIMIT_PERCENT:
            self.stats["daily_block"] += 1
            return self._result("HOLD", 0.0, f"当日亏损{daily:.2f}%达熔断线，禁止开空", price)
        if ema_slow is not None and price > float(ema_slow):
            self.stats["ema_intercept"] += 1
            return self._result("HOLD", 0.0,
                                f"宪法拦截：价格高于长周期趋势线({float(ema_slow):.0f})，禁止做空", price)

        eq = self.mark_to_market(price)
        max_margin = eq * CFG.MAX_POSITION_PERCENT / 100.0
        if self.position is not None and self.position["margin"] >= max_margin - EPS:
            self.stats["max_pos_block"] += 1
            return self._result("HOLD", 0.0, "本仓保证金已达上限，拒开空", price)

        allow = min(qty, float(CFG.SINGLE_ORDER_PERCENT),
                    CFG.MAX_POSITION_PERCENT - self.position_pct(price))
        if allow <= EPS:
            return self._result("HOLD", 0.0, "可开空保证金空间为0，拒开空", price)
        if qty > allow + EPS:
            self.stats["qty_capped"] += 1

        margin_add = allow / 100.0 * eq
        if margin_add > self.cash + EPS:
            self.stats["no_cash_block"] += 1
            return self._result("HOLD", 0.0, "可用保证金不足，拒开空", price)

        notional_add = margin_add * leverage
        qty_add = notional_add / price
        if self.position is None:
            self.position = {
                "side": "short",                "margin": margin_add,
                "leverage": float(leverage),
                "entry_price": price,
                "qty": qty_add,
                "notional": notional_add,
                "liq_price": self.liq_price("short", price, leverage),
                "stop_price": self._stop_price("short", price, atr_pct),
                "highest_price": price,
                "tp_activated": False,
            }
            self._last_funding_ts = timestamp
        else:
            p = self.position
            if getattr(CFG, "NO_AVERAGE_DOWN", True) and price >= p["entry_price"]:
                self.stats["avg_down_block"] += 1
                return self._result("HOLD", 0.0,
                                    f"禁止摊均价：当前价{price:.2f}≥持仓均价{p['entry_price']:.2f}，拒加空",
                                    price)
            new_margin = p["margin"] + margin_add
            new_qty = p["qty"] + qty_add
            p["entry_price"] = (p["entry_price"] * p["qty"] + price * qty_add) / new_qty
            p["qty"] = new_qty
            p["margin"] = new_margin
            p["notional"] = p["notional"] + notional_add
            p["leverage"] = p["notional"] / new_margin
            p["liq_price"] = self.liq_price("short", p["entry_price"], p["leverage"])
            if p.get("stop_price") is None:
                p["stop_price"] = p["entry_price"] * (1.0 + CFG.STOP_LOSS_RATIO)
        self.cash -= margin_add
        self.cash -= self._trading_cost(notional_add)    # 开空成本（费+滑点）
        filled_pct = margin_add / eq * 100.0
        self.stats["sells"] += 1
        self._record_trade("SELL", price, filled_pct, notional_add, timestamp,
                           f"{reason}（开空 {leverage}x）", qty_override=qty_add)
        return self._result("SELL", filled_pct,
                            f"开空{margin_add:.2f}保证金@{leverage}x（{reason}）", price)

    def _cover_short(self, price: float, qty: float, timestamp, reason: str) -> dict:
        """BUY 回补平空：平掉部分/全部空头。"""
        if self.position is None or self.position["side"] != "short":
            return self._result("HOLD", 0.0, "无空头可回补", price)
        p = self.position
        eq = self.mark_to_market(price)
        cover_notional = min(qty / 100.0 * eq, p["notional"])
        cover_fraction = cover_notional / p["notional"] if p["notional"] > EPS else 0.0
        cover_qty = p["qty"] * cover_fraction
        if cover_qty <= EPS:
            return self._result("HOLD", 0.0, "无可回补空头", price)

        realized = (p["entry_price"] - price) * cover_qty   # 空头盈利：价格低于开仓价为正
        self.realized_pnl += realized
        entry_before = p["entry_price"]
        pnl_pct = (entry_before - price) / entry_before * 100.0

        release_margin = p["margin"] * cover_fraction
        self.cash += release_margin + realized
        self.cash -= self._trading_cost(price * cover_qty)   # 回补成本（按平仓名义额）
        p["qty"] -= cover_qty
        p["margin"] -= release_margin
        p["notional"] = p["margin"] * p["leverage"]
        lev_backup = p["leverage"]
        if p["qty"] <= EPS:
            self.position = None
            self._last_funding_ts = None
        else:
            p["liq_price"] = self.liq_price("short", p["entry_price"], p["leverage"])

        filled_pct = cover_notional / eq * 100.0
        self.closed_trades.append({
            "time": self._time_str(timestamp),
            "action": "COVER",
            "price": round(price, 2),
            "entry": round(entry_before, 2),
            "pnl_pct": round(pnl_pct, 3),
            "realized_usdt": round(realized, 2),
        })
        self._record_trade("BUY", price, filled_pct, cover_notional, timestamp,
                           f"{reason}（回补平空）", qty_override=cover_qty)
        return self._result("BUY", filled_pct, f"空头回补（{reason}）", price)

    # ==================================================================
    # 清仓 / 强平 / 止损
    # ==================================================================
    def force_close(self, price: float, timestamp, reason: str = "清仓") -> dict:
        """以指定价格平掉全部头寸（CLOSE 指令与止损共用，多/空通吃）。"""
        if self.position is None:
            return self._result("HOLD", 0.0, "已无持仓，无需清仓", price)

        p = self.position
        if p["side"] == "long":
            realized = (price - p["entry_price"]) * p["qty"]
        else:
            realized = (p["entry_price"] - price) * p["qty"]
        self.realized_pnl += realized
        entry_backup = p["entry_price"]
        lev_backup = p["leverage"]
        # 归还全部保证金 + 已实现盈亏（扣除平仓成本）
        self.cash += p["margin"] + realized
        self.cash -= self._trading_cost(abs(price * p["qty"]))
        self.position = None
        self._last_funding_ts = None
        # 记录本次离场（供止损冷却/追高拦截：同向平仓后 N 根K线内禁止追回）
        self.last_exit = {"side": p["side"], "price": price,
                          "bar": self._bar_counter}

        # 平仓后回撤熔断重新锚定：空仓无风险敞口，把峰值重置为当前权益，
        # 避免旧峰值永久卡死后续交易（曾导致权益定格、19 个月 0 交易）。
        self.peak_equity = self.mark_to_market(price)

        pnl_pct = (price / entry_backup - 1.0) * 100.0 if p["side"] == "long" else \
                  (entry_backup - price) / entry_backup * 100.0
        self.stats["sells"] += 1
        self.closed_trades.append({
            "time": self._time_str(timestamp),
            "action": "CLOSE",
            "price": round(price, 2),
            "entry": round(entry_backup, 2),
            "leverage": round(lev_backup, 1),
            "pnl_pct": round(pnl_pct, 3),
            "realized_usdt": round(realized, 2),
        })
        # trade amount 字段统一记录退出名义额，供换手率审计；盈亏另见 closed_trades。
        self._record_trade("CLOSE", price, 0.0, abs(price * p["qty"]),
                           timestamp, reason, qty_override=p["qty"])
        # 平仓成交日志：force_close 是所有离场（CLOSE指令/熔断/止损/移动止盈/盘中止损）的
        # 唯一出口，日志在此统一打印，避免各调用点返回值被丢弃导致「只见建仓、不见平仓」。
        logger.info(
            "[平仓] %s %s仓 @ %.2f | %s | 本次盈亏 %+.2f USDT (%+.2f%%) | 权益 -> %.2f | 累计盈亏 %+.2f USDT",
            self._time_str(timestamp), "多" if p["side"] == "long" else "空",
            price, reason, realized, pnl_pct,
            self.mark_to_market(price), self.realized_pnl)
        return self._result("CLOSE", 0.0, f"清仓成交（{reason}）", price)

    def _force_close_on_circuit_breaker(self, price: float, timestamp) -> dict | None:
        """熔断即强制清仓：峰值回撤或当日亏损触达熔断线时，主动平掉全部持仓。

        这是脚本层硬风控的最后一环——旧逻辑只把 BUY 降级为 HOLD，从不主动
        平掉浮亏仓，导致浮亏仓被无限期死扛。现在只要触线且有持仓，立即
        force_close 离场。判据用「峰值回撤」而非「累计亏损」，天然可恢复。

        返回:
            - 触发清仓时返回 force_close 的结果 dict；
            - 无持仓或未触线时返回 None（由 execute_decision 继续撮合本轮动作）。
        """
        if self.position is None:
            return None
        # 账户级回撤闸门优先（Codex #6）：高水位不因平仓重置，连续亏损也会触发
        addd = self.account_dd_pct(price)
        if addd <= -float(getattr(CFG, "EQUITY_DRAWDOWN_LIMIT_PERCENT", 15.0)):
            self.stats["account_dd_force"] += 1
            return self.force_close(
                price, timestamp,
                reason=f"账户级回撤{addd:.2f}%触达{CFG.EQUITY_DRAWDOWN_LIMIT_PERCENT:.0f}%闸门，强制清仓")
        dd = self.drawdown_pct(price)
        daily = self.daily_pnl_pct(price)
        if dd <= -CFG.OVERALL_LOSS_LIMIT_PERCENT:
            return self.force_close(
                price, timestamp,
                reason=f"峰值回撤{dd:.2f}%触达熔断线，强制清仓")
        if daily <= -CFG.DAILY_LOSS_LIMIT_PERCENT:
            return self.force_close(
                price, timestamp,
                reason=f"当日亏损{daily:.2f}%触达熔断线，强制清仓")
        return None

    def check_liquidation(self, price: float, timestamp) -> bool:
        """检查逐仓是否触发强平。触发则全额保证金亏损、仓位归零，返回 True。"""
        if self.trading_mode == "spot" or self.position is None:
            return False
        p = self.position
        hit = (p["side"] == "long" and price <= p["liq_price"]) or \
              (p["side"] == "short" and price >= p["liq_price"])
        if not hit:
            return False
        # 简化强平：全额保证金亏损（不回收残值、不穿仓），逐仓隔离；另付平仓成本
        loss_margin = p["margin"]
        self.realized_pnl -= loss_margin           # 保证金全损
        self.cash -= self._trading_cost(abs(price * p["qty"]))  # 强平按退出名义额计成本
        # 保证金已经不在现金里（开仓时划出），故现金不回补；仅记录损失
        side = p["side"]
        entry = p["entry_price"]
        lev = p["leverage"]
        self.position = None
        self._last_funding_ts = None
        # 强平后同样重置峰值，避免空仓被旧峰值永久锁死
        self.peak_equity = self.mark_to_market(price)
        self.stats["liquidated"] += 1
        self.liquidations.append({
            "time": self._time_str(timestamp),
            "side": side,
            "entry": round(entry, 2),
            "liq_price": round(p["liq_price"], 2),
            "leverage": round(lev, 1),
            "loss_margin": round(loss_margin, 2),
        })
        logger.warning("[强平] %s仓 @%.2f 跌/涨破强平价，全额保证金 %.2f USDT 亏损（%sx）",
                       "多" if side == "long" else "空", price, loss_margin, lev)
        return True

    def check_stop_loss(self, current_price: float, timestamp,
                        defer: bool = False) -> dict | None:
        """检查是否触发止损：按「持仓浮亏占权益比例」触发，而非价格百分比。

        旧逻辑（价格 ≤ 开仓价×(1-STOP_LOSS_RATIO)）配低杠杆（如 3x，名义敞口
        仅 15% 权益）时极度钝化——价格跌 2% 只浮亏 0.3% 权益，永远触发不了，
        又叠加熔断线只拦 BUY 不逼平仓，导致浮亏仓死扛。新逻辑直接看「该仓
        浮亏是否吃掉 STOP_LOSS_RATIO 比例的总权益」，贴合逐仓保证金模型。

        defer=True（next_open）：命中不立即成交，挂起等次根开盘价离场。
        """
        self.roll_day(timestamp, current_price)
        if self.position is None:
            return None
        price = float(current_price)
        equity = self.mark_to_market(price)
        if equity <= EPS:
            return None
        unrealized = self._unrealized_pnl(price)
        # 浮亏（unrealized<0）且亏损幅度 ≥ 权益 × STOP_LOSS_RATIO → 触发
        loss_threshold = equity * CFG.STOP_LOSS_RATIO
        if unrealized <= -loss_threshold:
            if defer and self.pending_exit is not None:
                return None
            side = "多" if self.position["side"] == "long" else "空"
            loss_pct = abs(unrealized) / equity * 100.0
            self.stats["stop_loss"] += 1
            if defer:
                logger.warning("[止损·待执行] %s头浮亏 %.2f USDT（约占权益 %.2f%%），"
                               "触发回撤止损，次根开盘离场", side, abs(unrealized), loss_pct)
                self.set_pending_exit(
                    timestamp,
                    f"权益回撤止损（{side}头浮亏 {loss_pct:.2f}% 权益）")
                return None
            logger.warning("[止损] %s头浮亏 %.2f USDT（约占权益 %.2f%%），触发回撤止损，强制离场！",
                           side, abs(unrealized), loss_pct)
            return self.force_close(
                price, timestamp,
                reason=f"权益回撤止损（{side}头浮亏 {loss_pct:.2f}% 权益）")
        return None

    def check_trailing_tp(self, current_price: float, timestamp,
                          defer: bool = False) -> dict | None:
        """移动止盈（trailing take-profit）：从持仓期最高价回撤即止盈。

        逻辑：
            1. 先更新持仓期最高价（多头看高价、空头看低价，等价地按浮盈方向跟踪）；
            2. 未激活时，若浮盈率尚未达到 TRAILING_TP_ACTIVATE（默认 2%），只更新
               最高价、不触发止盈——避免刚开仓就被正常波动回撤误杀；
            3. 一旦激活，若价格从最高价回撤 ≥ TRAILING_TP_RATIO（默认 5%），
               立即止盈离场，锁定大部分利润（让利润奔跑、又不再坐过山车）。

        defer=True（next_open）：命中不立即成交，挂起等次根开盘价离场。
                （跟踪基准照常更新；重复命中由 set_pending_exit 去重。）

        返回: 触发止盈时返回 force_close 的结果 dict；否则返回 None。
        """
        if self.position is None:
            return None
        price = float(current_price)
        p = self.position

        # 1) 更新持仓期最高价（多头：价格新高即上移；空头：价格新低即下移）
        if p["side"] == "long":
            p["highest_price"] = max(p.get("highest_price", p["entry_price"]), price)
        else:
            p["highest_price"] = min(p.get("highest_price", p["entry_price"]), price)
        peak = p["highest_price"]

        # 2) 未激活：判断浮盈是否达到激活阈值（多头价涨 / 空头价跌）
        if not p.get("tp_activated", False):
            if p["side"] == "long":
                gain_pct = (peak / p["entry_price"] - 1.0)
            else:
                gain_pct = (p["entry_price"] / peak - 1.0)
            if gain_pct >= float(getattr(CFG, "TRAILING_TP_ACTIVATE", 0.02)):
                p["tp_activated"] = True
            else:
                return None

        # 3) 已激活：从最高价回撤超阈值 → 止盈
        if p["side"] == "long":
            drawdown = (price / peak - 1.0)          # ≤0
        else:
            drawdown = (peak / price - 1.0)          # 空头：价格回升视为回撤
        if drawdown <= -float(getattr(CFG, "TRAILING_TP_RATIO", 0.05)):
            if defer and self.pending_exit is not None:
                return None
            self.stats["take_profit"] += 1
            side = "多" if p["side"] == "long" else "空"
            if defer:
                logger.info("[移动止盈·待执行] %s头从最高价 %.2f 回撤 %.2f%%，"
                            "触发移动止盈，次根开盘离场",
                            side, peak, abs(drawdown) * 100.0)
                self.set_pending_exit(
                    timestamp,
                    f"移动止盈（{side}头自最高价{peak:.2f}回撤{abs(drawdown)*100:.2f}%）")
                return None
            logger.info("[移动止盈] %s头从最高价 %.2f 回撤 %.2f%%，触发移动止盈，锁定利润",
                        side, peak, abs(drawdown) * 100.0)
            return self.force_close(
                price, timestamp,
                reason=f"移动止盈（{side}头自最高价{peak:.2f}回撤{abs(drawdown)*100:.2f}%）")
        return None

    def check_trailing_tp_intrabar(self, high: float, low: float, timestamp) -> dict | None:
        """移动止盈·盘中击穿检测（与止损对称，日线回测专用）。

        收盘版 check_trailing_tp 每天只在收盘价检查一次——若当天一根大阴线从最高点
        直接跌穿触发线（如 247.50→220.38 单日 -11%），要等收盘才发现，实际回撤已
        达 8~11% 而非设定的 5%。本方法用 high/low 盘中极值：
            1. 用 high（多头）/ low（空头）先更新跟踪基准；
            2. 已激活后，若 low ≤ 触发线（多头），立即按触发线价成交（而非跌透的
               收盘市价），把滑点锁死在触发线处，与止损的「击穿按线价成交」同构。

        返回: 触发止盈时返回 force_close 的结果 dict；否则返回 None。
        """
        if self.position is None:
            return None
        if not bool(getattr(CFG, "TRAILING_TP_INTRABAR", True)):
            return None
        high = float(high)
        low = float(low)
        p = self.position

        # 1) 用盘中极值更新跟踪基准（覆盖收盘价够不到的影线新高/新低）
        if p["side"] == "long":
            p["highest_price"] = max(p.get("highest_price", p["entry_price"]), high)
        else:
            p["highest_price"] = min(p.get("highest_price", p["entry_price"]), low)
        peak = p["highest_price"]

        # 2) 激活判定（浮盈达阈值才启动跟踪，避免开仓初期正常波动误杀）
        if not p.get("tp_activated", False):
            if p["side"] == "long":
                gain = peak / p["entry_price"] - 1.0
            else:
                gain = p["entry_price"] / peak - 1.0
            if gain < float(getattr(CFG, "TRAILING_TP_ACTIVATE", 0.02)):
                return None
            p["tp_activated"] = True

        # 3) 已激活：低点（多头）/ 高点（空头）击穿触发线 → 按触发线价止盈离场
        ratio = float(getattr(CFG, "TRAILING_TP_RATIO", 0.05))
        side = "多" if p["side"] == "long" else "空"
        if p["side"] == "long":
            line = peak * (1.0 - ratio)
            if low <= line:
                self.stats["take_profit"] += 1
                dd_pct = abs(line / peak - 1.0) * 100.0
                logger.info(
                    "[移动止盈] %s头盘中最低价 %.2f 击穿触发线 %.2f（自最高价 %.2f 回撤 %.2f%%），按触发线价锁定",
                    side, low, line, peak, dd_pct)
                return self.force_close(
                    line, timestamp,
                    reason=f"移动止盈（{side}头盘中击穿，自最高价{peak:.2f}回撤{dd_pct:.2f}%）")
        else:
            line = peak * (1.0 + ratio)
            if high >= line:
                self.stats["take_profit"] += 1
                dd_pct = abs(peak / line - 1.0) * 100.0
                logger.info(
                    "[移动止盈] %s头盘中最高价 %.2f 击穿触发线 %.2f（自最低价 %.2f 回升 %.2f%%），按触发线价锁定",
                    side, high, line, peak, dd_pct)
                return self.force_close(
                    line, timestamp,
                    reason=f"移动止盈（{side}头盘中击穿，自最低价{peak:.2f}回升{dd_pct:.2f}%）")
        return None

    # ==================================================================
    # 内部记账辅助
    # ==================================================================
    def _record_trade(self, action: str, price: float, pct: float,
                      amount: float, timestamp, reason: str, qty_override: float = None) -> None:
        """把一笔成交写入 trades 历史。amount 为名义敞口（USDT）。"""
        self.stats["orders"] += 1
        if action == "BUY":
            self.stats["buys"] += 1
        self.trades.append({
            "time": self._time_str(timestamp),
            "action": action,
            "price": round(price, 2),
            "qty_pct": round(pct, 2),                    # 保证金占权益百分比
            "notional_usdt": round(amount, 2),
            "qty": round(qty_override if qty_override is not None
                         else (amount / price if price else 0.0), 6),
            "reason": reason,
        })

    def _result(self, action: str, filled_pct: float, reason: str, price: float) -> dict:
        return {
            "action": action,
            "filled_qty_pct": round(filled_pct, 2),
            "reason": reason,
            "price": price,
        }

    # ==================================================================
    # 状态持久化（state.json）
    # ==================================================================
    def save_state(self, price: float | None = None) -> None:
        """把账户状态写回 state.json（原子写入）。"""
        if self.state_path is None:
            return

        if price is not None:
            eq = self.mark_to_market(price)
            pos = self.position_pct(price)
            pnl = self.pnl_pct(price)
            daily = self.daily_pnl_pct(price)
            last_price = round(price, 2)
        else:
            ep = self.position["entry_price"] if self.position else None
            if ep:
                eq = self.mark_to_market(ep)
                pos = self.position_pct(ep)
            else:
                eq = self.cash
                pos = 0.0
            pnl = (eq / self.base_capital - 1.0) * 100.0 if self.base_capital > 0 else 0.0
            daily = (eq / self.day_start_equity - 1.0) * 100.0 if self.day_start_equity > 0 else 0.0
            last_price = None

        state = {
            "trading_mode": self.trading_mode,
            "capital": round(self.base_capital, 2),
            "position": round(pos, 2),
            "entry_price": round(self.position["entry_price"], 2) if self.position else None,
            "leverage": self.position["leverage"] if self.position else None,
            "liq_price": round(self.position["liq_price"], 2) if self.position else None,
            "stop_price": round(self.position["stop_price"], 2) if self.position and self.position.get("stop_price") else None,
            "pnl": round(pnl, 4),
            "daily_pnl": round(daily, 4),
            "equity": round(eq, 2),
            "cash": round(self.cash, 2),
            "position_detail": self.position,
            "realized_pnl": round(self.realized_pnl, 2),
            "peak_equity": round(self.peak_equity, 2),
            "account_peak_equity": round(self.account_peak_equity, 2),
            "last_funding_ts": self._time_str(self._last_funding_ts) if self._last_funding_ts is not None else None,
            "pending_exit": ({"ts": self._time_str(self.pending_exit.get("ts")),
                              "reason": self.pending_exit.get("reason", "")}
                             if self.pending_exit else None),
            "pending_action": self.pending_action,
            "last_processed_bar": self.last_processed_bar,
            "bar_counter": self._bar_counter,
            "last_exit": self.last_exit,
            "day_key": self.day_key,
            "day_start_equity": round(self.day_start_equity, 2),
            "last_price": last_price,
            "trades": self.trades[-200:],
            "closed_trades": self.closed_trades[-100:],
            "liquidations": self.liquidations[-50:],
            "stats": self.stats,
        }
        tmp = self.state_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.state_path)
            logger.debug("状态已保存: %s", self.state_path)
        except OSError as e:
            logger.error("状态保存失败: %s", e)

    def load_state(self) -> None:
        """从 state.json 恢复账本（仅实盘模式启动时调用）。"""
        if self.state_path is None or not self.state_path.exists():
            logger.info("未找到状态文件 %s，从默认本金 %.0f USDT 开始",
                        self.state_path, self.base_capital)
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            saved_mode = str(data.get("trading_mode") or "futures").strip().lower()
            if data.get("position_detail") is not None and saved_mode != self.trading_mode:
                raise ValueError(
                    f"状态持仓属于 {saved_mode}，当前为 {self.trading_mode}；为防账本串用拒绝加载")
            self.base_capital = float(data.get("capital", CFG.CAPITAL))
            self.cash = float(data.get("cash", self.base_capital))
            self.realized_pnl = float(data.get("realized_pnl", 0.0))
            self.peak_equity = float(data.get("peak_equity", self.base_capital))
            self.account_peak_equity = float(data.get("account_peak_equity", self.base_capital))
            self.position = data.get("position_detail")
            self._last_funding_ts = data.get("last_funding_ts")
            self.pending_exit = data.get("pending_exit")
            self.pending_action = data.get("pending_action")
            self.last_processed_bar = data.get("last_processed_bar")
            self._bar_counter = int(data.get("bar_counter", 0) or 0)
            self.last_exit = data.get("last_exit")
            self.day_key = data.get("day_key")
            self.day_start_equity = float(data.get("day_start_equity", self.base_capital))
            self.trades = list(data.get("trades", []))
            self.closed_trades = list(data.get("closed_trades", []))
            self.liquidations = list(data.get("liquidations", []))
            saved_stats = data.get("stats")
            if isinstance(saved_stats, dict):
                for k in self.stats:
                    if k in saved_stats:
                        self.stats[k] = saved_stats[k]
            logger.info("已从 %s 恢复账本：现金 %.2f，持仓 %s，已实现盈亏 %.2f",
                        self.state_path, self.cash,
                        (self.position or {}).get("side", "无"),
                        self.realized_pnl)
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
            logger.error("状态文件解析失败(%s)，将从默认本金重新开始", e)
