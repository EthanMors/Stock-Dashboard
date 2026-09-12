# pages/12_screener.py

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import inject_global_css, page_header, render_sidebar_nav, section_header, plotly_dark_layout
from data.screener import run_screener, enrich_with_fundamentals, SECTOR_OPTIONS, _MICRO_CAP_MAX, _SMALL_CAP_MAX
from data.screener_agent import run_risk_analysis, run_batch_risk_analysis, get_cached_analysis
from data.gemini_tracker import get_today_stats, PRO_DAILY_LIMIT
from plotly.subplots import make_subplots
from data.screener_ta import enrich_with_technicals, fetch_ta_for_tickers

st.set_page_config(page_title="Speculative Screener", layout="wide")

render_gemini_usage_bar()
inject_global_css()
render_sidebar_nav()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MIN_PRICE = 0.50
_DEFAULT_MAX_PRICE = 20.0
_DEFAULT_MIN_VOLUME = 100_000
_DEFAULT_MAX_BATCH = 5  # max stocks for batch analysis (Pro quota protection)

_ENRICH_N_OPTIONS = {
    "Top 10":  10,
    "Top 25":  25,
    "Top 50":  50,
}
_DEFAULT_ENRICH_N = "Top 25"

_VERDICT_COLORS = {
    "lottery ticket": "#ffd600",   # amber
    "speculative buy": "#00c853",  # green
    "hold/watch": "#2979ff",       # blue
    "avoid": "#ff1744",            # red
}

_VERDICT_ICONS = {
    "lottery ticket": "?",
    "speculative buy": "++",
    "hold/watch": "~",
    "avoid": "X",
}

# TA chart colour constants (matches 10_technical_analysis.py palette)
_TA_UP        = "#26a69a"
_TA_DOWN      = "#ef5350"
_TA_BB_LINE   = "rgba(100, 149, 237, 0.85)"
_TA_BB_FILL   = "rgba(100, 149, 237, 0.07)"
_TA_BB_MID    = "rgba(100, 149, 237, 0.5)"
_TA_SMA20_CLR = "#ffd600"
_TA_SMA50_CLR = "#ff6b6b"
_TA_MACD_CLR  = "#4f8ef7"
_TA_SIG_CLR   = "#ff9800"
_TA_HIST_POS  = "rgba(38,166,154,0.7)"
_TA_HIST_NEG  = "rgba(239,83,80,0.7)"
_TA_RSI_CLR   = "#a29bfe"
_TA_BG        = "#0e1117"
_TA_PLOT_BG   = "#161b27"
_TA_GRID      = "#1e2740"

_TA_SIGNAL_COLORS = {
    "Bullish": "#00c853",
    "Neutral": "#2979ff",
    "Bearish": "#ff1744",
}

_CAP_TIER_OPTIONS = {
    "Micro-cap (< $300M)": (1_000_000, _MICRO_CAP_MAX),
    "Small-cap (< $2B)":   (1_000_000, _SMALL_CAP_MAX),
    "Micro + Small (< $2B, sorted by score)": (1_000_000, _SMALL_CAP_MAX),
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_market_cap(mc) -> str:
    """Format a market cap value for display in the table."""
    if mc is None or (isinstance(mc, float) and pd.isna(mc)):
        return "N/A"
    try:
        mc = float(mc)
        if mc >= 1e9:
            return f"${mc / 1e9:.2f}B"
        if mc >= 1e6:
            return f"${mc / 1e6:.0f}M"
        return f"${mc:,.0f}"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_pct(val) -> str:
    """Format a decimal fraction as a percentage string (e.g. 0.15 → '+15.0%')."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "N/A"
    try:
        v = float(val)
        sign = "+" if v >= 0 else ""
        return f"{sign}{v:.1f}%"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_price(val) -> str:
    """Format a price value as a USD string."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "N/A"
    try:
        return f"${float(val):.2f}"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_volume(val) -> str:
    """Format a volume number with M/K suffix."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "N/A"
    try:
        v = float(val)
        if v >= 1_000_000:
            return f"{v / 1_000_000:.1f}M"
        if v >= 1_000:
            return f"{v / 1_000:.0f}K"
        return f"{v:.0f}"
    except (TypeError, ValueError):
        return "N/A"


def _score_color(score: int) -> str:
    """Return a hex color for a speculation score 0-100."""
    if score >= 70:
        return "#ff1744"  # high speculation = red warning
    if score >= 45:
        return "#ffd600"  # medium = amber
    return "#00c853"      # lower = green (less risky)


def _risk_score_color(rs: int) -> str:
    """Return a hex color for a Gemini risk score 1-10."""
    if rs >= 8:
        return "#ff1744"
    if rs >= 5:
        return "#ffd600"
    return "#00c853"


def _fmt_runway(years) -> str:
    """Format cash runway years as a human-readable string."""
    if years is None or (isinstance(years, float) and pd.isna(years)):
        return "N/A"
    try:
        y = float(years)
        if y >= 100:
            return "FCF+ (no burn)"
        return f"{y:.1f} yr"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_growth(val) -> str:
    """Format a decimal growth rate as a percentage string (e.g. 0.35 → '+35.0%')."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "N/A"
    try:
        v = float(val)
        sign = "+" if v >= 0 else ""
        return f"{sign}{v * 100:.1f}%"
    except (TypeError, ValueError):
        return "N/A"


def _build_price_chart(ticker: str, period: str = "6mo") -> go.Figure:
    """
    Fetch price history via yfinance and return a Plotly line chart.
    Returns an empty figure with a message if no data is available.
    This is called inside an @st.cache_data wrapper in the page.
    """
    import yfinance as yf
    try:
        hist = yf.Ticker(ticker).history(period=period)
    except Exception:
        hist = None

    if hist is None or hist.empty:
        fig = go.Figure()
        fig.update_layout(
            **plotly_dark_layout(
                annotations=[{
                    "text": f"No price data for {ticker}",
                    "xref": "paper", "yref": "paper",
                    "x": 0.5, "y": 0.5, "showarrow": False,
                    "font": {"size": 14, "color": "#556080"},
                }],
                xaxis_visible=False,
                yaxis_visible=False,
            )
        )
        return fig

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=hist.index,
        y=hist["Close"],
        mode="lines",
        name=ticker,
        line={"color": "#4f8ef7", "width": 2},
        fill="tozeroy",
        fillcolor="rgba(79,142,247,0.08)",
    ))
    fig.update_layout(
        **plotly_dark_layout(
            title=f"{ticker} — 6-Month Price",
            xaxis_title="Date",
            yaxis_title="Price (USD)",
            height=260,
            xaxis_rangeslider_visible=False,
        )
    )
    return fig


