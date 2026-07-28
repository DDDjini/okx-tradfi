"""
scripts/run_real_backtest.py — 用 OKX 真实数据批量回测

自动完成：
  1. 校验哪些交易对在 OKX 上线
  2. 逐个拉取真实 K 线（CSV 缓存）
  3. 四周期回测
  4. 输出汇总报告 + 明细 CSV

用法：
  python scripts/run_real_backtest.py
  python scripts/run_real_backtest.py --symbols TSLA-USDT NVDA-USDT
  python scripts/run_real_backtest.py --timeframe 1h
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.backtest import BacktestResult, backtest_fractal, format_result_summary
from core.config import PROJECT_ROOT, load_config
from core.data_feed import fetch_ohlcv_cached, validate_symbols

RESULTS_DIR = PROJECT_ROOT / "results"


def run_all_backtests(
    symbols: list[str],
    timeframes: dict,
    config,
    proxy_url: str,
) -> list[BacktestResult]:
    """批量回测所有交易对 × 周期。"""
    results: list[BacktestResult] = []
    total = len(symbols) * len(timeframes)
    current = 0

    for symbol in symbols:
        for tf_str, tf_cfg in timeframes.items():
            current += 1
            print(f"\n[{current}/{total}] {symbol} {tf_str} ...")

            try:
                df = fetch_ohlcv_cached(
                    symbol=symbol,
                    timeframe=tf_str,
                    limit=config.exchange_limit,
                    proxy_url=proxy_url,
                    use_cache=True,
                )
            except Exception as e:
                print(f"  ❌ 拉取失败: {e}")
                continue

            if df is None or len(df) < tf_cfg.left + tf_cfg.right + 3:
                print(f"  ⚠️  数据不足 ({len(df) if df is not None else 0} 行)")
                continue

            print(f"  📊 {len(df)} 根K线, 范围 {df['datetime'].iloc[0].strftime('%Y-%m-%d %H:%M')} ~ {df['datetime'].iloc[-1].strftime('%Y-%m-%d %H:%M')}")

            result = backtest_fractal(
                df=df,
                symbol=symbol,
                timeframe=tf_str,
                left=tf_cfg.left,
                right=tf_cfg.right,
                rr=tf_cfg.rr,
                sl_buffer=tf_cfg.sl_buffer,
                fee_rate=config.fee_rate,
            )
            results.append(result)

            # 打印简版
            if result.total_trades > 0:
                filter_stat = f"  过滤前{result.signals_raw}→后{result.total_trades}" if result.signals_raw > 0 else ""
                print(f"  ✅ {result.total_trades}笔  胜率{result.win_rate}%  收益{result.total_return_pct}%  盈亏因子{result.profit_factor}  回撤{result.max_drawdown_pct}%{filter_stat}")
            else:
                print(f"  ⚠️  无交易信号")

            # 避免触发 OKX 限速
            time.sleep(0.5)

    return results


def save_all_results(results: list[BacktestResult]) -> None:
    """保存汇总 + 明细 CSV。"""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # 汇总表
    summary_path = RESULTS_DIR / "real_backtest_summary.csv"
    with open(summary_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "symbol", "timeframe", "signals_raw", "signals_filtered",
            "trades", "wins", "losses", "win_rate",
            "long_trades", "long_win_rate", "short_trades", "short_win_rate",
            "total_return_pct", "profit_factor", "max_drawdown_pct",
            "sharpe_ratio", "sortino_ratio", "avg_win_pct", "avg_loss_pct",
            "avg_bars_held",
        ])
        writer.writeheader()
        for r in results:
            writer.writerow({
                "symbol": r.symbol,
                "timeframe": r.timeframe,
                "signals_raw": r.signals_raw,
                "signals_filtered": r.signals_filtered,
                "trades": r.total_trades,
                "wins": r.wins,
                "losses": r.losses,
                "win_rate": r.win_rate,
                "long_trades": r.long_trades,
                "long_win_rate": r.long_win_rate,
                "short_trades": r.short_trades,
                "short_win_rate": r.short_win_rate,
                "total_return_pct": r.total_return_pct,
                "profit_factor": r.profit_factor,
                "max_drawdown_pct": r.max_drawdown_pct,
                "sharpe_ratio": r.sharpe_ratio,
                "sortino_ratio": r.sortino_ratio,
                "avg_win_pct": r.avg_win_pct,
                "avg_loss_pct": r.avg_loss_pct,
                "avg_bars_held": r.avg_bars_held,
            })

    # 每个组合的交易明细
    for r in results:
        if not r.trades:
            continue
        safe_sym = r.symbol.replace("/", "_").replace("-", "_").replace(":", "_")
        trades_path = RESULTS_DIR / f"{safe_sym}_{r.timeframe}_trades.csv"
        with open(trades_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "side", "entry_time", "entry_price", "exit_time", "exit_price",
                "stop_loss", "take_profit", "rr", "pnl_pct", "exit_reason", "bars_held",
                "filter_info",
            ])
            writer.writeheader()
            for t in r.trades:
                writer.writerow({
                    "side": t.side,
                    "entry_time": t.entry_time,
                    "entry_price": t.entry_price,
                    "exit_time": t.exit_time,
                    "exit_price": t.exit_price,
                    "stop_loss": t.stop_loss,
                    "take_profit": t.take_profit,
                    "rr": t.rr,
                    "pnl_pct": t.pnl_pct,
                    "exit_reason": t.exit_reason,
                    "bars_held": t.bars_held,
                    "filter_info": t.filter_info,
                })

    print(f"\n📁 报告已保存到 {RESULTS_DIR}/")
    print(f"   汇总: {summary_path.name}")


def print_summary_table(results: list[BacktestResult]) -> None:
    """打印汇总表。"""
    print("\n" + "="*90)
    print("  真实数据回测汇总")
    print("="*90)
    header = f"{'交易对':<22} {'周期':<6} {'过滤后':>5} {'胜率':>8} {'收益':>10} {'盈亏因子':>8} {'最大回撤':>8} {'夏普':>6}"
    print(header)
    print("-"*90)
    for r in sorted(results, key=lambda x: (x.symbol, x.timeframe)):
        print(f"{r.symbol:<22} {r.timeframe:<6} {r.total_trades:>5} {r.win_rate:>7.1f}% {r.total_return_pct:>9.2f}% {r.profit_factor:>8} {r.max_drawdown_pct:>7.1f}% {r.sharpe_ratio:>6}")
    print("="*90)

    # 统计
    with_trades = [r for r in results if r.total_trades > 0]
    if with_trades:
        avg_wr = sum(r.win_rate for r in with_trades) / len(with_trades)
        avg_ret = sum(r.total_return_pct for r in with_trades) / len(with_trades)
        profitable = sum(1 for r in with_trades if r.total_return_pct > 0)
        total_raw = sum(r.signals_raw for r in results)
        total_filt = sum(r.signals_filtered for r in results)
        print(f"\n  有信号的组合: {len(with_trades)}")
        print(f"  原始信号:     {total_raw} → 过滤拦截: {total_filt}（{total_filt/max(total_raw,1)*100:.0f}% 被过滤）")
        print(f"  平均胜率:     {avg_wr:.1f}%")
        print(f"  平均收益:     {avg_ret:.2f}%")
        print(f"  盈利组合数:   {profitable}/{len(with_trades)}")


def main():
    parser = argparse.ArgumentParser(description="OKX 真实数据批量回测")
    parser.add_argument("--symbols", nargs="+", help="指定交易对（空格分隔），默认全部")
    parser.add_argument("--timeframe", type=str, help="指定周期，默认四周期全跑")
    parser.add_argument("--no-cache", action="store_true", help="忽略缓存重新拉取")
    args = parser.parse_args()

    config = load_config()

    print("="*60)
    print("  OKX TradFi 分型策略 — 真实数据批量回测")
    print("="*60)

    # 校验交易对
    target_symbols = args.symbols or config.symbols
    print(f"\n校验 {len(target_symbols)} 个交易对...")
    valid_symbols, invalid_symbols = validate_symbols(target_symbols, config.proxy_url)

    if not valid_symbols:
        print("❌ 没有有效的交易对")
        sys.exit(1)

    if invalid_symbols:
        print(f"  ⚠️  已剔除: {', '.join(invalid_symbols)}")
    print(f"  ✅ 有效: {', '.join(valid_symbols)}")

    # 确定周期
    if args.timeframe:
        if args.timeframe not in config.timeframes:
            print(f"❌ 未知周期: {args.timeframe}")
            sys.exit(1)
        timeframes = {args.timeframe: config.timeframes[args.timeframe]}
    else:
        timeframes = config.timeframes

    print(f"\n开始回测 {len(valid_symbols)} 个交易对 × {len(timeframes)} 个周期 = {len(valid_symbols)*len(timeframes)} 组...")

    results = run_all_backtests(valid_symbols, timeframes, config, config.proxy_url)

    # 保存 + 打印
    if results:
        save_all_results(results)
        print_summary_table(results)
    else:
        print("\n⚠️  没有生成任何回测结果")


if __name__ == "__main__":
    main()
