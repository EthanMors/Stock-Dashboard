import streamlit as st
import pandas as pd
import yfinance as yf
from plotly.subplots import make_subplots
import plotly.graph_objects as go

from components.gemini_usage_bar import render_gemini_usage_bar
from analytics.patterns import DetectedPattern, PatternDetectionEngine

st.set_page_config(page_title="Technical Analysis", page_icon="📈", layout="wide")
render_gemini_usage_bar()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PERIOD_CONFIG: dict[str, tuple[str, str]] = {
    "1D":  ("1d",  "5m"),
    "5D":  ("5d",  "15m"),
    "1M":  ("1mo", "1d"),
    "3M":  ("3mo", "1d"),
    "6M":  ("6mo", "1d"),
    "1Y":  ("1y",  "1d"),
    "2Y":  ("2y",  "1wk"),
    "5Y":  ("5y",  "1wk"),
}

_EMA_PALETTE = [
    "#FF6B6B",  # coral
    "#4ECDC4",  # teal
    "#45B7D1",  # sky blue
    "#FFEAA7",  # yellow
    "#A29BFE",  # lavender
    "#FD79A8",  # pink
    "#55EFC4",  # mint
    "#FDCB6E",  # orange-yellow
]

_UP   = "#26a69a"
_DOWN = "#ef5350"

# Pattern detection UI constants
_PATTERN_LABELS = {
    "bos_bullish": "BOS ↑", "bos_bearish": "BOS ↓",
    "choch_bullish": "CHoCH ↑", "choch_bearish": "CHoCH ↓",
    "breakout_resistance": "Resistance Break ↑", "breakout_support": "Support Break ↓",
    "bull_flag": "Bull Flag", "bear_flag": "Bear Flag",
    "pennant_bull": "Bull Pennant", "pennant_bear": "Bear Pennant",
    "asc_triangle": "Asc. Triangle", "desc_triangle": "Desc. Triangle",
    "sym_triangle": "Sym. Triangle",
    "rising_wedge": "Rising Wedge", "falling_wedge": "Falling Wedge",
    "cup_handle": "Cup & Handle",
    "head_shoulders": "H&S", "inv_head_shoulders": "Inv. H&S",
    "double_top": "Double Top", "double_bottom": "Double Bottom",
    "range_breakout_bull": "Range Break ↑", "range_breakout_bear": "Range Break ↓",
}

_CONFIDENCE_COLORS = {
    "Very High": "#26a69a",
    "High":      "#66bb6a",
    "Moderate":  "#ffa726",
    "Low":       "#ef5350",
    "Very Low":  "#b0bec5",
}

def _confidence_label(score: float) -> str:
    if score >= 0.85: return "Very High"
    if score >= 0.70: return "High"
    if score >= 0.55: return "Moderate"
    if score >= 0.40: return "Low"
    return "Very Low"
_BB_LINE  = "rgba(100, 149, 237, 0.85)"
_BB_FILL  = "rgba(100, 149, 237, 0.07)"
_BB_MID   = "rgba(100, 149, 237, 0.5)"
_GRID     = "#1f2937"
_BG       = "#0e1117"
_PLOT_BG  = "#161b27"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def _fetch_ohlcv(ticker: str, period: str, interval: str) -> pd.DataFrame:
    try:
        return yf.Ticker(ticker.upper()).history(
            period=period, interval=interval, auto_adjust=True
        )
    except Exception:
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Indicator calculations
# ---------------------------------------------------------------------------

