import sqlite3
import json
import os
from datetime import datetime
from typing import Optional

import streamlit as st

from data.fetcher import get_stock_info, get_financials
from data.calculator import (
    calc_pe_ratio, calc_ev_ebitda, calc_p_fcf, calc_peg_ratio,
    calc_gross_margin, calc_operating_margin, calc_net_margin, calc_roic,
    calc_revenue_yoy, calc_eps_yoy, calc_fcf_yoy,
    calc_net_debt_ebitda, calc_interest_coverage, calc_current_ratio,
    calc_short_interest, calc_insider_ownership,
)

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "thesis.db")


def _get_conn() -> sqlite3.Connection:
    """Return a SQLite connection with the schema applied."""
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    schema_path = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")
    with open(schema_path) as f:
        conn.executescript(f.read())
    return conn


def _build_snapshot(ticker: str) -> dict:
    """Compute and return all calculator metrics for snapshot storage."""
    info = get_stock_info(ticker)
    financials = get_financials(ticker)

    def pair(fn, *args):
        val, status = fn(*args)
        return {"value": val, "status": status}

    return {
        "pe_ratio":         pair(calc_pe_ratio, info),
        "ev_ebitda":        pair(calc_ev_ebitda, info),
        "p_fcf":            pair(calc_p_fcf, info),
        "peg_ratio":        pair(calc_peg_ratio, info),
        "gross_margin":     pair(calc_gross_margin, info),
        "operating_margin": pair(calc_operating_margin, info),
        "net_margin":       pair(calc_net_margin, info),
        "roic":             pair(calc_roic, info),
        "revenue_yoy":      pair(calc_revenue_yoy, financials),
        "eps_yoy":          pair(calc_eps_yoy, financials),
        "fcf_yoy":          pair(calc_fcf_yoy, financials),
        "net_debt_ebitda":  pair(calc_net_debt_ebitda, info),
        "interest_coverage":pair(calc_interest_coverage, financials),
        "current_ratio":    pair(calc_current_ratio, info),
        "short_interest":   pair(calc_short_interest, info),
        "insider_ownership":pair(calc_insider_ownership, info),
    }


def _save_snapshot(conn: sqlite3.Connection, thesis_id: int, ticker: str) -> None:
    """Insert a metric snapshot row linked to the given thesis_id."""
    metrics = _build_snapshot(ticker)
    conn.execute(
        "INSERT INTO thesis_snapshots (thesis_id, snapshot_date, metric_json) VALUES (?, ?, ?)",
        (thesis_id, datetime.utcnow().isoformat(), json.dumps(metrics)),
    )


