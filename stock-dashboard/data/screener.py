"""
data/screener.py
Speculative stock screener — yfinance screen API wrapper, speculation score
computation, and SQLite persistence for screener results (1h TTL).
"""

import concurrent.futures
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import yfinance as yf
from yfinance import EquityQuery

# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "screener.db")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "screener_schema.sql")


def _get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Read and execute the schema DDL. Idempotent — safe to call on every import."""
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
# Constants
# ---------------------------------------------------------------------------

# Market cap tier boundaries (in USD)
_MICRO_CAP_MAX = 300_000_000      # < $300M
_SMALL_CAP_MAX = 2_000_000_000    # < $2B

# Screener result cache TTL
_CACHE_TTL_HOURS = 1

# Fundamentals cache TTL (fundamentals change slowly; 24h is appropriate)
_FUNDAMENTALS_TTL_HOURS = 24

# Number of top-ranked candidates to enrich with fundamentals by default
_DEFAULT_ENRICH_TOP_N = 25

# Valid sectors for the sector filter dropdown. Empty string = "All Sectors".
SECTOR_OPTIONS = [
    "",
    "Technology",
    "Healthcare",
    "Consumer Cyclical",
    "Financial Services",
    "Basic Materials",
    "Communication Services",
    "Energy",
    "Industrials",
    "Real Estate",
    "Consumer Defensive",
    "Utilities",
]

# ---------------------------------------------------------------------------
# SQLite cache helpers for raw screener results
# ---------------------------------------------------------------------------

def _save_screener_results(screen_key: str, results: list[dict]) -> None:
    """Persist a list of screener result dicts to SQLite, keyed by screen_key."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO screener_results (screen_key, results_json, fetched_at) VALUES (?, ?, ?)",
            (screen_key, json.dumps(results), now_iso),
        )
        conn.commit()
    finally:
        conn.close()


def _load_screener_results(screen_key: str) -> Optional[list[dict]]:
    """Load cached screener results if they are within _CACHE_TTL_HOURS old."""
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT results_json, fetched_at
            FROM screener_results
            WHERE screen_key = ?
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            (screen_key,),
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

    if datetime.now(timezone.utc) - fetched_at > timedelta(hours=_CACHE_TTL_HOURS):
        return None

    try:
        return json.loads(data["results_json"])
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Fundamentals SQLite cache helpers
# ---------------------------------------------------------------------------

def _save_fundamentals(ticker: str, fundamentals: dict) -> None:
    """Persist fetched .info fundamentals for a single ticker to SQLite."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO screener_fundamentals (ticker, fundamentals_json, fetched_at) VALUES (?, ?, ?)",
            (ticker.upper(), json.dumps(fundamentals), now_iso),
        )
        conn.commit()
    finally:
        conn.close()


def _load_fundamentals(ticker: str) -> Optional[dict]:
    """
    Load cached fundamentals for ticker if within _FUNDAMENTALS_TTL_HOURS.
    Returns None if no row exists or the row is stale.
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT fundamentals_json, fetched_at
            FROM screener_fundamentals
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

    if datetime.now(timezone.utc) - fetched_at > timedelta(hours=_FUNDAMENTALS_TTL_HOURS):
        return None

    try:
        return json.loads(data["fundamentals_json"])
    except (json.JSONDecodeError, TypeError):
        return None


