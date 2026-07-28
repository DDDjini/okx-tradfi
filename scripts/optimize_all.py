"""
scripts/optimize_all.py — 全面优化对比

一次运行完成：
  1. RR 1.0-3.0 网格搜索（找最优盈亏比）
  2. 四种理论方法对比（无/缠论/波浪/缠论+波浪）
  3. 6% 止损截断效果对比
  4. 输出最佳参数建议
"""

from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.backtest import backtest_fractal, BacktestResult
from core.config import PROJECT_ROOT, load_config
from core.data_feed import fetch_ohlcv_cached, validate_symbols

RESULTS_DIR = PROJECT_ROOT / "results"


def get_data(symbol, tf_str, config):
    """拉取或读取缓存K线数据。"""
    return fetch_ohlcv_cached(
        symbol=symbol, timeframe=tf_str,
        limit=config.exchange_limit, proxy_url=config.proxy_url, use_cache=True,
    )


def run_grid_search(config) -> list[dict]:
    """RR 1.0→3.0 网格搜索。"""
    valid_syms, _ = validate_symbols(config.symbols, config.proxy_url)
    rr_values = np.arange(1.0, 3.1, 0.1)
    rows = []

    total = len(valid_syms) * len(config.timeframes) * len(rr_values)
    print(f"\n[1/3] RR网格搜索: {total} 组 ...")
    n = 0

    for symbol in valid_syms:
        for tf_str, tf_cfg in config.timeframes.items():
            try:
                df = get_data(symbol, tf_str, config)
            except Exception:
                continue
            if df is None or len(df) < 50:
                continue

            for rr in rr_values:
                n += 1
                result = backtest_fractal(
                    df=df, symbol=symbol, timeframe=tf_str,
                    left=tf_cfg.left, right=tf_cfg.right,
                    rr=float(rr), sl_buffer=tf_cfg.sl_buffer,
                    fee_rate=config.fee_rate, enable_filters=True,
                    max_sl_pct=0.06,
                )
                rows.append({
                    "symbol": symbol, "timeframe": tf_str, "rr": round(rr, 1),
                    "trades": result.total_trades, "win_rate": result.win_rate,
                    "total_return_pct": result.total_return_pct,
                    "profit_factor": result.profit_factor,
                    "max_drawdown_pct": result.max_drawdown_pct,
                    "sharpe": result.sharpe_ratio,
                    "sl_capped": result.sl_capped_count,
                })
                if n % 50 == 0:
                    print(f"  进度: {n}/{total}")

    # 保存
    path = RESULTS_DIR / "rr_grid_search.csv"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    # 总结最佳
    print(f"\n  RR 网格搜索完成，{len(rows)} 组。总结：")
    by_rr = {}
    for r in rows:
        rr = r["rr"]
        if rr not in by_rr:
            by_rr[rr] = {"wr": [], "ret": [], "pf": []}
        if r["trades"] >= 2:
            by_rr[rr]["wr"].append(r["win_rate"])
            by_rr[rr]["ret"].append(r["total_return_pct"])
            by_rr[rr]["pf"].append(r["profit_factor"])

    best_wr_rr = max(by_rr, key=lambda k: np.mean(by_rr[k]["wr"]) if by_rr[k]["wr"] else 0)
    best_ret_rr = max(by_rr, key=lambda k: np.mean(by_rr[k]["ret"]) if by_rr[k]["ret"] else -999)

    print(f"  胜率最高 RR: {best_wr_rr} (均WR={np.mean(by_rr[best_wr_rr]['wr']):.1f}%)")
    print(f"  收益最高 RR: {best_ret_rr} (均Ret={np.mean(by_rr[best_ret_rr]['ret']):.2f}%)")

    # 详细表
    print(f"\n  {'RR':>5} {'均胜率':>8} {'均收益':>9} {'均PF':>7} {'盈利组':>7}")
    for rr in sorted(by_rr.keys()):
        wr = np.mean(by_rr[rr]["wr"]) if by_rr[rr]["wr"] else 0
        ret = np.mean(by_rr[rr]["ret"]) if by_rr[rr]["ret"] else 0
        pf_vals = [p for p in by_rr[rr]["pf"] if p != float("inf")]
        pf = np.mean(pf_vals) if pf_vals else 0
        prof = sum(1 for r in by_rr[rr]["ret"] if r > 0)
        total = len(by_rr[rr]["ret"])
        print(f"  {rr:>4.1f} {wr:>7.1f}% {ret:>8.2f}% {pf:>6.2f} {prof:>4}/{total}")

    return rows


