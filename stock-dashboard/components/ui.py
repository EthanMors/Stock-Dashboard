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
/* The expand-sidebar arrow lives inside stToolbar — keep it visible */
[data-testid="stExpandSidebarButton"] { visibility: visible !important; }
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
# Sidebar navigation component
# ---------------------------------------------------------------------------

_NAV_PAGES = [
    ("Home",               "dashboard.py"),
    ("Metrics",            "pages/1_metrics.py"),
    ("Thesis",             "pages/2_thesis.py"),
    ("Watchlist",          "pages/3_watchlist.py"),
    ("News",               "pages/4_news.py"),
    ("Hedge Funds",        "pages/5_hedge_funds.py"),
    ("Option Chains",      "pages/6_option_chains.py"),
    ("Positions",          "pages/7_positions.py"),
    ("Social",             "pages/8_social.py"),
    ("Portfolio",          "pages/9_portfolio.py"),
    ("Technical Analysis", "pages/10_technical_analysis.py"),
    ("Backtest",           "pages/11_backtest.py"),
]


def render_sidebar_nav() -> None:
    """Render the brand block and page navigation links in the sidebar.

    The global CSS hides Streamlit's built-in multipage nav (stSidebarNav),
    so every page that calls inject_global_css() should also call this to
    keep the sidebar navigable. Call it once, right after inject_global_css().
    """
    st.sidebar.markdown(
        '<div class="sidebar-brand">'
        '<h1>Stock Dashboard</h1>'
        '<p>Live data · AI analysis · Portfolio</p>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.sidebar.markdown(
        '<p class="section-header" style="padding-left:0.25rem;">Navigation</p>',
        unsafe_allow_html=True,
    )
    for label, path in _NAV_PAGES:
        st.sidebar.page_link(path, label=label)


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
