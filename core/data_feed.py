"""
core/data_feed.py — OKX 行情数据获取（纯 requests，不依赖 ccxt）

功能：
  - 直接调 OKX REST API（SWAP 永续合约）
  - 交易对校验
  - K 线拉取 + CSV 缓存（增量更新）
  - 网络重试与优雅降级
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
import urllib3

from core.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

DATA_DIR = PROJECT_ROOT / "data"

# 禁用 SSL 警告（本地代理/VPN 环境）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# OKX 周期映射
TF_MAP = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H",
    "6h": "6H", "12h": "12H",
    "1d": "1D", "1w": "1W", "1M": "1M",
}

# 全局持久 session（复用 TCP 连接，减少 SSL 握手失败）
_global_session: Optional[requests.Session] = None


def _get_session(proxy_url: str = "") -> requests.Session:
    """获取全局持久 session（懒初始化）。"""
    global _global_session
    if _global_session is None:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        s = requests.Session()
        s.verify = False
        # 连接适配器：自动重试 SSL/连接错误
        retry = Retry(
            total=5,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        if proxy_url:
            s.proxies = {"http": proxy_url, "https": proxy_url}
        _global_session = s
    return _global_session


def _api_get(endpoint: str, params: dict | None = None, proxy_url: str = "") -> dict:
    """调 OKX REST API，持久连接 + 自动重试。"""
    url = f"https://www.okx.com{endpoint}"
    session = _get_session(proxy_url)
    for attempt in range(1, 4):
        try:
            r = session.get(url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            if data.get("code") != "0":
                raise RuntimeError(f"OKX API error: {data.get('msg', data)}")
            return data
        except Exception as e:
            logger.warning("API %s 第 %d 次失败：%s", endpoint, attempt, e)
            if attempt < 3:
                time.sleep(3 * attempt)
            else:
                raise


def _symbol_to_inst_id(symbol: str) -> str:
    """ccxt 统一格式 → OKX instId。如 TSLA/USDT:USDT → TSLA-USDT-SWAP"""
    if ":SWAP" in symbol or ":USDT" in symbol:
        base = symbol.split("/")[0]
        quote = symbol.split("/")[1].split(":")[0]
        return f"{base}-{quote}-SWAP"
    # 现货格式
    return symbol.replace("/", "-")


def fetch_swap_instruments(proxy_url: str = "") -> set[str]:
    """拉取 OKX 全部 SWAP 交易对，返回 ccxt 统一符号集合。"""
    data = _api_get("/api/v5/public/instruments", {"instType": "SWAP"}, proxy_url)
    result = set()
    for item in data.get("data", []):
        inst_id = item.get("instId", "")
        parts = inst_id.split("-")
        if len(parts) == 3 and parts[2] == "SWAP":
            base, quote = parts[0], parts[1]
            result.add(f"{base}/{quote}:{quote}")
    return result


def validate_symbols(
    symbols: list[str], proxy_url: str = ""
) -> tuple[list[str], list[str]]:
    """校验哪些交易对在 OKX SWAP 市场上线。返回 (valid, invalid)。"""
    try:
        swap_markets = fetch_swap_instruments(proxy_url)
    except Exception as e:
        logger.error("无法连接 OKX 拉取市场列表：%s", e)
        logger.error("请检查网络连接或代理设置")
        return [], symbols

    valid: list[str] = []
    invalid: list[str] = []
    for sym in symbols:
        if sym in swap_markets:
            valid.append(sym)
        else:
            invalid.append(sym)
            logger.warning("⚠️  交易对 %s 未在 OKX SWAP 上线，已剔除", sym)

    logger.info("交易对校验完成：%d 个有效，%d 个无效", len(valid), len(invalid))
    return valid, invalid


def fetch_ohlcv(
    symbol: str,
    timeframe: str,
    limit: int = 300,
    proxy_url: str = "",
    since: Optional[str] = None,
) -> pd.DataFrame:
    """
    拉取 OKX K 线数据（直接 REST API）。

    返回 DataFrame，列：timestamp, open, high, low, close, volume, datetime
    OKX 返回最新在前，这里反转为最旧在前。
    """
    inst_id = _symbol_to_inst_id(symbol)
    bar = TF_MAP.get(timeframe, timeframe)

    params: dict = {
        "instId": inst_id,
        "bar": bar,
        "limit": str(limit),
    }
    if since:
        # OKX 用 after 参数取更早的数据（毫秒时间戳）
        since_ms = int(pd.Timestamp(since, tz="UTC").timestamp() * 1000)
        params["after"] = str(since_ms)

    data = _api_get("/api/v5/market/candles", params, proxy_url)
    candles = data.get("data", [])

    if not candles:
        return pd.DataFrame()

    # OKX 返回 [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]，最新在前
    rows = []
    for c in candles:
        rows.append({
            "timestamp": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
        })

    df = pd.DataFrame(rows)
    # 反转为最旧在前
    df = df.iloc[::-1].reset_index(drop=True)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def fetch_ohlcv_cached(
    symbol: str,
    timeframe: str,
    limit: int = 300,
    proxy_url: str = "",
    use_cache: bool = True,
    since: Optional[str] = None,
) -> pd.DataFrame:
    """
    带 CSV 缓存的 K 线拉取。

    缓存策略：
      - 首次拉取后存入 data/{symbol}_{timeframe}.csv
      - 后续调用从缓存读取，并增量拉取最新数据追加
      - use_cache=False 强制重新拉取
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = symbol.replace("/", "_").replace("-", "_").replace(":", "_")
    cache_path = DATA_DIR / f"{safe_name}_{timeframe}.csv"

    # 尝试读缓存
    cached_df = pd.DataFrame()
    if use_cache and cache_path.exists():
        try:
            cached_df = pd.read_csv(cache_path, parse_dates=["datetime"])
            cached_df["datetime"] = pd.to_datetime(cached_df["datetime"], utc=True)
        except Exception as e:
            logger.warning("读取缓存 %s 失败：%s，将重新拉取", cache_path, e)
            cached_df = pd.DataFrame()

    # 拉取最新数据
    fetch_since = since
    if not fetch_since and not cached_df.empty:
        last_ts = int(cached_df["timestamp"].iloc[-1])

    try:
        new_df = fetch_ohlcv(
            symbol, timeframe, limit=limit, proxy_url=proxy_url, since=fetch_since
        )
    except Exception as e:
        logger.error("拉取 %s %s 失败：%s，使用缓存数据", symbol, timeframe, e)
        return cached_df

    if new_df.empty:
        return cached_df

    # 合并去重
    if not cached_df.empty:
        combined = pd.concat([cached_df, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["timestamp"], keep="last")
        combined = combined.sort_values("timestamp").reset_index(drop=True)
    else:
        combined = new_df

    # 写回缓存
    try:
        combined.to_csv(cache_path, index=False)
    except Exception as e:
        logger.warning("写入缓存 %s 失败：%s", cache_path, e)

    return combined
