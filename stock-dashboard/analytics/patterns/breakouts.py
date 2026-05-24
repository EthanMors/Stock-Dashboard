"""
Breakout and reversal pattern detectors.

Each function returns a list of DetectedPattern objects (usually 0 or 1).
All functions expect a preprocessed DataFrame (output of helpers.preprocess).
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from .helpers import (
    DetectedPattern,
    cluster_sr_levels,
    compute_confidence,
    compute_risk_reward,
    score_breakout_strength,
    score_candle_body,
    score_pattern_duration,
    score_symmetry,
    score_trend_alignment,
    score_trendline_fit,
    score_volume,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fit_line(x: np.ndarray, y: np.ndarray) -> Tuple[float, float, float]:
    """Return (slope, intercept, r2). r2=0 if variance is zero."""
    slope, intercept = np.polyfit(x, y, 1)
    y_pred = slope * x + intercept
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    if ss_tot == 0:
        return float(slope), float(intercept), 0.0
    r2 = 1.0 - float(np.sum((y - y_pred) ** 2)) / ss_tot
    return float(slope), float(intercept), r2


def _current_bar_scalars(df: pd.DataFrame) -> Tuple[float, float, float, float, float, float]:
    """Return (close, open, high, low, atr, volume_ratio) for the last bar."""
    c   = float(df["close"].iloc[-1])
    o   = float(df["open"].iloc[-1])
    h   = float(df["high"].iloc[-1])
    l   = float(df["low"].iloc[-1])
    atr = float(df["atr"].iloc[-1]) if "atr" in df.columns else c * 0.01
    vr  = float(df["volume_ratio"].iloc[-1]) if "volume_ratio" in df.columns else 1.0
    return c, o, h, l, atr, vr


def _base_comp(df: pd.DataFrame, close: float, level: float,
               direction: str, atr: float, vr: float) -> dict:
    return {
        "volume_score"      : score_volume(vr),
        "breakout_strength" : score_breakout_strength(close, level, direction, atr),
        "candle_body"       : score_candle_body(
            float(df["open"].iloc[-1]), close,
            float(df["high"].iloc[-1]), float(df["low"].iloc[-1]),
        ),
        "trend_alignment"   : score_trend_alignment(df, direction),
    }


# ---------------------------------------------------------------------------
# 1. S/R Breakout
# ---------------------------------------------------------------------------

def detect_sr_breakout(
    df: pd.DataFrame,
    lookback: int = 100,
    ticker: str = "",
    timeframe: str = "daily",
) -> List[DetectedPattern]:
    if len(df) < 20:
        return []

    close, open_, high, low, atr, vr = _current_bar_scalars(df)
    patterns: List[DetectedPattern] = []

    sr = cluster_sr_levels(df.iloc[-lookback:])
    if not sr:
        return []

    for zone in sr[:8]:  # check top 8 zones
        mid = zone["mid"]
        # Bullish breakout above resistance
        if close > mid * 1.002 and abs(close - mid) / mid < 0.03:
            comp = _base_comp(df, close, mid, "bullish", atr, vr)
            confidence = compute_confidence("breakout_resistance", comp)
            p = DetectedPattern(
                pattern_type="breakout_resistance", direction="bullish",
                start_index=df.index[-lookback], end_index=df.index[-1],
                detected_at_bar=df.index[-1], pattern_bars=lookback,
                key_levels={"resistance": mid},
                entry_price=close, stop_loss=mid * 0.997,
                target=close + (close - mid) * 2,
                confidence_score=confidence, component_scores=comp,
                volume_confirmed=vr >= 1.2,
                ticker=ticker, timeframe=timeframe,
            )
            patterns.append(compute_risk_reward(p))
            break

        # Bearish breakdown below support
        if close < mid * 0.998 and abs(close - mid) / mid < 0.03:
            comp = _base_comp(df, close, mid, "bearish", atr, vr)
            confidence = compute_confidence("breakout_support", comp)
            p = DetectedPattern(
                pattern_type="breakout_support", direction="bearish",
                start_index=df.index[-lookback], end_index=df.index[-1],
                detected_at_bar=df.index[-1], pattern_bars=lookback,
                key_levels={"support": mid},
                entry_price=close, stop_loss=mid * 1.003,
                target=close - (mid - close) * 2,
                confidence_score=confidence, component_scores=comp,
                volume_confirmed=vr >= 1.2,
                ticker=ticker, timeframe=timeframe,
            )
            patterns.append(compute_risk_reward(p))
            break

    return patterns[:1]


# ---------------------------------------------------------------------------
# 2. Bull / Bear Flag
# ---------------------------------------------------------------------------

def _detect_flag(
    df: pd.DataFrame,
    direction: str,
    ticker: str,
    timeframe: str,
    min_pole_pct: float = 0.04,
    max_pole_bars: int = 15,
    flag_bars_range: Tuple[int, int] = (5, 20),
) -> List[DetectedPattern]:
    if len(df) < 30:
        return []

    close = df["close"].values.astype(float)
    high  = df["high"].values.astype(float)
    low   = df["low"].values.astype(float)
    n     = len(df)

    # Find the best pole ending at least flag_bars_range[0] bars before the end
    min_flag = flag_bars_range[0]
    max_flag = flag_bars_range[1]

    best_pole: Optional[Tuple[int, int, float]] = None
    best_pct = min_pole_pct - 0.001
    scan_end = n - min_flag  # pole must end far enough back for flag

    for end in range(max_pole_bars, scan_end):
        for bars in range(3, min(max_pole_bars + 1, end + 1)):
            start = end - bars
            if direction == "bullish":
                pct = (close[end] - close[start]) / close[start] if close[start] > 0 else 0
            else:
                pct = (close[start] - close[end]) / close[start] if close[start] > 0 else 0
            if pct > best_pct:
                best_pct   = pct
                best_pole  = (start, end, pct)

    if best_pole is None:
        return []

    pole_start, pole_end, pole_pct = best_pole
    if direction == "bullish":
        pole_high = float(np.max(high[pole_start : pole_end + 1]))
        pole_low  = float(np.min(low[pole_start  : pole_end + 1]))
    else:
        pole_high = float(np.max(high[pole_start : pole_end + 1]))
        pole_low  = float(np.min(low[pole_start  : pole_end + 1]))
    pole_height = pole_high - pole_low

    # Flag = bars after pole_end up to the end of the data
    flag_start = pole_end + 1
    flag_len   = n - flag_start
    if not (min_flag <= flag_len <= max_flag):
        return []

    flag_x = np.arange(flag_len, dtype=float)
    flag_h = high[flag_start : flag_start + flag_len]
    flag_l = low[flag_start  : flag_start + flag_len]

    if len(flag_x) < 3:
        return []

    h_slope, h_int, h_r2 = _fit_line(flag_x, flag_h)
    l_slope, l_int, l_r2 = _fit_line(flag_x, flag_l)
    r2_avg = (h_r2 + l_r2) / 2

    if r2_avg < 0.30:
        return []

    # Bull flag: both slopes negative (downward channel); bear flag: both positive
    if direction == "bullish" and (h_slope >= 0 or l_slope >= 0):
        return []
    if direction == "bearish" and (h_slope <= 0 or l_slope <= 0):
        return []

    # Slopes should be roughly parallel
    if abs(h_slope) > 0 and abs(h_slope - l_slope) > abs(h_slope) * 0.6:
        return []

    # Breakout check at last bar
    curr_x = float(flag_len - 1)
    upper_at_end = h_slope * curr_x + h_int
    lower_at_end = l_slope * curr_x + l_int
    last_close = float(df["close"].iloc[-1])

    c_val, o_val, h_val, l_val, atr_val, vr = _current_bar_scalars(df)

    ptype: str
    if direction == "bullish" and last_close > upper_at_end * 1.002:
        ptype  = "bull_flag"
        target = pole_high + pole_height
        stop   = lower_at_end * 0.997
    elif direction == "bearish" and last_close < lower_at_end * 0.998:
        ptype  = "bear_flag"
        target = pole_low - pole_height
        stop   = upper_at_end * 1.003
    else:
        return []

    comp = {
        "volume_score"      : score_volume(vr),
        "breakout_strength" : score_breakout_strength(
            last_close,
            upper_at_end if direction == "bullish" else lower_at_end,
            direction, atr_val,
        ),
        "candle_body"       : score_candle_body(o_val, last_close, h_val, l_val),
        "trend_alignment"   : score_trend_alignment(df, direction),
        "trendline_fit"     : score_trendline_fit(r2_avg),
    }
    p = DetectedPattern(
        pattern_type=ptype, direction=direction,
        start_index=df.index[pole_start], end_index=df.index[-1],
        detected_at_bar=df.index[-1], pattern_bars=n - pole_start,
        key_levels={
            "pole_high": pole_high, "pole_low": pole_low,
            "flag_high": upper_at_end, "flag_low": lower_at_end,
            "flag_high_start": float(h_int),
            "flag_low_start":  float(l_int),
            "flag_bar_offset": float((pole_end - pole_start) + 1),
        },
        entry_price=last_close, stop_loss=stop, target=target,
        confidence_score=compute_confidence(ptype, comp),
        component_scores=comp, volume_confirmed=vr >= 1.2,
        ticker=ticker, timeframe=timeframe,
    )
    return [compute_risk_reward(p)]


def detect_bull_flag(df: pd.DataFrame, ticker: str = "", timeframe: str = "daily") -> List[DetectedPattern]:
    return _detect_flag(df, "bullish", ticker, timeframe)


def detect_bear_flag(df: pd.DataFrame, ticker: str = "", timeframe: str = "daily") -> List[DetectedPattern]:
    return _detect_flag(df, "bearish", ticker, timeframe)


# ---------------------------------------------------------------------------
# 3. Pennant (converging channel after a pole)
# ---------------------------------------------------------------------------

def _detect_pennant(
    df: pd.DataFrame,
    direction: str,
    ticker: str,
    timeframe: str,
) -> List[DetectedPattern]:
    if len(df) < 30:
        return []

    close = df["close"].values.astype(float)
    high  = df["high"].values.astype(float)
    low   = df["low"].values.astype(float)
    n     = len(df)

    # Find pole (same logic as flag)
    best_pole: Optional[Tuple[int, int, float]] = None
    best_pct = 0.039
    for end in range(5, n - 5):
        for bars in range(3, min(16, end + 1)):
            start = end - bars
            pct = (close[end] - close[start]) / close[start] if direction == "bullish" else (close[start] - close[end]) / close[start]
            if close[start] > 0 and pct > best_pct:
                best_pct, best_pole = pct, (start, end, pct)

    if best_pole is None:
        return []

    pole_start, pole_end, _ = best_pole
    pole_high  = float(np.max(high[pole_start : pole_end + 1]))
    pole_low   = float(np.min(low[pole_start  : pole_end + 1]))
    pole_height = pole_high - pole_low

    pennant_start = pole_end + 1
    pennant_len   = n - pennant_start
    if not (5 <= pennant_len <= 20):
        return []

    px = np.arange(pennant_len, dtype=float)
    ph = high[pennant_start : pennant_start + pennant_len]
    pl = low[pennant_start  : pennant_start + pennant_len]

    if len(px) < 3:
        return []

    h_slope, h_int, h_r2 = _fit_line(px, ph)
    l_slope, l_int, l_r2 = _fit_line(px, pl)
    r2_avg = (h_r2 + l_r2) / 2

    if r2_avg < 0.30:
        return []

    # Pennant: upper descending, lower ascending (converging)
    if direction == "bullish" and not (h_slope < 0 and l_slope > 0):
        return []
    if direction == "bearish" and not (h_slope < 0 and l_slope > 0):
        return []

    curr_x = float(pennant_len - 1)
    upper_end = h_slope * curr_x + h_int
    lower_end = l_slope * curr_x + l_int
    last_close = float(df["close"].iloc[-1])

    _, o_val, h_val, l_val, atr_val, vr = _current_bar_scalars(df)

    if direction == "bullish" and last_close > upper_end * 1.002:
        ptype  = "pennant_bull"
        target = pole_high + pole_height
        stop   = lower_end * 0.997
    elif direction == "bearish" and last_close < lower_end * 0.998:
        ptype  = "pennant_bear"
        target = pole_low - pole_height
        stop   = upper_end * 1.003
    else:
        return []

    comp = {
        "volume_score"      : score_volume(vr),
        "breakout_strength" : score_breakout_strength(last_close, upper_end if direction == "bullish" else lower_end, direction, atr_val),
        "candle_body"       : score_candle_body(o_val, last_close, h_val, l_val),
        "trend_alignment"   : score_trend_alignment(df, direction),
        "trendline_fit"     : score_trendline_fit(r2_avg),
    }
    p = DetectedPattern(
        pattern_type=ptype, direction=direction,
        start_index=df.index[pole_start], end_index=df.index[-1],
        detected_at_bar=df.index[-1], pattern_bars=n - pole_start,
        key_levels={
            "pole_high": pole_high, "pole_low": pole_low,
            "flag_high": upper_end, "flag_low": lower_end,
            "flag_high_start": float(h_int),
            "flag_low_start":  float(l_int),
            "flag_bar_offset": float((pole_end - pole_start) + 1),
        },
        entry_price=last_close, stop_loss=stop, target=target,
        confidence_score=compute_confidence(ptype, comp),
        component_scores=comp, volume_confirmed=vr >= 1.2,
        ticker=ticker, timeframe=timeframe,
    )
    return [compute_risk_reward(p)]


def detect_pennant(df: pd.DataFrame, ticker: str = "", timeframe: str = "daily") -> List[DetectedPattern]:
    return _detect_pennant(df, "bullish", ticker, timeframe) or _detect_pennant(df, "bearish", ticker, timeframe)


# ---------------------------------------------------------------------------
# 4–6. Triangle patterns
# ---------------------------------------------------------------------------

def _detect_triangle(
    df: pd.DataFrame,
    variant: str,  # 'asc' | 'desc' | 'sym'
    ticker: str,
    timeframe: str,
    lookback: int = 60,
) -> List[DetectedPattern]:
    if len(df) < 20:
        return []

    n   = min(lookback, len(df) - 1)
    win = df.iloc[-n:]
    x   = np.arange(n, dtype=float)

    sh_mask = win["swing_high"].notna().values
    sl_mask = win["swing_low"].notna().values

    if sh_mask.sum() < 2 or sl_mask.sum() < 2:
        return []

    sh_x, sh_y = x[sh_mask], win["swing_high"].values[sh_mask].astype(float)
    sl_x, sl_y = x[sl_mask], win["swing_low"].values[sl_mask].astype(float)

    h_slope, h_int, h_r2 = _fit_line(sh_x, sh_y)
    l_slope, l_int, l_r2 = _fit_line(sl_x, sl_y)
    r2_avg = (h_r2 + l_r2) / 2

    if r2_avg < 0.25:
        return []

    curr_x  = float(n - 1)
    avg_sh  = float(np.mean(sh_y))
    avg_sl  = float(np.mean(sl_y))

    flat_upper = abs(h_slope) / avg_sh < 0.002 if avg_sh > 0 else False
    flat_lower = abs(l_slope) / avg_sl < 0.002 if avg_sl > 0 else False

    # Variant-specific slope checks
    if variant == "asc"  and not (flat_upper and l_slope > 0):  return []
    if variant == "desc" and not (flat_lower and h_slope < 0):  return []
    if variant == "sym"  and not (h_slope < 0 and l_slope > 0): return []

    upper_at_curr = h_slope * curr_x + h_int
    lower_at_curr = l_slope * curr_x + l_int

    # Lines must still be converging
    upper_at_start = h_int
    lower_at_start = l_int
    if upper_at_curr - lower_at_curr >= upper_at_start - lower_at_start:
        return []

    close, open_, high, low, atr, vr = _current_bar_scalars(df)

    triangle_height = upper_at_start - lower_at_start

    if variant in ("asc", "sym") and close > upper_at_curr * 1.002:
        direction = "bullish"
        ptype     = f"{variant}_triangle" if variant != "sym" else "sym_triangle"
        if variant == "asc":
            ptype = "asc_triangle"
        target = close + triangle_height
        stop   = lower_at_curr * 0.997
    elif variant in ("desc", "sym") and close < lower_at_curr * 0.998:
        direction = "bearish"
        ptype     = "desc_triangle" if variant == "desc" else "sym_triangle"
        target = close - triangle_height
        stop   = upper_at_curr * 1.003
    else:
        return []

    comp = {
        "volume_score"      : score_volume(vr),
        "breakout_strength" : score_breakout_strength(close, upper_at_curr if direction == "bullish" else lower_at_curr, direction, atr),
        "candle_body"       : score_candle_body(open_, close, high, low),
        "trend_alignment"   : score_trend_alignment(df, direction),
        "trendline_fit"     : score_trendline_fit(r2_avg),
        "pattern_duration"  : score_pattern_duration(n, 15, 60),
    }
    p = DetectedPattern(
        pattern_type=ptype, direction=direction,
        start_index=df.index[-n], end_index=df.index[-1],
        detected_at_bar=df.index[-1], pattern_bars=n,
        key_levels={
            "upper_trendline": upper_at_curr, "lower_trendline": lower_at_curr,
            "upper_start": float(h_int),
            "lower_start": float(l_int),
        },
        entry_price=close, stop_loss=stop, target=target,
        confidence_score=compute_confidence(ptype, comp),
        component_scores=comp, volume_confirmed=vr >= 1.2,
        ticker=ticker, timeframe=timeframe,
    )
    return [compute_risk_reward(p)]


def detect_ascending_triangle(df, ticker="", timeframe="daily"):
    return _detect_triangle(df, "asc", ticker, timeframe)

def detect_descending_triangle(df, ticker="", timeframe="daily"):
    return _detect_triangle(df, "desc", ticker, timeframe)

def detect_symmetrical_triangle(df, ticker="", timeframe="daily"):
    return _detect_triangle(df, "sym", ticker, timeframe)


# ---------------------------------------------------------------------------
# 7–8. Wedge patterns
# ---------------------------------------------------------------------------

def _detect_wedge(
    df: pd.DataFrame,
    variant: str,  # 'rising' | 'falling'
    ticker: str,
    timeframe: str,
    lookback: int = 60,
) -> List[DetectedPattern]:
    """Rising wedge = both slopes positive but converging (bearish breakout).
    Falling wedge = both slopes negative but converging (bullish breakout)."""
    if len(df) < 20:
        return []

    n   = min(lookback, len(df) - 1)
    win = df.iloc[-n:]
    x   = np.arange(n, dtype=float)

    sh_mask = win["swing_high"].notna().values
    sl_mask = win["swing_low"].notna().values
    if sh_mask.sum() < 2 or sl_mask.sum() < 2:
        return []

    sh_x, sh_y = x[sh_mask], win["swing_high"].values[sh_mask].astype(float)
    sl_x, sl_y = x[sl_mask], win["swing_low"].values[sl_mask].astype(float)

    h_slope, h_int, h_r2 = _fit_line(sh_x, sh_y)
    l_slope, l_int, l_r2 = _fit_line(sl_x, sl_y)
    r2_avg = (h_r2 + l_r2) / 2

    if r2_avg < 0.30:
        return []

    # Rising wedge: both slopes > 0, lower slope > upper slope (converging upward)
    if variant == "rising"  and not (h_slope > 0 and l_slope > 0 and l_slope > h_slope):
        return []
    # Falling wedge: both slopes < 0, upper slope more negative (converging downward)
    if variant == "falling" and not (h_slope < 0 and l_slope < 0 and h_slope < l_slope):
        return []

    curr_x        = float(n - 1)
    upper_at_curr = h_slope * curr_x + h_int
    lower_at_curr = l_slope * curr_x + l_int

    close, open_, high, low, atr, vr = _current_bar_scalars(df)
    spread_start = abs(h_int - l_int)
    spread_end   = abs(upper_at_curr - lower_at_curr)
    if spread_end >= spread_start:
        return []

    if variant == "rising" and close < lower_at_curr * 0.998:
        direction = "bearish"
        ptype     = "rising_wedge"
        target    = close - spread_start
        stop      = upper_at_curr * 1.003
    elif variant == "falling" and close > upper_at_curr * 1.002:
        direction = "bullish"
        ptype     = "falling_wedge"
        target    = close + spread_start
        stop      = lower_at_curr * 0.997
    else:
        return []

    comp = {
        "volume_score"      : score_volume(vr),
        "breakout_strength" : score_breakout_strength(close, lower_at_curr if direction == "bearish" else upper_at_curr, direction, atr),
        "candle_body"       : score_candle_body(open_, close, high, low),
        "trend_alignment"   : score_trend_alignment(df, direction),
        "trendline_fit"     : score_trendline_fit(r2_avg),
    }
    p = DetectedPattern(
        pattern_type=ptype, direction=direction,
        start_index=df.index[-n], end_index=df.index[-1],
        detected_at_bar=df.index[-1], pattern_bars=n,
        key_levels={
            "upper_trendline": upper_at_curr, "lower_trendline": lower_at_curr,
            "upper_start": float(h_int),
            "lower_start": float(l_int),
        },
        entry_price=close, stop_loss=stop, target=target,
        confidence_score=compute_confidence(ptype, comp),
        component_scores=comp, volume_confirmed=vr >= 1.2,
        ticker=ticker, timeframe=timeframe,
    )
    return [compute_risk_reward(p)]


def detect_rising_wedge(df, ticker="", timeframe="daily"):
    return _detect_wedge(df, "rising", ticker, timeframe)

def detect_falling_wedge(df, ticker="", timeframe="daily"):
    return _detect_wedge(df, "falling", ticker, timeframe)


# ---------------------------------------------------------------------------
# 9. Double Top / Double Bottom
# ---------------------------------------------------------------------------

def _detect_double(
    df: pd.DataFrame,
    variant: str,  # 'top' | 'bottom'
    ticker: str,
    timeframe: str,
    lookback: int = 100,
) -> List[DetectedPattern]:
    if len(df) < 20:
        return []

    n   = min(lookback, len(df) - 1)
    win = df.iloc[-n:]

    if variant == "top":
        pts = win[win["swing_high"].notna()]
        col = "swing_high"
    else:
        pts = win[win["swing_low"].notna()]
        col = "swing_low"

    if len(pts) < 2:
        return []

    close, open_, high, low, atr, vr = _current_bar_scalars(df)

    # Scan all adjacent pairs
    for i in range(len(pts) - 1):
        p1_idx  = pts.index[i]
        p2_idx  = pts.index[i + 1]
        p1_val  = float(pts[col].iloc[i])
        p2_val  = float(pts[col].iloc[i + 1])

        if p1_val == 0:
            continue
        price_diff_pct = abs(p2_val - p1_val) / p1_val
        if price_diff_pct > 0.03:
            continue

        try:
            p1_pos = int(df.index.get_loc(p1_idx))
            p2_pos = int(df.index.get_loc(p2_idx))
        except Exception:
            continue

        bars_between = p2_pos - p1_pos
        if not (10 <= bars_between <= 60):
            continue

        # Trough/peak between the two points
        segment = df.iloc[p1_pos : p2_pos + 1]
        if variant == "top":
            neckline = float(segment["low"].min())
            depth_pct = (min(p1_val, p2_val) - neckline) / min(p1_val, p2_val)
            if depth_pct < 0.03:
                continue
            bars_since_p2 = (len(df) - 1) - p2_pos
            if bars_since_p2 > 20:
                continue
            if close >= neckline * 0.998:  # no breakdown yet
                continue
            direction = "bearish"
            ptype     = "double_top"
            pattern_height = max(p1_val, p2_val) - neckline
            target    = neckline - pattern_height
            stop      = max(p1_val, p2_val) * 1.005
        else:
            neckline = float(segment["high"].max())
            rise_pct = (neckline - max(p1_val, p2_val)) / max(p1_val, p2_val)
            if rise_pct < 0.03:
                continue
            bars_since_p2 = (len(df) - 1) - p2_pos
            if bars_since_p2 > 20:
                continue
            if close <= neckline * 1.002:  # no breakout yet
                continue
            direction = "bullish"
            ptype     = "double_bottom"
            pattern_height = neckline - min(p1_val, p2_val)
            target    = neckline + pattern_height
            stop      = min(p1_val, p2_val) * 0.995

        sym_ratio = min(p1_val, p2_val) / max(p1_val, p2_val)
        bars_total = (len(df) - 1) - p1_pos

        comp = {
            "volume_score"      : score_volume(vr),
            "breakout_strength" : score_breakout_strength(close, neckline, direction, atr),
            "candle_body"       : score_candle_body(open_, close, high, low),
            "trend_alignment"   : score_trend_alignment(df, direction),
            "symmetry"          : score_symmetry(sym_ratio),
            "pattern_duration"  : score_pattern_duration(bars_total, 15, 60),
        }
        p = DetectedPattern(
            pattern_type=ptype, direction=direction,
            start_index=p1_idx, end_index=df.index[-1],
            detected_at_bar=df.index[-1], pattern_bars=bars_total,
            key_levels={
                "left_peak": p1_val, "right_peak": p2_val,
                "neckline": neckline,
                "peak2_bar_offset": float(p2_pos - p1_pos),
            },
            entry_price=close, stop_loss=stop, target=target,
            confidence_score=compute_confidence(ptype, comp),
            component_scores=comp, volume_confirmed=vr >= 1.2,
            ticker=ticker, timeframe=timeframe,
        )
        return [compute_risk_reward(p)]

    return []


def detect_double_top(df, ticker="", timeframe="daily"):
    return _detect_double(df, "top", ticker, timeframe)

def detect_double_bottom(df, ticker="", timeframe="daily"):
    return _detect_double(df, "bottom", ticker, timeframe)


# ---------------------------------------------------------------------------
# 10. Head and Shoulders / Inverse H&S
# ---------------------------------------------------------------------------

def _detect_hs(
    df: pd.DataFrame,
    variant: str,  # 'hs' | 'ihs'
    ticker: str,
    timeframe: str,
    lookback: int = 120,
) -> List[DetectedPattern]:
    if len(df) < 30:
        return []

    n   = min(lookback, len(df) - 1)
    win = df.iloc[-n:]

    if variant == "hs":
        peaks = win[win["swing_high"].notna()]
        col   = "swing_high"
    else:
        peaks = win[win["swing_low"].notna()]
        col   = "swing_low"

    if len(peaks) < 3:
        return []

    close, open_, high, low, atr, vr = _current_bar_scalars(df)

    # Scan triplets of peaks
    for i in range(len(peaks) - 2):
        ls_idx  = peaks.index[i]
        hd_idx  = peaks.index[i + 1]
        rs_idx  = peaks.index[i + 2]
        ls_val  = float(peaks[col].iloc[i])
        hd_val  = float(peaks[col].iloc[i + 1])
        rs_val  = float(peaks[col].iloc[i + 2])

        # Head must be the extreme point
        if variant == "hs":
            if not (hd_val > ls_val and hd_val > rs_val):
                continue
        else:
            if not (hd_val < ls_val and hd_val < rs_val):
                continue

        # Shoulder symmetry
        avg_sh = (ls_val + rs_val) / 2
        if avg_sh == 0:
            continue
        shoulder_diff = abs(ls_val - rs_val) / avg_sh
        if shoulder_diff > 0.05:
            continue

        # Head must be sufficiently extreme vs shoulders
        extremeness = abs(hd_val - avg_sh) / avg_sh
        if extremeness < 0.02:
            continue

        try:
            ls_pos = int(df.index.get_loc(ls_idx))
            hd_pos = int(df.index.get_loc(hd_idx))
            rs_pos = int(df.index.get_loc(rs_idx))
        except Exception:
            continue

        bars_total = (len(df) - 1) - ls_pos

        # Time symmetry
        left_bars  = hd_pos - ls_pos
        right_bars = rs_pos - hd_pos
        time_sym   = min(left_bars, right_bars) / max(left_bars, right_bars) if max(left_bars, right_bars) > 0 else 0
        if time_sym < 0.50:
            continue

        # Neckline from the troughs/peaks between the three points
        if variant == "hs":
            t1_val = float(df["low"].iloc[ls_pos : hd_pos + 1].min())
            t2_val = float(df["low"].iloc[hd_pos : rs_pos + 1].min())
        else:
            t1_val = float(df["high"].iloc[ls_pos : hd_pos + 1].max())
            t2_val = float(df["high"].iloc[hd_pos : rs_pos + 1].max())

        # Simple linear neckline using bar positions
        nk_slope    = (t2_val - t1_val) / max(1, hd_pos - ls_pos)
        nk_intercept = t1_val - nk_slope * ls_pos
        nk_at_curr  = nk_slope * (len(df) - 1) + nk_intercept

        # Neckline slope check (≤ 0.3% per bar)
        mid_nk = (t1_val + t2_val) / 2
        if mid_nk > 0 and abs(nk_slope) / mid_nk > 0.003:
            continue

        # Recency check
        bars_since_rs = (len(df) - 1) - rs_pos
        if bars_since_rs > 30:
            continue

        # Breakout check
        if variant == "hs":
            if close >= nk_at_curr * 0.998:
                continue
            direction      = "bearish"
            ptype          = "head_shoulders"
            pattern_height = hd_val - nk_at_curr
            target         = nk_at_curr - pattern_height
            stop           = rs_val * 1.005
        else:
            if close <= nk_at_curr * 1.002:
                continue
            direction      = "bullish"
            ptype          = "inv_head_shoulders"
            pattern_height = nk_at_curr - hd_val
            target         = nk_at_curr + pattern_height
            stop           = rs_val * 0.995

        sym_score = score_symmetry(time_sym)
        comp = {
            "volume_score"      : score_volume(vr),
            "breakout_strength" : score_breakout_strength(close, nk_at_curr, direction, atr),
            "candle_body"       : score_candle_body(open_, close, high, low),
            "trend_alignment"   : score_trend_alignment(df, direction),
            "symmetry"          : sym_score,
            "pattern_duration"  : score_pattern_duration(bars_total, 20, 100),
        }
        p = DetectedPattern(
            pattern_type=ptype, direction=direction,
            start_index=ls_idx, end_index=df.index[-1],
            detected_at_bar=df.index[-1], pattern_bars=bars_total,
            key_levels={
                "left_peak": ls_val, "head": hd_val, "right_peak": rs_val,
                "neckline": nk_at_curr, "trough_1": t1_val, "trough_2": t2_val,
                "head_bar_offset":           float(hd_pos - ls_pos),
                "right_shoulder_bar_offset": float(rs_pos - ls_pos),
            },
            entry_price=close, stop_loss=stop, target=target,
            confidence_score=compute_confidence(ptype, comp),
            component_scores=comp, volume_confirmed=vr >= 1.2,
            ticker=ticker, timeframe=timeframe,
        )
        return [compute_risk_reward(p)]

    return []


def detect_head_and_shoulders(df, ticker="", timeframe="daily"):
    return _detect_hs(df, "hs", ticker, timeframe)

def detect_inv_head_and_shoulders(df, ticker="", timeframe="daily"):
    return _detect_hs(df, "ihs", ticker, timeframe)


# ---------------------------------------------------------------------------
# 11. Cup and Handle
# ---------------------------------------------------------------------------

def detect_cup_handle(
    df: pd.DataFrame,
    ticker: str = "",
    timeframe: str = "daily",
    lookback: int = 150,
    min_cup_bars: int = 30,
    max_handle_depth_pct: float = 0.50,
) -> List[DetectedPattern]:
    if len(df) < min_cup_bars + 10:
        return []

    n     = min(lookback, len(df) - 1)
    win   = df.iloc[-n:]
    close = win["close"].values.astype(float)
    high  = win["high"].values.astype(float)

    # Cup left rim: highest point in first half of window
    half   = n // 2
    l_rim_pos = int(np.argmax(close[:half]))
    l_rim_val = close[l_rim_pos]

    # Cup bottom: lowest point after left rim
    cup_seg   = close[l_rim_pos:]
    if len(cup_seg) < 10:
        return []
    bottom_pos_rel = int(np.argmin(cup_seg))
    bottom_pos     = l_rim_pos + bottom_pos_rel
    bottom_val     = cup_seg[bottom_pos_rel]

    cup_depth = (l_rim_val - bottom_val) / l_rim_val if l_rim_val > 0 else 0
    if cup_depth < 0.10:  # cup must be at least 10% deep
        return []

    # Right rim: recovery back to near left rim level after bottom
    recovery_seg = close[bottom_pos:]
    if len(recovery_seg) < 5:
        return []
    r_rim_pos_rel = int(np.argmax(recovery_seg))
    r_rim_pos     = bottom_pos + r_rim_pos_rel
    r_rim_val     = recovery_seg[r_rim_pos_rel]

    # Right rim should be close to left rim
    rim_diff = abs(r_rim_val - l_rim_val) / l_rim_val if l_rim_val > 0 else 1.0
    if rim_diff > 0.05:
        return []

    cup_bars = r_rim_pos - l_rim_pos
    if cup_bars < min_cup_bars:
        return []

    # Handle: small pullback after right rim
    handle_seg = close[r_rim_pos:]
    if len(handle_seg) < 3:
        return []

    handle_low_val  = float(np.min(handle_seg))
    handle_depth    = (r_rim_val - handle_low_val) / r_rim_val if r_rim_val > 0 else 1.0
    if handle_depth > max_handle_depth_pct * cup_depth:
        return []

    # Cup roundedness: fit a parabola to cup segment (R² from linear is low for a cup)
    cup_x  = np.arange(cup_bars + 1, dtype=float)
    cup_y  = close[l_rim_pos : r_rim_pos + 1]
    if len(cup_x) != len(cup_y):
        cup_x = cup_x[:len(cup_y)]
    _, _, linear_r2 = _fit_line(cup_x, cup_y)
    # A good cup has low linear R² (not a straight line)
    cup_fit_score = score_trendline_fit(max(0, 1.0 - abs(linear_r2)))

    close_last, open_last, high_last, low_last, atr, vr = _current_bar_scalars(df)
    rim_level = (l_rim_val + r_rim_val) / 2

    if close_last <= rim_level * 1.002:
        return []

    bars_total = r_rim_pos - l_rim_pos + len(handle_seg)
    comp = {
        "volume_score"      : score_volume(vr),
        "breakout_strength" : score_breakout_strength(close_last, rim_level, "bullish", atr),
        "candle_body"       : score_candle_body(open_last, close_last, high_last, low_last),
        "trend_alignment"   : score_trend_alignment(df, "bullish"),
        "trendline_fit"     : cup_fit_score,
        "pattern_duration"  : score_pattern_duration(bars_total, 30, 150),
    }
    pattern_height = rim_level - bottom_val
    p = DetectedPattern(
        pattern_type="cup_handle", direction="bullish",
        start_index=win.index[l_rim_pos], end_index=df.index[-1],
        detected_at_bar=df.index[-1], pattern_bars=bars_total,
        key_levels={
            "cup_left_rim": l_rim_val, "cup_right_rim": r_rim_val,
            "cup_bottom": bottom_val, "handle_low": handle_low_val,
            "bottom_bar_offset":    float(bottom_pos - l_rim_pos),
            "right_rim_bar_offset": float(r_rim_pos  - l_rim_pos),
        },
        entry_price=close_last,
        stop_loss=handle_low_val * 0.995,
        target=rim_level + pattern_height,
        confidence_score=compute_confidence("cup_handle", comp),
        component_scores=comp, volume_confirmed=vr >= 1.2,
        ticker=ticker, timeframe=timeframe,
    )
    return [compute_risk_reward(p)]


# ---------------------------------------------------------------------------
# 12. Range Consolidation Breakout
# ---------------------------------------------------------------------------

def detect_range_consolidation_breakout(
    df: pd.DataFrame,
    ticker: str = "",
    timeframe: str = "daily",
    lookback: int = 50,
    min_touches: int = 2,
) -> List[DetectedPattern]:
    if len(df) < lookback + 5:
        return []

    n   = min(lookback, len(df) - 1)
    win = df.iloc[-n:]

    recent_sh = win["swing_high"].dropna()
    recent_sl = win["swing_low"].dropna()

    if len(recent_sh) < min_touches or len(recent_sl) < min_touches:
        return []

    range_high = float(recent_sh.max())
    range_low  = float(recent_sl.min())

    if range_low == 0:
        return []
    range_width_pct = (range_high - range_low) / range_low
    if not (0.03 <= range_width_pct <= 0.20):
        return []

    # All swing highs near range ceiling (within 2%)
    sh_near_ceil = (recent_sh >= range_high * 0.98).sum()
    sl_near_floor = (recent_sl <= range_low * 1.02).sum()
    if sh_near_ceil < min_touches or sl_near_floor < min_touches:
        return []

    close, open_, high, low, atr, vr = _current_bar_scalars(df)
    range_width = range_high - range_low
    vol_avg     = float(win["volume"].mean())
    curr_vol    = float(df["volume"].iloc[-1])

    # Volume should expand on breakout (1.5x average)
    vol_expansion = curr_vol / vol_avg if vol_avg > 0 else 1.0

    patterns: List[DetectedPattern] = []

    if close > range_high * 1.002 and vol_expansion >= 1.3:
        comp = {
            "volume_score"      : score_volume(vr),
            "breakout_strength" : score_breakout_strength(close, range_high, "bullish", atr),
            "candle_body"       : score_candle_body(open_, close, high, low),
            "trend_alignment"   : score_trend_alignment(df, "bullish"),
        }
        p = DetectedPattern(
            pattern_type="range_breakout_bull", direction="bullish",
            start_index=df.index[-n], end_index=df.index[-1],
            detected_at_bar=df.index[-1], pattern_bars=n,
            key_levels={"range_high": range_high, "range_low": range_low, "range_width": range_width},
            entry_price=close,
            stop_loss=range_high - range_width * 0.2,
            target=range_high + range_width,
            confidence_score=compute_confidence("range_breakout_bull", comp),
            component_scores=comp, volume_confirmed=vol_expansion >= 1.5,
            ticker=ticker, timeframe=timeframe,
        )
        patterns.append(compute_risk_reward(p))

    elif close < range_low * 0.998 and vol_expansion >= 1.3:
        comp = {
            "volume_score"      : score_volume(vr),
            "breakout_strength" : score_breakout_strength(close, range_low, "bearish", atr),
            "candle_body"       : score_candle_body(open_, close, high, low),
            "trend_alignment"   : score_trend_alignment(df, "bearish"),
        }
        p = DetectedPattern(
            pattern_type="range_breakout_bear", direction="bearish",
            start_index=df.index[-n], end_index=df.index[-1],
            detected_at_bar=df.index[-1], pattern_bars=n,
            key_levels={"range_high": range_high, "range_low": range_low, "range_width": range_width},
            entry_price=close,
            stop_loss=range_low + range_width * 0.2,
            target=range_low - range_width,
            confidence_score=compute_confidence("range_breakout_bear", comp),
            component_scores=comp, volume_confirmed=vol_expansion >= 1.5,
            ticker=ticker, timeframe=timeframe,
        )
        patterns.append(compute_risk_reward(p))

    return patterns
