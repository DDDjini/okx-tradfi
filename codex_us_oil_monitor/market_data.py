"""Read-only market data acquisition with a fixed research cutoff and local raw cache."""

import argparse
import concurrent.futures
import hashlib
import io
import json
import os
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
CACHE.mkdir(exist_ok=True)
CRYPTO = ("BTC", "ETH")
US = ("SPY", "QQQ", "COIN", "MSTR", "NVDA", "TSLA", "AMD", "AAPL", "MSFT")
COLS = ["open", "high", "low", "close", "volume"]
START = int(pd.Timestamp("2024-08-21", tz="UTC").timestamp())
MEXC_START = int(pd.Timestamp("2026-05-01", tz="UTC").timestamp())
CUTOFF = int(pd.Timestamp("2026-11-10", tz="UTC").timestamp())
MEXC = "https://api.mexc.com"
BINANCE = "https://data.binance.vision/data/futures/um"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}


def get(url, params=None, attempts=3):
    for attempt in range(attempts):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=(7, 24))
            r.raise_for_status()
            return r.content
        except requests.RequestException as exc:
            if attempt + 1 == attempts or (
                isinstance(exc, requests.HTTPError) and exc.response.status_code in (400, 401, 403, 404)
            ):
                raise
            time.sleep(1 + attempt)


def cached(name, url, params=None):
    path = CACHE / name
    if path.exists():
        return path.read_bytes()
    payload = get(url, params)
    part = path.with_suffix(path.suffix + ".partial")
    part.write_bytes(payload)
    part.replace(path)
    return payload


def binance_archive(symbol, partition, day, interval="5m"):
    name = f"{symbol}USDT-{interval}-{day}.zip"
    url = f"{BINANCE}/{partition}/klines/{symbol}USDT/{interval}/{name}"
    raw = cached(name, url)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        content = archive.read(archive.namelist()[0])
    frame = pd.read_csv(io.BytesIO(content), header=None, usecols=range(6), names=["t", *COLS])
    frame["t"] = pd.to_numeric(frame["t"], errors="coerce")
    frame = frame.dropna(subset=["t"])
    frame["t"] = frame["t"].astype("int64") // 1000
    return frame.astype({col: "float64" for col in COLS})


def mexc_future(symbol, start, end, interval="Min5", cache=False):
    url = f"{MEXC}/api/v1/contract/kline/{symbol}_USDT"
    params = {"interval": interval, "start": start, "end": end - 1}
    name = f"mexc-futures-{symbol}-{interval}-{start}-{end}.json"
    raw = json.loads(cached(name, url, params) if cache else get(url, params))
    if not raw.get("success") or not raw.get("data") or not raw["data"].get("time"):
        raise ValueError(f"Empty MEXC futures result for {symbol}, {interval}, {start}–{end}")
    data = raw["data"]
    return pd.DataFrame({"t": data["time"], **{c: data["vol" if c == "volume" else c] for c in COLS}})


def mexc_spot(symbol, start, end, interval, cache=False):
    url = f"{MEXC}/api/v3/klines"
    params = {"symbol": f"{symbol}USDT", "interval": interval,
              "startTime": start * 1000, "endTime": end * 1000 - 1, "limit": 500}
    name = f"mexc-spot-{symbol}-{interval}-{start}-{end}.json"
    raw = json.loads(cached(name, url, params) if cache else get(url, params))
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"Empty MEXC spot result for {symbol}, {interval}, {start}–{end}")
    return pd.DataFrame([(int(x[0]) // 1000, *map(float, x[1:6])) for x in raw], columns=["t", *COLS])


def yahoo(symbol, interval):
    range_ = "730d" if interval == "60m" else "5y"
    params = {"interval": interval, "range": range_, "includePrePost": "false", "events": "splits"}
    data = json.loads(cached(f"yahoo-{symbol}-{interval}-{CUTOFF}.json", f"{YAHOO}/{symbol}", params))
    return data["chart"]["result"][0]


def contract_spec(symbol):
    data = json.loads(get(f"{MEXC}/api/v1/contract/detail", {"symbol": f"{symbol}_USDT"}))
    values = data["data"]
    return {key: values[key] for key in ("priceUnit", "contractSize", "volUnit", "minVol", "state")}


def clean(frame, start, end):
    frame = frame.loc[(frame.t >= start) & (frame.t < end)].sort_values("t")
    dupe = int(frame.t.duplicated().sum())
    if dupe:
        raise ValueError(f"Found {dupe} duplicate bar starts")
    return frame.reset_index(drop=True)


def dates(a, b, freq):
    return pd.date_range(a, b, freq=freq, inclusive="left", tz="UTC")


def acquire(selection):
    jobs = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for symbol in CRYPTO:
            if "binance" in selection:
                jobs["binance", symbol, "5m"] = [
                    pool.submit(binance_archive, symbol, "monthly", d.strftime("%Y-%m"))
                    for d in dates("2024-08-01", "2026-11-01", "MS")
                ] + [
                    pool.submit(binance_archive, symbol, "daily", d.strftime("%Y-%m-%d"))
                    for d in dates("2026-11-01", "2026-11-10", "D")
                ]
            if "mexc" in selection:
                # At most 1,728 bars per request. The futures endpoint accepts seconds.
                step = 6 * 86400
                jobs["mexc-futures", symbol, "5m"] = [
                    pool.submit(mexc_future, symbol, t, min(t + step, CUTOFF), cache=True)
                    for t in range(MEXC_START, CUTOFF, step)
                ]
                for interval, sec in (("4h", 14400), ("1d", 86400)):
                    step = 400 * sec
                    jobs["mexc-spot", symbol, interval] = [
                        pool.submit(mexc_spot, symbol, t, min(t + step, CUTOFF), interval, True)
                        for t in range(START, CUTOFF, step)
                    ]
        if "us" in selection:
            for symbol in US:
                jobs["yahoo", symbol, "chart"] = [
                    pool.submit(yahoo, symbol, interval) for interval in ("60m", "1d")
                ]
        failures = []
        for (venue, symbol, interval), futures in jobs.items():
            try:
                chunks = [future.result() for future in futures]
                if venue == "yahoo":
                    print(venue, symbol, *[len(c["timestamp"]) for c in chunks], flush=True)
                    continue
                data = clean(pd.concat(chunks, ignore_index=True), START, CUTOFF)
                path = CACHE / f"{venue}-{symbol}-{interval}.csv.gz"
                data.to_csv(path, index=False, compression={"method": "gzip", "mtime": 0})
                digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
                print(venue, symbol, interval, len(data), int(data.t.iloc[0]), int(data.t.iloc[-1]),
                      digest, flush=True)
            except Exception as exc:
                failures.append(f"{venue} {symbol} {interval}: {exc}")
        for failure in failures:
            print(failure, flush=True)
        return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="*", default=["binance", "mexc", "us"])
    args = parser.parse_args()
    if not set(args.sources) <= {"binance", "mexc", "us"}:
        parser.error("Sources are binance, mexc and us.")
    raise SystemExit(acquire(args.sources))