def _build_ta_chart(ta_data: dict, ticker: str) -> go.Figure:
    """
    Build a 3-row Plotly chart from a pre-fetched TA data dict:
      Row 1 (60%): Candlestick + SMA20 + SMA50 + Bollinger Bands
      Row 2 (20%): RSI with oversold/overbought reference lines
      Row 3 (20%): MACD line + Signal line + Histogram

    Parameters
    ----------
    ta_data : dict — as returned by _fetch_single_ta / the screener_ta SQLite cache.
              Must have keys: ohlcv_dates, ohlcv_open, ohlcv_high, ohlcv_low,
              ohlcv_close, ohlcv_volume, sma20_series, sma50_series,
              bb_upper_series, bb_mid_series, bb_lower_series,
              rsi_series, macd_line_series, macd_sig_series, macd_hist_series
    ticker  : str — used for chart title and candlestick legend name

    Returns
    -------
    go.Figure with 3 sub-plots. Returns a single-panel error figure if ta_data
    is empty or missing OHLCV data.
    """
    dates = ta_data.get("ohlcv_dates", [])
    opens = ta_data.get("ohlcv_open", [])
    highs = ta_data.get("ohlcv_high", [])
    lows  = ta_data.get("ohlcv_low", [])
    closes = ta_data.get("ohlcv_close", [])

    if not dates or not closes:
        fig = go.Figure()
        fig.update_layout(
            **plotly_dark_layout(
                annotations=[{
                    "text": f"No TA data available for {ticker}",
                    "xref": "paper", "yref": "paper",
                    "x": 0.5, "y": 0.5, "showarrow": False,
                    "font": {"size": 14, "color": "#556080"},
                }],
                xaxis_visible=False,
                yaxis_visible=False,
                height=300,
            )
        )
        return fig

    # Colour each volume/MACD histogram bar by direction
    hist_vals = ta_data.get("macd_hist_series", [])
    hist_colors = [
        _TA_HIST_POS if (v is not None and v >= 0) else _TA_HIST_NEG
        for v in hist_vals
    ]

    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.60, 0.20, 0.20],
        subplot_titles=[f"{ticker.upper()} — 6-Month Price", "RSI (14)", "MACD (12/26/9)"],
    )

    # ── Row 1: Candlestick ────────────────────────────────────────────────
    fig.add_trace(
        go.Candlestick(
            x=dates,
            open=opens,
            high=highs,
            low=lows,
            close=closes,
            name=ticker.upper(),
            increasing_line_color=_TA_UP,
            decreasing_line_color=_TA_DOWN,
            increasing_fillcolor=_TA_UP,
            decreasing_fillcolor=_TA_DOWN,
            line_width=1,
        ),
        row=1, col=1,
    )

    # ── Row 1: Bollinger Bands ────────────────────────────────────────────
    bb_upper = ta_data.get("bb_upper_series", [])
    bb_mid   = ta_data.get("bb_mid_series", [])
    bb_lower = ta_data.get("bb_lower_series", [])

    if bb_upper and any(v is not None for v in bb_upper):
        fig.add_trace(go.Scatter(
            x=dates, y=bb_upper,
            name="BB Upper",
            line=dict(color=_TA_BB_LINE, width=1, dash="dot"),
            showlegend=True,
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=dates, y=bb_lower,
            name="BB Lower",
            fill="tonexty",
            fillcolor=_TA_BB_FILL,
            line=dict(color=_TA_BB_LINE, width=1, dash="dot"),
            showlegend=True,
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=dates, y=bb_mid,
            name="BB Mid",
            line=dict(color=_TA_BB_MID, width=1),
            showlegend=True,
        ), row=1, col=1)

    # ── Row 1: SMA20 & SMA50 ──────────────────────────────────────────────
    sma20 = ta_data.get("sma20_series", [])
    sma50 = ta_data.get("sma50_series", [])

    if sma20 and any(v is not None for v in sma20):
        fig.add_trace(go.Scatter(
            x=dates, y=sma20,
            name="SMA20",
            line=dict(color=_TA_SMA20_CLR, width=1.5),
            showlegend=True,
        ), row=1, col=1)

    if sma50 and any(v is not None for v in sma50):
        fig.add_trace(go.Scatter(
            x=dates, y=sma50,
            name="SMA50",
            line=dict(color=_TA_SMA50_CLR, width=1.5),
            showlegend=True,
        ), row=1, col=1)

    # ── Row 2: RSI ────────────────────────────────────────────────────────
    rsi_vals = ta_data.get("rsi_series", [])
    if rsi_vals and any(v is not None for v in rsi_vals):
        fig.add_trace(go.Scatter(
            x=dates, y=rsi_vals,
            name="RSI (14)",
            line=dict(color=_TA_RSI_CLR, width=1.5),
            showlegend=True,
        ), row=2, col=1)

        # Oversold / overbought reference lines
        fig.add_hline(
            y=70, line_dash="dot", line_color="rgba(239,83,80,0.5)",
            line_width=1, row=2, col=1,
        )
        fig.add_hline(
            y=30, line_dash="dot", line_color="rgba(38,166,154,0.5)",
            line_width=1, row=2, col=1,
        )

    # ── Row 3: MACD ───────────────────────────────────────────────────────
    macd_line_vals = ta_data.get("macd_line_series", [])
    macd_sig_vals  = ta_data.get("macd_sig_series", [])
    macd_hist_vals = ta_data.get("macd_hist_series", [])

    if macd_line_vals and any(v is not None for v in macd_line_vals):
        fig.add_trace(go.Scatter(
            x=dates, y=macd_line_vals,
            name="MACD",
            line=dict(color=_TA_MACD_CLR, width=1.5),
            showlegend=True,
        ), row=3, col=1)

    if macd_sig_vals and any(v is not None for v in macd_sig_vals):
        fig.add_trace(go.Scatter(
            x=dates, y=macd_sig_vals,
            name="Signal",
            line=dict(color=_TA_SIG_CLR, width=1.5),
            showlegend=True,
        ), row=3, col=1)

    if macd_hist_vals and any(v is not None for v in macd_hist_vals):
        fig.add_trace(go.Bar(
            x=dates, y=macd_hist_vals,
            name="Histogram",
            marker_color=hist_colors,
            showlegend=False,
        ), row=3, col=1)

    # ── Layout ────────────────────────────────────────────────────────────
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=_TA_BG,
        plot_bgcolor=_TA_PLOT_BG,
        height=580,
        margin=dict(l=0, r=60, t=40, b=0),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
            font=dict(size=10),
            bgcolor="rgba(0,0,0,0)",
        ),
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
    )

    fig.update_xaxes(gridcolor=_TA_GRID, showgrid=True, zeroline=False)
    fig.update_yaxes(gridcolor=_TA_GRID, showgrid=True, zeroline=False)
    fig.update_yaxes(side="right", row=1, col=1, title_text="Price (USD)", title_font_size=10)
    fig.update_yaxes(side="right", row=2, col=1, title_text="RSI", title_font_size=10, range=[0, 100])
    fig.update_yaxes(side="right", row=3, col=1, title_text="MACD", title_font_size=10)

    return fig


