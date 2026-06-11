# Plan: AI Signal Backtester

## Overview

This plan adds a new page (`pages/10_backtest.py`) and supporting data infrastructure that measures the historical predictive accuracy of every AI signal produced by the dashboard. It uses two complementary modes: (1) Cached Signal Replay — extracting existing AI analyses already stored in SQLite DBs (with real `analyzed_at` timestamps) and pairing them with yfinance forward price returns; and (2) Walk-Forward Technical Replay — re-running the existing `PatternDetectionEngine` on rolling 60-day OHLCV windows across a historical date range, generating many additional data points with zero Gemini calls. The expected outcome when this plan is fully executed: a fully functional "Backtest" page (numbered 10) appears in the Streamlit sidebar, where the user can select tickers, a date range, a forward horizon (1/5/10/20 days), and which signal types to include, click "Run Backtest," and see comprehensive accuracy metrics including Hit Rate, Information Coefficient (IC), ICIR, t-statistic, per-direction average returns, equity curves vs SPY, and signal instance drill-downs, all sourced from a new `backtest.db` SQLite database.

---

## Files to Create

- `stock-dashboard/db/backtest_schema.sql` — DDL for `backtest_runs`, `backtest_signals`, `backtest_outcomes`, `backtest_metrics` tables
- `stock-dashboard/data/backtest_signals.py` — Signal extraction from all 7 AI sources plus technical pattern engine; signal scoring to numeric [-1, 1]
- `stock-dashboard/data/backtest_engine.py` — Core orchestration: run_backtest(), forward return fetching, metric computation, equity curve simulation
- `stock-dashboard/pages/10_backtest.py` — Streamlit UI page with sidebar controls and 6-tab main area

## Files to Modify

- `stock-dashboard/requirements.txt` — Already contains `scipy>=1.13.0`; no change required

## Database Changes

- New database: `stock-dashboard/db/backtest.db` (auto-created on first import of `backtest_engine.py`)
- New schema file: `stock-dashboard/db/backtest_schema.sql`
- Tables: `backtest_runs`, `backtest_signals`, `backtest_outcomes`, `backtest_metrics`

---

## Step-by-Step Implementation

---

### Step 1: Create `stock-dashboard/db/backtest_schema.sql`

**File**: `stock-dashboard/db/backtest_schema.sql`
**Action**: Create this file with the following exact content:

```sql
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id       TEXT PRIMARY KEY,
    tickers      TEXT NOT NULL,
    date_from    TEXT NOT NULL,
    date_to      TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    signal_types TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    signal_date  TEXT NOT NULL,
    signal_type  TEXT NOT NULL,
    direction    TEXT NOT NULL,
    score        REAL NOT NULL,
    source_db    TEXT,
    raw_json     TEXT,
    FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_bs_run_id
    ON backtest_signals (run_id);

CREATE INDEX IF NOT EXISTS idx_bs_ticker_date
    ON backtest_signals (ticker, signal_date);

CREATE INDEX IF NOT EXISTS idx_bs_signal_type
    ON backtest_signals (signal_type);

CREATE TABLE IF NOT EXISTS backtest_outcomes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id        INTEGER NOT NULL,
    forward_return   REAL,
    benchmark_return REAL,
    alpha            REAL,
    price_at_signal  REAL,
    price_at_horizon REAL,
    correct          INTEGER,
    FOREIGN KEY (signal_id) REFERENCES backtest_signals(id)
);

CREATE INDEX IF NOT EXISTS idx_bo_signal_id
    ON backtest_outcomes (signal_id);

CREATE TABLE IF NOT EXISTS backtest_metrics (
    run_id             TEXT NOT NULL,
    signal_type        TEXT NOT NULL,
    horizon_days       INTEGER NOT NULL,
    signal_count       INTEGER,
    hit_rate           REAL,
    avg_return_bull    REAL,
    avg_return_bear    REAL,
    avg_return_neutral REAL,
    ic                 REAL,
    ic_std             REAL,
    icir               REAL,
    t_stat             REAL,
    profit_factor      REAL,
    avg_alpha          REAL,
    sharpe             REAL,
    max_drawdown       REAL,
    PRIMARY KEY (run_id, signal_type, horizon_days)
);
```

**Why**: Defines the persistence schema for backtest runs, per-signal inputs, per-signal outcomes, and aggregated metrics. Using `CREATE TABLE IF NOT EXISTS` makes it idempotent on repeated imports.

---

### Step 2: Create `stock-dashboard/data/backtest_signals.py`

**File**: `stock-dashboard/data/backtest_signals.py`
**Action**: Create this file with the following exact content:

