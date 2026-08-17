"""
notify/feishu.py — 飞书自定义机器人 Webhook 推送

支持：
  - 交互式消息卡片（策略卡）
  - 纯文本消息
  - 签名校验（可选）
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import logging
import time
from typing import Optional

import requests

from core.strategy import TradeSignal

logger = logging.getLogger(__name__)

# 信号胜率 >= 70% 且长历史（≥7笔）验证通过 → 飞书卡片加 ⭐
# 基于 2026-08-17 长历史回测重新评定
STAR_SIGNALS: dict[str, list[str]] = {
    "AMZN/USDT:USDT": ["1h", "4h"],
    "CL/USDT:USDT": ["1h"],
    "GOOGL/USDT:USDT": ["1h", "4h"],
    "INTC/USDT:USDT": ["4h"],
    "MU/USDT:USDT": ["1h", "4h"],
    "NVDA/USDT:USDT": ["1h"],
    "OPENAI/USDT:USDT": ["1h"],
    "SNDK/USDT:USDT": ["1h"],
    "TSLA/USDT:USDT": ["1h"],
    "XAU/USDT:USDT": ["1h", "4h"],
}


def is_star_signal(symbol: str, timeframe: str) -> bool:
    return timeframe in STAR_SIGNALS.get(symbol, [])


class FeishuNotifier:
    """飞书 Webhook 推送器。"""

    def __init__(self, webhook: str, secret: str = "", timeout: int = 10):
        self.webhook = webhook
        self.secret = secret
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.webhook)

    def _gen_sign(self, timestamp: int) -> str:
        """生成飞书签名校验。"""
        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        return base64.b64encode(hmac_code).decode("utf-8")

    def _post(self, payload: dict) -> bool:
        """发送 POST 请求到飞书 Webhook。"""
        if not self.enabled:
            logger.warning("飞书 Webhook 未配置，跳过推送")
            return False

        # 签名校验
        if self.secret:
            timestamp = int(time.time())
            payload["timestamp"] = str(timestamp)
            payload["sign"] = self._gen_sign(timestamp)

        try:
            resp = requests.post(self.webhook, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code", 0) == 0 or data.get("StatusCode", 0) == 0:
                return True
            logger.error("飞书返回错误：%s", data)
            return False
        except requests.exceptions.RequestException as e:
            logger.error("飞书推送失败（网络）：%s", e)
            return False
        except Exception as e:
            logger.error("飞书推送失败：%s", e)
            return False

    def send_signal_card(self, signal: TradeSignal) -> bool:
        """
        发送策略卡消息（交互式卡片）。

        卡片内容：方向 / 入场 / 止损 / 止盈 / 盈亏比 / 建议仓位
        """
        is_long = signal.side.value == "long"
        header_color = "green" if is_long else "red"
        direction_emoji = signal.side_emoji

        title = f"{direction_emoji} {'做多' if is_long else '做空'}信号 | {signal.symbol} | {signal.timeframe}"
        if is_star_signal(signal.symbol, signal.timeframe):
            title = f"⭐ {title}"

        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "template": header_color,
                    "title": {"tag": "plain_text", "content": title},
                },
                "elements": [
                    {
                        "tag": "div",
                        "fields": [
                            {"is_short": True, "text": {"tag": "lark_md",
                                "content": f"**方向**\n{signal.side_cn} {'🟢' if is_long else '🔴'}"}},
                            {"is_short": True, "text": {"tag": "lark_md",
                                "content": f"**周期**\n{signal.timeframe}"}},
                            {"is_short": True, "text": {"tag": "lark_md",
                                "content": f"**建议入场**\n{signal.entry_price}"}},
                            {"is_short": True, "text": {"tag": "lark_md",
                                "content": f"**止损价**\n{signal.stop_loss} ({signal.sl_pct*100:.2f}%)"}},
                            {"is_short": True, "text": {"tag": "lark_md",
                                "content": f"**止盈1** ({signal.rr1}R)\n{signal.take_profit_1} ({signal.tp1_pct*100:.2f}%)"}},
                            {"is_short": True, "text": {"tag": "lark_md",
                                "content": f"**止盈2** ({signal.rr2}R)\n{signal.take_profit_2} ({signal.tp2_pct*100:.2f}%)"}},
                        ],
                    },
                    {"tag": "hr"},
                    {
                        "tag": "div",
                        "fields": [
                            {"is_short": True, "text": {"tag": "lark_md",
                                "content": f"**分型极值**\n{signal.pivot_price}"}},
                            {"is_short": True, "text": {"tag": "lark_md",
                                "content": f"**建议仓位**\n≈ {signal.suggest_qty_usdt:.0f} USDT"}},
                        ],
                    },
                    {"tag": "hr"},
                    {
                        "tag": "note",
                        "elements": [
                            {"tag": "plain_text",
                             "content": f"信号时间: {signal.signal_time}  |  风险占比: {signal.risk_pct*100:.1f}%  |  仅供参考，不构成投资建议"}
                        ],
                    },
                ],
            },
        }
        return self._post(card)

    def send_text(self, text: str) -> bool:
        """发送纯文本消息。"""
        payload = {
            "msg_type": "text",
            "content": {"text": text},
        }
        return self._post(payload)