# ---------------------------------------------------------------------------
# Cached data fetchers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3600)
def _cached_screener(
    min_market_cap: int,
    max_market_cap: int,
    min_price: float,
    max_price: float,
    min_volume: int,
    sector: str,
) -> pd.DataFrame:
    """
    Wrapper that applies @st.cache_data around run_screener().
    The underlying run_screener() also checks a SQLite cache (1h TTL), but
    @st.cache_data here prevents re-hitting SQLite on every Streamlit rerun.
    """
    return run_screener(
        min_market_cap=min_market_cap,
        max_market_cap=max_market_cap,
        min_price=min_price,
        max_price=max_price,
        min_volume=min_volume,
        sector=sector,
        force_refresh=False,
    )


@st.cache_data(ttl=600)
def _cached_price_chart(ticker: str) -> go.Figure:
    """Cache the price chart for 10 minutes per ticker."""
    return _build_price_chart(ticker)


@st.cache_data(ttl=3600)
def _cached_ta_chart(ticker: str) -> go.Figure:
    """Cache the full TA chart for 1 hour per ticker (matches TA cache TTL)."""
    from data.screener_ta import _fetch_single_ta
    _, ta_data = _fetch_single_ta(ticker)
    return _build_ta_chart(ta_data, ticker)


# ---------------------------------------------------------------------------
# Render functions
# ---------------------------------------------------------------------------

