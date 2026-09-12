"""
data/screener_ta.py
Technical analysis enrichment for the Speculative Screener.

Fetches 6 months of daily OHLCV via yfinance, computes RSI(14), SMA20, SMA50,
MACD(12/26/9), and Bollinger Bands(20, 2σ) using pandas/numpy only.
Derives a deterministic TA Signal (Bullish/Neutral/Bearish) and a list of
human-readable signal strings.
Results are cached in db/screener.db (screener_ta table, 1-hour TTL).
Parallel enrichment via ThreadPoolExecutor mirrors enrich_with_fundamentals.
"""

import concurrent.futures
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------------------
# DB setup — shares screener.db with data/screener.py
# ---------------------------------------------------------------------------

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "screener.db")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "screener_ta_schema.sql")

# TTL for TA cache (same as screener results cache)
_TA_CACHE_TTL_HOURS = 1

# How many days back to look for golden/death cross detection
_CROSS_LOOKBACK_DAYS = 10


def _get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Read and execute the TA schema DDL. Idempotent — safe to call on every import."""
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        ddl = fh.read()
    conn = _get_connection()
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()


init_db()  # run at import time

# ---------------------------------------------------------------------------
# SQLite cache helpers
# ---------------------------------------------------------------------------

def _save_ta(ticker: str, ta_data: dict) -> None:
    """Persist a TA result dict for ticker to the screener_ta table."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO screener_ta (ticker, ta_json, fetched_at) VALUES (?, ?, ?)",
            (ticker.upper(), json.dumps(ta_data), now_iso),
        )
        conn.commit()
    finally:
        conn.close()


def _load_ta(ticker: str) -> Optional[dict]:
    """
    Load cached TA data for ticker if within _TA_CACHE_TTL_HOURS.
    Returns None if no row exists or the row is stale.
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT ta_json, fetched_at
            FROM screener_ta
            WHERE ticker = ?
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            (ticker.upper(),),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    data = dict(row)
    try:
        fetched_at = datetime.fromisoformat(data["fetched_at"].replace("Z", "+00:00"))
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

    if datetime.now(timezone.utc) - fetched_at > timedelta(hours=_TA_CACHE_TTL_HOURS):
        return None

    try:
        return json.loads(data["ta_json"])
    except (json.JSONDecodeError, TypeError):
        return None

# ---------------------------------------------------------------------------
# Indicator calculations (pandas/numpy only — no ta-lib or pandas_ta)
# ---------------------------------------------------------------------------

def _calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """
    Compute RSI using Wilder's smoothing (EWM with alpha = 1/period).
    Returns a Series of the same length as close; first (period) values will be NaN.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100.0 - (100.0 / (1.0 + rs))


def _calc_macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Compute MACD line, signal line, and histogram.
    Returns (macd_line, signal_line, histogram) as three pd.Series of the same length as close.
    """
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _calc_bollinger(
    close: pd.Series,
    period: int = 20,
    std_mult: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Compute Bollinger Bands. Returns (upper, mid, lower) as three pd.Series.
    """
    sma = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    return sma + std_mult * std, sma, sma - std_mult * std

# ---------------------------------------------------------------------------
# Signal derivation
# ---------------------------------------------------------------------------

