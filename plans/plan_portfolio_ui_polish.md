# Plan: Portfolio Page UI Polish

## Overview

This is a layout and styling polish pass on the Dashboard tab (tab 1) of `9_portfolio.py`. The goal is to make the page decision-first: AI Insights appears immediately below the KPI bar so the user sees what to do before seeing charts. The five collapsed expanders at the bottom are replaced by one compact signal matrix table. Visual clutter (redundant captions, noisy donut labels, inconsistent headers, missing risk delta coloring) is cleaned up. No data fetching, caching, session_state keys, or other tabs are touched.

**Expected outcome when fully executed:** The Dashboard tab shows (from top to bottom): account selector → KPI bar → AI Insights card (with action buttons above it) → charts/donuts/risk strip/positions table → signal matrix table + Details expander. All section headers use `section_header()`. Risk strip metrics show warning deltas. Donut charts are height 260 with `textinfo="percent"` plus a right-side legend.

---

## Files Involved

| File | Action | What changes |
|------|--------|--------------|
| `stock-dashboard/pages/9_portfolio.py` | **Modify** | All changes are within the Dashboard tab block (lines 3726–3865) and the two chart-builder functions `_build_allocation_charts` and `_dashboard_visuals_ui`. No other sections are touched. |
| `stock-dashboard/components/ui.py` | **Read-only reference** | `section_header()` is already imported; no changes needed. |

---

## Prerequisites & Dependencies

- No new pip packages required.
- No database changes required.
- No environment variable changes required.
- `section_header` is already imported at line 22 of `9_portfolio.py`:
  ```python
  from components.ui import inject_global_css, page_header
  ```
  This import must be extended to also import `section_header`. That is Step 1.

---

## Step-by-Step Implementation

---

### Step 1: Add `section_header` to the import from `components.ui`

**File:** `stock-dashboard/pages/9_portfolio.py`
**Location:** Line 22 — the existing import line
**Action:** Replace the existing import line with the version that includes `section_header`.

Find this exact line:
```python
from components.ui import inject_global_css, page_header
```

Replace with:
```python
from components.ui import inject_global_css, page_header, section_header
```

**Why:** `section_header()` renders the small all-caps blue section labels defined in `_GLOBAL_CSS`. Several steps below call it. Without this import those steps will throw `NameError`.

---

### Step 2: Remove the "Select account above" caption

**File:** `stock-dashboard/pages/9_portfolio.py`
**Location:** Lines 2398–2402 — the account selector block
**Action:** Replace the entire account selector block with the version that removes the `st.caption` line.

Find this exact block (lines 2398–2403):
```python
# ── Account selector (compact row) ────────────────────────────────────────────
_acct_col, _title_col = st.columns([2, 5])
with _acct_col:
    selected_label = st.selectbox("Account", list(accounts.keys()), label_visibility="collapsed")
with _title_col:
    st.caption("Select account above · All sections update automatically")
selected_account_id = label_to_id.get(selected_label, selected_label)
```

Replace with:
```python
# ── Account selector (compact row) ────────────────────────────────────────────
_acct_col, _spacer_col = st.columns([2, 5])
with _acct_col:
    selected_label = st.selectbox("Account", list(accounts.keys()), label_visibility="collapsed")
selected_account_id = label_to_id.get(selected_label, selected_label)
```

**Why:** The caption adds no actionable information — users can see the selectbox is a selector. Removing it reduces visual noise.

---

### Step 3: Update `_build_allocation_charts` — reduce donut heights and switch to legend

**File:** `stock-dashboard/pages/9_portfolio.py`
**Location:** Inside `_build_allocation_charts` function — the two `go.Figure` / `fig.update_layout` calls for the weights donut and the sectors donut.

**Action 3a:** Find the weights donut `go.Figure` constructor and layout block. Replace:

```python
    fig_weights = go.Figure(go.Pie(
        labels=labels_w,
        values=values_w,
        hole=0.55,
        textinfo="label+percent",
        textfont=dict(size=11),
        marker=dict(
            colors=_DONUT_COLORS[:len(labels_w)],
            line=dict(color="#0e1117", width=2),
        ),
        hovertemplate="<b>%{label}</b><br>Weight: %{value:.1f}%<extra></extra>",
    ))
    fig_weights.update_layout(
        **_plotly_portfolio_layout(
            title=dict(text="Position Weights", font=dict(size=13)),
            height=300,
            margin=dict(l=16, r=16, t=48, b=16),
            showlegend=False,
        )
    )
```

With:

```python
    fig_weights = go.Figure(go.Pie(
        labels=labels_w,
        values=values_w,
        hole=0.55,
        textinfo="percent",
        textfont=dict(size=10),
        marker=dict(
            colors=_DONUT_COLORS[:len(labels_w)],
            line=dict(color="#0e1117", width=2),
        ),
        hovertemplate="<b>%{label}</b><br>Weight: %{value:.1f}%<extra></extra>",
    ))
    fig_weights.update_layout(
        **_plotly_portfolio_layout(
            height=260,
            margin=dict(l=8, r=8, t=32, b=8),
            showlegend=True,
            legend=dict(
                orientation="v",
                yanchor="middle",
                y=0.5,
                xanchor="left",
                x=1.0,
                font=dict(size=10),
                bgcolor="rgba(0,0,0,0)",
            ),
        )
    )
```

