# Plan: Hedge Fund Smart Cache + Portfolio Overlap

## 1. Task Summary

Replace the current midnight-expiry monolithic hedge fund cache (one blob in `hedge_fund_cache` table) with a quarter-aware, per-fund persistent store. The new system tracks one DB row per fund, polls EDGAR only during the 45-day window following each quarter end, and adjusts its polling frequency based on where in that window today falls. After the DB layer is upgraded, the portfolio page (`pages/9_portfolio.py`) gains a new "Hedge Fund Overlap" section at the bottom that shows which concentrated funds hold any tickers currently in the user's Webull positions. No existing behavior on `pages/5_hedge_funds.py` is changed.

When fully executed:
- `db/cache.db` will contain two new tables: `hedge_fund_filings` and `hedge_fund_check_log`.
- `data/cache.py` will have five new helper functions operating on those tables.
- `data/hedge_fund_fetcher.py` will have five new functions (`_is_filing_window`, `_check_interval_hours`, `_poll_edgar_rss`, `_fetch_fund_from_filing`, `refresh_hedge_fund_db`, `get_all_funds_from_db`) and `get_concentrated_funds` will be updated to use the new DB-first approach.
- `pages/9_portfolio.py` will render a "Hedge Fund Overlap" section after the Options Analysis section at the bottom of the page.

---

## 2. Files Involved

| File | Status | Changes |
|---|---|---|
| `stock-dashboard/data/cache.py` | Modified | Add 2 new tables in `_get_connection()`, add 5 new helper functions |
| `stock-dashboard/data/hedge_fund_fetcher.py` | Modified | Add 6 new functions; update `get_concentrated_funds()` |
| `stock-dashboard/pages/9_portfolio.py` | Modified | Add 3 new helper functions; add rendering call at bottom of main flow |
| `stock-dashboard/pages/5_hedge_funds.py` | Read-only | No changes — verified as-is |

---

## 3. Prerequisites & Dependencies

No new pip packages are required. All dependencies (`sqlite3`, `json`, `datetime`, `requests` is NOT used — `edgartools` is used instead) are already present. The `edgartools` package (`edgar`) is already installed and used in `hedge_fund_fetcher.py`. The `requests` library is standard Python. No environment variables need to change. No manual DB migration is needed — the new tables are created automatically by `_get_connection()` on first connection after the code changes.

---

## 4. Step-by-Step Implementation

---

### Step 1: Add two new tables to `_get_connection()` in `data/cache.py`

**File:** `stock-dashboard/data/cache.py`

**Location:** Inside the `_get_connection()` function, immediately after the closing `""")` of the `hedge_fund_cache` table's `conn.execute(...)` call (after line 27), and before `conn.commit()` on line 28.

**Action:** Insert the following two `conn.execute(...)` blocks between the existing `hedge_fund_cache` CREATE TABLE statement and the `conn.commit()` call.

The current code block at lines 22–28 looks like this:
```python
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hedge_fund_cache (
            cache_key   TEXT PRIMARY KEY,
            data_json   TEXT NOT NULL,
            cached_date TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn
```

Replace that block with:
```python
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hedge_fund_cache (
            cache_key   TEXT PRIMARY KEY,
            data_json   TEXT NOT NULL,
            cached_date TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hedge_fund_filings (
            cik              TEXT PRIMARY KEY,
            name             TEXT NOT NULL,
            accession_number TEXT NOT NULL,
            report_period    TEXT NOT NULL,
            filing_date      TEXT NOT NULL,
            total_holdings   INTEGER NOT NULL,
            total_value      REAL NOT NULL,
            holdings_json    TEXT NOT NULL,
            fetched_at       TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hedge_fund_check_log (
            id           INTEGER PRIMARY KEY CHECK (id = 1),
            last_checked TEXT NOT NULL,
            last_count   INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    return conn
```

**Why:** The two new tables persist per-fund filing data and the last-checked timestamp respectively. They must be created in `_get_connection()` so they are automatically available on first DB access, consistent with the pattern used for `metric_cache` and `hedge_fund_cache`.

---

### Step 2: Add five new helper functions to `data/cache.py`

