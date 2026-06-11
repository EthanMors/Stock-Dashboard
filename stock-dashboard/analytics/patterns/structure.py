"""
Market structure detection: BOS, CHoCH, Order Blocks, Fair Value Gaps.
"""

from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd

from .helpers import (
    DetectedPattern,
    compute_confidence,
    compute_risk_reward,
    score_breakout_strength,
    score_candle_body,
    score_trend_alignment,
    score_volume,
)


# ---------------------------------------------------------------------------
# BOS / CHoCH
# ---------------------------------------------------------------------------

def detect_bos_choch(
    df: pd.DataFrame,
    lookback: int = 100,
    ticker: str = "",
    timeframe: str = "daily",
    max_lag: int = 10,
) -> List[DetectedPattern]:
    """Detect BOS/CHoCH by scanning the most recent max_lag bars for breakouts."""
    if len(df) < 20:
        return []

    # Scan each of the last max_lag bars as the "current" bar, return first hit
    for lag in range(min(max_lag, len(df) - 20)):
        scan_df = df.iloc[: len(df) - lag] if lag > 0 else df
        result  = _bos_choch_at(scan_df, lookback, ticker, timeframe)
        if result:
            return result
    return []


def _bos_choch_at(
    df: pd.DataFrame,
    lookback: int,
    ticker: str,
    timeframe: str,
) -> List[DetectedPattern]:
    """Check whether the last bar of df is a BOS or CHoCH event."""
    patterns: List[DetectedPattern] = []
    n = min(lookback, len(df) - 1)
    window = df.iloc[-n:]

    sh_bars = window[window["swing_high"].notna()]
    sl_bars = window[window["swing_low"].notna()]

    close = float(df["close"].iloc[-1])
    open_ = float(df["open"].iloc[-1])
    high  = float(df["high"].iloc[-1])
    low   = float(df["low"].iloc[-1])
    atr   = float(df["atr"].iloc[-1]) if "atr" in df.columns else close * 0.01
    vr    = float(df["volume_ratio"].iloc[-1]) if "volume_ratio" in df.columns else 1.0

    # ── Bullish: close breaks above most-recent swing high ──────────────────
    if len(sh_bars) >= 2:
        rsh      = sh_bars.iloc[-1]
        psh      = sh_bars.iloc[-2]
        sh_price = float(rsh["swing_high"])
        sh_idx   = rsh.name

        if close > sh_price * 1.002:
            # CHoCH if the broken swing high was lower than the one before it
            is_choch = sh_price < float(psh["swing_high"])
            ptype    = "choch_bullish" if is_choch else "bos_bullish"

            try:
                sh_pos = int(df.index.get_loc(sh_idx))
            except Exception:
                sh_pos = max(0, len(df) - 20)
            bars_n = (len(df) - 1) - sh_pos

            comp = {
                "volume_score"      : score_volume(vr),
                "breakout_strength" : score_breakout_strength(close, sh_price, "bullish", atr),
                "candle_body"       : score_candle_body(open_, close, high, low),
                "trend_alignment"   : score_trend_alignment(df, "bullish"),
            }
            p = DetectedPattern(
                pattern_type=ptype, direction="bullish",
                start_index=sh_idx, end_index=df.index[-1],
                detected_at_bar=df.index[-1], pattern_bars=bars_n,
                key_levels={"trigger_level": sh_price, "broken_swing": sh_price},
                entry_price=close, stop_loss=low * 0.995,
                target=close + 2.0 * atr,
                confidence_score=compute_confidence(ptype, comp),
                component_scores=comp, volume_confirmed=vr >= 1.2,
                ticker=ticker, timeframe=timeframe,
            )
            patterns.append(compute_risk_reward(p))

    # ── Bearish: close breaks below most-recent swing low ───────────────────
    if len(sl_bars) >= 2:
        rsl      = sl_bars.iloc[-1]
        psl      = sl_bars.iloc[-2]
        sl_price = float(rsl["swing_low"])
        sl_idx   = rsl.name

        if close < sl_price * 0.998:
            is_choch = sl_price > float(psl["swing_low"])
            ptype    = "choch_bearish" if is_choch else "bos_bearish"

            try:
                sl_pos = int(df.index.get_loc(sl_idx))
            except Exception:
                sl_pos = max(0, len(df) - 20)
            bars_n = (len(df) - 1) - sl_pos

            comp = {
                "volume_score"      : score_volume(vr),
                "breakout_strength" : score_breakout_strength(close, sl_price, "bearish", atr),
                "candle_body"       : score_candle_body(open_, close, high, low),
                "trend_alignment"   : score_trend_alignment(df, "bearish"),
            }
            p = DetectedPattern(
                pattern_type=ptype, direction="bearish",
                start_index=sl_idx, end_index=df.index[-1],
                detected_at_bar=df.index[-1], pattern_bars=bars_n,
                key_levels={"trigger_level": sl_price, "broken_swing": sl_price},
                entry_price=close, stop_loss=high * 1.005,
                target=close - 2.0 * atr,
                confidence_score=compute_confidence(ptype, comp),
                component_scores=comp, volume_confirmed=vr >= 1.2,
                ticker=ticker, timeframe=timeframe,
            )
            patterns.append(compute_risk_reward(p))

    return patterns