```python
"""
backtest_signals.py

Extracts historical AI signals from all cached SQLite sources and converts
them to a normalized list of signal dicts ready for the backtest engine.

Each returned signal dict has these keys:
    ticker       : str   — uppercase ticker symbol
    signal_date  : date  — datetime.date object (the day of analysis)
    signal_type  : str   — one of: options_ai|news|reddit|hedge_fund|macro|mpt|technical
    direction    : str   — 'bullish'|'bearish'|'neutral'
    score        : float — numeric [-1, 1] representing signal strength and direction
    source_db    : str   — human-readable source description
    raw_json     : str   — JSON-serialized snippet of the original AI output

Public functions
----------------
get_options_ai_signals(tickers, date_from, date_to) -> list[dict]
get_news_signals(tickers, date_from, date_to) -> list[dict]
get_reddit_signals(tickers, date_from, date_to) -> list[dict]
get_hedge_fund_signals(tickers, date_from, date_to) -> list[dict]
get_macro_signals(date_from, date_to) -> list[dict]
get_mpt_signals(tickers, date_from, date_to) -> list[dict]
get_technical_signals(tickers, date_from, date_to) -> list[dict]
score_signal(signal_type, raw_output) -> float
"""

import json
import os
import sqlite3
from datetime import date, datetime, timezone

import pandas as pd
import yfinance as yf

from analytics.patterns import PatternDetectionEngine

# ---------------------------------------------------------------------------
# DB path constants
# ---------------------------------------------------------------------------

_PORTFOLIO_DB = os.path.join(os.path.dirname(__file__), "..", "db", "portfolio.db")
_WSB_DB       = os.path.join(os.path.dirname(__file__), "..", "db", "wsb.db")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _portfolio_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_PORTFOLIO_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _wsb_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_WSB_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_date(ts: str) -> date | None:
    """Parse an ISO datetime string or date string to a datetime.date.

    Handles formats:
      'YYYY-MM-DDTHH:MM:SS'
      'YYYY-MM-DD HH:MM:SS'
      'YYYY-MM-DD'

    Returns None if parsing fails.
    """
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts[:19], fmt).date()
        except ValueError:
            continue
    return None


def _date_in_range(d: date | None, date_from: date, date_to: date) -> bool:
    """Return True if d is not None and falls within [date_from, date_to] inclusive."""
    if d is None:
        return False
    return date_from <= d <= date_to


# ---------------------------------------------------------------------------
# Score conversion (numeric [-1, 1])
# ---------------------------------------------------------------------------

def score_signal(signal_type: str, raw_output: dict) -> float:
    """Convert a signal's categorical output to a numeric score in [-1, 1].

    Score meanings:
      +1.0 = maximum bullish
      -1.0 = maximum bearish
       0.0 = neutral

    Parameters
    ----------
    signal_type : One of options_ai|news|reddit|hedge_fund|macro|mpt|technical
    raw_output  : Dict containing the original AI/engine output fields

    Returns
    -------
    float in [-1, 1]
    """
    if signal_type == "options_ai":
        bias = raw_output.get("directional_bias", "neutral")
        strength = raw_output.get("bias_strength", "weak")
        confidence = raw_output.get("confidence", "low")

        direction_map = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}
        strength_map  = {"strong": 1.0, "moderate": 0.7, "weak": 0.4}
        conf_map      = {"high": 1.0, "medium": 0.7, "low": 0.4}

        d = direction_map.get(bias, 0.0)
        s = strength_map.get(strength, 0.4)
        c = conf_map.get(confidence, 0.4)
        return round(d * s * c, 4)

    elif signal_type == "news":
        # sentiment_score is already in [-1, 1]; use directly
        score = float(raw_output.get("sentiment_score", 0.0) or 0.0)
        return max(-1.0, min(1.0, score))

    elif signal_type == "reddit":
        # sentiment_score * (hype_level / 10) as a directional amplifier
        score    = float(raw_output.get("sentiment_score", 0.0) or 0.0)
        hype     = float(raw_output.get("hype_level", 5) or 5)
        amplifier = hype / 10.0
        return max(-1.0, min(1.0, round(score * amplifier, 4)))

    elif signal_type == "hedge_fund":
        ownership = raw_output.get("ownership_type", "mixed")
        conviction = raw_output.get("conviction_level", "low")

        ownership_map  = {"bullish_equity": 1.0, "hedged": 0.0, "speculative_put": -1.0, "mixed": 0.0}
        conviction_map = {"high": 1.0, "medium": 0.7, "low": 0.4}

        d = ownership_map.get(ownership, 0.0)
        c = conviction_map.get(conviction, 0.4)
        return round(d * c, 4)

    elif signal_type == "macro":
        category = raw_output.get("macro_category", "neutral")
        score    = float(raw_output.get("sentiment_score", 0.0) or 0.0)

        category_map = {
            "bullish_for_equities": 1.0,
            "bearish_for_equities": -1.0,
            "mixed": 0.0,
            "sector_specific": 0.0,
            "neutral": 0.0,
        }
        direction = category_map.get(category, 0.0)
        # Use sentiment_score if direction is non-zero, else 0
        if direction == 0.0:
            return 0.0
        return max(-1.0, min(1.0, round(direction * abs(score), 4)))

    elif signal_type == "mpt":
        recommendation = raw_output.get("recommendation", "hold")
        risk           = raw_output.get("risk_assessment", "medium")

        rec_map  = {"increase": 1.0, "hold": 0.0, "reduce": -1.0}
        # Risk modulates magnitude: high risk = 0.6 (more uncertain), medium = 0.8, low = 1.0
        risk_mod = {"high": 0.6, "medium": 0.8, "low": 1.0}

        d = rec_map.get(recommendation, 0.0)
        r = risk_mod.get(risk, 0.8)
        return round(d * r, 4)

    elif signal_type == "technical":
        direction = raw_output.get("direction", "neutral")
        confidence = float(raw_output.get("confidence_score", 0.5) or 0.5)

        direction_map = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}
        d = direction_map.get(direction, 0.0)
        return round(d * confidence, 4)

    return 0.0


def _direction_from_score(score: float) -> str:
    """Convert a numeric score to a direction label."""
    if score > 0.05:
        return "bullish"
    elif score < -0.05:
        return "bearish"
    return "neutral"


# ---------------------------------------------------------------------------
# Signal extractors — Cached AI sources
# ---------------------------------------------------------------------------

def get_options_ai_signals(
    tickers: list[str],
    date_from: date,
    date_to: date,
) -> list[dict]:
    """Extract options AI signals from portfolio_options_analysis in portfolio.db.

    One signal per row. Uses the most bullish/bearish expiry reading per day
    per ticker (deduplication: if multiple expiries analyzed on the same day,
    keep the one with the highest abs(score)).

    Parameters
    ----------
    tickers   : List of uppercase ticker strings.
    date_from : Start date (inclusive).
    date_to   : End date (inclusive).

    Returns
    -------
    List of signal dicts with keys: ticker, signal_date, signal_type, direction,
    score, source_db, raw_json.
    """
    tickers_upper = [t.upper() for t in tickers]
    placeholders = ",".join("?" for _ in tickers_upper)

    conn = _portfolio_conn()
    try:
        rows = conn.execute(
            f"""
            SELECT ticker, directional_bias, bias_strength, confidence,
                   metrics_json, analyzed_at
            FROM portfolio_options_analysis
            WHERE ticker IN ({placeholders})
            ORDER BY analyzed_at ASC
            """,
            tickers_upper,
        ).fetchall()
    finally:
        conn.close()

    # Build signals; deduplicate to one per (ticker, date) by max abs(score)
    best: dict[tuple, dict] = {}
    for row in rows:
        row_dict = dict(row)
        d = _parse_date(row_dict.get("analyzed_at", ""))
        if not _date_in_range(d, date_from, date_to):
            continue

        raw = {
            "directional_bias": row_dict.get("directional_bias", "neutral"),
            "bias_strength":    row_dict.get("bias_strength", "weak"),
            "confidence":       row_dict.get("confidence", "low"),
        }
        sc = score_signal("options_ai", raw)
        key = (row_dict["ticker"].upper(), d)
        existing = best.get(key)
        if existing is None or abs(sc) > abs(existing["score"]):
            best[key] = {
                "ticker":      row_dict["ticker"].upper(),
                "signal_date": d,
                "signal_type": "options_ai",
                "direction":   _direction_from_score(sc),
                "score":       sc,
                "source_db":   "portfolio.db/portfolio_options_analysis",
                "raw_json":    json.dumps(raw),
            }

    return list(best.values())


def get_news_signals(
    tickers: list[str],
    date_from: date,
    date_to: date,
) -> list[dict]:
    """Extract news sentiment signals from portfolio_news_analysis in portfolio.db.

    One signal per row (each row is already a per-ticker analysis snapshot).
    If multiple rows exist for the same (ticker, date), keep the one with
    highest abs(sentiment_score).

    Parameters
    ----------
    tickers   : List of uppercase ticker strings.
    date_from : Start date (inclusive).
    date_to   : End date (inclusive).

    Returns
    -------
    List of signal dicts.
    """
    tickers_upper = [t.upper() for t in tickers]
    placeholders = ",".join("?" for _ in tickers_upper)

    conn = _portfolio_conn()
    try:
        rows = conn.execute(
            f"""
            SELECT ticker, sentiment_score, sentiment_label, impact_level, analyzed_at
            FROM portfolio_news_analysis
            WHERE ticker IN ({placeholders})
            ORDER BY analyzed_at ASC
            """,
            tickers_upper,
        ).fetchall()
    finally:
        conn.close()

    best: dict[tuple, dict] = {}
    for row in rows:
        row_dict = dict(row)
        d = _parse_date(row_dict.get("analyzed_at", ""))
        if not _date_in_range(d, date_from, date_to):
            continue

        raw = {
            "sentiment_score": row_dict.get("sentiment_score", 0.0),
            "sentiment_label": row_dict.get("sentiment_label", "neutral"),
            "impact_level":    row_dict.get("impact_level", 5),
        }
        sc = score_signal("news", raw)
        key = (row_dict["ticker"].upper(), d)
        existing = best.get(key)
        if existing is None or abs(sc) > abs(existing["score"]):
            best[key] = {
                "ticker":      row_dict["ticker"].upper(),
                "signal_date": d,
                "signal_type": "news",
                "direction":   _direction_from_score(sc),
                "score":       sc,
                "source_db":   "portfolio.db/portfolio_news_analysis",
                "raw_json":    json.dumps(raw),
            }

    return list(best.values())


def get_reddit_signals(
    tickers: list[str],
    date_from: date,
    date_to: date,
) -> list[dict]:
    """Extract Reddit WSB sentiment signals from wsb_ticker_summaries in wsb.db.

    Note: wsb_ticker_summaries has one row per ticker (PRIMARY KEY = ticker),
    updated in place. So for most tickers there will be at most one signal
    per ticker across the full date range. Include it if analyzed_at falls
    within the requested window.

    Parameters
    ----------
    tickers   : List of uppercase ticker strings.
    date_from : Start date (inclusive).
    date_to   : End date (inclusive).

    Returns
    -------
    List of signal dicts.
    """
    tickers_upper = [t.upper() for t in tickers]
    placeholders = ",".join("?" for _ in tickers_upper)

    conn = _wsb_conn()
    try:
        rows = conn.execute(
            f"""
            SELECT ticker, sentiment_score, sentiment_label, hype_level, analyzed_at
            FROM wsb_ticker_summaries
            WHERE ticker IN ({placeholders})
            ORDER BY analyzed_at ASC
            """,
            tickers_upper,
        ).fetchall()
    finally:
        conn.close()

    signals = []
    for row in rows:
        row_dict = dict(row)
        d = _parse_date(row_dict.get("analyzed_at", ""))
        if not _date_in_range(d, date_from, date_to):
            continue

        raw = {
            "sentiment_score": row_dict.get("sentiment_score", 0.0),
            "sentiment_label": row_dict.get("sentiment_label", "neutral"),
            "hype_level":      row_dict.get("hype_level", 5),
        }
        sc = score_signal("reddit", raw)
        signals.append({
            "ticker":      row_dict["ticker"].upper(),
            "signal_date": d,
            "signal_type": "reddit",
            "direction":   _direction_from_score(sc),
            "score":       sc,
            "source_db":   "wsb.db/wsb_ticker_summaries",
            "raw_json":    json.dumps(raw),
        })

    return signals


def get_hedge_fund_signals(
    tickers: list[str],
    date_from: date,
    date_to: date,
) -> list[dict]:
    """Extract hedge fund positioning signals from hedge_fund_analysis in portfolio.db.

    The hedge_fund_analysis table stores result_json as a JSON blob keyed by
    portfolio snapshot (ticker_key), not individual tickers. This function
    parses the per-ticker signals embedded in result_json.

    result_json structure (from run_hedge_fund_analysis / hedge_fund_agent.py):
      {
        "per_ticker_signals": {
          "AAPL": {
            "conviction_level": "high"|"medium"|"low",
            "ownership_type": "bullish_equity"|"hedged"|"speculative_put"|"mixed",
            ...
          },
          ...
        },
        "portfolio_signal": {
          "overall_stance": "bullish"|"bearish"|"mixed"|"defensive"
        },
        ...
      }

    Parameters
    ----------
    tickers   : List of uppercase ticker strings.
    date_from : Start date (inclusive).
    date_to   : End date (inclusive).

    Returns
    -------
    List of signal dicts, one per (ticker, analyzed_at) found.
    """
    tickers_upper = set(t.upper() for t in tickers)

    conn = _portfolio_conn()
    try:
        rows = conn.execute(
            """
            SELECT result_json, analyzed_at
            FROM hedge_fund_analysis
            ORDER BY analyzed_at ASC
            """
        ).fetchall()
    finally:
        conn.close()

    signals = []
    for row in rows:
        row_dict = dict(row)
        d = _parse_date(row_dict.get("analyzed_at", ""))
        if not _date_in_range(d, date_from, date_to):
            continue

        try:
            result = json.loads(row_dict.get("result_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue

        per_ticker = result.get("per_ticker_signals", {})
        for ticker, ticker_data in per_ticker.items():
            if ticker.upper() not in tickers_upper:
                continue

            raw = {
                "ownership_type":  ticker_data.get("ownership_type", "mixed"),
                "conviction_level": ticker_data.get("conviction_level", "low"),
            }
            sc = score_signal("hedge_fund", raw)
            signals.append({
                "ticker":      ticker.upper(),
                "signal_date": d,
                "signal_type": "hedge_fund",
                "direction":   _direction_from_score(sc),
                "score":       sc,
                "source_db":   "portfolio.db/hedge_fund_analysis",
                "raw_json":    json.dumps(raw),
            })

    return signals


def get_macro_signals(
    date_from: date,
    date_to: date,
) -> list[dict]:
    """Extract macro news signals from macro_news_analysis in portfolio.db.

    Macro signals are market-wide (not per-ticker) and will be paired with
    SPY forward returns in the backtest engine. The ticker field is set to
    'SPY' for all macro signals.

    For each day in the date range, if multiple macro articles were analyzed,
    aggregate by taking the mean sentiment_score and the most common
    macro_category. This prevents one viral article from generating 20 signals.

    Parameters
    ----------
    date_from : Start date (inclusive).
    date_to   : End date (inclusive).

    Returns
    -------
    List of signal dicts with ticker='SPY'.
    """
    conn = _portfolio_conn()
    try:
        rows = conn.execute(
            """
            SELECT macro_category, sentiment_score, impact_level, affected_sectors, analyzed_at
            FROM macro_news_analysis
            ORDER BY analyzed_at ASC
            """
        ).fetchall()
    finally:
        conn.close()

    # Group by date; keep the average sentiment_score and most common macro_category
    from collections import Counter
    daily: dict[date, list[dict]] = {}
    for row in rows:
        row_dict = dict(row)
        d = _parse_date(row_dict.get("analyzed_at", ""))
        if not _date_in_range(d, date_from, date_to):
            continue
        daily.setdefault(d, []).append(row_dict)

    signals = []
    for d, day_rows in sorted(daily.items()):
        scores_list = [float(r.get("sentiment_score") or 0.0) for r in day_rows]
        categories  = [r.get("macro_category", "neutral") for r in day_rows]
        avg_score   = sum(scores_list) / len(scores_list)
        # Most common macro_category for the day
        top_category = Counter(categories).most_common(1)[0][0]

        raw = {
            "macro_category": top_category,
            "sentiment_score": avg_score,
        }
        sc = score_signal("macro", raw)
        signals.append({
            "ticker":      "SPY",
            "signal_date": d,
            "signal_type": "macro",
            "direction":   _direction_from_score(sc),
            "score":       sc,
            "source_db":   "portfolio.db/macro_news_analysis",
            "raw_json":    json.dumps(raw),
        })

    return signals


def get_mpt_signals(
    tickers: list[str],
    date_from: date,
    date_to: date,
) -> list[dict]:
    """Extract MPT analysis signals from mpt_analysis in portfolio.db.

    mpt_analysis stores result_json (Gemini output) and metrics_json (Python
    pre-computed metrics). Per-ticker signals live inside result_json under
    the 'ticker_analysis' key (a dict keyed by ticker).

    result_json structure (from run_mpt_analysis / mpt_agent.py):
      {
        "ticker_analysis": {
          "AAPL": {
            "recommendation": "increase"|"hold"|"reduce",
            "risk_assessment": "high"|"medium"|"low",
            ...
          },
          ...
        },
        "mpt_analysis": {
          "overall_score": "excellent"|"good"|"fair"|"poor",
          "rebalancing_priority": "urgent"|"moderate"|"low"
        },
        ...
      }

    Parameters
    ----------
    tickers   : List of uppercase ticker strings.
    date_from : Start date (inclusive).
    date_to   : End date (inclusive).

    Returns
    -------
    List of signal dicts, one per (ticker, analyzed_at).
    """
    tickers_upper = set(t.upper() for t in tickers)

    conn = _portfolio_conn()
    try:
        rows = conn.execute(
            """
            SELECT result_json, analyzed_at
            FROM mpt_analysis
            ORDER BY analyzed_at ASC
            """
        ).fetchall()
    finally:
        conn.close()

    signals = []
    for row in rows:
        row_dict = dict(row)
        d = _parse_date(row_dict.get("analyzed_at", ""))
        if not _date_in_range(d, date_from, date_to):
            continue

        try:
            result = json.loads(row_dict.get("result_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue

        ticker_analysis = result.get("ticker_analysis", {})
        for ticker, ta in ticker_analysis.items():
            if ticker.upper() not in tickers_upper:
                continue

            raw = {
                "recommendation": ta.get("recommendation", "hold"),
                "risk_assessment": ta.get("risk_assessment", "medium"),
            }
            sc = score_signal("mpt", raw)
            signals.append({
                "ticker":      ticker.upper(),
                "signal_date": d,
                "signal_type": "mpt",
                "direction":   _direction_from_score(sc),
                "score":       sc,
                "source_db":   "portfolio.db/mpt_analysis",
                "raw_json":    json.dumps(raw),
            })

    return signals


def get_technical_signals(
    tickers: list[str],
    date_from: date,
    date_to: date,
) -> list[dict]:
    """Generate technical pattern signals by replaying PatternDetectionEngine
    on rolling 60-day OHLCV windows across the date range.

    This is the only signal source that computes signals dynamically (no Gemini).
    For each ticker, fetches 2 years of OHLCV from yfinance, then for each
    trading date T in [date_from, date_to], slices the most recent 60 bars
    ending on T, runs PatternDetectionEngine.detect_all(), and emits one signal
    per detected pattern (filtered to confidence_score >= 0.55 to reduce noise).

    To avoid yfinance quota abuse, the full OHLCV frame is fetched once per
    ticker and then sliced in memory.

    Parameters
    ----------
    tickers   : List of uppercase ticker strings.
    date_from : Start date (inclusive).
    date_to   : End date (inclusive).

    Returns
    -------
    List of signal dicts. Multiple patterns may be detected on the same day;
    each becomes its own signal row in backtest_signals.
    """
    signals = []
    _WINDOW = 60         # bars of history fed to the engine per day
    _MIN_CONF = 0.55     # minimum confidence_score to include a pattern

    for ticker in tickers:
        ticker_upper = ticker.upper()
        try:
            df_raw = yf.Ticker(ticker_upper).history(period="2y")
        except Exception:
            continue

        if df_raw is None or df_raw.empty or len(df_raw) < _WINDOW + 1:
            continue

        # Ensure index is tz-naive date
        df_raw.index = pd.to_datetime(df_raw.index).tz_localize(None)
        df_raw = df_raw.sort_index()

        # Collect all trading dates that fall within [date_from, date_to]
        all_dates = [
            ts.date()
            for ts in df_raw.index
            if date_from <= ts.date() <= date_to
        ]

        for signal_dt in all_dates:
            # Slice: all bars up to and including signal_dt
            mask = df_raw.index.date <= signal_dt
            window_df = df_raw[mask].tail(_WINDOW)

            if len(window_df) < _WINDOW:
                continue

            try:
                engine = PatternDetectionEngine(
                    df=window_df,
                    ticker=ticker_upper,
                    timeframe="daily",
                )
                patterns = engine.detect_all()
            except Exception:
                continue

            for pat in patterns:
                if pat.confidence_score < _MIN_CONF:
                    continue
                # Only emit patterns where detected_at_bar is the last bar
                # (i.e., the pattern was detected on this specific day, not stale)
                if pat.detected_at_bar != window_df.index[-1]:
                    continue

                raw = {
                    "pattern_type":    pat.pattern_type,
                    "direction":       pat.direction,
                    "confidence_score": pat.confidence_score,
                    "entry_price":     pat.entry_price,
                    "target":          pat.target,
                    "stop_loss":       pat.stop_loss,
                }
                sc = score_signal("technical", raw)
                if sc == 0.0:
                    continue

                signals.append({
                    "ticker":      ticker_upper,
                    "signal_date": signal_dt,
                    "signal_type": "technical",
                    "direction":   _direction_from_score(sc),
                    "score":       sc,
                    "source_db":   "PatternDetectionEngine/historical_ohlcv",
                    "raw_json":    json.dumps(raw),
                })

    return signals
```