def _fetch_single_fundamentals(ticker: str) -> tuple[str, dict]:
    """
    Fetch yf.Ticker(ticker).info and extract the fundamental keys relevant to
    speculation scoring. Returns (ticker_upper, fundamentals_dict).

    Checks the SQLite cache first. On any error or missing key, returns the
    partial dict with None values for missing fields — never raises.

    Keys extracted (all may be None for micro/small-caps with sparse data):
        revenue_growth        float | None  — YoY revenue growth as decimal (e.g. 0.35 = 35%)
        earnings_growth       float | None  — YoY earnings growth as decimal
        gross_margins         float | None  — gross margin as decimal
        total_cash            float | None  — total cash in USD
        total_debt            float | None  — total debt in USD
        free_cashflow         float | None  — annual FCF in USD (negative = burning cash)
        operating_cashflow    float | None  — operating cash flow in USD
        short_percent_float   float | None  — short interest as % of float (0–1 decimal)
        shares_outstanding    float | None  — total shares outstanding
        float_shares          float | None  — float shares
        target_mean_price     float | None  — analyst mean price target in USD
        debt_to_equity        float | None  — debt/equity ratio
        held_percent_insiders float | None  — insider ownership as decimal (e.g. 0.08 = 8%)
    """
    ticker_upper = ticker.upper()

    # Cache check
    cached = _load_fundamentals(ticker_upper)
    if cached is not None:
        return ticker_upper, cached

    _KEY_MAP = {
        "revenue_growth":        "revenueGrowth",
        "earnings_growth":       "earningsGrowth",
        "gross_margins":         "grossMargins",
        "total_cash":            "totalCash",
        "total_debt":            "totalDebt",
        "free_cashflow":         "freeCashflow",
        "operating_cashflow":    "operatingCashflow",
        "short_percent_float":   "shortPercentOfFloat",
        "shares_outstanding":    "sharesOutstanding",
        "float_shares":          "floatShares",
        "target_mean_price":     "targetMeanPrice",
        "debt_to_equity":        "debtToEquity",
        "held_percent_insiders": "heldPercentInsiders",
    }

    result: dict = {k: None for k in _KEY_MAP}

    fetch_ok = False
    try:
        info = yf.Ticker(ticker_upper).info
        fetch_ok = bool(info) and len(info) > 1
        for our_key, yf_key in _KEY_MAP.items():
            val = info.get(yf_key)
            if val is not None:
                try:
                    result[our_key] = float(val)
                except (TypeError, ValueError):
                    result[our_key] = None
    except Exception:
        pass  # Return all-None dict on any network or parsing error

    # Only cache successful fetches — caching a failed (all-None) fetch would
    # suppress retries for the full 24h TTL.
    if fetch_ok:
        _save_fundamentals(ticker_upper, result)
    return ticker_upper, result


def fetch_fundamentals_for_tickers(tickers: list[str], max_workers: int = 8) -> dict[str, dict]:
    """
    Fetch fundamentals for a list of tickers in parallel using ThreadPoolExecutor.

    Parameters
    ----------
    tickers     : list[str] — list of ticker symbols (case-insensitive)
    max_workers : int — number of parallel threads (default 8)

    Returns
    -------
    dict mapping ticker (uppercase str) → fundamentals dict.
    Each fundamentals dict has the keys defined in _fetch_single_fundamentals.
    Tickers that fail silently return an all-None fundamentals dict.
    """
    results: dict[str, dict] = {}
    if not tickers:
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_fetch_single_fundamentals, t): t for t in tickers
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                ticker_upper, fund_dict = future.result()
                results[ticker_upper] = fund_dict
            except Exception:
                orig_ticker = futures[future].upper()
                results[orig_ticker] = {k: None for k in (
                    "revenue_growth", "earnings_growth", "gross_margins",
                    "total_cash", "total_debt", "free_cashflow", "operating_cashflow",
                    "short_percent_float", "shares_outstanding", "float_shares",
                    "target_mean_price", "debt_to_equity", "held_percent_insiders",
                )}

    return results


# ---------------------------------------------------------------------------
# yfinance screener
# ---------------------------------------------------------------------------

