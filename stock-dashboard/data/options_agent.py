import json
import re
import subprocess
from datetime import datetime

import numpy as np
import pandas as pd

from data.gemini_tracker import record_call


# ── Gemini Pro runner ─────────────────────────────────────────────────────────

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
            timeout=120,
        )
        output = result.stdout.strip()
        if output:
            record_call("pro")
        return output, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return "", "Timed out after 120s"
    except Exception as exc:
        return "", str(exc)


# ── Metric helpers ────────────────────────────────────────────────────────────

def _put_call_ratios(calls_df: pd.DataFrame, puts_df: pd.DataFrame) -> dict:
    """Compute put/call ratios by open interest and volume across the full chain."""
    def safe_sum(df: pd.DataFrame, col: str) -> float:
        if col not in df.columns:
            return 0.0
        return float(pd.to_numeric(df[col], errors="coerce").fillna(0).sum())

    call_oi  = safe_sum(calls_df, "openInterest")
    put_oi   = safe_sum(puts_df,  "openInterest")
    call_vol = safe_sum(calls_df, "volume")
    put_vol  = safe_sum(puts_df,  "volume")

    return {
        "pcr_oi":        round(put_oi  / call_oi,  3) if call_oi  > 0 else None,
        "pcr_vol":       round(put_vol / call_vol, 3) if call_vol > 0 else None,
        "total_call_oi":  int(call_oi),
        "total_put_oi":   int(put_oi),
        "total_call_vol": int(call_vol),
        "total_put_vol":  int(put_vol),
    }


def _max_pain(calls_df: pd.DataFrame, puts_df: pd.DataFrame) -> float | None:
    """
    Find the max pain strike: where total dollar loss to option buyers (= gain to sellers)
    is maximized, i.e. where the underlying closing there would expire the most premium worthless.
    For each candidate strike S, sum (S - K) * OI for calls with K < S, and
    sum (K - S) * OI for puts with K > S.
    """
    try:
        calls = calls_df[["strike", "openInterest"]].copy()
        puts  = puts_df[["strike",  "openInterest"]].copy()
        calls["openInterest"] = pd.to_numeric(calls["openInterest"], errors="coerce").fillna(0)
        puts["openInterest"]  = pd.to_numeric(puts["openInterest"],  errors="coerce").fillna(0)

        all_strikes = sorted(set(calls["strike"].tolist() + puts["strike"].tolist()))
        if not all_strikes:
            return None

        min_loss = float("inf")
        pain_strike = all_strikes[0]

        for s in all_strikes:
            call_loss = ((s - calls["strike"]).clip(lower=0) * calls["openInterest"]).sum()
            put_loss  = ((puts["strike"] - s).clip(lower=0)  * puts["openInterest"]).sum()
            total = call_loss + put_loss
            if total < min_loss:
                min_loss = total
                pain_strike = s

        return float(pain_strike)
    except Exception:
        return None