**Action 3b:** Immediately after Action 3a, find the sectors donut `go.Figure` constructor and layout block. Replace:

```python
    fig_sectors = go.Figure(go.Pie(
        labels=labels_s,
        values=values_s,
        hole=0.55,
        textinfo="label+percent",
        textfont=dict(size=11),
        marker=dict(
            colors=_DONUT_COLORS[:len(labels_s)],
            line=dict(color="#0e1117", width=2),
        ),
        hovertemplate="<b>%{label}</b><br>Allocation: %{value:.1f}%<extra></extra>",
    ))
    fig_sectors.update_layout(
        **_plotly_portfolio_layout(
            title=dict(text="Sector Allocation", font=dict(size=13)),
            height=300,
            margin=dict(l=16, r=16, t=48, b=16),
            showlegend=False,
        )
    )
```

With:

```python
    fig_sectors = go.Figure(go.Pie(
        labels=labels_s,
        values=values_s,
        hole=0.55,
        textinfo="percent",
        textfont=dict(size=10),
        marker=dict(
            colors=_DONUT_COLORS[:len(labels_s)],
            line=dict(color="#0e1117", width=2),
        ),
        hovertemplate="<b>%{label}</b><br>Allocation: %{value:.1f}%<extra></extra>",
    ))
    fig_sectors.update_layout(
        **_plotly_portfolio_layout(
            height=260,
            margin=dict(l=8, r=8, t=32, b=8),
            showlegend=True,
            legend=dict(
                orientation="v",
                yanchor="middle",
                y=0.5,
                xanchor="left",
                x=1.0,
                font=dict(size=10),
                bgcolor="rgba(0,0,0,0)",
            ),
        )
    )
```

**Why:** With `textinfo="label+percent"` each slice shows a label string inside the donut ring, which is cramped at height 300 when many tickers are present. Switching to `textinfo="percent"` (number only) plus a right-side vertical legend lets users identify slices without crowding. Reducing height to 260 ensures two stacked donuts match the performance chart height (360) more closely.

---

### Step 4: Add section headers and shorten the performance chart caption inside `_dashboard_visuals_ui`

**File:** `stock-dashboard/pages/9_portfolio.py`
**Location:** Inside the `_dashboard_visuals_ui` fragment function — the `_chart_col` block and the Holdings header.

**Action 4a:** Find the performance chart `st.caption` (the four-sentence caption). Replace:

```python
            st.caption(
                "Assumes current holdings held throughout the entire period. "
                "No transaction history is available from the Webull SDK. "
                "Portfolio return normalized to first trading day of the selected window. "
                "SPY shown for reference."
            )
```

With:

```python
            st.caption(
                "Current holdings held throughout period · normalized to first trading day · SPY for reference"
            )
```

**Action 4b:** Find the Holdings section header inside `_dashboard_visuals_ui`. Replace:

```python
        st.markdown(
            f"#### Holdings &nbsp;<span style='color:#aaa;font-size:0.85rem'>"
            f"{_n_pos} position{'s' if _n_pos != 1 else ''} · sorted by market value</span>",
            unsafe_allow_html=True,
        )
```

With:

```python
        section_header(
            f"Holdings — {_n_pos} position{'s' if _n_pos != 1 else ''} · sorted by market value"
        )
```

**Why:** The four-sentence caption duplicates obvious context (the chart title already says "current holdings"). The `#### Holdings` markdown mixes heading levels inconsistently; `section_header()` applies the standard all-caps style from `components/ui.py`.

---

### Step 5: Add section header above donut charts inside `_dashboard_visuals_ui`

**File:** `stock-dashboard/pages/9_portfolio.py`
**Location:** Inside `_dashboard_visuals_ui` — the `_donut_col` block. 

Find this block:

```python
    with _donut_col:
        fig_weights, fig_sectors = _build_allocation_charts(positions)
        st.plotly_chart(fig_weights, use_container_width=True)
        st.plotly_chart(fig_sectors, use_container_width=True)
```

Replace with:

```python
    with _donut_col:
        fig_weights, fig_sectors = _build_allocation_charts(positions)
        section_header("Position Weights")
        st.plotly_chart(fig_weights, use_container_width=True)
        section_header("Sector Allocation")
        st.plotly_chart(fig_sectors, use_container_width=True)
```

**Why:** Because the `title` key was removed from the donut layout (Step 3), the charts now need an external label. `section_header()` provides the consistent small-caps blue style.

---

### Step 6: Add delta-based warning coloring to the risk strip inside `_dashboard_visuals_ui`

**File:** `stock-dashboard/pages/9_portfolio.py`
**Location:** Inside `_dashboard_visuals_ui` — the "Row 2: Risk stats strip" block. This is the block starting with `_r1, _r2, _r3, _r4, _r5 = st.columns(5)` and ending with `_r5.metric("Top Position", "N/A")`.

