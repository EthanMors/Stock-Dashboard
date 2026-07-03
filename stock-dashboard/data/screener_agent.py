"""AI-Powered Multi-Stage Stock Screener — backend orchestrator.

Finds "next big boom" candidates through a three-stage intelligence funnel and
evaluates how a candidate would fit the user's existing portfolio.

Funnel
------
Stage 1 — Python "Boom Score" (zero LLM cost)
    Multi-factor quant rank over a curated industry universe. Factors target
    early-stage outperformers: revenue/earnings growth & acceleration, price
    momentum + 52-week-high proximity, analyst upside, margins/quality, and
    short-squeeze / volume-surge "hype" potential. Returns the top 5 per pool.

Stage 2 — Catalyst & Sentiment ranking (Gemini 3.5 Flash)
    Feeds the top 5 (quant factors + business summary) to Flash, which ranks the
    top 2 by near-term catalyst velocity and "underappreciated boom" likelihood.

Stage 3 — Deep-dive boom/risk audit (Gemini 3.1 Pro)
    For the final 2, Pro delivers a boom-potential verdict, key risks, and a
    suggested entry/alert level.

Portfolio Fit (Gemini 3.1 Pro)
    For any candidate, explains the impact on the user's portfolio: the exposure
    it adds (sector/factor/theme), how it interacts with existing holdings
    (overlap, correlation, diversification), concentration impact, and a fit
    verdict with a suggested position size. Correlation/overlap are computed in
    Python and passed in — the LLM never does the math.

All Gemini calls go through ``data.agy_client.run_agy`` (see implementor-skills).
"""

import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone

import pandas as pd

from data.agy_client import PRO_MODEL, run_agy
from data.gemini_tracker import record_call
from data.fetcher import (
    get_stock_info,
    get_batch_history,
    get_next_earnings_date,
    get_quarterly_income_stmt,
)

# ---------------------------------------------------------------------------
# Curated industry pools (2026 boom-hunting universe)
# ---------------------------------------------------------------------------

INDUSTRY_POOLS: dict[str, list[str]] = {
    "Semiconductors & AI Hardware": [
        "NVDA", "AMD", "AVGO", "MU", "TSM", "ASML", "AMAT", "LRCX", "KLAC", "MRVL",
        "ARM", "ON", "SWKS", "QRVO", "MPWR", "ENTG", "TER", "COHR", "LSCC", "AEHR",
        "CRDO", "WOLF",
    ],
    "Cloud & AI Software": [
        "MSFT", "AMZN", "GOOGL", "PLTR", "DDOG", "NET", "ZS", "CRM", "SNOW", "MDB",
        "CRWD", "PATH", "GTLB", "HUBS", "BILL", "ESTC", "CFLT", "DOCN", "AI", "SOUN",
        "IOT", "NOW",
    ],
    "Biotech & Digital Health": [
        "TEM", "RXRX", "UTHR", "BNTX", "LEGN", "VRTX", "AMGN", "MRNA", "EXAS", "NTLA",
        "CRSP", "BEAM", "VKTX", "ALNY", "SRPT", "ARWR", "IONS", "INSM", "NBIX", "DXCM",
        "HIMS", "GH",
    ],
    "Fintech & Digital Finance": [
        "SQ", "PYPL", "SOFI", "HOOD", "NU", "AFRM", "V", "MA", "COIN", "UPST",
        "MARA", "RIOT", "TOST", "FOUR", "GPN", "FIS", "MQ", "DAVE", "FLYW", "WEX",
        "LC", "PSFE",
    ],
    "Nuclear & Energy Transition": [
        "OKLO", "SMR", "CEG", "VST", "GEV", "NXT", "FSLR", "ENPH", "SEDG", "RUN",
        "PLUG", "BE", "BLDP", "LEU", "UEC", "CCJ", "DNN", "NNE", "BWXT", "TLN",
        "SHLS", "STEM",
    ],
    "Space & Frontier Tech": [
        "RKLB", "LUNR", "ASTS", "ACHR", "JOBY", "KTOS", "AVAV", "PL", "RDW", "SPCE",
        "RCAT", "EVLV", "ONDS", "SATS", "IRDM", "VSAT", "BKSY", "SIDU", "MNTS",
    ],
}

# Special sentinel industry label for the dynamic Reddit-trending pool (see
# get_reddit_trending_pool below). Not a key in INDUSTRY_POOLS.
REDDIT_TRENDING_LABEL = "🔥 Reddit Trending (Dynamic)"

# Default Stage-1 factor weights (must sum to ~1.0; the page lets the user tune).
DEFAULT_WEIGHTS = {
    "growth":      0.25,   # revenue/earnings growth & acceleration
    "momentum":    0.25,   # SPY-relative price momentum + 52w-high proximity + earnings timing
    "analyst":     0.15,   # analyst target upside + institutional ownership
    "hype":        0.15,   # WSB sentiment + short-squeeze + volume-trend potential
    "smart_money": 0.20,   # 13F fund coverage & aggregate conviction weight
}

