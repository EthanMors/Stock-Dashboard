# Plan: Screener Technical Analysis

## Overview

This plan adds technical analysis (RSI, SMA20/50, MACD, Bollinger Bands) to the Speculative
Screener page. A new data module `data/screener_ta.py` fetches 6 months of daily OHLCV per
ticker via yfinance, computes all indicators with pandas/numpy, derives a deterministic
"TA Signal" (Bullish / Neutral / Bearish), and caches results to a new `screener_ta` table in
`db/screener.db` (1-hour TTL). An `enrich_with_technicals()` function enriches the top-N
screener rows in parallel (matching the `enrich_with_fundamentals` pattern). The screener page
gains new TA columns in the results table, a full 3-row Plotly multi-subplot TA chart inside
each stock-detail expander, and an updated sidebar "About" explanation. The Gemini risk-analysis
prompt in `screener_agent.py` is also extended with three TA fields.

---

## Files to Create

- `stock-dashboard/data/screener_ta.py` — new data module: OHLCV fetch, indicator math,
  signal scoring, SQLite cache helpers, parallel enrichment function
- `stock-dashboard/db/screener_ta_schema.sql` — DDL for the `screener_ta` table

## Files to Modify

- `stock-dashboard/db/screener_schema.sql` — append `\i screener_ta_schema.sql` comment; no
  actual DDL change needed here (see Step 1 — the TA schema is its own file, loaded by
  `screener_ta.py`'s `init_db()`)
- `stock-dashboard/data/screener_agent.py` — extend `_build_prompt` and `_PROMPT_TEMPLATE`
  to include three TA fields: rsi, trend_vs_sma50, ta_signal
- `stock-dashboard/pages/12_screener.py` — add import of `enrich_with_technicals`, add
  `_cached_ta_chart` + `_build_ta_chart` helpers, update `_render_results_table` to show TA
  columns, replace simple price chart in `_render_stock_detail` with the new TA chart,
  add TA signal badge + signal list, update sidebar "About", update page `main()` to call
  `enrich_with_technicals`

---

## Prerequisites & Dependencies

No new pip packages. All indicator math uses `pandas` and `numpy`, both already installed.
No schema migration needed for existing tables — only a new table is added with
`CREATE TABLE IF NOT EXISTS` (idempotent).

---

## Step-by-Step Implementation

---

### Step 1: Create the TA schema file

**File to create**: `stock-dashboard/db/screener_ta_schema.sql`

**Action**: Create this file with the following exact content:

```sql
CREATE TABLE IF NOT EXISTS screener_ta (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT    NOT NULL,
    ta_json     TEXT    NOT NULL,
    fetched_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_screener_ta_ticker
    ON screener_ta (ticker);

CREATE INDEX IF NOT EXISTS idx_screener_ta_ticker_fetched
    ON screener_ta (ticker, fetched_at);
```

**Why**: The SQLite cache for TA results needs its own schema file following the project
pattern (separate `.sql` file per table, loaded by the owning module's `init_db()`).

---

### Step 2: Create `data/screener_ta.py`

**File to create**: `stock-dashboard/data/screener_ta.py`

**Action**: Create the file with the following exact content in full:

```python
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
```

**Why**: This is the core data module for all TA functionality. It follows the exact
patterns of `data/screener.py`: same `_DB_PATH` / `_SCHEMA_PATH` construction, same
`_get_connection()` pattern, same `init_db()` at import time, same `_save_*/``_load_*`
cache helper pair, same `ThreadPoolExecutor` parallel fetch approach.

---

### Step 3: Extend `screener_agent.py` — add TA fields to the Gemini prompt

**File**: `stock-dashboard/data/screener_agent.py`

**Location**: Inside `_PROMPT_TEMPLATE` (the multi-line string constant at lines 171–237),
in the `=== TECHNICAL METRICS ===` section, after the existing `Speculation Score:` line
(currently line 189).

**Action**: Replace the existing `_PROMPT_TEMPLATE` string. The only changes are:
1. Add three new lines to the `=== TECHNICAL METRICS ===` section after `Speculation Score:`
2. Add a TA-specific instruction paragraph to the `IMPORTANT GUIDANCE FOR FUNDAMENTAL SIGNALS:` section.
3. Add the three format placeholders to the `.format()` call in `_build_prompt`.

**Exact replacement for `_PROMPT_TEMPLATE` string** — replace lines 171–237 of the current
file (the full `_PROMPT_TEMPLATE = """\..."""` assignment) with this:

```python
_PROMPT_TEMPLATE = """\
You are a speculative equity risk analyst. Your task is to assess the risk/reward
profile of a small or micro-cap stock based on the quantitative metrics below.
These are HIGH-RISK speculative stocks. Your analysis must be rigorous, honest about
risks, and use the exact numbers provided.

=== TECHNICAL METRICS ===
Ticker:                      {ticker}
Company Name:                {name}
Current Price:               ${price}
Market Cap:                  {market_cap}
52-Week High:                ${week52_high}  (current is {week52_high_chg_pct}% from high)
52-Week Low:                 ${week52_low}   (current is {week52_low_chg_pct}% above low)
Avg Daily Volume (3M):       {volume_3m}
Avg Daily Volume (10D):      {volume_10d}
Volume Spike Ratio (10D/3M): {volume_spike}
Forward PE:                  {forward_pe}
EPS (TTM):                   {eps_ttm}
Speculation Score:           {speculation_score}/100  (technical sub-score: {technical_total}/50, fundamental sub-score: {fundamental_total}/50)
RSI (14):                    {rsi}
Price Trend vs SMA50:        {trend_vs_sma50}
TA Signal:                   {ta_signal}

=== FUNDAMENTAL METRICS ===
Revenue Growth (YoY):        {revenue_growth}
Gross Margin:                {gross_margins}
Free Cash Flow:              {free_cashflow}
Cash Runway:                 {cash_runway}
Short % of Float:            {short_percent_float}
Analyst Mean Price Target:   {target_mean_price}  (implied upside: {analyst_upside})
Debt / Equity:               {debt_to_equity}
Insider Ownership:           {held_percent_insiders}
Has Fundamentals Data:       {has_fundamentals}

=== YOUR ANALYSIS TASK ===
Analyze this speculative stock and produce a structured risk assessment. Be specific
— reference the actual numbers above. Do not invent data not present in the metrics.

IMPORTANT GUIDANCE FOR FUNDAMENTAL SIGNALS:
- If cash_runway < 6 months, this is a CRITICAL red flag — dilution or bankruptcy risk is high. Always include this in red_flags.
- If short_percent_float > 20%, explicitly address squeeze potential AND covering pressure risk.
- If analyst_upside > 100%, assess whether the target is realistic given the cash/revenue data.
- If revenue_growth is N/A or has_fundamentals is False, note that fundamental data was unavailable and increase uncertainty in your assessment.
- Use insider ownership to gauge conviction: > 20% insider ownership is bullish for small-caps; < 2% is a warning sign.

IMPORTANT GUIDANCE FOR TECHNICAL SIGNALS:
- RSI <= 30 suggests the stock is oversold and may be a mean-reversion opportunity; RSI >= 70 suggests overbought conditions and near-term selling pressure.
- If TA Signal is "Bearish", weight this as an additional risk factor even if fundamentals look attractive.
- If TA Signal is "Bullish" and Price Trend vs SMA50 is "Above SMA50", this is a confirming positive technical setup.

For risk_score: rate overall risk from 1 (extremely low risk) to 10 (extremely high risk).
For upside_thesis: what specific factors in the numbers above suggest potential upside? 2-3 sentences. Reference specific numbers.
For key_risks: list 3-5 specific risk factors based on the data.
For red_flags: list any concrete warning signs visible in these metrics — especially cash runway < 6mo, negative FCF with thin cash, or extreme short interest. If none, return an empty list.
For verdict: assign exactly one of these labels:
  "lottery ticket" — extremely speculative, could go 10x or zero
  "speculative buy" — elevated risk but metrics suggest real upside potential
  "hold/watch" — not actionable yet, monitor for catalyst
  "avoid" — risk/reward unfavourable based on these metrics

Rules:
- Be specific: reference actual numbers from both metric blocks
- verdict must be exactly one of the four labels listed above
- key_risks and red_flags must be lists of plain strings (no sub-objects)
- risk_score must be an integer 1-10
- Respond ONLY with a single JSON object (no markdown fences, no preamble, no trailing text)

{{
  "risk_score": <integer 1-10>,
  "upside_thesis": "<2-3 sentence specific upside case>",
  "key_risks": ["<risk 1>", "<risk 2>", "<risk 3>"],
  "red_flags": ["<flag 1>"],
  "verdict": "<lottery ticket|speculative buy|hold/watch|avoid>"
}}
"""
```

**Location in `_build_prompt`**: Find the `return _PROMPT_TEMPLATE.format(` call (near
line 351). The existing call ends with `has_fundamentals=has_fund_str,`. Add three new
keyword arguments immediately before the closing `)` of the `.format(` call. The exact
text to find and replace is:

Find this text (the last 3 lines of the `.format()` call before the closing paren):
```python
        held_percent_insiders=insider_str,
        has_fundamentals=has_fund_str,
    )
```

Replace with:
```python
        held_percent_insiders=insider_str,
        has_fundamentals=has_fund_str,
        rsi=_fmt_optional(metrics.get("rsi"), ".1f") if metrics.get("rsi") is not None else "N/A",
        trend_vs_sma50=str(metrics.get("trend_vs_sma50") or "N/A"),
        ta_signal=str(metrics.get("ta_signal") or "N/A"),
    )
```

**Why**: The Gemini risk prompt already has excellent metric coverage; adding three TA
fields (RSI, SMA50 trend, overall TA signal) gives the model relevant momentum context
with minimal prompt size increase. `_fmt_optional` is already defined in this file.

---

### Step 4: Add TA chart builder and cached fetcher to `pages/12_screener.py`

**File**: `stock-dashboard/pages/12_screener.py`

**Sub-step 4a — Add imports**

Find the existing import block at the top of the file (lines 1–11). The current imports end with:
```python
from data.gemini_tracker import get_today_stats, PRO_DAILY_LIMIT
```

Add two new import lines directly after that line:
```python
from plotly.subplots import make_subplots
from data.screener_ta import enrich_with_technicals, fetch_ta_for_tickers
```

**Sub-step 4b — Add TA chart color constants**

Find the line `_CAP_TIER_OPTIONS = {` in the `# Constants` section (around line 49).
Insert the following block of constants **immediately before** that line:

```python
# TA chart colour constants (matches 10_technical_analysis.py palette)
_TA_UP        = "#26a69a"
_TA_DOWN      = "#ef5350"
_TA_BB_LINE   = "rgba(100, 149, 237, 0.85)"
_TA_BB_FILL   = "rgba(100, 149, 237, 0.07)"
_TA_BB_MID    = "rgba(100, 149, 237, 0.5)"
_TA_SMA20_CLR = "#ffd600"
_TA_SMA50_CLR = "#ff6b6b"
_TA_MACD_CLR  = "#4f8ef7"
_TA_SIG_CLR   = "#ff9800"
_TA_HIST_POS  = "rgba(38,166,154,0.7)"
_TA_HIST_NEG  = "rgba(239,83,80,0.7)"
_TA_RSI_CLR   = "#a29bfe"
_TA_BG        = "#0e1117"
_TA_PLOT_BG   = "#161b27"
_TA_GRID      = "#1e2740"

_TA_SIGNAL_COLORS = {
    "Bullish": "#00c853",
    "Neutral": "#2979ff",
    "Bearish": "#ff1744",
}
```

**Sub-step 4c — Add `_build_ta_chart` helper function**

Find the existing `_build_price_chart` function (starts at line 154 of the original file).
Insert the following new function **immediately after** the closing `return fig` of
`_build_price_chart` (i.e., after the function ends, before `# Cached data fetchers`):

```python
def _build_ta_chart(ta_data: dict, ticker: str) -> go.Figure:
    """
    Build a 3-row Plotly chart from a pre-fetched TA data dict:
      Row 1 (60%): Candlestick + SMA20 + SMA50 + Bollinger Bands
      Row 2 (20%): RSI with oversold/overbought reference lines
      Row 3 (20%): MACD line + Signal line + Histogram

    Parameters
    ----------
    ta_data : dict — as returned by _fetch_single_ta / the screener_ta SQLite cache.
              Must have keys: ohlcv_dates, ohlcv_open, ohlcv_high, ohlcv_low,
              ohlcv_close, ohlcv_volume, sma20_series, sma50_series,
              bb_upper_series, bb_mid_series, bb_lower_series,
              rsi_series, macd_line_series, macd_sig_series, macd_hist_series
    ticker  : str — used for chart title and candlestick legend name

    Returns
    -------
    go.Figure with 3 sub-plots. Returns a single-panel error figure if ta_data
    is empty or missing OHLCV data.
    """
    dates = ta_data.get("ohlcv_dates", [])
    opens = ta_data.get("ohlcv_open", [])
    highs = ta_data.get("ohlcv_high", [])
    lows  = ta_data.get("ohlcv_low", [])
    closes = ta_data.get("ohlcv_close", [])

    if not dates or not closes:
        fig = go.Figure()
        fig.update_layout(
            **plotly_dark_layout(
                annotations=[{
                    "text": f"No TA data available for {ticker}",
                    "xref": "paper", "yref": "paper",
                    "x": 0.5, "y": 0.5, "showarrow": False,
                    "font": {"size": 14, "color": "#556080"},
                }],
                xaxis_visible=False,
                yaxis_visible=False,
                height=300,
            )
        )
        return fig

    # Colour each volume/MACD histogram bar by direction
    hist_vals = ta_data.get("macd_hist_series", [])
    hist_colors = [
        _TA_HIST_POS if (v is not None and v >= 0) else _TA_HIST_NEG
        for v in hist_vals
    ]

    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.60, 0.20, 0.20],
        subplot_titles=[f"{ticker.upper()} — 6-Month Price", "RSI (14)", "MACD (12/26/9)"],
    )

    # ── Row 1: Candlestick ────────────────────────────────────────────────
    fig.add_trace(
        go.Candlestick(
            x=dates,
            open=opens,
            high=highs,
            low=lows,
            close=closes,
            name=ticker.upper(),
            increasing_line_color=_TA_UP,
            decreasing_line_color=_TA_DOWN,
            increasing_fillcolor=_TA_UP,
            decreasing_fillcolor=_TA_DOWN,
            line_width=1,
        ),
        row=1, col=1,
    )

    # ── Row 1: Bollinger Bands ────────────────────────────────────────────
    bb_upper = ta_data.get("bb_upper_series", [])
    bb_mid   = ta_data.get("bb_mid_series", [])
    bb_lower = ta_data.get("bb_lower_series", [])

    if bb_upper and any(v is not None for v in bb_upper):
        fig.add_trace(go.Scatter(
            x=dates, y=bb_upper,
            name="BB Upper",
            line=dict(color=_TA_BB_LINE, width=1, dash="dot"),
            showlegend=True,
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=dates, y=bb_lower,
            name="BB Lower",
            fill="tonexty",
            fillcolor=_TA_BB_FILL,
            line=dict(color=_TA_BB_LINE, width=1, dash="dot"),
            showlegend=True,
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=dates, y=bb_mid,
            name="BB Mid",
            line=dict(color=_TA_BB_MID, width=1),
            showlegend=True,
        ), row=1, col=1)

    # ── Row 1: SMA20 & SMA50 ──────────────────────────────────────────────
    sma20 = ta_data.get("sma20_series", [])
    sma50 = ta_data.get("sma50_series", [])

    if sma20 and any(v is not None for v in sma20):
        fig.add_trace(go.Scatter(
            x=dates, y=sma20,
            name="SMA20",
            line=dict(color=_TA_SMA20_CLR, width=1.5),
            showlegend=True,
        ), row=1, col=1)

    if sma50 and any(v is not None for v in sma50):
        fig.add_trace(go.Scatter(
            x=dates, y=sma50,
            name="SMA50",
            line=dict(color=_TA_SMA50_CLR, width=1.5),
            showlegend=True,
        ), row=1, col=1)

    # ── Row 2: RSI ────────────────────────────────────────────────────────
    rsi_vals = ta_data.get("rsi_series", [])
    if rsi_vals and any(v is not None for v in rsi_vals):
        fig.add_trace(go.Scatter(
            x=dates, y=rsi_vals,
            name="RSI (14)",
            line=dict(color=_TA_RSI_CLR, width=1.5),
            showlegend=True,
        ), row=2, col=1)

        # Oversold / overbought reference lines
        fig.add_hline(
            y=70, line_dash="dot", line_color="rgba(239,83,80,0.5)",
            line_width=1, row=2, col=1,
        )
        fig.add_hline(
            y=30, line_dash="dot", line_color="rgba(38,166,154,0.5)",
            line_width=1, row=2, col=1,
        )

    # ── Row 3: MACD ───────────────────────────────────────────────────────
    macd_line_vals = ta_data.get("macd_line_series", [])
    macd_sig_vals  = ta_data.get("macd_sig_series", [])
    macd_hist_vals = ta_data.get("macd_hist_series", [])

    if macd_line_vals and any(v is not None for v in macd_line_vals):
        fig.add_trace(go.Scatter(
            x=dates, y=macd_line_vals,
            name="MACD",
            line=dict(color=_TA_MACD_CLR, width=1.5),
            showlegend=True,
        ), row=3, col=1)

    if macd_sig_vals and any(v is not None for v in macd_sig_vals):
        fig.add_trace(go.Scatter(
            x=dates, y=macd_sig_vals,
            name="Signal",
            line=dict(color=_TA_SIG_CLR, width=1.5),
            showlegend=True,
        ), row=3, col=1)

    if macd_hist_vals and any(v is not None for v in macd_hist_vals):
        fig.add_trace(go.Bar(
            x=dates, y=macd_hist_vals,
            name="Histogram",
            marker_color=hist_colors,
            showlegend=False,
        ), row=3, col=1)

    # ── Layout ────────────────────────────────────────────────────────────
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=_TA_BG,
        plot_bgcolor=_TA_PLOT_BG,
        height=580,
        margin=dict(l=0, r=60, t=40, b=0),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
            font=dict(size=10),
            bgcolor="rgba(0,0,0,0)",
        ),
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
    )

    fig.update_xaxes(gridcolor=_TA_GRID, showgrid=True, zeroline=False)
    fig.update_yaxes(gridcolor=_TA_GRID, showgrid=True, zeroline=False)
    fig.update_yaxes(side="right", row=1, col=1, title_text="Price (USD)", title_font_size=10)
    fig.update_yaxes(side="right", row=2, col=1, title_text="RSI", title_font_size=10, range=[0, 100])
    fig.update_yaxes(side="right", row=3, col=1, title_text="MACD", title_font_size=10)

    return fig
```

**Sub-step 4d — Add `_cached_ta_chart` cached wrapper**

Find the existing `@st.cache_data(ttl=600)` decorator above `_cached_price_chart` (around
line 233 of the original). Insert the following new cached function **immediately after**
the `_cached_price_chart` function (i.e., after its closing line `return _build_price_chart(ticker)`):

```python
@st.cache_data(ttl=3600)
def _cached_ta_chart(ticker: str) -> go.Figure:
    """Cache the full TA chart for 1 hour per ticker (matches TA cache TTL)."""
    from data.screener_ta import _fetch_single_ta
    _, ta_data = _fetch_single_ta(ticker)
    return _build_ta_chart(ta_data, ticker)
```

**Why**: These helpers follow the exact pattern of `_build_price_chart` / `_cached_price_chart`
already in the file. The 1-hour TTL matches the SQLite cache TTL in `screener_ta.py`.

---

### Step 5: Update `_render_results_table` to show TA columns

**File**: `stock-dashboard/pages/12_screener.py`

**Location**: Inside `_render_results_table`, in the block that appends fundamental columns
(the `if "has_fundamentals" in df.columns and df["has_fundamentals"].any():` block). Find
the closing `display_data["Has Fundamentals"] = ...` line inside that block (around line 414
of the original). Immediately after that line, add the following block that appends TA columns:

```python
    # Append TA columns if any rows have been enriched with technicals
    if "has_technicals" in df.columns and df["has_technicals"].any():
        def _fmt_rsi(v) -> str:
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return "N/A"
            try:
                r = float(v)
                if r <= 30:
                    return f"{r:.0f} (OS)"
                if r >= 70:
                    return f"{r:.0f} (OB)"
                return f"{r:.0f}"
            except (TypeError, ValueError):
                return "N/A"

        display_data["RSI"] = df["rsi"].apply(_fmt_rsi)
        display_data["Trend vs SMA50"] = df["trend_vs_sma50"].apply(
            lambda v: v if v is not None else "N/A"
        )
        display_data["TA Signal"] = df["ta_signal"].apply(
            lambda v: v if v is not None else "N/A"
        )
```

**Why**: Mirrors the `has_fundamentals` conditional column pattern already present in the
function. The `_fmt_rsi` formatter annotates oversold (OS) and overbought (OB) values inline.

---

### Step 6: Replace price chart with TA chart in `_render_stock_detail`

**File**: `stock-dashboard/pages/12_screener.py`

**Location**: Inside `_render_stock_detail`, find the two-line block that currently renders
the price chart:

```python
        # Price chart
        chart_fig = _cached_price_chart(ticker)
        st.plotly_chart(chart_fig, use_container_width=True)
```

Replace those three lines (the comment and both statement lines) with the following block:

```python
        # TA chart (falls back to price chart if TA data unavailable)
        if row.get("has_technicals"):
            ta_chart_fig = _cached_ta_chart(ticker)
        else:
            ta_chart_fig = _cached_price_chart(ticker)
        st.plotly_chart(ta_chart_fig, use_container_width=True)
```

**Why**: When TA enrichment ran for this ticker, we show the full 3-row TA chart. When it
did not (ticker outside top-N), we fall back to the original price chart, preserving
existing behaviour for non-enriched rows.

---

### Step 7: Add TA signal badge and signal list to `_render_stock_detail`

**File**: `stock-dashboard/pages/12_screener.py`

**Location**: Inside `_render_stock_detail`, immediately after the `st.plotly_chart(...)` call
added in Step 6, and before the `# Key metrics row` comment. Insert the following block:

```python
        # TA signal badge and signal list (only when has_technicals)
        if row.get("has_technicals"):
            ta_signal = str(row.get("ta_signal") or "Neutral")
            ta_color = _TA_SIGNAL_COLORS.get(ta_signal, "#2979ff")
            ta_rsi = row.get("rsi")
            ta_rsi_str = f"RSI {float(ta_rsi):.0f}" if ta_rsi is not None else "RSI N/A"
            trend = str(row.get("trend_vs_sma50") or "N/A")
            ta_col1, ta_col2, ta_col3, _ = st.columns([1, 1, 2, 4])
            ta_col1.markdown(
                f'<div style="background:{ta_color};border-radius:8px;padding:6px 12px;'
                f'text-align:center;font-weight:700;font-size:0.85rem;color:#0d1020;">'
                f'TA: {ta_signal}</div>',
                unsafe_allow_html=True,
            )
            ta_col2.markdown(
                f'<div style="background:#161b27;border:1px solid #1e2740;border-radius:8px;'
                f'padding:6px 12px;text-align:center;font-size:0.8rem;color:#e8eaf0;">'
                f'{ta_rsi_str}</div>',
                unsafe_allow_html=True,
            )
            ta_col3.markdown(
                f'<div style="background:#161b27;border:1px solid #1e2740;border-radius:8px;'
                f'padding:6px 12px;text-align:center;font-size:0.8rem;color:#e8eaf0;">'
                f'{trend}</div>',
                unsafe_allow_html=True,
            )

            # Signal list (collapsed expander)
            ta_signals_raw = row.get("ta_signal_list")
            if ta_signals_raw:
                try:
                    import json as _json_ta
                    ta_signal_items = _json_ta.loads(ta_signals_raw) if isinstance(ta_signals_raw, str) else ta_signals_raw
                except (TypeError, ValueError):
                    ta_signal_items = []
                if ta_signal_items:
                    with st.expander("Technical Signals Detected", expanded=False):
                        for sig in ta_signal_items:
                            st.markdown(f"- {sig}")
```

**Why**: Displays the TA signal verdict as a color-coded badge (same HTML pattern as the
Gemini verdict badge in `_render_analysis_block`), an RSI pill, and a trend pill. The signal
list provides the human-readable explanation of what drove the signal score.

---

### Step 8: Update `main()` in `pages/12_screener.py` to call `enrich_with_technicals`

**File**: `stock-dashboard/pages/12_screener.py`

**Location**: In `main()`, find the two `enrich_with_fundamentals` call sites. There are two:
one under `if "screener_results_df" not in st.session_state or run_btn:` and one under
`if refresh_btn:`. In both locations, after the `df = enrich_with_fundamentals(...)` call
(and its surrounding `with st.spinner(...)` block), add a new enrichment call for TA.

**First site** — the `run_btn` path. Find this exact text:

```python
        if not df.empty:
            with st.spinner(f"Enriching top {enrich_top_n} candidates with fundamentals (24h cached)..."):
                df = enrich_with_fundamentals(df, top_n=enrich_top_n)
        st.session_state["screener_results_df"] = df
```

Replace with:

```python
        if not df.empty:
            with st.spinner(f"Enriching top {enrich_top_n} candidates with fundamentals (24h cached)..."):
                df = enrich_with_fundamentals(df, top_n=enrich_top_n)
            with st.spinner(f"Computing technical analysis for top {enrich_top_n} candidates (1h cached)..."):
                df = enrich_with_technicals(df, top_n=enrich_top_n)
        st.session_state["screener_results_df"] = df
```

**Second site** — the `refresh_btn` path. Find this exact text:

```python
        if not df.empty:
            with st.spinner(f"Enriching top {enrich_top_n} candidates with fundamentals (24h cached)..."):
                df = enrich_with_fundamentals(df, top_n=enrich_top_n)
        st.session_state["screener_results_df"] = df
        _cached_screener.clear()
```

Replace with:

```python
        if not df.empty:
            with st.spinner(f"Enriching top {enrich_top_n} candidates with fundamentals (24h cached)..."):
                df = enrich_with_fundamentals(df, top_n=enrich_top_n)
            with st.spinner(f"Computing technical analysis for top {enrich_top_n} candidates (1h cached)..."):
                df = enrich_with_technicals(df, top_n=enrich_top_n)
        st.session_state["screener_results_df"] = df
        _cached_screener.clear()
```

**Why**: Both run paths (initial run and force-refresh) must call TA enrichment so the
session state DataFrame always includes TA columns when TA data is available. The spinner
matches the style of the existing fundamentals spinner.

---

### Step 9: Update `_render_sidebar` in `pages/12_screener.py` to explain TA signals

**File**: `stock-dashboard/pages/12_screener.py`

**Location**: Inside `_render_sidebar()`, find the last `with st.expander` block (the one
for "What does the Gemini Risk Analysis do?"). Add a new `with st.expander` block immediately
**after** that block's closing line and **before** the `st.markdown("---")` line that
follows it.

Find this exact text:
```python
        with st.expander("What does the Gemini Risk Analysis do?"):
            st.markdown(
                "Gemini 2.5 Pro reviews the stock's quantitative metrics and returns:\n"
                "- **Risk Score (1–10):** 1 = very low risk, 10 = extremely high risk\n"
                "- **Upside Thesis:** what the numbers suggest about upside potential\n"
                "- **Key Risks:** 3–5 specific risk factors from the metrics\n"
                "- **Red Flags:** any concrete warning signs\n"
                "- **Verdict:** lottery ticket / speculative buy / hold-watch / avoid\n\n"
                "Results are cached for 24 hours to conserve Gemini Pro quota (50/day)."
            )
        st.markdown("---")
```

Replace with:

```python
        with st.expander("What does the Gemini Risk Analysis do?"):
            st.markdown(
                "Gemini 2.5 Pro reviews the stock's quantitative metrics and returns:\n"
                "- **Risk Score (1–10):** 1 = very low risk, 10 = extremely high risk\n"
                "- **Upside Thesis:** what the numbers suggest about upside potential\n"
                "- **Key Risks:** 3–5 specific risk factors from the metrics\n"
                "- **Red Flags:** any concrete warning signs\n"
                "- **Verdict:** lottery ticket / speculative buy / hold-watch / avoid\n\n"
                "Results are cached for 24 hours to conserve Gemini Pro quota (50/day)."
            )
        with st.expander("How are Technical Analysis signals computed?"):
            st.markdown(
                "For the top-N candidates, 6 months of daily OHLCV data is fetched "
                "from Yahoo Finance and five indicators are computed:\n\n"
                "- **RSI (14):** Relative Strength Index. ≤ 30 = oversold (potential bounce); "
                "≥ 70 = overbought (potential pullback).\n"
                "- **SMA20 / SMA50:** 20- and 50-day simple moving averages. A **Golden Cross** "
                "(SMA20 crosses above SMA50) is bullish; a **Death Cross** is bearish.\n"
                "- **MACD (12/26/9):** Momentum oscillator. A MACD line crossing above the "
                "signal line is bullish; crossing below is bearish.\n"
                "- **Bollinger Bands (20, 2σ):** Volatility envelope. Price near the lower "
                "band suggests oversold; near the upper band suggests overextension.\n\n"
                "The **TA Signal** (Bullish / Neutral / Bearish) is a simple vote-count: "
                "each bullish indicator signal adds +1 (golden cross adds +2), bearish adds -1 "
                "(-2 for death cross). Signal ≥ +2 net = Bullish; ≤ -2 net = Bearish; "
                "otherwise Neutral. Results are cached for 1 hour."
            )
        st.markdown("---")
```

**Why**: Users need to understand what the new TA Signal means and how it is computed.
Placing it in the same sidebar "About" expander group maintains the existing page structure.

---

## Database Changes

**New table `screener_ta` in `db/screener.db`**

Created by `screener_ta_schema.sql` and applied by `data/screener_ta.py`'s `init_db()`
at import time. No changes to existing tables.

Schema (exact DDL from Step 1):

```sql
CREATE TABLE IF NOT EXISTS screener_ta (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT    NOT NULL,
    ta_json     TEXT    NOT NULL,
    fetched_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_screener_ta_ticker
    ON screener_ta (ticker);
CREATE INDEX IF NOT EXISTS idx_screener_ta_ticker_fetched
    ON screener_ta (ticker, fetched_at);
```

**No rollback required** — `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`
are idempotent. To remove: `DROP TABLE IF EXISTS screener_ta;` in a SQLite shell.

---

## UI / UX Specification

### Results table (after TA enrichment)
New columns appended after `"Has Fundamentals"`:
| Column | Source | Format |
|--------|--------|--------|
| RSI | `df["rsi"]` | `"28 (OS)"` for ≤30, `"75 (OB)"` for ≥70, `"52"` otherwise, `"N/A"` if None |
| Trend vs SMA50 | `df["trend_vs_sma50"]` | Passthrough string: `"Above SMA50"`, `"Below SMA50"`, or `"N/A"` |
| TA Signal | `df["ta_signal"]` | Passthrough string: `"Bullish"`, `"Neutral"`, or `"Bearish"` |

These columns only appear when `df["has_technicals"].any()` is True (same guard as
fundamentals columns).

### Stock detail expander — TA chart
- **Chart type**: `make_subplots` with 3 rows, shared x-axis
- **Row heights**: `[0.60, 0.20, 0.20]`
- **Row 1 (60%)**: Candlestick + BB Upper (blue dotted) + BB Lower (blue dotted, filled) + BB Mid (blue) + SMA20 (amber `#ffd600`) + SMA50 (coral `#ff6b6b`)
- **Row 2 (20%)**: RSI line (lavender `#a29bfe`) + red dotted hline at 70 + green dotted hline at 30; y-axis range [0, 100]; axis label "RSI"
- **Row 3 (20%)**: MACD line (blue `#4f8ef7`) + Signal line (orange `#ff9800`) + Histogram bars (green `rgba(38,166,154,0.7)` for positive, red `rgba(239,83,80,0.7)` for negative); axis label "MACD"
- **Background**: `paper_bgcolor="#0e1117"`, `plot_bgcolor="#161b27"`, `template="plotly_dark"`
- **Grid color**: `#1e2740`
- **Height**: 580px
- **Fallback**: if `row.get("has_technicals")` is False, show existing `_cached_price_chart`

### TA signal badge row (above key metrics)
4 columns `[1, 1, 2, 4]`:
- Col 1: colored div badge showing `"TA: Bullish"` / `"TA: Neutral"` / `"TA: Bearish"`
  - Color: `_TA_SIGNAL_COLORS` — Bullish=`#00c853`, Neutral=`#2979ff`, Bearish=`#ff1744`
- Col 2: dark pill showing `"RSI 28"` or `"RSI N/A"`
- Col 3: dark pill showing `"Above SMA50"` / `"Below SMA50"` / `"N/A"`
- Col 4: empty spacer

Below the badge row: a collapsed `st.expander("Technical Signals Detected")` listing each
signal string from `ta_signal_list` as bullet points.

---

## Testing Checklist

1. **Launch the app** — navigate to Speculative Screener. Confirm no import errors.
   Expected: page loads with no traceback in the terminal or on-screen.

2. **Run screener with default filters** — click "Run Screener".
   Expected: spinner shows "Computing technical analysis for top 25 candidates...", then results
   table appears. If TA enrichment succeeded, the table has columns: RSI, Trend vs SMA50, TA Signal.

3. **Results table TA columns** — check that RSI column shows values like `"52"`, `"28 (OS)"`,
   `"72 (OB)"`, or `"N/A"`. Trend vs SMA50 shows `"Above SMA50"` or `"Below SMA50"`.
   TA Signal shows `"Bullish"`, `"Neutral"`, or `"Bearish"`.
   Failure: column missing = import not added or `enrich_with_technicals` not called in `main()`.

4. **TA chart renders** — expand any ticker in the top 25. Confirm the 3-row TA chart shows
   instead of the old line chart. Verify candlestick row, RSI row, and MACD row are all visible.
   Failure: old line chart still showing = `has_technicals` check not working or chart function
   not added.

5. **TA signal badge** — in the same expanded ticker, above "Key Metrics", verify a colored
   badge row shows the TA signal, RSI value pill, and SMA50 trend pill.
   Failure: badge missing = Step 7 insert location wrong.

6. **Technical Signals expander** — click the "Technical Signals Detected" expander.
   Expected: list of 1–6 bullet points, e.g., `"RSI 28 — oversold"`, `"Price below SMA50 ($4.82)"`.
   Failure: empty or missing expander = `ta_signal_list` column not populated.

7. **Ticker outside top-25** — expand a ticker ranked below 25th. Confirm it shows the old
   price line chart (not the TA chart) and no TA badge row.
   Expected: `has_technicals` is False for these rows, so fallback to `_cached_price_chart`.

8. **SQLite cache works** — run the screener a second time immediately. Confirm the TA spinner
   completes in under 1 second (cached results served from SQLite).
   Failure: slow second run = `_load_ta` not returning cached data.

9. **Force Refresh bypasses cache** — click "Force Refresh". Confirm that screener re-fetches
   and TA spinner runs again (may be fast if within 1h window since SQLite TA cache is
   independent of the screener cache).

10. **Sidebar About section** — in the sidebar, expand the "How are Technical Analysis signals
    computed?" expander. Confirm the text describes RSI, SMA20/50, MACD, Bollinger Bands, and
    the voting logic.
    Failure: expander absent = Step 9 edit applied at wrong location.

11. **Gemini analysis includes TA fields** — run Gemini analysis on one stock from the top-25
    (which has TA data). In the Gemini prompt log (visible in terminal during analysis), or by
    checking the returned analysis, confirm the upside_thesis or key_risks reference the RSI
    or TA signal when relevant.
    Expected: the Pro model has access to RSI, trend, and TA signal in its context.

12. **Edge case — ticker with < 26 bars** — if yfinance returns fewer than 26 bars (very new
    listing), confirm the expander shows the fallback price chart and no TA badge (has_technicals=False).
    Expected: no crash, graceful fallback.

---

## Rollback Plan

To fully undo all changes:

1. **Delete `stock-dashboard/data/screener_ta.py`**
2. **Delete `stock-dashboard/db/screener_ta_schema.sql`**
3. **Revert `stock-dashboard/data/screener_agent.py`** — restore `_PROMPT_TEMPLATE` to its
   original content (remove the three TA lines and the TA guidance paragraph) and revert the
   three lines added to the `.format()` call.
4. **Revert `stock-dashboard/pages/12_screener.py`** — remove:
   - The two new import lines (`make_subplots` and `screener_ta` imports)
   - The `_TA_*` constants block
   - The `_build_ta_chart` function
   - The `_cached_ta_chart` function
   - The TA column block in `_render_results_table`
   - The TA chart / badge / signal-list additions in `_render_stock_detail`
   - The two `enrich_with_technicals` calls in `main()`
   - The new sidebar expander block in `_render_sidebar`
5. **Drop the DB table** (optional — does not affect app correctness if left):
   Open a SQLite shell: `sqlite3 stock-dashboard/db/screener.db`
   Run: `DROP TABLE IF EXISTS screener_ta;`
   Run: `DROP INDEX IF EXISTS idx_screener_ta_ticker;`
   Run: `DROP INDEX IF EXISTS idx_screener_ta_ticker_fetched;`
