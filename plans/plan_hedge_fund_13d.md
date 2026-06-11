# Implementation Plan: SC 13D Filing Rationale on Hedge Funds Page

## 1. Task Summary

This plan adds SC 13D "Purpose of Transaction" (Item 4) text to the hedge funds page. For each concentrated hedge fund card, we fetch all SC 13D filings filed BY that fund (identified by its CIK), extract the Item 4 text from each filing's full-text submission, cache the results in SQLite, match each 13D entry to the fund's 13F holdings by normalizing the subject company name against the holding's issuer name, and display matched rationales as collapsible expanders inside the fund card — directly below the holdings table. No AI inference is needed: all text displayed is verbatim SEC filing content.

When fully executed, each fund card in the hedge funds page will show a "SC 13D Filings — Stated Rationale" section below the holdings table. Each rationale entry is a `st.expander` titled with the subject company name and filing date. Inside it is the cleaned Item 4 text. Funds with no SC 13D filings show nothing extra. The data is fetched once per CIK per page session (via `@st.cache_data`) and persisted in SQLite for cross-session reuse.

---

## 2. Files Involved

| File | Status | Changes |
|------|--------|---------|
| `stock-dashboard/data/thirteend_fetcher.py` | **CREATE** | New module — all SC 13D fetch, extract, and match logic |
| `stock-dashboard/data/cache.py` | **MODIFY** | Add `sc13d_cache` table creation + 4 new helper functions |
| `stock-dashboard/pages/5_hedge_funds.py` | **MODIFY** | Import new fetcher; modify `_render_fund_card()` to display rationale section |

---

## 3. Prerequisites & Dependencies

### Python packages
No new packages are required. `edgartools` (v5.30.0, already installed) provides `get_entity`. `rapidfuzz` is already installed as a dependency of `edgartools` and will be used for fuzzy name matching.

Verify both are present by running:
```
python -c "import edgar; from rapidfuzz import fuzz; print('OK')"
```

### Database
The `sc13d_cache` table will be added automatically by `cache.py`'s `_get_connection()` function (no separate migration needed — it uses `CREATE TABLE IF NOT EXISTS`). The existing `cache.db` file is at `stock-dashboard/db/cache.db`.

### No environment variable changes needed.

---

## 4. Step-by-Step Implementation

---

### Step 1: Add `sc13d_cache` table creation to `cache.py`

**File**: `stock-dashboard/data/cache.py`

