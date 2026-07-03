# pages/10_backtest.py

import json
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import inject_global_css, page_header, render_sidebar_nav
from data.backtest_engine import (
    compute_equity_curve,
    get_run_metrics,
    get_run_outcomes,
    get_run_signals,
    list_recent_runs,
    run_backtest,
)

st.set_page_config(page_title="AI Signal Backtester", layout="wide")

render_gemini_usage_bar()
inject_global_css()
render_sidebar_nav()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALL_SIGNAL_TYPES = [
    "options_ai",
    "news",
    "reddit",
    "hedge_fund",
    "macro",
    "mpt",
    "technical",
    "screener",
]

_SIGNAL_LABELS = {
    "options_ai":  "Options AI",
    "news":        "News Sentiment",
    "reddit":      "Reddit WSB",
    "hedge_fund":  "Hedge Fund",
    "macro":       "Macro News",
    "mpt":         "MPT Analysis",
    "technical":   "Technical Patterns",
    "screener":    "AI Screener Picks",
    "SPY":         "SPY Benchmark",
}

_HORIZON_OPTIONS = {
    "1 day":   1,
    "5 days":  5,
    "10 days": 10,
    "20 days": 20,
}

_EQUITY_COLORS = {
    "options_ai":  "#2196F3",
    "news":        "#4CAF50",
    "reddit":      "#FF9800",
    "hedge_fund":  "#9C27B0",
    "macro":       "#F44336",
    "mpt":         "#00BCD4",
    "technical":   "#8BC34A",
    "screener":    "#E91E63",
    "SPY":         "#607D8B",
}

_MIN_SIGNALS_DISPLAY = 5  # below this count, show "Insufficient data"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_default_tickers() -> list[str]:
    """Try to read portfolio tickers from Webull. Fall back to empty list."""
    try:
        from data.webull_positions import get_env_account_ids, get_positions, is_configured
        if not is_configured():
            return []
        account_ids = get_env_account_ids()
        if not account_ids:
            return []
        positions = get_positions(account_ids[0])
        if not positions or isinstance(positions, dict):
            return []
        _TICKER_FIELDS = ["symbol", "ticker", "tickerSymbol", "stockSymbol", "sym"]
        tickers = []
        for pos in positions:
            for field in _TICKER_FIELDS:
                val = pos.get(field, "")
                if val and isinstance(val, str):
                    tickers.append(val.upper().strip())
                    break
        return list(dict.fromkeys(tickers))  # deduplicate preserving order
    except Exception:
        return []


def _pct(val: float | None) -> str:
    """Format a float as a percentage string."""
    if val is None:
        return "N/A"
    return f"{val * 100:.2f}%"


def _fmt(val: float | None, decimals: int = 4) -> str:
    """Format a float with given decimal places."""
    if val is None:
        return "N/A"
    return f"{val:.{decimals}f}"


def _significant_label(t_stat: float | None) -> str:
    """Return a significance label based on t-statistic."""
    if t_stat is None:
        return "N/A"
    abs_t = abs(t_stat)
    if abs_t >= 2.576:
        return "Yes (99%)"
    elif abs_t >= 1.960:
        return "Yes (95%)"
    elif abs_t >= 1.645:
        return "Marginal (90%)"
    return "No"


# ---------------------------------------------------------------------------
# Cached data fetchers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3600)
def _cached_forward_returns_check(tickers: tuple[str, ...]) -> bool:
    """Lightweight check that yfinance can fetch data for at least one ticker."""
    import yfinance as yf
    for ticker in tickers:
        try:
            df = yf.Ticker(ticker).history(period="5d")
            if df is not None and not df.empty:
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Render functions
# ---------------------------------------------------------------------------