# ---------------------------------------------------------------------------
# Dynamic Reddit-trending pool
# ---------------------------------------------------------------------------

_WSB_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "wsb.db")


def get_reddit_trending_pool(limit: int = 20) -> list[str]:
    """Return the top `limit` tickers by total WSB mentions over the last 7 days.

    Reads daily_ticker_mentions from wsb.db (populated by pages/8_social.py). Returns
    an empty list if the DB/table doesn't exist yet or has no rows in the window —
    callers MUST handle the empty-list case with a UI notice, never assume non-empty.
    """
    if not os.path.exists(_WSB_DB_PATH):
        return []
    conn = sqlite3.connect(_WSB_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT ticker, SUM(mentions) AS total_mentions
            FROM daily_ticker_mentions
            WHERE date >= date('now', '-7 day')
            GROUP BY ticker
            ORDER BY total_mentions DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [r["ticker"].upper() for r in rows]


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------

def _safe(value):
    try:
        f = float(value)
        return f if f == f else None  # NaN guard
    except (TypeError, ValueError):
        return None


def _scale(value, lo: float, hi: float) -> float:
    """Linearly map value from [lo, hi] onto [0, 100], clamped."""
    if value is None:
        return 0.0
    if hi == lo:
        return 0.0
    pct = (value - lo) / (hi - lo)
    return max(0.0, min(100.0, pct * 100.0))


def _catalyst_timing_pts(days_to_earnings: int | None) -> float:
    """Score 0-100: closer upcoming earnings (within ~45 days) scores higher.

    None (unknown earnings date) is treated as neutral (50). A negative value
    (stale/past date data) is also treated as neutral. Beyond 45 days out scores 0.
    """
    if days_to_earnings is None or days_to_earnings < 0:
        return 50.0
    if days_to_earnings > 45:
        return 0.0
    return round(100.0 - (days_to_earnings / 45.0) * 100.0, 1)


# ---------------------------------------------------------------------------
# Stage 1 — Python Boom Score
# ---------------------------------------------------------------------------

def _momentum_returns(tickers: list[str]) -> dict[str, dict]:
    """Return {ticker: {"ret_3m", "ret_6m", "ret_3m_rel", "ret_6m_rel"}} from one batch fetch.

    ret_3m/ret_6m are absolute % returns. ret_3m_rel/ret_6m_rel are the ticker's return
    minus SPY's return over the same window (relative strength). SPY is fetched once via
    the same get_batch_history call by appending it to the requested ticker list — it is
    NOT counted as one of the scored tickers. Any *_rel value is None if SPY data or the
    ticker's own data is unavailable (caller must fall back to absolute returns).
    """
    universe = list(dict.fromkeys(tickers + ["SPY"]))
    out: dict[str, dict] = {
        t: {"ret_3m": None, "ret_6m": None, "ret_3m_rel": None, "ret_6m_rel": None}
        for t in tickers
    }
    try:
        hist = get_batch_history(tuple(universe), period="6mo")
    except Exception:
        hist = pd.DataFrame()
    if hist is None or hist.empty:
        return out

    def _returns(col: str):
        if col not in hist.columns:
            return None, None
        series = hist[col].dropna()
        if len(series) < 2:
            return None, None
        last = series.iloc[-1]
        first = series.iloc[0]
        r6 = (last / first - 1.0) * 100.0 if first else None
        r3 = None
        # ~63 trading days ≈ 3 months
        if len(series) > 63:
            ref = series.iloc[-63]
            r3 = (last / ref - 1.0) * 100.0 if ref else None
        return r3, r6

    spy_r3, spy_r6 = _returns("SPY")

    for t in tickers:
        r3, r6 = _returns(t)
        out[t]["ret_3m"] = r3
        out[t]["ret_6m"] = r6
        out[t]["ret_3m_rel"] = (r3 - spy_r3) if (r3 is not None and spy_r3 is not None) else None
        out[t]["ret_6m_rel"] = (r6 - spy_r6) if (r6 is not None and spy_r6 is not None) else None
    return out


def _smart_money_map() -> dict[str, dict]:
    """Aggregate 13F fund coverage per ticker from the local hedge-fund cache DB.

    Reads ONLY from the local SQLite cache via get_all_funds_from_db() — this makes
    zero network/EDGAR calls, so it is safe to call once per Stage-1 run over the
    whole universe. Returns {ticker: {"fund_count": int, "total_weight_pct": float,
    "funds": [fund_name, ...]}}. Empty dict if the cache is empty or unavailable.
    """
    try:
        from data.hedge_fund_fetcher import get_all_funds_from_db
        funds = get_all_funds_from_db()
    except Exception:
        funds = []

    out: dict[str, dict] = {}
    for f in funds:
        for h in f.get("holdings", []):
            t = str(h.get("ticker", "")).upper().strip()
            if not t:
                continue
            entry = out.setdefault(t, {"fund_count": 0, "total_weight_pct": 0.0, "funds": []})
            entry["fund_count"] += 1
            entry["total_weight_pct"] += float(h.get("pct_of_portfolio", 0.0) or 0.0)
            entry["funds"].append(f.get("name", ""))
    return out


def _wsb_map(tickers: list[str]) -> dict[str, dict]:
    """Batch-lookup wsb_ticker_summaries rows for the given tickers (one query, one connection).

    Returns {ticker: {"sentiment_score", "sentiment_label", "hype_level", "analyzed_at"}}.
    Missing tickers are simply absent from the returned dict. Empty dict if wsb.db/table
    doesn't exist yet (module is fresh-installed / Reddit page never used).
    """
    out: dict[str, dict] = {}
    if not tickers or not os.path.exists(_WSB_DB_PATH):
        return out
    conn = sqlite3.connect(_WSB_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in tickers)
        rows = conn.execute(
            f"""
            SELECT ticker, sentiment_score, sentiment_label, hype_level, analyzed_at
            FROM wsb_ticker_summaries
            WHERE ticker IN ({placeholders})
            """,
            [t.upper() for t in tickers],
        ).fetchall()
        for r in rows:
            out[r["ticker"].upper()] = dict(r)
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return out


def _revenue_growth_acceleration(quarterly: dict) -> float | None:
    """QoQ revenue growth acceleration = latest QoQ growth% minus prior QoQ growth%.

    `quarterly` is the {period_str: {line_item: value}} dict returned by
    fetcher.get_quarterly_income_stmt() (columns are quarter-end date strings, most
    recent first once sorted descending). Returns None if fewer than 3 quarters of
    'Total Revenue' data are available, or if any of the 3 values are zero/None.
    """
    if not quarterly:
        return None
    revenue_by_period: dict[str, float] = {}
    for period, line_items in quarterly.items():
        if not isinstance(line_items, dict):
            continue
        val = line_items.get("Total Revenue")
        if val is not None:
            try:
                revenue_by_period[period] = float(val)
            except (TypeError, ValueError):
                continue
    if len(revenue_by_period) < 3:
        return None
    ordered_periods = sorted(revenue_by_period.keys(), reverse=True)  # most recent first
    latest, prior, prior2 = (revenue_by_period[p] for p in ordered_periods[:3])
    if not prior or not prior2:
        return None
    growth_latest = (latest / prior - 1.0) * 100.0
    growth_prior = (prior / prior2 - 1.0) * 100.0
    return growth_latest - growth_prior


def _score_ticker(
    info: dict,
    mom: dict,
    weights: dict,
    smart_money: dict | None = None,
    wsb: dict | None = None,
    earnings_date: str | None = None,
    quarterly: dict | None = None,
) -> dict:
    """Compute the multi-factor boom score and the raw factor readouts for one ticker."""
    price = _safe(info.get("currentPrice")) or _safe(info.get("regularMarketPrice"))
    hi_52 = _safe(info.get("fiftyTwoWeekHigh"))
    rev_growth = _safe(info.get("revenueGrowth"))            # fraction, e.g. 0.42
    earn_growth = (_safe(info.get("earningsGrowth"))
                   or _safe(info.get("earningsQuarterlyGrowth")))
    gross_m = _safe(info.get("grossMargins"))
    target = _safe(info.get("targetMeanPrice"))
    short_float = _safe(info.get("shortPercentOfFloat"))     # fraction
    avg_vol = _safe(info.get("averageVolume"))
    inst_pct = _safe(info.get("heldPercentInstitutions"))
    net_purchase = _safe(info.get("netSharePurchaseActivity"))
    vol_10d = _safe(info.get("averageVolume10days")) or _safe(info.get("averageDailyVolume10Day"))

    ret_3m = mom.get("ret_3m")
    ret_6m = mom.get("ret_6m")
    ret_3m_rel = mom.get("ret_3m_rel")
    ret_6m_rel = mom.get("ret_6m_rel")

    # --- Days to next earnings -------------------------------------------
    days_to_earnings = None
    if earnings_date:
        try:
            days_to_earnings = (datetime.strptime(earnings_date, "%Y-%m-%d").date() - date.today()).days
        except ValueError:
            days_to_earnings = None

    # --- Revenue growth acceleration ---------------------------------------
    revenue_accel = _revenue_growth_acceleration(quarterly or {})

    # --- Growth & quality sub-score (0-100) -------------------------------
    rev_pts = _scale((rev_growth or 0) * 100, 5, 60)          # 5%→0, 60%+→100
    earn_pts = _scale((earn_growth or 0) * 100, 0, 80)
    margin_pts = _scale((gross_m or 0) * 100, 30, 80)
    accel_pts = _scale(revenue_accel, -10, 20) if revenue_accel is not None else None
    if accel_pts is not None:
        growth_score = 0.35 * rev_pts + 0.25 * earn_pts + 0.15 * margin_pts + 0.25 * accel_pts
    else:
        growth_score = 0.45 * rev_pts + 0.35 * earn_pts + 0.20 * margin_pts

    # --- Momentum sub-score (0-100) — SPY-relative, with catalyst timing --
    near_high = (price / hi_52 * 100.0) if (price and hi_52) else None
    near_high_pts = _scale(near_high, 60, 100)                # within 0-40% of high
    r3_pts = _scale(ret_3m_rel, -15, 30) if ret_3m_rel is not None else _scale(ret_3m, -10, 50)
    r6_pts = _scale(ret_6m_rel, -20, 50) if ret_6m_rel is not None else _scale(ret_6m, -10, 80)
    catalyst_pts = _catalyst_timing_pts(days_to_earnings)
    momentum_score = 0.30 * near_high_pts + 0.25 * r3_pts + 0.25 * r6_pts + 0.20 * catalyst_pts

    # --- Analyst / institutional sub-score (0-100) -------------------------
    upside = ((target / price - 1.0) * 100.0) if (target and price) else None
    upside_pts = _scale(upside, 0, 50)
    inst_pts = _scale((inst_pct or 0) * 100, 20, 80)
    if net_purchase is not None:
        if net_purchase > 0:
            inst_pts = min(100.0, inst_pts + 10.0)
        elif net_purchase < 0:
            inst_pts = max(0.0, inst_pts - 10.0)
    if upside is not None:
        analyst_score = 0.65 * upside_pts + 0.35 * inst_pts
    else:
        analyst_score = inst_pts

    # --- Hype sub-score (0-100) — WSB blend + short float + volume trend --
    short_pts = _scale((short_float or 0) * 100, 3, 25)       # 3%→0, 25%+→100
    vol_trend = (vol_10d / avg_vol) if (vol_10d and avg_vol) else None
    vol_trend_pts = _scale(vol_trend, 1.0, 3.0)                # 1x→0, 3x+→100
    wsb_pts = None
    if wsb and wsb.get("hype_level") is not None:
        hype_lvl = _safe(wsb.get("hype_level")) or 0.0
        sentiment = _safe(wsb.get("sentiment_score")) or 0.0
        wsb_pts = _scale(hype_lvl, 0, 10)
        if sentiment < 0:
            wsb_pts *= 0.5
    if wsb_pts is not None:
        hype_score = 0.45 * wsb_pts + 0.30 * short_pts + 0.25 * vol_trend_pts
    else:
        hype_score = 0.55 * short_pts + 0.45 * vol_trend_pts

    # --- Smart-money sub-score (0-100) --------------------------------------
    sm = smart_money or {}
    fund_count = sm.get("fund_count", 0)
    total_weight = sm.get("total_weight_pct", 0.0)
    fund_count_pts = _scale(fund_count, 0, 8)
    weight_pts = _scale(total_weight, 0, 20)
    smart_money_score = 0.6 * fund_count_pts + 0.4 * weight_pts

    boom_score = (
        weights.get("growth", 0)      * growth_score
        + weights.get("momentum", 0)    * momentum_score
        + weights.get("analyst", 0)     * analyst_score
        + weights.get("hype", 0)        * hype_score
        + weights.get("smart_money", 0) * smart_money_score
    )

    return {
        "ticker": str(info.get("symbol", "")).upper(),
        "name": info.get("shortName") or info.get("longName") or "",
        "sector": info.get("sector", ""),
        "industry": info.get("industry", ""),
        "price": price,
        "market_cap": _safe(info.get("marketCap")),
        "boom_score": round(boom_score, 1),
        "subscores": {
            "growth": round(growth_score, 1),
            "momentum": round(momentum_score, 1),
            "analyst": round(analyst_score, 1),
            "hype": round(hype_score, 1),
            "smart_money": round(smart_money_score, 1),
        },
        "factors": {
            "rev_growth_pct": round(rev_growth * 100, 1) if rev_growth is not None else None,
            "earn_growth_pct": round(earn_growth * 100, 1) if earn_growth is not None else None,
            "gross_margin_pct": round(gross_m * 100, 1) if gross_m is not None else None,
            "revenue_accel_pct": round(revenue_accel, 1) if revenue_accel is not None else None,
            "ret_3m_pct": round(ret_3m, 1) if ret_3m is not None else None,
            "ret_6m_pct": round(ret_6m, 1) if ret_6m is not None else None,
            "ret_3m_rel_pct": round(ret_3m_rel, 1) if ret_3m_rel is not None else None,
            "ret_6m_rel_pct": round(ret_6m_rel, 1) if ret_6m_rel is not None else None,
            "pct_of_52w_high": round(near_high, 1) if near_high is not None else None,
            "days_to_earnings": days_to_earnings,
            "analyst_upside_pct": round(upside, 1) if upside is not None else None,
            "institutional_pct": round(inst_pct * 100, 1) if inst_pct is not None else None,
            "short_pct_float": round(short_float * 100, 1) if short_float is not None else None,
            "vol_trend_x": round(vol_trend, 2) if vol_trend is not None else None,
            "smart_money_fund_count": fund_count,
            "smart_money_weight_pct": round(total_weight, 2),
            "recommendation": info.get("recommendationKey", ""),
            "business_summary": (info.get("longBusinessSummary", "") or "")[:600],
        },
    }


def _fetch_ticker_bundle(ticker: str) -> dict:
    """Fetch all per-ticker Stage-1 yfinance inputs. Safe to run inside a worker thread —
    each call goes through an @st.cache_data-wrapped fetcher function."""
    info = get_stock_info(ticker) or {}
    info.setdefault("symbol", ticker)
    earnings_date = get_next_earnings_date(ticker)
    quarterly = get_quarterly_income_stmt(ticker)
    return {"info": info, "earnings_date": earnings_date, "quarterly": quarterly}


def enrich_top_candidates_with_positioning(top_rows: list[dict]) -> list[dict]:
    """Attach a put/call-ratio positioning readout to the Stage-1 top candidates ONLY.

    Options chains are expensive to fetch, so this must NEVER be called on the full
    universe — only on the already-ranked top 5 (or fewer). Mutates each row in place
    by setting row["positioning"] to a dict (or None on any failure) and returns the
    same list for convenience.
    """
    import yfinance as yf
    from analytics.chain import load_raw_chain, clean_chain
    from analytics.positioning import put_call_ratio

    for row in top_rows:
        row["positioning"] = None
        ticker = row.get("ticker", "")
        if not ticker:
            continue
        try:
            t = yf.Ticker(ticker)
            exps = list(t.options)[:2]  # nearest 2 expirations only, keep it cheap
            if not exps:
                continue
            raw = load_raw_chain(ticker, exps)
            if raw is None or raw.empty:
                continue
            chain = clean_chain(raw)
            if chain.empty:
                continue
            pcr = put_call_ratio(chain, method="volume")
            ratio = pcr.get("pcr")
            row["positioning"] = {
                "put_call_ratio": round(ratio, 2) if isinstance(ratio, (int, float)) and ratio not in (float("inf"),) else None,
                "call_volume": int(pcr.get("call_total", 0) or 0),
                "put_volume": int(pcr.get("put_total", 0) or 0),
            }
        except Exception:
            continue
    return top_rows


def compute_quant_scores(tickers: list[str], weights: dict | None = None) -> list[dict]:
    """Stage 1: score and rank a list of tickers by boom potential (descending).

    Fetches all per-ticker yfinance data concurrently (ThreadPoolExecutor, max_workers=8)
    to keep the expanded ~20-25 ticker universe fast. Smart-money and WSB data are fetched
    once for the whole batch (DB reads, cheap). After ranking, attaches an options
    put/call-ratio readout to the top 5 only (see enrich_top_candidates_with_positioning).
    """
    weights = weights or DEFAULT_WEIGHTS
    tickers = [t.upper().strip() for t in tickers if t.strip()]
    if not tickers:
        return []

    mom = _momentum_returns(tickers)
    smart_money = _smart_money_map()
    wsb_rows = _wsb_map(tickers)

    bundles: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch_ticker_bundle, t): t for t in tickers}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                bundles[t] = fut.result()
            except Exception:
                bundles[t] = {"info": {}, "earnings_date": None, "quarterly": {}}

    scored: list[dict] = []
    for t in tickers:
        bundle = bundles.get(t, {})
        info = bundle.get("info") or {}
        if not info:
            continue
        row = _score_ticker(
            info=info,
            mom=mom.get(t, {}),
            weights=weights,
            smart_money=smart_money.get(t, {}),
            wsb=wsb_rows.get(t),
            earnings_date=bundle.get("earnings_date"),
            quarterly=bundle.get("quarterly") or {},
        )
        scored.append(row)

    scored.sort(key=lambda r: r["boom_score"], reverse=True)
    enrich_top_candidates_with_positioning(scored[:5])
    return scored


