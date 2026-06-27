"""AI-Powered Multi-Stage Stock Screener — backend orchestrator.

Finds "next big boom" candidates through a three-stage intelligence funnel and
evaluates how a candidate would fit the user's existing portfolio.

Funnel
------
Stage 1 — Python "Boom Score" (zero LLM cost)
    Multi-factor quant rank over a curated industry universe. Factors target
    early-stage outperformers: revenue/earnings growth & acceleration, price
    momentum + 52-week-high proximity, analyst upside, margins/quality, and
    short-squeeze / volume-surge "hype" potential. Returns the top 5 per pool.

Stage 2 — Catalyst & Sentiment ranking (Gemini 3.5 Flash)
    Feeds the top 5 (quant factors + business summary) to Flash, which ranks the
    top 2 by near-term catalyst velocity and "underappreciated boom" likelihood.

Stage 3 — Deep-dive boom/risk audit (Gemini 3.1 Pro)
    For the final 2, Pro delivers a boom-potential verdict, key risks, and a
    suggested entry/alert level.

Portfolio Fit (Gemini 3.1 Pro)
    For any candidate, explains the impact on the user's portfolio: the exposure
    it adds (sector/factor/theme), how it interacts with existing holdings
    (overlap, correlation, diversification), concentration impact, and a fit
    verdict with a suggested position size. Correlation/overlap are computed in
    Python and passed in — the LLM never does the math.

All Gemini calls go through ``data.agy_client.run_agy`` (see implementor-skills).
"""

import json
import re

import pandas as pd

from data.agy_client import PRO_MODEL, run_agy
from data.gemini_tracker import record_call
from data.fetcher import get_stock_info, get_batch_history

# ---------------------------------------------------------------------------
# Curated industry pools (2026 boom-hunting universe)
# ---------------------------------------------------------------------------

INDUSTRY_POOLS: dict[str, list[str]] = {
    "Semiconductors & AI Hardware": ["NVDA", "AMD", "AVGO", "MU", "TSM", "ASML", "AMAT", "FORM"],
    "Cloud & AI Software":          ["MSFT", "AMZN", "GOOGL", "PLTR", "DDOG", "NET", "ZS", "CRM"],
    "Biotech & Digital Health":     ["TEM", "RXRX", "UTHR", "BNTX", "LEGN", "VRTX", "AMGN", "MRNA"],
    "Fintech & Digital Finance":    ["SQ", "PYPL", "SOFI", "HOOD", "NU", "AFRM", "V", "MA"],
    "Nuclear & Energy Transition":  ["OKLO", "SMR", "CEG", "VST", "GEV", "NXT", "FSLR", "ENPH"],
    "Space & Frontier Tech":        ["RKLB", "LUNR", "ASTS", "ACHR", "JOBY", "KTOS", "AVAV", "PL"],
}

# Default Stage-1 factor weights (must sum to ~1.0; the page lets the user tune).
DEFAULT_WEIGHTS = {
    "growth":   0.30,   # revenue/earnings growth & acceleration
    "momentum": 0.30,   # price momentum + 52w-high proximity
    "analyst":  0.20,   # analyst target upside + rating
    "hype":     0.20,   # short-squeeze + volume-surge potential
}


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------

def _safe(value):
    try:
        f = float(value)
        return f if f == f else None  # NaN guard
    except (TypeError, ValueError):
        return None


def _scale(value, lo: float, hi: float) -> float:
    """Linearly map value from [lo, hi] onto [0, 100], clamped."""
    if value is None:
        return 0.0
    if hi == lo:
        return 0.0
    pct = (value - lo) / (hi - lo)
    return max(0.0, min(100.0, pct * 100.0))


# ---------------------------------------------------------------------------
# Stage 1 — Python Boom Score
# ---------------------------------------------------------------------------

