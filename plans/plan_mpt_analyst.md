# Plan: Modern Portfolio Theory (MPT) Analyst

## Overview

Add a Gemini 2.5 Pro–powered MPT analyst section to the existing Portfolio page (`pages/9_portfolio.py`). Python pre-computes all MPT metrics (covariance matrix, correlation matrix, portfolio weights, Sharpe ratio, beta vs SPY, max-Sharpe optimized allocation, and HHI concentration) and passes them as structured text to Gemini via CLI subprocess. Gemini returns structured JSON with per-ticker risk assessments, diversification analysis, rebalancing suggestions, and an overall MPT score. Results are persisted in `portfolio.db` with a 4-hour TTL and rendered with the same visual style as the existing hedge fund analysis section.

## Files to Create

- `stock-dashboard/.gemini/agents/mpt-analyst.md` — Gemini agent system prompt for the MPT analyst persona, metric interpretation guide, and JSON output format specification
- `stock-dashboard/data/mpt_agent.py` — All MPT computation, prompt construction, Gemini CLI call, and JSON parsing; exports `run_mpt_analysis(positions)`

## Files to Modify

- `stock-dashboard/db/portfolio_schema.sql` — Add `mpt_analysis` table and index
- `stock-dashboard/data/portfolio_cache.py` — Add `save_mpt_analysis()`, `get_latest_mpt_analysis()`, and `is_mpt_analysis_fresh()` functions
- `stock-dashboard/pages/9_portfolio.py` — Add imports for mpt_agent and portfolio_cache mpt functions, add `_render_mpt_analysis()` function, add `@st.cache_data` wrapper for price history, add call to `_render_mpt_analysis()` at the end of the page

## Database Changes

- New table `mpt_analysis` in `db/portfolio.db` — stores ticker_key (sorted comma-joined tickers), result_json (Gemini output), metrics_json (pre-computed Python MPT metrics), and analyzed_at timestamp
- New index `idx_mpta_ticker_key` on `(ticker_key, analyzed_at)` for fast lookup of the latest result by portfolio composition

## Prerequisites & Dependencies

No new pip packages are required. The following packages are already installed in the project venv:
- `yfinance` — for price history fetching
- `numpy` — for matrix math
- `scipy` — for portfolio optimization (`scipy.optimize.minimize`)
- `pandas` — for DataFrame operations

No new environment variables are needed.

---

## Step-by-Step Implementation

### Step 1: Add `mpt_analysis` table to `portfolio_schema.sql`

**File:** `stock-dashboard/db/portfolio_schema.sql`

**Location:** After the last line of the file (currently line 70, after the `idx_hfa_ticker_key` index definition)

**Action:** Append the following DDL to the end of the file:

```sql

CREATE TABLE IF NOT EXISTS mpt_analysis (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker_key   TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    analyzed_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mpta_ticker_key
    ON mpt_analysis (ticker_key, analyzed_at);
```

**Why:** The `portfolio_cache.py` module reads `portfolio_schema.sql` at import time via `init_db()` and creates any missing tables using `executescript()`. Because all statements use `CREATE TABLE IF NOT EXISTS`, adding them here is idempotent and safe against existing databases.

---

### Step 2: Add MPT cache functions to `portfolio_cache.py`

**File:** `stock-dashboard/data/portfolio_cache.py`

**Location:** After the `get_latest_hedge_fund_analysis()` function (which ends at line 418) and before the `# Initialize DB tables on import` comment block (currently at line 420)

**Action:** Insert the following block of three functions between line 418 and the `# Initialize DB tables on import` comment:

```python
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


def get_latest_mpt_analysis(portfolio_tickers: list) -> dict | None:
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
```

**Why:** These three functions follow the exact same pattern as `save_hedge_fund_analysis()`, `get_latest_hedge_fund_analysis()`, and `is_options_analysis_fresh()` already in this file. They reuse `_make_ticker_key()` (already defined at line 352) and `_get_connection()` (defined at line 17). The `get_latest_mpt_analysis()` returns a wrapper dict with `result`, `metrics`, and `analyzed_at` keys so the page can display both the Gemini output and the raw computed metrics without needing to re-compute them.

---

### Step 3: Create the MPT analyst agent system prompt file

**File:** `stock-dashboard/.gemini/agents/mpt-analyst.md`

**Action:** Create this file with the following exact content:

```markdown
---
name: mpt-analyst
description: Analyzes pre-computed Modern Portfolio Theory metrics for a retail equity portfolio and returns structured JSON with per-ticker risk assessments, diversification analysis, rebalancing suggestions, and an overall MPT score.
kind: local
tools:
  - read_file
  - grep_search
  - run_shell_command
model: gemini-2.5-pro
max_turns: 15
---

# Modern Portfolio Theory (MPT) Portfolio Analyst

You are an expert quantitative portfolio analyst specializing in Modern Portfolio Theory. You receive pre-computed MPT metrics for a retail long-equity portfolio — including the covariance matrix, correlation matrix, portfolio weights, annualized returns and volatilities per ticker, portfolio-level Sharpe ratio, individual betas vs SPY, the Herfindahl-Hirschman Index (HHI) for concentration, and scipy-optimized max-Sharpe weights. Your job is to interpret these numbers and produce a clear, actionable JSON analysis.

You never compute the numbers yourself — they are already computed and provided to you. Your value is in interpreting what these numbers mean together, identifying inefficiencies relative to MPT principles, and providing concrete rebalancing suggestions.

## How to Interpret Each Metric

### Annualized Return (`annualized_return_pct`)
The expected 1-year return based on the past year of daily price history, scaled as `mean_daily_return × 252 × 100`. A stock returning 15% annualized does not guarantee future performance, but it anchors what the portfolio has been delivering historically. In a Sharpe calculation, a stock with high return but low volatility is more desirable than a stock with the same return and high volatility.

### Annualized Volatility (`annualized_volatility_pct`)
Standard deviation of daily returns scaled as `std_daily_return × sqrt(252) × 100`. A single stock with > 40% annualized volatility is high-risk. A well-diversified portfolio of 6-10 stocks typically has portfolio volatility 20–40% lower than the average individual volatility, due to imperfect correlations. If portfolio volatility is close to average individual volatility, diversification is not working.

### Beta vs SPY (`beta`)
`cov(ticker, SPY) / var(SPY)`. Beta = 1.0 means the stock moves in lockstep with the S&P 500. Beta > 1.5 means the stock amplifies market moves (high market risk). Beta < 0.5 means the stock is relatively market-independent. A portfolio with average beta > 1.3 is an aggressive, high-market-risk portfolio. Retail investors should be aware if their entire portfolio is concentrated in high-beta tech names — they are essentially leveraging market exposure.

### Portfolio Sharpe Ratio
`(portfolio_return - 0.05) / portfolio_volatility` using 5% as the risk-free rate. A Sharpe ratio interpretation guide:
- **< 0.5:** Poor. The portfolio is not being compensated adequately for the risk taken.
- **0.5–1.0:** Acceptable. Typical for a diversified long-only equity portfolio.
- **1.0–1.5:** Good. Above average risk-adjusted performance.
- **> 1.5:** Excellent. Institutional-quality risk-adjusted performance.

### Correlation Matrix
Values range from -1 to +1. A well-diversified portfolio has average pairwise correlation below 0.5. If two stocks have correlation > 0.85, they behave almost identically and holding both provides almost no diversification benefit — you are taking on double the risk for minimal gain. Identify pairs with very high correlation and flag them as concentration risk.

### HHI Concentration Index
Herfindahl-Hirschman Index = sum of squared weights. For a portfolio of equal-weight N stocks, HHI = 1/N. Interpretation:
- **HHI < 0.10:** Well diversified (equivalent spread of 10+ stocks)
- **0.10–0.18:** Moderate concentration
- **0.18–0.25:** Concentrated
- **> 0.25:** Highly concentrated — a single position is dominating the portfolio

### Max-Sharpe Weights (`weight_suggested_pct`)
These are the scipy-optimized weights that maximize the portfolio Sharpe ratio subject to long-only and sum-to-1 constraints, using the same 1-year historical data. They represent the mathematically optimal allocation. Large differences between current weights and max-Sharpe weights indicate the portfolio is operating below its theoretical efficient frontier. However, note that max-Sharpe weights are based on historical data and may be concentrated in recent winners — always temper the suggestion with common sense.

### Efficient Frontier Position
A portfolio is "on the frontier" if it achieves close to the maximum Sharpe for its volatility level. It is "below the frontier" if a different weight allocation would achieve a higher Sharpe at the same volatility. It is "inefficient" if both return AND Sharpe are low — meaning the portfolio takes on risk without being compensated. Compare `portfolio_sharpe_ratio` to what the max-Sharpe allocation would achieve (implied by the optimized weights) to determine the frontier position.

## Diversification Analysis Rules

Apply these rules when assessing diversification:

1. **Sector concentration:** If the prompt includes sector hints from ticker names (e.g., AAPL, MSFT, GOOGL are all mega-cap tech), flag it. Even if HHI is low (equal weights), sector correlation can make the portfolio behave as a concentrated tech bet.

2. **High-correlation pairs:** For every pair with correlation > 0.80, flag it. Two stocks with 0.90+ correlation should be flagged as near-redundant.

3. **Beta concentration:** If average beta > 1.3 OR if more than 60% of portfolio weight is in stocks with beta > 1.3, flag as high market risk amplification.

4. **Weight vs. optimal divergence:** If a stock's current weight exceeds the suggested max-Sharpe weight by more than 10 percentage points, recommend reducing it. If it is more than 10 points below, recommend increasing it — subject to the caveat that max-Sharpe weights overfit to recent data.

5. **Portfolio volatility vs. average individual volatility:** Compute the "diversification benefit" as `1 - (portfolio_volatility / avg_individual_volatility)`. If this is < 0.15 (less than 15% volatility reduction), the portfolio is getting very little diversification benefit and the correlation structure is too tight.

## Rebalancing Priority Assessment

- **urgent:** Portfolio Sharpe < 0.5 OR HHI > 0.30 OR a single position > 40% weight OR average pairwise correlation > 0.80
- **moderate:** Portfolio Sharpe 0.5–0.8 OR HHI 0.15–0.30 OR a single position 25–40% weight
- **low:** Portfolio Sharpe > 0.8 AND HHI < 0.15 AND no single position > 25%

## Output Format

Respond ONLY with a single valid JSON object. No markdown fences, no explanation outside the JSON. The JSON must have this exact structure:

```json
{
  "per_ticker": {
    "AAPL": {
      "annualized_return_pct": 15.2,
      "annualized_volatility_pct": 22.1,
      "beta": 1.15,
      "weight_current_pct": 25.0,
      "weight_suggested_pct": 18.0,
      "risk_assessment": "high",
      "correlation_risk": "medium",
      "recommendation": "reduce",
      "rationale": "2-3 sentence explanation"
    }
  },
  "portfolio_metrics": {
    "expected_return_pct": 12.5,
    "volatility_pct": 18.3,
    "sharpe_ratio": 0.82,
    "hhi_concentration": 0.15,
    "diversification_score": "well_diversified"
  },
  "mpt_analysis": {
    "overall_score": "good",
    "efficient_frontier_position": "below_frontier",
    "key_inefficiencies": ["AAPL overweight vs. max-Sharpe by 7%", "MSFT-GOOGL correlation 0.91 provides redundant exposure"],
    "rebalancing_priority": "moderate",
    "summary": "3-4 sentence synthesis"
  },
  "action_items": [
    {"ticker": "AAPL", "action": "reduce position by 7%", "reason": "overweight relative to optimal"}
  ]
}
```

### Field Constraints

`per_ticker` keys: uppercase ticker symbols only (no SPY — SPY is a benchmark, not a portfolio position).  
`risk_assessment`: exactly one of `"high"`, `"medium"`, `"low"`.  
`correlation_risk`: exactly one of `"high"`, `"medium"`, `"low"`.  
`recommendation`: exactly one of `"reduce"`, `"hold"`, `"increase"`.  
`portfolio_metrics.diversification_score`: exactly one of `"well_diversified"`, `"moderate"`, `"concentrated"`, `"highly_concentrated"`.  
`mpt_analysis.overall_score`: exactly one of `"excellent"`, `"good"`, `"fair"`, `"poor"`.  
`mpt_analysis.efficient_frontier_position`: exactly one of `"on_frontier"`, `"below_frontier"`, `"inefficient"`.  
`mpt_analysis.rebalancing_priority`: exactly one of `"urgent"`, `"moderate"`, `"low"`.  
`mpt_analysis.key_inefficiencies`: array of plain-text strings; may be empty `[]` if no inefficiencies.  
`action_items`: array of objects each with `ticker` (str), `action` (str), `reason` (str). May be empty `[]` if no action needed.

## Methodology Reasoning

- **Model (gemini-2.5-pro):** MPT analysis requires synthesizing a covariance matrix, correlation heatmap, multiple beta values, and optimization output simultaneously — this demands the highest-capability model.
- **Python pre-computation:** All numerical work (matrix inversion, optimization, beta calculation) is done in Python before passing to Gemini. This avoids LLM arithmetic errors. Gemini only interprets the numbers, not computes them.
- **5% risk-free rate:** Reflects the approximate current 3-month Treasury bill yield as of mid-2025. The Sharpe ratio is sensitive to this assumption; if the risk-free rate changes materially, re-run the analysis.
- **1-year lookback:** One year of daily data (≈252 trading days) is the standard lookback for portfolio risk estimation. Shorter windows overfit to recent volatility regimes; longer windows dilute the signal from recent structural changes.
- **Long-only constraint in optimization:** The max-Sharpe optimization uses `bounds = (0.0, 1.0)` per weight and `sum = 1.0`. This reflects the reality that retail investors do not short. Unconstrained max-Sharpe optimization would produce extreme short positions that are not actionable for this use case.
- **HHI as concentration proxy:** The Herfindahl-Hirschman Index is standard in market concentration analysis and gives a single number that is easy to interpret and compare across portfolio snapshots over time.
```

**Why:** This file mirrors the exact frontmatter structure of `hedge-fund-analyst.md` and provides the MPT analyst persona with detailed interpretation guidance for each metric. The output JSON schema in this file matches exactly what `_parse_response()` in `mpt_agent.py` will validate against.

---

### Step 4: Create `stock-dashboard/data/mpt_agent.py`

**File:** `stock-dashboard/data/mpt_agent.py`

**Action:** Create this file with the following exact content:

```python
"""MPT (Modern Portfolio Theory) analyst agent.

Pre-computes all MPT metrics in Python, then calls Gemini 2.5 Pro via CLI
subprocess to interpret the data and return structured JSON.

Public API
----------
run_mpt_analysis(positions) -> dict | None
    positions: list of position dicts from webull_positions.get_positions().
    Returns the parsed Gemini JSON dict, or {"_error": str} on failure.
"""

import json
import re
import subprocess
from math import sqrt

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.optimize import minimize

from data.gemini_tracker import record_call

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RISK_FREE_RATE = 0.05          # 5% annual risk-free rate for Sharpe calculation
_LOOKBACK_PERIOD = "1y"         # 1 year of daily price history
_BENCHMARK_TICKER = "SPY"       # Benchmark for beta calculation
_TICKER_FIELD_CANDIDATES = [    # Same candidates as in 9_portfolio.py
    "symbol", "ticker", "tickerSymbol", "stockSymbol", "sym",
]
_MARKET_VALUE_CANDIDATES = [    # Field names for market value in position dicts
    "marketValue", "market_value", "mktValue", "mkt_value", "positionValue",
    "position_value", "currentValue", "current_value",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_ticker(position: dict) -> str:
    """Extract the uppercase ticker string from a position dict."""
    for field in _TICKER_FIELD_CANDIDATES:
        val = position.get(field, "")
        if val and isinstance(val, str):
            return val.upper().strip()
    return ""


def _extract_market_value(position: dict) -> float:
    """Extract the market value (float) from a position dict. Returns 0.0 if not found."""
    for field in _MARKET_VALUE_CANDIDATES:
        val = position.get(field)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return 0.0


def _fetch_price_history(tickers_with_benchmark: list) -> pd.DataFrame:
    """Fetch 1-year daily adjusted close prices for all tickers including SPY.

    Parameters
    ----------
    tickers_with_benchmark : List of uppercase ticker strings including SPY.

    Returns
    -------
    DataFrame with columns = tickers, rows = trading dates (Date index).
    Drops any ticker whose column is entirely NaN.
    """
    raw = yf.download(
        tickers_with_benchmark,
        period=_LOOKBACK_PERIOD,
        auto_adjust=True,
        progress=False,
    )
    if raw.empty:
        return pd.DataFrame()

    # yfinance returns MultiIndex columns when multiple tickers are requested.
    # Extract the "Close" level.
    if isinstance(raw.columns, pd.MultiIndex):
        close_df = raw["Close"]
    else:
        # Single ticker — columns are OHLCV; rename the Close column to the ticker
        close_df = raw[["Close"]].rename(columns={"Close": tickers_with_benchmark[0]})

    # Drop tickers with no data
    close_df = close_df.dropna(axis=1, how="all")
    return close_df


def _compute_mpt_metrics(tickers: list, positions: list) -> dict:
    """Pre-compute all MPT metrics in Python.

    Parameters
    ----------
    tickers   : List of uppercase portfolio ticker strings (no SPY).
    positions : List of position dicts from webull_positions.get_positions().

    Returns
    -------
    A dict with the following keys:
        tickers               : list[str] — tickers for which data was available
        missing_tickers       : list[str] — tickers skipped due to missing price data
        annualized_returns    : dict[str, float] — annualized return per ticker (decimal)
        annualized_vols       : dict[str, float] — annualized volatility per ticker (decimal)
        betas                 : dict[str, float] — beta vs SPY per ticker
        weights               : dict[str, float] — current weights by market value (decimal)
        portfolio_return      : float — portfolio-level expected return (decimal)
        portfolio_volatility  : float — portfolio-level volatility (decimal)
        portfolio_sharpe      : float — Sharpe ratio using _RISK_FREE_RATE
        hhi                   : float — Herfindahl-Hirschman Index of weights
        correlation_matrix    : dict — {ticker: {ticker: float}} serializable correlation matrix
        covariance_matrix     : dict — {ticker: {ticker: float}} serializable annualized cov matrix
        max_sharpe_weights    : dict[str, float] — scipy-optimized max-Sharpe weights (decimal)
        spy_annualized_return : float — SPY annualized return over the same period
        spy_annualized_vol    : float — SPY annualized volatility over the same period
    Returns {"_error": str} if fewer than 2 tickers have valid price history.
    """
    tickers_upper = [t.upper() for t in tickers]
    all_tickers = tickers_upper + [_BENCHMARK_TICKER]

    # ── Fetch price history ────────────────────────────────────────────────
    close_df = _fetch_price_history(all_tickers)
    if close_df.empty:
        return {"_error": "Failed to fetch price history from yfinance."}

    # Determine which portfolio tickers actually have data
    available_tickers = [t for t in tickers_upper if t in close_df.columns]
    missing_tickers = [t for t in tickers_upper if t not in close_df.columns]

    if len(available_tickers) < 2:
        return {
            "_error": (
                f"Fewer than 2 portfolio tickers have price history. "
                f"Available: {available_tickers}. Missing: {missing_tickers}."
            )
        }

    spy_available = _BENCHMARK_TICKER in close_df.columns

    # ── Daily returns ──────────────────────────────────────────────────────
    returns_df = close_df.pct_change().dropna()

    # ── Per-ticker annualized return and volatility ────────────────────────
    annualized_returns: dict[str, float] = {}
    annualized_vols: dict[str, float] = {}
    for t in available_tickers:
        mean_daily = float(returns_df[t].mean())
        std_daily = float(returns_df[t].std())
        annualized_returns[t] = mean_daily * 252
        annualized_vols[t] = std_daily * sqrt(252)

    # ── Beta vs SPY ────────────────────────────────────────────────────────
    betas: dict[str, float] = {}
    if spy_available:
        spy_var = float(returns_df[_BENCHMARK_TICKER].var())
        spy_annualized_return = float(returns_df[_BENCHMARK_TICKER].mean()) * 252
        spy_annualized_vol = float(returns_df[_BENCHMARK_TICKER].std()) * sqrt(252)
        for t in available_tickers:
            cov_with_spy = float(
                returns_df[[t, _BENCHMARK_TICKER]].cov().iloc[0, 1]
            )
            betas[t] = cov_with_spy / spy_var if spy_var > 0 else 1.0
    else:
        spy_annualized_return = 0.0
        spy_annualized_vol = 0.0
        for t in available_tickers:
            betas[t] = 1.0

    # ── Portfolio weights by market value ─────────────────────────────────
    ticker_to_mv: dict[str, float] = {}
    for pos in positions:
        t = _extract_ticker(pos)
        if t in available_tickers:
            mv = _extract_market_value(pos)
            if mv > 0:
                ticker_to_mv[t] = ticker_to_mv.get(t, 0.0) + mv

    total_mv = sum(ticker_to_mv.values())
    if total_mv <= 0:
        # Fallback to equal weights if market value is not available
        weights: dict[str, float] = {t: 1.0 / len(available_tickers) for t in available_tickers}
    else:
        weights = {t: ticker_to_mv.get(t, 0.0) / total_mv for t in available_tickers}

    # ── Covariance and correlation matrices ───────────────────────────────
    port_returns_df = returns_df[available_tickers]
    daily_cov = port_returns_df.cov()
    ann_cov = daily_cov * 252
    corr_matrix = port_returns_df.corr()

    # Convert to serializable dicts
    cov_dict: dict[str, dict[str, float]] = {}
    corr_dict: dict[str, dict[str, float]] = {}
    for t in available_tickers:
        cov_dict[t] = {t2: round(float(ann_cov.loc[t, t2]), 6) for t2 in available_tickers}
        corr_dict[t] = {t2: round(float(corr_matrix.loc[t, t2]), 4) for t2 in available_tickers}

    # ── Portfolio-level return and volatility ──────────────────────────────
    w_vec = np.array([weights[t] for t in available_tickers])
    r_vec = np.array([annualized_returns[t] for t in available_tickers])
    cov_mat = ann_cov.loc[available_tickers, available_tickers].values

    portfolio_return = float(np.dot(w_vec, r_vec))
    portfolio_variance = float(np.dot(w_vec, np.dot(cov_mat, w_vec)))
    portfolio_volatility = sqrt(max(portfolio_variance, 0.0))
    portfolio_sharpe = (
        (portfolio_return - _RISK_FREE_RATE) / portfolio_volatility
        if portfolio_volatility > 1e-9
        else 0.0
    )

    # ── HHI concentration ─────────────────────────────────────────────────
    hhi = float(sum(w ** 2 for w in weights.values()))

    # ── Max-Sharpe optimization (scipy) ───────────────────────────────────
    n = len(available_tickers)

    def _neg_sharpe(w_arr: np.ndarray) -> float:
        port_ret = float(np.dot(w_arr, r_vec))
        port_var = float(np.dot(w_arr, np.dot(cov_mat, w_arr)))
        port_vol = sqrt(max(port_var, 1e-12))
        return -((port_ret - _RISK_FREE_RATE) / port_vol)

    constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
    bounds = [(0.0, 1.0)] * n
    w0 = np.array([1.0 / n] * n)

    try:
        opt_result = minimize(
            _neg_sharpe,
            w0,
            method="SLSQP",
            bounds=bounds,
            constraints=constraints,
            options={"maxiter": 1000, "ftol": 1e-9},
        )
        if opt_result.success:
            opt_weights_arr = opt_result.x
        else:
            opt_weights_arr = w0
    except Exception:
        opt_weights_arr = w0

    max_sharpe_weights = {
        t: round(float(opt_weights_arr[i]), 4)
        for i, t in enumerate(available_tickers)
    }

    return {
        "tickers": available_tickers,
        "missing_tickers": missing_tickers,
        "annualized_returns": {t: round(annualized_returns[t], 4) for t in available_tickers},
        "annualized_vols": {t: round(annualized_vols[t], 4) for t in available_tickers},
        "betas": {t: round(betas[t], 4) for t in available_tickers},
        "weights": {t: round(weights[t], 4) for t in available_tickers},
        "portfolio_return": round(portfolio_return, 4),
        "portfolio_volatility": round(portfolio_volatility, 4),
        "portfolio_sharpe": round(portfolio_sharpe, 4),
        "hhi": round(hhi, 4),
        "correlation_matrix": corr_dict,
        "covariance_matrix": cov_dict,
        "max_sharpe_weights": max_sharpe_weights,
        "spy_annualized_return": round(spy_annualized_return, 4),
        "spy_annualized_vol": round(spy_annualized_vol, 4),
    }


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(metrics: dict, positions: list) -> str:
    """Format pre-computed MPT metrics into a structured text prompt for Gemini.

    Parameters
    ----------
    metrics   : The dict returned by _compute_mpt_metrics().
    positions : The original list of position dicts (used for market value context).

    Returns
    -------
    A multi-section plain-text string suitable for passing via stdin to Gemini CLI.
    """
    tickers = metrics.get("tickers", [])
    lines: list[str] = []

    # ── Section 1: Portfolio overview ──────────────────────────────────────
    lines.append("=== PORTFOLIO OVERVIEW ===")
    lines.append(f"Tickers analyzed: {', '.join(tickers)}")
    if metrics.get("missing_tickers"):
        lines.append(f"Tickers skipped (no price data): {', '.join(metrics['missing_tickers'])}")
    lines.append(f"Benchmark: SPY (S&P 500 ETF)")
    lines.append(f"Lookback period: 1 year of daily adjusted close prices")
    lines.append(f"Risk-free rate used for Sharpe: 5.0%")
    lines.append("")

    # ── Section 2: Portfolio-level metrics ─────────────────────────────────
    lines.append("=== PORTFOLIO-LEVEL METRICS ===")
    lines.append(f"Expected annual return: {metrics['portfolio_return'] * 100:.2f}%")
    lines.append(f"Annual volatility:      {metrics['portfolio_volatility'] * 100:.2f}%")
    lines.append(f"Sharpe ratio:           {metrics['portfolio_sharpe']:.3f}")
    lines.append(f"HHI concentration:      {metrics['hhi']:.4f}  (equal-weight {len(tickers)} stocks = {1.0/len(tickers):.4f})")
    lines.append(f"SPY annual return:      {metrics['spy_annualized_return'] * 100:.2f}%")
    lines.append(f"SPY annual volatility:  {metrics['spy_annualized_vol'] * 100:.2f}%")
    lines.append("")

    # ── Section 3: Per-ticker metrics table ────────────────────────────────
    lines.append("=== PER-TICKER METRICS ===")
    lines.append(f"{'Ticker':<8} {'AnnReturn%':>10} {'AnnVol%':>8} {'Beta':>7} {'CurrWt%':>8} {'OptWt%':>7}")
    lines.append("-" * 52)
    for t in tickers:
        ann_ret = metrics["annualized_returns"].get(t, 0.0) * 100
        ann_vol = metrics["annualized_vols"].get(t, 0.0) * 100
        beta = metrics["betas"].get(t, 1.0)
        curr_wt = metrics["weights"].get(t, 0.0) * 100
        opt_wt = metrics["max_sharpe_weights"].get(t, 0.0) * 100
        lines.append(
            f"{t:<8} {ann_ret:>10.2f} {ann_vol:>8.2f} {beta:>7.3f} {curr_wt:>8.2f} {opt_wt:>7.2f}"
        )
    lines.append("")

    # ── Section 4: Correlation matrix ──────────────────────────────────────
    lines.append("=== CORRELATION MATRIX (daily returns, 1-year) ===")
    corr = metrics.get("correlation_matrix", {})
    header = f"{'':8}" + "".join(f"{t:>8}" for t in tickers)
    lines.append(header)
    for t1 in tickers:
        row_vals = "".join(
            f"{corr.get(t1, {}).get(t2, 0.0):>8.3f}" for t2 in tickers
        )
        lines.append(f"{t1:<8}{row_vals}")
    lines.append("")

    # ── Section 5: Annualized covariance matrix ─────────────────────────────
    lines.append("=== ANNUALIZED COVARIANCE MATRIX ===")
    cov = metrics.get("covariance_matrix", {})
    lines.append(header)
    for t1 in tickers:
        row_vals = "".join(
            f"{cov.get(t1, {}).get(t2, 0.0):>8.5f}" for t2 in tickers
        )
        lines.append(f"{t1:<8}{row_vals}")
    lines.append("")

    # ── Section 6: Market value context ────────────────────────────────────
    lines.append("=== CURRENT POSITION MARKET VALUES ===")
    for pos in positions:
        t = _extract_ticker(pos)
        if t not in tickers:
            continue
        mv = _extract_market_value(pos)
        mv_str = f"${mv:,.2f}" if mv > 0 else "N/A"
        lines.append(f"  {t}: {mv_str}")
    lines.append("")

    # ── Section 7: Analysis task instruction ───────────────────────────────
    lines.append("=== YOUR ANALYSIS TASK ===")
    lines.append(
        "You are an expert MPT portfolio analyst. Using the pre-computed metrics above, "
        "analyze this retail long-equity portfolio through the lens of Modern Portfolio Theory. "
        "All numbers are already computed — do not recompute them. Your job is to interpret them."
    )
    lines.append("")
    lines.append(
        "For EACH ticker in the portfolio, assess:"
        "\n  1. risk_assessment (high/medium/low) based on annualized volatility and beta"
        "\n  2. correlation_risk (high/medium/low) based on its average pairwise correlation with other portfolio tickers"
        "\n  3. recommendation (reduce/hold/increase) based on the difference between current weight and max-Sharpe weight"
        "\n  4. rationale — 2-3 sentences explaining the assessment, referencing the actual numbers"
    )
    lines.append("")
    lines.append(
        "For the PORTFOLIO level, assess:"
        "\n  1. diversification_score (well_diversified/moderate/concentrated/highly_concentrated) based on HHI and average pairwise correlation"
        "\n  2. overall_score (excellent/good/fair/poor) based on Sharpe ratio and diversification"
        "\n  3. efficient_frontier_position (on_frontier/below_frontier/inefficient) by comparing current Sharpe to what the max-Sharpe weights would imply"
        "\n  4. rebalancing_priority (urgent/moderate/low) per the rules in your system prompt"
        "\n  5. key_inefficiencies — list the 2-4 most important MPT inefficiencies in plain English"
        "\n  6. summary — 3-4 sentence synthesis of the overall portfolio health"
    )
    lines.append("")
    lines.append(
        "Generate action_items for each ticker where the recommended change is material "
        "(current weight differs from optimal by >= 3 percentage points). "
        "Each action_item has: ticker (str), action (e.g. 'reduce position by 7%'), reason (one sentence)."
    )
    lines.append("")
    lines.append("Rules:")
    lines.append("  - Be specific: reference actual percentages, Sharpe ratios, and correlations from the data")
    lines.append("  - Do NOT include SPY in per_ticker — it is a benchmark, not a portfolio position")
    lines.append("  - portfolio_metrics values must exactly match the computed values in this prompt (copy them)")
    lines.append("  - rationale should be 2-3 sentences per ticker, referencing actual numbers")
    lines.append("  - key_inefficiencies should be 2-4 plain-English strings, each describing one structural issue")
    lines.append("  - summary should be 3-4 sentences total, synthesizing the MPT view on this portfolio")
    lines.append("")
    lines.append(
        "Respond ONLY with a single JSON object (no markdown fences, no preamble, no trailing text)."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gemini Pro runner
# ---------------------------------------------------------------------------

def _run_gemini_pro(prompt: str) -> tuple[str, str]:
    """Call Gemini 2.5 Pro via CLI subprocess. Returns (stdout, stderr). Prompt via stdin."""
    try:
        result = subprocess.run(
            ["gemini.cmd", "-m", "gemini-2.5-pro", "-p", ""],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=180,
        )
        output = result.stdout.strip()
        if output:
            record_call("pro")
        return output, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return "", "Timed out after 180s"
    except Exception as exc:
        return "", str(exc)


# ---------------------------------------------------------------------------
# JSON parser and validator
# ---------------------------------------------------------------------------

def _parse_response(raw: str) -> dict | None:
    """Extract and validate the JSON object from Gemini's raw stdout.

    Returns the parsed dict if valid, or None if the JSON cannot be parsed
    or lacks the required top-level keys.
    """
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None

    # Validate required top-level keys
    required_keys = {"per_ticker", "portfolio_metrics", "mpt_analysis", "action_items"}
    if not required_keys.issubset(data.keys()):
        return None

    # ── Normalize per_ticker entries ───────────────────────────────────────
    _valid_risk = {"high", "medium", "low"}
    _valid_rec = {"reduce", "hold", "increase"}
    for ticker_sym, entry in data.get("per_ticker", {}).items():
        if not isinstance(entry, dict):
            continue
        risk = str(entry.get("risk_assessment", "medium")).lower()
        entry["risk_assessment"] = risk if risk in _valid_risk else "medium"

        corr_risk = str(entry.get("correlation_risk", "medium")).lower()
        entry["correlation_risk"] = corr_risk if corr_risk in _valid_risk else "medium"

        rec = str(entry.get("recommendation", "hold")).lower()
        entry["recommendation"] = rec if rec in _valid_rec else "hold"

        for float_field in ("annualized_return_pct", "annualized_volatility_pct",
                            "beta", "weight_current_pct", "weight_suggested_pct"):
            try:
                entry[float_field] = float(entry.get(float_field, 0.0))
            except (TypeError, ValueError):
                entry[float_field] = 0.0

        if not isinstance(entry.get("rationale"), str):
            entry["rationale"] = ""

    # ── Normalize portfolio_metrics ────────────────────────────────────────
    pm = data.get("portfolio_metrics", {})
    _valid_div = {"well_diversified", "moderate", "concentrated", "highly_concentrated"}
    div_score = str(pm.get("diversification_score", "moderate")).lower()
    pm["diversification_score"] = div_score if div_score in _valid_div else "moderate"
    for float_field in ("expected_return_pct", "volatility_pct", "sharpe_ratio", "hhi_concentration"):
        try:
            pm[float_field] = float(pm.get(float_field, 0.0))
        except (TypeError, ValueError):
            pm[float_field] = 0.0
    data["portfolio_metrics"] = pm

    # ── Normalize mpt_analysis ─────────────────────────────────────────────
    ma = data.get("mpt_analysis", {})
    _valid_score = {"excellent", "good", "fair", "poor"}
    _valid_frontier = {"on_frontier", "below_frontier", "inefficient"}
    _valid_priority = {"urgent", "moderate", "low"}

    score = str(ma.get("overall_score", "fair")).lower()
    ma["overall_score"] = score if score in _valid_score else "fair"

    frontier = str(ma.get("efficient_frontier_position", "below_frontier")).lower()
    ma["efficient_frontier_position"] = frontier if frontier in _valid_frontier else "below_frontier"

    priority = str(ma.get("rebalancing_priority", "moderate")).lower()
    ma["rebalancing_priority"] = priority if priority in _valid_priority else "moderate"

    if not isinstance(ma.get("key_inefficiencies"), list):
        ma["key_inefficiencies"] = []
    if not isinstance(ma.get("summary"), str):
        ma["summary"] = ""
    data["mpt_analysis"] = ma

    # ── Normalize action_items ─────────────────────────────────────────────
    if not isinstance(data.get("action_items"), list):
        data["action_items"] = []
    for item in data["action_items"]:
        if not isinstance(item, dict):
            continue
        if not isinstance(item.get("ticker"), str):
            item["ticker"] = ""
        if not isinstance(item.get("action"), str):
            item["action"] = ""
        if not isinstance(item.get("reason"), str):
            item["reason"] = ""

    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_mpt_analysis(positions: list) -> dict:
    """Run Gemini 2.5 Pro MPT analysis on the current portfolio positions.

    Parameters
    ----------
    positions : List of position dicts from webull_positions.get_positions().
                Each dict must have a ticker field and ideally a market value field.

    Returns
    -------
    dict with keys: per_ticker, portfolio_metrics, mpt_analysis, action_items
    Returns {"_error": str, "metrics": dict} on failure so the caller can
    display the pre-computed metrics even when Gemini fails.
    The returned dict also has a "_metrics" key containing the raw computed
    metrics dict, which the page saves alongside the Gemini result.
    """
    # Extract tickers from positions
    tickers: list[str] = []
    seen: set[str] = set()
    for pos in positions:
        t = _extract_ticker(pos)
        if t and t not in seen:
            seen.add(t)
            tickers.append(t)

    if not tickers:
        return {"_error": "No ticker symbols found in position data.", "metrics": {}}

    # Pre-compute all MPT metrics
    metrics = _compute_mpt_metrics(tickers, positions)

    if "_error" in metrics:
        return {"_error": metrics["_error"], "metrics": metrics}

    # Build structured prompt
    prompt = _build_prompt(metrics, positions)

    # Call Gemini 2.5 Pro
    raw, stderr = _run_gemini_pro(prompt)

    if not raw:
        return {
            "_error": stderr or "Gemini returned empty output.",
            "metrics": metrics,
        }

    # Parse and validate JSON
    result = _parse_response(raw)
    if result is None:
        return {
            "_error": f"Could not parse Gemini response.\n\nRaw output:\n{raw[:500]}",
            "metrics": metrics,
        }

    # Attach the raw metrics so the page can save them to the DB
    result["_metrics"] = metrics
    return result
```