# ---------------------------------------------------------------------------
# Gemini runners + JSON parsing
# ---------------------------------------------------------------------------

def _run_flash(prompt: str) -> str:
    try:
        output, _ = run_agy(prompt, model=None, timeout=120)
        if output:
            record_call("flash")
        return output
    except Exception:
        return ""


def _run_pro(prompt: str) -> tuple[str, str]:
    try:
        output, stderr = run_agy(prompt, model=PRO_MODEL, timeout=180)
        if output:
            record_call("pro")
        return output, stderr
    except Exception as exc:
        return "", str(exc)


def _parse_json(raw: str) -> dict | None:
    if not raw:
        return None
    clean = re.sub(r"```(?:json)?\s*", "", raw).strip().strip("`")
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", clean, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                return None
    return None


# ---------------------------------------------------------------------------
# Data grounding helpers for Stage 2 and 3 prompts
# ---------------------------------------------------------------------------

def _fmt_pub_date(raw) -> str:
    """Format a Massive.com published_utc value (float unix ts or ISO string) as YYYY-MM-DD."""
    if not raw:
        return "?"
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc).strftime("%Y-%m-%d")
        return str(raw)[:10]
    except Exception:
        return "?"


def _news_block(ticker: str, limit: int = 6) -> str:
    """Return a bounded, prompt-ready block of recent headlines for `ticker`.

    Degrades gracefully to an explicit "no data" line when MASSIVE_API_KEY is unset,
    the API call fails, or no articles are found — never raises.
    """
    try:
        from data.news_fetcher import fetch_news
        articles = fetch_news(ticker, limit=limit) or []
    except Exception:
        articles = []
    if not articles:
        return "  (no recent news available for this ticker)"
    lines = []
    for a in articles[:limit]:
        title = (a.get("title") or "").strip()[:140]
        if not title:
            continue
        pub = _fmt_pub_date(a.get("published_utc"))
        publisher = a.get("publisher")
        pub_name = publisher.get("name", "") if isinstance(publisher, dict) else ""
        lines.append(f"  - [{pub}] {title} ({pub_name})")
    return "\n".join(lines) if lines else "  (no recent news available for this ticker)"