def _build_equity_query(
    min_market_cap: int,
    max_market_cap: int,
    min_price: float,
    max_price: float,
    min_volume: int,
    sector: str,
) -> EquityQuery:
    """Build a custom EquityQuery from user filter parameters.

    Parameters
    ----------
    min_market_cap : int — minimum market cap in USD (e.g. 10_000_000)
    max_market_cap : int — maximum market cap in USD (e.g. 2_000_000_000)
    min_price      : float — minimum stock price
    max_price      : float — maximum stock price
    min_volume     : int — minimum average daily volume (3-month)
    sector         : str — sector string as returned by Yahoo Finance, or "" for all sectors

    Returns
    -------
    EquityQuery object ready to pass to yf.screen()
    """
    conditions = [
        EquityQuery("IS-IN", ["exchange", "NMS", "NYQ"]),
        EquityQuery("EQ", ["region", "us"]),
        EquityQuery("BTWN", ["intradaymarketcap", min_market_cap, max_market_cap]),
        EquityQuery("GTE", ["intradayprice", min_price]),
        EquityQuery("LTE", ["intradayprice", max_price]),
        EquityQuery("GTE", ["avgdailyvol3m", min_volume]),
    ]
    if sector:
        conditions.append(EquityQuery("EQ", ["sector", sector]))

    return EquityQuery("AND", conditions)


def _run_screen(
    min_market_cap: int,
    max_market_cap: int,
    min_price: float,
    max_price: float,
    min_volume: int,
    sector: str,
    size: int = 100,
) -> list[dict]:
    """
    Execute the yfinance custom equity screen.

    Returns a list of raw quote dicts as returned by Yahoo Finance.
    Returns an empty list on any error.
    """
    query = _build_equity_query(min_market_cap, max_market_cap, min_price, max_price, min_volume, sector)
    try:
        result = yf.screen(
            query,
            sortField="avgdailyvol3m",
            sortAsc=False,
            size=size,
        )
        quotes = result.get("quotes", [])
        return [q for q in quotes if isinstance(q, dict)]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Speculation score
# ---------------------------------------------------------------------------

def _compute_technical_score(quote: dict) -> dict[str, int]:
    """
    Compute the technical sub-score block (0–50 points total) for a single
    Yahoo Finance quote dict.

    Technical components (each 0–10 points, 5 components = 50 pts max):
    T1. Distance above 52-week low (momentum upward):
        fiftyTwoWeekLowChangePercent * 100 / 5, capped at 10
    T2. Distance below 52-week high (recovery room):
        If fiftyTwoWeekHighChangePercent < 0: min(abs(pct) * 100 * 10, 10); else 0
    T3. Volume spike (10-day vs 3-month):
        ratio = vol10d/vol3m; min((ratio - 1) * 5, 10) if ratio > 1 else 0
    T4. Small cap premium:
        marketCap < 100M → 10; < 300M → 8; < 500M → 5; < 2B → 3; else 0
    T5. Forward PE discount:
        0 < fwdPE < 15 → 10; 15 ≤ fwdPE < 25 → 5; None or ≤ 0 → 3; else 0

    Returns a dict:
        {
            "t1_52w_momentum": int,      # 0–10
            "t2_52w_recovery": int,      # 0–10
            "t3_volume_spike": int,      # 0–10
            "t4_small_cap":    int,      # 0–10
            "t5_fwd_pe":       int,      # 0–10
            "technical_total": int,      # 0–50
        }
    """
    t1 = t2 = t3 = t4 = t5 = 0

    # T1: 52-week low momentum
    low_chg = quote.get("fiftyTwoWeekLowChangePercent")
    if low_chg is not None:
        try:
            t1 = max(0, min(int(float(low_chg) * 100 / 5), 10))
        except (TypeError, ValueError):
            pass

    # T2: 52-week high recovery room
    high_chg = quote.get("fiftyTwoWeekHighChangePercent")
    if high_chg is not None:
        try:
            v = float(high_chg)
            if v < 0:
                t2 = min(int(abs(v) * 100 * 10), 10)
        except (TypeError, ValueError):
            pass

    # T3: volume spike
    vol_10d = quote.get("averageDailyVolume10Day")
    vol_3m = quote.get("averageDailyVolume3Month")
    if vol_10d and vol_3m and float(vol_3m) > 0:
        try:
            ratio = float(vol_10d) / float(vol_3m)
            if ratio > 1:
                t3 = min(int((ratio - 1) * 5), 10)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # T4: small cap premium
    market_cap = quote.get("marketCap")
    if market_cap is not None:
        try:
            mc = float(market_cap)
            if mc < 100_000_000:
                t4 = 10
            elif mc < 300_000_000:
                t4 = 8
            elif mc < 500_000_000:
                t4 = 5
            elif mc < 2_000_000_000:
                t4 = 3
        except (TypeError, ValueError):
            pass

    # T5: forward PE discount
    fwd_pe = quote.get("forwardPE")
    if fwd_pe is not None:
        try:
            pe = float(fwd_pe)
            if 0 < pe < 15:
                t5 = 10
            elif 15 <= pe < 25:
                t5 = 5
        except (TypeError, ValueError):
            pass
    else:
        t5 = 3  # no forward PE = inherently speculative

    tech_total = t1 + t2 + t3 + t4 + t5

    return {
        "t1_52w_momentum": t1,
        "t2_52w_recovery": t2,
        "t3_volume_spike": t3,
        "t4_small_cap":    t4,
        "t5_fwd_pe":       t5,
        "technical_total": tech_total,
    }