def _render_sidebar() -> None:
    with st.sidebar:
        st.markdown("---")
        st.markdown("### About This Page")
        with st.expander("What is Speculative Screening?", expanded=True):
            st.markdown(
                "This screener surfaces **small and micro-cap stocks** that exhibit "
                "characteristics associated with high upside potential — unusual volume, "
                "proximity to 52-week lows, small market cap, and forward PE discounts. "
                "A **Speculation Score (0–100)** is computed from these factors."
            )
        with st.expander("How is the Speculation Score calculated?"):
            st.markdown(
                "The score is a **blended 100-point scale** split into two blocks:\n\n"
                "**Technical Block (50 pts):** 5 components × 10 pts each:\n"
                "1. **52-week momentum** — how far above the 52-week low\n"
                "2. **52-week recovery room** — how far below the 52-week high\n"
                "3. **Volume spike** — 10-day vs 3-month average volume ratio\n"
                "4. **Small cap premium** — smaller cap = higher potential\n"
                "5. **Forward PE discount** — low or no PE = unloved / early stage\n\n"
                "**Fundamental Block (50 pts):** Fetched via yfinance .info for top N tickers:\n"
                "1. **Revenue growth** — YoY growth (0–15 pts; ≥50% YoY = max)\n"
                "2. **Cash runway** — total cash / annual FCF burn (0–10 pts; ≥2yr = max)\n"
                "3. **Analyst upside** — mean price target vs current price (0–10 pts)\n"
                "4. **Short squeeze** — short % of float (0–10 pts; ≥20% = max)\n"
                "5. **Debt health** — debt/equity ratio (0–5 pts; ≤0.5 = max)\n\n"
                "Tickers not in the top-N enrichment window are shown as 'Tech only' "
                "with their score scaled from the technical block.\n\n"
                "Higher score = more speculative (more risk AND more potential upside)."
            )
        with st.expander("What does the Gemini Risk Analysis do?"):
            st.markdown(
                "Gemini 3.1 Pro reviews the stock's quantitative metrics and returns:\n"
                "- **Risk Score (1–10):** 1 = very low risk, 10 = extremely high risk\n"
                "- **Upside Thesis:** what the numbers suggest about upside potential\n"
                "- **Key Risks:** 3–5 specific risk factors from the metrics\n"
                "- **Red Flags:** any concrete warning signs\n"
                "- **Verdict:** lottery ticket / speculative buy / hold-watch / avoid\n\n"
                "Results are cached for 24 hours to conserve Gemini Pro quota (50/day)."
            )
        with st.expander("How are Technical Analysis signals computed?"):
            st.markdown(
                "For the top-N candidates, 6 months of daily OHLCV data is fetched "
                "from Yahoo Finance and five indicators are computed:\n\n"
                "- **RSI (14):** Relative Strength Index. ≤ 30 = oversold (potential bounce); "
                "≥ 70 = overbought (potential pullback).\n"
                "- **SMA20 / SMA50:** 20- and 50-day simple moving averages. A **Golden Cross** "
                "(SMA20 crosses above SMA50) is bullish; a **Death Cross** is bearish.\n"
                "- **MACD (12/26/9):** Momentum oscillator. A MACD line crossing above the "
                "signal line is bullish; crossing below is bearish.\n"
                "- **Bollinger Bands (20, 2σ):** Volatility envelope. Price near the lower "
                "band suggests oversold; near the upper band suggests overextension.\n\n"
                "The **TA Signal** (Bullish / Neutral / Bearish) is a simple vote-count: "
                "each bullish indicator signal adds +1 (golden cross adds +2), bearish adds -1 "
                "(-2 for death cross). Signal ≥ +2 net = Bullish; ≤ -2 net = Bearish; "
                "otherwise Neutral. Results are cached for 1 hour."
            )
        st.markdown("---")
        st.caption(
            "Data sourced from Yahoo Finance via yfinance. "
            "Gemini analysis via Google Gemini 3.1 Pro CLI."
        )



def _render_filters() -> tuple[int, int, float, float, int, str, int]:
    """
    Render the filter controls and return the current filter values as a tuple:
    (min_market_cap, max_market_cap, min_price, max_price, min_volume, sector, enrich_top_n)
    """
    section_header("Screener Filters")

    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        cap_tier = st.selectbox(
            "Market Cap Tier",
            options=list(_CAP_TIER_OPTIONS.keys()),
            index=0,
            key="screener_cap_tier",
        )
        min_cap, max_cap = _CAP_TIER_OPTIONS[cap_tier]

    with col2:
        price_range = st.slider(
            "Price Range (USD)",
            min_value=0.10,
            max_value=50.0,
            value=(_DEFAULT_MIN_PRICE, _DEFAULT_MAX_PRICE),
            step=0.10,
            key="screener_price_range",
        )
        min_price, max_price = price_range

    with col3:
        volume_options = {
            "50K+": 50_000,
            "100K+": 100_000,
            "250K+": 250_000,
            "500K+": 500_000,
            "1M+": 1_000_000,
        }
        vol_label = st.selectbox(
            "Min Avg Daily Volume (3M)",
            options=list(volume_options.keys()),
            index=1,
            key="screener_volume",
        )
        min_volume = volume_options[vol_label]

    with col4:
        sector_display = ["All Sectors"] + [s for s in SECTOR_OPTIONS if s]
        sector_sel = st.selectbox(
            "Sector",
            options=sector_display,
            index=0,
            key="screener_sector",
        )
        sector = "" if sector_sel == "All Sectors" else sector_sel

    with col5:
        enrich_label = st.selectbox(
            "Enrich with Fundamentals (top N)",
            options=list(_ENRICH_N_OPTIONS.keys()),
            index=list(_ENRICH_N_OPTIONS.keys()).index(_DEFAULT_ENRICH_N),
            key="screener_enrich_n",
            help=(
                "Fetches yfinance .info for the top N candidates by technical score "
                "to compute revenue growth, cash runway, analyst targets, short interest, "
                "and balance-sheet health. Uses a 24h SQLite cache."
            ),
        )
        enrich_top_n = _ENRICH_N_OPTIONS[enrich_label]

    return min_cap, max_cap, min_price, max_price, min_volume, sector, enrich_top_n