def _ownership_summary(ticker: str, max_funds: int = 5) -> list[dict]:
    """Return up to `max_funds` tracked 13F funds holding `ticker`, sorted by weight desc.

    Each item: {"fund": str, "pct_of_fund_portfolio": float}. Empty list if no tracked
    fund holds the ticker or the hedge-fund cache is empty/unavailable.
    """
    try:
        from data.hedge_fund_fetcher import get_all_funds_from_db
        funds = get_all_funds_from_db()
    except Exception:
        return []
    ticker_upper = ticker.upper()
    matches = []
    for f in funds:
        for h in f.get("holdings", []):
            if str(h.get("ticker", "")).upper() == ticker_upper:
                matches.append({
                    "fund": f.get("name", ""),
                    "pct_of_fund_portfolio": h.get("pct_of_portfolio", 0.0),
                })
                break
    matches.sort(key=lambda m: m["pct_of_fund_portfolio"], reverse=True)
    return matches[:max_funds]


def _macro_context_block(max_items: int = 3) -> str:
    """Return a bounded block of the most recent macro news analyses (last 72h)."""
    try:
        from data.macro_news_cache import get_recent_analyses
        rows = get_recent_analyses(hours=72, limit=max_items)
    except Exception:
        rows = []
    if not rows:
        return "  (no recent macro data available)"
    lines = []
    for r in rows[:max_items]:
        cat = r.get("macro_category", "neutral")
        summ = (r.get("summary") or "")[:180]
        lines.append(f"  - [{cat}] {summ}")
    return "\n".join(lines) if lines else "  (no recent macro data available)"


