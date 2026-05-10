# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All commands should be run from `stock-dashboard/` with the virtual environment activated:

```powershell
# Activate venv (Windows)
.\venv\Scripts\Activate.ps1

# Run the app
streamlit run dashboard.py

# Run analytics tests
pytest analytics/tests/

# Run a single test file
pytest analytics/tests/test_volatility.py
```

## Architecture

**Streamlit multi-page app** with a layered architecture:

```
pages/ (Streamlit UI)  →  data/ (fetchers + calculators)  →  db/ (SQLite)
```

**Entry point:** `dashboard.py` — home page with market snapshot and watchlist overview.

**Pages (`pages/N_*.py`):**
Each page is self-contained — it imports from `data/`, `components/`, and `analytics/` and manages its own state via `st.session_state`. Pages use `@st.cache_data(ttl=3600)` to wrap expensive fetcher calls.

**Data fetchers (`data/`):**

| Module | External Source | Notes |
|--------|----------------|-------|
| `fetcher.py` | Yahoo Finance (yfinance) | Stock info, OHLCV, financials, earnings |
| `calculator.py` | — | Pure functions; takes fetcher output and returns metric dicts |
| `cache.py` | SQLite `db/cache.db` | Persistent cache with 1h TTL for metrics, midnight expiry for hedge funds |
| `news_fetcher.py` | Massive.com API | Includes paywall detection via BeautifulSoup |
| `hedge_fund_fetcher.py` | SEC EDGAR (edgartools) | 13F filing analysis; cached daily |
| `reddit_fetcher.py` | Reddit JSON API | Fetches r/WallStreetBets posts; deduplicates by `post_id` |
| `wsb_sentiment.py` | Google Gemini API | LLM sentiment on Reddit posts; results stored in `db/wsb.db` |
| `webull_positions.py` | Webull Official OpenAPI | APP_KEY + APP_SECRET auth; positions + balance via `webull-python-sdk-trade` |

**Analytics module (`analytics/`):** Options analysis — IV rank/skew, Greeks surfaces, max pain, gamma exposure. Has its own pytest suite in `analytics/tests/` with synthetic fixtures in `conftest.py`.

**Databases (`db/`):**
- `cache.db` — metrics cache + hedge fund cache (auto-created by `cache.py`)
- `thesis.db` — investment theses, watchlist, thesis snapshots (schema: `db/schema.sql`)
- `wsb.db` — Reddit posts + Gemini sentiment scores (schema: `db/wsb_schema.sql`)

## Environment Variables

Copy `.env.example` to `.env`. Required variables:

```env
MASSIVE_API_KEY=       # Massive.com news API (required for news page)
REDDIT_USERNAME=       # Used in Reddit User-Agent header
WEBULL_APP_KEY=        # Webull Official OpenAPI key (from developer.webull.com)
WEBULL_APP_SECRET=     # Webull Official OpenAPI secret
WEBULL_ACCOUNT_ID=     # Webull brokerage account number
WEBULL_REGION_ID=      # "us" or "hk" (defaults to "us")
```

Webull API keys start in "Pending" status and must be authorized via the Webull mobile app (Profile → Settings → API Management). Keys are valid for 15 days; a local `webull_token_info.json` file tracks the activation timestamp for expiry warnings.

## Key Patterns

- **Caching:** Two-layer — `@st.cache_data(ttl=3600)` for in-session memoization; SQLite for cross-session persistence. Don't cache in both layers for the same data path.
- **Metric calculation:** Always pass raw fetcher output to `calculator.py` functions; never compute ratios inline in pages.
- **New pages:** Follow `pages/N_*.py` naming; import shared components from `components/` rather than duplicating chart/card code.
- **SQLite access:** Use the helpers in `cache.py` and `thesis_form.py` rather than opening raw connections in pages.

## Use of AI for analysis
Make sure whenever we are attempting to use AI for any type of analysis we have to implement it using the CLI version I do not have access to any API keys for gemini or claude so use the CLI on the pro tier.