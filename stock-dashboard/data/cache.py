import sqlite3
import json
import os
from datetime import datetime, timedelta, date
from typing import Optional

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "cache.db")


def _get_connection() -> sqlite3.Connection:
    """Return a SQLite connection, creating the cache tables if needed."""
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metric_cache (
            ticker      TEXT PRIMARY KEY,
            metric_json TEXT NOT NULL,
            timestamp   TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hedge_fund_cache (
            cache_key   TEXT PRIMARY KEY,
            data_json   TEXT NOT NULL,
            cached_date TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hedge_fund_filings (
            cik              TEXT PRIMARY KEY,
            name             TEXT NOT NULL,
            accession_number TEXT NOT NULL,
            report_period    TEXT NOT NULL,
            filing_date      TEXT NOT NULL,
            total_holdings   INTEGER NOT NULL,
            total_value      REAL NOT NULL,
            holdings_json    TEXT NOT NULL,
            fetched_at       TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hedge_fund_check_log (
            id           INTEGER PRIMARY KEY CHECK (id = 1),
            last_checked TEXT NOT NULL,
            last_count   INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def save_to_cache(ticker: str, data: dict) -> None:
    """Persist a metrics dict for a ticker to the SQLite cache."""
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO metric_cache (ticker, metric_json, timestamp)
            VALUES (?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                metric_json = excluded.metric_json,
                timestamp   = excluded.timestamp
            """,
            (ticker.upper(), json.dumps(data), datetime.utcnow().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def load_from_cache(ticker: str, max_age_hours: int = 1) -> Optional[dict]:
    """Load cached metrics for a ticker if the entry is within max_age_hours."""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT metric_json, timestamp FROM metric_cache WHERE ticker = ?",
            (ticker.upper(),),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    metric_json, timestamp_str = row
    cached_at = datetime.fromisoformat(timestamp_str)
    if datetime.utcnow() - cached_at > timedelta(hours=max_age_hours):
        return None

    try:
        return json.loads(metric_json)
    except json.JSONDecodeError:
        return None


def save_hedge_fund_cache(key: str, data: list) -> None:
    """Persist hedge fund list to SQLite, keyed by date so it expires at midnight."""
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO hedge_fund_cache (cache_key, data_json, cached_date)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                data_json   = excluded.data_json,
                cached_date = excluded.cached_date
            """,
            (key, json.dumps(data), str(date.today())),
        )
        conn.commit()
    finally:
        conn.close()


def load_hedge_fund_cache(key: str) -> Optional[list]:
    """Load hedge fund list from SQLite; returns None if missing or from a prior day."""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT data_json, cached_date FROM hedge_fund_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    data_json, cached_date = row
    if cached_date != str(date.today()):
        return None

    try:
        return json.loads(data_json)
    except json.JSONDecodeError:
        return None


def get_all_hedge_fund_filings() -> list:
    """Return all rows from hedge_fund_filings as a list of FundProfile-shaped dicts."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT cik, name, accession_number, report_period, filing_date,
                   total_holdings, total_value, holdings_json, fetched_at
            FROM hedge_fund_filings
            """
        ).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        (cik, name, accession_number, report_period, filing_date,
         total_holdings, total_value, holdings_json, fetched_at) = row
        try:
            holdings = json.loads(holdings_json)
        except (json.JSONDecodeError, TypeError):
            holdings = []
        result.append({
            "cik": cik,
            "name": name,
            "accession_number": accession_number,
            "report_period": report_period,
            "filing_date": filing_date,
            "total_holdings": total_holdings,
            "total_value": total_value,
            "holdings": holdings,
            "fetched_at": fetched_at,
        })
    return result


def upsert_hedge_fund_filing(fund: dict, accession_number: str) -> None:
    """Insert or update one fund row in hedge_fund_filings."""
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO hedge_fund_filings
                (cik, name, accession_number, report_period, filing_date,
                 total_holdings, total_value, holdings_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cik) DO UPDATE SET
                name             = excluded.name,
                accession_number = excluded.accession_number,
                report_period    = excluded.report_period,
                filing_date      = excluded.filing_date,
                total_holdings   = excluded.total_holdings,
                total_value      = excluded.total_value,
                holdings_json    = excluded.holdings_json,
                fetched_at       = excluded.fetched_at
            """,
            (
                str(fund.get("cik", "")),
                str(fund.get("name", "")),
                str(accession_number),
                str(fund.get("report_period", "")),
                str(fund.get("filing_date", "")),
                int(fund.get("total_holdings", 0)),
                float(fund.get("total_value", 0.0)),
                json.dumps(fund.get("holdings", [])),
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_stored_accession_numbers() -> dict:
    """Return {cik: accession_number} for every row in hedge_fund_filings."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT cik, accession_number FROM hedge_fund_filings"
        ).fetchall()
    finally:
        conn.close()
    return {row[0]: row[1] for row in rows}


def get_last_check_time() -> Optional[datetime]:
    """Return the datetime of the last EDGAR poll, or None if never checked."""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT last_checked FROM hedge_fund_check_log WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except (ValueError, TypeError):
        return None


def save_last_check_time(count: int) -> None:
    """Upsert the hedge_fund_check_log row with the current UTC time and count."""
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO hedge_fund_check_log (id, last_checked, last_count)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_checked = excluded.last_checked,
                last_count   = excluded.last_count
            """,
            (datetime.utcnow().isoformat(), int(count)),
        )
        conn.commit()
    finally:
        conn.close()