Find this entire block:

```python
        risk = _compute_risk_stats(positions, perf_period_label, close_df_perf)
        _r1, _r2, _r3, _r4, _r5 = st.columns(5)
        _r1.metric(
            "Portfolio Beta",
            f"{risk['beta']:.2f}" if risk["beta"] is not None else "N/A",
            help="Weighted beta vs SPY over selected period",
        )
        _r2.metric(
            "Ann. Volatility",
            f"{risk['ann_vol']:.1f}%" if risk["ann_vol"] is not None else "N/A",
            help="Annualized portfolio standard deviation",
        )
        _r3.metric(
            "Sharpe Ratio",
            f"{risk['sharpe']:.2f}" if risk["sharpe"] is not None else "N/A",
            help=f"(Ann. return − {_RISK_FREE_RATE*100:.1f}% risk-free) / Ann. vol",
        )
        _r4.metric(
            "Max Drawdown",
            f"{risk['max_drawdown']:.1f}%" if risk["max_drawdown"] is not None else "N/A",
            help="Maximum peak-to-trough decline over selected period",
        )
        if risk["top_conc"] is not None:
            _r5.metric(
                "Top Position",
                f"{risk['top_conc']:.1f}%",
                delta=risk["top_ticker"],
                delta_color="off",
                help="Largest single-position weight by market value",
            )
        else:
            _r5.metric("Top Position", "N/A")
```

Replace with:

```python
        risk = _compute_risk_stats(positions, perf_period_label, close_df_perf)
        _r1, _r2, _r3, _r4, _r5 = st.columns(5)

        # Beta: warn if > 1.5
        _beta_val = risk["beta"]
        _beta_delta = "⚠ High market exposure" if (_beta_val is not None and _beta_val > 1.5) else None
        _r1.metric(
            "Portfolio Beta",
            f"{_beta_val:.2f}" if _beta_val is not None else "N/A",
            delta=_beta_delta,
            delta_color="inverse",
            help="Weighted beta vs SPY over selected period. ⚠ shown when beta > 1.5",
        )

        _r2.metric(
            "Ann. Volatility",
            f"{risk['ann_vol']:.1f}%" if risk["ann_vol"] is not None else "N/A",
            help="Annualized portfolio standard deviation",
        )

        _r3.metric(
            "Sharpe Ratio",
            f"{risk['sharpe']:.2f}" if risk["sharpe"] is not None else "N/A",
            help=f"(Ann. return − {_RISK_FREE_RATE*100:.1f}% risk-free) / Ann. vol",
        )

        # Max drawdown: warn if < -20%
        _dd_val = risk["max_drawdown"]
        _dd_delta = "⚠ Severe drawdown" if (_dd_val is not None and _dd_val < -20.0) else None
        _r4.metric(
            "Max Drawdown",
            f"{_dd_val:.1f}%" if _dd_val is not None else "N/A",
            delta=_dd_delta,
            delta_color="inverse",
            help="Maximum peak-to-trough decline over selected period. ⚠ shown when worse than -20%",
        )

        # Top concentration: warn if > 20%
        if risk["top_conc"] is not None:
            _conc_delta = (
                f"⚠ Concentrated" if risk["top_conc"] > 20.0
                else risk["top_ticker"]
            )
            _conc_color = "inverse" if risk["top_conc"] > 20.0 else "off"
            _r5.metric(
                "Top Position",
                f"{risk['top_conc']:.1f}%",
                delta=f"{_conc_delta} ({risk['top_ticker']})" if risk["top_conc"] > 20.0 else _conc_delta,
                delta_color=_conc_color,
                help="Largest single-position weight by market value. ⚠ shown when > 20%",
            )
        else:
            _r5.metric("Top Position", "N/A")
```

**Why:** Risk stats currently display numbers with no contextual alarm. Adding `delta` strings with `delta_color="inverse"` causes Streamlit to show a red indicator when a threshold is breached (beta > 1.5, drawdown < -20%, concentration > 20%). The `delta_color="inverse"` flag makes a positive delta text show red (since "⚠ High market exposure" appearing means a bad condition). For the ticker-only delta (the normal case with no warning) `delta_color="off"` is used to keep it gray.

---

### Step 7: Replace the Dashboard tab body — promote AI Insights above charts, add signals matrix, replace five expanders

**File:** `stock-dashboard/pages/9_portfolio.py`
**Location:** The entire `with _tab_dash:` block, lines 3726–3865.

Find this entire block (from `with _tab_dash:` through the closing `st.expander("🏦 Smart Money Summary", ...)` block):

