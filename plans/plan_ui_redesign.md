# Plan: Professional UI Redesign

## Overview

This plan delivers a cohesive, professional visual overhaul of the Streamlit stock dashboard. It introduces a dark-mode Streamlit theme via `.streamlit/config.toml`, a shared `components/ui.py` module that injects global CSS and provides reusable header/layout helpers, updated Plotly chart helpers in `components/charts.py`, and targeted edits to `dashboard.py` and all 11 page files to use consistent headers, remove emoji noise, and apply the new helpers. No data-fetching or business logic is changed anywhere.

## Files to Create

- `stock-dashboard/.streamlit/config.toml` — Streamlit theme (dark palette, Inter font, accent color)
- `stock-dashboard/components/ui.py` — Global CSS injector, `page_header()`, `section_header()`, `plotly_dark_layout()` helpers

## Files to Modify

- `stock-dashboard/components/charts.py` — Apply `plotly_dark_layout()` helper to all chart functions
- `stock-dashboard/dashboard.py` — Use `page_header()`, remove emoji from title, clean sidebar, call `inject_global_css()`
- `stock-dashboard/pages/1_metrics.py` — Use `page_header()`, remove emoji from header
- `stock-dashboard/pages/2_thesis.py` — Use `page_header()`, remove emoji from title
- `stock-dashboard/pages/3_watchlist.py` — Use `page_header()`, remove emoji from title
- `stock-dashboard/pages/4_news.py` — Use `page_header()`, clean tab labels
- `stock-dashboard/pages/5_hedge_funds.py` — Use `page_header()`, clean title
- `stock-dashboard/pages/6_option_chains.py` — Use `page_header()`, clean title
- `stock-dashboard/pages/7_positions.py` — Use `page_header()`, clean title
- `stock-dashboard/pages/8_social.py` — Use `page_header()`, clean title
- `stock-dashboard/pages/9_portfolio.py` — Use `page_header()`, clean title
- `stock-dashboard/pages/10_technical_analysis.py` — Use `page_header()`, clean title
- `stock-dashboard/pages/11_backtest.py` — Use `page_header()`, clean title

## Database Changes

None.

## Prerequisites & Dependencies

No new pip packages required. All changes use only the existing Streamlit, Plotly, and Python standard library.

---

## Step-by-Step Implementation

---

### Step 1: Create `.streamlit/config.toml`

**File**: `stock-dashboard/.streamlit/config.toml` (new file — the `.streamlit/` directory does not yet exist)

**Action**: Create the directory `stock-dashboard/.streamlit/` and write the following file:

```toml
[theme]
base = "dark"
primaryColor = "#4f8ef7"
backgroundColor = "#0e1117"
secondaryBackgroundColor = "#161b27"
textColor = "#e8eaf0"
font = "sans serif"

[server]
headless = true
```