**File:** `stock-dashboard/data/cache.py`

**Location:** After the closing of `load_hedge_fund_cache()` (currently ending at line 116, after the final `return None`), append the following five functions at the end of the file.

**Action:** Add the following code block at the very end of `data/cache.py`, after line 116:

```python


def get_all_hedge_fund_filings() -> list:
    """Return all rows from hedge_fund_filings as a list of FundProfile-shaped dicts.

    Each dict has keys: cik, name, accession_number, report_period, filing_date,
    total_holdings, total_value, holdings (list), fetched_at.
    Returns an empty list if the table is empty or the DB has not been created yet.
    """
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT cik, name, accession_number, report_period, filing_date,
                   total_holdings, total_value, holdings_json, fetched_at
            FROM hedge_fund_filings
            """
        ).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        (cik, name, accession_number, report_period, filing_date,
         total_holdings, total_value, holdings_json, fetched_at) = row
        try:
            holdings = json.loads(holdings_json)
        except (json.JSONDecodeError, TypeError):
            holdings = []
        result.append({
            "cik": cik,
            "name": name,
            "accession_number": accession_number,
            "report_period": report_period,
            "filing_date": filing_date,
            "total_holdings": total_holdings,
            "total_value": total_value,
            "holdings": holdings,
            "fetched_at": fetched_at,
        })
    return result


def upsert_hedge_fund_filing(fund: dict, accession_number: str) -> None:
    """Insert or update one fund row in hedge_fund_filings.

    `fund` must be a FundProfile-shaped dict with keys: name, cik, report_period,
    filing_date, total_holdings, total_value, holdings.
    `accession_number` is the filing's accession number string (e.g. '0001234567-24-000001').
    """
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO hedge_fund_filings
                (cik, name, accession_number, report_period, filing_date,
                 total_holdings, total_value, holdings_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cik) DO UPDATE SET
                name             = excluded.name,
                accession_number = excluded.accession_number,
                report_period    = excluded.report_period,
                filing_date      = excluded.filing_date,
                total_holdings   = excluded.total_holdings,
                total_value      = excluded.total_value,
                holdings_json    = excluded.holdings_json,
                fetched_at       = excluded.fetched_at
            """,
            (
                str(fund.get("cik", "")),
                str(fund.get("name", "")),
                str(accession_number),
                str(fund.get("report_period", "")),
                str(fund.get("filing_date", "")),
                int(fund.get("total_holdings", 0)),
                float(fund.get("total_value", 0.0)),
                json.dumps(fund.get("holdings", [])),
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_stored_accession_numbers() -> dict:
    """Return {cik: accession_number} for every row in hedge_fund_filings.

    Used to detect which funds have new filings without re-downloading everything.
    Returns an empty dict if the table is empty.
    """
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT cik, accession_number FROM hedge_fund_filings"
        ).fetchall()
    finally:
        conn.close()
    return {row[0]: row[1] for row in rows}


def get_last_check_time() -> Optional[datetime]:
    """Return the datetime of the last EDGAR poll, or None if never checked.

    Reads the single row (id=1) from hedge_fund_check_log.
    Returns None if the table is empty.
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT last_checked FROM hedge_fund_check_log WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except (ValueError, TypeError):
        return None


def save_last_check_time(count: int) -> None:
    """Upsert the hedge_fund_check_log row (id=1) with the current UTC time and count.

    `count` is the number of funds updated in the most recent poll.
    """
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO hedge_fund_check_log (id, last_checked, last_count)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_checked = excluded.last_checked,
                last_count   = excluded.last_count
            """,
            (datetime.utcnow().isoformat(), int(count)),
        )
        conn.commit()
    finally:
        conn.close()
```

**Why:** These five functions provide the complete data-access layer for the new smart cache. They follow the exact same pattern as the existing `save_to_cache` / `load_from_cache` functions above them: open connection via `_get_connection()`, execute SQL in a try/finally that closes the connection, return Python-native types. `Optional[datetime]` is already imported at line 5 (`from typing import Optional`). `json` and `datetime` are already imported at lines 2 and 4.

---

### Step 3: Add new import to `data/hedge_fund_fetcher.py`