def _render_sidebar() -> dict | None:
    """Render sidebar controls. Returns run_config dict if user clicked Run, else None."""
    with st.sidebar:
        st.header("Backtest Configuration")

        # Tickers input
        default_tickers = _get_default_tickers()
        tickers_input = st.text_input(
            label="Tickers (comma-separated)",
            value=",".join(default_tickers) if default_tickers else "AAPL,MSFT,NVDA,SPY",
            help="Enter one or more uppercase ticker symbols separated by commas.",
            key="bt_tickers_input",
        )
        tickers = [t.strip().upper() for t in tickers_input.split(",") if t.strip()]

        # Date range
        st.subheader("Date Range")
        today = date.today()
        default_from = today - timedelta(days=90)
        date_from = st.date_input(
            label="From",
            value=default_from,
            max_value=today - timedelta(days=2),
            key="bt_date_from",
        )
        date_to = st.date_input(
            label="To",
            value=today - timedelta(days=1),
            min_value=date_from,
            max_value=today - timedelta(days=1),
            key="bt_date_to",
        )

        # Forward horizon
        horizon_label = st.radio(
            label="Forward Horizon",
            options=list(_HORIZON_OPTIONS.keys()),
            index=1,  # default: 5 days
            key="bt_horizon",
            help="Number of trading days after signal date to measure price return.",
        )
        horizon_days = _HORIZON_OPTIONS[horizon_label]

        # Signal type selection
        st.subheader("Signal Types")
        selected_types = []
        for stype in _ALL_SIGNAL_TYPES:
            checked = st.checkbox(
                label=_SIGNAL_LABELS[stype],
                value=True,
                key=f"bt_stype_{stype}",
            )
            if checked:
                selected_types.append(stype)

        st.divider()

        # Recent runs
        st.subheader("Recent Runs")
        recent = list_recent_runs(limit=10)
        if recent:
            run_options = {
                f"{r['created_at'][:16]} | {r['horizon_days']}d | {json.loads(r['tickers'])[:3]}": r["run_id"]
                for r in recent
            }
            selected_label = st.selectbox(
                label="Load a previous run",
                options=["(new run)"] + list(run_options.keys()),
                index=0,
                key="bt_prev_run",
            )
            if selected_label != "(new run)":
                prev_run_id = run_options[selected_label]
                if st.button("Load Run", key="bt_load_run"):
                    st.session_state["bt_current_run_id"] = prev_run_id
                    st.rerun()
        else:
            st.caption("No previous runs yet.")

        st.divider()

        run_clicked = st.button(
            label="Run Backtest",
            type="primary",
            use_container_width=True,
            key="bt_run_button",
            disabled=len(tickers) == 0 or len(selected_types) == 0,
        )

        if run_clicked:
            return {
                "tickers":      tickers,
                "date_from":    date_from,
                "date_to":      date_to,
                "horizon_days": horizon_days,
                "signal_types": selected_types,
            }

    return None