# ---------------------------------------------------------------------------
# Order Blocks
# ---------------------------------------------------------------------------

def detect_order_blocks(df: pd.DataFrame, lookback: int = 50) -> List[dict]:
    """Identify order blocks (last opposing candle before a strong directional move)."""
    obs: List[dict] = []
    if len(df) < lookback + 4:
        return obs

    start = max(0, len(df) - lookback)
    for i in range(start, len(df) - 3):
        o   = float(df["open"].iloc[i])
        c   = float(df["close"].iloc[i])
        sub = df["close"].iloc[i + 1 : i + 4].values.astype(float)
        if len(sub) < 3:
            continue

        if c < o:  # bearish candle → potential bullish OB
            if np.all(np.diff(sub) > 0) and sub[-1] > 0:
                move = (sub[-1] - sub[0]) / sub[0]
                if move > 0.02:
                    obs.append({
                        "direction": "bullish",
                        "ob_low": min(o, c), "ob_high": max(o, c),
                        "bar_idx": df.index[i],
                        "broken": float(df["close"].iloc[-1]) < min(o, c),
                    })
        elif c > o:  # bullish candle → potential bearish OB
            if np.all(np.diff(sub) < 0) and sub[0] > 0:
                move = (sub[0] - sub[-1]) / sub[0]
                if move > 0.02:
                    obs.append({
                        "direction": "bearish",
                        "ob_low": min(o, c), "ob_high": max(o, c),
                        "bar_idx": df.index[i],
                        "broken": float(df["close"].iloc[-1]) > max(o, c),
                    })

    return obs[-10:]


# ---------------------------------------------------------------------------
# Fair Value Gaps
# ---------------------------------------------------------------------------

def detect_fvgs(df: pd.DataFrame, lookback: int = 50) -> List[dict]:
    """Detect 3-candle imbalances (fair value gaps)."""
    fvgs: List[dict] = []
    if len(df) < 3:
        return fvgs

    start = max(2, len(df) - lookback)
    for i in range(start, len(df)):
        h2 = float(df["high"].iloc[i - 2])
        l2 = float(df["low"].iloc[i - 2])
        h0 = float(df["high"].iloc[i])
        l0 = float(df["low"].iloc[i])

        if l0 > h2:  # bullish FVG: gap up
            filled = bool(df["low"].iloc[i:].min() <= h2) if i < len(df) - 1 else False
            fvgs.append({
                "direction": "bullish", "fvg_low": h2, "fvg_high": l0,
                "bar_idx": df.index[i], "filled": filled,
            })

        if h0 < l2:  # bearish FVG: gap down
            filled = bool(df["high"].iloc[i:].max() >= l2) if i < len(df) - 1 else False
            fvgs.append({
                "direction": "bearish", "fvg_low": h0, "fvg_high": l2,
                "bar_idx": df.index[i], "filled": filled,
            })

    return fvgs[-20:]
