from datetime import datetime
import streamlit as st

from components.gemini_usage_bar import render_gemini_usage_bar
from data.fetcher import get_stock_info, get_price_history, get_financials, get_earnings_history
from data.calculator import (
    calc_pe_ratio, calc_ev_ebitda, calc_p_fcf, calc_peg_ratio,
    calc_gross_margin, calc_operating_margin, calc_net_margin, calc_roic,
    calc_revenue_yoy, calc_eps_yoy, calc_fcf_yoy,
    calc_net_debt_ebitda, calc_interest_coverage, calc_current_ratio,
    calc_short_interest, calc_insider_ownership,
)
from components.metric_cards import render_metric_group
from components.charts import (
    price_chart, revenue_chart, margin_chart, fcf_chart, earnings_chart,
)

st.set_page_config(page_title="Stock Metrics", layout="wide")

render_gemini_usage_bar()


# ---------------------------------------------------------------------------
# Sidebar — benchmark reference
# ---------------------------------------------------------------------------

def _render_sidebar() -> None:
    """Render benchmark reference guide in the sidebar."""
    with st.sidebar:
        st.header("Benchmark Guide")
        with st.expander("Valuation", expanded=False):
            st.markdown(
                "- **P/E** ≤20 good, ≤35 neutral, >35 warn\n"
                "- **EV/EBITDA** ≤15 good, ≤25 neutral\n"
                "- **P/FCF** ≤20 good, ≤35 neutral\n"
                "- **PEG** ≤1 good, ≤2 neutral"
            )
        with st.expander("Profitability", expanded=False):
            st.markdown(
                "- **Gross Margin** ≥30% neutral, higher = good\n"
                "- **Op Margin** ≥10% neutral\n"
                "- **Net Margin** ≥5% neutral\n"
                "- **ROIC** ≥10% neutral"
            )
        with st.expander("Growth", expanded=False):
            st.markdown(
                "- **Revenue YoY** ≥5% neutral\n"
                "- **EPS YoY** ≥5% neutral\n"
                "- **FCF YoY** ≥5% neutral"
            )
        with st.expander("Balance Sheet", expanded=False):
            st.markdown(
                "- **Net Debt/EBITDA** ≤2 good, ≤4 neutral\n"
                "- **Interest Coverage** ≥3× neutral\n"
                "- **Current Ratio** ≥1.5 neutral"
            )
        with st.expander("Sentiment", expanded=False):
            st.markdown(
                "- **Short Interest** ≤5% good, ≤15% neutral\n"
                "- **Insider Ownership** ≥5% neutral"
            )


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

def _render_header(info: dict, ticker: str) -> None:
    """Render company name, sector, market cap, and current price header."""
    name       = info.get("longName") or info.get("shortName") or ticker
    sector     = info.get("sector", "—")
    industry   = info.get("industry", "—")
    mkt_cap    = info.get("marketCap")
    price      = info.get("currentPrice")
    currency   = info.get("currency", "USD")

    mkt_cap_str = (
        f"${mkt_cap / 1e12:.2f}T" if mkt_cap and mkt_cap >= 1e12 else
        f"${mkt_cap / 1e9:.2f}B"  if mkt_cap and mkt_cap >= 1e9  else
        f"${mkt_cap / 1e6:.2f}M"  if mkt_cap                     else "—"
    )
    price_str = f"{currency} {price:.2f}" if price else "—"

    st.title(name)
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Ticker",      ticker.upper())
    col2.metric("Sector",      sector)
    col3.metric("Market Cap",  mkt_cap_str)
    col4.metric("Price",       price_str)
    st.caption(f"{industry}")


# ---------------------------------------------------------------------------
# Metric row builders
# ---------------------------------------------------------------------------

def _valuation_metrics(info: dict) -> list[dict]:
    """Build metric dicts for the valuation row."""
    pe,  pe_s  = calc_pe_ratio(info)
    ev,  ev_s  = calc_ev_ebitda(info)
    pf,  pf_s  = calc_p_fcf(info)
    peg, peg_s = calc_peg_ratio(info)
    return [
        {"label": "P/E Ratio",   "value": pe,  "benchmark_status": pe_s,
         "description": "Trailing twelve-month price-to-earnings ratio.",
         "benchmark_text": "≤20 good · ≤35 neutral · >35 warn"},
        {"label": "EV/EBITDA",   "value": ev,  "benchmark_status": ev_s,
         "description": "Enterprise value divided by EBITDA.",
         "benchmark_text": "≤15 good · ≤25 neutral · >25 warn"},
        {"label": "P/FCF",       "value": pf,  "benchmark_status": pf_s,
         "description": "Price divided by free cash flow per share.",
         "benchmark_text": "≤20 good · ≤35 neutral · >35 warn"},
        {"label": "PEG Ratio",   "value": peg, "benchmark_status": peg_s,
         "description": "P/E divided by expected earnings growth rate.",
         "benchmark_text": "≤1 good · ≤2 neutral · >2 warn"},
    ]


