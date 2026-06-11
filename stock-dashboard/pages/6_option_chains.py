import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf
from datetime import datetime
from scipy.stats import norm

from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import inject_global_css, page_header, plotly_dark_layout, render_sidebar_nav
from data.options_agent import run_options_analysis

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


# ── Option curves ─────────────────────────────────────────────────────────────

_GREEK_COLORS = {
    "delta": ("#4fc3f7", "rgba(79,195,247,0.1)"),
    "gamma": ("#81c784", "rgba(129,199,132,0.1)"),
    "theta": ("#ef5350", "rgba(239,83,80,0.1)"),
    "vega":  ("#ffb74d", "rgba(255,183,77,0.1)"),
}
_GREEK_LABELS = {
    "delta": "Delta",
    "gamma": "Gamma",
    "theta": "Theta ($/day)",
    "vega":  "Vega (per 1% IV)",
}


def _render_option_curves(
    calls_raw: pd.DataFrame,
    puts_raw: pd.DataFrame,
    current_price: float,
    expiry: str,
    opt_type: str,
) -> None:
    st.subheader("Option Curves")

    # ── IV Smile ──────────────────────────────────────────────────────────────
    def _iv_series(df: pd.DataFrame) -> pd.DataFrame:
        d = df[["strike", "impliedVolatility"]].copy()
        d = d[d["impliedVolatility"].notna() & (d["impliedVolatility"] > 0)]
        d["iv_pct"] = d["impliedVolatility"] * 100
        return d.sort_values("strike")

    calls_iv = _iv_series(calls_raw)
    puts_iv  = _iv_series(puts_raw)

    fig_iv = go.Figure()
    fig_iv.add_trace(go.Scatter(
        x=calls_iv["strike"], y=calls_iv["iv_pct"],
        mode="lines+markers", name="Calls IV",
        line=dict(color="#4fc3f7", width=2), marker=dict(size=4),
    ))
    fig_iv.add_trace(go.Scatter(
        x=puts_iv["strike"], y=puts_iv["iv_pct"],
        mode="lines+markers", name="Puts IV",
        line=dict(color="#ef5350", width=2), marker=dict(size=4),
    ))
    fig_iv.add_vline(
        x=current_price,
        line=dict(color="#ffd600", width=1.5, dash="dash"),
        annotation_text=f"Spot ${current_price:.2f}",
        annotation_position="top right",
        annotation_font_color="#ffd600",
    )
    fig_iv.update_layout(
        **plotly_dark_layout(
            title=f"Implied Volatility Smile — {expiry}",
            xaxis_title="Strike Price ($)",
            yaxis_title="Implied Volatility (%)",
            height=400,
            legend=dict(orientation="h", y=1.02, x=1, xanchor="right",
                        bgcolor="rgba(0,0,0,0)", font=dict(size=11)),
            hovermode="x unified",
        )
    )
    st.plotly_chart(fig_iv, use_container_width=True)

    # ── Greeks Curve ──────────────────────────────────────────────────────────
    expiry_dt = datetime.strptime(expiry, "%Y-%m-%d")
    T = max((expiry_dt - datetime.today()).days / 365.0, 1.0 / 365)

    raw = calls_raw if opt_type == "call" else puts_raw
    greek_rows = []
    for _, row in raw.iterrows():
        iv = float(row["impliedVolatility"]) if pd.notna(row.get("impliedVolatility")) and row.get("impliedVolatility") else 0.0
        if iv <= 0:
            continue
        strike = float(row["strike"])
        g = bs_greeks(current_price, strike, T, RISK_FREE_RATE, iv, opt_type)
        greek_rows.append({"Strike": strike, **g})

    if not greek_rows:
        st.caption("Not enough IV data to plot Greeks curves.")
        return

    gdf = pd.DataFrame(greek_rows).sort_values("Strike")

    selected_greek = st.selectbox(
        "Greek",
        list(_GREEK_COLORS.keys()),
        format_func=lambda k: _GREEK_LABELS[k],
        key="greek_curve_select",
    )

    line_color, fill_color = _GREEK_COLORS[selected_greek]
    label = _GREEK_LABELS[selected_greek]

    fig_g = go.Figure()
    fig_g.add_trace(go.Scatter(
        x=gdf["Strike"], y=gdf[selected_greek],
        mode="lines+markers", name=label,
        line=dict(color=line_color, width=2), marker=dict(size=4),
        fill="tozeroy", fillcolor=fill_color,
    ))
    fig_g.add_vline(
        x=current_price,
        line=dict(color="#ffd600", width=1.5, dash="dash"),
        annotation_text=f"Spot ${current_price:.2f}",
        annotation_position="top right",
        annotation_font_color="#ffd600",
    )
    fig_g.update_layout(
        **plotly_dark_layout(
            title=f"{label} vs Strike — {opt_type.capitalize()}s expiring {expiry}",
            xaxis_title="Strike Price ($)",
            yaxis_title=label,
            height=400,
            hovermode="x unified",
        )
    )
    st.plotly_chart(fig_g, use_container_width=True)


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

render_gemini_usage_bar()
inject_global_css()
render_sidebar_nav()

page_header("Option Chain Viewer", "Live options chain with Black-Scholes Greeks, IV smile, and AI analysis.")

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

st.divider()
_render_option_curves(calls_df, puts_df, current_price, expiry, opt_type)

