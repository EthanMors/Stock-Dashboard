"""Portfolio AI Insights Agent

Combines all portfolio analysis data (positions, news sentiment, options,
Reddit, hedge funds, MPT) and calls Gemini 2.5 Pro via CLI to generate
top-level actionable insights across ALL data sources.

Public API
----------
run_portfolio_insights(portfolio_data) -> dict
    portfolio_data: dict with keys:
        balance        (dict) - account balance fields
        positions      (list) - position dicts from webull
        news_results   (dict) - {ticker: entry} from news analysis
        options_results(dict) - {sess_key: entry} from options analysis
        wsb_results    (dict) - {ticker: entry} from reddit sentiment
        mpt_analysis   (dict) - mpt entry from session state
        hf_analysis    (dict) - hedge fund analysis from session state
    Returns parsed JSON dict, or {"_error": str} on failure.
"""

import json
import re
import subprocess

from data.gemini_tracker import record_call

# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """You are a professional portfolio analyst with expertise in equity analysis, options flow, fundamental research, and quantitative finance.

Below is a comprehensive real-time snapshot of a live trading portfolio. Analyze ALL the data holistically and provide actionable intelligence. Cross-reference signals from different sources (e.g., if news is bearish but options flow is bullish, flag the divergence).

=== PORTFOLIO DATA ===
{data_block}
=== END DATA ===

Respond ONLY with valid JSON matching this exact schema:
{{
  "portfolio_health": {{
    "score": "<excellent|good|fair|poor>",
    "one_liner": "<single concise sentence summarizing overall portfolio health>",
    "summary": "<2-3 sentence narrative that synthesizes insights from ALL available data sources above>"
  }},
  "top_actions": [
    {{
      "ticker": "<stock symbol or 'PORTFOLIO' for portfolio-level actions>",
      "action": "<Buy|Sell|Trim|Hold|Hedge|Rebalance|Watch>",
      "urgency": "<immediate|this_week|this_month>",
      "rationale": "<1-2 sentences citing specific data points from the snapshot above>",
      "catalyst": "<key upcoming catalyst or risk to watch>"
    }}
  ],
  "key_risks": [
    {{
      "risk": "<short risk name>",
      "severity": "<high|medium|low>",
      "detail": "<concise 1-sentence description of the risk and which positions are affected>"
    }}
  ],
  "cross_signal_themes": ["<theme1>", "<theme2>", "<theme3>"],
  "smart_money_divergence": "<description if retail/Reddit sentiment diverges from institutional 13F positioning, or 'None detected'>"
}}