**Why**: This module owns the entire signal extraction contract. Every signal type is normalized to the same dict schema before entering the engine. The technical signal generator is the most complex because it recomputes engine results on rolling windows; the `detected_at_bar == window_df.index[-1]` guard ensures only patterns that are fresh on day T are emitted, preventing stale historical patterns from polluting the signal on the wrong date.

---

### Step 3: Create `stock-dashboard/data/backtest_engine.py`

**File**: `stock-dashboard/data/backtest_engine.py`
**Action**: Create this file with the following exact content:

```python
"""
backtest_engine.py

Core backtest orchestration. Accepts a run configuration, extracts signals
from backtest_signals.py, fetches forward returns from yfinance, scores
all signals against outcomes, computes per-signal-type metrics, and persists
everything to backtest.db.

Public API
----------
run_backtest(run_config: dict) -> str
    Orchestrates a full run and returns the run_id string.

get_run_signals(run_id: str) -> list[dict]
    Returns all signals for a completed run.

get_run_outcomes(run_id: str) -> list[dict]
    Returns all outcomes (joined with their signals) for a completed run.

get_run_metrics(run_id: str) -> list[dict]
    Returns all metrics rows for a completed run.

compute_equity_curve(signals_df: pd.DataFrame, outcomes_df: pd.DataFrame) -> pd.DataFrame
    Returns a date-indexed DataFrame of cumulative portfolio value by signal type.
"""

import json
import os
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import spearmanr

from data.backtest_signals import (
    get_hedge_fund_signals,
    get_macro_signals,
    get_mpt_signals,
    get_news_signals,
    get_options_ai_signals,
    get_reddit_signals,
    get_technical_signals,
)

# ---------------------------------------------------------------------------
# DB path + connection
# ---------------------------------------------------------------------------

_BACKTEST_DB  = os.path.join(os.path.dirname(__file__), "..", "db", "backtest.db")
_SCHEMA_PATH  = os.path.join(os.path.dirname(__file__), "..", "db", "backtest_schema.sql")

_RISK_FREE_DAILY = 0.045 / 252  # 4.5% annual risk-free rate, dailized


def _get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_BACKTEST_DB), exist_ok=True)
    conn = sqlite3.connect(_BACKTEST_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create backtest.db tables from backtest_schema.sql if they don't exist."""
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
# Forward return fetching
# ---------------------------------------------------------------------------

def fetch_forward_returns(
    ticker: str,
    signal_dates: list[date],
    horizon_days: int,
    ohlcv_df: Optional[pd.DataFrame] = None,
) -> dict[date, float]:
    """Compute lookahead-safe forward returns for each signal date.

    Lookahead prevention protocol:
      - Entry price: first available OPEN on T+1 (day after signal date)
      - Exit price: CLOSE on T+N (where N = horizon_days trading days after entry)
      - Forward return = (exit_close - entry_open) / entry_open
      - Signal date itself (T) is NEVER used as entry or exit

    Parameters
    ----------
    ticker       : Uppercase ticker string.
    signal_dates : List of date objects for which to compute forward returns.
    horizon_days : Number of trading days after T+1 open to measure exit.
    ohlcv_df     : Optional pre-fetched OHLCV DataFrame. If None, fetches from yfinance.

    Returns
    -------
    Dict mapping each signal_date to its forward_return float.
    Returns empty dict if data fetch fails. Dates with insufficient forward
    data (e.g., too close to today) are omitted from the result.
    """
    if ohlcv_df is None:
        try:
            df = yf.Ticker(ticker.upper()).history(period="2y")
        except Exception:
            return {}
        if df is None or df.empty:
            return {}
    else:
        df = ohlcv_df.copy()

    # Normalize index to tz-naive
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    df.columns = [c.lower() for c in df.columns]

    result: dict[date, float] = {}
    trading_dates = [ts.date() for ts in df.index]

    for sig_date in signal_dates:
        # Find index of first trading day AFTER signal date (T+1)
        try:
            entry_idx = next(
                i for i, d in enumerate(trading_dates) if d > sig_date
            )
        except StopIteration:
            continue  # No trading day after signal date

        # Find index of T+N exit (horizon_days trading days after entry)
        exit_idx = entry_idx + horizon_days
        if exit_idx >= len(trading_dates):
            continue  # Not enough forward data

        entry_price = float(df.iloc[entry_idx]["open"])
        exit_price  = float(df.iloc[exit_idx]["close"])

        if entry_price <= 0:
            continue

        fwd_return = (exit_price - entry_price) / entry_price
        result[sig_date] = round(fwd_return, 6)

    return result


def fetch_spy_returns(
    signal_dates: list[date],
    horizon_days: int,
) -> dict[date, float]:
    """Compute SPY forward returns for the same signal dates and horizon.

    Uses the same lookahead-safe protocol as fetch_forward_returns().

    Parameters
    ----------
    signal_dates : List of date objects.
    horizon_days : Number of trading days for the forward window.

    Returns
    -------
    Dict mapping each signal_date to its SPY forward_return.
    """
    try:
        df = yf.Ticker("SPY").history(period="2y")
    except Exception:
        return {}
    if df is None or df.empty:
        return {}

    return fetch_forward_returns("SPY", signal_dates, horizon_days, ohlcv_df=df)


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------

def compute_ic(
    scores: list[float],
    forward_returns: list[float],
) -> tuple[float, float]:
    """Compute the Information Coefficient (IC) as Spearman rank correlation.

    IC is the Spearman rank correlation between signal scores and forward returns.
    IC > 0.05 is meaningful; IC > 0.10 is strong.

    Parameters
    ----------
    scores          : List of numeric signal scores in [-1, 1].
    forward_returns : Corresponding list of forward return floats.

    Returns
    -------
    Tuple of (ic, ic_std) where:
      ic      = Spearman rank correlation coefficient
      ic_std  = Standard deviation of IC (approximated as sqrt((1 - ic^2) / (n - 2)))
                for single-period IC; returns 0.0 if n < 3.
    """
    n = len(scores)
    if n < 3:
        return 0.0, 0.0

    corr, _ = spearmanr(scores, forward_returns)
    ic = float(corr) if not np.isnan(corr) else 0.0

    # Approximate standard error of Spearman rho
    if n > 2:
        ic_std = float(np.sqrt((1.0 - ic ** 2) / max(1, n - 2)))
    else:
        ic_std = 0.0

    return round(ic, 6), round(ic_std, 6)


def compute_hit_rate(
    directions: list[str],
    forward_returns: list[float],
) -> float:
    """Compute directional hit rate, excluding neutral signals.

    Hit = 1 if (direction == 'bullish' and forward_return > 0)
               OR (direction == 'bearish' and forward_return < 0)
    Hit = 0 otherwise.
    Neutral signals are excluded from the denominator.

    Parameters
    ----------
    directions      : List of 'bullish'|'bearish'|'neutral' strings.
    forward_returns : Corresponding forward return floats.

    Returns
    -------
    Hit rate as a float in [0, 1]. Returns 0.0 if no directional signals.
    """
    hits = 0
    total = 0
    for d, r in zip(directions, forward_returns):
        if d == "neutral":
            continue
        total += 1
        if d == "bullish" and r > 0:
            hits += 1
        elif d == "bearish" and r < 0:
            hits += 1

    return round(hits / total, 4) if total > 0 else 0.0


def compute_profit_factor(
    directions: list[str],
    forward_returns: list[float],
) -> float:
    """Compute profit factor: gross_profit / gross_loss from directional signals.

    Profit  = sum of |forward_return| where direction was correct
    Loss    = sum of |forward_return| where direction was wrong
    Neutral signals are excluded.
    Returns 0.0 if no losses (undefined); returns gross_profit / gross_loss otherwise.

    Parameters
    ----------
    directions      : List of 'bullish'|'bearish'|'neutral' strings.
    forward_returns : Corresponding forward return floats.

    Returns
    -------
    Profit factor as a float. Returns float('inf') if there are profits but no losses.
    Returns 0.0 if no directional signals.
    """
    gross_profit = 0.0
    gross_loss   = 0.0

    for d, r in zip(directions, forward_returns):
        if d == "neutral":
            continue
        correct = (d == "bullish" and r > 0) or (d == "bearish" and r < 0)
        if correct:
            gross_profit += abs(r)
        else:
            gross_loss += abs(r)

    if gross_loss == 0.0:
        return float("inf") if gross_profit > 0 else 0.0
    return round(gross_profit / gross_loss, 4)


def compute_equity_curve(
    signals_df: pd.DataFrame,
    outcomes_df: pd.DataFrame,
) -> pd.DataFrame:
    """Simulate a $10,000 portfolio following each signal type and return equity curves.

    Trading rules for the simulation:
      - Start with $10,000 per signal type.
      - On each signal date T, the strategy enters at T+1 open and exits at T+N close
        (forward return already computed in outcomes_df).
      - Each trade invests the entire current portfolio value / N_concurrent_signals
        (equal-weighted among all signals active on that date for a given type).
      - Simplified: treat each signal independently (size = $10,000 / total_signals).
        Sum the position-level gains to get total portfolio change per signal type per date.
      - Neutral signals are excluded (do not trade).
      - If direction == 'bullish' → long → gain = forward_return
      - If direction == 'bearish' → short → gain = -forward_return
      - SPY buy-and-hold: compute SPY's compound return over the same date range
        by chaining (1 + spy_daily_return) for each day.

    Parameters
    ----------
    signals_df  : DataFrame with columns [id, run_id, ticker, signal_date,
                  signal_type, direction, score, source_db, raw_json]
    outcomes_df : DataFrame with columns [id, signal_id, forward_return,
                  benchmark_return, alpha, price_at_signal, price_at_horizon, correct]

    Returns
    -------
    DataFrame with columns: date (datetime.date), signal_type (str), portfolio_value (float).
    Sorted by signal_type, date. Also includes a 'SPY' row per date for the benchmark.
    """
    if signals_df.empty or outcomes_df.empty:
        return pd.DataFrame(columns=["date", "signal_type", "portfolio_value"])

    # Join signals and outcomes
    merged = signals_df.merge(
        outcomes_df,
        left_on="id",
        right_on="signal_id",
        how="inner",
        suffixes=("_sig", "_out"),
    )

    # Exclude rows with missing forward_return or neutral direction
    merged = merged[
        merged["forward_return"].notna() &
        (merged["direction"] != "neutral")
    ].copy()

    if merged.empty:
        return pd.DataFrame(columns=["date", "signal_type", "portfolio_value"])

    # Ensure signal_date is a date object
    merged["signal_date"] = pd.to_datetime(merged["signal_date"]).dt.date

    signal_types = merged["signal_type"].unique().tolist()
    all_signal_dates = sorted(merged["signal_date"].unique().tolist())

    records = []
    _INITIAL_CAPITAL = 10_000.0

    for stype in signal_types:
        subset = merged[merged["signal_type"] == stype].sort_values("signal_date")
        if subset.empty:
            continue

        total_trades = len(subset)
        if total_trades == 0:
            continue

        # Equal-sized allocation per trade
        trade_size = _INITIAL_CAPITAL / total_trades
        portfolio_value = _INITIAL_CAPITAL

        cum_records = []
        running = _INITIAL_CAPITAL

        for _, trade_row in subset.iterrows():
            fwd = float(trade_row["forward_return"])
            direction = trade_row["direction"]

            # P&L per trade: long if bullish, short if bearish
            if direction == "bullish":
                pnl = trade_size * fwd
            else:  # bearish
                pnl = trade_size * (-fwd)

            running += pnl
            cum_records.append({
                "date":           trade_row["signal_date"],
                "signal_type":    stype,
                "portfolio_value": round(running, 4),
            })

        records.extend(cum_records)

    # SPY benchmark: chain daily SPY returns over the signal date range
    # Use benchmark_return from the first signal type as the SPY series
    spy_rows = merged[merged["direction"] != "neutral"].drop_duplicates(
        subset=["signal_date"]
    ).sort_values("signal_date")[["signal_date", "benchmark_return"]].dropna()

    if not spy_rows.empty:
        spy_value = _INITIAL_CAPITAL
        for _, spy_row in spy_rows.iterrows():
            spy_return = float(spy_row["benchmark_return"])
            spy_value *= (1.0 + spy_return)
            records.append({
                "date":           spy_row["signal_date"],
                "signal_type":    "SPY",
                "portfolio_value": round(spy_value, 4),
            })

    if not records:
        return pd.DataFrame(columns=["date", "signal_type", "portfolio_value"])

    result = pd.DataFrame(records)
    result = result.sort_values(["signal_type", "date"]).reset_index(drop=True)
    return result


def _compute_sharpe(returns: list[float]) -> float:
    """Compute annualized Sharpe ratio for a series of per-trade returns.

    Assumes each return represents one period of horizon_days trading days.
    Annualizes by multiplying daily Sharpe by sqrt(252).

    Parameters
    ----------
    returns : List of forward return floats (not compounded).

    Returns
    -------
    Annualized Sharpe ratio. Returns 0.0 if std == 0 or len < 2.
    """
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns, dtype=float)
    mean_r = np.mean(arr)
    std_r  = np.std(arr, ddof=1)
    if std_r == 0.0:
        return 0.0
    sharpe_per_period = (mean_r - _RISK_FREE_DAILY) / std_r
    # Annualize (each return spans horizon_days; 252/horizon_days periods/year)
    return round(float(sharpe_per_period * np.sqrt(252)), 4)


def _compute_max_drawdown(equity_curve: list[float]) -> float:
    """Compute maximum peak-to-trough drawdown from an equity curve.

    Parameters
    ----------
    equity_curve : List of portfolio values in chronological order.

    Returns
    -------
    Maximum drawdown as a negative float in (-inf, 0]. E.g., -0.25 = 25% drawdown.
    Returns 0.0 if curve has fewer than 2 points.
    """
    if len(equity_curve) < 2:
        return 0.0
    arr = np.array(equity_curve, dtype=float)
    peak = arr[0]
    max_dd = 0.0
    for val in arr[1:]:
        if val > peak:
            peak = val
        dd = (val - peak) / peak if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd
    return round(float(max_dd), 6)


# ---------------------------------------------------------------------------
# Score (compute metrics for a completed run)
# ---------------------------------------------------------------------------

def score_run(run_id: str) -> dict:
    """Compute all metrics for a completed run and write them to backtest_metrics.

    Reads backtest_signals and backtest_outcomes from backtest.db for the given
    run_id, computes per-signal-type metrics, writes to backtest_metrics, and
    returns a summary dict.

    Parameters
    ----------
    run_id : The UUID string identifying the run.

    Returns
    -------
    Dict with keys: run_id, metrics (list of metric dicts, one per signal_type).
    """
    conn = _get_connection()
    try:
        run_row = conn.execute(
            "SELECT * FROM backtest_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run_row is None:
            return {"error": f"No run found with id {run_id}"}

        horizon_days = int(dict(run_row)["horizon_days"])

        sig_rows = conn.execute(
            "SELECT * FROM backtest_signals WHERE run_id = ?", (run_id,)
        ).fetchall()
        out_rows = conn.execute(
            """
            SELECT o.*
            FROM backtest_outcomes o
            JOIN backtest_signals s ON s.id = o.signal_id
            WHERE s.run_id = ?
            """,
            (run_id,),
        ).fetchall()
    finally:
        conn.close()

    if not sig_rows or not out_rows:
        return {"run_id": run_id, "metrics": []}

    signals_df  = pd.DataFrame([dict(r) for r in sig_rows])
    outcomes_df = pd.DataFrame([dict(r) for r in out_rows])

    # Merge signals with outcomes on id -> signal_id
    merged = signals_df.merge(
        outcomes_df,
        left_on="id",
        right_on="signal_id",
        how="inner",
        suffixes=("_sig", "_out"),
    )

    # Only include rows with a valid forward_return
    merged = merged[merged["forward_return"].notna()].copy()

    _MIN_SIGNALS = 5  # Must have at least 5 valid signals to compute metrics
    metrics_list = []

    for stype in merged["signal_type"].unique():
        sdf = merged[merged["signal_type"] == stype]
        n   = len(sdf)
        if n < _MIN_SIGNALS:
            metrics_list.append({
                "signal_type":  stype,
                "signal_count": n,
                "insufficient": True,
            })
            continue

        fwd_returns = sdf["forward_return"].tolist()
        scores      = sdf["score"].tolist()
        directions  = sdf["direction"].tolist()
        alphas      = sdf["alpha"].dropna().tolist()

        # IC
        ic, ic_std = compute_ic(scores, fwd_returns)
        icir = round(ic / ic_std, 4) if ic_std > 0 else 0.0
        t_stat = round(ic / (ic_std / np.sqrt(n)), 4) if ic_std > 0 else 0.0

        # Hit rate
        hit_rate = compute_hit_rate(directions, fwd_returns)

        # Average returns by direction
        bull_rets = [r for d, r in zip(directions, fwd_returns) if d == "bullish"]
        bear_rets = [r for d, r in zip(directions, fwd_returns) if d == "bearish"]
        neut_rets = [r for d, r in zip(directions, fwd_returns) if d == "neutral"]

        avg_bull = round(float(np.mean(bull_rets)), 6) if bull_rets else None
        avg_bear = round(float(np.mean(bear_rets)), 6) if bear_rets else None
        avg_neut = round(float(np.mean(neut_rets)), 6) if neut_rets else None

        # Profit factor
        pf = compute_profit_factor(directions, fwd_returns)

        # Alpha
        avg_alpha = round(float(np.mean(alphas)), 6) if alphas else None

        # Sharpe (on all directional returns as if long/short)
        directional_pnls = []
        for d, r in zip(directions, fwd_returns):
            if d == "bullish":
                directional_pnls.append(r)
            elif d == "bearish":
                directional_pnls.append(-r)
        sharpe = _compute_sharpe(directional_pnls)

        # Max drawdown from equity curve
        equity_curve_df = compute_equity_curve(signals_df, outcomes_df)
        stype_curve = equity_curve_df[
            equity_curve_df["signal_type"] == stype
        ]["portfolio_value"].tolist()
        max_dd = _compute_max_drawdown(stype_curve) if stype_curve else 0.0

        row = {
            "run_id":          run_id,
            "signal_type":     stype,
            "horizon_days":    horizon_days,
            "signal_count":    n,
            "hit_rate":        hit_rate,
            "avg_return_bull": avg_bull,
            "avg_return_bear": avg_bear,
            "avg_return_neutral": avg_neut,
            "ic":              ic,
            "ic_std":          ic_std,
            "icir":            icir,
            "t_stat":          t_stat,
            "profit_factor":   pf if pf != float("inf") else 9999.0,
            "avg_alpha":       avg_alpha,
            "sharpe":          sharpe,
            "max_drawdown":    max_dd,
        }
        metrics_list.append(row)

    # Write to backtest_metrics (replace any prior run's metrics rows)
    conn = _get_connection()
    try:
        conn.execute(
            "DELETE FROM backtest_metrics WHERE run_id = ?", (run_id,)
        )
        for m in metrics_list:
            if m.get("insufficient"):
                continue
            conn.execute(
                """
                INSERT INTO backtest_metrics (
                    run_id, signal_type, horizon_days, signal_count,
                    hit_rate, avg_return_bull, avg_return_bear, avg_return_neutral,
                    ic, ic_std, icir, t_stat, profit_factor, avg_alpha,
                    sharpe, max_drawdown
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    m["signal_type"],
                    m["horizon_days"],
                    m["signal_count"],
                    m["hit_rate"],
                    m["avg_return_bull"],
                    m["avg_return_bear"],
                    m["avg_return_neutral"],
                    m["ic"],
                    m["ic_std"],
                    m["icir"],
                    m["t_stat"],
                    m["profit_factor"],
                    m["avg_alpha"],
                    m["sharpe"],
                    m["max_drawdown"],
                ),
            )
        conn.commit()
    finally:
        conn.close()

    return {"run_id": run_id, "metrics": metrics_list}


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_backtest(run_config: dict) -> str:
    """Orchestrate a full backtest run and return the run_id.

    Parameters
    ----------
    run_config : Dict with keys:
        tickers      : list[str] — uppercase ticker strings
        date_from    : datetime.date — start date
        date_to      : datetime.date — end date
        horizon_days : int — 1|5|10|20
        signal_types : list[str] — subset of ['options_ai','news','reddit',
                       'hedge_fund','macro','mpt','technical']

    Returns
    -------
    run_id string (UUID4). Raises ValueError on invalid config.

    Side effects
    ------------
    Writes rows to backtest_runs, backtest_signals, backtest_outcomes,
    and backtest_metrics in backtest.db.
    """
    tickers      = [t.upper() for t in run_config["tickers"]]
    date_from    = run_config["date_from"]
    date_to      = run_config["date_to"]
    horizon_days = int(run_config["horizon_days"])
    signal_types = run_config["signal_types"]

    if not tickers:
        raise ValueError("At least one ticker is required.")
    if date_from > date_to:
        raise ValueError("date_from must be <= date_to.")
    if horizon_days not in (1, 5, 10, 20):
        raise ValueError("horizon_days must be 1, 5, 10, or 20.")

    run_id   = str(uuid.uuid4())
    now_iso  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    # Persist the run metadata
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO backtest_runs (run_id, tickers, date_from, date_to,
                                       horizon_days, signal_types, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                json.dumps(tickers),
                date_from.isoformat(),
                date_to.isoformat(),
                horizon_days,
                json.dumps(signal_types),
                now_iso,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # ── Step 1: Gather all signals ──────────────────────────────────────────
    all_signals: list[dict] = []

    if "options_ai" in signal_types:
        all_signals.extend(get_options_ai_signals(tickers, date_from, date_to))

    if "news" in signal_types:
        all_signals.extend(get_news_signals(tickers, date_from, date_to))

    if "reddit" in signal_types:
        all_signals.extend(get_reddit_signals(tickers, date_from, date_to))

    if "hedge_fund" in signal_types:
        all_signals.extend(get_hedge_fund_signals(tickers, date_from, date_to))

    if "macro" in signal_types:
        all_signals.extend(get_macro_signals(date_from, date_to))

    if "mpt" in signal_types:
        all_signals.extend(get_mpt_signals(tickers, date_from, date_to))

    if "technical" in signal_types:
        all_signals.extend(get_technical_signals(tickers, date_from, date_to))

    if not all_signals:
        return run_id  # No signals found; run exists but will be empty

    # ── Step 2: Persist signals ─────────────────────────────────────────────
    signal_id_map: dict[int, dict] = {}  # row insertion order -> signal dict

    conn = _get_connection()
    try:
        for sig in all_signals:
            cursor = conn.execute(
                """
                INSERT INTO backtest_signals
                    (run_id, ticker, signal_date, signal_type, direction,
                     score, source_db, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    sig["ticker"],
                    sig["signal_date"].isoformat(),
                    sig["signal_type"],
                    sig["direction"],
                    sig["score"],
                    sig.get("source_db", ""),
                    sig.get("raw_json", "{}"),
                ),
            )
            inserted_id = cursor.lastrowid
            signal_id_map[inserted_id] = sig
        conn.commit()
    finally:
        conn.close()

    # ── Step 3: Fetch forward returns ───────────────────────────────────────
    # Group signal dates by ticker to minimize yfinance calls (one fetch per ticker)
    from collections import defaultdict
    ticker_to_dates: dict[str, list[date]] = defaultdict(list)
    for sig_id, sig in signal_id_map.items():
        ticker_to_dates[sig["ticker"]].append(sig["signal_date"])

    # Fetch OHLCV once per ticker
    ticker_ohlcv: dict[str, pd.DataFrame] = {}
    for ticker in ticker_to_dates:
        try:
            df = yf.Ticker(ticker).history(period="2y")
            if df is not None and not df.empty:
                df.index = pd.to_datetime(df.index).tz_localize(None)
                df.columns = [c.lower() for c in df.columns]
                ticker_ohlcv[ticker] = df
        except Exception:
            pass

    # Fetch SPY once
    spy_ohlcv: Optional[pd.DataFrame] = None
    try:
        spy_df = yf.Ticker("SPY").history(period="2y")
        if spy_df is not None and not spy_df.empty:
            spy_df.index = pd.to_datetime(spy_df.index).tz_localize(None)
            spy_df.columns = [c.lower() for c in spy_df.columns]
            spy_ohlcv = spy_df
    except Exception:
        pass

    # Compute forward returns per ticker
    ticker_fwd_returns: dict[str, dict[date, float]] = {}
    for ticker, dates in ticker_to_dates.items():
        ohlcv = ticker_ohlcv.get(ticker)
        fwd = fetch_forward_returns(ticker, list(set(dates)), horizon_days, ohlcv_df=ohlcv)
        ticker_fwd_returns[ticker] = fwd

    # Compute SPY forward returns for all unique signal dates
    all_signal_dates = list(set(sig["signal_date"] for sig in signal_id_map.values()))
    spy_fwd_returns = fetch_spy_returns(all_signal_dates, horizon_days) if spy_ohlcv is None else \
        fetch_forward_returns("SPY", all_signal_dates, horizon_days, ohlcv_df=spy_ohlcv)

    # ── Step 4: Persist outcomes ────────────────────────────────────────────
    conn = _get_connection()
    try:
        for sig_id, sig in signal_id_map.items():
            ticker     = sig["ticker"]
            sig_date   = sig["signal_date"]
            direction  = sig["direction"]

            fwd_return = ticker_fwd_returns.get(ticker, {}).get(sig_date)
            bench_ret  = spy_fwd_returns.get(sig_date)
            alpha      = (fwd_return - bench_ret) if (fwd_return is not None and bench_ret is not None) else None

            # price_at_signal: close price on signal date T
            price_at_signal  = None
            price_at_horizon = None
            ohlcv = ticker_ohlcv.get(ticker)
            if ohlcv is not None:
                sig_date_ts = pd.Timestamp(sig_date)
                exact_rows = ohlcv[ohlcv.index.date == sig_date]
                if not exact_rows.empty:
                    price_at_signal = float(exact_rows.iloc[-1]["close"])

                # horizon price: close on T+N trading days after T+1 open
                trading_dates = [ts.date() for ts in ohlcv.index]
                try:
                    entry_idx = next(i for i, d in enumerate(trading_dates) if d > sig_date)
                    exit_idx  = entry_idx + horizon_days
                    if exit_idx < len(trading_dates):
                        price_at_horizon = float(ohlcv.iloc[exit_idx]["close"])
                except StopIteration:
                    pass

            # correct: 1 if direction matched, 0 if not, NULL if neutral
            if direction == "neutral" or fwd_return is None:
                correct = None
            elif direction == "bullish" and fwd_return > 0:
                correct = 1
            elif direction == "bearish" and fwd_return < 0:
                correct = 1
            else:
                correct = 0

            conn.execute(
                """
                INSERT INTO backtest_outcomes
                    (signal_id, forward_return, benchmark_return, alpha,
                     price_at_signal, price_at_horizon, correct)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sig_id,
                    fwd_return,
                    bench_ret,
                    alpha,
                    price_at_signal,
                    price_at_horizon,
                    correct,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    # ── Step 5: Compute and persist metrics ─────────────────────────────────
    score_run(run_id)

    return run_id


# ---------------------------------------------------------------------------
# Read helpers (used by page)
# ---------------------------------------------------------------------------

def get_run_signals(run_id: str) -> list[dict]:
    """Return all backtest_signals rows for a run as a list of dicts."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM backtest_signals WHERE run_id = ?", (run_id,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_run_outcomes(run_id: str) -> list[dict]:
    """Return backtest_outcomes joined with backtest_signals for a run."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT s.id AS signal_id, s.ticker, s.signal_date, s.signal_type,
                   s.direction, s.score, s.raw_json,
                   o.forward_return, o.benchmark_return, o.alpha,
                   o.price_at_signal, o.price_at_horizon, o.correct
            FROM backtest_signals s
            JOIN backtest_outcomes o ON o.signal_id = s.id
            WHERE s.run_id = ?
            ORDER BY s.signal_date ASC
            """,
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_run_metrics(run_id: str) -> list[dict]:
    """Return all backtest_metrics rows for a run as a list of dicts."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM backtest_metrics WHERE run_id = ?", (run_id,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def list_recent_runs(limit: int = 20) -> list[dict]:
    """Return the most recent N backtest runs as a list of dicts."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
```

