# Planner Skills — Stock Dashboard

Reference for the `codebase-planner` agent. Contains architecture decisions, design patterns, trade-offs, and decision frameworks for writing implementation plans for this project. Read `implementor-skills.md` for exact code patterns.

---

## Project Summary

**Streamlit multi-page stock dashboard** with 9 pages, Python 3.11+, SQLite persistence, and Gemini CLI for AI analysis. No React, no REST API, no Docker — everything runs locally via `streamlit run dashboard.py`.

```
stock-dashboard/
├── dashboard.py              # Home page
├── pages/N_*.py              # 9 pages (numbered 1–9)
├── data/                     # Data fetchers, analyzers, cache modules
├── analytics/                # Options math library (with pytest suite)
├── components/               # Reusable Streamlit UI pieces
├── db/                       # SQLite databases + schema files
```

---

## Architecture Principles

### 1. Pages are UI-only

Pages (`pages/N_*.py`) must not contain business logic. All data fetching, calculations, and AI calls belong in `data/` modules. Pages only:
- Call data module functions
- Wrap expensive calls in `@st.cache_data`
- Manage `st.session_state`
- Render Streamlit widgets

### 2. Data modules own the data contract

Each `data/` module owns one domain:
| Module | Owns |
|--------|------|
| `fetcher.py` | yfinance calls |
| `calculator.py` | Pure metric math |
| `cache.py` | `cache.db` (metrics + hedge funds) |
| `portfolio_cache.py` | `portfolio.db` (news + options analysis) |
| `news_fetcher.py` | Massive.com API + article scraping |
| `news_analyzer.py` | Gemini Flash news sentiment |
| `reddit_fetcher.py` | Reddit JSON API |
| `wsb_sentiment.py` | Gemini Flash Reddit sentiment |
| `options_agent.py` | Gemini Pro options analysis |
| `hedge_fund_fetcher.py` | SEC EDGAR 13F filings |
| `webull_positions.py` | Webull OpenAPI positions |
| `gemini_tracker.py` | Daily Gemini usage counting |

**Do not create a new module unless the domain is genuinely new.** Extend existing modules instead.

### 3. Two-layer caching — never double-cache

- **In-memory layer:** `@st.cache_data(ttl=N)` in pages. Fast, session-scoped. Use for yfinance and other external HTTP calls.
- **SQLite layer:** Persistent, cross-session. Use for AI analysis results, hedge fund filings, Reddit posts.
- **Rule:** Never put `@st.cache_data` in a `data/` module that writes to SQLite. Pick one layer.

### 4. All AI analysis via Gemini CLI subprocess

No Python SDK. No API keys. Only:
```
gemini.cmd -p ""                         # Flash (1000/day)
gemini.cmd -m gemini-2.5-pro -p ""      # Pro (50/day)
```
Always pass the prompt via stdin. Track calls with `gemini_tracker.record_call()`. Parse JSON from stdout with `re.search(r"\{.*\}", raw, re.DOTALL)`.

### 5. One SQLite database per concern

| DB | Contents |
|----|----------|
| `cache.db` | Metric cache (1h TTL), hedge fund filings (midnight expiry) |
| `thesis.db` | Investment theses, watchlist, thesis snapshots |
| `wsb.db` | Reddit posts + sentiment scores |
| `portfolio.db` | News analysis, options analysis |

When adding a new AI feature: add to `portfolio.db` if it analyses portfolio positions, otherwise create a new db with its own schema file.

---

## Decision Framework

### When to add a new page

✅ Add a new page when:
- The feature is a distinct user workflow with its own sidebar and layout
- It will have its own data domain that doesn't overlap existing pages

❌ Don't add a page when:
- The feature is a new section within an existing page (add a `st.tab` or expander)
- The feature is shared across multiple pages (add to `components/`)

**New page naming:** `pages/{N+1}_pagename.py` where N is the current highest numbered page.

### When to create a new data module vs. extend existing

Create a NEW `data/` module when:
- Talking to a new external service that doesn't fit any existing module
- Adding a new AI analysis with its own Gemini prompt and output schema
- Adding a new SQLite DB domain

Extend an EXISTING module when:
- Adding another function to an existing external service (e.g., new yfinance endpoint → `fetcher.py`)
- Adding a TTL check or staleness helper for existing cached data
- Adding a new metric calculation → `calculator.py`

### When to use Flash vs. Pro

| Use Flash | Use Pro |
|-----------|---------|
| News sentiment | Options chain analysis |
| Reddit post sentiment | Any task requiring multi-step reasoning over structured data |
| Simple classification tasks | Deep financial analysis |
| Summary generation | Tasks where accuracy >> cost |

**Daily limits:** Flash = 1000, Pro = 50. Budget Pro calls carefully. Always check `gemini_tracker.get_today_stats()` before gating Pro calls in UI.

### When to add to `portfolio.db` vs. `cache.db`

Add to **`portfolio.db`** when:
- The data is an AI analysis result tied to a specific position or ticker
- You need historical records (time-series of sentiment, options analyses)
- The data has article-level deduplication (like news seen URLs)

Add to **`cache.db`** when:
- The data is a cache of external API data (not AI-generated)
- The cache has a simple TTL or date-based expiry
- The data is hedge fund or metric related

### When to add a new `db/*.sql` schema file

