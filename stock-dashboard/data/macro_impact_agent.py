"""Macro Impact Agent.

Takes the live FRED macro snapshot (unemployment, fed funds rate, CPI inflation,
10-year Treasury yield, real GDP) together with the user's current holdings and
calls Gemini 3.1 Pro via the Antigravity (`agy`) CLI to explain how the current
macro regime impacts the portfolio *right now*.

Follows the same runner/JSON-parse pattern as ``portfolio_insights_agent`` and
``macro_news_analyzer`` — Pro tier, strict JSON schema, no inline math by the LLM.

Public API
----------
run_macro_impact_analysis(indicators, positions) -> dict
    indicators: dict from ``data.fred_fetcher.get_macro_indicators()``
    positions:  list of position dicts (from webull) — may be empty
    Returns parsed JSON dict, or ``{"_error": str}`` on failure.
"""

import json
import re

from data.agy_client import PRO_MODEL, run_agy
from data.gemini_tracker import record_call

# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """You are a macro strategist advising on a live equity portfolio. \
You are given the latest US macroeconomic indicators (from FRED) and the investor's current holdings. \
Explain how this macro backdrop impacts THESE specific holdings right now. Be concrete and tie each \
indicator to the positions it actually affects (e.g. rate-sensitive growth names vs. rising 10-year yields, \
consumer discretionary vs. unemployment/inflation, etc.).

=== MACRO INDICATORS (latest FRED data) ===
{macro_block}
=== END MACRO ===

=== CURRENT HOLDINGS ===
{positions_block}
=== END HOLDINGS ===

Respond ONLY with valid JSON matching this exact schema:
{{
  "macro_regime": {{
    "label": "<goldilocks|expansion|slowdown|contraction|recovery|stagflation>",
    "one_liner": "<single concise sentence naming the current regime>",
    "summary": "<2-3 sentence read of the macro backdrop synthesizing the indicators above>"
  }},
  "portfolio_impact": {{
    "stance": "<tailwind|headwind|mixed|neutral>",
    "rationale": "<2-3 sentences on the net effect of this macro regime on THIS portfolio>"
  }},
  "indicator_effects": [
    {{
      "indicator": "<one of the indicator labels above>",
      "reading": "<current value + direction, e.g. '4.3%, ticking up'>",
      "impact": "<positive|negative|mixed|neutral>",
      "affected_holdings": ["<ticker>", "<ticker>"],
      "detail": "<1 sentence on how this indicator hits the named holdings>"
    }}
  ],
  "position_callouts": [
    {{
      "ticker": "<stock symbol>",
      "sensitivity": "<high|medium|low>",
      "effect": "<benefits|pressured|neutral>",
      "detail": "<1 sentence citing the specific macro driver>"
    }}
  ],
  "recommended_actions": ["<short actionable suggestion>", "<...>"]
}}

Rules:
- indicator_effects: one entry per macro indicator provided above (skip any marked unavailable).
- position_callouts: 3-5 of the most macro-sensitive holdings, ranked most-affected first.
- recommended_actions: 2-4 short, concrete suggestions tied to the macro read.
- affected_holdings must use tickers from the holdings list; use [] if none apply.
- Be specific — cite the actual indicator values and tickers from the data above.
- If holdings are empty, analyze at the index/sector level and say so briefly.
- Do NOT wrap JSON in markdown code fences.
"""


# ---------------------------------------------------------------------------
# Data formatting
# ---------------------------------------------------------------------------

def _fmt(value, suffix: str = "", signed: bool = False) -> str:
    if value is None:
        return "n/a"
    try:
        fmt = "{:+.2f}" if signed else "{:.2f}"
        return fmt.format(float(value)) + suffix
    except (TypeError, ValueError):
        return str(value)


def _build_macro_block(indicators: dict) -> str:
    """Format the FRED indicator dict into a readable text block."""
    order = ["unemployment", "fed_funds", "cpi", "treasury_10y", "real_gdp"]
    lines: list[str] = []
    for key in order:
        ind = indicators.get(key)
        if not ind:
            continue
        label = ind.get("label", key)
        if "_error" in ind:
            lines.append(f"  {label}: unavailable ({ind['_error']})")
            continue

        units = ind.get("units", "")
        unit_suffix = "%" if units == "%" else ""
        value_str = _fmt(ind.get("value"), unit_suffix)
        change_str = _fmt(ind.get("change"), unit_suffix, signed=True)
        date = ind.get("date", "")

        line = f"  {label}: {value_str} (as of {date}; change vs prior {change_str})"
        extra = ind.get("extra", {})
        if "index_level" in extra:
            line += f" [CPI index {_fmt(extra['index_level'])}]"
        lines.append(line)

    return "\n".join(lines) if lines else "  No macro data available."


def _build_positions_block(positions: list) -> str:
    """Format holdings into a compact ticker/weight list."""
    if not positions:
        return "  (no holdings provided — analyze at the market/sector level)"

    _ticker_fields = ["symbol", "ticker", "tickerSymbol", "stockSymbol", "sym"]
    _value_fields = ["marketValue", "market_value", "mktValue"]
    _prop_fields = ["proportion"]

    def _get(d, fields, default="?"):
        for f in fields:
            v = d.get(f)
            if v is not None:
                return v
        return default

    lines: list[str] = []
    for p in positions[:30]:
        ticker = str(_get(p, _ticker_fields, "?")).upper()
        mv = _get(p, _value_fields, "")
        prop = _get(p, _prop_fields, "")
        prop_str = f"{float(prop) * 100:.1f}%" if prop not in ("", "?", None) else ""
        mv_str = f"${float(mv):,.0f}" if mv not in ("", "?", None) else ""
        bits = " | ".join(filter(None, [mv_str, f"weight {prop_str}" if prop_str else ""]))
        lines.append(f"  {ticker}" + (f": {bits}" if bits else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gemini runner
# ---------------------------------------------------------------------------

def _run_gemini_pro(prompt: str) -> tuple[str, str]:
    """Call the Antigravity (agy) CLI on the Pro tier. Returns (stdout, stderr)."""
    try:
        return run_agy(prompt, model=PRO_MODEL, timeout=180)
    except Exception as exc:
        return "", str(exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_macro_impact_analysis(indicators: dict, positions: list) -> dict:
    """Analyze how the current macro regime impacts the portfolio via Gemini 3.1 Pro.

    Args:
        indicators: dict from ``fred_fetcher.get_macro_indicators()``.
        positions:  list of position dicts (may be empty).

    Returns:
        Parsed JSON dict with keys: macro_regime, portfolio_impact,
        indicator_effects, position_callouts, recommended_actions.
        Or ``{"_error": str}`` on failure.
    """
    macro_block = _build_macro_block(indicators)
    positions_block = _build_positions_block(positions or [])
    prompt = _PROMPT_TEMPLATE.format(macro_block=macro_block, positions_block=positions_block)

    raw, stderr = _run_gemini_pro(prompt)

    if not raw:
        return {"_error": f"No response from Gemini 3.1 Pro. {stderr[:200]}"}

    record_call("pro")

    clean = re.sub(r"```(?:json)?\s*", "", raw).strip().strip("`")
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", clean, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {"_error": f"Could not parse Gemini response as JSON.\nRaw response:\n{raw[:800]}"}