# ---------------------------------------------------------------------------
# Stage 2 — Flash catalyst ranking
# ---------------------------------------------------------------------------

def _candidate_brief(row: dict) -> str:
    f = row["factors"]
    s = row["subscores"]
    pos = row.get("positioning")
    pos_line = ""
    if pos:
        pos_line = (
            f"\n  Put/Call(vol)={pos.get('put_call_ratio')} "
            f"(call_vol={pos.get('call_volume')}, put_vol={pos.get('put_volume')})"
        )
    return (
        f"[{row['ticker']}] {row['name']} — {row['sector']} / {row['industry']}\n"
        f"  BoomScore {row['boom_score']} (growth {s['growth']}, momentum {s['momentum']}, "
        f"analyst {s['analyst']}, hype {s['hype']}, smart_money {s['smart_money']})\n"
        f"  RevGrowth={f['rev_growth_pct']}% EarnGrowth={f['earn_growth_pct']}% "
        f"GrossMargin={f['gross_margin_pct']}% RevAccel={f['revenue_accel_pct']}pp\n"
        f"  Ret3m(rel SPY)={f['ret_3m_rel_pct']}% Ret6m(rel SPY)={f['ret_6m_rel_pct']}% "
        f"%of52wHigh={f['pct_of_52w_high']}% DaysToEarnings={f['days_to_earnings']} "
        f"AnalystUpside={f['analyst_upside_pct']}% (rating: {f['recommendation']})\n"
        f"  ShortFloat={f['short_pct_float']}% VolTrend={f['vol_trend_x']}x "
        f"SmartMoney={f['smart_money_fund_count']} funds ({f['smart_money_weight_pct']}% agg weight) "
        f"InstOwnership={f['institutional_pct']}%"
        f"{pos_line}\n"
        f"  Business: {f['business_summary']}"
    )