**Why**: This is the core computation engine. It separates concerns: signal extraction (delegated to `backtest_signals.py`), forward return computation (yfinance, one fetch per ticker), metric math (IC, hit rate, Sharpe, etc.), and persistence. The `run_backtest()` function writes to SQLite at each step (runs → signals → outcomes → metrics) so that a partial run can be inspected if it fails midway. The `compute_equity_curve()` function simulates a simplistic equal-weighted long/short strategy following each signal type for the equity curve tab.

---

### Step 4: Create `stock-dashboard/pages/10_backtest.py`

**File**: `stock-dashboard/pages/10_backtest.py`
**Action**: Create this file with the following exact content:

```python
# pages/10_backtest.py

import json
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from components.gemini_usage_bar import render_gemini_usage_bar
from data.backtest_engine import (
    compute_equity_curve,
    get_run_metrics,
    get_run_outcomes,
    get_run_signals,
    list_recent_runs,
    run_backtest,
)

st.set_page_config(page_title="AI Signal Backtester", layout="wide")

render_gemini_usage_bar()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALL_SIGNAL_TYPES = [
    "options_ai",
    "news",
    "reddit",
    "hedge_fund",
    "macro",
    "mpt",
    "technical",
]

_SIGNAL_LABELS = {
    "options_ai":  "Options AI",
    "news":        "News Sentiment",
    "reddit":      "Reddit WSB",
    "hedge_fund":  "Hedge Fund",
    "macro":       "Macro News",
    "mpt":         "MPT Analysis",
    "technical":   "Technical Patterns",
    "SPY":         "SPY Benchmark",
}

_HORIZON_OPTIONS = {
    "1 day":   1,
    "5 days":  5,
    "10 days": 10,
    "20 days": 20,
}

_EQUITY_COLORS = {
    "options_ai":  "#2196F3",
    "news":        "#4CAF50",
    "reddit":      "#FF9800",
    "hedge_fund":  "#9C27B0",
    "macro":       "#F44336",
    "mpt":         "#00BCD4",
    "technical":   "#8BC34A",
    "SPY":         "#607D8B",
}

_MIN_SIGNALS_DISPLAY = 5  # below this count, show "Insufficient data"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_default_tickers() -> list[str]:
    """Try to read portfolio tickers from Webull. Fall back to empty list."""
    try:
        from data.webull_positions import get_env_account_ids, get_positions, is_configured
        if not is_configured():
            return []
        account_ids = get_env_account_ids()
        if not account_ids:
            return []
        positions = get_positions(account_ids[0])
        if not positions or isinstance(positions, dict):
            return []
        _TICKER_FIELDS = ["symbol", "ticker", "tickerSymbol", "stockSymbol", "sym"]
        tickers = []
        for pos in positions:
            for field in _TICKER_FIELDS:
                val = pos.get(field, "")
                if val and isinstance(val, str):
                    tickers.append(val.upper().strip())
                    break
        return list(dict.fromkeys(tickers))  # deduplicate preserving order
    except Exception:
        return []


def _pct(val: float | None) -> str:
    """Format a float as a percentage string."""
    if val is None:
        return "N/A"
    return f"{val * 100:.2f}%"


def _fmt(val: float | None, decimals: int = 4) -> str:
    """Format a float with given decimal places."""
    if val is None:
        return "N/A"
    return f"{val:.{decimals}f}"


def _significant_label(t_stat: float | None) -> str:
    """Return a significance label based on t-statistic."""
    if t_stat is None:
        return "N/A"
    abs_t = abs(t_stat)
    if abs_t >= 2.576:
        return "Yes (99%)"
    elif abs_t >= 1.960:
        return "Yes (95%)"
    elif abs_t >= 1.645:
        return "Marginal (90%)"
    return "No"


# ---------------------------------------------------------------------------
# Cached data fetchers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3600)
def _cached_forward_returns_check(tickers: tuple[str, ...]) -> bool:
    """Lightweight check that yfinance can fetch data for at least one ticker."""
    import yfinance as yf
    for ticker in tickers:
        try:
            df = yf.Ticker(ticker).history(period="5d")
            if df is not None and not df.empty:
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Render functions
# ---------------------------------------------------------------------------

def _render_sidebar() -> dict | None:
    """Render sidebar controls. Returns run_config dict if user clicked Run, else None."""
    with st.sidebar:
        st.header("Backtest Configuration")

        # Tickers input
        default_tickers = _get_default_tickers()
        tickers_input = st.text_input(
            label="Tickers (comma-separated)",
            value=",".join(default_tickers) if default_tickers else "AAPL,MSFT,NVDA,SPY",
            help="Enter one or more uppercase ticker symbols separated by commas.",
            key="bt_tickers_input",
        )
        tickers = [t.strip().upper() for t in tickers_input.split(",") if t.strip()]

        # Date range
        st.subheader("Date Range")
        today = date.today()
        default_from = today - timedelta(days=90)
        date_from = st.date_input(
            label="From",
            value=default_from,
            max_value=today - timedelta(days=2),
            key="bt_date_from",
        )
        date_to = st.date_input(
            label="To",
            value=today - timedelta(days=1),
            min_value=date_from,
            max_value=today - timedelta(days=1),
            key="bt_date_to",
        )

        # Forward horizon
        horizon_label = st.radio(
            label="Forward Horizon",
            options=list(_HORIZON_OPTIONS.keys()),
            index=1,  # default: 5 days
            key="bt_horizon",
            help="Number of trading days after signal date to measure price return.",
        )
        horizon_days = _HORIZON_OPTIONS[horizon_label]

        # Signal type selection
        st.subheader("Signal Types")
        selected_types = []
        for stype in _ALL_SIGNAL_TYPES:
            checked = st.checkbox(
                label=_SIGNAL_LABELS[stype],
                value=True,
                key=f"bt_stype_{stype}",
            )
            if checked:
                selected_types.append(stype)

        st.divider()

        # Recent runs
        st.subheader("Recent Runs")
        recent = list_recent_runs(limit=10)
        if recent:
            run_options = {
                f"{r['created_at'][:16]} | {r['horizon_days']}d | {json.loads(r['tickers'])[:3]}": r["run_id"]
                for r in recent
            }
            selected_label = st.selectbox(
                label="Load a previous run",
                options=["(new run)"] + list(run_options.keys()),
                index=0,
                key="bt_prev_run",
            )
            if selected_label != "(new run)":
                prev_run_id = run_options[selected_label]
                if st.button("Load Run", key="bt_load_run"):
                    st.session_state["bt_current_run_id"] = prev_run_id
                    st.rerun()
        else:
            st.caption("No previous runs yet.")

        st.divider()

        run_clicked = st.button(
            label="Run Backtest",
            type="primary",
            use_container_width=True,
            key="bt_run_button",
            disabled=len(tickers) == 0 or len(selected_types) == 0,
        )

        if run_clicked:
            return {
                "tickers":      tickers,
                "date_from":    date_from,
                "date_to":      date_to,
                "horizon_days": horizon_days,
                "signal_types": selected_types,
            }

    return None


def _render_overview_tab(metrics: list[dict], run_config_display: dict) -> None:
    """Render the Overview tab: summary KPI cards + metrics table."""
    if not metrics:
        st.info("No metrics computed. This may mean fewer than 5 signals were found for all signal types, or no signals exist in the selected date range.")
        return

    # Filter to rows with sufficient data
    valid = [m for m in metrics if not m.get("insufficient", False)]
    insuf = [m for m in metrics if m.get("insufficient", False)]

    if not valid:
        st.warning("All signal types had fewer than 5 signals in the selected window. Broaden the date range or add more signal types.")
        return

    # KPI summary cards
    best_by_ic = max(valid, key=lambda m: m.get("ic", 0.0) or 0.0)
    worst_by_ic = min(valid, key=lambda m: m.get("ic", 0.0) or 0.0)
    best_by_hit = max(valid, key=lambda m: m.get("hit_rate", 0.0) or 0.0)

    all_ics = [m["ic"] for m in valid if m.get("ic") is not None]
    composite_ic = round(float(np.mean(all_ics)), 4) if all_ics else None

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric(
            label="Total Signals",
            value=sum(m.get("signal_count", 0) for m in valid),
        )
    with c2:
        st.metric(
            label="Best IC",
            value=f"{best_by_ic.get('ic', 0):.4f}",
            help=f"Signal type: {_SIGNAL_LABELS.get(best_by_ic['signal_type'], best_by_ic['signal_type'])}",
        )
    with c3:
        st.metric(
            label="Best Hit Rate",
            value=_pct(best_by_hit.get("hit_rate")),
            help=f"Signal type: {_SIGNAL_LABELS.get(best_by_hit['signal_type'], best_by_hit['signal_type'])}",
        )
    with c4:
        st.metric(
            label="Composite IC",
            value=f"{composite_ic:.4f}" if composite_ic is not None else "N/A",
            help="Mean IC across all valid signal types.",
        )

    st.divider()

    # Metrics table
    table_rows = []
    for m in valid:
        label = _SIGNAL_LABELS.get(m["signal_type"], m["signal_type"])
        table_rows.append({
            "Signal Type":       label,
            "N Signals":         m.get("signal_count", 0),
            "Hit Rate":          _pct(m.get("hit_rate")),
            "IC":                _fmt(m.get("ic"), 4),
            "ICIR":              _fmt(m.get("icir"), 4),
            "t-stat":            _fmt(m.get("t_stat"), 2),
            "Significant?":      _significant_label(m.get("t_stat")),
            "Avg Bull Return":   _pct(m.get("avg_return_bull")),
            "Avg Bear Return":   _pct(m.get("avg_return_bear")),
            "Profit Factor":     _fmt(m.get("profit_factor"), 2),
            "Avg Alpha":         _pct(m.get("avg_alpha")),
            "Sharpe":            _fmt(m.get("sharpe"), 2),
            "Max Drawdown":      _pct(m.get("max_drawdown")),
        })

    if insuf:
        for m in insuf:
            label = _SIGNAL_LABELS.get(m["signal_type"], m["signal_type"])
            table_rows.append({
                "Signal Type":      label,
                "N Signals":        m.get("signal_count", 0),
                "Hit Rate":         "Insufficient data",
                "IC":               "Insufficient data",
                "ICIR":             "Insufficient data",
                "t-stat":           "Insufficient data",
                "Significant?":     "Insufficient data",
                "Avg Bull Return":  "Insufficient data",
                "Avg Bear Return":  "Insufficient data",
                "Profit Factor":    "Insufficient data",
                "Avg Alpha":        "Insufficient data",
                "Sharpe":           "Insufficient data",
                "Max Drawdown":     "Insufficient data",
            })

    st.dataframe(
        pd.DataFrame(table_rows),
        use_container_width=True,
        hide_index=True,
    )


def _render_equity_tab(signals_list: list[dict], outcomes_list: list[dict]) -> None:
    """Render the Equity Curves tab."""
    if not signals_list or not outcomes_list:
        st.info("No signal/outcome data to plot equity curves.")
        return

    signals_df  = pd.DataFrame(signals_list)
    outcomes_df = pd.DataFrame(outcomes_list)

    equity_df = compute_equity_curve(signals_df, outcomes_df)

    if equity_df.empty:
        st.info("Not enough directional signals to build equity curves.")
        return

    fig = go.Figure()

    for stype in equity_df["signal_type"].unique():
        curve = equity_df[equity_df["signal_type"] == stype].sort_values("date")
        color = _EQUITY_COLORS.get(stype, "#888888")
        label = _SIGNAL_LABELS.get(stype, stype)
        width = 3 if stype == "SPY" else 1.5
        dash  = "dash" if stype == "SPY" else "solid"

        fig.add_trace(go.Scatter(
            x=curve["date"],
            y=curve["portfolio_value"],
            mode="lines",
            name=label,
            line=dict(color=color, width=width, dash=dash),
            hovertemplate=f"{label}<br>Date: %{{x}}<br>Value: $%{{y:,.2f}}<extra></extra>",
        ))

    fig.update_layout(
        title="Simulated $10,000 Portfolio by Signal Type vs SPY",
        xaxis_title="Date",
        yaxis_title="Portfolio Value ($)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        template="plotly_dark",
        height=500,
    )
    fig.add_hline(y=10000, line_dash="dot", line_color="gray", annotation_text="$10k baseline")

    st.plotly_chart(fig, use_container_width=True)


def _render_heatmap_tab(signals_list: list[dict]) -> None:
    """Render the Signal Heatmap tab: signal_type vs date, colored by direction."""
    if not signals_list:
        st.info("No signals to display in heatmap.")
        return

    df = pd.DataFrame(signals_list)
    df["signal_date"] = pd.to_datetime(df["signal_date"]).dt.date

    # Aggregate: for each (signal_type, date), take the mean score
    pivot = df.groupby(["signal_type", "signal_date"])["score"].mean().unstack(level=1)
    pivot.index = [_SIGNAL_LABELS.get(i, i) for i in pivot.index]

    # Fill NaN with 0 for display
    pivot = pivot.fillna(0.0)

    # Sort columns (dates)
    pivot = pivot[sorted(pivot.columns)]

    z_values = pivot.values.tolist()
    y_labels = pivot.index.tolist()
    x_labels = [str(d) for d in pivot.columns]

    fig = go.Figure(data=go.Heatmap(
        z=z_values,
        x=x_labels,
        y=y_labels,
        colorscale=[
            [0.0,  "#B71C1C"],   # strong bearish: dark red
            [0.45, "#FFCDD2"],   # weak bearish: light red
            [0.50, "#BDBDBD"],   # neutral: gray
            [0.55, "#C8E6C9"],   # weak bullish: light green
            [1.0,  "#1B5E20"],   # strong bullish: dark green
        ],
        zmid=0.0,
        zmin=-1.0,
        zmax=1.0,
        text=[[f"{v:.3f}" for v in row] for row in z_values],
        hovertemplate="Signal Type: %{y}<br>Date: %{x}<br>Score: %{z:.4f}<extra></extra>",
        colorbar=dict(title="Signal Score", tickvals=[-1, -0.5, 0, 0.5, 1]),
    ))

    fig.update_layout(
        title="Signal Scores by Type and Date (green=bullish, red=bearish, gray=neutral)",
        xaxis_title="Date",
        yaxis_title="Signal Type",
        template="plotly_dark",
        height=max(300, len(y_labels) * 60 + 100),
        xaxis=dict(tickangle=-45),
    )

    st.plotly_chart(fig, use_container_width=True)


def _render_breakdown_tab(signals_list: list[dict], outcomes_list: list[dict]) -> None:
    """Render the Signal Breakdown tab: per-signal-type deep dive."""
    if not signals_list or not outcomes_list:
        st.info("No data available for breakdown.")
        return

    signals_df  = pd.DataFrame(signals_list)
    outcomes_df = pd.DataFrame(outcomes_list)

    # Join
    merged = signals_df.merge(
        outcomes_df,
        left_on="id",
        right_on="signal_id",
        how="inner",
        suffixes=("_sig", "_out"),
    )
    merged = merged[merged["forward_return"].notna()].copy()

    if merged.empty:
        st.info("No outcomes with computed forward returns found.")
        return

    available_types = sorted(merged["signal_type"].unique().tolist())
    selected_type = st.selectbox(
        label="Select Signal Type",
        options=available_types,
        format_func=lambda x: _SIGNAL_LABELS.get(x, x),
        key="bt_breakdown_type",
    )

    sdf = merged[merged["signal_type"] == selected_type].copy()
    if len(sdf) < 2:
        st.warning(f"Too few data points for {_SIGNAL_LABELS.get(selected_type, selected_type)}.")
        return

    col1, col2 = st.columns(2)

    with col1:
        # Scatter: signal score vs forward return
        fig_scatter = go.Figure()
        colors = sdf["direction"].map({"bullish": "#4CAF50", "bearish": "#F44336", "neutral": "#9E9E9E"})

        fig_scatter.add_trace(go.Scatter(
            x=sdf["score"],
            y=sdf["forward_return"],
            mode="markers",
            marker=dict(color=colors, size=8, opacity=0.7),
            text=sdf.apply(
                lambda r: f"Ticker: {r['ticker']}<br>Date: {r['signal_date']}<br>Direction: {r['direction']}", axis=1
            ),
            hovertemplate="%{text}<br>Score: %{x:.4f}<br>Return: %{y:.4%}<extra></extra>",
            name="Signals",
        ))

        # Add trend line
        x_vals = sdf["score"].values
        y_vals = sdf["forward_return"].values
        if len(x_vals) >= 2:
            z = np.polyfit(x_vals, y_vals, 1)
            p = np.poly1d(z)
            x_line = np.linspace(x_vals.min(), x_vals.max(), 50)
            fig_scatter.add_trace(go.Scatter(
                x=x_line,
                y=p(x_line),
                mode="lines",
                line=dict(color="#FFD700", width=2, dash="dash"),
                name="Trend",
            ))

        fig_scatter.update_layout(
            title=f"{_SIGNAL_LABELS.get(selected_type, selected_type)}: Score vs Forward Return",
            xaxis_title="Signal Score",
            yaxis_title="Forward Return",
            template="plotly_dark",
            height=400,
        )
        st.plotly_chart(fig_scatter, use_container_width=True)

    with col2:
        # Histogram: forward return distribution by direction
        fig_hist = go.Figure()
        for direction, color in [("bullish", "#4CAF50"), ("bearish", "#F44336"), ("neutral", "#9E9E9E")]:
            subset = sdf[sdf["direction"] == direction]["forward_return"]
            if subset.empty:
                continue
            fig_hist.add_trace(go.Histogram(
                x=subset,
                name=direction.capitalize(),
                marker_color=color,
                opacity=0.7,
                nbinsx=20,
                hovertemplate=f"{direction}: %{{x:.4%}}<br>Count: %{{y}}<extra></extra>",
            ))

        fig_hist.update_layout(
            barmode="overlay",
            title="Forward Return Distribution by Direction",
            xaxis_title="Forward Return",
            yaxis_title="Count",
            template="plotly_dark",
            height=400,
        )
        st.plotly_chart(fig_hist, use_container_width=True)

    st.subheader("Individual Signals")
    display_df = sdf[[
        "ticker", "signal_date", "direction", "score",
        "forward_return", "benchmark_return", "alpha", "correct"
    ]].copy()
    display_df["forward_return"]   = display_df["forward_return"].map(lambda x: f"{x:.4%}" if pd.notna(x) else "N/A")
    display_df["benchmark_return"] = display_df["benchmark_return"].map(lambda x: f"{x:.4%}" if pd.notna(x) else "N/A")
    display_df["alpha"]            = display_df["alpha"].map(lambda x: f"{x:.4%}" if pd.notna(x) else "N/A")
    display_df["correct"]          = display_df["correct"].map(lambda x: "Hit" if x == 1 else ("Miss" if x == 0 else "Neutral"))
    display_df.columns = ["Ticker", "Signal Date", "Direction", "Score", "Forward Return", "SPY Return", "Alpha", "Outcome"]

    st.dataframe(display_df, use_container_width=True, hide_index=True)


def _render_composite_tab(signals_list: list[dict], outcomes_list: list[dict], metrics: list[dict]) -> None:
    """Render the Composite Signal tab: IC-weighted composite score and equity curve."""
    if not signals_list or not outcomes_list or not metrics:
        st.info("Run a backtest first to see composite signal analysis.")
        return

    valid_metrics = [m for m in metrics if not m.get("insufficient", False) and m.get("ic") is not None]
    if not valid_metrics:
        st.warning("No signal types have valid IC values for weighting.")
        return

    # IC-weighted composite: weight each signal type by its IC (clipped to [0, inf] — negative IC has zero weight)
    ic_weights = {m["signal_type"]: max(0.0, m["ic"]) for m in valid_metrics}
    total_weight = sum(ic_weights.values())

    if total_weight == 0:
        st.warning("All signal ICs are <= 0. No positive IC weights available for composite.")
        return

    normalized_weights = {k: v / total_weight for k, v in ic_weights.items()}

    # Display the weights
    st.subheader("IC Weights for Composite")
    weight_data = [
        {"Signal Type": _SIGNAL_LABELS.get(k, k), "IC": _fmt(m.get("ic"), 4), "Weight": f"{normalized_weights.get(k, 0):.1%}"}
        for m, k in [(m, m["signal_type"]) for m in valid_metrics]
        if k in normalized_weights
    ]
    st.dataframe(pd.DataFrame(weight_data), use_container_width=True, hide_index=True)

    st.divider()

    # Build composite signals: for each (ticker, date), compute weighted score
    signals_df  = pd.DataFrame(signals_list)
    outcomes_df = pd.DataFrame(outcomes_list)

    merged = signals_df.merge(
        outcomes_df,
        left_on="id",
        right_on="signal_id",
        how="inner",
        suffixes=("_sig", "_out"),
    )
    merged = merged[merged["forward_return"].notna()].copy()
    merged["signal_date"] = pd.to_datetime(merged["signal_date"]).dt.date

    # Only use signal types with positive IC weight
    merged = merged[merged["signal_type"].isin(normalized_weights)].copy()
    if merged.empty:
        st.warning("No signal data for IC-positive signal types.")
        return

    merged["weighted_score"] = merged.apply(
        lambda r: r["score"] * normalized_weights.get(r["signal_type"], 0.0), axis=1
    )

    # Group by (ticker, date): composite score = sum of weighted scores
    composite = (
        merged.groupby(["ticker", "signal_date"])
        .agg(
            composite_score=("weighted_score", "sum"),
            forward_return=("forward_return", "mean"),
            benchmark_return=("benchmark_return", "mean"),
        )
        .reset_index()
    )

    if len(composite) < 3:
        st.warning("Not enough composite data points for IC computation.")
        return

    from scipy.stats import spearmanr
    comp_ic_val, _ = spearmanr(composite["composite_score"], composite["forward_return"])
    comp_ic = float(comp_ic_val) if not np.isnan(comp_ic_val) else 0.0

    composite["direction"] = composite["composite_score"].map(
        lambda s: "bullish" if s > 0.05 else ("bearish" if s < -0.05 else "neutral")
    )
    directions = composite["direction"].tolist()
    fwd_rets   = composite["forward_return"].tolist()

    from data.backtest_engine import compute_hit_rate
    comp_hit = compute_hit_rate(directions, fwd_rets)

    # Sharpe of composite
    directional_pnls = []
    for d, r in zip(directions, fwd_rets):
        if d == "bullish":
            directional_pnls.append(r)
        elif d == "bearish":
            directional_pnls.append(-r)

    from data.backtest_engine import _compute_sharpe
    comp_sharpe = _compute_sharpe(directional_pnls)

    c1, c2, c3 = st.columns(3)
    c1.metric("Composite IC", f"{comp_ic:.4f}")
    c2.metric("Composite Hit Rate", _pct(comp_hit))
    c3.metric("Composite Sharpe", _fmt(comp_sharpe, 2))

    st.divider()

    # Composite equity curve vs SPY
    composite = composite.sort_values("signal_date")
    _INITIAL_CAPITAL = 10_000.0
    total_trades = len(composite[composite["direction"] != "neutral"])
    if total_trades == 0:
        st.info("No directional composite signals to plot equity curve.")
        return

    trade_size = _INITIAL_CAPITAL / total_trades
    running = _INITIAL_CAPITAL
    spy_running = _INITIAL_CAPITAL
    eq_dates, eq_values, spy_values = [], [], []

    for _, row in composite.iterrows():
        d   = row["direction"]
        fwd = row["forward_return"]
        bm  = row["benchmark_return"]

        if d != "neutral":
            pnl = trade_size * fwd if d == "bullish" else trade_size * (-fwd)
            running += pnl
        spy_running *= (1.0 + (bm if pd.notna(bm) else 0.0))

        eq_dates.append(row["signal_date"])
        eq_values.append(round(running, 4))
        spy_values.append(round(spy_running, 4))

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=eq_dates, y=eq_values,
        mode="lines", name="Composite Signal",
        line=dict(color="#FFD700", width=2),
    ))
    fig.add_trace(go.Scatter(
        x=eq_dates, y=spy_values,
        mode="lines", name="SPY Buy & Hold",
        line=dict(color="#607D8B", width=2, dash="dash"),
    ))
    fig.add_hline(y=10000, line_dash="dot", line_color="gray")
    fig.update_layout(
        title="IC-Weighted Composite Signal vs SPY ($10k Start)",
        xaxis_title="Date",
        yaxis_title="Portfolio Value ($)",
        template="plotly_dark",
        height=400,
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    st.plotly_chart(fig, use_container_width=True)


def _render_export_tab(signals_list: list[dict], outcomes_list: list[dict]) -> None:
    """Render the Data Export tab."""
    if not signals_list or not outcomes_list:
        st.info("Run a backtest first to export data.")
        return

    signals_df  = pd.DataFrame(signals_list)
    outcomes_df = pd.DataFrame(outcomes_list)

    merged = signals_df.merge(
        outcomes_df,
        left_on="id",
        right_on="signal_id",
        how="inner",
        suffixes=("_sig", "_out"),
    )

    export_df = merged[[
        "ticker", "signal_date", "signal_type", "direction", "score",
        "forward_return", "benchmark_return", "alpha", "correct",
        "price_at_signal", "price_at_horizon", "source_db",
    ]].copy()

    export_df.columns = [
        "Ticker", "Signal Date", "Signal Type", "Direction", "Score",
        "Forward Return", "SPY Return", "Alpha", "Correct (1=Yes, 0=No)",
        "Price at Signal", "Price at Horizon", "Source DB",
    ]

    csv_bytes = export_df.to_csv(index=False).encode("utf-8")

    st.download_button(
        label="Download Signals + Outcomes as CSV",
        data=csv_bytes,
        file_name="backtest_signals_outcomes.csv",
        mime="text/csv",
        use_container_width=True,
    )

    st.dataframe(export_df.head(200), use_container_width=True, hide_index=True)

    if len(export_df) > 200:
        st.caption(f"Showing first 200 of {len(export_df)} rows. Download the CSV for the full dataset.")


def _render_main() -> None:
    """Render the main page area based on current session state."""
    st.title("AI Signal Backtester")
    st.caption(
        "Measures the historical predictive accuracy of each AI signal type "
        "against actual forward price returns. Signal data comes from cached "
        "SQLite analyses; technical patterns are replayed on historical OHLCV. "
        "No new Gemini calls are made during a backtest."
    )

    run_config = _render_sidebar()

    # If a new run was requested, execute it with a progress spinner
    if run_config is not None:
        with st.spinner("Running backtest... this may take 1-2 minutes for technical pattern replay."):
            try:
                run_id = run_backtest(run_config)
                st.session_state["bt_current_run_id"] = run_id
                st.session_state["bt_last_run_config"] = run_config
                st.success(f"Backtest complete. Run ID: {run_id}")
            except Exception as exc:
                st.error(f"Backtest failed: {exc}")
                return

    current_run_id = st.session_state.get("bt_current_run_id")

    if current_run_id is None:
        st.info("Configure a backtest in the sidebar and click **Run Backtest** to begin.")
        return

    # Load results
    signals_list  = get_run_signals(current_run_id)
    outcomes_list = get_run_outcomes(current_run_id)
    metrics       = get_run_metrics(current_run_id)

    # Display run summary header
    run_config_display = st.session_state.get("bt_last_run_config", {})
    n_signals  = len(signals_list)
    n_outcomes = len(outcomes_list)

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Signals Extracted", n_signals)
    col2.metric("Outcomes Computed", n_outcomes)
    col3.metric("Run ID", current_run_id[:8] + "...")

    # Tabs
    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "Overview",
        "Equity Curves",
        "Signal Heatmap",
        "Signal Breakdown",
        "Composite Signal",
        "Data Export",
    ])

    with tab1:
        _render_overview_tab(metrics, run_config_display)

    with tab2:
        _render_equity_tab(signals_list, outcomes_list)

    with tab3:
        _render_heatmap_tab(signals_list)

    with tab4:
        _render_breakdown_tab(signals_list, outcomes_list)

    with tab5:
        _render_composite_tab(signals_list, outcomes_list, metrics)

    with tab6:
        _render_export_tab(signals_list, outcomes_list)


# ---------------------------------------------------------------------------
# Page entry
# ---------------------------------------------------------------------------

_render_main()
```