def _compute_fundamental_score(fundamentals: dict, current_price: Optional[float]) -> dict[str, int]:
    """
    Compute the fundamental sub-score block (0–50 points total).

    Requires a fundamentals dict as returned by _fetch_single_fundamentals.
    current_price is the regularMarketPrice from the screener quote dict (may be None).

    Fundamental components:
    F1. Revenue growth (0–15 pts):
        revenueGrowth >= 0.50 (50% YoY) → 15
        >= 0.25 → 10
        >= 0.10 → 5
        >= 0.00 → 2
        < 0 or None → 0
    F2. Cash runway (0–10 pts):
        Computed as: totalCash / abs(freeCashflow) in years (only when FCF < 0).
        If FCF >= 0 (cash-flow positive): → 10 (no burn risk)
        runway >= 2 years → 10
        runway >= 1 year  → 6
        runway >= 0.5 year → 2
        runway < 0.5 year  → 0  (also sets red_flag_cash_runway = True)
        FCF None or totalCash None → 5 (unknown)
    F3. Analyst target upside (0–10 pts):
        upside = (targetMeanPrice - currentPrice) / currentPrice
        upside >= 1.00 (100% upside) → 10
        upside >= 0.50 → 7
        upside >= 0.25 → 4
        upside >= 0.00 → 1
        upside < 0 or targetMeanPrice/currentPrice None → 0
    F4. Short squeeze potential (0–10 pts):
        shortPercentOfFloat >= 0.20 (20%) → 10
        >= 0.15 → 7
        >= 0.10 → 4
        >= 0.05 → 1
        < 0.05 or None → 0
    F5. Balance-sheet health / debt (0–5 pts):
        debtToEquity is None → 3 (unknown — micro-caps often lack this)
        debtToEquity <= 0.5 → 5
        <= 1.0 → 3
        <= 2.0 → 1
        > 2.0 → 0

    Returns a dict:
        {
            "f1_revenue_growth":    int,   # 0–15
            "f2_cash_runway":       int,   # 0–10
            "f3_analyst_upside":    int,   # 0–10
            "f4_short_squeeze":     int,   # 0–10
            "f5_debt_health":       int,   # 0–5
            "fundamental_total":    int,   # 0–50
            "cash_runway_years":    float | None,  # computed runway, None if not applicable
            "analyst_upside_pct":   float | None,  # computed upside as decimal, None if N/A
            "red_flag_cash_runway": bool,  # True if runway < 6 months
            "has_fundamentals":     bool,  # True (this dict always represents real fetch)
        }
    """
    f1 = f2 = f3 = f4 = f5 = 0
    cash_runway_years: Optional[float] = None
    analyst_upside_pct: Optional[float] = None
    red_flag_cash_runway = False

    # F1: revenue growth
    rev_growth = fundamentals.get("revenue_growth")
    if rev_growth is not None:
        try:
            rg = float(rev_growth)
            if rg >= 0.50:
                f1 = 15
            elif rg >= 0.25:
                f1 = 10
            elif rg >= 0.10:
                f1 = 5
            elif rg >= 0.00:
                f1 = 2
            else:
                f1 = 0
        except (TypeError, ValueError):
            pass

    # F2: cash runway
    total_cash = fundamentals.get("total_cash")
    fcf = fundamentals.get("free_cashflow")
    if fcf is not None and total_cash is not None:
        try:
            fcf_f = float(fcf)
            cash_f = float(total_cash)
            if fcf_f >= 0:
                # Cash-flow positive — no burn risk
                f2 = 10
                cash_runway_years = None  # infinite / not applicable
            else:
                burn_per_year = abs(fcf_f)
                if burn_per_year > 0:
                    cash_runway_years = cash_f / burn_per_year
                    if cash_runway_years >= 2.0:
                        f2 = 10
                    elif cash_runway_years >= 1.0:
                        f2 = 6
                    elif cash_runway_years >= 0.5:
                        f2 = 2
                    else:
                        f2 = 0
                        red_flag_cash_runway = True
                else:
                    f2 = 5
        except (TypeError, ValueError, ZeroDivisionError):
            f2 = 5
    else:
        f2 = 5  # unknown

    # F3: analyst target upside
    target_price = fundamentals.get("target_mean_price")
    if target_price is not None and current_price is not None:
        try:
            tp = float(target_price)
            cp = float(current_price)
            if cp > 0:
                analyst_upside_pct = (tp - cp) / cp
                if analyst_upside_pct >= 1.00:
                    f3 = 10
                elif analyst_upside_pct >= 0.50:
                    f3 = 7
                elif analyst_upside_pct >= 0.25:
                    f3 = 4
                elif analyst_upside_pct >= 0.00:
                    f3 = 1
                else:
                    f3 = 0
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # F4: short squeeze potential
    short_pct = fundamentals.get("short_percent_float")
    if short_pct is not None:
        try:
            sp = float(short_pct)
            if sp >= 0.20:
                f4 = 10
            elif sp >= 0.15:
                f4 = 7
            elif sp >= 0.10:
                f4 = 4
            elif sp >= 0.05:
                f4 = 1
            else:
                f4 = 0
        except (TypeError, ValueError):
            pass

    # F5: balance-sheet / debt
    dte = fundamentals.get("debt_to_equity")
    if dte is None:
        f5 = 3  # unknown — micro-caps often lack this; give moderate score
    else:
        try:
            d = float(dte)
            if d <= 0.5:
                f5 = 5
            elif d <= 1.0:
                f5 = 3
            elif d <= 2.0:
                f5 = 1
            else:
                f5 = 0
        except (TypeError, ValueError):
            f5 = 3

    fund_total = f1 + f2 + f3 + f4 + f5

    return {
        "f1_revenue_growth":    f1,
        "f2_cash_runway":       f2,
        "f3_analyst_upside":    f3,
        "f4_short_squeeze":     f4,
        "f5_debt_health":       f5,
        "fundamental_total":    fund_total,
        "cash_runway_years":    cash_runway_years,
        "analyst_upside_pct":   analyst_upside_pct,
        "red_flag_cash_runway": red_flag_cash_runway,
        "has_fundamentals":     True,
    }


