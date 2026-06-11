# Plan: Parallelize "Analyze All Positions" Buttons on Portfolio Page

## Overview

The portfolio page (`9_portfolio.py`) has two "Analyze All Positions" buttons — one for news analysis (lines 698–715) and one for options analysis (lines 988–1024 inside `_options_analysis_ui`). Both currently iterate tickers sequentially with a `time.sleep(2)` delay between calls, making them slow for portfolios with many positions. This plan replaces both sequential loops with `concurrent.futures.ThreadPoolExecutor(max_workers=3)`, fanning out the heavy Gemini/yfinance work across 3 parallel workers while keeping all Streamlit UI calls (progress bar updates) on the main thread.

## Files Involved

| File | Status | Change |
|------|--------|--------|
| `stock-dashboard/pages/9_portfolio.py` | **Modified** | Add `ThreadPoolExecutor, as_completed` import; replace news Analyze All loop (lines 698–715); replace options Analyze All loop (lines 988–1024) with parallel worker pattern |

No new files are created. No database changes are required.

## Prerequisites & Dependencies

No new pip packages are needed. `concurrent.futures` is part of the Python 3 standard library and is already available in the project's venv.

## Step-by-Step Implementation

---

### Step 1: Add `concurrent.futures` import to `9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Line 1 is `import time`. Insert the new import directly after line 1, on line 2, so it reads:

```
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
```

**Exact action:** After the text `import time` on line 1, insert a new line containing:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed
```

The top of the file must then look exactly like this (lines 1–10 after the change):

```python
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import groupby

import numpy as np
import streamlit as st
import pandas as pd
import yfinance as yf
from datetime import datetime
from scipy.stats import norm
```

**Why:** `ThreadPoolExecutor` and `as_completed` are the only new symbols required by this plan. They live in the stdlib `concurrent.futures` module which does not need to be installed.

---

### Step 2: Replace the news "Analyze All Positions" sequential loop

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** The block starting at (original) line 698, inside the top-level page body (not inside any function). This is the `if analyze_all:` block that currently runs a `for idx, ticker in enumerate(tickers):` loop and calls `time.sleep(2)` at line 713.

**Find this exact text to replace** (original lines 698–715):

```python
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
```

**Replace it with this exact text:**

```python
# Analyze all positions
if analyze_all:
    progress = st.progress(0, text="Starting analysis…")
    completed = 0

    def _analyze_ticker(ticker):
        return ticker, _load_or_analyze(ticker)

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_analyze_ticker, t): t for t in tickers}
        for future in as_completed(futures):
            ticker, entry = future.result()
            st.session_state.news_results[ticker] = entry
            completed += 1
            label = "Gemini" if not entry.get("from_db") else "cache"
            progress.progress(completed / len(tickers), text=f"Done ({label}): {ticker}")
    progress.empty()
```

**Why:** The sequential loop called `_load_or_analyze` one at a time with a 2-second sleep between each Gemini call. The thread pool fans out all tickers simultaneously (capped at 3 concurrent workers) so Gemini calls overlap instead of stacking. All `st.progress` updates remain on the main thread inside the `as_completed` loop, which is safe. Each worker writes to a unique `ticker` key in `st.session_state.news_results`, so there are no key collisions between threads. The `time.sleep(2)` is removed entirely.

---

### Step 3: Replace the options "Analyze All Positions" sequential loop

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Inside the `_options_analysis_ui` function (decorated with `@st.fragment`). This is the `if opt_analyze_all:` block starting at (original) line 988 and ending at line 1024 (`oa_progress.empty()`).

**Find this exact text to replace** (original lines 988–1024):

```python
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
```

**Replace it with this exact text** (indentation is 4 spaces throughout, matching the surrounding function body):

```python
    if opt_analyze_all:
        oa_progress = st.progress(0, text="Starting options analysis…")
        oa_completed = 0

        def _analyze_options_ticker(t):
            """Worker: fetch price/chain, calc Greeks, run Gemini. Returns (sess_key, entry_or_None)."""
            from datetime import datetime as _dt_w, timezone as _tz_w
            try:
                t_price, t_expirations = _fetch_price_and_expirations(t)
            except Exception:
                return _opt_session_key(t, "", "put"), None
            if not t_expirations or t_price is None:
                return _opt_session_key(t, "", "put"), None
            nearest_expiry = t_expirations[0]
            sess_key = _opt_session_key(t, nearest_expiry, "put")
            cached_db = get_latest_options_analysis(t, nearest_expiry, "put")
            if cached_db is not None and is_options_analysis_fresh(cached_db.get("analyzed_at", "")):
                return sess_key, {**cached_db, "from_db": True}
            try:
                t_calls, t_puts = _fetch_chain(t, nearest_expiry)
                t_calls_display = _build_options_display_df(t_calls, t_price, nearest_expiry, "call")
                t_puts_display  = _build_options_display_df(t_puts,  t_price, nearest_expiry, "put")
            except Exception:
                return sess_key, None
            result = run_options_analysis(t, t_price, nearest_expiry, "put", t_calls, t_puts, t_calls_display, t_puts_display)
            if result and "_error" not in result:
                save_options_analysis(t, nearest_expiry, "put", t_price, result)
            analyzed_at = _dt_w.now(_tz_w.utc).strftime("%Y-%m-%dT%H:%M:%S")
            entry = {**result, "analyzed_at": analyzed_at, "from_db": False}
            return sess_key, entry

        with ThreadPoolExecutor(max_workers=3) as oa_executor:
            oa_futures = {oa_executor.submit(_analyze_options_ticker, t): t for t in tickers}
            for oa_future in as_completed(oa_futures):
                t = oa_futures[oa_future]
                sess_key, entry = oa_future.result()
                oa_completed += 1
                if entry is None:
                    oa_progress.progress(oa_completed / len(tickers), text=f"Skipped {t}")
                else:
                    st.session_state.options_results[sess_key] = entry
                    label = "cache" if entry.get("from_db") else "Gemini"
                    oa_progress.progress(oa_completed / len(tickers), text=f"Done ({label}): {t}")
        oa_progress.empty()
