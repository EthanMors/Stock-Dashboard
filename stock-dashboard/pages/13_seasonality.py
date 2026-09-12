# pages/13_seasonality.py
#
# Seasonality & Regime — what is coming, and how the portfolio should adapt.
# Five tabs: Outlook, Seasonality, Macro Regime, Sector Rotation, Calendar.

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import (
    explainer,
    inject_global_css,
    page_header,
    plotly_dark_layout,
    render_sidebar_nav,
    section_header,
)
from data.gemini_tracker import PRO_DAILY_LIMIT, get_today_stats
from data.seasonality import (
    BENCHMARK,
    DEFAULT_LOOKBACK_YEARS,
    SECTOR_ETFS,
    build_analysis_payload,
    get_monthly_seasonality,
    get_seasonality_matrix,
)
from data.seasonality_agent import run_seasonality_analysis
from data.seasonality_cache import (
    get_latest_analysis,
    get_previous_regime,
    get_regime_history,
    save_analysis,
    save_regime_snapshot,
)
from data.webull_positions import get_env_account_ids, get_positions, is_configured

st.set_page_config(page_title="Seasonality & Regime", layout="wide")

render_gemini_usage_bar()
inject_global_css()
render_sidebar_nav()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOOKBACK_OPTIONS = {"10 years": 10, "20 years": 20, "30 years": 30}
_DEFAULT_LOOKBACK = "20 years"

_ACTION_COLORS = {
    "add": "#00c853",
    "overweight": "#00c853",
    "accumulate on weakness": "#64dd17",
    "hold": "#2979ff",
    "neutral": "#8892b0",
    "underweight": "#ff9100",
    "trim": "#ff6d00",
    "hedge": "#ffd600",
    "exit": "#ff1744",
    "avoid": "#ff1744",
}

_KIND_ICONS = {
    "fed": "🏛️",
    "macro": "📊",
    "earnings": "📄",
    "market": "🔔",
}

_POSITIVE = "#00c853"
_NEGATIVE = "#ff5252"
_MUTED = "#8892b0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _color_for(action: str) -> str:
    return _ACTION_COLORS.get((action or "").strip().lower(), _MUTED)


