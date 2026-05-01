import streamlit as st
from data.webull_positions import (
    try_cached_connect,
    send_mfa,
    login_with_mfa,
    get_positions,
    get_account_summary,
)

st.set_page_config(page_title="Portfolio Positions", layout="wide")
st.title("Portfolio Positions")

# ── Session state keys ────────────────────────────────────────────────────────
st.session_state.setdefault("wb_client", None)
st.session_state.setdefault("wb_login_step", "start")   # start | mfa_sent
st.session_state.setdefault("_wb_pending", None)        # webull instance awaiting MFA

# ── Auto-connect with cached credentials ─────────────────────────────────────
if st.session_state.wb_client is None:
    with st.spinner("Checking cached credentials…"):
        wb = try_cached_connect()
    if wb is not None:
        st.session_state.wb_client = wb
        st.session_state.wb_login_step = "start"

# ── Positions view ────────────────────────────────────────────────────────────
if st.session_state.wb_client is not None:
    wb = st.session_state.wb_client

    if st.sidebar.button("Disconnect / Re-login"):
        st.session_state.wb_client = None
        st.session_state.wb_login_step = "start"
        st.session_state._wb_pending = None
        st.rerun()

    with st.spinner("Fetching account data…"):
        summary = get_account_summary(wb)
        positions = get_positions(wb)

    # Account summary metrics
    if summary:
        summary_keys = [k for k in summary if summary[k] != 0.0]
        if summary_keys:
            cols = st.columns(min(len(summary_keys), 4))
            for i, key in enumerate(summary_keys[:4]):
                cols[i].metric(key, f"${summary[key]:,.2f}")

    st.divider()

    # Positions table
    if not positions:
        st.info("No open positions.")
    else:
        import pandas as pd

        df = pd.DataFrame(positions)
        df = df.rename(columns={
            "ticker": "Ticker",
            "name": "Name",
            "quantity": "Qty",
            "cost_basis": "Cost Basis",
            "current_price": "Price",
            "market_value": "Market Value",
            "unrealized_pnl": "Unrealized P&L",
            "unrealized_pnl_pct": "P&L %",
        })

        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Cost Basis":     st.column_config.NumberColumn(format="$%.2f"),
                "Price":          st.column_config.NumberColumn(format="$%.2f"),
                "Market Value":   st.column_config.NumberColumn(format="$%.2f"),
                "Unrealized P&L": st.column_config.NumberColumn(format="$%.2f"),
                "P&L %":          st.column_config.NumberColumn(format="%.2f%%"),
            },
        )

# ── Login flow ────────────────────────────────────────────────────────────────
else:
    st.info("Connect your Webull account to view positions. Credentials are cached locally after first login.")

    if st.session_state.wb_login_step == "start":
        with st.form("wb_login_form"):
            email = st.text_input("Webull Email")
            password = st.text_input("Webull Password", type="password")
            submitted = st.form_submit_button("Send MFA Code")

        if submitted and email and password:
            with st.spinner("Sending MFA code…"):
                try:
                    wb_pending = send_mfa(email)
                    st.session_state._wb_pending = wb_pending
                    st.session_state._wb_email = email
                    st.session_state._wb_password = password
                    st.session_state.wb_login_step = "mfa_sent"
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to send MFA code: {e}")

    elif st.session_state.wb_login_step == "mfa_sent":
        st.success("MFA code sent. Check your registered email or phone.")

        with st.form("wb_mfa_form"):
            mfa_code = st.text_input("MFA Code")
            col_login, col_back = st.columns(2)
            with col_login:
                submitted = st.form_submit_button("Login", use_container_width=True)
            with col_back:
                back = st.form_submit_button("Back", use_container_width=True)

        if back:
            st.session_state.wb_login_step = "start"
            st.session_state._wb_pending = None
            st.rerun()

        if submitted and mfa_code:
            with st.spinner("Logging in…"):
                try:
                    wb = login_with_mfa(
                        st.session_state._wb_pending,
                        st.session_state._wb_email,
                        st.session_state._wb_password,
                        mfa_code,
                    )
                    st.session_state.wb_client = wb
                    st.session_state.wb_login_step = "start"
                    st.session_state._wb_pending = None
                    st.rerun()
                except Exception as e:
                    st.error(f"Login failed: {e}")