def _render_overview_tab(metrics: list[dict], run_config_display: dict) -> None:
    """Render the Overview tab: summary KPI cards + metrics table."""
    if not metrics:
        st.info("No metrics computed. This may mean fewer than 5 signals were found for all signal types, or no signals exist in the selected date range.")
        return

    # Filter to rows with sufficient data
    valid = [m for m in metrics if not m.get("insufficient", False)]
    insuf = [m for m in metrics if m.get("insufficient", False)]

    if not valid:
        st.warning("All signal types had fewer than 5 signals in the selected window. Broaden the date range or add more signal types.")
        return

    # KPI summary cards
    best_by_ic = max(valid, key=lambda m: m.get("ic", 0.0) or 0.0)
    worst_by_ic = min(valid, key=lambda m: m.get("ic", 0.0) or 0.0)
    best_by_hit = max(valid, key=lambda m: m.get("hit_rate", 0.0) or 0.0)

    all_ics = [m["ic"] for m in valid if m.get("ic") is not None]
    composite_ic = round(float(np.mean(all_ics)), 4) if all_ics else None

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric(
            label="Total Signals",
            value=sum(m.get("signal_count", 0) for m in valid),
        )
    with c2:
        st.metric(
            label="Best IC",
            value=f"{best_by_ic.get('ic', 0):.4f}",
            help=f"Signal type: {_SIGNAL_LABELS.get(best_by_ic['signal_type'], best_by_ic['signal_type'])}",
        )
    with c3:
        st.metric(
            label="Best Hit Rate",
            value=_pct(best_by_hit.get("hit_rate")),
            help=f"Signal type: {_SIGNAL_LABELS.get(best_by_hit['signal_type'], best_by_hit['signal_type'])}",
        )
    with c4:
        st.metric(
            label="Composite IC",
            value=f"{composite_ic:.4f}" if composite_ic is not None else "N/A",
            help="Mean IC across all valid signal types.",
        )

    st.divider()

    # Metrics table
    table_rows = []
    for m in valid:
        label = _SIGNAL_LABELS.get(m["signal_type"], m["signal_type"])
        table_rows.append({
            "Signal Type":       label,
            "N Signals":         m.get("signal_count", 0),
            "Hit Rate":          _pct(m.get("hit_rate")),
            "IC":                _fmt(m.get("ic"), 4),
            "ICIR":              _fmt(m.get("icir"), 4),
            "t-stat":            _fmt(m.get("t_stat"), 2),
            "Significant?":      _significant_label(m.get("t_stat")),
            "Avg Bull Return":   _pct(m.get("avg_return_bull")),
            "Avg Bear Return":   _pct(m.get("avg_return_bear")),
            "Profit Factor":     _fmt(m.get("profit_factor"), 2),
            "Avg Alpha":         _pct(m.get("avg_alpha")),
            "Sharpe":            _fmt(m.get("sharpe"), 2),
            "Max Drawdown":      _pct(m.get("max_drawdown")),
        })

    if insuf:
        for m in insuf:
            label = _SIGNAL_LABELS.get(m["signal_type"], m["signal_type"])
            table_rows.append({
                "Signal Type":      label,
                "N Signals":        m.get("signal_count", 0),
                "Hit Rate":         "Insufficient data",
                "IC":               "Insufficient data",
                "ICIR":             "Insufficient data",
                "t-stat":           "Insufficient data",
                "Significant?":     "Insufficient data",
                "Avg Bull Return":  "Insufficient data",
                "Avg Bear Return":  "Insufficient data",
                "Profit Factor":    "Insufficient data",
                "Avg Alpha":        "Insufficient data",
                "Sharpe":           "Insufficient data",
                "Max Drawdown":     "Insufficient data",
            })

    st.dataframe(
        pd.DataFrame(table_rows),
        use_container_width=True,
        hide_index=True,
    )


def _render_equity_tab(signals_list: list[dict], outcomes_list: list[dict]) -> None:
    """Render the Equity Curves tab."""
    if not signals_list or not outcomes_list:
        st.info("No signal/outcome data to plot equity curves.")
        return

    signals_df  = pd.DataFrame(signals_list)
    outcomes_df = pd.DataFrame(outcomes_list)

    equity_df = compute_equity_curve(signals_df, outcomes_df)

    if equity_df.empty:
        st.info("Not enough directional signals to build equity curves.")
        return

    fig = go.Figure()

    for stype in equity_df["signal_type"].unique():
        curve = equity_df[equity_df["signal_type"] == stype].sort_values("date")
        color = _EQUITY_COLORS.get(stype, "#888888")
        label = _SIGNAL_LABELS.get(stype, stype)
        width = 3 if stype == "SPY" else 1.5
        dash  = "dash" if stype == "SPY" else "solid"

        fig.add_trace(go.Scatter(
            x=curve["date"],
            y=curve["portfolio_value"],
            mode="lines",
            name=label,
            line=dict(color=color, width=width, dash=dash),
            hovertemplate=f"{label}<br>Date: %{{x}}<br>Value: $%{{y:,.2f}}<extra></extra>",
        ))

    fig.update_layout(
        title="Simulated $10,000 Portfolio by Signal Type vs SPY",
        xaxis_title="Date",
        yaxis_title="Portfolio Value ($)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        template="plotly_dark",
        height=500,
    )
    fig.add_hline(y=10000, line_dash="dot", line_color="gray", annotation_text="$10k baseline")

    st.plotly_chart(fig, use_container_width=True)


