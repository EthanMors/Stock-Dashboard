# Plan: Covered Options Strategy Desk

## Overview

Add a new **"Covered Options Strategy Desk"** section to the Dashboard tab of
`pages/9_portfolio.py`. It detects portfolio holdings with >= 100 shares
(covered-call capacity), lets the user pick one (or run all), gathers real
market/macro/technical/options data for that ticker, and runs a 4-persona +
1-moderator Gemini "roundtable" (via `data/agy_client.run_agy` only) to decide
whether to sell options against the position, which specific contract to sell,
and why — with a plain-English education block and the full agent transcript
visible in an expander. Results are cached in `portfolio.db` (new table
`covered_options_analysis`, 24h TTL) so reopening the page does not re-burn
Gemini calls; a "Refresh analysis" button forces a re-run.

When this plan is fully executed: opening the Portfolio page → Dashboard tab
shows a new section listing covered-call-eligible positions, letting the user
run the roundtable per ticker (or all at once), and rendering a decision card,
a recommended-contract table, an education block with real computed
breakeven/max-profit numbers, and an expander with the full multi-agent
discussion transcript.

---

## Files to Create

- `stock-dashboard/data/covered_options_agent.py` — the entire new domain
  module: eligible-position detection, technical snapshot (EMA/RSI/MACD/
  support-resistance), options candidate-chain builder (using `analytics/`
  helpers for IV rank/skew/expected move/max pain), earnings-date lookup,
  strategy math (max profit / breakeven, computed in Python — never by the
  LLM), and the 4-persona + 1-moderator Gemini roundtable orchestrator.
  Follows the exact structure of `data/mpt_agent.py` (pure-Python metrics +
  Gemini interpretation in one module) and the JSON/runner conventions of
  `data/screener_agent.py`.

## Files to Modify

- `stock-dashboard/db/portfolio_schema.sql` — add one new table
  `covered_options_analysis` (append at the end of the file; do not touch
  existing tables).
- `stock-dashboard/data/portfolio_cache.py` — add
  `save_covered_options_analysis()`, `get_latest_covered_options_analysis()`,
  `is_covered_options_analysis_fresh()` (append at the end of the file, same
  pattern as the existing `mpt_analysis` cache helpers).
- `stock-dashboard/pages/9_portfolio.py` — add imports, a new
  `st.session_state.covered_options_results` dict, and a new
  "Covered Options Strategy Desk" section rendered at the end of the
  `with _tab_dash:` block (Dashboard tab), after the existing "Details —
  per-ticker news & MPT analytics" expander and before the
  `# TAB 2: NEWS` comment block.

## Database Changes