def _derive_signals(
    close: pd.Series,
    rsi: pd.Series,
    sma20: pd.Series,
    sma50: pd.Series,
    macd_line: pd.Series,
    signal_line: pd.Series,
    bb_upper: pd.Series,
    bb_lower: pd.Series,
    volume: pd.Series,
) -> tuple[str, list[str], float, str, str]:
    """
    Derive a TA Signal (Bullish/Neutral/Bearish) and a list of human-readable
    signal strings from the computed indicators.

    Parameters
    ----------
    close, rsi, sma20, sma50, macd_line, signal_line, bb_upper, bb_lower, volume:
        pd.Series — indicator series, all aligned to the same date index.

    Returns
    -------
    (ta_signal, signal_list, rsi_value, trend_vs_sma50, price_vs_bb)

    ta_signal     : str — "Bullish", "Neutral", or "Bearish"
    signal_list   : list[str] — human-readable detected signal strings (1–6 items)
    rsi_value     : float — most recent RSI value, or NaN if not computable
    trend_vs_sma50: str — "Above SMA50", "Below SMA50", or "N/A"
    price_vs_bb   : str — "Near Upper Band", "Near Lower Band", or "Mid Range"
    """
    signal_list: list[str] = []
    bullish_count = 0
    bearish_count = 0

    if close.empty or len(close) < 2:
        return "Neutral", [], float("nan"), "N/A", "N/A"

    current_price = float(close.iloc[-1])

    # --- RSI ---
    rsi_val = float("nan")
    if not rsi.empty and not pd.isna(rsi.iloc[-1]):
        rsi_val = float(rsi.iloc[-1])
        if rsi_val <= 30:
            signal_list.append(f"RSI {rsi_val:.0f} — oversold")
            bullish_count += 1
        elif rsi_val >= 70:
            signal_list.append(f"RSI {rsi_val:.0f} — overbought")
            bearish_count += 1
        else:
            signal_list.append(f"RSI {rsi_val:.0f} — neutral")

    # --- Price vs SMA50 ---
    trend_vs_sma50 = "N/A"
    if not sma50.empty and not pd.isna(sma50.iloc[-1]):
        sma50_val = float(sma50.iloc[-1])
        if current_price > sma50_val:
            trend_vs_sma50 = "Above SMA50"
            signal_list.append(f"Price above SMA50 (${sma50_val:.2f})")
            bullish_count += 1
        else:
            trend_vs_sma50 = "Below SMA50"
            signal_list.append(f"Price below SMA50 (${sma50_val:.2f})")
            bearish_count += 1

    # --- Golden / Death Cross (SMA20 crosses SMA50 within last _CROSS_LOOKBACK_DAYS bars) ---
    if (
        not sma20.empty
        and not sma50.empty
        and len(sma20.dropna()) >= _CROSS_LOOKBACK_DAYS
        and len(sma50.dropna()) >= _CROSS_LOOKBACK_DAYS
    ):
        lookback = min(_CROSS_LOOKBACK_DAYS, len(sma20) - 1)
        sma20_recent = sma20.iloc[-lookback:]
        sma50_recent = sma50.iloc[-lookback:]
        diff = sma20_recent - sma50_recent
        cross_days: Optional[int] = None

        # Walk backwards to find the most recent sign change
        diff_vals = diff.values
        for i in range(len(diff_vals) - 1, 0, -1):
            if diff_vals[i] > 0 and diff_vals[i - 1] <= 0:
                cross_days = len(diff_vals) - 1 - i
                signal_list.append(
                    f"Golden cross {cross_days} day(s) ago (SMA20 crossed above SMA50)"
                )
                bullish_count += 2
                break
            elif diff_vals[i] < 0 and diff_vals[i - 1] >= 0:
                cross_days = len(diff_vals) - 1 - i
                signal_list.append(
                    f"Death cross {cross_days} day(s) ago (SMA20 crossed below SMA50)"
                )
                bearish_count += 2
                break

    # --- MACD crossover (last 5 bars) ---
    if (
        not macd_line.empty
        and not signal_line.empty
        and not pd.isna(macd_line.iloc[-1])
        and not pd.isna(signal_line.iloc[-1])
    ):
        macd_diff = (macd_line - signal_line).iloc[-5:]
        if len(macd_diff) >= 2:
            recent = macd_diff.values
            for i in range(len(recent) - 1, 0, -1):
                if recent[i] > 0 and recent[i - 1] <= 0:
                    signal_list.append("MACD bullish crossover (last 5 bars)")
                    bullish_count += 1
                    break
                elif recent[i] < 0 and recent[i - 1] >= 0:
                    signal_list.append("MACD bearish crossover (last 5 bars)")
                    bearish_count += 1
                    break

    # --- Bollinger Band position ---
    price_vs_bb = "Mid Range"
    if (
        not bb_upper.empty
        and not bb_lower.empty
        and not pd.isna(bb_upper.iloc[-1])
        and not pd.isna(bb_lower.iloc[-1])
    ):
        upper_val = float(bb_upper.iloc[-1])
        lower_val = float(bb_lower.iloc[-1])
        band_width = upper_val - lower_val
        if band_width > 0:
            pct_b = (current_price - lower_val) / band_width
            if pct_b >= 0.9:
                price_vs_bb = "Near Upper Band"
                signal_list.append("Near upper Bollinger Band")
                bearish_count += 1
            elif pct_b <= 0.1:
                price_vs_bb = "Near Lower Band"
                signal_list.append("Near lower Bollinger Band")
                bullish_count += 1

    # --- Volume trend (10-bar vs 20-bar average) ---
    if not volume.empty and len(volume.dropna()) >= 20:
        vol_10 = float(volume.iloc[-10:].mean())
        vol_20 = float(volume.iloc[-20:].mean())
        if vol_20 > 0:
            vol_ratio = vol_10 / vol_20
            if vol_ratio >= 1.5:
                signal_list.append(
                    f"Volume elevated ({vol_ratio:.1f}x 20-day avg)"
                )
                bullish_count += 1

    # --- Compute overall TA signal ---
    if bullish_count >= bearish_count + 2:
        ta_signal = "Bullish"
    elif bearish_count >= bullish_count + 2:
        ta_signal = "Bearish"
    else:
        ta_signal = "Neutral"

    return ta_signal, signal_list, rsi_val, trend_vs_sma50, price_vs_bb