**File:** `stock-dashboard/data/hedge_fund_fetcher.py`

**Location:** Line 9, which currently reads:
```python
from data.cache import load_hedge_fund_cache, save_hedge_fund_cache
```

**Action:** Replace that single line with the following:
```python
from data.cache import (
    load_hedge_fund_cache,
    save_hedge_fund_cache,
    get_all_hedge_fund_filings,
    upsert_hedge_fund_filing,
    get_stored_accession_numbers,
    get_last_check_time,
    save_last_check_time,
)
```

**Why:** The new functions added to `data/cache.py` in Step 2 must be importable in `hedge_fund_fetcher.py` before the new orchestration functions defined in Steps 4–9 can call them.

---

### Step 4: Add `_is_filing_window()` to `data/hedge_fund_fetcher.py`

**File:** `stock-dashboard/data/hedge_fund_fetcher.py`

**Location:** After the `_apply_unit_correction()` function (which ends at line 129 with `return funds`), and before the `@st.cache_data(ttl=86400)` decorator on line 132. Insert between lines 129 and 132.

**Action:** Insert the following function:

```python

def _is_filing_window() -> bool:
    """Return True if today falls within 0–45 days after a quarter-end date.

    Quarter ends used: March 31, June 30, September 30, December 31.
    The 45-day window starts ON the quarter-end date (day 0) and includes the
    45th day after it. Both the current year and the prior year are checked so
    that the December 31 window carries into the following January and February.
    """
    today = datetime.date.today()
    year = today.year
    quarter_ends = [
        datetime.date(year - 1, 12, 31),
        datetime.date(year, 3, 31),
        datetime.date(year, 6, 30),
        datetime.date(year, 9, 30),
        datetime.date(year, 12, 31),
    ]
    for qe in quarter_ends:
        diff = (today - qe).days
        if 0 <= diff <= 45:
            return True
    return False

```

**Why:** This function determines whether the SEC 13F-HR filing window is currently active. Funds have 45 days after each quarter end to file. Polling outside this window is wasteful and would never find new filings, so `refresh_hedge_fund_db()` exits early when this returns False.

---

### Step 5: Add `_check_interval_hours()` to `data/hedge_fund_fetcher.py`

**File:** `stock-dashboard/data/hedge_fund_fetcher.py`

**Location:** Immediately after `_is_filing_window()` (inserted in Step 4), before the `@st.cache_data(ttl=86400)` decorator.

**Action:** Insert the following function:

```python

def _check_interval_hours() -> int:
    """Return how many hours to wait between EDGAR polls based on filing window position.

    - Not in window           → 999999 (effectively never)
    - Days 1–30 of window     → 72 hours (check every 3 days; filings trickle in)
    - Days 31–45 of window    → 24 hours (final stretch; more filings arrive daily)
    """
    today = datetime.date.today()
    year = today.year
    quarter_ends = [
        datetime.date(year - 1, 12, 31),
        datetime.date(year, 3, 31),
        datetime.date(year, 6, 30),
        datetime.date(year, 9, 30),
        datetime.date(year, 12, 31),
    ]
    min_diff = None
    for qe in quarter_ends:
        diff = (today - qe).days
        if 0 <= diff <= 45:
            if min_diff is None or diff < min_diff:
                min_diff = diff
    if min_diff is None:
        return 999999
    if min_diff <= 30:
        return 72
    return 24

```

**Why:** Polling frequency should increase as the filing deadline approaches. Days 1–30 see fewer new 13Fs; days 31–45 see a rush of last-minute filings. The returned integer is consumed by `refresh_hedge_fund_db()` to decide whether the last check was recent enough to skip this poll.

---

### Step 6: Add `_fetch_fund_from_filing()` to `data/hedge_fund_fetcher.py`

**File:** `stock-dashboard/data/hedge_fund_fetcher.py`

**Location:** Immediately after `_check_interval_hours()` (inserted in Step 5), before the `@st.cache_data(ttl=86400)` decorator.

**Action:** Insert the following function:

```python

def _fetch_fund_from_filing(filing, cik: str) -> Optional[FundProfile]:
    """Download and parse one 13F-HR filing into a FundProfile dict.

    Calls filing.obj() to download the full report — this is the expensive network
    call. Returns None on any failure or if the fund has >= _CONCENTRATED_THRESHOLD
    positions (keeping only concentrated funds consistent with existing behavior).

    Args:
        filing: An edgartools filing object (already has .cik, .company, .filing_date).
        cik: The CIK string extracted from the filing before calling this function.

    Returns:
        A FundProfile TypedDict dict, or None if the fund is not concentrated or
        parsing fails.
    """
    try:
        report = filing.obj()
        if report is None:
            return None

        has_table = getattr(report, "has_infotable", True)
        if not has_table:
            return None

        n_positions = getattr(report, "total_holdings", None)
        if n_positions is None or n_positions >= _CONCENTRATED_THRESHOLD:
            return None

        total_val = _normalize_value(getattr(report, "total_value", None))
        holdings = _parse_holdings(report)
        name = (
            getattr(report, "management_company_name", None)
            or getattr(filing, "company", None)
            or cik
        )
        report_period = str(getattr(report, "report_period", "") or "")
        filing_date = str(
            getattr(report, "filing_date", "")
            or getattr(filing, "filing_date", "")
            or ""
        )

        return FundProfile(
            name=str(name),
            cik=cik,
            report_period=report_period,
            filing_date=filing_date,
            total_holdings=int(n_positions),
            total_value=total_val,
            holdings=holdings,
        )
    except Exception:
        return None

```

**Why:** This extracts the inner parsing logic from `get_concentrated_funds()` into a standalone function so it can be called from `refresh_hedge_fund_db()` for individual new-or-updated filings without duplicating code. It mirrors the existing `try/except: continue` pattern in the existing loop.

---

### Step 7: Add `refresh_hedge_fund_db()` to `data/hedge_fund_fetcher.py`

**File:** `stock-dashboard/data/hedge_fund_fetcher.py`

**Location:** Immediately after `_fetch_fund_from_filing()` (inserted in Step 6), before the `@st.cache_data(ttl=86400)` decorator.

**Action:** Insert the following function:

```python

def refresh_hedge_fund_db() -> int:
    """Smart EDGAR poll: fetch new/updated 13F-HR filings and upsert them into the DB.

    Returns the number of fund rows updated (0 if skipped or nothing new found).

    Algorithm:
    1. If not in the 45-day filing window, return 0 immediately.
    2. If the last poll was within _check_interval_hours() hours, return 0 (too soon).
    3. Fetch the first _MAX_FILINGS_TO_SCAN filings from EDGAR (header-only, no .obj()).
    4. Compare each filing's accession_number against what is stored in the DB.
    5. For each CIK with a new or changed accession_number, download the full report
       via _fetch_fund_from_filing() and upsert it.
    6. Record the poll timestamp and count via save_last_check_time().
    7. Return the count of updated funds.
    """
    if not _is_filing_window():
        return 0

    interval_hours = _check_interval_hours()
    last_check = get_last_check_time()
    if last_check is not None:
        import datetime as _dt_mod
        elapsed = (_dt_mod.datetime.utcnow() - last_check).total_seconds() / 3600
        if elapsed < interval_hours:
            return 0

    try:
        filings_batch = get_filings(form="13F-HR")
    except Exception:
        return 0

    stored = get_stored_accession_numbers()
    seen_ciks: set = set()
    updated_count = 0
    scanned = 0

    for filing in filings_batch:
        if scanned >= _MAX_FILINGS_TO_SCAN:
            break
        scanned += 1

        cik = str(getattr(filing, "cik", "") or "")
        if not cik or cik in seen_ciks:
            continue
        seen_ciks.add(cik)

        # Get accession_number without downloading full report
        accession_number = str(getattr(filing, "accession_number", "") or "")
        if not accession_number:
            continue

        stored_accession = stored.get(cik)
        if stored_accession == accession_number:
            # Already have this exact filing in DB — skip the expensive .obj() call
            continue

        # New or updated filing — download and parse
        fund_profile = _fetch_fund_from_filing(filing, cik)
        if fund_profile is None:
            continue

        upsert_hedge_fund_filing(fund_profile, accession_number)
        updated_count += 1

    save_last_check_time(count=updated_count)
    return updated_count

```