```python
with _tab_dash:

    # ── Action buttons row ────────────────────────────────────────────────────
    _bc1, _bc2, _bc3 = st.columns([2, 2, 4])
    with _bc1:
        if st.button("⚡ Analyze Everything", use_container_width=True, key="analyze_everything_btn"):
            st.session_state.analyze_all_news    = True
            st.session_state.analyze_all_options = True
            st.session_state.analyze_all_hf      = True
            st.session_state.analyze_all_mpt     = True
            st.session_state.analyze_all_reddit  = True
            st.rerun()
    with _bc2:
        _run_insights = st.button("🤖 Run AI Insights", use_container_width=True, key="ai_insights_btn")
    with _bc3:
        st.caption(
            "⚡ **Analyze Everything** runs all 5 agents in parallel · "
            "🤖 **AI Insights** synthesizes all results with Gemini 2.5 Pro"
        )

    # ── NEW: Allocation charts + Performance chart + Risk strip + Enhanced table ──
    _dashboard_visuals_ui(positions_result, tickers)

    st.markdown("---")

    # ── AI Insights section ───────────────────────────────────────────────────
    st.markdown("### 🤖 AI Insights")

    if _run_insights:
        st.session_state.ai_insights = None
        _pd = {
            "balance":         selected_balance,
            "positions":       positions_result,
            "news_results":    st.session_state.news_results,
            "options_results": st.session_state.options_results,
            "wsb_results":     st.session_state.wsb_results,
            "mpt_analysis":    st.session_state.mpt_analysis,
            "hf_analysis":     st.session_state.hf_analysis,
        }
        with st.spinner("Gemini 2.5 Pro synthesizing all portfolio data… (~60–120s)"):
            _insights_result = run_portfolio_insights(_pd)
        st.session_state.ai_insights = _insights_result

    if st.session_state.ai_insights is not None:
        _render_ai_insights(st.session_state.ai_insights)
        if st.button("🔄 Refresh Insights", key="ai_refresh_btn"):
            st.session_state.ai_insights = None
            st.rerun()
    else:
        st.info(
            "Click **🤖 Run AI Insights** to get Gemini 2.5 Pro's holistic analysis combining "
            "positions, news, options flow, Reddit sentiment, hedge fund 13F data, and MPT metrics. "
            "For best results, run **⚡ Analyze Everything** first."
        )

    st.markdown("---")

    # ── Compact Signal Summaries (demoted to expanders) ───────────────────────
    with st.expander("📰 News Sentiment Summary", expanded=False):
        _n_news = len(st.session_state.news_results)
        _npos = sum(1 for e in st.session_state.news_results.values()
                    if e.get("result", {}).get("sentiment_label") == "positive")
        _nneg = sum(1 for e in st.session_state.news_results.values()
                    if e.get("result", {}).get("sentiment_label") == "negative")
        st.caption(f"{_n_news} analyzed · {_npos} positive · {_nneg} negative")
        _render_news_compact_summary()
        if _n_news > 0:
            for _t, _ce in st.session_state.news_results.items():
                _sl = _ce["result"]["sentiment_label"].upper()
                _ss = _ce["result"]["sentiment_score"]
                _fd = _ce.get("from_db", False)
                with st.expander(f"**{_t}** — {_sl} ({_ss:+.2f})" + (" [cached]" if _fd else ""), expanded=False):
                    if _fd:
                        st.info(f"Cached from {_ce.get('analyzed_at','')} UTC")
                    _render_analysis(_t, _ce["result"])

    with st.expander("⚡ Options Flow Summary", expanded=False):
        _n_opt = len([e for e in st.session_state.options_results.values() if "_error" not in e])
        st.caption(f"{_n_opt} analyzed")
        _render_options_compact_summary()

    with st.expander("📡 Reddit Sentiment Summary", expanded=False):
        _nwsb = len(st.session_state.wsb_results)
        _wpos = sum(1 for e in st.session_state.wsb_results.values() if (e.get("summary_row") or {}).get("sentiment_label") == "positive")
        _wneg = sum(1 for e in st.session_state.wsb_results.values() if (e.get("summary_row") or {}).get("sentiment_label") == "negative")
        st.caption(f"{_nwsb} analyzed · {_wpos} positive · {_wneg} negative")
        _render_reddit_compact_summary()

    with st.expander("📊 MPT & Portfolio Analytics", expanded=False):
        _mpt_ss = st.session_state.mpt_analysis
        if _mpt_ss:
            _mpt_r  = _mpt_ss.get("result", {})
            _mpt_m  = _mpt_ss.get("metrics", {})
            _mpt_ma = _mpt_r.get("mpt_analysis", {})
            _mpt_sc = _mpt_ma.get("overall_score", "fair")
            _mpt_rb = _mpt_ma.get("rebalancing_priority", "?")
            _pr     = (_mpt_m.get("portfolio_return", 0) or 0) * 100
            _pv     = (_mpt_m.get("portfolio_volatility", 0) or 0) * 100
            _sh     = _mpt_m.get("portfolio_sharpe", 0) or 0
            _msc    = _HEALTH_COLOR.get(_mpt_sc, "#ffd600")
            st.markdown(
                f'<div style="background:{_msc}22;border-left:4px solid {_msc};border-radius:6px;'
                f'padding:8px 14px;margin-bottom:10px">'
                f'<strong style="color:{_msc}">MPT: {_mpt_sc.upper()}</strong>'
                f'<span style="color:#aaa;font-size:0.83rem"> · Rebalancing: {_mpt_rb}</span></div>',
                unsafe_allow_html=True,
            )
            _mm1, _mm2, _mm3 = st.columns(3)
            _mm1.metric("Return", f"{_pr:.1f}%")
            _mm2.metric("Volatility", f"{_pv:.1f}%")
            _mm3.metric("Sharpe", f"{_sh:.2f}")
            _ais = _mpt_r.get("action_items", [])
            if _ais:
                st.caption("**Rebalancing:**")
                for _ai in _ais[:3]:
                    _aic = "#00c853" if any(w in (_ai.get("action","")).lower() for w in ("increase","add","buy")) else "#ff1744" if any(w in (_ai.get("action","")).lower() for w in ("reduce","sell","trim")) else "#aaa"
                    st.markdown(f'<span style="color:{_aic};font-weight:700">{_ai.get("ticker","?")}</span>: {_ai.get("action","?")}', unsafe_allow_html=True)
        else:
            st.info("Run MPT Analysis via ⚡ Analyze Everything or the ⚡ Options & MPT tab.")

    with st.expander("🏦 Smart Money Summary", expanded=False):
        _hf_ss = st.session_state.hf_analysis
        if _hf_ss and "_error" not in _hf_ss:
            _hfps  = _hf_ss.get("portfolio_signal", {})
            _hfst  = _hfps.get("overall_stance", "mixed").upper()
            _hfc   = _hfps.get("confidence", "?")
            _hfcol = {"BULLISH": "#00c853", "BEARISH": "#ff1744", "MIXED": "#ffd600", "DEFENSIVE": "#ff6d00"}.get(_hfst, "#ffd600")
            _hfth  = _hfps.get("cross_ticker_themes", [])
            st.markdown(
                f'<div style="background:{_hfcol}22;border-left:4px solid {_hfcol};border-radius:6px;'
                f'padding:8px 14px;margin-bottom:6px">'
                f'<strong style="color:{_hfcol}">13F: {_hfst}</strong>'
                f'<span style="color:#aaa;font-size:0.83rem"> · Conf: {_hfc}</span></div>',
                unsafe_allow_html=True,
            )
            if _hfth:
                st.caption(" · ".join(_hfth[:3]))
        else:
            st.info("Run Hedge Fund Analysis in the 🏦 Smart Money tab.")
```

