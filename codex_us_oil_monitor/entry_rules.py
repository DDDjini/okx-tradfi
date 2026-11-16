"""Translate a frozen signal bracket into concrete maker-limit price and size proposals."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from math import ceil, floor, isclose, isfinite


@dataclass(frozen=True)
class LimitRule:
    id: str
    name: str
    legs: tuple
    quote_clip: bool = False
    min_rr: float = 0.0
    spacing: float = 0.15
    buffer_bp: float = 1.0


@dataclass(frozen=True)
class LadderRule:
    id: str
    name: str
    legs: tuple
    mapping: str = "facing"

    def __post_init__(self):
        if (self.mapping not in ("facing", "low") or not 3 <= len(self.legs) <= 5 or
                not isclose(sum(w for _, w in self.legs), 1, abs_tol=1e-9) or
                any(not 0 <= d <= 1 or not 0 < w <= 1 for d, w in self.legs) or
                any(a[0] >= b[0] for a, b in zip(self.legs, self.legs[1:]))):
            raise ValueError("Invalid 3–5 level ladder")


def tick_price(value, tick, up):
    if not tick:
        return value
    n = value/tick
    return round((ceil(n-1e-9) if up else floor(n+1e-9))*tick,12)


def ladder_prices(side, lo, hi, legs, mapping, tick=None):
    """Map each planned level by its original leg index, before testing eligibility."""
    return [tick_price(hi - d*(hi-lo) if mapping == "facing" and side > 0
                       else lo + d*(hi-lo), tick, side < 0) for d, _ in legs]


def make_limits(side, lo, hi, mid, sl, tp1, tp2, rule, *, tick=None, bid=None, ask=None):
    """Offsets are relative to MID and the appropriate zone edge. Output weights are unreallocated."""
    nums=(lo,hi,mid,sl,tp1,tp2)
    if (side not in (-1,1) or not all(isfinite(x) and x>0 for x in nums)
            or not lo<hi or not lo<=mid<=hi or not side*sl<side*mid<side*tp1<side*tp2):
        raise ValueError("Invalid zone or stop/target ordering")
    if tick is not None and (not isfinite(tick) or tick<=0):
        raise ValueError("Price tick must be positive")
    if any(x is not None and (not isfinite(x) or x<=0) for x in (bid,ask)):
        raise ValueError("Quotes must be positive")
    if bid is not None and ask is not None and bid>ask:
        raise ValueError("Bid exceeds ask")
    if isinstance(rule,LadderRule):
        sl,t1,t2=tick_price(sl,tick,side>0),tick_price(tp1,tick,side<0),tick_price(tp2,tick,side<0)
        if not side*sl<side*t1<side*t2:
            return sl,t1,t2,[]
        weights={}
        for p,(_,w) in zip(ladder_prices(side,lo,hi,rule.legs,rule.mapping,tick),rule.legs):
            if lo <= p <= hi and side*sl < side*p < side*t1:
                weights[p]=weights.get(p,0)+w
        return sl,t1,t2,sorted(weights.items(),key=lambda x:side*x[0],reverse=True)
    if (not isfinite(rule.min_rr) or rule.min_rr<0 or not isfinite(rule.spacing) or rule.spacing<0
            or not isfinite(rule.buffer_bp) or rule.buffer_bp<0 or not rule.legs
            or len(rule.legs)>3 or not isclose(sum(w for _,w in rule.legs),1,abs_tol=1e-9)
            or any(not -1<=x<=1 or not 0<w<=1 for x,w in rule.legs)):
        raise ValueError("Invalid entry rule")
    sl,t1,t2=tick_price(sl,tick,side>0),tick_price(tp1,tick,side<0),tick_price(tp2,tick,side<0)
    if not side*sl<side*t1<side*t2:
        return sl,t1,t2,[]
    mid_o=side*mid
    far,near=sorted((side*lo,side*hi))
    bounds=min(near,(side*t1+rule.min_rr*side*sl)/(1+rule.min_rr))
    q=ask if side>0 else bid
    if rule.quote_clip and q is not None:
        ref=(bid+ask)/2 if bid is not None and ask is not None else q
        if not side*sl<side*ref<side*t1:
            return sl,t1,t2,[]
        bounds=min(bounds,side*q-max(tick or 0,q*rule.buffer_bp/10000))
    # The target itself is excluded, even when the rule's minimum RR is zero.
    if bounds>=side*t1:
        bounds=side*t1-max(tick or 0,abs(t1)*1e-10)
    space=max(tick or 0,rule.spacing*(hi-lo)/2)
    orders=[]
    for fraction,weight in sorted(rule.legs,reverse=True):
        raw=mid_o+fraction*((near-mid_o) if fraction>=0 else (mid_o-far))
        room=bounds
        if orders:
            room=min(room,side*orders[-1][0]-space)
        price=tick_price(side*min(raw,room),tick,side<0)
        if far<=side*price<=near and side*sl<side*price<side*t1:
            orders.append((price,weight))
    return sl,t1,t2,orders


def size_orders(orders, equity, cap, *, unit=None, step=None, minimum=None):
    """Compute optional contract counts, with a hard cap on nominal equity share."""
    cap=Decimal(str(cap))
    if not cap.is_finite() or not Decimal(0)<cap<=Decimal("0.15"):
        raise ValueError("Notional cap must be >0 and <=15% of equity")
    if equity is not None and (not equity.is_finite() or equity<=0):
        raise ValueError("Equity must be finite and positive")
    sizes=[]
    remaining=cap
    for price,weight in orders:
        share=min(remaining,(cap*Decimal(str(weight))).quantize(Decimal("0.000000000001")))
        remaining-=share
        row={"limit":price,"equity_share":float(share),"type":"limit","post_only":True}
        if equity is not None:
            budget=equity*share
            row["max_notional"]=float(budget)
            if unit is not None:
                unit_d,step_d,min_d=(Decimal(str(x)) for x in (unit,step,minimum))
                if not all(x.is_finite() and x>0 for x in (unit_d,step_d,min_d)):
                    raise ValueError("Invalid contract multiplier or steps")
                contract_price=Decimal(str(price))*unit_d
                contracts=(budget/contract_price/step_d).to_integral_value(rounding=ROUND_DOWN)*step_d
                if contracts<min_d:
                    contracts=Decimal(0)
                row.update(contracts=float(contracts),order_notional=float(contracts*contract_price))
        sizes.append(row)
    return sizes