def save_thesis(fields: dict, thesis_id: Optional[int] = None) -> int:
    """Insert or update a thesis row and write a snapshot. Returns thesis id."""
    conn = _get_conn()
    now = datetime.utcnow().isoformat()
    try:
        if thesis_id is None:
            cur = conn.execute(
                """INSERT INTO thesis (
                    ticker, date_created, date_updated, conviction_level,
                    business_summary, moat_description, why_undervalued,
                    catalyst_short, catalyst_medium, bear_case, bear_probability,
                    bull_price, base_price, bear_price, time_horizon_months,
                    entry_price, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    fields["ticker"].upper(), now, now, fields["conviction_level"],
                    fields.get("business_summary"), fields.get("moat_description"),
                    fields.get("why_undervalued"), fields.get("catalyst_short"),
                    fields.get("catalyst_medium"), fields.get("bear_case"),
                    fields.get("bear_probability"), fields.get("bull_price"),
                    fields.get("base_price"), fields.get("bear_price"),
                    fields.get("time_horizon_months"), fields.get("entry_price"),
                    fields.get("status", "Active"),
                ),
            )
            thesis_id = cur.lastrowid
        else:
            conn.execute(
                """UPDATE thesis SET
                    date_updated=?, conviction_level=?, business_summary=?,
                    moat_description=?, why_undervalued=?, catalyst_short=?,
                    catalyst_medium=?, bear_case=?, bear_probability=?,
                    bull_price=?, base_price=?, bear_price=?,
                    time_horizon_months=?, entry_price=?, status=?
                WHERE id=?""",
                (
                    now, fields["conviction_level"], fields.get("business_summary"),
                    fields.get("moat_description"), fields.get("why_undervalued"),
                    fields.get("catalyst_short"), fields.get("catalyst_medium"),
                    fields.get("bear_case"), fields.get("bear_probability"),
                    fields.get("bull_price"), fields.get("base_price"),
                    fields.get("bear_price"), fields.get("time_horizon_months"),
                    fields.get("entry_price"), fields.get("status", "Active"),
                    thesis_id,
                ),
            )
        _save_snapshot(conn, thesis_id, fields["ticker"])
        conn.commit()
        return thesis_id
    except Exception as e:
        conn.rollback()
        st.error(f"Failed to save thesis: {e}")
        return -1
    finally:
        conn.close()


def load_thesis(thesis_id: int) -> Optional[dict]:
    """Load a single thesis row by id, returning a plain dict or None."""
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM thesis WHERE id=?", (thesis_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _text_area_row(label: str, key: str, default: str = "") -> str:
    """Render a labeled text area and return its value."""
    return st.text_area(label, value=default, key=key)


def _number_input_row(label: str, key: str, default: Optional[float] = None) -> Optional[float]:
    """Render a labeled number input allowing blank (None) values."""
    raw = st.text_input(label, value="" if default is None else str(default), key=key)
    try:
        return float(raw) if raw.strip() else None
    except ValueError:
        return None


def render_thesis_form(existing: Optional[dict] = None) -> None:
    """Render the full thesis entry form. If existing is provided, pre-populate for editing."""
    is_edit = existing is not None
    ticker_default = existing["ticker"] if is_edit else ""

    with st.form("thesis_form"):
        st.subheader("Thesis" if not is_edit else f"Edit Thesis — {existing['ticker']}")

        ticker = st.text_input("Ticker Symbol", value=ticker_default, key="tf_ticker").upper().strip()

        col1, col2 = st.columns(2)
        with col1:
            conviction = st.selectbox(
                "Conviction Level",
                ["High", "Medium", "Low"],
                index=["High", "Medium", "Low"].index(existing["conviction_level"]) if is_edit else 0,
                key="tf_conviction",
            )
        with col2:
            status = st.selectbox(
                "Status",
                ["Active", "Closed", "Watching"],
                index=["Active", "Closed", "Watching"].index(existing["status"]) if is_edit else 0,
                key="tf_status",
            )

        business_summary = _text_area_row("Business Summary", "tf_biz", existing.get("business_summary", "") if is_edit else "")
        moat_description = _text_area_row("Moat Description", "tf_moat", existing.get("moat_description", "") if is_edit else "")
        why_undervalued  = _text_area_row("Why Undervalued", "tf_why", existing.get("why_undervalued", "") if is_edit else "")
        catalyst_short   = _text_area_row("Short-Term Catalyst", "tf_cat_s", existing.get("catalyst_short", "") if is_edit else "")
        catalyst_medium  = _text_area_row("Medium-Term Catalyst", "tf_cat_m", existing.get("catalyst_medium", "") if is_edit else "")
        bear_case        = _text_area_row("Bear Case", "tf_bear", existing.get("bear_case", "") if is_edit else "")

        st.markdown("**Price Targets & Sizing**")
        col3, col4, col5 = st.columns(3)
        with col3:
            bull_price  = _number_input_row("Bull Price Target", "tf_bull", existing.get("bull_price") if is_edit else None)
        with col4:
            base_price  = _number_input_row("Base Price Target", "tf_base", existing.get("base_price") if is_edit else None)
        with col5:
            bear_price  = _number_input_row("Bear Price Target", "tf_bearpr", existing.get("bear_price") if is_edit else None)

        col6, col7, col8 = st.columns(3)
        with col6:
            entry_price     = _number_input_row("Entry Price", "tf_entry", existing.get("entry_price") if is_edit else None)
        with col7:
            bear_probability = _number_input_row("Bear Probability %", "tf_bearprob", existing.get("bear_probability") if is_edit else None)
        with col8:
            horizon = st.number_input(
                "Time Horizon (months)",
                min_value=1, max_value=120,
                value=int(existing["time_horizon_months"]) if is_edit and existing.get("time_horizon_months") else 12,
                key="tf_horizon",
            )

        submitted = st.form_submit_button("Save Thesis")

    if submitted:
        if not ticker:
            st.error("Ticker symbol is required.")
            return
        fields = {
            "ticker": ticker, "conviction_level": conviction, "status": status,
            "business_summary": business_summary, "moat_description": moat_description,
            "why_undervalued": why_undervalued, "catalyst_short": catalyst_short,
            "catalyst_medium": catalyst_medium, "bear_case": bear_case,
            "bear_probability": bear_probability, "bull_price": bull_price,
            "base_price": base_price, "bear_price": bear_price,
            "time_horizon_months": horizon, "entry_price": entry_price,
        }
        tid = save_thesis(fields, thesis_id=existing["id"] if is_edit else None)
        if tid > 0:
            st.success(f"Thesis {'updated' if is_edit else 'saved'} for {ticker}.")
            st.session_state["thesis_edit_id"] = None
            st.rerun()
