import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

_OPTIONS_TTL_HOURS = 4  # cached options analysis is fresh for this many hours

# ---------------------------------------------------------------------------
# DB path — db/portfolio.db, resolved relative to this file so it works
# regardless of the cwd when Streamlit launches.
# ---------------------------------------------------------------------------
_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "portfolio.db")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "portfolio_schema.sql")


def _get_connection() -> sqlite3.Connection:
    """Return a sqlite3.Connection with row_factory set to sqlite3.Row."""
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Read db/portfolio_schema.sql and execute it to create tables if absent."""
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        ddl = fh.read()
    conn = _get_connection()
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()


def get_latest_analysis(ticker: str) -> Optional[dict]:
    """Return the most recently stored analysis row for *ticker*, or None.

    The returned dict has these keys:
        id, ticker, sentiment_score, sentiment_label, summary, impact_level,
        key_themes (list[str] — deserialized from JSON), is_stock_specific (bool),
        article_count, sector, is_fallback (bool), latest_article_url,
        latest_article_published_utc, analyzed_at (str ISO timestamp)
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT *
            FROM   portfolio_news_analysis
            WHERE  ticker = ?
            ORDER  BY analyzed_at DESC
            LIMIT  1
            """,
            (ticker.upper(),),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    data = dict(row)
    # Deserialize key_themes from JSON string back to list
    raw_themes = data.get("key_themes") or "[]"
    try:
        data["key_themes"] = json.loads(raw_themes)
    except (json.JSONDecodeError, TypeError):
        data["key_themes"] = []
    # Normalize booleans
    data["is_stock_specific"] = bool(data.get("is_stock_specific", 1))
    data["is_fallback"] = bool(data.get("is_fallback", 0))
    return data


def save_analysis(
    ticker: str,
    result_dict: dict,
    article_count: int,
    sector: str,
    is_fallback: bool,
    articles: list[dict],
) -> None:
    """Insert a new analysis row and upsert all article URLs for *ticker*.

    Parameters
    ----------
    ticker       : Stock ticker (will be uppercased).
    result_dict  : Dict returned by analyze_articles(); must contain keys:
                   sentiment_score, sentiment_label, summary, impact_level,
                   key_themes, is_stock_specific.
    article_count: Total number of articles returned by fetch_news() (before
                   truncation to _MAX_ARTICLES_TO_SCRAPE).
    sector       : Sector string from get_sector_info(), or "" if not applicable.
    is_fallback  : True when sector-level articles were used as a supplement.
    articles     : The raw list of article dicts from fetch_news().  Each dict
                   must have at minimum an "article_url" key; "published_utc"
                   and "title" are used when present.
    """
    ticker = ticker.upper()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    # Determine the most recently published article for the staleness check
    latest_url: str = ""
    latest_pub: Optional[float] = None
    for art in articles:
        pub = art.get("published_utc")
        url = art.get("article_url", "")
        if pub is not None and url:
            try:
                pub_float = float(pub)
            except (TypeError, ValueError):
                continue
            if latest_pub is None or pub_float > latest_pub:
                latest_pub = pub_float
                latest_url = url

    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO portfolio_news_analysis (
                ticker, sentiment_score, sentiment_label, summary,
                impact_level, key_themes, is_stock_specific,
                article_count, sector, is_fallback,
                latest_article_url, latest_article_published_utc, analyzed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker,
                result_dict.get("sentiment_score"),
                result_dict.get("sentiment_label"),
                result_dict.get("summary"),
                result_dict.get("impact_level"),
                json.dumps(result_dict.get("key_themes", [])),
                1 if result_dict.get("is_stock_specific", True) else 0,
                article_count,
                sector,
                1 if is_fallback else 0,
                latest_url,
                latest_pub,
                now_iso,
            ),
        )

        # Upsert every article URL so future staleness checks work
        for art in articles:
            url = art.get("article_url", "")
            if not url:
                continue
            pub = art.get("published_utc")
            try:
                pub_float = float(pub) if pub is not None else None
            except (TypeError, ValueError):
                pub_float = None
            title = art.get("title", "")
            conn.execute(
                """
                INSERT INTO portfolio_news_seen_articles (ticker, article_url, published_utc, title)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ticker, article_url) DO NOTHING
                """,
                (ticker, url, pub_float, title),
            )

        conn.commit()
    finally:
        conn.close()


def has_new_articles(ticker: str, articles: list[dict]) -> bool:
    """Return True if any article_url in *articles* is not yet in the DB for *ticker*.

    Also returns True when the seen-articles table has no rows for *ticker* at all
    (i.e., this ticker has never been analyzed).

    Parameters
    ----------
    ticker   : Stock ticker (will be uppercased).
    articles : The raw article list from fetch_news().
    """
    ticker = ticker.upper()
    conn = _get_connection()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM portfolio_news_seen_articles WHERE ticker = ?",
            (ticker,),
        ).fetchone()[0]
        if count == 0:
            return True

        for art in articles:
            url = art.get("article_url", "")
            if not url:
                continue
            exists = conn.execute(
                """
                SELECT 1 FROM portfolio_news_seen_articles
                WHERE  ticker = ? AND article_url = ?
                LIMIT  1
                """,
                (ticker, url),
            ).fetchone()
            if exists is None:
                return True
    finally:
        conn.close()

    return False


def get_sentiment_history(ticker: str) -> list[dict]:
    """Return all analysis rows for *ticker* ordered by analyzed_at ASC.

    Each dict contains at minimum: analyzed_at (str), sentiment_score (float).
    All other columns are also included for completeness.
    """
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM   portfolio_news_analysis
            WHERE  ticker = ?
            ORDER  BY analyzed_at ASC
            """,
            (ticker.upper(),),
        ).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        data = dict(row)
        raw_themes = data.get("key_themes") or "[]"
        try:
            data["key_themes"] = json.loads(raw_themes)
        except (json.JSONDecodeError, TypeError):
            data["key_themes"] = []
        data["is_stock_specific"] = bool(data.get("is_stock_specific", 1))
        data["is_fallback"] = bool(data.get("is_fallback", 0))
        result.append(data)
    return result