**Why:** This is the orchestration function that replaces the naive nightly cache. It skips the expensive EDGAR scan entirely when outside the filing window or when polled too recently. When it does scan, it avoids calling `filing.obj()` (the slow network call) for filings already in the DB.

---

### Step 8: Add `get_all_funds_from_db()` to `data/hedge_fund_fetcher.py`

**File:** `stock-dashboard/data/hedge_fund_fetcher.py`

**Location:** Immediately after `refresh_hedge_fund_db()` (inserted in Step 7), before the `@st.cache_data(ttl=86400)` decorator.

**Action:** Insert the following function:

```python

def get_all_funds_from_db() -> list:
    """Return all concentrated funds stored in the DB as a list of FundProfile dicts.

    This is the primary read path after the DB has been populated. Calls
    get_all_hedge_fund_filings() from cache.py and returns the list directly.
    Returns an empty list if the DB is empty (which triggers fallback in
    get_concentrated_funds()).
    """
    return get_all_hedge_fund_filings()

```

**Why:** This thin wrapper keeps the boundary clean between the fetcher layer and the cache layer. Pages and the portfolio overlap feature import from `hedge_fund_fetcher`, not directly from `cache`.

---

### Step 9: Update `get_concentrated_funds()` in `data/hedge_fund_fetcher.py`

**File:** `stock-dashboard/data/hedge_fund_fetcher.py`

**Location:** The entire `get_concentrated_funds()` function, currently at lines 132–206.

**Action:** Replace the entire function (from `@st.cache_data(ttl=86400)` through the final `return concentrated` at line 206) with the following:

```python
@st.cache_data(ttl=3600)
def get_concentrated_funds(
    max_scan: int = _MAX_FILINGS_TO_SCAN,
    threshold: int = _CONCENTRATED_THRESHOLD,
) -> list:
    """Return all concentrated funds (< threshold positions) from the smart DB cache.

    Primary flow:
    1. Call refresh_hedge_fund_db() — this is a no-op if outside the filing window
       or polled too recently; otherwise it downloads only new/changed filings.
    2. Read all funds from the DB via get_all_funds_from_db().
    3. If the DB is empty (first-ever run), fall back to a full EDGAR scan to
       populate the DB initially, then return those results.

    The @st.cache_data(ttl=3600) wrapper prevents repeated DB reads within the
    same Streamlit session (reduced from 86400s so changes propagate in ~1 hour).
    """
    # Step 1: Smart poll — only actually fetches when needed
    refresh_hedge_fund_db()

    # Step 2: Read from DB
    funds = get_all_funds_from_db()
    if funds:
        return funds

    # Step 3: DB is empty — do initial full population (legacy scan path)
    try:
        filings_batch = get_filings(form="13F-HR")
    except Exception:
        return []

    seen_ciks: set = set()
    concentrated = []
    scanned = 0

    for filing in filings_batch:
        if scanned >= max_scan:
            break
        scanned += 1

        cik = str(getattr(filing, "cik", "") or "")
        if not cik or cik in seen_ciks:
            continue
        seen_ciks.add(cik)

        accession_number = str(getattr(filing, "accession_number", "") or "")
        fund_profile = _fetch_fund_from_filing(filing, cik)
        if fund_profile is None:
            continue

        concentrated.append(fund_profile)
        if accession_number:
            upsert_hedge_fund_filing(fund_profile, accession_number)

    concentrated = _apply_unit_correction(concentrated)

    if concentrated:
        save_last_check_time(count=len(concentrated))

    return concentrated
```

**Why:** The updated function delegates smart polling to `refresh_hedge_fund_db()` and reads from the persistent DB rather than a midnight-expiring blob. The legacy full-scan path is kept as a fallback for the first-ever run when the DB is empty, ensuring no cold-start regression. The old `load_hedge_fund_cache` / `save_hedge_fund_cache` calls are removed because the new DB tables replace that functionality. The `@st.cache_data` TTL is reduced from 86400 to 3600 so that a user who comes back after a EDGAR poll that ran in a background tab will see updated data within an hour.

---