def _momentum_returns(tickers: list[str]) -> dict[str, dict]:
    """Return {ticker: {"ret_3m": float|None, "ret_6m": float|None}} from a batch fetch."""
    out: dict[str, dict] = {t: {"ret_3m": None, "ret_6m": None} for t in tickers}
    try:
        hist = get_batch_history(tuple(tickers), period="6mo")
    except Exception:
        hist = pd.DataFrame()
    if hist is None or hist.empty:
        return out

    for t in tickers:
        if t not in hist.columns:
            continue
        series = hist[t].dropna()
        if len(series) < 2:
            continue
        last = series.iloc[-1]
        first = series.iloc[0]
        if first:
            out[t]["ret_6m"] = (last / first - 1.0) * 100.0
        # ~63 trading days ≈ 3 months
        if len(series) > 63:
            ref = series.iloc[-63]
            if ref:
                out[t]["ret_3m"] = (last / ref - 1.0) * 100.0
    return out


def _score_ticker(info: dict, mom: dict, weights: dict) -> dict:
    """Compute the multi-factor boom score and the raw factor readouts for one ticker."""
    price = _safe(info.get("currentPrice")) or _safe(info.get("regularMarketPrice"))
    hi_52 = _safe(info.get("fiftyTwoWeekHigh"))
    rev_growth = _safe(info.get("revenueGrowth"))            # fraction, e.g. 0.42
    earn_growth = (_safe(info.get("earningsGrowth"))
                   or _safe(info.get("earningsQuarterlyGrowth")))
    gross_m = _safe(info.get("grossMargins"))
    target = _safe(info.get("targetMeanPrice"))
    short_float = _safe(info.get("shortPercentOfFloat"))     # fraction
    avg_vol = _safe(info.get("averageVolume"))
    vol = _safe(info.get("volume")) or _safe(info.get("regularMarketVolume"))

    ret_3m = mom.get("ret_3m")
    ret_6m = mom.get("ret_6m")

    # --- Growth & quality sub-score (0-100) -------------------------------
    rev_pts = _scale((rev_growth or 0) * 100, 5, 60)          # 5%→0, 60%+→100
    earn_pts = _scale((earn_growth or 0) * 100, 0, 80)
    margin_pts = _scale((gross_m or 0) * 100, 30, 80)
    growth_score = 0.45 * rev_pts + 0.35 * earn_pts + 0.20 * margin_pts

    # --- Momentum sub-score (0-100) ---------------------------------------
    near_high = (price / hi_52 * 100.0) if (price and hi_52) else None
    near_high_pts = _scale(near_high, 60, 100)                # within 0-40% of high
    r3_pts = _scale(ret_3m, -10, 50)
    r6_pts = _scale(ret_6m, -10, 80)
    momentum_score = 0.40 * near_high_pts + 0.30 * r3_pts + 0.30 * r6_pts

    # --- Analyst upside sub-score (0-100) ---------------------------------
    upside = ((target / price - 1.0) * 100.0) if (target and price) else None
    analyst_score = _scale(upside, 0, 50)

    # --- Hype / squeeze sub-score (0-100) ---------------------------------
    short_pts = _scale((short_float or 0) * 100, 3, 25)       # 3%→0, 25%+→100
    vol_surge = (vol / avg_vol) if (vol and avg_vol) else None
    vol_pts = _scale(vol_surge, 1.0, 3.0)                     # 1x→0, 3x+→100
    hype_score = 0.60 * short_pts + 0.40 * vol_pts

    boom_score = (
        weights.get("growth", 0)   * growth_score
        + weights.get("momentum", 0) * momentum_score
        + weights.get("analyst", 0)  * analyst_score
        + weights.get("hype", 0)     * hype_score
    )

    return {
        "ticker": str(info.get("symbol", "")).upper(),
        "name": info.get("shortName") or info.get("longName") or "",
        "sector": info.get("sector", ""),
        "industry": info.get("industry", ""),
        "price": price,
        "market_cap": _safe(info.get("marketCap")),
        "boom_score": round(boom_score, 1),
        "subscores": {
            "growth": round(growth_score, 1),
            "momentum": round(momentum_score, 1),
            "analyst": round(analyst_score, 1),
            "hype": round(hype_score, 1),
        },
        "factors": {
            "rev_growth_pct": round(rev_growth * 100, 1) if rev_growth is not None else None,
            "earn_growth_pct": round(earn_growth * 100, 1) if earn_growth is not None else None,
            "gross_margin_pct": round(gross_m * 100, 1) if gross_m is not None else None,
            "ret_3m_pct": round(ret_3m, 1) if ret_3m is not None else None,
            "ret_6m_pct": round(ret_6m, 1) if ret_6m is not None else None,
            "pct_of_52w_high": round(near_high, 1) if near_high is not None else None,
            "analyst_upside_pct": round(upside, 1) if upside is not None else None,
            "short_pct_float": round(short_float * 100, 1) if short_float is not None else None,
            "vol_surge_x": round(vol_surge, 2) if vol_surge is not None else None,
            "recommendation": info.get("recommendationKey", ""),
            "business_summary": (info.get("longBusinessSummary", "") or "")[:600],
        },
    }


