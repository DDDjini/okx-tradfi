"""Fetch the expanded market panel and read regular-hours SIP / Yahoo data."""

import concurrent.futures
import json
from datetime import timedelta

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from engine import Bars, iso
from market_data import CACHE, COLS, CRYPTO, CUTOFF, binance_archive, clean, dates, yahoo


LONG_START = int(pd.Timestamp("2022-01-01",tz="UTC").timestamp())
SIP = ("SPY","QQQ","AAPL","NVDA","TSLA","COIN","MSTR","GLD","USO","BNO")
STOCKS = ("SPY","QQQ","IWM","DIA","AAPL","MSFT","NVDA","AMD","TSLA","META","AMZN","GOOGL",
          "COIN","MSTR","AVGO","NFLX","PLTR","JPM","XOM","UNH","COST","SMH","XLE")
ETPS = ("GLD","IAU","USO","BNO")
FUTURES = ("GC=F","CL=F")
UNIVERSE = (*STOCKS,*ETPS,*FUTURES)
NY = "America/New_York"
CALENDAR_START = "2021-11-10"


def instrument_group(symbol):
    if symbol in ("GLD","IAU","GC=F"):
        return "黄金"
    if symbol in ("USO","BNO","CL=F"):
        return "原油"
    return "美股"


def acquire():
    with concurrent.futures.ThreadPoolExecutor(10) as pool:
        charts = {(s,i):pool.submit(yahoo,s,i) for s in UNIVERSE for i in ("60m","1d")}
        archive = {
            s:[pool.submit(binance_archive,s,"monthly",d.strftime("%Y-%m"))
               for d in dates("2022-01-01","2026-11-01","MS")] +
              [pool.submit(binance_archive,s,"daily",d.strftime("%Y-%m-%d"))
               for d in dates("2026-11-01","2026-11-10","D")] for s in CRYPTO
        }
        failures = []
        for (s,interval),task in charts.items():
            try:
                value=task.result()
                print("Yahoo",s,interval,len(value["timestamp"]),flush=True)
            except Exception as exc:
                failures.append(f"Yahoo {s} {interval}: {exc}")
        for s,tasks in archive.items():
            try:
                frame=clean(pd.concat([f.result() for f in tasks],ignore_index=True),LONG_START,CUTOFF)
                frame.to_csv(CACHE/f"binance-long-{s}-5m.csv.gz",index=False,
                             compression={"method":"gzip","mtime":0})
                print("Binance",s,len(frame),iso(frame.t.iloc[0]),iso(frame.t.iloc[-1]+300),flush=True)
            except Exception as exc:
                failures.append(f"Binance {s}: {exc}")
    for error in failures:
        print(error,flush=True)
    return 1 if failures else 0


def yahoo_data(symbol,tf):
    path=CACHE/f"yahoo-{symbol}-{tf}-{CUTOFF}.json"
    return json.loads(path.read_text(encoding="utf-8"))["chart"]["result"][0]


def local_date(t):
    return str(pd.Timestamp(t,unit="s",tz="UTC").tz_convert(NY).date())


def quotes(data):
    q=data["indicators"]["quote"][0]
    return {int(t):tuple(q[c][i] for c in COLS) for i,t in enumerate(data["timestamp"])
            if all(q[c][i] is not None for c in COLS)}


def sessions():
    return xcals.get_calendar("XNYS",start=CALENDAR_START,end="2026-11-10").schedule


def grouped(rows):
    return (rows[0][0],rows[-1][1],rows[0][2],max(x[3] for x in rows),
            min(x[4] for x in rows),rows[-1][5],sum(x[6] for x in rows))


def rows_to_bars(rows):
    return Bars.from_frame(pd.DataFrame(rows,columns=["t","end",*COLS]))