_OPTIONS_FIELDS = [
    "directional_bias", "bias_strength", "confidence",
    "iv_analysis", "pcr_analysis", "max_pain_analysis",
    "gamma_exposure_analysis", "key_levels", "unusual_activity",
    "risk_factors", "summary",
]


def save_options_analysis(
    ticker: str,
    expiry: str,
    opt_type: str,
    spot_price: float,
    result_dict: dict,
) -> None:
    """Persist a new options analysis row for *ticker/expiry/opt_type*."""
    ticker = ticker.upper()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    metrics = result_dict.get("metrics", {})

    def _str(v) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, list):
            return "\n".join(str(i) for i in v)
        return str(v)

    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO portfolio_options_analysis (
                ticker, expiry, opt_type, spot_price,
                directional_bias, bias_strength, confidence,
                iv_analysis, pcr_analysis, max_pain_analysis,
                gamma_exposure_analysis, key_levels, unusual_activity,
                risk_factors, summary, metrics_json, analyzed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ticker, expiry, opt_type, spot_price,
                _str(result_dict.get("directional_bias")),
                _str(result_dict.get("bias_strength")),
                _str(result_dict.get("confidence")),
                _str(result_dict.get("iv_analysis")),
                _str(result_dict.get("pcr_analysis")),
                _str(result_dict.get("max_pain_analysis")),
                _str(result_dict.get("gamma_exposure_analysis")),
                _str(result_dict.get("key_levels")),
                _str(result_dict.get("unusual_activity")),
                _str(result_dict.get("risk_factors")),
                _str(result_dict.get("summary")),
                json.dumps(metrics),
                now_iso,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_latest_options_analysis(ticker: str, expiry: str, opt_type: str) -> Optional[dict]:
    """Return the most recent options analysis row, or None."""
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT * FROM portfolio_options_analysis
            WHERE  ticker = ? AND expiry = ? AND opt_type = ?
            ORDER  BY analyzed_at DESC
            LIMIT  1
            """,
            (ticker.upper(), expiry, opt_type),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    data = dict(row)
    raw_metrics = data.pop("metrics_json", None) or "{}"
    try:
        data["metrics"] = json.loads(raw_metrics)
    except (json.JSONDecodeError, TypeError):
        data["metrics"] = {}
    return data


def is_options_analysis_fresh(analyzed_at_str: str) -> bool:
    """Return True if *analyzed_at_str* (UTC ISO) is within _OPTIONS_TTL_HOURS."""
    try:
        analyzed_at = datetime.fromisoformat(analyzed_at_str).replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - analyzed_at < timedelta(hours=_OPTIONS_TTL_HOURS)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Hedge Fund Analysis cache helpers
# ---------------------------------------------------------------------------

_HF_TTL_HOURS = 4  # same TTL as options analysis


def _make_ticker_key(portfolio_tickers: list) -> str:
    """Return a stable string key for a set of portfolio tickers."""
    return ",".join(sorted(t.upper() for t in portfolio_tickers if t))


def save_hedge_fund_analysis(portfolio_tickers: list, result_dict: dict) -> None:
    """Persist a hedge fund intelligence analysis result for this portfolio snapshot.

    Parameters
    ----------
    portfolio_tickers : List of ticker strings currently in the portfolio.
    result_dict       : The dict returned by run_hedge_fund_analysis().
    """
    ticker_key = _make_ticker_key(portfolio_tickers)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO hedge_fund_analysis (ticker_key, result_json, analyzed_at)
            VALUES (?, ?, ?)
            """,
            (ticker_key, json.dumps(result_dict), now_iso),
        )
        conn.commit()
    finally:
        conn.close()