Replace with:

```python
with _tab_dash:

    # ── AI Insights section (decision-first, sits directly under KPI bar) ──────
    section_header("AI Insights")

    _ins_btn_col, _ins_run_col, _ins_cap_col = st.columns([2, 2, 4])
    with _ins_btn_col:
        if st.button("⚡ Analyze Everything", use_container_width=True, key="analyze_everything_btn"):
            st.session_state.analyze_all_news    = True
            st.session_state.analyze_all_options = True
            st.session_state.analyze_all_hf      = True
            st.session_state.analyze_all_mpt     = True
            st.session_state.analyze_all_reddit  = True
            st.rerun()
    with _ins_run_col:
        _run_insights = st.button("🤖 Run AI Insights", use_container_width=True, key="ai_insights_btn")
    with _ins_cap_col:
        st.caption("⚡ runs all 5 agents · 🤖 synthesizes with Gemini 2.5 Pro")

    if _run_insights:
        st.session_state.ai_insights = None
        _pd = {
            "balance":         selected_balance,
            "positions":       positions_result,
            "news_results":    st.session_state.news_results,
            "options_results": st.session_state.options_results,
            "wsb_results":     st.session_state.wsb_results,
            "mpt_analysis":    st.session_state.mpt_analysis,
            "hf_analysis":     st.session_state.hf_analysis,
        }
        with st.spinner("Gemini 2.5 Pro synthesizing all portfolio data… (~60–120s)"):
            _insights_result = run_portfolio_insights(_pd)
        st.session_state.ai_insights = _insights_result

    if st.session_state.ai_insights is not None:
        _render_ai_insights(st.session_state.ai_insights)
        if st.button("🔄 Refresh Insights", key="ai_refresh_btn"):
            st.session_state.ai_insights = None
            st.rerun()
    else:
        st.markdown(
            '<div style="background:#161b27;border:1px solid #1e2740;border-radius:8px;'
            'padding:14px 18px;color:#7a85a0;font-size:0.85rem;line-height:1.5">'
            'Run <strong style="color:#e8eaf0">🤖 Run AI Insights</strong> for Gemini 2.5 Pro\'s holistic view — '
            'top actions, key risks, and smart-money divergence across all signals. '
            'For best results, click <strong style="color:#e8eaf0">⚡ Analyze Everything</strong> first.'
            '</div>',
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # ── Portfolio visuals: performance chart, donuts, risk strip, holdings table ──
    _dashboard_visuals_ui(positions_result, tickers)

    st.markdown("---")

    # ── Signal Matrix — one table replacing five collapsed expanders ──────────
    section_header("Signal Matrix")
    st.caption(
        "Summarizes all analysis run so far. Empty cells mean that signal has not been run yet — "
        "use the tabs above or ⚡ Analyze Everything."
    )

    # Build one row per portfolio ticker
    _sig_rows = []
    for _st_ticker in tickers:
        _row: dict = {"Ticker": _st_ticker}

        # News sentiment
        _news_entry = st.session_state.news_results.get(_st_ticker)
        if _news_entry:
            _nr = _news_entry.get("result", {})
            _nl = _nr.get("sentiment_label", "neutral")
            _ns = _nr.get("sentiment_score", 0.0)
            _news_icon = {"positive": "▲", "negative": "▼", "neutral": "●"}.get(_nl, "●")
            _row["News"] = f"{_news_icon} {_nl.capitalize()} ({_ns:+.2f})"
        else:
            _row["News"] = "—"

        # Reddit sentiment
        _wsb_entry = st.session_state.wsb_results.get(_st_ticker)
        if _wsb_entry:
            _wr = (_wsb_entry.get("summary_row") or {})
            _wl = _wr.get("sentiment_label", "neutral")
            _ws = _wr.get("sentiment_score", 0.0)
            _wsb_icon = {"positive": "▲", "negative": "▼", "neutral": "●"}.get(_wl, "●")
            _row["Reddit"] = f"{_wsb_icon} {_wl.capitalize()} ({_ws:+.2f})"
        else:
            _row["Reddit"] = "—"

        # Options bias — find any session key for this ticker
        _opt_bias = "—"
        for _ok, _oe in st.session_state.options_results.items():
            if _ok.startswith(_st_ticker + "|") and "_error" not in _oe:
                _ob = _oe.get("directional_bias", "neutral")
                _ob_icon = {"bullish": "▲", "bearish": "▼", "neutral": "●"}.get(_ob, "●")
                _opt_bias = f"{_ob_icon} {_ob.capitalize()}"
                break
        _row["Options"] = _opt_bias

        # Smart Money — count funds holding this ticker from hf_analysis
        _sm_text = "—"
        _hf_ss = st.session_state.hf_analysis
        if _hf_ss and "_error" not in _hf_ss:
            _hf_pt = _hf_ss.get("per_ticker", {})
            _hf_entry = _hf_pt.get(_st_ticker, {})
            if _hf_entry:
                _fc = _hf_entry.get("fund_count", 0)
                _ot = _hf_entry.get("ownership_type", "bullish_equity")
                _ot_short = {"bullish_equity": "Bullish", "hedged": "Hedged",
                             "speculative_put": "Put", "mixed": "Mixed"}.get(_ot, _ot)
                _sm_text = f"{_ot_short} ({_fc} fund{'s' if _fc != 1 else ''})"
        _row["Smart Money"] = _sm_text

        _sig_rows.append(_row)

    if _sig_rows:
        _sig_df = pd.DataFrame(_sig_rows)

        def _color_signal_cell(val: str) -> str:
            """Return CSS color string for a signal cell value."""
            s = str(val)
            if s == "—":
                return "color: #556080"
            if "▲" in s or "Bullish" in s:
                return "color: #2ecc71; font-weight: 600"
            if "▼" in s or "Bearish" in s or "negative" in s.lower():
                return "color: #e74c3c; font-weight: 600"
            if "neutral" in s.lower() or "Neutral" in s or "●" in s:
                return "color: #ffd600"
            return "color: #e8eaf0"

        _sig_signal_cols = ["News", "Reddit", "Options", "Smart Money"]
        _styled_sig = _sig_df.style.map(_color_signal_cell, subset=_sig_signal_cols)
        st.dataframe(
            _styled_sig,
            use_container_width=True,
            hide_index=True,
            height=38 + 35 * len(_sig_df),
            column_config={
                "Ticker":       st.column_config.TextColumn("Ticker",       width="small"),
                "News":         st.column_config.TextColumn("News",         width="medium"),
                "Reddit":       st.column_config.TextColumn("Reddit",       width="medium"),
                "Options":      st.column_config.TextColumn("Options",      width="small"),
                "Smart Money":  st.column_config.TextColumn("Smart Money",  width="medium"),
            },
        )

    # ── Details expander — per-ticker news cards + MPT card ────────────────────
    with st.expander("Details — per-ticker news & MPT analytics", expanded=False):
        # Per-ticker news detail cards
        _n_news_d = len(st.session_state.news_results)
        if _n_news_d > 0:
            section_header("News Detail")
            for _t, _ce in st.session_state.news_results.items():
                _sl = _ce["result"]["sentiment_label"].upper()
                _ss = _ce["result"]["sentiment_score"]
                _fd = _ce.get("from_db", False)
                with st.expander(
                    f"**{_t}** — {_sl} ({_ss:+.2f})" + (" [cached]" if _fd else ""),
                    expanded=False,
                ):
                    if _fd:
                        st.info(f"Cached from {_ce.get('analyzed_at','')} UTC")
                    _render_analysis(_t, _ce["result"])
        else:
            st.caption("No news analyzed yet — use the 📰 News tab.")

        # MPT card
        _mpt_ss = st.session_state.mpt_analysis
        if _mpt_ss:
            section_header("MPT & Portfolio Analytics")
            _mpt_r  = _mpt_ss.get("result", {})
            _mpt_m  = _mpt_ss.get("metrics", {})
            _mpt_ma = _mpt_r.get("mpt_analysis", {})
            _mpt_sc = _mpt_ma.get("overall_score", "fair")
            _mpt_rb = _mpt_ma.get("rebalancing_priority", "?")
            _pr     = (_mpt_m.get("portfolio_return", 0) or 0) * 100
            _pv     = (_mpt_m.get("portfolio_volatility", 0) or 0) * 100
            _sh     = _mpt_m.get("portfolio_sharpe", 0) or 0
            _msc    = _HEALTH_COLOR.get(_mpt_sc, "#ffd600")
            st.markdown(
                f'<div style="background:{_msc}22;border-left:4px solid {_msc};border-radius:6px;'
                f'padding:8px 14px;margin-bottom:10px">'
                f'<strong style="color:{_msc}">MPT: {_mpt_sc.upper()}</strong>'
                f'<span style="color:#aaa;font-size:0.83rem"> · Rebalancing: {_mpt_rb}</span></div>',
                unsafe_allow_html=True,
            )
            _mm1, _mm2, _mm3 = st.columns(3)
            _mm1.metric("Return", f"{_pr:.1f}%")
            _mm2.metric("Volatility", f"{_pv:.1f}%")
            _mm3.metric("Sharpe", f"{_sh:.2f}")
            _ais = _mpt_r.get("action_items", [])
            if _ais:
                st.caption("Rebalancing actions:")
                for _ai in _ais[:3]:
                    _aic = (
                        "#00c853" if any(w in (_ai.get("action", "")).lower() for w in ("increase", "add", "buy"))
                        else "#ff1744" if any(w in (_ai.get("action", "")).lower() for w in ("reduce", "sell", "trim"))
                        else "#aaa"
                    )
                    st.markdown(
                        f'<span style="color:{_aic};font-weight:700">{_ai.get("ticker","?")}</span>: {_ai.get("action","?")}',
                        unsafe_allow_html=True,
                    )
        else:
            st.caption("Run MPT Analysis via ⚡ Analyze Everything or the ⚡ Options & MPT tab.")
```

