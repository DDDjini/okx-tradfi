"""
scripts/backtest_yahoo_2y.py — Yahoo Finance 2年数据全量回测

用 yfinance 拉取近 2 年完整 K 线，回测全部 17 个股票 × 4 周期。

新特性：
  - 双止盈（TP1=1.0R 保守 + TP2=RR 激进）
  - 同级别多空冲突抑制
  - 1d ATR 异动增强
  - 最大止损 8%
"""

from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.backtest import backtest_fractal, BacktestResult, format_result_summary
from core.config import PROJECT_ROOT, load_config
from core.data_feed_yahoo import fetch_yahoo_cached

RESULTS_DIR = PROJECT_ROOT / "results"


def run():
    config = load_config()

    print("=" * 70)
    print("  Yahoo Finance 2年数据全量回测")
    print("=" * 70)

    # 使用全部 17 个交易对
    all_symbols = list(config.symbols) if len(config.symbols) >= 15 else [
        "AAPL/USDT:USDT", "MSFT/USDT:USDT", "GOOGL/USDT:USDT", "AMZN/USDT:USDT",
        "TSLA/USDT:USDT", "NVDA/USDT:USDT", "INTC/USDT:USDT", "MU/USDT:USDT",
        "TSM/USDT:USDT", "SPCX/USDT:USDT",
        "SAMSUNG/USDT:USDT", "SKHYNIX/USDT:USDT",
        "XAU/USDT:USDT", "CL/USDT:USDT", "XAG/USDT:USDT",
        "SNDK/USDT:USDT", "OPENAI/USDT:USDT",
    ]

    # 直接拉取（跳过慢速校验）
    valid = all_symbols
    print(f"  直接拉取 {len(valid)} 个交易对...\n")

    # 周期配置
    timeframes = config.timeframes if len(config.timeframes) >= 3 else {
        "4h": type('TFC', (), {"left": 3, "right": 3, "rr": 1.8, "sl_buffer": 0.003})(),
        "1h": type('TFC', (), {"left": 2, "right": 2, "rr": 1.7, "sl_buffer": 0.002})(),
        "1d": type('TFC', (), {"left": 4, "right": 4, "rr": 2.0, "sl_buffer": 0.004})(),
    }

    # 拉取周期对应
    period_map = {"4h": 730, "1h": 730, "1d": 730}

    all_results = []
    total = len(valid) * len(timeframes)
    n = 0

    for symbol in valid:
        for tf_str, tf_cfg_ in timeframes.items():
            n += 1
            tf_cfg = tf_cfg_ if hasattr(tf_cfg_, 'left') else timeframes[tf_str]
            print(f"\n[{n}/{total}] {symbol} {tf_str} ...")

            try:
                df = fetch_yahoo_cached(
                    symbol=symbol, timeframe=tf_str,
                    period_days=period_map.get(tf_str, 730),
                )
            except Exception as e:
                print(f"  ❌ 拉取失败: {e}")
                continue

            if df is None or len(df) < 50:
                print(f"  ⚠️ 数据不足 ({len(df) if df is not None else 0} 行)")
                continue

            print(f"  📊 {len(df)} 根K线, {df['datetime'].iloc[0].strftime('%Y-%m-%d')} ~ {df['datetime'].iloc[-1].strftime('%Y-%m-%d')}")

            result = backtest_fractal(
                df=df, symbol=symbol, timeframe=tf_str,
                left=tf_cfg.left, right=tf_cfg.right,
                rr=tf_cfg.rr, sl_buffer=tf_cfg.sl_buffer,
                fee_rate=config.fee_rate, enable_filters=True,
                max_sl_pct=0.08, suppress_conflicts=True,
                conflict_window=5 if tf_str != "1d" else 3,
                atr_boost_1d=(tf_str == "1d"),
                rr1=1.0,
            )
            all_results.append(result)
            print(format_result_summary(result))
            time.sleep(0.3)

    # 保存
    save_all(all_results)

    # 汇总
    print_summary(all_results)


