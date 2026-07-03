"""SQLite persistence for the AI Stock Screener's picks tracker.

Owns db/screener.db. Every screener run is recorded in screener_runs, and every
Flash/Pro finalist is recorded as a row in screener_picks (append-only — a ticker
gets one row when it becomes a Flash finalist and a second row when its Pro
verdict comes back). See data/screener_agent.py for how these are populated and
pages/12_screener.py "Picks Tracker" tab for how they are displayed.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "screener.db")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "screener_schema.sql")


def _get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create screener.db tables from screener_schema.sql if they don't exist."""
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        ddl = fh.read()
    conn = _get_connection()
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()


init_db()


def save_run(industry: str, weights: dict) -> str:
    """Insert a new screener_runs row and return the generated run_id (UUID4 string)."""
    run_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO screener_runs (run_id, run_at, industry, weights_json) VALUES (?, ?, ?, ?)",
            (run_id, now_iso, industry, json.dumps(weights)),
        )
        conn.commit()
    finally:
        conn.close()
    return run_id


def save_pick(
    run_id: str,
    ticker: str,
    industry: str,
    boom_score: float | None,
    stage_reached: str,
    boom_potential: str | None,
    conviction: str | None,
    entry_price: float | None,
    alert_price: float | None,
) -> None:
    """Insert one finalist pick row tied to run_id. Always inserts (append-only)."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO screener_picks
                (run_id, ticker, industry, boom_score, stage_reached,
                 boom_potential, conviction, entry_price, alert_price, picked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                ticker.upper(),
                industry,
                boom_score,
                stage_reached,
                boom_potential,
                conviction,
                entry_price,
                alert_price,
                now_iso,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_picks(limit: int = 200) -> list[dict]:
    """Return all recorded picks, most recent first, as a list of dicts."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM screener_picks ORDER BY picked_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