# ---------------------------------------------------------------------------
# Core per-ticker fetch-and-compute
# ---------------------------------------------------------------------------

def _fetch_single_ta(ticker: str) -> tuple[str, dict]:
    """
    Fetch 6 months of daily OHLCV for ticker, compute all indicators, derive
    signals, and cache the result in SQLite.

    Returns (ticker_upper, ta_dict) where ta_dict has the following keys:
        rsi             : float | None  — latest RSI(14) value
        sma20           : float | None  — latest SMA20 value
        sma50           : float | None  — latest SMA50 value
        macd_line       : float | None  — latest MACD line value
        macd_signal     : float | None  — latest MACD signal line value
        macd_histogram  : float | None  — latest MACD histogram value
        bb_upper        : float | None  — latest Bollinger upper band
        bb_mid          : float | None  — latest Bollinger mid (SMA20)
        bb_lower        : float | None  — latest Bollinger lower band
        ta_signal       : str           — "Bullish", "Neutral", or "Bearish"
        signal_list     : list[str]     — human-readable signal strings
        trend_vs_sma50  : str           — "Above SMA50", "Below SMA50", or "N/A"
        price_vs_bb     : str           — "Near Upper Band", "Near Lower Band", "Mid Range"
        ohlcv_dates     : list[str]     — ISO date strings for the OHLCV series
        ohlcv_open      : list[float]
        ohlcv_high      : list[float]
        ohlcv_low       : list[float]
        ohlcv_close     : list[float]
        ohlcv_volume    : list[float]
        rsi_series      : list[float | None]
        macd_line_series: list[float | None]
        macd_sig_series : list[float | None]
        macd_hist_series: list[float | None]
        sma20_series    : list[float | None]
        sma50_series    : list[float | None]
        bb_upper_series : list[float | None]
        bb_mid_series   : list[float | None]
        bb_lower_series : list[float | None]
        fetch_ok        : bool

    On any error, returns an all-None/empty dict with fetch_ok=False. Never raises.
    """
    ticker_upper = ticker.upper()

    # Cache check
    cached = _load_ta(ticker_upper)
    if cached is not None:
        return ticker_upper, cached

    empty: dict = {
        "rsi": None, "sma20": None, "sma50": None,
        "macd_line": None, "macd_signal": None, "macd_histogram": None,
        "bb_upper": None, "bb_mid": None, "bb_lower": None,
        "ta_signal": "Neutral", "signal_list": [],
        "trend_vs_sma50": "N/A", "price_vs_bb": "Mid Range",
        "ohlcv_dates": [], "ohlcv_open": [], "ohlcv_high": [],
        "ohlcv_low": [], "ohlcv_close": [], "ohlcv_volume": [],
        "rsi_series": [], "macd_line_series": [], "macd_sig_series": [],
        "macd_hist_series": [], "sma20_series": [], "sma50_series": [],
        "bb_upper_series": [], "bb_mid_series": [], "bb_lower_series": [],
        "fetch_ok": False,
    }

    try:
        hist = yf.Ticker(ticker_upper).history(period="6mo", interval="1d", auto_adjust=True)
    except Exception:
        return ticker_upper, empty

    if hist is None or hist.empty or len(hist) < 26:
        return ticker_upper, empty

    close = hist["Close"].astype(float)
    volume = hist["Volume"].astype(float)

    try:
        rsi_series = _calc_rsi(close, period=14)
        sma20_series = close.rolling(window=20).mean()
        sma50_series = close.rolling(window=50).mean()
        macd_line_series, macd_signal_series, macd_hist_series = _calc_macd(close)
        bb_upper_series, bb_mid_series, bb_lower_series = _calc_bollinger(close)
    except Exception:
        return ticker_upper, empty

    def _last(s: pd.Series) -> Optional[float]:
        try:
            v = s.iloc[-1]
            return None if pd.isna(v) else float(v)
        except Exception:
            return None

    def _series_to_list(s: pd.Series) -> list:
        return [None if pd.isna(v) else float(v) for v in s.tolist()]

    ta_signal, signal_list, rsi_val, trend_vs_sma50, price_vs_bb = _derive_signals(
        close, rsi_series, sma20_series, sma50_series,
        macd_line_series, macd_signal_series,
        bb_upper_series, bb_lower_series, volume,
    )

    result: dict = {
        "rsi":            rsi_val if not (isinstance(rsi_val, float) and pd.isna(rsi_val)) else None,
        "sma20":          _last(sma20_series),
        "sma50":          _last(sma50_series),
        "macd_line":      _last(macd_line_series),
        "macd_signal":    _last(macd_signal_series),
        "macd_histogram": _last(macd_hist_series),
        "bb_upper":       _last(bb_upper_series),
        "bb_mid":         _last(bb_mid_series),
        "bb_lower":       _last(bb_lower_series),
        "ta_signal":      ta_signal,
        "signal_list":    signal_list,
        "trend_vs_sma50": trend_vs_sma50,
        "price_vs_bb":    price_vs_bb,
        # OHLCV series for chart rendering (stored as lists of scalars)
        "ohlcv_dates":    [str(d.date()) if hasattr(d, "date") else str(d) for d in hist.index],
        "ohlcv_open":     hist["Open"].astype(float).tolist(),
        "ohlcv_high":     hist["High"].astype(float).tolist(),
        "ohlcv_low":      hist["Low"].astype(float).tolist(),
        "ohlcv_close":    close.tolist(),
        "ohlcv_volume":   volume.tolist(),
        # Indicator series for chart rendering
        "rsi_series":       _series_to_list(rsi_series),
        "macd_line_series": _series_to_list(macd_line_series),
        "macd_sig_series":  _series_to_list(macd_signal_series),
        "macd_hist_series": _series_to_list(macd_hist_series),
        "sma20_series":     _series_to_list(sma20_series),
        "sma50_series":     _series_to_list(sma50_series),
        "bb_upper_series":  _series_to_list(bb_upper_series),
        "bb_mid_series":    _series_to_list(bb_mid_series),
        "bb_lower_series":  _series_to_list(bb_lower_series),
        "fetch_ok": True,
    }

    _save_ta(ticker_upper, result)
    return ticker_upper, result