def _calc_ema(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean()


def _calc_bollinger(
    close: pd.Series, period: int, std_mult: float
) -> tuple[pd.Series, pd.Series, pd.Series]:
    sma = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    return sma + std_mult * std, sma, sma - std_mult * std


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def _detect_patterns(ticker: str, period: str, interval: str) -> list:
    """Run the full pattern detection pipeline. Cached 5 minutes."""
    try:
        raw = yf.Ticker(ticker.upper()).history(
            period=period, interval=interval, auto_adjust=True
        )
        if raw is None or raw.empty:
            return []
        engine = PatternDetectionEngine(raw, ticker=ticker)
        return engine.detect_all()
    except Exception:
        return []


def _add_pattern_overlays(
    fig: go.Figure,
    patterns: list,
    df: pd.DataFrame,
) -> go.Figure:
    """Add chart annotations for detected patterns (confidence ≥ 0.55)."""
    shown_sl = shown_tp = False
    for p in patterns:
        if p.confidence_score < 0.55:
            continue
        label  = _PATTERN_LABELS.get(p.pattern_type, p.pattern_type)
        color  = _UP if p.direction == "bullish" else _DOWN
        clabel = _confidence_label(p.confidence_score)

        # Annotation pin at the detection bar
        try:
            x_val = p.detected_at_bar
        except Exception:
            x_val = df.index[-1]

        y_val = p.entry_price or float(df["close"].iloc[-1])

        fig.add_annotation(
            x=x_val, y=y_val,
            text=f"<b>{label}</b><br>{p.confidence_score:.2f} {clabel}",
            showarrow=True, arrowhead=2, arrowcolor=color, arrowsize=1,
            ax=0, ay=-40,
            bgcolor=color, opacity=0.85,
            font=dict(color="white", size=9),
            row=1, col=1,
        )

        # Stop loss line (red dashed) — only for the highest-confidence pattern
        if not shown_sl and p.stop_loss:
            fig.add_hline(
                y=p.stop_loss, line_dash="dash",
                line_color="rgba(239,83,80,0.6)", line_width=1,
                annotation_text="SL", annotation_font_size=9,
                annotation_position="right",
                row=1, col=1,
            )
            shown_sl = True

        # Target line (green dashed) — only for the highest-confidence pattern
        if not shown_tp and p.target:
            fig.add_hline(
                y=p.target, line_dash="dash",
                line_color="rgba(38,166,154,0.6)", line_width=1,
                annotation_text="TP", annotation_font_size=9,
                annotation_position="right",
                row=1, col=1,
            )
            shown_tp = True

        if shown_sl and shown_tp:
            break

    return fig


# ---------------------------------------------------------------------------
# Chart builder
# ---------------------------------------------------------------------------

def _build_chart(
    df: pd.DataFrame,
    ticker: str,
    show_emas: bool,
    ema_periods: list[int],
    show_bb: bool,
    bb_period: int,
    bb_std: float,
    patterns: list | None = None,
    show_patterns: bool = False,
) -> go.Figure:
    vol_colors = [
        _UP if c >= o else _DOWN
        for o, c in zip(df["Open"], df["Close"])
    ]

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.02,
        row_heights=[0.78, 0.22],
    )

    # ── Candlesticks ──────────────────────────────────────────────────────
    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df["Open"],
            high=df["High"],
            low=df["Low"],
            close=df["Close"],
            name=ticker.upper(),
            increasing_line_color=_UP,
            decreasing_line_color=_DOWN,
            increasing_fillcolor=_UP,
            decreasing_fillcolor=_DOWN,
            line_width=1,
        ),
        row=1, col=1,
    )

    # ── Bollinger Bands ───────────────────────────────────────────────────
    if show_bb and len(df) >= bb_period:
        upper, mid, lower = _calc_bollinger(df["Close"], bb_period, bb_std)
        bb_label = f"BB({bb_period},{bb_std:.1f})"

        fig.add_trace(go.Scatter(
            x=df.index, y=upper,
            name=f"{bb_label} Upper",
            line=dict(color=_BB_LINE, width=1, dash="dot"),
        ), row=1, col=1)

        fig.add_trace(go.Scatter(
            x=df.index, y=lower,
            name=f"{bb_label} Lower",
            fill="tonexty",
            fillcolor=_BB_FILL,
            line=dict(color=_BB_LINE, width=1, dash="dot"),
        ), row=1, col=1)

        fig.add_trace(go.Scatter(
            x=df.index, y=mid,
            name=f"{bb_label} Mid",
            line=dict(color=_BB_MID, width=1),
        ), row=1, col=1)

    # ── EMAs ──────────────────────────────────────────────────────────────
    if show_emas:
        for i, period in enumerate(sorted(ema_periods)):
            if len(df) >= period:
                fig.add_trace(go.Scatter(
                    x=df.index,
                    y=_calc_ema(df["Close"], period),
                    name=f"EMA {period}",
                    line=dict(color=_EMA_PALETTE[i % len(_EMA_PALETTE)], width=1.5),
                ), row=1, col=1)

    # ── Volume bars ───────────────────────────────────────────────────────
    fig.add_trace(
        go.Bar(
            x=df.index,
            y=df["Volume"],
            name="Volume",
            marker_color=vol_colors,
            showlegend=False,
            opacity=0.75,
        ),
        row=2, col=1,
    )

    # ── Layout ────────────────────────────────────────────────────────────
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=_BG,
        plot_bgcolor=_PLOT_BG,
        height=700,
        margin=dict(l=0, r=60, t=40, b=0),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.01,
            xanchor="left",
            x=0,
            font=dict(size=11),
            bgcolor="rgba(0,0,0,0)",
        ),
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
    )

    fig.update_xaxes(gridcolor=_GRID, showgrid=True, zeroline=False)
    fig.update_yaxes(gridcolor=_GRID, showgrid=True, zeroline=False)
    fig.update_yaxes(side="right", row=1, col=1)
    fig.update_yaxes(side="right", row=2, col=1, title_text="Vol", title_font_size=10)

    if show_patterns and patterns:
        fig = _add_pattern_overlays(fig, patterns, df)

    return fig


