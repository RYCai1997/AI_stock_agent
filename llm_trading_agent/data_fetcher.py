# -*- coding: utf-8 -*-
"""
data_fetcher.py —— 数据获取模块
================================
职责：
    1. fetch_klines()         从 Binance 公共 API 获取 BTC/USDT K线数据；
    2. calculate_indicators() 用 pandas 原生实现 EMA200 / RSI14 / 布林带 / MACD；
    3. get_latest_slice()     取最新一根K线（含全部指标），作为「战情切片」；
    4. fetch_latest_price()   获取实时最新价（用于高频止损巡检）。

关键设计：
    - 多域名自动故障切换（api.binance.com 被某些网络屏蔽时，
      自动改走 data-api.binance.vision 等备用端点）；
    - 所有 EMA / RSI / MACD / 布林带均为「因果滤波器」：第 i 根K线
      的指标只依赖前 i 根的数据，因此全量预计算后逐根回放不会引入未来函数。
"""

import logging
import time

import pandas as pd

from config import CFG

logger = logging.getLogger(__name__)

# Binance K线响应中我们需要的列（前 6 列）
KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume"]

# 各周期的毫秒数（用于时间对齐）
INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
}


# ----------------------------------------------------------------------
# 底层请求（带重试 + 多域名切换）
# ----------------------------------------------------------------------
def _http_get_json(params: dict, endpoint: str = "api/v3/klines") -> list | dict:
    """
    向 Binance 发起 GET 请求，带重试与多域名故障切换。

    参数:
        params:   查询参数（symbol、interval、limit、startTime...）
        endpoint: API 路径，默认K线接口

    返回:
        Binance 返回的 JSON（list 或 dict）

    异常:
        所有域名、所有重试均失败时抛出最后一次异常（由调用方兜底为 HOLD）。
    """
    import requests  # 局部导入，保持模块顶层轻量

    last_err: Exception | None = None
    for attempt in range(1, CFG.DATA_RETRY + 1):
        for base_url in CFG.BINANCE_BASE_URLS:
            url = f"{base_url.rstrip('/')}/{endpoint}"
            try:
                resp = requests.get(url, params=params, timeout=CFG.FETCH_TIMEOUT)
                if resp.status_code == 200:
                    return resp.json()
                # 429=限频、418=IP封禁：需要退避重试
                if resp.status_code in (429, 418):
                    last_err = RuntimeError(f"Binance限频 HTTP {resp.status_code}")
                    time.sleep(2 ** attempt)
                    continue
                last_err = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            except requests.RequestException as e:
                last_err = e
                logger.warning("[行情] 端点 %s 不可用(%s)，尝试下一个...", base_url, e)
                time.sleep(0.5)
        if attempt < CFG.DATA_RETRY:
            logger.warning("[行情] 第 %d 次重试（%ds 后退避）", attempt, 2 ** attempt)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"行情请求最终失败: {last_err}")


def _http_get_futures_json(params: dict, endpoint: str = "fapi/v1/fundingRate") -> list | dict:
    """请求 Binance USD-M Futures 公共接口（历史资金费率等）。"""
    import requests

    last_err: Exception | None = None
    for attempt in range(1, CFG.DATA_RETRY + 1):
        for base_url in CFG.BINANCE_FUTURES_BASE_URLS:
            url = f"{base_url.rstrip('/')}/{endpoint}"
            try:
                resp = requests.get(url, params=params, timeout=CFG.FETCH_TIMEOUT)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (429, 418):
                    last_err = RuntimeError(f"Binance Futures限频 HTTP {resp.status_code}")
                    time.sleep(2 ** attempt)
                    continue
                last_err = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            except requests.RequestException as e:
                last_err = e
                logger.warning("[资金费率] 端点 %s 不可用(%s)，尝试下一个...", base_url, e)
                time.sleep(0.5)
        if attempt < CFG.DATA_RETRY:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"历史资金费率请求最终失败: {last_err}")


def _epoch_ms_to_beijing(series: pd.Series) -> pd.Series:
    """把 Unix 毫秒时间戳转换为北京时间（naive datetime）。"""
    return (
        pd.to_datetime(series, unit="ms", utc=True)
        .dt.tz_convert(CFG.TIMEZONE)
        .dt.tz_localize(None)
    )