**Location**: Inside the `_get_connection()` function, after the `conn.execute("""CREATE TABLE IF NOT EXISTS hedge_fund_check_log ...`)` block and before `conn.commit()` (currently at line 47).

**Action**: Insert the following `conn.execute()` call after the `hedge_fund_check_log` table creation block:

```python
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sc13d_cache (
            filer_cik        TEXT NOT NULL,
            accession_number TEXT NOT NULL,
            subject_company  TEXT NOT NULL,
            filing_date      TEXT NOT NULL,
            item4_text       TEXT,
            fetched_at       TEXT NOT NULL,
            PRIMARY KEY (filer_cik, accession_number)
        )
    """)
```

**Why**: This table stores one row per (fund CIK, accession number) pair. Because SC 13D filings don't change once filed, caching by accession number means we only pay the network cost once per filing forever. The `item4_text` column is nullable because some HTML-only filings cannot yield extracted text.

---

### Step 2: Add four cache helper functions to `cache.py`

**File**: `stock-dashboard/data/cache.py`

**Location**: After the final function `save_last_check_time()` (currently ending at line 279), append the following four functions at the end of the file.

**Action**: Add these four functions verbatim at the end of the file:

```python
def get_cached_sc13d_accessions(filer_cik: str) -> set:
    """Return the set of accession_numbers already stored for a given filer CIK.

    Used to detect which filings are new and need to be downloaded.
    Returns an empty set if the filer has no cached rows.
    """
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT accession_number FROM sc13d_cache WHERE filer_cik = ?",
            (str(filer_cik),),
        ).fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def save_sc13d_filing(
    filer_cik: str,
    accession_number: str,
    subject_company: str,
    filing_date: str,
    item4_text: Optional[str],
) -> None:
    """Insert or replace one SC 13D filing row in sc13d_cache.

    filer_cik: the hedge fund's CIK string (e.g. '1040570')
    accession_number: EDGAR accession number string (e.g. '0000905718-11-000083')
    subject_company: the company name from f.company (SEC filing subject)
    filing_date: ISO date string (e.g. '2011-02-25')
    item4_text: extracted Item 4 text, or None if extraction failed
    """
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO sc13d_cache
                (filer_cik, accession_number, subject_company, filing_date,
                 item4_text, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(filer_cik, accession_number) DO UPDATE SET
                subject_company = excluded.subject_company,
                filing_date     = excluded.filing_date,
                item4_text      = excluded.item4_text,
                fetched_at      = excluded.fetched_at
            """,
            (
                str(filer_cik),
                str(accession_number),
                str(subject_company),
                str(filing_date),
                item4_text,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def load_sc13d_filings(filer_cik: str) -> list:
    """Return all cached SC 13D rows for a filer CIK as a list of dicts.

    Each dict has keys: accession_number, subject_company, filing_date, item4_text.
    Returns an empty list if no rows are cached for this CIK.
    Rows are sorted by filing_date descending (most recent first).
    """
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT accession_number, subject_company, filing_date, item4_text
            FROM sc13d_cache
            WHERE filer_cik = ?
            ORDER BY filing_date DESC
            """,
            (str(filer_cik),),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "accession_number": row[0],
            "subject_company": row[1],
            "filing_date": row[2],
            "item4_text": row[3],
        }
        for row in rows
    ]


def sc13d_fetch_flag_exists(filer_cik: str) -> bool:
    """Return True if we have ever attempted to fetch SC 13D data for this CIK.

    This distinguishes 'no filings found' (a valid result we've stored) from
    'never fetched' (we need to try). We detect it by checking whether any row
    exists with this filer_cik — including rows with NULL item4_text.
    Even if a fund has zero SC 13D filings, we store a sentinel row to avoid
    re-fetching on every page load.

    Sentinel row convention: accession_number = '__none__', subject_company = '',
    filing_date = today's ISO date, item4_text = NULL.
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM sc13d_cache WHERE filer_cik = ? LIMIT 1",
            (str(filer_cik),),
        ).fetchone()
    finally:
        conn.close()
    return row is not None
```

**Why**: These four functions are the complete SQLite interface for the SC 13D cache. They follow the exact same pattern as the existing `get_stored_accession_numbers()`, `upsert_hedge_fund_filing()`, and `get_all_hedge_fund_filings()` functions already in `cache.py` — same `_get_connection()` usage, same `try/finally` pattern, same direct SQL strings with parameterized queries.

---

### Step 3: Create the new file `stock-dashboard/data/thirteend_fetcher.py`

**File**: `stock-dashboard/data/thirteend_fetcher.py` (new file — does not exist yet)

**Action**: Create this file with the following complete content:

```python
"""
thirteend_fetcher.py — SC 13D filing fetcher for the hedge funds page.

For a given fund CIK, fetches all SC 13D / SC 13D/A filings that the fund has
filed with the SEC (i.e., where the fund is the REPORTING PERSON disclosing a
>=5% stake in a subject company).  Extracts the Item 4 "Purpose of Transaction"
text from each filing's full-text submission.  Results are cached in SQLite via
cache.py so each accession number is only downloaded once.

Public API
----------
get_sc13d_rationales(filer_cik: str) -> list[dict]
    Returns cached+new SC 13D rationale rows for a fund CIK.
    Each dict: {subject_company, filing_date, item4_text, accession_number}

match_rationales_to_holdings(rationales: list, holdings: list) -> dict
    Returns a dict mapping normalized issuer name -> list of rationale dicts.
    Used to identify which holdings have a corresponding SC 13D rationale.
"""

import re
from datetime import date
from typing import Optional

import streamlit as st
from edgar import get_entity, set_identity

from data.cache import (
    get_cached_sc13d_accessions,
    save_sc13d_filing,
    load_sc13d_filings,
    sc13d_fetch_flag_exists,
)

set_identity("ethanjosemorris@gmail.com")

_MAX_13D_FILINGS_TO_SCAN = 50  # Cap per fund to avoid slow page loads


def _extract_item4_text(filing_text: str) -> Optional[str]:
    """Extract Item 4 'Purpose of Transaction' text from a raw SC 13D filing text.

    Looks for the 'Item 4' heading (case-insensitive, with optional spaces and
    punctuation) and extracts up to the 'Item 5' heading.  Returns None if Item 4
    cannot be found (e.g. HTML-only table-formatted filings).

    Args:
        filing_text: The full plain-text content of an SC 13D filing (f.text).

    Returns:
        Cleaned Item 4 text string, or None if not found.
    """
    if not filing_text:
        return None

    m4 = re.search(r"Item\s*4[.\s]", filing_text, re.IGNORECASE)
    if not m4:
        return None

    start = m4.start()
    m5 = re.search(r"Item\s*5[.\s]", filing_text[start + 10:], re.IGNORECASE)
    if m5:
        end = start + 10 + m5.start()
    else:
        end = min(len(filing_text), start + 4000)

    raw = filing_text[start:end].strip()

    # Normalize whitespace: collapse 3+ newlines to double newline, collapse spaces
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    raw = re.sub(r"[ \t]+", " ", raw)

    return raw if len(raw) > 20 else None


def _normalize_company_name(name: str) -> str:
    """Normalize a company name for matching: uppercase, strip punctuation/spaces.

    Args:
        name: Raw company name string (e.g. 'Apple Inc.' or 'APPLE INC').

    Returns:
        Normalized string (e.g. 'APPLE INC').
    """
    s = name.upper()
    s = re.sub(r"[^A-Z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _fetch_and_cache_sc13d_filings(filer_cik: str) -> None:
    """Download new SC 13D filings for a fund CIK and cache them in SQLite.

    This function is called only when the fund has never been fetched (no sentinel
    row exists in sc13d_cache for this CIK).  It:
      1. Calls entity.get_filings(form='SC 13D') to get all SC 13D + SC 13D/A filings.
      2. Checks which accession numbers are already cached.
      3. For each new filing, downloads the full text and extracts Item 4.
      4. Saves each filing row to SQLite via save_sc13d_filing().
      5. If the fund has zero SC 13D filings, writes a sentinel row so we don't
         re-fetch on every page load.

    Args:
        filer_cik: The fund's CIK string (e.g. '1040570').

    Returns:
        None. Results are written to SQLite.
    """
    try:
        entity = get_entity(filer_cik)
        filings = entity.get_filings(form="SC 13D")
    except Exception:
        # Network error or CIK not found — write sentinel so we don't retry endlessly
        save_sc13d_filing(
            filer_cik=filer_cik,
            accession_number="__none__",
            subject_company="",
            filing_date=str(date.today()),
            item4_text=None,
        )
        return

    total = len(filings) if hasattr(filings, "__len__") else 0

    if total == 0:
        # No SC 13D filings for this fund — write sentinel
        save_sc13d_filing(
            filer_cik=filer_cik,
            accession_number="__none__",
            subject_company="",
            filing_date=str(date.today()),
            item4_text=None,
        )
        return

    cached_accessions = get_cached_sc13d_accessions(filer_cik)
    scanned = 0

    for filing in filings:
        if scanned >= _MAX_13D_FILINGS_TO_SCAN:
            break
        scanned += 1

        accession_number = str(getattr(filing, "accession_number", "") or "")
        if not accession_number:
            continue

        if accession_number in cached_accessions:
            continue  # Already stored — skip expensive text download

        subject_company = str(getattr(filing, "company", "") or "")
        filing_date = str(getattr(filing, "filing_date", "") or "")

        # Download full text and extract Item 4
        item4_text: Optional[str] = None
        try:
            raw_text = filing.text
            if callable(raw_text):
                raw_text = raw_text()
            item4_text = _extract_item4_text(raw_text)
        except Exception:
            item4_text = None

        save_sc13d_filing(
            filer_cik=filer_cik,
            accession_number=accession_number,
            subject_company=subject_company,
            filing_date=filing_date,
            item4_text=item4_text,
        )
        cached_accessions.add(accession_number)


def _get_deduplicated_rationales(raw_rows: list) -> list:
    """Deduplicate SC 13D rows by subject company, keeping only the most recent.

    Since get_filings() returns results in date-descending order and we fetch/store
    them in that order, the first row per subject company (lowest filing_date index
    after sorting DESC) is the most recent filing.  Excludes sentinel rows
    (accession_number == '__none__').

    Args:
        raw_rows: List of dicts from load_sc13d_filings().

    Returns:
        List of dicts (one per unique subject company), most recent per company,
        sorted by filing_date descending.
    """
    seen: set = set()
    result = []
    for row in raw_rows:
        if row["accession_number"] == "__none__":
            continue
        key = _normalize_company_name(row["subject_company"])
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


@st.cache_data(ttl=86400)
def get_sc13d_rationales(filer_cik: str) -> list:
    """Return SC 13D rationale rows for a fund CIK, fetching if not yet cached.

    The @st.cache_data(ttl=86400) wrapper prevents repeated SQLite reads within
    the same Streamlit session (and across re-runs within 24 hours).  The SQLite
    layer provides cross-session persistence so EDGAR is only hit when new filings
    appear.

    Algorithm:
      1. Check if this CIK has ever been fetched (sentinel or real rows in cache).
      2. If not: call _fetch_and_cache_sc13d_filings() to download and cache.
      3. Load all cached rows from SQLite.
      4. Deduplicate: keep only the most recent filing per subject company.
      5. Return the deduplicated list.

    Args:
        filer_cik: The fund's CIK string (same CIK stored in hedge_fund_filings).

    Returns:
        List of dicts, each with keys:
          - subject_company (str): name of the company the fund filed 13D on
          - filing_date (str): ISO date string of the most recent filing
          - item4_text (str or None): extracted Item 4 text, or None
          - accession_number (str): EDGAR accession number
        Sorted by filing_date descending. Returns [] if no SC 13D filings exist.
    """
    if not sc13d_fetch_flag_exists(filer_cik):
        _fetch_and_cache_sc13d_filings(filer_cik)

    raw_rows = load_sc13d_filings(filer_cik)
    return _get_deduplicated_rationales(raw_rows)


def match_rationales_to_holdings(rationales: list, holdings: list) -> dict:
    """Match SC 13D rationale entries to 13F holdings by normalized company name.

    Uses exact normalized match first, then rapidfuzz partial ratio >= 85 as
    a fallback.  A rationale is only matched to ONE holding (the highest-scoring
    one if multiple exceed threshold).

    Args:
        rationales: Output of get_sc13d_rationales() — list of dicts.
        holdings: List of HoldingRow dicts from the fund's 13F filing.
                  Each has 'issuer' (str) and 'ticker' (str) keys.

    Returns:
        Dict mapping normalized issuer string -> list of rationale dicts.
        Example: {'APPLE INC': [{'subject_company': 'Apple Inc.', ...}]}
        Issuers with no matching rationale do not appear in the dict.
    """
    if not rationales or not holdings:
        return {}

    try:
        from rapidfuzz import fuzz as _fuzz
        _has_rapidfuzz = True
    except ImportError:
        _has_rapidfuzz = False

    # Build normalized issuer map: normalized_name -> original issuer string
    norm_holdings = {}
    for h in holdings:
        issuer = str(h.get("issuer") or "")
        if issuer:
            norm_holdings[_normalize_company_name(issuer)] = issuer

    result: dict = {}

    for rationale in rationales:
        subj = str(rationale.get("subject_company") or "")
        if not subj:
            continue
        norm_subj = _normalize_company_name(subj)

        # Exact match first
        if norm_subj in norm_holdings:
            orig_issuer = norm_holdings[norm_subj]
            result.setdefault(orig_issuer, []).append(rationale)
            continue

        # Fuzzy fallback
        if not _has_rapidfuzz:
            continue

        best_score = 0
        best_issuer = None
        for norm_issuer, orig_issuer in norm_holdings.items():
            score = _fuzz.partial_ratio(norm_subj, norm_issuer)
            if score > best_score:
                best_score = score
                best_issuer = orig_issuer

        if best_score >= 85 and best_issuer is not None:
            result.setdefault(best_issuer, []).append(rationale)

    return result
```

**Why**: This is a self-contained module following the same pattern as `hedge_fund_fetcher.py` — it imports from `data.cache`, calls `set_identity()` at module level, uses `@st.cache_data` as the in-session layer, and lets SQLite handle cross-session persistence. Keeping it in a separate file (rather than adding to `hedge_fund_fetcher.py`) follows the project's one-concern-per-module pattern and keeps both files readable.

---

### Step 4: Modify `_render_fund_card()` in `pages/5_hedge_funds.py`

**File**: `stock-dashboard/pages/5_hedge_funds.py`

**Location**: The top of the file (import section, currently lines 1–4) and the `_render_fund_card()` function (currently lines 72–87).

**Action Part A — Add import**: Replace the current import block:

```python
import streamlit as st

from components.gemini_usage_bar import render_gemini_usage_bar
from data.hedge_fund_fetcher import get_categorized_funds
```

with:

```python
import streamlit as st

from components.gemini_usage_bar import render_gemini_usage_bar
from data.hedge_fund_fetcher import get_categorized_funds
from data.thirteend_fetcher import get_sc13d_rationales, match_rationales_to_holdings
```

**Why**: Makes the two new functions available to `_render_fund_card()`.

**Action Part B — Modify `_render_fund_card()`**: Replace the entire `_render_fund_card()` function (lines 72–87 currently):

```python
def _render_fund_card(fund: dict) -> None:
    label = (
        f"{fund['name']} — "
        f"{fund['total_holdings']} positions · "
        f"{_fmt_value(fund['total_value'])}"
    )
    with st.expander(label, expanded=False):
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Positions", fund["total_holdings"])
        col2.metric("Portfolio Value", _fmt_value(fund["total_value"]))
        col3.metric("Report Period", fund["report_period"] or "—")
        col4.metric("Filed", fund["filing_date"] or "—")

        st.markdown(f"**CIK:** {fund['cik']}")
        st.markdown("---")
        _render_holdings_table(fund["holdings"])
```

with:

```python
def _render_fund_card(fund: dict) -> None:
    label = (
        f"{fund['name']} — "
        f"{fund['total_holdings']} positions · "
        f"{_fmt_value(fund['total_value'])}"
    )
    with st.expander(label, expanded=False):
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Positions", fund["total_holdings"])
        col2.metric("Portfolio Value", _fmt_value(fund["total_value"]))
        col3.metric("Report Period", fund["report_period"] or "—")
        col4.metric("Filed", fund["filing_date"] or "—")

        st.markdown(f"**CIK:** {fund['cik']}")
        st.markdown("---")
        _render_holdings_table(fund["holdings"])
        _render_13d_rationale_section(fund)
```

**Why**: The only change is replacing the final `_render_holdings_table(fund["holdings"])` call with two calls — the existing table render plus a new rationale section render. The rationale call is extracted into its own function for readability and testability.

---

### Step 5: Add `_render_13d_rationale_section()` to `pages/5_hedge_funds.py`

**File**: `stock-dashboard/pages/5_hedge_funds.py`

**Location**: After the `_render_holdings_table()` function definition (currently ending at line 69) and before `_render_fund_card()` (currently starting at line 72).

**Action**: Insert the following new function between `_render_holdings_table()` and `_render_fund_card()`:

```python
def _render_13d_rationale_section(fund: dict) -> None:
    """Fetch and display SC 13D rationale entries for a fund inside its card.

    Shows a 'SC 13D Filings — Stated Rationale' sub-header followed by one
    st.expander per matched holding that has a 13D rationale.  If the fund has
    no SC 13D filings at all, this function renders nothing (no header either).

    Each rationale expander is titled: '<Subject Company> · Filed <date>'
    Inside: the Item 4 'Purpose of Transaction' text verbatim (or a fallback
    message if Item 4 could not be extracted from the filing).

    Args:
        fund: A FundProfile dict (must have keys 'cik' and 'holdings').
    """
    cik = fund.get("cik", "")
    holdings = fund.get("holdings", [])
    if not cik:
        return

    try:
        rationales = get_sc13d_rationales(cik)
    except Exception:
        return  # Silently skip on network or parse error

    if not rationales:
        return  # Fund has no SC 13D filings — show nothing

    # Match rationales to holdings by company name
    matched = match_rationales_to_holdings(rationales, holdings)

    # Also collect unmatched rationales (not tied to a current 13F holding)
    # These are shown separately so no information is hidden
    matched_accessions: set = set()
    for rationale_list in matched.values():
        for r in rationale_list:
            matched_accessions.add(r["accession_number"])
    unmatched = [r for r in rationales if r["accession_number"] not in matched_accessions]

    st.markdown("---")
    st.markdown("**SC 13D Filings — Stated Rationale**")
    st.caption(
        "Item 4 'Purpose of Transaction' text from the most recent SC 13D or SC 13D/A "
        "filed by this manager on EDGAR. Matched to holdings by issuer name."
    )

    # Render matched rationales (tied to a current holding)
    if matched:
        for issuer, rationale_list in matched.items():
            for rationale in rationale_list:
                expander_title = (
                    f"{rationale['subject_company']} · Filed {rationale['filing_date']}"
                )
                with st.expander(expander_title, expanded=False):
                    st.caption(f"Matched to holding: **{issuer}** | Accession: {rationale['accession_number']}")
                    if rationale["item4_text"]:
                        st.text(rationale["item4_text"])
                    else:
                        st.info(
                            "Item 4 text could not be extracted from this filing "
                            "(HTML-formatted document). View on EDGAR: "
                            f"https://www.sec.gov/cgi-bin/browse-edgar?"
                            f"action=getcompany&CIK={cik}&type=SC+13D"
                        )

    # Render unmatched rationales (SC 13D on a company not in current 13F)
    if unmatched:
        for rationale in unmatched:
            expander_title = (
                f"{rationale['subject_company']} · Filed {rationale['filing_date']} "
                f"(not in current 13F)"
            )
            with st.expander(expander_title, expanded=False):
                st.caption(f"Accession: {rationale['accession_number']}")
                if rationale["item4_text"]:
                    st.text(rationale["item4_text"])
                else:
                    st.info(
                        "Item 4 text could not be extracted from this filing "
                        "(HTML-formatted document). View on EDGAR: "
                        f"https://www.sec.gov/cgi-bin/browse-edgar?"
                        f"action=getcompany&CIK={cik}&type=SC+13D"
                    )
```

**Why**: This function handles the entire UI concern in one place. It is deliberately separated from `_render_fund_card()` to keep each function focused. It silently returns on any error so a 13D fetch failure never crashes the fund card. Unmatched rationales (where the 13D target is no longer in the 13F) are still shown — this is valuable context (e.g., a fund exited a position they had previously filed a 13D on).

---

## 5. Database Changes

### New table: `sc13d_cache` (in `stock-dashboard/db/cache.db`)

This table is created automatically via `CREATE TABLE IF NOT EXISTS` in `_get_connection()`. No manual migration is needed. Schema:

```sql
CREATE TABLE IF NOT EXISTS sc13d_cache (
    filer_cik        TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    subject_company  TEXT NOT NULL,
    filing_date      TEXT NOT NULL,
    item4_text       TEXT,               -- NULL if Item 4 could not be extracted
    fetched_at       TEXT NOT NULL,
    PRIMARY KEY (filer_cik, accession_number)
);
```

**Sentinel row convention**: When a fund has zero SC 13D filings, one row is written with `accession_number = '__none__'` and `subject_company = ''`. This prevents re-fetching EDGAR on every page load for funds that never file 13Ds.

### Rollback

To fully remove the table: connect to `stock-dashboard/db/cache.db` with any SQLite client and run:
```sql
DROP TABLE IF EXISTS sc13d_cache;
```

---

## 6. UI/UX Specification

### Location in fund card
The SC 13D rationale section appears INSIDE each fund's `st.expander`, after the holdings DataFrame. Specifically:
- A horizontal rule `st.markdown("---")` visually separates the holdings table from the rationale section.
- A bold sub-header `st.markdown("**SC 13D Filings — Stated Rationale**")`.
- A `st.caption()` explaining the data source.
- One `st.expander` per rationale entry (collapsed by default).

### Expander titles
- Matched (holding exists in 13F): `"<Subject Company> · Filed <YYYY-MM-DD>"`
- Unmatched (no current 13F holding): `"<Subject Company> · Filed <YYYY-MM-DD> (not in current 13F)"`

### Expander contents
- `st.caption()` with accession number and matched holding name.
- `st.text()` with the raw Item 4 text (`st.text` preserves whitespace/newlines).
- `st.info()` with EDGAR link if item4_text is None.

### Loading behavior
- `get_sc13d_rationales()` is decorated with `@st.cache_data(ttl=86400)`.
- First call for a new CIK will hit EDGAR (downloads up to 50 filings' text).
- Subsequent calls within 24 hours return instantly from memory cache.
- Cross-session: SQLite cache means even first call after session restart returns instantly if data was previously fetched.
- The fund card outer expander is `expanded=False` — so the 13D fetch only runs when the user opens the card AND Streamlit renders `_render_fund_card()`. Because `@st.cache_data` is eager (computed when the function is first called regardless of whether the expander is open), the fetch runs for ALL funds in the visible tab when the page loads. This is acceptable given the SQLite cache avoids re-downloading.

### No spinner needed
The `get_sc13d_rationales()` call is fast after first load (SQLite read). For first-ever load of a CIK, the function takes 2–10 seconds (depends on how many filings the fund has). The outer page spinner in `main()` already covers the initial data load. No additional spinner is needed inside the fund card.

---

## 7. Testing Checklist

1. **Table creation**: Start the app fresh. Open `stock-dashboard/db/cache.db` in a SQLite browser. Confirm the `sc13d_cache` table now exists with columns: `filer_cik, accession_number, subject_company, filing_date, item4_text, fetched_at`.

2. **Page loads without error**: Navigate to the Hedge Funds page. Confirm the page loads with no Python exceptions in the Streamlit terminal output.

3. **Fund cards render**: Open any fund card expander. Confirm the metrics, holdings table, and horizontal rule all display correctly (no regression from the existing UI).

4. **SC 13D section present for active filers**: Open a fund card for a fund known to file SC 13Ds (Devon Energy, CIK=1090012, or American Financial Group, CIK=943719 if present in the DB). Confirm the "SC 13D Filings — Stated Rationale" header and at least one expander appear below the holdings table.

5. **Item 4 text displays**: Expand a rationale entry. Confirm the Item 4 text is visible and begins with "Item 4." or "ITEM 4.". Confirm it is NOT empty and NOT a spinner or error message (for filings that have plain-text content).

6. **Sentinel written for no-13D funds**: Open a fund card for a fund with no SC 13D filings (e.g., Bratton Capital Management, CIK=1723787). Confirm NO "SC 13D Filings" section appears (the function silently returns). Then check the DB: run `SELECT * FROM sc13d_cache WHERE filer_cik = '1723787'` and confirm one sentinel row with `accession_number='__none__'` exists.

7. **Cache is used on re-run**: Reload the page. Open the same fund card again. Confirm in Streamlit terminal that no new EDGAR HTTP requests are made (the SQLite + st.cache_data cache should absorb the call).

8. **Fuzzy matching works**: Find a fund where the SC 13D subject company name slightly differs from the 13F issuer name (e.g., "Apple Inc." vs "APPLE INC"). Confirm the rationale appears as "matched" (not in the "not in current 13F" group).

9. **Unmatched rationales displayed**: If a fund has filed a 13D on a company it no longer holds in its 13F, confirm that rationale appears with the "(not in current 13F)" suffix in the expander title.

10. **HTML-only filing fallback**: If a rationale has `item4_text = None`, confirm `st.info()` is displayed with an EDGAR link (not an error or blank space).

11. **No crash on bad CIK**: Manually insert a row in `hedge_fund_filings` with a nonsense CIK (or test with a CIK that has no EDGAR entity). Confirm the fund card still renders normally — `_render_13d_rationale_section()` catches the exception and returns silently.

12. **Max scan cap respected**: For a fund with >50 SC 13D filings, confirm that only `_MAX_13D_FILINGS_TO_SCAN = 50` accession numbers are stored in `sc13d_cache` for that CIK (check count in DB).

---

## 8. Rollback Plan

If the implementation needs to be fully reverted:

**Step R1**: Revert `stock-dashboard/pages/5_hedge_funds.py` to remove the import and the call to `_render_13d_rationale_section()`:
- Remove `from data.thirteend_fetcher import get_sc13d_rationales, match_rationales_to_holdings` from the import block.
- Remove the `_render_13d_rationale_section()` function definition entirely (the new function added between `_render_holdings_table` and `_render_fund_card`).
- In `_render_fund_card()`, remove the `_render_13d_rationale_section(fund)` call, leaving only `_render_holdings_table(fund["holdings"])`.

**Step R2**: Delete `stock-dashboard/data/thirteend_fetcher.py`:
```
Remove-Item "stock-dashboard\data\thirteend_fetcher.py"
```

**Step R3**: Revert `stock-dashboard/data/cache.py`:
- Remove the `sc13d_cache` table creation block from `_get_connection()` (the `conn.execute("""CREATE TABLE IF NOT EXISTS sc13d_cache ...""")` block added in Step 1).
- Remove the four functions added in Step 2: `get_cached_sc13d_accessions`, `save_sc13d_filing`, `load_sc13d_filings`, `sc13d_fetch_flag_exists`.

**Step R4**: Drop the SQLite table (optional, DB will just have an unused table otherwise):
```python
import sqlite3
conn = sqlite3.connect("stock-dashboard/db/cache.db")
conn.execute("DROP TABLE IF EXISTS sc13d_cache")
conn.commit()
conn.close()
```

After Steps R1–R3 the app is fully restored to its previous state. Step R4 is cosmetic cleanup only.
