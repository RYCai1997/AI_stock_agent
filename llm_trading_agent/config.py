# -*- coding: utf-8 -*-
"""
config.py —— 全局配置管理模块
================================
职责：
    1. 从项目根目录的 .env 文件中读取 API 密钥与可调参数；
    2. 集中定义所有交易参数（仓位上限、止损比例、本金、回测起始日等）；
    3. 统一管理文件路径（state.json、trading.log）。

设计说明：
    - 优先使用 python-dotenv 加载 .env；若该库尚未安装（第一次运行还没
      pip install），则回退到内置的极简 .env 解析器，保证程序不因缺少
      dotenv 而崩溃，方便「双击即用」。
    - 所有参数都可以通过 .env 覆盖，默认值与本项目 README 一致。
"""

import os
from pathlib import Path

# ----------------------------------------------------------------------
# 基础路径
# ----------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent          # 项目根目录
ENV_FILE = BASE_DIR / ".env"                        # 环境变量文件


def _load_env_file(path: Path) -> None:
    """
    加载 .env 文件到进程环境变量（不覆盖已存在的环境变量）。

    说明：先用 python-dotenv 尝试（标准方式）；如果导入失败（依赖未安装），
    就用手写解析兜底——按行拆分 'KEY=VALUE'，跳过空行与 # 注释行。
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(path, override=False)
        return
    except Exception:
        pass  # python-dotenv 未安装，走手动解析

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_env_file(ENV_FILE)


def _env_int(key: str, default: int) -> int:
    """读取整数型环境变量，解析失败时返回默认值。"""
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    """读取浮点型环境变量，解析失败时返回默认值。"""
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    """读取布尔型环境变量：'1/true/yes' 视为 True。"""
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes")


def _env_str(key: str, default: str) -> str:
    """读取字符串型环境变量，去首尾空白（缺失/为空时用默认值）。"""
    return (os.getenv(key) or default).strip()


class Config:
    """集中式配置对象（模块加载时实例化为 CFG，供全项目引用）。"""

    # ===================== 交易标的 =====================
    SYMBOL: str = os.getenv("SYMBOL", "BTCUSDT").strip().upper()  # 交易对（BTC/ETH/DOGE/SOL 等）
    INTERVAL: str = os.getenv("INTERVAL", "4h").strip().lower()   # K线周期/切片间隔（15m~1d）

    # ===================== 回测范围（GUI 可调）=====================
    START_DATE: str | None = (os.getenv("START_DATE", "2025-08-10") or None)  # 回测起始
    BACKTEST_END: str | None = (os.getenv("BACKTEST_END", "") or None)        # 回测截止（空=现在）
    LIVE_START: str | None = (os.getenv("LIVE_START", "") or None)            # 实盘曲线起点（空=不回放预热）
    MAX_BACKTEST_BARS: int = _env_int("MAX_BACKTEST_BARS", 1000)              # 回测/预热切片上限

    # ===================== LLM 配置（OpenAI 兼容协议，模型/端点/密钥均可自由指定） =====================
    # llm_client 用 requests 裸调 OpenAI 兼容的 /chat/completions 端点，不依赖任何 SDK。
    # 模型名、API 端点、密钥三者均可在 GUI 中自由填写（或 .env 覆盖），完全由用户指定。
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "deepseek").strip().lower()
    LLM_MODEL_ENV: str = os.getenv("LLM_MODEL", "").strip()                  # 用户显式指定的模型
    # 通用密钥字段优先；若未填则回退到旧的 DEEPSEEK_API_KEY（向后兼容）
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "").strip() or os.getenv("DEEPSEEK_API_KEY", "").strip()
    # API 端点：通用字段优先，回退旧字段，再回退到「按 provider 的默认端点」。
    # 未显式填写 LLM_BASE_URL 时，deepseek → 官方端点，siliconflow → 硅基流动端点。
    LLM_BASE_URL: str = (
        os.getenv("LLM_BASE_URL", "").strip()
        or os.getenv("DEEPSEEK_BASE_URL", "").strip()
        or ("https://api.siliconflow.cn/v1" if LLM_PROVIDER in ("siliconflow", "silicon")
            else "https://api.deepseek.com/v1")
    )
    # 保留旧字段别名（供其它模块可能的引用），值已归一化到 LLM_API_KEY/LLM_BASE_URL
    DEEPSEEK_API_KEY: str = LLM_API_KEY
    DEEPSEEK_BASE_URL: str = LLM_BASE_URL

    LLM_TEMPERATURE: float = 0.2   # 分析温度：0.05 太机械（每次只念规则套话），0.2 给 LLM 一点表达空间但保持稳定
    LLM_MAX_TOKENS: int = _env_int("LLM_MAX_TOKENS", 512)   # 输出长度上限（仅 JSON，512 足够；过大反而诱发重复输出）
    LLM_TIMEOUT: float = 60.0       # API 请求超时（秒）
    MAX_API_RETRY: int = 3          # API 失败自动重试次数（指数退避）
    # ---- LLM 决策自由度（v9：把策略判断权从引擎移交 LLM，红线仍由引擎强制）----
    # TREND_FILTER_LEVEL 控制「趋势方向判断」归谁：
    #   "engine" —— 引擎用 EMA50+MACD 硬拦截（v7 及以前：LLM 只能在规则引擎同款
    #                窗口里做选择题，导致成交理由全是"站上EMA50且MACD多头"套话）；
    #   "llm"    —— 引擎不再拦 BUY 的趋势条件，把「现在是不是趋势、右侧还是左侧、
    #                该不该追」全部交给 LLM 用 K 线窗口+多指标综合判断（v9 默认）。
    #                红线（熔断/仓位硬顶/单笔上限/禁摊均价/追高冷却/强制止损/强平）仍硬性。
    # 该开关只影响 BUY 的趋势门槛；SELL 减仓/CLOSE 不受影响。
    TREND_FILTER_LEVEL: str = _env_str("TREND_FILTER_LEVEL", "llm").lower()
    # LLM 宪法版本：v9（策略判断权交 LLM，默认）vs v8（旧版，入场规则写进宪法 →
    # LLM 只能在规则引擎窗口内做选择题）。A/B 对照用，验证放开判断权是否更聪明。
    LLM_CONSTITUTION_VERSION: str = _env_str("LLM_CONSTITUTION_VERSION", "v9").lower()
    # LLM 战情简报中附加的近期 K 线窗口根数（让 LLM 能看走势形态而非单根快照）。
    # 每根约 12~15 token，24 根 ≈ 350 token（与全 prompt 相比可接受）；越大 LLM 越能
    # 判断趋势/震荡，但 token 成本线性上升。0 = 关闭窗口（退回单根快照）。
    LLM_KLINE_WINDOW: int = _env_int("LLM_KLINE_WINDOW", 24)

    # 模型默认值：.env 中未显式设置 LLM_MODEL 时的兜底模型名
    _DEFAULT_MODELS = {
        "deepseek": "deepseek-chat",
        "siliconflow": "deepseek-ai/DeepSeek-V4-Flash",
        "silicon": "deepseek-ai/DeepSeek-V4-Flash",
    }

    # ===================== 仓位与风控参数 =====================
    # spot 为当前研究主线；futures 仅保留用于复现旧回测，不自动删除旧功能。
    TRADING_MODE: str = _env_str("TRADING_MODE", "spot").lower()          # spot/futures
    SPOT_MAX_EXPOSURE_PERCENT: float = _env_float("SPOT_MAX_EXPOSURE_PERCENT", 75.0)
    SPOT_SINGLE_ORDER_PERCENT: float = _env_float("SPOT_SINGLE_ORDER_PERCENT", 25.0)
    SPOT_PROBE_PERCENT: float = _env_float("SPOT_PROBE_PERCENT", 10.0)
    SPOT_MEDIUM_TARGET_PERCENT: float = _env_float("SPOT_MEDIUM_TARGET_PERCENT", 25.0)
    SPOT_HIGH_STEP_PERCENT: float = _env_float("SPOT_HIGH_STEP_PERCENT", 50.0)
    SPOT_MIN_REBALANCE_PERCENT: float = _env_float("SPOT_MIN_REBALANCE_PERCENT", 5.0)
    SPOT_LOW_REDUCE_PERCENT: float = _env_float("SPOT_LOW_REDUCE_PERCENT", 10.0)
    SPOT_MEDIUM_REDUCE_PERCENT: float = _env_float("SPOT_MEDIUM_REDUCE_PERCENT", 25.0)
    SPOT_SIZING_MODE: str = _env_str("SPOT_SIZING_MODE", "confidence").lower()  # confidence/fixed/confirmed
    SPOT_FIXED_TARGET_PERCENT: float = _env_float("SPOT_FIXED_TARGET_PERCENT", 10.0)
    SPOT_ADD_MIN_BARS: int = _env_int("SPOT_ADD_MIN_BARS", 2)
    SPOT_ADD_CONFIRM_ATR: float = _env_float("SPOT_ADD_CONFIRM_ATR", 1.0)
    SPOT_DEEP_BREAK_ATR: float = _env_float("SPOT_DEEP_BREAK_ATR", 1.0)
    SPOT_RISK_BUDGET_PERCENT: float = _env_float("SPOT_RISK_BUDGET_PERCENT", 2.0)
    MAX_POSITION_PERCENT: int = _env_int("MAX_POSITION_PERCENT", 20)      # 单仓保证金占权益上限 20%（逐仓隔离）
    SINGLE_ORDER_PERCENT: int = _env_int("SINGLE_ORDER_PERCENT", 5)       # 单次下单保证金最多占权益 5%
    # ---- 合约杠杆（逐仓保证金模式，1~10 倍，LLM 在红线内自主选择）----
    MAX_LEVERAGE: int = _env_int("MAX_LEVERAGE", 10)                      # 允许的最大杠杆倍数（1~10，1=现货无杠杆）
    MIN_LEVERAGE: int = 1                                                  # 最小杠杆倍数
    # ---- 逐仓强平（简化模型）----
    LIQUIDATION_BUFFER: float = _env_float("LIQUIDATION_BUFFER", 0.0)     # 维持保证金率缓冲（0=亏损吃掉全部保证金即强平）
    # ---- 资金费率（每 8 小时结算一次，固定默认值，可在 .env 覆盖）----
    FUNDING_RATE_PER_8H: float = _env_float("FUNDING_RATE_PER_8H", 0.0001)  # 0.01%/8h，多头按名义敞口支付
    BACKTEST_FUNDING_MODE: str = _env_str("BACKTEST_FUNDING_MODE", "historical").lower()  # historical/fixed
    # ---- 趋势判定基准线（双线：快线 EMA 做微观动能，慢线 EMA 做趋势方向）----
    EMA_TREND_PERIOD: int = _env_int("EMA_TREND_PERIOD", 6)                # 快线周期（6=微观动能/信号密度，不再充当趋势方向判官）
    EMA_TREND_SLOW_PERIOD: int = _env_int("EMA_TREND_SLOW_PERIOD", 50)     # 慢线周期（50=客观趋势方向基准，宪法拦截/禁止逆势开仓依据）
    STOP_LOSS_RATIO: float = _env_float("STOP_LOSS_RATIO", 0.02)          # 止损比例 2%
    # ---- 移动止盈（trailing take-profit）：持仓期记录最高价，从最高点回撤即止盈 ----
    TRAILING_TP_RATIO: float = _env_float("TRAILING_TP_RATIO", 0.05)      # 移动止盈回撤比例 5%（从持仓期最高价回撤 5% 触发止盈）
    TRAILING_TP_ACTIVATE: float = _env_float("TRAILING_TP_ACTIVATE", 0.02)  # 移动止盈激活阈值：浮盈达 2% 后才启动跟踪（避免开仓初期噪音误杀）
    TRAILING_TP_INTRABAR: bool = _env_bool("TRAILING_TP_INTRABAR", True)  # 移动止盈盘中击穿检测：用 high/low 极值更新基准，low≤触发线即按触发线价成交（与止损同构），
                                                                            #   消除日线「收盘才检查→大阴线穿透→实际回撤 8~11% 才离场」的滞后
    POST_LOSS_COOLDOWN_BARS: int = _env_int("POST_LOSS_COOLDOWN_BARS", 3)  # 止损冷却：同向平仓后 N 根K线禁止再开仓（打断「止损→更高价追回」锯齿磨损）
    REENTRY_GATE_MODE: str = _env_str("REENTRY_GATE_MODE", "trend").lower()  # 再入场闸门模式：trend=动态趋势锚（默认，价格站上慢线=顺势放行，仅在趋势线下方的反弹追高用静态锚拦截；
                                                                            #   EMA50 动态跟随，主升浪中每次回踩站上均线即可再进场，不再被「平仓价×1.02 永久锁死」）
                                                                            #   price=旧静态价锚（现价高于上次离场价 REENTRY_MAX_PREMIUM 即拦，A/B 回归对照用）
    REENTRY_MAX_PREMIUM: float = _env_float("REENTRY_MAX_PREMIUM", 0.02)  # 追高拦截比例（price 档：现价高于上次离场价超该比例（多 2%）即暂缓回补；
                                                                            #   trend 档：仅用于趋势线下方的逆势反弹追高判定）
    CHASE_DEV_START_PCT: float = _env_float("CHASE_DEV_START_PCT", 8.0)   # 追高乖离衰减阈值：新开仓现价相对趋势线(EMA50)乖离超该 %（多头偏高/空头偏低）
                                                                            #   → 引擎不拦截但强制降敞口（浪末接刀防护：校准证明乖离硬顶会误杀大赢单，
                                                                            #   故改为「强势可追但轻仓」，参考 daily_stock_analysis 乖离分档哲学）
    CHASE_DEV_DECAY_FACTOR: float = _env_float("CHASE_DEV_DECAY_FACTOR", 0.5)  # 衰减系数：乖离超阈值时单仓保证金 × 该系数（默认减半）
    CHASE_DEV_MAX_LEVERAGE: int = _env_int("CHASE_DEV_MAX_LEVERAGE", 2)   # 衰减时杠杆上限（默认压到 ≤2x，缩小宽止损的深亏绝对值）
    ATR_STOP_ENABLED: bool = _env_bool("ATR_STOP_ENABLED", True)          # ATR 自适应止损：止损距离 = max(固定 STOP_LOSS_RATIO, ATR_STOP_MULT×ATR_pct)
                                                                            #   （双指标取更宽：低波动用 2% 保底、高波动随 ATR 放宽，避免噪声扫损）
    ATR_STOP_MULT: float = _env_float("ATR_STOP_MULT", 1.5)               # ATR 止损倍数（×ATR14 百分比）
    ATR_PERIOD: int = _env_int("ATR_PERIOD", 14)                          # ATR 计算周期
    # ===================== 回测执行成交模型（三档） =====================
    # 决定止损/止盈/开平仓的「触发识别时点」与「成交价」——回测必须先声明用哪种成交假设，
    # 且应与实盘真实执行机制一致，否则回测会系统性优于/劣于实盘（前视或过度保守）：
    #   intrabar —— 止损/止盈按盘中极值击穿即时成交（止损线价/移动止盈触发线价）；
    #               开平仓按当根收盘价。隐含「条件单盘中自动执行」假设——只有实盘真挂了
    #               条件单或有盘中巡检时才成立。
    #   close    —— 全部按当根收盘价识别并成交（与实盘 run_slice 的收盘检查语义一致）。
    #               最保守：单根大阴线从峰值直接穿透触发线时，只能等收盘按收盘价离场，
    #               实际回撤=真实回撤（用户 v6 日志「回撤 8~11% 才止盈」即此语义）。
    #   next_open—— 当根收盘识别触发/决策（同 close），但成交延迟到次根开盘价（模拟
    #               「收盘信号 + 次日才执行」的延迟交易者，含隔夜跳空损耗）。
    # 2026-09-03（Codex 审查）：正式回测默认 next_open——用当根完整收盘指标又按同一
    # 收盘价成交是「用未来信息成交」的偏乐观口径；close/intrabar 仅作敏感性上下界。
    EXECUTION_MODEL: str = _env_str("EXECUTION_MODEL", "next_open").lower()
    NO_AVERAGE_DOWN: bool = _env_bool("NO_AVERAGE_DOWN", True)            # 禁止浮亏摊均价：加仓不重置止损线（止损锚定首仓开仓价）
    DAILY_LOSS_LIMIT_PERCENT: float = _env_float("DAILY_LOSS_LIMIT_PERCENT", 2.0)   # 当日亏损熔断线 2%（当天有效，次日清零）
    OVERALL_LOSS_LIMIT_PERCENT: float = _env_float("OVERALL_LOSS_LIMIT_PERCENT", 5.0)  # 峰值回撤熔断线 5%（相对历史峰值回撤，可恢复；放宽以避免过早打断主升浪）
    # 账户级回撤闸门（Codex #6，2026-09-03）：高水位「永不因平仓重置」的账户级最大回撤上限。
    # 与 OVERALL_LOSS_LIMIT_PERCENT 解耦——后者峰值在平仓/强平后重置（交易门控防冻结），
    # 连续亏损会被清零；本闸门只随创新高上移，超过阈值禁开新仓且强制清仓。
    # 触发后按 kill switch 处理：需人工审查并重置/注资，不宣称能在空仓时自动恢复。
    EQUITY_DRAWDOWN_LIMIT_PERCENT: float = _env_float("EQUITY_DRAWDOWN_LIMIT_PERCENT", 15.0)
    ALLOW_SHORT: bool = _env_bool("ALLOW_SHORT", False)                   # 是否允许做空（False=纯做多语义）
    CAPITAL: float = _env_float("CAPITAL", 100000.0)                      # 模拟本金（USDT）

    # ===================== V10 引擎护栏（Generalized 策略，2026-09-04 发布） =====================
    # 与 CLI --v10-* 开关等价，供 .env 固化 / 实盘落地复用同一配置源；CLI 传入时优先于此处。
    # 设计依据见 STRATEGY_v10_guardrail.md：引擎只做事后护栏（破位强平/弱市降杠杆/离场冷却），
    # 判断权留在 LLM；rl2-only / dd 模式 / 事前形态闸门均已实跑证伪，勿单独启用。
    V10_ENABLED: bool = _env_bool("V10_ENABLED", False)                   # 总开关（默认关=旧路径零影响）
    V10_RL1_MODE: str = _env_str("V10_RL1_MODE", "breach").lower()        # 红线①模式：breach=收盘<EMA50即强平（实跑验证）
    V10_RL1_COOL: int = _env_int("V10_RL1_COOL", 20)                      # 红线①离场后禁 BUY 根数（实跑验证=20）
    V10_RL2: bool = _env_bool("V10_RL2", True)                            # 红线②：收盘<EMA50 的 BUY 压杠杆至 1x

    # ===================== 交易成本（Codex #3，2026-09-03） =====================
    # 显式、可配置、偏保守：taker 手续费 + 滑点，均按单边名义敞口计提。
    # 建模说明：滑点按「价差成本等价钱数」计提（= 成交价偏移对已实现盈亏的一阶影响），
    # 无需改写入场/离场价，与逐仓账本天然自洽（成本直接扣现金，权益即时反映）。
    SIMULATE_COSTS: bool = _env_bool("SIMULATE_COSTS", True)              # 是否计提交易成本（机制探针可关）
    FEE_TAKER_PCT: float = _env_float("FEE_TAKER_PCT", 0.0004)            # 合约 taker 手续费 0.04%/边（主流 CEX 吃单档）
    SPOT_FEE_TAKER_PCT: float = _env_float("SPOT_FEE_TAKER_PCT", 0.001)   # 现货 taker 0.10%/边（未计VIP/BNB折扣）
    SLIPPAGE_PCT: float = _env_float("SLIPPAGE_PCT", 0.0002)              # 滑点 2bps/边（1d 慢周期保守估计）

    # ===================== 行情数据源 =====================
    # 按顺序尝试多个 Binance 公共端点，自动故障切换（部分网络会屏蔽主域名）
    BINANCE_BASE_URLS: list = [
        u.strip() for u in os.getenv(
            "BINANCE_BASE_URLS",
            "https://api.binance.com,https://data-api.binance.vision",
        ).split(",") if u.strip()
    ]
    BINANCE_FUTURES_BASE_URLS: list = [
        u.strip() for u in os.getenv(
            "BINANCE_FUTURES_BASE_URLS",
            "https://fapi.binance.com",
        ).split(",") if u.strip()
    ]
    FETCH_TIMEOUT: float = 15.0     # 行情请求超时（秒）
    DATA_RETRY: int = 3             # 行情请求重试次数

    # ===================== 调度与运行 =====================
    ENABLE_FREQUENT_STOP_CHECK: bool = _env_bool("ENABLE_FREQUENT_STOP_CHECK", True)  # 实盘模式每 N 分钟巡检一次止损
    STOP_CHECK_MINUTES: int = _env_int("STOP_CHECK_MINUTES", 2)                       # 止损巡检间隔（分钟）
    BACKTEST_USE_LLM: bool = _env_bool("BACKTEST_USE_LLM", False)                     # 回测是否调用真实 LLM（默认规则引擎）

    # ===================== 时区 =====================
    TIMEZONE: str = "Asia/Shanghai"  # 展示与「交易日」均按北京时间

    # ===================== 文件路径 =====================
    STATE_FILE: Path = BASE_DIR / "state.json"     # 持仓/盈亏持久化文件
    LOG_FILE: Path = BASE_DIR / "trading.log"      # 日志文件

    # ------------------------------------------------------------------
    # 便捷属性与方法
    # ------------------------------------------------------------------
    @property
    def llm_model(self) -> str:
        """返回最终生效的 LLM 模型名：显式设置优先，否则按 provider 取默认。"""
        if self.LLM_MODEL_ENV:
            return self.LLM_MODEL_ENV
        return self._DEFAULT_MODELS.get(self.LLM_PROVIDER, "deepseek-chat")

    @property
    def BASE_DIR(self) -> Path:
        """项目根目录（供 main 等模块写入报告时使用）。"""
        return BASE_DIR

    def has_llm_key(self) -> bool:
        """是否已配置 LLM API 密钥。"""
        return bool(self.LLM_API_KEY)


# 全局单例：其它模块统一 from config import CFG 使用
CFG = Config()