def _no_fundamentals_score() -> dict[str, int]:
    """
    Return a placeholder fundamental score dict for tickers that were not
    enriched (not in top N or fetch failed). All fundamental components are 0.
    The technical score will be scaled to 0–100 for these tickers.
    """
    return {
        "f1_revenue_growth":    0,
        "f2_cash_runway":       0,
        "f3_analyst_upside":    0,
        "f4_short_squeeze":     0,
        "f5_debt_health":       0,
        "fundamental_total":    0,
        "cash_runway_years":    None,
        "analyst_upside_pct":   None,
        "red_flag_cash_runway": False,
        "has_fundamentals":     False,
    }


def _compute_speculation_score(quote: dict) -> int:
    """
    Backwards-compatible wrapper used during the initial technical scoring pass
    (before fundamentals are available). Returns the technical sub-score scaled
    to 0–100 so the initial sort order is meaningful.

    Internally calls _compute_technical_score and doubles the result.
    """
    tech = _compute_technical_score(quote)
    # Scale 0–50 → 0–100
    return max(0, min(tech["technical_total"] * 2, 100))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_screener(
    min_market_cap: int,
    max_market_cap: int,
    min_price: float,
    max_price: float,
    min_volume: int,
    sector: str,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Run the speculative stock screener with the given filters and return
    a DataFrame of candidates with computed speculation scores.

    Uses a 1-hour SQLite cache keyed by the filter combination. Pass
    force_refresh=True to bypass the cache.

    Parameters
    ----------
    min_market_cap : int — minimum market cap in USD
    max_market_cap : int — maximum market cap in USD
    min_price      : float — minimum stock price in USD
    max_price      : float — maximum stock price in USD
    min_volume     : int — minimum 3-month average daily volume
    sector         : str — Yahoo Finance sector string, or "" for all sectors
    force_refresh  : bool — if True, bypass cache and re-fetch from Yahoo

    Returns
    -------
    pd.DataFrame with columns:
        ticker, name, price, change_pct, market_cap, volume_3m, volume_10d,
        week52_high, week52_low, week52_high_chg_pct, week52_low_chg_pct,
        forward_pe, eps_ttm, speculation_score
    Returns an empty DataFrame if the screen returns no results or errors.
    """
    screen_key = (
        f"{min_market_cap}_{max_market_cap}_{min_price}_{max_price}"
        f"_{min_volume}_{sector or 'ALL'}"
    )

    if not force_refresh:
        cached = _load_screener_results(screen_key)
        if cached is not None:
            return _quotes_to_dataframe(cached)

    quotes = _run_screen(min_market_cap, max_market_cap, min_price, max_price, min_volume, sector)

    if not quotes:
        return pd.DataFrame()

    _save_screener_results(screen_key, quotes)
    return _quotes_to_dataframe(quotes)


def _quotes_to_dataframe(quotes: list[dict]) -> pd.DataFrame:
    """
    Convert a list of Yahoo Finance quote dicts to the canonical screener DataFrame.

    Returns a DataFrame with columns:
        ticker, name, price, change_pct, market_cap, volume_3m, volume_10d,
        week52_high, week52_low, week52_high_chg_pct, week52_low_chg_pct,
        forward_pe, eps_ttm, speculation_score
    Returns an empty DataFrame if quotes is empty.
    """
    if not quotes:
        return pd.DataFrame()

    rows = []
    for q in quotes:
        ticker = q.get("symbol", "")
        if not ticker:
            continue
        tech_breakdown = _compute_technical_score(q)
        no_fund = _no_fundamentals_score()
        # Initial score: technical total scaled to 0-100 (fundamentals not yet fetched)
        initial_score = max(0, min(tech_breakdown["technical_total"] * 2, 100))
        rows.append({
            "ticker": ticker.upper(),
            "name": q.get("shortName") or q.get("longName") or ticker,
            "price": q.get("regularMarketPrice"),
            "change_pct": q.get("regularMarketChangePercent"),
            "market_cap": q.get("marketCap"),
            "volume_3m": q.get("averageDailyVolume3Month"),
            "volume_10d": q.get("averageDailyVolume10Day"),
            "week52_high": q.get("fiftyTwoWeekHigh"),
            "week52_low": q.get("fiftyTwoWeekLow"),
            "week52_high_chg_pct": q.get("fiftyTwoWeekHighChangePercent"),
            "week52_low_chg_pct": q.get("fiftyTwoWeekLowChangePercent"),
            "forward_pe": q.get("forwardPE"),
            "eps_ttm": q.get("epsTrailingTwelveMonths"),
            "speculation_score": initial_score,
            # Fundamental columns — populated as None until enrich_with_fundamentals() is called
            "revenue_growth":        None,
            "cash_runway_years":     None,
            "short_percent_float":   None,
            "analyst_upside_pct":    None,
            "debt_to_equity":        None,
            "held_percent_insiders": None,
            "red_flag_cash_runway":  False,
            "has_fundamentals":      False,
            # Score breakdown as JSON string so it survives DataFrame serialization
            "score_breakdown":       json.dumps({**tech_breakdown, **no_fund}),
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.sort_values("speculation_score", ascending=False).reset_index(drop=True)
    return df


def enrich_with_fundamentals(
    df: pd.DataFrame,
    top_n: int = _DEFAULT_ENRICH_TOP_N,
) -> pd.DataFrame:
    """
    Enrich the top N rows of the screener DataFrame (by current speculation_score)
    with fundamental data fetched in parallel via yf.Ticker(symbol).info.

    For tickers outside the top N, fundamental columns remain None and
    has_fundamentals remains False.

    Recomputes speculation_score for enriched rows as:
        technical_total (0–50) + fundamental_total (0–50) = 0–100 blended score

    For non-enriched rows, speculation_score remains the technical-scaled score.

    Parameters
    ----------
    df    : pd.DataFrame — output of run_screener(), with score_breakdown column
    top_n : int — how many top-ranked tickers to fetch fundamentals for

    Returns
    -------
    pd.DataFrame — same columns as input plus populated fundamental columns for top N rows.
    The DataFrame is re-sorted by speculation_score descending.
    """
    if df.empty:
        return df

    df = df.copy()
    top_tickers = df.head(top_n)["ticker"].tolist()

    fund_map = fetch_fundamentals_for_tickers(top_tickers)

    for idx, row in df.iterrows():
        ticker = str(row["ticker"]).upper()
        if ticker not in fund_map:
            continue

        fund_data = fund_map[ticker]
        current_price = row.get("price")

        # Parse existing technical breakdown from the JSON string
        try:
            breakdown = json.loads(row.get("score_breakdown") or "{}")
        except (json.JSONDecodeError, TypeError):
            breakdown = {}

        fund_score = _compute_fundamental_score(fund_data, current_price)

        # Blended score
        tech_total = breakdown.get("technical_total", 0)
        fund_total = fund_score["fundamental_total"]
        blended_score = max(0, min(tech_total + fund_total, 100))

        df.at[idx, "speculation_score"]    = blended_score
        df.at[idx, "revenue_growth"]       = fund_data.get("revenue_growth")
        df.at[idx, "cash_runway_years"]    = fund_score["cash_runway_years"]
        df.at[idx, "short_percent_float"]  = fund_data.get("short_percent_float")
        df.at[idx, "analyst_upside_pct"]   = fund_score["analyst_upside_pct"]
        df.at[idx, "debt_to_equity"]       = fund_data.get("debt_to_equity")
        df.at[idx, "held_percent_insiders"]= fund_data.get("held_percent_insiders")
        df.at[idx, "red_flag_cash_runway"] = fund_score["red_flag_cash_runway"]
        df.at[idx, "has_fundamentals"]     = True
        df.at[idx, "score_breakdown"]      = json.dumps({
            **breakdown,
            **fund_score,
            "gross_margins":     fund_data.get("gross_margins"),
            "free_cashflow":     fund_data.get("free_cashflow"),
            "target_mean_price": fund_data.get("target_mean_price"),
        })

    # Enriched rows carry a true blended (tech+fund) score; non-enriched rows
    # carry a technical-only score scaled to 0-100. The two are not comparable,
    # so rank all enriched candidates above the "technicals only" remainder.
    df = df.sort_values(
        ["has_fundamentals", "speculation_score"], ascending=[False, False]
    ).reset_index(drop=True)
    return df
