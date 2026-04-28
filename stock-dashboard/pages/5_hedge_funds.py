import streamlit as st

from data.hedge_fund_fetcher import get_categorized_funds

st.set_page_config(page_title="Hedge Funds", page_icon="🏦", layout="wide")


def _render_sidebar() -> None:
    with st.sidebar:
        st.header("About This Page")
        with st.expander("What is a 13F filing?", expanded=True):
            st.markdown(
                "SEC Form **13F-HR** is filed quarterly by institutional investment "
                "managers with more than **$100M in assets under management**. "
                "It discloses all equity holdings held at the end of each quarter."
            )
        with st.expander("What does 'concentrated' mean?"):
            st.markdown(
                "A concentrated fund holds **fewer than 15 positions**. "
                "These managers make high-conviction bets on a small number of "
                "securities rather than diversifying broadly."
            )
        with st.expander("How are the groups defined?"):
            st.markdown(
                "- **Top 5** — highest total reported portfolio value\n"
                "- **Bottom 5** — lowest total reported portfolio value\n"
                "- **Daily Pick** — one fund randomly selected from the middle tier; "
                "the selection is deterministic per calendar day and resets at midnight"
            )
        st.markdown("---")
        st.caption("Data sourced from SEC EDGAR via edgartools. Reflects the most recent 13F-HR filing per manager.")


def _fmt_value(dollars: float) -> str:
    """Format a dollar value as $XB, $XM, or $XK."""
    if dollars >= 1e9:
        return f"${dollars / 1e9:.2f}B"
    elif dollars >= 1e6:
        return f"${dollars / 1e6:.2f}M"
    elif dollars >= 1e3:
        return f"${dollars / 1e3:.2f}K"
    return f"${dollars:,.0f}"


def _render_holdings_table(holdings: list) -> None:
    import pandas as pd

    if not holdings:
        st.info("No holdings detail available for this filing.")
        return

    rows = []
    for h in holdings:
        rows.append(
            {
                "Ticker": h["ticker"] or "—",
                "Issuer": h["issuer"] or "—",
                "Value": _fmt_value(h["value"]),
                "Shares": f"{h['shares']:,.0f}" if h["shares"] else "—",
                "% of Fund": f"{h['pct_of_portfolio']:.1f}%",
                "Type": h["put_call"] if h["put_call"] else "Equity",
            }
        )

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)


def _render_fund_card(fund: dict) -> None:
    label = (
        f"{fund['name']} — "
        f"{fund['total_holdings']} positions · "
        f"{_fmt_value(fund['total_value'])}"
    )
    with st.expander(label, expanded=False):
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Positions", fund["total_holdings"])
        col2.metric("Portfolio Value", _fmt_value(fund["total_value"]))
        col3.metric("Report Period", fund["report_period"] or "—")
        col4.metric("Filed", fund["filing_date"] or "—")

        st.markdown(f"**CIK:** {fund['cik']}")
        st.markdown("---")
        _render_holdings_table(fund["holdings"])


def _render_fund_list(funds: list) -> None:
    if not funds:
        st.info("No funds in this category.")
        return
    for fund in funds:
        _render_fund_card(fund)


def main() -> None:
    _render_sidebar()

    st.title("Concentrated Hedge Funds — 13F Analysis")
    st.markdown(
        "Institutional managers with **fewer than 15 reported positions** in their "
        "latest SEC 13F-HR filing, grouped by portfolio size."
    )
    st.markdown("---")

    with st.spinner(
        "Loading 13F data from SEC EDGAR... "
        "First load may take 1–2 minutes while scanning recent filings."
    ):
        try:
            data = get_categorized_funds()
        except Exception as e:
            st.error(f"Failed to load 13F data: {e}")
            return

    if not data["top5"] and not data["bottom5"] and not data["daily_middle"]:
        st.warning(
            "No concentrated funds were found in the scanned filings. "
            "SEC EDGAR may be temporarily unavailable, or the recent filing batch "
            "contained no managers with fewer than 15 positions. Try again later."
        )
        return

    total_found = len(data["top5"]) + len(data["bottom5"]) + data["middle_count"]
    st.caption(
        f"Data as of: {data['as_of_date']} · "
        f"Concentrated funds found: {total_found} · "
        f"Middle-tier pool: {data['middle_count']} fund{'s' if data['middle_count'] != 1 else ''}"
    )

    tab_top, tab_bottom, tab_middle = st.tabs(
        ["Top 5 (Largest)", "Bottom 5 (Smallest)", "Daily Pick"]
    )

    with tab_top:
        st.subheader("Top 5 Concentrated Funds by Portfolio Value")
        _render_fund_list(data["top5"])

    with tab_bottom:
        st.subheader("Bottom 5 Concentrated Funds by Portfolio Value")
        _render_fund_list(data["bottom5"])

    with tab_middle:
        st.subheader("Today's Daily Pick from the Middle Tier")
        st.caption(
            "Selected deterministically from the middle-tier pool using today's date as a seed. "
            "Resets automatically at midnight."
        )
        _render_fund_list(data["daily_middle"])


main()