def fetch_ta_for_tickers(tickers: list[str], max_workers: int = 6) -> dict[str, dict]:
    """
    Fetch and compute TA for a list of tickers in parallel using ThreadPoolExecutor.

    Parameters
    ----------
    tickers     : list[str] — list of ticker symbols (case-insensitive)
    max_workers : int — number of parallel threads (default 6; lower than fundamentals
                  to avoid yfinance rate limits on OHLCV requests)

    Returns
    -------
    dict mapping ticker (uppercase str) → ta dict as returned by _fetch_single_ta.
    Tickers that fail return an all-None/empty dict with fetch_ok=False.
    """
    results: dict[str, dict] = {}
    if not tickers:
        return results

    empty_template: dict = {
        "rsi": None, "sma20": None, "sma50": None,
        "macd_line": None, "macd_signal": None, "macd_histogram": None,
        "bb_upper": None, "bb_mid": None, "bb_lower": None,
        "ta_signal": "Neutral", "signal_list": [],
        "trend_vs_sma50": "N/A", "price_vs_bb": "Mid Range",
        "ohlcv_dates": [], "ohlcv_open": [], "ohlcv_high": [],
        "ohlcv_low": [], "ohlcv_close": [], "ohlcv_volume": [],
        "rsi_series": [], "macd_line_series": [], "macd_sig_series": [],
        "macd_hist_series": [], "sma20_series": [], "sma50_series": [],
        "bb_upper_series": [], "bb_mid_series": [], "bb_lower_series": [],
        "fetch_ok": False,
    }

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_fetch_single_ta, t): t for t in tickers
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                ticker_upper, ta_dict = future.result()
                results[ticker_upper] = ta_dict
            except Exception:
                orig_ticker = futures[future].upper()
                results[orig_ticker] = dict(empty_template)

    return results


