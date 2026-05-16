import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "portfolio.db")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "macro_news_schema.sql")


def _get_connection() -> sqlite3.Connection:
    """Return a sqlite3.Connection with row_factory set to sqlite3.Row."""
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Read db/macro_news_schema.sql and execute it to create tables if absent."""
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        ddl = fh.read()
    conn = _get_connection()
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()


def get_recent_analyses(category: Optional[str] = None, impact_type: Optional[str] = None,
                       hours: int = 24, limit: int = 50) -> list[dict]:
    """Fetch recent macro news analyses from the database.

    Parameters
    ----------
    category : Feed category filter (None = no filter)
    impact_type : market_impact_type filter (None = no filter)
    hours : Only include analyses from the last N hours
    limit : Maximum number of results to return

    Returns
    -------
    List of analysis dicts with deserialized JSON columns.
    """
    conn = _get_connection()
    try:
        cutoff_time = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        query = "SELECT * FROM macro_news_analysis WHERE analyzed_at >= ?"
        params = [cutoff_time]

        if category and category != "All":
            query += " AND feed_category = ?"
            params.append(category)

        if impact_type and impact_type != "All":
            query += " AND market_impact_type = ?"
            params.append(impact_type)

        query += " ORDER BY analyzed_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        data = dict(row)

        # Deserialize key_themes from JSON
        raw_themes = data.get("key_themes") or "[]"
        try:
            data["key_themes"] = json.loads(raw_themes)
        except (json.JSONDecodeError, TypeError):
            data["key_themes"] = []

        # Deserialize affected_sectors from JSON
        raw_sectors = data.get("affected_sectors") or "[]"
        try:
            data["affected_sectors"] = json.loads(raw_sectors)
        except (json.JSONDecodeError, TypeError):
            data["affected_sectors"] = []

        result.append(data)

    return result


def save_macro_analysis(article: dict, result_dict: dict) -> None:
    """Insert or ignore a macro news analysis row.

    Uses INSERT OR IGNORE to deduplicate by article_url.

    Parameters
    ----------
    article : Dict with keys: article_url, title, description, published_utc, source, feed_category
    result_dict : Dict from analyze_macro_articles with: sentiment_score, sentiment_label, summary,
                  impact_level, key_themes, market_impact_type, affected_sectors, macro_category
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO macro_news_analysis (
                feed_category, article_url, title, published_utc, source,
                sentiment_score, sentiment_label, summary, impact_level, key_themes,
                market_impact_type, affected_sectors, macro_category, analyzed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                article.get("feed_category"),
                article.get("article_url"),
                article.get("title"),
                article.get("published_utc"),
                article.get("source"),
                result_dict.get("sentiment_score"),
                result_dict.get("sentiment_label"),
                result_dict.get("summary"),
                result_dict.get("impact_level"),
                json.dumps(result_dict.get("key_themes", [])),
                result_dict.get("market_impact_type"),
                json.dumps(result_dict.get("affected_sectors", [])),
                result_dict.get("macro_category"),
                now_iso,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def is_category_fresh(category: Optional[str] = None, max_age_minutes: int = 120) -> bool:
    """Check if a feed category has fresh cached analyses.

    When category is None or "All", checks the most recent analyzed_at across all rows.

    Parameters
    ----------
    category : Feed category to check (None/"All" = check all)
    max_age_minutes : Threshold in minutes; returns True if cache is newer

    Returns
    -------
    True if cache is fresh (exists and is recent), False otherwise.
    """
    conn = _get_connection()
    try:
        if category is None or category == "All":
            # Check most recent across all categories
            row = conn.execute(
                "SELECT analyzed_at FROM macro_news_analysis ORDER BY analyzed_at DESC LIMIT 1"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT analyzed_at FROM macro_news_analysis WHERE feed_category = ? ORDER BY analyzed_at DESC LIMIT 1",
                (category,),
            ).fetchone()
    finally:
        conn.close()

    if row is None:
        return False

    try:
        analyzed_at = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
        age_minutes = (datetime.now(timezone.utc) - analyzed_at).total_seconds() / 60
        return age_minutes < max_age_minutes
    except (ValueError, TypeError):
        return False


def get_article_urls_seen(category: Optional[str] = None) -> set[str]:
    """Get all article URLs already analyzed in a category.

    Parameters
    ----------
    category : Feed category (None/"all" = all categories)

    Returns
    -------
    Set of article_url strings.
    """
    conn = _get_connection()
    try:
        if category is None or category == "all" or category == "All":
            rows = conn.execute("SELECT article_url FROM macro_news_analysis").fetchall()
        else:
            rows = conn.execute(
                "SELECT article_url FROM macro_news_analysis WHERE feed_category = ?",
                (category,),
            ).fetchall()
    finally:
        conn.close()

    return {row[0] for row in rows if row[0]}


# ---------------------------------------------------------------------------
# Initialize DB tables on import
# ---------------------------------------------------------------------------
init_db()