**Why**: This is the Streamlit UI layer. It follows the exact page template from `implementor-skills.md` (set_page_config → render_gemini_usage_bar → helpers → cached functions → render functions → page entry). It delegates all data computation to `backtest_engine.py` and only handles widgets, state, and display logic.

---

## Signal Scoring Reference (for implementor verification)

This section is a complete reference for how each signal type maps to a numeric score. The implementor must NOT deviate from these formulas in `score_signal()`.

### Options AI (`options_ai`)
```
score = direction_val * strength_val * confidence_val

direction_val:  bullish=+1.0, bearish=-1.0, neutral=0.0
strength_val:   strong=1.0, moderate=0.7, weak=0.4
confidence_val: high=1.0, medium=0.7, low=0.4

Example: bullish/strong/high → 1.0 * 1.0 * 1.0 = +1.0
Example: bearish/moderate/medium → -1.0 * 0.7 * 0.7 = -0.49
Example: neutral/* → 0.0
```

### News Sentiment (`news`)
```
score = raw sentiment_score (already in [-1, 1])
Clipped to [-1.0, 1.0]
```

### Reddit WSB (`reddit`)
```
score = sentiment_score * (hype_level / 10.0)
Clipped to [-1.0, 1.0]

Example: sentiment_score=0.8, hype_level=7 → 0.8 * 0.7 = 0.56
```