### Step 10: Add new import to `pages/9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** After the existing import block at the top of the file. The last import in the current file is:

```python
from data.portfolio_cache import (
    get_latest_analysis,
    save_analysis,
    has_new_articles,
    get_sentiment_history,
    save_options_analysis,
    get_latest_options_analysis,
    is_options_analysis_fresh,
)
```

This block currently ends at line 29.

**Action:** After line 29 (after the closing `)` of the `portfolio_cache` import), add the following new import on a new line:

```python
from data.hedge_fund_fetcher import get_all_funds_from_db
```

**Why:** The portfolio page needs to read the hedge fund DB to find overlapping funds. `get_all_funds_from_db()` is the correct read-only accessor defined in Step 8, and importing at the top of the file follows the existing import pattern.

---

### Step 11: Add `_get_portfolio_tickers()` helper to `pages/9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** After the existing `_extract_ticker()` function (currently at lines 63–68), before `_sentiment_color()` at line 71.

The existing `_extract_ticker()` function ends at line 68:
```python
def _extract_ticker(position: dict) -> str:
    for field in _TICKER_FIELD_CANDIDATES:
        val = position.get(field, "")
        if val and isinstance(val, str):
            return val.upper().strip()
    return ""
```

**Action:** Insert the following new function immediately after line 68 (after the closing of `_extract_ticker`), before `_sentiment_color`:

```python

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

```

**Why:** This function is the first step in the hedge fund overlap pipeline. It reuses the already-defined `_extract_ticker()` helper and `_TICKER_FIELD_CANDIDATES` constant, avoiding code duplication and following existing patterns.

---

### Step 12: Add `_find_overlapping_funds()` helper to `pages/9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Immediately after `_get_portfolio_tickers()` (inserted in Step 11), before `_sentiment_color`.

**Action:** Insert the following new function:

```python

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

```

**Why:** This function is the core logic for the portfolio overlap feature. It reads from the DB (via `get_all_funds_from_db()`) and filters funds to only those with tickers matching the current portfolio. Wrapping the `get_all_funds_from_db()` call in a `try/except` ensures the portfolio page does not crash if the hedge fund DB is unavailable.

---

### Step 13: Add `_render_hedge_fund_overlap()` to `pages/9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Immediately after `_find_overlapping_funds()` (inserted in Step 12), before `_sentiment_color`.

**Action:** Insert the following new function:

```python

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

```

**Why:** This renders the visual section for the hedge fund overlap feature. The `st.expander` pattern, `st.metric` for metadata, and `st.dataframe` for holdings tables are consistent with `pages/5_hedge_funds.py`'s `_render_fund_card()` function. The `pd.DataFrame` import is already present at the top of the file. Value formatting mirrors `_fmt_value()` from `pages/5_hedge_funds.py`.

---

### Step 14: Add the "Hedge Fund Overlap" section to the bottom of the portfolio page's main flow in `pages/9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** At the very end of the file, after the final line `_options_analysis_ui(tickers)` (currently line 868).

**Action:** Append the following lines at the very end of the file (after line 868):

```python

# ---------------------------------------------------------------------------
# Hedge Fund Overlap
# ---------------------------------------------------------------------------
st.markdown("---")
st.subheader("Hedge Fund Overlap")
_render_hedge_fund_overlap(positions_result)
```

**Why:** This call is placed after `_options_analysis_ui(tickers)` because `positions_result` has already been validated and is guaranteed to be a non-empty list at this point in the file. The `st.stop()` calls earlier in the file (lines 333, 336, 392) ensure that if `positions_result` is missing or an error, execution halts before reaching this code. The `---` divider and `st.subheader` are consistent with how "News Analysis" and "Options Analysis" are introduced above.

---

## 5. Database Changes

The two new tables are created automatically by `_get_connection()` on first connect after the Step 1 code change is deployed. No manual migration command is needed.

**New table definitions (for reference):**

```sql
CREATE TABLE IF NOT EXISTS hedge_fund_filings (
    cik              TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    report_period    TEXT NOT NULL,
    filing_date      TEXT NOT NULL,
    total_holdings   INTEGER NOT NULL,
    total_value      REAL NOT NULL,
    holdings_json    TEXT NOT NULL,
    fetched_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hedge_fund_check_log (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    last_checked TEXT NOT NULL,
    last_count   INTEGER NOT NULL DEFAULT 0
);
```