**Why:** This is the core layout change.
1. Moving AI Insights above the charts puts the "what to do" answer before raw data — decision-first layout.
2. The bare `st.info()` empty state is replaced by a styled dark card (`background:#161b27; border:1px solid #1e2740`) that matches the page's dark theme better than Streamlit's default blue info box.
3. The five independent collapsed expanders are replaced by a single `st.dataframe` signal matrix. Each row is one portfolio ticker; columns are the four signal domains. Empty cells show "—" in muted gray. The `_color_signal_cell` styler uses `▲`/`▼`/`●` prefix to color green/red/yellow — the same icon vocabulary already used in `_WSB_SENTIMENT_ICON` and `_OPT_BIAS_ICON` elsewhere in the file.
4. All per-ticker news detail cards and the MPT card are preserved and reachable inside a single "Details" expander. Options flow and Reddit row data are accessible via their own tabs; the matrix already shows their summary column.
5. `section_header()` is used for all internal labels, replacing `st.markdown("### ...")` calls.
6. The long buttons-hint caption is shortened from two full sentences to a compact 8-word caption.

---

## Database Changes

None. This plan makes no database schema changes.

---

## UI/UX Specification

### Dashboard tab final layout (top to bottom):

1. **Account selectbox** — compact, 2/5 column split, no caption
2. **KPI bar** — 5 metrics (unchanged)
3. `section_header("AI Insights")` — small-caps blue label
4. **Buttons row** — `[⚡ Analyze Everything] [🤖 Run AI Insights] [compact caption]` in 2-2-4 columns
5. **AI Insights card** — `_render_ai_insights()` result, or the styled dark empty-state card
6. `st.markdown("---")` — divider
7. **`_dashboard_visuals_ui` fragment** (period selector, performance chart, donuts with headers, risk strip with warning deltas, holdings table with `section_header`)
8. `st.markdown("---")` — divider
9. `section_header("Signal Matrix")` + caption
10. **Signal matrix `st.dataframe`** — tickers × (News, Reddit, Options, Smart Money) with color styling
11. **Details expander** — per-ticker news cards + MPT card

