"""Causal S-28 setup, confirmation and execution shared by replay and the shadow scanner."""

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from math import ceil, floor

import numpy as np
import pandas as pd

from entry_rules import LadderRule, LimitRule, make_limits
from single_point import SinglePointRule, plan_single


def iso(t):
    return datetime.fromtimestamp(int(t), timezone.utc).isoformat(timespec="minutes").replace("+00:00", "Z")


@dataclass(frozen=True)
class Bars:
    t: np.ndarray
    end: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray

    def __len__(self):
        return len(self.t)

    def slice(self, at):
        return Bars(*(getattr(self, f.name)[at] for f in fields(self)))

    @classmethod
    def from_frame(cls, frame, seconds=None):
        starts = frame.t.to_numpy(dtype="int64")
        ends = frame.end.to_numpy(dtype="int64") if seconds is None else starts + seconds
        bars = cls(starts, ends, *(frame[c].to_numpy(dtype=float)
                                  for c in ("open", "high", "low", "close", "volume")))
        if (not len(bars) or np.any(ends <= starts) or np.any(ends[:-1] > starts[1:]) or
                not np.isfinite([bars.o, bars.h, bars.l, bars.c, bars.v]).all() or
                np.any(bars.l <= 0) or np.any(bars.v < 0) or
                np.any(bars.h < np.maximum(bars.o, bars.c)) or np.any(bars.l > np.minimum(bars.o, bars.c))):
            raise ValueError("Invalid, overlapping or empty OHLCV series")
        return bars