**Why**: This sets the global Streamlit palette so every page shares the same dark background (#0e1117), card surface (#161b27), and accent blue (#4f8ef7) without any per-page CSS. `font = "sans serif"` maps to the browser's system sans-serif stack, which is Inter or equivalent on modern OS.

---

### Step 2: Create `components/ui.py`

**File**: `stock-dashboard/components/ui.py` (new file)

**Action**: Write the entire file with the content below. This is the single source of truth for all shared UI helpers.

```python
"""
components/ui.py
Shared UI helpers: global CSS injection, page header, section header,
and a Plotly layout helper. Import and call inject_global_css() at the
top of every page (after st.set_page_config and render_gemini_usage_bar).
"""

import streamlit as st


# ---------------------------------------------------------------------------
# Global CSS
# ---------------------------------------------------------------------------

_GLOBAL_CSS = """
<style>
/* ── Import Inter font ────────────────────────────────────────────── */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

/* ── Root typography ──────────────────────────────────────────────── */
html, body, [class*="css"] {
    font-family: 'Inter', system-ui, -apple-system, sans-serif !important;
}

/* ── Hide Streamlit chrome ───────────────────────────────────────── */
#MainMenu { visibility: hidden; }
footer { visibility: hidden; }
[data-testid="stToolbar"] { visibility: hidden; }
[data-testid="stDecoration"] { display: none; }
header[data-testid="stHeader"] { background: transparent; }

/* ── Main content area: tighter top padding ──────────────────────── */
.block-container {
    padding-top: 1.25rem !important;
    padding-bottom: 2rem !important;
    max-width: 1400px;
}

/* ── Metric cards ─────────────────────────────────────────────────── */
[data-testid="stMetric"] {
    background-color: #161b27;
    border: 1px solid #1e2740;
    border-radius: 10px;
    padding: 14px 18px 12px 18px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.35);
    transition: box-shadow 0.15s ease;
}
[data-testid="stMetric"]:hover {
    box-shadow: 0 4px 16px rgba(79, 142, 247, 0.18);
    border-color: #2a3a60;
}
[data-testid="stMetricLabel"] {
    font-size: 0.72rem !important;
    font-weight: 600 !important;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #8892b0 !important;
}
[data-testid="stMetricValue"] {
    font-size: 1.45rem !important;
    font-weight: 700 !important;
    color: #e8eaf0 !important;
    line-height: 1.2;
}
[data-testid="stMetricDelta"] {
    font-size: 0.78rem !important;
    font-weight: 500 !important;
}

/* ── Sidebar ──────────────────────────────────────────────────────── */
[data-testid="stSidebar"] {
    background-color: #0d1020 !important;
    border-right: 1px solid #1a2035 !important;
}
[data-testid="stSidebar"] .block-container {
    padding-top: 1rem !important;
}
[data-testid="stSidebarNav"] {
    display: none;
}

/* ── Sidebar brand block ─────────────────────────────────────────── */
.sidebar-brand {
    padding: 0.5rem 0.25rem 1rem 0.25rem;
    border-bottom: 1px solid #1a2035;
    margin-bottom: 0.75rem;
}
.sidebar-brand h1 {
    font-size: 1.1rem !important;
    font-weight: 700 !important;
    color: #4f8ef7 !important;
    margin: 0 !important;
    letter-spacing: -0.01em;
}
.sidebar-brand p {
    font-size: 0.7rem !important;
    color: #556080 !important;
    margin: 0 !important;
    line-height: 1.4;
}

/* ── Sidebar nav links ───────────────────────────────────────────── */
[data-testid="stPageLink"] a {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.42rem 0.75rem;
    border-radius: 6px;
    font-size: 0.85rem !important;
    font-weight: 500;
    color: #aab4cf !important;
    text-decoration: none !important;
    transition: background 0.12s ease, color 0.12s ease;
    margin-bottom: 2px;
}
[data-testid="stPageLink"] a:hover {
    background: #1a2540;
    color: #e8eaf0 !important;
}

/* ── Sidebar search input ────────────────────────────────────────── */
[data-testid="stSidebar"] [data-testid="stTextInput"] input {
    background-color: #12182b !important;
    border: 1px solid #1e2740 !important;
    border-radius: 6px !important;
    color: #e8eaf0 !important;
    font-size: 0.875rem !important;
}
[data-testid="stSidebar"] [data-testid="stTextInput"] input:focus {
    border-color: #4f8ef7 !important;
    box-shadow: 0 0 0 2px rgba(79, 142, 247, 0.2) !important;
}

/* ── Buttons ─────────────────────────────────────────────────────── */
[data-testid="stButton"] > button {
    background-color: #1a2540 !important;
    border: 1px solid #2a3a60 !important;
    border-radius: 6px !important;
    color: #c0cbe0 !important;
    font-size: 0.82rem !important;
    font-weight: 600 !important;
    letter-spacing: 0.02em;
    padding: 0.35rem 0.9rem !important;
    transition: background 0.12s ease, border-color 0.12s ease;
}
[data-testid="stButton"] > button:hover {
    background-color: #4f8ef7 !important;
    border-color: #4f8ef7 !important;
    color: #ffffff !important;
}
[data-testid="stButton"] > button:active {
    background-color: #3a70d8 !important;
}

/* ── Primary "Go" button (sidebar) ──────────────────────────────── */
[data-testid="stSidebar"] [data-testid="stButton"] > button {
    background-color: #4f8ef7 !important;
    border-color: #4f8ef7 !important;
    color: #ffffff !important;
    width: 100%;
}
[data-testid="stSidebar"] [data-testid="stButton"] > button:hover {
    background-color: #3a70d8 !important;
    border-color: #3a70d8 !important;
}

/* ── Tabs ────────────────────────────────────────────────────────── */
[data-testid="stTabs"] [data-baseweb="tab-list"] {
    gap: 0;
    border-bottom: 1px solid #1e2740;
    background: transparent;
}
[data-testid="stTabs"] [data-baseweb="tab"] {
    background: transparent !important;
    border: none !important;
    border-bottom: 2px solid transparent !important;
    border-radius: 0 !important;
    color: #7a85a0 !important;
    font-size: 0.82rem !important;
    font-weight: 600 !important;
    letter-spacing: 0.04em;
    padding: 0.5rem 1.1rem !important;
    transition: color 0.12s ease, border-color 0.12s ease;
}
[data-testid="stTabs"] [aria-selected="true"] {
    border-bottom-color: #4f8ef7 !important;
    color: #e8eaf0 !important;
}
[data-testid="stTabs"] [data-baseweb="tab"]:hover {
    color: #c0cbe0 !important;
}

/* ── Dataframes ──────────────────────────────────────────────────── */
[data-testid="stDataFrame"] {
    border-radius: 8px;
    overflow: hidden;
    border: 1px solid #1e2740;
}

/* ── Expanders ───────────────────────────────────────────────────── */
[data-testid="stExpander"] {
    background-color: #12182b !important;
    border: 1px solid #1e2740 !important;
    border-radius: 8px !important;
    margin-bottom: 6px;
}
[data-testid="stExpander"] summary {
    font-size: 0.85rem !important;
    font-weight: 600 !important;
    color: #c0cbe0 !important;
    padding: 0.6rem 0.75rem !important;
}

/* ── Info / Warning / Error / Success boxes ──────────────────────── */
[data-testid="stAlert"] {
    border-radius: 8px !important;
    font-size: 0.85rem !important;
}

/* ── Divider ─────────────────────────────────────────────────────── */
hr {
    border-color: #1a2035 !important;
    margin: 1rem 0 !important;
}

/* ── Page-header block ───────────────────────────────────────────── */
.page-header {
    padding: 0.25rem 0 1rem 0;
    border-bottom: 1px solid #1e2740;
    margin-bottom: 1.25rem;
}
.page-header h1 {
    font-size: 1.6rem !important;
    font-weight: 700 !important;
    color: #e8eaf0 !important;
    margin: 0 0 0.15rem 0 !important;
    letter-spacing: -0.02em;
    line-height: 1.2;
}
.page-header p {
    font-size: 0.82rem !important;
    color: #556080 !important;
    margin: 0 !important;
    line-height: 1.4;
}

/* ── Section header ──────────────────────────────────────────────── */
.section-header {
    font-size: 0.68rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: #4f8ef7 !important;
    margin: 1.5rem 0 0.6rem 0;
}

/* ── Caption / footer text ───────────────────────────────────────── */
[data-testid="stCaptionContainer"] p {
    font-size: 0.73rem !important;
    color: #556080 !important;
}

/* ── Progress bar ────────────────────────────────────────────────── */
[data-testid="stProgress"] > div > div {
    background-color: #4f8ef7 !important;
}
[data-testid="stProgress"] > div {
    background-color: #1a2035 !important;
    border-radius: 4px;
    height: 5px !important;
}

/* ── Selectbox / text-input labels ──────────────────────────────── */
[data-testid="stSelectbox"] label,
[data-testid="stTextInput"] label,
[data-testid="stSlider"] label {
    font-size: 0.75rem !important;
    font-weight: 600 !important;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: #8892b0 !important;
}

/* ── Landing page feature cards ──────────────────────────────────── */
.feature-card {
    background: #12182b;
    border: 1px solid #1e2740;
    border-radius: 10px;
    padding: 1.1rem 1.25rem 1rem 1.25rem;
    height: 100%;
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.feature-card:hover {
    border-color: #2a3a60;
    box-shadow: 0 4px 18px rgba(79, 142, 247, 0.12);
}
.feature-card h3 {
    font-size: 0.9rem !important;
    font-weight: 700 !important;
    color: #e8eaf0 !important;
    margin: 0 0 0.5rem 0 !important;
}
.feature-card p {
    font-size: 0.78rem !important;
    color: #7a85a0 !important;
    line-height: 1.55;
    margin: 0 0 0.75rem 0 !important;
}

/* ── Gemini usage bar container ──────────────────────────────────── */
.gemini-bar-container {
    background: #0d1020;
    border-bottom: 1px solid #1a2035;
    padding: 0.4rem 0 0.5rem 0;
    margin-bottom: 0.75rem;
}
</style>
"""


def inject_global_css() -> None:
    """Inject the global CSS block into the current Streamlit page.

    Call this once per page, immediately after render_gemini_usage_bar().
    Safe to call multiple times — Streamlit deduplicates identical HTML blocks.
    """
    st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Page header component
# ---------------------------------------------------------------------------

def page_header(title: str, subtitle: str = "") -> None:
    """Render a consistent page header with title and optional muted subtitle.

    Replaces ad-hoc st.title() / st.header() calls at the top of each page.

    Args:
        title:    The page title (plain text, no emoji).
        subtitle: Optional one-line description shown in muted text below title.
    """
    subtitle_html = f"<p>{subtitle}</p>" if subtitle else ""
    st.markdown(
        f'<div class="page-header"><h1>{title}</h1>{subtitle_html}</div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Section header component
# ---------------------------------------------------------------------------

def section_header(label: str) -> None:
    """Render a small all-caps section label used to separate content blocks.

    Use this instead of st.subheader() for internal page sections where a
    lighter visual weight is appropriate (e.g., above a metric row).

    Args:
        label: The section label text (will be uppercased via CSS).
    """
    st.markdown(
        f'<p class="section-header">{label}</p>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Plotly dark layout helper
# ---------------------------------------------------------------------------

def plotly_dark_layout(**overrides) -> dict:
    """Return a Plotly layout dict with the dashboard's canonical dark theme.

    Merges the base dark style with any keyword overrides you supply.
    Intended to be passed directly to fig.update_layout(**plotly_dark_layout(...)).

    Usage:
        fig.update_layout(**plotly_dark_layout(title="My Chart", height=450))

    Base values (can all be overridden):
        template         = "plotly_dark"
        paper_bgcolor    = "#0e1117"   (matches config.toml backgroundColor)
        plot_bgcolor     = "#161b27"   (matches config.toml secondaryBackgroundColor)
        font.family      = "Inter, system-ui, sans-serif"
        font.color       = "#e8eaf0"
        font.size        = 12
        xaxis.gridcolor  = "#1e2740"
        yaxis.gridcolor  = "#1e2740"
        xaxis.linecolor  = "#1e2740"
        yaxis.linecolor  = "#1e2740"
        margin           = dict(l=48, r=24, t=48, b=40)
        legend.bgcolor   = "rgba(0,0,0,0)"
        legend.bordercolor = "#1e2740"
        legend.font.size = 11
        hoverlabel.bgcolor = "#161b27"
        hoverlabel.bordercolor = "#2a3a60"
        hoverlabel.font.size = 12
    """
    base = dict(
        template="plotly_dark",
        paper_bgcolor="#0e1117",
        plot_bgcolor="#161b27",
        font=dict(family="Inter, system-ui, sans-serif", color="#e8eaf0", size=12),
        xaxis=dict(gridcolor="#1e2740", linecolor="#1e2740", zerolinecolor="#1e2740"),
        yaxis=dict(gridcolor="#1e2740", linecolor="#1e2740", zerolinecolor="#1e2740"),
        margin=dict(l=48, r=24, t=48, b=40),
        legend=dict(
            bgcolor="rgba(0,0,0,0)",
            bordercolor="#1e2740",
            font=dict(size=11),
        ),
        hoverlabel=dict(
            bgcolor="#161b27",
            bordercolor="#2a3a60",
            font=dict(size=12),
        ),
    )
    base.update(overrides)
    return base
```

**Why**: Centralising CSS and Plotly layout means every page gets a consistent look without duplicating style code. The CSS uses stable `data-testid` selectors that Streamlit maintains across versions.

---

### Step 3: Update `components/charts.py` to use `plotly_dark_layout()`

**File**: `stock-dashboard/components/charts.py`

**Action**: Add the import of `plotly_dark_layout` at the top of the file, then replace the `fig.update_layout(...)` call in each of the five chart functions.

#### 3a — Add import at top of file

Locate the existing imports block (lines 1–6):
```python
from typing import Optional
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

_THEME = "plotly_dark"
```

Replace it with:
```python
from typing import Optional
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from components.ui import plotly_dark_layout

_THEME = "plotly_dark"
```

#### 3b — Update `_empty_fig()`

Find:
```python
    fig.update_layout(
        template=_THEME,
        annotations=[{"text": message, "xref": "paper", "yref": "paper",
                       "x": 0.5, "y": 0.5, "showarrow": False,
                       "font": {"size": 16, "color": "gray"}}],
        xaxis_visible=False, yaxis_visible=False,
    )
```

Replace with:
```python
    fig.update_layout(
        **plotly_dark_layout(
            annotations=[{"text": message, "xref": "paper", "yref": "paper",
                           "x": 0.5, "y": 0.5, "showarrow": False,
                           "font": {"size": 16, "color": "#556080"}}],
            xaxis_visible=False,
            yaxis_visible=False,
        )
    )
```

#### 3c — Update `price_chart()`

Find:
```python
    fig.update_layout(
        template=_THEME,
        title=f"{ticker} — Price",
        xaxis_rangeslider_visible=False,
        xaxis_title="Date",
        yaxis_title="Price (USD)",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02},
    )
```

Replace with:
```python
    fig.update_layout(
        **plotly_dark_layout(
            title=f"{ticker} — Price",
            xaxis_rangeslider_visible=False,
            xaxis_title="Date",
            yaxis_title="Price (USD)",
            legend={"orientation": "h", "yanchor": "bottom", "y": 1.02,
                    "bgcolor": "rgba(0,0,0,0)", "font": {"size": 11}},
        )
    )
```

#### 3d — Update `revenue_chart()`

Find:
```python
    fig.update_layout(
        template=_THEME,
        title=f"{ticker} — Annual Revenue",
        xaxis_title="Year", yaxis_title="Revenue (USD B)",
    )
```

Replace with:
```python
    fig.update_layout(
        **plotly_dark_layout(
            title=f"{ticker} — Annual Revenue",
            xaxis_title="Year",
            yaxis_title="Revenue (USD B)",
        )
    )
```

#### 3e — Update `margin_chart()`

Find:
```python
    fig.update_layout(
        template=_THEME,
        title=f"{ticker} — Margins %",
        xaxis_title="Year", yaxis_title="Margin (%)",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02},
    )
```

Replace with:
```python
    fig.update_layout(
        **plotly_dark_layout(
            title=f"{ticker} — Margins %",
            xaxis_title="Year",
            yaxis_title="Margin (%)",
            legend={"orientation": "h", "yanchor": "bottom", "y": 1.02,
                    "bgcolor": "rgba(0,0,0,0)", "font": {"size": 11}},
        )
    )
```

#### 3f — Update `fcf_chart()`

Find:
```python
    fig.update_layout(
        template=_THEME,
        title=f"{ticker} — Free Cash Flow",
        xaxis_title="Year", yaxis_title="FCF (USD B)",
    )
```

Replace with:
```python
    fig.update_layout(
        **plotly_dark_layout(
            title=f"{ticker} — Free Cash Flow",
            xaxis_title="Year",
            yaxis_title="FCF (USD B)",
        )
    )
```

#### 3g — Update `earnings_chart()`

Find:
```python
    fig.update_layout(
        template=_THEME,
        title=f"{ticker} — EPS Actual vs. Estimate",
        barmode="group",
        xaxis_title="Quarter", yaxis_title="EPS (USD)",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02},
    )
```

Replace with:
```python
    fig.update_layout(
        **plotly_dark_layout(
            title=f"{ticker} — EPS Actual vs. Estimate",
            barmode="group",
            xaxis_title="Quarter",
            yaxis_title="EPS (USD)",
            legend={"orientation": "h", "yanchor": "bottom", "y": 1.02,
                    "bgcolor": "rgba(0,0,0,0)", "font": {"size": 11}},
        )
    )
```

**Why**: All five shared chart functions now emit charts that match the global dark theme (matching background, gridline colour, font) rather than using Plotly's default dark template which has a different background shade.

---

### Step 4: Update `dashboard.py`

**File**: `stock-dashboard/dashboard.py`

**Action**: Four targeted edits.

#### 4a — Add import of `inject_global_css` and `page_header`

Find the existing imports block (lines 1–7):
```python
from datetime import datetime
from typing import Optional

import streamlit as st

from components.gemini_usage_bar import render_gemini_usage_bar
from data.fetcher import get_stock_info
```

Replace with:
```python
from datetime import datetime
from typing import Optional

import streamlit as st

from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import inject_global_css, page_header
from data.fetcher import get_stock_info
```

#### 4b — Call `inject_global_css()` after `render_gemini_usage_bar()`

Find:
```python
render_gemini_usage_bar()

_PAGES = [
```

Replace with:
```python
render_gemini_usage_bar()
inject_global_css()

_PAGES = [
```

#### 4c — Replace the sidebar brand block and nav

Find the entire `_render_sidebar()` function:
```python
def _render_sidebar() -> None:
    """Compose all sidebar sections."""
    st.sidebar.title("📈 Stock Dashboard")
    st.sidebar.markdown("---")
    _render_sidebar_search()
    st.sidebar.markdown("---")
    _render_sidebar_nav()
    _render_sidebar_footer()
```

Replace with:
```python
def _render_sidebar() -> None:
    """Compose all sidebar sections."""
    st.sidebar.markdown(
        '<div class="sidebar-brand">'
        '<h1>Stock Dashboard</h1>'
        '<p>Live data · AI analysis · Portfolio</p>'
        '</div>',
        unsafe_allow_html=True,
    )
    _render_sidebar_search()
    st.sidebar.markdown('<p class="section-header" style="padding-left:0.25rem;">Navigation</p>', unsafe_allow_html=True)
    _render_sidebar_nav()
    _render_sidebar_footer()
```

#### 4d — Replace the sidebar nav to use plain labels (no emoji)

Find:
```python
def _render_sidebar_nav() -> None:
    """Render page navigation links with icons."""
    st.sidebar.markdown("### Navigation")
    for page in _PAGES:
        st.sidebar.page_link(page["path"], label=f"{page['icon']} {page['label']}")
```

Replace with:
```python
def _render_sidebar_nav() -> None:
    """Render page navigation links."""
    for page in _PAGES:
        st.sidebar.page_link(page["path"], label=page["label"])
```

#### 4e — Replace the sidebar search header (remove emoji from `### 🔍 Global Search`)

Find:
```python
    st.sidebar.markdown("### 🔍 Global Search")
```

Replace with:
```python
    st.sidebar.markdown('<p class="section-header" style="padding-left:0.25rem;">Search</p>', unsafe_allow_html=True)
```

#### 4f — Replace the main page title and subtitle in `main()`

Find:
```python
    st.title("📈 Stock Dashboard")
    st.markdown("##### Live market data · Investment thesis tracker · Watchlist")
    st.markdown("---")
```

Replace with:
```python
    page_header("Stock Dashboard", "Live market data · Investment thesis tracker · Watchlist")
```

#### 4g — Replace the `_render_quick_stats` section header (remove rocket emoji)

Find:
```python
    st.markdown("#### 🚀 Stocks")
```

Replace with:
```python
    st.markdown('<p class="section-header">Stocks</p>', unsafe_allow_html=True)
```

#### 4h — Replace the indexes section header (remove globe emoji)

Find:
```python
    st.markdown("#### 🌎 Major Indexes")
```

Replace with:
```python
    st.markdown('<p class="section-header">Major Indexes</p>', unsafe_allow_html=True)
```

#### 4i — Replace the market snapshot subheader and landing section

Find:
```python
    st.subheader("Market Snapshot")
    _render_indexes()
    st.markdown("---")
    _render_quick_stats()
```

Replace with:
```python
    st.markdown('<p class="section-header">Market Snapshot</p>', unsafe_allow_html=True)
    _render_indexes()
    st.divider()
    _render_quick_stats()
```

#### 4j — Replace the `_render_landing` feature-card section to use styled cards

Find the entire `_render_landing()` function body starting from `st.markdown("---")` through the final `st.caption(...)`:
```python
def _render_landing() -> None:
    """Render the main dashboard landing page with instructions."""
    st.markdown("---")
    st.subheader("Welcome")
    st.markdown(
        "Use the **sidebar search** to look up any ticker instantly, "
        "or navigate to a section below."
    )

    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        st.markdown("### 📊 Metrics")
        st.markdown(
            "Deep-dive valuation, profitability, growth, and balance sheet "
            "metrics for any ticker. Includes candlestick, revenue, margin, "
            "FCF, and earnings charts."
        )
        st.page_link("pages/1_metrics.py", label="Open Metrics →")

    with col2:
        st.markdown("### 📝 Thesis Tracker")
        st.markdown(
            "Write and store investment theses with conviction level, "
            "price targets, catalysts, and bear cases. Tracks metrics at "
            "time of writing vs. today."
        )
        st.page_link("pages/2_thesis.py", label="Open Thesis Tracker →")

    with col3:
        st.markdown("### 👁️ Watchlist")
        st.markdown(
            "Monitor a list of tickers with live prices, P/E, gross margin, "
            "52-week range, and alert prices. One-click to analyze any ticker."
        )
        st.page_link("pages/3_watchlist.py", label="Open Watchlist →")

    with col4:
        st.markdown("### 📰 News")
        st.markdown(
            "Latest news for any ticker via Massive.com, with sentiment analysis. "
            "Paywalled sources are automatically skipped. Click any article "
            "to scrape the full text."
        )
        st.page_link("pages/4_news.py", label="Open News →")

    with col5:
        st.markdown("### 🏦 Hedge Funds")
        st.markdown(
            "Concentrated hedge fund portfolios from SEC 13F filings. "
            "Browse the top, bottom, and a daily-rotating pick from funds "
            "with fewer than 15 reported positions."
        )
        st.page_link("pages/5_hedge_funds.py", label="Open Hedge Funds →")

    st.markdown("---")
    st.caption(
        "Data provided by [Yahoo Finance](https://finance.yahoo.com) via yfinance. "
        "Not financial advice. All data cached for 1 hour."
    )
```

Replace the entire function with:
```python
def _render_landing() -> None:
    """Render the main dashboard landing page with instructions."""
    st.divider()
    st.markdown('<p class="section-header">Quick Access</p>', unsafe_allow_html=True)

    col1, col2, col3, col4, col5 = st.columns(5)

    _cards = [
        (col1, "Metrics",
         "Deep-dive valuation, profitability, growth, and balance sheet metrics "
         "with candlestick, revenue, margin, FCF, and earnings charts.",
         "pages/1_metrics.py", "Open Metrics"),
        (col2, "Thesis Tracker",
         "Write and store investment theses with conviction level, price targets, "
         "catalysts, and bear cases. Tracks metrics at time of writing vs. today.",
         "pages/2_thesis.py", "Open Thesis Tracker"),
        (col3, "Watchlist",
         "Monitor tickers with live prices, P/E, gross margin, 52-week range, "
         "and alert prices. One-click to analyze any ticker.",
         "pages/3_watchlist.py", "Open Watchlist"),
        (col4, "News",
         "Latest ticker news via Massive.com with sentiment analysis. "
         "Paywalled sources are automatically skipped.",
         "pages/4_news.py", "Open News"),
        (col5, "Hedge Funds",
         "Concentrated hedge fund portfolios from SEC 13F filings. "
         "Top, bottom, and a daily-rotating pick from funds with fewer than 15 positions.",
         "pages/5_hedge_funds.py", "Open Hedge Funds"),
    ]

    for col, title, desc, path, link_label in _cards:
        with col:
            st.markdown(
                f'<div class="feature-card">'
                f'<h3>{title}</h3>'
                f'<p>{desc}</p>'
                f'</div>',
                unsafe_allow_html=True,
            )
            st.page_link(path, label=f"{link_label} →")

    st.divider()
    st.caption(
        "Data provided by Yahoo Finance via yfinance. "
        "Not financial advice. All data cached for 1 hour."
    )
```

**Why**: The landing page now uses the `.feature-card` CSS class for a consistent card-grid look without raw emoji headers. The `page_header()` call replaces the `st.title()` + `st.markdown` subtitle pattern.

---

### Step 5: Update `pages/1_metrics.py`

**File**: `stock-dashboard/pages/1_metrics.py`

**Action**: Two edits.

#### 5a — Add import

Find:
```python
from components.metric_cards import render_metric_group
from components.charts import (
```

Replace with:
```python
from components.metric_cards import render_metric_group
from components.charts import (
```

Then find:
```python
from components.charts import (
    price_chart, revenue_chart, margin_chart, fcf_chart, earnings_chart,
)
```

Replace with:
```python
from components.charts import (
    price_chart, revenue_chart, margin_chart, fcf_chart, earnings_chart,
)
from components.ui import inject_global_css, page_header
```

#### 5b — Call `inject_global_css()` after `render_gemini_usage_bar()`

Find:
```python
render_gemini_usage_bar()


# ---------------------------------------------------------------------------
# Sidebar — benchmark reference
```

Replace with:
```python
render_gemini_usage_bar()
inject_global_css()


# ---------------------------------------------------------------------------
# Sidebar — benchmark reference
```

#### 5c — Replace `st.header("Stock Metrics")` in `main()`

Find:
```python
    _render_sidebar()
    st.header("Stock Metrics")
```

Replace with:
```python
    _render_sidebar()
    page_header("Stock Metrics", "Valuation, profitability, growth, and balance sheet metrics for any ticker.")
```

#### 5d — Replace `st.title(name)` in `_render_header()`

Find:
```python
    st.title(name)
    col1, col2, col3, col4 = st.columns(4)
```

Replace with:
```python
    st.markdown(f'<h2 style="font-size:1.35rem;font-weight:700;color:#e8eaf0;margin:0 0 0.75rem 0;">{name}</h2>', unsafe_allow_html=True)
    col1, col2, col3, col4 = st.columns(4)
```

**Why**: The company name in `_render_header` is a dynamic value, so we keep it as a styled inline heading rather than routing through `page_header()` which is meant for static top-of-page titles.

---

### Step 6: Update `pages/2_thesis.py`

**File**: `stock-dashboard/pages/2_thesis.py`

**Action**: Two edits.

#### 6a — Add import

Find:
```python
st.set_page_config(page_title="Thesis Tracker", layout="wide")

render_gemini_usage_bar()
```

Replace with:
```python
st.set_page_config(page_title="Thesis Tracker", layout="wide")

render_gemini_usage_bar()
from components.ui import inject_global_css, page_header
inject_global_css()
```

#### 6b — Replace `st.title("Thesis Tracker")` in `main()`

Find:
```python
def main() -> None:
    """Entry point for the Thesis Tracker page."""
    st.title("Thesis Tracker")
```

Replace with:
```python
def main() -> None:
    """Entry point for the Thesis Tracker page."""
    page_header("Thesis Tracker", "Write, track, and compare investment theses against live metrics.")
```

**Why**: Consistent page header pattern; removes bare `st.title()`.

---

### Step 7: Update `pages/3_watchlist.py`

**File**: `stock-dashboard/pages/3_watchlist.py`

**Action**: Two edits.

#### 7a — Add import

Find:
```python
st.set_page_config(page_title="Watchlist", layout="wide")

render_gemini_usage_bar()
```

Replace with:
```python
st.set_page_config(page_title="Watchlist", layout="wide")

render_gemini_usage_bar()
from components.ui import inject_global_css, page_header
inject_global_css()
```

#### 7b — Replace `st.title("Watchlist")` in `main()`

Find:
```python
def main() -> None:
    """Entry point for the Watchlist page."""
    st.title("Watchlist")
    _render_add_form()
```

Replace with:
```python
def main() -> None:
    """Entry point for the Watchlist page."""
    page_header("Watchlist", "Monitor tickers with live prices, key metrics, and price alerts.")
    _render_add_form()
```

---

### Step 8: Update `pages/4_news.py`

**File**: `stock-dashboard/pages/4_news.py`

**Action**: Three edits.

#### 8a — Add import

Find:
```python
st.set_page_config(page_title="News", layout="wide")

render_gemini_usage_bar()
```

Replace with:
```python
st.set_page_config(page_title="News", layout="wide")

render_gemini_usage_bar()
from components.ui import inject_global_css, page_header
inject_global_css()
```

#### 8b — Replace the stock news tab header

Find:
```python
    st.header("📈 Stock News")
```

Replace with:
```python
    page_header("Stock News", "Latest news for any ticker via Massive.com, with sentiment analysis.")
```

#### 8c — Replace the market pulse tab header

Find:
```python
    st.header("🌍 Market Pulse")
```

Replace with:
```python
    page_header("Market Pulse", "Macro news analysis — monetary policy, geopolitical events, and sector moves.")
```

#### 8d — Replace the main tab labels to remove emoji

Find:
```python
    tab1, tab2 = st.tabs(["📈 Stock News", "🌍 Market Pulse"])
```

Replace with:
```python
    tab1, tab2 = st.tabs(["Stock News", "Market Pulse"])
```

---

### Step 9: Update `pages/5_hedge_funds.py`

**File**: `stock-dashboard/pages/5_hedge_funds.py`

**Action**: Two edits.

#### 9a — Add import

Find:
```python
st.set_page_config(page_title="Hedge Funds", page_icon="🏦", layout="wide")

render_gemini_usage_bar()
```

Replace with:
```python
st.set_page_config(page_title="Hedge Funds", page_icon="🏦", layout="wide")

render_gemini_usage_bar()
from components.ui import inject_global_css, page_header
inject_global_css()
```

#### 9b — Replace title in `main()`

Find:
```python
    st.title("Concentrated Hedge Funds — 13F Analysis")
    st.markdown(
        "Institutional managers with **fewer than 15 reported positions** in their "
        "latest SEC 13F-HR filing, grouped by portfolio size."
    )
    st.markdown("---")
```

Replace with:
```python
    page_header(
        "Hedge Funds — 13F Analysis",
        "Institutional managers with fewer than 15 reported positions in their latest SEC 13F-HR filing.",
    )
```

---

### Step 10: Update `pages/6_option_chains.py`

**File**: `stock-dashboard/pages/6_option_chains.py`

**Action**: Three edits.

#### 10a — Add import after the existing imports at the top of file (lines 1–10)

Find:
```python
from components.gemini_usage_bar import render_gemini_usage_bar
from data.options_agent import run_options_analysis
```

Replace with:
```python
from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import inject_global_css, page_header
from data.options_agent import run_options_analysis
```

#### 10b — Call `inject_global_css()` after `render_gemini_usage_bar()`

Find:
```python
st.set_page_config(page_title="Option Chain Viewer", layout="wide")

render_gemini_usage_bar()

st.title("Option Chain Viewer")
```

Replace with:
```python
st.set_page_config(page_title="Option Chain Viewer", layout="wide")

render_gemini_usage_bar()
inject_global_css()

page_header("Option Chain Viewer", "Live options chain with Black-Scholes Greeks, IV smile, and AI analysis.")
```

#### 10c — Also update the two Plotly chart `update_layout` calls inside `_render_option_curves()` to use `plotly_dark_layout()`

Find:
```python
    fig_iv.update_layout(
        title=f"Implied Volatility Smile — {expiry}",
        xaxis_title="Strike Price ($)",
        yaxis_title="Implied Volatility (%)",
        template="plotly_dark",
        height=400,
        margin=dict(l=40, r=40, t=50, b=40),
        legend=dict(orientation="h", y=1.02, x=1, xanchor="right"),
        hovermode="x unified",
    )
```

Replace with:
```python
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
```

And find:
```python
    fig_g.update_layout(
        title=f"{label} vs Strike — {opt_type.capitalize()}s expiring {expiry}",
        xaxis_title="Strike Price ($)",
        yaxis_title=label,
        template="plotly_dark",
        height=400,
        margin=dict(l=40, r=40, t=50, b=40),
        hovermode="x unified",
    )
```

Replace with:
```python
    fig_g.update_layout(
        **plotly_dark_layout(
            title=f"{label} vs Strike — {opt_type.capitalize()}s expiring {expiry}",
            xaxis_title="Strike Price ($)",
            yaxis_title=label,
            height=400,
            hovermode="x unified",
        )
    )
```

To make `plotly_dark_layout` available in step 10c, add the import at the top alongside the other component import added in 10a:

Find (after edit 10a):
```python
from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import inject_global_css, page_header
from data.options_agent import run_options_analysis
```

Replace with:
```python
from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import inject_global_css, page_header, plotly_dark_layout
from data.options_agent import run_options_analysis
```

---

### Step 11: Update `pages/7_positions.py`

**File**: `stock-dashboard/pages/7_positions.py`

**Action**: Three edits.

#### 11a — Add import

Find:
```python
import streamlit as st
import pandas as pd
from components.gemini_usage_bar import render_gemini_usage_bar
```

Replace with:
```python
import streamlit as st
import pandas as pd
from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import inject_global_css, page_header
```

#### 11b — Call `inject_global_css()` after `render_gemini_usage_bar()`

Find:
```python
render_gemini_usage_bar()

st.title("Portfolio Positions")
```

Replace with:
```python
render_gemini_usage_bar()
inject_global_css()

page_header("Portfolio Positions", "Live account balances and open positions from Webull.")
```

#### 11c — Replace bare `st.subheader("Positions")`

Find:
```python
st.subheader("Positions")
```

Replace with:
```python
st.markdown('<p class="section-header">Positions</p>', unsafe_allow_html=True)
```

---

### Step 12: Update `pages/8_social.py`

**File**: `stock-dashboard/pages/8_social.py`

**Action**: Two edits.

#### 12a — Add import

Find:
```python
from components.gemini_usage_bar import render_gemini_usage_bar
from data.reddit_fetcher import fetch_top_posts_for_ticker, fetch_daily_top_tickers, TOP_N
```

Replace with:
```python
from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import inject_global_css, page_header
from data.reddit_fetcher import fetch_top_posts_for_ticker, fetch_daily_top_tickers, TOP_N
```

#### 12b — Call `inject_global_css()` after `render_gemini_usage_bar()`

Find:
```python
render_gemini_usage_bar()

# ---------------------------------------------------------------------------
# Reddit DB paths (unchanged from 8_reddit.py)
```

Replace with:
```python
render_gemini_usage_bar()
inject_global_css()

# ---------------------------------------------------------------------------
# Reddit DB paths (unchanged from 8_reddit.py)
```

#### 12c — Replace `st.title("🌐 Social Sentiment")` in the main render function

Find:
```python
    st.title("🌐 Social Sentiment")
```

Replace with:
```python
    page_header("Social Sentiment", "Reddit WallStreetBets and Twitter/X sentiment analysis.")
```

---

### Step 13: Update `pages/9_portfolio.py`

**File**: `stock-dashboard/pages/9_portfolio.py`

**Action**: Three edits.

#### 13a — Add import after the existing component imports

Find:
```python
from components.gemini_usage_bar import render_gemini_usage_bar
from data.options_agent import run_options_analysis
```

Replace with:
```python
from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import inject_global_css, page_header
from data.options_agent import run_options_analysis
```

#### 13b — Call `inject_global_css()` after `render_gemini_usage_bar()`

Find:
```python
render_gemini_usage_bar()

st.title("Portfolio")
```

Replace with:
```python
render_gemini_usage_bar()
inject_global_css()

page_header("Portfolio", "AI-powered portfolio analysis — news, options, technical patterns, and smart money.")
```

---

### Step 14: Update `pages/10_technical_analysis.py`

**File**: `stock-dashboard/pages/10_technical_analysis.py`

**Action**: Three edits.

#### 14a — Add import

Find:
```python
from components.gemini_usage_bar import render_gemini_usage_bar
from analytics.patterns import DetectedPattern, PatternDetectionEngine
```

Replace with:
```python
from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import inject_global_css, page_header
from analytics.patterns import DetectedPattern, PatternDetectionEngine
```

#### 14b — Call `inject_global_css()` after `render_gemini_usage_bar()`

Find:
```python
st.set_page_config(page_title="Technical Analysis", page_icon="📈", layout="wide")
render_gemini_usage_bar()
```

Replace with:
```python
st.set_page_config(page_title="Technical Analysis", page_icon="📈", layout="wide")
render_gemini_usage_bar()
inject_global_css()
```

#### 14c — Replace `st.title("📈 Technical Analysis")` inside the main render function

Find:
```python
    st.title("📈 Technical Analysis")
```

Replace with:
```python
    page_header("Technical Analysis", "Candlestick charts, EMA/Bollinger overlays, and AI pattern detection.")
```

---

### Step 15: Update `pages/11_backtest.py`

**File**: `stock-dashboard/pages/11_backtest.py`

**Action**: Three edits.

#### 15a — Add import

Find:
```python
from components.gemini_usage_bar import render_gemini_usage_bar
from data.backtest_engine import (
```

Replace with:
```python
from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import inject_global_css, page_header
from data.backtest_engine import (
```

#### 15b — Call `inject_global_css()` after `render_gemini_usage_bar()`

Find:
```python
st.set_page_config(page_title="AI Signal Backtester", layout="wide")

render_gemini_usage_bar()
```

Replace with:
```python
st.set_page_config(page_title="AI Signal Backtester", layout="wide")

render_gemini_usage_bar()
inject_global_css()
```

#### 15c — Replace `st.title("AI Signal Backtester")` inside the main render function

Find:
```python
    st.title("AI Signal Backtester")
```

Replace with:
```python
    page_header("AI Signal Backtester", "Backtest AI-generated signals against historical price data.")
```

---

## Testing Checklist

After all steps are complete, run `streamlit run dashboard.py` from `stock-dashboard/` with the venv activated and verify the following:

1. **Config.toml takes effect**: The app background is `#0e1117` (very dark navy), sidebar background is darker `#0d1020`, and buttons/progress bars use the blue accent `#4f8ef7`. If the background is pure black or default Streamlit blue, the config.toml is not being read — check that `stock-dashboard/.streamlit/config.toml` exists.

2. **Home page header**: The home page shows "Stock Dashboard" as the clean `page_header` component (large white text, muted subtitle below, underline divider). There must be no emoji in the title.

3. **Sidebar branding**: The sidebar shows "Stock Dashboard" in blue with the sub-caption "Live data · AI analysis · Portfolio". The default Streamlit auto-nav list is hidden. Navigation links show plain text labels without emoji.

4. **Sidebar search**: The search input has a dark background (`#12182b`) with a blue focus ring. The "Go" button is filled blue.

5. **Metric cards (home page market snapshot)**: Each `st.metric` block has a visible dark card border, rounded corners, and a subtle shadow. Verify on the home page stock/index row.

6. **Metrics page**: Navigate to Metrics page, enter "AAPL". The page header reads "Stock Metrics" (no emoji). Below the ticker input, the company name appears as the inline `h2` heading. Metric groups render as card rows with the same bordered appearance.

7. **Charts on Metrics page**: Switch through all 5 chart tabs. Each chart has background `#0e1117`, grid lines `#1e2740`, and consistent font. Specifically verify the price candlestick chart does NOT show a white or grey background.

8. **Option Chains page**: Navigate to Option Chains, enter "AAPL". The page header reads "Option Chain Viewer". The IV Smile and Greeks Curve charts use the dark layout (not the default `plotly_dark` which has a lighter background).

9. **All other pages**: Navigate to Thesis Tracker, Watchlist, News, Hedge Funds, Positions, Social Sentiment, Portfolio, Technical Analysis, Backtest. Each should show its clean `page_header()` title without emoji.

10. **Tab styling**: On the Metrics page chart tabs and the News page tabs, the active tab has a blue underline and the tab text is white. Inactive tabs are muted grey.

11. **Streamlit footer hidden**: The "Made with Streamlit" footer and the hamburger deploy button are not visible on any page.

12. **Expanders**: On the Metrics sidebar (Benchmark Guide expanders) and on the Hedge Funds page (fund detail expanders), the expander headers are styled with the dark card background and show the rounded border.

13. **No import errors**: If any page throws an `ImportError` for `components.ui`, check that `stock-dashboard/components/ui.py` was created in Step 2 and that the import statement in the failing page matches exactly `from components.ui import inject_global_css, page_header` (or `plotly_dark_layout` where applicable).

14. **Edge case — offline/no data**: On the Metrics page with an invalid ticker (e.g., "ZZZZ"), the `st.error()` alert should still appear with correct styling (rounded red box, readable text).

---

## Rollback Plan

If the app breaks after any step:

1. Delete `stock-dashboard/.streamlit/config.toml` — this immediately reverts to the default Streamlit dark theme.
2. Delete `stock-dashboard/components/ui.py` — removes the new module.
3. In each modified page file, revert the three changes per page:
   - Remove the `from components.ui import ...` line.
   - Remove the `inject_global_css()` call.
   - Change `page_header(...)` back to `st.title(...)` or `st.header(...)` with the original text.
4. In `components/charts.py`, remove the `from components.ui import plotly_dark_layout` import and revert each `fig.update_layout(**plotly_dark_layout(...))` call back to its original `fig.update_layout(template=_THEME, ...)` form using the original arguments recorded in this plan.
5. In `dashboard.py`, revert steps 4a–4j: remove the `inject_global_css` and `page_header` imports, restore `st.sidebar.title("📈 Stock Dashboard")`, restore `st.sidebar.markdown("### Navigation")`, restore `st.sidebar.page_link(page["path"], label=f"{page['icon']} {page['label']}")`, restore `st.sidebar.markdown("### 🔍 Global Search")`, restore `st.title("📈 Stock Dashboard")` and `st.markdown("##### Live market data...")`, and restore the original `_render_landing()` function.

All original code for each of these is documented verbatim in this plan under each step's "Find:" block, making reversion straightforward.