def _iv_skew(calls_df: pd.DataFrame, puts_df: pd.DataFrame, spot: float) -> float | None:
    """
    25-delta proxy skew: avg IV of OTM puts minus avg IV of OTM calls.
    Positive value = put skew (bearish fear premium).
    Negative value = call skew (unusual bullish demand).
    """
    try:
        calls = calls_df[["strike", "impliedVolatility"]].copy()
        puts  = puts_df[["strike",  "impliedVolatility"]].copy()

        otm_calls = calls[calls["strike"] > spot]["impliedVolatility"]
        otm_puts  = puts[puts["strike"]  < spot]["impliedVolatility"]

        otm_calls = pd.to_numeric(otm_calls, errors="coerce").dropna()
        otm_puts  = pd.to_numeric(otm_puts,  errors="coerce").dropna()

        if otm_calls.empty or otm_puts.empty:
            return None

        # Use the 25% most OTM on each side for cleaner skew signal
        n_calls = max(1, len(otm_calls) // 4)
        n_puts  = max(1, len(otm_puts)  // 4)

        avg_call_iv = otm_calls.nlargest(n_calls).mean()
        avg_put_iv  = otm_puts.nsmallest(n_puts).mean()   # lowest strikes = most OTM

        return round(float(avg_put_iv - avg_call_iv), 4)
    except Exception:
        return None


def _net_gamma_exposure(display_df: pd.DataFrame, spot: float) -> float | None:
    """
    Approximate net dealer gamma exposure (GEX) from the displayed chain.
    Dealers are short calls (negative delta) and short puts (positive delta).
    GEX = sum over calls(gamma * OI * 100 * spot^2 / 100)
          - sum over puts(gamma * OI * 100 * spot^2 / 100)
    Positive GEX = dealers long gamma → they sell rallies and buy dips (price-pinning).
    Negative GEX = dealers short gamma → they buy rallies and sell dips (vol amplification).
    The display_df has ITM column and Delta; ITM calls have delta > 0, ITM puts delta < 0.
    We distinguish calls vs puts by delta sign (calls > 0, puts <= 0).
    """
    try:
        df = display_df.copy()
        if "Gamma" not in df.columns or "Open Int." not in df.columns:
            return None

        df["Gamma"]     = pd.to_numeric(df["Gamma"],     errors="coerce").fillna(0)
        df["Open Int."] = pd.to_numeric(df["Open Int."], errors="coerce").fillna(0)
        df["Delta"]     = pd.to_numeric(df.get("Delta", 0), errors="coerce").fillna(0)

        factor = spot * spot / 100  # gamma * OI * 100 * S^2 / 100 → in dollar-delta terms

        calls = df[df["Delta"] > 0]
        puts  = df[df["Delta"] <= 0]

        call_gex = (calls["Gamma"] * calls["Open Int."] * 100 * factor).sum()
        put_gex  = (puts["Gamma"]  * puts["Open Int."]  * 100 * factor).sum()

        return round(float(call_gex - put_gex), 2)
    except Exception:
        return None


def _high_oi_strikes(calls_df: pd.DataFrame, puts_df: pd.DataFrame, top_n: int = 3) -> dict:
    """Return top N open-interest strikes for calls and puts — potential support/resistance."""
    def top_strikes(df: pd.DataFrame) -> list[dict]:
        if df.empty or "openInterest" not in df.columns:
            return []
        d = df[["strike", "openInterest"]].copy()
        d["openInterest"] = pd.to_numeric(d["openInterest"], errors="coerce").fillna(0)
        d = d[d["openInterest"] > 0].nlargest(top_n, "openInterest")
        return [{"strike": float(r["strike"]), "oi": int(r["openInterest"])} for _, r in d.iterrows()]

    return {
        "top_call_oi_strikes": top_strikes(calls_df),
        "top_put_oi_strikes":  top_strikes(puts_df),
    }


# ── Prompt builder ────────────────────────────────────────────────────────────

_PROMPT = """\
You are an expert options market analyst and derivatives strategist.
Your task is to analyze the option chain data below and produce a thorough, \
reasoned assessment of what the options market is signaling about the underlying stock's \
near-term direction and risk profile.

=== CONTEXT ===
Ticker:         {ticker}
Current Price:  ${price:.2f}
Expiration:     {expiry} ({dte} days to expiry)
Contract Type:  {opt_type_label}
Risk-Free Rate: 4.5%

=== COMPUTED METRICS (full chain, not just displayed rows) ===
Put/Call OI Ratio:     {pcr_oi}   (total call OI: {call_oi:,} | total put OI: {put_oi:,})
Put/Call Volume Ratio: {pcr_vol}  (total call vol: {call_vol:,} | total put vol: {put_vol:,})
Max Pain Strike:       ${max_pain}  (current price is ${pain_dist:+.2f} from max pain)
IV Skew (put - call):  {iv_skew}  (OTM put avg IV minus OTM call avg IV; positive = fear premium)
Net Dealer GEX:        {net_gex}  (positive = dealers long gamma → pinning; negative = vol amplification)

Top Call OI Strikes (potential resistance / call walls):
{top_call_oi}

Top Put OI Strikes (potential support / put floors):
{top_put_oi}

=== DISPLAYED OPTION CHAIN (nearest {n_contracts} ITM + {n_contracts} OTM {opt_type_label}) ===
{chain_table}

=== YOUR ANALYSIS TASK ===
Analyze EVERY metric above in depth. For each one, explain:
  1. What the raw number means mechanically
  2. What market participants are likely doing to produce this reading
  3. What it implies for the underlying stock's price direction or volatility

Then synthesize everything into a directional view with confidence and key caveats.

Rules:
- Be specific: reference actual numbers from the data (strikes, ratios, IV values)
- Explain the "why" behind every claim — no unsupported assertions
- Flag if any metrics conflict with each other and explain how to weight them
- Keep each analysis field focused but thorough (3-6 sentences minimum per field)

Respond ONLY with a single JSON object (no markdown fences, no preamble):
{{
  "directional_bias": "<bullish|bearish|neutral>",
  "bias_strength": "<strong|moderate|weak>",
  "confidence": "<high|medium|low>",
  "iv_analysis": "<thorough analysis of implied volatility level and skew>",
  "pcr_analysis": "<analysis of put/call ratios by OI and volume and what positioning implies>",
  "max_pain_analysis": "<what the max pain level means, distance from spot, what happens as expiry nears>",
  "gamma_exposure_analysis": "<dealer gamma position, direction of hedging flows, pinning or vol-amplification risk>",
  "key_levels": "<specific strikes acting as support or resistance based on OI concentration, and why>",
  "unusual_activity": "<any strikes where volume significantly exceeds open interest, suggesting fresh positioning — or note if none>",
  "risk_factors": "<3-4 specific scenarios that would invalidate the directional bias>",
  "summary": "<3-5 sentence narrative tying all signals together into one clear conclusion about what the options market is pricing in>"
}}
"""


def _build_prompt(
    ticker: str,
    price: float,
    expiry: str,
    opt_type: str,
    calls_df: pd.DataFrame,
    puts_df: pd.DataFrame,
    display_df: pd.DataFrame,
    metrics: dict,
) -> str:
    dte = (datetime.strptime(expiry, "%Y-%m-%d") - datetime.today()).days
    opt_type_label = "Calls" if opt_type == "call" else "Puts"

    pcr_oi  = f"{metrics['pcr_oi']:.3f}"  if metrics.get("pcr_oi")  is not None else "N/A"
    pcr_vol = f"{metrics['pcr_vol']:.3f}" if metrics.get("pcr_vol") is not None else "N/A"
    max_pain_val = metrics.get("max_pain")
    max_pain_str = f"{max_pain_val:.2f}" if max_pain_val is not None else "N/A"
    pain_dist    = (max_pain_val - price) if max_pain_val is not None else 0.0

    iv_skew_val = metrics.get("iv_skew")
    iv_skew_str = f"{iv_skew_val:+.4f} ({iv_skew_val*100:+.2f}%)" if iv_skew_val is not None else "N/A"

    net_gex_val = metrics.get("net_gex")
    net_gex_str = f"${net_gex_val:,.0f}" if net_gex_val is not None else "N/A"

    def fmt_oi_strikes(lst: list[dict]) -> str:
        if not lst:
            return "  (no data)"
        return "\n".join(f"  Strike ${s['strike']:.2f} — OI {s['oi']:,}" for s in lst)

    top_call_oi = fmt_oi_strikes(metrics.get("top_call_oi_strikes", []))
    top_put_oi  = fmt_oi_strikes(metrics.get("top_put_oi_strikes",  []))

    chain_table = display_df.to_string(index=False)
    n_contracts = max(1, len(display_df) // 2)

    return _PROMPT.format(
        ticker=ticker.upper(),
        price=price,
        expiry=expiry,
        dte=max(dte, 0),
        opt_type_label=opt_type_label,
        pcr_oi=pcr_oi,
        call_oi=metrics.get("total_call_oi", 0),
        put_oi=metrics.get("total_put_oi", 0),
        pcr_vol=pcr_vol,
        call_vol=metrics.get("total_call_vol", 0),
        put_vol=metrics.get("total_put_vol", 0),
        max_pain=max_pain_str,
        pain_dist=pain_dist,
        iv_skew=iv_skew_str,
        net_gex=net_gex_str,
        top_call_oi=top_call_oi,
        top_put_oi=top_put_oi,
        chain_table=chain_table,
        n_contracts=n_contracts,
    )


# ── JSON parser ───────────────────────────────────────────────────────────────

_REQUIRED_FIELDS = [
    "directional_bias", "bias_strength", "confidence",
    "iv_analysis", "pcr_analysis", "max_pain_analysis",
    "gamma_exposure_analysis", "key_levels", "unusual_activity",
    "risk_factors", "summary",
]


def _parse_response(raw: str) -> dict | None:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None

    # Normalize bias/strength/confidence to valid values
    bias = str(data.get("directional_bias", "neutral")).lower()
    if bias not in ("bullish", "bearish", "neutral"):
        bias = "neutral"
    data["directional_bias"] = bias

    strength = str(data.get("bias_strength", "moderate")).lower()
    if strength not in ("strong", "moderate", "weak"):
        strength = "moderate"
    data["bias_strength"] = strength

    conf = str(data.get("confidence", "medium")).lower()
    if conf not in ("high", "medium", "low"):
        conf = "medium"
    data["confidence"] = conf

    # Ensure all required text fields exist
    for field in _REQUIRED_FIELDS:
        if field not in data:
            data[field] = ""

    return data


# ── Public API ────────────────────────────────────────────────────────────────

def run_options_analysis(
    ticker: str,
    price: float,
    expiry: str,
    opt_type: str,
    calls_df: pd.DataFrame,
    puts_df: pd.DataFrame,
    display_df: pd.DataFrame,
) -> dict | None:
    """
    Compute chain metrics, build a prompt, call Gemini 2.5 Pro, and return
    a structured analysis dict. Returns None if Gemini fails or returns garbage.

    The returned dict has all fields from _REQUIRED_FIELDS plus a 'metrics' key
    with the raw computed numbers for display in the UI.
    """
    metrics = {
        **_put_call_ratios(calls_df, puts_df),
        "max_pain": _max_pain(calls_df, puts_df),
        "iv_skew":  _iv_skew(calls_df, puts_df, price),
        "net_gex":  _net_gamma_exposure(display_df, price),
        **_high_oi_strikes(calls_df, puts_df),
    }

    prompt = _build_prompt(ticker, price, expiry, opt_type, calls_df, puts_df, display_df, metrics)
    raw, stderr = _run_gemini_pro(prompt)
    if not raw:
        return {"_error": stderr or "Gemini returned empty output.", "metrics": metrics}

    result = _parse_response(raw)
    if result is None:
        return {"_error": f"Could not parse Gemini response.\n\nRaw output:\n{raw[:500]}", "metrics": metrics}

    result["metrics"] = metrics
    return result