# ---------------------------------------------------------------------------
# Sidebar — indicator controls
# ---------------------------------------------------------------------------

def _render_sidebar() -> tuple[bool, bool, int, float, bool]:
    """Returns (show_emas, show_bb, bb_period, bb_std, show_patterns)."""
    with st.sidebar:
        st.header("Indicators")

        # ── EMA ───────────────────────────────────────────────────────────
        show_emas = st.checkbox("Exponential Moving Averages", value=True, key="ta_show_emas")

        if show_emas:
            st.markdown("**Active EMAs**")
            to_remove = []
            for i, p in enumerate(st.session_state.ta_ema_periods):
                color = _EMA_PALETTE[i % len(_EMA_PALETTE)]
                c1, c2 = st.columns([4, 1])
                c1.markdown(
                    f'<span style="color:{color};font-weight:600">━ EMA {p}</span>',
                    unsafe_allow_html=True,
                )
                if c2.button("✕", key=f"ta_rm_ema_{i}", help=f"Remove EMA {p}"):
                    to_remove.append(p)
            for p in to_remove:
                if p in st.session_state.ta_ema_periods:
                    st.session_state.ta_ema_periods.remove(p)
                    st.rerun()

            st.markdown("")
            a1, a2 = st.columns([3, 2])
            new_period = a1.number_input(
                "Add EMA period",
                min_value=2, max_value=500, value=20, step=1,
                key="ta_ema_add_val",
                label_visibility="collapsed",
            )
            if a2.button("＋ Add", key="ta_ema_add_btn", use_container_width=True):
                p = int(new_period)
                if p not in st.session_state.ta_ema_periods:
                    st.session_state.ta_ema_periods.append(p)
                    st.session_state.ta_ema_periods.sort()
                    st.rerun()

        st.markdown("---")

        # ── Bollinger Bands ───────────────────────────────────────────────
        show_bb = st.checkbox("Bollinger Bands", value=True, key="ta_show_bb")
        bb_period, bb_std = 20, 2.0
        if show_bb:
            bb_period = st.number_input(
                "Period", min_value=5, max_value=200, value=20, step=1,
                key="ta_bb_period",
            )
            bb_std = st.number_input(
                "Std Dev multiplier", min_value=0.5, max_value=5.0, value=2.0, step=0.1,
                key="ta_bb_std", format="%.1f",
            )

        st.markdown("---")

        # ── Pattern Detection ──────────────────────────────────────────────
        show_patterns = st.checkbox("Pattern Detection", value=False, key="ta_show_patterns")
        if show_patterns:
            st.caption(
                "Scans for 20+ chart patterns: BOS/CHoCH, flags, triangles, "
                "wedges, H&S, double tops/bottoms, and more. Annotates chart "
                "with detected patterns ≥ 0.55 confidence."
            )

    return show_emas, show_bb, int(bb_period), float(bb_std), show_patterns


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

