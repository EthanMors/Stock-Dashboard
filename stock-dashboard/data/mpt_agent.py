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
        "Respond ONLY with a single JSON object (no markdown fences, no preamble, no trailing text). "
        "Use EXACTLY these key names:"
    )
    lines.append("""{
  "per_ticker": {
    "<TICKER>": {
      "annualized_return_pct": <float>,
      "annualized_volatility_pct": <float>,
      "beta": <float>,
      "weight_current_pct": <float>,
      "weight_suggested_pct": <float>,
      "risk_assessment": "<high|medium|low>",
      "correlation_risk": "<high|medium|low>",
      "recommendation": "<reduce|hold|increase>",
      "rationale": "<2-3 sentences referencing actual numbers>"
    }
  },
  "portfolio_metrics": {
    "expected_return_pct": <float>,
    "volatility_pct": <float>,
    "sharpe_ratio": <float>,
    "hhi_concentration": <float>,
    "diversification_score": "<well_diversified|moderate|concentrated|highly_concentrated>"
  },
  "mpt_analysis": {
    "overall_score": "<excellent|good|fair|poor>",
    "efficient_frontier_position": "<on_frontier|below_frontier|inefficient>",
    "key_inefficiencies": ["<string>", "<string>"],
    "rebalancing_priority": "<urgent|moderate|low>",
    "summary": "<3-4 sentence synthesis>"
  },
  "action_items": [
    {"ticker": "<TICKER>", "action": "<e.g. reduce position by 7%>", "reason": "<one sentence>"}
  ]
}""")

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
