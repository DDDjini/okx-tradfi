"""
core/data_feed_yahoo.py — Yahoo Finance 数据源（替代 OKX）

优势：
  - 日线数据可追溯到上市日（5-10年+）
  - 小时线 730天
  - 30分钟线 60天
  - 无需 API Key，免费无限调用
  - GitHub Actions 服务器直连，无需代理

股票代码映射：
  - 美股直接用 ticker（TSLA, AAPL 等）
  - 韩股加 .KS 后缀（005930.KS = Samsung）
  - 商品用期货/ETF（CL=F, GLD, SLV）
  - OPENAI 未上市，用 AI 板块 ETF BOTZ 替代
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd
import numpy as np

from core.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

DATA_DIR = PROJECT_ROOT / "data"

# OKX 符号 → Yahoo Finance ticker 映射
SYMBOL_MAP: dict[str, str] = {
    "SNDK/USDT:USDT": "SNDK",          # SanDisk (被WD收购, 现在用WDC近似)
    "MU/USDT:USDT": "MU",              # Micron
    "SPCX/USDT:USDT": "SPCX",          # SPAC ETF
    "OPENAI/USDT:USDT": "BOTZ",        # OpenAI未上市→AI机器人ETF替代
    "SKHYNIX/USDT:USDT": "000660.KS",  # SK Hynix 韩国
    "XAU/USDT:USDT": "GLD",            # 黄金→SPDR Gold ETF
    "CL/USDT:USDT": "CL=F",            # WTI原油期货
    "XAG/USDT:USDT": "SLV",            # 白银→iShares Silver ETF
    "INTC/USDT:USDT": "INTC",          # Intel
    "TSLA/USDT:USDT": "TSLA",          # Tesla
    "SAMSUNG/USDT:USDT": "005930.KS",  # Samsung 韩国
    "NVDA/USDT:USDT": "NVDA",          # NVIDIA
    "GOOGL/USDT:USDT": "GOOGL",        # Google
    "AAPL/USDT:USDT": "AAPL",          # Apple
    "TSM/USDT:USDT": "TSM",            # TSMC ADR
    "MSFT/USDT:USDT": "MSFT",          # Microsoft
    "AMZN/USDT:USDT": "AMZN",          # Amazon
}

# 非美股不在 Yahoo Finance 的替代方案
FALLBACK_MAP = {
    "SNDK": "WDC",   # SanDisk被WD收购，用WDC近似
}

# 价格缩放因子：把原始货币价格换算成"USDT 计价"
# 韩国股票 yahoo 返回的是 KRW（韩元），需要 ÷ 1300 转 USD
PRICE_SCALE: dict[str, float] = {
    "005930.KS": 1 / 1300.0,  # SAMSUNG KOSPI → USD
    "000660.KS": 1 / 1300.0,  # SKHYNIX KOSPI → USD
}

# 周期映射（yfinance 支持的周期）
INTERVAL_MAP = {
    "30m": "30m",
    "1h": "1h",
    "4h": "1h",    # yfinance 没有4h，用1h然后聚合
    "1d": "1d",
}

# 各周期最大可拉取天数
MAX_PERIOD_DAYS = {
    "30m": 60,
    "1h": 730,
    "4h": 730,
    "1d": 3650,    # 10年
}


def _okx_to_yahoo(symbol: str) -> str:
    """OKX 统一符号 → Yahoo Finance ticker。"""
    ticker = SYMBOL_MAP.get(symbol, symbol.split("/")[0])
    ticker = FALLBACK_MAP.get(ticker, ticker)
    return ticker


def fetch_yahoo_ohlcv(
    symbol: str,
    timeframe: str,
    period_days: int = 730,
    proxy_url: str = "",
) -> pd.DataFrame:
    """
    从 Yahoo Finance 拉取 K 线数据。

    返回标准 DataFrame：timestamp, open, high, low, close, volume, datetime
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance 未安装，请运行: pip install yfinance")
        return pd.DataFrame()

    ticker = _okx_to_yahoo(symbol)
    interval = INTERVAL_MAP.get(timeframe, "1d")
    actual_days = min(period_days, MAX_PERIOD_DAYS.get(timeframe, 730))

    # yfinance 用 period 参数
    if actual_days <= 60:
        period_str = f"{actual_days}d"
    elif actual_days <= 730:
        period_str = f"{actual_days}d"
    else:
        period_str = f"{min(actual_days, 3650)}d"

    try:
        logger.debug("拉取 %s (%s) %s %s", symbol, ticker, interval, period_str)
        stock = yf.Ticker(ticker)
        df = stock.history(period=period_str, interval=interval)

        if df is None or len(df) == 0:
            logger.warning("Yahoo 返回空数据: %s (%s)", symbol, ticker)
            return pd.DataFrame()

        df = df.reset_index()
        # 标准化列名
        col_map = {}
        for c in df.columns:
            cl = c.lower()
            if "date" in cl or "time" in cl or "datetime" in cl:
                col_map[c] = "datetime"
            elif cl in ("open",):
                col_map[c] = "open"
            elif cl in ("high",):
                col_map[c] = "high"
            elif cl in ("low",):
                col_map[c] = "low"
            elif cl in ("close",):
                col_map[c] = "close"
            elif cl in ("volume",):
                col_map[c] = "volume"

        df = df.rename(columns=col_map)

        # 确保有所有需要的列
        for required in ["datetime", "open", "high", "low", "close", "volume"]:
            if required not in df.columns:
                if required == "volume":
                    df["volume"] = 0
                else:
                    logger.error("缺少列: %s in %s", required, ticker)
                    return pd.DataFrame()

        df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
        # yfinance 返回的 datetime 可能不带时区
        if df["datetime"].dt.tz is None:
            df["datetime"] = df["datetime"].dt.tz_localize("UTC")

        df["timestamp"] = df["datetime"].astype("int64") // 10**6
        df = df.sort_values("datetime").reset_index(drop=True)

        # 4h 周期：从 1h 数据聚合成 4h
        if timeframe == "4h" and interval == "1h":
            df = _aggregate_to_4h(df)

        # 应用价格缩放（如 KRW → USD）
        scale = PRICE_SCALE.get(ticker, 1.0)
        if scale != 1.0:
            for col in ("open", "high", "low", "close"):
                df[col] = df[col] * scale
            logger.debug("%s 价格缩放 × %.6f (KRW→USD)", symbol, scale)

        logger.info("Yahoo %s (%s) %s: %d 根K线, %s ~ %s",
            symbol, ticker, timeframe, len(df),
            df["datetime"].iloc[0].strftime("%Y-%m-%d"),
            df["datetime"].iloc[-1].strftime("%Y-%m-%d"))

        return df

    except Exception as e:
        logger.error("Yahoo 数据拉取失败 %s (%s): %s", symbol, ticker, e)
        return pd.DataFrame()


