import json
import re
import subprocess

from data.gemini_tracker import record_call


# ---------------------------------------------------------------------------
# Gemini Pro runner (same pattern as options_agent.py)
# ---------------------------------------------------------------------------

def _run_gemini_pro(prompt: str) -> tuple[str, str]:
    """Call Gemini 2.5 Pro via CLI. Returns (stdout, stderr). Prompt passed via stdin."""
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
# Prompt builder
# ---------------------------------------------------------------------------

def _format_value(val: float) -> str:
    """Format a dollar value into a human-readable string (B/M/K)."""
    if val >= 1e9:
        return f"${val / 1e9:.2f}B"
    if val >= 1e6:
        return f"${val / 1e6:.2f}M"
    if val >= 1e3:
        return f"${val / 1e3:.2f}K"
    return f"${val:,.0f}"


def _build_prompt(overlapping_funds: list, portfolio_tickers: list) -> str:
    """Build the LLM-optimized prompt text from overlap data.

    Parameters
    ----------
    overlapping_funds : List of fund dicts as returned by _find_overlapping_funds()
                        in 9_portfolio.py. Each dict has keys:
                        name, cik, report_period, filing_date, total_value,
                        overlapping_holdings (list of HoldingRow dicts with keys:
                        ticker, issuer, value, shares, pct_of_portfolio, put_call),
                        overlap_count.
    portfolio_tickers : List of uppercase ticker strings for the portfolio.
    """
    lines = []

    # Section 1 — Portfolio tickers
    lines.append("=== PORTFOLIO TICKERS HELD ===")
    lines.append(", ".join(portfolio_tickers))
    lines.append("")

    # Section 2 — Per-fund breakdown
    lines.append("=== OVERLAPPING FUNDS (concentrated hedge funds also holding your stocks) ===")
    lines.append("")

    for fund in overlapping_funds:
        fund_name = fund.get("name") or fund.get("cik", "Unknown")
        total_val = fund.get("total_value", 0.0)
        # Estimate position count from overlapping holdings list length
        # (total_holdings not available in the overlap dict — use overlapping only)
        holdings = fund.get("overlapping_holdings", [])

        lines.append(
            f"FUND: {fund_name}  |  AUM: {_format_value(total_val)}  "
            f"|  Report: {fund.get('report_period', 'N/A')}  "
            f"|  Filed: {fund.get('filing_date', 'N/A')}"
        )
        lines.append("  Holdings overlapping your portfolio:")
        for h in holdings:
            ticker = str(h.get("ticker", "")).upper()
            pct = h.get("pct_of_portfolio", 0.0)
            val = h.get("value", 0.0)
            shares = h.get("shares", 0.0)
            put_call = str(h.get("put_call", "")).strip().upper()
            position_type = put_call if put_call else "EQUITY"
            shares_str = f"{shares / 1e6:.2f}M" if shares >= 1e6 else f"{shares:,.0f}"
            lines.append(
                f"    - {ticker}: {pct:.1f}% of fund ({_format_value(val)}, {shares_str} shares) [{position_type}]"
            )
        lines.append("")

    # Section 3 — Cross-fund ticker summary (aggregate stats per ticker)
    lines.append("=== CROSS-FUND TICKER SUMMARY ===")

    # Build per-ticker aggregates
    ticker_stats: dict[str, dict] = {}
    for fund in overlapping_funds:
        for h in fund.get("overlapping_holdings", []):
            t = str(h.get("ticker", "")).upper()
            if not t:
                continue
            if t not in ticker_stats:
                ticker_stats[t] = {"fund_count": 0, "pct_sum": 0.0, "put_count": 0}
            ticker_stats[t]["fund_count"] += 1
            ticker_stats[t]["pct_sum"] += h.get("pct_of_portfolio", 0.0)
            put_call = str(h.get("put_call", "")).strip().upper()
            if put_call in ("PUT", "P"):
                ticker_stats[t]["put_count"] += 1

    for ticker in portfolio_tickers:
        stats = ticker_stats.get(ticker)
        if stats is None:
            continue
        fc = stats["fund_count"]
        avg_pct = stats["pct_sum"] / fc if fc > 0 else 0.0
        put_c = stats["put_count"]
        lines.append(
            f"{ticker}: owned by {fc} fund{'s' if fc != 1 else ''}  |  "
            f"avg position size: {avg_pct:.1f}% of fund  |  "
            f"put positions: {put_c} of {fc}"
        )

    lines.append("")

    # Final instruction block
    lines.append("=== YOUR ANALYSIS TASK ===")
    lines.append(
        "You are a hedge fund analyst reviewing 13F SEC filing data. "
        "Based on the fund concentration, position sizes as % of fund AUM, put/call flags, "
        "and cross-fund ticker ownership patterns above, analyze the smart money positioning "
        "across this portfolio."
    )
    lines.append("")
    lines.append(
        "For each ticker owned by at least one fund, infer:"
        "\n  1. The conviction level (high/medium/low) based on position size as % of fund and number of funds"
        "\n  2. The ownership type (bullish_equity / hedged / speculative_put / mixed)"
        "\n  3. The inferred investment thesis — what macro, sector, or company-specific thesis "
        "does this concentration imply? Reference the fund's AUM, filing date, and position weighting."
        "\n  4. The single most important key signal about smart money positioning in this stock"
    )
    lines.append("")
    lines.append(
        "Then synthesize a portfolio-level signal: what does the collective hedge fund positioning "
        "across ALL your stocks suggest about overall market stance, sector rotation, or risk?"
    )
    lines.append("")
    lines.append(
        "Flag any unusual or notable positioning: e.g. a fund with 40%+ in one of your tickers, "
        "put-heavy positioning suggesting a hedge rather than a bullish bet, very recent filing dates "
        "indicating timely positioning, or a single fund owning multiple of your tickers (concentrated overlap)."
    )
    lines.append("")
    lines.append("Rules:")
    lines.append("  - Be specific: reference actual fund names, percentages, and dollar amounts from the data")
    lines.append("  - Distinguish between a long equity position and a put/call option position")
    lines.append("  - Inferred thesis should be 2-3 sentences per ticker")
    lines.append("  - key_signal should be one sentence only")
    lines.append("  - Only include tickers in per_ticker that are actually held by at least one fund above")
    lines.append("  - cross_ticker_themes should be 2-4 thematic strings (brief phrases, not sentences)")
    lines.append("  - flags should be a list of strings, each describing one notable anomaly (or empty list)")
    lines.append("")
    lines.append(
        "Respond ONLY with a single JSON object (no markdown fences, no preamble, no trailing text):"
    )
    lines.append("""{
  "per_ticker": {
    "<TICKER>": {
      "conviction_level": "<high|medium|low>",
      "ownership_type": "<bullish_equity|hedged|speculative_put|mixed>",
      "inferred_thesis": "<2-3 sentence inference>",
      "fund_count": <integer>,
      "key_signal": "<one sentence>"
    }
  },
  "portfolio_signal": {
    "overall_stance": "<bullish|bearish|mixed|defensive>",
    "confidence": "<high|medium|low>",
    "cross_ticker_themes": ["<theme 1>", "<theme 2>"],
    "summary": "<3-4 sentence synthesis>"
  },
  "flags": ["<notable anomaly 1>", "<notable anomaly 2>"]
}""")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON parser
# ---------------------------------------------------------------------------

