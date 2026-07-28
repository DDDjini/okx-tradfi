"""
core/strategy.py — 信号 → 策略卡生成

将分型信息转换为完整的交易策略卡：
  方向 / 入场 / 止损 / 止盈 / 盈亏比 / 建议仓位
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    """完整交易策略卡。"""
    symbol: str
    timeframe: str
    side: Side
    entry_price: float
    stop_loss: float
    take_profit: float
    rr: float
    pivot_price: float
    signal_time: str
    risk_pct: float
    suggest_qty_usdt: float

    # 便捷属性
    @property
    def side_cn(self) -> str:
        return self.side.cn

    @property
    def side_emoji(self) -> str:
        return self.side.emoji

    @property
    def sl_pct(self) -> float:
        """止损百分比（正数）。"""
        if self.side == Side.LONG:
            return (self.entry_price - self.stop_loss) / self.entry_price
        return (self.stop_loss - self.entry_price) / self.entry_price

    @property
    def tp_pct(self) -> float:
        """止盈百分比（正数）。"""
        if self.side == Side.LONG:
            return (self.take_profit - self.entry_price) / self.entry_price
        return (self.entry_price - self.take_profit) / self.entry_price

    def signal_id(self) -> str:
        """唯一信号 ID（用于去重）。"""
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
) -> TradeSignal:
    """
    根据分型极值价构建交易策略卡。

    做多：止损 = 分型低点 × (1 - buffer)；止盈 = 入场 + RR × 风险距离
    做空：止损 = 分型高点 × (1 + buffer)；止盈 = 入场 - RR × 风险距离

    参数
    ----
    symbol       : 交易对，如 TSLA-USDT
    timeframe    : 周期，如 1h
    side         : long / short
    entry_price  : 建议入场价（通常是确认后下一根开盘价）
    pivot_price  : 分型极值价
    rr           : 目标盈亏比
    sl_buffer    : 止损缓冲比例
    signal_time  : 信号时间字符串（默认当前 UTC 时间）
    risk         : 风控配置（用于计算建议仓位）
    """
    if isinstance(side, str):
        side = Side(side)

    if signal_time is None:
        signal_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # 止损止盈计算
    if side == Side.LONG:
        stop_loss = pivot_price * (1 - sl_buffer)
        risk_distance = entry_price - stop_loss
        take_profit = entry_price + rr * risk_distance
    else:
        stop_loss = pivot_price * (1 + sl_buffer)
        risk_distance = stop_loss - entry_price
        take_profit = entry_price - rr * risk_distance

    # 建议仓位
    risk_pct = risk.risk_per_trade if risk else 0.01
    account_size = risk.account_size if risk else 0
    # 建议仓位 = 本金 × 单笔风险% ÷ 止损距离%
    sl_pct = abs(risk_distance / entry_price) if entry_price > 0 else 0
    if sl_pct > 0 and account_size > 0:
        suggest_qty_usdt = account_size * risk_pct / sl_pct
    else:
        suggest_qty_usdt = 0.0

    return TradeSignal(
        symbol=symbol,
        timeframe=timeframe,
        side=side,
        entry_price=round(entry_price, 6),
        stop_loss=round(stop_loss, 6),
        take_profit=round(take_profit, 6),
        rr=rr,
        pivot_price=round(pivot_price, 6),
        signal_time=signal_time,
        risk_pct=risk_pct,
        suggest_qty_usdt=round(suggest_qty_usdt, 2),
    )