**Rollback SQL (if tables need to be removed):**
```sql
DROP TABLE IF EXISTS hedge_fund_filings;
DROP TABLE IF EXISTS hedge_fund_check_log;
```

The old `hedge_fund_cache` table is left in place and its helper functions (`save_hedge_fund_cache`, `load_hedge_fund_cache`) remain in `cache.py` unchanged.

---

## 6. UI/UX Specification

### Hedge Fund Overlap Section (bottom of Portfolio page)

- **Divider:** `st.markdown("---")` — matches the dividers above "News Analysis" and "Options Analysis"
- **Section header:** `st.subheader("Hedge Fund Overlap")` — matches the header style of "Positions", "News Analysis", "Options Analysis"
- **Spinner text:** `"Checking hedge fund holdings…"` during `_find_overlapping_funds()` call
- **Empty DB / no tickers:** `st.info(...)` — info box, not a warning or error
- **No overlaps found:** `st.info(...)` with explanation of the filing window
- **Caption line when overlaps found:** Shows count of portfolio tickers checked and count of overlapping funds
- **Per-fund expander label:** `"{fund_name} — {N} shared position(s)"` — `expanded=False` (collapsed by default)
- **Per-fund metadata:** 3-column `st.columns(3)` with `st.metric` widgets: "Report Period", "Filing Date", "Fund Portfolio Value"
- **Overlapping holdings table:** `st.dataframe` with `use_container_width=True, hide_index=True`
  - Columns: Ticker, Issuer, % of Fund, Value, Type
  - Value formatted as `$XM` for >= $1M, `$X,XXX` for smaller amounts
  - "Type" shows the `put_call` field or "Equity" if empty

---

## 7. Testing Checklist

1. **New tables are created automatically**
   - Action: Delete `db/cache.db` (or just run the app fresh). Load the Hedge Funds page or Portfolio page.
   - Expected: No error. Run `sqlite3 db/cache.db ".tables"` and verify `hedge_fund_filings` and `hedge_fund_check_log` appear alongside `metric_cache` and `hedge_fund_cache`.
   - Failure: Table not present = `_get_connection()` changes not saved.

2. **`pages/5_hedge_funds.py` is unchanged**
   - Action: Navigate to the Hedge Funds page. Wait for data to load.
   - Expected: Top 5, Bottom 5, and Daily Pick tabs all render with fund cards exactly as before.
   - Failure: Error message or blank page = import or `get_concentrated_funds()` regression.

3. **First-ever run populates the DB**
   - Action: Delete `db/cache.db`. Navigate to the Hedge Funds page and let it load.
   - Expected: Funds display on page. Run `sqlite3 db/cache.db "SELECT COUNT(*) FROM hedge_fund_filings;"` — should be > 0.
   - Failure: Count is 0 = the fallback scan in `get_concentrated_funds()` is not calling `upsert_hedge_fund_filing()`.

4. **`_is_filing_window()` returns correct values**
   - Action: Temporarily test in a Python shell: `from data.hedge_fund_fetcher import _is_filing_window`. Today is 2026-05-14 — that is 44 days after March 31 (0-indexed: March 31 is day 0, May 14 is day 44). Expect: `True`.
   - Expected: `True` for May 14, 2026.
   - Failure: Returns `False` = date arithmetic is wrong.

5. **`_check_interval_hours()` returns correct values**
   - Action: In a Python shell on 2026-05-14: `from data.hedge_fund_fetcher import _check_interval_hours; print(_check_interval_hours())`. Day 44 after March 31 is > 30, so should return 24.
   - Expected: `24`
   - Failure: Returns 72 or 999999 = window day calculation is wrong.

6. **`refresh_hedge_fund_db()` skips poll when interval not elapsed**
   - Action: Call `refresh_hedge_fund_db()` twice in quick succession from a Python shell.
   - Expected: First call runs (may return 0 if no new filings). Second call returns 0 immediately without hitting EDGAR (very fast).
   - Failure: Second call is slow (still downloading) = `get_last_check_time()` or interval logic is broken.