def _render_results_table(df: pd.DataFrame) -> None:
    """
    Render the screener results as a styled dataframe.
    df must have the columns produced by data/screener.py run_screener().
    """
    if df.empty:
        st.info(
            "No candidates found with the current filters. "
            "Try relaxing the market cap tier, price range, or minimum volume."
        )
        return

    section_header(f"Candidates ({len(df)} found, sorted by Speculation Score)")

    # Build base columns
    display_data = {
        "Ticker":       df["ticker"],
        "Name":         df["name"],
        "Price":        df["price"].apply(_fmt_price),
        "Day Chg%":     df["change_pct"].apply(_fmt_pct),
        "Market Cap":   df["market_cap"].apply(_fmt_market_cap),
        "Vol (3M Avg)": df["volume_3m"].apply(_fmt_volume),
        "Vol Spike":    df.apply(
            lambda r: f"{float(r['volume_10d']) / float(r['volume_3m']):.1f}x"
            if r.get("volume_10d") and r.get("volume_3m") and float(r.get("volume_3m", 0)) > 0
            else "N/A",
            axis=1,
        ),
        "52W High Chg": df["week52_high_chg_pct"].apply(_fmt_pct),
        "52W Low Chg":  df["week52_low_chg_pct"].apply(_fmt_pct),
        "Fwd PE":       df["forward_pe"].apply(
            lambda v: f"{float(v):.1f}" if v is not None and not pd.isna(v) else "N/A"
        ),
        "EPS (TTM)":    df["eps_ttm"].apply(
            lambda v: f"{float(v):.2f}" if v is not None and not pd.isna(v) else "N/A"
        ),
        "Spec Score":   df["speculation_score"],
    }

    # Append fundamental columns if any rows have been enriched
    if "has_fundamentals" in df.columns and df["has_fundamentals"].any():
        display_data["Rev Growth"] = df["revenue_growth"].apply(_fmt_growth)
        display_data["Cash Runway"] = df["cash_runway_years"].apply(_fmt_runway)
        display_data["Short % Flt"] = df["short_percent_float"].apply(
            lambda v: f"{float(v) * 100:.1f}%" if v is not None and not pd.isna(v) else "N/A"
        )
        display_data["Analyst Upside"] = df["analyst_upside_pct"].apply(
            lambda v: f"+{float(v) * 100:.0f}%" if v is not None and not pd.isna(v) and float(v) >= 0
            else (f"{float(v) * 100:.0f}%" if v is not None and not pd.isna(v) else "N/A")
        )
        display_data["Has Fundamentals"] = df["has_fundamentals"].apply(
            lambda v: "Yes" if v else "Tech only"
        )

    # Append TA columns if any rows have been enriched with technicals
    if "has_technicals" in df.columns and df["has_technicals"].any():
        def _fmt_rsi(v) -> str:
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return "N/A"
            try:
                r = float(v)
                if r <= 30:
                    return f"{r:.0f} (OS)"
                if r >= 70:
                    return f"{r:.0f} (OB)"
                return f"{r:.0f}"
            except (TypeError, ValueError):
                return "N/A"

        display_data["RSI"] = df["rsi"].apply(_fmt_rsi)
        display_data["Trend vs SMA50"] = df["trend_vs_sma50"].apply(
            lambda v: v if v is not None else "N/A"
        )
        display_data["TA Signal"] = df["ta_signal"].apply(
            lambda v: v if v is not None else "N/A"
        )

    display_df = pd.DataFrame(display_data)

    st.dataframe(display_df, use_container_width=True, hide_index=True)

    # Show cash-runway warning summary if any red flags present
    if "red_flag_cash_runway" in df.columns:
        danger_tickers = df[df["red_flag_cash_runway"] == True]["ticker"].tolist()
        if danger_tickers:
            st.warning(
                f"**Cash Runway Warning:** The following tickers have < 6 months of cash runway "
                f"based on current FCF burn rate: {', '.join(danger_tickers)}. "
                "Dilution or liquidity risk is elevated."
            )