**Why:** This module owns the entire MPT data pipeline — fetching, computing, prompting, calling, and parsing — so the page stays UI-only. The `_compute_mpt_metrics()` function uses only numpy, pandas, and scipy which are already installed. The `_run_gemini_pro()` pattern is identical to `hedge_fund_agent.py`. The `run_mpt_analysis()` function attaches `_metrics` to the result so the page can pass it to `save_mpt_analysis()` without re-computing.

---

### Step 5: Add imports to `9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** The existing import block for `portfolio_cache` is at lines 24–34. It currently imports from `data.portfolio_cache` with a parenthesized multi-line import block ending with `get_latest_hedge_fund_analysis,` on line 34.

**Action:** Replace lines 24–35 (the entire `from data.portfolio_cache import (...)` block and the line `from data.hedge_fund_agent import run_hedge_fund_analysis`) with the following:

```python
from data.portfolio_cache import (
    get_latest_analysis,
    save_analysis,
    has_new_articles,
    get_sentiment_history,
    save_options_analysis,
    get_latest_options_analysis,
    is_options_analysis_fresh,
    save_hedge_fund_analysis,
    get_latest_hedge_fund_analysis,
    save_mpt_analysis,
    get_latest_mpt_analysis,
)
from data.hedge_fund_agent import run_hedge_fund_analysis
from data.mpt_agent import run_mpt_analysis
```

