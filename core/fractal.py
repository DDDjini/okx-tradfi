"""
core/fractal.py — 高低点分型识别

分型定义：
  低点分型：第 i 根 K 线最低价是 [i-left, i+right] 窗口内唯一的最低点
  高点分型：第 i 根 K 线最高价是 [i-left, i+right] 窗口内唯一的最高点

无未来函数：分型在第 i 根出现，但需等第 i+right 根收盘才确认。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def add_fractals(df: pd.DataFrame, left: int, right: int) -> pd.DataFrame:
    """
    为 K 线 DataFrame 添加 fractal_low / fractal_high 两列（bool）。

    参数
    ----
    df    : 至少包含 open/high/low/close 列的 DataFrame
    left  : 左侧窗口根数
    right : 右侧窗口根数（也即确认延迟根数）

    返回
    ----
    添加了两列的新 DataFrame（不修改原对象）
    """
    df = df.copy().reset_index(drop=True)
    n = len(df)
    if n < left + right + 1:
        df["fractal_low"] = False
        df["fractal_high"] = False
        return df

    lows = df["low"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    fractal_low = np.zeros(n, dtype=bool)
    fractal_high = np.zeros(n, dtype=bool)

    # 滑动窗口检测：用 numpy 切片比纯 Python 循环快 5-10 倍
    for i in range(left, n - right):
        lo_win = lows[i - left: i + right + 1]
        hi_win = highs[i - left: i + right + 1]

        lo_min = lo_win.min()
        if lows[i] == lo_min and np.sum(lo_win == lo_min) == 1:
            fractal_low[i] = True

        hi_max = hi_win.max()
        if highs[i] == hi_max and np.sum(hi_win == hi_max) == 1:
            fractal_high[i] = True

    df["fractal_low"] = fractal_low
    df["fractal_high"] = fractal_high
    return df


def last_confirmed_fractal(df: pd.DataFrame, right: int) -> dict | None:
    """
    找到最后一根**已确认**的分型（即出现位置 + right <= 最后一根索引）。

    返回 dict：{idx, type: 'low'|'high', price} 或 None。
    用于实时监控中判断"刚收盘确认的新分型"。
    """
    df = df.reset_index(drop=True)
    n = len(df)
    # 可确认的最大分型位置：n - 1 - right
    confirmable_end = n - 1 - right
    if confirmable_end < 0:
        return None

    # 从后往前找最后一个分型
    for i in range(confirmable_end, -1, -1):
        if df.loc[i, "fractal_low"]:
            return {"idx": i, "type": "low", "price": float(df.loc[i, "low"])}
        if df.loc[i, "fractal_high"]:
            return {"idx": i, "type": "high", "price": float(df.loc[i, "high"])}
    return None