def _render_stock_detail(row: pd.Series, analysis: dict | None) -> None:
    """
    Render an expander with price chart, key metrics, and Gemini analysis for one stock.

    Parameters
    ----------
    row      : pd.Series — one row from the screener DataFrame
    analysis : dict | None — pre-loaded Gemini analysis, or None if not yet run
    """
    score = int(row.get("speculation_score", 0))
    score_color = _score_color(score)
    ticker = str(row.get("ticker", ""))
    name = str(row.get("name", ticker))

    label = (
        f"{ticker} — {name}  |  "
        f"Score: {score}/100  |  "
        f"Price: {_fmt_price(row.get('price'))}"
    )

    with st.expander(label, expanded=False):
        # TA chart (falls back to price chart if TA data unavailable)
        if row.get("has_technicals"):
            ta_chart_fig = _cached_ta_chart(ticker)
        else:
            ta_chart_fig = _cached_price_chart(ticker)
        st.plotly_chart(ta_chart_fig, use_container_width=True)

        # TA signal badge and signal list (only when has_technicals)
        if row.get("has_technicals"):
            ta_signal = str(row.get("ta_signal") or "Neutral")
            ta_color = _TA_SIGNAL_COLORS.get(ta_signal, "#2979ff")
            ta_rsi = row.get("rsi")
            ta_rsi_str = f"RSI {float(ta_rsi):.0f}" if ta_rsi is not None else "RSI N/A"
            trend = str(row.get("trend_vs_sma50") or "N/A")
            ta_col1, ta_col2, ta_col3, _ = st.columns([1, 1, 2, 4])
            ta_col1.markdown(
                f'<div style="background:{ta_color};border-radius:8px;padding:6px 12px;'
                f'text-align:center;font-weight:700;font-size:0.85rem;color:#0d1020;">'
                f'TA: {ta_signal}</div>',
                unsafe_allow_html=True,
            )
            ta_col2.markdown(
                f'<div style="background:#161b27;border:1px solid #1e2740;border-radius:8px;'
                f'padding:6px 12px;text-align:center;font-size:0.8rem;color:#e8eaf0;">'
                f'{ta_rsi_str}</div>',
                unsafe_allow_html=True,
            )
            ta_col3.markdown(
                f'<div style="background:#161b27;border:1px solid #1e2740;border-radius:8px;'
                f'padding:6px 12px;text-align:center;font-size:0.8rem;color:#e8eaf0;">'
                f'{trend}</div>',
                unsafe_allow_html=True,
            )

            # Signal list (collapsed expander)
            ta_signals_raw = row.get("ta_signal_list")
            if ta_signals_raw:
                try:
                    import json as _json_ta
                    ta_signal_items = _json_ta.loads(ta_signals_raw) if isinstance(ta_signals_raw, str) else ta_signals_raw
                except (TypeError, ValueError):
                    ta_signal_items = []
                if ta_signal_items:
                    with st.expander("Technical Signals Detected", expanded=False):
                        for sig in ta_signal_items:
                            st.markdown(f"- {sig}")

        # Key metrics row
        mc1, mc2, mc3, mc4, mc5 = st.columns(5)
        mc1.metric("Price", _fmt_price(row.get("price")))
        mc2.metric("Market Cap", _fmt_market_cap(row.get("market_cap")))
        mc3.metric("52W High Chg", _fmt_pct(row.get("week52_high_chg_pct")))
        mc4.metric("52W Low Chg", _fmt_pct(row.get("week52_low_chg_pct")))
        mc5.metric(
            "Spec Score",
            f"{score}/100",
            help="Speculation score 0-100: higher = more speculative (higher risk AND higher potential upside)",
        )

        # Score breakdown
        import json as _json
        breakdown_raw = row.get("score_breakdown")
        if breakdown_raw:
            try:
                bd = _json.loads(breakdown_raw) if isinstance(breakdown_raw, str) else breakdown_raw
            except (TypeError, ValueError):
                bd = {}
            if bd:
                tech_total = bd.get("technical_total", 0)
                fund_total = bd.get("fundamental_total", 0)
                has_fund = bd.get("has_fundamentals", False)
                with st.expander("Score Breakdown", expanded=False):
                    sb_col1, sb_col2 = st.columns(2)
                    with sb_col1:
                        st.markdown(f"**Technical Sub-score: {tech_total}/50**")
                        st.markdown(f"- 52W Momentum: {bd.get('t1_52w_momentum', 0)}/10")
                        st.markdown(f"- 52W Recovery Room: {bd.get('t2_52w_recovery', 0)}/10")
                        st.markdown(f"- Volume Spike: {bd.get('t3_volume_spike', 0)}/10")
                        st.markdown(f"- Small Cap Premium: {bd.get('t4_small_cap', 0)}/10")
                        st.markdown(f"- Forward PE Discount: {bd.get('t5_fwd_pe', 0)}/10")
                    with sb_col2:
                        if has_fund:
                            st.markdown(f"**Fundamental Sub-score: {fund_total}/50**")
                            st.markdown(f"- Revenue Growth: {bd.get('f1_revenue_growth', 0)}/15")
                            st.markdown(f"- Cash Runway: {bd.get('f2_cash_runway', 0)}/10")
                            st.markdown(f"- Analyst Upside: {bd.get('f3_analyst_upside', 0)}/10")
                            st.markdown(f"- Short Squeeze: {bd.get('f4_short_squeeze', 0)}/10")
                            st.markdown(f"- Debt Health: {bd.get('f5_debt_health', 0)}/5")
                        else:
                            st.markdown("**Fundamentals: Not yet enriched**")
                            st.caption("Score shown is technical-only (×2 scaling). "
                                       "Run enrichment to get the blended 100-pt score.")

        # Cash runway warning badge
        if row.get("red_flag_cash_runway"):
            cash_yr = row.get("cash_runway_years")
            runway_str = f"{float(cash_yr):.1f} yr" if cash_yr is not None else "< 6 mo"
            st.error(
                f"**CASH RUNWAY WARNING ({ticker}):** Estimated cash runway is {runway_str}. "
                "Severe dilution or insolvency risk. Review latest 10-Q before investing."
            )

        # Fundamentals detail section (only when enriched)
        if row.get("has_fundamentals"):
            with st.expander("Fundamentals Detail", expanded=False):
                fd1, fd2, fd3, fd4 = st.columns(4)
                rev_g = row.get("revenue_growth")
                fd1.metric("Rev Growth (YoY)", _fmt_growth(rev_g))
                runway = row.get("cash_runway_years")
                fd2.metric("Cash Runway", _fmt_runway(runway))
                short_pct = row.get("short_percent_float")
                fd3.metric(
                    "Short % Float",
                    f"{float(short_pct) * 100:.1f}%" if short_pct is not None else "N/A",
                )
                analyst_up = row.get("analyst_upside_pct")
                fd4.metric(
                    "Analyst Upside",
                    f"+{float(analyst_up) * 100:.0f}%" if analyst_up is not None and float(analyst_up) >= 0
                    else (f"{float(analyst_up) * 100:.0f}%" if analyst_up is not None else "N/A"),
                )
                fd5, fd6, _, _ = st.columns(4)
                dte = row.get("debt_to_equity")
                fd5.metric("Debt/Equity", f"{float(dte):.2f}" if dte is not None else "N/A")
                insider = row.get("held_percent_insiders")
                fd6.metric(
                    "Insider Own.",
                    f"{float(insider) * 100:.1f}%" if insider is not None else "N/A",
                )

        st.markdown("---")

        # Gemini analysis section
        if analysis is not None and "_error" not in analysis:
            _render_analysis_block(ticker, analysis)
        else:
            # Show analyze button
            stats = get_today_stats()
            pro_remaining = PRO_DAILY_LIMIT - stats.get("pro", 0)

            if pro_remaining <= 0:
                st.warning("Gemini Pro daily limit (50) reached. Analysis unavailable until tomorrow.")
            else:
                btn_key = f"analyze_{ticker}"
                if st.button(
                    f"Analyze {ticker} with Gemini Pro",
                    key=btn_key,
                    help=f"Uses 1 Gemini Pro call. {pro_remaining} remaining today.",
                ):
                    with st.spinner(f"Running Gemini 3.1 Pro analysis for {ticker}..."):
                        result = run_risk_analysis(row.to_dict())
                    st.session_state[f"analysis_{ticker}"] = result
                    st.rerun()

                if analysis is not None and "_error" in analysis:
                    st.error(f"Analysis failed: {analysis['_error']}")