def _init_state() -> None:
    st.session_state.setdefault("ta_ema_periods", [9, 21, 50, 200])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _init_state()

    st.title("📈 Technical Analysis")
    st.markdown("##### Candlestick · EMAs · Bollinger Bands · Volume · Powered by yfinance")
    st.markdown("---")

    # ── Controls row ──────────────────────────────────────────────────────
    ctrl1, ctrl2 = st.columns([2, 6])
    with ctrl1:
        ticker = st.text_input(
            "Ticker",
            value=st.session_state.get("active_ticker", "AAPL"),
            placeholder="e.g. AAPL",
            key="ta_ticker_input",
        ).upper().strip()
    with ctrl2:
        period_label = st.radio(
            "Period",
            options=list(_PERIOD_CONFIG.keys()),
            index=5,
            horizontal=True,
            key="ta_period_radio",
            label_visibility="collapsed",
        )

    show_emas, show_bb, bb_period, bb_std, show_patterns = _render_sidebar()

    if not ticker:
        st.info("Enter a ticker symbol above.")
        return

    period, interval = _PERIOD_CONFIG[period_label]

    with st.spinner(f"Loading {ticker} · {period_label}…"):
        df = _fetch_ohlcv(ticker, period, interval)

    if df is None or df.empty:
        st.error(f"No data returned for **{ticker}**. Check the symbol and try again.")
        return

    # ── Price summary strip ───────────────────────────────────────────────
    latest = df.iloc[-1]
    prev   = df.iloc[-2] if len(df) > 1 else latest
    pct    = (latest["Close"] - prev["Close"]) / prev["Close"] * 100
    sign   = "+" if pct >= 0 else ""

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Close",  f"${latest['Close']:.2f}", f"{sign}{pct:.2f}%")
    m2.metric("Open",   f"${latest['Open']:.2f}")
    m3.metric("High",   f"${latest['High']:.2f}")
    m4.metric("Low",    f"${latest['Low']:.2f}")
    m5.metric("Volume", f"{int(latest['Volume']):,}")

    # ── Pattern detection (run before chart so overlays are ready) ────────
    patterns: list = []
    if show_patterns:
        with st.spinner("Running pattern detection…"):
            patterns = _detect_patterns(ticker, period, interval)

    # ── Chart ─────────────────────────────────────────────────────────────
    fig = _build_chart(df, ticker, show_emas, st.session_state.ta_ema_periods,
                       show_bb, bb_period, bb_std,
                       patterns=patterns, show_patterns=show_patterns)
    st.plotly_chart(fig, use_container_width=True)

    # ── Footer ────────────────────────────────────────────────────────────
    st.caption(
        f"{len(df):,} bars · {interval} interval · "
        f"{df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')} · "
        "Data via yfinance · Cached 60s"
    )

    # ── Pattern Detection section ─────────────────────────────────────────
    if show_patterns:
        _render_pattern_section(patterns, ticker)


