from datetime import datetime
from typing import Optional

import streamlit as st

from data.fetcher import get_stock_info

st.set_page_config(
    page_title="Stock Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

_PAGES = [
    {"icon": "📊", "label": "Metrics",     "path": "pages/1_metrics.py"},
    {"icon": "📝", "label": "Thesis",      "path": "pages/2_thesis.py"},
    {"icon": "👁️",  "label": "Watchlist",   "path": "pages/3_watchlist.py"},
    {"icon": "📰", "label": "News",        "path": "pages/4_news.py"},
    {"icon": "🏦", "label": "Hedge Funds", "path": "pages/5_hedge_funds.py"},
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
    st.sidebar.markdown("### 🔍 Global Search")
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
    """Render page navigation links with icons."""
    st.sidebar.markdown("### Navigation")
    for page in _PAGES:
        st.sidebar.page_link(page["path"], label=f"{page['icon']} {page['label']}")


def _render_sidebar_footer() -> None:
    """Render last-updated timestamp and watchlist count at sidebar bottom."""
    st.sidebar.markdown("---")
    last = st.session_state.get("last_fetch")
    st.sidebar.caption(f"Last fetch: {last}" if last else "Last fetch: —")
    wl = st.session_state.get("watchlist") or []
    st.sidebar.caption(f"Watchlist: {len(wl)} ticker{'s' if len(wl) != 1 else ''}")


def _render_sidebar() -> None:
    """Compose all sidebar sections."""
    st.sidebar.title("📈 Stock Dashboard")
    st.sidebar.markdown("---")
    _render_sidebar_search()
    st.sidebar.markdown("---")
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

    st.markdown("#### 🚀 Stocks")
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
    st.markdown("#### 🌎 Major Indexes")
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
    st.markdown("---")
    st.subheader("Welcome")
    st.markdown(
        "Use the **sidebar search** to look up any ticker instantly, "
        "or navigate to a section below."
    )

    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        st.markdown("### 📊 Metrics")
        st.markdown(
            "Deep-dive valuation, profitability, growth, and balance sheet "
            "metrics for any ticker. Includes candlestick, revenue, margin, "
            "FCF, and earnings charts."
        )
        st.page_link("pages/1_metrics.py", label="Open Metrics →")

    with col2:
        st.markdown("### 📝 Thesis Tracker")
        st.markdown(
            "Write and store investment theses with conviction level, "
            "price targets, catalysts, and bear cases. Tracks metrics at "
            "time of writing vs. today."
        )
        st.page_link("pages/2_thesis.py", label="Open Thesis Tracker →")

    with col3:
        st.markdown("### 👁️ Watchlist")
        st.markdown(
            "Monitor a list of tickers with live prices, P/E, gross margin, "
            "52-week range, and alert prices. One-click to analyze any ticker."
        )
        st.page_link("pages/3_watchlist.py", label="Open Watchlist →")

    with col4:
        st.markdown("### 📰 News")
        st.markdown(
            "Latest news for any ticker via Massive.com, with sentiment analysis. "
            "Paywalled sources are automatically skipped. Click any article "
            "to scrape the full text."
        )
        st.page_link("pages/4_news.py", label="Open News →")

    with col5:
        st.markdown("### 🏦 Hedge Funds")
        st.markdown(
            "Concentrated hedge fund portfolios from SEC 13F filings. "
            "Browse the top, bottom, and a daily-rotating pick from funds "
            "with fewer than 15 reported positions."
        )
        st.page_link("pages/5_hedge_funds.py", label="Open Hedge Funds →")

    st.markdown("---")
    st.caption(
        "Data provided by [Yahoo Finance](https://finance.yahoo.com) via yfinance. "
        "Not financial advice. All data cached for 1 hour."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point for the main dashboard landing page."""
    _init_state()
    _render_sidebar()

    st.title("📈 Stock Dashboard")
    st.markdown("##### Live market data · Investment thesis tracker · Watchlist")
    st.markdown("---")

    st.subheader("Market Snapshot")
    _render_indexes()
    st.markdown("---")
    _render_quick_stats()

    _render_landing()


main()