def _aggregate_to_4h(df: pd.DataFrame) -> pd.DataFrame:
    """将 1h K 线聚合成 4h K 线。"""
    df = df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.set_index("datetime")

    ohlc = df.resample("4h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()

    ohlc = ohlc.reset_index()
    ohlc["timestamp"] = ohlc["datetime"].astype("int64") // 10**6
    return ohlc


def fetch_yahoo_cached(
    symbol: str,
    timeframe: str,
    period_days: int = 730,
    proxy_url: str = "",
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    带 CSV 缓存的 Yahoo Finance 数据拉取。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ticker = _okx_to_yahoo(symbol)
    safe_name = f"yahoo_{ticker}_{timeframe}"
    cache_path = DATA_DIR / f"{safe_name}.csv"

    # 读缓存
    if use_cache and cache_path.exists():
        try:
            cached = pd.read_csv(cache_path, parse_dates=["datetime"])
            cached["datetime"] = pd.to_datetime(cached["datetime"], utc=True)
            cache_age = (pd.Timestamp.now(tz="UTC") - cached["datetime"].max()).days
            if cache_age < 1:  # 24h 内缓存有效
                logger.debug("使用缓存: %s (%s) %d 根", symbol, ticker, len(cached))
                return cached
        except Exception:
            pass

    df = fetch_yahoo_ohlcv(symbol, timeframe, period_days, proxy_url)
    if len(df) == 0:
        return pd.DataFrame()

    try:
        df.to_csv(cache_path, index=False)
    except Exception:
        pass

    return df


def validate_yahoo_symbols(symbols: list[str]) -> tuple[list[str], list[str]]:
    """校验哪些交易对在 Yahoo Finance 有数据。"""
    import yfinance as yf
    valid, invalid = [], []
    for sym in symbols:
        ticker = _okx_to_yahoo(sym)
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            if info and info.get("regularMarketPrice"):
                valid.append(sym)
                logger.info("  ✅ %s → %s (%.2f)", sym, ticker, info["regularMarketPrice"])
            else:
                invalid.append(sym)
                logger.warning("  ⚠️ %s → %s 无数据", sym, ticker)
        except Exception:
            invalid.append(sym)
            logger.warning("  ⚠️ %s → %s 查询失败", sym, ticker)
    return valid, invalid
