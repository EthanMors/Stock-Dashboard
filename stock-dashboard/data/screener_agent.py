"""
data/screener_agent.py
Gemini 2.5 Pro risk analysis for speculative stock screener candidates.
Persists results to db/screener.db (screener_analysis table).
"""

import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional

from data.gemini_tracker import record_call

# ---------------------------------------------------------------------------
# DB setup (shares screener.db with data/screener.py)
# ---------------------------------------------------------------------------

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "screener.db")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "screener_schema.sql")


def _get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    """Ensure the schema exists. Called at import time. Safe if already initialised."""
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        ddl = fh.read()
    conn = _get_connection()
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()


_init_db()

# ---------------------------------------------------------------------------
# Analysis cache TTL
# ---------------------------------------------------------------------------

_ANALYSIS_TTL_HOURS = 24  # Re-analyse once per day at most


def is_analysis_fresh(analyzed_at_str: str) -> bool:
    """Return True if the analysis timestamp is within _ANALYSIS_TTL_HOURS."""
    try:
        analyzed_at = datetime.fromisoformat(analyzed_at_str.replace("Z", "+00:00"))
        if analyzed_at.tzinfo is None:
            analyzed_at = analyzed_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - analyzed_at < timedelta(hours=_ANALYSIS_TTL_HOURS)
    except (ValueError, TypeError):
        return False


def get_cached_analysis(ticker: str) -> Optional[dict]:
    """
    Return the most recent analysis for ticker from screener_analysis if still fresh.
    Returns None if no row exists or the row is stale.
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT risk_score, upside_thesis, key_risks, red_flags, verdict,
                   raw_metrics, analyzed_at
            FROM screener_analysis
            WHERE ticker = ?
            ORDER BY analyzed_at DESC
            LIMIT 1
            """,
            (ticker.upper(),),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    data = dict(row)
    if not is_analysis_fresh(data.get("analyzed_at", "")):
        return None

    # Deserialize JSON columns
    for col in ("key_risks", "red_flags", "raw_metrics"):
        raw = data.get(col) or "[]"
        try:
            data[col] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            data[col] = []

    return data


def save_analysis(ticker: str, result: dict) -> None:
    """
    Persist a Gemini risk analysis result to the screener_analysis table.

    Parameters
    ----------
    ticker : str — uppercase ticker symbol
    result : dict — must contain keys: risk_score (int), upside_thesis (str),
                    key_risks (list[str]), red_flags (list[str]),
                    verdict (str), raw_metrics (dict)
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO screener_analysis
                (ticker, risk_score, upside_thesis, key_risks, red_flags,
                 verdict, raw_metrics, analyzed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker.upper(),
                int(result.get("risk_score", 5)),
                str(result.get("upside_thesis", "")),
                json.dumps(result.get("key_risks", [])),
                json.dumps(result.get("red_flags", [])),
                str(result.get("verdict", "")),
                json.dumps(result.get("raw_metrics", {})),
                now_iso,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Gemini 2.5 Pro runner (identical to hedge_fund_agent.py / options_agent.py)
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

_PROMPT_TEMPLATE = """\
You are a speculative equity risk analyst. Your task is to assess the risk/reward
profile of a small or micro-cap stock based on the quantitative metrics below.
These are HIGH-RISK speculative stocks. Your analysis must be rigorous, honest about
risks, and use the exact numbers provided.

=== TECHNICAL METRICS ===
Ticker:                      {ticker}
Company Name:                {name}
Current Price:               ${price}
Market Cap:                  {market_cap}
52-Week High:                ${week52_high}  (current is {week52_high_chg_pct}% from high)
52-Week Low:                 ${week52_low}   (current is {week52_low_chg_pct}% above low)
Avg Daily Volume (3M):       {volume_3m}
Avg Daily Volume (10D):      {volume_10d}
Volume Spike Ratio (10D/3M): {volume_spike}
Forward PE:                  {forward_pe}
EPS (TTM):                   {eps_ttm}
Speculation Score:           {speculation_score}/100  (technical sub-score: {technical_total}/50, fundamental sub-score: {fundamental_total}/50)
RSI (14):                    {rsi}
Price Trend vs SMA50:        {trend_vs_sma50}
TA Signal:                   {ta_signal}

