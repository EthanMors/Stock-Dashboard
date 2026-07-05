"""Covered Options Strategy Desk agent.

Detects portfolio positions with covered-call capacity (>= 100 shares),
gathers real market/macro/technical/options data for a chosen ticker, and
runs a 4-persona + 1-moderator Gemini "roundtable" via the Antigravity
(`agy`) CLI to decide whether to sell options against the position, which
specific contract, and why.

Call budget per full roundtable run (documented, bounded):
    Round 1 (opinions)   : 4 parallel Flash calls  (macro, equity, technical, options)
    Round 2 (rebuttal)   : 1 combined Flash call    (all 4 personas rebut each other)
    Round 3 (moderator)  : 1 Pro call               (final verdict + recommendation)
    TOTAL = 6 agy calls per ticker per full analysis.

All numeric strategy math (max profit, breakeven, annualized yield, downside
cushion) is computed in Python and handed to the agents as ground truth — the
LLM never invents or recomputes numbers, only narrates/reasons about them.

Public API
----------
find_covered_call_eligible_positions(positions) -> list[dict]
find_near_eligible_positions(positions) -> list[dict]
assess_strategy_capabilities(cash, eligible, near_eligible) -> dict
run_covered_options_roundtable(ticker, position, macro_indicators) -> dict
"""

import json
import re
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import yfinance as yf

from analytics.chain import get_enriched_chain
from analytics.helpers import find_atm_strike
from analytics.positioning import max_pain as _analytics_max_pain
from analytics.volatility import iv_rank_percentile, iv_skew as _analytics_iv_skew, expected_move as _analytics_expected_move
from data.agy_client import PRO_MODEL, run_agy
from data.gemini_tracker import record_call
from data.fetcher import get_price_history, get_stock_info, get_next_earnings_date
from data import calculator

# ---------------------------------------------------------------------------
# Position field candidates (mirrors data/mpt_agent.py exactly)
# ---------------------------------------------------------------------------

_TICKER_FIELD_CANDIDATES = ["symbol", "ticker", "tickerSymbol", "stockSymbol", "sym"]
_QTY_FIELD_CANDIDATES = [
    "position", "qty", "quantity", "positionQty", "position_qty",
    "holdingQty", "holding_qty", "sharesHeld", "shares_held", "shares",
]
_COST_FIELD_CANDIDATES = [
    "costPrice", "cost_price", "avgCost", "avg_cost", "averageCost",
    "average_cost", "costBasis", "cost_basis", "avgUnitCost",
    "avg_unit_cost", "averagePrice", "average_price",
]
_MV_FIELD_CANDIDATES = [
    "marketValue", "market_value", "mktValue", "mkt_value",
    "positionValue", "position_value", "currentValue", "current_value",
]
_LAST_PRICE_FIELD_CANDIDATES = [
    "lastPrice", "last_price", "currentPrice", "current_price", "price",
]

# Minimum share count to be shown in the "near-eligible" (Almost There) table.
# Positions with shares in [NEAR_ELIGIBLE_MIN_SHARES, 100) are close to
# covered-call eligibility (100 shares) but not yet there.
NEAR_ELIGIBLE_MIN_SHARES = 80


def _extract_ticker(position: dict) -> str:
    for field in _TICKER_FIELD_CANDIDATES:
        val = position.get(field, "")
        if val and isinstance(val, str):
            return val.upper().strip()
    return ""


def _first_float(d: dict, candidates: list) -> float:
    for field in candidates:
        val = d.get(field)
        if val is not None:
            try:
                f = float(val)
                if f != 0.0:
                    return f
            except (TypeError, ValueError):
                continue
    return 0.0


def _extract_position_fields(position: dict) -> dict:
    """Extract (qty, avg_cost, market_value, last_price) from a Webull position dict."""
    qty = _first_float(position, _QTY_FIELD_CANDIDATES)
    avg_cost = _first_float(position, _COST_FIELD_CANDIDATES)
    market_val = _first_float(position, _MV_FIELD_CANDIDATES)
    last_price = _first_float(position, _LAST_PRICE_FIELD_CANDIDATES)
    if last_price == 0.0 and qty > 0 and market_val > 0:
        last_price = market_val / qty
    return {"qty": qty, "avg_cost": avg_cost, "market_value": market_val, "last_price": last_price}


# ---------------------------------------------------------------------------
# Step A: Eligible position detection
# ---------------------------------------------------------------------------

