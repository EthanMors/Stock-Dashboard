"""
Foundation for pattern detection: DetectedPattern dataclass, preprocessing
pipeline, swing-point detection, and confidence scoring utilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# DetectedPattern dataclass
# ---------------------------------------------------------------------------

@dataclass
class DetectedPattern:
    # ── Identity ─────────────────────────────────────────────────────────────
    pattern_type: str
    """
    One of: 'bos_bullish', 'bos_bearish', 'choch_bullish', 'choch_bearish',
    'breakout_resistance', 'breakout_support',
    'bull_flag', 'bear_flag', 'pennant_bull', 'pennant_bear',
    'asc_triangle', 'desc_triangle', 'sym_triangle',
    'rising_wedge', 'falling_wedge', 'cup_handle',
    'head_shoulders', 'inv_head_shoulders',
    'double_top', 'double_bottom',
    'range_breakout_bull', 'range_breakout_bear'
    """
    direction: str              # 'bullish' or 'bearish'

    # ── Location ─────────────────────────────────────────────────────────────
    start_index: Any
    end_index: Any
    detected_at_bar: Any
    pattern_bars: int

    # ── Key Levels ───────────────────────────────────────────────────────────
    key_levels: Dict[str, float] = field(default_factory=dict)

    # ── Trade Parameters ─────────────────────────────────────────────────────
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    risk_reward_ratio: Optional[float] = None

    # ── Quality Metrics ──────────────────────────────────────────────────────
    confidence_score: float = 0.0
    volume_confirmed: bool = False
    close_confirmed: bool = True
    component_scores: Dict[str, float] = field(default_factory=dict)

    # ── Metadata ─────────────────────────────────────────────────────────────
    timeframe: str = "daily"
    ticker: str = ""
    notes: str = ""


def compute_risk_reward(p: DetectedPattern) -> DetectedPattern:
    if all(x is not None for x in (p.entry_price, p.stop_loss, p.target)):
        risk   = abs(p.entry_price - p.stop_loss)    # type: ignore[operator]
        reward = abs(p.target      - p.entry_price)  # type: ignore[operator]
        if risk > 0:
            p.risk_reward_ratio = round(reward / risk, 2)
    return p


# ---------------------------------------------------------------------------
# Swing-point detection
# ---------------------------------------------------------------------------

def find_swing_highs_lows(df: pd.DataFrame, n: int = 5) -> pd.DataFrame:
    """Mark swing highs/lows using an n-bar left/right lookback."""
    df = df.copy()
    highs  = df["high"].values
    lows   = df["low"].values
    length = len(df)
    sh     = np.full(length, np.nan)
    sl     = np.full(length, np.nan)

    for i in range(n, length - n):
        if highs[i] >= np.max(highs[i - n : i + n + 1]):
            sh[i] = highs[i]
        if lows[i] <= np.min(lows[i - n : i + n + 1]):
            sl[i] = lows[i]

    df["swing_high"] = sh
    df["swing_low"]  = sl
    return df


def filter_significant_swings(df: pd.DataFrame, min_pct: float = 0.005) -> pd.DataFrame:
    """Remove swing points whose price differs by less than min_pct from a neighbor."""
    df = df.copy()

    sh_idx = df.index[df["swing_high"].notna()].tolist()
    to_clear: set = set()
    for i in range(1, len(sh_idx)):
        pv = float(df.loc[sh_idx[i - 1], "swing_high"])
        cv = float(df.loc[sh_idx[i],     "swing_high"])
        if pv > 0 and abs(cv - pv) / pv < min_pct:
            # Keep the higher one
            to_clear.add(sh_idx[i] if pv >= cv else sh_idx[i - 1])
    for idx in to_clear:
        df.loc[idx, "swing_high"] = np.nan

    sl_idx = df.index[df["swing_low"].notna()].tolist()
    to_clear = set()
    for i in range(1, len(sl_idx)):
        pv = float(df.loc[sl_idx[i - 1], "swing_low"])
        cv = float(df.loc[sl_idx[i],     "swing_low"])
        if pv > 0 and abs(cv - pv) / pv < min_pct:
            # Keep the lower one
            to_clear.add(sl_idx[i - 1] if pv <= cv else sl_idx[i])
    for idx in to_clear:
        df.loc[idx, "swing_low"] = np.nan

    return df


# ---------------------------------------------------------------------------
# Preprocessing pipeline
# ---------------------------------------------------------------------------

def preprocess(df: pd.DataFrame, swing_n: int = 5) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    df.sort_index(inplace=True)

    # ATR (14-period EMA of true range)
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=14, adjust=False).mean()

    # EMAs
    for span in (20, 50, 200):
        df[f"ema_{span}"] = df["close"].ewm(span=span, adjust=False).mean()

    # Volume ratio
    vol_sma = df["volume"].rolling(20).mean()
    df["volume_sma_20"] = vol_sma
    df["volume_ratio"]  = df["volume"] / vol_sma.replace(0, np.nan)

    # Swing points
    df = find_swing_highs_lows(df, n=swing_n)
    df = filter_significant_swings(df, min_pct=0.005)
    return df


# ---------------------------------------------------------------------------
# S/R level clustering
# ---------------------------------------------------------------------------

def cluster_sr_levels(
    df: pd.DataFrame,
    tol: float = 0.015,
    min_touches: int = 2,
) -> List[dict]:
    sh = df["swing_high"].dropna().values
    sl = df["swing_low"].dropna().values
    prices = np.sort(np.concatenate([sh, sl]))
    if len(prices) < min_touches:
        return []

    clusters: List[List[float]] = []
    cur: List[float] = [float(prices[0])]
    for p in prices[1:]:
        ref = float(np.mean(cur))
        if ref > 0 and abs(float(p) - ref) / ref < tol:
            cur.append(float(p))
        else:
            if len(cur) >= min_touches:
                clusters.append(cur)
            cur = [float(p)]
    if len(cur) >= min_touches:
        clusters.append(cur)

    return sorted(
        [{"mid": float(np.mean(c)), "touches": len(c)} for c in clusters],
        key=lambda x: x["touches"],
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Confidence scoring helpers
# ---------------------------------------------------------------------------

def score_volume(vr: float) -> float:
    if vr >= 3.0: return 1.0
    if vr >= 2.0: return 0.9
    if vr >= 1.5: return 0.75
    if vr >= 1.2: return 0.6
    if vr >= 1.0: return 0.4
    return 0.2


def score_breakout_strength(close: float, level: float, direction: str, atr: float) -> float:
    if atr <= 0:
        return 0.5
    dist = (close - level) / atr if direction == "bullish" else (level - close) / atr
    if dist >= 1.0: return 1.0
    if dist >= 0.5: return 0.8
    if dist >= 0.2: return 0.6
    if dist >= 0.1: return 0.4
    return 0.2


def score_candle_body(o: float, c: float, h: float, l: float) -> float:
    rng = h - l
    if rng == 0:
        return 0.5
    ratio = abs(c - o) / rng
    if ratio >= 0.80: return 1.0
    if ratio >= 0.60: return 0.8
    if ratio >= 0.40: return 0.6
    if ratio >= 0.20: return 0.4
    return 0.2


def score_trend_alignment(df: pd.DataFrame, direction: str) -> float:
    c    = float(df["close"].iloc[-1])
    e50  = float(df["ema_50"].iloc[-1])
    e200 = float(df["ema_200"].iloc[-1])
    if direction == "bullish":
        return (0.4 if c > e50 else 0.0) + (0.4 if e50 > e200 else 0.0) + (0.2 if c > e200 else 0.0)
    return (0.4 if c < e50 else 0.0) + (0.4 if e50 < e200 else 0.0) + (0.2 if c < e200 else 0.0)


def score_trendline_fit(r2: float) -> float:
    if r2 >= 0.95: return 1.0
    if r2 >= 0.85: return 0.85
    if r2 >= 0.75: return 0.70
    if r2 >= 0.65: return 0.55
    return 0.30


def score_symmetry(ratio: float) -> float:
    if ratio >= 0.90: return 1.0
    if ratio >= 0.75: return 0.8
    if ratio >= 0.60: return 0.6
    if ratio >= 0.50: return 0.4
    return 0.2


def score_pattern_duration(bars: int, lo: int, hi: int) -> float:
    if lo <= bars <= hi:
        mid = (lo + hi) / 2
        return max(0.5, 1.0 - abs(bars - mid) / max(1, hi - lo))
    if bars < lo:
        return max(0.1, (bars / lo) * 0.5)
    return max(0.1, (hi / bars) * 0.6)


# ---------------------------------------------------------------------------
# Confidence weights & composite scorer
# ---------------------------------------------------------------------------

CONFIDENCE_WEIGHTS: Dict[str, Dict[str, float]] = {
    "bos_bullish":   {"volume_score": 0.25, "breakout_strength": 0.30, "candle_body": 0.20, "trend_alignment": 0.25},
    "bos_bearish":   {"volume_score": 0.25, "breakout_strength": 0.30, "candle_body": 0.20, "trend_alignment": 0.25},
    "choch_bullish": {"volume_score": 0.25, "breakout_strength": 0.30, "candle_body": 0.20, "trend_alignment": 0.25},
    "choch_bearish": {"volume_score": 0.25, "breakout_strength": 0.30, "candle_body": 0.20, "trend_alignment": 0.25},
    "bull_flag":     {"volume_score": 0.25, "breakout_strength": 0.20, "candle_body": 0.15, "trend_alignment": 0.15, "trendline_fit": 0.25},
    "bear_flag":     {"volume_score": 0.25, "breakout_strength": 0.20, "candle_body": 0.15, "trend_alignment": 0.15, "trendline_fit": 0.25},
    "pennant_bull":  {"volume_score": 0.25, "breakout_strength": 0.20, "candle_body": 0.15, "trend_alignment": 0.15, "trendline_fit": 0.25},
    "pennant_bear":  {"volume_score": 0.25, "breakout_strength": 0.20, "candle_body": 0.15, "trend_alignment": 0.15, "trendline_fit": 0.25},
    "asc_triangle":  {"volume_score": 0.20, "breakout_strength": 0.20, "candle_body": 0.10, "trend_alignment": 0.15, "trendline_fit": 0.20, "pattern_duration": 0.15},
    "desc_triangle": {"volume_score": 0.20, "breakout_strength": 0.20, "candle_body": 0.10, "trend_alignment": 0.15, "trendline_fit": 0.20, "pattern_duration": 0.15},
    "sym_triangle":  {"volume_score": 0.20, "breakout_strength": 0.20, "candle_body": 0.10, "trend_alignment": 0.15, "trendline_fit": 0.20, "pattern_duration": 0.15},
    "rising_wedge":  {"volume_score": 0.25, "breakout_strength": 0.25, "candle_body": 0.20, "trend_alignment": 0.15, "trendline_fit": 0.15},
    "falling_wedge": {"volume_score": 0.25, "breakout_strength": 0.25, "candle_body": 0.20, "trend_alignment": 0.15, "trendline_fit": 0.15},
    "head_shoulders":     {"volume_score": 0.20, "breakout_strength": 0.20, "candle_body": 0.15, "trend_alignment": 0.10, "symmetry": 0.20, "pattern_duration": 0.15},
    "inv_head_shoulders": {"volume_score": 0.20, "breakout_strength": 0.20, "candle_body": 0.15, "trend_alignment": 0.10, "symmetry": 0.20, "pattern_duration": 0.15},
    "double_top":    {"volume_score": 0.20, "breakout_strength": 0.20, "candle_body": 0.15, "trend_alignment": 0.10, "symmetry": 0.25, "pattern_duration": 0.10},
    "double_bottom": {"volume_score": 0.20, "breakout_strength": 0.20, "candle_body": 0.15, "trend_alignment": 0.10, "symmetry": 0.25, "pattern_duration": 0.10},
    "cup_handle":    {"volume_score": 0.25, "breakout_strength": 0.20, "candle_body": 0.10, "trend_alignment": 0.15, "trendline_fit": 0.15, "pattern_duration": 0.15},
    "range_breakout_bull": {"volume_score": 0.30, "breakout_strength": 0.25, "candle_body": 0.20, "trend_alignment": 0.25},
    "range_breakout_bear": {"volume_score": 0.30, "breakout_strength": 0.25, "candle_body": 0.20, "trend_alignment": 0.25},
    "breakout_resistance": {"volume_score": 0.30, "breakout_strength": 0.25, "candle_body": 0.20, "trend_alignment": 0.25},
    "breakout_support":    {"volume_score": 0.30, "breakout_strength": 0.25, "candle_body": 0.20, "trend_alignment": 0.25},
    "default": {"volume_score": 0.25, "breakout_strength": 0.25, "candle_body": 0.20, "trend_alignment": 0.30},
}


def compute_confidence(ptype: str, scores: dict) -> float:
    weights = CONFIDENCE_WEIGHTS.get(ptype, CONFIDENCE_WEIGHTS["default"])
    num = sum(scores.get(k, 0.0) * w for k, w in weights.items())
    den = sum(weights[k] for k in weights if k in scores)
    return round(num / den, 3) if den > 0 else 0.0


# ---------------------------------------------------------------------------
# Confluence bonus
# ---------------------------------------------------------------------------

def apply_confluence_bonus(
    p: DetectedPattern,
    df: pd.DataFrame,
    sr_levels: List[dict],
    fvgs: List[dict],
    order_blocks: List[dict],
) -> DetectedPattern:
    bonus = 0.0
    notes: List[str] = []
    entry = p.entry_price or float(df["close"].iloc[-1])
    ema50 = float(df["ema_50"].iloc[-1])

    if p.direction == "bullish" and entry > ema50:
        bonus += 0.03
        notes.append("Above EMA50")
    elif p.direction == "bearish" and entry < ema50:
        bonus += 0.03
        notes.append("Below EMA50")

    for zone in sr_levels[:5]:
        if entry > 0 and abs(entry - zone["mid"]) / entry < 0.01 and zone["touches"] >= 3:
            bonus += 0.05
            notes.append(f"Near S/R {zone['mid']:.2f}")
            break

    for fvg in fvgs:
        if not fvg.get("filled", True) and fvg.get("direction") == p.direction:
            lo, hi = fvg.get("fvg_low", 0.0), fvg.get("fvg_high", 0.0)
            if lo <= entry <= hi:
                bonus += 0.04
                notes.append(f"{p.direction.capitalize()} FVG")
                break

    for ob in order_blocks:
        if not ob.get("broken", True) and ob.get("direction") == p.direction:
            lo, hi = ob.get("ob_low", 0.0), ob.get("ob_high", 0.0)
            if lo <= entry <= hi:
                bonus += 0.05
                notes.append("Order block")
                break

    if notes:
        p.confidence_score = min(1.0, p.confidence_score + bonus)
        sep = " | " if p.notes else ""
        p.notes = p.notes + sep + " | ".join(notes)

    return p