def _render_heatmap_tab(signals_list: list[dict]) -> None:
    """Render the Signal Heatmap tab: signal_type vs date, colored by direction."""
    if not signals_list:
        st.info("No signals to display in heatmap.")
        return

    df = pd.DataFrame(signals_list)
    df["signal_date"] = pd.to_datetime(df["signal_date"]).dt.date

    # Aggregate: for each (signal_type, date), take the mean score
    pivot = df.groupby(["signal_type", "signal_date"])["score"].mean().unstack(level=1)
    pivot.index = [_SIGNAL_LABELS.get(i, i) for i in pivot.index]

    # Fill NaN with 0 for display
    pivot = pivot.fillna(0.0)

    # Sort columns (dates)
    pivot = pivot[sorted(pivot.columns)]

    z_values = pivot.values.tolist()
    y_labels = pivot.index.tolist()
    x_labels = [str(d) for d in pivot.columns]

    fig = go.Figure(data=go.Heatmap(
        z=z_values,
        x=x_labels,
        y=y_labels,
        colorscale=[
            [0.0,  "#B71C1C"],   # strong bearish: dark red
            [0.45, "#FFCDD2"],   # weak bearish: light red
            [0.50, "#BDBDBD"],   # neutral: gray
            [0.55, "#C8E6C9"],   # weak bullish: light green
            [1.0,  "#1B5E20"],   # strong bullish: dark green
        ],
        zmid=0.0,
        zmin=-1.0,
        zmax=1.0,
        text=[[f"{v:.3f}" for v in row] for row in z_values],
        hovertemplate="Signal Type: %{y}<br>Date: %{x}<br>Score: %{z:.4f}<extra></extra>",
        colorbar=dict(title="Signal Score", tickvals=[-1, -0.5, 0, 0.5, 1]),
    ))

    fig.update_layout(
        title="Signal Scores by Type and Date (green=bullish, red=bearish, gray=neutral)",
        xaxis_title="Date",
        yaxis_title="Signal Type",
        template="plotly_dark",
        height=max(300, len(y_labels) * 60 + 100),
        xaxis=dict(tickangle=-45),
    )

    st.plotly_chart(fig, use_container_width=True)


