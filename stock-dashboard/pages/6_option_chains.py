import numpy as np
import pandas as pd
import streamlit as st
import yfinance as yf
from datetime import datetime
from scipy.stats import norm

RISK_FREE_RATE = 0.045
N_CONTRACTS = 10  # nearest ITM and nearest OTM contracts to show


# ── Greeks ────────────────────────────────────────────────────────────────────

def bs_greeks(S: float, K: float, T: float, r: float, sigma: float, opt_type: str) -> dict:
    """Black-Scholes Greeks.

    Vega and rho are scaled per 1% move (÷100). Theta is per calendar day (÷365).
    """
    zero = dict(delta=0.0, gamma=0.0, theta=0.0, vega=0.0, rho=0.0)
    if T <= 1e-6 or sigma <= 1e-6 or S <= 0 or K <= 0:
        return zero

    sqrt_T = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    pdf_d1 = norm.pdf(d1)

    gamma = pdf_d1 / (S * sigma * sqrt_T)
    vega = S * pdf_d1 * sqrt_T / 100

    if opt_type == "call":
        delta = norm.cdf(d1)
        theta = (-(S * pdf_d1 * sigma) / (2 * sqrt_T) - r * K * np.exp(-r * T) * norm.cdf(d2)) / 365
        rho = K * T * np.exp(-r * T) * norm.cdf(d2) / 100
    else:
        delta = norm.cdf(d1) - 1.0
        theta = (-(S * pdf_d1 * sigma) / (2 * sqrt_T) + r * K * np.exp(-r * T) * norm.cdf(-d2)) / 365
        rho = -K * T * np.exp(-r * T) * norm.cdf(-d2) / 100

    return dict(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho)


# ── Chain filtering ───────────────────────────────────────────────────────────

def build_display_df(
    raw: pd.DataFrame,
    current_price: float,
    expiry: str,
    opt_type: str,
    n: int = N_CONTRACTS,
) -> pd.DataFrame:
    """Return n nearest ITM + n nearest OTM contracts with computed Greeks."""
    expiry_dt = datetime.strptime(expiry, "%Y-%m-%d")
    T = max((expiry_dt - datetime.today()).days / 365.0, 1.0 / 365)

    if opt_type == "call":
        itm = raw[raw["strike"] < current_price].nlargest(n, "strike")
        otm = raw[raw["strike"] >= current_price].nsmallest(n, "strike")
    else:
        itm = raw[raw["strike"] > current_price].nsmallest(n, "strike")
        otm = raw[raw["strike"] <= current_price].nlargest(n, "strike")

    def _float(v) -> float:
        return float(v) if pd.notna(v) and v else 0.0

    def _int(v) -> int:
        return int(v) if pd.notna(v) and v else 0

    rows = []
    for _, row in pd.concat([itm, otm]).sort_values("strike").iterrows():
        iv = _float(row.get("impliedVolatility"))
        g = bs_greeks(current_price, float(row["strike"]), T, RISK_FREE_RATE, iv, opt_type)
        rows.append({
            "ITM":       bool(row.get("inTheMoney", False)),
            "Strike":    float(row["strike"]),
            "Bid":       _float(row.get("bid")),
            "Ask":       _float(row.get("ask")),
            "Last":      _float(row.get("lastPrice")),
            "Volume":    _int(row.get("volume")),
            "Open Int.": _int(row.get("openInterest")),
            "IV %":      round(iv * 100, 2),
            "Delta":     round(g["delta"], 4),
            "Gamma":     round(g["gamma"], 6),
            "Theta":     round(g["theta"], 4),
            "Vega":      round(g["vega"], 4),
            "Rho":       round(g["rho"], 4),
        })

    return pd.DataFrame(rows)


# ── Cached data fetchers ──────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def fetch_price_and_expirations(sym: str):
    t = yf.Ticker(sym)
    try:
        price = float(t.fast_info["last_price"])
    except Exception:
        price = None
    return price, list(t.options)


@st.cache_data(ttl=60)
def fetch_chain(sym: str, exp: str):
    chain = yf.Ticker(sym).option_chain(exp)
    return chain.calls, chain.puts


# ── Streamlit page ────────────────────────────────────────────────────────────

st.set_page_config(page_title="Option Chain Viewer", layout="wide")
st.title("Option Chain Viewer")

default_ticker = st.session_state.get("active_ticker", "AAPL") or "AAPL"

col_sym, col_btn = st.columns([5, 1])
with col_sym:
    ticker_sym = st.text_input("Ticker Symbol", value=default_ticker).upper().strip()
with col_btn:
    st.markdown("<br>", unsafe_allow_html=True)
    st.button("Reload", use_container_width=True)

if not ticker_sym:
    st.stop()

try:
    current_price, expirations = fetch_price_and_expirations(ticker_sym)
except Exception as e:
    st.error(f"Failed to fetch data for **{ticker_sym}**: {e}")
    st.stop()

if not expirations:
    st.warning(f"No options listed for **{ticker_sym}**. Check the ticker symbol.")
    st.stop()

if current_price is None:
    st.warning(f"Could not retrieve a current price for **{ticker_sym}**.")
    st.stop()

ctrl_price, ctrl_exp, ctrl_type = st.columns([2, 3, 2])
with ctrl_price:
    st.metric("Current Price", f"${current_price:,.2f}")
with ctrl_exp:
    expiry = st.selectbox("Expiration Date", expirations)
with ctrl_type:
    contract_label = st.radio("Contract Type", ["Calls", "Puts"], horizontal=True)

opt_type = "call" if contract_label == "Calls" else "put"

try:
    calls_df, puts_df = fetch_chain(ticker_sym, expiry)
except Exception as e:
    st.error(f"Error loading option chain for {expiry}: {e}")
    st.stop()

raw = calls_df if opt_type == "call" else puts_df

with st.spinner("Calculating Greeks…"):
    display_df = build_display_df(raw, current_price, expiry, opt_type)

if display_df.empty:
    st.info("No contracts to display for this selection.")
    st.stop()

st.caption(
    f"{N_CONTRACTS} nearest ITM + {N_CONTRACTS} nearest OTM {contract_label.lower()} · "
    f"Greeks via Black-Scholes · Risk-free rate assumed {RISK_FREE_RATE * 100:.1f}%"
)

st.dataframe(
    display_df,
    use_container_width=True,
    hide_index=True,
    column_config={
        "ITM":       st.column_config.CheckboxColumn("ITM"),
        "Strike":    st.column_config.NumberColumn("Strike",    format="$%.2f"),
        "Bid":       st.column_config.NumberColumn("Bid",       format="$%.2f"),
        "Ask":       st.column_config.NumberColumn("Ask",       format="$%.2f"),
        "Last":      st.column_config.NumberColumn("Last",      format="$%.2f"),
        "Volume":    st.column_config.NumberColumn("Volume",    format="%d"),
        "Open Int.": st.column_config.NumberColumn("Open Int.", format="%d"),
        "IV %":      st.column_config.NumberColumn("IV %",      format="%.2f%%"),
        "Delta":     st.column_config.NumberColumn("Delta",     format="%.4f"),
        "Gamma":     st.column_config.NumberColumn("Gamma",     format="%.6f"),
        "Theta":     st.column_config.NumberColumn("Theta",     format="%.4f"),
        "Vega":      st.column_config.NumberColumn("Vega",      format="%.4f"),
        "Rho":       st.column_config.NumberColumn("Rho",       format="%.4f"),
    },
)