def _render_pattern_section(patterns: list, ticker: str) -> None:
    """Render the Pattern Detection results table below the chart."""
    st.markdown("---")
    st.subheader("🔍 Pattern Detection")

    if not patterns:
        st.info("No patterns detected for the current timeframe. Try a longer period (3M, 6M, 1Y).")
        return

    st.caption(
        f"{len(patterns)} pattern{'s' if len(patterns) != 1 else ''} detected · "
        "Sorted by confidence · Chart annotations show patterns ≥ 0.55"
    )

    rows = []
    for p in patterns:
        label   = _PATTERN_LABELS.get(p.pattern_type, p.pattern_type)
        clabel  = _confidence_label(p.confidence_score)
        color   = _CONFIDENCE_COLORS.get(clabel, "#b0bec5")
        dir_icon = "↑" if p.direction == "bullish" else "↓"
        rr_str  = f"{p.risk_reward_ratio:.1f}:1" if p.risk_reward_ratio else "—"
        entry   = f"${p.entry_price:.2f}"   if p.entry_price  else "—"
        sl_str  = f"${p.stop_loss:.2f}"     if p.stop_loss    else "—"
        tp_str  = f"${p.target:.2f}"        if p.target       else "—"
        vol_str = "✓" if p.volume_confirmed else "—"
        rows.append({
            "Pattern":    label,
            "Dir":        dir_icon,
            "Confidence": f"{p.confidence_score:.2f}",
            "Level":      clabel,
            "Entry":      entry,
            "Stop":       sl_str,
            "Target":     tp_str,
            "R/R":        rr_str,
            "Vol":        vol_str,
            "Notes":      p.notes or "—",
        })

    import pandas as pd
    tbl = pd.DataFrame(rows)
    st.dataframe(
        tbl,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Pattern":    st.column_config.TextColumn("Pattern",    width="medium"),
            "Dir":        st.column_config.TextColumn("Dir",        width="small"),
            "Confidence": st.column_config.TextColumn("Confidence", width="small"),
            "Level":      st.column_config.TextColumn("Level",      width="small"),
            "Entry":      st.column_config.TextColumn("Entry",      width="small"),
            "Stop":       st.column_config.TextColumn("Stop",       width="small"),
            "Target":     st.column_config.TextColumn("Target",     width="small"),
            "R/R":        st.column_config.TextColumn("R/R",        width="small"),
            "Vol":        st.column_config.TextColumn("Vol ✓",      width="small"),
            "Notes":      st.column_config.TextColumn("Notes",      width="large"),
        },
    )

    # Detail expander for highest-confidence pattern
    if patterns:
        top = patterns[0]
        top_label = _PATTERN_LABELS.get(top.pattern_type, top.pattern_type)
        with st.expander(f"Top pattern details — {top_label} ({top.confidence_score:.2f})", expanded=False):
            cols = st.columns(3)
            cols[0].markdown(f"**Type:** {top_label}")
            cols[0].markdown(f"**Direction:** {top.direction.capitalize()}")
            cols[0].markdown(f"**Bars:** {top.pattern_bars}")
            cols[1].markdown(f"**Entry:** {f'${top.entry_price:.2f}' if top.entry_price else '—'}")
            cols[1].markdown(f"**Stop:** {f'${top.stop_loss:.2f}' if top.stop_loss else '—'}")
            cols[1].markdown(f"**Target:** {f'${top.target:.2f}' if top.target else '—'}")
            cols[2].markdown(f"**R/R:** {f'{top.risk_reward_ratio:.1f}:1' if top.risk_reward_ratio else '—'}")
            cols[2].markdown(f"**Vol confirmed:** {'Yes' if top.volume_confirmed else 'No'}")
            cols[2].markdown(f"**Timeframe:** {top.timeframe}")

            if top.key_levels:
                st.markdown("**Key Levels:**")
                kl_cols = st.columns(min(len(top.key_levels), 4))
                for col_w, (k, v) in zip(kl_cols * 10, top.key_levels.items()):
                    col_w.metric(k.replace("_", " ").title(), f"${v:.2f}")

            if top.component_scores:
                st.markdown("**Component Scores:**")
                sc_data = {k.replace("_", " ").title(): [round(v, 2)] for k, v in top.component_scores.items()}
                st.dataframe(pd.DataFrame(sc_data), hide_index=True, use_container_width=True)

            if top.notes:
                st.info(f"Notes: {top.notes}")


main()