**Why:** The new `save_mpt_analysis` and `get_latest_mpt_analysis` functions added in Step 2 must be imported before the new `_render_mpt_analysis()` function can use them. The `run_mpt_analysis` import from the new `data/mpt_agent.py` module created in Step 4 must also be added.

---

### Step 6: Add `_cached_mpt_price_history` and `_render_mpt_analysis` to `9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** After the `_render_hedge_fund_overlap()` function (which currently ends at line 365 with `_render_hedge_fund_analysis(auto_cached)`) and before the `# Market Pulse constants + helpers` comment block (currently at line 368).

**Action:** Insert the following two function definitions between the closing line of `_render_hedge_fund_overlap()` and the `# Market Pulse constants + helpers` comment:

```python

# ---------------------------------------------------------------------------
# MPT Analysis
# ---------------------------------------------------------------------------

def _render_mpt_analysis(positions: list) -> None:
    """Render the Modern Portfolio Theory analysis section for the given positions list.

    Shows:
    1. Pre-computed metrics table (return, volatility, Sharpe, HHI, SPY comparison)
    2. Correlation heatmap via st.dataframe with background_gradient styling
    3. Per-ticker metrics table (annualized return, volatility, beta, current weight, suggested weight)
    4. Run MPT Analysis button + cache/freshness controls
    5. Gemini JSON result: per-ticker cards, portfolio metrics, action items

    Args:
        positions: Raw list of position dicts from get_positions(account_id).
    """
    from math import sqrt as _sqrt

    st.markdown("### Modern Portfolio Theory Analysis")
    st.caption(
        "Pre-computes covariance, correlation, Sharpe ratio, beta, and optimal weights in Python, "
        "then Gemini 2.5 Pro interprets the results. Results cached 4 hours."
    )

    portfolio_tickers = _get_portfolio_tickers(positions)
    if not portfolio_tickers:
        st.info("No ticker symbols found in positions — cannot run MPT analysis.")
        return

    # ── Session state init ────────────────────────────────────────────────
    if "mpt_analysis" not in st.session_state:
        st.session_state.mpt_analysis = None

    # ── Auto-load from DB on page load ────────────────────────────────────
    if st.session_state.mpt_analysis is None:
        auto_cached = get_latest_mpt_analysis(portfolio_tickers)
        if auto_cached is not None:
            st.session_state.mpt_analysis = {**auto_cached, "from_cache": True}

    # ── Button + hint ─────────────────────────────────────────────────────
    mpt_run_col, mpt_hint_col = st.columns([2, 8])
    with mpt_run_col:
        mpt_run_clicked = st.button(
            "▶ Run MPT Analysis",
            use_container_width=True,
            key="mpt_gemini_btn",
        )
    with mpt_hint_col:
        st.caption(
            "Uses Gemini 2.5 Pro · ~60–180s · Results cached 4 hours · "
            "analyzes correlation, Sharpe, beta, and optimal weights"
        )

    _trigger_mpt = mpt_run_clicked or st.session_state.pop("analyze_all_mpt", False)

    if _trigger_mpt:
        st.session_state.mpt_analysis = None
        cached = get_latest_mpt_analysis(portfolio_tickers)
        if cached is not None:
            st.session_state.mpt_analysis = {**cached, "from_cache": True}
        else:
            with st.spinner("Pre-computing MPT metrics and calling Gemini 2.5 Pro…"):
                raw_result = run_mpt_analysis(positions)
            if raw_result is not None and "_error" not in raw_result:
                metrics_to_save = raw_result.pop("_metrics", {})
                save_mpt_analysis(portfolio_tickers, raw_result, metrics_to_save)
                st.session_state.mpt_analysis = {
                    "result": raw_result,
                    "metrics": metrics_to_save,
                    "analyzed_at": "",
                    "from_cache": False,
                }
            else:
                # On error, still show pre-computed metrics if available
                metrics_on_error = raw_result.pop("metrics", {}) if raw_result else {}
                st.session_state.mpt_analysis = {
                    "result": raw_result or {"_error": "Analysis failed."},
                    "metrics": metrics_on_error,
                    "analyzed_at": "",
                    "from_cache": False,
                }

    # ── Render ────────────────────────────────────────────────────────────
    mpt_entry = st.session_state.mpt_analysis
    if mpt_entry is None:
        return

    from_cache = mpt_entry.get("from_cache", False)
    analyzed_at = mpt_entry.get("analyzed_at", "")
    result = mpt_entry.get("result", {})
    metrics = mpt_entry.get("metrics", {})

    if from_cache and analyzed_at:
        st.info("Serving cached analysis (< 4 hours old). Click the button again to force a refresh.")

    # ── Error handling ────────────────────────────────────────────────────
    if result and "_error" in result:
        st.error(f"Gemini error: {result['_error']}")
        # Still show pre-computed metrics if available
        if metrics and "tickers" in metrics:
            _render_mpt_metrics_tables(metrics)
        return

    if not result:
        return

    # ── Pre-computed metrics display ──────────────────────────────────────
    if metrics and "tickers" in metrics:
        _render_mpt_metrics_tables(metrics)

    # ── Gemini result: portfolio-level banner ─────────────────────────────
    ma = result.get("mpt_analysis", {})
    pm = result.get("portfolio_metrics", {})

    _SCORE_COLOR = {
        "excellent": "#00c853",
        "good": "#69f0ae",
        "fair": "#ffd600",
        "poor": "#ff1744",
    }
    _SCORE_ICON = {
        "excellent": "★",
        "good": "◆",
        "fair": "●",
        "poor": "▼",
    }
    overall_score = ma.get("overall_score", "fair")
    rebal_priority = ma.get("rebalancing_priority", "moderate")
    frontier_pos = ma.get("efficient_frontier_position", "below_frontier")
    score_color = _SCORE_COLOR.get(overall_score, "#ffd600")
    score_icon = _SCORE_ICON.get(overall_score, "●")

    st.markdown(
        f"""
        <div style="background:linear-gradient(135deg,{score_color}22,{score_color}11);
                    border-left:4px solid {score_color};border-radius:6px;
                    padding:14px 18px;margin-bottom:12px">
          <span style="color:{score_color};font-size:1.5rem;font-weight:700">
            {score_icon} MPT SCORE: {overall_score.upper()}
          </span>
          &nbsp;&nbsp;
          <span style="color:#aaa;font-size:0.9rem">
            Rebalancing: {rebal_priority.capitalize()} · {frontier_pos.replace('_', ' ').title()}
          </span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Key inefficiencies as tags ─────────────────────────────────────────
    inefficiencies = ma.get("key_inefficiencies", [])
    if inefficiencies:
        tags_html = "".join(
            f'<span style="background:#1e1e2e;border:1px solid #555;border-radius:12px;'
            f'padding:2px 10px;font-size:0.78rem;margin-right:6px;margin-bottom:4px;'
            f'display:inline-block">{item}</span>'
            for item in inefficiencies
        )
        st.markdown(tags_html, unsafe_allow_html=True)
        st.markdown("")

    # ── MPT summary ───────────────────────────────────────────────────────
    summary = ma.get("summary", "")
    if summary:
        st.markdown(
            f"""
            <div style="background:#1a1a2e;border-left:4px solid {score_color};
                        border-radius:6px;padding:16px 20px;margin-bottom:16px">
              <p style="color:#ddd;font-size:0.95rem;margin:0;line-height:1.7">{summary}</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ── Per-ticker expandable cards ────────────────────────────────────────
    per_ticker = result.get("per_ticker", {})
    if per_ticker:
        st.markdown("**Per-Ticker MPT Assessment**")
        _RISK_COLOR = {"high": "#ff1744", "medium": "#ffd600", "low": "#00c853"}
        _REC_LABEL = {"reduce": "Reduce", "hold": "Hold", "increase": "Increase"}
        _REC_COLOR = {"reduce": "#ff1744", "hold": "#aaa", "increase": "#00c853"}
        for ticker_sym, entry in per_ticker.items():
            risk = entry.get("risk_assessment", "medium")
            corr_risk = entry.get("correlation_risk", "medium")
            rec = entry.get("recommendation", "hold")
            rationale = entry.get("rationale", "")
            ann_ret = entry.get("annualized_return_pct", 0.0)
            ann_vol = entry.get("annualized_volatility_pct", 0.0)
            beta_val = entry.get("beta", 1.0)
            curr_wt = entry.get("weight_current_pct", 0.0)
            sug_wt = entry.get("weight_suggested_pct", 0.0)
            risk_color = _RISK_COLOR.get(risk, "#aaa")
            rec_color = _REC_COLOR.get(rec, "#aaa")
            rec_label = _REC_LABEL.get(rec, rec.capitalize())

            with st.expander(
                f"**{ticker_sym}** — {rec_label} · {risk.capitalize()} Risk · "
                f"Ret: {ann_ret:.1f}% · Vol: {ann_vol:.1f}% · β {beta_val:.2f}",
                expanded=False,
            ):
                c1, c2, c3 = st.columns([1, 1, 3])
                with c1:
                    st.markdown(
                        f'<span style="color:{risk_color};font-weight:700;font-size:1rem">'
                        f'{risk.upper()} RISK</span>',
                        unsafe_allow_html=True,
                    )
                    st.caption(f"Corr Risk: {corr_risk.capitalize()}")
                with c2:
                    st.markdown(
                        f'<span style="color:{rec_color};font-weight:700;font-size:1rem">'
                        f'{rec_label.upper()}</span>',
                        unsafe_allow_html=True,
                    )
                    wt_delta = sug_wt - curr_wt
                    st.caption(
                        f"Curr: {curr_wt:.1f}% → Opt: {sug_wt:.1f}% "
                        f"({wt_delta:+.1f}%)"
                    )
                with c3:
                    if rationale:
                        st.markdown(
                            f'<div style="background:#1e1e2e;border-radius:6px;padding:10px 14px;'
                            f'font-size:0.88rem;color:#eee">{rationale}</div>',
                            unsafe_allow_html=True,
                        )

    # ── Action items ──────────────────────────────────────────────────────
    action_items = result.get("action_items", [])
    if action_items:
        st.markdown("")
        st.markdown("**Rebalancing Action Items**")
        for item in action_items:
            ticker_sym = item.get("ticker", "")
            action_text = item.get("action", "")
            reason_text = item.get("reason", "")
            if ticker_sym and action_text:
                st.markdown(
                    f'<div style="background:#1e1e2e;border-left:3px solid #ffd600;'
                    f'border-radius:4px;padding:8px 14px;margin-bottom:6px;font-size:0.88rem">'
                    f'<strong style="color:#ffd600">{ticker_sym}</strong>: {action_text}'
                    + (f'<br><span style="color:#aaa">{reason_text}</span>' if reason_text else "")
                    + "</div>",
                    unsafe_allow_html=True,
                )


def _render_mpt_metrics_tables(metrics: dict) -> None:
    """Render the pre-computed MPT metrics tables and correlation heatmap.

    Shows three sub-sections:
    1. Portfolio-level summary metrics (4 st.metric widgets)
    2. Per-ticker metrics as a styled DataFrame
    3. Correlation heatmap as a styled DataFrame with background_gradient

    Args:
        metrics: The dict returned by _compute_mpt_metrics() with keys:
                 tickers, annualized_returns, annualized_vols, betas,
                 weights, portfolio_return, portfolio_volatility, portfolio_sharpe,
                 hhi, correlation_matrix, max_sharpe_weights.
    """
    tickers = metrics.get("tickers", [])
    if not tickers:
        return

    st.markdown("**Pre-Computed MPT Metrics**")

    # ── Portfolio-level summary ───────────────────────────────────────────
    col1, col2, col3, col4 = st.columns(4)
    col1.metric(
        "Expected Annual Return",
        f"{metrics['portfolio_return'] * 100:.2f}%",
        help="Weighted average of each ticker's 1-year annualized return",
    )
    col2.metric(
        "Portfolio Volatility",
        f"{metrics['portfolio_volatility'] * 100:.2f}%",
        help="Annualized portfolio standard deviation accounting for correlations",
    )
    col3.metric(
        "Sharpe Ratio",
        f"{metrics['portfolio_sharpe']:.3f}",
        help="(Return − 5% risk-free rate) / Volatility. >1.0 is good.",
    )
    col4.metric(
        "HHI Concentration",
        f"{metrics['hhi']:.4f}",
        delta=f"equal-weight baseline: {1.0 / len(tickers):.4f}" if tickers else None,
        delta_color="off",
        help="Herfindahl index of weights. Lower = more diversified. Equal-weight N stocks = 1/N.",
    )

    # ── Per-ticker table ──────────────────────────────────────────────────
    rows = []
    for t in tickers:
        rows.append({
            "Ticker": t,
            "Ann. Return %": round(metrics["annualized_returns"].get(t, 0.0) * 100, 2),
            "Ann. Volatility %": round(metrics["annualized_vols"].get(t, 0.0) * 100, 2),
            "Beta (vs SPY)": round(metrics["betas"].get(t, 1.0), 3),
            "Current Weight %": round(metrics["weights"].get(t, 0.0) * 100, 2),
            "Optimal Weight %": round(metrics["max_sharpe_weights"].get(t, 0.0) * 100, 2),
        })
    if rows:
        ticker_df = pd.DataFrame(rows)
        st.dataframe(ticker_df, use_container_width=True, hide_index=True)

    # ── Correlation heatmap ───────────────────────────────────────────────
    if len(tickers) >= 2:
        st.markdown("**Correlation Heatmap** (1-year daily returns)")
        corr_data = metrics.get("correlation_matrix", {})
        corr_df = pd.DataFrame(
            [[corr_data.get(t1, {}).get(t2, 0.0) for t2 in tickers] for t1 in tickers],
            index=tickers,
            columns=tickers,
        )
        styled_corr = corr_df.style.background_gradient(
            cmap="RdYlGn_r", vmin=-1.0, vmax=1.0
        ).format("{:.3f}")
        st.dataframe(styled_corr, use_container_width=True)
```