def _profitability_metrics(info: dict) -> list[dict]:
    """Build metric dicts for the profitability row."""
    gm,  gm_s  = calc_gross_margin(info)
    om,  om_s  = calc_operating_margin(info)
    nm,  nm_s  = calc_net_margin(info)
    ro,  ro_s  = calc_roic(info)
    return [
        {"label": "Gross Margin %",     "value": gm, "benchmark_status": gm_s,
         "description": "Gross profit as a percentage of revenue.",
         "benchmark_text": "≥30% neutral · higher is better"},
        {"label": "Operating Margin %", "value": om, "benchmark_status": om_s,
         "description": "Operating income as a percentage of revenue.",
         "benchmark_text": "≥10% neutral · higher is better"},
        {"label": "Net Margin %",       "value": nm, "benchmark_status": nm_s,
         "description": "Net income as a percentage of revenue.",
         "benchmark_text": "≥5% neutral · higher is better"},
        {"label": "ROIC %",             "value": ro, "benchmark_status": ro_s,
         "description": "Return on invested capital (proxied by ROE).",
         "benchmark_text": "≥10% neutral · higher is better"},
    ]


def _growth_metrics(financials: dict) -> list[dict]:
    """Build metric dicts for the growth row."""
    rv,  rv_s  = calc_revenue_yoy(financials)
    ep,  ep_s  = calc_eps_yoy(financials)
    fc,  fc_s  = calc_fcf_yoy(financials)
    return [
        {"label": "Revenue YoY %", "value": rv, "benchmark_status": rv_s,
         "description": "Year-over-year revenue growth.",
         "benchmark_text": "≥5% neutral · higher is better"},
        {"label": "EPS YoY %",     "value": ep, "benchmark_status": ep_s,
         "description": "Year-over-year basic EPS growth.",
         "benchmark_text": "≥5% neutral · higher is better"},
        {"label": "FCF YoY %",     "value": fc, "benchmark_status": fc_s,
         "description": "Year-over-year free cash flow growth.",
         "benchmark_text": "≥5% neutral · higher is better"},
        {"label": "Analyst Revisions", "value": None, "benchmark_status": "neutral",
         "description": "Consensus estimate revision direction (coming soon).",
         "benchmark_text": "N/A"},
    ]


def _balance_sheet_metrics(info: dict, financials: dict) -> list[dict]:
    """Build metric dicts for the balance sheet row."""
    nd,  nd_s  = calc_net_debt_ebitda(info)
    ic,  ic_s  = calc_interest_coverage(financials)
    cr,  cr_s  = calc_current_ratio(info)
    cash_raw   = info.get("totalCash")
    cash_val   = round(cash_raw / 1e9, 2) if cash_raw else None
    return [
        {"label": "Net Debt/EBITDA",     "value": nd,       "benchmark_status": nd_s,
         "description": "Net debt relative to EBITDA; measures leverage.",
         "benchmark_text": "≤2 good · ≤4 neutral · >4 warn"},
        {"label": "Interest Coverage",   "value": ic,       "benchmark_status": ic_s,
         "description": "EBIT divided by interest expense.",
         "benchmark_text": "≥3× neutral · higher is better"},
        {"label": "Current Ratio",       "value": cr,       "benchmark_status": cr_s,
         "description": "Current assets divided by current liabilities.",
         "benchmark_text": "≥1.5 neutral · higher is better"},
        {"label": "Cash (USD B)",        "value": cash_val, "benchmark_status": "neutral",
         "description": "Total cash and short-term investments.",
         "benchmark_text": "Context-dependent"},
    ]


# ---------------------------------------------------------------------------
# Chart tabs
# ---------------------------------------------------------------------------

def _render_chart_tabs(ticker: str) -> None:
    """Render tabbed chart section for price, revenue, margins, FCF, earnings."""
    tab_price, tab_rev, tab_margin, tab_fcf, tab_earn = st.tabs(
        ["Price", "Revenue", "Margins", "FCF", "Earnings"]
    )

    with tab_price:
        period = st.selectbox(
            "Period", ["1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "max"],
            index=3, key="price_period",
        )
        hist = get_price_history(ticker, period)
        st.plotly_chart(price_chart(hist, ticker), use_container_width=True)

    financials = get_financials(ticker)

    with tab_rev:
        st.plotly_chart(revenue_chart(financials, ticker), use_container_width=True)

    with tab_margin:
        st.plotly_chart(margin_chart(financials, ticker), use_container_width=True)

    with tab_fcf:
        st.plotly_chart(fcf_chart(financials, ticker), use_container_width=True)

    with tab_earn:
        earnings = get_earnings_history(ticker)
        st.plotly_chart(earnings_chart(earnings, ticker), use_container_width=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point for the Metrics page."""
    _render_sidebar()
    st.header("Stock Metrics")

    ticker_input = st.text_input(
        "Enter Ticker Symbol", value=st.session_state.get("active_ticker", ""),
        placeholder="e.g. AAPL, MSFT, NVDA", key="metrics_ticker_input",
    ).upper().strip()

    if not ticker_input:
        st.info("Enter a ticker symbol above to load metrics.")
        return

    st.session_state["active_ticker"] = ticker_input
    st.session_state["last_fetch"] = datetime.now().strftime("%H:%M:%S")

    with st.spinner(f"Loading data for {ticker_input}…"):
        info       = get_stock_info(ticker_input)
        financials = get_financials(ticker_input)

    if not info:
        st.error(f"Could not fetch data for **{ticker_input}**. Check the ticker symbol.")
        return

    _render_header(info, ticker_input)
    st.markdown("---")

    render_metric_group("Valuation",     _valuation_metrics(info))
    render_metric_group("Profitability", _profitability_metrics(info))
    render_metric_group("Growth",        _growth_metrics(financials))
    render_metric_group("Balance Sheet", _balance_sheet_metrics(info, financials))

    st.markdown("---")
    _render_chart_tabs(ticker_input)


main()
