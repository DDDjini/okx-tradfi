"""
scripts/grid_search_rr.py — 盈亏比 RR 网格搜索

对每个 交易对×周期，RR 从 1.0 到 3.0 以 0.1 步长迭代回测。
找出：① 胜率最高的 RR    ② 收益最高的 RR
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.backtest import backtest_fractal, BacktestResult
from core.config import PROJECT_ROOT, load_config
from core.data_feed import fetch_ohlcv_cached, validate_symbols

RESULTS_DIR = PROJECT_ROOT / "results"


def run_grid_search() -> list[dict]:
    config = load_config()
    valid_syms, _ = validate_symbols(config.symbols, config.proxy_url)
    if not valid_syms:
        print("没有有效交易对")
        return []

    rr_values = np.arange(1.0, 3.1, 0.1)
    all_rows = []

    total = len(valid_syms) * len(config.timeframes) * len(rr_values)
    current = 0

    print(f"网格搜索: {len(valid_syms)} 交易对 × {len(config.timeframes)} 周期 × {len(rr_values)} 个RR = {total} 组")
    print("=" * 90)
    print(f"{'交易对':<22} {'周期':<6} {'RR':>6} {'笔数':>5} {'胜率':>8} {'收益':>10} {'盈亏因子':>8} {'最大回撤':>8}")
    print("-" * 90)

    best_by_wr = {}   # (symbol, tf) -> best RR by win_rate
    best_by_ret = {}  # (symbol, tf) -> best RR by total_return

    for symbol in valid_syms:
        for tf_str, tf_cfg in config.timeframes.items():
            try:
                df = fetch_ohlcv_cached(
                    symbol=symbol, timeframe=tf_str,
                    limit=config.exchange_limit, proxy_url=config.proxy_url, use_cache=True,
                )
            except Exception as e:
                print(f"  {symbol} {tf_str}: 拉取失败 {e}")
                continue

            if df is None or len(df) < tf_cfg.left + tf_cfg.right + 3:
                continue

            wr_best, ret_best = None, None
            wr_max, ret_max = -1, -999

            for rr in rr_values:
                current += 1
                result = backtest_fractal(
                    df=df, symbol=symbol, timeframe=tf_str,
                    left=tf_cfg.left, right=tf_cfg.right,
                    rr=float(rr), sl_buffer=tf_cfg.sl_buffer,
                    fee_rate=config.fee_rate, enable_filters=True,
                )

                row = {
                    "symbol": symbol, "timeframe": tf_str, "rr": round(rr, 1),
                    "trades": result.total_trades, "win_rate": result.win_rate,
                    "total_return_pct": result.total_return_pct,
                    "profit_factor": result.profit_factor,
                    "max_drawdown_pct": result.max_drawdown_pct,
                    "sharpe": result.sharpe_ratio,
                }
                all_rows.append(row)

                # 跟踪最佳
                if result.total_trades >= 3:
                    if result.win_rate > wr_max:
                        wr_max = result.win_rate
                        wr_best = row.copy()
                    if result.total_return_pct > ret_max:
                        ret_max = result.total_return_pct
                        ret_best = row.copy()

                if result.total_trades > 0:
                    print(f"{symbol:<22} {tf_str:<6} {rr:>5.1f} {result.total_trades:>5} {result.win_rate:>7.1f}% {result.total_return_pct:>9.2f}% {result.profit_factor:>8} {result.max_drawdown_pct:>7.1f}%")

            # 记录每个组合的最佳
            key = (symbol, tf_str)
            if wr_best:
                best_by_wr[key] = wr_best
            if ret_best:
                best_by_ret[key] = ret_best

    return all_rows, best_by_wr, best_by_ret


def print_best_results(best_by_wr: dict, best_by_ret: dict):
    """打印每个组合的最佳RR。"""
    print("\n" + "=" * 90)
    print("  每个组合的最优 RR — 按胜率")
    print("=" * 90)
    print(f"{'交易对':<22} {'周期':<6} {'最佳RR':>7} {'胜率':>8} {'收益':>10} {'盈亏因子':>8}")
    print("-" * 90)
    for key in sorted(best_by_wr.keys()):
        r = best_by_wr[key]
        print(f"{r['symbol']:<22} {r['timeframe']:<6} {r['rr']:>6.1f} {r['win_rate']:>7.1f}% {r['total_return_pct']:>9.2f}% {r['profit_factor']:>8}")

    print("\n" + "=" * 90)
    print("  每个组合的最优 RR — 按收益率")
    print("=" * 90)
    print(f"{'交易对':<22} {'周期':<6} {'最佳RR':>7} {'胜率':>8} {'收益':>10} {'盈亏因子':>8}")
    print("-" * 90)
    for key in sorted(best_by_ret.keys()):
        r = best_by_ret[key]
        print(f"{r['symbol']:<22} {r['timeframe']:<6} {r['rr']:>6.1f} {r['win_rate']:>7.1f}% {r['total_return_pct']:>9.2f}% {r['profit_factor']:>8}")

    # 全局统计：哪个RR整体表现最好
    print("\n" + "=" * 90)
    print("  全局 RR 表现统计（按胜率加权平均）")
    print("=" * 90)
    print(f"{'RR':>6} {'平均胜率':>10} {'平均收益':>10} {'盈利组合数':>10}")
    print("-" * 90)

    all_rr_stats = {}
    for r in best_by_wr.values():
        rr = r['rr']
        if rr not in all_rr_stats:
            all_rr_stats[rr] = {'wr_sum': 0, 'ret_sum': 0, 'profitable': 0, 'count': 0}
        all_rr_stats[rr]['wr_sum'] += r['win_rate']
        all_rr_stats[rr]['ret_sum'] += r['total_return_pct']
        all_rr_stats[rr]['profitable'] += 1 if r['total_return_pct'] > 0 else 0
        all_rr_stats[rr]['count'] += 1

    for rr in sorted(all_rr_stats.keys()):
        s = all_rr_stats[rr]
        avg_wr = s['wr_sum'] / s['count']
        avg_ret = s['ret_sum'] / s['count']
        print(f"{rr:>5.1f} {avg_wr:>9.1f}% {avg_ret:>9.2f}% {s['profitable']:>10}/{s['count']}")


def save_grid_results(all_rows: list[dict]):
    path = RESULTS_DIR / "rr_grid_search.csv"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "symbol", "timeframe", "rr", "trades", "win_rate",
            "total_return_pct", "profit_factor", "max_drawdown_pct", "sharpe",
        ])
        writer.writeheader()
        for r in all_rows:
            writer.writerow(r)
    print(f"\n网格搜索结果已保存: {path}")


if __name__ == "__main__":
    all_rows, best_by_wr, best_by_ret = run_grid_search()
    if all_rows:
        save_grid_results(all_rows)
        print_best_results(best_by_wr, best_by_ret)
    else:
        print("无数据")
