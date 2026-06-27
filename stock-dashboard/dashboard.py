from datetime import datetime
from typing import Optional

import streamlit as st

from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import inject_global_css, page_header
from data.fetcher import get_stock_info

st.set_page_config(
    page_title="Stock Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

render_gemini_usage_bar()
inject_global_css()

_PAGES = [
    {"icon": "📊", "label": "Metrics",     "path": "pages/1_metrics.py"},
    {"icon": "📝", "label": "Thesis",      "path": "pages/2_thesis.py"},
    {"icon": "👁️",  "label": "Watchlist",   "path": "pages/3_watchlist.py"},
    {"icon": "📰", "label": "News",        "path": "pages/4_news.py"},
    {"icon": "🏦", "label": "Hedge Funds", "path": "pages/5_hedge_funds.py"},
    {"icon": "⛓️", "label": "Option Chains", "path": "pages/6_option_chains.py"},
    {"icon": "💼", "label": "Positions",     "path": "pages/7_positions.py"},
    {"icon": "🌐", "label": "Social",        "path": "pages/8_social.py"},
    {"icon": "📁", "label": "Portfolio",     "path": "pages/9_portfolio.py"},
    {"icon": "📈", "label": "Technical Analysis", "path": "pages/10_technical_analysis.py"},
    {"icon": "🔬", "label": "Backtest",      "path": "pages/11_backtest.py"},
    {"icon": "🔍", "label": "AI Screener",   "path": "pages/12_screener.py"},
]

_DEMO_TICKERS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"]
_INDEX_TICKERS = ["^GSPC", "^DJI", "^IXIC", "^RUT"]


# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------

def _init_state() -> None:
    """Initialise session state keys if not already set."""
    st.session_state.setdefault("active_ticker", "")
    st.session_state.setdefault("watchlist", [])
    st.session_state.setdefault("last_fetch", None)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def _render_sidebar_search() -> None:
    """Render global ticker search in sidebar, persisting to session state."""
    st.sidebar.markdown('<p class="section-header" style="padding-left:0.25rem;">Search</p>', unsafe_allow_html=True)
    ticker = st.sidebar.text_input(
        "Ticker Symbol",
        value=st.session_state.get("active_ticker", ""),
        placeholder="e.g. AAPL",
        key="sidebar_ticker_input",
        label_visibility="collapsed",
    ).upper().strip()

    if st.sidebar.button("Go", key="sidebar_go"):
        if ticker:
            st.session_state["active_ticker"] = ticker
            st.session_state["metrics_ticker_input"] = ticker
            st.session_state["last_fetch"] = datetime.now().strftime("%H:%M:%S")
            st.switch_page("pages/1_metrics.py")
        else:
            st.sidebar.warning("Enter a ticker first.")


def _render_sidebar_nav() -> None:
    """Render page navigation links."""
    for page in _PAGES:
        st.sidebar.page_link(page["path"], label=page["label"])


def _render_sidebar_footer() -> None:
    """Render last-updated timestamp and watchlist count at sidebar bottom."""
    st.sidebar.markdown("---")
    last = st.session_state.get("last_fetch")
    st.sidebar.caption(f"Last fetch: {last}" if last else "Last fetch: —")
    wl = st.session_state.get("watchlist") or []
    st.sidebar.caption(f"Watchlist: {len(wl)} ticker{'s' if len(wl) != 1 else ''}")


def _render_sidebar() -> None:
    """Compose all sidebar sections."""
    st.sidebar.markdown(
        '<div class="sidebar-brand">'
        '<h1>Stock Dashboard</h1>'
        '<p>Live data · AI analysis · Portfolio</p>'
        '</div>',
        unsafe_allow_html=True,
    )
    _render_sidebar_search()
    st.sidebar.markdown('<p class="section-header" style="padding-left:0.25rem;">Navigation</p>', unsafe_allow_html=True)
    _render_sidebar_nav()
    _render_sidebar_footer()


# ---------------------------------------------------------------------------
# Quick stats strip
# ---------------------------------------------------------------------------

def _quick_stat_data(ticker: str) -> dict:
    """Return price and daily change for a single ticker, with index fallbacks."""
    info = get_stock_info(ticker)
    
    # Try multiple keys for price and previous close (indexes use different keys)
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose")
    
    # Fallback: if info is sparse (common for indexes), use history
    if price is None or prev_close is None:
        from data.fetcher import get_price_history
        hist = get_price_history(ticker, period="5d")
        if not hist.empty:
            price = hist["Close"].iloc[-1]
            if len(hist) > 1:
                prev_close = hist["Close"].iloc[-2]

    change = ((price - prev_close) / prev_close * 100) if price and prev_close else None
    name = info.get("shortName") or info.get("longName") or ticker
    return {"ticker": ticker, "name": name, "price": price, "change": change}


def _fmt_change(change: Optional[float]) -> tuple[str, str]:
    """Return (formatted string, delta_color) for a daily change value."""
    if change is None:
        return "—", "off"
    sign  = "+" if change >= 0 else ""
    return f"{sign}{change:.2f}%", "normal"


def _render_quick_stats() -> None:
    """Render a strip of live price + daily change for watchlist or demo tickers."""
    wl_tickers = st.session_state.get("watchlist") or []
    tickers    = (wl_tickers[:5] if wl_tickers else _DEMO_TICKERS)

    st.markdown('<p class="section-header">Stocks</p>', unsafe_allow_html=True)
    with st.spinner("Loading stock stats…"):
        stats = [_quick_stat_data(t) for t in tickers]

    cols = st.columns(len(stats))
    for col, s in zip(cols, stats):
        change_str, delta_color = _fmt_change(s["change"])
        price_str = f"${s['price']:.2f}" if s["price"] else "—"
        col.metric(
            label=f"{s['ticker']}",
            value=price_str,
            delta=change_str,
            delta_color=delta_color,
            help=s["name"],
        )


def _render_indexes() -> None:
    """Render a strip of live price + daily change for major market indexes."""
    st.markdown('<p class="section-header">Major Indexes</p>', unsafe_allow_html=True)
    with st.spinner("Loading index stats…"):
        stats = [_quick_stat_data(t) for t in _INDEX_TICKERS]

    cols = st.columns(len(stats))
    for col, s in zip(cols, stats):
        change_str, delta_color = _fmt_change(s["change"])
        price_str = f"{s['price']:,.2f}" if s["price"] else "—"
        # For indexes, we usually don't show the $ sign
        label = s["name"].replace(" (^", " (").replace(")", "")
        # Shorten some common index names if needed
        label = label.replace("S&P 500", "S&P 500").replace("Dow Jones Industrial Average", "Dow 30").replace("Nasdaq 100", "Nasdaq").replace("Russell 2000", "Russell 2K")
        
        col.metric(
            label=s["ticker"].replace("^", ""),
            value=price_str,
            delta=change_str,
            delta_color=delta_color,
            help=s["name"],
        )


# ---------------------------------------------------------------------------
# Landing content
# ---------------------------------------------------------------------------

def _render_landing() -> None:
    """Render the main dashboard landing page with instructions."""
    st.divider()
    st.markdown('<p class="section-header">Quick Access</p>', unsafe_allow_html=True)

    col1, col2, col3, col4, col5 = st.columns(5)

    _cards = [
        (col1, "Metrics",
         "Deep-dive valuation, profitability, growth, and balance sheet metrics "
         "with candlestick, revenue, margin, FCF, and earnings charts.",
         "pages/1_metrics.py", "Open Metrics"),
        (col2, "Thesis Tracker",
         "Write and store investment theses with conviction level, price targets, "
         "catalysts, and bear cases. Tracks metrics at time of writing vs. today.",
         "pages/2_thesis.py", "Open Thesis Tracker"),
        (col3, "Watchlist",
         "Monitor tickers with live prices, P/E, gross margin, 52-week range, "
         "and alert prices. One-click to analyze any ticker.",
         "pages/3_watchlist.py", "Open Watchlist"),
        (col4, "News",
         "Latest ticker news via Massive.com with sentiment analysis. "
         "Paywalled sources are automatically skipped.",
         "pages/4_news.py", "Open News"),
        (col5, "Hedge Funds",
         "Concentrated hedge fund portfolios from SEC 13F filings. "
         "Top, bottom, and a daily-rotating pick from funds with fewer than 15 positions.",
         "pages/5_hedge_funds.py", "Open Hedge Funds"),
    ]

    for col, title, desc, path, link_label in _cards:
        with col:
            st.markdown(
                f'<div class="feature-card">'
                f'<h3>{title}</h3>'
                f'<p>{desc}</p>'
                f'</div>',
                unsafe_allow_html=True,
            )
            st.page_link(path, label=f"{link_label} →")

    st.divider()
    st.caption(
        "Data provided by Yahoo Finance via yfinance. "
        "Not financial advice. All data cached for 1 hour."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point for the main dashboard landing page."""
    _init_state()
    _render_sidebar()

    page_header("Stock Dashboard", "Live market data · Investment thesis tracker · Watchlist")

    st.markdown('<p class="section-header">Market Snapshot</p>', unsafe_allow_html=True)
    _render_indexes()
    st.divider()
    _render_quick_stats()

    _render_landing()


main()