Rules:
- top_actions: exactly 3 items, ranked by priority (most actionable first)
- key_risks: exactly 3 items, ranked by severity (highest first)
- cross_signal_themes: 3-5 short descriptive tags (e.g., "Options bearish skew on AAPL", "Institutional accumulation in tech")
- Be specific — cite tickers, numbers, and data sources from the snapshot above
- If a data section is missing or empty, note the gap briefly and use available data
- Do NOT wrap JSON in markdown code fences
"""


# ---------------------------------------------------------------------------
# Data formatting
# ---------------------------------------------------------------------------

def _build_data_block(portfolio_data: dict) -> str:
    """Format portfolio_data dict into a readable text block for the prompt."""
    sections: list[str] = []

    # Balance & Account
    balance = portfolio_data.get("balance", {})
    positions = portfolio_data.get("positions", [])
    if balance:
        net_liq = balance.get("total_net_liquidation_value", "N/A")
        day_pnl = balance.get("total_day_profit_loss", "N/A")
        unreal  = balance.get("total_unrealized_profit_loss", "N/A")
        cash    = balance.get("total_cash_balance", "N/A")
        mkt_val = balance.get("total_market_value", "N/A")
        sections.append(
            f"ACCOUNT BALANCE:\n"
            f"  Net Liquidation: ${net_liq}\n"
            f"  Market Value: ${mkt_val}\n"
            f"  Day P&L: ${day_pnl}\n"
            f"  Unrealized P&L: ${unreal}\n"
            f"  Cash: ${cash}"
        )

    # Positions
    if positions:
        _ticker_fields  = ["symbol", "ticker", "tickerSymbol", "stockSymbol", "sym"]
        _value_fields   = ["marketValue", "market_value", "mktValue"]
        _upnl_fields    = ["unrealizedProfitLoss", "unrealized_profit_loss", "unrealizedPnl"]
        _prop_fields    = ["proportion"]

        def _get(d, fields, default="?"):
            for f in fields:
                v = d.get(f)
                if v is not None:
                    return v
            return default

        pos_lines = []
        for p in positions[:25]:
            ticker = str(_get(p, _ticker_fields, "?")).upper()
            mv     = _get(p, _value_fields, "")
            upnl   = _get(p, _upnl_fields, "")
            prop   = _get(p, _prop_fields, "")
            prop_str = f"{float(prop)*100:.1f}%" if prop and prop != "?" else str(prop)
            mv_str   = f"${float(mv):,.2f}" if mv and mv != "?" else str(mv)
            upnl_str = f"${float(upnl):+,.2f}" if upnl and upnl != "?" else str(upnl)
            pos_lines.append(f"  {ticker}: MV={mv_str} | Unrealized={upnl_str} | Weight={prop_str}")
        sections.append("POSITIONS (" + str(len(positions)) + " total):\n" + "\n".join(pos_lines))

    # News Sentiment
    news_results = portfolio_data.get("news_results", {})
    if news_results:
        news_lines = []
        for ticker, entry in list(news_results.items())[:15]:
            r       = entry.get("result", {})
            label   = r.get("sentiment_label", "?").upper()
            score   = r.get("sentiment_score", 0.0)
            impact  = r.get("impact_level", 0)
            summary = r.get("summary", "")[:180]
            themes  = r.get("key_themes", [])
            theme_str = " | ".join(themes[:3]) if themes else ""
            news_lines.append(
                f"  {ticker}: {label} ({score:+.2f}, impact {impact}/10)"
                + (f" [{theme_str}]" if theme_str else "")
                + (f" — {summary}" if summary else "")
            )
        sections.append("NEWS SENTIMENT:\n" + "\n".join(news_lines))

    # Options Flow
    options_results = portfolio_data.get("options_results", {})
    if options_results:
        opt_lines = []
        for sess_key, entry in list(options_results.items())[:10]:
            if "_error" in entry:
                continue
            parts    = sess_key.split("|")
            ticker   = parts[0] if parts else sess_key
            bias     = entry.get("directional_bias", "neutral").upper()
            strength = entry.get("bias_strength", "moderate")
            conf     = entry.get("confidence", "medium")
            metrics  = entry.get("metrics", {})
            pcr_oi   = metrics.get("pcr_oi")
            pcr_vol  = metrics.get("pcr_vol")
            max_pain = metrics.get("max_pain")
            iv_skew  = metrics.get("iv_skew")
            pcr_str  = f"P/C_OI={pcr_oi:.3f}" if pcr_oi is not None else ""
            skew_str = f"IV_skew={iv_skew*100:+.1f}%" if iv_skew is not None else ""
            pain_str = f"MaxPain=${max_pain:.2f}" if max_pain is not None else ""
            summary  = entry.get("summary", "")[:120]
            opt_lines.append(
                f"  {ticker}: {bias} ({strength} strength, conf={conf})"
                + (" | " + " | ".join(filter(None, [pcr_str, skew_str, pain_str])) if any([pcr_str, skew_str, pain_str]) else "")
                + (f" — {summary}" if summary else "")
            )
        if opt_lines:
            sections.append("OPTIONS FLOW:\n" + "\n".join(opt_lines))

    # Reddit / WSB Sentiment
    wsb_results = portfolio_data.get("wsb_results", {})
    if wsb_results:
        wsb_lines = []
        for ticker, entry in list(wsb_results.items())[:10]:
            summary_row = entry.get("summary_row")
            if summary_row:
                label   = summary_row.get("sentiment_label", "neutral").upper()
                score   = summary_row.get("sentiment_score", 0.0)
                summary = summary_row.get("summary", "")[:120]
                wsb_lines.append(f"  {ticker}: {label} ({score:+.2f}) — {summary}")
        if wsb_lines:
            sections.append("REDDIT/WSB SENTIMENT:\n" + "\n".join(wsb_lines))

    # MPT Analysis
    mpt_entry = portfolio_data.get("mpt_analysis")
    if mpt_entry:
        result  = mpt_entry.get("result", {})
        metrics = mpt_entry.get("metrics", {})
        ma      = result.get("mpt_analysis", {})
        overall = ma.get("overall_score", "?")
        rebal   = ma.get("rebalancing_priority", "?")
        summary = ma.get("summary", "")[:200]
        inefficiencies = ma.get("key_inefficiencies", [])
        port_ret = (metrics.get("portfolio_return", 0) or 0) * 100
        port_vol = (metrics.get("portfolio_volatility", 0) or 0) * 100
        sharpe   = metrics.get("portfolio_sharpe", 0) or 0
        hhi      = metrics.get("hhi", 0) or 0

        action_items = result.get("action_items", [])
        actions_str = " | ".join(
            f"{a.get('ticker', '?')}: {a.get('action', '?')}"
            for a in action_items[:5]
        )
        sections.append(
            f"MPT ANALYSIS:\n"
            f"  Overall Score: {overall.upper()} | Rebalancing Priority: {rebal}\n"
            f"  Expected Annual Return: {port_ret:.1f}% | Volatility: {port_vol:.1f}% | Sharpe: {sharpe:.2f} | HHI: {hhi:.4f}\n"
            + (f"  Key Inefficiencies: {', '.join(inefficiencies)}\n" if inefficiencies else "")
            + (f"  Summary: {summary}\n" if summary else "")
            + (f"  Recommended Actions: {actions_str}" if actions_str else "")
        )

    # Hedge Fund / Smart Money
    hf_entry = portfolio_data.get("hf_analysis")
    if hf_entry and "_error" not in hf_entry:
        ps      = hf_entry.get("portfolio_signal", {})
        stance  = ps.get("overall_stance", "?").upper()
        conf    = ps.get("confidence", "?")
        themes  = ps.get("cross_ticker_themes", [])
        summary = ps.get("summary", "")[:200]
        flags   = hf_entry.get("flags", [])

        per_ticker = hf_entry.get("per_ticker", {})
        pt_lines = []
        for t, te in list(per_ticker.items())[:8]:
            conv    = te.get("conviction_level", "?")
            ot      = te.get("ownership_type", "?")
            thesis  = te.get("inferred_thesis", "")[:80]
            pt_lines.append(f"    {t}: {conv} conviction | {ot} | {thesis}")

        sections.append(
            f"SMART MONEY / 13F FILINGS:\n"
            f"  Overall Stance: {stance} (confidence: {conf})\n"
            + (f"  Themes: {', '.join(themes)}\n" if themes else "")
            + (f"  Summary: {summary}\n" if summary else "")
            + (f"  Per-Ticker:\n" + "\n".join(pt_lines) + "\n" if pt_lines else "")
            + (f"  Flags: {'; '.join(flags)}" if flags else "")
        )

    return "\n\n".join(sections) if sections else "No portfolio data available."


# ---------------------------------------------------------------------------
# Gemini runner
# ---------------------------------------------------------------------------

def _run_gemini_pro(prompt: str) -> tuple[str, str]:
    """Call Gemini 2.5 Pro via CLI subprocess. Returns (stdout, stderr)."""
    try:
        result = subprocess.run(
            ["gemini.cmd", "-m", "gemini-2.5-pro", "-p", ""],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
        )
        return result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return "", "Gemini 2.5 Pro timed out after 180s."
    except Exception as e:
        return "", str(e)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_portfolio_insights(portfolio_data: dict) -> dict:
    """Generate holistic actionable insights from all portfolio data using Gemini 2.5 Pro.

    Args:
        portfolio_data: dict with keys: balance, positions, news_results,
                        options_results, wsb_results, mpt_analysis, hf_analysis.

    Returns:
        Parsed JSON dict with keys: portfolio_health, top_actions, key_risks,
        cross_signal_themes, smart_money_divergence. Or {"_error": str} on failure.
    """
    data_block = _build_data_block(portfolio_data)
    prompt = _PROMPT_TEMPLATE.format(data_block=data_block)

    raw, stderr = _run_gemini_pro(prompt)

    if not raw:
        return {"_error": f"No response from Gemini 2.5 Pro. {stderr[:200]}"}

    record_call("pro")

    # Strip markdown code fences if present
    clean = re.sub(r"```(?:json)?\s*", "", raw).strip().strip("`")

    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        # Try to extract a JSON object from the response
        m = re.search(r"\{.*\}", clean, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        return {"_error": f"Could not parse Gemini response as JSON.\nRaw response:\n{raw[:800]}"}