def _parse_response(raw: str) -> dict | None:
    """Extract and parse the JSON object from Gemini's raw stdout."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None

    # Validate top-level structure
    if "per_ticker" not in data or "portfolio_signal" not in data:
        return None

    # Normalize portfolio_signal fields
    ps = data.get("portfolio_signal", {})
    stance = str(ps.get("overall_stance", "mixed")).lower()
    if stance not in ("bullish", "bearish", "mixed", "defensive"):
        stance = "mixed"
    ps["overall_stance"] = stance

    conf = str(ps.get("confidence", "medium")).lower()
    if conf not in ("high", "medium", "low"):
        conf = "medium"
    ps["confidence"] = conf

    if not isinstance(ps.get("cross_ticker_themes"), list):
        ps["cross_ticker_themes"] = []
    if not isinstance(ps.get("summary"), str):
        ps["summary"] = ""
    data["portfolio_signal"] = ps

    # Normalize per_ticker entries
    for ticker, entry in data.get("per_ticker", {}).items():
        if not isinstance(entry, dict):
            continue
        conv = str(entry.get("conviction_level", "medium")).lower()
        if conv not in ("high", "medium", "low"):
            conv = "medium"
        entry["conviction_level"] = conv

        ot = str(entry.get("ownership_type", "bullish_equity")).lower()
        if ot not in ("bullish_equity", "hedged", "speculative_put", "mixed"):
            ot = "bullish_equity"
        entry["ownership_type"] = ot

        if not isinstance(entry.get("inferred_thesis"), str):
            entry["inferred_thesis"] = ""
        if not isinstance(entry.get("key_signal"), str):
            entry["key_signal"] = ""
        if not isinstance(entry.get("fund_count"), int):
            try:
                entry["fund_count"] = int(entry.get("fund_count", 1))
            except (TypeError, ValueError):
                entry["fund_count"] = 1

    # Normalize flags
    if not isinstance(data.get("flags"), list):
        data["flags"] = []

    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_hedge_fund_analysis(overlapping_funds: list, portfolio_tickers: list) -> dict | None:
    """Run Gemini 2.5 Pro analysis on hedge fund overlap data.

    Builds a structured prompt from the overlapping fund/holding data, calls
    Gemini 2.5 Pro via CLI subprocess (stdin delivery), and returns a parsed
    dict. Returns None if Gemini fails or returns unparseable output.

    Parameters
    ----------
    overlapping_funds : List of fund dicts as returned by _find_overlapping_funds()
                        in 9_portfolio.py. Each dict has keys:
                        name, cik, report_period, filing_date, total_value,
                        overlapping_holdings (list of HoldingRow dicts),
                        overlap_count.
    portfolio_tickers : List of uppercase ticker strings for the portfolio.

    Returns
    -------
    dict with keys: per_ticker, portfolio_signal, flags
    Returns {"_error": str} on failure (no None so the caller can check _error).
    Returns None only if Gemini returns empty output.
    """
    if not overlapping_funds or not portfolio_tickers:
        return {"_error": "No overlapping fund data to analyze."}

    prompt = _build_prompt(overlapping_funds, portfolio_tickers)
    raw, stderr = _run_gemini_pro(prompt)

    if not raw:
        return {"_error": stderr or "Gemini returned empty output."}

    result = _parse_response(raw)
    if result is None:
        return {"_error": f"Could not parse Gemini response.\n\nRaw output:\n{raw[:500]}"}

    return result