def run_theory_comparison(config):
    """四种理论方法对比。"""
    valid_syms, _ = validate_symbols(config.symbols, config.proxy_url)

    methods = [
        (None, "PSY+EMA"),
        ("chan", "PSY+EMA+缠论"),
        ("elliott", "PSY+EMA+波浪"),
        ("chan+elliott", "PSY+EMA+缠论+波浪"),
    ]

    print(f"\n[2/3] 理论方法对比: {len(valid_syms)} 交易对 × {len(config.timeframes)} 周期 × {len(methods)} 方法")

    all_results = []
    for symbol in valid_syms:
        for tf_str, tf_cfg in config.timeframes.items():
            try:
                df = get_data(symbol, tf_str, config)
            except Exception:
                continue
            if df is None or len(df) < 50:
                continue

            row = {"symbol": symbol, "timeframe": tf_str}
            best_method = None
            best_ret = -999

            for method_code, method_name in methods:
                result = backtest_fractal(
                    df=df, symbol=symbol, timeframe=tf_str,
                    left=tf_cfg.left, right=tf_cfg.right,
                    rr=tf_cfg.rr, sl_buffer=tf_cfg.sl_buffer,
                    fee_rate=config.fee_rate, enable_filters=True,
                    max_sl_pct=0.06, theory_method=method_code,
                )
                key = str(method_code or "none")
                row[f"{key}_trades"] = result.total_trades
                row[f"{key}_wr"] = result.win_rate
                row[f"{key}_ret"] = result.total_return_pct
                row[f"{key}_pf"] = result.profit_factor
                row[f"{key}_dd"] = result.max_drawdown_pct
                row[f"{key}_filtered"] = result.signals_filtered

                if result.total_trades >= 2 and result.total_return_pct > best_ret:
                    best_ret = result.total_return_pct
                    best_method = method_name

            row["best_method"] = best_method
            all_results.append(row)

    # 总结
    print(f"\n  理论方法对比完成。总结：")
    totals = {}
    for m in methods:
        key = str(m[0] or "none")
        totals[key] = {"ret_sum": 0, "wr_sum": 0, "prof": 0, "count": 0, "name": m[1]}

    for r in all_results:
        for m in methods:
            key = str(m[0] or "none")
            trades = r.get(f"{key}_trades", 0)
            if trades >= 2:
                totals[key]["ret_sum"] += r[f"{key}_ret"]
                totals[key]["wr_sum"] += r[f"{key}_wr"]
                totals[key]["prof"] += 1 if r[f"{key}_ret"] > 0 else 0
                totals[key]["count"] += 1

    print(f"\n  {'方法':<22} {'有效组':>6} {'均胜率':>8} {'均收益':>9} {'盈利组':>7}")
    if all(t["count"] > 0 for t in totals.values()):
        best_overall = max(totals, key=lambda k: totals[k]["ret_sum"] / totals[k]["count"])
    else:
        best_overall = None

    for key, t in totals.items():
        if t["count"] > 0:
            avg_wr = t["wr_sum"] / t["count"]
            avg_ret = t["ret_sum"] / t["count"]
            mark = " ← 最佳" if key == best_overall else ""
            print(f"  {t['name']:<22} {t['count']:>6} {avg_wr:>7.1f}% {avg_ret:>8.2f}% {t['prof']:>5}/{t['count']}{mark}")

    # 统计哪个方法赢了最多组合
    method_wins = {}
    for r in all_results:
        bm = r.get("best_method", "")
        method_wins[bm] = method_wins.get(bm, 0) + 1
    print(f"\n  各方法在单个组合上胜出次数：")
    for m, count in sorted(method_wins.items(), key=lambda x: -x[1]):
        print(f"    {m}: {count} 次")

    # 保存
    path = RESULTS_DIR / "theory_comparison.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=all_results[0].keys())
        writer.writeheader()
        writer.writerows(all_results)

    return all_results


def run_sl_cap_comparison(config):
    """止损截断有无对比。"""
    valid_syms, _ = validate_symbols(config.symbols, config.proxy_url)

    print(f"\n[3/3] 止损截断对比: 无截断 vs 6%截断")

    results = []
    for symbol in valid_syms[:5]:  # 前5个就够了
        for tf_str, tf_cfg in config.timeframes.items():
            try:
                df = get_data(symbol, tf_str, config)
            except Exception:
                continue
            if df is None or len(df) < 50:
                continue

            r_no = backtest_fractal(df=df, symbol=symbol, timeframe=tf_str,
                left=tf_cfg.left, right=tf_cfg.right, rr=tf_cfg.rr, sl_buffer=tf_cfg.sl_buffer,
                fee_rate=config.fee_rate, enable_filters=True, max_sl_pct=0)
            r_yes = backtest_fractal(df=df, symbol=symbol, timeframe=tf_str,
                left=tf_cfg.left, right=tf_cfg.right, rr=tf_cfg.rr, sl_buffer=tf_cfg.sl_buffer,
                fee_rate=config.fee_rate, enable_filters=True, max_sl_pct=0.06)

            diff = r_yes.total_return_pct - r_no.total_return_pct
            results.append({
                "symbol": symbol, "timeframe": tf_str,
                "no_sl_cap_ret": r_no.total_return_pct, "yes_sl_cap_ret": r_yes.total_return_pct,
                "diff": diff, "capped": r_yes.sl_capped_count,
            })

    # 总结
    diffs = [r["diff"] for r in results]
    avg_diff = np.mean(diffs) if diffs else 0
    better = sum(1 for d in diffs if d > 0)
    print(f"\n  6%止损截断: 平均收益变化 {avg_diff:+.2f}%, {better}/{len(diffs)} 组改善")

    # 保存
    path = RESULTS_DIR / "sl_cap_comparison.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)

    return results


if __name__ == "__main__":
    config = load_config()
    print("=" * 60)
    print("  OKX TradFi 全面参数优化")
    print("=" * 60)

    t0 = time.time()

    # 1. RR 网格搜索
    grid_rows = run_grid_search(config)

    # 2. 理论方法对比
    theory_rows = run_theory_comparison(config)

    # 3. 止损截断对比
    sl_rows = run_sl_cap_comparison(config)

    elapsed = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"  优化完成！总耗时 {elapsed:.0f}秒")
    print(f"  结果已保存到 {RESULTS_DIR}/")
    print(f"    - rr_grid_search.csv")
    print(f"    - theory_comparison.csv")
    print(f"    - sl_cap_comparison.csv")
    print(f"{'=' * 60}")