7. **Portfolio page loads without error**
   - Action: Navigate to the Portfolio page (page 9) with a valid Webull account configured.
   - Expected: Page loads, positions display, and at the bottom a "Hedge Fund Overlap" section appears below Options Analysis.
   - Failure: ImportError = Step 10 import not saved. AttributeError = function name typo.

8. **Hedge Fund Overlap section shows correct info with no DB data**
   - Action: Delete `db/cache.db`. Navigate directly to the Portfolio page before going to Hedge Funds page (so DB is empty).
   - Expected: The "Hedge Fund Overlap" section shows: "No concentrated hedge funds (< 15 positions) hold any of your current positions..."
   - Failure: Crash or wrong message = `get_all_funds_from_db()` not returning an empty list safely.

9. **Hedge Fund Overlap section shows overlapping funds when DB is populated**
   - Action: First, go to the Hedge Funds page to populate the DB. Then go to the Portfolio page.
   - Expected: If any of your Webull positions match holdings in concentrated funds, they appear as expanders labeled `"{Fund Name} — N shared position(s)"`.
   - Failure: No expanders despite expected overlaps = `_find_overlapping_funds()` ticker matching is case-sensitive or the holdings list is empty.

10. **Expander contents are correct**
    - Action: Click open an expander in the Hedge Fund Overlap section.
    - Expected: Three metric columns (Report Period, Filing Date, Fund Portfolio Value), then a "Overlapping Holdings" subheading, then a table with columns Ticker, Issuer, % of Fund, Value, Type.
    - Failure: Missing columns or wrong values = `_render_hedge_fund_overlap()` rows dict keys are wrong.

11. **Out-of-window date skips all polling**
    - Action: Temporarily set system date to a mid-quarter date (e.g., August 15) or mock `datetime.date.today()` in a Python shell to return `datetime.date(2026, 8, 15)` — 46 days past June 30, not yet within Sept 30 window. Call `_is_filing_window()`.
    - Expected: `False`. Then call `refresh_hedge_fund_db()` — should return 0 instantly.
    - Failure: Returns `True` or triggers EDGAR call = window check is wrong.

---

## 8. Rollback Plan

If the changes cause a critical failure, revert in this order:

**Step A: Revert `pages/9_portfolio.py`**
- Remove the three lines added at the end of the file (the `# Hedge Fund Overlap` block: `st.markdown("---")`, `st.subheader(...)`, `_render_hedge_fund_overlap(...)`).
- Remove the `_render_hedge_fund_overlap()` function (added in Step 13).
- Remove the `_find_overlapping_funds()` function (added in Step 12).
- Remove the `_get_portfolio_tickers()` function (added in Step 11).
- Remove the `from data.hedge_fund_fetcher import get_all_funds_from_db` import line (added in Step 10).

**Step B: Revert `data/hedge_fund_fetcher.py`**
- Restore `get_concentrated_funds()` to its original form (lines 132–206 as originally read).
- Remove `get_all_funds_from_db()` (added in Step 8).
- Remove `refresh_hedge_fund_db()` (added in Step 7).
- Remove `_fetch_fund_from_filing()` (added in Step 6).
- Remove `_check_interval_hours()` (added in Step 5).
- Remove `_is_filing_window()` (added in Step 4).
- Restore the import on line 9 to: `from data.cache import load_hedge_fund_cache, save_hedge_fund_cache` (revert Step 3).

**Step C: Revert `data/cache.py`**
- Remove the five new functions added at the end of the file (Step 2).
- Inside `_get_connection()`, remove the two new `conn.execute(...)` blocks for `hedge_fund_filings` and `hedge_fund_check_log` (Step 1), restoring the function to its original form ending at `conn.commit(); return conn` after only the two original tables.

**Step D: Drop new DB tables (optional)**
```sql
sqlite3 stock-dashboard/db/cache.db "DROP TABLE IF EXISTS hedge_fund_filings; DROP TABLE IF EXISTS hedge_fund_check_log;"
```
The old `hedge_fund_cache` table is unaffected and does not need to be touched.
