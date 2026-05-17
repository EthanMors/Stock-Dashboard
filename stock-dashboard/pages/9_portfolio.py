import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import groupby

import numpy as np
import streamlit as st
import pandas as pd
import yfinance as yf
from datetime import datetime
from scipy.stats import norm

from components.gemini_usage_bar import render_gemini_usage_bar
from data.options_agent import run_options_analysis
from data.webull_positions import (
    is_configured,
    get_account_list,
    get_balance,
    get_env_account_ids,
    get_positions,
)
from data.news_fetcher import fetch_news, scrape_article
from data.news_analyzer import analyze_articles, get_sector_info
from data import macro_news_fetcher, macro_news_analyzer, macro_news_cache
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
from data.hedge_fund_fetcher import get_all_funds_from_db, refresh_hedge_fund_db

st.set_page_config(page_title="Portfolio", layout="wide")


@st.cache_data(ttl=300)
def _cached_account_list():
    return get_account_list()


@st.cache_data(ttl=300)
def _cached_balance(account_id: str):
    return get_balance(account_id)


@st.cache_data(ttl=300)
def _cached_positions(account_id: str):
    return get_positions(account_id)

render_gemini_usage_bar()

st.title("Portfolio")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_MIN_TICKER_ARTICLES = 3   # if fewer direct articles, also pull sector news
_MAX_ARTICLES_TO_SCRAPE = 5
_TICKER_FIELD_CANDIDATES = ["symbol", "ticker", "tickerSymbol", "stockSymbol", "sym"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_ticker(position: dict) -> str:
    for field in _TICKER_FIELD_CANDIDATES:
        val = position.get(field, "")
        if val and isinstance(val, str):
            return val.upper().strip()
    return ""


def _get_portfolio_tickers(positions: list) -> list:
    """Extract unique uppercase ticker strings from a list of position dicts.

    Iterates each position and tries each key in _TICKER_FIELD_CANDIDATES in order.
    Returns a list of unique, non-empty, uppercased ticker strings. Preserves
    insertion order (first occurrence of each ticker wins).
    """
    seen: set = set()
    result: list = []
    for pos in positions:
        t = _extract_ticker(pos)
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


def _find_overlapping_funds(portfolio_tickers: list) -> list:
    """Find concentrated hedge funds that hold any ticker in portfolio_tickers.

    Calls get_all_funds_from_db() to read from the smart cache DB. For each fund,
    checks which of its holdings' tickers are in the portfolio. Returns only funds
    with at least one overlapping holding, sorted by overlap_count descending.

    Args:
        portfolio_tickers: List of uppercase ticker strings (e.g. ['AAPL', 'TSLA']).

    Returns:
        List of dicts, each with keys:
            name (str), cik (str), report_period (str), filing_date (str),
            total_value (float), overlapping_holdings (list of HoldingRow dicts),
            overlap_count (int).
    """
    if not portfolio_tickers:
        return []

    portfolio_set = set(t.upper() for t in portfolio_tickers)

    try:
        refresh_hedge_fund_db()
        all_funds = get_all_funds_from_db()
    except Exception:
        return []

    overlapping = []
    for fund in all_funds:
        holdings = fund.get("holdings", [])
        matches = [
            h for h in holdings
            if str(h.get("ticker", "")).upper() in portfolio_set
        ]
        if not matches:
            continue
        overlapping.append({
            "name": fund.get("name", ""),
            "cik": fund.get("cik", ""),
            "report_period": fund.get("report_period", ""),
            "filing_date": fund.get("filing_date", ""),
            "total_value": fund.get("total_value", 0.0),
            "overlapping_holdings": matches,
            "overlap_count": len(matches),
        })

    overlapping.sort(key=lambda x: x["overlap_count"], reverse=True)
    return overlapping


def _render_hedge_fund_analysis(result: dict) -> None:
    """Render the structured Gemini hedge fund intelligence analysis.

    Displays:
    - Portfolio signal banner (overall_stance + confidence)
    - cross_ticker_themes as inline tags
    - Portfolio summary text
    - Per-ticker expandable cards (conviction_level, ownership_type, inferred_thesis, key_signal)
    - Flags warning block (if any flags present)

    Args:
        result: Dict returned by run_hedge_fund_analysis() with keys:
                per_ticker, portfolio_signal, flags.
    """
    if result is None:
        st.error("Analysis failed — no response from Gemini.")
        return
    if "_error" in result:
        st.error(f"Gemini error: {result['_error']}")
        return

    ps = result.get("portfolio_signal", {})
    stance = ps.get("overall_stance", "mixed")
    conf = ps.get("confidence", "medium")
    themes = ps.get("cross_ticker_themes", [])
    summary = ps.get("summary", "")

    _STANCE_COLOR = {
        "bullish": "#00c853",
        "bearish": "#ff1744",
        "mixed": "#ffd600",
        "defensive": "#ff6d00",
    }
    _STANCE_ICON = {
        "bullish": "▲",
        "bearish": "▼",
        "mixed": "●",
        "defensive": "◆",
    }
    color = _STANCE_COLOR.get(stance, "#ffd600")
    icon = _STANCE_ICON.get(stance, "●")

    # Portfolio signal banner
    st.markdown(
        f"""
        <div style="background:linear-gradient(135deg,{color}22,{color}11);
                    border-left:4px solid {color};border-radius:6px;
                    padding:14px 18px;margin-bottom:12px">
          <span style="color:{color};font-size:1.5rem;font-weight:700">
            {icon} SMART MONEY: {stance.upper()}
          </span>
          &nbsp;&nbsp;
          <span style="color:#aaa;font-size:0.9rem">Confidence: {conf.capitalize()}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Theme tags
    if themes:
        tags_html = "".join(
            f'<span style="background:#1e1e2e;border:1px solid #555;border-radius:12px;'
            f'padding:2px 10px;font-size:0.78rem;margin-right:6px;display:inline-block">{t}</span>'
            for t in themes
        )
        st.markdown(tags_html, unsafe_allow_html=True)
        st.markdown("")

    # Portfolio summary
    if summary:
        st.markdown(
            f"""
            <div style="background:#1a1a2e;border-left:4px solid {color};
                        border-radius:6px;padding:16px 20px;margin-bottom:16px">
              <p style="color:#ddd;font-size:0.95rem;margin:0;line-height:1.7">{summary}</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # Per-ticker expandable cards
    per_ticker = result.get("per_ticker", {})
    if per_ticker:
        st.markdown("**Per-Ticker Smart Money Signals**")
        _CONV_COLOR = {"high": "#00c853", "medium": "#ffd600", "low": "#aaa"}
        _OT_LABEL = {
            "bullish_equity": "Bullish Equity",
            "hedged": "Hedged",
            "speculative_put": "Speculative Put",
            "mixed": "Mixed",
        }
        for ticker_sym, entry in per_ticker.items():
            conv = entry.get("conviction_level", "medium")
            ot = entry.get("ownership_type", "bullish_equity")
            thesis = entry.get("inferred_thesis", "")
            key_signal = entry.get("key_signal", "")
            fund_count = entry.get("fund_count", 1)
            conv_color = _CONV_COLOR.get(conv, "#aaa")
            ot_label = _OT_LABEL.get(ot, ot.replace("_", " ").title())

            with st.expander(
                f"**{ticker_sym}** — {ot_label} · {conv.capitalize()} Conviction · {fund_count} fund{'s' if fund_count != 1 else ''}",
                expanded=False,
            ):
                c1, c2 = st.columns([1, 3])
                with c1:
                    st.markdown(
                        f'<span style="color:{conv_color};font-weight:700;font-size:1.1rem">'
                        f'{conv.upper()} CONVICTION</span>',
                        unsafe_allow_html=True,
                    )
                    st.caption(ot_label)
                with c2:
                    if key_signal:
                        st.markdown(
                            f'<div style="background:#1e1e2e;border-radius:6px;padding:10px 14px;'
                            f'font-size:0.88rem;color:#eee">'
                            f'<strong>Key Signal:</strong> {key_signal}</div>',
                            unsafe_allow_html=True,
                        )
                if thesis:
                    st.markdown(f"**Inferred Thesis:** {thesis}")

    # Flags warning block
    flags = result.get("flags", [])
    if flags:
        st.markdown("")
        flag_lines = "\n".join(f"- {f}" for f in flags)
        st.warning(f"**Notable Positioning Flags:**\n{flag_lines}")


def _render_hedge_fund_overlap(positions: list) -> None:
    """Render the Hedge Fund Overlap section for the given positions list.

    Shows a table of overlapping holdings inside an st.expander for each
    concentrated hedge fund that holds any ticker from the current portfolio.
    Displays a friendly info message when no overlaps are found.
    Also renders a Smart Money Analysis section powered by Gemini 2.5 Pro.

    Args:
        positions: Raw list of position dicts from get_positions(account_id).
    """
    portfolio_tickers = _get_portfolio_tickers(positions)

    if not portfolio_tickers:
        st.info("No ticker symbols found in your positions — cannot check hedge fund overlap.")
        return

    with st.spinner("Checking hedge fund holdings…"):
        overlapping = _find_overlapping_funds(portfolio_tickers)

    if not overlapping:
        st.info(
            "No concentrated hedge funds (< 15 positions) hold any of your current positions "
            "based on the most recent 13F-HR filings in the database. "
            "The database is populated from SEC EDGAR during the 45-day filing window after each quarter end."
        )
        return

    # Count unique portfolio tickers with overlapping funds
    _overlapping_tickers = set()
    for _f in overlapping:
        for _h in _f.get("overlapping_holdings", []):
            _t = str(_h.get("ticker", "")).upper()
            if _t:
                _overlapping_tickers.add(_t)
    st.info(
        f"Found {len(overlapping)} concentrated fund{'s' if len(overlapping) != 1 else ''} "
        f"holding {len(_overlapping_tickers)} of your position{'s' if len(_overlapping_tickers) != 1 else ''}."
    )

    # ── Smart Money Analysis ───────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### Smart Money Analysis")
    st.caption(
        "Gemini 2.5 Pro infers investment theses and portfolio-level signals from the 13F data above. "
        "Results cached 4 hours."
    )

    if "hf_analysis" not in st.session_state:
        st.session_state.hf_analysis = None

    hf_run_col, hf_hint_col = st.columns([2, 8])
    with hf_run_col:
        hf_run_clicked = st.button(
            "▶ Run Hedge Fund Intelligence",
            use_container_width=True,
            key="hf_gemini_btn",
        )
    with hf_hint_col:
        st.caption("Uses Gemini 2.5 Pro · ~60–180s · Results cached 4 hours · analyzes all overlapping funds")

    _trigger_hf = hf_run_clicked or st.session_state.pop("analyze_all_hf", False)

    if _trigger_hf:
        # Clear session state to force fresh analysis or cache check
        st.session_state.hf_analysis = None
        cached = get_latest_hedge_fund_analysis(portfolio_tickers)
        if cached is not None:
            st.session_state.hf_analysis = {**cached, "from_cache": True}
        else:
            with st.spinner("Gemini 2.5 Pro analyzing hedge fund positioning…"):
                result = run_hedge_fund_analysis(overlapping, portfolio_tickers)
            if result is not None and "_error" not in result:
                save_hedge_fund_analysis(portfolio_tickers, result)
            st.session_state.hf_analysis = {**(result or {}), "from_cache": False}

    if st.session_state.hf_analysis is not None:
        hf_entry = st.session_state.hf_analysis
        from_cache = hf_entry.get("from_cache", False)
        if from_cache:
            st.info("Serving cached analysis (< 4 hours old). Click the button again to force a refresh.")
        _render_hedge_fund_analysis(hf_entry)
    else:
        # Check DB on page load (without waiting for button click)
        auto_cached = get_latest_hedge_fund_analysis(portfolio_tickers)
        if auto_cached is not None:
            st.session_state.hf_analysis = {**auto_cached, "from_cache": True}
            st.info("Serving cached analysis (< 4 hours old). Click the button above to force a refresh.")
            _render_hedge_fund_analysis(auto_cached)


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
        _RISK_COLOR = {"high": "#ff1744", "medium": "#ff6d00", "low": "#00c853"}
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
                    delta_color = "#00c853" if wt_delta > 0 else ("#ff1744" if wt_delta < 0 else "#aaa")
                    st.markdown(
                        f'<span style="font-size:0.8rem;color:#aaa">Curr: {curr_wt:.1f}% → Opt: {sug_wt:.1f}% </span>'
                        f'<span style="font-size:0.8rem;color:{delta_color};font-weight:700">({wt_delta:+.1f}%)</span>',
                        unsafe_allow_html=True,
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
                _action_lower = action_text.lower()
                if "reduce" in _action_lower or "sell" in _action_lower or "trim" in _action_lower:
                    _item_color = "#ff1744"
                elif "increase" in _action_lower or "add" in _action_lower or "buy" in _action_lower:
                    _item_color = "#00c853"
                else:
                    _item_color = "#aaa"
                st.markdown(
                    f'<div style="background:#1e1e2e;border-left:3px solid {_item_color};'
                    f'border-radius:4px;padding:8px 14px;margin-bottom:6px;font-size:0.88rem">'
                    f'<strong style="color:{_item_color}">{ticker_sym}</strong>: {action_text}'
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
    _mpt_row_h = 35
    _mpt_hdr_h = 38
    if rows:
        ticker_df = pd.DataFrame(rows)
        st.dataframe(ticker_df, use_container_width=True, hide_index=True,
                     height=_mpt_hdr_h + _mpt_row_h * len(ticker_df))

    # ── Correlation heatmap ───────────────────────────────────────────────
    if len(tickers) >= 2:
        st.markdown("**Correlation Heatmap** (1-year daily returns)")
        corr_data = metrics.get("correlation_matrix", {})
        corr_df = pd.DataFrame(
            [[corr_data.get(t1, {}).get(t2, 0.0) for t2 in tickers] for t1 in tickers],
            index=tickers,
            columns=tickers,
        )
        def _corr_color(val):
            try:
                v = float(val)
            except (TypeError, ValueError):
                return ""
            # red for high positive correlation, green for low/negative
            if v >= 0.7:
                return "background-color: #c0392b; color: white"
            if v >= 0.4:
                return "background-color: #e67e22; color: white"
            if v >= 0.1:
                return "background-color: #f39c12; color: black"
            if v >= -0.1:
                return "background-color: #27ae60; color: white"
            return "background-color: #2980b9; color: white"

        styled_corr = corr_df.style.map(_corr_color).format("{:.3f}")
        st.dataframe(styled_corr, use_container_width=True,
                     height=_mpt_hdr_h + _mpt_row_h * len(corr_df))


# ---------------------------------------------------------------------------
# Market Pulse constants + helpers
# ---------------------------------------------------------------------------

_MACRO_SENTIMENT_ICON = {"positive": "🟢", "negative": "🔴", "neutral": "🟡"}

_CATEGORY_DISPLAY = {
    "monetary_policy": "Monetary Policy",
    "geopolitical": "Geopolitical",
    "macro_economy": "Macro Economy",
    "energy_commodities": "Energy & Commodities",
    "sector_financials": "Sector: Financials",
    "sector_technology": "Sector: Technology",
    "sector_energy": "Sector: Energy",
}

_MACRO_CAT_DISPLAY = {
    "bullish_for_equities": "Bullish for Equities",
    "bearish_for_equities": "Bearish for Equities",
    "mixed": "Mixed Impact",
    "sector_specific": "Sector Specific",
    "neutral": "Neutral",
}


def _impact_icon(level: int) -> str:
    if 1 <= level <= 3:
        return "📰"
    if 4 <= level <= 6:
        return "⚡"
    return "🚨"


def _format_pub_date(utc_val) -> str:
    try:
        if isinstance(utc_val, (int, float)):
            dt = datetime.fromtimestamp(utc_val, tz=datetime.now().astimezone().tzinfo)
            return dt.strftime("%b %d, %Y")
        return str(utc_val)[:10] if utc_val else "—"
    except Exception:
        return "—"


def _render_macro_card_portfolio(analysis: dict, idx: int) -> None:
    title = analysis.get("title", "No title")
    url = analysis.get("article_url", "")
    source = analysis.get("source", "")
    published = _format_pub_date(analysis.get("published_utc"))
    sentiment_score = analysis.get("sentiment_score", 0.0)
    sentiment_label = analysis.get("sentiment_label", "neutral")
    summary = analysis.get("summary", "")
    impact_level = analysis.get("impact_level", 0)
    key_themes = analysis.get("key_themes", [])
    affected_sectors = analysis.get("affected_sectors", [])
    macro_category = analysis.get("macro_category", "neutral")

    with st.expander(f"{_impact_icon(impact_level)} {title}", expanded=False):
        col1, col2 = st.columns([3, 1])
        with col1:
            meta = f"{source} · {published}"
            if affected_sectors:
                meta += " · " + " ".join(f"`{s}`" for s in affected_sectors)
            st.caption(meta)
            if summary:
                st.markdown(summary)
            if url:
                st.markdown(f"[Open article ↗]({url})")
            if key_themes:
                st.markdown(" ".join(f"`{t}`" for t in key_themes))
        with col2:
            icon = _MACRO_SENTIMENT_ICON.get(sentiment_label, "")
            st.markdown(f"**{icon} {sentiment_label.capitalize()}** ({sentiment_score:+.1f})")
            st.markdown(f"Impact **{impact_level}/10**")
            st.markdown(_MACRO_CAT_DISPLAY.get(macro_category, macro_category))


def _render_market_pulse_section() -> None:
    """Render the Market Pulse macro news section on the portfolio page."""
    st.markdown("---")
    st.subheader("🌍 Market Pulse")
    st.caption("Macro, geopolitical, and sector-wide news that moves markets.")

    mp_col1, mp_col2, mp_col3 = st.columns([2, 2, 1])
    with mp_col1:
        cat_options = ["All"] + list(_CATEGORY_DISPLAY.values())
        selected_cat_label = st.selectbox("Category", cat_options, key="mp_portfolio_cat")
        cat_reverse = {v: k for k, v in _CATEGORY_DISPLAY.items()}
        selected_cat = cat_reverse.get(selected_cat_label)
    with mp_col2:
        min_impact = st.slider("Min Impact Level", 1, 10, 4, key="mp_portfolio_impact")
    with mp_col3:
        st.markdown("")
        st.markdown("")
        refresh_clicked = st.button("🔄 Refresh", key="mp_portfolio_refresh", use_container_width=True)

    is_fresh = macro_news_cache.is_category_fresh(selected_cat, max_age_minutes=120)
    if not is_fresh or refresh_clicked:
        with st.spinner("Fetching macro news…"):
            raw = macro_news_fetcher.fetch_macro_articles(selected_cat)
        seen = macro_news_cache.get_article_urls_seen(selected_cat or "all")
        new_articles = [a for a in raw if a["article_url"] not in seen]
        if new_articles:
            with st.spinner(f"Analyzing {len(new_articles)} new articles with Gemini…"):
                for feed_cat, batch_iter in groupby(new_articles, key=lambda a: a["feed_category"]):
                    batch = list(batch_iter)[:5]
                    result = macro_news_analyzer.analyze_macro_articles(batch, feed_cat)
                    for article in batch:
                        macro_news_cache.save_macro_analysis(article, result)

    analyses = macro_news_cache.get_recent_analyses(
        category=selected_cat,
        hours=48,
        limit=20,
    )
    analyses = [a for a in analyses if a.get("impact_level", 0) >= min_impact]

    if not analyses:
        st.info("No macro news in cache yet — click 🔄 Refresh to fetch.")
        return

    for i, analysis in enumerate(analyses):
        _render_macro_card_portfolio(analysis, i)


def _sentiment_color(label: str) -> str:
    return {"positive": "#2ecc71", "negative": "#e74c3c"}.get(label, "#95a5a6")


def _impact_bar(level: int) -> str:
    filled = min(max(level, 0), 10)
    color = "#e74c3c" if filled >= 7 else "#f39c12" if filled >= 4 else "#2ecc71"
    return (
        f'<div style="background:#333;border-radius:4px;height:8px;width:100%">'
        f'<div style="background:{color};border-radius:4px;height:8px;width:{filled * 10}%"></div>'
        f"</div>"
    )


def _render_analysis(ticker: str, result: dict) -> None:
    label = result["sentiment_label"]
    score = result["sentiment_score"]
    color = _sentiment_color(label)
    impact = result["impact_level"]
    themes = result.get("key_themes", [])
    summary = result.get("summary", "")
    is_specific = result.get("is_stock_specific", True)

    badge = "Stock-Specific" if is_specific else "Sector-Level Fallback"
    badge_color = "#3498db" if is_specific else "#9b59b6"

    st.markdown(
        f"""
        <div style="border:1px solid #333;border-radius:8px;padding:16px;margin-bottom:12px">
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">
            <span style="font-size:1.4rem;font-weight:700;color:{color}">
              {label.upper()} &nbsp; {score:+.2f}
            </span>
            <span style="background:{badge_color};color:#fff;padding:2px 8px;
                         border-radius:12px;font-size:0.75rem">{badge}</span>
          </div>
          <div style="margin-bottom:8px">
            <span style="font-size:0.8rem;color:#aaa">Impact Level {impact}/10</span>
            {_impact_bar(impact)}
          </div>
          {"<div style='margin-bottom:10px'>" + "".join(
              f'<span style="background:#1e1e2e;border:1px solid #555;border-radius:12px;'
              f'padding:2px 10px;font-size:0.75rem;margin-right:6px;display:inline-block">{t}</span>'
              for t in themes
          ) + "</div>" if themes else ""}
          <p style="color:#ccc;font-size:0.9rem;line-height:1.5;margin:0">{summary}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _run_analysis_for_ticker(ticker: str, previous_analysis: dict | None = None) -> dict:
    """Fetch articles, scrape, and call Gemini for *ticker*.

    Returns the session-state cache dict:
    {
        "result": <analyze_articles() return dict>,
        "article_count": int,
        "sector": str,
        "is_fallback": bool,
        "analyzed_at": str ISO timestamp,
        "from_db": bool,
    }
    Also persists the result to the SQLite DB via save_analysis().
    """
    articles = fetch_news(ticker, limit=20)
    scraped: list[dict] = []
    for art in articles[:_MAX_ARTICLES_TO_SCRAPE]:
        url = art.get("article_url", "")
        content = scrape_article(url) if url else ""
        if content.startswith(("HTTP", "Access denied", "Request failed", "Could not")):
            content = art.get("description", "")
        scraped.append({
            "title": art.get("title", ""),
            "content": content,
            "source": (
                art.get("publisher", {}).get("name", "")
                if isinstance(art.get("publisher"), dict)
                else str(art.get("publisher", ""))
            ),
        })

    is_fallback = False
    sector = ""
    if len(scraped) < _MIN_TICKER_ARTICLES:
        sector, etf = get_sector_info(ticker)
        if etf:
            sector_articles = fetch_news(etf, limit=10)
            for art in sector_articles[:_MAX_ARTICLES_TO_SCRAPE]:
                url = art.get("article_url", "")
                content = scrape_article(url) if url else ""
                if content.startswith(("HTTP", "Access denied", "Request failed", "Could not")):
                    content = art.get("description", "")
                scraped.append({
                    "title": art.get("title", ""),
                    "content": content,
                    "source": (
                        art.get("publisher", {}).get("name", "")
                        if isinstance(art.get("publisher"), dict)
                        else str(art.get("publisher", ""))
                    ),
                })
            is_fallback = True

    result = analyze_articles(scraped, ticker, sector=sector, is_sector_fallback=is_fallback, previous_analysis=previous_analysis)
    save_analysis(
        ticker=ticker,
        result_dict=result,
        article_count=len(articles),
        sector=sector,
        is_fallback=is_fallback,
        articles=articles,
    )
    from datetime import datetime, timezone
    analyzed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "result": result,
        "article_count": len(articles),
        "sector": sector,
        "is_fallback": is_fallback,
        "analyzed_at": analyzed_at,
        "from_db": False,
    }


def _load_or_analyze(ticker: str) -> dict:
    """Return a session-cache dict for *ticker*, using DB cache when valid.

    Decision tree:
    1. Check st.session_state.news_results — return immediately if present
       (in-session memoization; no DB hit needed).
    2. Query DB for latest analysis via get_latest_analysis().
    3. Fetch fresh article list (fast — already @st.cache_data(ttl=900)).
    4. Call has_new_articles() to detect staleness.
    5a. If NOT stale: populate session_state from DB row and return.
    5b. If stale (or no DB row): call _run_analysis_for_ticker() which
        runs Gemini and saves the new result to DB.
    """
    ticker = ticker.upper()

    # Layer 1: in-session memoization
    if ticker in st.session_state.news_results:
        return st.session_state.news_results[ticker]

    # Layer 2: DB lookup
    cached_db = get_latest_analysis(ticker)
    articles = fetch_news(ticker, limit=20)

    if cached_db is not None and not has_new_articles(ticker, articles):
        # No new articles — serve DB result
        session_entry = {
            "result": {
                "sentiment_score": cached_db["sentiment_score"],
                "sentiment_label": cached_db["sentiment_label"],
                "summary": cached_db["summary"],
                "impact_level": cached_db["impact_level"],
                "key_themes": cached_db["key_themes"],
                "is_stock_specific": cached_db["is_stock_specific"],
            },
            "article_count": cached_db["article_count"],
            "sector": cached_db["sector"] or "",
            "is_fallback": cached_db["is_fallback"],
            "analyzed_at": cached_db["analyzed_at"],
            "from_db": True,
        }
        st.session_state.news_results[ticker] = session_entry
        return session_entry

    # Layer 3: new articles exist (or first-ever analysis) — run Gemini
    session_entry = _run_analysis_for_ticker(ticker, previous_analysis=cached_db)
    st.session_state.news_results[ticker] = session_entry
    return session_entry


# ---------------------------------------------------------------------------
# Account loading (mirrors 7_positions.py)
# ---------------------------------------------------------------------------
if not is_configured():
    st.error("Webull API credentials not configured.")
    st.markdown("Add `WEBULL_APP_KEY` and `WEBULL_APP_SECRET` to your `.env` file and restart.")
    st.stop()

env_ids = get_env_account_ids()

with st.spinner("Fetching account list…"):
    account_list_result = _cached_account_list()

id_to_label: dict[str, str] = {}
if isinstance(account_list_result, list):
    for account in account_list_result:
        aid = (
            account.get("accountId")
            or account.get("account_id")
            or account.get("accountNo")
            or account.get("id")
            or ""
        )
        label = account.get("account_label") or aid
        if aid:
            id_to_label[aid] = label

if env_ids:
    account_ids = env_ids
elif isinstance(account_list_result, dict) and "error" in account_list_result:
    st.error(f"API error: {account_list_result['error']}")
    st.stop()
elif not account_list_result:
    st.warning("No accounts returned. Check your credentials.")
    st.stop()
else:
    account_ids = list(id_to_label.keys())

accounts: dict[str, dict] = {}
with st.spinner("Fetching account balances…"):
    for aid in account_ids:
        balance = _cached_balance(aid)
        if isinstance(balance, dict) and "error" not in balance:
            label = id_to_label.get(aid) or aid
            accounts[label] = balance

if not accounts:
    st.warning("No balance data returned. Check your credentials.")
    st.stop()

label_to_id: dict[str, str] = {}
for aid in account_ids:
    lbl = id_to_label.get(aid) or aid
    label_to_id[lbl] = aid

selected_label = st.selectbox("Account", list(accounts.keys()))
selected_account_id = label_to_id.get(selected_label, selected_label)

# ---------------------------------------------------------------------------
# Balance summary
# ---------------------------------------------------------------------------
selected_balance = accounts[selected_label]
balance_fields = [
    "total_net_liquidation_value",
    "total_market_value",
    "total_cash_balance",
    "total_unrealized_profit_loss",
    "total_day_profit_loss",
]
display = {k: selected_balance.get(k) for k in balance_fields if k in selected_balance}
if display:
    df_bal = pd.DataFrame([display])
    df_bal.columns = [c.replace("_", " ").title() for c in df_bal.columns]
    st.dataframe(df_bal, use_container_width=True, hide_index=True)
else:
    st.json(selected_balance)

# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------
st.subheader("Positions")

with st.spinner("Fetching positions…"):
    positions_result = _cached_positions(selected_account_id)

if isinstance(positions_result, dict) and "error" in positions_result:
    st.error(f"API error: {positions_result['error']}")
    st.stop()
elif not positions_result:
    st.info("No positions found for this account.")
    st.stop()

if isinstance(positions_result, dict):
    positions_result = [positions_result]

# Extract tickers from positions
tickers: list[str] = []
for pos in positions_result:
    t = _extract_ticker(pos)
    if t and t not in tickers:
        tickers.append(t)

df_pos = pd.DataFrame(positions_result)

# Drop unwanted columns before renaming
_drop_raw = ("positionid", "instrumenttype")
_drop_cols = [c for c in df_pos.columns if c.lower().replace("_", "") in _drop_raw]
df_pos = df_pos.drop(columns=_drop_cols, errors="ignore")

# Convert proportion to percentage string
for _col in [c for c in df_pos.columns if c.lower() == "proportion"]:
    df_pos[_col] = (df_pos[_col].astype(float) * 100).round(2).astype(str) + "%"

# Reorder: symbol leftmost, currency rightmost
_sym_candidates = {"symbol", "ticker", "tickersymbol", "stocksymbol", "sym"}
_sym_col = next((c for c in df_pos.columns if c.lower().replace("_", "") in _sym_candidates), None)
_cur_col = next((c for c in df_pos.columns if c.lower() == "currency"), None)
_middle = [c for c in df_pos.columns if c not in (_sym_col, _cur_col)]
df_pos = df_pos[[c for c in ([_sym_col] + _middle + [_cur_col]) if c is not None]]

df_pos.columns = [c.replace("_", " ").title() for c in df_pos.columns]

def _color_pnl(val):
    try:
        v = float(val)
        if v > 0:
            return "color: #2ecc71"
        if v < 0:
            return "color: #e74c3c"
    except (TypeError, ValueError):
        pass
    return ""

_pos_tab, _mpt_tab = st.tabs(["Positions Table", "MPT Analysis"])

with _pos_tab:
    # Only color P&L and rate columns — not neutral positives like market value or quantity
    _pnl_keywords = ("profit", "loss", "return", "change", "rate")
    _pnl_cols = [c for c in df_pos.columns if any(kw in c.lower() for kw in _pnl_keywords)]
    styled = df_pos.style
    if _pnl_cols:
        styled = styled.map(_color_pnl, subset=_pnl_cols)
    _row_height = 35
    _header_height = 38
    st.dataframe(styled, use_container_width=True, hide_index=True,
                 height=_header_height + _row_height * len(df_pos))

with _mpt_tab:
    _render_mpt_analysis(positions_result)

# ---------------------------------------------------------------------------
# Analyze Everything
# ---------------------------------------------------------------------------
st.markdown("---")
if st.button("⚡ Analyze Everything", use_container_width=True, key="analyze_everything_btn"):
    st.session_state.analyze_all_news = True
    st.session_state.analyze_all_options = True
    st.session_state.analyze_all_hf = True
    st.session_state.analyze_all_mpt = True
    st.rerun()

if not tickers:
    st.warning("Could not extract ticker symbols from position data — news analysis unavailable.")
    st.stop()

# ---------------------------------------------------------------------------
# News Analysis
# ---------------------------------------------------------------------------
st.markdown("---")
st.subheader("News Analysis")

if "news_results" not in st.session_state:
    st.session_state.news_results = {}

col_a, col_b = st.columns([3, 1])
with col_a:
    selected_ticker = st.selectbox("Analyze news for", ["— Select a stock —"] + tickers)
with col_b:
    analyze_all = st.button("Analyze All Positions", use_container_width=True)

# Single ticker analysis
if selected_ticker and selected_ticker != "— Select a stock —":
    if st.button(f"Run News Analysis for {selected_ticker}") or selected_ticker in st.session_state.news_results:
        with st.spinner(f"Fetching and analyzing news for {selected_ticker}…"):
            cached = _load_or_analyze(selected_ticker)

        article_count = cached["article_count"]
        sector = cached["sector"]
        is_fallback = cached["is_fallback"]
        from_db = cached.get("from_db", False)
        analyzed_at = cached.get("analyzed_at", "")

        if from_db and analyzed_at:
            st.info(f"Cached analysis from {analyzed_at} UTC — no new articles detected.")
        else:
            st.success(f"Freshly analyzed at {analyzed_at} UTC.")
        note = f"{article_count} articles found"
        if is_fallback and sector:
            note += f" — supplemented with {sector} sector news"
        st.caption(note)
        _render_analysis(selected_ticker, cached["result"])

# Analyze all positions
_trigger_all_news = analyze_all or st.session_state.pop("analyze_all_news", False)
if _trigger_all_news:
    progress = st.progress(0, text="Starting analysis…")
    completed = 0

    def _analyze_ticker(ticker):
        return ticker, _load_or_analyze(ticker)

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_analyze_ticker, t): t for t in tickers}
        for future in as_completed(futures):
            ticker, entry = future.result()
            st.session_state.news_results[ticker] = entry
            completed += 1
            label = "Gemini" if not entry.get("from_db") else "cache"
            progress.progress(completed / len(tickers), text=f"Done ({label}): {ticker}")
    progress.empty()

# Display all cached results
if st.session_state.news_results:
    st.markdown("### Analysis Results")
    for ticker, cached in st.session_state.news_results.items():
        sentiment_label = cached["result"]["sentiment_label"].upper()
        sentiment_score = cached["result"]["sentiment_score"]
        from_db = cached.get("from_db", False)
        analyzed_at = cached.get("analyzed_at", "")
        cache_tag = " [cached]" if from_db else ""
        with st.expander(
            f"**{ticker}** — {sentiment_label} ({sentiment_score:+.2f}){cache_tag}",
            expanded=False,
        ):
            if from_db and analyzed_at:
                st.info(f"Cached from {analyzed_at} UTC — no new articles detected.")
            elif analyzed_at:
                st.success(f"Freshly analyzed at {analyzed_at} UTC.")
            note = f"{cached['article_count']} articles"
            if cached["is_fallback"] and cached["sector"]:
                note += f" · {cached['sector']} sector fallback"
            st.caption(note)
            _render_analysis(ticker, cached["result"])

    if st.button("Clear All Results"):
        st.session_state.news_results = {}
        st.rerun()

_render_market_pulse_section()

# ---------------------------------------------------------------------------
# Options Analysis
# ---------------------------------------------------------------------------
st.markdown("---")
st.subheader("Options Analysis")

_RISK_FREE_RATE = 0.045
_N_CONTRACTS = 10


def _bs_greeks(S: float, K: float, T: float, r: float, sigma: float, opt_type: str) -> dict:
    zero = dict(delta=0.0, gamma=0.0, theta=0.0, vega=0.0, rho=0.0)
    if T <= 1e-6 or sigma <= 1e-6 or S <= 0 or K <= 0:
        return zero
    sqrt_T = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    pdf_d1 = norm.pdf(d1)
    gamma = pdf_d1 / (S * sigma * sqrt_T)
    vega = S * pdf_d1 * sqrt_T / 100
    if opt_type == "call":
        delta = norm.cdf(d1)
        theta = (-(S * pdf_d1 * sigma) / (2 * sqrt_T) - r * K * np.exp(-r * T) * norm.cdf(d2)) / 365
        rho = K * T * np.exp(-r * T) * norm.cdf(d2) / 100
    else:
        delta = norm.cdf(d1) - 1.0
        theta = (-(S * pdf_d1 * sigma) / (2 * sqrt_T) + r * K * np.exp(-r * T) * norm.cdf(-d2)) / 365
        rho = -K * T * np.exp(-r * T) * norm.cdf(-d2) / 100
    return dict(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho)


def _build_options_display_df(raw: pd.DataFrame, current_price: float, expiry: str, opt_type: str) -> pd.DataFrame:
    expiry_dt = datetime.strptime(expiry, "%Y-%m-%d")
    T = max((expiry_dt - datetime.today()).days / 365.0, 1.0 / 365)
    n = _N_CONTRACTS
    if opt_type == "call":
        itm = raw[raw["strike"] < current_price].nlargest(n, "strike")
        otm = raw[raw["strike"] >= current_price].nsmallest(n, "strike")
    else:
        itm = raw[raw["strike"] > current_price].nsmallest(n, "strike")
        otm = raw[raw["strike"] <= current_price].nlargest(n, "strike")

    def _f(v) -> float:
        return float(v) if pd.notna(v) and v else 0.0

    def _i(v) -> int:
        return int(v) if pd.notna(v) and v else 0

    rows = []
    for _, row in pd.concat([itm, otm]).sort_values("strike").iterrows():
        iv = _f(row.get("impliedVolatility"))
        g = _bs_greeks(current_price, float(row["strike"]), T, _RISK_FREE_RATE, iv, opt_type)
        rows.append({
            "ITM":       bool(row.get("inTheMoney", False)),
            "Strike":    float(row["strike"]),
            "Bid":       _f(row.get("bid")),
            "Ask":       _f(row.get("ask")),
            "Last":      _f(row.get("lastPrice")),
            "Volume":    _i(row.get("volume")),
            "Open Int.": _i(row.get("openInterest")),
            "IV %":      round(iv * 100, 2),
            "Delta":     round(g["delta"], 4),
            "Gamma":     round(g["gamma"], 6),
            "Theta":     round(g["theta"], 4),
            "Vega":      round(g["vega"], 4),
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=60)
def _fetch_price_and_expirations(sym: str):
    t = yf.Ticker(sym)
    try:
        price = float(t.fast_info["last_price"])
    except Exception:
        price = None
    return price, list(t.options)


@st.cache_data(ttl=60)
def _fetch_chain(sym: str, exp: str):
    chain = yf.Ticker(sym).option_chain(exp)
    return chain.calls, chain.puts


_OPT_BIAS_COLOR = {"bullish": "#00c853", "bearish": "#ff1744", "neutral": "#ffd600"}
_OPT_BIAS_ICON  = {"bullish": "▲", "bearish": "▼", "neutral": "●"}


def _render_options_analysis(result: dict, spot: float) -> None:
    if result is None:
        st.error("Analysis failed — no response from Gemini.")
        return
    if "_error" in result:
        st.error(f"Gemini error: {result['_error']}")
        return

    bias     = result.get("directional_bias", "neutral")
    strength = result.get("bias_strength", "moderate")
    conf     = result.get("confidence", "medium")
    metrics  = result.get("metrics", {})
    color    = _OPT_BIAS_COLOR.get(bias, "#ffd600")
    icon     = _OPT_BIAS_ICON.get(bias, "●")

    st.markdown(
        f"""
        <div style="background:linear-gradient(135deg,{color}22,{color}11);
                    border-left:4px solid {color};border-radius:6px;
                    padding:14px 18px;margin-bottom:12px">
          <span style="color:{color};font-size:1.5rem;font-weight:700">
            {icon} {bias.upper()} — {strength.capitalize()} Conviction
          </span>
          &nbsp;&nbsp;
          <span style="color:#aaa;font-size:0.9rem">Confidence: {conf.capitalize()}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    m1, m2, m3, m4, m5 = st.columns(5)
    pcr_oi       = metrics.get("pcr_oi")
    pcr_vol      = metrics.get("pcr_vol")
    max_pain_val = metrics.get("max_pain")
    iv_skew_val  = metrics.get("iv_skew")
    net_gex_val  = metrics.get("net_gex")

    m1.metric("P/C OI Ratio",   f"{pcr_oi:.3f}"  if pcr_oi  is not None else "N/A",
              help="Put OI ÷ call OI across full chain. >1 = more puts outstanding.")
    m2.metric("P/C Vol Ratio",  f"{pcr_vol:.3f}" if pcr_vol is not None else "N/A",
              help="Put volume ÷ call volume today.")
    m3.metric("Max Pain",       f"${max_pain_val:.2f}" if max_pain_val is not None else "N/A",
              delta=f"{max_pain_val - spot:+.2f} from spot" if max_pain_val is not None else None,
              help="Strike maximizing combined OI dollar loss to buyers at expiry.")
    m4.metric("IV Skew",        f"{iv_skew_val*100:+.2f}%" if iv_skew_val is not None else "N/A",
              help="OTM put avg IV − OTM call avg IV. Positive = bearish fear premium.")
    m5.metric("Net Dealer GEX", f"${net_gex_val/1e6:.2f}M" if net_gex_val is not None else "N/A",
              help="Positive = dealers long gamma (pinning). Negative = vol amplification.")

    st.markdown("---")

    sections = [
        ("📊 Implied Volatility",          "iv_analysis",             True),
        ("⚖️ Put/Call Ratio",               "pcr_analysis",            True),
        ("🎯 Max Pain",                     "max_pain_analysis",       True),
        ("⚡ Gamma Exposure & Flows",       "gamma_exposure_analysis", False),
        ("🏦 Key Price Levels",             "key_levels",              False),
        ("🚨 Unusual Activity",             "unusual_activity",        False),
        ("⚠️ Risk Factors",                 "risk_factors",            False),
    ]
    for label, field, expanded in sections:
        with st.expander(label, expanded=expanded):
            val = result.get(field, "")
            text = "\n".join(f"- {item}" for item in val) if isinstance(val, list) else str(val).strip()
            if text:
                st.markdown(text)
            else:
                st.caption("No data returned.")

    summary = result.get("summary", "").strip()
    if summary:
        st.markdown(
            f"""
            <div style="background:#1a1a2e;border-left:4px solid {color};
                        border-radius:6px;padding:16px 20px;margin-top:12px">
              <p style="color:#ddd;font-size:0.95rem;margin:0;line-height:1.7">{summary}</p>
            </div>
            """,
            unsafe_allow_html=True,
        )


# ── Options Analysis helpers ───────────────────────────────────────────────────

_OPT_COL_CONFIG = {
    "ITM":       st.column_config.CheckboxColumn("ITM"),
    "Strike":    st.column_config.NumberColumn("Strike",    format="$%.2f"),
    "Bid":       st.column_config.NumberColumn("Bid",       format="$%.2f"),
    "Ask":       st.column_config.NumberColumn("Ask",       format="$%.2f"),
    "Last":      st.column_config.NumberColumn("Last",      format="$%.2f"),
    "Volume":    st.column_config.NumberColumn("Volume",    format="%d"),
    "Open Int.": st.column_config.NumberColumn("Open Int.", format="%d"),
    "IV %":      st.column_config.NumberColumn("IV %",      format="%.2f%%"),
    "Delta":     st.column_config.NumberColumn("Delta",     format="%.4f"),
    "Gamma":     st.column_config.NumberColumn("Gamma",     format="%.6f"),
    "Theta":     st.column_config.NumberColumn("Theta",     format="%.4f"),
    "Vega":      st.column_config.NumberColumn("Vega",      format="%.4f"),
}

if "options_results" not in st.session_state:
    st.session_state.options_results = {}


def _opt_session_key(ticker: str, expiry: str, opt_type: str) -> str:
    return f"{ticker}|{expiry}|{opt_type}"


def _load_or_analyze_options(
    ticker: str,
    expiry: str,
    opt_type: str,
    price: float,
    calls_df,
    puts_df,
    calls_display_df,
    puts_display_df,
) -> dict:
    """Return options analysis from session state → DB cache → Gemini (in that order)."""
    sess_key = _opt_session_key(ticker, expiry, opt_type)

    if sess_key in st.session_state.options_results:
        return st.session_state.options_results[sess_key]

    cached_db = get_latest_options_analysis(ticker, expiry, opt_type)
    if cached_db is not None and is_options_analysis_fresh(cached_db.get("analyzed_at", "")):
        entry = {**cached_db, "from_db": True}
        st.session_state.options_results[sess_key] = entry
        return entry

    result = run_options_analysis(ticker, price, expiry, opt_type, calls_df, puts_df, calls_display_df, puts_display_df)
    if result and "_error" not in result:
        save_options_analysis(ticker, expiry, opt_type, price, result)
    from datetime import datetime as _dt, timezone as _tz
    analyzed_at = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%S")
    entry = {**result, "analyzed_at": analyzed_at, "from_db": False}
    st.session_state.options_results[sess_key] = entry
    return entry


# ── Options Analysis UI (fragment = reruns only this section) ─────────────────

@st.fragment
def _options_analysis_ui(tickers: list[str]) -> None:
    oa_col_a, oa_col_b = st.columns([3, 1])
    with oa_col_a:
        opt_ticker = st.selectbox(
            "Analyze options for",
            ["— Select a stock —"] + tickers,
            key="opt_ticker_select",
        )
    with oa_col_b:
        opt_analyze_all = st.button("Analyze All Positions", use_container_width=True, key="opt_analyze_all_btn")

    _trigger_all_options = opt_analyze_all or st.session_state.pop("analyze_all_options", False)
    if _trigger_all_options:
        oa_progress = st.progress(0, text="Starting options analysis…")
        oa_completed = 0

        def _analyze_options_ticker(t):
            """Worker: fetch price/chain, calc Greeks, run Gemini. Returns (sess_key, entry_or_None)."""
            from datetime import datetime as _dt_w, timezone as _tz_w
            try:
                t_price, t_expirations = _fetch_price_and_expirations(t)
            except Exception:
                return _opt_session_key(t, "", "put"), None
            if not t_expirations or t_price is None:
                return _opt_session_key(t, "", "put"), None
            nearest_expiry = t_expirations[0]
            sess_key = _opt_session_key(t, nearest_expiry, "put")
            cached_db = get_latest_options_analysis(t, nearest_expiry, "put")
            if cached_db is not None and is_options_analysis_fresh(cached_db.get("analyzed_at", "")):
                return sess_key, {**cached_db, "from_db": True}
            try:
                t_calls, t_puts = _fetch_chain(t, nearest_expiry)
                t_calls_display = _build_options_display_df(t_calls, t_price, nearest_expiry, "call")
                t_puts_display  = _build_options_display_df(t_puts,  t_price, nearest_expiry, "put")
            except Exception:
                return sess_key, None
            result = run_options_analysis(t, t_price, nearest_expiry, "put", t_calls, t_puts, t_calls_display, t_puts_display)
            if result and "_error" not in result:
                save_options_analysis(t, nearest_expiry, "put", t_price, result)
            analyzed_at = _dt_w.now(_tz_w.utc).strftime("%Y-%m-%dT%H:%M:%S")
            entry = {**result, "analyzed_at": analyzed_at, "from_db": False}
            return sess_key, entry

        with ThreadPoolExecutor(max_workers=3) as oa_executor:
            oa_futures = {oa_executor.submit(_analyze_options_ticker, t): t for t in tickers}
            for oa_future in as_completed(oa_futures):
                t = oa_futures[oa_future]
                sess_key, entry = oa_future.result()
                oa_completed += 1
                if entry is None:
                    oa_progress.progress(oa_completed / len(tickers), text=f"Skipped {t}")
                else:
                    st.session_state.options_results[sess_key] = entry
                    label = "cache" if entry.get("from_db") else "Gemini"
                    oa_progress.progress(oa_completed / len(tickers), text=f"Done ({label}): {t}")
        oa_progress.empty()

    if opt_ticker and opt_ticker != "— Select a stock —":
        try:
            opt_price, opt_expirations = _fetch_price_and_expirations(opt_ticker)
        except Exception as e:
            st.error(f"Failed to fetch options data for {opt_ticker}: {e}")
            opt_expirations = []
            opt_price = None

        if not opt_expirations:
            st.warning(f"No options listed for {opt_ticker}.")
        elif opt_price is None:
            st.warning(f"Could not retrieve current price for {opt_ticker}.")
        else:
            oa_ctrl1, oa_ctrl2, oa_ctrl3 = st.columns([2, 3, 2])
            with oa_ctrl1:
                st.metric("Current Price", f"${opt_price:,.2f}")
            with oa_ctrl2:
                opt_expiry = st.selectbox("Expiration Date", opt_expirations, key="opt_expiry_select")
            with oa_ctrl3:
                opt_contract_label = st.radio("Contract Type", ["Calls", "Puts"], horizontal=True, key="opt_contract_radio")

            opt_type = "call" if opt_contract_label == "Calls" else "put"

            try:
                opt_calls_df, opt_puts_df = _fetch_chain(opt_ticker, opt_expiry)
            except Exception as e:
                st.error(f"Error loading option chain for {opt_expiry}: {e}")
                opt_calls_df = opt_puts_df = None

            if opt_calls_df is not None:
                with st.spinner("Calculating Greeks…"):
                    opt_calls_display_df = _build_options_display_df(opt_calls_df, opt_price, opt_expiry, "call")
                    opt_puts_display_df  = _build_options_display_df(opt_puts_df,  opt_price, opt_expiry, "put")

                opt_display_df = opt_calls_display_df if opt_type == "call" else opt_puts_display_df

                if not opt_display_df.empty:
                    st.caption(
                        f"{_N_CONTRACTS} nearest ITM + {_N_CONTRACTS} nearest OTM {opt_contract_label.lower()} · "
                        f"Greeks via Black-Scholes · Risk-free rate {_RISK_FREE_RATE*100:.1f}%"
                    )
                    st.dataframe(opt_display_df, use_container_width=True, hide_index=True, column_config=_OPT_COL_CONFIG)

                    opt_sess_key = _opt_session_key(opt_ticker, opt_expiry, opt_type)

                    run_col, hint_col = st.columns([2, 8])
                    with run_col:
                        opt_run_clicked = st.button("▶ Run Gemini Analysis", use_container_width=True, key="opt_run_btn")
                    with hint_col:
                        st.caption("Uses Gemini 2.5 Pro · ~30–90s · Results cached 4 hours · analyzes both calls & puts")

                    if opt_run_clicked:
                        st.session_state.options_results.pop(opt_sess_key, None)
                        with st.spinner("Gemini 2.5 Pro analyzing the full option chain…"):
                            entry = _load_or_analyze_options(
                                opt_ticker, opt_expiry, opt_type, opt_price,
                                opt_calls_df, opt_puts_df, opt_calls_display_df, opt_puts_display_df,
                            )

                    if opt_sess_key in st.session_state.options_results:
                        entry = st.session_state.options_results[opt_sess_key]
                        analyzed_at = entry.get("analyzed_at", "")
                        from_db = entry.get("from_db", False)
                        if from_db and analyzed_at:
                            st.info(f"Cached analysis from {analyzed_at} UTC (re-runs after 4 hours).")
                        elif analyzed_at:
                            st.success(f"Freshly analyzed at {analyzed_at} UTC.")
                        _render_options_analysis(entry, opt_price)
                    else:
                        cached_db = get_latest_options_analysis(opt_ticker, opt_expiry, opt_type)
                        if cached_db is not None and is_options_analysis_fresh(cached_db.get("analyzed_at", "")):
                            entry = {**cached_db, "from_db": True}
                            st.session_state.options_results[opt_sess_key] = entry
                            st.info(f"Cached analysis from {cached_db['analyzed_at']} UTC (re-runs after 4 hours).")
                            _render_options_analysis(entry, opt_price)

    # ── All Options Results Summary ────────────────────────────────────────────

    if st.session_state.options_results:
        st.markdown("### Options Analysis Results")
        for sess_key, entry in st.session_state.options_results.items():
            if "_error" in entry:
                continue
            parts = sess_key.split("|")
            t_label = parts[0] if len(parts) > 0 else sess_key
            exp_label = parts[1] if len(parts) > 1 else ""
            type_label = "Calls" if (len(parts) > 2 and parts[2] == "call") else "Puts"
            bias = entry.get("directional_bias", "neutral").upper()
            strength = entry.get("bias_strength", "moderate").capitalize()
            conf = entry.get("confidence", "medium").capitalize()
            from_db = entry.get("from_db", False)
            cache_tag = " [cached]" if from_db else ""
            spot = entry.get("spot_price") or 0.0
            with st.expander(
                f"**{t_label}** · {exp_label} {type_label} — {bias} ({strength}){cache_tag}",
                expanded=False,
            ):
                analyzed_at = entry.get("analyzed_at", "")
                if from_db and analyzed_at:
                    st.info(f"Cached from {analyzed_at} UTC.")
                elif analyzed_at:
                    st.success(f"Analyzed at {analyzed_at} UTC.")
                st.caption(f"Confidence: {conf}")
                _render_options_analysis(entry, spot)

        if st.button("Clear Options Results", key="opt_clear_btn"):
            st.session_state.options_results = {}
            st.rerun()


_options_analysis_ui(tickers)

# ---------------------------------------------------------------------------
# Smart Money Analysis
# ---------------------------------------------------------------------------
st.markdown("---")
_render_hedge_fund_overlap(positions_result)