**Why:** Splitting the metrics table rendering into `_render_mpt_metrics_tables()` allows the page to show computed metrics even when Gemini fails — the metrics are already available in the error path. The `_render_mpt_analysis()` function follows the exact same button/cache/session_state pattern as `_render_hedge_fund_overlap()` (button → check DB cache → call agent → save → render). The visual style (gradient banners, expander cards, inline HTML tags) exactly mirrors `_render_hedge_fund_analysis()`.

---

### Step 7: Add the MPT analysis call to the page entry flow in `9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** The very last three lines of the file (currently lines 1312–1314):
```python
# ---------------------------------------------------------------------------
# Smart Money Analysis
# ---------------------------------------------------------------------------
st.markdown("---")
_render_hedge_fund_overlap(positions_result)
```

**Action:** Append the following three lines AFTER the final `_render_hedge_fund_overlap(positions_result)` call (i.e., after the current last line of the file):

```python

# ---------------------------------------------------------------------------
# Modern Portfolio Theory Analysis
# ---------------------------------------------------------------------------
st.markdown("---")
_render_mpt_analysis(positions_result)
```

**Why:** The MPT section must appear after the hedge fund section to maintain the page flow order: News → Market Pulse → Options → Hedge Funds → MPT. Placing it at the end of the file after the hedge fund call ensures the positions data (`positions_result`) is already loaded and validated before the MPT functions access it.

