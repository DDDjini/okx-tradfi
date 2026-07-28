"""
core/chan_theory.py — 缠论分析 + 波浪理论信号过滤

缠论核心：
  - 笔(Bi)：相邻顶底分型间具有方向性的K线运动
  - 线段(Segment)：至少3笔构成的趋势结构
  - 中枢(Center)：线段重叠形成的密集交易区间
  - 三买/三卖：回调中枢确认 + 趋势延续入场点

波浪理论：
  - 5浪推动结构检测
  - 第三浪（最强浪）入场点
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.strategy import Side


# ============================================================
#  缠论：笔(Bi) 检测
# ============================================================

def detect_bi_sequence(
    df: pd.DataFrame, left: int = 3, right: int = 3, min_bars: int = 5
) -> pd.DataFrame:
    """
    检测缠论「笔」序列。

    笔 = 相邻顶底分型之间的K线段，要求至少 min_bars 根K线。
    返回 DataFrame 增加 bi_type/bi_idx 列。
    """
    df = df.copy().reset_index(drop=True)
    n = len(df)
    lows = df["low"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)

    # 找所有分型
    fractals = []  # (idx, type: 'top'/'bottom', price)
    for i in range(left, n - right):
        lo_win = lows[i - left: i + right + 1]
        if lows[i] == lo_win.min() and (lo_win == lows[i]).sum() == 1:
            fractals.append((i, "bottom", lows[i]))
            continue
        hi_win = highs[i - left: i + right + 1]
        if highs[i] == hi_win.max() and (hi_win == highs[i]).sum() == 1:
            fractals.append((i, "top", highs[i]))

    if len(fractals) < 2:
        df["bi_type"] = 0
        df["bi_idx"] = -1
        return df

    # 合并同向连续分型（取极值）
    merged = [fractals[0]]
    for f in fractals[1:]:
        prev = merged[-1]
        if f[1] == prev[1]:  # 同向
            if f[1] == "bottom" and f[2] < prev[2]:
                merged[-1] = f
            elif f[1] == "top" and f[2] > prev[2]:
                merged[-1] = f
        else:
            merged.append(f)

    # 筛选有效的笔（至少 min_bars 根K线，且顶底交替）
    bi_list = [(0, "start", df["close"].iloc[0])]  # 起点
    last_bi_idx = 0
    for f in merged:
        if f[0] - last_bi_idx >= min_bars:
            bi_list.append(f)
            last_bi_idx = f[0]

    # 标记每根K线属于第几笔
    df["bi_type"] = 0   # 0=中性, 1=向上笔, -1=向下笔
    df["bi_idx"] = -1

    for i in range(1, len(bi_list)):
        _, bi_dir, _ = bi_list[i - 1]
        end_idx, end_type, end_price = bi_list[i]
        direction = 1 if end_type in ("top", "start") and i % 2 == 0 else -1
        direction = 1 if end_type == "top" else (
            -1 if end_type == "bottom" else 0
        )

        df.loc[bi_list[i - 1][0]:end_idx, "bi_type"] = direction
        df.loc[bi_list[i - 1][0]:end_idx, "bi_idx"] = i - 1

    return df


# ============================================================
#  缠论：中枢(Center) 检测
# ============================================================

def detect_centers(df: pd.DataFrame) -> list[dict]:
    """
    从包含 bi_type 的数据中检测中枢。

    中枢 = 连续至少3笔的重叠区间。
    返回中枢列表 [{start_idx, end_idx, zg, zd, confidence}]。
    """
    if "bi_type" not in df.columns:
        return []

    bi_changes = []
    for i in range(1, len(df)):
        if df["bi_type"].iloc[i] != df["bi_type"].iloc[i - 1] and df["bi_type"].iloc[i] != 0:
            bi_changes.append({
                "idx": i,
                "price": df["close"].iloc[i],
                "high": df["high"].iloc[i],
                "low": df["low"].iloc[i],
                "dir": df["bi_type"].iloc[i],
            })

    centers = []
    i = 0
    while i < len(bi_changes) - 2:
        seg = bi_changes[i:i + 4]  # 至少4个拐点=3笔
        if len(seg) >= 4:
            highs = [s["high"] for s in seg]
            lows = [s["low"] for s in seg]
            zg = min(highs[0], highs[2])
            zd = max(lows[0], lows[2])
            if zg > zd:  # 有重叠
                centers.append({
                    "start_idx": seg[0]["idx"],
                    "end_idx": seg[-1]["idx"],
                    "zg": zg, "zd": zd,
                    "confidence": (zg - zd) / max((zg + zd) / 2, 0.0001),
                })
            i += 2
        else:
            i += 1

    return centers


# ============================================================
#  缠论：买卖点检测
# ============================================================

def chan_trade_signal(
    df: pd.DataFrame, idx: int, side: Side,
    left: int = 3, right: int = 3,
) -> tuple[bool, str]:
    """
    缠论入场过滤：检测三买/三卖。

    三买：上升趋势中，价格回调到中枢上沿(zg)获得支撑，再次上攻
    三卖：下降趋势中，价格反弹到中枢下沿(zd)遇阻，再次下跌
    """
    if idx < 34:
        return False, "数据不足"

    # 检测笔序列
    if "bi_type" not in df.columns:
        df_tmp = detect_bi_sequence(df[:idx + 1])
        bi_type = df_tmp["bi_type"].iloc[idx]
    else:
        bi_type = df["bi_type"].iloc[idx]

    centers = detect_centers(df.iloc[:idx + 1])

    if side == Side.LONG:
        # 检查是否在向上的笔中
        if bi_type <= 0:
            return False, "不在向上笔中"

        # 检查是否有中枢支撑
        for c in reversed(centers[-3:] if len(centers) > 3 else centers):
            price = df["close"].iloc[idx]
            if c["zd"] <= price <= c["zg"] + c["confidence"] * 100:
                return True, f"缠论三买：价格({price:.2f})回踩中枢[{c['zd']:.2f}-{c['zg']:.2f}]"
        return False, "未回踩中枢"

    else:  # SHORT
        if bi_type >= 0:
            return False, "不在向下笔中"

        for c in reversed(centers[-3:] if len(centers) > 3 else centers):
            price = df["close"].iloc[idx]
            if c["zd"] - c["confidence"] * 100 <= price <= c["zg"]:
                return True, f"缠论三卖：价格({price:.2f})反弹至中枢[{c['zd']:.2f}-{c['zg']:.2f}]"
        return False, "未反弹中枢"


# ============================================================
#  波浪理论：5浪推动 + ABC修正
# ============================================================

def detect_elliott_wave(df: pd.DataFrame, idx: int) -> tuple[bool, str, int]:
    """
    检测艾略特波浪结构。

    5浪推动：1(涨)-2(回调)-3(主升)-4(回调)-5(末升)
    返回 (是否在推动浪中, 当前浪位置描述, 浪数)。
    """
    n = len(df)
    if idx < 20:
        return False, "数据不足", 0

    # 简化检测：找连续的高低点序列
    window = df.iloc[max(0, idx - 50):idx + 1]
    highs = window["high"].values
    lows = window["low"].values
    closes = window["close"].values

    # 简化版：检测连续N个更高高点（上升推动）
    swing_highs = []
    swing_lows = []
    for i in range(3, len(window) - 3):
        lo_win = lows[i - 3:i + 4]
        hi_win = highs[i - 3:i + 4]
        if lows[i] == lo_win.min() and (lo_win == lows[i]).sum() == 1:
            swing_lows.append((i, lows[i]))
        if highs[i] == hi_win.max() and (hi_win == highs[i]).sum() == 1:
            swing_highs.append((i, highs[i]))

    # 检测上升5浪
    if len(swing_highs) >= 5 and len(swing_lows) >= 4:
        recent_highs = swing_highs[-5:]
        recent_lows = swing_lows[-4:]

        # 检查是否规则上升：HH > HH > HH, HL > HL > HL
        h_prices = [h[1] for h in recent_highs]
        l_prices = [l[1] for l in recent_lows]

        if (h_prices[0] < h_prices[1] < h_prices[2] < h_prices[3] < h_prices[4] and
            l_prices[0] < l_prices[1] < l_prices[2] < l_prices[3]):
            return True, "上升5浪推动中", 5

        # 检查是否是第3浪（最强）
        if len(h_prices) >= 3 and len(l_prices) >= 2:
            if h_prices[0] < h_prices[1] < h_prices[2] and l_prices[0] < l_prices[1]:
                return True, "第3浪主升中", 3

    return False, "", 0


def elliott_filter(
    df: pd.DataFrame, idx: int, side: Side
) -> tuple[bool, str]:
    """
    波浪理论入场过滤：只在第3浪中入场。
    """
    in_wave, desc, wave_num = detect_elliott_wave(df, idx)

    if not in_wave:
        return False, "不在推动浪中"

    # 做多：需要上升推动浪
    if side == Side.LONG:
        if wave_num == 3:
            return True, f"波浪：第3浪({desc})，最强主升"
        if wave_num == 5:
            return True, f"波浪：上升5浪({desc})"
        return False, f"波浪：浪{wave_num}，不适合做多"

    # 做空：需要下降推动浪
    else:
        return False, "波浪理论主要用于做多，做空需ABC修正检测"


def elliott_abc_filter(
    df: pd.DataFrame, idx: int, side: Side
) -> tuple[bool, str]:
    """
    ABC 修正浪检测：做空用。
    """
    if side == Side.LONG:
        return True, ""

    n = len(df)
    if idx < 20:
        return False, "数据不足"

    window = df.iloc[max(0, idx - 50):idx + 1]
    highs = window["high"].values
    lows = window["low"].values

    swing_lows = []
    for i in range(3, len(window) - 3):
        if lows[i] == lows[i - 3:i + 4].min() and (lows[i - 3:i + 4] == lows[i]).sum() == 1:
            swing_lows.append((i, lows[i]))

    if len(swing_lows) >= 3:
        recent = swing_lows[-3:]
        if recent[0][1] > recent[1][1] < recent[2][1]:
            return True, "ABC修正：下降C浪"

    return False, "不在ABC修正中"


# ============================================================
#  统一调用入口
# ============================================================

def apply_theory_filter(
    df: pd.DataFrame, idx: int, side: Side,
    method: str = "chan",
) -> tuple[bool, str]:
    """
    统一理论过滤器入口。

    参数
    ----
    method: "chan" | "elliott" | "chan+elliott"
    """
    if method == "chan":
        return chan_trade_signal(df, idx, side)

    elif method == "elliott":
        if side == Side.LONG:
            return elliott_filter(df, idx, side)
        else:
            return elliott_abc_filter(df, idx, side)

    elif method == "chan+elliott":
        ok1, r1 = chan_trade_signal(df, idx, side)
        if not ok1:
            return False, f"缠论: {r1}"
        if side == Side.LONG:
            ok2, r2 = elliott_filter(df, idx, side)
        else:
            ok2, r2 = elliott_abc_filter(df, idx, side)
        if not ok2:
            return False, f"波浪: {r2}"
        return True, f"缠论 {r1} + 波浪 {r2}"

    return True, ""
