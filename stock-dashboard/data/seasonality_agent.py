"""Regime & seasonality strategist agent.

All numbers are computed in `data/seasonality.py` first; this module only hands
the finished figures to Gemini 2.5 Pro via CLI subprocess and parses the
structured JSON verdict back out.

Public API
----------
run_seasonality_analysis(payload) -> dict | None
    payload: build_analysis_payload() output from data/seasonality.py.
    Returns the parsed Gemini JSON dict, or {"_error": str} on failure.
"""

import json
import re
import subprocess

import pandas as pd

from data.gemini_tracker import record_call

_PROMPT_TEMPLATE = """\
You are a macro strategist and portfolio risk manager (Persona: regime-strategist).

Your job: tell this investor what the next 1-3 months look like for the portfolio
they actually hold, and what to change. Be concrete and decisive. No hedging
boilerplate, no generic "consult a financial advisor" filler.

Today is {as_of}. The month ahead is {month_ahead}.

## Current macro regime (computed from cross-asset price behaviour)
{regime_block}

## Seasonality for the month ahead
{seasonality_block}

## Sector relative strength vs SPY
{momentum_block}

## The investor's current sector exposure vs S&P 500 weights
{exposure_block}

## Quantitative rotation scorecard (higher = more deserving of capital)
{scorecard_block}

## Scheduled catalysts in the next 60 days
{catalyst_block}

## Holdings
{holdings_block}

---

Analyse this and respond with ONLY a JSON object, no markdown fence, no prose
outside the JSON, in exactly this shape:

{{
  "regime_label": "short name for the regime, e.g. 'Late-cycle disinflation'",
  "regime_summary": "3-5 sentences on what regime we are in, what is actually driving it in the data above, and what typically works in it",
  "confidence": "high|medium|low",
  "month_ahead": "{month_ahead}",
  "seasonal_outlook": "3-5 sentences on what the seasonal data implies for the next 1-3 months for THIS portfolio specifically, including where seasonality and the regime disagree",
  "macro_outlook": "3-5 sentences on the rate path, inflation, growth, and what would have to change for the regime to flip",
  "catalyst_watch": [
    {{"date": "YYYY-MM-DD", "event": "name", "why_it_matters": "one sentence tied to a specific holding or sector in this portfolio"}}
  ],
  "position_actions": [
    {{"ticker": "SYM", "action": "add|hold|trim|hedge|exit", "rationale": "one or two sentences citing the seasonal, momentum, or regime evidence above", "urgency": "now|this month|next quarter|watch"}}
  ],
  "sector_actions": [
    {{"sector": "sector name", "etf": "ETF ticker", "action": "add|overweight|hold|underweight|avoid", "rationale": "why, citing the data above", "conviction": "high|medium|low"}}
  ],
  "risk_factors": ["specific things that would break this view, each one sentence"]
}}

Rules:
- Cover every holding listed above in position_actions.
- In sector_actions, prioritise sectors the portfolio has NO or LOW exposure to
  that the scorecard ranks highly — the investor explicitly wants to know which
  sectors to add.
- Say when seasonality is too weak a signal to act on; do not manufacture
  conviction from a 0.3% average monthly edge.
- Reference actual numbers from the data above in your rationales.
"""


def _format_regime(regime: dict) -> str:
    lines = [
        f"Regime label: {regime.get('label', 'Unknown')}",
        f"Risk appetite score: {regime.get('risk_score', 0)} (-100 risk-off to +100 risk-on)",
        f"Rate score: {regime.get('rate_score', 0)} (positive = yields falling / easing)",
        f"Growth score: {regime.get('growth_score', 0)}",
        f"Inflation score: {regime.get('inflation_score', 0)}",
        f"Regime-favoured sectors: {', '.join(regime.get('favored_sectors', [])) or 'none'}",
        f"Regime-pressured sectors: {', '.join(regime.get('pressured_sectors', [])) or 'none'}",
        "",
        "Indicator readings:",
    ]
    for name, data in (regime.get("indicators") or {}).items():
        value = data.get("value")
        change = data.get("change_1m")
        bits = []
        if value is not None:
            bits.append(f"level {value}")
        if change is not None:
            bits.append(f"{change:+.2f}% over ~1 month")
        if data.get("percentile_1y") is not None:
            bits.append(f"{data['percentile_1y']}th percentile of the last year")
        lines.append(f"- {name}: {', '.join(bits) or 'n/a'} — {data.get('note', '')}")
    return "\n".join(lines)


def _format_seasonality(payload: dict) -> str:
    month = payload.get("month_ahead", "")
    lines = []

    bench = payload.get("benchmark_seasonality", {}) or {}
    bench_month = (bench.get("months") or {}).get(month)
    if bench_month:
        lines.append(
            f"SPY in {month} over {bench.get('years_of_data', 0)} years: "
            f"avg {bench_month['avg']}%, median {bench_month['median']}%, "
            f"win rate {bench_month['win_rate']}%, "
            f"best {bench_month['best']}%, worst {bench_month['worst']}%."
        )
    if bench.get("best_month"):
        lines.append(
            f"SPY's historically strongest month is {bench['best_month']}, "
            f"weakest is {bench['worst_month']}."
        )

    windows = payload.get("active_seasonal_windows") or []
    if windows:
        lines.append("")
        lines.append("Active seasonal windows right now:")
        for window in windows:
            lines.append(f"- {window['window']} (through {window['ends']}): {window['note']}")

    for label, key in (("Sector ETFs", "sector_seasonality"),
                       ("Holdings", "holdings_seasonality")):
        matrix = payload.get(key)
        if isinstance(matrix, pd.DataFrame) and not matrix.empty and month in matrix.columns:
            lines.append("")
            lines.append(f"{label} — average {month} return (%), best first:")
            column = matrix[month].dropna().sort_values(ascending=False)
            for ticker, value in column.items():
                lines.append(f"- {ticker}: {value:+.2f}%")
    return "\n".join(lines) or "No seasonality data available."