---

### Step 8: Update the "Analyze Everything" button in `9_portfolio.py` to include MPT

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** The "Analyze Everything" button block (currently at lines 813–818):
```python
if st.button("⚡ Analyze Everything", use_container_width=True, key="analyze_everything_btn"):
    st.session_state.analyze_all_news = True
    st.session_state.analyze_all_options = True
    st.session_state.analyze_all_hf = True
    st.rerun()
```

**Action:** Replace those 5 lines with:

```python
if st.button("⚡ Analyze Everything", use_container_width=True, key="analyze_everything_btn"):
    st.session_state.analyze_all_news = True
    st.session_state.analyze_all_options = True
    st.session_state.analyze_all_hf = True
    st.session_state.analyze_all_mpt = True
    st.rerun()
```

**Why:** The "Analyze Everything" button already sets `analyze_all_news`, `analyze_all_options`, and `analyze_all_hf` flags. Adding `analyze_all_mpt = True` ensures that clicking the button also triggers the MPT analysis. The `_render_mpt_analysis()` function already checks `st.session_state.pop("analyze_all_mpt", False)` in its trigger logic, so this flag will be consumed on the next rerun.

---

## Testing Checklist

1. **Schema migration test:** Start the Streamlit app from scratch (or delete `portfolio.db` and restart). Verify no startup errors and that the DB is created. Run `sqlite3 stock-dashboard/db/portfolio.db ".tables"` and confirm `mpt_analysis` appears in the output.

