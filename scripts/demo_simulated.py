"""
scripts/demo_simulated.py — 模拟数据演示

在无 OKX 网络连接时（沙盒环境）也能运行：
  - 生成模拟 K 线数据（带趋势 + 噪声 + 分型结构）
  - 运行四周期回测
  - 输出报告到 demo_output/

用法：
  python scripts/demo_simulated.py
  python scripts/demo_simulated.py --symbol TSLA-USDT
  python scripts/demo_simulated.py --bars 2000
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.backtest import BacktestResult, backtest_fractal, format_result_summary
from core.config import PROJECT_ROOT, load_config

DEMO_DIR = PROJECT_ROOT / "demo_output"


def generate_simulated_ohlcv(bars: int = 1000, seed: int = 42) -> pd.DataFrame:
    """
    生成模拟 K 线数据：
      - 基础价格 100 USDT
      - 趋势分量（正弦波 + 随机游走）
      - 噪声波动
      - 确保产生足够的高低点分型
    """
    rng = np.random.default_rng(seed)
    n = bars

    # 随机游走 + 正弦趋势
    returns = rng.normal(0, 0.015, n)
    trend = np.sin(np.linspace(0, 12 * np.pi, n)) * 0.003
    price_changes = returns + trend

    # 累积价格
    close = 100 * np.cumprod(1 + price_changes)

    # 高低价波动
    volatility = rng.uniform(0.002, 0.008, n)
    high = close * (1 + volatility)
    low = close * (1 - volatility)
    opn = np.roll(close, 1)
    opn[0] = close[0]
    volume = rng.uniform(1000, 50000, n)

    timestamps = np.arange(n) * 3600_000  # 每根 1 小时
    datetimes = pd.to_datetime(timestamps, unit="ms", utc=True)

    df = pd.DataFrame({
        "timestamp": timestamps,
        "datetime": datetimes,
        "open": opn,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })
    return df


def generate_equity_curve(trades: list, initial_capital: float = 10000) -> pd.DataFrame:
    """根据交易列表生成净值曲线 DataFrame。"""
    if not trades:
        return pd.DataFrame(columns=["trade_num", "equity", "time"])

    rows = []
    equity = initial_capital
    for i, t in enumerate(trades, 1):
        equity *= (1 + t.pnl_pct / 100)
        rows.append({
            "trade_num": i,
            "equity": round(equity, 2),
            "time": t.exit_time,
            "pnl_pct": t.pnl_pct,
        })
    return pd.DataFrame(rows)


def save_demo_report(results: list[BacktestResult]) -> None:
    """保存演示报告到 demo_output/。"""
    DEMO_DIR.mkdir(parents=True, exist_ok=True)

    # 汇总 CSV
    summary_path = DEMO_DIR / "summary.csv"
    with open(summary_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "symbol", "timeframe", "trades", "win_rate", "total_return",
            "profit_factor", "max_drawdown", "sharpe", "long_trades",
            "short_trades", "long_win_rate", "short_win_rate",
        ])
        writer.writeheader()
        for r in results:
            writer.writerow({
                "symbol": r.symbol,
                "timeframe": r.timeframe,
                "trades": r.total_trades,
                "win_rate": r.win_rate,
                "total_return": r.total_return_pct,
                "profit_factor": r.profit_factor,
                "max_drawdown": r.max_drawdown_pct,
                "sharpe": r.sharpe_ratio,
                "long_trades": r.long_trades,
                "short_trades": r.short_trades,
                "long_win_rate": r.long_win_rate,
                "short_win_rate": r.short_win_rate,
            })

    # 每个结果保存交易明细 + 净值曲线
    for r in results:
        safe_sym = r.symbol.replace("/", "_").replace("-", "_")
        # 交易明细
        trades_path = DEMO_DIR / f"{safe_sym}_{r.timeframe}_trades.csv"
        if r.trades:
            with open(trades_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "side", "entry_time", "entry_price", "exit_time", "exit_price",
                    "stop_loss", "take_profit", "pnl_pct", "exit_reason", "bars_held",
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
                        "pnl_pct": t.pnl_pct,
                        "exit_reason": t.exit_reason,
                        "bars_held": t.bars_held,
                    })

        # 净值曲线
        equity_df = generate_equity_curve(r.trades)
        if not equity_df.empty:
            equity_path = DEMO_DIR / f"{safe_sym}_{r.timeframe}_equity.csv"
            equity_df.to_csv(equity_path, index=False)

    print(f"\n📁 报告已保存到 {DEMO_DIR}/")
    print(f"   汇总: {summary_path.name}")
    for r in results:
        safe_sym = r.symbol.replace("/", "_").replace("-", "_")
        print(f"   {r.symbol} {r.timeframe}: {safe_sym}_{r.timeframe}_trades.csv, {safe_sym}_{r.timeframe}_equity.csv")


def main():
    parser = argparse.ArgumentParser(description="模拟数据回测演示（无需 OKX 网络）")
    parser.add_argument("--symbol", type=str, default="DEMO-USDT", help="模拟交易对名称")
    parser.add_argument("--bars", type=int, default=1000, help="模拟 K 线根数")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    args = parser.parse_args()

    config = load_config()

    print("="*60)
    print("  OKX TradFi 分型策略 — 模拟数据回测演示")
    print("="*60)
    print(f"  交易对: {args.symbol}")
    print(f"  K线数:  {args.bars}")
    print(f"  随机种子: {args.seed}")
    print(f"  周期配置: {list(config.timeframes.keys())}")
    print("="*60)

    # 生成模拟数据
    print("\n生成模拟 K 线数据...")
    df = generate_simulated_ohlcv(bars=args.bars, seed=args.seed)
    print(f"  生成 {len(df)} 根 K 线，价格范围 {df['low'].min():.2f} ~ {df['high'].max():.2f}")

    # 四周期回测
    all_results: list[BacktestResult] = []
    for tf_str, tf_cfg in config.timeframes.items():
        print(f"\n回测 {args.symbol} {tf_str} ...")
        result = backtest_fractal(
            df=df,
            symbol=args.symbol,
            timeframe=tf_str,
            left=tf_cfg.left,
            right=tf_cfg.right,
            rr=tf_cfg.rr,
            sl_buffer=tf_cfg.sl_buffer,
            fee_rate=config.fee_rate,
        )
        print(format_result_summary(result))
        all_results.append(result)

    # 汇总
    print("\n" + "="*60)
    print("  模拟回测汇总")
    print("="*60)
    print(f"{'周期':<8} {'笔数':>4} {'胜率':>8} {'收益':>10} {'盈亏因子':>8} {'最大回撤':>8} {'夏普':>6}")
    print("-"*56)
    for r in all_results:
        print(f"{r.timeframe:<8} {r.total_trades:>4} {r.win_rate:>7.1f}% {r.total_return_pct:>9.2f}% {r.profit_factor:>8} {r.max_drawdown_pct:>7.1f}% {r.sharpe_ratio:>6}")
    print("="*60)

    # 保存报告
    save_demo_report(all_results)


if __name__ == "__main__":
    main()