def _format_momentum(momentum: list) -> str:
    if not momentum:
        return "No sector momentum data available."
    lines = ["Sector | 1m vs SPY | 3m vs SPY | 6m vs SPY | blended RS | above 200DMA"]
    for row in momentum:
        lines.append(
            f"{row['sector']} ({row['etf']}) | "
            f"{row.get('rs_1m')} | {row.get('rs_3m')} | {row.get('rs_6m')} | "
            f"{row.get('rs_score')} | {row.get('above_200dma')}"
        )
    return "\n".join(lines)


def _format_exposure(exposure: dict) -> str:
    sectors = exposure.get("sectors") or []
    if not sectors:
        return "No classified positions."
    lines = [f"Total portfolio value: ${exposure.get('total_value', 0):,.2f}", "",
             "Sector | portfolio weight % | S&P weight % | gap (pts) | holdings"]
    for row in sectors:
        holdings = ", ".join(row.get("tickers", [])) or "—"
        lines.append(
            f"{row['sector']} | {row['weight']} | {row['benchmark_weight']} | "
            f"{row['gap']:+.2f} | {holdings}"
        )
    if exposure.get("unclassified"):
        lines.append("")
        lines.append(f"Unclassified tickers: {', '.join(exposure['unclassified'])}")
    return "\n".join(lines)


def _format_scorecard(scorecard: list) -> str:
    if not scorecard:
        return "No scorecard available."
    lines = ["Sector | score | seasonal avg % | blended RS | regime fit | weight gap | suggested action"]
    for row in scorecard:
        lines.append(
            f"{row['sector']} ({row['etf']}) | {row['total_score']} | "
            f"{row.get('seasonal_avg')} | {row.get('rs_score')} | "
            f"{row.get('regime_fit')} | {row.get('gap')} | {row.get('action')}"
        )
    return "\n".join(lines)


def _format_catalysts(catalysts: list) -> str:
    if not catalysts:
        return "No scheduled catalysts found in the window."
    lines = []
    for event in catalysts[:40]:
        lines.append(
            f"- {event['date']} (in {event['days_out']}d) [{event['kind']}] "
            f"{event['event']}: {event['detail']}"
        )
    return "\n".join(lines)


def _format_holdings(payload: dict) -> str:
    tickers = payload.get("tickers") or []
    if not tickers:
        return "No holdings supplied."
    exposure_by_ticker = {}
    for row in (payload.get("exposure", {}).get("sectors") or []):
        for ticker in row.get("tickers", []):
            exposure_by_ticker[ticker] = row["sector"]
    return "\n".join(
        f"- {ticker} ({exposure_by_ticker.get(ticker, 'sector unknown')})"
        for ticker in tickers
    )


def build_prompt(payload: dict) -> str:
    """Render the full strategist prompt from a build_analysis_payload() dict."""
    return _PROMPT_TEMPLATE.format(
        as_of=payload.get("as_of", ""),
        month_ahead=payload.get("month_ahead", ""),
        regime_block=_format_regime(payload.get("regime", {}) or {}),
        seasonality_block=_format_seasonality(payload),
        momentum_block=_format_momentum(payload.get("sector_momentum") or []),
        exposure_block=_format_exposure(payload.get("exposure", {}) or {}),
        scorecard_block=_format_scorecard(payload.get("scorecard") or []),
        catalyst_block=_format_catalysts(payload.get("catalysts") or []),
        holdings_block=_format_holdings(payload),
    )


def _run_gemini_pro(prompt: str) -> tuple[str, str]:
    """Run Gemini 2.5 Pro headlessly. Returns (stdout, stderr)."""
    try:
        result = subprocess.run(
            ["gemini.cmd", "-m", "gemini-2.5-pro", "-p", ""],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=240,
        )
        output = result.stdout.strip()
        if output:
            record_call("pro")
        return output, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return "", "Timed out after 240s"
    except FileNotFoundError:
        return "", ("gemini.cmd was not found on PATH — install the Gemini CLI "
                    "(npm i -g @google/gemini-cli) and restart the dashboard.")
    except Exception as exc:
        return "", str(exc)


def _parse_gemini_json(raw: str) -> dict | None:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return None


def run_seasonality_analysis(payload: dict) -> dict | None:
    """Ask Gemini 2.5 Pro to turn the computed payload into an action plan.

    Returns the parsed JSON dict, or {"_error": "..."} when the call or the
    parse fails.
    """
    prompt = build_prompt(payload)
    raw, err = _run_gemini_pro(prompt)
    if not raw:
        return {"_error": err or "Gemini returned no output."}

    parsed = _parse_gemini_json(raw)
    if parsed is None:
        return {"_error": f"Could not parse JSON from Gemini output: {raw[:400]}"}

    # Normalise the list fields so the page can render without guarding.
    for key in ("catalyst_watch", "position_actions", "sector_actions", "risk_factors"):
        if not isinstance(parsed.get(key), list):
            parsed[key] = []
    return parsed