# ── Gemini AI Analysis ────────────────────────────────────────────────────────

st.divider()
st.subheader("Gemini AI Options Analysis")

_BIAS_COLOR = {"bullish": "#00c853", "bearish": "#ff1744", "neutral": "#ffd600"}
_BIAS_ICON  = {"bullish": "▲", "bearish": "▼", "neutral": "●"}


def _render_analysis(result: dict | None) -> None:
    if result is None:
        st.error("Analysis failed — no response from Gemini.")
        return
    if "_error" in result:
        st.error(f"Gemini error: {result['_error']}")
        return

    bias     = result.get("directional_bias", "neutral")
    strength = result.get("bias_strength", "moderate")
    conf     = result.get("confidence", "medium")
    metrics  = result.get("metrics", {})
    color    = _BIAS_COLOR.get(bias, "#ffd600")
    icon     = _BIAS_ICON.get(bias, "●")

    # ── Bias banner ───────────────────────────────────────────────────────────
    st.markdown(
        f"""
        <div style="background:linear-gradient(135deg,{color}22,{color}11);
                    border-left:4px solid {color};border-radius:6px;
                    padding:14px 18px;margin-bottom:12px">
          <span style="color:{color};font-size:1.5rem;font-weight:700">
            {icon} {bias.upper()} — {strength.capitalize()} Conviction
          </span>
          &nbsp;&nbsp;
          <span style="color:#aaa;font-size:0.9rem">Confidence: {conf.capitalize()}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Raw metric cards ──────────────────────────────────────────────────────
    m1, m2, m3, m4, m5 = st.columns(5)
    pcr_oi  = metrics.get("pcr_oi")
    pcr_vol = metrics.get("pcr_vol")
    max_pain_val = metrics.get("max_pain")
    iv_skew_val  = metrics.get("iv_skew")
    net_gex_val  = metrics.get("net_gex")

    m1.metric("P/C OI Ratio",  f"{pcr_oi:.3f}"  if pcr_oi  is not None else "N/A",
              help="Put open interest ÷ call open interest (full chain). >1 = more puts outstanding.")
    m2.metric("P/C Vol Ratio", f"{pcr_vol:.3f}" if pcr_vol is not None else "N/A",
              help="Put volume ÷ call volume today. >1 = more put buying.")
    m3.metric("Max Pain",      f"${max_pain_val:.2f}" if max_pain_val is not None else "N/A",
              delta=f"{max_pain_val - current_price:+.2f} from spot" if max_pain_val is not None else None,
              help="Strike where combined OI dollar loss to option buyers is maximized at expiry.")
    m4.metric("IV Skew",       f"{iv_skew_val*100:+.2f}%" if iv_skew_val is not None else "N/A",
              help="OTM put avg IV minus OTM call avg IV. Positive = put fear premium (bearish skew).")
    m5.metric("Net Dealer GEX", f"${net_gex_val/1e6:.2f}M" if net_gex_val is not None else "N/A",
              help="Positive = dealers long gamma (price-pinning). Negative = dealers short gamma (vol amplification).")

    st.markdown("---")

    # ── Analysis sections ─────────────────────────────────────────────────────
    sections = [
        ("📊 Implied Volatility Analysis",  "iv_analysis",             True),
        ("⚖️ Put/Call Ratio Analysis",       "pcr_analysis",            True),
        ("🎯 Max Pain Analysis",             "max_pain_analysis",       True),
        ("⚡ Gamma Exposure & Dealer Flows", "gamma_exposure_analysis", False),
        ("🏦 Key Price Levels",              "key_levels",              False),
        ("🚨 Unusual Activity",              "unusual_activity",        False),
        ("⚠️ Risk Factors",                  "risk_factors",            False),
    ]

    for label, field, expanded in sections:
        with st.expander(label, expanded=expanded):
            val = result.get(field, "")
            if isinstance(val, list):
                text = "\n".join(f"- {item}" for item in val)
            else:
                text = str(val).strip()
            if text:
                st.markdown(text)
            else:
                st.caption("No data returned for this section.")

    # ── Summary ───────────────────────────────────────────────────────────────
    summary = result.get("summary", "").strip()
    if summary:
        st.markdown(
            f"""
            <div style="background:#1a1a2e;border-left:4px solid {color};
                        border-radius:6px;padding:16px 20px;margin-top:12px">
              <p style="color:#ddd;font-size:0.95rem;margin:0;line-height:1.7">{summary}</p>
            </div>
            """,
            unsafe_allow_html=True,
        )


cache_key = f"options_analysis_{ticker_sym}_{expiry}_{opt_type}"

col_run, col_hint = st.columns([2, 8])
with col_run:
    run_clicked = st.button("▶ Run Gemini Analysis", use_container_width=True)
with col_hint:
    st.caption(
        "Uses Gemini 2.5 Pro · ~30–90s · "
        "Analyzes P/C ratio, IV skew, max pain, gamma exposure, key levels, unusual activity"
    )

if run_clicked:
    st.session_state.pop(cache_key, None)

if cache_key not in st.session_state and run_clicked:
    with st.spinner("Gemini 2.5 Pro analyzing the option chain…"):
        analysis_result = run_options_analysis(
            ticker_sym, current_price, expiry, opt_type, calls_df, puts_df, display_df
        )
    st.session_state[cache_key] = analysis_result

if cache_key in st.session_state:
    _render_analysis(st.session_state[cache_key])