# ---------------------------------------------------------------------------
# Public enrichment API
# ---------------------------------------------------------------------------

def enrich_with_technicals(
    df: pd.DataFrame,
    top_n: int = 25,
) -> pd.DataFrame:
    """
    Enrich the top N rows of the screener DataFrame with TA columns.

    For tickers outside top_n, TA columns remain None / default values.
    Does NOT modify speculation_score — TA is informational, not scored.

    Parameters
    ----------
    df    : pd.DataFrame — output of run_screener() or enrich_with_fundamentals()
    top_n : int — how many top-ranked tickers (by current speculation_score) to enrich

    Returns
    -------
    pd.DataFrame — same row order as input, with added columns:
        rsi             float | None
        trend_vs_sma50  str  | None   ("Above SMA50" | "Below SMA50" | "N/A")
        ta_signal       str  | None   ("Bullish" | "Neutral" | "Bearish")
        ta_signal_list  str  | None   JSON-encoded list[str] of signal strings
        has_technicals  bool
    """
    if df.empty:
        return df

    df = df.copy()

    # Initialize new columns with defaults
    df["rsi"] = None
    df["trend_vs_sma50"] = None
    df["ta_signal"] = None
    df["ta_signal_list"] = None
    df["has_technicals"] = False

    top_tickers = df.head(top_n)["ticker"].tolist()
    ta_map = fetch_ta_for_tickers(top_tickers)

    for idx, row in df.iterrows():
        ticker = str(row["ticker"]).upper()
        if ticker not in ta_map:
            continue
        ta = ta_map[ticker]
        if not ta.get("fetch_ok"):
            continue
        df.at[idx, "rsi"] = ta.get("rsi")
        df.at[idx, "trend_vs_sma50"] = ta.get("trend_vs_sma50", "N/A")
        df.at[idx, "ta_signal"] = ta.get("ta_signal", "Neutral")
        df.at[idx, "ta_signal_list"] = json.dumps(ta.get("signal_list", []))
        df.at[idx, "has_technicals"] = True

    return df