```

**Why:** The original loop called `_fetch_price_and_expirations`, `_fetch_chain`, `_build_options_display_df`, `run_options_analysis`, and `save_options_analysis` sequentially with a 2-second sleep between tickers. All of these are pure computation or I/O — none of them call Streamlit widgets — so they are safe to run in worker threads. The worker function `_analyze_options_ticker` encapsulates the entire per-ticker logic and returns `(sess_key, entry_or_None)`. The main thread then writes the result to `st.session_state.options_results` and updates the progress bar. Each ticker maps to a unique `sess_key` (`"{ticker}|{expiry}|put"`), so there are no key collisions. The `time.sleep(2)` is removed.

---

## Testing Checklist

After applying both changes, start the app (`streamlit run dashboard.py` from `stock-dashboard/` with venv active) and navigate to the **Portfolio** page.

1. **Import check:** The page must load without any `ImportError` or `NameError`. If it errors on load, confirm the `from concurrent.futures import ThreadPoolExecutor, as_completed` line was inserted on line 2 (after `import time`) and not duplicated.

2. **News single-ticker analysis unchanged:** Select a ticker from the "Analyze news for" dropdown and click "Run News Analysis for {ticker}". Confirm it still loads and displays the sentiment result. This path must not have been modified.

3. **News Analyze All — small portfolio:** With 2–3 tickers in the portfolio, click "Analyze All Positions" (news section). Confirm:
   - A progress bar appears immediately with text "Starting analysis…"
   - The bar fills as each ticker completes (not all at once at the end)
   - Progress text shows "Done (Gemini): TICKER" or "Done (cache): TICKER" for each ticker
   - The progress bar disappears after all tickers complete
   - "Analysis Results" expanders appear below for all tickers

4. **News Analyze All — cache path:** Click "Analyze All Positions" a second time immediately after the first run. All tickers should show "Done (cache): TICKER" in the progress label (no fresh Gemini calls since articles haven't changed).

5. **Options single-ticker analysis unchanged:** Select a ticker from the "Analyze options for" dropdown, pick an expiry, and click "▶ Run Gemini Analysis". Confirm it still works. This path must not have been modified.

6. **Options Analyze All — small portfolio:** Click "Analyze All Positions" (options section). Confirm:
   - A progress bar appears with text "Starting options analysis…"
   - The bar fills as each ticker completes in any order (parallel — order is non-deterministic)
   - Progress text shows "Done (Gemini): TICKER", "Done (cache): TICKER", or "Skipped TICKER" for each
   - The progress bar disappears after all tickers complete
   - "Options Analysis Results" expanders appear below for all tickers that succeeded

7. **Options Analyze All — cache path:** Click "Analyze All Positions" (options) a second time within 4 hours. All tickers should show "Done (cache): TICKER" (the 4-hour TTL check in `is_options_analysis_fresh` should pass).

8. **Tickers with no options:** If any portfolio ticker has no listed options (e.g., a foreign ADR), confirm it shows "Skipped TICKER" in the progress bar and does not crash.

9. **"Clear All Results" and "Clear Options Results" buttons:** After an Analyze All run, click both clear buttons. Confirm results disappear and the page reruns cleanly.

10. **No regression on other page sections:** Scroll through the full page — Market Pulse, Hedge Fund Overlap — and confirm they still render correctly. These sections were not touched.

## Rollback Plan

If the changes cause crashes or incorrect behavior:

1. Open `stock-dashboard/pages/9_portfolio.py`.
2. Remove the line `from concurrent.futures import ThreadPoolExecutor, as_completed` (line 2 after the change).
3. Replace the news `if analyze_all:` block (Step 2 change) with the original sequential version:

```python
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
```

4. Replace the options `if opt_analyze_all:` block (Step 3 change) with the original sequential version:

```python
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
```

5. Restart the Streamlit app and confirm the page loads correctly.

No database changes were made, so no DB rollback is needed.
