from datetime import datetime
from typing import Optional

import pandas as pd
import streamlit as st

from components.thesis_form import _get_conn
from data.fetcher import get_stock_info
from data.calculator import calc_pe_ratio, calc_gross_margin

st.set_page_config(page_title="Watchlist", layout="wide")

_ALERT_THRESHOLD = 0.05  # 5% from alert price triggers highlight


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _load_watchlist() -> list[dict]:
    """Return all watchlist rows as plain dicts."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM watchlist ORDER BY date_added DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _add_to_watchlist(ticker: str, notes: str, alert_price: Optional[float]) -> None:
    """Insert a new ticker into the watchlist, ignoring duplicates."""
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO watchlist (ticker, date_added, notes, alert_price)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                   notes = excluded.notes,
                   alert_price = excluded.alert_price""",
            (ticker.upper(), datetime.utcnow().isoformat(), notes or None, alert_price),
        )
        conn.commit()
    except Exception as e:
        st.error(f"Could not add {ticker}: {e}")
    finally:
        conn.close()


def _delete_from_watchlist(ticker: str) -> None:
    """Remove a ticker from the watchlist by ticker symbol."""
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM watchlist WHERE ticker=?", (ticker.upper(),))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Live data fetch for watchlist rows
# ---------------------------------------------------------------------------

def _fetch_row_data(ticker: str) -> dict:
    """Return a display-ready dict of live metrics for one watchlist ticker."""
    info = get_stock_info(ticker)
    pe, _ = calc_pe_ratio(info)
    gm, _ = calc_gross_margin(info)

    price      = info.get("currentPrice")
    prev_close = info.get("previousClose")
    high_52    = info.get("fiftyTwoWeekHigh")
    low_52     = info.get("fiftyTwoWeekLow")
    pct_change = ((price - prev_close) / prev_close * 100) if price and prev_close else None
    pct_from_high = ((price - high_52) / high_52 * 100) if price and high_52 else None

    return {
        "ticker":        ticker,
        "price":         price,
        "pct_change":    pct_change,
        "pe":            pe,
        "gross_margin":  gm,
        "52w_high":      high_52,
        "52w_low":       low_52,
        "pct_from_high": pct_from_high,
    }


def _fmt(v, decimals: int = 2, suffix: str = "") -> str:
    """Format a numeric value for display, returning '—' for None."""
    if v is None:
        return "—"
    try:
        return f"{float(v):.{decimals}f}{suffix}"
    except (TypeError, ValueError):
        return "—"


# ---------------------------------------------------------------------------
# Add ticker form
# ---------------------------------------------------------------------------

def _render_add_form() -> None:
    """Render the form for adding a new ticker to the watchlist."""
    with st.form("add_watchlist"):
        st.subheader("Add Ticker")
        col1, col2, col3 = st.columns([2, 3, 2])
        with col1:
            ticker = st.text_input("Ticker Symbol", placeholder="e.g. AAPL")
        with col2:
            notes = st.text_input("Notes (optional)")
        with col3:
            alert_raw = st.text_input("Alert Price (optional)")
        submitted = st.form_submit_button("Add to Watchlist")

    if submitted:
        ticker = ticker.upper().strip()
        if not ticker:
            st.error("Ticker symbol is required.")
            return
        alert_price = None
        if alert_raw.strip():
            try:
                alert_price = float(alert_raw)
            except ValueError:
                st.warning("Alert price must be a number; saved without it.")
        _add_to_watchlist(ticker, notes, alert_price)
        st.session_state["watchlist"] = None  # invalidate cache
        st.rerun()


# ---------------------------------------------------------------------------
# Watchlist table
# ---------------------------------------------------------------------------

def _near_alert(price: Optional[float], alert: Optional[float]) -> bool:
    """Return True if price is within _ALERT_THRESHOLD of alert_price."""
    if price is None or alert is None or alert == 0:
        return False
    return abs(price - alert) / abs(alert) <= _ALERT_THRESHOLD


def _build_display_df(rows: list[dict], live: list[dict]) -> pd.DataFrame:
    """Merge watchlist DB rows with live data into a display DataFrame."""
    live_map = {r["ticker"]: r for r in live}
    records = []
    for row in rows:
        t = row["ticker"]
        d = live_map.get(t, {})
        records.append({
            "Ticker":        t,
            "Price":         _fmt(d.get("price")),
            "Day %":         _fmt(d.get("pct_change"), suffix="%"),
            "P/E":           _fmt(d.get("pe")),
            "Gross Margin":  _fmt(d.get("gross_margin"), suffix="%"),
            "52W High":      _fmt(d.get("52w_high")),
            "52W Low":       _fmt(d.get("52w_low")),
            "% from High":   _fmt(d.get("pct_from_high"), suffix="%"),
            "Alert Price":   _fmt(row.get("alert_price")),
            "Notes":         row.get("notes") or "",
        })
    return pd.DataFrame(records)


def _row_style(row, rows_db: list[dict], live_map: dict) -> list[str]:
    """Apply background highlight if price is near alert for this row."""
    ticker = row["Ticker"]
    db_row = next((r for r in rows_db if r["ticker"] == ticker), {})
    live   = live_map.get(ticker, {})
    if _near_alert(live.get("price"), db_row.get("alert_price")):
        return ["background-color: #4a3000"] * len(row)
    return [""] * len(row)


def _render_table(rows_db: list[dict], live: list[dict]) -> None:
    """Render the styled watchlist DataFrame."""
    if not rows_db:
        st.info("Your watchlist is empty. Add a ticker above.")
        return
    df = _build_display_df(rows_db, live)
    live_map = {r["ticker"]: r for r in live}
    styled = df.style.apply(_row_style, axis=1, rows_db=rows_db, live_map=live_map)
    st.dataframe(styled, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Per-row action buttons
# ---------------------------------------------------------------------------

def _render_row_actions(rows_db: list[dict]) -> None:
    """Render Analyze and Remove buttons for each watchlist ticker."""
    if not rows_db:
        return
    st.markdown("### Actions")
    cols_per_row = 4
    chunks = [rows_db[i:i + cols_per_row] for i in range(0, len(rows_db), cols_per_row)]
    for chunk in chunks:
        cols = st.columns(cols_per_row)
        for col, row in zip(cols, chunk):
            ticker = row["ticker"]
            with col:
                st.markdown(f"**{ticker}**")
                if st.button("Analyze", key=f"analyze_{ticker}"):
                    st.session_state["active_ticker"] = ticker
                    st.session_state["metrics_ticker_input"] = ticker
                    st.switch_page("pages/1_metrics.py")
                if st.button("Remove", key=f"remove_{ticker}"):
                    _delete_from_watchlist(ticker)
                    st.session_state["watchlist"] = None
                    st.rerun()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point for the Watchlist page."""
    st.title("Watchlist")
    _render_add_form()
    st.markdown("---")

    rows_db = _load_watchlist()
    st.session_state["watchlist"] = [r["ticker"] for r in rows_db]

    if rows_db:
        with st.spinner("Fetching live data…"):
            live = [_fetch_row_data(r["ticker"]) for r in rows_db]
    else:
        live = []

    _render_table(rows_db, live)
    st.markdown("---")
    _render_row_actions(rows_db)


main()