def _signed_html(value, suffix: str = "%", digits: int = 2) -> str:
    """Green/red signed number, or a muted dash when the value is missing."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return f'<span style="color:{_MUTED}">—</span>'
    color = _POSITIVE if float(value) >= 0 else _NEGATIVE
    return f'<span style="color:{color};font-weight:600">{float(value):+.{digits}f}{suffix}</span>'


def _score_bar(score: float, width: int = 120) -> str:
    """A small centred bar for a [-100, 100] score."""
    pct = max(-100.0, min(100.0, float(score)))
    half = width / 2
    length = abs(pct) / 100.0 * half
    color = _POSITIVE if pct >= 0 else _NEGATIVE
    offset = half if pct >= 0 else half - length
    return (
        f'<div style="position:relative;width:{width}px;height:10px;'
        f'background:#161b27;border:1px solid #1e2740;border-radius:5px;">'
        f'<div style="position:absolute;left:{offset}px;top:0;width:{length}px;'
        f'height:8px;background:{color};border-radius:4px;"></div>'
        f'<div style="position:absolute;left:{half}px;top:-2px;width:1px;'
        f'height:12px;background:#2a3a60;"></div></div>'
    )


# ---------------------------------------------------------------------------
# Cached data fetchers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def _cached_positions(account_id: str):
    return get_positions(account_id)


@st.cache_data(ttl=3600, show_spinner=False)
def _cached_payload(positions_key: tuple, lookback_years: int, _positions: list):
    """Full computed payload. `positions_key` is the cache key; `_positions` is
    excluded from hashing (leading underscore) since it holds unhashable dicts."""
    return build_analysis_payload(_positions, lookback_years)


@st.cache_data(ttl=3600, show_spinner=False)
def _cached_matrix(tickers: tuple, lookback_years: int):
    return get_seasonality_matrix(list(tickers), lookback_years)


@st.cache_data(ttl=3600, show_spinner=False)
def _cached_stats(ticker: str, lookback_years: int):
    return get_monthly_seasonality(ticker, lookback_years)


# ---------------------------------------------------------------------------
# Position loading
# ---------------------------------------------------------------------------

def _load_positions() -> list:
    """Positions from Webull, or the manual ticker list the user typed."""
    manual = st.session_state.get("seasonality_manual_tickers", "").strip()
    if manual:
        return [
            {"symbol": part.strip().upper(), "marketValue": 1.0}
            for part in manual.replace(",", " ").split()
            if part.strip()
        ]

    if not is_configured():
        return []

    account_ids = get_env_account_ids()
    if not account_ids:
        return []

    positions = _cached_positions(account_ids[0])
    if isinstance(positions, dict):   # {"error": ...}
        return []
    return positions or []


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def _render_seasonality_heatmap(matrix: pd.DataFrame, title: str) -> None:
    """Ticker x month heatmap of average monthly returns."""
    if matrix.empty:
        st.info("No seasonality data available for these tickers.")
        return

    # One wild small-cap can otherwise wash the whole map out, so clamp the
    # colour range to a robust percentile and let outliers saturate.
    finite = matrix.values[~pd.isna(matrix.values)]
    limit = 5.0
    if finite.size:
        limit = max(1.0, float(pd.Series(abs(finite)).quantile(0.90)))

    fig = go.Figure(
        go.Heatmap(
            z=matrix.values,
            x=list(matrix.columns),
            y=list(matrix.index),
            colorscale=[[0.0, "#ff1744"], [0.5, "#161b27"], [1.0, "#00c853"]],
            zmid=0,
            zmin=-limit,
            zmax=limit,
            colorbar=dict(title="Avg %", thickness=12),
            hovertemplate="%{y} · %{x}<br>avg %{z:.2f}%<extra></extra>",
        )
    )
    fig.update_layout(
        **plotly_dark_layout(
            title=title,
            height=max(320, 34 * len(matrix.index) + 120),
            xaxis=dict(side="top", gridcolor="#1e2740"),
            yaxis=dict(autorange="reversed", gridcolor="#1e2740"),
        )
    )
    st.plotly_chart(fig, use_container_width=True)
    explainer(
        "**Each row is a ticker, each column a calendar month.** The colour is that "
        "ticker's *average* return in that month across every year of history — "
        "green means it usually rose, red means it usually fell, near-black means "
        "no consistent pattern. Hover any square for the exact number.\n\n"
        "**Reading it:** scan *across* a row to see a ticker's yearly rhythm, or "
        "*down* a column to see who tends to do well in a given month. A row that "
        "is mostly one colour has a real seasonal tilt; a row that looks like "
        "confetti does not.\n\n"
        "**The big caveat:** an average hides how often it actually happened. A "
        "+4% average could be nineteen flat years and one +80% moonshot. Always "
        "check the **win rate** in the single-ticker table further down before you "
        "trust a green square — a month that averages +1% but rose 80% of the time "
        "is a far better signal than one averaging +4% that rose 45% of the time.\n\n"
        "Colour intensity is capped at the 90th percentile of the values on screen, "
        "so one wild small-cap cannot wash the rest of the map out."
    )


def _render_month_profile(stats: dict, highlight_month: str) -> None:
    """Bar chart of average monthly return with the month ahead highlighted."""
    months = stats.get("months") or {}
    if not months:
        st.info(f"No monthly history available for {stats.get('ticker', 'this ticker')}.")
        return

    names = list(months.keys())
    averages = [months[name]["avg"] for name in names]
    win_rates = [months[name]["win_rate"] for name in names]
    colors = [
        "#ffd600" if name == highlight_month
        else (_POSITIVE if value >= 0 else _NEGATIVE)
        for name, value in zip(names, averages)
    ]

    fig = go.Figure(
        go.Bar(
            x=names,
            y=averages,
            marker_color=colors,
            customdata=win_rates,
            hovertemplate="%{x}<br>avg %{y:.2f}%<br>win rate %{customdata:.0f}%<extra></extra>",
        )
    )
    fig.update_layout(
        **plotly_dark_layout(
            title=f"{stats['ticker']} — average return by month "
                  f"({stats.get('years_of_data', 0)} years)",
            height=360,
            yaxis=dict(title="Avg return %", gridcolor="#1e2740"),
        )
    )
    st.plotly_chart(fig, use_container_width=True)
    explainer(
        "**One bar per calendar month** — the average return this ticker posted in "
        "that month across every year of history. Green bars are months it typically "
        "gained, red bars months it typically lost. The **yellow bar is the month "
        "ahead**, so that is the one that matters right now.\n\n"
        "Hover a bar to see its **win rate** — the share of years that month was "
        "actually positive. That is the honest measure of reliability: the bar "
        "height tells you how big the move was on average, the win rate tells you "
        "how often it showed up at all. Roughly: above 65% is a pattern worth "
        "noting, 45–55% is a coin flip regardless of how tall the bar looks.\n\n"
        "**Do not trade on this alone.** With 20 years of history each month only "
        "has 20 observations, which is a small sample. Treat it as a tiebreaker "
        "when the macro regime and momentum already point the same way."
    )


def _render_regime_history_chart(history: list) -> None:
    if len(history) < 2:
        st.caption(
            "Regime scores are snapshotted once per day — revisit this page over "
            "several days to build the trend line."
        )
        return

    dates = [row["snapshot_date"] for row in history]
    fig = go.Figure()
    for key, label, color in (
        ("risk_score", "Risk appetite", "#4f8ef7"),
        ("rate_score", "Rates (easing +)", "#00c853"),
        ("growth_score", "Growth", "#ffd600"),
        ("inflation_score", "Inflation", "#ff6d00"),
    ):
        fig.add_trace(go.Scatter(
            x=dates,
            y=[row.get(key) for row in history],
            name=label,
            mode="lines+markers",
            line=dict(color=color, width=2),
        ))
    fig.add_hline(y=0, line_color="#2a3a60", line_width=1)
    fig.update_layout(
        **plotly_dark_layout(
            title="Regime scores over time",
            height=340,
            yaxis=dict(title="Score", range=[-100, 100], gridcolor="#1e2740"),
        )
    )
    st.plotly_chart(fig, use_container_width=True)
    explainer(
        "**Each line is one of the four regime scores, tracked day by day.** The page "
        "saves a snapshot every time you open it, so this chart fills in over time "
        "rather than backfilling history.\n\n"
        "**What you are looking for is the slope, not the level.** A line crossing "
        "the zero midline is the meaningful event — risk appetite turning negative, "
        "or the rate line flipping from tightening to easing, is the kind of shift "
        "that changes which sectors work. Lines drifting sideways mean the regime is "
        "stable and last week's positioning still applies.\n\n"
        "Two of these moving together is a stronger signal than one moving alone: "
        "risk appetite and growth both rolling over at once is a genuine risk-off "
        "turn, whereas either one alone is usually noise."
    )


def _render_exposure_chart(exposure: dict) -> None:
    sectors = exposure.get("sectors") or []
    if not sectors:
        st.info("No classified sector exposure to chart.")
        return

    # Plotly stacks the first y entry at the bottom, so sort descending to put
    # the biggest underweights — the actionable end — at the top.
    ordered = sorted(sectors, key=lambda row: row["gap"], reverse=True)
    names = [row["sector"] for row in ordered]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=names,
        x=[row["weight"] for row in ordered],
        name="Your weight",
        orientation="h",
        marker_color="#4f8ef7",
        hovertemplate="%{y}<br>your weight %{x:.2f}%<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        y=names,
        x=[row["benchmark_weight"] for row in ordered],
        name="S&P 500 weight",
        mode="markers",
        marker=dict(color="#ffd600", symbol="diamond", size=11),
        hovertemplate="%{y}<br>S&P weight %{x:.2f}%<extra></extra>",
    ))
    fig.update_layout(
        **plotly_dark_layout(
            title="Sector exposure vs the S&P 500",
            height=max(360, 32 * len(names) + 120),
            barmode="overlay",
            xaxis=dict(title="Weight %", gridcolor="#1e2740"),
            yaxis=dict(gridcolor="#1e2740"),
        )
    )
    st.plotly_chart(fig, use_container_width=True)
    explainer(
        "**Blue bar = what percent of your money is in that sector. Yellow diamond = "
        "what percent the S&P 500 holds.** The gap between them is your active bet.\n\n"
        "- **Bar sticks out well past the diamond** — you are overweight. You are "
        "making a concentrated bet on that sector, whether you meant to or not.\n"
        "- **Diamond sits far to the right of the bar** — you are underweight, and "
        "this is where the obvious candidates to add live.\n"
        "- **Bar and diamond roughly level** — you are market-weight there and it is "
        "not driving your results either way.\n\n"
        "Sectors are sorted with the **biggest underweights at the top**, since those "
        "are the actionable end.\n\n"
        "**This is not a scorecard to flatten.** Matching the index everywhere just "
        "recreates an index fund with extra steps — deliberate overweights are the "
        "whole point of picking stocks. What this chart is good for is catching the "
        "bets you did *not* mean to make, and spotting whole sectors you have zero "
        "exposure to. The S&P weights are a fixed approximation and drift slowly, so "
        "read gaps of a point or two as noise."
    )


# ---------------------------------------------------------------------------
# Tab renderers
# ---------------------------------------------------------------------------

def _render_outlook_tab(payload: dict, tickers: list) -> None:
    regime = payload.get("regime", {}) or {}
    month_ahead = payload.get("month_ahead", "")

    section_header("Where we are")

    cols = st.columns(4)
    cols[0].metric("Regime", regime.get("label", "Unknown"))
    cols[1].metric("Risk appetite", f"{regime.get('risk_score', 0):+.0f}")
    cols[2].metric("Rates (easing +)", f"{regime.get('rate_score', 0):+.0f}")
    cols[3].metric("Inflation pressure", f"{regime.get('inflation_score', 0):+.0f}")

    explainer(
        "**Regime** is a plain-English label for what kind of market we are in right "
        "now. The three numbers beside it are scores from **-100 to +100**, each "
        "built from how a basket of prices has been behaving over the last few "
        "months. Zero means no clear signal.\n\n"
        "- **Risk appetite** — positive means money is flowing into riskier assets "
        "(stocks over bonds, small caps over large). Negative means money is hiding.\n"
        "- **Rates (easing +)** — positive means the bond market expects rates to "
        "come down, which usually helps long-duration growth stocks. Negative means "
        "tightening.\n"
        "- **Inflation pressure** — positive means reflation (commodities, energy, "
        "materials tend to do better). Negative means disinflation.\n\n"
        "**What to do with it:** treat roughly **±40 or more as a real tilt** and "
        "anything inside ±20 as noise. At +40 risk appetite you can lean into the "
        "sectors the model favours below; at -40 the sensible response is usually to "
        "do less — fewer new positions, smaller ones — rather than to bet the other "
        "way. These are descriptions of the current weather, not forecasts."
    )

    previous = get_previous_regime()
    if previous and previous.get("regime_label") and previous["regime_label"] != regime.get("label"):
        st.warning(
            f"**Regime shift** — was *{previous['regime_label']}* on "
            f"{previous['snapshot_date']}, now *{regime.get('label')}*."
        )

    windows = payload.get("active_seasonal_windows") or []
    if windows:
        st.markdown("")
        for window in windows:
            st.info(f"**{window['window']}** (through {window['ends']}) — {window['note']}")

    bench = payload.get("benchmark_seasonality", {}) or {}
    bench_month = (bench.get("months") or {}).get(month_ahead)
    if bench_month:
        st.markdown(
            f"**{month_ahead} for {BENCHMARK}** over {bench.get('years_of_data', 0)} years: "
            f"average {_signed_html(bench_month['avg'])}, median {_signed_html(bench_month['median'])}, "
            f"positive **{bench_month['win_rate']:.0f}%** of the time "
            f"(best {bench_month['best']:+.1f}%, worst {bench_month['worst']:+.1f}%).",
            unsafe_allow_html=True,
        )

    top_adds = [row for row in (payload.get("scorecard") or []) if row["total_score"] > 10][:4]
    if top_adds:
        st.markdown("")
        section_header("Sectors the model wants more of")
        for row in top_adds:
            # Inside a raw HTML block Streamlit does not process markdown, so
            # emphasis has to be written as tags.
            exposure_note = (
                f"you hold <b>{row['weight']:.1f}%</b> vs S&amp;P <b>{row['benchmark_weight']:.1f}%</b>"
                if row["weight"] else "<b>you hold none of this sector</b>"
            )
            st.markdown(
                f"<div style='border-left:3px solid {_color_for(row['action'])};"
                f"padding:8px 14px;margin-bottom:8px;background:#161b27;border-radius:6px;'>"
                f"<b>{row['sector']}</b> ({row['etf']}) · score <b>{row['total_score']:.0f}</b> · "
                f"{row['action']}<br>"
                f"<span style='color:{_MUTED};font-size:0.88rem'>{row['rationale']}. "
                f"Today {exposure_note}.</span></div>",
                unsafe_allow_html=True,
            )

        explainer(
            "**Each card is one sector the model scored above +10** for the month "
            "ahead. The **score** is a single blended number out of roughly ±100 that "
            "mixes four things: how that sector usually performs in this calendar "
            "month, how it has been performing against the S&P recently, whether it "
            "fits the current macro regime, and how far your own holdings sit below "
            "the S&P's weight in it.\n\n"
            "The bold word after the score (**add**, **overweight**, **hold** and so "
            "on) is the suggested direction, and the coloured left edge matches it — "
            "green for add, blue for hold, orange/red for trim. The grey line "
            "underneath tells you which of the four ingredients did the work, plus "
            "what you currently hold versus the S&P.\n\n"
            "**The caveat:** a high score often just means you own none of it. That "
            "is a diversification argument, not a prediction. Check the full "
            "scorecard in the Sector Rotation tab to see which component is driving "
            "the number before acting on it."
        )

    st.markdown("---")
    _render_ai_section(payload, tickers)


def _render_ai_section(payload: dict, tickers: list) -> None:
    section_header("Strategist Analysis")
    st.caption(
        "Gemini 2.5 Pro reads every number on this page — regime, seasonality, "
        "momentum, your exposure, and the catalyst calendar — and returns an "
        "action plan. Results cached 12 hours."
    )

    stats = get_today_stats()
    pro_used = stats.get("pro", 0) if isinstance(stats, dict) else 0
    quota_left = max(0, PRO_DAILY_LIMIT - pro_used)

    run_col, hint_col = st.columns([2, 8])
    with run_col:
        run_clicked = st.button(
            "▶ Run Regime Strategist",
            use_container_width=True,
            disabled=quota_left <= 0,
            key="seasonality_run_btn",
        )
    with hint_col:
        st.caption(
            f"Uses Gemini 2.5 Pro · ~60–240s · {quota_left}/{PRO_DAILY_LIMIT} Pro calls left today"
        )

    if "seasonality_analysis" not in st.session_state:
        cached = get_latest_analysis(tickers)
        st.session_state.seasonality_analysis = (
            {**cached, "from_cache": True} if cached else None
        )

    if run_clicked:
        cached = get_latest_analysis(tickers)
        if cached is not None:
            st.session_state.seasonality_analysis = {**cached, "from_cache": True}
        else:
            with st.spinner("Gemini 2.5 Pro building the regime playbook…"):
                result = run_seasonality_analysis(payload)
            if result and "_error" not in result:
                # Keep the DataFrames out of the stored inputs blob.
                serialisable = {
                    key: value for key, value in payload.items()
                    if not isinstance(value, pd.DataFrame)
                }
                save_analysis(tickers, result, serialisable)
            st.session_state.seasonality_analysis = {**(result or {}), "from_cache": False}

    analysis = st.session_state.get("seasonality_analysis")
    if not analysis:
        st.info("Run the strategist to get a written playbook for the months ahead.")
        return

    if "_error" in analysis:
        st.error(f"Analysis failed: {analysis['_error']}")
        return

    if analysis.get("from_cache"):
        st.caption(f"Cached analysis from {analysis.get('analyzed_at', 'earlier')} UTC.")

    st.markdown(f"### {analysis.get('regime_label', 'Regime')}")
    confidence = (analysis.get("confidence") or "").lower()
    if confidence:
        st.caption(f"Confidence: **{confidence}**")
    st.write(analysis.get("regime_summary", ""))

    left, right = st.columns(2)
    with left:
        st.markdown(f"**Seasonal outlook — {analysis.get('month_ahead', '')}**")
        st.write(analysis.get("seasonal_outlook", ""))
    with right:
        st.markdown("**Macro outlook**")
        st.write(analysis.get("macro_outlook", ""))

    position_actions = analysis.get("position_actions") or []
    if position_actions:
        st.markdown("---")
        st.markdown("#### What to do with what you own")
        for item in position_actions:
            action = item.get("action", "")
            st.markdown(
                f"<div style='border-left:3px solid {_color_for(action)};padding:8px 14px;"
                f"margin-bottom:8px;background:#161b27;border-radius:6px;'>"
                f"<b>{item.get('ticker', '')}</b> — <b style='color:{_color_for(action)}'>"
                f"{action.upper()}</b> "
                f"<span style='color:{_MUTED};font-size:0.82rem'>({item.get('urgency', '')})</span>"
                f"<br><span style='font-size:0.9rem'>{item.get('rationale', '')}</span></div>",
                unsafe_allow_html=True,
            )

    sector_actions = analysis.get("sector_actions") or []
    if sector_actions:
        st.markdown("#### Sectors to add or cut")
        for item in sector_actions:
            action = item.get("action", "")
            st.markdown(
                f"<div style='border-left:3px solid {_color_for(action)};padding:8px 14px;"
                f"margin-bottom:8px;background:#161b27;border-radius:6px;'>"
                f"<b>{item.get('sector', '')}</b> ({item.get('etf', '')}) — "
                f"<b style='color:{_color_for(action)}'>{action.upper()}</b> "
                f"<span style='color:{_MUTED};font-size:0.82rem'>"
                f"(conviction: {item.get('conviction', '')})</span>"
                f"<br><span style='font-size:0.9rem'>{item.get('rationale', '')}</span></div>",
                unsafe_allow_html=True,
            )

    catalyst_watch = analysis.get("catalyst_watch") or []
    if catalyst_watch:
        st.markdown("#### Catalysts the strategist is watching")
        for item in catalyst_watch:
            st.markdown(
                f"- **{item.get('date', '')}** · {item.get('event', '')} — "
                f"{item.get('why_it_matters', '')}"
            )

    risk_factors = analysis.get("risk_factors") or []
    if risk_factors:
        st.markdown("#### What would break this view")
        for item in risk_factors:
            st.markdown(f"- {item}")


def _render_seasonality_tab(payload: dict, tickers: list, lookback: int) -> None:
    month_ahead = payload.get("month_ahead", "")

    section_header("Your holdings by month")
    holdings_matrix = payload.get("holdings_seasonality")
    if isinstance(holdings_matrix, pd.DataFrame) and not holdings_matrix.empty:
        _render_seasonality_heatmap(
            holdings_matrix,
            f"Average monthly return by holding — last {lookback} years",
        )
        if month_ahead in holdings_matrix.columns:
            column = holdings_matrix[month_ahead].dropna().sort_values(ascending=False)
            if not column.empty:
                st.markdown(f"**{month_ahead} ranking of your holdings**")
                rows = "".join(
                    f"<tr><td style='padding:4px 12px'>{ticker}</td>"
                    f"<td style='padding:4px 12px'>{_signed_html(value)}</td></tr>"
                    for ticker, value in column.items()
                )
                st.markdown(
                    f"<table style='font-size:0.9rem'><thead><tr>"
                    f"<th style='padding:4px 12px;text-align:left'>Ticker</th>"
                    f"<th style='padding:4px 12px;text-align:left'>Avg {month_ahead}</th>"
                    f"</tr></thead><tbody>{rows}</tbody></table>",
                    unsafe_allow_html=True,
                )
    else:
        st.info("Add holdings (or type tickers in the sidebar) to see their seasonality.")

    st.markdown("---")
    section_header("Sectors by month")
    sector_matrix = payload.get("sector_seasonality")
    if isinstance(sector_matrix, pd.DataFrame) and not sector_matrix.empty:
        _render_seasonality_heatmap(
            sector_matrix,
            f"Average monthly return by sector ETF — last {lookback} years",
        )
    else:
        st.info("Sector seasonality unavailable — price history could not be loaded.")

    st.markdown("---")
    section_header("Single-ticker profile")
    choices = [BENCHMARK] + tickers + sorted(SECTOR_ETFS.values())
    seen: list = []
    for choice in choices:
        if choice not in seen:
            seen.append(choice)
    selected = st.selectbox("Ticker", seen, key="seasonality_profile_ticker")
    stats = _cached_stats(selected, lookback)
    _render_month_profile(stats, month_ahead)

    months = stats.get("months") or {}
    if months:
        frame = pd.DataFrame.from_dict(months, orient="index")
        frame.index.name = "Month"
        frame = frame.rename(columns={
            "avg": "Avg %", "median": "Median %", "win_rate": "Win rate %",
            "best": "Best %", "worst": "Worst %", "stdev": "Stdev %", "count": "Years",
        })
        st.dataframe(frame, use_container_width=True)

        explainer(
            "**One row per calendar month**, built from every year of history this "
            "ticker has.\n\n"
            "- **Avg %** — the mean return in that month. Easily distorted by one "
            "huge year.\n"
            "- **Median %** — the middle year. If median is far below average, one "
            "or two outliers are inflating the average.\n"
            "- **Win rate %** — the share of years that month finished positive.\n"
            "- **Best / Worst %** — the two extreme years, i.e. the realistic range.\n"
            "- **Stdev %** — how spread out the years were. Big stdev = unreliable.\n"
            "- **Years** — how many observations the row is based on.\n\n"
            "**Read win rate first, average second.** A month averaging +1.0% that "
            "was positive 80% of the time is a far more usable pattern than one "
            "averaging +4.0% that was positive 45% of the time — the second is one "
            "lucky year wearing a disguise. Cross-check Avg against Median and "
            "Stdev: when all three agree the pattern is real; when Best and Worst are "
            "20 points apart, the average is not telling you much.\n\n"
            "Even 30 years gives only 30 observations per month, so treat every row "
            "as a mild tiebreaker, never a standalone reason to trade."
        )

    quarters = stats.get("quarters") or {}
    if quarters:
        cols = st.columns(len(quarters))
        for col, (quarter, data) in zip(cols, quarters.items()):
            col.metric(quarter, f"{data['avg']:+.2f}%", f"{data['win_rate']:.0f}% win rate")

        explainer(
            "**The same history rolled up into three-month blocks.** Q1 is "
            "January–March, Q2 April–June, Q3 July–September, Q4 "
            "October–December. The big number is the average total return across "
            "that quarter; the smaller line below it is the share of years the "
            "quarter finished green.\n\n"
            "Quarters are worth a glance because they are less noisy than single "
            "months — three times as much data goes into each one, so a quarterly "
            "tilt that shows up with a 70%+ win rate is more trustworthy than the "
            "same tilt in one month.\n\n"
            "**Use it as a sanity check on the monthly numbers.** If a month looks "
            "strong but its whole quarter is weak, the monthly result is probably "
            "noise. If the month and the quarter agree, you are looking at something "
            "steadier — a genuine seasonal rhythm rather than a single good year."
        )


def _render_regime_tab(payload: dict) -> None:
    regime = payload.get("regime", {}) or {}

    section_header(f"Regime: {regime.get('label', 'Unknown')}")
    st.caption(f"Computed from cross-asset price behaviour as of {regime.get('as_of', '')}.")

    for key, label, note in (
        ("risk_score", "Risk appetite", "Risk-off ← → Risk-on"),
        ("rate_score", "Rate direction", "Tightening ← → Easing"),
        ("growth_score", "Growth", "Contracting ← → Expanding"),
        ("inflation_score", "Inflation", "Disinflation ← → Reflation"),
    ):
        value = regime.get(key, 0.0)
        label_col, bar_col, value_col = st.columns([2, 3, 5])
        label_col.markdown(f"**{label}**")
        bar_col.markdown(_score_bar(value), unsafe_allow_html=True)
        value_col.markdown(
            f"{_signed_html(value, suffix='', digits=0)} "
            f"<span style='color:{_MUTED};font-size:0.82rem'>{note}</span>",
            unsafe_allow_html=True,
        )

    explainer(
        "**Each bar is centred on zero.** The centre line is neutral; the bar grows "
        "to the right for the label on the right of the arrow and to the left for "
        "the label on the left. The number beside it is the same value on a **-100 "
        "to +100** scale, so a bar filling half of one side is roughly ±50.\n\n"
        "- **Risk appetite** — right = risk-on, money moving into stocks and "
        "riskier corners of the market. Left = risk-off, money moving to safety.\n"
        "- **Rate direction** — right = easing expected (helps growth and long-dated "
        "assets). Left = tightening.\n"
        "- **Growth** — right = the economy is expanding on these price signals. "
        "Left = contracting.\n"
        "- **Inflation** — right = reflation (energy, materials, commodities). "
        "Left = disinflation.\n\n"
        "**How far is far?** Past about ±40 the tilt is worth acting on. Inside ±20, "
        "treat it as flat. Two bars pointing the same way (say risk-on plus easing) "
        "is a much stronger read than one big bar on its own. All four are derived "
        "from recent price behaviour across asset classes — they describe what "
        "markets are currently doing, not what they will do next."
    )

    favored = regime.get("favored_sectors") or []
    pressured = regime.get("pressured_sectors") or []
    if favored or pressured:
        st.markdown("")
        left, right = st.columns(2)
        left.success("**Regime favours:** " + (", ".join(favored) or "nothing clearly"))
        right.error("**Regime pressures:** " + (", ".join(pressured) or "nothing clearly"))

        explainer(
            "**These two boxes are a lookup, not a forecast.** The four scores above "
            "get mapped to the sectors that have historically behaved well or badly "
            "in that kind of environment — for example, easing rates have tended to "
            "favour technology and real estate, while reflation has tended to favour "
            "energy and materials.\n\n"
            "Green is the tailwind list, red is the headwind list. If a box says "
            "\"nothing clearly\", the scores are too close to zero to point anywhere, "
            "which is itself useful information: it means sector selection is "
            "unlikely to be where your returns come from this month.\n\n"
            "**Do not treat the red list as a sell list.** A headwind is a reason to "
            "avoid *adding*, and at most a reason to trim a position you already "
            "doubt. Regimes also flip faster than portfolios should."
        )

    st.markdown("---")
    section_header("Indicator detail")
    indicators = regime.get("indicators") or {}
    if indicators:
        rows = []
        for name, data in indicators.items():
            rows.append({
                "Indicator": name,
                "Level": data.get("value"),
                "1m change %": data.get("change_1m"),
                "1y percentile": data.get("percentile_1y"),
                "What it tells you": data.get("note", ""),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        explainer(
            "**This is the raw material the four scores are built from.** Each row is "
            "one market-based indicator — things like credit spreads, the shape of "
            "the yield curve, commodity prices, or the ratio of one index to "
            "another.\n\n"
            "- **Level** — the current reading, in that indicator's own units.\n"
            "- **1m change %** — how much it has moved in the last month. Direction "
            "usually matters more than the level.\n"
            "- **1y percentile** — where the current level sits against the last "
            "year of readings. 90 means it is near a one-year high, 10 near a "
            "one-year low. This is the quickest way to see whether something is "
            "actually unusual.\n"
            "- **What it tells you** — a one-line note saying which direction is "
            "bullish for stocks, because it is not the same for every row. For some "
            "indicators (risk-on ratios) higher is bullish; for others (volatility, "
            "credit spreads) higher is bearish. Always read this column before "
            "judging a number.\n\n"
            "Expect rows to disagree. One contradictory indicator is normal; several "
            "moving the same way at once is what actually shifts the regime."
        )
    else:
        st.info("Indicator data unavailable — price history could not be loaded.")

    st.markdown("---")
    section_header("Regime history")
    _render_regime_history_chart(get_regime_history(180))


def _render_rotation_tab(payload: dict) -> None:
    exposure = payload.get("exposure", {}) or {}
    scorecard = payload.get("scorecard") or []
    momentum = payload.get("sector_momentum") or []

    section_header("Where your money actually is")
    if exposure.get("total_value"):
        st.caption(f"Total classified value: ${exposure['total_value']:,.2f}")
    _render_exposure_chart(exposure)

    if exposure.get("unclassified"):
        st.caption(
            "Not classified into a sector (ETFs, cash, or missing data): "
            + ", ".join(exposure["unclassified"])
        )

    st.markdown("---")
    section_header(f"Rotation scorecard — {payload.get('month_ahead', '')}")
    st.caption(
        "Score blends the seasonal edge for the month ahead (25%), relative "
        "strength vs SPY (30%), fit with the current macro regime (25%), and how "
        "underweight you already are vs the S&P 500 (20%)."
    )

    if scorecard:
        frame = pd.DataFrame([{
            "Sector": row["sector"],
            "ETF": row["etf"],
            "Score": row["total_score"],
            "Action": row["action"],
            "Your wt %": row["weight"],
            "S&P wt %": row["benchmark_weight"],
            "Gap pts": row["gap"],
            f"Avg {payload.get('month_ahead', '')} %": row["seasonal_avg"],
            "RS vs SPY": row["rs_score"],
            "Regime fit": row["regime_fit"],
        } for row in scorecard])
        st.dataframe(frame, use_container_width=True, hide_index=True)

        explainer(
            "**One row per sector, ranked by a single blended Score.** The Score is "
            "four ingredients weighted together: the seasonal edge for the month "
            "ahead (**25%**), relative strength versus SPY (**30%**), fit with the "
            "current macro regime (**25%**), and how underweight you already are "
            "versus the S&P 500 (**20%**). Everything is on a roughly ±100 scale.\n\n"
            "- **Your wt % / S&P wt %** — your share of that sector versus the "
            "index's share. **Gap pts** is the difference; negative means you hold "
            "less than the index.\n"
            "- **Avg [month] %** — the sector ETF's average return in that month "
            "historically.\n"
            "- **RS vs SPY** — recent performance against the S&P, scored.\n"
            "- **Regime fit** — how well the sector matches the four macro scores.\n\n"
            "**Action words**, strongest to weakest: *add* and *overweight* mean buy "
            "more; *accumulate on weakness* means only on a dip; *hold* and *neutral* "
            "mean do nothing; *underweight*, *trim* and *hedge* mean reduce; *exit* "
            "and *avoid* are the strongest negative.\n\n"
            "Because underweight counts for 20%, an empty sector can score well "
            "purely for being empty. Read the per-sector reasons underneath to see "
            "which ingredient actually earned the score."
        )

        st.markdown("**Why each sector scores what it does**")
        for row in scorecard:
            st.markdown(
                f"- **{row['sector']}** ({row['etf']}) · {row['total_score']:.0f} · "
                f"*{row['action']}* — {row['rationale']}."
            )
    else:
        st.info("Scorecard unavailable — sector price history could not be loaded.")

    st.markdown("---")
    section_header("Sector relative strength")
    if momentum:
        frame = pd.DataFrame([{
            "Sector": row["sector"],
            "ETF": row["etf"],
            "1m %": row["ret_1m"],
            "3m %": row["ret_3m"],
            "6m %": row["ret_6m"],
            "12m %": row["ret_12m"],
            "vs SPY 1m": row["rs_1m"],
            "vs SPY 3m": row["rs_3m"],
            "vs SPY 6m": row["rs_6m"],
            "Blended RS": row["rs_score"],
            "Above 200DMA": row["above_200dma"],
        } for row in momentum])
        st.dataframe(frame, use_container_width=True, hide_index=True)

        explainer(
            "**Left half: raw returns. Right half: returns after subtracting the "
            "market.**\n\n"
            "The **1m / 3m / 6m / 12m %** columns are what the sector ETF actually "
            "did over those windows. The **vs SPY** columns take the same windows "
            "and subtract SPY's return, so +3.0 means the sector beat the S&P by "
            "three percentage points. That is the more useful set: a sector up 8% in "
            "a market up 10% is a laggard, even though 8% looks fine on its own.\n\n"
            "**Blended RS** rolls the vs-SPY columns into one number, weighted "
            "towards the more recent windows. **Above 200DMA** is a yes/no trend "
            "filter — whether the ETF is trading above its 200-day average price, "
            "which is the crudest possible \"is this in an uptrend\" test.\n\n"
            "**Reading it:** strong short windows with weak long windows is a turn "
            "that may not stick; all four positive plus above the 200DMA is a "
            "durable leader. Momentum tends to persist over months and then reverse "
            "sharply, so a sector at the very top of this table is not automatically "
            "the best thing to buy today."
        )
    else:
        st.info("Momentum data unavailable.")


def _render_calendar_tab(payload: dict) -> None:
    catalysts = payload.get("catalysts") or []

    section_header("Next 60 days")
    if not catalysts:
        st.info("No scheduled catalysts found in the window.")
        return

    st.caption(
        "FOMC dates are the published Fed calendar. CPI and payrolls follow the "
        "usual monthly cadence (payrolls the first Friday, CPI the following "
        "week) and can shift by a day or two. Earnings dates come from yfinance."
    )

    kinds = sorted({event["kind"] for event in catalysts})
    selected = st.multiselect(
        "Show",
        kinds,
        default=kinds,
        format_func=lambda kind: f"{_KIND_ICONS.get(kind, '•')} {kind}",
        key="seasonality_catalyst_kinds",
    )

    explainer(
        "**Every scheduled event in the next 60 days that tends to move prices**, "
        "newest first. Use the filter above to hide kinds you do not care about.\n\n"
        "- 🏛️ **fed** — Federal Reserve meetings and decisions. These set interest "
        "rate expectations and move the whole market at once.\n"
        "- 📊 **macro** — scheduled economic data such as inflation (CPI) and jobs "
        "(payrolls). Also market-wide, and the reaction usually comes from the gap "
        "between the number and what was expected.\n"
        "- 📄 **earnings** — a single company reporting results. Moves that stock, "
        "and sometimes its sector.\n"
        "- 🔔 **market** — structural dates such as option expiries and quarter ends, "
        "which can cause flow-driven moves with no news attached.\n\n"
        "**The coloured left border is a countdown, not a judgement.** Orange means "
        "**7 days or fewer**, yellow means **within 21 days**, blue means further "
        "out. Nothing here says whether an event is good or bad — the point is to "
        "know it is coming. The usual practical response to an orange bar is to "
        "avoid adding risk into it, since events are precisely when the market "
        "stops following the slower patterns on the rest of this page."
    )

    for event in catalysts:
        if event["kind"] not in selected:
            continue
        urgency_color = (
            "#ff6d00" if event["days_out"] <= 7
            else "#ffd600" if event["days_out"] <= 21
            else "#4f8ef7"
        )
        st.markdown(
            f"<div style='border-left:3px solid {urgency_color};padding:8px 14px;"
            f"margin-bottom:6px;background:#161b27;border-radius:6px;'>"
            f"{_KIND_ICONS.get(event['kind'], '•')} <b>{event['event']}</b> · "
            f"{event['date']} "
            f"<span style='color:{_MUTED};font-size:0.82rem'>"
            f"(in {event['days_out']} days)</span><br>"
            f"<span style='color:{_MUTED};font-size:0.88rem'>{event['detail']}</span></div>",
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Sidebar + page entry
# ---------------------------------------------------------------------------

def _render_sidebar() -> None:
    with st.sidebar:
        st.markdown("---")
        st.markdown("**Seasonality settings**")
        st.selectbox(
            "Lookback",
            list(_LOOKBACK_OPTIONS.keys()),
            index=list(_LOOKBACK_OPTIONS.keys()).index(_DEFAULT_LOOKBACK),
            key="seasonality_lookback_label",
            help="How far back the monthly return statistics reach.",
        )
        st.text_input(
            "Tickers (overrides Webull)",
            key="seasonality_manual_tickers",
            placeholder="AAPL MSFT XOM",
            help="Leave blank to use your live Webull positions. Manual tickers "
                 "are weighted equally.",
        )
        if st.button("↻ Recompute", use_container_width=True, key="seasonality_refresh"):
            st.cache_data.clear()
            st.session_state.pop("seasonality_analysis", None)
            st.rerun()


def _render_main() -> None:
    page_header(
        "Seasonality & Regime",
        "What is coming — the calendar, the macro regime, and how the portfolio "
        "should tilt to meet it.",
    )

    lookback = _LOOKBACK_OPTIONS.get(
        st.session_state.get("seasonality_lookback_label", _DEFAULT_LOOKBACK),
        DEFAULT_LOOKBACK_YEARS,
    )

    positions = _load_positions()
    tickers = []
    for position in positions:
        symbol = str(position.get("symbol", "")).upper().strip()
        if symbol and symbol not in tickers:
            tickers.append(symbol)

    if not positions:
        st.warning(
            "No positions loaded. Connect Webull, or type tickers in the sidebar "
            "to analyse a hypothetical portfolio. Market-wide seasonality and "
            "regime data below still work without positions."
        )

    with st.spinner("Computing seasonality, regime, momentum, and catalysts…"):
        payload = _cached_payload(tuple(sorted(tickers)), lookback, positions)

    # One regime snapshot per day so shifts become visible over time.
    regime = payload.get("regime") or {}
    if regime.get("label") and regime["label"] != "Unknown":
        save_regime_snapshot(regime)

    outlook_tab, seasonality_tab, regime_tab, rotation_tab, calendar_tab = st.tabs([
        "Outlook", "Seasonality", "Macro Regime", "Sector Rotation", "Calendar",
    ])

    with outlook_tab:
        _render_outlook_tab(payload, tickers)
    with seasonality_tab:
        _render_seasonality_tab(payload, tickers, lookback)
    with regime_tab:
        _render_regime_tab(payload)
    with rotation_tab:
        _render_rotation_tab(payload)
    with calendar_tab:
        _render_calendar_tab(payload)


_render_sidebar()
_render_main()