def save_all(results: list[BacktestResult]):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / "yahoo_2y_backtest.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "symbol", "timeframe", "signals_raw", "conflicts", "filtered",
            "trades", "tp1_hits", "tp2_hits", "wins", "losses", "win_rate",
            "long_trades", "long_win_rate", "short_trades", "short_win_rate",
            "total_return_pct", "profit_factor", "max_drawdown_pct",
            "sharpe_ratio", "avg_win_pct", "avg_loss_pct", "avg_bars_held",
        ])
        writer.writeheader()
        for r in results:
            writer.writerow({
                "symbol": r.symbol, "timeframe": r.timeframe,
                "signals_raw": r.signals_raw, "conflicts": r.conflicts_suppressed,
                "filtered": r.signals_filtered, "trades": r.total_trades,
                "tp1_hits": r.tp1_hits, "tp2_hits": r.tp2_hits,
                "wins": r.wins, "losses": r.losses, "win_rate": r.win_rate,
                "long_trades": r.long_trades, "long_win_rate": r.long_win_rate,
                "short_trades": r.short_trades, "short_win_rate": r.short_win_rate,
                "total_return_pct": r.total_return_pct,
                "profit_factor": r.profit_factor,
                "max_drawdown_pct": r.max_drawdown_pct,
                "sharpe_ratio": r.sharpe_ratio,
                "avg_win_pct": r.avg_win_pct, "avg_loss_pct": r.avg_loss_pct,
                "avg_bars_held": r.avg_bars_held,
            })
    print(f"\n📁 结果已保存: {path}")


def print_summary(results: list[BacktestResult]):
    print("\n" + "=" * 95)
    print("  2年回测汇总")
    print("=" * 95)
    hdr = f"{'交易对':<18} {'周期':<5} {'交易':>4} {'胜率':>7} {'收益':>9} {'PF':>6} {'回撤':>7} {'TP1':>4} {'TP2':>4} {'夏普':>5}"
    print(hdr)
    print("-" * 95)
    for r in sorted(results, key=lambda x: (-x.total_return_pct, x.symbol)):
        print(f"{r.symbol:<18} {r.timeframe:<5} {r.total_trades:>4} {r.win_rate:>6.1f}% {r.total_return_pct:>8.2f}% {r.profit_factor:>5} {r.max_drawdown_pct:>6.1f}% {r.tp1_hits:>4} {r.tp2_hits:>4} {r.sharpe_ratio:>5}")
    print("=" * 95)

    with_trades = [r for r in results if r.total_trades > 0]
    if with_trades:
        avg_wr = sum(r.win_rate for r in with_trades) / len(with_trades)
        avg_ret = sum(r.total_return_pct for r in with_trades) / len(with_trades)
        profitable = sum(1 for r in with_trades if r.total_return_pct > 0)
        total_tp1 = sum(r.tp1_hits for r in with_trades)
        total_tp2 = sum(r.tp2_hits for r in with_trades)
        print(f"\n有信号: {len(with_trades)} 组")
        print(f"平均胜率: {avg_wr:.1f}%  平均收益: {avg_ret:.2f}%")
        print(f"盈利: {profitable}/{len(with_trades)}")
        print(f"止盈命中: TP1={total_tp1}次  TP2={total_tp2}次")

        # 分周期统计
        for tf in ["4h", "1h", "1d"]:
            tf_results = [r for r in with_trades if r.timeframe == tf]
            if tf_results:
                avg_wr_tf = sum(r.win_rate for r in tf_results) / len(tf_results)
                avg_ret_tf = sum(r.total_return_pct for r in tf_results) / len(tf_results)
                prof_tf = sum(1 for r in tf_results if r.total_return_pct > 0)
                print(f"  {tf}: 均胜率{avg_wr_tf:.1f}%  均收益{avg_ret_tf:.2f}%  盈利{prof_tf}/{len(tf_results)}")


if __name__ == "__main__":
    run()
