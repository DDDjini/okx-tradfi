"""
core/risk.py — 风控闸门

三重闸门：
  1. 信号去重（同一 signal_id 不重复推送）
  2. 同向冷却（同交易对同方向在冷却期内不重复推送）
  3. 单周期每日上限（单周期每日信号数不超过上限）

状态持久化到 JSON 文件，重启后恢复。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.config import PROJECT_ROOT, RiskConfig
from core.strategy import TradeSignal

logger = logging.getLogger(__name__)

STATE_DIR = PROJECT_ROOT / "data"
STATE_FILE = STATE_DIR / "risk_state.json"


class RiskController:
    """风控控制器：三重闸门 + 状态持久化。"""

    def __init__(self, config: RiskConfig, state_file: Optional[Path] = None):
        self.config = config
        self.state_file = state_file or STATE_FILE
        # 推送记录: {signal_id: timestamp}
        self._pushed: dict[str, float] = {}
        # 冷却记录: {f"{symbol}|{side}": last_push_timestamp}
        self._cooldowns: dict[str, float] = {}
        self._load_state()

    # ---- 状态持久化 ----

    def _load_state(self) -> None:
        """从 JSON 文件加载历史状态。"""
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            self._pushed = state.get("pushed", {})
            self._cooldowns = state.get("cooldowns", {})
            logger.info("风控状态已加载：%d 条推送记录", len(self._pushed))
        except Exception as e:
            logger.warning("加载风控状态失败：%s，从空白开始", e)

    def _save_state(self) -> None:
        """持久化状态到 JSON。"""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"pushed": self._pushed, "cooldowns": self._cooldowns},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as e:
            logger.warning("保存风控状态失败：%s", e)

    # ---- 三重闸门 ----

    def allow(self, signal: TradeSignal) -> tuple[bool, str]:
        """
        判断信号是否允许推送。

        返回 (allowed: bool, reason: str)。
        """
        now = time.time()

        # 闸门 1：信号去重
        sid = signal.signal_id()
        if sid in self._pushed:
            return False, "已推送过"

        # 闸门 2：同向冷却
        cooldown_key = f"{signal.symbol}|{signal.side.value}"
        last_push = self._cooldowns.get(cooldown_key)
        if last_push is not None:
            elapsed = now - last_push
            if elapsed < self.config.signal_cooldown_sec:
                remaining = self.config.signal_cooldown_sec - elapsed
                return False, f"冷却期内（剩余 {remaining/3600:.1f}h）"

        # 闸门 3：单周期每日上限
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_count = sum(
            1
            for sid_key, ts in self._pushed.items()
            if datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") == today
            and f"|{signal.timeframe}|" in sid_key
        )
        if daily_count >= self.config.max_signals_per_day:
            return False, f"今日 {signal.timeframe} 信号达上限（{self.config.max_signals_per_day}）"

        return True, "ok"

    def record(self, signal: TradeSignal) -> None:
        """记录已推送的信号（通过风控后调用）。"""
        now = time.time()
        sid = signal.signal_id()
        self._pushed[sid] = now
        self._cooldowns[f"{signal.symbol}|{signal.side.value}"] = now
        self._save_state()
        logger.info("风控记录：%s 已登记", sid)

    # ---- 清理 ----

    def cleanup(self, max_age_days: int = 7) -> None:
        """清理过期的推送记录（默认保留 7 天）。"""
        cutoff = time.time() - max_age_days * 86400
        before = len(self._pushed)
        self._pushed = {k: v for k, v in self._pushed.items() if v > cutoff}
        self._cooldowns = {k: v for k, v in self._cooldowns.items() if v > cutoff}
        after = len(self._pushed)
        if before != after:
            logger.info("风控清理：%d → %d 条记录", before, after)
            self._save_state()
