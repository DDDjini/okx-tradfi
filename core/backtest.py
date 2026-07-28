"""
core/backtest.py — 回测引擎（无未来函数 + 多层过滤）

严格时序：
  分型出现(i) → 确认(i+right) → 入场(i+right+1 开盘价)

规则：
  - 最大止损 6% 硬截断
  - 多层过滤：PSY+EMA7/34 + 缠论/波浪理论
  - 双边手续费 fee_rate × 2
  - 出场后 i = exit_idx + 1，避免重叠持仓
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from core.fractal import add_fractals
from core.indicators import add_all_indicators, filter_signal
from core.chan_theory import apply_theory_filter, detect_bi_sequence
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
    take_profit: float
    rr: float
    pnl_pct: float
    pnl_usdt: float
    exit_reason: str
    bars_held: int
    filter_info: str = ""
    sl_capped: bool = False


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    params: dict
    trades: list[Trade]
    total_trades: int = 0
    signals_raw: int = 0
    signals_filtered: int = 0
    signals_chan_rejected: int = 0
    sl_capped_count: int = 0
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
    sortino_ratio: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    avg_bars_held: float = 0.0


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
    max_sl_pct: float = 0.06,
    theory_method: Optional[str] = None,
) -> BacktestResult:
    """
    对单个 DataFrame 运行分型回测。

    参数
    ----
    enable_filters : PSY+EMA7/34 三重过滤
    max_sl_pct     : 最大止损比例（默认 6%），硬截断
    theory_method  : 缠论/波浪理论过滤 ("chan" | "elliott" | "chan+elliott" | None)
    """
    filter_desc = "PSY+EMA7/34"
    if theory_method:
        filter_desc += f"+{theory_method}"

    params = {
        "left": left, "right": right, "rr": rr,
        "sl_buffer": sl_buffer, "fee_rate": fee_rate,
        "max_sl_pct": max_sl_pct,
        "filters": filter_desc,
    }
    result = BacktestResult(symbol=symbol, timeframe=timeframe, params=params, trades=[])

    if df is None or len(df) < max(34, left + right) + 3:
        return result

    df = add_all_indicators(df)
    if theory_method and "chan" in theory_method:
        df = detect_bi_sequence(df, left=left, right=right)

    df = add_fractals(df, left, right)
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
    chan_rejected = 0
    sl_capped = 0
    i = left + right + 1

    while i < n - 1:
        pivot_idx = i - right
        if df.loc[pivot_idx, "fractal_low"]:
            side = Side.LONG
        elif df.loc[pivot_idx, "fractal_high"]:
            side = Side.SHORT
        else:
            i += 1
            continue

        raw_signals += 1
        filter_tags = []

        # ---- PSY + 均线过滤器 ----
        if enable_filters and pivot_idx >= 34:
            passed, reason = filter_signal(df, pivot_idx, side)
            if not passed:
                filtered_out += 1
                i += 1
                continue
            filter_tags.append(reason)

        # ---- 缠论/波浪理论过滤器 ----
        if theory_method and pivot_idx >= 34:
            passed, reason = apply_theory_filter(df, pivot_idx, side, method=theory_method)
            if not passed:
                filtered_out += 1
                chan_rejected += 1
                i += 1
                continue
            filter_tags.append(reason)

        entry_idx = i + 1
        if entry_idx >= n:
            break
        entry_price = opens[entry_idx]
        pivot_price = lows[pivot_idx] if side == Side.LONG else highs[pivot_idx]

        # 止损止盈 + 6% 硬截断
        if side == Side.LONG:
            stop_loss = pivot_price * (1 - sl_buffer)
            risk_dist = entry_price - stop_loss
            take_profit = entry_price + rr * risk_dist
        else:
            stop_loss = pivot_price * (1 + sl_buffer)
            risk_dist = stop_loss - entry_price
            take_profit = entry_price - rr * risk_dist

        sl_cap_hit = False
        if max_sl_pct > 0:
            sl_distance = abs(entry_price - stop_loss) / entry_price
            if sl_distance > max_sl_pct:
                if side == Side.LONG:
                    stop_loss = entry_price * (1 - max_sl_pct)
                else:
                    stop_loss = entry_price * (1 + max_sl_pct)
                sl_cap_hit = True
                sl_capped += 1

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
                if hi >= take_profit:
                    exit_idx, exit_price, exit_reason = j, take_profit, "take_profit"
                    break
            else:
                if hi >= stop_loss:
                    exit_idx, exit_price, exit_reason = j, stop_loss, "stop_loss"
                    break
                if lo <= take_profit:
                    exit_idx, exit_price, exit_reason = j, take_profit, "take_profit"
                    break

        gross_ret = (exit_price - entry_price) / entry_price if side == Side.LONG else (entry_price - exit_price) / entry_price
        net_ret = gross_ret - double_fee

        trades.append(Trade(
            symbol=symbol, timeframe=timeframe, side=side.value,
            entry_idx=entry_idx, entry_time=times[entry_idx], entry_price=round(entry_price, 6),
            exit_idx=exit_idx, exit_time=times[exit_idx], exit_price=round(exit_price, 6),
            stop_loss=round(stop_loss, 6), take_profit=round(take_profit, 6), rr=rr,
            pnl_pct=round(net_ret * 100, 4), pnl_usdt=round(net_ret, 6),
            exit_reason=exit_reason, bars_held=exit_idx - entry_idx,
            filter_info=" | ".join(filter_tags), sl_capped=sl_cap_hit,
        ))
        i = exit_idx + 1

    # 汇总
    result.signals_raw = raw_signals
    result.signals_filtered = filtered_out
    result.signals_chan_rejected = chan_rejected
    result.sl_capped_count = sl_capped
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
    downside = pnls[pnls < 0]
    if len(downside) > 0 and downside.std() > 0:
        result.sortino_ratio = round(pnls.mean() / downside.std() * np.sqrt(len(pnls)), 2)

    if wins_mask.any():
        result.avg_win_pct = round(pnls[wins_mask].mean() * 100, 2)
    if (~wins_mask).any():
        result.avg_loss_pct = round(pnls[~wins_mask].mean() * 100, 2)

    result.avg_bars_held = round(np.mean([t.bars_held for t in trades]), 1)

    return result


def format_result_summary(r: BacktestResult) -> str:
    filter_tag = r.params.get("filters", "无")
    max_sl = r.params.get("max_sl_pct", 0)
    lines = [
        f"{'='*60}",
        f"  {r.symbol} | {r.timeframe}  |  过滤: {filter_tag}",
        f"  参数: left={r.params['left']}  right={r.params['right']}  RR={r.params['rr']}  buffer={r.params['sl_buffer']}  maxSL={max_sl*100:.0f}%",
        f"{'='*60}",
    ]
    if r.signals_raw > 0:
        kept = r.signals_raw - r.signals_filtered
        lines.append(f"  原始信号: {r.signals_raw} → 过滤后: {kept}（拦截 {r.signals_filtered}）")
    if r.sl_capped_count > 0:
        lines.append(f"  止损截断: {r.sl_capped_count} 笔")
    if r.signals_chan_rejected > 0:
        lines.append(f"  理论拒绝: {r.signals_chan_rejected} 笔")
    lines += [
        f"  最终交易: {r.total_trades}笔  胜率: {r.win_rate}%",
        f"  做多: {r.long_trades}笔({r.long_win_rate}%)  做空: {r.short_trades}笔({r.short_win_rate}%)",
        f"  收益: {r.total_return_pct}%  盈亏因子: {r.profit_factor}  回撤: {r.max_drawdown_pct}%",
        f"  夏普: {r.sharpe_ratio}  均盈: {r.avg_win_pct}%  均亏: {r.avg_loss_pct}%",
        f"{'='*60}",
    ]
    return "\n".join(lines)
