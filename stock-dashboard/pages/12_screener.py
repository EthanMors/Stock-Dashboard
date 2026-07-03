# pages/12_screener.py
"""AI-Powered Multi-Stage Stock Screener.

Hunts for "next big boom" candidates across curated industry pools using a
three-stage funnel (Python quant → Gemini Flash catalysts → Gemini Pro deep-dive)
and evaluates how each candidate would fit the user's existing portfolio.
"""

import pandas as pd
import streamlit as st

from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import inject_global_css, page_header, render_sidebar_nav, section_header
from data import screener_agent, screener_cache
from data.fetcher import get_batch_history
from data.screener_agent import (
    INDUSTRY_POOLS,
    REDDIT_TRENDING_LABEL,
    DEFAULT_WEIGHTS,
    compute_quant_scores,
    get_reddit_trending_pool,
    rank_catalysts_flash,
    deep_dive_pro,
    analyze_portfolio_fit,
)

st.set_page_config(page_title="AI Stock Screener", layout="wide")

render_gemini_usage_bar()
inject_global_css()
render_sidebar_nav()

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
_DEFAULTS = {
    "scr_scores": None,       # Stage 1 ranked list
    "scr_industry": None,     # industry label that produced scr_scores
    "scr_flash": None,        # Stage 2 result
    "scr_pro": None,          # Stage 3 result
    "scr_fit": {},            # {ticker: portfolio-fit result}
    "scr_run_id": None,       # screener_cache.save_run() id for the current scr_scores run
    "scr_flash_saved": False, # whether Stage-2 finalists were already saved to screener.db
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_webull_holdings() -> list[str]:
    """Best-effort fetch of current holding tickers from Webull (empty on failure)."""
    try:
        from data.webull_positions import is_configured, get_env_account_ids, get_positions
        if not is_configured():
            return []
        ids = get_env_account_ids()
        if not ids:
            return []
        positions = get_positions(ids[0])
        if not isinstance(positions, list):
            return []
        fields = ["symbol", "ticker", "tickerSymbol", "stockSymbol", "sym"]
        out = []
        for p in positions:
            for fld in fields:
                v = p.get(fld)
                if v:
                    out.append(str(v).upper())
                    break
        return sorted(set(out))
    except Exception:
        return []


def _normalize_weights(g: int, m: int, a: int, h: int, sm: int) -> dict:
    total = g + m + a + h + sm
    if total == 0:
        return DEFAULT_WEIGHTS
    return {
        "growth": g / total,
        "momentum": m / total,
        "analyst": a / total,
        "hype": h / total,
        "smart_money": sm / total,
    }


_IMPACT_COLOR = {
    "explosive": "#9b59b6", "high": "#2ecc71", "moderate": "#f39c12", "limited": "#e74c3c",
}
_FIT_COLOR = {
    "strong_diversifier": "#2ecc71", "good_fit": "#27ae60", "redundant": "#f39c12",
    "concentration_risk": "#e67e22", "poor_fit": "#e74c3c",
}


# ---------------------------------------------------------------------------
# Sidebar controls
# ---------------------------------------------------------------------------

def _render_sidebar() -> dict:
    with st.sidebar:
        st.markdown("### 🔍 Screener Controls")
        industry_options = list(INDUSTRY_POOLS.keys()) + [REDDIT_TRENDING_LABEL]
        industry = st.radio("Industry pool", industry_options, key="scr_pool")

        custom_raw = st.text_input(
            "Add custom tickers (comma-separated)", key="scr_custom",
            help="Appended to the selected industry pool.",
        )

        st.markdown("#### Boom-score weights")
        st.caption("How much each factor drives the Stage-1 rank.")
        w_growth = st.slider("Growth & quality", 0, 100, 25, key="w_growth")
        w_mom = st.slider("Momentum", 0, 100, 25, key="w_mom")
        w_analyst = st.slider("Analyst upside", 0, 100, 15, key="w_analyst")
        w_hype = st.slider("Squeeze / hype", 0, 100, 15, key="w_hype")
        w_smart = st.slider("Smart money (13F)", 0, 100, 20, key="w_smart")

        run = st.button("🚀 Run Screen", use_container_width=True, type="primary")

        st.markdown("---")
        st.caption(
            "Stage 1 = free Python quant. Stage 2 uses 1 Gemini **Flash** call, "
            "Stage 3 + each Portfolio-Fit use 1 Gemini **Pro** call (50/day)."
        )

    custom = [t.strip().upper() for t in custom_raw.split(",") if t.strip()]
    weights = _normalize_weights(w_growth, w_mom, w_analyst, w_hype, w_smart)
    return {"industry": industry, "custom": custom, "weights": weights, "run": run}


# ---------------------------------------------------------------------------
# Tab 1 — Leaderboard
# ---------------------------------------------------------------------------

def _render_leaderboard(scores: list[dict]) -> None:
    section_header("Stage 1 · Quantitative Boom Leaderboard")
    st.caption(
        "Pure-Python multi-factor rank. The top 5 (highlighted) advance to the Gemini stages "
        "and get an options put/call-ratio readout."
    )

    rows = []
    for i, r in enumerate(scores):
        f = r["factors"]
        pos = r.get("positioning") or {}
        rows.append({
            "Rank": i + 1,
            "Ticker": r["ticker"],
            "Boom": r["boom_score"],
            "Growth": r["subscores"]["growth"],
            "Momentum": r["subscores"]["momentum"],
            "Analyst": r["subscores"]["analyst"],
            "Hype": r["subscores"]["hype"],
            "SmartMoney": r["subscores"]["smart_money"],
            "RevGr%": f["rev_growth_pct"],
            "Ret6m(rel)%": f["ret_6m_rel_pct"],
            "%52wHi": f["pct_of_52w_high"],
            "Upside%": f["analyst_upside_pct"],
            "Short%": f["short_pct_float"],
            "Put/Call": pos.get("put_call_ratio"),
        })
    df = pd.DataFrame(rows)

    def _hl_top5(row):
        return ["background-color: #1d3b2a" if row["Rank"] <= 5 else "" for _ in row]

    styled = df.style.apply(_hl_top5, axis=1).format(precision=1, na_rep="—")
    st.dataframe(styled, use_container_width=True, hide_index=True)

    st.markdown("**Top 5 advancing:** " + ", ".join(r["ticker"] for r in scores[:5]))


# ---------------------------------------------------------------------------
# Tab 2 — Catalyst (Flash)
# ---------------------------------------------------------------------------

def _render_catalyst(industry: str, scores: list[dict]) -> None:
    section_header("Stage 2 · Catalyst & Sentiment Ranking (Gemini Flash)")
    if st.button("🤖 Rank catalysts with Flash", key="scr_flash_btn"):
        with st.spinner("Gemini Flash ranking top catalyst plays…"):
            st.session_state.scr_flash = rank_catalysts_flash(industry, scores[:5])
        st.session_state.scr_pro = None  # finalists may have changed
        st.session_state.scr_flash_saved = False

    result = st.session_state.scr_flash
    if result is None:
        st.info("Run the screen, then rank catalysts to surface the 2 strongest near-term plays.")
        return
    if "_error" in result:
        st.error(result["_error"])
        return

    # Persist Flash finalists to the picks tracker (once per Flash result).
    if not st.session_state.scr_flash_saved and st.session_state.scr_run_id:
        by_ticker = {r["ticker"]: r for r in scores}
        for item in result.get("ranked", []):
            ticker = item.get("ticker", "")
            row = by_ticker.get(ticker)
            if row is None:
                continue
            screener_cache.save_pick(
                run_id=st.session_state.scr_run_id,
                ticker=ticker,
                industry=industry,
                boom_score=row.get("boom_score"),
                stage_reached="flash",
                boom_potential=None,
                conviction=None,
                entry_price=row.get("price"),
                alert_price=None,
            )
        st.session_state.scr_flash_saved = True

    for item in result.get("ranked", []):
        ticker = item.get("ticker", "?")
        rank = item.get("rank", "?")
        hype = item.get("hype_score", "?")
        thesis = item.get("boom_thesis", "")
        risk = item.get("risk_flag", "")
        cats = item.get("catalysts", [])
        cat_html = "".join(f"<li style='margin-bottom:2px'>{c}</li>" for c in cats)
        st.markdown(
            f'<div style="background:#161b27;border:1px solid #1e2740;border-left:4px solid #3aa6ff;'
            f'border-radius:8px;padding:14px 18px;margin-bottom:12px">'
            f'<span style="color:#e8eaf0;font-size:1.15rem;font-weight:700">#{rank} · {ticker}</span>'
            f'&nbsp;<span style="background:#3aa6ff22;color:#3aa6ff;padding:2px 9px;border-radius:10px;'
            f'font-size:0.78rem">Hype {hype}/10</span>'
            f'<p style="color:#cdd3e0;font-size:0.92rem;margin:8px 0 6px">{thesis}</p>'
            f'<span style="color:#7a85a0;font-size:0.78rem">CATALYSTS</span>'
            f'<ul style="color:#cdd3e0;font-size:0.85rem;margin:4px 0 6px 18px">{cat_html}</ul>'
            + (f'<span style="color:#e0a02f;font-size:0.82rem">⚠ {risk}</span>' if risk else "")
            + '</div>',
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Tab 3 — Deep-Dive (Pro)
# ---------------------------------------------------------------------------

def _finalists_from_flash(scores: list[dict]) -> list[dict]:
    """Map the Flash-ranked tickers back to their Stage-1 score rows."""
    flash = st.session_state.scr_flash or {}
    ranked = flash.get("ranked", []) if isinstance(flash, dict) else []
    by_ticker = {r["ticker"]: r for r in scores}
    finalists = [by_ticker[i["ticker"]] for i in ranked if i.get("ticker") in by_ticker]
    return finalists or scores[:2]


def _render_deep_dive(industry: str, scores: list[dict]) -> None:
    section_header("Stage 3 · Institutional Deep-Dive (Gemini Pro)")
    finalists = _finalists_from_flash(scores)
    st.caption("Finalists: " + ", ".join(r["ticker"] for r in finalists))

    if st.button("🏦 Run Pro deep-dive", key="scr_pro_btn"):
        with st.spinner("Gemini 3.1 Pro delivering boom/risk verdicts…"):
            st.session_state.scr_pro = deep_dive_pro(industry, finalists)

    result = st.session_state.scr_pro
    if result is None:
        st.info("Rank catalysts first, then run the Pro deep-dive on the 2 finalists.")
        return
    if "_error" in result:
        st.error(result["_error"])
        return

    # Persist Pro verdicts to the picks tracker.
    if st.session_state.scr_run_id:
        by_ticker = {r["ticker"]: r for r in scores}
        for v in result.get("verdicts", []):
            ticker = v.get("ticker", "")
            row = by_ticker.get(ticker)
            if row is None:
                continue
            screener_cache.save_pick(
                run_id=st.session_state.scr_run_id,
                ticker=ticker,
                industry=industry,
                boom_score=row.get("boom_score"),
                stage_reached="pro",
                boom_potential=v.get("boom_potential"),
                conviction=v.get("conviction"),
                entry_price=row.get("price"),
                alert_price=v.get("alert_price"),
            )

    for v in result.get("verdicts", []):
        ticker = v.get("ticker", "?")
        boom = v.get("boom_potential", "moderate")
        conv = v.get("conviction", "medium")
        thesis = v.get("thesis", "")
        cats = v.get("key_catalysts", [])
        risks = v.get("key_risks", [])
        entry = v.get("suggested_entry", "")
        alert = v.get("alert_price")
        bc = _IMPACT_COLOR.get(boom, "#95a5a6")
        cat_html = "".join(f"<li>{c}</li>" for c in cats)
        risk_html = "".join(f"<li>{r}</li>" for r in risks)
        st.markdown(
            f'<div style="background:#161b27;border:1px solid #1e2740;border-left:4px solid {bc};'
            f'border-radius:8px;padding:16px 20px;margin-bottom:14px">'
            f'<span style="color:#e8eaf0;font-size:1.25rem;font-weight:700">{ticker}</span>'
            f'&nbsp;<span style="background:{bc}22;color:{bc};padding:2px 10px;border-radius:10px;'
            f'font-size:0.8rem;font-weight:700">{boom.upper()} BOOM POTENTIAL</span>'
            f'&nbsp;<span style="color:#7a85a0;font-size:0.78rem">conviction: {conv}</span>'
            f'<p style="color:#cdd3e0;font-size:0.93rem;margin:10px 0">{thesis}</p>'
            f'<div style="display:flex;gap:24px;flex-wrap:wrap">'
            f'<div><span style="color:#2ecc71;font-size:0.78rem">CATALYSTS</span>'
            f'<ul style="color:#cdd3e0;font-size:0.84rem;margin:4px 0 0 18px">{cat_html}</ul></div>'
            f'<div><span style="color:#e74c3c;font-size:0.78rem">RISKS</span>'
            f'<ul style="color:#cdd3e0;font-size:0.84rem;margin:4px 0 0 18px">{risk_html}</ul></div>'
            f'</div>'
            + (f'<p style="color:#9aa3b8;font-size:0.84rem;margin-top:10px">📍 Entry: {entry}'
               + (f' · Alert ${alert}' if alert else "") + '</p>' if entry else "")
            + '</div>',
            unsafe_allow_html=True,
        )

    h2h = result.get("head_to_head", "")
    if h2h:
        st.success(f"**Head-to-head:** {h2h}")


# ---------------------------------------------------------------------------
# Tab 4 — Portfolio Fit
# ---------------------------------------------------------------------------

def _render_fit(scores: list[dict]) -> None:
    section_header("Portfolio Fit · Impact, Exposure & Interaction (Gemini Pro)")
    st.caption(
        "How would adding a candidate affect YOUR portfolio — new exposure, "
        "overlap and correlation with current holdings, and concentration impact."
    )

    tickers = [r["ticker"] for r in scores]
    sel_col, load_col = st.columns([3, 2])
    with sel_col:
        candidate = st.selectbox("Candidate to evaluate", tickers, key="scr_fit_ticker")
    with load_col:
        st.markdown("")
        st.markdown("")
        if st.button("📥 Load my Webull holdings", key="scr_load_holdings", use_container_width=True):
            loaded = _load_webull_holdings()
            if loaded:
                st.session_state.scr_holdings_text = ", ".join(loaded)
                st.toast(f"Loaded {len(loaded)} holdings.")
            else:
                st.toast("No Webull holdings found / not configured.")

    holdings_text = st.text_input(
        "Your current holdings (comma-separated tickers)",
        key="scr_holdings_text",
        help="Used to compute exposure overlap and correlation. Auto-fillable from Webull above.",
    )
    holdings = [t.strip().upper() for t in holdings_text.split(",") if t.strip()]

    if st.button("🧩 Analyze portfolio fit", key="scr_fit_btn"):
        row = next((r for r in scores if r["ticker"] == candidate), None)
        if row is None:
            st.error("Candidate not found in current screen results.")
        else:
            with st.spinner(f"Gemini 3.1 Pro analyzing how {candidate} fits your portfolio…"):
                st.session_state.scr_fit[candidate] = analyze_portfolio_fit(row, holdings)

    result = st.session_state.scr_fit.get(candidate)
    if result is None:
        st.info("Pick a candidate, set your holdings, then analyze the fit.")
        return
    if "_error" in result:
        st.error(result["_error"])
        return

    verdict = result.get("fit_verdict", "good_fit")
    vc = _FIT_COLOR.get(verdict, "#95a5a6")
    summary = result.get("summary", "")
    corr = (result.get("_correlation") or {}).get("avg_correlation")

    st.markdown(
        f'<div style="background:linear-gradient(135deg,{vc}22,{vc}11);border-left:4px solid {vc};'
        f'border-radius:8px;padding:14px 20px;margin-bottom:12px">'
        f'<span style="color:{vc};font-size:1.2rem;font-weight:700">'
        f'🧩 {candidate}: {verdict.replace("_", " ").upper()}</span>'
        + (f'&nbsp;&nbsp;<span style="color:#9aa3b8;font-size:0.85rem">avg corr {corr}</span>'
           if corr is not None else "")
        + (f'<p style="color:#cdd3e0;font-size:0.92rem;margin:8px 0 0">{summary}</p>' if summary else "")
        + '</div>',
        unsafe_allow_html=True,
    )

    exp = result.get("exposure_added", {})
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**📦 Exposure Added**")
        sectors = ", ".join(exp.get("sectors", []) or []) or "—"
        themes = ", ".join(exp.get("themes", []) or []) or "—"
        geo = exp.get("geography", "—")
        st.markdown(
            f'<div style="background:#161b27;border:1px solid #1e2740;border-radius:8px;padding:12px 16px">'
            f'<div style="color:#7a85a0;font-size:0.75rem">SECTORS</div>'
            f'<div style="color:#e8eaf0;font-size:0.9rem;margin-bottom:6px">{sectors}</div>'
            f'<div style="color:#7a85a0;font-size:0.75rem">THEMES / FACTORS</div>'
            f'<div style="color:#e8eaf0;font-size:0.9rem;margin-bottom:6px">{themes}</div>'
            f'<div style="color:#7a85a0;font-size:0.75rem">GEOGRAPHY</div>'
            f'<div style="color:#e8eaf0;font-size:0.9rem">{geo}</div></div>',
            unsafe_allow_html=True,
        )
        div_benefit = result.get("diversification_benefit", "")
        if div_benefit:
            st.markdown(f"**Diversification benefit:** {div_benefit}")
        size = result.get("suggested_position_size", "")
        if size:
            st.markdown(f"**Suggested size:** {size}")

    with c2:
        st.markdown("**🔗 Interaction with Holdings**")
        for it in result.get("interaction", []):
            t = it.get("ticker", "?")
            rel = it.get("relationship", "")
            detail = it.get("detail", "")
            rel_color = {"overlapping": "#e67e22", "correlated": "#f39c12",
                         "complementary": "#2ecc71", "hedging": "#3aa6ff"}.get(rel, "#95a5a6")
            st.markdown(
                f'<div style="background:#161b27;border:1px solid #1e2740;border-left:3px solid {rel_color};'
                f'border-radius:6px;padding:8px 12px;margin-bottom:6px">'
                f'<strong style="color:#e8eaf0">{t}</strong>'
                f'&nbsp;<span style="color:{rel_color};font-size:0.74rem">{rel}</span>'
                + (f'<br><span style="color:#cdd3e0;font-size:0.82rem">{detail}</span>' if detail else "")
                + '</div>',
                unsafe_allow_html=True,
            )

    conc = result.get("concentration_impact", "")
    if conc:
        st.markdown(f"**🎯 Concentration impact:** {conc}")
    risks = result.get("risks", [])
    if risks:
        st.warning("**Portfolio-level risks:** " + " · ".join(risks))


# ---------------------------------------------------------------------------
# Tab 5 — Picks Tracker
# ---------------------------------------------------------------------------

def _render_picks_tracker() -> None:
    section_header("Picks Tracker · Historical Screener Hit-Rate")
    st.caption(
        "Every Flash/Pro finalist from every run is recorded here with its price at pick "
        "time. Use this to see whether the screener's picks actually work over time."
    )

    picks = screener_cache.get_all_picks(limit=200)
    if not picks:
        st.info(
            "No picks recorded yet. Run a screen and advance a candidate to the "
            "Catalyst (Flash) or Deep-Dive (Pro) stage to start tracking."
        )
        return

    unique_tickers = sorted({p["ticker"] for p in picks})
    try:
        hist = get_batch_history(tuple(unique_tickers), period="5d")
    except Exception:
        hist = pd.DataFrame()

    last_price: dict[str, float] = {}
    if hist is not None and not hist.empty:
        for t in unique_tickers:
            if t in hist.columns:
                series = hist[t].dropna()
                if not series.empty:
                    last_price[t] = float(series.iloc[-1])

    rows = []
    returns = []
    for p in picks:
        ticker = p["ticker"]
        entry = p.get("entry_price")
        now_price = last_price.get(ticker)
        pct_return = None
        if entry and now_price:
            pct_return = (now_price / entry - 1.0) * 100.0
            returns.append(pct_return)
        rows.append({
            "Ticker": ticker,
            "Industry": p.get("industry", ""),
            "Picked At": str(p.get("picked_at", ""))[:10],
            "Stage": p.get("stage_reached", ""),
            "Boom Score": p.get("boom_score"),
            "Boom Potential": p.get("boom_potential") or "—",
            "Conviction": p.get("conviction") or "—",
            "Entry Price": entry,
            "Price Now": now_price,
            "% Return": round(pct_return, 2) if pct_return is not None else None,
            "Alert Price": p.get("alert_price"),
        })

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Total Picks Recorded", len(picks))
    with c2:
        hit_rate = (sum(1 for r in returns if r > 0) / len(returns) * 100.0) if returns else None
        st.metric("Positive-Return Hit Rate", f"{hit_rate:.1f}%" if hit_rate is not None else "N/A")
    with c3:
        avg_return = (sum(returns) / len(returns)) if returns else None
        st.metric("Avg Return Since Pick", f"{avg_return:.2f}%" if avg_return is not None else "N/A")

    df = pd.DataFrame(rows)
    st.dataframe(
        df.style.format(precision=2, na_rep="—"),
        use_container_width=True,
        hide_index=True,
    )


# ---------------------------------------------------------------------------
# Page entry
# ---------------------------------------------------------------------------

page_header(
    "AI Stock Screener",
    "Hunt the next big boom across curated sectors — quant rank, AI catalyst ranking, "
    "Pro deep-dive, and portfolio-fit analysis.",
)

_ctrl = _render_sidebar()

if _ctrl["run"]:
    if _ctrl["industry"] == REDDIT_TRENDING_LABEL:
        universe = list(dict.fromkeys(get_reddit_trending_pool(20) + _ctrl["custom"]))
        if not universe:
            st.warning(
                "No Reddit-trending tickers found in the last 7 days. Visit the "
                "Social Sentiment page and refresh daily mentions first, or pick a "
                "different industry pool."
            )
    else:
        universe = list(dict.fromkeys(INDUSTRY_POOLS[_ctrl["industry"]] + _ctrl["custom"]))

    if universe:
        with st.spinner(f"Scoring {len(universe)} {_ctrl['industry']} names…"):
            st.session_state.scr_scores = compute_quant_scores(universe, _ctrl["weights"])
        st.session_state.scr_industry = _ctrl["industry"]
        st.session_state.scr_flash = None
        st.session_state.scr_pro = None
        st.session_state.scr_run_id = screener_cache.save_run(_ctrl["industry"], _ctrl["weights"])
        st.session_state.scr_flash_saved = False

_scores = st.session_state.scr_scores
if not _scores:
    st.info("👈 Pick an industry pool, tune the boom-score weights, and click **🚀 Run Screen**.")
    st.stop()

st.caption(f"Showing screen for **{st.session_state.scr_industry}** · {len(_scores)} names scored.")

_tab_lead, _tab_cat, _tab_deep, _tab_fit, _tab_tracker = st.tabs([
    "📊 Leaderboard", "🤖 Catalyst (Flash)", "🏦 Deep-Dive (Pro)", "🧩 Portfolio Fit",
    "📈 Picks Tracker",
])
with _tab_lead:
    _render_leaderboard(_scores)
with _tab_cat:
    _render_catalyst(st.session_state.scr_industry, _scores)
with _tab_deep:
    _render_deep_dive(st.session_state.scr_industry, _scores)
with _tab_fit:
    _render_fit(_scores)
with _tab_tracker:
    _render_picks_tracker()
