"""
backtest.py — 回测 CLI 入口

用法：
  python backtest.py --symbol TSLA-USDT                      # 回测单交易对（四周期全跑）
  python backtest.py --symbol NVDA-USDT --timeframe 1h       # 指定周期
  python backtest.py --symbol TSLA-USDT --since 2025-06-01   # 指定起始时间
  python backtest.py --all                                    # 回测全部有效交易对
  python backtest.py --symbol TSLA-USDT --no-cache           # 忽略缓存重新拉取
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.backtest import BacktestResult, backtest_fractal, format_result_summary
from core.config import PROJECT_ROOT, load_config
from core.data_feed import fetch_ohlcv_cached, validate_symbols

RESULTS_DIR = PROJECT_ROOT / "results"


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def run_backtest(
    symbol: str,
    timeframe: str,
    tf_cfg,
    config,
    use_cache: bool,
    since: str | None,
) -> BacktestResult | None:
    """对单个 交易对 × 周期 运行回测。"""
    logger = logging.getLogger(__name__)
    logger.info("拉取 %s %s K 线数据...", symbol, timeframe)

    try:
        df = fetch_ohlcv_cached(
            symbol=symbol,
            timeframe=timeframe,
            limit=config.exchange_limit,
            proxy_url=config.proxy_url,
            use_cache=use_cache,
            since=since,
        )
    except Exception as e:
        logger.error("拉取 %s %s 数据失败：%s", symbol, timeframe, e)
        return None

    if df is None or len(df) < tf_cfg.left + tf_cfg.right + 3:
        logger.warning("%s %s 数据不足（%d 行），跳过", symbol, timeframe, len(df) if df is not None else 0)
        return None

    logger.info("%s %s: 获取 %d 根 K 线，开始回测...", symbol, timeframe, len(df))

    result = backtest_fractal(
        df=df,
        symbol=symbol,
        timeframe=timeframe,
        left=tf_cfg.left,
        right=tf_cfg.right,
        rr=tf_cfg.rr,
        sl_buffer=tf_cfg.sl_buffer,
        fee_rate=config.fee_rate,
    )

    return result


def save_result(result: BacktestResult) -> None:
    """将回测交易明细保存到 CSV。"""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_sym = result.symbol.replace("/", "_").replace("-", "_")
    filename = f"{safe_sym}_{result.timeframe}_trades.csv"
    filepath = RESULTS_DIR / filename

    if not result.trades:
        return

    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "side", "entry_time", "entry_price", "exit_time", "exit_price",
            "stop_loss", "take_profit", "rr", "pnl_pct", "exit_reason", "bars_held",
        ])
        writer.writeheader()
        for t in result.trades:
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
            })

    logging.getLogger(__name__).info("交易明细已保存：%s", filepath)


def main():
    parser = argparse.ArgumentParser(description="OKX TradFi 分型策略回测")
    parser.add_argument("--symbol", type=str, help="交易对（如 TSLA-USDT）")
    parser.add_argument("--all", action="store_true", help="回测 config.yaml 中全部有效交易对")
    parser.add_argument("--timeframe", type=str, default=None, help="指定周期（如 1h），不指定则四周期全跑")
    parser.add_argument("--since", type=str, default=None, help="起始时间（如 2025-06-01）")
    parser.add_argument("--no-cache", action="store_true", help="忽略缓存，强制重新拉取")
    args = parser.parse_args()

    if not args.symbol and not args.all:
        parser.error("请指定 --symbol 或 --all")

    config = load_config()
    setup_logging(config.log_level)
    logger = logging.getLogger(__name__)

    logger.info("="*50)
    logger.info("OKX TradFi 分型策略回测")
    logger.info("="*50)

    # 确定交易对列表
    if args.all:
        symbols = config.symbols
    else:
        symbols = [args.symbol]

    # 校验交易对
    valid_symbols, invalid_symbols = validate_symbols(symbols, config.proxy_url)
    if not valid_symbols:
        logger.error("没有有效的交易对")
        sys.exit(1)
    if invalid_symbols:
        logger.warning("以下交易对未上线，已剔除：%s", ", ".join(invalid_symbols))

    # 确定周期
    if args.timeframe:
        if args.timeframe not in config.timeframes:
            logger.error("未知周期：%s，可选：%s", args.timeframe, list(config.timeframes.keys()))
            sys.exit(1)
        timeframes = {args.timeframe: config.timeframes[args.timeframe]}
    else:
        timeframes = config.timeframes

    # 逐个运行回测
    all_results: list[BacktestResult] = []
    for symbol in valid_symbols:
        for tf_str, tf_cfg in timeframes.items():
            result = run_backtest(symbol, tf_str, tf_cfg, config, use_cache=not args.no_cache, since=args.since)
            if result is None:
                continue
            print()
            print(format_result_summary(result))
            save_result(result)
            all_results.append(result)

    # 汇总
    if all_results:
        print("\n" + "="*50)
        print("  回测汇总")
        print("="*50)
        print(f"{'交易对':<14} {'周期':<6} {'笔数':>4} {'胜率':>8} {'收益':>10} {'盈亏因子':>8} {'最大回撤':>8}")
        print("-"*70)
        for r in all_results:
            print(f"{r.symbol:<14} {r.timeframe:<6} {r.total_trades:>4} {r.win_rate:>7.1f}% {r.total_return_pct:>9.2f}% {r.profit_factor:>8} {r.max_drawdown_pct:>7.1f}%")
        print("="*50)
        logger.info("回测完成，共 %d 组结果", len(all_results))
    else:
        logger.warning("没有生成任何回测结果")


if __name__ == "__main__":
    main()