# ----------------------------------------------------------------------
# 对外主函数
# ----------------------------------------------------------------------
def fetch_klines(symbol: str = None, interval: str = None,
                 start_date: str | None = None, end_date: str | None = None,
                 limit: int = 1000) -> pd.DataFrame:
    """
    获取 Binance K线数据，返回升序 DataFrame。

    参数:
        symbol:     交易对，默认 BTCUSDT
        interval:   K线周期，默认 4h
        start_date: 起始日期字符串（北京时间，如 "2025-08-10"）。
                    不为 None 时：从该日期分页拉取直到 end_date（默认现在），
                    返回期间全部K线（历史回测用，可超过 1000 根）。
                    为 None 时：只取最近 limit 根K线（实盘切片用）。
        end_date:   结束日期（可选，北京时间）
        limit:      start_date 为 None 时的取数根数（默认 1000）

    返回:
        DataFrame，列: open_time(北京时间datetime), open, high, low,
                      close, volume；已去重、升序排序。
    """
    symbol = symbol or CFG.SYMBOL
    interval = interval or CFG.INTERVAL

    base_params = {"symbol": symbol, "interval": interval}
    rows: list = []

    if start_date is not None:
        # ---- 历史分页拉取模式（回测） ----
        start_ts = pd.Timestamp(start_date, tz=CFG.TIMEZONE)
        # 对齐到周期边界，避免从半根K线开始
        start_ms = int(start_ts.timestamp() * 1000)
        step_ms = INTERVAL_MS.get(interval, 14_400_000)
        start_ms = (start_ms // step_ms) * step_ms

        if end_date is not None:
            end_ms = int(pd.Timestamp(end_date, tz=CFG.TIMEZONE).timestamp() * 1000)
        else:
            end_ms = int(time.time() * 1000)

        cursor = start_ms
        while cursor < end_ms:
            batch = _http_get_json({
                **base_params,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,          # Binance 单次最多 1000 根
            })
            if not batch:
                break
            rows.extend(batch)
            last_open = batch[-1][0]    # 最后一根的 open_time(ms)
            if last_open + 1 <= cursor:  # 防死循环保险丝
                break
            cursor = last_open + 1
            time.sleep(0.05)            # 温和限速，避免 429
    else:
        # ---- 最近 N 根模式（实盘） ----
        rows = _http_get_json({**base_params, "limit": min(limit, 1000)})

    if not rows:
        logger.warning("[行情] 未获取到任何K线数据")
        return pd.DataFrame(columns=KLINE_COLS)

    # 只保留需要的列并转成数值类型
    df = pd.DataFrame(rows)
    df = df.iloc[:, :6].copy()
    df.columns = KLINE_COLS
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # 时间列：ms -> 北京时间
    df["open_time"] = _epoch_ms_to_beijing(pd.to_numeric(df["open_time"]))

    # 去重 + 排序（分页拼接时可能重叠 1 行）
    df = df.drop_duplicates(subset="open_time", keep="last")
    df = df.sort_values("open_time").reset_index(drop=True)
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    return df


def closed_klines_only(df: pd.DataFrame, interval: str | None = None,
                       now=None) -> pd.DataFrame:
    """只返回已经闭合的K线，防止 live/回测把当前形成中的K线当作收盘信号。

    Binance 返回的 open_time 是K线起点；闭合时点按 open_time + interval 判断。
    未识别周期时抛错，避免悄悄使用不完整数据。
    """
    if df is None or df.empty:
        return df.copy() if df is not None else pd.DataFrame(columns=KLINE_COLS)
    iv = (interval or CFG.INTERVAL).strip().lower()
    if iv not in INTERVAL_MS:
        raise ValueError(f"无法判断K线是否闭合：未知周期 {iv!r}")
    now_ts = pd.Timestamp.now(tz=CFG.TIMEZONE).tz_localize(None) if now is None else pd.Timestamp(now)
    if now_ts.tzinfo is not None:
        now_ts = now_ts.tz_convert(CFG.TIMEZONE).tz_localize(None)
    close_at = pd.to_datetime(df["open_time"]) + pd.to_timedelta(INTERVAL_MS[iv], unit="ms")
    return df.loc[close_at <= now_ts].reset_index(drop=True)


def fetch_funding_rates(symbol: str, start_date: str,
                        end_date: str | None = None) -> pd.DataFrame:
    """分页获取 USD-M 永续合约历史资金费率，时间统一为北京时间 naive datetime。"""
    start_ts = pd.Timestamp(start_date, tz=CFG.TIMEZONE)
    end_ts = (pd.Timestamp(end_date, tz=CFG.TIMEZONE)
              if end_date else pd.Timestamp.now(tz=CFG.TIMEZONE))
    cursor = int(start_ts.timestamp() * 1000)
    end_ms = int(end_ts.timestamp() * 1000)
    rows: list[dict] = []
    while cursor < end_ms:
        batch = _http_get_futures_json({
            "symbol": symbol.strip().upper(),
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1000,
        })
        if not batch:
            break
        rows.extend(batch)
        last_ms = int(batch[-1]["fundingTime"])
        if last_ms + 1 <= cursor:
            break
        cursor = last_ms + 1
        time.sleep(0.05)
    if not rows:
        return pd.DataFrame(columns=["funding_time", "funding_rate", "mark_price"])
    df = pd.DataFrame(rows)
    df["funding_time"] = _epoch_ms_to_beijing(pd.to_numeric(df["fundingTime"]))
    df["funding_rate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
    if "markPrice" in df:
        df["mark_price"] = pd.to_numeric(df["markPrice"], errors="coerce")
    else:
        df["mark_price"] = float("nan")
    df = df[["funding_time", "funding_rate", "mark_price"]]
    df = (df.dropna(subset=["funding_time", "funding_rate"])
          .drop_duplicates(subset="funding_time", keep="last")
          .sort_values("funding_time").reset_index(drop=True))
    # fundingRate 接口通常不返回历史 markPrice；用同一结算小时的 1h mark-price
    # K线开盘价补齐。它与结算时点对齐，避免用日线未来收盘价估算 funding 名义额。
    if df["mark_price"].isna().any():
        marks = fetch_mark_price_opens(symbol, start_date, end_date)
        if not marks.empty:
            df["_funding_key"] = df["funding_time"].dt.floor("1h")
            df = df.merge(marks, how="left", left_on="_funding_key", right_on="funding_time_key")
            df["mark_price"] = df["mark_price"].fillna(df["mark_price_open"])
            df = df[["funding_time", "funding_rate", "mark_price"]]
    return df


def fetch_mark_price_opens(symbol: str, start_date: str,
                           end_date: str | None = None) -> pd.DataFrame:
    """获取 1h mark-price K线开盘价，作为 funding 结算时点名义额近似。

    使用1h而非8h是因为交易所极端时期可能临时改为每2h结算 funding。
    """
    start_ts = pd.Timestamp(start_date, tz=CFG.TIMEZONE)
    end_ts = (pd.Timestamp(end_date, tz=CFG.TIMEZONE)
              if end_date else pd.Timestamp.now(tz=CFG.TIMEZONE))
    cursor = int(start_ts.timestamp() * 1000)
    end_ms = int(end_ts.timestamp() * 1000)
    rows: list = []
    while cursor < end_ms:
        batch = _http_get_futures_json({
            "symbol": symbol.strip().upper(), "interval": "1h",
            "startTime": cursor, "endTime": end_ms, "limit": 1000,
        }, endpoint="fapi/v1/markPriceKlines")
        if not batch:
            break
        rows.extend(batch)
        last_open = int(batch[-1][0])
        if last_open + 1 <= cursor:
            break
        cursor = last_open + 1
        time.sleep(0.05)
    if not rows:
        return pd.DataFrame(columns=["funding_time_key", "mark_price_open"])
    out = pd.DataFrame({
        "funding_time_key": _epoch_ms_to_beijing(pd.Series([r[0] for r in rows])),
        "mark_price_open": pd.to_numeric(pd.Series([r[1] for r in rows]), errors="coerce"),
    })
    return (out.dropna().drop_duplicates("funding_time_key", keep="last")
            .sort_values("funding_time_key").reset_index(drop=True))


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    用 pandas 原生实现技术指标（无需 TA-Lib）。

    新增列:
        ema_200      指数移动平均(200)
        rsi_14       RSI 相对强弱指标(14)，Wilder 平滑
        bb_mid/bb_upper/bb_lower   布林带(20, 2)
        macd_dif/macd_dea/macd_hist   MACD(12,26,9)，柱=(DIF-DEA)*2
        vol_ratio    量比 = 当前成交量 / 20期平均成交量
        change_pct   本根K线涨跌幅(%)
        prev_close   前一根收盘价

    注意:
        - 指标是因果的（只用历史数据），回测预计算无未来函数；
        - 前 ~200 根处于预热期，ema_200 等为 NaN，回测会从首个有效值开始。
    """
    df = df.copy()
    close = df["close"]

    # ---- EMA 快线（微观动能，周期可配置 EMA_TREND_PERIOD，默认 6）----
    # 快线显著缩短预热窗口：6 根即可出信号，用于微观动能/信号密度判断。
    trend_span = int(getattr(CFG, "EMA_TREND_PERIOD", 6))
    df["ema_trend"] = close.ewm(span=trend_span, adjust=False, min_periods=trend_span).mean()

    # ---- EMA 慢线（客观趋势方向基准，周期可配置 EMA_TREND_SLOW_PERIOD，默认 50）----
    # 慢线是「趋势方向判官」：宪法拦截、禁止逆势开仓都以此为准（右侧交易，
    # 不逆势抄底）。周期 50 平滑掉短周期噪音，避免下跌趋势中每次小反弹都被
    # 快线误判为「多头区域」。
    slow_span = int(getattr(CFG, "EMA_TREND_SLOW_PERIOD", 50))
    df["ema_slow"] = close.ewm(span=slow_span, adjust=False, min_periods=slow_span).mean()

    # ---- EMA 200（保留列，供旧引用/展示兼容）----
    df["ema_200"] = close.ewm(span=200, adjust=False, min_periods=200).mean()

    # ---- RSI(14)，Wilder 平滑（与 TA-Lib 高度一致）----
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1.0 / 14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0.0, pd.NA)          # 防除零
    rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi = rsi.fillna(100.0)                                # 全涨行情 loss=0 -> RSI=100
    df["rsi_14"] = rsi

    # ---- 布林带(20, 2)：中轨=20均线，带宽=2倍标准差 ----
    df["bb_mid"] = close.rolling(20).mean()
    bb_std = close.rolling(20).std(ddof=0)                 # 总体标准差（同国内行情软件）
    df["bb_upper"] = df["bb_mid"] + 2.0 * bb_std
    df["bb_lower"] = df["bb_mid"] - 2.0 * bb_std

    # ---- MACD(12,26,9) ----
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["macd_dif"] = ema12 - ema26                          # DIF 快线
    df["macd_dea"] = df["macd_dif"].ewm(span=9, adjust=False, min_periods=9).mean()  # DEA 慢线
    df["macd_hist"] = (df["macd_dif"] - df["macd_dea"]) * 2.0  # 柱体（国内惯例乘2）

    # ---- ATR(周期可配 ATR_PERIOD，默认 14)：真实波幅均值（Wilder 平滑）----
    # 用途：ATR 自适应止损——止损距离随波动率伸缩（双指标取 max(固定2%, 1.5×ATR%)），
    # 高波动期不被噪声轻易扫损，低波动期保持 2% 保底。
    atr_period = int(getattr(CFG, "ATR_PERIOD", 14))
    pc = close.shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    df["atr14"] = tr.ewm(alpha=1.0 / atr_period, adjust=False,
                         min_periods=atr_period).mean()      # 绝对价
    df["atr_pct"] = df["atr14"] / close * 100.0              # 占价格百分比（回测循环传给执行器）

    # ---- 量比 & 涨跌幅（供战情简报与规则引擎使用）----
    df["prev_close"] = close.shift(1)
    df["change_pct"] = (close / df["prev_close"] - 1.0) * 100.0
    vol_ma20 = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / vol_ma20

    # ---- 确认式现货加减仓所需的因果结构列（全部只引用当前及更早K线）----
    df["ema_trend_prev"] = df["ema_trend"].shift(1)
    df["ema_slow_prev"] = df["ema_slow"].shift(1)
    df["macd_hist_prev"] = df["macd_hist"].shift(1)
    df["high_prev"] = df["high"].shift(1)
    df["high_20_prev"] = df["high"].shift(1).rolling(20).max()
    df["low_10_prev"] = df["low"].shift(1).rolling(10).min()
    df["low_5_prev"] = df["low"].shift(1).rolling(5).min()

    return df


def get_latest_slice(df: pd.DataFrame) -> pd.Series:
    """
    返回最新一根K线（含全部指标）作为当前战情切片。

    若传入的 df 尚未计算指标，会自动补算一次（保险）。
    """
    if "ema_200" not in df.columns:
        df = calculate_indicators(df)
    if df.empty:
        raise ValueError("数据为空，无法取最新切片")
    return df.iloc[-1]


def fetch_latest_price(symbol: str = None) -> float:
    """
    获取最新成交价（实盘模式下用于止损的高频巡检）。
    走 /api/v3/ticker/price 端点，同样支持多域名切换与重试。
    """
    symbol = symbol or CFG.SYMBOL
    data = _http_get_json({"symbol": symbol}, endpoint="api/v3/ticker/price")
    try:
        return float(data["price"])
    except (KeyError, TypeError, ValueError) as e:
        raise RuntimeError(f"最新价解析失败: {data!r}") from e