def get_latest_hedge_fund_analysis(portfolio_tickers: list) -> Optional[dict]:
    """Return the most recent hedge fund analysis for this portfolio snapshot, or None.

    Returns None when no row exists or when the most recent row is older than
    _HF_TTL_HOURS hours (4 hours, same TTL as options analysis).

    Parameters
    ----------
    portfolio_tickers : List of ticker strings currently in the portfolio.
    """
    ticker_key = _make_ticker_key(portfolio_tickers)
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT result_json, analyzed_at
            FROM   hedge_fund_analysis
            WHERE  ticker_key = ?
            ORDER  BY analyzed_at DESC
            LIMIT  1
            """,
            (ticker_key,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    analyzed_at_str = row["analyzed_at"]
    # Check TTL — reuse the same logic as is_options_analysis_fresh
    if not is_options_analysis_fresh(analyzed_at_str):
        return None

    try:
        return json.loads(row["result_json"])
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# MPT Analysis cache helpers
# ---------------------------------------------------------------------------

_MPT_TTL_HOURS = 4  # cached MPT analysis is fresh for this many hours


def save_mpt_analysis(portfolio_tickers: list, result: dict, metrics: dict) -> None:
    """Persist an MPT analysis result and its pre-computed metrics for this portfolio snapshot.

    Parameters
    ----------
    portfolio_tickers : List of ticker strings currently in the portfolio.
    result            : The parsed Gemini JSON dict returned by run_mpt_analysis().
    metrics           : The pre-computed Python MPT metrics dict from _compute_mpt_metrics().
    """
    ticker_key = _make_ticker_key(portfolio_tickers)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO mpt_analysis (ticker_key, result_json, metrics_json, analyzed_at)
            VALUES (?, ?, ?, ?)
            """,
            (ticker_key, json.dumps(result), json.dumps(metrics), now_iso),
        )
        conn.commit()
    finally:
        conn.close()


def get_latest_mpt_analysis(portfolio_tickers: list) -> Optional[dict]:
    """Return the most recent MPT analysis for this portfolio snapshot, or None.

    Returns None when no row exists or when the most recent row is older than
    _MPT_TTL_HOURS hours.

    Parameters
    ----------
    portfolio_tickers : List of ticker strings currently in the portfolio.

    Returns
    -------
    dict with keys: result (parsed Gemini JSON dict), metrics (pre-computed metrics dict),
    analyzed_at (ISO UTC string). Returns None if not found or stale.
    """
    ticker_key = _make_ticker_key(portfolio_tickers)
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT result_json, metrics_json, analyzed_at
            FROM   mpt_analysis
            WHERE  ticker_key = ?
            ORDER  BY analyzed_at DESC
            LIMIT  1
            """,
            (ticker_key,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    analyzed_at_str = row["analyzed_at"]
    if not is_mpt_analysis_fresh(analyzed_at_str):
        return None

    try:
        result = json.loads(row["result_json"])
        metrics = json.loads(row["metrics_json"])
    except (json.JSONDecodeError, TypeError):
        return None

    return {"result": result, "metrics": metrics, "analyzed_at": analyzed_at_str}


def is_mpt_analysis_fresh(analyzed_at_str: str) -> bool:
    """Return True if *analyzed_at_str* (UTC ISO) is within _MPT_TTL_HOURS."""
    try:
        analyzed_at = datetime.fromisoformat(analyzed_at_str).replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - analyzed_at < timedelta(hours=_MPT_TTL_HOURS)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Initialize DB tables on import
# ---------------------------------------------------------------------------
init_db()