=== FUNDAMENTAL METRICS ===
Revenue Growth (YoY):        {revenue_growth}
Gross Margin:                {gross_margins}
Free Cash Flow:              {free_cashflow}
Cash Runway:                 {cash_runway}
Short % of Float:            {short_percent_float}
Analyst Mean Price Target:   {target_mean_price}  (implied upside: {analyst_upside})
Debt / Equity:               {debt_to_equity}
Insider Ownership:           {held_percent_insiders}
Has Fundamentals Data:       {has_fundamentals}

=== YOUR ANALYSIS TASK ===
Analyze this speculative stock and produce a structured risk assessment. Be specific
— reference the actual numbers above. Do not invent data not present in the metrics.

IMPORTANT GUIDANCE FOR FUNDAMENTAL SIGNALS:
- If cash_runway < 6 months, this is a CRITICAL red flag — dilution or bankruptcy risk is high. Always include this in red_flags.
- If short_percent_float > 20%, explicitly address squeeze potential AND covering pressure risk.
- If analyst_upside > 100%, assess whether the target is realistic given the cash/revenue data.
- If revenue_growth is N/A or has_fundamentals is False, note that fundamental data was unavailable and increase uncertainty in your assessment.
- Use insider ownership to gauge conviction: > 20% insider ownership is bullish for small-caps; < 2% is a warning sign.

IMPORTANT GUIDANCE FOR TECHNICAL SIGNALS:
- RSI <= 30 suggests the stock is oversold and may be a mean-reversion opportunity; RSI >= 70 suggests overbought conditions and near-term selling pressure.
- If TA Signal is "Bearish", weight this as an additional risk factor even if fundamentals look attractive.
- If TA Signal is "Bullish" and Price Trend vs SMA50 is "Above SMA50", this is a confirming positive technical setup.

For risk_score: rate overall risk from 1 (extremely low risk) to 10 (extremely high risk).
For upside_thesis: what specific factors in the numbers above suggest potential upside? 2-3 sentences. Reference specific numbers.
For key_risks: list 3-5 specific risk factors based on the data.
For red_flags: list any concrete warning signs visible in these metrics — especially cash runway < 6mo, negative FCF with thin cash, or extreme short interest. If none, return an empty list.
For verdict: assign exactly one of these labels:
  "lottery ticket" — extremely speculative, could go 10x or zero
  "speculative buy" — elevated risk but metrics suggest real upside potential
  "hold/watch" — not actionable yet, monitor for catalyst
  "avoid" — risk/reward unfavourable based on these metrics

Rules:
- Be specific: reference actual numbers from both metric blocks
- verdict must be exactly one of the four labels listed above
- key_risks and red_flags must be lists of plain strings (no sub-objects)
- risk_score must be an integer 1-10
- Respond ONLY with a single JSON object (no markdown fences, no preamble, no trailing text)