Always create a separate `.sql` file for a new table. Never define DDL inline in Python except for the simplest single-table init. Schema files:
- Live in `db/` alongside the databases
- Use `CREATE TABLE IF NOT EXISTS` (idempotent)
- Include `CREATE INDEX IF NOT EXISTS` for any column used in `WHERE` clauses
- Are read by the owning `data/` module's `init_db()` function

---

## Page Organization Pattern

Every page follows the same section order:
1. Imports
2. `st.set_page_config(page_title=..., layout="wide")`
3. `render_gemini_usage_bar()`
4. Module-level constants (`_CAPS_CASE`)
5. Helper functions (`_private_helper()`)
6. Cached data functions (`@st.cache_data`)
7. Render functions (`_render_sidebar()`, `_render_section()`)
8. Page entry (calls render functions at module scope)

Plans should specify which section each new function goes into.

---

## Existing Page Inventory (what each page does)

| # | File | Title | Key Data | DB |
|---|------|-------|----------|----|
| 1 | `1_metrics.py` | Metrics | yfinance via fetcher + calculator | cache.db (1h) |
| 2 | `2_thesis.py` | Thesis | fetcher + calculator + thesis_form | thesis.db |
| 3 | `3_watchlist.py` | Watchlist | fetcher | cache.db (1h) |
| 4 | `4_news.py` | News | news_fetcher (Massive.com) | None (display only) |
| 5 | `5_hedge_funds.py` | Hedge Funds | hedge_fund_fetcher (SEC EDGAR) | cache.db |
| 6 | `6_option_chains.py` | Option Chains | yfinance options + options_agent | portfolio.db |
| 7 | `7_positions.py` | Positions | webull_positions | None |
| 8 | `8_reddit.py` | Reddit WSB | reddit_fetcher + wsb_sentiment | wsb.db |
| 9 | `9_portfolio.py` | Portfolio | All of the above combined | portfolio.db |

---

## Key External Services & Constraints

| Service | Module | Auth | Rate Limit | Notes |
|---------|--------|------|-----------|-------|
| Yahoo Finance (yfinance) | fetcher.py | None | ~2000/day | Free |
| Massive.com News | news_fetcher.py | `MASSIVE_API_KEY` in .env | Varies | 15min cache |
| Reddit JSON API | reddit_fetcher.py | None | ~60/min | r/WallStreetBets |
| SEC EDGAR (edgartools) | hedge_fund_fetcher.py | Email identity string | ~10/min | 13F filings |
| Webull OpenAPI | webull_positions.py | APP_KEY + APP_SECRET + ACCOUNT_ID | Varies | 15-day key expiry |
| Gemini CLI (Flash) | news_analyzer, wsb_sentiment | Local `gemini.cmd` | 1000/day | Free tier |
| Gemini CLI (Pro) | options_agent | Local `gemini.cmd` | 50/day | Pro tier |

When planning features that use multiple external services, always note which service and whether it requires a new env variable.

---

## Analytics Module (`analytics/`)

The `analytics/` module is a standalone options math library with its own pytest suite. It is imported by pages (particularly `6_option_chains.py`) for chain enrichment and positioning analysis. When modifying analytics:
- All changes must pass `pytest analytics/tests/`
- The module uses synthetic fixtures in `analytics/tests/conftest.py` — don't assume live data
- Do not add Streamlit imports to `analytics/`

---

## Common Planning Mistakes to Avoid

1. **Don't propose SDK-based Gemini calls.** Only `subprocess` + `gemini.cmd` is available.
2. **Don't propose opening raw `sqlite3.connect()` in page files.** Always route through `data/` module helpers.
3. **Don't add `@st.cache_data` inside `data/` modules that write to SQLite.** Pick one caching layer.
4. **Don't propose `gemini.cmd --agent`.** The `--agent` flag is interactive-only and returns empty stdout in headless mode. Use `-p ""` always.
5. **Don't hardcode db paths relative to cwd.** Use `os.path.join(os.path.dirname(__file__), "..", "db", "file.db")`.
6. **Don't duplicate metric calculations in pages.** All ratio math goes through `calculator.py`.
7. **Don't propose REST API endpoints.** This is a pure Streamlit app — no FastAPI, Flask, or separate backend process.
8. **Don't add new pages numbered out of sequence.** Check the current highest page number and increment by 1.

---

## Plan File Format

Plans should be written to `plan_{feature_name}.md` at the project root (`C:\Users\ethan\Downloads\Stock Dashboard\`), not inside `stock-dashboard/`.

Each plan must include:

```markdown
# Plan: Feature Name

## Overview
2-3 sentences describing what this adds and why.

## Files to Create
- `stock-dashboard/path/to/new_file.py` — what it does

## Files to Modify
- `stock-dashboard/path/to/existing.py` — what changes and why

## Database Changes
- New table `table_name` in `db/which.db` — schema summary
- New schema file `db/schema.sql` if needed

## Step-by-Step Implementation
### Step 1: Create schema file
[exact DDL to write]

### Step 2: Create data module
[exact function signatures and logic]

### Step N: Wire into page
[exact imports to add, section to modify, where in the page flow]

## Testing
[how to verify the feature works in the running app]
```

Every step must be unambiguous — the spec-implementer (Haiku model) executes it without any prior context about the codebase.