def _candidate_brief_with_news(row: dict) -> str:
    """Stage-2-only variant of _candidate_brief: appends a bounded recent-news block."""
    base = _candidate_brief(row)
    news = _news_block(row["ticker"], limit=6)
    return f"{base}\n  RECENT NEWS:\n{news}"


_FLASH_PROMPT = """You are an equity catalyst analyst hunting for the "next big boom" stock. \
Below are the top quantitatively-ranked candidates in the {industry} sector. \
Identify the 2 with the strongest NEAR-TERM catalyst velocity and the highest odds of being \
an underappreciated boom (growth/story the market hasn't fully priced), NOT already over-hyped.

CANDIDATES:
{candidates_block}

Respond ONLY with valid JSON:
{{
  "ranked": [
    {{
      "ticker": "<symbol>",
      "rank": 1,
      "hype_score": <integer 1-10>,
      "catalysts": ["<near-term catalyst>", "<...>"],
      "boom_thesis": "<1-2 sentence reason this could be a breakout>",
      "risk_flag": "<main risk or 'over-hyped' if crowded>"
    }}
  ]
}}

Rules:
- ranked: exactly 2 items (the 2 best), rank 1 = highest conviction.
- hype_score: 1=ignored by market, 10=euphoric/crowded. Prefer 4-7 (room to run).
- Cite the actual numbers from the data above.
- Do NOT wrap JSON in markdown code fences.
"""