### Hedge Fund (`hedge_fund`)
```
score = ownership_val * conviction_val

ownership_val:  bullish_equity=+1.0, hedged=0.0, speculative_put=-1.0, mixed=0.0
conviction_val: high=1.0, medium=0.7, low=0.4

Example: bullish_equity/high → 1.0 * 1.0 = +1.0
Example: speculative_put/medium → -1.0 * 0.7 = -0.7
```

### Macro News (`macro`)
```
category_val: bullish_for_equities=+1.0, bearish_for_equities=-1.0,
              mixed=0.0, sector_specific=0.0, neutral=0.0

If category_val == 0.0: score = 0.0
Otherwise: score = category_val * abs(sentiment_score)
Clipped to [-1.0, 1.0]

Example: bullish_for_equities, sentiment_score=0.6 → 1.0 * 0.6 = +0.6
Example: mixed → 0.0 regardless of sentiment_score
```

### MPT Analysis (`mpt`)
```
score = rec_val * risk_mod_val

rec_val:      increase=+1.0, hold=0.0, reduce=-1.0
risk_mod_val: high=0.6, medium=0.8, low=1.0
(high risk reduces signal strength because the recommendation is more uncertain)

Example: increase/low_risk → 1.0 * 1.0 = +1.0
Example: reduce/high_risk  → -1.0 * 0.6 = -0.6
```

