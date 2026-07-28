"""
core/config.py — 统一配置加载

从 config.yaml 读取所有策略/风控/监控参数，从 .env 读取密钥与代理。
全局只需调用 load_config() 一次，返回不可变 Config 对象。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class TimeframeConfig:
    """单周期参数。"""
    tf: str
    left: int
    right: int
    rr: float
    sl_buffer: float


@dataclass(frozen=True)
class RiskConfig:
    account_size: float
    risk_per_trade: float
    signal_cooldown_sec: int
    max_signals_per_day: int


@dataclass(frozen=True)
class Config:
    symbols: list[str]
    timeframes: dict[str, TimeframeConfig]
    fee_rate: float
    risk: RiskConfig
    fetch_delay_sec: int
    smart_polling: bool
    exchange_limit: int
    log_level: str
    log_file: str
    # 来自 .env
    feishu_webhook: str
    feishu_secret: str
    proxy_url: str


def load_config(config_path: str | Path | None = None) -> Config:
    """加载 config.yaml + .env，返回 Config 对象。"""
    # 加载 .env
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    # 加载 yaml
    if config_path is None:
        config_path = PROJECT_ROOT / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f)

    # 解析四周期
    tf_raw = raw.get("timeframes", {})
    timeframes: dict[str, TimeframeConfig] = {}
    for tf, params in tf_raw.items():
        timeframes[tf] = TimeframeConfig(
            tf=tf,
            left=int(params["left"]),
            right=int(params["right"]),
            rr=float(params["rr"]),
            sl_buffer=float(params["sl_buffer"]),
        )

    risk_raw = raw.get("risk", {})
    risk = RiskConfig(
        account_size=float(risk_raw.get("account_size", 10000)),
        risk_per_trade=float(risk_raw.get("risk_per_trade", 0.01)),
        signal_cooldown_sec=int(risk_raw.get("signal_cooldown_sec", 14400)),
        max_signals_per_day=int(risk_raw.get("max_signals_per_day", 6)),
    )

    monitor_raw = raw.get("monitor", {})
    logging_raw = raw.get("logging", {})
    exchange_raw = raw.get("exchange", {})
    fee_raw = raw.get("fee", {})

    return Config(
        symbols=list(raw.get("symbols", [])),
        timeframes=timeframes,
        fee_rate=float(fee_raw.get("rate", 0.0005)),
        risk=risk,
        fetch_delay_sec=int(monitor_raw.get("fetch_delay_sec", 8)),
        smart_polling=bool(monitor_raw.get("smart_polling", True)),
        exchange_limit=int(exchange_raw.get("limit", 300)),
        log_level=str(logging_raw.get("level", "INFO")),
        log_file=str(logging_raw.get("file", "logs/monitor.log")),
        feishu_webhook=os.getenv("FEISHU_WEBHOOK", ""),
        feishu_secret=os.getenv("FEISHU_SECRET", ""),
        proxy_url=os.getenv("PROXY_URL", ""),
    )
