"""SQLite persistence for the seasonality / macro regime page.

Owns `db/seasonality.db`. Three concerns:
  * `seasonality_analysis`     — cached Gemini 2.5 Pro regime + adaptation analyses
  * `regime_snapshot`          — one row per day so regime *shifts* can be detected
  * `seasonality_stats_cache`  — computed per-ticker seasonality blobs (24h TTL)

Public API
----------
save_analysis(tickers, result, inputs)      -> None
get_latest_analysis(tickers, max_age_hours) -> dict | None
save_regime_snapshot(regime)                -> None
get_regime_history(days)                    -> list[dict]
get_previous_regime()                       -> dict | None
get_cached_stats(ticker, years)             -> dict | None
save_cached_stats(ticker, years, stats)     -> None
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "seasonality.db")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "seasonality_schema.sql")

_STATS_TTL_HOURS = 24
_ANALYSIS_TTL_HOURS = 12

# JSON-encoded columns on seasonality_analysis, with their fallback value.
_JSON_COLUMNS = {
    "catalyst_watch": [],
    "position_actions": [],
    "sector_actions": [],
    "risk_factors": [],
    "inputs_json": {},
}


def _get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        ddl = fh.read()
    conn = _get_connection()
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()


init_db()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _portfolio_key(tickers: list) -> str:
    """Stable cache key for a set of holdings — order-independent."""
    return ",".join(sorted({str(t).upper().strip() for t in tickers if t}))


# ---------------------------------------------------------------------------
# Gemini analysis
# ---------------------------------------------------------------------------

def save_analysis(tickers: list, result: dict, inputs: dict | None = None) -> None:
    """Persist a Gemini regime/adaptation analysis for this set of holdings."""
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO seasonality_analysis (
                portfolio_key, regime_label, regime_summary, confidence,
                month_ahead, seasonal_outlook, macro_outlook, catalyst_watch,
                position_actions, sector_actions, risk_factors, inputs_json,
                analyzed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _portfolio_key(tickers),
                result.get("regime_label", ""),
                result.get("regime_summary", ""),
                result.get("confidence", ""),
                result.get("month_ahead", ""),
                result.get("seasonal_outlook", ""),
                result.get("macro_outlook", ""),
                json.dumps(result.get("catalyst_watch", [])),
                json.dumps(result.get("position_actions", [])),
                json.dumps(result.get("sector_actions", [])),
                json.dumps(result.get("risk_factors", [])),
                json.dumps(inputs or {}),
                _now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_latest_analysis(tickers: list, max_age_hours: int = _ANALYSIS_TTL_HOURS) -> dict | None:
    """Return the most recent analysis for these holdings, or None if stale/absent."""
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT * FROM seasonality_analysis
            WHERE portfolio_key = ?
            ORDER BY analyzed_at DESC LIMIT 1
            """,
            (_portfolio_key(tickers),),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    data = dict(row)
    analyzed = _parse_iso(data.get("analyzed_at", ""))
    if analyzed is None:
        return None
    if datetime.now(timezone.utc) - analyzed > timedelta(hours=max_age_hours):
        return None

    for column, fallback in _JSON_COLUMNS.items():
        try:
            data[column] = json.loads(data.get(column) or "null")
        except (json.JSONDecodeError, TypeError):
            data[column] = fallback
        if data[column] is None:
            data[column] = fallback
    return data


# ---------------------------------------------------------------------------
# Regime snapshots
# ---------------------------------------------------------------------------

def save_regime_snapshot(regime: dict) -> None:
    """Upsert today's regime reading so shifts can be diffed day over day."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO regime_snapshot (
                snapshot_date, regime_label, risk_score, rate_score,
                growth_score, inflation_score, indicators_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date) DO UPDATE SET
                regime_label    = excluded.regime_label,
                risk_score      = excluded.risk_score,
                rate_score      = excluded.rate_score,
                growth_score    = excluded.growth_score,
                inflation_score = excluded.inflation_score,
                indicators_json = excluded.indicators_json,
                recorded_at     = excluded.recorded_at
            """,
            (
                today,
                regime.get("label", ""),
                float(regime.get("risk_score", 0.0)),
                float(regime.get("rate_score", 0.0)),
                float(regime.get("growth_score", 0.0)),
                float(regime.get("inflation_score", 0.0)),
                json.dumps(regime.get("indicators", {})),
                _now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_regime_history(days: int = 90) -> list:
    """Return regime snapshots for the last `days` days, oldest first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT * FROM regime_snapshot
            WHERE snapshot_date >= ?
            ORDER BY snapshot_date ASC
            """,
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    history = []
    for row in rows:
        item = dict(row)
        try:
            item["indicators"] = json.loads(item.get("indicators_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            item["indicators"] = {}
        history.append(item)
    return history


def get_previous_regime() -> dict | None:
    """Return the most recent snapshot from a day *before* today, if any."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT * FROM regime_snapshot
            WHERE snapshot_date < ?
            ORDER BY snapshot_date DESC LIMIT 1
            """,
            (today,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Seasonality stat blobs
# ---------------------------------------------------------------------------

def get_cached_stats(ticker: str, years: int) -> dict | None:
    """Return cached seasonality stats for a ticker, or None if missing/stale."""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT stats_json, cached_at FROM seasonality_stats_cache WHERE cache_key = ?",
            (f"{ticker.upper()}|{years}",),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None
    cached = _parse_iso(row["cached_at"])
    if cached is None or datetime.now(timezone.utc) - cached > timedelta(hours=_STATS_TTL_HOURS):
        return None
    try:
        return json.loads(row["stats_json"])
    except (json.JSONDecodeError, TypeError):
        return None


def save_cached_stats(ticker: str, years: int, stats: dict) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO seasonality_stats_cache (cache_key, stats_json, cached_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                stats_json = excluded.stats_json,
                cached_at  = excluded.cached_at
            """,
            (f"{ticker.upper()}|{years}", json.dumps(stats), _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
