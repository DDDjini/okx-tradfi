"""
core/backtest.py — 回测引擎 v2.0

新特性：
  - 双止盈位（TP1 保守锁利 + TP2 激进目标）
  - 同级别多空冲突抑制（震荡行情不交易）
  - 1d 周期异动检测增强（ATR 波动放大确认）
  - 最大止损 8% 硬截断

严格时序：分型(i) → 确认(i+right) → 入场(i+right+1开盘)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from core.fractal import add_fractals
from core.indicators import add_all_indicators, filter_signal
from core.strategy import Side

logger = logging.getLogger(__name__)


@dataclass
class Trade:
    symbol: str
    timeframe: str
    side: str
    entry_idx: int
    entry_time: str
    entry_price: float
    exit_idx: int
    exit_time: str
    exit_price: float
    stop_loss: float
    take_profit_1: float   # 第一止盈
    take_profit_2: float   # 第二止盈
    rr: float
    pnl_pct: float
    pnl_usdt: float
    exit_reason: str        # "tp1" / "tp2" / "stop_loss" / "end_of_data"
    bars_held: int
    filter_info: str = ""
    atr_pct: float = 0      # 入场时 ATR 波动率


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    params: dict
    trades: list[Trade]
    total_trades: int = 0
    signals_raw: int = 0
    signals_filtered: int = 0
    conflicts_suppressed: int = 0    # 震荡冲突抑制数
    tp1_hits: int = 0               # 命中TP1次数
    tp2_hits: int = 0               # 命中TP2次数
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    long_trades: int = 0
    short_trades: int = 0
    long_win_rate: float = 0.0
    short_win_rate: float = 0.0
    total_return_pct: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    avg_bars_held: float = 0.0


def _detect_conflicts(df: pd.DataFrame, left: int, right: int, window: int = 5) -> set[int]:
    """
    检测同一周期内多空信号冲突。
    如果在 window 根K线内既有做多又有做空分型，标记为震荡区。

    返回被抑制的分型位置集合。
    """
    n = len(df)
    suppressed = set()
    for i in range(left + right + 1, n):
        lo_fractal = df.loc[i - right, "fractal_low"]
        hi_fractal = df.loc[i - right, "fractal_high"]
        if not (lo_fractal or hi_fractal):
            continue

        # 检查前 window 根内是否有相反信号
        start = max(0, i - window)
        for j in range(start, i + 1):
            if j - right >= 0:
                if lo_fractal and df.loc[j - right, "fractal_high"]:
                    suppressed.add(i - right)
                    suppressed.add(j - right)
                elif hi_fractal and df.loc[j - right, "fractal_low"]:
                    suppressed.add(i - right)
                    suppressed.add(j - right)
    return suppressed


def _calc_atr_pct(df: pd.DataFrame, idx: int, period: int = 14) -> float:
    """计算入场时的 ATR(14) 波动率百分比。"""
    if idx < period + 2:
        return 0
    highs = df["high"].iloc[idx - period:idx]
    lows = df["low"].iloc[idx - period:idx]
    closes = df["close"].iloc[idx - period - 1:idx - 1]
    tr = np.maximum(highs - lows, np.abs(highs - closes.values))
    atr = tr.mean() if len(tr) > 0 else 0
    price = df["close"].iloc[idx]
    return atr / price if price > 0 else 0


def backtest_fractal(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    left: int,
    right: int,
    rr: float,
    sl_buffer: float,
    fee_rate: float = 0.0005,
    enable_filters: bool = True,
    max_sl_pct: float = 0.08,
    suppress_conflicts: bool = True,
    conflict_window: int = 5,
    atr_boost_1d: bool = True,
    rr1: float = 1.0,
) -> BacktestResult:
    """
    回测引擎 v2.0。

    参数
    ----
    suppress_conflicts : 同级别多空冲突抑制
    conflict_window    : 冲突检测窗口（K线根数）
    atr_boost_1d       : 1d 周期用 ATR 异动增强过滤
    rr1                : TP1 盈亏比（保守止盈）
    """
    filter_desc = "PSY+EMA+双TP"
    if suppress_conflicts:
        filter_desc += "+冲突抑制"
    if atr_boost_1d and timeframe == "1d":
        filter_desc += "+ATR异动"

    params = {
        "left": left, "right": right, "rr": rr, "sl_buffer": sl_buffer,
        "fee_rate": fee_rate, "max_sl_pct": max_sl_pct, "rr1": rr1,
        "filters": filter_desc,
    }
    result = BacktestResult(symbol=symbol, timeframe=timeframe, params=params, trades=[])

    if df is None or len(df) < max(34, left + right) + 3:
        return result

    df = add_all_indicators(df)
    df = add_fractals(df, left, right)

    # 冲突检测
    suppressed_set = set()
    if suppress_conflicts:
        suppressed_set = _detect_conflicts(df, left, right, conflict_window)
        if suppressed_set:
            logger.debug("%s %s: 抑制 %d 个冲突分型", symbol, timeframe, len(suppressed_set))

    n = len(df)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    opens = df["open"].to_numpy(dtype=float)

    if "datetime" in df.columns:
        times = df["datetime"].dt.strftime("%Y-%m-%d %H:%M").tolist()
    else:
        times = [str(i) for i in range(n)]

    trades: list[Trade] = []
    double_fee = fee_rate * 2
    raw_signals = 0
    filtered_out = 0
    conflicts = 0
    tp1_hits = 0
    tp2_hits = 0
    i = left + right + 1

    while i < n - 1:
        pivot_idx = i - right

        # 冲突抑制
        if pivot_idx in suppressed_set:
            raw_signals += 1
            conflicts += 1
            i += 1
            continue

        if df.loc[pivot_idx, "fractal_low"]:
            side = Side.LONG
        elif df.loc[pivot_idx, "fractal_high"]:
            side = Side.SHORT
        else:
            i += 1
            continue

        raw_signals += 1
        filter_tags = []

        # PSY + EMA 过滤器
        if enable_filters and pivot_idx >= 34:
            passed, reason = filter_signal(df, pivot_idx, side)
            if not passed:
                filtered_out += 1
                i += 1
                continue
            filter_tags.append(reason)

        # 1d ATR 异动增强
        atr_val = 0
        if atr_boost_1d and timeframe == "1d" and pivot_idx >= 15:
            atr_val = _calc_atr_pct(df, pivot_idx)
            if atr_val < 0.015:  # ATR < 1.5%，波动太小不值得交易
                filtered_out += 1
                i += 1
                continue
            filter_tags.append(f"ATR={atr_val*100:.1f}%")

        entry_idx = i + 1
        if entry_idx >= n:
            break
        entry_price = opens[entry_idx]
        pivot_price = lows[pivot_idx] if side == Side.LONG else highs[pivot_idx]

        # 止损
        if side == Side.LONG:
            stop_loss = pivot_price * (1 - sl_buffer)
            risk_dist = entry_price - stop_loss
        else:
            stop_loss = pivot_price * (1 + sl_buffer)
            risk_dist = stop_loss - entry_price

        # 双止盈
        if side == Side.LONG:
            tp1 = entry_price + rr1 * risk_dist
            tp2 = entry_price + rr * risk_dist
        else:
            tp1 = entry_price - rr1 * risk_dist
            tp2 = entry_price - rr * risk_dist

        # 止损截断（8%）
        if max_sl_pct > 0 and risk_dist > 0:
            sl_distance = risk_dist / entry_price
            if sl_distance > max_sl_pct:
                if side == Side.LONG:
                    stop_loss = entry_price * (1 - max_sl_pct)
                else:
                    stop_loss = entry_price * (1 + max_sl_pct)

        # 逐根扫描出场
        exit_idx = n - 1
        exit_reason = "end_of_data"
        exit_price = opens[exit_idx]

        for j in range(entry_idx + 1, n):
            hi, lo = highs[j], lows[j]
            if side == Side.LONG:
                if lo <= stop_loss:
                    exit_idx, exit_price, exit_reason = j, stop_loss, "stop_loss"
                    break
                if hi >= tp1:
                    exit_idx, exit_price, exit_reason = j, tp1, "tp1"
                    tp1_hits += 1
                    break
                if hi >= tp2:
                    exit_idx, exit_price, exit_reason = j, tp2, "tp2"
                    tp2_hits += 1
                    break
            else:
                if hi >= stop_loss:
                    exit_idx, exit_price, exit_reason = j, stop_loss, "stop_loss"
                    break
                if lo <= tp1:
                    exit_idx, exit_price, exit_reason = j, tp1, "tp1"
                    tp1_hits += 1
                    break
                if lo <= tp2:
                    exit_idx, exit_price, exit_reason = j, tp2, "tp2"
                    tp2_hits += 1
                    break

        gross_ret = (exit_price - entry_price) / entry_price if side == Side.LONG else (entry_price - exit_price) / entry_price
        net_ret = gross_ret - double_fee

        trades.append(Trade(
            symbol=symbol, timeframe=timeframe, side=side.value,
            entry_idx=entry_idx, entry_time=times[entry_idx], entry_price=round(entry_price, 6),
            exit_idx=exit_idx, exit_time=times[exit_idx], exit_price=round(exit_price, 6),
            stop_loss=round(stop_loss, 6),
            take_profit_1=round(tp1, 6), take_profit_2=round(tp2, 6), rr=rr,
            pnl_pct=round(net_ret * 100, 4), pnl_usdt=round(net_ret, 6),
            exit_reason=exit_reason, bars_held=exit_idx - entry_idx,
            filter_info=" | ".join(filter_tags), atr_pct=round(atr_val * 100, 2),
        ))
        i = exit_idx + 1

    # 汇总
    result.signals_raw = raw_signals
    result.signals_filtered = filtered_out
    result.conflicts_suppressed = conflicts
    result.tp1_hits = tp1_hits
    result.tp2_hits = tp2_hits
    result.trades = trades
    result.total_trades = len(trades)
    if not trades:
        return result

    pnls = np.array([t.pnl_pct / 100 for t in trades])
    wins_mask = pnls > 0

    result.wins = int(wins_mask.sum())
    result.losses = int((~wins_mask).sum())
    result.win_rate = round(result.wins / result.total_trades * 100, 2)

    long_mask = np.array([t.side == "long" for t in trades])
    short_mask = ~long_mask
    result.long_trades = int(long_mask.sum())
    result.short_trades = int(short_mask.sum())
    if result.long_trades > 0:
        result.long_win_rate = round((pnls[long_mask] > 0).sum() / result.long_trades * 100, 2)
    if result.short_trades > 0:
        result.short_win_rate = round((pnls[short_mask] > 0).sum() / result.short_trades * 100, 2)

    equity = np.cumprod(1 + pnls)
    result.total_return_pct = round((equity[-1] - 1) * 100, 2)

    gross_profit = pnls[wins_mask].sum() if wins_mask.any() else 0
    gross_loss = abs(pnls[~wins_mask].sum()) if (~wins_mask).any() else 0
    result.profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")

    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak
    result.max_drawdown_pct = round(abs(drawdown.min()) * 100, 2) if len(drawdown) > 0 else 0

    if pnls.std() > 0:
        result.sharpe_ratio = round(pnls.mean() / pnls.std() * np.sqrt(len(pnls)), 2)

    if wins_mask.any():
        result.avg_win_pct = round(pnls[wins_mask].mean() * 100, 2)
    if (~wins_mask).any():
        result.avg_loss_pct = round(pnls[~wins_mask].mean() * 100, 2)
    result.avg_bars_held = round(np.mean([t.bars_held for t in trades]), 1)

    return result


def format_result_summary(r: BacktestResult) -> str:
    filter_tag = r.params.get("filters", "无")
    lines = [
        f"{'='*60}",
        f"  {r.symbol} | {r.timeframe}  |  {filter_tag}",
        f"  参数: left={r.params['left']} right={r.params['right']} RR={r.params['rr']} buf={r.params['sl_buffer']} TP1_RR={r.params.get('rr1',1.0)}",
        f"{'='*60}",
    ]
    if r.signals_raw > 0:
        kept = r.signals_raw - r.signals_filtered - r.conflicts_suppressed
        parts = [
            f"原始{r.signals_raw}",
            f"冲突抑制{r.conflicts_suppressed}" if r.conflicts_suppressed else "",
            f"过滤拦截{r.signals_filtered}" if r.signals_filtered else "",
            f"= {kept}笔交易",
        ]
        lines.append("  信号: " + " → ".join(p for p in parts if p))
    if r.tp1_hits or r.tp2_hits:
        lines.append(f"  止盈: TP1={r.tp1_hits}次  TP2={r.tp2_hits}次")
    lines += [
        f"  交易: {r.total_trades}笔  胜率: {r.win_rate}%",
        f"  做多: {r.long_trades}笔({r.long_win_rate}%)  做空: {r.short_trades}笔({r.short_win_rate}%)",
        f"  收益: {r.total_return_pct}%  PF: {r.profit_factor}  回撤: {r.max_drawdown_pct}%",
        f"  夏普: {r.sharpe_ratio}  均盈: {r.avg_win_pct}%  均亏: {r.avg_loss_pct}%",
        f"{'='*60}",
    ]
    return "\n".join(lines)