def _render_breakdown_tab(signals_list: list[dict], outcomes_list: list[dict]) -> None:
    """Render the Signal Breakdown tab: per-signal-type deep dive."""
    if not signals_list or not outcomes_list:
        st.info("No data available for breakdown.")
        return

    signals_df  = pd.DataFrame(signals_list)
    outcomes_df = pd.DataFrame(outcomes_list)

    # outcomes_df may be pre-joined (from get_run_outcomes); keep only outcome columns
    _OC = {"id", "signal_id", "forward_return", "benchmark_return",
           "alpha", "price_at_signal", "price_at_horizon", "correct"}
    outcomes_clean = outcomes_df[[c for c in outcomes_df.columns if c in _OC]]

    # Join
    merged = signals_df.merge(
        outcomes_clean,
        left_on="id",
        right_on="signal_id",
        how="inner",
    )
    merged = merged[merged["forward_return"].notna()].copy()

    if merged.empty:
        st.info("No outcomes with computed forward returns found.")
        return

    available_types = sorted(merged["signal_type"].unique().tolist())
    selected_type = st.selectbox(
        label="Select Signal Type",
        options=available_types,
        format_func=lambda x: _SIGNAL_LABELS.get(x, x),
        key="bt_breakdown_type",
    )

    sdf = merged[merged["signal_type"] == selected_type].copy()
    if len(sdf) < 2:
        st.warning(f"Too few data points for {_SIGNAL_LABELS.get(selected_type, selected_type)}.")
        return

    col1, col2 = st.columns(2)

    with col1:
        # Scatter: signal score vs forward return
        fig_scatter = go.Figure()
        colors = sdf["direction"].map({"bullish": "#4CAF50", "bearish": "#F44336", "neutral": "#9E9E9E"})

        fig_scatter.add_trace(go.Scatter(
            x=sdf["score"],
            y=sdf["forward_return"],
            mode="markers",
            marker=dict(color=colors, size=8, opacity=0.7),
            text=sdf.apply(
                lambda r: f"Ticker: {r['ticker']}<br>Date: {r['signal_date']}<br>Direction: {r['direction']}", axis=1
            ),
            hovertemplate="%{text}<br>Score: %{x:.4f}<br>Return: %{y:.4%}<extra></extra>",
            name="Signals",
        ))

        # Add trend line
        x_vals = sdf["score"].values
        y_vals = sdf["forward_return"].values
        if len(x_vals) >= 2:
            z = np.polyfit(x_vals, y_vals, 1)
            p = np.poly1d(z)
            x_line = np.linspace(x_vals.min(), x_vals.max(), 50)
            fig_scatter.add_trace(go.Scatter(
                x=x_line,
                y=p(x_line),
                mode="lines",
                line=dict(color="#FFD700", width=2, dash="dash"),
                name="Trend",
            ))

        fig_scatter.update_layout(
            title=f"{_SIGNAL_LABELS.get(selected_type, selected_type)}: Score vs Forward Return",
            xaxis_title="Signal Score",
            yaxis_title="Forward Return",
            template="plotly_dark",
            height=400,
        )
        st.plotly_chart(fig_scatter, use_container_width=True)

    with col2:
        # Histogram: forward return distribution by direction
        fig_hist = go.Figure()
        for direction, color in [("bullish", "#4CAF50"), ("bearish", "#F44336"), ("neutral", "#9E9E9E")]:
            subset = sdf[sdf["direction"] == direction]["forward_return"]
            if subset.empty:
                continue
            fig_hist.add_trace(go.Histogram(
                x=subset,
                name=direction.capitalize(),
                marker_color=color,
                opacity=0.7,
                nbinsx=20,
                hovertemplate=f"{direction}: %{{x:.4%}}<br>Count: %{{y}}<extra></extra>",
            ))

        fig_hist.update_layout(
            barmode="overlay",
            title="Forward Return Distribution by Direction",
            xaxis_title="Forward Return",
            yaxis_title="Count",
            template="plotly_dark",
            height=400,
        )
        st.plotly_chart(fig_hist, use_container_width=True)

    st.subheader("Individual Signals")
    display_df = sdf[[
        "ticker", "signal_date", "direction", "score",
        "forward_return", "benchmark_return", "alpha", "correct"
    ]].copy()
    display_df["forward_return"]   = display_df["forward_return"].map(lambda x: f"{x:.4%}" if pd.notna(x) else "N/A")
    display_df["benchmark_return"] = display_df["benchmark_return"].map(lambda x: f"{x:.4%}" if pd.notna(x) else "N/A")
    display_df["alpha"]            = display_df["alpha"].map(lambda x: f"{x:.4%}" if pd.notna(x) else "N/A")
    display_df["correct"]          = display_df["correct"].map(lambda x: "Hit" if x == 1 else ("Miss" if x == 0 else "Neutral"))
    display_df.columns = ["Ticker", "Signal Date", "Direction", "Score", "Forward Return", "SPY Return", "Alpha", "Outcome"]

    st.dataframe(display_df, use_container_width=True, hide_index=True)


