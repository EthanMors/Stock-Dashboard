import streamlit as st
import pandas as pd
from data.webull_positions import is_configured, get_account_list, get_balance

st.set_page_config(page_title="Portfolio Positions", layout="wide")
st.title("Portfolio Positions")

if not is_configured():
    st.error("Webull API credentials not configured.")
    st.markdown("Add `WEBULL_APP_KEY` and `WEBULL_APP_SECRET` to your `.env` file and restart the app.")
    st.stop()

with st.spinner("Fetching account list…"):
    result = get_account_list()

if isinstance(result, dict) and "error" in result:
    st.error(f"API error: {result['error']}")
elif not result:
    st.warning("No accounts returned. Check your credentials.")
elif isinstance(result, list):
    rows = []
    for account in result:
        account_id = (
            account.get("accountId")
            or account.get("account_id")
            or account.get("accountNo")
            or account.get("id")
            or ""
        )
        balance_data = get_balance(account_id) if account_id else {}
        if isinstance(balance_data, dict) and "error" not in balance_data:
            row = {**account, **{f"balance_{k}": v for k, v in balance_data.items()}}
        else:
            row = account
        rows.append(row)
    df = pd.json_normalize(rows)
    cols = [
        "account_id",
        "account_number",
        "account_label",
        "balance_total_net_liquidation_value",
        "balance_total_market_value",
        "balance_total_cash_balance",
        "balance_total_unrealized_profit_loss",
        "balance_total_day_profit_loss",
    ]
    df = df[[c for c in cols if c in df.columns]]
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.caption(f"{len(rows)} account(s) returned.")
else:
    st.json(result)
