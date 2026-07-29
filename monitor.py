"""
monitor.py — 实时监控主循环（智能 K 线收盘对齐）

轮询逻辑：
  只在 30m / 1h / 4h / 1d K 线收盘时触发数据拉取和信号扫描，
  其他时间休眠等待下一个收盘时刻 + 8s 延迟（确保交易所数据就绪）。

K线收盘时刻（UTC）：
  30m → :00 / :30
  1h  → :00
  4h  → 00:00 / 04:00 / 08:00 / 12:00 / 16:00 / 20:00
  1d  → 00:00

启动时先扫一轮全量，然后进入等收盘轮询。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import PROJECT_ROOT, load_config
from core.data_feed_yahoo import fetch_yahoo_cached
from core.fractal import add_fractals, last_confirmed_fractal
from core.indicators import add_all_indicators, filter_signal
from core.risk import RiskController
from core.strategy import Side, build_signal
from notify.feishu import FeishuNotifier

import json

PUSHED_CACHE_FILE = PROJECT_ROOT / "data" / "pushed_cache.json"


def load_pushed_cache() -> set:
    """从磁盘加载已推送记录。"""
    if PUSHED_CACHE_FILE.exists():
        try:
            with open(PUSHED_CACHE_FILE, "r") as f:
                data = json.load(f)
            return set(data)
        except Exception:
            pass
    return set()


def save_pushed_cache(cache: set) -> None:
    """保存已推送记录到磁盘。"""
    PUSHED_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PUSHED_CACHE_FILE, "w") as f:
        json.dump(list(cache), f)


def setup_logging(level: str, log_file: str) -> None:
    log_path = PROJECT_ROOT / log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(ch)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(fh)


def _next_candle_close(now: datetime, delay_sec: int) -> datetime:
    """
    计算下一个 K 线收盘时刻 + 延迟（UTC）。

    返回最近的下一个需要触发的时间点。
    如果多个周期同时收盘（如 :00 时 30m/1h 都收盘），只返回最早那个。
    """
    # 把所有周期下次收盘时间算出来
    candidates = []

    # 30m: :00 and :30
    minute = now.minute
    if minute < 30:
        next_t = now.replace(minute=30, second=0, microsecond=0)
    else:
        next_t = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    # 1h: :00
    next_h = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    # 4h: 00:00, 04:00, 08:00, 12:00, 16:00, 20:00
    hour = now.hour
    next_4h_hour = ((hour // 4) + 1) * 4
    if next_4h_hour >= 24:
        next_4h = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    else:
        next_4h = now.replace(hour=next_4h_hour, minute=0, second=0, microsecond=0)

    # 1d: 00:00
    next_d = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    # 找最早的一个
    return min(next_t, next_h, next_4h, next_d) + timedelta(seconds=delay_sec)


def _closed_since_last(now: datetime, delay_sec: int) -> set[str]:
    """
    判断此刻哪些周期刚收盘（在 delay_sec 窗口内）。

    返回刚收盘的周期集合，如 {"30m", "1h"}。
    """
    closed = set()
    minute = now.minute
    second = now.second
    hour = now.hour

    # 30m: 在 :00/:30 之后 delay 秒内
    if minute in (0, 30) and second <= delay_sec + 5:
        closed.add("30m")
    # 1h: 在 :00 之后
    if minute == 0 and second <= delay_sec + 5:
        closed.add("1h")
    # 4h: 在 00/04/08/12/16/20:00 之后
    if minute == 0 and hour % 4 == 0 and second <= delay_sec + 5:
        closed.add("4h")
    # 1d: 在 00:00 之后
    if hour == 0 and minute == 0 and second <= delay_sec + 5:
        closed.add("1d")

    return closed


def scan_all(
    symbols: list[str],
    timeframes: dict,
    config,
    risk_ctrl: RiskController,
    notifier: FeishuNotifier,
    pushed_cache: set,
    conflict_cache: dict,
    logger: logging.Logger,
) -> int:
    """扫描所有交易对×周期，返回推送信号数。含冲突抑制。"""
    pushed_count = 0

    for symbol in symbols:
        for tf_str, tf_cfg in timeframes.items():
            try:
                period = 730 if tf_str in ("1d", "4h", "1h") else 60
                df = fetch_yahoo_cached(
                    symbol=symbol, timeframe=tf_str,
                    period_days=period,
                )
                if df is None or len(df) < tf_cfg.left + tf_cfg.right + 3:
                    continue

                df = add_all_indicators(df)
                df = add_fractals(df, tf_cfg.left, tf_cfg.right)
                fractal = last_confirmed_fractal(df, tf_cfg.right)
                if fractal is None:
                    continue

                cache_key = f"{symbol}|{tf_str}|{fractal['idx']}"
                if cache_key in pushed_cache:
                    continue

                side = Side.LONG if fractal["type"] == "low" else Side.SHORT

                # 冲突抑制：同级别 5 根 K 线内出现反向信号 = 震荡
                opposite = "high" if fractal["type"] == "low" else "low"
                conflict_window = 5 if tf_str != "1d" else 3
                for j in range(max(0, fractal['idx'] - conflict_window),
                               min(len(df), fractal['idx'] + conflict_window)):
                    if j - tf_cfg.right >= 0 and df.loc[j - tf_cfg.right, f"fractal_{opposite}"]:
                        logger.debug("冲突抑制: %s %s idx=%d", symbol, tf_str, fractal['idx'])
                        pushed_cache.add(cache_key)
                        # 把反向信号也抑制
                        opp_key = f"{symbol}|{tf_str}|{j - tf_cfg.right}"
                        pushed_cache.add(opp_key)
                        break
                else:
                    # 没被 break 才继续
                    pass
                if cache_key in pushed_cache:
                    continue

                # PSY + EMA 过滤
                if fractal["idx"] >= 34:
                    passed, reason = filter_signal(df, fractal["idx"], side)
                    if not passed:
                        logger.debug("过滤: %s %s %s", symbol, tf_str, reason)
                        pushed_cache.add(cache_key)
                        continue

                # 1d ATR 异动过滤
                if tf_str == "1d" and fractal["idx"] >= 15:
                    highs = df["high"].iloc[max(0,fractal['idx']-14):fractal['idx']]
                    lows = df["low"].iloc[max(0,fractal['idx']-14):fractal['idx']]
                    tr = (highs.values - lows.values).mean()
                    price = df["close"].iloc[fractal['idx']]
                    atr_pct = tr / price if price > 0 else 0
                    if atr_pct < 0.015:
                        logger.debug("ATR异动: %s %s ATR=%.1f%% 太小", symbol, tf_str, atr_pct*100)
                        pushed_cache.add(cache_key)
                        continue

                entry_price = float(df.iloc[-1]["close"])
                signal_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                signal = build_signal(
                    symbol=symbol, timeframe=tf_str, side=side,
                    entry_price=entry_price, pivot_price=fractal["price"],
                    rr=tf_cfg.rr, sl_buffer=tf_cfg.sl_buffer,
                    signal_time=signal_time, risk=config.risk,
                    rr1=1.0, rr2=tf_cfg.rr,
                )

                allowed, reason = risk_ctrl.allow(signal)
                if not allowed:
                    logger.debug("风控拦截: %s", reason)
                    pushed_cache.add(cache_key)
                    continue

                success = notifier.send_signal_card(signal)
                if success:
                    risk_ctrl.record(signal)
                    pushed_cache.add(cache_key)
                    pushed_count += 1
                    logger.info("📡 %s %s %s @ %s", signal.side_cn, symbol, tf_str, entry_price)
                else:
                    logger.warning("推送失败: %s %s", symbol, tf_str)

            except Exception as e:
                logger.error("扫描 %s %s 出错: %s", symbol, tf_str, e, exc_info=True)

    return pushed_count


def main():
    parser = argparse.ArgumentParser(description="OKX TradFi 智能监控")
    parser.add_argument("--once", action="store_true", help="只扫一轮退出")
    args = parser.parse_args()

    config = load_config()
    setup_logging(config.log_level, config.log_file)
    logger = logging.getLogger(__name__)

    logger.info("=" * 50)
    logger.info("OKX TradFi 智能监控启动")
    logger.info(f"周期: {list(config.timeframes.keys())}")
    logger.info(f"智能轮询: {'ON' if config.smart_polling else 'OFF'}")
    logger.info("=" * 50)

    # Yahoo Finance 数据无需预校验，所有 config 里的 symbol 都参与
    valid_symbols = config.symbols
    if not valid_symbols:
        logger.error("config.yaml 中无交易对，退出")
        sys.exit(1)

    risk_ctrl = RiskController(config.risk)
    notifier = FeishuNotifier(config.feishu_webhook, config.feishu_secret)

    if not notifier.enabled:
        logger.warning("飞书 Webhook 未配置！请在 .env 中设置 FEISHU_WEBHOOK")

    # 从磁盘恢复已推送记录（跨轮次去重）
    pushed_cache: set[str] = load_pushed_cache()
    logger.info(f"已加载 {len(pushed_cache)} 条历史推送记录")

    # 启动时全量扫一轮
    logger.info("启动扫描: 全量检查所有周期...")
    conflict_cache: dict = {}
    count = scan_all(valid_symbols, config.timeframes, config, risk_ctrl, notifier, pushed_cache, conflict_cache, logger)
    save_pushed_cache(pushed_cache)  # 持久化去重记录
    logger.info("启动扫描完成, 推送 %d 信号", count)

    if args.once:
        return

    # 智能轮询
    logger.info("进入智能轮询模式 (Ctrl+C 退出)")

    try:
        while True:
            now = datetime.now(timezone.utc)

            if config.smart_polling:
                # 计算下一个 K 线收盘时刻
                next_trigger = _next_candle_close(now, config.fetch_delay_sec)
                wait_sec = (next_trigger - now).total_seconds()
                wait_sec = max(0, min(wait_sec, 900))  # 最多等 15 分钟

                if wait_sec > 5:
                    logger.info("休眠 %.0f 秒, 下次触发: %s UTC", wait_sec,
                        next_trigger.strftime("%H:%M:%S"))
                    time.sleep(wait_sec)
                    now = datetime.now(timezone.utc)

                closed = _closed_since_last(now, config.fetch_delay_sec)
                trigger_tfs = closed & set(config.timeframes.keys())
                if trigger_tfs:
                    logger.info("K线收盘: %s", ", ".join(sorted(trigger_tfs)))
                else:
                    # 没对齐也扫一次（可能是手动唤醒）
                    trigger_tfs = set(config.timeframes.keys())
            else:
                time.sleep(60)
                trigger_tfs = set(config.timeframes.keys())

            # 扫描
            logger.info("--- 扫描周期 %s ---", now.strftime("%Y-%m-%d %H:%M:%S UTC"))

            # 只对触发的周期扫描（减少 API 调用）
            scan_tfs = {k: v for k, v in config.timeframes.items() if k in trigger_tfs}
            count = scan_all(valid_symbols, scan_tfs, config, risk_ctrl, notifier, pushed_cache, conflict_cache, logger)

            if count > 0:
                logger.info("本轮推送 %d 信号", count)
                save_pushed_cache(pushed_cache)

            risk_ctrl.cleanup()

    except KeyboardInterrupt:
        logger.info("用户中断, 退出")


if __name__ == "__main__":
    main()
