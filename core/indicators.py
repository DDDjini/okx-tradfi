"""
core/indicators.py — 技术指标增强过滤器

包含：
  - PSY 心理线（12 日）：衡量市场情绪，超买超卖判断
  - EMA7 / EMA34 双均线：趋势方向 + 支撑阻力确认
  - 信号过滤器：结合分型 + PSY + 均线三重验证
"""

from __future__ import annotations

import pandas as pd
import numpy as np

from core.strategy import Side


def add_psy(df: pd.DataFrame, period: int = 12) -> pd.DataFrame:
    """
    添加 PSY 心理线指标。

    PSY = (N 天内上涨天数 / N) × 100
    - PSY > 75：超买区域，不宜追多
    - PSY < 25：超卖区域，不宜追空
    """
    df = df.copy()
    up = (df["close"].diff() > 0).astype(int)
    df["psy"] = up.rolling(window=period, min_periods=period).sum() / period * 100
    return df


def add_ema_dual(df: pd.DataFrame, fast: int = 7, slow: int = 34) -> pd.DataFrame:
    """
    添加 EMA 双均线（快速 7 日 / 慢速 34 日）。

    EMA7 > EMA34 → 多头趋势
    EMA7 < EMA34 → 空头趋势
    """
    df = df.copy()
    df["ema7"] = df["close"].ewm(span=fast, min_periods=fast, adjust=False).mean()
    df["ema34"] = df["close"].ewm(span=slow, min_periods=slow, adjust=False).mean()
    df["ma_trend"] = np.where(df["ema7"] > df["ema34"], 1, -1)  # 1=多头, -1=空头
    return df


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """一次性添加所有技术指标到 DataFrame。"""
    df = add_psy(df, period=12)
    df = add_ema_dual(df, fast=7, slow=34)
    return df


# ---- 信号过滤器 ----

def filter_signal(
    df: pd.DataFrame,
    idx: int,
    side: Side,
    psy_overbought: float = 70,
    psy_oversold: float = 30,
    require_trend: bool = True,
) -> tuple[bool, str]:
    """
    判断分型信号是否通过 PSY + 均线过滤。

    参数
    ----
    df : 已包含 psy/ema7/ema34/ma_trend 列的 DataFrame
    idx : 分型出现位置
    side : 多/空方向
    psy_overbought : PSY 超买阈值（默认 70）
    psy_oversold : PSY 超卖阈值（默认 30）
    require_trend : 是否要求均线趋势一致

    返回
    ----
    (通过, 原因)
    """
    row = df.iloc[idx]
    psy = row.get("psy", 50)
    ema7 = row.get("ema7", None)
    ema34 = row.get("ema34", None)

    reasons = []

    # 1. PSY 心理线过滤
    if side == Side.LONG:
        if pd.notna(psy) and psy > psy_overbought:
            return False, f"PSY={psy:.0f} 超买，不宜追多"
        if pd.notna(psy) and psy < psy_oversold:
            reasons.append(f"PSY={psy:.0f} 超卖配合✓")
    else:
        if pd.notna(psy) and psy < psy_oversold:
            return False, f"PSY={psy:.0f} 超卖，不宜追空"
        if pd.notna(psy) and psy > psy_overbought:
            reasons.append(f"PSY={psy:.0f} 超买配合✓")

    # 2. 均线趋势过滤
    if require_trend and ema7 is not None and ema34 is not None and pd.notna(ema7) and pd.notna(ema34):
        if side == Side.LONG and ema7 < ema34:
            return False, f"均线空头排列(EMA7={ema7:.4f} < EMA34={ema34:.4f})，不宜做多"
        if side == Side.SHORT and ema7 > ema34:
            return False, f"均线多头排列(EMA7={ema7:.4f} > EMA34={ema34:.4f})，不宜做空"
        if side == Side.LONG:
            reasons.append(f"均线多头✓")
        else:
            reasons.append(f"均线空头✓")

    reason_str = " | ".join(reasons) if reasons else "基本分型信号"
    return True, reason_str