### Technical Patterns (`technical`)
```
score = direction_val * confidence_score

direction_val: bullish=+1.0, bearish=-1.0, neutral=0.0

Example: bullish, confidence_score=0.82 → +0.82
Example: bearish, confidence_score=0.65 → -0.65
```

### Direction from score
```
score > 0.05  → "bullish"
score < -0.05 → "bearish"
else          → "neutral"
```

---

## Lookahead Bias Prevention Protocol (mandatory verification)

The implementor MUST verify these constraints are satisfied in `fetch_forward_returns()`:

1. The entry price is the **OPEN** on the first trading day STRICTLY AFTER `signal_date`. This is identified by `next(i for i, d in enumerate(trading_dates) if d > sig_date)`.
2. The exit price is the **CLOSE** at index `entry_idx + horizon_days`.
3. The signal_date's own OHLCV data is NEVER used for computing `forward_return`.
4. `price_at_signal` (stored in backtest_outcomes for reference only) uses the CLOSE on the signal_date itself — this is NOT used in computing `forward_return`.

---

## IC Computation Details (mandatory verification)

The Information Coefficient uses **Spearman rank correlation** (not Pearson). This is deliberate:
- Signal scores and forward returns are not normally distributed
- Spearman is more robust to outliers (e.g., a meme stock with +300% return)
- `scipy.stats.spearmanr(scores, forward_returns)` returns `(correlation, p_value)`
- Use only the first element (correlation)
- Handle `NaN` result by returning 0.0