def aggregate(bars, seconds, offset=0):
    """Fixed UTC buckets. Discard buckets with any missing or unfinished input bar."""
    step = int(bars.end[0] - bars.t[0])
    if seconds % step or np.any(bars.end - bars.t != step):
        raise ValueError("Cannot aggregate these bars into the requested grid")
    bins = ((bars.t - offset) // seconds) * seconds + offset
    frame = pd.DataFrame({"t": bins, "first": bars.t, "last": bars.end,
                          "open": bars.o, "high": bars.h, "low": bars.l, "close": bars.c, "volume": bars.v})
    groups = frame.groupby("t", sort=True).agg(first=("first", "first"), last=("last", "last"),
        n=("open", "size"), open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"), volume=("volume", "sum"))
    full = groups[(groups.n == seconds // step) & (groups["first"] == groups.index)
                  & (groups["last"] == groups.index + seconds)]
    return Bars.from_frame(full.reset_index(), seconds)


def fractal_indices(bars):
    """At index i, the newest possible pivot is centered at i−2."""
    m = np.arange(2, len(bars) - 2)
    low, high = np.full(len(bars), -1, dtype=int), np.full(len(bars), -1, dtype=int)
    bot = m[np.logical_and.reduce([bars.l[m] < bars.l[m + k] for k in (-2, -1, 1, 2)])]
    top = m[np.logical_and.reduce([bars.h[m] > bars.h[m + k] for k in (-2, -1, 1, 2)])]
    low[bot + 2], high[top + 2] = bot, top
    return np.maximum.accumulate(low), np.maximum.accumulate(high)


def features(bars, scaled=False):
    c, h, l = map(pd.Series, (bars.c, bars.h, bars.l))
    ma = c.rolling(20).mean().to_numpy()
    ema26, ema50 = (c.ewm(span=s).mean().to_numpy() for s in (26, 50))
    hh, ll = h.rolling(20).max().to_numpy(), l.rolling(20).min().to_numpy()
    atr = pd.concat((h-l, (h-c.shift()).abs(), (l-c.shift()).abs()), axis=1).max(axis=1).rolling(14).mean()
    atr = atr.to_numpy()
    up, down = (bars.c > ma) & (bars.c > ema26), (bars.c < ma) & (bars.c < ema26)
    limit = 3 * atr if scaled else 0.06 * bars.c
    direction = (up & (hh-bars.c < limit)).astype(int) - (down & (bars.c-ll < limit)).astype(int)
    return ma, ema26, ema50, (hh+ll)/2, atr, up, down, direction


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: int
    setup_open: int
    setup: int
    touch: int
    ready: int
    pivot: str
    pivot_known: int | None
    fractal_at: int | None
    fractal_known: int | None
    mid: float
    atr: float
    risk: float
    gap: int
    breakout: int
    reference: float
    tp1_r: float = 2.56
    tp2_r: float = 5.03

    @property
    def id(self):
        return f"{self.symbol}/{iso(self.setup)}/{'L' if self.side == 1 else 'S'}"

    @property
    def zone(self):
        return self.mid - self.atr, self.mid + self.atr

    @property
    def levels(self):
        return (self.mid-self.side*self.risk, self.mid+self.side*self.tp1_r*self.risk,
                self.mid+self.side*self.tp2_r*self.risk)


def generate(symbol, setup, confirm, *, intraday=False, fractal=False, rth=False,
             overlap=True, one_window=False, warm=70, long_only=None, confirm_seconds=None,
             confirm_offset=1.0, reaction=False):
    """Build confirmed signals in availability order. The touch and confirmation clocks start separately."""
    ma, ema26, ema50, mid20, atr, up, down, sides = features(setup, intraday)
    bottom, top = fractal_indices(setup)
    fb, ft = fractal_indices(confirm) if fractal else (None, None)
    out = []
    if long_only is None:
        long_only=rth
    for i in range(warm, len(setup)):
        side = int(sides[i])
        if side == 0 or (long_only and side < 0) or atr[i] <= 0:
            continue
        above = up if side > 0 else down
        gap = next((k for k in range(1, 4)
                    if side > 0 and setup.l[i-k] <= ema26[i-k] or
                    side < 0 and setup.h[i-k] >= ema26[i-k]), None)
        bk = next((k for k in range(1, 6) if above[i-k] and not above[i-k-1]), None)
        if gap is None or bk is None:
            continue
        fp = int(bottom[i] if side > 0 else top[i])
        candidates = {"MID20": mid20[i], "EMA26": ema26[i], "EMA50": ema50[i], "MA20": ma[i]}
        if fp >= 0:
            candidates["分型"] = setup.l[fp] if side > 0 else setup.h[fp]
        close = setup.c[i]
        def score(price):
            delta = side * (close-price)
            return max(0, 1-abs(delta)/(3*atr[i])) + (delta > 0) + 0.5*(delta <= 1.5*atr[i])
        pivot = max(candidates, key=lambda k: score(candidates[k]))
        mid = float(candidates[pivot])
        zlo, zhi = mid-atr[i], mid+atr[i]
        risk = float(atr[i]) if intraday else mid * (0.022 if side > 0 else 0.0205)
        if zlo <= 0 or (side < 0 and mid-5.03*risk <= 0):
            continue
        t = int(setup.end[i])
        j0 = int(np.searchsorted(confirm.t, t))
        if rth:
            stop_touch = min(j0+4, len(confirm))
        else:
            window = 12 * (confirm_seconds or int(confirm.end[0]-confirm.t[0]))
            stop_touch = int(np.searchsorted(confirm.end, t+window, side="right"))
        for j in range(j0, stop_touch):
            near = confirm.l[j] <= zhi if side > 0 else confirm.h[j] >= zlo
            if not near or (overlap and (confirm.l[j] > zhi or confirm.h[j] < zlo)):
                continue
            if rth:
                stop_confirm = min(j+5, len(confirm))
            else:
                deadline = t+window if one_window else int(confirm.end[j])+window
                stop_confirm = int(np.searchsorted(confirm.end, deadline, side="right"))
            threshold = mid + side*atr[i]*confirm_offset
            for k in range(j, stop_confirm):
                if side*(confirm.c[k] - threshold) <= 0:
                    continue
                wick = confirm.l[k] if side > 0 else confirm.h[k]
                if reaction and (side*(wick-threshold) > 0 or side*(confirm.c[k]-confirm.o[k]) <= 0):
                    continue
                center = -1
                if fractal:
                    center = int(fb[k] if side > 0 else ft[k])
                    if center < j:
                        continue
                    wick = confirm.l[center] if side > 0 else confirm.h[center]
                    if not zlo <= wick <= zhi:
                        continue
                out.append(Signal(symbol, side, int(setup.t[i]), t, int(confirm.end[j]), int(confirm.end[k]),
                    pivot, int(setup.end[fp+2]) if pivot == "分型" else None,
                    int(confirm.t[center]) if center >= 0 else None,
                    int(confirm.end[center+2]) if center >= 0 else None,
                    mid, float(atr[i]), risk, gap, bk, float(confirm.c[k])))
                break
            break
    return sorted(out, key=lambda s: (s.ready, s.setup, s.symbol))


@dataclass(frozen=True)
class Policy:
    ttl: int
    hold: int | None
    step: int
    rth: bool = False
    lag: int = 300


@dataclass(frozen=True)
class Cost:
    maker: float
    taker: float
    slip: float
    funding: float
    penetrate: float


# Illustrative scenarios in fractions of order notional, independent of user fee tier.
BASE = Cost(0.0002, 0.0005, 0.0002, 0.0001, 0.0001)
STRESS = Cost(0.0005, 0.0010, 0.0006, 0.0002, 0.0002)
US_COST = Cost(0.0002, 0.0005, 0.0005, 0, 0.0001)
US_STRESS = Cost(0.0005, 0.0010, 0.0015, 0, 0.0002)
PLANS = {
    "edge": ((1.0, 1.0),),
    "mid": ((0.0, 1.0),),
    "two": ((1.0, 0.6), (0.0, 0.4)),
    "shallow": ((0.5, 0.6), (0.0, 0.4)),
}


def bracket(signal, model, tick=None, quote=None):
    sl, tp1, tp2 = signal.levels
    side = signal.side
    if isinstance(model, SinglePointRule):
        plan = plan_single(side,*signal.zone,signal.mid,sl,model,STRESS,tick=tick)
        return (plan.stop,plan.target,None,[(plan.entry,1.0)]) if plan else (sl,None,None,[])
    if isinstance(model, (LimitRule, LadderRule)):
        return make_limits(side,*signal.zone,signal.mid,sl,tp1,tp2,model,tick=tick,bid=quote,ask=quote)
    if tick:
        sl = (ceil(sl/tick) if side > 0 else floor(sl/tick)) * tick
        tp1 = (floor(tp1/tick) if side > 0 else ceil(tp1/tick)) * tick
        tp2 = (floor(tp2/tick) if side > 0 else ceil(tp2/tick)) * tick
    orders = []
    for offset, weight in PLANS.get(model, ()):
        price = signal.mid + side * offset * signal.atr
        if tick:
            price = (floor(price/tick) if side > 0 else ceil(price/tick)) * tick
        if side*(price-sl) > 0 and side*(tp1-price) > 0:
            if orders and orders[-1][0] == price:
                orders[-1] = price, orders[-1][1] + weight
            else:
                orders.append((price, weight))
    return sl, tp1, tp2, orders


TERMINAL = {"TP", "TP1+TP2", "TP1+SL", "SL", "TIME", "TP1+TIME"}


def simulate(s, bars, policy, model, cost, *, detail=False, cap=0.15, tick=None, session_gaps=()):
    """Replay marketable open or resting limits. Returns cash P&L per initial equity."""
    start = int(np.searchsorted(bars.t, s.ready+policy.lag))
    single = isinstance(model, SinglePointRule)
    quote=bars.o[start] if isinstance(model,(LimitRule,LadderRule,SinglePointRule)) and start<len(bars) else None
    sl, tp1, tp2, limits = bracket(s, model, tick, quote)
    plan = [(p, w*cap) for p, w in limits]
    t, end, o, h, l, c = bars.t, bars.end, bars.o, bars.h, bars.l, bars.c
    expire_t = s.ready + policy.ttl*policy.step
    expire = min(start+policy.ttl, len(t)) if policy.rth else int(np.searchsorted(t, expire_t))
    side = s.side
    pending = list(plan)
    qty = initial_qty = used = entry_paid = fees = slip_cost = carry = pnl = 0.0
    first_fill = None
    half = False
    events = []
    collision = 0
    deadline = None

    def record(label, stamp, price=None, amount=None, intrabar=False):
        if detail:
            events.append({"kind": label, "time": int(stamp), "price": float(price) if price is not None else None,
                           "qty": float(amount) if amount is not None else None, "intrabar": intrabar})

    def fill(price, budget, stamp, market=False, at_open=False):
        nonlocal qty, initial_qty, used, entry_paid, fees, slip_cost, first_fill
        actual = price*(1+side*cost.slip) if market else price
        units = budget/actual
        qty += units
        initial_qty += units
        used += budget
        entry_paid += budget
        fees += budget*(cost.taker if market else cost.maker)
        slip_cost += abs(actual-price)*units
        if first_fill is None:
            first_fill = int(stamp)
        record("ENTRY", stamp, actual, units, not (market or at_open))

    def close(price, stamp, label, fraction=1.0, market=False, at_open=False):
        nonlocal qty, pnl, fees, slip_cost
        units = qty*fraction
        actual = price*(1-side*cost.slip) if market else price
        pnl += side*(actual - entry_paid/initial_qty)*units
        fees += actual*units*(cost.taker if market else cost.maker)
        slip_cost += abs(actual-price)*units
        qty -= units
        record(label, stamp, actual, units, not at_open)

    def done(status, stamp, mark=None):
        ret = pnl-fees-carry
        if mark is not None and qty > 0:
            ret += side*(mark-entry_paid/initial_qty)*qty
        row = {"status": status, "terminal": int(stamp), "net": float(ret), "filled": float(used),
               "first_fill": first_fill, "tp1_hit": half or status=="TP", "ambiguous": collision}
        if detail:
            row.update(entry=float(entry_paid/initial_qty) if initial_qty else None, gross=float(pnl),
                       fees=float(fees), funding=float(carry), slip=float(slip_cost), plan=plan,
                       quote=float(quote) if quote is not None else None,
                       levels=[sl,tp1,tp2], events=events,
                       activation=int(t[start]) if start < len(t) else s.ready+policy.lag,
                       expiry=int(t[start+policy.ttl]) if policy.rth and start+policy.ttl < len(t) else
                              (None if policy.rth else expire_t))
        return row

    if model != "market" and not plan:
        return done("INVALID", s.ready)
    if single and s.side*(s.reference-tp1) >= 0:
        return done("RUNAWAY", s.ready)
    if single and s.side*(s.reference-sl) <= 0:
        return done("INVALID", s.ready)
    if start >= len(t):
        return done("PENDING", s.ready)
    if start >= expire:
        return done("EXPIRED", expire_t)

    gap_index = int(np.searchsorted(session_gaps, s.ready))
    next_session_gap = session_gaps[gap_index] if gap_index < len(session_gaps) else None
    for j in range(start, len(t)):
        # Crypto feed gaps cannot be treated as invisible execution time.
        if next_session_gap is not None and t[j] >= next_session_gap:
            return done("GAP", next_session_gap)
        if not policy.rth and ((j > start and t[j] != end[j-1]) or
                               (j == start and t[j] >= s.ready+policy.lag+policy.step)):
            return done("GAP", end[j-1] if j else t[j])
        if j >= expire:
            if pending:
                record("CANCEL_TTL", t[j])
            pending.clear()
            if not qty:
                return done("EXPIRED", t[j])
        if deadline is not None and j >= deadline:
            close(o[j], t[j], "TIME", market=True, at_open=True)
            return done("TP1+TIME" if half else "TIME", t[j])
        if qty and cost.funding and t[j] % (8*3600) == 0:
            carry += qty * o[j] * cost.funding
        if j == start:
            if side*(o[j]-sl) <= 0:
                return done("INVALID", t[j])
            if side*(tp1-o[j]) <= 0:
                return done("RUNAWAY", t[j])
            if model == "market":
                p = o[j]*(1+side*cost.slip)
                if side*(tp1-p) <= 0:
                    return done("INVALID", t[j])
                fill(o[j], cap, t[j], market=True)
                deadline = None if policy.hold is None else j+policy.hold
            else:
                for price, budget in pending.copy():
                    if side*(o[j]-price) <= 0:
                        record("CANCEL_MARKETABLE", t[j], price)
                        pending.remove((price,budget))
                if not pending:
                    return done("INVALID", t[j])

        adverse, favorable = (l[j], h[j]) if side > 0 else (h[j], l[j])
        target = tp2 if half else tp1
        stop_hit = side*(adverse-sl) <= 0
        target_hit = side*(favorable-target) >= target*cost.penetrate
        # If a target is already exceeded at the bar open while flat, cancel the idea.
        if not qty and side*(o[j]-tp1) >= 0:
            record("CANCEL_TP1", t[j])
            return done("RUNAWAY", t[j])
        if qty and side*(o[j]-sl) <= 0:
            close(o[j], t[j], "SL", market=True, at_open=True)
            return done("TP1+SL" if half else "SL", t[j])
        # The opening print is observed before any intrabar pullback. Cancel resting
        # legs once an existing position reaches its first target at that print.
        if qty and side*(o[j]-target) >= target*cost.penetrate:
            if single:
                close(tp1, t[j], "TP", at_open=True)
                return done("TP", t[j])
            if half:
                close(tp2, t[j], "TP2", at_open=True)
                return done("TP1+TP2", t[j])
            close(tp1, t[j], "TP1", 0.5, at_open=True)
            half = True
            pending.clear()
            target_hit = side*(favorable-tp2) >= tp2*cost.penetrate
        new_limit = False
        if pending:
            for price, budget in pending.copy():
                if side*(price-adverse) >= price*cost.penetrate:
                    at_open = side*(price-o[j]) >= price*cost.penetrate
                    fill(price, budget, t[j] if at_open else end[j], at_open=at_open)
                    pending.remove((price, budget))
                    new_limit = True
                    if deadline is None and policy.hold is not None:
                        deadline = j+policy.hold
        if not qty:
            if stop_hit or target_hit:
                record("CANCEL_SL" if stop_hit else "CANCEL_TP1", end[j])
                return done("INVALID" if stop_hit else "RUNAWAY", end[j])
            continue
        if stop_hit:
            collision += int(target_hit or new_limit)
            at_open = side*(o[j]-sl) <= 0
            close(o[j] if at_open else sl, t[j] if at_open else end[j],
                  "SL", market=True, at_open=at_open)
            # If a resting order gaps through stop, both fill and stop belong to this bar.
            return done("TP1+SL" if half else "SL", end[j] if new_limit else (t[j] if at_open else end[j]))
        if target_hit and new_limit:
            # The high/low can precede the entry. Credit neither TP in this bar.
            collision += 1
            pending.clear()
            record("CANCEL_ORDER_AMBIGUITY", end[j])
            continue
        if target_hit:
            if single:
                close(tp1, end[j], "TP")
                return done("TP", end[j])
            if not half:
                close(tp1, end[j], "TP1", 0.5)
                half = True
                pending.clear()
            if side*(favorable-tp2) >= tp2*cost.penetrate:
                close(tp2, end[j], "TP2")
                return done("TP1+TP2", end[j])

    if not qty and (not policy.rth and end[-1] >= expire_t or policy.rth and start+policy.ttl <= len(t)):
        record("CANCEL_TTL", end[-1])
        return done("EXPIRED", end[-1])
    return done("OPEN" if qty else "PENDING", end[-1], c[-1] if qty else None)


def replay(signals, feed, policy, model, cost, *, detail=False, ticks=None, session_gaps=None):
    out, busy = [], {}
    for s in signals:
        until, blocker = busy.get(s.symbol, (0,None))
        if s.ready < until:
            r = {"status": "LOCKED", "net": 0.0, "filled": 0.0, "first_fill": None,
                 "terminal": until, "blocked_by":blocker, "tp1_hit": False, "ambiguous": 0}
        else:
            r = simulate(s, feed[s.symbol], policy, model, cost, detail=detail,
                         tick=(ticks or {}).get(s.symbol), session_gaps=(session_gaps or {}).get(s.symbol, ()))
            busy[s.symbol] = (r["terminal"],s.id)
        out.append((s, r))
    return out