{{
  "risk_score": <integer 1-10>,
  "upside_thesis": "<2-3 sentence specific upside case>",
  "key_risks": ["<risk 1>", "<risk 2>", "<risk 3>"],
  "red_flags": ["<flag 1>"],
  "verdict": "<lottery ticket|speculative buy|hold/watch|avoid>"
}}
"""


def _fmt_market_cap(mc) -> str:
    """Format a market cap number as a human-readable string."""
    if mc is None:
        return "N/A"
    try:
        mc = float(mc)
        if mc >= 1e9:
            return f"${mc / 1e9:.2f}B"
        if mc >= 1e6:
            return f"${mc / 1e6:.0f}M"
        if mc >= 1e3:
            return f"${mc / 1e3:.0f}K"
        return f"${mc:,.0f}"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_optional(val, fmt: str = ".2f", suffix: str = "") -> str:
    """Format an optional numeric value; return 'N/A' if None."""
    if val is None:
        return "N/A"
    try:
        return format(float(val), fmt) + suffix
    except (TypeError, ValueError):
        return "N/A"


def _build_prompt(metrics: dict) -> str:
    """
    Build the Gemini prompt string from a metrics dict.

    metrics may be a dict from a screener DataFrame row (which contains
    the fundamental columns populated by enrich_with_fundamentals) or
    a bare dict with only technical keys (for stocks not enriched with
    fundamentals, in which case fundamental fields will be 'N/A').
    """
    vol_3m = metrics.get("volume_3m")
    vol_10d = metrics.get("volume_10d")

    if vol_3m and vol_10d and float(vol_3m) > 0:
        try:
            spike = f"{float(vol_10d) / float(vol_3m):.2f}x"
        except (TypeError, ValueError, ZeroDivisionError):
            spike = "N/A"
    else:
        spike = "N/A"

    high_chg = metrics.get("week52_high_chg_pct")
    low_chg = metrics.get("week52_low_chg_pct")

    # Parse score breakdown for sub-scores
    import json as _json
    breakdown_raw = metrics.get("score_breakdown") or "{}"
    try:
        breakdown = _json.loads(breakdown_raw) if isinstance(breakdown_raw, str) else breakdown_raw
    except (TypeError, ValueError):
        breakdown = {}

    technical_total = breakdown.get("technical_total", metrics.get("speculation_score", 0))
    fundamental_total = breakdown.get("fundamental_total", 0)

    # Fundamental fields — default to "N/A" if not present
    rev_growth = metrics.get("revenue_growth")
    rev_growth_str = f"{float(rev_growth) * 100:.1f}%" if rev_growth is not None else "N/A"

    gross_margins = None
    # gross_margins may come from the fundamentals dict embedded in score_breakdown
    # or from a future direct column; check breakdown dict
    gm_raw = breakdown.get("gross_margins") if breakdown else None
    if gm_raw is None:
        gm_raw = metrics.get("gross_margins")
    gross_margins_str = f"{float(gm_raw) * 100:.1f}%" if gm_raw is not None else "N/A"

    free_cashflow_raw = breakdown.get("free_cashflow") if breakdown else None
    if free_cashflow_raw is None:
        free_cashflow_raw = metrics.get("free_cashflow")
    free_cashflow_str = _fmt_market_cap(free_cashflow_raw) if free_cashflow_raw is not None else "N/A"

    cash_runway_years = metrics.get("cash_runway_years")
    if cash_runway_years is None:
        fcf_raw = breakdown.get("free_cashflow") if breakdown else None
        if fcf_raw is not None and float(fcf_raw) >= 0:
            cash_runway_str = "FCF positive (no burn)"
        else:
            cash_runway_str = "N/A"
    else:
        try:
            cash_runway_str = f"{float(cash_runway_years):.1f} years"
        except (TypeError, ValueError):
            cash_runway_str = "N/A"

    short_pct = metrics.get("short_percent_float")
    short_pct_str = f"{float(short_pct) * 100:.1f}%" if short_pct is not None else "N/A"

    target_price = breakdown.get("target_mean_price") if breakdown else None
    if target_price is None:
        target_price = metrics.get("target_mean_price")
    target_price_str = f"${float(target_price):.2f}" if target_price is not None else "N/A"

    analyst_upside = metrics.get("analyst_upside_pct")
    analyst_upside_str = f"{float(analyst_upside) * 100:.1f}%" if analyst_upside is not None else "N/A"

    dte = metrics.get("debt_to_equity")
    dte_str = f"{float(dte):.2f}" if dte is not None else "N/A"

    insider_pct = metrics.get("held_percent_insiders")
    insider_str = f"{float(insider_pct) * 100:.1f}%" if insider_pct is not None else "N/A"

    has_fund = metrics.get("has_fundamentals", False)
    has_fund_str = "Yes" if has_fund else "No (technicals only — fundamental data unavailable)"

    return _PROMPT_TEMPLATE.format(
        ticker=str(metrics.get("ticker", "")).upper(),
        name=str(metrics.get("name", "N/A")),
        price=_fmt_optional(metrics.get("price")),
        market_cap=_fmt_market_cap(metrics.get("market_cap")),
        week52_high=_fmt_optional(metrics.get("week52_high")),
        week52_high_chg_pct=_fmt_optional(high_chg, ".1f"),
        week52_low=_fmt_optional(metrics.get("week52_low")),
        week52_low_chg_pct=_fmt_optional(low_chg, ".1f"),
        volume_3m=_fmt_optional(vol_3m, ",.0f") if vol_3m is not None else "N/A",
        volume_10d=_fmt_optional(vol_10d, ",.0f") if vol_10d is not None else "N/A",
        volume_spike=spike,
        forward_pe=_fmt_optional(metrics.get("forward_pe")),
        eps_ttm=_fmt_optional(metrics.get("eps_ttm")),
        speculation_score=metrics.get("speculation_score", 0),
        technical_total=technical_total,
        fundamental_total=fundamental_total,
        revenue_growth=rev_growth_str,
        gross_margins=gross_margins_str,
        free_cashflow=free_cashflow_str,
        cash_runway=cash_runway_str,
        short_percent_float=short_pct_str,
        target_mean_price=target_price_str,
        analyst_upside=analyst_upside_str,
        debt_to_equity=dte_str,
        held_percent_insiders=insider_str,
        has_fundamentals=has_fund_str,
        rsi=_fmt_optional(metrics.get("rsi"), ".1f") if metrics.get("rsi") is not None else "N/A",
        trend_vs_sma50=str(metrics.get("trend_vs_sma50") or "N/A"),
        ta_signal=str(metrics.get("ta_signal") or "N/A"),
    )


# ---------------------------------------------------------------------------
# JSON parser
# ---------------------------------------------------------------------------

_VALID_VERDICTS = {"lottery ticket", "speculative buy", "hold/watch", "avoid"}


def _parse_response(raw: str) -> Optional[dict]:
    """Extract and parse the JSON object from Gemini's raw stdout."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None

    # Validate and normalize risk_score
    try:
        rs = int(data.get("risk_score", 5))
        rs = max(1, min(10, rs))
    except (TypeError, ValueError):
        rs = 5
    data["risk_score"] = rs

    # Normalize verdict
    verdict = str(data.get("verdict", "")).lower().strip()
    if verdict not in _VALID_VERDICTS:
        verdict = "hold/watch"
    data["verdict"] = verdict

    # Ensure list fields are lists of strings
    for field in ("key_risks", "red_flags"):
        val = data.get(field, [])
        if not isinstance(val, list):
            val = []
        data[field] = [str(item) for item in val]

    # Ensure text fields are strings
    data["upside_thesis"] = str(data.get("upside_thesis", "")).strip()

    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_risk_analysis(metrics: dict) -> dict:
    """
    Run Gemini 2.5 Pro risk analysis for a single speculative stock.

    Checks the SQLite cache first. If a fresh analysis (< 24h old) exists,
    returns it without calling Gemini. Otherwise calls Gemini, parses the
    result, saves it to SQLite, and returns it.

    Parameters
    ----------
    metrics : dict — a single row from the screener DataFrame as a dict.
              Must contain at minimum: ticker, name, price, market_cap,
              week52_high, week52_low, week52_high_chg_pct,
              week52_low_chg_pct, volume_3m, volume_10d, forward_pe,
              eps_ttm, speculation_score

    Returns
    -------
    dict with keys: risk_score (int), upside_thesis (str),
                    key_risks (list[str]), red_flags (list[str]),
                    verdict (str), raw_metrics (dict)
    On failure returns {"_error": str, "risk_score": 5, "verdict": "hold/watch",
                        "upside_thesis": "", "key_risks": [], "red_flags": []}
    """
    ticker = str(metrics.get("ticker", "")).upper()

    # Check cache first
    cached = get_cached_analysis(ticker)
    if cached is not None:
        return cached

    prompt = _build_prompt(metrics)
    raw, stderr = _run_gemini_pro(prompt)

    if not raw:
        return {
            "_error": stderr or "Gemini returned empty output.",
            "risk_score": 5,
            "verdict": "hold/watch",
            "upside_thesis": "",
            "key_risks": [],
            "red_flags": [],
            "raw_metrics": metrics,
        }

    result = _parse_response(raw)
    if result is None:
        return {
            "_error": f"Could not parse Gemini response.\n\nRaw output:\n{raw[:500]}",
            "risk_score": 5,
            "verdict": "hold/watch",
            "upside_thesis": "",
            "key_risks": [],
            "red_flags": [],
            "raw_metrics": metrics,
        }

    result["raw_metrics"] = metrics
    save_analysis(ticker, result)
    return result


def run_batch_risk_analysis(rows: list[dict]) -> dict[str, dict]:
    """
    Run risk analysis for a list of screener row dicts. Returns a dict mapping
    ticker → analysis result dict. Uses cached results where available.
    Each row must be a dict with the same keys as a screener DataFrame row.
    Calls Gemini serially to avoid rate limit issues.

    Parameters
    ----------
    rows : list[dict] — list of screener DataFrame rows converted to dicts

    Returns
    -------
    dict mapping ticker (str) → result dict (same shape as run_risk_analysis return)
    """
    results = {}
    for row in rows:
        ticker = str(row.get("ticker", "")).upper()
        if not ticker:
            continue
        results[ticker] = run_risk_analysis(row)
    return results