The IC standard deviation uses the approximation: `sqrt((1 - IC²) / (N - 2))`
This is the standard error of Spearman rho under the null hypothesis of independence.

The t-statistic: `IC / (IC_std / sqrt(N))`
- |t| >= 2.576 → statistically significant at 99% confidence
- |t| >= 1.960 → statistically significant at 95% confidence
- |t| >= 1.645 → marginal at 90% confidence

ICIR = IC / IC_std (information ratio of the IC itself across periods).
Note: With only one period per run, ICIR is computed from the formula above rather than the rolling IC / std(IC) method used in multi-period backtesting. This is acceptable for the current use case.

---

## Hedge Fund Signal Extraction — Critical Detail

The `hedge_fund_analysis` table uses `ticker_key` (a sorted, comma-joined string of all portfolio tickers at the time of analysis) as its primary organizational key. The `result_json` column contains the full Gemini output, which includes a `per_ticker_signals` dict keyed by individual ticker symbols.

The `get_hedge_fund_signals()` function must:
1. Query ALL rows from `hedge_fund_analysis` (not filtered by ticker_key, since the key is a portfolio-level composite, not per-ticker)
2. For each row, parse `result_json` and look inside `per_ticker_signals`
3. Extract only the tickers in the requested `tickers` list
4. Emit one signal per (ticker, row) combination found

The field names inside `per_ticker_signals` are `ownership_type` and `conviction_level` as specified. If the actual stored Gemini output uses different field names, the implementor must inspect an actual row in portfolio.db to confirm. The plan specifies the intended field names based on the hedge_fund_agent.py prompt template.

---

## MPT Signal Extraction — Critical Detail

Similar to hedge fund, the `mpt_analysis` table stores `result_json` (Gemini output) and `metrics_json` (Python-computed metrics). The per-ticker signals live inside `result_json` under the `ticker_analysis` key.

The `get_mpt_signals()` function must:
1. Query ALL rows from `mpt_analysis`
2. For each row, parse `result_json` and look inside `ticker_analysis`
3. Extract only the tickers in the requested `tickers` list
4. Emit one signal per (ticker, row) combination

The fields inside each per-ticker entry are `recommendation` and `risk_assessment`.

---

## Technical Signal Generation — Performance Note

The `get_technical_signals()` function is the most computationally expensive:
- It fetches 2 years of OHLCV for each ticker (one yfinance call per ticker)
- For each trading date in [date_from, date_to], it instantiates `PatternDetectionEngine` and calls `detect_all()`
- For a 90-day range with 63 trading days and 5 tickers, this runs ~315 `detect_all()` calls
- Each call runs all 5 phases of pattern detection across 15+ pattern types

For a typical date range of 90 days, this should complete in under 60 seconds. For longer ranges (> 180 days), the page displays a spinner warning. The implementor must NOT add `@st.cache_data` to this function since it is inside a `data/` module that writes to SQLite (two-layer caching violation).

The `detected_at_bar == window_df.index[-1]` guard is the key lookahead prevention mechanism: it ensures only patterns whose last confirmed bar IS the current day T are included. Patterns that were detected earlier and are still "active" (open) but detected on a prior day are excluded, because they would already have been included on the day they were detected.

---

## Database Path Note

All three `data/` modules use `os.path.dirname(__file__)` as their anchor. Since files are in `stock-dashboard/data/`, the path `os.path.join(os.path.dirname(__file__), "..", "db", "backtest.db")` resolves to `stock-dashboard/db/backtest.db`. This matches the project convention for all other databases.

---

## Testing Checklist

### Test 1: Schema creation
**Action**: Launch the Streamlit app and navigate to the Backtest page.
**Expected**: No errors. The page loads with sidebar controls visible.
**Verify**: Check that `stock-dashboard/db/backtest.db` now exists on disk.
**Failure sign**: FileNotFoundError or SQLite error on page load.

### Test 2: Empty run (no cached data)
**Action**: Set a date range for which no AI analyses exist (e.g., 5 years ago), select all signal types, click Run Backtest.
**Expected**: Run completes, shows "No metrics computed" message in Overview tab, run appears in "Recent Runs" list.
**Failure sign**: Exception thrown or page crashes.

### Test 3: News signal extraction
**Action**: Open `stock-dashboard/db/portfolio.db` with a SQLite browser and confirm rows exist in `portfolio_news_analysis`.
**Then**: Set date range to cover those rows' `analyzed_at` dates, select only "News Sentiment", click Run.
**Expected**: Overview tab shows news signal count > 0, IC and Hit Rate values displayed.
**Failure sign**: Signal count = 0 despite data existing in DB.

### Test 4: Technical signal generation
**Action**: Select 1-2 tickers (e.g., AAPL, MSFT), date range = last 30 days, enable only "Technical Patterns", click Run.
**Expected**: Technical signals appear. Pattern types visible in Signal Breakdown tab.
**Failure sign**: Zero signals or Python exception from PatternDetectionEngine.

### Test 5: Lookahead prevention verification
**Action**: In Signal Breakdown tab, select any signal type. Find a signal with a known signal_date.
**Expected**: "Price at Signal" column shows the close price on the signal date. "Forward Return" = (price_at_horizon - entry_open) / entry_open where entry_open is T+1's open.
**Verify manually**: Pull OHLCV from yfinance for that ticker and date. Confirm entry is T+1 open, exit is T+N close.
**Failure sign**: forward_return computed from same-day prices (lookahead bias).

### Test 6: Equity curves tab
**Action**: With a run that has at least one directional signal type with N >= 5, go to Equity Curves tab.
**Expected**: Plotly chart renders with colored lines per signal type plus gray SPY line. Y axis starts near $10,000. Lines diverge over time based on signal accuracy.
**Failure sign**: Empty chart or "Not enough directional signals" warning when signals exist.

### Test 7: Heatmap rendering
**Action**: Go to Signal Heatmap tab with a completed run.
**Expected**: Heatmap shows signal types on Y axis, dates on X axis, cells colored green (bullish) / red (bearish) / gray (neutral).
**Failure sign**: Empty heatmap or Plotly error.

### Test 8: Composite signal tab
**Action**: With multiple signal types that have valid IC values > 0, go to Composite Signal tab.
**Expected**: IC weights table displayed, composite equity curve plotted alongside SPY.
**Failure sign**: "All signal ICs are <= 0" warning with a mix of signal types.

### Test 9: CSV export
**Action**: Go to Data Export tab, click "Download Signals + Outcomes as CSV".
**Expected**: CSV file downloads with columns: Ticker, Signal Date, Signal Type, Direction, Score, Forward Return, SPY Return, Alpha, Correct, Price at Signal, Price at Horizon, Source DB.
**Failure sign**: File is empty or download fails.

### Test 10: Load previous run
**Action**: After running a backtest, reload the page. Select the run from "Recent Runs" dropdown, click "Load Run".
**Expected**: The previously completed run's results reload without re-running the backtest.
**Failure sign**: Page shows "No metrics computed" or triggers a new run.

### Test 11: Insufficient signals handling
**Action**: Run a backtest on a very narrow date range (e.g., 3 days) for signal types with sparse data.
**Expected**: Overview table shows "Insufficient data" for signal types with N < 5. No Python error.
**Failure sign**: Exception in `score_run()` or metrics table missing rows.

### Test 12: Macro signal ticker = SPY
**Action**: Select "Macro News" signal type and include SPY as one of the tickers. Run backtest.
**Expected**: Macro signals appear with ticker = "SPY" in the Signal Breakdown table. Forward returns are SPY's own returns (benchmark == forward for macro signals).
**Failure sign**: Macro signals not appearing or producing NaN forward returns.

---

## Rollback Plan

If the implementation introduces errors or the page cannot be loaded:

1. Delete `stock-dashboard/pages/10_backtest.py` — this removes the page from the sidebar with no side effects on other pages.
2. Delete `stock-dashboard/data/backtest_signals.py` — removes signal extraction logic.
3. Delete `stock-dashboard/data/backtest_engine.py` — removes engine.
4. Delete `stock-dashboard/db/backtest_schema.sql` — removes schema file.
5. Delete `stock-dashboard/db/backtest.db` (if it was created) — removes the database.
6. No modifications were made to any existing file, so no rollback is needed for other files.
7. `requirements.txt` was not modified (scipy was already present).

To verify rollback was successful: restart Streamlit app and confirm no import errors on any of the existing 9 pages.