def _render_composite_tab(signals_list: list[dict], outcomes_list: list[dict], metrics: list[dict]) -> None:
    """Render the Composite Signal tab: IC-weighted composite score and equity curve."""
    if not signals_list or not outcomes_list or not metrics:
        st.info("Run a backtest first to see composite signal analysis.")
        return

    valid_metrics = [m for m in metrics if not m.get("insufficient", False) and m.get("ic") is not None]
    if not valid_metrics:
        st.warning("No signal types have valid IC values for weighting.")
        return

    # IC-weighted composite: weight each signal type by its IC (clipped to [0, inf] — negative IC has zero weight)
    ic_weights = {m["signal_type"]: max(0.0, m["ic"]) for m in valid_metrics}
    total_weight = sum(ic_weights.values())

    if total_weight == 0:
        st.warning("All signal ICs are <= 0. No positive IC weights available for composite.")
        return

    normalized_weights = {k: v / total_weight for k, v in ic_weights.items()}

    # Display the weights
    st.subheader("IC Weights for Composite")
    weight_data = [
        {"Signal Type": _SIGNAL_LABELS.get(k, k), "IC": _fmt(m.get("ic"), 4), "Weight": f"{normalized_weights.get(k, 0):.1%}"}
        for m, k in [(m, m["signal_type"]) for m in valid_metrics]
        if k in normalized_weights
    ]
    st.dataframe(pd.DataFrame(weight_data), use_container_width=True, hide_index=True)

    st.divider()

    # Build composite signals: for each (ticker, date), compute weighted score
    signals_df  = pd.DataFrame(signals_list)
    outcomes_df = pd.DataFrame(outcomes_list)

    _OC = {"id", "signal_id", "forward_return", "benchmark_return",
           "alpha", "price_at_signal", "price_at_horizon", "correct"}
    outcomes_clean = outcomes_df[[c for c in outcomes_df.columns if c in _OC]]

    merged = signals_df.merge(
        outcomes_clean,
        left_on="id",
        right_on="signal_id",
        how="inner",
    )
    merged = merged[merged["forward_return"].notna()].copy()
    merged["signal_date"] = pd.to_datetime(merged["signal_date"]).dt.date

    # Only use signal types with positive IC weight
    merged = merged[merged["signal_type"].isin(normalized_weights)].copy()
    if merged.empty:
        st.warning("No signal data for IC-positive signal types.")
        return

    merged["weighted_score"] = merged.apply(
        lambda r: r["score"] * normalized_weights.get(r["signal_type"], 0.0), axis=1
    )

    # Group by (ticker, date): composite score = sum of weighted scores
    composite = (
        merged.groupby(["ticker", "signal_date"])
        .agg(
            composite_score=("weighted_score", "sum"),
            forward_return=("forward_return", "mean"),
            benchmark_return=("benchmark_return", "mean"),
        )
        .reset_index()
    )

    if len(composite) < 3:
        st.warning("Not enough composite data points for IC computation.")
        return

    from scipy.stats import spearmanr
    comp_ic_val, _ = spearmanr(composite["composite_score"], composite["forward_return"])
    comp_ic = float(comp_ic_val) if not np.isnan(comp_ic_val) else 0.0

    composite["direction"] = composite["composite_score"].map(
        lambda s: "bullish" if s > 0.05 else ("bearish" if s < -0.05 else "neutral")
    )
    directions = composite["direction"].tolist()
    fwd_rets   = composite["forward_return"].tolist()

    from data.backtest_engine import compute_hit_rate
    comp_hit = compute_hit_rate(directions, fwd_rets)

    # Sharpe of composite - use inline computation instead of importing _compute_sharpe
    directional_pnls = []
    for d, r in zip(directions, fwd_rets):
        if d == "bullish":
            directional_pnls.append(r)
        elif d == "bearish":
            directional_pnls.append(-r)

    if len(directional_pnls) >= 2:
        arr = np.array(directional_pnls)
        comp_sharpe = float((np.mean(arr) - 0.045/252) / np.std(arr, ddof=1) * np.sqrt(252)) if np.std(arr, ddof=1) > 0 else 0.0
    else:
        comp_sharpe = 0.0

    c1, c2, c3 = st.columns(3)
    c1.metric("Composite IC", f"{comp_ic:.4f}")
    c2.metric("Composite Hit Rate", _pct(comp_hit))
    c3.metric("Composite Sharpe", _fmt(comp_sharpe, 2))

    st.divider()

    # Composite equity curve vs SPY
    composite = composite.sort_values("signal_date")
    _INITIAL_CAPITAL = 10_000.0
    total_trades = len(composite[composite["direction"] != "neutral"])
    if total_trades == 0:
        st.info("No directional composite signals to plot equity curve.")
        return

    trade_size = _INITIAL_CAPITAL / total_trades
    running = _INITIAL_CAPITAL
    spy_running = _INITIAL_CAPITAL
    eq_dates, eq_values, spy_values = [], [], []

    for _, row in composite.iterrows():
        d   = row["direction"]
        fwd = row["forward_return"]
        bm  = row["benchmark_return"]

        if d != "neutral":
            pnl = trade_size * fwd if d == "bullish" else trade_size * (-fwd)
            running += pnl
        spy_running *= (1.0 + (bm if pd.notna(bm) else 0.0))

        eq_dates.append(row["signal_date"])
        eq_values.append(round(running, 4))
        spy_values.append(round(spy_running, 4))

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=eq_dates, y=eq_values,
        mode="lines", name="Composite Signal",
        line=dict(color="#FFD700", width=2),
    ))
    fig.add_trace(go.Scatter(
        x=eq_dates, y=spy_values,
        mode="lines", name="SPY Buy & Hold",
        line=dict(color="#607D8B", width=2, dash="dash"),
    ))
    fig.add_hline(y=10000, line_dash="dot", line_color="gray")
    fig.update_layout(
        title="IC-Weighted Composite Signal vs SPY ($10k Start)",
        xaxis_title="Date",
        yaxis_title="Portfolio Value ($)",
        template="plotly_dark",
        height=400,
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    st.plotly_chart(fig, use_container_width=True)


def _render_export_tab(signals_list: list[dict], outcomes_list: list[dict]) -> None:
    """Render the Data Export tab."""
    if not signals_list or not outcomes_list:
        st.info("Run a backtest first to export data.")
        return

    signals_df  = pd.DataFrame(signals_list)
    outcomes_df = pd.DataFrame(outcomes_list)

    _OC = {"id", "signal_id", "forward_return", "benchmark_return",
           "alpha", "price_at_signal", "price_at_horizon", "correct"}
    outcomes_clean = outcomes_df[[c for c in outcomes_df.columns if c in _OC]]

    merged = signals_df.merge(
        outcomes_clean,
        left_on="id",
        right_on="signal_id",
        how="inner",
    )

    export_df = merged[[
        "ticker", "signal_date", "signal_type", "direction", "score",
        "forward_return", "benchmark_return", "alpha", "correct",
        "price_at_signal", "price_at_horizon", "source_db",
    ]].copy()

    export_df.columns = [
        "Ticker", "Signal Date", "Signal Type", "Direction", "Score",
        "Forward Return", "SPY Return", "Alpha", "Correct (1=Yes, 0=No)",
        "Price at Signal", "Price at Horizon", "Source DB",
    ]

    csv_bytes = export_df.to_csv(index=False).encode("utf-8")

    st.download_button(
        label="Download Signals + Outcomes as CSV",
        data=csv_bytes,
        file_name="backtest_signals_outcomes.csv",
        mime="text/csv",
        use_container_width=True,
    )

    st.dataframe(export_df.head(200), use_container_width=True, hide_index=True)

    if len(export_df) > 200:
        st.caption(f"Showing first 200 of {len(export_df)} rows. Download the CSV for the full dataset.")


def _render_main() -> None:
    """Render the main page area based on current session state."""
    page_header("AI Signal Backtester", "Backtest AI-generated signals against historical price data.")

    run_config = _render_sidebar()

    # If a new run was requested, execute it with a progress spinner
    if run_config is not None:
        with st.spinner("Running backtest... this may take 1-2 minutes for technical pattern replay."):
            try:
                run_id = run_backtest(run_config)
                st.session_state["bt_current_run_id"] = run_id
                st.session_state["bt_last_run_config"] = run_config
                st.success(f"Backtest complete. Run ID: {run_id}")
            except Exception as exc:
                st.error(f"Backtest failed: {exc}")
                return

    current_run_id = st.session_state.get("bt_current_run_id")

    if current_run_id is None:
        st.info("Configure a backtest in the sidebar and click **Run Backtest** to begin.")
        return

    # Load results
    signals_list  = get_run_signals(current_run_id)
    outcomes_list = get_run_outcomes(current_run_id)
    metrics       = get_run_metrics(current_run_id)

    # Display run summary header
    run_config_display = st.session_state.get("bt_last_run_config", {})
    n_signals  = len(signals_list)
    n_outcomes = len(outcomes_list)

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Signals Extracted", n_signals)
    col2.metric("Outcomes Computed", n_outcomes)
    col3.metric("Run ID", current_run_id[:8] + "...")

    # Tabs
    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "Overview",
        "Equity Curves",
        "Signal Heatmap",
        "Signal Breakdown",
        "Composite Signal",
        "Data Export",
    ])

    with tab1:
        _render_overview_tab(metrics, run_config_display)

    with tab2:
        _render_equity_tab(signals_list, outcomes_list)

    with tab3:
        _render_heatmap_tab(signals_list)

    with tab4:
        _render_breakdown_tab(signals_list, outcomes_list)

    with tab5:
        _render_composite_tab(signals_list, outcomes_list, metrics)

    with tab6:
        _render_export_tab(signals_list, outcomes_list)


# ---------------------------------------------------------------------------
# Page entry
# ---------------------------------------------------------------------------

_render_main()