def compute_quant_scores(tickers: list[str], weights: dict | None = None) -> list[dict]:
    """Stage 1: score and rank a list of tickers by boom potential (descending)."""
    weights = weights or DEFAULT_WEIGHTS
    tickers = [t.upper().strip() for t in tickers if t.strip()]
    mom = _momentum_returns(tickers)

    scored: list[dict] = []
    for t in tickers:
        info = get_stock_info(t) or {}
        if not info:
            continue
        info.setdefault("symbol", t)
        scored.append(_score_ticker(info, mom.get(t, {}), weights))

    scored.sort(key=lambda r: r["boom_score"], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Gemini runners + JSON parsing
# ---------------------------------------------------------------------------

def _run_flash(prompt: str) -> str:
    try:
        output, _ = run_agy(prompt, model=None, timeout=120)
        if output:
            record_call("flash")
        return output
    except Exception:
        return ""


def _run_pro(prompt: str) -> tuple[str, str]:
    try:
        output, stderr = run_agy(prompt, model=PRO_MODEL, timeout=180)
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
# Stage 2 — Flash catalyst ranking
# ---------------------------------------------------------------------------

def _candidate_brief(row: dict) -> str:
    f = row["factors"]
    s = row["subscores"]
    return (
        f"[{row['ticker']}] {row['name']} — {row['sector']} / {row['industry']}\n"
        f"  BoomScore {row['boom_score']} (growth {s['growth']}, momentum {s['momentum']}, "
        f"analyst {s['analyst']}, hype {s['hype']})\n"
        f"  RevGrowth={f['rev_growth_pct']}% EarnGrowth={f['earn_growth_pct']}% "
        f"GrossMargin={f['gross_margin_pct']}%\n"
        f"  Ret3m={f['ret_3m_pct']}% Ret6m={f['ret_6m_pct']}% %of52wHigh={f['pct_of_52w_high']}% "
        f"AnalystUpside={f['analyst_upside_pct']}% (rating: {f['recommendation']})\n"
        f"  ShortFloat={f['short_pct_float']}% VolSurge={f['vol_surge_x']}x\n"
        f"  Business: {f['business_summary']}"
    )


_FLASH_PROMPT = """You are an equity catalyst analyst hunting for the "next big boom" stock. \
Below are the top quantitatively-ranked candidates in the {industry} sector. \
Identify the 2 with the strongest NEAR-TERM catalyst velocity and the highest odds of being \
an underappreciated boom (growth/story the market hasn't fully priced), NOT already over-hyped.

CANDIDATES:
{candidates_block}

Respond ONLY with valid JSON:
{{
  "ranked": [
    {{
      "ticker": "<symbol>",
      "rank": 1,
      "hype_score": <integer 1-10>,
      "catalysts": ["<near-term catalyst>", "<...>"],
      "boom_thesis": "<1-2 sentence reason this could be a breakout>",
      "risk_flag": "<main risk or 'over-hyped' if crowded>"
    }}
  ]
}}

Rules:
- ranked: exactly 2 items (the 2 best), rank 1 = highest conviction.
- hype_score: 1=ignored by market, 10=euphoric/crowded. Prefer 4-7 (room to run).
- Cite the actual numbers from the data above.
- Do NOT wrap JSON in markdown code fences.
"""


def rank_catalysts_flash(industry: str, top_candidates: list[dict]) -> dict:
    """Stage 2: Flash ranks the top 2 catalyst plays from the Stage-1 leaders."""
    if not top_candidates:
        return {"_error": "No candidates to rank."}
    block = "\n\n".join(_candidate_brief(r) for r in top_candidates[:5])
    prompt = _FLASH_PROMPT.format(industry=industry, candidates_block=block)
    raw = _run_flash(prompt)
    parsed = _parse_json(raw)
    if not parsed:
        return {"_error": f"Flash ranking failed.\n{raw[:400]}"}
    return parsed


# ---------------------------------------------------------------------------
# Stage 3 — Pro deep-dive
# ---------------------------------------------------------------------------

_PRO_PROMPT = """You are a senior analyst delivering a final boom/risk verdict on 2 finalist stocks \
in the {industry} sector. Use the quantitative profiles below.

FINALISTS:
{finalists_block}

Respond ONLY with valid JSON:
{{
  "verdicts": [
    {{
      "ticker": "<symbol>",
      "boom_potential": "<explosive|high|moderate|limited>",
      "conviction": "<high|medium|low>",
      "thesis": "<2-3 sentence boom thesis grounded in the data>",
      "key_catalysts": ["<catalyst>", "<...>"],
      "key_risks": ["<risk>", "<...>"],
      "suggested_entry": "<price or condition to start a position>",
      "alert_price": <number or null>
    }}
  ],
  "head_to_head": "<1-2 sentences: which finalist is the better boom bet right now and why>"
}}

Rules:
- verdicts: one per finalist.
- Be specific — cite tickers and the numbers provided.
- Do NOT wrap JSON in markdown code fences.
"""


def deep_dive_pro(industry: str, finalists: list[dict]) -> dict:
    """Stage 3: Pro produces the final boom/risk verdict for the 2 finalists."""
    if not finalists:
        return {"_error": "No finalists to analyze."}
    block = "\n\n".join(_candidate_brief(r) for r in finalists[:2])
    prompt = _PRO_PROMPT.format(industry=industry, finalists_block=block)
    raw, stderr = _run_pro(prompt)
    parsed = _parse_json(raw)
    if not parsed:
        return {"_error": f"Pro deep-dive failed. {stderr[:200]}\n{raw[:400]}"}
    return parsed


# ---------------------------------------------------------------------------
# Portfolio-fit agent
# ---------------------------------------------------------------------------

def _portfolio_correlation(candidate: str, holdings: list[str]) -> dict:
    """Compute the candidate's 6-month return correlation vs each holding + average."""
    syms = [candidate] + [h for h in holdings if h and h != candidate]
    result = {"avg_correlation": None, "per_holding": {}}
    if len(syms) < 2:
        return result
    try:
        hist = get_batch_history(tuple(syms), period="6mo")
    except Exception:
        hist = pd.DataFrame()
    if hist is None or hist.empty or candidate not in hist.columns:
        return result

    returns = hist.pct_change().dropna(how="all")
    if candidate not in returns.columns:
        return result

    corrs = []
    for h in holdings:
        if h == candidate or h not in returns.columns:
            continue
        c = returns[candidate].corr(returns[h])
        if c == c:  # not NaN
            result["per_holding"][h] = round(float(c), 2)
            corrs.append(c)
    if corrs:
        result["avg_correlation"] = round(float(sum(corrs) / len(corrs)), 2)
    return result


def _holdings_profile(holdings: list[str]) -> list[dict]:
    """Fetch lightweight sector/industry profile for each current holding."""
    profile = []
    for h in holdings:
        info = get_stock_info(h) or {}
        profile.append({
            "ticker": h,
            "sector": info.get("sector", "Unknown"),
            "industry": info.get("industry", "Unknown"),
        })
    return profile


_FIT_PROMPT = """You are a portfolio construction advisor. The investor is considering adding \
{candidate} to their portfolio. Explain the IMPACT this addition would have: the new exposure it \
introduces, how it interacts with existing holdings, and the effect on concentration and \
diversification. Use the computed data below — do NOT recompute correlations yourself.

=== CANDIDATE ===
{candidate_block}

=== CURRENT HOLDINGS (sector profile) ===
{holdings_block}

=== COMPUTED INTERACTION DATA ===
Average 6-month return correlation of {candidate} vs the portfolio: {avg_corr}
Per-holding correlation: {per_holding}
=== END DATA ===

Respond ONLY with valid JSON:
{{
  "fit_verdict": "<strong_diversifier|good_fit|redundant|concentration_risk|poor_fit>",
  "summary": "<2-3 sentence read on what adding this does to the portfolio>",
  "exposure_added": {{
    "sectors": ["<sector>"],
    "themes": ["<theme/factor, e.g. 'AI infrastructure', 'rate-sensitive growth'>"],
    "geography": "<brief note or 'US large-cap' etc.>"
  }},
  "interaction": [
    {{
      "ticker": "<existing holding>",
      "relationship": "<overlapping|complementary|hedging|correlated>",
      "detail": "<1 sentence: how the candidate interacts with this holding>"
    }}
  ],
  "concentration_impact": "<1-2 sentences on sector/factor concentration effect>",
  "diversification_benefit": "<low|medium|high> — <short reason citing correlation>",
  "suggested_position_size": "<e.g. '2-4% starter' with brief reasoning>",
  "risks": ["<portfolio-level risk of adding this>", "<...>"]
}}

Rules:
- interaction: cover the 3-5 most-related existing holdings (overlap or correlation).
- Cite the actual correlation numbers and sectors provided above.
- If the portfolio is empty, say so and analyze on a standalone basis.
- Do NOT wrap JSON in markdown code fences.
"""


def analyze_portfolio_fit(candidate_row: dict, holdings: list[str]) -> dict:
    """Pro agent: how does adding this candidate impact the portfolio?

    Args:
        candidate_row: a Stage-1 score row (from compute_quant_scores) for the candidate.
        holdings:      list of the user's current holding tickers.

    Returns:
        Parsed JSON dict (see schema), or {"_error": str} on failure.
    """
    candidate = candidate_row.get("ticker", "").upper()
    holdings = [h.upper().strip() for h in holdings if h and h.strip() and h.upper() != candidate]

    corr = _portfolio_correlation(candidate, holdings)
    profile = _holdings_profile(holdings)

    f = candidate_row.get("factors", {})
    candidate_block = (
        f"{candidate} — {candidate_row.get('name', '')}\n"
        f"  Sector: {candidate_row.get('sector', '')} / {candidate_row.get('industry', '')}\n"
        f"  Market cap: {candidate_row.get('market_cap')}\n"
        f"  RevGrowth={f.get('rev_growth_pct')}% EarnGrowth={f.get('earn_growth_pct')}% "
        f"GrossMargin={f.get('gross_margin_pct')}%\n"
        f"  Business: {f.get('business_summary', '')}"
    )
    holdings_block = (
        "\n".join(f"  {p['ticker']}: {p['sector']} / {p['industry']}" for p in profile)
        if profile else "  (no current holdings)"
    )
    per_holding = ", ".join(f"{k}={v}" for k, v in corr["per_holding"].items()) or "n/a"

    prompt = _FIT_PROMPT.format(
        candidate=candidate,
        candidate_block=candidate_block,
        holdings_block=holdings_block,
        avg_corr=corr["avg_correlation"] if corr["avg_correlation"] is not None else "n/a",
        per_holding=per_holding,
    )
    raw, stderr = _run_pro(prompt)
    parsed = _parse_json(raw)
    if not parsed:
        return {"_error": f"Portfolio-fit analysis failed. {stderr[:200]}\n{raw[:400]}"}
    parsed["_correlation"] = corr  # attach computed numbers for the UI
    return parsed
