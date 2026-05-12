import json
from typing import Optional

import pandas as pd
import streamlit as st

from components.gemini_usage_bar import render_gemini_usage_bar
from components.thesis_form import _get_conn, render_thesis_form, load_thesis
from data.fetcher import get_stock_info, get_financials
from data.calculator import (
    calc_pe_ratio, calc_ev_ebitda, calc_p_fcf, calc_peg_ratio,
    calc_gross_margin, calc_operating_margin, calc_net_margin, calc_roic,
    calc_revenue_yoy, calc_eps_yoy, calc_fcf_yoy,
    calc_net_debt_ebitda, calc_interest_coverage, calc_current_ratio,
    calc_short_interest, calc_insider_ownership,
)

st.set_page_config(page_title="Thesis Tracker", layout="wide")

render_gemini_usage_bar()

_CONVICTION_COLOR = {"High": "#00c853", "Medium": "#ffd600", "Low": "#ff6d00"}
_STATUS_OPTIONS = ["All", "Active", "Closed", "Watching"]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _fetch_all_theses(status_filter: str) -> list[dict]:
    """Return all thesis rows, optionally filtered by status."""
    conn = _get_conn()
    try:
        if status_filter == "All":
            rows = conn.execute("SELECT * FROM thesis ORDER BY date_updated DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM thesis WHERE status=? ORDER BY date_updated DESC",
                (status_filter,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _delete_thesis(thesis_id: int) -> None:
    """Delete a thesis and its snapshots by id."""
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM thesis WHERE id=?", (thesis_id,))
        conn.commit()
    finally:
        conn.close()


def _load_latest_snapshot(thesis_id: int) -> Optional[dict]:
    """Return the most recent snapshot metric dict for a thesis, or None."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT metric_json FROM thesis_snapshots WHERE thesis_id=? ORDER BY snapshot_date DESC LIMIT 1",
            (thesis_id,),
        ).fetchone()
        return json.loads(row["metric_json"]) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Live metrics snapshot (mirrors _build_snapshot in thesis_form)
# ---------------------------------------------------------------------------

def _live_metrics(ticker: str) -> dict:
    """Compute current metric values for comparison against the stored snapshot."""
    info = get_stock_info(ticker)
    financials = get_financials(ticker)

    def pair(fn, *args):
        val, status = fn(*args)
        return {"value": val, "status": status}

    return {
        "pe_ratio":          pair(calc_pe_ratio, info),
        "ev_ebitda":         pair(calc_ev_ebitda, info),
        "p_fcf":             pair(calc_p_fcf, info),
        "peg_ratio":         pair(calc_peg_ratio, info),
        "gross_margin":      pair(calc_gross_margin, info),
        "operating_margin":  pair(calc_operating_margin, info),
        "net_margin":        pair(calc_net_margin, info),
        "roic":              pair(calc_roic, info),
        "revenue_yoy":       pair(calc_revenue_yoy, financials),
        "eps_yoy":           pair(calc_eps_yoy, financials),
        "fcf_yoy":           pair(calc_fcf_yoy, financials),
        "net_debt_ebitda":   pair(calc_net_debt_ebitda, info),
        "interest_coverage": pair(calc_interest_coverage, financials),
        "current_ratio":     pair(calc_current_ratio, info),
        "short_interest":    pair(calc_short_interest, info),
        "insider_ownership": pair(calc_insider_ownership, info),
    }


_METRIC_LABELS = {
    "pe_ratio": "P/E Ratio", "ev_ebitda": "EV/EBITDA", "p_fcf": "P/FCF",
    "peg_ratio": "PEG Ratio", "gross_margin": "Gross Margin %",
    "operating_margin": "Operating Margin %", "net_margin": "Net Margin %",
    "roic": "ROIC %", "revenue_yoy": "Revenue YoY %", "eps_yoy": "EPS YoY %",
    "fcf_yoy": "FCF YoY %", "net_debt_ebitda": "Net Debt/EBITDA",
    "interest_coverage": "Interest Coverage", "current_ratio": "Current Ratio",
    "short_interest": "Short Interest %", "insider_ownership": "Insider Ownership %",
}

_STATUS_EMOJI = {"good": "🟢", "neutral": "🟡", "warn": "🔴"}


def _fmt(val) -> str:
    """Format a metric value for display."""
    if val is None:
        return "—"
    try:
        return f"{float(val):.2f}"
    except (TypeError, ValueError):
        return str(val)


# ---------------------------------------------------------------------------
# Thesis vs. reality section
# ---------------------------------------------------------------------------

def _render_thesis_vs_reality(thesis: dict) -> None:
    """Render a comparison table of snapshot metrics vs live metrics."""
    snapshot = _load_latest_snapshot(thesis["id"])
    if snapshot is None:
        st.info("No metric snapshot available for this thesis.")
        return

    with st.spinner("Fetching live metrics…"):
        live = _live_metrics(thesis["ticker"])

    rows = []
    for key, label in _METRIC_LABELS.items():
        snap_entry = snapshot.get(key, {})
        live_entry = live.get(key, {})
        rows.append({
            "Metric": label,
            "At Thesis": _fmt(snap_entry.get("value")),
            "At Thesis Signal": _STATUS_EMOJI.get(snap_entry.get("status", "neutral"), ""),
            "Today": _fmt(live_entry.get("value")),
            "Today Signal": _STATUS_EMOJI.get(live_entry.get("status", "neutral"), ""),
        })

    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Detail expander
# ---------------------------------------------------------------------------

def _render_thesis_detail(thesis: dict) -> None:
    """Render full thesis fields and the thesis-vs-reality comparison."""
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"**Conviction:** {thesis['conviction_level']}")
        st.markdown(f"**Status:** {thesis['status']}")
        st.markdown(f"**Entry Price:** {_fmt(thesis.get('entry_price'))}")
        st.markdown(f"**Time Horizon:** {thesis.get('time_horizon_months', '—')} months")
    with col2:
        st.markdown(f"**Bull / Base / Bear:** {_fmt(thesis.get('bull_price'))} / {_fmt(thesis.get('base_price'))} / {_fmt(thesis.get('bear_price'))}")
        st.markdown(f"**Bear Probability:** {_fmt(thesis.get('bear_probability'))}%")
        st.markdown(f"**Created:** {thesis.get('date_created', '')[:10]}")
        st.markdown(f"**Updated:** {thesis.get('date_updated', '')[:10]}")

    for label, field in [
        ("Business Summary", "business_summary"),
        ("Moat", "moat_description"),
        ("Why Undervalued", "why_undervalued"),
        ("Short-Term Catalyst", "catalyst_short"),
        ("Medium-Term Catalyst", "catalyst_medium"),
        ("Bear Case", "bear_case"),
    ]:
        if thesis.get(field):
            st.markdown(f"**{label}:** {thesis[field]}")

    st.markdown("---")
    st.subheader("Thesis vs. Reality")
    _render_thesis_vs_reality(thesis)


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

def _render_thesis_list(theses: list[dict]) -> None:
    """Render the thesis list table with color-coded conviction and action buttons."""
    if not theses:
        st.info("No theses found. Add one below.")
        return

    df = pd.DataFrame([{
        "ID": t["id"],
        "Ticker": t["ticker"],
        "Conviction": t["conviction_level"],
        "Status": t["status"],
        "Entry": _fmt(t.get("entry_price")),
        "Base Target": _fmt(t.get("base_price")),
        "Horizon (mo)": t.get("time_horizon_months", "—"),
        "Updated": t.get("date_updated", "")[:10],
    } for t in theses])

    st.dataframe(
        df.style.apply(
            lambda row: [
                f"color: {_CONVICTION_COLOR.get(row['Conviction'], '#ffffff')}"
                if col == "Conviction" else ""
                for col in df.columns
            ],
            axis=1,
        ),
        use_container_width=True,
        hide_index=True,
    )


def _render_thesis_actions(theses: list[dict]) -> None:
    """Render expandable detail panels with Edit / Delete controls per thesis."""
    for thesis in theses:
        label = f"{thesis['ticker']} — {thesis['conviction_level']} | {thesis['status']}"
        with st.expander(label):
            _render_thesis_detail(thesis)
            col_edit, col_del, _ = st.columns([1, 1, 6])
            with col_edit:
                if st.button("Edit", key=f"edit_{thesis['id']}"):
                    st.session_state["thesis_edit_id"] = thesis["id"]
                    st.rerun()
            with col_del:
                if st.button("Delete", key=f"del_{thesis['id']}"):
                    st.session_state[f"confirm_del_{thesis['id']}"] = True
                    st.rerun()
            if st.session_state.get(f"confirm_del_{thesis['id']}"):
                st.warning(f"Delete thesis for {thesis['ticker']}? This cannot be undone.")
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Confirm Delete", key=f"confirm_{thesis['id']}"):
                        _delete_thesis(thesis["id"])
                        st.session_state.pop(f"confirm_del_{thesis['id']}", None)
                        st.rerun()
                with c2:
                    if st.button("Cancel", key=f"cancel_{thesis['id']}"):
                        st.session_state.pop(f"confirm_del_{thesis['id']}", None)
                        st.rerun()


def main() -> None:
    """Entry point for the Thesis Tracker page."""
    st.title("Thesis Tracker")

    status_filter = st.selectbox("Filter by Status", _STATUS_OPTIONS, key="thesis_status_filter")
    theses = _fetch_all_theses(status_filter)

    _render_thesis_list(theses)
    _render_thesis_actions(theses)

    st.markdown("---")
    edit_id = st.session_state.get("thesis_edit_id")
    if edit_id:
        existing = load_thesis(edit_id)
        if existing:
            render_thesis_form(existing=existing)
        else:
            st.session_state["thesis_edit_id"] = None
            st.rerun()
    else:
        st.subheader("Add New Thesis")
        render_thesis_form()


main()