def _render_analysis_block(ticker: str, analysis: dict) -> None:
    """
    Render the Gemini risk analysis results for a single stock.
    This is called from within an expander in _render_stock_detail.
    """
    risk_score = analysis.get("risk_score", 5)
    verdict = analysis.get("verdict", "hold/watch")
    verdict_color = _VERDICT_COLORS.get(verdict, "#aab4cf")
    verdict_icon = _VERDICT_ICONS.get(verdict, "~")
    risk_color = _risk_score_color(risk_score)

    # Header row
    col_rs, col_vd, col_spacer = st.columns([1, 2, 5])
    col_rs.markdown(
        f'<div style="background:{risk_color};border-radius:8px;padding:8px 12px;'
        f'text-align:center;font-weight:700;font-size:1.1rem;color:#0d1020;">'
        f'Risk {risk_score}/10</div>',
        unsafe_allow_html=True,
    )
    col_vd.markdown(
        f'<div style="background:{verdict_color};border-radius:8px;padding:8px 12px;'
        f'text-align:center;font-weight:700;font-size:0.9rem;color:#0d1020;">'
        f'[{verdict_icon}] {verdict.upper()}</div>',
        unsafe_allow_html=True,
    )

    st.markdown("&nbsp;", unsafe_allow_html=True)

    # Upside thesis
    upside = analysis.get("upside_thesis", "")
    if upside:
        st.markdown(f"**Upside Thesis:** {upside}")

    # Key risks
    key_risks = analysis.get("key_risks", [])
    if key_risks:
        st.markdown("**Key Risks:**")
        for r in key_risks:
            st.markdown(f"- {r}")

    # Red flags
    red_flags = analysis.get("red_flags", [])
    if red_flags:

    # Row 1: Verdict badge + Risk score meter
    col_verdict, col_score = st.columns([1, 1])

    with col_verdict:
        st.markdown("**Verdict**")
        st.markdown(_render_verdict_badge(verdict), unsafe_allow_html=True)

    with col_score:
        st.markdown(f"**Risk Score: {risk_score}/10**")
        st.markdown(_render_risk_score_bar(risk_score), unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # Row 2: Upside Thesis (callout / quote)
    if upside_thesis:
        st.markdown("**Upside Thesis**")
        st.info(upside_thesis)

    # Row 3: Key Risks & Red Flags side-by-side
    col_risks, col_flags = st.columns(2)

    with col_risks:
        st.markdown("**Key Risks**")
        if key_risks:
            for r in key_risks:
                st.markdown(f"• {r}")
        else:
            st.caption("None identified.")

    with col_flags:
        st.markdown("**Red Flags**")
        if red_flags:
            for f in red_flags:
                st.markdown(f"⚠️ {f}")
        else:
            st.caption("None identified.")

    # Analyzed at timestamp
    analyzed_at = analysis.get("analyzed_at")
    if analyzed_at:
        try:
            dt = datetime.fromisoformat(analyzed_at)
            age_str = f"Analyzed {dt.strftime('%b %d, %Y %H:%M UTC')}"
        except (ValueError, TypeError):
            age_str = f"Analyzed: {analyzed_at}"
        st.caption(f"_{age_str} · Cached for 24h_")


def _render_batch_analysis_section(df: pd.DataFrame) -> None:
    """Render the 'Analyze Top N with Gemini' batch section below the results table."""
    if df.empty:
        return

    section_header("Batch Gemini Analysis")

    stats = get_today_stats()
    pro_remaining = PRO_DAILY_LIMIT - stats.get("pro", 0)

    if pro_remaining <= 0:
        st.warning("Gemini Pro daily limit (50) reached. Batch analysis unavailable until tomorrow.")
        return

    max_n = min(10, len(df), pro_remaining)
    if max_n < 1:
        return

    col_n, col_btn = st.columns([1, 2])
    with col_n:
        n = st.number_input(
            "Analyze top N candidates",
            min_value=1,
            max_value=max_n,
            value=min(5, max_n),
            step=1,
            key="screener_batch_n",
            help=f"Each stock uses 1 Gemini Pro call. {pro_remaining} remaining today.",
        )
    with col_btn:
        st.markdown("&nbsp;", unsafe_allow_html=True)
        if st.button(
            f"Analyze Top {n} Candidates",
            key="screener_batch_analyze",
            help=f"Will use up to {n} Gemini Pro calls.",
        ):
            top_rows = df.head(int(n)).to_dict(orient="records")
            with st.spinner(f"Running Gemini 3.1 Pro analysis for {n} stocks..."):
                batch_results = run_batch_risk_analysis(top_rows)
            for ticker, result in batch_results.items():
                st.session_state[f"analysis_{ticker}"] = result
            st.rerun()


def _render_disclaimer() -> None:
    """Render the mandatory financial disclaimer."""
    st.markdown("---")
    st.warning(
        "**DISCLAIMER:** This page is for informational and educational purposes only. "
        "It does NOT constitute financial advice, investment recommendations, or an offer "
        "to buy or sell securities. Speculative and micro-cap stocks carry extreme risk "
        "including total loss of principal. Past screening results do not predict future "
        "performance. Always conduct your own due diligence and consult a licensed financial "
        "advisor before making any investment decisions."
    )


# ---------------------------------------------------------------------------
# Page entry
# ---------------------------------------------------------------------------

def main() -> None:
    _render_sidebar()

    page_header(
        "Speculative Stock Screener",
        "Surface small and micro-cap candidates with high upside potential, "
        "then assess risk with Gemini 3.1 Pro.",
    )

    _render_disclaimer()

    # --- Filters ---
    min_cap, max_cap, min_price, max_price, min_volume, sector, enrich_top_n = _render_filters()

    col_run, col_refresh = st.columns([1, 1])
    with col_run:
        run_btn = st.button("Run Screener", key="screener_run", type="primary")
    with col_refresh:
        refresh_btn = st.button(
            "Force Refresh (bypass cache)",
            key="screener_refresh",
            help="Re-fetch from Yahoo Finance, ignoring the 1-hour cached results.",
        )

    # On first load OR when run button pressed, fetch results
    if "screener_results_df" not in st.session_state or run_btn:
        with st.spinner("Running screener via Yahoo Finance..."):
            df = _cached_screener(min_cap, max_cap, min_price, max_price, min_volume, sector)
        if not df.empty:
            with st.spinner(f"Enriching top {enrich_top_n} candidates with fundamentals (24h cached)..."):
                df = enrich_with_fundamentals(df, top_n=enrich_top_n)
            with st.spinner(f"Computing technical analysis for top {enrich_top_n} candidates (1h cached)..."):
                df = enrich_with_technicals(df, top_n=enrich_top_n)
        st.session_state["screener_results_df"] = df
        st.session_state["screener_filter_key"] = (
            min_cap, max_cap, min_price, max_price, min_volume, sector
        )

    if refresh_btn:
        with st.spinner("Refreshing from Yahoo Finance (bypassing cache)..."):
            df = run_screener(
                min_market_cap=min_cap,
                max_market_cap=max_cap,
                min_price=min_price,
                max_price=max_price,
                min_volume=min_volume,
                sector=sector,
                force_refresh=True,
            )
        if not df.empty:
            with st.spinner(f"Enriching top {enrich_top_n} candidates with fundamentals (24h cached)..."):
                df = enrich_with_fundamentals(df, top_n=enrich_top_n)
            with st.spinner(f"Computing technical analysis for top {enrich_top_n} candidates (1h cached)..."):
                df = enrich_with_technicals(df, top_n=enrich_top_n)
        st.session_state["screener_results_df"] = df
        _cached_screener.clear()

    df: pd.DataFrame = st.session_state.get("screener_results_df", pd.DataFrame())

    if df.empty and not run_btn and not refresh_btn:
        st.info("Click **Run Screener** to fetch candidates with the current filters.")
        return

    # --- Results table ---
    _render_results_table(df)

    if df.empty:
        return

    # --- Batch analysis ---
    _render_batch_analysis_section(df)

    # --- Per-stock detail expanders ---
    section_header("Stock Details & Gemini Analysis")

    for _, row in df.iterrows():
        ticker = str(row.get("ticker", ""))
        # Load from session state (set by button click or batch), or try SQLite cache
        analysis = st.session_state.get(f"analysis_{ticker}")
        if analysis is None:
            analysis = get_cached_analysis(ticker)
        _render_stock_detail(row, analysis)


main()