New table `covered_options_analysis` in `db/portfolio.db` (schema lives in
`db/portfolio_schema.sql`, same file the rest of `portfolio.db`'s tables use):

```
id                          INTEGER PK AUTOINCREMENT
ticker                      TEXT NOT NULL
chain_snapshot_date         TEXT NOT NULL   -- "YYYY-MM-DD", today's date when analyzed
spot_price                  REAL
classification              TEXT            -- e.g. "long_term_compounder"
classification_reason       TEXT
verdict                      TEXT            -- e.g. "sell_covered_call"
verdict_reason              TEXT
recommended_contracts_json  TEXT            -- JSON list of contract dicts (see schema below)
education_json               TEXT           -- JSON dict (how_it_works/max_profit/breakeven/wins/loses)
discussion_json              TEXT           -- JSON dict with full persona transcript
context_json                 TEXT           -- JSON dict: technical snapshot + candidate chain + iv metrics (for display without re-fetching)
analyzed_at                  TEXT NOT NULL  -- ISO UTC "YYYY-MM-DDTHH:MM:SS"
```

Indexes: `idx_coa_ticker` on `(ticker)`, `idx_coa_lookup` on
`(ticker, chain_snapshot_date, analyzed_at)`.

---

## Step-by-Step Implementation

### Step 1 — Append the new table to `db/portfolio_schema.sql`

**File:** `stock-dashboard/db/portfolio_schema.sql`
**Location:** Append at the very end of the file (after the existing
`idx_mpta_ticker_key` index statement, which is currently the last line).
**Action:** Add exactly this block:

```sql

CREATE TABLE IF NOT EXISTS covered_options_analysis (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                      TEXT NOT NULL,
    chain_snapshot_date         TEXT NOT NULL,
    spot_price                  REAL,
    classification              TEXT,
    classification_reason       TEXT,
    verdict                     TEXT,
    verdict_reason              TEXT,
    recommended_contracts_json  TEXT,
    education_json              TEXT,
    discussion_json              TEXT,
    context_json                 TEXT,
    analyzed_at                  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_coa_ticker
    ON covered_options_analysis (ticker);

CREATE INDEX IF NOT EXISTS idx_coa_lookup
    ON covered_options_analysis (ticker, chain_snapshot_date, analyzed_at);
```

**Why:** Every new table needs its own `CREATE TABLE IF NOT EXISTS` block in
the owning db's schema file (see `implementor-skills.md` → "Adding a New
Schema File"). `portfolio.db` is correct per `planner-skills.md` because this
is an AI analysis result tied to a specific ticker.

---

### Step 2 — Add cache helpers to `data/portfolio_cache.py`

**File:** `stock-dashboard/data/portfolio_cache.py`
**Location:** Append at the very end of the file, immediately before the
final two lines:

```python
# ---------------------------------------------------------------------------
# Initialize DB tables on import
# ---------------------------------------------------------------------------
init_db()
```

**Action:** Insert this new block right before that final comment + `init_db()`
call (i.e., after the `is_mpt_analysis_fresh()` function and before the
"Initialize DB tables on import" comment):

```python
# ---------------------------------------------------------------------------
# Covered Options Analysis cache helpers
# ---------------------------------------------------------------------------

_COVERED_OPTIONS_TTL_HOURS = 24  # cached roundtable analysis is fresh for 24h


def save_covered_options_analysis(
    ticker: str,
    chain_snapshot_date: str,
    spot_price: float,
    result_dict: dict,
    context_dict: dict,
) -> None:
    """Persist a covered-options roundtable analysis for *ticker*.

    Parameters
    ----------
    ticker              : Stock ticker (will be uppercased).
    chain_snapshot_date : "YYYY-MM-DD" — the date the option chain was pulled.
    spot_price           : Spot price at analysis time.
    result_dict          : The dict returned by
                            covered_options_agent.run_covered_options_roundtable().
                            Must contain keys: classification, classification_reason,
                            verdict, verdict_reason, recommended_contracts,
                            education, discussion.
    context_dict         : The pre-computed Python context dict (technical snapshot,
                            candidate chain, iv metrics) so the UI can redisplay it
                            without re-fetching from yfinance.
    """
    ticker = ticker.upper()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO covered_options_analysis (
                ticker, chain_snapshot_date, spot_price,
                classification, classification_reason,
                verdict, verdict_reason,
                recommended_contracts_json, education_json,
                discussion_json, context_json, analyzed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ticker,
                chain_snapshot_date,
                spot_price,
                result_dict.get("classification"),
                result_dict.get("classification_reason"),
                result_dict.get("verdict"),
                result_dict.get("verdict_reason"),
                json.dumps(result_dict.get("recommended_contracts", [])),
                json.dumps(result_dict.get("education", {})),
                json.dumps(result_dict.get("discussion", {})),
                json.dumps(context_dict or {}),
                now_iso,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_latest_covered_options_analysis(ticker: str) -> Optional[dict]:
    """Return the most recent covered-options analysis row for *ticker*, or None.

    Returned dict keys: ticker, chain_snapshot_date, spot_price, classification,
    classification_reason, verdict, verdict_reason,
    recommended_contracts (list, deserialized), education (dict, deserialized),
    discussion (dict, deserialized), context (dict, deserialized), analyzed_at.
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT * FROM covered_options_analysis
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
    for json_field, out_field in (
        ("recommended_contracts_json", "recommended_contracts"),
        ("education_json", "education"),
        ("discussion_json", "discussion"),
        ("context_json", "context"),
    ):
        raw = data.pop(json_field, None) or ("[]" if out_field == "recommended_contracts" else "{}")
        try:
            data[out_field] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            data[out_field] = [] if out_field == "recommended_contracts" else {}
    return data


def is_covered_options_analysis_fresh(analyzed_at_str: str) -> bool:
    """Return True if *analyzed_at_str* (UTC ISO) is within _COVERED_OPTIONS_TTL_HOURS."""
    try:
        analyzed_at = datetime.fromisoformat(analyzed_at_str).replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - analyzed_at < timedelta(hours=_COVERED_OPTIONS_TTL_HOURS)
    except (ValueError, TypeError):
        return False

```

**Why:** Matches the exact save/get/is_fresh triplet pattern already used for
`mpt_analysis` and `hedge_fund_analysis` in this same file. `json`, `datetime`,
`timedelta`, `timezone`, and `Optional` are already imported at the top of
`data/portfolio_cache.py` — no new imports needed.

---

### Step 3 — Create `data/covered_options_agent.py`

**File:** `stock-dashboard/data/covered_options_agent.py` (new file)
**Action:** Create the file with exactly this content:

```python
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
        bid = float(row.get("bid", 0) or 0)
        ask = float(row.get("ask", 0) or 0)
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 4)
        last = float(row.get("lastprice", 0) or 0)
        return round(last, 4)

    def _build_candidates(option_type: str) -> list[dict]:
        rows = enriched[(enriched["option_type"] == option_type) & (enriched["is_otm"])].copy()
        if rows.empty:
            return []
        candidates = []
        for _, row in rows.iterrows():
            dte = int(round(row["tte"] * 365.25))
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
                "bid": round(float(row.get("bid", 0) or 0), 4),
                "ask": round(float(row.get("ask", 0) or 0), 4),
                "mid_premium": mid,
                "delta": round(float(row["delta"]), 4),
                "iv": round(iv_val, 4),
                "open_interest": int(row.get("openinterest", 0) or 0),
                "volume": int(row.get("volume", 0) or 0),
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
```

**Why:** This is the entire new domain module. It mirrors `data/mpt_agent.py`
(pure-Python metrics + Gemini interpretation together in one file),
`data/screener_agent.py` (Flash/Pro runner + JSON parser + concurrent calls),
and `data/macro_impact_agent.py` (macro block formatting). All Gemini calls
route exclusively through `data.agy_client.run_agy`, tracked with
`data.gemini_tracker.record_call`, exactly as required. `analytics/chain.py`,
`analytics/volatility.py`, `analytics/positioning.py`, and
`analytics/helpers.py` are reused for IV rank/skew/expected-move/max-pain so
no options math is duplicated. Note: `Optional` typing is not needed here
(all functions use `dict | None` union syntax, matching Python 3.11+ already
used elsewhere in this codebase, e.g. `data/portfolio_cache.py`'s
`analyzed_at_str: str`, `data/mpt_agent.py`'s `-> dict | None`).

---

### Step 4 — Wire into `pages/9_portfolio.py`: imports

**File:** `stock-dashboard/pages/9_portfolio.py`
**Location:** In the import block at the top of the file, immediately after
this existing line (currently line 55):

```python
from data.fetcher import get_batch_history
```

**Action:** Insert this new import line directly after it:

```python
from data.covered_options_agent import (
    find_covered_call_eligible_positions,
    run_covered_options_roundtable,
)
from data.portfolio_cache import (
    save_covered_options_analysis,
    get_latest_covered_options_analysis,
    is_covered_options_analysis_fresh,
)
```

**Why:** `data.portfolio_cache` is already imported earlier in the file with a
large `from data.portfolio_cache import (...)` block (lines 35-47) — do NOT
add these three names to that existing import block (it would require
re-reading and re-editing a large multi-line import that already spans many
lines; a second `from data.portfolion_cache import (...)` statement directly
after is valid Python and keeps the diff minimal and unambiguous). Both
import blocks together give the page everything it needs.

---

### Step 5 — Wire into `pages/9_portfolio.py`: session state key

**File:** `stock-dashboard/pages/9_portfolio.py`
**Location:** Find this existing block (around line 3421-3427):

```python
if "news_results"    not in st.session_state: st.session_state.news_results    = {}
if "options_results" not in st.session_state: st.session_state.options_results = {}
if "wsb_results"     not in st.session_state: st.session_state.wsb_results     = {}
if "hf_analysis"     not in st.session_state: st.session_state.hf_analysis     = None
if "mpt_analysis"    not in st.session_state: st.session_state.mpt_analysis    = None
if "ai_insights"     not in st.session_state: st.session_state.ai_insights     = None
if "macro_impact"    not in st.session_state: st.session_state.macro_impact    = None
```

**Action:** Add one more line immediately after the `macro_impact` line:

```python
if "covered_options_results" not in st.session_state: st.session_state.covered_options_results = {}
```

**Why:** Follows the exact `if "key" not in st.session_state:` initialization
pattern already used for every other cross-run result dict on this page. This
dict is keyed by ticker string, holding the roundtable result for that ticker
during the current session (mirrors `news_results`/`wsb_results`).

---

### Step 6 — Add the "Covered Options Strategy Desk" section to the Dashboard tab

**File:** `stock-dashboard/pages/9_portfolio.py`
**Location:** Find this exact block (around lines 4412-4416):

```python
        else:
            st.caption("Run MPT Analysis via ⚡ Analyze Everything or the ⚡ Options & MPT tab.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2: NEWS — Full news analysis
# ═══════════════════════════════════════════════════════════════════════════════
```

The line `st.caption("Run MPT Analysis via ⚡ Analyze Everything or the ⚡ Options & MPT tab.")`
is the last statement inside the `with st.expander("Details — per-ticker news & MPT analytics", expanded=False):`
block, which itself is the last statement inside `with _tab_dash:`. The blank
lines and `# TAB 2: NEWS` comment mark the end of the Dashboard tab.

**Action:** Insert the entire block below, indented with 4 spaces (so it is
still inside `with _tab_dash:`), immediately after the
`st.caption("Run MPT Analysis via ⚡ Analyze Everything or the ⚡ Options & MPT tab.")`
line and its `else:` block ends (i.e., insert at the same indentation level as
the `with st.expander("Details — per-ticker news & MPT analytics", ...)`
statement, AFTER that whole expander block closes), and BEFORE the
`# ═══...` / `# TAB 2: NEWS` comment block:

```python

    # ── Covered Options Strategy Desk ──────────────────────────────────────────
    st.markdown("---")
    section_header("Covered Options Strategy Desk")
    st.caption(
        "Positions with 100+ shares can support covered calls. Pick a stock (or run all) for a "
        "4-persona AI roundtable — Macro Strategist, Equity Analyst, Technical Analyst, and Options "
        "Strategist — moderated to a final verdict on whether and how to sell options against it."
    )

    _eligible_positions = find_covered_call_eligible_positions(positions_result)

    if not _eligible_positions:
        st.info("No positions with 100+ shares were found — covered calls require at least one full lot (100 shares).")
    else:
        _elig_rows = [
            {
                "Ticker": p["ticker"],
                "Shares": f"{p['shares']:.0f}",
                "Lots (100sh)": p["lots"],
                "Cost Basis": f"${p['cost_basis']:.2f}",
                "Current Price": f"${p['current_price']:.2f}",
                "Unrealized P/L": f"${p['unrealized_pl']:+,.2f} ({p['unrealized_pl_pct']:+.1f}%)",
            }
            for p in _eligible_positions
        ]
        st.dataframe(pd.DataFrame(_elig_rows), use_container_width=True, hide_index=True)

        _cod_col_a, _cod_col_b, _cod_col_c = st.columns([3, 2, 2])
        with _cod_col_a:
            _cod_ticker_options = ["— Select a stock —"] + [p["ticker"] for p in _eligible_positions]
            _cod_selected_ticker = st.selectbox(
                "Run roundtable for", _cod_ticker_options, key="covered_opt_ticker_select"
            )
        with _cod_col_b:
            _cod_run_single = st.button(
                "▶ Run Roundtable", use_container_width=True, key="covered_opt_run_single_btn",
                disabled=(_cod_selected_ticker == "— Select a stock —"),
            )
        with _cod_col_c:
            _cod_run_all = st.button(
                "⚡ Analyze All Eligible", use_container_width=True, key="covered_opt_run_all_btn"
            )

        def _run_one_covered_options(ticker: str, position: dict, force: bool = False) -> None:
            cached_db = get_latest_covered_options_analysis(ticker)
            if not force and cached_db is not None and is_covered_options_analysis_fresh(cached_db.get("analyzed_at", "")):
                st.session_state.covered_options_results[ticker] = {**cached_db, "from_db": True}
                return
            macro_dict = fred_fetcher.get_macro_indicators() if fred_fetcher.is_configured() else {}
            cached_news = st.session_state.news_results.get(ticker)
            result = run_covered_options_roundtable(ticker, position, macro_dict, cached_news)
            if result and "_error" not in result:
                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                save_covered_options_analysis(
                    ticker, today_str, position.get("current_price", 0.0), result, result.get("context", {})
                )
            analyzed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            st.session_state.covered_options_results[ticker] = {**(result or {}), "analyzed_at": analyzed_at, "from_db": False}

        if _cod_run_single and _cod_selected_ticker != "— Select a stock —":
            _cod_pos = next((p for p in _eligible_positions if p["ticker"] == _cod_selected_ticker), None)
            if _cod_pos is not None:
                with st.status(f"Running covered options roundtable for {_cod_selected_ticker}…", expanded=True) as _cod_status:
                    st.write("1/3 — Gathering technical snapshot & option chain…")
                    st.write("2/3 — Running 4-persona opinion + rebuttal rounds (Gemini Flash)…")
                    st.write("3/3 — Moderator synthesizing final verdict (Gemini Pro)…")
                    _run_one_covered_options(_cod_selected_ticker, _cod_pos, force=True)
                    _cod_status.update(label=f"Roundtable complete for {_cod_selected_ticker}", state="complete")
                st.rerun()

        if _cod_run_all:
            _cod_progress = st.progress(0, text="Starting covered options roundtables…")
            for _i, _p in enumerate(_eligible_positions):
                _cod_progress.progress(
                    _i / len(_eligible_positions),
                    text=f"Analyzing {_p['ticker']} ({_i + 1}/{len(_eligible_positions)})…",
                )
                _run_one_covered_options(_p["ticker"], _p, force=False)
            _cod_progress.progress(1.0, text="All eligible positions analyzed.")
            time.sleep(0.4)
            _cod_progress.empty()
            st.rerun()

        _COD_VERDICT_COLOR = {
            "sell_covered_call": "#00c853", "sell_cash_secured_put": "#00c853",
            "wheel_candidate": "#00c853", "wait_until_after_earnings": "#ffd600",
            "no_iv_too_low": "#ff9100", "no_too_cheap_to_cap": "#ff9100",
            "hold_no_options": "#ff1744",
        }

        for _p in _eligible_positions:
            _t = _p["ticker"]
            _entry = st.session_state.covered_options_results.get(_t)
            if _entry is None:
                continue
            if "_error" in _entry:
                with st.expander(f"**{_t}** — Analysis failed", expanded=False):
                    st.error(_entry["_error"])
                continue

            _verdict = _entry.get("verdict", "hold_no_options")
            _vcolor = _COD_VERDICT_COLOR.get(_verdict, "#aaa")
            _cache_tag = " · cached" if _entry.get("from_db") else ""
            with st.expander(
                f"**{_t}** — {_verdict.replace('_', ' ').title()}{_cache_tag}", expanded=False
            ):
                st.markdown(
                    f'<div style="background:{_vcolor}22;border-left:4px solid {_vcolor};border-radius:6px;'
                    f'padding:10px 16px;margin-bottom:10px">'
                    f'<strong style="color:{_vcolor};font-size:1.1rem">{_verdict.replace("_", " ").upper()}</strong>'
                    f'<br><span style="color:#ccc;font-size:0.9rem">{_entry.get("verdict_reason", "")}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                _class = _entry.get("classification", "")
                st.caption(f"Classification: **{_class.replace('_', ' ').title()}** — {_entry.get('classification_reason', '')}")

                _recs = _entry.get("recommended_contracts", [])
                if _recs:
                    st.markdown("**Recommended Contract(s):**")
                    _rec_rows = [
                        {
                            "Type": r.get("type", "").replace("_", " ").title(),
                            "Strike": f"${r.get('strike', 0):.2f}",
                            "Expiry": r.get("expiry", ""),
                            "DTE": r.get("dte", ""),
                            "Premium (mid)": f"${r.get('mid_premium', 0):.2f}",
                            "Ann. Yield %": f"{r.get('annualized_yield_pct', 0):.2f}%",
                            "Cushion %": f"{r.get('downside_cushion_pct', 0):.2f}%",
                            "Max Profit/sh": f"${r.get('max_profit_per_share', 0):.2f}",
                            "Breakeven": f"${r.get('breakeven_price', 0):.2f}",
                        }
                        for r in _recs
                    ]
                    st.dataframe(pd.DataFrame(_rec_rows), use_container_width=True, hide_index=True)
                    for r in _recs:
                        if r.get("rationale"):
                            st.caption(f"• {r.get('rationale')}")
                else:
                    st.info("No specific contract recommended — see verdict above.")

                _edu = _entry.get("education", {})
                if _edu:
                    with st.expander("📘 Strategy Education", expanded=False):
                        if _edu.get("how_it_works"):
                            st.markdown(f"**How it works:** {_edu['how_it_works']}")
                        if _edu.get("max_profit"):
                            st.markdown(f"**Max profit:** {_edu['max_profit']}")
                        if _edu.get("breakeven"):
                            st.markdown(f"**Breakeven:** {_edu['breakeven']}")
                        if _edu.get("when_it_wins"):
                            st.markdown(f"**When it wins:** {_edu['when_it_wins']}")
                        if _edu.get("when_it_loses"):
                            st.markdown(f"**When it loses:** {_edu['when_it_loses']}")

                if _entry.get("moderator_summary"):
                    st.markdown(
                        f'<div style="background:#1a1a2e;border-left:4px solid {_vcolor};'
                        f'border-radius:6px;padding:14px 18px;margin-top:10px">'
                        f'<p style="color:#ddd;font-size:0.92rem;margin:0;line-height:1.6">{_entry["moderator_summary"]}</p>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                _discussion = _entry.get("discussion", {})
                if _discussion:
                    with st.expander("🗣️ Full Agent Discussion Transcript", expanded=False):
                        _r1p = _discussion.get("round1_parsed", {})
                        st.markdown("**Round 1 — Opinions**")
                        st.markdown(f"- **Macro Strategist:** {_r1p.get('macro', {}).get('opinion', 'N/A')}")
                        st.markdown(f"- **Equity Analyst:** {_r1p.get('equity', {}).get('opinion', 'N/A')}")
                        st.markdown(f"- **Technical Analyst:** {_r1p.get('technical', {}).get('opinion', 'N/A')}")
                        st.markdown(f"- **Options Strategist:** {_r1p.get('options', {}).get('opinion', 'N/A')}")
                        _r2p = _discussion.get("round2_parsed", {})
                        st.markdown("**Round 2 — Rebuttals**")
                        st.markdown(f"- **Macro Strategist:** {_r2p.get('macro_strategist_rebuttal', 'N/A')}")
                        st.markdown(f"- **Equity Analyst:** {_r2p.get('equity_analyst_rebuttal', 'N/A')}")
                        st.markdown(f"- **Technical Analyst:** {_r2p.get('technical_analyst_rebuttal', 'N/A')}")
                        st.markdown(f"- **Options Strategist:** {_r2p.get('options_strategist_rebuttal', 'N/A')}")
                        st.markdown("**Round 3 — Moderator Verdict (raw)**")
                        st.code(_discussion.get("round3_raw", ""), language="json")

                if st.button(f"🔄 Refresh analysis for {_t}", key=f"covered_opt_refresh_{_t}"):
                    with st.status(f"Refreshing covered options roundtable for {_t}…", expanded=True):
                        _run_one_covered_options(_t, _p, force=True)
                    st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2: NEWS — Full news analysis
# ═══════════════════════════════════════════════════════════════════════════════
```

**Why:** This is the "MAIN body" section the task requires, placed at the end
of the Dashboard tab (the page's primary tab) so it is immediately visible
without navigating to a sub-tab. It reuses `positions_result` (already fetched
earlier in the page), `st.session_state.news_results` (to avoid a duplicate
Gemini news-sentiment call when grounding the Equity Analyst persona), and
`fred_fetcher` / `datetime` / `timezone` / `time` / `pd`, all of which are
already imported at the top of `pages/9_portfolio.py`. The
`st.status(...)` progress pattern for the single-ticker run and
`st.progress(...)` pattern for the batch run both match patterns already used
elsewhere on this page (`st.progress(0, text=...)` is used repeatedly in the
"Analyze Everything" flow at lines 4034-4238). The `st.button` "Refresh
analysis" per ticker satisfies the plan's caching/UX requirement to force a
re-run without waiting for the 24h TTL to expire.

---

## Testing

1. **Launch the app**: `streamlit run dashboard.py` from `stock-dashboard/`
   with the venv activated. Navigate to the Portfolio page → Dashboard tab.
2. **Eligible positions table**: Scroll to "Covered Options Strategy Desk".
   Confirm any position with >= 100 shares appears in the table with correct
   Shares, Lots, Cost Basis, Current Price, and Unrealized P/L. If no position
   qualifies, confirm the info message "No positions with 100+ shares..." is
   shown instead of an error.
3. **Single-ticker roundtable**: Select an eligible ticker, click
   "▶ Run Roundtable". Confirm the `st.status(...)` box shows the 3 step
   messages, then the page reruns and an expander for that ticker appears
   with a colored verdict banner, classification line, a recommended-contract
   table (or "No specific contract recommended" info box), a "📘 Strategy
   Education" expander, a moderator summary block, and a "🗣️ Full Agent
   Discussion Transcript" expander showing Round 1/2/3 content.
4. **Caching**: Refresh the browser page (or navigate away and back). Confirm
   the same ticker's result reappears instantly with "· cached" in the
   expander title and no new Gemini calls are made (check
   `gemini_usage_bar` counters at the top of the page do not increase).
5. **Refresh button**: Inside an already-analyzed ticker's expander, click
   "🔄 Refresh analysis for TICKER". Confirm a fresh roundtable runs (Gemini
   usage counters increase by 6: 4 Flash opinion calls + 1 Flash rebuttal +
   1 Pro moderator call) and the expander updates with new content, no
   "cached" tag.
6. **Analyze All**: Click "⚡ Analyze All Eligible" with 2+ eligible tickers.
   Confirm the `st.progress` bar advances per ticker and all eligible tickers
   get an expander after rerun.
7. **No-options ticker edge case**: If a portfolio holding has no listed
   options (e.g., certain small-caps), confirm running the roundtable for it
   shows an expander with "Analysis failed" and the exact error message from
   `get_candidate_chain` (e.g., "TICKER has no listed options."), not a crash.
8. **Empty portfolio edge case**: Temporarily verify (via existing account
   selector) that a portfolio with zero positions >= 100 shares shows only
   the info message and no crash.
9. **Numbers sanity check**: For a recommended covered call contract, verify
   in the UI that `Max Profit/sh` roughly equals
   `(strike - cost_basis) + premium` and `Breakeven` roughly equals
   `cost_basis - premium`, using the values shown in the same row.
10. **DB verification**: After running at least one roundtable, inspect
    `db/portfolio.db` (e.g., via `python -c "import sqlite3; c=sqlite3.connect('db/portfolio.db'); print(c.execute('SELECT ticker, verdict, analyzed_at FROM covered_options_analysis').fetchall())"`
    from `stock-dashboard/`) and confirm a row exists per analyzed ticker.
11. **Regression check**: Confirm the existing "Signal Matrix" and "Details —
    per-ticker news & MPT analytics" sections directly above the new section
    still render correctly and that the News/Options/TA/Reddit/Smart
    Money/Market Pulse/Economy tabs are unaffected.