def rank_catalysts_flash(industry: str, top_candidates: list[dict]) -> dict:
    """Stage 2: Flash ranks the top 2 catalyst plays from the Stage-1 leaders."""
    if not top_candidates:
        return {"_error": "No candidates to rank."}
    block = "\n\n".join(_candidate_brief_with_news(r) for r in top_candidates[:5])
    prompt = _FLASH_PROMPT.format(industry=industry, candidates_block=block)
    raw = _run_flash(prompt)
    parsed = _parse_json(raw)
    if not parsed:
        return {"_error": f"Flash ranking failed.\n{raw[:400]}"}
    return parsed


# ---------------------------------------------------------------------------
# Stage 3 — Pro deep-dive
# ---------------------------------------------------------------------------

def _finalist_brief_deep(row: dict) -> str:
    """Stage-3-only variant of _candidate_brief: appends WSB sentiment, 13F ownership,
    and recent news for one finalist. Macro context is appended once at the prompt
    level (it's market-wide, not per-ticker) — see deep_dive_pro below."""
    base = _candidate_brief(row)
    ticker = row["ticker"]

    wsb = _wsb_map([ticker]).get(ticker.upper())
    if wsb:
        wsb_line = (
            f"  WSB Sentiment: {wsb.get('sentiment_label', 'neutral')} "
            f"(score {wsb.get('sentiment_score')}), hype {wsb.get('hype_level')}/10 "
            f"(as of {str(wsb.get('analyzed_at', '?'))[:10]})"
        )
    else:
        wsb_line = "  WSB Sentiment: no data on file"

    owners = _ownership_summary(ticker)
    if owners:
        own_line = "  13F Ownership: " + "; ".join(
            f"{o['fund']} ({o['pct_of_fund_portfolio']}% of fund)" for o in owners
        )
    else:
        own_line = "  13F Ownership: no tracked fund currently holds this ticker"

    news = _news_block(ticker, limit=6)

    return f"{base}\n{wsb_line}\n{own_line}\n  RECENT NEWS:\n{news}"


_PRO_PROMPT = """You are a senior analyst delivering a final boom/risk verdict on 2 finalist stocks \
in the {industry} sector. Use the quantitative profiles, WSB sentiment, 13F ownership, recent \
news, and macro context below.

FINALISTS:
{finalists_block}

=== MACRO CONTEXT (last 72h) ===
{macro_block}
=== END MACRO CONTEXT ===

Respond ONLY with valid JSON:
{{
  "verdicts": [
    {{
      "ticker": "<symbol>",
      "boom_potential": "<explosive|high|moderate|limited>",
      "conviction": "<high|medium|low>",
      "thesis": "<2-3 sentence boom thesis grounded in the data>",
      "key_catalysts": ["<catalyst>", "<...>"],
      "key_risks": ["<risk>", "<...>"],
      "suggested_entry": "<price or condition to start a position>",
      "alert_price": <number or null>
    }}
  ],
  "head_to_head": "<1-2 sentences: which finalist is the better boom bet right now and why>"
}}

Rules:
- verdicts: one per finalist.
- Be specific — cite tickers and the numbers provided. If WSB/13F/macro data is marked
  unavailable, do not invent it — say the data wasn't available and reason from what is.
- Do NOT wrap JSON in markdown code fences.
"""


def deep_dive_pro(industry: str, finalists: list[dict]) -> dict:
    """Stage 3: Pro produces the final boom/risk verdict for the 2 finalists.

    Grounds the prompt in real data: WSB sentiment + 13F ownership + recent news per
    finalist (via _finalist_brief_deep), plus one shared macro-context block. Every
    data source degrades gracefully to an explicit "no data" note — never raises.
    """
    if not finalists:
        return {"_error": "No finalists to analyze."}
    block = "\n\n".join(_finalist_brief_deep(r) for r in finalists[:2])
    macro_block = _macro_context_block()
    prompt = _PRO_PROMPT.format(industry=industry, finalists_block=block, macro_block=macro_block)
    raw, stderr = _run_pro(prompt)
    parsed = _parse_json(raw)
    if not parsed:
        return {"_error": f"Pro deep-dive failed. {stderr[:200]}\n{raw[:400]}"}
    return parsed


# ---------------------------------------------------------------------------
# Portfolio-fit agent
# ---------------------------------------------------------------------------