### Column config for signal matrix:
- `Ticker`: TextColumn, width="small"
- `News`: TextColumn, width="medium"
- `Reddit`: TextColumn, width="medium"
- `Options`: TextColumn, width="small"
- `Smart Money`: TextColumn, width="medium"
- `hide_index=True`, `height=38 + 35 * len(_sig_df)`

### Donut chart specs after Step 3:
- `textinfo="percent"` (no label in slice)
- `textfont=dict(size=10)`
- `height=260`
- `margin=dict(l=8, r=8, t=32, b=8)`
- `showlegend=True` with `orientation="v"`, `y=0.5`, `x=1.0` (right-side vertical legend)
- No `title` key in layout (title replaced by `section_header()` in Step 5)

### Risk strip delta coloring (Step 6):
| Metric | Threshold | `delta` text | `delta_color` |
|--------|-----------|--------------|---------------|
| Portfolio Beta | > 1.5 | `"⚠ High market exposure"` | `"inverse"` |
| Max Drawdown | < -20.0 | `"⚠ Severe drawdown"` | `"inverse"` |
| Top Position | > 20.0% | `"⚠ Concentrated ({ticker})"` | `"inverse"` |
| Top Position | ≤ 20.0% | ticker name string | `"off"` |

`delta_color="inverse"` in Streamlit renders positive delta text as red and negative delta text as green. Since the ⚠ warning string is always positive (non-empty, non-zero), it will always render red when the threshold is breached.

