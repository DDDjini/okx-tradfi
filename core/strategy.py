"""
core/strategy.py — 信号 → 策略卡生成

将分型信息转换为完整的交易策略卡：
  方向 / 入场 / 止损 / 第一止盈 / 第二止盈 / 盈亏比 / 建议仓位

双止盈位：
  TP1 = 入场 + RR1 × 风险距离（保守止盈，锁利）
  TP2 = 入场 + RR2 × 风险距离（激进止盈，让利润奔跑）
  建议：TP1 平 50% 仓位，TP2 平剩余 50%
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from core.config import RiskConfig


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def cn(self) -> str:
        return "做多" if self == Side.LONG else "做空"

    @property
    def emoji(self) -> str:
        return "📈" if self == Side.LONG else "📉"


@dataclass
class TradeSignal:
    """完整交易策略卡，含双止盈位。"""
    symbol: str
    timeframe: str
    side: Side
    entry_price: float
    stop_loss: float
    take_profit_1: float        # 第一止盈（保守）
    take_profit_2: float        # 第二止盈（激进）
    rr: float                   # 总盈亏比（基于TP2）
    rr1: float                  # TP1 盈亏比
    rr2: float                  # TP2 盈亏比
    pivot_price: float
    signal_time: str
    risk_pct: float
    suggest_qty_usdt: float

    @property
    def side_cn(self) -> str:
        return self.side.cn

    @property
    def side_emoji(self) -> str:
        return self.side.emoji

    @property
    def sl_pct(self) -> float:
        if self.side == Side.LONG:
            return (self.entry_price - self.stop_loss) / self.entry_price
        return (self.stop_loss - self.entry_price) / self.entry_price

    @property
    def tp1_pct(self) -> float:
        if self.side == Side.LONG:
            return (self.take_profit_1 - self.entry_price) / self.entry_price
        return (self.entry_price - self.take_profit_1) / self.entry_price

    @property
    def tp2_pct(self) -> float:
        if self.side == Side.LONG:
            return (self.take_profit_2 - self.entry_price) / self.entry_price
        return (self.entry_price - self.take_profit_2) / self.entry_price

    def signal_id(self) -> str:
        return f"{self.symbol}|{self.timeframe}|{self.side.value}|{self.pivot_price:.6f}"


def build_signal(
    symbol: str,
    timeframe: str,
    side: Side | str,
    entry_price: float,
    pivot_price: float,
    rr: float,
    sl_buffer: float,
    signal_time: Optional[str] = None,
    risk: Optional[RiskConfig] = None,
    rr1: float = 1.0,
    rr2: Optional[float] = None,
) -> TradeSignal:
    """
    构建双止盈策略卡。

    参数
    ----
    rr1 : TP1 盈亏比（默认 1.0，保守锁利）
    rr2 : TP2 盈亏比（默认 = rr，激进目标）
    """
    if isinstance(side, str):
        side = Side(side)

    if signal_time is None:
        signal_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if rr2 is None:
        rr2 = rr

    # 止损计算
    if side == Side.LONG:
        stop_loss = pivot_price * (1 - sl_buffer)
        risk_dist = entry_price - stop_loss
        tp1 = entry_price + rr1 * risk_dist
        tp2 = entry_price + rr2 * risk_dist
    else:
        stop_loss = pivot_price * (1 + sl_buffer)
        risk_dist = stop_loss - entry_price
        tp1 = entry_price - rr1 * risk_dist
        tp2 = entry_price - rr2 * risk_dist

    # 建议仓位
    risk_pct_val = risk.risk_per_trade if risk else 0.01
    account_size = risk.account_size if risk else 0
    sl_pct_val = abs(risk_dist / entry_price) if entry_price > 0 else 0
    if sl_pct_val > 0 and account_size > 0:
        suggest_qty = account_size * risk_pct_val / sl_pct_val
    else:
        suggest_qty = 0.0

    return TradeSignal(
        symbol=symbol, timeframe=timeframe, side=side,
        entry_price=round(entry_price, 6),
        stop_loss=round(stop_loss, 6),
        take_profit_1=round(tp1, 6),
        take_profit_2=round(tp2, 6),
        rr=rr, rr1=round(rr1, 1), rr2=round(rr2, 1),
        pivot_price=round(pivot_price, 6),
        signal_time=signal_time,
        risk_pct=risk_pct_val,
        suggest_qty_usdt=round(suggest_qty, 2),
    )