def _portfolio_correlation(candidate: str, holdings: list[str]) -> dict:
    """Compute the candidate's 6-month return correlation vs each holding + average."""
    syms = [candidate] + [h for h in holdings if h and h != candidate]
    result = {"avg_correlation": None, "per_holding": {}}
    if len(syms) < 2:
        return result
    try:
        hist = get_batch_history(tuple(syms), period="6mo")
    except Exception:
        hist = pd.DataFrame()
    if hist is None or hist.empty or candidate not in hist.columns:
        return result

    returns = hist.pct_change().dropna(how="all")
    if candidate not in returns.columns:
        return result

    corrs = []
    for h in holdings:
        if h == candidate or h not in returns.columns:
            continue
        c = returns[candidate].corr(returns[h])
        if c == c:  # not NaN
            result["per_holding"][h] = round(float(c), 2)
            corrs.append(c)
    if corrs:
        result["avg_correlation"] = round(float(sum(corrs) / len(corrs)), 2)
    return result


def _holdings_profile(holdings: list[str]) -> list[dict]:
    """Fetch lightweight sector/industry profile for each current holding."""
    profile = []
    for h in holdings:
        info = get_stock_info(h) or {}
        profile.append({
            "ticker": h,
            "sector": info.get("sector", "Unknown"),
            "industry": info.get("industry", "Unknown"),
        })
    return profile


_FIT_PROMPT = """You are a portfolio construction advisor. The investor is considering adding \
{candidate} to their portfolio. Explain the IMPACT this addition would have: the new exposure it \
introduces, how it interacts with existing holdings, and the effect on concentration and \
diversification. Use the computed data below — do NOT recompute correlations yourself.

=== CANDIDATE ===
{candidate_block}

=== CURRENT HOLDINGS (sector profile) ===
{holdings_block}

=== COMPUTED INTERACTION DATA ===
Average 6-month return correlation of {candidate} vs the portfolio: {avg_corr}
Per-holding correlation: {per_holding}
=== END DATA ===

Respond ONLY with valid JSON:
{{
  "fit_verdict": "<strong_diversifier|good_fit|redundant|concentration_risk|poor_fit>",
  "summary": "<2-3 sentence read on what adding this does to the portfolio>",
  "exposure_added": {{
    "sectors": ["<sector>"],
    "themes": ["<theme/factor, e.g. 'AI infrastructure', 'rate-sensitive growth'>"],
    "geography": "<brief note or 'US large-cap' etc.>"
  }},
  "interaction": [
    {{
      "ticker": "<existing holding>",
      "relationship": "<overlapping|complementary|hedging|correlated>",
      "detail": "<1 sentence: how the candidate interacts with this holding>"
    }}
  ],
  "concentration_impact": "<1-2 sentences on sector/factor concentration effect>",
  "diversification_benefit": "<low|medium|high> — <short reason citing correlation>",
  "suggested_position_size": "<e.g. '2-4% starter' with brief reasoning>",
  "risks": ["<portfolio-level risk of adding this>", "<...>"]
}}

Rules:
- interaction: cover the 3-5 most-related existing holdings (overlap or correlation).
- Cite the actual correlation numbers and sectors provided above.
- If the portfolio is empty, say so and analyze on a standalone basis.
- Do NOT wrap JSON in markdown code fences.
"""


def analyze_portfolio_fit(candidate_row: dict, holdings: list[str]) -> dict:
    """Pro agent: how does adding this candidate impact the portfolio?

    Args:
        candidate_row: a Stage-1 score row (from compute_quant_scores) for the candidate.
        holdings:      list of the user's current holding tickers.

    Returns:
        Parsed JSON dict (see schema), or {"_error": str} on failure.
    """
    candidate = candidate_row.get("ticker", "").upper()
    holdings = [h.upper().strip() for h in holdings if h and h.strip() and h.upper() != candidate]

    corr = _portfolio_correlation(candidate, holdings)
    profile = _holdings_profile(holdings)

    f = candidate_row.get("factors", {})
    candidate_block = (
        f"{candidate} — {candidate_row.get('name', '')}\n"
        f"  Sector: {candidate_row.get('sector', '')} / {candidate_row.get('industry', '')}\n"
        f"  Market cap: {candidate_row.get('market_cap')}\n"
        f"  RevGrowth={f.get('rev_growth_pct')}% EarnGrowth={f.get('earn_growth_pct')}% "
        f"GrossMargin={f.get('gross_margin_pct')}%\n"
        f"  Business: {f.get('business_summary', '')}"
    )
    holdings_block = (
        "\n".join(f"  {p['ticker']}: {p['sector']} / {p['industry']}" for p in profile)
        if profile else "  (no current holdings)"
    )
    per_holding = ", ".join(f"{k}={v}" for k, v in corr["per_holding"].items()) or "n/a"

    prompt = _FIT_PROMPT.format(
        candidate=candidate,
        candidate_block=candidate_block,
        holdings_block=holdings_block,
        avg_corr=corr["avg_correlation"] if corr["avg_correlation"] is not None else "n/a",
        per_holding=per_holding,
    )
    raw, stderr = _run_pro(prompt)
    parsed = _parse_json(raw)
    if not parsed:
        return {"_error": f"Portfolio-fit analysis failed. {stderr[:200]}\n{raw[:400]}"}
    parsed["_correlation"] = corr  # attach computed numbers for the UI
    return parsed
