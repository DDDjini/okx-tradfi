"""
scripts/test_feishu.py — 飞书连通性测试

用法：
  python scripts/test_feishu.py

会发送一条文本 + 一张模拟策略卡到飞书群。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config
from core.strategy import Side, build_signal
from notify.feishu import FeishuNotifier


def main():
    config = load_config()
    notifier = FeishuNotifier(config.feishu_webhook, config.feishu_secret)

    if not notifier.enabled:
        print("❌ 飞书 Webhook 未配置！")
        print("   请复制 .env.example 为 .env，填入 FEISHU_WEBHOOK 地址")
        sys.exit(1)

    print("正在发送测试消息...")

    # 1. 文本测试
    ok1 = notifier.send_text("✅ 信号系统连通性测试成功！")
    print(f"  文本消息: {'✅ 成功' if ok1 else '❌ 失败'}")

    # 2. 策略卡测试
    test_signal = build_signal(
        symbol="TSLA-USDT",
        timeframe="1h",
        side=Side.LONG,
        entry_price=248.50,
        pivot_price=244.90,
        rr=1.6,
        sl_buffer=0.0015,
        signal_time="2025-01-01 12:00:00 UTC (测试)",
        risk=config.risk,
    )
    ok2 = notifier.send_signal_card(test_signal)
    print(f"  策略卡片: {'✅ 成功' if ok2 else '❌ 失败'}")

    if ok1 and ok2:
        print("\n🎉 飞书连通性测试通过！")
    else:
        print("\n⚠️  部分测试失败，请检查：")
        print("  1. Webhook 地址是否正确")
        print("  2. 安全设置关键词是否填了「信号」")
        print("  3. 签名校验密钥是否匹配（如启用了签名校验）")


if __name__ == "__main__":
    main()
