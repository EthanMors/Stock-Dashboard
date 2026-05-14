import time

import numpy as np
import streamlit as st
import pandas as pd
import yfinance as yf
from datetime import datetime
from scipy.stats import norm

from components.gemini_usage_bar import render_gemini_usage_bar
from data.options_agent import run_options_analysis
from data.webull_positions import (
    is_configured,
    get_account_list,
    get_balance,
    get_env_account_ids,
    get_positions,
)
from data.news_fetcher import fetch_news, scrape_article
from data.news_analyzer import analyze_articles, get_sector_info
from data.portfolio_cache import (
    get_latest_analysis,
    save_analysis,
    has_new_articles,
    get_sentiment_history,
    save_options_analysis,
    get_latest_options_analysis,
    is_options_analysis_fresh,
)
from data.hedge_fund_fetcher import get_all_funds_from_db, refresh_hedge_fund_db

st.set_page_config(page_title="Portfolio", layout="wide")


@st.cache_data(ttl=300)
def _cached_account_list():
    return get_account_list()


@st.cache_data(ttl=300)
def _cached_balance(account_id: str):
    return get_balance(account_id)


@st.cache_data(ttl=300)
def _cached_positions(account_id: str):
    return get_positions(account_id)

render_gemini_usage_bar()

st.title("Portfolio")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_MIN_TICKER_ARTICLES = 3   # if fewer direct articles, also pull sector news
_MAX_ARTICLES_TO_SCRAPE = 5
_TICKER_FIELD_CANDIDATES = ["symbol", "ticker", "tickerSymbol", "stockSymbol", "sym"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_ticker(position: dict) -> str:
    for field in _TICKER_FIELD_CANDIDATES:
        val = position.get(field, "")
        if val and isinstance(val, str):
            return val.upper().strip()
    return ""


def _get_portfolio_tickers(positions: list) -> list:
    """Extract unique uppercase ticker strings from a list of position dicts.

    Iterates each position and tries each key in _TICKER_FIELD_CANDIDATES in order.
    Returns a list of unique, non-empty, uppercased ticker strings. Preserves
    insertion order (first occurrence of each ticker wins).
    """
    seen: set = set()
    result: list = []
    for pos in positions:
        t = _extract_ticker(pos)
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


def _find_overlapping_funds(portfolio_tickers: list) -> list:
    """Find concentrated hedge funds that hold any ticker in portfolio_tickers.

    Calls get_all_funds_from_db() to read from the smart cache DB. For each fund,
    checks which of its holdings' tickers are in the portfolio. Returns only funds
    with at least one overlapping holding, sorted by overlap_count descending.

    Args:
        portfolio_tickers: List of uppercase ticker strings (e.g. ['AAPL', 'TSLA']).

    Returns:
        List of dicts, each with keys:
            name (str), cik (str), report_period (str), filing_date (str),
            total_value (float), overlapping_holdings (list of HoldingRow dicts),
            overlap_count (int).
    """
    if not portfolio_tickers:
        return []

    portfolio_set = set(t.upper() for t in portfolio_tickers)

    try:
        refresh_hedge_fund_db()
        all_funds = get_all_funds_from_db()
    except Exception:
        return []

    overlapping = []
    for fund in all_funds:
        holdings = fund.get("holdings", [])
        matches = [
            h for h in holdings
            if str(h.get("ticker", "")).upper() in portfolio_set
        ]
        if not matches:
            continue
        overlapping.append({
            "name": fund.get("name", ""),
            "cik": fund.get("cik", ""),
            "report_period": fund.get("report_period", ""),
            "filing_date": fund.get("filing_date", ""),
            "total_value": fund.get("total_value", 0.0),
            "overlapping_holdings": matches,
            "overlap_count": len(matches),
        })

    overlapping.sort(key=lambda x: x["overlap_count"], reverse=True)
    return overlapping


def _render_hedge_fund_overlap(positions: list) -> None:
    """Render the Hedge Fund Overlap section for the given positions list.

    Shows a table of overlapping holdings inside an st.expander for each
    concentrated hedge fund that holds any ticker from the current portfolio.
    Displays a friendly info message when no overlaps are found.

    Args:
        positions: Raw list of position dicts from get_positions(account_id).
    """
    portfolio_tickers = _get_portfolio_tickers(positions)

    if not portfolio_tickers:
        st.info("No ticker symbols found in your positions — cannot check hedge fund overlap.")
        return

    with st.spinner("Checking hedge fund holdings…"):
        overlapping = _find_overlapping_funds(portfolio_tickers)

    if not overlapping:
        st.info(
            "No concentrated hedge funds (< 15 positions) hold any of your current positions "
            "based on the most recent 13F-HR filings in the database. "
            "The database is populated from SEC EDGAR during the 45-day filing window after each quarter end."
        )
        return

    st.caption(
        f"Checking {len(portfolio_tickers)} portfolio ticker(s) against "
        f"{len(overlapping)} concentrated fund(s) with overlapping positions."
    )

    for fund in overlapping:
        overlap_count = fund["overlap_count"]
        fund_name = fund["name"] or fund["cik"]
        header = f"{fund_name} — {overlap_count} shared position{'s' if overlap_count != 1 else ''}"

        with st.expander(header, expanded=False):
            meta_col1, meta_col2, meta_col3 = st.columns(3)
            meta_col1.metric("Report Period", fund["report_period"] or "—")
            meta_col2.metric("Filing Date", fund["filing_date"] or "—")

            total_val = fund["total_value"]
            if total_val >= 1e9:
                val_str = f"${total_val / 1e9:.2f}B"
            elif total_val >= 1e6:
                val_str = f"${total_val / 1e6:.2f}M"
            elif total_val >= 1e3:
                val_str = f"${total_val / 1e3:.2f}K"
            else:
                val_str = f"${total_val:,.0f}"
            meta_col3.metric("Fund Portfolio Value", val_str)

            st.markdown("**Overlapping Holdings**")
            rows = []
            for h in fund["overlapping_holdings"]:
                rows.append({
                    "Ticker": str(h.get("ticker", "")).upper() or "—",
                    "Issuer": str(h.get("issuer", "")) or "—",
                    "% of Fund": f"{h.get('pct_of_portfolio', 0.0):.1f}%",
                    "Value": (
                        f"${h.get('value', 0.0) / 1e6:.2f}M"
                        if h.get("value", 0.0) >= 1e6
                        else f"${h.get('value', 0.0):,.0f}"
                    ),
                    "Type": str(h.get("put_call", "")) or "Equity",
                })
            if rows:
                overlap_df = pd.DataFrame(rows)
                st.dataframe(overlap_df, use_container_width=True, hide_index=True)


def _sentiment_color(label: str) -> str:
    return {"positive": "#2ecc71", "negative": "#e74c3c"}.get(label, "#95a5a6")


def _impact_bar(level: int) -> str:
    filled = min(max(level, 0), 10)
    color = "#e74c3c" if filled >= 7 else "#f39c12" if filled >= 4 else "#2ecc71"
    return (
        f'<div style="background:#333;border-radius:4px;height:8px;width:100%">'
        f'<div style="background:{color};border-radius:4px;height:8px;width:{filled * 10}%"></div>'
        f"</div>"
    )


def _render_analysis(ticker: str, result: dict) -> None:
    label = result["sentiment_label"]
    score = result["sentiment_score"]
    color = _sentiment_color(label)
    impact = result["impact_level"]
    themes = result.get("key_themes", [])
    summary = result.get("summary", "")
    is_specific = result.get("is_stock_specific", True)

    badge = "Stock-Specific" if is_specific else "Sector-Level Fallback"
    badge_color = "#3498db" if is_specific else "#9b59b6"

    st.markdown(
        f"""
        <div style="border:1px solid #333;border-radius:8px;padding:16px;margin-bottom:12px">
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">
            <span style="font-size:1.4rem;font-weight:700;color:{color}">
              {label.upper()} &nbsp; {score:+.2f}
            </span>
            <span style="background:{badge_color};color:#fff;padding:2px 8px;
                         border-radius:12px;font-size:0.75rem">{badge}</span>
          </div>
          <div style="margin-bottom:8px">
            <span style="font-size:0.8rem;color:#aaa">Impact Level {impact}/10</span>
            {_impact_bar(impact)}
          </div>
          {"<div style='margin-bottom:10px'>" + "".join(
              f'<span style="background:#1e1e2e;border:1px solid #555;border-radius:12px;'
              f'padding:2px 10px;font-size:0.75rem;margin-right:6px;display:inline-block">{t}</span>'
              for t in themes
          ) + "</div>" if themes else ""}
          <p style="color:#ccc;font-size:0.9rem;line-height:1.5;margin:0">{summary}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _run_analysis_for_ticker(ticker: str, previous_analysis: dict | None = None) -> dict:
    """Fetch articles, scrape, and call Gemini for *ticker*.

    Returns the session-state cache dict:
    {
        "result": <analyze_articles() return dict>,
        "article_count": int,
        "sector": str,
        "is_fallback": bool,
        "analyzed_at": str ISO timestamp,
        "from_db": bool,
    }
    Also persists the result to the SQLite DB via save_analysis().
    """
    articles = fetch_news(ticker, limit=20)
    scraped: list[dict] = []
    for art in articles[:_MAX_ARTICLES_TO_SCRAPE]:
        url = art.get("article_url", "")
        content = scrape_article(url) if url else ""
        if content.startswith(("HTTP", "Access denied", "Request failed", "Could not")):
            content = art.get("description", "")
        scraped.append({
            "title": art.get("title", ""),
            "content": content,
            "source": (
                art.get("publisher", {}).get("name", "")
                if isinstance(art.get("publisher"), dict)
                else str(art.get("publisher", ""))
            ),
        })

    is_fallback = False
    sector = ""
    if len(scraped) < _MIN_TICKER_ARTICLES:
        sector, etf = get_sector_info(ticker)
        if etf:
            sector_articles = fetch_news(etf, limit=10)
            for art in sector_articles[:_MAX_ARTICLES_TO_SCRAPE]:
                url = art.get("article_url", "")
                content = scrape_article(url) if url else ""
                if content.startswith(("HTTP", "Access denied", "Request failed", "Could not")):
                    content = art.get("description", "")
                scraped.append({
                    "title": art.get("title", ""),
                    "content": content,
                    "source": (
                        art.get("publisher", {}).get("name", "")
                        if isinstance(art.get("publisher"), dict)
                        else str(art.get("publisher", ""))
                    ),
                })
            is_fallback = True

    result = analyze_articles(scraped, ticker, sector=sector, is_sector_fallback=is_fallback, previous_analysis=previous_analysis)
    save_analysis(
        ticker=ticker,
        result_dict=result,
        article_count=len(articles),
        sector=sector,
        is_fallback=is_fallback,
        articles=articles,
    )
    from datetime import datetime, timezone
    analyzed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "result": result,
        "article_count": len(articles),
        "sector": sector,
        "is_fallback": is_fallback,
        "analyzed_at": analyzed_at,
        "from_db": False,
    }


def _load_or_analyze(ticker: str) -> dict:
    """Return a session-cache dict for *ticker*, using DB cache when valid.

    Decision tree:
    1. Check st.session_state.news_results — return immediately if present
       (in-session memoization; no DB hit needed).
    2. Query DB for latest analysis via get_latest_analysis().
    3. Fetch fresh article list (fast — already @st.cache_data(ttl=900)).
    4. Call has_new_articles() to detect staleness.
    5a. If NOT stale: populate session_state from DB row and return.
    5b. If stale (or no DB row): call _run_analysis_for_ticker() which
        runs Gemini and saves the new result to DB.
    """
    ticker = ticker.upper()

    # Layer 1: in-session memoization
    if ticker in st.session_state.news_results:
        return st.session_state.news_results[ticker]

    # Layer 2: DB lookup
    cached_db = get_latest_analysis(ticker)
    articles = fetch_news(ticker, limit=20)

    if cached_db is not None and not has_new_articles(ticker, articles):
        # No new articles — serve DB result
        session_entry = {
            "result": {
                "sentiment_score": cached_db["sentiment_score"],
                "sentiment_label": cached_db["sentiment_label"],
                "summary": cached_db["summary"],
                "impact_level": cached_db["impact_level"],
                "key_themes": cached_db["key_themes"],
                "is_stock_specific": cached_db["is_stock_specific"],
            },
            "article_count": cached_db["article_count"],
            "sector": cached_db["sector"] or "",
            "is_fallback": cached_db["is_fallback"],
            "analyzed_at": cached_db["analyzed_at"],
            "from_db": True,
        }
        st.session_state.news_results[ticker] = session_entry
        return session_entry

    # Layer 3: new articles exist (or first-ever analysis) — run Gemini
    session_entry = _run_analysis_for_ticker(ticker, previous_analysis=cached_db)
    st.session_state.news_results[ticker] = session_entry
    return session_entry


# ---------------------------------------------------------------------------
# Account loading (mirrors 7_positions.py)
# ---------------------------------------------------------------------------
if not is_configured():
    st.error("Webull API credentials not configured.")
    st.markdown("Add `WEBULL_APP_KEY` and `WEBULL_APP_SECRET` to your `.env` file and restart.")
    st.stop()

env_ids = get_env_account_ids()

with st.spinner("Fetching account list…"):
    account_list_result = _cached_account_list()

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

accounts: dict[str, dict] = {}
with st.spinner("Fetching account balances…"):
    for aid in account_ids:
        balance = _cached_balance(aid)
        if isinstance(balance, dict) and "error" not in balance:
            label = id_to_label.get(aid) or aid
            accounts[label] = balance

if not accounts:
    st.warning("No balance data returned. Check your credentials.")
    st.stop()

label_to_id: dict[str, str] = {}
for aid in account_ids:
    lbl = id_to_label.get(aid) or aid
    label_to_id[lbl] = aid

selected_label = st.selectbox("Account", list(accounts.keys()))
selected_account_id = label_to_id.get(selected_label, selected_label)

# ---------------------------------------------------------------------------
# Balance summary
# ---------------------------------------------------------------------------
selected_balance = accounts[selected_label]
balance_fields = [
    "total_net_liquidation_value",
    "total_market_value",
    "total_cash_balance",
    "total_unrealized_profit_loss",
    "total_day_profit_loss",
]
display = {k: selected_balance.get(k) for k in balance_fields if k in selected_balance}
if display:
    df_bal = pd.DataFrame([display])
    df_bal.columns = [c.replace("_", " ").title() for c in df_bal.columns]
    st.dataframe(df_bal, use_container_width=True, hide_index=True)
else:
    st.json(selected_balance)

# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------
st.subheader("Positions")

with st.spinner("Fetching positions…"):
    positions_result = _cached_positions(selected_account_id)

if isinstance(positions_result, dict) and "error" in positions_result:
    st.error(f"API error: {positions_result['error']}")
    st.stop()
elif not positions_result:
    st.info("No positions found for this account.")
    st.stop()

if isinstance(positions_result, dict):
    positions_result = [positions_result]

# Extract tickers from positions
tickers: list[str] = []
for pos in positions_result:
    t = _extract_ticker(pos)
    if t and t not in tickers:
        tickers.append(t)

df_pos = pd.DataFrame(positions_result)

# Drop unwanted columns before renaming
_drop_raw = ("positionid", "instrumenttype")
_drop_cols = [c for c in df_pos.columns if c.lower().replace("_", "") in _drop_raw]
df_pos = df_pos.drop(columns=_drop_cols, errors="ignore")

# Convert proportion to percentage string
for _col in [c for c in df_pos.columns if c.lower() == "proportion"]:
    df_pos[_col] = (df_pos[_col].astype(float) * 100).round(2).astype(str) + "%"

# Reorder: symbol leftmost, currency rightmost
_sym_candidates = {"symbol", "ticker", "tickersymbol", "stocksymbol", "sym"}
_sym_col = next((c for c in df_pos.columns if c.lower().replace("_", "") in _sym_candidates), None)
_cur_col = next((c for c in df_pos.columns if c.lower() == "currency"), None)
_middle = [c for c in df_pos.columns if c not in (_sym_col, _cur_col)]
df_pos = df_pos[[c for c in ([_sym_col] + _middle + [_cur_col]) if c is not None]]

df_pos.columns = [c.replace("_", " ").title() for c in df_pos.columns]

def _color_pnl(val):
    try:
        v = float(val)
        if v > 0:
            return "color: #2ecc71"
        if v < 0:
            return "color: #e74c3c"
    except (TypeError, ValueError):
        pass
    return ""

# Only color P&L and rate columns — not neutral positives like market value or quantity
_pnl_keywords = ("profit", "loss", "return", "change", "rate")
_pnl_cols = [c for c in df_pos.columns if any(kw in c.lower() for kw in _pnl_keywords)]
styled = df_pos.style
if _pnl_cols:
    styled = styled.map(_color_pnl, subset=_pnl_cols)
_row_height = 35
_header_height = 38
st.dataframe(styled, use_container_width=True, hide_index=True,
             height=_header_height + _row_height * len(df_pos))

if not tickers:
    st.warning("Could not extract ticker symbols from position data — news analysis unavailable.")
    st.stop()

# ---------------------------------------------------------------------------
# News Analysis
# ---------------------------------------------------------------------------
st.markdown("---")
st.subheader("News Analysis")

if "news_results" not in st.session_state:
    st.session_state.news_results = {}

col_a, col_b = st.columns([3, 1])
with col_a:
    selected_ticker = st.selectbox("Analyze news for", ["— Select a stock —"] + tickers)
with col_b:
    analyze_all = st.button("Analyze All Positions", use_container_width=True)

# Single ticker analysis
if selected_ticker and selected_ticker != "— Select a stock —":
    if st.button(f"Run News Analysis for {selected_ticker}") or selected_ticker in st.session_state.news_results:
        with st.spinner(f"Fetching and analyzing news for {selected_ticker}…"):
            cached = _load_or_analyze(selected_ticker)

        article_count = cached["article_count"]
        sector = cached["sector"]
        is_fallback = cached["is_fallback"]
        from_db = cached.get("from_db", False)
        analyzed_at = cached.get("analyzed_at", "")

        if from_db and analyzed_at:
            st.info(f"Cached analysis from {analyzed_at} UTC — no new articles detected.")
        else:
            st.success(f"Freshly analyzed at {analyzed_at} UTC.")
        note = f"{article_count} articles found"
        if is_fallback and sector:
            note += f" — supplemented with {sector} sector news"
        st.caption(note)
        _render_analysis(selected_ticker, cached["result"])

# Analyze all positions
if analyze_all:
    progress = st.progress(0, text="Starting analysis…")
    for idx, ticker in enumerate(tickers):
        if ticker in st.session_state.news_results and not st.session_state.news_results[ticker].get("from_db", False):
            # Already analyzed fresh this session — skip without DB overhead
            progress.progress((idx + 1) / len(tickers), text=f"Skipping {ticker} (session cache)")
            continue

        progress.progress(idx / len(tickers), text=f"Checking cache for {ticker}…")
        with st.spinner(f"Loading {ticker}…"):
            entry = _load_or_analyze(ticker)
        if entry.get("from_db", False):
            progress.progress((idx + 1) / len(tickers), text=f"Using DB cache: {ticker}")
        else:
            progress.progress((idx + 1) / len(tickers), text=f"Done (Gemini): {ticker}")
            time.sleep(2)

    progress.empty()

# Display all cached results
if st.session_state.news_results:
    st.markdown("### Analysis Results")
    for ticker, cached in st.session_state.news_results.items():
        sentiment_label = cached["result"]["sentiment_label"].upper()
        sentiment_score = cached["result"]["sentiment_score"]
        from_db = cached.get("from_db", False)
        analyzed_at = cached.get("analyzed_at", "")
        cache_tag = " [cached]" if from_db else ""
        with st.expander(
            f"**{ticker}** — {sentiment_label} ({sentiment_score:+.2f}){cache_tag}",
            expanded=False,
        ):
            if from_db and analyzed_at:
                st.info(f"Cached from {analyzed_at} UTC — no new articles detected.")
            elif analyzed_at:
                st.success(f"Freshly analyzed at {analyzed_at} UTC.")
            note = f"{cached['article_count']} articles"
            if cached["is_fallback"] and cached["sector"]:
                note += f" · {cached['sector']} sector fallback"
            st.caption(note)
            _render_analysis(ticker, cached["result"])

    if st.button("Clear All Results"):
        st.session_state.news_results = {}
        st.rerun()

# ---------------------------------------------------------------------------
# Options Analysis
# ---------------------------------------------------------------------------
st.markdown("---")
st.subheader("Options Analysis")

_RISK_FREE_RATE = 0.045
_N_CONTRACTS = 10


def _bs_greeks(S: float, K: float, T: float, r: float, sigma: float, opt_type: str) -> dict:
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


def _build_options_display_df(raw: pd.DataFrame, current_price: float, expiry: str, opt_type: str) -> pd.DataFrame:
    expiry_dt = datetime.strptime(expiry, "%Y-%m-%d")
    T = max((expiry_dt - datetime.today()).days / 365.0, 1.0 / 365)
    n = _N_CONTRACTS
    if opt_type == "call":
        itm = raw[raw["strike"] < current_price].nlargest(n, "strike")
        otm = raw[raw["strike"] >= current_price].nsmallest(n, "strike")
    else:
        itm = raw[raw["strike"] > current_price].nsmallest(n, "strike")
        otm = raw[raw["strike"] <= current_price].nlargest(n, "strike")

    def _f(v) -> float:
        return float(v) if pd.notna(v) and v else 0.0

    def _i(v) -> int:
        return int(v) if pd.notna(v) and v else 0

    rows = []
    for _, row in pd.concat([itm, otm]).sort_values("strike").iterrows():
        iv = _f(row.get("impliedVolatility"))
        g = _bs_greeks(current_price, float(row["strike"]), T, _RISK_FREE_RATE, iv, opt_type)
        rows.append({
            "ITM":       bool(row.get("inTheMoney", False)),
            "Strike":    float(row["strike"]),
            "Bid":       _f(row.get("bid")),
            "Ask":       _f(row.get("ask")),
            "Last":      _f(row.get("lastPrice")),
            "Volume":    _i(row.get("volume")),
            "Open Int.": _i(row.get("openInterest")),
            "IV %":      round(iv * 100, 2),
            "Delta":     round(g["delta"], 4),
            "Gamma":     round(g["gamma"], 6),
            "Theta":     round(g["theta"], 4),
            "Vega":      round(g["vega"], 4),
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=60)
def _fetch_price_and_expirations(sym: str):
    t = yf.Ticker(sym)
    try:
        price = float(t.fast_info["last_price"])
    except Exception:
        price = None
    return price, list(t.options)


@st.cache_data(ttl=60)
def _fetch_chain(sym: str, exp: str):
    chain = yf.Ticker(sym).option_chain(exp)
    return chain.calls, chain.puts


_OPT_BIAS_COLOR = {"bullish": "#00c853", "bearish": "#ff1744", "neutral": "#ffd600"}
_OPT_BIAS_ICON  = {"bullish": "▲", "bearish": "▼", "neutral": "●"}


def _render_options_analysis(result: dict, spot: float) -> None:
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
    color    = _OPT_BIAS_COLOR.get(bias, "#ffd600")
    icon     = _OPT_BIAS_ICON.get(bias, "●")

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

    m1, m2, m3, m4, m5 = st.columns(5)
    pcr_oi       = metrics.get("pcr_oi")
    pcr_vol      = metrics.get("pcr_vol")
    max_pain_val = metrics.get("max_pain")
    iv_skew_val  = metrics.get("iv_skew")
    net_gex_val  = metrics.get("net_gex")

    m1.metric("P/C OI Ratio",   f"{pcr_oi:.3f}"  if pcr_oi  is not None else "N/A",
              help="Put OI ÷ call OI across full chain. >1 = more puts outstanding.")
    m2.metric("P/C Vol Ratio",  f"{pcr_vol:.3f}" if pcr_vol is not None else "N/A",
              help="Put volume ÷ call volume today.")
    m3.metric("Max Pain",       f"${max_pain_val:.2f}" if max_pain_val is not None else "N/A",
              delta=f"{max_pain_val - spot:+.2f} from spot" if max_pain_val is not None else None,
              help="Strike maximizing combined OI dollar loss to buyers at expiry.")
    m4.metric("IV Skew",        f"{iv_skew_val*100:+.2f}%" if iv_skew_val is not None else "N/A",
              help="OTM put avg IV − OTM call avg IV. Positive = bearish fear premium.")
    m5.metric("Net Dealer GEX", f"${net_gex_val/1e6:.2f}M" if net_gex_val is not None else "N/A",
              help="Positive = dealers long gamma (pinning). Negative = vol amplification.")

    st.markdown("---")

    sections = [
        ("📊 Implied Volatility",          "iv_analysis",             True),
        ("⚖️ Put/Call Ratio",               "pcr_analysis",            True),
        ("🎯 Max Pain",                     "max_pain_analysis",       True),
        ("⚡ Gamma Exposure & Flows",       "gamma_exposure_analysis", False),
        ("🏦 Key Price Levels",             "key_levels",              False),
        ("🚨 Unusual Activity",             "unusual_activity",        False),
        ("⚠️ Risk Factors",                 "risk_factors",            False),
    ]
    for label, field, expanded in sections:
        with st.expander(label, expanded=expanded):
            val = result.get(field, "")
            text = "\n".join(f"- {item}" for item in val) if isinstance(val, list) else str(val).strip()
            if text:
                st.markdown(text)
            else:
                st.caption("No data returned.")

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


# ── Options Analysis helpers ───────────────────────────────────────────────────

_OPT_COL_CONFIG = {
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
}

if "options_results" not in st.session_state:
    st.session_state.options_results = {}


def _opt_session_key(ticker: str, expiry: str, opt_type: str) -> str:
    return f"{ticker}|{expiry}|{opt_type}"


def _load_or_analyze_options(
    ticker: str,
    expiry: str,
    opt_type: str,
    price: float,
    calls_df,
    puts_df,
    calls_display_df,
    puts_display_df,
) -> dict:
    """Return options analysis from session state → DB cache → Gemini (in that order)."""
    sess_key = _opt_session_key(ticker, expiry, opt_type)

    if sess_key in st.session_state.options_results:
        return st.session_state.options_results[sess_key]

    cached_db = get_latest_options_analysis(ticker, expiry, opt_type)
    if cached_db is not None and is_options_analysis_fresh(cached_db.get("analyzed_at", "")):
        entry = {**cached_db, "from_db": True}
        st.session_state.options_results[sess_key] = entry
        return entry

    result = run_options_analysis(ticker, price, expiry, opt_type, calls_df, puts_df, calls_display_df, puts_display_df)
    if result and "_error" not in result:
        save_options_analysis(ticker, expiry, opt_type, price, result)
    from datetime import datetime as _dt, timezone as _tz
    analyzed_at = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%S")
    entry = {**result, "analyzed_at": analyzed_at, "from_db": False}
    st.session_state.options_results[sess_key] = entry
    return entry


# ── Options Analysis UI (fragment = reruns only this section) ─────────────────

@st.fragment
def _options_analysis_ui(tickers: list[str]) -> None:
    oa_col_a, oa_col_b = st.columns([3, 1])
    with oa_col_a:
        opt_ticker = st.selectbox(
            "Analyze options for",
            ["— Select a stock —"] + tickers,
            key="opt_ticker_select",
        )
    with oa_col_b:
        opt_analyze_all = st.button("Analyze All Positions", use_container_width=True, key="opt_analyze_all_btn")

    if opt_analyze_all:
        oa_progress = st.progress(0, text="Starting options analysis…")
        for idx, t in enumerate(tickers):
            oa_progress.progress(idx / len(tickers), text=f"Fetching options for {t}…")
            try:
                t_price, t_expirations = _fetch_price_and_expirations(t)
            except Exception:
                oa_progress.progress((idx + 1) / len(tickers), text=f"Skipped {t} — could not fetch options")
                continue
            if not t_expirations or t_price is None:
                oa_progress.progress((idx + 1) / len(tickers), text=f"Skipped {t} — no options listed")
                continue
            nearest_expiry = t_expirations[0]
            sess_key = _opt_session_key(t, nearest_expiry, "put")
            cached_db = get_latest_options_analysis(t, nearest_expiry, "put")
            if cached_db is not None and is_options_analysis_fresh(cached_db.get("analyzed_at", "")):
                if sess_key not in st.session_state.options_results:
                    st.session_state.options_results[sess_key] = {**cached_db, "from_db": True}
                oa_progress.progress((idx + 1) / len(tickers), text=f"Using cache: {t}")
                continue
            try:
                t_calls, t_puts = _fetch_chain(t, nearest_expiry)
                t_calls_display = _build_options_display_df(t_calls, t_price, nearest_expiry, "call")
                t_puts_display  = _build_options_display_df(t_puts,  t_price, nearest_expiry, "put")
            except Exception:
                oa_progress.progress((idx + 1) / len(tickers), text=f"Skipped {t} — chain fetch failed")
                continue
            oa_progress.progress(idx / len(tickers), text=f"Analyzing {t} with Gemini…")
            result = run_options_analysis(t, t_price, nearest_expiry, "put", t_calls, t_puts, t_calls_display, t_puts_display)
            if result and "_error" not in result:
                save_options_analysis(t, nearest_expiry, "put", t_price, result)
            from datetime import datetime as _dt2, timezone as _tz2
            entry = {**result, "analyzed_at": _dt2.now(_tz2.utc).strftime("%Y-%m-%dT%H:%M:%S"), "from_db": False}
            st.session_state.options_results[sess_key] = entry
            oa_progress.progress((idx + 1) / len(tickers), text=f"Done: {t}")
            time.sleep(2)
        oa_progress.empty()

    if opt_ticker and opt_ticker != "— Select a stock —":
        try:
            opt_price, opt_expirations = _fetch_price_and_expirations(opt_ticker)
        except Exception as e:
            st.error(f"Failed to fetch options data for {opt_ticker}: {e}")
            opt_expirations = []
            opt_price = None

        if not opt_expirations:
            st.warning(f"No options listed for {opt_ticker}.")
        elif opt_price is None:
            st.warning(f"Could not retrieve current price for {opt_ticker}.")
        else:
            oa_ctrl1, oa_ctrl2, oa_ctrl3 = st.columns([2, 3, 2])
            with oa_ctrl1:
                st.metric("Current Price", f"${opt_price:,.2f}")
            with oa_ctrl2:
                opt_expiry = st.selectbox("Expiration Date", opt_expirations, key="opt_expiry_select")
            with oa_ctrl3:
                opt_contract_label = st.radio("Contract Type", ["Calls", "Puts"], horizontal=True, key="opt_contract_radio")

            opt_type = "call" if opt_contract_label == "Calls" else "put"

            try:
                opt_calls_df, opt_puts_df = _fetch_chain(opt_ticker, opt_expiry)
            except Exception as e:
                st.error(f"Error loading option chain for {opt_expiry}: {e}")
                opt_calls_df = opt_puts_df = None

            if opt_calls_df is not None:
                with st.spinner("Calculating Greeks…"):
                    opt_calls_display_df = _build_options_display_df(opt_calls_df, opt_price, opt_expiry, "call")
                    opt_puts_display_df  = _build_options_display_df(opt_puts_df,  opt_price, opt_expiry, "put")

                opt_display_df = opt_calls_display_df if opt_type == "call" else opt_puts_display_df

                if not opt_display_df.empty:
                    st.caption(
                        f"{_N_CONTRACTS} nearest ITM + {_N_CONTRACTS} nearest OTM {opt_contract_label.lower()} · "
                        f"Greeks via Black-Scholes · Risk-free rate {_RISK_FREE_RATE*100:.1f}%"
                    )
                    st.dataframe(opt_display_df, use_container_width=True, hide_index=True, column_config=_OPT_COL_CONFIG)

                    opt_sess_key = _opt_session_key(opt_ticker, opt_expiry, opt_type)

                    run_col, hint_col = st.columns([2, 8])
                    with run_col:
                        opt_run_clicked = st.button("▶ Run Gemini Analysis", use_container_width=True, key="opt_run_btn")
                    with hint_col:
                        st.caption("Uses Gemini 2.5 Pro · ~30–90s · Results cached 4 hours · analyzes both calls & puts")

                    if opt_run_clicked:
                        st.session_state.options_results.pop(opt_sess_key, None)
                        with st.spinner("Gemini 2.5 Pro analyzing the full option chain…"):
                            entry = _load_or_analyze_options(
                                opt_ticker, opt_expiry, opt_type, opt_price,
                                opt_calls_df, opt_puts_df, opt_calls_display_df, opt_puts_display_df,
                            )

                    if opt_sess_key in st.session_state.options_results:
                        entry = st.session_state.options_results[opt_sess_key]
                        analyzed_at = entry.get("analyzed_at", "")
                        from_db = entry.get("from_db", False)
                        if from_db and analyzed_at:
                            st.info(f"Cached analysis from {analyzed_at} UTC (re-runs after 4 hours).")
                        elif analyzed_at:
                            st.success(f"Freshly analyzed at {analyzed_at} UTC.")
                        _render_options_analysis(entry, opt_price)
                    else:
                        cached_db = get_latest_options_analysis(opt_ticker, opt_expiry, opt_type)
                        if cached_db is not None and is_options_analysis_fresh(cached_db.get("analyzed_at", "")):
                            entry = {**cached_db, "from_db": True}
                            st.session_state.options_results[opt_sess_key] = entry
                            st.info(f"Cached analysis from {cached_db['analyzed_at']} UTC (re-runs after 4 hours).")
                            _render_options_analysis(entry, opt_price)

    # ── All Options Results Summary ────────────────────────────────────────────

    if st.session_state.options_results:
        st.markdown("### Options Analysis Results")
        for sess_key, entry in st.session_state.options_results.items():
            if "_error" in entry:
                continue
            parts = sess_key.split("|")
            t_label = parts[0] if len(parts) > 0 else sess_key
            exp_label = parts[1] if len(parts) > 1 else ""
            type_label = "Calls" if (len(parts) > 2 and parts[2] == "call") else "Puts"
            bias = entry.get("directional_bias", "neutral").upper()
            strength = entry.get("bias_strength", "moderate").capitalize()
            conf = entry.get("confidence", "medium").capitalize()
            from_db = entry.get("from_db", False)
            cache_tag = " [cached]" if from_db else ""
            spot = entry.get("spot_price") or 0.0
            with st.expander(
                f"**{t_label}** · {exp_label} {type_label} — {bias} ({strength}){cache_tag}",
                expanded=False,
            ):
                analyzed_at = entry.get("analyzed_at", "")
                if from_db and analyzed_at:
                    st.info(f"Cached from {analyzed_at} UTC.")
                elif analyzed_at:
                    st.success(f"Analyzed at {analyzed_at} UTC.")
                st.caption(f"Confidence: {conf}")
                _render_options_analysis(entry, spot)

        if st.button("Clear Options Results", key="opt_clear_btn"):
            st.session_state.options_results = {}
            st.rerun()


_options_analysis_ui(tickers)

# ---------------------------------------------------------------------------
# Hedge Fund Overlap
# ---------------------------------------------------------------------------
st.markdown("---")
st.subheader("Hedge Fund Overlap")
_render_hedge_fund_overlap(positions_result)
