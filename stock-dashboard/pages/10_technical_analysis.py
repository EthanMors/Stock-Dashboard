import streamlit as st
import pandas as pd
import yfinance as yf
from plotly.subplots import make_subplots
import plotly.graph_objects as go

from components.gemini_usage_bar import render_gemini_usage_bar

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

    return fig


# ---------------------------------------------------------------------------
# Sidebar — indicator controls
# ---------------------------------------------------------------------------

def _render_sidebar() -> tuple[bool, bool, int, float]:
    """Returns (show_emas, show_bb, bb_period, bb_std)."""
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

    return show_emas, show_bb, int(bb_period), float(bb_std)


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

    show_emas, show_bb, bb_period, bb_std = _render_sidebar()

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

    # ── Chart ─────────────────────────────────────────────────────────────
    fig = _build_chart(df, ticker, show_emas, st.session_state.ta_ema_periods,
                       show_bb, bb_period, bb_std)
    st.plotly_chart(fig, use_container_width=True)

    # ── Footer ────────────────────────────────────────────────────────────
    st.caption(
        f"{len(df):,} bars · {interval} interval · "
        f"{df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')} · "
        "Data via yfinance · Cached 60s"
    )


main()
