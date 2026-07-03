"""
backtest_signals.py

Extracts historical AI signals from all cached SQLite sources and converts
them to a normalized list of signal dicts ready for the backtest engine.

Each returned signal dict has these keys:
    ticker       : str   — uppercase ticker symbol
    signal_date  : date  — datetime.date object (the day of analysis)
    signal_type  : str   — one of: options_ai|news|reddit|hedge_fund|macro|mpt|technical|screener
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
get_screener_signals(tickers, date_from, date_to) -> list[dict]
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
_SCREENER_DB  = os.path.join(os.path.dirname(__file__), "..", "db", "screener.db")


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


def _screener_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_SCREENER_DB)
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
    signal_type : One of options_ai|news|reddit|hedge_fund|macro|mpt|technical|screener
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

    elif signal_type == "screener":
        stage = raw_output.get("stage_reached", "flash")
        potential = raw_output.get("boom_potential")
        conviction = raw_output.get("conviction")

        if stage == "pro" and potential:
            potential_map = {"explosive": 1.0, "high": 0.8, "moderate": 0.5, "limited": 0.2}
            conviction_map = {"high": 1.0, "medium": 0.7, "low": 0.4}
            p = potential_map.get(potential, 0.5)
            c = conviction_map.get(conviction, 0.7)
            return round(p * c, 4)
        # Flash-only finalist (no Pro verdict yet): weak bullish signal — it made the
        # top-2 catalyst cut, but hasn't been risk-audited.
        return 0.3

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


def get_screener_signals(
    tickers: list[str],
    date_from: date,
    date_to: date,
) -> list[dict]:
    """Extract AI screener pick signals from screener_picks in screener.db.

    Every row in screener_picks (Flash finalist or Pro-verdict finalist) becomes one
    signal. Flash-only rows (stage_reached='flash') get a fixed weak-bullish score via
    score_signal(); Pro rows (stage_reached='pro') are scored from boom_potential +
    conviction. See score_signal()'s "screener" branch for the exact mapping.

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
    if not tickers_upper:
        return []
    placeholders = ",".join("?" for _ in tickers_upper)

    conn = _screener_conn()
    try:
        rows = conn.execute(
            f"""
            SELECT ticker, boom_score, stage_reached, boom_potential, conviction, picked_at
            FROM screener_picks
            WHERE ticker IN ({placeholders})
            ORDER BY picked_at ASC
            """,
            tickers_upper,
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

    signals = []
    for row in rows:
        row_dict = dict(row)
        d = _parse_date(row_dict.get("picked_at", ""))
        if not _date_in_range(d, date_from, date_to):
            continue

        raw = {
            "stage_reached": row_dict.get("stage_reached", "flash"),
            "boom_potential": row_dict.get("boom_potential"),
            "conviction": row_dict.get("conviction"),
        }
        sc = score_signal("screener", raw)
        signals.append({
            "ticker":      row_dict["ticker"].upper(),
            "signal_date": d,
            "signal_type": "screener",
            "direction":   _direction_from_score(sc),
            "score":       sc,
            "source_db":   "screener.db/screener_picks",
            "raw_json":    json.dumps(raw),
        })

    return signals
