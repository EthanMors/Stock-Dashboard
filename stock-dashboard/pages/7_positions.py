import streamlit as st
import pandas as pd
from data.webull_positions import is_configured, get_account_list, get_balance, get_env_account_ids

st.set_page_config(page_title="Portfolio Positions", layout="wide")
st.title("Portfolio Positions")

if not is_configured():
    st.error("Webull API credentials not configured.")
    st.markdown("Add `WEBULL_APP_KEY` and `WEBULL_APP_SECRET` to your `.env` file and restart the app.")
    st.stop()

env_ids = get_env_account_ids()

with st.spinner("Fetching account list…"):
    account_list_result = get_account_list()

# Build id -> label map from account list
id_to_label: dict[str, str] = {}
if isinstance(account_list_result, list):
    for account in account_list_result:
        aid = (
            account.get("accountId")
            or account.get("account_id")
            or account.get("accountNo")
            or account.get("id")
            or ""
        )
        label = account.get("account_label") or aid
        if aid:
            id_to_label[aid] = label

if env_ids:
    account_ids = env_ids
elif isinstance(account_list_result, dict) and "error" in account_list_result:
    st.error(f"API error: {account_list_result['error']}")
    st.stop()
elif not account_list_result:
    st.warning("No accounts returned. Check your credentials.")
    st.stop()
else:
    account_ids = list(id_to_label.keys())

# Build label -> balance dict for each account
accounts: dict[str, dict] = {}
with st.spinner("Fetching account balances…"):
    for aid in account_ids:
        balance = get_balance(aid)
        if isinstance(balance, dict) and "error" not in balance:
            label = id_to_label.get(aid) or aid
            accounts[label] = balance

if not accounts:
    st.warning("No balance data returned. Check your credentials.")
    st.stop()

selected_label = st.selectbox("Select Account", list(accounts.keys()))
selected_balance = accounts[selected_label]

balance_cols = [
    "total_net_liquidation_value",
    "total_market_value",
    "total_cash_balance",
    "total_unrealized_profit_loss",
    "total_day_profit_loss",
]
display = {k: selected_balance.get(k) for k in balance_cols if k in selected_balance}
if display:
    df = pd.DataFrame([display])
    df.columns = [c.replace("_", " ").title() for c in df.columns]
    st.dataframe(df, use_container_width=True, hide_index=True)
else:
    st.info("No balance fields available for this account.")
    st.json(selected_balance)