def regular_market(symbol, calendar, provider):
    daily_file=yahoo_data(symbol,"1d")
    daily={local_date(t):row for t,row in quotes(daily_file).items()}
    splits=[(local_date(x["date"]),x["numerator"]/x["denominator"])
            for x in daily_file.get("events",{}).get("splits",{}).values() if x["date"]<CUTOFF]
    if provider=="Alpaca SIP":
        parts=[pd.read_csv(CACHE/f"alpaca-sip-{symbol}-{year}-30m.csv")
               for year in range(2022,pd.Timestamp(CUTOFF,unit="s",tz="UTC").year+1)]
        joined=pd.concat(parts,ignore_index=True)
        if joined.t.duplicated().any():
            raise ValueError(f"{symbol}: duplicate SIP bars")
        raw={int(x[0]):x[1:] for x in joined.itertuples(index=False,name=None)}
        seconds=1800
    else:
        raw=quotes(yahoo_data(symbol,"60m"))
        seconds=3600
    start,end=min(raw),min(max(raw)+seconds,CUTOFF)
    samples, setups, confirms, gaps, mismatch=[],[],[],[],[]
    for day,ses in calendar.iterrows():
        a,b=int(ses["open"].timestamp()),int(ses["close"].timestamp())
        date=str(day.date())
        if b>CUTOFF or date not in daily:
            continue
        # Yahoo's complete daily OHLC seeds warm-up before intraday availability.
        if provider=="Yahoo 60m":
            setups.append((a,b,*daily[date]))
        elif a<LONG_START:
            continue
        if a<start or b>end:
            continue
        expected=list(range(a,b,seconds))
        if any(t not in raw for t in expected):
            gaps.append(a)
            if provider=="Alpaca SIP":
                setups.append((a,b,*daily[date]))
            continue
        raw_close=raw[expected[-1]][3]
        factor=float(np.prod([f for d,f in splits if d>date])) if provider=="Alpaca SIP" else 1.0
        if provider=="Yahoo 60m" and abs(raw_close/daily[date][3]-1)>0.05:
            proposed=float(np.prod([f for d,f in splits if d>date]))
            if proposed!=1 and abs(raw_close/proposed/daily[date][3]-1)<0.025:
                factor=proposed
            else:
                gaps.append(a)
                continue
        dif=abs(raw_close/factor/daily[date][3]-1)
        mismatch.append(dif)
        if dif>0.05:
            gaps.append(a)
            if provider=="Alpaca SIP":
                setups.append((a,b,*daily[date]))
            continue
        rows=[(t,min(t+seconds,b),*(x/factor for x in raw[t][:4]),raw[t][4]*factor) for t in expected]
        samples.extend(rows)
        if provider=="Alpaca SIP":
            setups.append(grouped(rows))
        n=14400//seconds
        confirms.extend(grouped(rows[j:j+n]) for j in range(0,len(rows),n))
    fine,setup,confirm=map(rows_to_bars,(samples,setups,confirms))
    return fine,setup,confirm,gaps,{"symbol":symbol,"provider":provider,
        "start":iso(fine.t[0]),"end":iso(fine.end[-1]),"bars":len(fine),"gaps":len(gaps),
        "worst_daily_close_diff":max(mismatch,default=0),"split_events":len(splits)}


def futures_market(symbol):
    """Yahoo continuous series grouped into 18:00–17:00 New York sessions."""
    hourly=quotes(yahoo_data(symbol,"60m"))
    hourly={t:x for t,x in hourly.items() if t%3600==0 and t+3600<=CUTOFF}
    groups={}
    for t in sorted(hourly):
        et=pd.Timestamp(t,unit="s",tz="UTC").tz_convert(NY)
        if et.hour==17:
            continue
        label=et.date()+timedelta(days=et.hour>=18)
        if label.weekday()<5:
            groups.setdefault(label,[]).append((t,t+3600,*hourly[t]))
    days, confirmations, samples, gaps=[],[],[],[]
    for label,rows in groups.items():
        a=int(pd.Timestamp(label-timedelta(days=1),tz=NY).replace(hour=18).timestamp())
        b=int(pd.Timestamp(label,tz=NY).replace(hour=17).timestamp())
        # Omit partial holidays and unknown vendor gaps from both confirmation and price paths.
        if [r[0] for r in rows]!=list(range(a,b,3600)):
            gaps.append(a)
            continue
        days.append(grouped(rows))
        samples.extend(rows)
        confirmations.extend(grouped(rows[j:j+4]) for j in range(0,len(rows),4))
    fine,setup,confirm=map(rows_to_bars,(samples,days,confirmations))
    return fine,setup,confirm,gaps,{"symbol":symbol,"provider":"Yahoo 连续期货",
        "start":iso(fine.t[0]),"end":iso(fine.end[-1]),"bars":len(fine),"gaps":len(gaps),
        "worst_daily_close_diff":None,"split_events":0}


if __name__=="__main__":
    raise SystemExit(acquire())