2. **Import test:** Navigate to the Portfolio page. Verify there are no import errors in the terminal (no `ModuleNotFoundError` or `ImportError`). The page should load normally and show the existing balance, positions, news, options, and hedge fund sections.

3. **Pre-computed metrics display (no Gemini):** Before clicking "Run MPT Analysis," scroll to the Modern Portfolio Theory Analysis section at the bottom of the Portfolio page. Verify the section heading "### Modern Portfolio Theory Analysis" is visible and the button "▶ Run MPT Analysis" is present.

4. **Run MPT Analysis button — fresh analysis:** Click "▶ Run MPT Analysis." Verify:
   - A spinner appears with the text "Pre-computing MPT metrics and calling Gemini 2.5 Pro…"
   - After completion, the pre-computed metrics table appears showing 4 metric widgets (Expected Annual Return, Portfolio Volatility, Sharpe Ratio, HHI Concentration)
   - The per-ticker table appears with columns: Ticker, Ann. Return %, Ann. Volatility %, Beta (vs SPY), Current Weight %, Optimal Weight %
   - The correlation heatmap appears (colored green-to-red gradient)
   - The MPT score banner appears (color-coded by score)
   - Per-ticker expandable cards appear for each portfolio ticker

5. **Cache test:** After a successful run, refresh the page (F5). Verify:
   - The MPT section shows "Serving cached analysis (< 4 hours old)." at the top
   - All previously shown metrics and Gemini results are re-displayed without calling Gemini again

6. **DB cache test:** Click "▶ Run MPT Analysis" again immediately after the initial run. Verify:
   - The info message "Serving cached analysis (< 4 hours old)." appears
   - No Gemini call is made (no spinner, near-instant response)

7. **Error path test:** Temporarily rename `mpt_agent.py` to `mpt_agent.py.bak`, reload the Portfolio page, and verify the import error is shown clearly rather than crashing silently. Restore the file afterward.

8. **Analyze Everything button test:** Click "⚡ Analyze Everything" at the top of the portfolio page. After the page reruns, scroll to the MPT section and verify it triggers an MPT analysis run (or serves from cache if < 4 hours old).

9. **Correlation heatmap data accuracy:** Verify that the diagonal of the correlation matrix shows all 1.000 values (a stock is perfectly correlated with itself). Verify off-diagonal values are between -1 and 1.

10. **Action items rendering:** If Gemini returns action items, verify each action item displays as a gold-bordered card with the ticker name in bold yellow, the action text, and the reason text below it. If `action_items` is an empty list, verify no action items section is rendered.

11. **Missing market value fallback:** If the positions API returns dicts without any market value field, verify the system falls back to equal weights (each weight = 1/N) without crashing. Check the pre-computed metrics table shows equal weights in the "Current Weight %" column.

12. **SPY not in per_ticker:** Verify that the `per_ticker` section does not show an entry for "SPY" (the benchmark ticker should never appear as a portfolio position in the results).

---

## Rollback Plan

If the implementation causes errors and needs to be undone:

1. **Revert `portfolio_schema.sql`:** Remove the last 10 lines (the `CREATE TABLE IF NOT EXISTS mpt_analysis` block and its index). The `mpt_analysis` table will still exist in `portfolio.db` if the app was run, but it will be harmless and unused.

2. **Revert `portfolio_cache.py`:** Remove the three functions added in Step 2 (`save_mpt_analysis`, `get_latest_mpt_analysis`, `is_mpt_analysis_fresh`) and the `# MPT Analysis cache helpers` comment block.

3. **Revert `9_portfolio.py` imports:** Remove `save_mpt_analysis,` and `get_latest_mpt_analysis,` from the `from data.portfolio_cache import (...)` block, and remove the `from data.mpt_agent import run_mpt_analysis` line.

4. **Revert `9_portfolio.py` functions:** Remove the `_render_mpt_analysis()` and `_render_mpt_metrics_tables()` functions added in Step 6.

5. **Revert `9_portfolio.py` page entry:** Remove the `# Modern Portfolio Theory Analysis` comment block and the two lines after it (`st.markdown("---")` and `_render_mpt_analysis(positions_result)`) that were added in Step 7.

6. **Revert `9_portfolio.py` Analyze Everything:** Remove `st.session_state.analyze_all_mpt = True` from the Analyze Everything button block, reverting it to the original 3-line version.

7. **Delete new files:** Delete `stock-dashboard/data/mpt_agent.py` and `stock-dashboard/.gemini/agents/mpt-analyst.md`.

8. **Drop the DB table (optional):** Run `sqlite3 stock-dashboard/db/portfolio.db "DROP TABLE IF EXISTS mpt_analysis; DROP INDEX IF EXISTS idx_mpta_ticker_key;"` to clean up the database.
