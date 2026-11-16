"""One deterministic limit, one full-position target, and one stop."""

from dataclasses import dataclass
from math import isfinite

from entry_rules import tick_price


@dataclass(frozen=True)
class SinglePointRule:
    id: str
    method: str
    depth: float
    target_r: float | None
    min_rr: float
    stop_r: float = 1.0

    def __post_init__(self):
        if (self.method not in {"nearest","facing","low"} or not 0 <= self.depth <= 1
                or not isfinite(self.min_rr) or self.min_rr <= 0 or not 0 < self.stop_r <= 1
                or (self.target_r is not None and (not isfinite(self.target_r) or self.target_r <= 0))
                or (self.method == "nearest" and self.target_r is None)):
            raise ValueError("Invalid single-point rule")


@dataclass(frozen=True)
class SinglePoint:
    entry: float
    stop: float
    target: float
    reward: float
    risk: float

    @property
    def rr(self):
        return self.reward/self.risk


def plan_single(side, lo, hi, mid, stop, rule, cost, *, tick=None):
    """Use only the confirmed zone. Apply the net R:R floor after price rounding."""
    if (side not in (-1, 1) or not all(isfinite(x) and x > 0 for x in (lo, hi, mid, stop))
            or not lo < hi or not lo <= mid <= hi or side*(mid-stop) <= 0):
        raise ValueError("Invalid zone or stop")
    if tick is not None and (not isfinite(tick) or tick <= 0):
        raise ValueError("Invalid price tick")
    unit_r = abs(mid-stop)
    sl = tick_price(mid-side*unit_r*rule.stop_r, tick, side > 0)
    if sl <= 0 or side*(mid-sl) <= 0:
        return None
    far, near = sorted((side*lo, side*hi))
    stop_fill = sl*(1-side*cost.slip)
    fee = cost.maker
    rr = rule.min_rr
    target = None
    if rule.target_r is not None:
        target = tick_price(mid+side*rule.target_r*unit_r, tick, side < 0)
        if target <= 0:
            return None
    if rule.method == "nearest":
        # Oriented price ceiling implied by net reward >= rr * net stop loss.
        bound = ((side-fee)*target + rr*(side-cost.taker)*stop_fill) / ((1+rr)*(1+side*fee))
        raw_entry = side*min(near,bound)
    elif rule.method == "facing":
        raw_entry = side*(near-rule.depth*(near-far))
    elif rule.method == "low":
        raw_entry = lo+rule.depth*(hi-lo)
    else:
        raise ValueError(f"Unknown single-point rule {rule.method}")
    entry = tick_price(raw_entry,tick,side < 0)
    if not lo <= entry <= hi or side*(entry-sl) < 0.5*unit_r:
        return None
    loss = side*(entry-stop_fill)+fee*entry+cost.taker*stop_fill
    if target is None:
        raw_target = (rr*loss+(side+fee)*entry)/(side-fee)
        target = tick_price(raw_target,tick,side > 0)
    reward = side*(target-entry)-fee*(target+entry)
    if target <= 0 or loss <= 0 or reward+1e-10*entry < rr*loss:
        return None
    return SinglePoint(entry,sl,target,reward/entry,loss/entry)