---

## Testing Checklist

1. **Python syntax check**: Run `python -m py_compile stock-dashboard/pages/9_portfolio.py` from the project root with the venv activated. Expected: no output (success). Failure: a SyntaxError traceback will appear.

2. **Python syntax check ui.py**: Run `python -m py_compile stock-dashboard/components/ui.py`. Expected: no output. (This file was not modified; the check confirms no accidental corruption.)

3. **Launch the app**: Start the dashboard. Navigate to the Portfolio page. Expected: page loads without error or exception in the terminal.

4. **AI Insights position**: On the Dashboard tab, confirm that the "AI Insights" section header and the two action buttons appear **before** the performance chart / donut charts, immediately below the KPI bar. Failure: buttons appear below the charts.

5. **AI Insights empty state card**: Before clicking any button, confirm the empty state is a dark card (dark background, border, no blue `st.info` color). Failure: a light-blue Streamlit info box appears.

6. **AI Insights populated state**: Click "🤖 Run AI Insights". After completion, confirm the `_render_ai_insights()` card renders with the health banner. Failure: error or no output.

7. **Donut charts**: Confirm that the Position Weights and Sector Allocation donuts show only percentage numbers inside slices (no ticker/sector labels inside the ring), and show a right-side legend listing labels. Failure: labels appear inside the ring.

8. **Donut heights**: Confirm that the two donuts stacked in the right column are approximately the same combined height as the performance chart in the left column (no large blank gap). Failure: large blank space below the charts.

9. **Section headers**: Confirm "Position Weights" and "Sector Allocation" appear as small blue all-caps labels above their respective donuts. Confirm "Holdings — N positions · sorted by market value" appears as a small blue all-caps label above the positions table. Failure: no label, or `####` heading style.

10. **Risk strip delta coloring (beta warning)**: If portfolio beta is > 1.5, confirm the Portfolio Beta metric shows a red delta indicator with "⚠ High market exposure". If beta ≤ 1.5, confirm no delta appears. (Test with a high-beta portfolio or temporarily set threshold to 0.0 in a scratch copy to verify delta logic, then restore.)

11. **Risk strip delta coloring (max drawdown)**: Confirm Max Drawdown shows a red "⚠ Severe drawdown" delta when the value is worse than -20%. Otherwise no delta shown.

12. **Risk strip delta coloring (top concentration)**: Confirm Top Position shows a red "⚠ Concentrated" delta when the top position > 20%, and a gray ticker-name delta when ≤ 20%.

13. **Signal matrix — empty state**: Before running any analysis tabs, confirm the signal matrix table shows all "—" in the News/Reddit/Options/Smart Money columns. Confirm the caption explains which tab fills each column. Failure: crash or no table.

14. **Signal matrix — populated**: Run news analysis (News tab) for at least one ticker. Return to the Dashboard tab. Confirm the News column for that ticker now shows a colored ▲/▼/● indicator and a sentiment label. Failure: still shows "—".

15. **Signal matrix — color coding**: Confirm bullish/positive values appear green, bearish/negative values appear red, neutral values appear yellow, and "—" appears in muted gray.

16. **Details expander**: Confirm the "Details — per-ticker news & MPT analytics" expander is present at the bottom of the Dashboard tab. Open it and confirm per-ticker news cards are visible after running news analysis. Failure: expander missing or content missing.

17. **Five old expanders gone**: Confirm that the five individual expanders ("📰 News Sentiment Summary", "⚡ Options Flow Summary", "📡 Reddit Sentiment Summary", "📊 MPT & Portfolio Analytics", "🏦 Smart Money Summary") no longer appear on the Dashboard tab. Failure: any of the five old expanders still visible.

18. **Performance chart caption**: Confirm the performance chart shows a single short caption line (not four sentences). Failure: multi-sentence caption.

19. **Account selector**: Confirm no caption text appears to the right of the account selectbox. Failure: "Select account above" text visible.

20. **Other tabs unchanged**: Click through the News, Options & MPT, Technical Analysis, Reddit, Smart Money, and Market Pulse tabs. Confirm all behave identically to before. Failure: any error or missing section in other tabs.

---

## Rollback Plan

If the changes cause an unrecoverable error:

1. `git diff stock-dashboard/pages/9_portfolio.py` to review what changed.
2. `git checkout stock-dashboard/pages/9_portfolio.py` to restore the file to the last committed state.
3. Confirm the restore worked: `python -m py_compile stock-dashboard/pages/9_portfolio.py` should pass.
4. `git checkout stock-dashboard/components/ui.py` if that file was accidentally modified.

No database changes were made, so no SQL rollback is needed.