def find_covered_call_eligible_positions(positions: list) -> list[dict]:
    """Return one dict per position with >= 100 shares (covered-call capacity).

    Each dict has keys: ticker, shares, lots (int, shares // 100),
    cost_basis (avg cost per share), current_price, market_value,
    unrealized_pl (dollars), unrealized_pl_pct.
    """
    eligible: list[dict] = []
    for pos in positions:
        ticker = _extract_ticker(pos)
        if not ticker:
            continue
        fields = _extract_position_fields(pos)
        shares = fields["qty"]
        if shares < 100:
            continue
        lots = int(shares // 100)
        cost_basis = fields["avg_cost"]
        current_price = fields["last_price"]
        market_value = fields["market_value"]
        unrealized_pl = (current_price - cost_basis) * shares if cost_basis > 0 and current_price > 0 else 0.0
        unrealized_pl_pct = (
            ((current_price - cost_basis) / cost_basis * 100) if cost_basis > 0 else 0.0
        )
        eligible.append({
            "ticker": ticker,
            "shares": shares,
            "lots": lots,
            "cost_basis": cost_basis,
            "current_price": current_price,
            "market_value": market_value,
            "unrealized_pl": unrealized_pl,
            "unrealized_pl_pct": unrealized_pl_pct,
        })
    eligible.sort(key=lambda d: d["market_value"], reverse=True)
    return eligible


def find_near_eligible_positions(positions: list) -> list[dict]:
    """Return one dict per position with NEAR_ELIGIBLE_MIN_SHARES <= shares < 100
    (i.e. close to covered-call eligibility but not there yet).

    Each dict has keys: ticker, shares, shares_needed (100 - shares, float),
    current_price, cost_to_complete (shares_needed * current_price).
    """
    near_eligible: list[dict] = []
    for pos in positions:
        ticker = _extract_ticker(pos)
        if not ticker:
            continue
        fields = _extract_position_fields(pos)
        shares = fields["qty"]
        if shares < NEAR_ELIGIBLE_MIN_SHARES or shares >= 100:
            continue
        shares_needed = 100 - shares
        current_price = fields["last_price"]
        cost_to_complete = shares_needed * current_price if current_price > 0 else 0.0
        near_eligible.append({
            "ticker": ticker,
            "shares": shares,
            "shares_needed": shares_needed,
            "current_price": current_price,
            "cost_to_complete": cost_to_complete,
        })
    near_eligible.sort(key=lambda d: d["cost_to_complete"])
    return near_eligible


def assess_strategy_capabilities(cash: float, eligible: list, near_eligible: list) -> dict:
    """Assess which option strategies the account can support right now, using
    only the account's available cash and already-computed eligible /
    near-eligible position lists. Pure Python math — makes NO network or
    Gemini/agy calls.

    Parameters
    ----------
    cash          : Available cash balance (float). Callers should pass 0.0
                    if the real balance is unavailable.
    eligible      : Output of find_covered_call_eligible_positions().
    near_eligible : Output of find_near_eligible_positions().

    Returns
    -------
    dict with keys:
        cash                       : float, sanitized (never negative/None)
        can_covered_call           : bool
        covered_call_tickers       : list[str]
        can_cash_secured_put       : bool
        max_affordable_put_strike  : float (floor(cash / 100), in whole dollars)
        can_collar                 : bool (needs both a covered-call lot AND
                                     enough cash for a cash-secured put)
        can_wheel                  : bool (same condition as collar — a full
                                     wheel needs both legs available)
        affordable_lot_completions : list[dict] — near_eligible entries whose
                                     cost_to_complete is <= cash, each with an
                                     added "affordable": True key
        notes                      : list[str] — plain-English strategy notes
    """
    safe_cash = float(cash) if cash and cash > 0 else 0.0

    covered_call_tickers = [p["ticker"] for p in eligible]
    can_covered_call = len(covered_call_tickers) > 0

    max_affordable_put_strike = float(int(safe_cash // 100))
    can_cash_secured_put = max_affordable_put_strike >= 5.0

    affordable_lot_completions = [
        {**p, "affordable": True}
        for p in near_eligible
        if p.get("cost_to_complete", 0.0) > 0 and p["cost_to_complete"] <= safe_cash
    ]

    can_collar = can_covered_call and can_cash_secured_put
    can_wheel = can_covered_call and can_cash_secured_put

    notes: list[str] = []
    if can_covered_call:
        notes.append(
            f"Covered calls: you already hold a full 100-share lot in "
            f"{', '.join(covered_call_tickers)} — you can sell calls against these shares today."
        )
    else:
        notes.append("Covered calls: no position currently has a full 100-share lot.")

    if can_cash_secured_put:
        notes.append(
            f"Cash-secured puts need strike × 100 in cash set aside; with ${safe_cash:,.2f} "
            f"available you could secure up to a ${max_affordable_put_strike:,.0f} strike."
        )
    else:
        notes.append(
            f"Cash-secured puts: with only ${safe_cash:,.2f} available, you can't comfortably "
            "secure even a $5 strike (needs $500+ set aside)."
        )

    if affordable_lot_completions:
        tickers_afford = ", ".join(p["ticker"] for p in affordable_lot_completions)
        notes.append(
            f"Your cash could complete the lot on: {tickers_afford} — buying the remaining "
            "shares would unlock covered calls on that ticker."
        )

    if can_wheel:
        notes.append(
            "Wheel strategy: you have both a full lot for covered calls and enough cash for a "
            "cash-secured put, so running a full wheel (sell puts, get assigned into shares, "
            "sell covered calls, get called away, repeat) is workable right now."
        )
    else:
        notes.append(
            "Wheel strategy: needs both a full 100-share lot AND enough cash for a cash-secured "
            "put — not fully available yet."
        )

    return {
        "cash": safe_cash,
        "can_covered_call": can_covered_call,
        "covered_call_tickers": covered_call_tickers,
        "can_cash_secured_put": can_cash_secured_put,
        "max_affordable_put_strike": max_affordable_put_strike,
        "can_collar": can_collar,
        "can_wheel": can_wheel,
        "affordable_lot_completions": affordable_lot_completions,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Step B: Technical snapshot (EMA / RSI / MACD / support-resistance / trend)
# ---------------------------------------------------------------------------

def compute_technical_snapshot(ticker: str) -> dict:
    """Compute a compact technical-analysis dict for *ticker* from 1y daily history.

    Returns a dict with keys: price, ema20, ema50, ema200 (None if < 200 bars),
    rsi14, macd_line, macd_signal, macd_hist, support_60d, resistance_60d,
    trend ("uptrend"|"downtrend"|"sideways"), momentum ("overbought"|"oversold"|"neutral").
    Returns {"error": "<msg>"} if there is not enough price history.
    """
    df = get_price_history(ticker, period="1y")
    if df is None or df.empty or len(df) < 60:
        return {"error": f"Insufficient price history for {ticker}."}

    close = df["Close"].astype(float)

    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean() if len(close) >= 200 else None

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_series = 100 - (100 / (1 + rs))
    rsi_latest = float(rsi_series.iloc[-1]) if pd.notna(rsi_series.iloc[-1]) else 50.0

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = macd_line - macd_signal

    price = float(close.iloc[-1])
    support_60d = float(close.tail(60).min())
    resistance_60d = float(close.tail(60).max())

    e20 = float(ema20.iloc[-1])
    e50 = float(ema50.iloc[-1])
    if price > e20 > e50:
        trend = "uptrend"
    elif price < e20 < e50:
        trend = "downtrend"
    else:
        trend = "sideways"

    if rsi_latest >= 70:
        momentum = "overbought"
    elif rsi_latest <= 30:
        momentum = "oversold"
    else:
        momentum = "neutral"

    return {
        "price": price,
        "ema20": e20,
        "ema50": e50,
        "ema200": float(ema200.iloc[-1]) if ema200 is not None else None,
        "rsi14": round(rsi_latest, 2),
        "macd_line": round(float(macd_line.iloc[-1]), 4),
        "macd_signal": round(float(macd_signal.iloc[-1]), 4),
        "macd_hist": round(float(macd_hist.iloc[-1]), 4),
        "support_60d": support_60d,
        "resistance_60d": resistance_60d,
        "trend": trend,
        "momentum": momentum,
    }


# ---------------------------------------------------------------------------
# Step C: Options candidate chain (uses analytics/ helpers)
# ---------------------------------------------------------------------------

_TARGET_DTES = [30, 60, 90, 120]  # ~4 monthly expirations


def _select_target_expirations(available: list[str]) -> list[str]:
    """Pick the expiration closest to each of _TARGET_DTES, deduplicated, sorted."""
    if not available:
        return []
    today = datetime.now(timezone.utc).date()
    parsed = []
    for exp in available:
        try:
            d = datetime.strptime(exp, "%Y-%m-%d").date()
            parsed.append((exp, (d - today).days))
        except ValueError:
            continue
    parsed = [p for p in parsed if p[1] >= 0]
    if not parsed:
        return []

    selected: list[str] = []
    for target in _TARGET_DTES:
        best = min(parsed, key=lambda p: abs(p[1] - target))
        if best[0] not in selected:
            selected.append(best[0])
    selected.sort(key=lambda exp: datetime.strptime(exp, "%Y-%m-%d"))
    return selected


def get_candidate_chain(ticker: str, spot: float) -> dict:
    """Fetch and enrich the option chain for the next ~4 monthly expirations.

    Returns a dict with keys:
        expirations           : list[str] — expirations actually used
        earnings_date          : str "YYYY-MM-DD" or None
        front_month_expiration : str or None
        iv_rank                : dict (from analytics.volatility.iv_rank_percentile) or None
        iv_skew                : dict (from analytics.volatility.iv_skew) or None
        expected_move           : dict (from analytics.volatility.expected_move) or None
        max_pain               : float or None
        call_candidates        : list[dict] — OTM call candidates (covered-call side)
        put_candidates          : list[dict] — OTM put candidates (cash-secured-put side)
    Returns {"error": "<msg>"} if the ticker has no options or the chain is empty.
    """
    try:
        available = list(yf.Ticker(ticker).options)
    except Exception as exc:
        return {"error": f"Could not fetch expirations for {ticker}: {exc}"}

    if not available:
        return {"error": f"{ticker} has no listed options."}

    target_expirations = _select_target_expirations(available)
    if not target_expirations:
        return {"error": f"No valid future expirations found for {ticker}."}

    earnings_date = get_next_earnings_date(ticker)

    enriched = get_enriched_chain(ticker, tuple(target_expirations), spot)
    if enriched is None or enriched.empty:
        return {"error": f"Enriched chain is empty for {ticker}."}

    front_month = target_expirations[0]

    # mid_iv can arrive as object/string dtype when yfinance rows are sparse —
    # coerce so aggregations below never hit a non-numeric reduction.
    if "mid_iv" in enriched.columns:
        enriched["mid_iv"] = pd.to_numeric(enriched["mid_iv"], errors="coerce")

    # ── IV rank proxy: current front-month ATM mid_iv vs. 1y realized-vol series ──
    iv_rank_result = None
    front_chain = enriched[enriched["expiration"] == front_month]
    if not front_chain.empty:
        atm_strike = find_atm_strike(front_chain["strike"], spot)
        atm_rows = front_chain[front_chain["strike"] == atm_strike]
        current_iv = float(atm_rows["mid_iv"].mean()) if not atm_rows.empty and pd.notna(atm_rows["mid_iv"].mean()) else None
        if current_iv is not None:
            price_hist = get_price_history(ticker, period="1y")
            if price_hist is not None and not price_hist.empty and len(price_hist) > 30:
                daily_ret = price_hist["Close"].pct_change().dropna()
                realized_vol_series = daily_ret.rolling(20).std() * np.sqrt(252)
                realized_vol_series = realized_vol_series.dropna()
                iv_rank_result = iv_rank_percentile(current_iv, realized_vol_series, warn_if_short=False)
                iv_rank_result["note"] = (
                    "IV Rank is a proxy: current front-month ATM implied volatility ranked "
                    "against this stock's own trailing 1y 20-day realized-volatility distribution "
                    "(no persisted historical IV series is available in this app)."
                )

    skew_result = _analytics_iv_skew(enriched, front_month)
    expected_move_result = _analytics_expected_move(enriched, front_month, spot)
    max_pain_result = _analytics_max_pain(enriched, front_month)
    max_pain_strike = max_pain_result.get("max_pain_strike")

    def _mid_price(row) -> float:
        def _num(val) -> float:
            try:
                f = float(val)
            except (TypeError, ValueError):
                return 0.0
            return 0.0 if pd.isna(f) else f

        bid = _num(row.get("bid"))
        ask = _num(row.get("ask"))
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 4)
        return round(_num(row.get("lastprice")), 4)

    def _build_candidates(option_type: str) -> list[dict]:
        rows = enriched[(enriched["option_type"] == option_type) & (enriched["is_otm"])].copy()
        if rows.empty:
            return []

        def _safe_float(val, default: float = 0.0) -> float:
            try:
                f = float(val)
            except (TypeError, ValueError):
                return default
            return default if pd.isna(f) else f

        candidates = []
        for _, row in rows.iterrows():
            tte = _safe_float(row.get("tte"))
            if tte <= 0:
                continue
            dte = int(round(tte * 365.25))
            if dte <= 0:
                continue
            mid = _mid_price(row)
            if mid <= 0:
                continue
            strike = float(row["strike"])
            iv_val = float(row["mid_iv"]) if pd.notna(row.get("mid_iv")) else 0.0
            annualized_yield_pct = round((mid / strike) * (365.0 / dte) * 100, 2) if strike > 0 else 0.0
            if option_type == "call":
                downside_cushion_pct = round((mid / spot) * 100, 2) if spot > 0 else 0.0
            else:
                downside_cushion_pct = round(((spot - strike) / spot) * 100, 2) if spot > 0 else 0.0
            earnings_before_expiry = False
            if earnings_date:
                try:
                    exp_dt = datetime.strptime(row["expiration"], "%Y-%m-%d").date()
                    earn_dt = datetime.strptime(earnings_date, "%Y-%m-%d").date()
                    earnings_before_expiry = earn_dt <= exp_dt
                except ValueError:
                    pass
            candidates.append({
                "expiry": row["expiration"],
                "dte": dte,
                "strike": strike,
                "bid": round(_safe_float(row.get("bid")), 4),
                "ask": round(_safe_float(row.get("ask")), 4),
                "mid_premium": mid,
                "delta": round(_safe_float(row.get("delta")), 4),
                "iv": round(iv_val, 4),
                "open_interest": int(_safe_float(row.get("openinterest"))),
                "volume": int(_safe_float(row.get("volume"))),
                "annualized_yield_pct": annualized_yield_pct,
                "downside_cushion_pct": downside_cushion_pct,
                "earnings_before_expiry": earnings_before_expiry,
            })
        # Keep the "sweet spot" delta band (0.15-0.40 magnitude) and cap to 12 rows,
        # sorted by expiry then strike, so the prompt stays bounded.
        band = [c for c in candidates if 0.15 <= abs(c["delta"]) <= 0.40]
        pool = band if band else candidates
        pool.sort(key=lambda c: (c["expiry"], c["strike"]))
        return pool[:12]

    return {
        "expirations": target_expirations,
        "earnings_date": earnings_date,
        "front_month_expiration": front_month,
        "iv_rank": iv_rank_result,
        "iv_skew": skew_result,
        "expected_move": expected_move_result,
        "max_pain": max_pain_strike,
        "call_candidates": _build_candidates("call"),
        "put_candidates": _build_candidates("put"),
    }


# ---------------------------------------------------------------------------
# Step D: Strategy math — computed in Python, never by the LLM
# ---------------------------------------------------------------------------

def compute_strategy_math(contract: dict, cost_basis: float, spot: float, contract_type: str) -> dict:
    """Compute max profit / breakeven for a single candidate contract.

    contract_type: "covered_call" or "cash_secured_put".
    For covered_call: assumes the contract is written against already-owned
    shares at cost_basis.
    For cash_secured_put: assumes fresh capital equal to the strike is set aside.

    Returns dict: {max_profit_per_share, max_profit_pct, breakeven_price,
                   breakeven_move_pct, assignment_price}.
    """
    strike = contract["strike"]
    premium = contract["mid_premium"]

    if contract_type == "covered_call":
        basis = cost_basis if cost_basis > 0 else spot
        max_profit_per_share = (strike - basis) + premium
        max_profit_pct = round((max_profit_per_share / basis) * 100, 2) if basis > 0 else 0.0
        breakeven_price = round(basis - premium, 4)
        breakeven_move_pct = round(((breakeven_price - spot) / spot) * 100, 2) if spot > 0 else 0.0
        return {
            "max_profit_per_share": round(max_profit_per_share, 4),
            "max_profit_pct": max_profit_pct,
            "breakeven_price": breakeven_price,
            "breakeven_move_pct": breakeven_move_pct,
            "assignment_price": strike,
        }
    else:  # cash_secured_put
        max_profit_per_share = premium
        max_profit_pct = round((premium / strike) * 100, 2) if strike > 0 else 0.0
        breakeven_price = round(strike - premium, 4)
        breakeven_move_pct = round(((breakeven_price - spot) / spot) * 100, 2) if spot > 0 else 0.0
        return {
            "max_profit_per_share": round(max_profit_per_share, 4),
            "max_profit_pct": max_profit_pct,
            "breakeven_price": breakeven_price,
            "breakeven_move_pct": breakeven_move_pct,
            "assignment_price": strike,
        }


# ---------------------------------------------------------------------------
# Step E: Fundamentals + news + macro block builders (prompt grounding)
# ---------------------------------------------------------------------------

def _build_fundamentals_block(ticker: str) -> str:
    info = get_stock_info(ticker)
    if not info:
        return "  (no fundamental data available)"
    pe, pe_status = calculator.calc_pe_ratio(info)
    peg, peg_status = calculator.calc_peg_ratio(info)
    gm, gm_status = calculator.calc_gross_margin(info)
    om, om_status = calculator.calc_operating_margin(info)
    rev_yoy, rev_status = calculator.calc_revenue_yoy(info)
    eps_yoy, eps_status = calculator.calc_eps_yoy(info)
    name = info.get("longName") or info.get("shortName") or ticker
    sector = info.get("sector", "Unknown")
    industry = info.get("industry", "Unknown")
    summary = (info.get("longBusinessSummary") or "")[:600]
    lines = [
        f"  Name: {name}  |  Sector: {sector}  |  Industry: {industry}",
        f"  P/E: {pe if pe_status == 'ok' else 'N/A'}  |  PEG: {peg if peg_status == 'ok' else 'N/A'}",
        f"  Gross Margin: {gm if gm_status == 'ok' else 'N/A'}%  |  Operating Margin: {om if om_status == 'ok' else 'N/A'}%",
        f"  Revenue YoY: {rev_yoy if rev_status == 'ok' else 'N/A'}%  |  EPS YoY: {eps_yoy if eps_status == 'ok' else 'N/A'}%",
    ]
    if summary:
        lines.append(f"  Business summary: {summary}")
    return "\n".join(lines)


def _build_news_block(ticker: str, cached_news_entry: dict | None) -> str:
    """Use an already-analyzed news entry from session state if present (no extra
    Gemini call). Otherwise fetch raw headlines only (no sentiment analysis) so we
    don't burn an additional Flash call just for grounding context."""
    if cached_news_entry and "result" in cached_news_entry:
        r = cached_news_entry["result"]
        themes = ", ".join(r.get("key_themes", [])[:5])
        return (
            f"  Cached sentiment: {r.get('sentiment_label', 'neutral').upper()} "
            f"({r.get('sentiment_score', 0.0):+.2f}), impact {r.get('impact_level', 0)}/10\n"
            f"  Summary: {r.get('summary', '')[:300]}\n"
            + (f"  Themes: {themes}" if themes else "")
        )
    try:
        from data.news_fetcher import fetch_news
        articles = fetch_news(ticker, limit=5) or []
    except Exception:
        articles = []
    if not articles:
        return "  (no recent news available)"
    lines = []
    for art in articles[:5]:
        title = art.get("title", "")[:140]
        lines.append(f"  - {title}")
    return "\n".join(lines)


def _build_macro_block(macro_indicators: dict) -> str:
    order = ["unemployment", "fed_funds", "cpi", "treasury_10y", "real_gdp"]
    lines: list[str] = []
    for key in order:
        ind = (macro_indicators or {}).get(key)
        if not ind:
            continue
        label = ind.get("label", key)
        if "_error" in ind:
            lines.append(f"  {label}: unavailable")
            continue
        units = ind.get("units", "")
        unit_suffix = "%" if units == "%" else ""
        value = ind.get("value")
        value_str = f"{value:.2f}{unit_suffix}" if isinstance(value, (int, float)) else "n/a"
        date = ind.get("date", "")
        lines.append(f"  {label}: {value_str} (as of {date})")
    return "\n".join(lines) if lines else "  No macro data available."


def _fmt_candidates(candidates: list[dict]) -> str:
    if not candidates:
        return "  (no candidates in the 0.15-0.40 delta band)"
    header = f"  {'Expiry':<12}{'DTE':>5}{'Strike':>9}{'Mid':>8}{'Delta':>8}{'IV%':>7}{'OI':>8}{'AnnYield%':>11}{'Cushion%':>10}{'Earn?':>7}"
    lines = [header]
    for c in candidates:
        lines.append(
            f"  {c['expiry']:<12}{c['dte']:>5}{c['strike']:>9.2f}{c['mid_premium']:>8.2f}"
            f"{c['delta']:>8.3f}{c['iv']*100:>7.1f}{c['open_interest']:>8d}"
            f"{c['annualized_yield_pct']:>11.2f}{c['downside_cushion_pct']:>10.2f}"
            f"{'YES' if c['earnings_before_expiry'] else 'no':>7}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step F: Gemini runners + JSON parsing (same pattern as screener_agent.py)
# ---------------------------------------------------------------------------

def _run_flash(prompt: str, timeout: int = 90) -> str:
    try:
        output, _ = run_agy(prompt, model=None, timeout=timeout)
        if output:
            record_call("flash")
        return output
    except Exception:
        return ""


def _run_pro(prompt: str, timeout: int = 180) -> tuple[str, str]:
    try:
        output, stderr = run_agy(prompt, model=PRO_MODEL, timeout=timeout)
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
# Step G: Persona prompts (Round 1 — opinions)
# ---------------------------------------------------------------------------

_MACRO_STRATEGIST_PROMPT = """You are the MACRO STRATEGIST on a 4-person options strategy roundtable. \
Your ONLY job is to judge whether the current macro/rate/volatility environment favors SELLING option \
premium (covered calls / cash-secured puts) on equities right now, or favors staying away from short-vol \
strategies. You do not discuss the specific stock's fundamentals or chart — that is other personas' job.

=== CURRENT MACRO INDICATORS (FRED) ===
{macro_block}
=== END MACRO ===

Respond ONLY with valid JSON (no markdown fences):
{{
  "persona": "macro_strategist",
  "regime_view": "<risk_on|risk_off|mixed|late_cycle_caution>",
  "vol_environment": "<favorable_for_selling_premium|unfavorable_for_selling_premium|neutral>",
  "opinion": "<3-5 sentences: your read of the macro backdrop and what it implies for selling options right now>",
  "key_risk": "<1 sentence: the single biggest macro risk to a short-premium options strategy right now>"
}}
"""

_EQUITY_ANALYST_PROMPT = """You are the EQUITY ANALYST on a 4-person options strategy roundtable analyzing {ticker}. \
Your ONLY job is to classify whether this stock is a long-term compounder the investor should never risk losing \
to assignment, a fair-value/short-term holding where being called away or put to is fine, or a speculative/ \
overvalued name that should probably be trimmed via wheel-style option selling. You do not discuss macro or \
the option chain — that is other personas' job.

=== FUNDAMENTALS & BUSINESS SUMMARY ({ticker}) ===
{fundamentals_block}
=== END FUNDAMENTALS ===

=== RECENT NEWS ({ticker}) ===
{news_block}
=== END NEWS ===

Respond ONLY with valid JSON (no markdown fences):
{{
  "persona": "equity_analyst",
  "time_horizon_view": "<long_term_compounder|fair_value_hold|overvalued_trim_candidate|speculative>",
  "assignment_risk_tolerance": "<low|medium|high>",
  "opinion": "<3-5 sentences: your fundamental thesis on {ticker} and how it should shape option-selling risk tolerance>"
}}
"""

_TECHNICAL_ANALYST_PROMPT = """You are the TECHNICAL ANALYST on a 4-person options strategy roundtable analyzing {ticker}. \
Your ONLY job is to read the trend, momentum, and key levels, and translate that into strike-placement guidance \
for covered calls (above resistance?) and cash-secured puts (above support?). You do not discuss fundamentals, \
macro, or the option chain premiums — that is other personas' job.

=== TECHNICAL SNAPSHOT ({ticker}) ===
Price:            ${price:.2f}
EMA20:            ${ema20:.2f}
EMA50:            ${ema50:.2f}
EMA200:           {ema200_str}
RSI(14):          {rsi14:.1f}
MACD line:        {macd_line:.4f}
MACD signal:      {macd_signal:.4f}
MACD histogram:   {macd_hist:.4f}
60-day support:   ${support_60d:.2f}
60-day resistance: ${resistance_60d:.2f}
Trend:            {trend}
Momentum:         {momentum}
=== END TECHNICAL SNAPSHOT ===

Respond ONLY with valid JSON (no markdown fences):
{{
  "persona": "technical_analyst",
  "trend": "<uptrend|downtrend|sideways>",
  "momentum_read": "<overbought|oversold|neutral>",
  "strike_guidance": "<1-2 sentences: where to place call/put strikes relative to support/resistance/RSI>",
  "opinion": "<3-5 sentences: your technical read and what it means for option-selling timing and strike selection>"
}}
"""

_OPTIONS_STRATEGIST_PROMPT = """You are the OPTIONS STRATEGIST on a 4-person options strategy roundtable analyzing {ticker}. \
Your ONLY job is to read the REAL option chain data below and propose specific candidate contracts. All numbers \
below are pre-computed in Python — do not recompute them, just interpret and choose from them.

=== OPTIONS CONTEXT ({ticker}) ===
Spot Price:        ${spot:.2f}
Next Earnings:     {earnings_date_str}
Front-Month Expiry: {front_month}
IV Rank (proxy):   {iv_rank_str}
IV Skew:           {iv_skew_str}
Expected Move (front month): {expected_move_str}
Max Pain (front month):      {max_pain_str}
=== END CONTEXT ===

=== OTM CALL CANDIDATES (covered-call side, delta band 0.15-0.40) ===
{call_candidates_block}
=== END CALLS ===

=== OTM PUT CANDIDATES (cash-secured-put side, delta band 0.15-0.40) ===
{put_candidates_block}
=== END PUTS ===

Respond ONLY with valid JSON (no markdown fences):
{{
  "persona": "options_strategist",
  "iv_environment": "<rich|cheap|fair>",
  "earnings_flag": "<before_expiry|after_expiry|none_scheduled|unknown>",
  "top_call_candidate": {{"strike": <float or null>, "expiry": "<YYYY-MM-DD or null>", "rationale": "<1-2 sentences citing actual numbers from the table above>"}},
  "top_put_candidate": {{"strike": <float or null>, "expiry": "<YYYY-MM-DD or null>", "rationale": "<1-2 sentences citing actual numbers from the table above>"}},
  "opinion": "<3-5 sentences synthesizing IV rank, skew, expected move, earnings timing, and your candidate picks>"
}}
"""


def _build_technical_prompt(ticker: str, tech: dict) -> str:
    ema200_str = f"${tech['ema200']:.2f}" if tech.get("ema200") is not None else "N/A (< 200 bars of history)"
    return _TECHNICAL_ANALYST_PROMPT.format(
        ticker=ticker, price=tech["price"], ema20=tech["ema20"], ema50=tech["ema50"],
        ema200_str=ema200_str, rsi14=tech["rsi14"], macd_line=tech["macd_line"],
        macd_signal=tech["macd_signal"], macd_hist=tech["macd_hist"],
        support_60d=tech["support_60d"], resistance_60d=tech["resistance_60d"],
        trend=tech["trend"], momentum=tech["momentum"],
    )


def _build_options_prompt(ticker: str, spot: float, chain_ctx: dict) -> str:
    earnings_date_str = chain_ctx.get("earnings_date") or "None scheduled / unknown"
    iv_rank = chain_ctx.get("iv_rank")
    iv_rank_str = (
        f"{iv_rank.get('iv_rank'):.1f} (proxy vs realized-vol history, regime={iv_rank.get('regime')})"
        if iv_rank and iv_rank.get("iv_rank") is not None else "N/A (insufficient history)"
    )
    skew = chain_ctx.get("iv_skew") or {}
    skew_str = (
        f"{skew.get('skew_raw'):.4f} (25-delta put IV minus call IV)"
        if skew.get("skew_raw") is not None else "N/A"
    )
    em = chain_ctx.get("expected_move") or {}
    em_straddle = (em.get("straddle") or {}) if em else {}
    expected_move_str = (
        f"±${em_straddle.get('em_dollars'):.2f} (±{em_straddle.get('em_pct'):.1f}%)"
        if em_straddle.get("em_dollars") is not None else "N/A"
    )
    max_pain = chain_ctx.get("max_pain")
    max_pain_str = f"${max_pain:.2f}" if max_pain is not None else "N/A"

    return _OPTIONS_STRATEGIST_PROMPT.format(
        ticker=ticker, spot=spot, earnings_date_str=earnings_date_str,
        front_month=chain_ctx.get("front_month_expiration", "N/A"),
        iv_rank_str=iv_rank_str, iv_skew_str=skew_str, expected_move_str=expected_move_str,
        max_pain_str=max_pain_str,
        call_candidates_block=_fmt_candidates(chain_ctx.get("call_candidates", [])),
        put_candidates_block=_fmt_candidates(chain_ctx.get("put_candidates", [])),
    )


# ---------------------------------------------------------------------------
# Step H: Round 2 — combined rebuttal prompt
# ---------------------------------------------------------------------------

_REBUTTAL_PROMPT = """You are moderating Round 2 of a 4-person options strategy roundtable analyzing {ticker}. \
Below are the Round-1 opinions from all four specialists. For EACH persona, write a short rebuttal/adjustment: \
does anything another persona said change their view? Where do they still disagree?

=== MACRO STRATEGIST (Round 1) ===
{macro_opinion}

=== EQUITY ANALYST (Round 1) ===
{equity_opinion}

=== TECHNICAL ANALYST (Round 1) ===
{technical_opinion}

=== OPTIONS STRATEGIST (Round 1) ===
{options_opinion}

Respond ONLY with valid JSON (no markdown fences):
{{
  "macro_strategist_rebuttal": "<1-3 sentences>",
  "equity_analyst_rebuttal": "<1-3 sentences>",
  "technical_analyst_rebuttal": "<1-3 sentences>",
  "options_strategist_rebuttal": "<1-3 sentences>",
  "points_of_disagreement": ["<short phrase>", "<short phrase>"]
}}
"""


# ---------------------------------------------------------------------------
# Step I: Round 3 — moderator final verdict prompt
# ---------------------------------------------------------------------------

_MODERATOR_PROMPT = """You are the MODERATOR / PORTFOLIO MANAGER running a 4-person options strategy roundtable on {ticker}. \
You have the position details, all four Round-1 specialist opinions, and their Round-2 rebuttals below. Your job \
is to force any remaining disagreements to a resolution and issue ONE final, actionable recommendation.

Pre-computed strategy math for each candidate below is EXACT — never recompute or contradict these numbers, only \
reason about which candidate is best and why.

=== POSITION ===
Ticker: {ticker}
Shares held: {shares:.0f}  |  Lots (100-share blocks): {lots}
Cost basis/share: ${cost_basis:.2f}  |  Current price: ${spot:.2f}
Unrealized P/L: ${unrealized_pl:+,.2f} ({unrealized_pl_pct:+.1f}%)

=== ROUND 1 OPINIONS ===
Macro Strategist:    {macro_opinion}
Equity Analyst:      {equity_opinion}
Technical Analyst:   {technical_opinion}
Options Strategist:  {options_opinion}

=== ROUND 2 REBUTTALS ===
Macro Strategist rebuttal:   {macro_rebuttal}
Equity Analyst rebuttal:     {equity_rebuttal}
Technical Analyst rebuttal:  {technical_rebuttal}
Options Strategist rebuttal: {options_rebuttal}
Points of disagreement:      {disagreements}

=== CANDIDATE CONTRACTS WITH PRE-COMPUTED STRATEGY MATH (exact — do not recompute) ===
{candidates_math_block}
=== END CANDIDATES ===

Respond ONLY with valid JSON (no markdown fences):
{{
  "classification": "<long_term_compounder|fair_value_short_term_hold|overvalued_trim_candidate|speculative>",
  "classification_reason": "<2-3 sentences citing the Equity Analyst's view and your own synthesis>",
  "verdict": "<sell_covered_call|sell_cash_secured_put|wait_until_after_earnings|no_iv_too_low|no_too_cheap_to_cap|wheel_candidate|hold_no_options>",
  "verdict_reason": "<2-4 sentences synthesizing ALL four personas and resolving any disagreement>",
  "recommended_contracts": [
    {{
      "type": "<covered_call|cash_secured_put>",
      "strike": <float, must match one of the candidates above>,
      "expiry": "<YYYY-MM-DD, must match one of the candidates above>",
      "rationale": "<1-3 sentences: why this specific contract is the best risk/reward trade-off, citing its exact annualized yield and cushion numbers from the candidates block>"
    }}
  ],
  "education": {{
    "how_it_works": "<2-3 sentences explaining the mechanics of the recommended strategy in plain English>",
    "when_it_wins": "<1-2 sentences>",
    "when_it_loses": "<1-2 sentences, e.g. covered call underperforms buy-and-hold if the stock rips through the strike, or loses outright if the stock falls more than the premium collected>"
  }},
  "moderator_summary": "<3-4 sentence final synthesis, decision-first>"
}}

Rules:
- recommended_contracts: 1-2 items max, chosen ONLY from the candidates block above (exact strike/expiry match).
- If verdict is "hold_no_options", "no_iv_too_low", or "no_too_cheap_to_cap", recommended_contracts MUST be an empty list.
- Do not invent numbers — every strike/expiry you cite must exist in the candidates block above.
"""


def _fmt_candidates_math_block(candidates_with_math: list[dict]) -> str:
    if not candidates_with_math:
        return "  (no eligible candidates — chain empty or no strikes in the target delta band)"
    lines = []
    for c in candidates_with_math:
        m = c["math"]
        lines.append(
            f"  [{c['type']}] {c['expiry']} ${c['strike']:.2f} strike | premium ${c['mid_premium']:.2f} | "
            f"delta {c['delta']:.3f} | IV {c['iv']*100:.1f}% | ann. yield {c['annualized_yield_pct']:.2f}% | "
            f"cushion {c['downside_cushion_pct']:.2f}% | earnings-before-expiry: {'YES' if c['earnings_before_expiry'] else 'no'} | "
            f"max profit/share ${m['max_profit_per_share']:.2f} ({m['max_profit_pct']:.2f}%) | "
            f"breakeven ${m['breakeven_price']:.2f} ({m['breakeven_move_pct']:+.2f}% from spot)"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step J: Public orchestrator
# ---------------------------------------------------------------------------

def run_covered_options_roundtable(
    ticker: str,
    position: dict,
    macro_indicators: dict,
    cached_news_entry: dict | None = None,
) -> dict:
    """Run the full 4-persona + moderator roundtable for one covered-call-eligible ticker.

    Parameters
    ----------
    ticker            : Uppercase ticker string.
    position          : One eligible-position dict from find_covered_call_eligible_positions().
    macro_indicators  : dict from data.fred_fetcher.get_macro_indicators() (may be {}).
    cached_news_entry : Optional already-analyzed news entry from
                        st.session_state.news_results.get(ticker) (avoids an extra Gemini call).

    Returns
    -------
    dict with keys: classification, classification_reason, verdict, verdict_reason,
    recommended_contracts (list of dicts merged with pre-computed math),
    education (dict), discussion (dict with all round-1/round-2/moderator raw text
    and parsed opinions), context (technical snapshot + candidate chain dict).
    Returns {"_error": str} on unrecoverable failure (e.g., no price history or no chain).
    """
    spot = position.get("current_price") or 0.0
    if spot <= 0:
        info = get_stock_info(ticker)
        spot = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0.0)
    if spot <= 0:
        return {"_error": f"Could not determine a current price for {ticker}."}

    tech = compute_technical_snapshot(ticker)
    if "error" in tech:
        return {"_error": tech["error"]}

    chain_ctx = get_candidate_chain(ticker, spot)
    if "error" in chain_ctx:
        return {"_error": chain_ctx["error"]}

    macro_block = _build_macro_block(macro_indicators)
    fundamentals_block = _build_fundamentals_block(ticker)
    news_block = _build_news_block(ticker, cached_news_entry)

    # ── Round 1: 4 parallel Flash opinion calls ────────────────────────────
    from concurrent.futures import ThreadPoolExecutor

    prompts = {
        "macro": _MACRO_STRATEGIST_PROMPT.format(macro_block=macro_block),
        "equity": _EQUITY_ANALYST_PROMPT.format(
            ticker=ticker, fundamentals_block=fundamentals_block, news_block=news_block,
        ),
        "technical": _build_technical_prompt(ticker, tech),
        "options": _build_options_prompt(ticker, spot, chain_ctx),
    }
    raw_round1: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_run_flash, p): key for key, p in prompts.items()}
        for fut in futures:
            key = futures[fut]
            raw_round1[key] = fut.result()

    parsed_round1 = {key: (_parse_json(raw) or {}) for key, raw in raw_round1.items()}

    macro_opinion = parsed_round1["macro"].get("opinion", raw_round1["macro"][:400] or "No response.")
    equity_opinion = parsed_round1["equity"].get("opinion", raw_round1["equity"][:400] or "No response.")
    technical_opinion = parsed_round1["technical"].get("opinion", raw_round1["technical"][:400] or "No response.")
    options_opinion = parsed_round1["options"].get("opinion", raw_round1["options"][:400] or "No response.")

    # ── Round 2: 1 combined rebuttal Flash call ────────────────────────────
    rebuttal_prompt = _REBUTTAL_PROMPT.format(
        ticker=ticker, macro_opinion=macro_opinion, equity_opinion=equity_opinion,
        technical_opinion=technical_opinion, options_opinion=options_opinion,
    )
    raw_rebuttal = _run_flash(rebuttal_prompt, timeout=90)
    parsed_rebuttal = _parse_json(raw_rebuttal) or {}

    # ── Build candidates-with-math list for the moderator ─────────────────
    candidates_with_math: list[dict] = []
    for c in chain_ctx.get("call_candidates", []):
        math = compute_strategy_math(c, position.get("cost_basis", 0.0), spot, "covered_call")
        candidates_with_math.append({**c, "type": "covered_call", "math": math})
    for c in chain_ctx.get("put_candidates", []):
        math = compute_strategy_math(c, position.get("cost_basis", 0.0), spot, "cash_secured_put")
        candidates_with_math.append({**c, "type": "cash_secured_put", "math": math})

    # ── Round 3: 1 Pro moderator call ──────────────────────────────────────
    moderator_prompt = _MODERATOR_PROMPT.format(
        ticker=ticker,
        shares=position.get("shares", 0.0),
        lots=position.get("lots", 0),
        cost_basis=position.get("cost_basis", 0.0),
        spot=spot,
        unrealized_pl=position.get("unrealized_pl", 0.0),
        unrealized_pl_pct=position.get("unrealized_pl_pct", 0.0),
        macro_opinion=macro_opinion, equity_opinion=equity_opinion,
        technical_opinion=technical_opinion, options_opinion=options_opinion,
        macro_rebuttal=parsed_rebuttal.get("macro_strategist_rebuttal", "N/A"),
        equity_rebuttal=parsed_rebuttal.get("equity_analyst_rebuttal", "N/A"),
        technical_rebuttal=parsed_rebuttal.get("technical_analyst_rebuttal", "N/A"),
        options_rebuttal=parsed_rebuttal.get("options_strategist_rebuttal", "N/A"),
        disagreements=", ".join(parsed_rebuttal.get("points_of_disagreement", [])) or "None flagged",
        candidates_math_block=_fmt_candidates_math_block(candidates_with_math),
    )
    raw_moderator, moderator_stderr = _run_pro(moderator_prompt, timeout=180)
    if not raw_moderator:
        return {"_error": f"Moderator (Gemini Pro) returned no response. {moderator_stderr[:200]}"}

    moderator_result = _parse_json(raw_moderator)
    if moderator_result is None:
        return {"_error": f"Could not parse moderator response.\n\nRaw output:\n{raw_moderator[:500]}"}

    # ── Merge pre-computed math into the moderator's recommended contracts ─
    lookup = {(c["strike"], c["expiry"], c["type"]): c for c in candidates_with_math}
    enriched_recs = []
    for rec in moderator_result.get("recommended_contracts", []) or []:
        key = (rec.get("strike"), rec.get("expiry"), rec.get("type"))
        matched = lookup.get(key)
        if matched is None:
            # try a float-tolerant match
            for k, v in lookup.items():
                if k[1] == rec.get("expiry") and k[2] == rec.get("type") and abs((k[0] or 0) - (rec.get("strike") or 0)) < 0.01:
                    matched = v
                    break
        if matched is not None:
            enriched_recs.append({**rec, **matched["math"],
                                   "mid_premium": matched["mid_premium"],
                                   "delta": matched["delta"], "iv": matched["iv"],
                                   "dte": matched["dte"],
                                   "annualized_yield_pct": matched["annualized_yield_pct"],
                                   "downside_cushion_pct": matched["downside_cushion_pct"]})
        else:
            enriched_recs.append(rec)

    education = moderator_result.get("education", {})
    if enriched_recs:
        best = enriched_recs[0]
        education["max_profit"] = (
            f"${best.get('max_profit_per_share', 0):.2f}/share "
            f"({best.get('max_profit_pct', 0):+.2f}%) if assigned/exercised at ${best.get('strike', 0):.2f}."
        )
        education["breakeven"] = (
            f"${best.get('breakeven_price', 0):.2f} "
            f"({best.get('breakeven_move_pct', 0):+.2f}% from today's spot of ${spot:.2f})."
        )
    else:
        education.setdefault("max_profit", "N/A — no contract recommended.")
        education.setdefault("breakeven", "N/A — no contract recommended.")

    discussion = {
        "round1_raw": raw_round1,
        "round1_parsed": parsed_round1,
        "round2_raw": raw_rebuttal,
        "round2_parsed": parsed_rebuttal,
        "round3_raw": raw_moderator,
    }

    return {
        "classification": moderator_result.get("classification", "fair_value_short_term_hold"),
        "classification_reason": moderator_result.get("classification_reason", ""),
        "verdict": moderator_result.get("verdict", "hold_no_options"),
        "verdict_reason": moderator_result.get("verdict_reason", ""),
        "recommended_contracts": enriched_recs,
        "education": education,
        "moderator_summary": moderator_result.get("moderator_summary", ""),
        "discussion": discussion,
        "context": {
            "technical": tech,
            "chain": {k: v for k, v in chain_ctx.items() if k not in ("call_candidates", "put_candidates")},
            "call_candidates": chain_ctx.get("call_candidates", []),
            "put_candidates": chain_ctx.get("put_candidates", []),
        },
    }
