# Plan: Screener Fundamentals Enrichment

> **IMPLEMENTOR NOTE:** Before writing any code, read `stock-dashboard/implementor-skills.md` in full. It contains the exact SQLite helper patterns, Gemini CLI invocation, DB path convention, timestamp format, and caching rules you must follow. Every code pattern in this plan is derived from that file and the existing codebase. All file paths below are absolute.

---

## 1. Task Summary

This plan extends the existing Speculative Stock Screener (page 12) to incorporate fundamental data — revenue growth, cash runway, short interest, analyst price targets, balance-sheet health, and insider ownership — into both the speculation score and the Gemini risk-analysis prompt. Currently the score is purely technical (price/volume signals). After this plan is executed:

- `data/screener.py` will fetch `yf.Ticker(symbol).info` in parallel (up to 8 threads) for the top N candidates ranked by their base technical score, persist results to a new `screener_fundamentals` table in `screener.db` (24h TTL), and compute a blended 100-point score split 50 pts technical + 50 pts fundamental.
- The score computation returns a breakdown dict so the UI can show per-component contributions.
- `data/screener_agent.py`'s `_build_prompt` will include a full fundamentals block in the Gemini prompt.
- `pages/12_screener.py` will expose a "Enrich top N fundamentals" control, add fundamental columns to the results table, display a score breakdown in each expander, and show a cash-runway warning badge.
- `db/screener_schema.sql` will gain a new `screener_fundamentals` table (idempotent `CREATE TABLE IF NOT EXISTS`).

---

## 2. Files Involved

| File | Status | Changes |
|------|--------|---------|
| `C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard\db\screener_schema.sql` | **Modify** | Append new `screener_fundamentals` table DDL |
| `C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard\data\screener.py` | **Modify** | Add fundamentals fetch layer, parallel enrichment, blended score, breakdown dict, new public API function `enrich_with_fundamentals` |
| `C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard\data\screener_agent.py` | **Modify** | Extend `_build_prompt` to accept and render a fundamentals block; update `_PROMPT_TEMPLATE` |
| `C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard\pages\12_screener.py` | **Modify** | Add enrich-N control, new fundamental columns in table, score breakdown in expander, cash-runway warning badge, updated sidebar help text |

No files are created from scratch; all changes extend existing modules.

---

## 3. Prerequisites & Dependencies

No new pip packages are required. `concurrent.futures` is part of the Python standard library. All other dependencies (`yfinance`, `pandas`, `streamlit`, `plotly`) are already installed in the project venv at `C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard\venv\`.

No new environment variables are required.

The SQLite schema change is purely additive (`CREATE TABLE IF NOT EXISTS`) — it will not affect existing rows or indexes in `screener.db`.

---

## 4. Step-by-Step Implementation

---

### Step 1: Append the `screener_fundamentals` table to `db/screener_schema.sql`

**File:** `C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard\db\screener_schema.sql`

**Location:** At the very end of the file, after the last existing line (`    ON screener_analysis (ticker, analyzed_at);`). Add two blank lines, then the block below.

**Action:** Append the following text to the end of the file. Do not alter any existing lines.

```sql


CREATE TABLE IF NOT EXISTS screener_fundamentals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    fundamentals_json TEXT NOT NULL,
    fetched_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sf_ticker
    ON screener_fundamentals (ticker);

CREATE INDEX IF NOT EXISTS idx_sf_ticker_fetched
    ON screener_fundamentals (ticker, fetched_at);
```

**Why:** The schema file is the single source of truth executed by `init_db()` at import time via `conn.executescript(ddl)`. Adding `CREATE TABLE IF NOT EXISTS` is idempotent — it will not break existing installs. The index on `(ticker, fetched_at)` supports the TTL cache lookup pattern used by the other two tables.

---

### Step 2: Add the fundamentals fetch/cache layer and blended score to `data/screener.py`

**File:** `C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard\data\screener.py`

This step makes **four changes** to the existing file, applied in the order listed below.

---

#### 2a — Add `concurrent.futures` import

**Location:** The very top of the file, in the imports block. The current imports block ends at line 15 (`import yfinance as yf`). Insert one new line **after** `from yfinance import EquityQuery` (currently line 16 of the file).

Find this exact text:
```python
import yfinance as yf
from yfinance import EquityQuery
```

Replace with:
```python
import concurrent.futures
import yfinance as yf
from yfinance import EquityQuery
```

**Why:** `concurrent.futures.ThreadPoolExecutor` is used in Step 2c to parallelize `yf.Ticker(symbol).info` calls. Adding the import here keeps all standard-library imports together at the top.

---

#### 2b — Add the fundamentals TTL constant

**Location:** In the `# Constants` section, after the line `_CACHE_TTL_HOURS = 1`. Find this exact text:

```python
# Screener result cache TTL
_CACHE_TTL_HOURS = 1
```

Replace with:
```python
# Screener result cache TTL
_CACHE_TTL_HOURS = 1

# Fundamentals cache TTL (fundamentals change slowly; 24h is appropriate)
_FUNDAMENTALS_TTL_HOURS = 24

# Number of top-ranked candidates to enrich with fundamentals by default
_DEFAULT_ENRICH_TOP_N = 25
```

**Why:** These constants are referenced by the fundamentals cache helpers added in 2c and by the public API in 2e. Defining them as module-level constants follows the existing pattern (`_CACHE_TTL_HOURS`, `_MICRO_CAP_MAX`, `_SMALL_CAP_MAX`).

---

#### 2c — Add the fundamentals SQLite cache helpers and parallel fetch function

**Location:** After the existing `_load_screener_results` function (which ends with `return None`), before the `# yfinance screener` section comment. Insert the entire block below between those two sections.

Find this exact text (which is the last two lines of `_load_screener_results` and the section divider that follows):
```python
    try:
        return json.loads(data["results_json"])
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# yfinance screener
# ---------------------------------------------------------------------------
```

Replace with:
```python
    try:
        return json.loads(data["results_json"])
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Fundamentals SQLite cache helpers
# ---------------------------------------------------------------------------

def _save_fundamentals(ticker: str, fundamentals: dict) -> None:
    """Persist fetched .info fundamentals for a single ticker to SQLite."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO screener_fundamentals (ticker, fundamentals_json, fetched_at) VALUES (?, ?, ?)",
            (ticker.upper(), json.dumps(fundamentals), now_iso),
        )
        conn.commit()
    finally:
        conn.close()


def _load_fundamentals(ticker: str) -> Optional[dict]:
    """
    Load cached fundamentals for ticker if within _FUNDAMENTALS_TTL_HOURS.
    Returns None if no row exists or the row is stale.
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT fundamentals_json, fetched_at
            FROM screener_fundamentals
            WHERE ticker = ?
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            (ticker.upper(),),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    data = dict(row)
    try:
        fetched_at = datetime.fromisoformat(data["fetched_at"].replace("Z", "+00:00"))
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

    if datetime.now(timezone.utc) - fetched_at > timedelta(hours=_FUNDAMENTALS_TTL_HOURS):
        return None

    try:
        return json.loads(data["fundamentals_json"])
    except (json.JSONDecodeError, TypeError):
        return None


def _fetch_single_fundamentals(ticker: str) -> tuple[str, dict]:
    """
    Fetch yf.Ticker(ticker).info and extract the fundamental keys relevant to
    speculation scoring. Returns (ticker_upper, fundamentals_dict).

    Checks the SQLite cache first. On any error or missing key, returns the
    partial dict with None values for missing fields — never raises.

    Keys extracted (all may be None for micro/small-caps with sparse data):
        revenue_growth        float | None  — YoY revenue growth as decimal (e.g. 0.35 = 35%)
        earnings_growth       float | None  — YoY earnings growth as decimal
        gross_margins         float | None  — gross margin as decimal
        total_cash            float | None  — total cash in USD
        total_debt            float | None  — total debt in USD
        free_cashflow         float | None  — annual FCF in USD (negative = burning cash)
        operating_cashflow    float | None  — operating cash flow in USD
        short_percent_float   float | None  — short interest as % of float (0–1 decimal)
        shares_outstanding    float | None  — total shares outstanding
        float_shares          float | None  — float shares
        target_mean_price     float | None  — analyst mean price target in USD
        debt_to_equity        float | None  — debt/equity ratio
        held_percent_insiders float | None  — insider ownership as decimal (e.g. 0.08 = 8%)
    """
    ticker_upper = ticker.upper()

    # Cache check
    cached = _load_fundamentals(ticker_upper)
    if cached is not None:
        return ticker_upper, cached

    _KEY_MAP = {
        "revenue_growth":        "revenueGrowth",
        "earnings_growth":       "earningsGrowth",
        "gross_margins":         "grossMargins",
        "total_cash":            "totalCash",
        "total_debt":            "totalDebt",
        "free_cashflow":         "freeCashflow",
        "operating_cashflow":    "operatingCashflow",
        "short_percent_float":   "shortPercentOfFloat",
        "shares_outstanding":    "sharesOutstanding",
        "float_shares":          "floatShares",
        "target_mean_price":     "targetMeanPrice",
        "debt_to_equity":        "debtToEquity",
        "held_percent_insiders": "heldPercentInsiders",
    }

    result: dict = {k: None for k in _KEY_MAP}

    try:
        info = yf.Ticker(ticker_upper).info
        for our_key, yf_key in _KEY_MAP.items():
            val = info.get(yf_key)
            if val is not None:
                try:
                    result[our_key] = float(val)
                except (TypeError, ValueError):
                    result[our_key] = None
    except Exception:
        pass  # Return all-None dict on any network or parsing error

    _save_fundamentals(ticker_upper, result)
    return ticker_upper, result


def fetch_fundamentals_for_tickers(tickers: list[str], max_workers: int = 8) -> dict[str, dict]:
    """
    Fetch fundamentals for a list of tickers in parallel using ThreadPoolExecutor.

    Parameters
    ----------
    tickers     : list[str] — list of ticker symbols (case-insensitive)
    max_workers : int — number of parallel threads (default 8)

    Returns
    -------
    dict mapping ticker (uppercase str) → fundamentals dict.
    Each fundamentals dict has the keys defined in _fetch_single_fundamentals.
    Tickers that fail silently return an all-None fundamentals dict.
    """
    results: dict[str, dict] = {}
    if not tickers:
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_fetch_single_fundamentals, t): t for t in tickers
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                ticker_upper, fund_dict = future.result()
                results[ticker_upper] = fund_dict
            except Exception:
                orig_ticker = futures[future].upper()
                results[orig_ticker] = {k: None for k in (
                    "revenue_growth", "earnings_growth", "gross_margins",
                    "total_cash", "total_debt", "free_cashflow", "operating_cashflow",
                    "short_percent_float", "shares_outstanding", "float_shares",
                    "target_mean_price", "debt_to_equity", "held_percent_insiders",
                )}

    return results


# ---------------------------------------------------------------------------
# yfinance screener
# ---------------------------------------------------------------------------
```

**Why:** This block introduces the full fundamentals persistence layer. `_save_fundamentals` / `_load_fundamentals` follow the exact same TTL cache pattern as `_save_screener_results` / `_load_screener_results` above them. `_fetch_single_fundamentals` is the per-ticker worker — it checks the cache first so repeated `enrich_with_fundamentals` calls in the same session are free. `fetch_fundamentals_for_tickers` is the parallel coordinator that will be called from both the data layer and the page.

---

#### 2d — Replace `_compute_speculation_score` with the blended scoring function

**Location:** The existing `_compute_speculation_score` function spans from the docstring `"""Compute a 0–100 speculation score…"""` through `return max(0, min(score, 100))`. Replace the **entire function** with the two functions below.

Find this exact text (the entire existing function):
```python
def _compute_speculation_score(quote: dict) -> int:
    """
    Compute a 0–100 speculation score for a single Yahoo Finance quote dict.

    Scoring components (each 0–20 points):
    1. Distance from 52-week low (higher = more upside momentum shown):
       fiftyTwoWeekLowChangePercent / 2, capped at 20
    2. Distance below 52-week high (more room to recover = more upside):
       If fiftyTwoWeekHighChangePercent < 0, score = min(abs(pct) * 20, 20); else 0
    3. Volume spike (10-day avg vs 3-month avg):
       If averageDailyVolume10Day and averageDailyVolume3Month both present:
       ratio = 10day / 3month; score = min((ratio - 1) * 10, 20) if ratio > 1 else 0
    4. Small cap premium (smaller = higher speculation potential):
       marketCap < 100M → 20 pts; < 300M → 15 pts; < 500M → 10 pts; < 2B → 5 pts; else 0
    5. Forward PE discount (low or N/A forward PE suggests growth not yet priced in):
       forwardPE present and 0 < forwardPE < 15 → 20 pts
       forwardPE present and 15 <= forwardPE < 25 → 10 pts
       forwardPE None or <= 0 → 5 pts (unprofitable = speculative by nature)
       else 0

    Returns an integer 0–100.
    """
    score = 0

    # Component 1: distance above 52-week low (momentum upward)
    low_chg_pct = quote.get("fiftyTwoWeekLowChangePercent")
    if low_chg_pct is not None:
        try:
            score += min(int(float(low_chg_pct) * 100 / 2), 20)
        except (TypeError, ValueError):
            pass

    # Component 2: distance below 52-week high (upside recovery room)
    high_chg_pct = quote.get("fiftyTwoWeekHighChangePercent")
    if high_chg_pct is not None:
        try:
            v = float(high_chg_pct)
            if v < 0:
                score += min(int(abs(v) * 100 * 20), 20)
        except (TypeError, ValueError):
            pass

    # Component 3: volume spike (10-day vs 3-month average)
    vol_10d = quote.get("averageDailyVolume10Day")
    vol_3m = quote.get("averageDailyVolume3Month")
    if vol_10d and vol_3m and vol_3m > 0:
        try:
            ratio = float(vol_10d) / float(vol_3m)
            if ratio > 1:
                score += min(int((ratio - 1) * 10), 20)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # Component 4: small cap premium
    market_cap = quote.get("marketCap")
    if market_cap is not None:
        try:
            mc = float(market_cap)
            if mc < 100_000_000:
                score += 20
            elif mc < 300_000_000:
                score += 15
            elif mc < 500_000_000:
                score += 10
            elif mc < 2_000_000_000:
                score += 5
        except (TypeError, ValueError):
            pass

    # Component 5: forward PE discount
    fwd_pe = quote.get("forwardPE")
    if fwd_pe is not None:
        try:
            pe = float(fwd_pe)
            if 0 < pe < 15:
                score += 20
            elif 15 <= pe < 25:
                score += 10
        except (TypeError, ValueError):
            pass
    else:
        score += 5  # unprofitable / no forward PE = inherently speculative

    return max(0, min(score, 100))
```

Replace with the following two functions (the first computes the technical sub-score with a 50-pt ceiling; the second computes the fundamental sub-score and merges both into a final score dict):

```python
def _compute_technical_score(quote: dict) -> dict[str, int]:
    """
    Compute the technical sub-score block (0–50 points total) for a single
    Yahoo Finance quote dict.

    Technical components (each 0–10 points, 5 components = 50 pts max):
    T1. Distance above 52-week low (momentum upward):
        fiftyTwoWeekLowChangePercent * 100 / 5, capped at 10
    T2. Distance below 52-week high (recovery room):
        If fiftyTwoWeekHighChangePercent < 0: min(abs(pct) * 100 * 10, 10); else 0
    T3. Volume spike (10-day vs 3-month):
        ratio = vol10d/vol3m; min((ratio - 1) * 5, 10) if ratio > 1 else 0
    T4. Small cap premium:
        marketCap < 100M → 10; < 300M → 8; < 500M → 5; < 2B → 3; else 0
    T5. Forward PE discount:
        0 < fwdPE < 15 → 10; 15 ≤ fwdPE < 25 → 5; None or ≤ 0 → 3; else 0

    Returns a dict:
        {
            "t1_52w_momentum": int,      # 0–10
            "t2_52w_recovery": int,      # 0–10
            "t3_volume_spike": int,      # 0–10
            "t4_small_cap":    int,      # 0–10
            "t5_fwd_pe":       int,      # 0–10
            "technical_total": int,      # 0–50
        }
    """
    t1 = t2 = t3 = t4 = t5 = 0

    # T1: 52-week low momentum
    low_chg = quote.get("fiftyTwoWeekLowChangePercent")
    if low_chg is not None:
        try:
            t1 = max(0, min(int(float(low_chg) * 100 / 5), 10))
        except (TypeError, ValueError):
            pass

    # T2: 52-week high recovery room
    high_chg = quote.get("fiftyTwoWeekHighChangePercent")
    if high_chg is not None:
        try:
            v = float(high_chg)
            if v < 0:
                t2 = min(int(abs(v) * 100 * 10), 10)
        except (TypeError, ValueError):
            pass

    # T3: volume spike
    vol_10d = quote.get("averageDailyVolume10Day")
    vol_3m = quote.get("averageDailyVolume3Month")
    if vol_10d and vol_3m and float(vol_3m) > 0:
        try:
            ratio = float(vol_10d) / float(vol_3m)
            if ratio > 1:
                t3 = min(int((ratio - 1) * 5), 10)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # T4: small cap premium
    market_cap = quote.get("marketCap")
    if market_cap is not None:
        try:
            mc = float(market_cap)
            if mc < 100_000_000:
                t4 = 10
            elif mc < 300_000_000:
                t4 = 8
            elif mc < 500_000_000:
                t4 = 5
            elif mc < 2_000_000_000:
                t4 = 3
        except (TypeError, ValueError):
            pass

    # T5: forward PE discount
    fwd_pe = quote.get("forwardPE")
    if fwd_pe is not None:
        try:
            pe = float(fwd_pe)
            if 0 < pe < 15:
                t5 = 10
            elif 15 <= pe < 25:
                t5 = 5
        except (TypeError, ValueError):
            pass
    else:
        t5 = 3  # no forward PE = inherently speculative

    tech_total = t1 + t2 + t3 + t4 + t5

    return {
        "t1_52w_momentum": t1,
        "t2_52w_recovery": t2,
        "t3_volume_spike": t3,
        "t4_small_cap":    t4,
        "t5_fwd_pe":       t5,
        "technical_total": tech_total,
    }


def _compute_fundamental_score(fundamentals: dict, current_price: Optional[float]) -> dict[str, int]:
    """
    Compute the fundamental sub-score block (0–50 points total).

    Requires a fundamentals dict as returned by _fetch_single_fundamentals.
    current_price is the regularMarketPrice from the screener quote dict (may be None).

    Fundamental components:
    F1. Revenue growth (0–15 pts):
        revenueGrowth >= 0.50 (50% YoY) → 15
        >= 0.25 → 10
        >= 0.10 → 5
        >= 0.00 → 2
        < 0 or None → 0
    F2. Cash runway (0–10 pts):
        Computed as: totalCash / abs(freeCashflow) in years (only when FCF < 0).
        If FCF >= 0 (cash-flow positive): → 10 (no burn risk)
        runway >= 2 years → 10
        runway >= 1 year  → 6
        runway >= 0.5 year → 2
        runway < 0.5 year  → 0  (also sets red_flag_cash_runway = True)
        FCF None or totalCash None → 5 (unknown)
    F3. Analyst target upside (0–10 pts):
        upside = (targetMeanPrice - currentPrice) / currentPrice
        upside >= 1.00 (100% upside) → 10
        upside >= 0.50 → 7
        upside >= 0.25 → 4
        upside >= 0.00 → 1
        upside < 0 or targetMeanPrice/currentPrice None → 0
    F4. Short squeeze potential (0–10 pts):
        shortPercentOfFloat >= 0.20 (20%) → 10
        >= 0.15 → 7
        >= 0.10 → 4
        >= 0.05 → 1
        < 0.05 or None → 0
    F5. Balance-sheet health / debt (0–5 pts):
        debtToEquity is None → 3 (unknown — micro-caps often lack this)
        debtToEquity <= 0.5 → 5
        <= 1.0 → 3
        <= 2.0 → 1
        > 2.0 → 0

    Returns a dict:
        {
            "f1_revenue_growth":    int,   # 0–15
            "f2_cash_runway":       int,   # 0–10
            "f3_analyst_upside":    int,   # 0–10
            "f4_short_squeeze":     int,   # 0–10
            "f5_debt_health":       int,   # 0–5
            "fundamental_total":    int,   # 0–50
            "cash_runway_years":    float | None,  # computed runway, None if not applicable
            "analyst_upside_pct":   float | None,  # computed upside as decimal, None if N/A
            "red_flag_cash_runway": bool,  # True if runway < 6 months
            "has_fundamentals":     bool,  # True (this dict always represents real fetch)
        }
    """
    f1 = f2 = f3 = f4 = f5 = 0
    cash_runway_years: Optional[float] = None
    analyst_upside_pct: Optional[float] = None
    red_flag_cash_runway = False

    # F1: revenue growth
    rev_growth = fundamentals.get("revenue_growth")
    if rev_growth is not None:
        try:
            rg = float(rev_growth)
            if rg >= 0.50:
                f1 = 15
            elif rg >= 0.25:
                f1 = 10
            elif rg >= 0.10:
                f1 = 5
            elif rg >= 0.00:
                f1 = 2
            else:
                f1 = 0
        except (TypeError, ValueError):
            pass

    # F2: cash runway
    total_cash = fundamentals.get("total_cash")
    fcf = fundamentals.get("free_cashflow")
    if fcf is not None and total_cash is not None:
        try:
            fcf_f = float(fcf)
            cash_f = float(total_cash)
            if fcf_f >= 0:
                # Cash-flow positive — no burn risk
                f2 = 10
                cash_runway_years = None  # infinite / not applicable
            else:
                burn_per_year = abs(fcf_f)
                if burn_per_year > 0:
                    cash_runway_years = cash_f / burn_per_year
                    if cash_runway_years >= 2.0:
                        f2 = 10
                    elif cash_runway_years >= 1.0:
                        f2 = 6
                    elif cash_runway_years >= 0.5:
                        f2 = 2
                    else:
                        f2 = 0
                        red_flag_cash_runway = True
                else:
                    f2 = 5
        except (TypeError, ValueError, ZeroDivisionError):
            f2 = 5
    else:
        f2 = 5  # unknown

    # F3: analyst target upside
    target_price = fundamentals.get("target_mean_price")
    if target_price is not None and current_price is not None:
        try:
            tp = float(target_price)
            cp = float(current_price)
            if cp > 0:
                analyst_upside_pct = (tp - cp) / cp
                if analyst_upside_pct >= 1.00:
                    f3 = 10
                elif analyst_upside_pct >= 0.50:
                    f3 = 7
                elif analyst_upside_pct >= 0.25:
                    f3 = 4
                elif analyst_upside_pct >= 0.00:
                    f3 = 1
                else:
                    f3 = 0
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # F4: short squeeze potential
    short_pct = fundamentals.get("short_percent_float")
    if short_pct is not None:
        try:
            sp = float(short_pct)
            if sp >= 0.20:
                f4 = 10
            elif sp >= 0.15:
                f4 = 7
            elif sp >= 0.10:
                f4 = 4
            elif sp >= 0.05:
                f4 = 1
            else:
                f4 = 0
        except (TypeError, ValueError):
            pass

    # F5: balance-sheet / debt
    dte = fundamentals.get("debt_to_equity")
    if dte is None:
        f5 = 3  # unknown — micro-caps often lack this; give moderate score
    else:
        try:
            d = float(dte)
            if d <= 0.5:
                f5 = 5
            elif d <= 1.0:
                f5 = 3
            elif d <= 2.0:
                f5 = 1
            else:
                f5 = 0
        except (TypeError, ValueError):
            f5 = 3

    fund_total = f1 + f2 + f3 + f4 + f5

    return {
        "f1_revenue_growth":    f1,
        "f2_cash_runway":       f2,
        "f3_analyst_upside":    f3,
        "f4_short_squeeze":     f4,
        "f5_debt_health":       f5,
        "fundamental_total":    fund_total,
        "cash_runway_years":    cash_runway_years,
        "analyst_upside_pct":   analyst_upside_pct,
        "red_flag_cash_runway": red_flag_cash_runway,
        "has_fundamentals":     True,
    }


def _no_fundamentals_score() -> dict[str, int]:
    """
    Return a placeholder fundamental score dict for tickers that were not
    enriched (not in top N or fetch failed). All fundamental components are 0.
    The technical score will be scaled to 0–100 for these tickers.
    """
    return {
        "f1_revenue_growth":    0,
        "f2_cash_runway":       0,
        "f3_analyst_upside":    0,
        "f4_short_squeeze":     0,
        "f5_debt_health":       0,
        "fundamental_total":    0,
        "cash_runway_years":    None,
        "analyst_upside_pct":   None,
        "red_flag_cash_runway": False,
        "has_fundamentals":     False,
    }


def _compute_speculation_score(quote: dict) -> int:
    """
    Backwards-compatible wrapper used during the initial technical scoring pass
    (before fundamentals are available). Returns the technical sub-score scaled
    to 0–100 so the initial sort order is meaningful.

    Internally calls _compute_technical_score and doubles the result.
    """
    tech = _compute_technical_score(quote)
    # Scale 0–50 → 0–100
    return max(0, min(tech["technical_total"] * 2, 100))
```

**Why:** The original `_compute_speculation_score` is replaced with a modular design. `_compute_technical_score` produces the 50-pt technical block (same logic as before, halved per-component). `_compute_fundamental_score` produces the 50-pt fundamental block using the new `info` keys. `_no_fundamentals_score` is used for tickers outside the top-N enrichment window so the UI can visually distinguish "technicals only" entries. The backwards-compatible `_compute_speculation_score` wrapper preserves the existing call site in `_quotes_to_dataframe` and ensures the initial sort order is sensible before fundamentals are loaded.

---

#### 2e — Add `enrich_with_fundamentals` public function and update `_quotes_to_dataframe` return columns

**Location:** At the very end of `data/screener.py`, after the existing `_quotes_to_dataframe` function. Insert a new public function `enrich_with_fundamentals` and also modify the return value of `_quotes_to_dataframe` to include a score breakdown column.

**Sub-change 2e-i:** First, modify `_quotes_to_dataframe` to add the `score_breakdown` column. Find this exact block inside `_quotes_to_dataframe`:

```python
        score = _compute_speculation_score(q)
        rows.append({
            "ticker": ticker.upper(),
            "name": q.get("shortName") or q.get("longName") or ticker,
            "price": q.get("regularMarketPrice"),
            "change_pct": q.get("regularMarketChangePercent"),
            "market_cap": q.get("marketCap"),
            "volume_3m": q.get("averageDailyVolume3Month"),
            "volume_10d": q.get("averageDailyVolume10Day"),
            "week52_high": q.get("fiftyTwoWeekHigh"),
            "week52_low": q.get("fiftyTwoWeekLow"),
            "week52_high_chg_pct": q.get("fiftyTwoWeekHighChangePercent"),
            "week52_low_chg_pct": q.get("fiftyTwoWeekLowChangePercent"),
            "forward_pe": q.get("forwardPE"),
            "eps_ttm": q.get("epsTrailingTwelveMonths"),
            "speculation_score": score,
        })
```

Replace with:

```python
        tech_breakdown = _compute_technical_score(q)
        no_fund = _no_fundamentals_score()
        # Initial score: technical total scaled to 0-100 (fundamentals not yet fetched)
        initial_score = max(0, min(tech_breakdown["technical_total"] * 2, 100))
        rows.append({
            "ticker": ticker.upper(),
            "name": q.get("shortName") or q.get("longName") or ticker,
            "price": q.get("regularMarketPrice"),
            "change_pct": q.get("regularMarketChangePercent"),
            "market_cap": q.get("marketCap"),
            "volume_3m": q.get("averageDailyVolume3Month"),
            "volume_10d": q.get("averageDailyVolume10Day"),
            "week52_high": q.get("fiftyTwoWeekHigh"),
            "week52_low": q.get("fiftyTwoWeekLow"),
            "week52_high_chg_pct": q.get("fiftyTwoWeekHighChangePercent"),
            "week52_low_chg_pct": q.get("fiftyTwoWeekLowChangePercent"),
            "forward_pe": q.get("forwardPE"),
            "eps_ttm": q.get("epsTrailingTwelveMonths"),
            "speculation_score": initial_score,
            # Fundamental columns — populated as None until enrich_with_fundamentals() is called
            "revenue_growth":        None,
            "cash_runway_years":     None,
            "short_percent_float":   None,
            "analyst_upside_pct":    None,
            "debt_to_equity":        None,
            "held_percent_insiders": None,
            "red_flag_cash_runway":  False,
            "has_fundamentals":      False,
            # Score breakdown as JSON string so it survives DataFrame serialization
            "score_breakdown":       json.dumps({**tech_breakdown, **no_fund}),
        })
```

**Sub-change 2e-ii:** Now add the `enrich_with_fundamentals` function at the very end of the file, after `_quotes_to_dataframe`. Append the following block at the end of `data/screener.py`:

```python


def enrich_with_fundamentals(
    df: pd.DataFrame,
    top_n: int = _DEFAULT_ENRICH_TOP_N,
) -> pd.DataFrame:
    """
    Enrich the top N rows of the screener DataFrame (by current speculation_score)
    with fundamental data fetched in parallel via yf.Ticker(symbol).info.

    For tickers outside the top N, fundamental columns remain None and
    has_fundamentals remains False.

    Recomputes speculation_score for enriched rows as:
        technical_total (0–50) + fundamental_total (0–50) = 0–100 blended score

    For non-enriched rows, speculation_score remains the technical-scaled score.

    Parameters
    ----------
    df    : pd.DataFrame — output of run_screener(), with score_breakdown column
    top_n : int — how many top-ranked tickers to fetch fundamentals for

    Returns
    -------
    pd.DataFrame — same columns as input plus populated fundamental columns for top N rows.
    The DataFrame is re-sorted by speculation_score descending.
    """
    if df.empty:
        return df

    df = df.copy()
    top_tickers = df.head(top_n)["ticker"].tolist()

    fund_map = fetch_fundamentals_for_tickers(top_tickers)

    for idx, row in df.iterrows():
        ticker = str(row["ticker"]).upper()
        if ticker not in fund_map:
            continue

        fund_data = fund_map[ticker]
        current_price = row.get("price")

        # Parse existing technical breakdown from the JSON string
        try:
            breakdown = json.loads(row.get("score_breakdown") or "{}")
        except (json.JSONDecodeError, TypeError):
            breakdown = {}

        fund_score = _compute_fundamental_score(fund_data, current_price)

        # Blended score
        tech_total = breakdown.get("technical_total", 0)
        fund_total = fund_score["fundamental_total"]
        blended_score = max(0, min(tech_total + fund_total, 100))

        df.at[idx, "speculation_score"]    = blended_score
        df.at[idx, "revenue_growth"]       = fund_data.get("revenue_growth")
        df.at[idx, "cash_runway_years"]    = fund_score["cash_runway_years"]
        df.at[idx, "short_percent_float"]  = fund_data.get("short_percent_float")
        df.at[idx, "analyst_upside_pct"]   = fund_score["analyst_upside_pct"]
        df.at[idx, "debt_to_equity"]       = fund_data.get("debt_to_equity")
        df.at[idx, "held_percent_insiders"]= fund_data.get("held_percent_insiders")
        df.at[idx, "red_flag_cash_runway"] = fund_score["red_flag_cash_runway"]
        df.at[idx, "has_fundamentals"]     = True
        df.at[idx, "score_breakdown"]      = json.dumps({
            **breakdown,
            **fund_score,
        })

    df = df.sort_values("speculation_score", ascending=False).reset_index(drop=True)
    return df
```

**Why:** `enrich_with_fundamentals` is the public entrypoint the page calls after `run_screener`. It limits enrichment to the top N to control latency (fetching .info for 100 tickers at ~1-2s each even with 8 workers would be 12+ seconds; top-25 with 8 workers is ~6-8s on a cold cache). The function re-sorts the DataFrame so blended-score order is correct after enrichment. The `score_breakdown` column stores a JSON dict so the page can display per-component breakdowns without re-computing.

---

### Step 3: Extend `data/screener_agent.py` — updated prompt with fundamentals block

**File:** `C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard\data\screener_agent.py`

This step makes **two changes** to the existing file.

---

#### 3a — Replace `_PROMPT_TEMPLATE` with the extended version

**Location:** The `_PROMPT_TEMPLATE` constant, which starts at the line `_PROMPT_TEMPLATE = """\` and ends at the closing `"""`. Replace the **entire constant assignment** (from `_PROMPT_TEMPLATE = """\` through the closing `"""`) with:

```python
_PROMPT_TEMPLATE = """\
You are a speculative equity risk analyst. Your task is to assess the risk/reward
profile of a small or micro-cap stock based on the quantitative metrics below.
These are HIGH-RISK speculative stocks. Your analysis must be rigorous, honest about
risks, and use the exact numbers provided.

=== TECHNICAL METRICS ===
Ticker:                      {ticker}
Company Name:                {name}
Current Price:               ${price}
Market Cap:                  {market_cap}
52-Week High:                ${week52_high}  (current is {week52_high_chg_pct}% from high)
52-Week Low:                 ${week52_low}   (current is {week52_low_chg_pct}% above low)
Avg Daily Volume (3M):       {volume_3m}
Avg Daily Volume (10D):      {volume_10d}
Volume Spike Ratio (10D/3M): {volume_spike}
Forward PE:                  {forward_pe}
EPS (TTM):                   {eps_ttm}
Speculation Score:           {speculation_score}/100  (technical sub-score: {technical_total}/50, fundamental sub-score: {fundamental_total}/50)

=== FUNDAMENTAL METRICS ===
Revenue Growth (YoY):        {revenue_growth}
Gross Margin:                {gross_margins}
Free Cash Flow:              {free_cashflow}
Cash Runway:                 {cash_runway}
Short % of Float:            {short_percent_float}
Analyst Mean Price Target:   {target_mean_price}  (implied upside: {analyst_upside})
Debt / Equity:               {debt_to_equity}
Insider Ownership:           {held_percent_insiders}
Has Fundamentals Data:       {has_fundamentals}

=== YOUR ANALYSIS TASK ===
Analyze this speculative stock and produce a structured risk assessment. Be specific
— reference the actual numbers above. Do not invent data not present in the metrics.

IMPORTANT GUIDANCE FOR FUNDAMENTAL SIGNALS:
- If cash_runway < 6 months, this is a CRITICAL red flag — dilution or bankruptcy risk is high. Always include this in red_flags.
- If short_percent_float > 20%, explicitly address squeeze potential AND covering pressure risk.
- If analyst_upside > 100%, assess whether the target is realistic given the cash/revenue data.
- If revenue_growth is N/A or has_fundamentals is False, note that fundamental data was unavailable and increase uncertainty in your assessment.
- Use insider ownership to gauge conviction: > 20% insider ownership is bullish for small-caps; < 2% is a warning sign.

For risk_score: rate overall risk from 1 (extremely low risk) to 10 (extremely high risk).
For upside_thesis: what specific factors in the numbers above suggest potential upside? 2-3 sentences. Reference specific numbers.
For key_risks: list 3-5 specific risk factors based on the data.
For red_flags: list any concrete warning signs visible in these metrics — especially cash runway < 6mo, negative FCF with thin cash, or extreme short interest. If none, return an empty list.
For verdict: assign exactly one of these labels:
  "lottery ticket" — extremely speculative, could go 10x or zero
  "speculative buy" — elevated risk but metrics suggest real upside potential
  "hold/watch" — not actionable yet, monitor for catalyst
  "avoid" — risk/reward unfavourable based on these metrics

Rules:
- Be specific: reference actual numbers from both metric blocks
- verdict must be exactly one of the four labels listed above
- key_risks and red_flags must be lists of plain strings (no sub-objects)
- risk_score must be an integer 1-10
- Respond ONLY with a single JSON object (no markdown fences, no preamble, no trailing text)

{{
  "risk_score": <integer 1-10>,
  "upside_thesis": "<2-3 sentence specific upside case>",
  "key_risks": ["<risk 1>", "<risk 2>", "<risk 3>"],
  "red_flags": ["<flag 1>"],
  "verdict": "<lottery ticket|speculative buy|hold/watch|avoid>"
}}
"""
```

**Why:** The new prompt includes a `=== FUNDAMENTAL METRICS ===` block with all 8 fundamental signals. The `IMPORTANT GUIDANCE FOR FUNDAMENTAL SIGNALS` section gives Gemini explicit rules for cash runway and short interest so its analysis is grounded in the real numbers. The `{technical_total}/50` and `{fundamental_total}/50` score split helps the model understand what drove the total score.

---

#### 3b — Replace `_build_prompt` with the extended version

**Location:** The entire `_build_prompt` function, from `def _build_prompt(metrics: dict) -> str:` through the closing `return _PROMPT_TEMPLATE.format(...)` call (including the closing parenthesis). Replace it with:

```python
def _build_prompt(metrics: dict) -> str:
    """
    Build the Gemini prompt string from a metrics dict.

    metrics may be a dict from a screener DataFrame row (which contains
    the fundamental columns populated by enrich_with_fundamentals) or
    a bare dict with only technical keys (for stocks not enriched with
    fundamentals, in which case fundamental fields will be 'N/A').
    """
    vol_3m = metrics.get("volume_3m")
    vol_10d = metrics.get("volume_10d")

    if vol_3m and vol_10d and float(vol_3m) > 0:
        try:
            spike = f"{float(vol_10d) / float(vol_3m):.2f}x"
        except (TypeError, ValueError, ZeroDivisionError):
            spike = "N/A"
    else:
        spike = "N/A"

    high_chg = metrics.get("week52_high_chg_pct")
    low_chg = metrics.get("week52_low_chg_pct")

    # Parse score breakdown for sub-scores
    import json as _json
    breakdown_raw = metrics.get("score_breakdown") or "{}"
    try:
        breakdown = _json.loads(breakdown_raw) if isinstance(breakdown_raw, str) else breakdown_raw
    except (TypeError, ValueError):
        breakdown = {}

    technical_total = breakdown.get("technical_total", metrics.get("speculation_score", 0))
    fundamental_total = breakdown.get("fundamental_total", 0)

    # Fundamental fields — default to "N/A" if not present
    rev_growth = metrics.get("revenue_growth")
    rev_growth_str = f"{float(rev_growth) * 100:.1f}%" if rev_growth is not None else "N/A"

    gross_margins = None
    # gross_margins may come from the fundamentals dict embedded in score_breakdown
    # or from a future direct column; check breakdown dict
    gm_raw = breakdown.get("gross_margins") if breakdown else None
    if gm_raw is None:
        gm_raw = metrics.get("gross_margins")
    gross_margins_str = f"{float(gm_raw) * 100:.1f}%" if gm_raw is not None else "N/A"

    free_cashflow_raw = breakdown.get("free_cashflow") if breakdown else None
    if free_cashflow_raw is None:
        free_cashflow_raw = metrics.get("free_cashflow")
    free_cashflow_str = _fmt_market_cap(free_cashflow_raw) if free_cashflow_raw is not None else "N/A"

    cash_runway_years = metrics.get("cash_runway_years")
    if cash_runway_years is None:
        fcf_raw = breakdown.get("free_cashflow") if breakdown else None
        if fcf_raw is not None and float(fcf_raw) >= 0:
            cash_runway_str = "FCF positive (no burn)"
        else:
            cash_runway_str = "N/A"
    else:
        try:
            cash_runway_str = f"{float(cash_runway_years):.1f} years"
        except (TypeError, ValueError):
            cash_runway_str = "N/A"

    short_pct = metrics.get("short_percent_float")
    short_pct_str = f"{float(short_pct) * 100:.1f}%" if short_pct is not None else "N/A"

    target_price = breakdown.get("target_mean_price") if breakdown else None
    if target_price is None:
        target_price = metrics.get("target_mean_price")
    target_price_str = f"${float(target_price):.2f}" if target_price is not None else "N/A"

    analyst_upside = metrics.get("analyst_upside_pct")
    analyst_upside_str = f"{float(analyst_upside) * 100:.1f}%" if analyst_upside is not None else "N/A"

    dte = metrics.get("debt_to_equity")
    dte_str = f"{float(dte):.2f}" if dte is not None else "N/A"

    insider_pct = metrics.get("held_percent_insiders")
    insider_str = f"{float(insider_pct) * 100:.1f}%" if insider_pct is not None else "N/A"

    has_fund = metrics.get("has_fundamentals", False)
    has_fund_str = "Yes" if has_fund else "No (technicals only — fundamental data unavailable)"

    return _PROMPT_TEMPLATE.format(
        ticker=str(metrics.get("ticker", "")).upper(),
        name=str(metrics.get("name", "N/A")),
        price=_fmt_optional(metrics.get("price")),
        market_cap=_fmt_market_cap(metrics.get("market_cap")),
        week52_high=_fmt_optional(metrics.get("week52_high")),
        week52_high_chg_pct=_fmt_optional(high_chg, ".1f"),
        week52_low=_fmt_optional(metrics.get("week52_low")),
        week52_low_chg_pct=_fmt_optional(low_chg, ".1f"),
        volume_3m=_fmt_optional(vol_3m, ",.0f") if vol_3m is not None else "N/A",
        volume_10d=_fmt_optional(vol_10d, ",.0f") if vol_10d is not None else "N/A",
        volume_spike=spike,
        forward_pe=_fmt_optional(metrics.get("forward_pe")),
        eps_ttm=_fmt_optional(metrics.get("eps_ttm")),
        speculation_score=metrics.get("speculation_score", 0),
        technical_total=technical_total,
        fundamental_total=fundamental_total,
        revenue_growth=rev_growth_str,
        gross_margins=gross_margins_str,
        free_cashflow=free_cashflow_str,
        cash_runway=cash_runway_str,
        short_percent_float=short_pct_str,
        target_mean_price=target_price_str,
        analyst_upside=analyst_upside_str,
        debt_to_equity=dte_str,
        held_percent_insiders=insider_str,
        has_fundamentals=has_fund_str,
    )
```

**Why:** The new `_build_prompt` reads all the fundamental columns from the metrics dict (populated by `enrich_with_fundamentals`) and formats them as human-readable strings for the prompt. For stocks without fundamentals (`has_fundamentals=False`), all fundamental fields display as "N/A" and the prompt explicitly states "technicals only — fundamental data unavailable" so Gemini knows to express higher uncertainty.

Note: `gross_margins`, `free_cashflow`, and `target_mean_price` are NOT stored as top-level DataFrame columns (to avoid schema bloat), so the function looks them up from the `score_breakdown` JSON dict where `_compute_fundamental_score` does not store them. These three values are not needed for scoring — they are prompt-context only. However, the `score_breakdown` dict as currently designed in Step 2d does not include them either. To resolve this, the implementer must also update `_fetch_single_fundamentals` in `data/screener.py` to store `gross_margins`, `free_cashflow`, and `target_mean_price` in the fundamentals dict (they are already fetched). Then in `enrich_with_fundamentals`, when building the `score_breakdown` JSON, also include:
```python
"gross_margins":    fund_data.get("gross_margins"),
"free_cashflow":    fund_data.get("free_cashflow"),
"target_mean_price": fund_data.get("target_mean_price"),
```
Add these three lines inside the `score_breakdown` JSON construction in `enrich_with_fundamentals` in `data/screener.py`, specifically inside the `json.dumps({...})` call at `df.at[idx, "score_breakdown"] = json.dumps({...})`. The full updated assignment for that line is:

```python
        df.at[idx, "score_breakdown"]      = json.dumps({
            **breakdown,
            **fund_score,
            "gross_margins":     fund_data.get("gross_margins"),
            "free_cashflow":     fund_data.get("free_cashflow"),
            "target_mean_price": fund_data.get("target_mean_price"),
        })
```

This ensures `_build_prompt` can access these three values from the `score_breakdown` dict for prompt construction.

---

### Step 4: Update `pages/12_screener.py`

**File:** `C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard\pages\12_screener.py`

This step makes **seven targeted changes** to the existing page file.

---

#### 4a — Add `enrich_with_fundamentals` to the import from `data.screener`

**Location:** Line 9 of the file, which currently reads:
```python
from data.screener import run_screener, SECTOR_OPTIONS, _MICRO_CAP_MAX, _SMALL_CAP_MAX
```

Replace with:
```python
from data.screener import run_screener, enrich_with_fundamentals, SECTOR_OPTIONS, _MICRO_CAP_MAX, _SMALL_CAP_MAX
```

**Why:** The page needs to call `enrich_with_fundamentals` to trigger the parallel fundamentals fetch after running the base screener.

---

#### 4b — Add a new constant for the enrich-N options

**Location:** In the `# Constants` section, after the line `_DEFAULT_MAX_BATCH = 5  # max stocks for batch analysis (Pro quota protection)`. Find this exact text:

```python
_DEFAULT_MAX_BATCH = 5  # max stocks for batch analysis (Pro quota protection)
```

Replace with:
```python
_DEFAULT_MAX_BATCH = 5  # max stocks for batch analysis (Pro quota protection)

_ENRICH_N_OPTIONS = {
    "Top 10":  10,
    "Top 25":  25,
    "Top 50":  50,
}
_DEFAULT_ENRICH_N = "Top 25"
```

**Why:** These options populate the "Enrich top N" selectbox added in Step 4d. Using a dict means the selectbox shows human-readable labels while mapping to integer values.

---

#### 4c — Add helper functions for formatting fundamental values

**Location:** In the `# Helpers` section, after the existing `_risk_score_color` function (which ends with `return "#00c853"`), before `_build_price_chart`. Find this exact text:

```python
def _risk_score_color(rs: int) -> str:
    """Return a hex color for a Gemini risk score 1-10."""
    if rs >= 8:
        return "#ff1744"
    if rs >= 5:
        return "#ffd600"
    return "#00c853"
```

Replace with:
```python
def _risk_score_color(rs: int) -> str:
    """Return a hex color for a Gemini risk score 1-10."""
    if rs >= 8:
        return "#ff1744"
    if rs >= 5:
        return "#ffd600"
    return "#00c853"


def _fmt_runway(years) -> str:
    """Format cash runway years as a human-readable string."""
    if years is None or (isinstance(years, float) and pd.isna(years)):
        return "N/A"
    try:
        y = float(years)
        if y >= 100:
            return "FCF+ (no burn)"
        return f"{y:.1f} yr"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_growth(val) -> str:
    """Format a decimal growth rate as a percentage string (e.g. 0.35 → '+35.0%')."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "N/A"
    try:
        v = float(val)
        sign = "+" if v >= 0 else ""
        return f"{sign}{v * 100:.1f}%"
    except (TypeError, ValueError):
        return "N/A"
```

**Why:** `_fmt_runway` and `_fmt_growth` are used in the updated results table and expander to display fundamental values in a consistent format matching the existing `_fmt_pct` / `_fmt_price` helpers.

---

#### 4d — Add the enrich-N control and enrichment trigger to `_render_filters`

**Location:** Inside `_render_filters`, after the `return min_cap, max_cap, min_price, max_price, min_volume, sector` line. The entire function signature and return type must be updated as well.

Find this exact text (the function signature and return statement):
```python
def _render_filters() -> tuple[int, int, float, float, int, str]:
    """
    Render the filter controls and return the current filter values as a tuple:
    (min_market_cap, max_market_cap, min_price, max_price, min_volume, sector)
    """
```

Replace with:
```python
def _render_filters() -> tuple[int, int, float, float, int, str, int]:
    """
    Render the filter controls and return the current filter values as a tuple:
    (min_market_cap, max_market_cap, min_price, max_price, min_volume, sector, enrich_top_n)
    """
```

Then find the return statement at the end of `_render_filters`:
```python
    return min_cap, max_cap, min_price, max_price, min_volume, sector
```

Replace with:
```python
    with col4:
        enrich_label = st.selectbox(
            "Enrich with Fundamentals (top N)",
            options=list(_ENRICH_N_OPTIONS.keys()),
            index=list(_ENRICH_N_OPTIONS.keys()).index(_DEFAULT_ENRICH_N),
            key="screener_enrich_n",
            help=(
                "Fetches yfinance .info for the top N candidates by technical score "
                "to compute revenue growth, cash runway, analyst targets, short interest, "
                "and balance-sheet health. Uses a 24h SQLite cache."
            ),
        )
        enrich_top_n = _ENRICH_N_OPTIONS[enrich_label]

    return min_cap, max_cap, min_price, max_price, min_volume, sector, enrich_top_n
```

**Why:** The enrich-N selectbox sits in `col4` of the existing 4-column filter layout (it replaces the sector selectbox's column). However, `col4` already holds the Sector selectbox. Since we need a 5th control, we must change the layout to 5 columns. Find and replace the column layout line inside `_render_filters`:

Find:
```python
    col1, col2, col3, col4 = st.columns(4)
```

Replace with:
```python
    col1, col2, col3, col4, col5 = st.columns(5)
```

Then the sector selectbox (which currently uses `with col4:`) must be renumbered. Find this exact block inside `_render_filters`:
```python
    with col4:
        sector_display = ["All Sectors"] + [s for s in SECTOR_OPTIONS if s]
        sector_sel = st.selectbox(
            "Sector",
            options=sector_display,
            index=0,
            key="screener_sector",
        )
        sector = "" if sector_sel == "All Sectors" else sector_sel
```

Replace with:
```python
    with col4:
        sector_display = ["All Sectors"] + [s for s in SECTOR_OPTIONS if s]
        sector_sel = st.selectbox(
            "Sector",
            options=sector_display,
            index=0,
            key="screener_sector",
        )
        sector = "" if sector_sel == "All Sectors" else sector_sel

    with col5:
        enrich_label = st.selectbox(
            "Enrich with Fundamentals (top N)",
            options=list(_ENRICH_N_OPTIONS.keys()),
            index=list(_ENRICH_N_OPTIONS.keys()).index(_DEFAULT_ENRICH_N),
            key="screener_enrich_n",
            help=(
                "Fetches yfinance .info for the top N candidates by technical score "
                "to compute revenue growth, cash runway, analyst targets, short interest, "
                "and balance-sheet health. Uses a 24h SQLite cache."
            ),
        )
        enrich_top_n = _ENRICH_N_OPTIONS[enrich_label]
```

And update the return statement at the very bottom of `_render_filters` (after the col5 block):
```python
    return min_cap, max_cap, min_price, max_price, min_volume, sector, enrich_top_n
```

**Why:** The Sector selectbox stays in `col4`; the Enrich-N selectbox goes into a new `col5`. The filter tuple grows by one element (`enrich_top_n`), which must be propagated in the `main()` function (Step 4f).

---

#### 4e — Update `_render_results_table` to include fundamental columns

**Location:** Inside `_render_results_table`, replace the existing `display_df` construction block. Find this exact block:

```python
    display_df = pd.DataFrame({
        "Ticker":       df["ticker"],
        "Name":         df["name"],
        "Price":        df["price"].apply(_fmt_price),
        "Day Chg%":     df["change_pct"].apply(_fmt_pct),
        "Market Cap":   df["market_cap"].apply(_fmt_market_cap),
        "Vol (3M Avg)": df["volume_3m"].apply(_fmt_volume),
        "Vol Spike":    df.apply(
            lambda r: f"{float(r['volume_10d']) / float(r['volume_3m']):.1f}x"
            if r.get("volume_10d") and r.get("volume_3m") and float(r.get("volume_3m", 0)) > 0
            else "N/A",
            axis=1,
        ),
        "52W High Chg": df["week52_high_chg_pct"].apply(_fmt_pct),
        "52W Low Chg":  df["week52_low_chg_pct"].apply(_fmt_pct),
        "Fwd PE":       df["forward_pe"].apply(
            lambda v: f"{float(v):.1f}" if v is not None and not pd.isna(v) else "N/A"
        ),
        "EPS (TTM)":    df["eps_ttm"].apply(
            lambda v: f"{float(v):.2f}" if v is not None and not pd.isna(v) else "N/A"
        ),
        "Spec Score":   df["speculation_score"],
    })
```

Replace with:
```python
    # Build base columns
    display_data = {
        "Ticker":       df["ticker"],
        "Name":         df["name"],
        "Price":        df["price"].apply(_fmt_price),
        "Day Chg%":     df["change_pct"].apply(_fmt_pct),
        "Market Cap":   df["market_cap"].apply(_fmt_market_cap),
        "Vol (3M Avg)": df["volume_3m"].apply(_fmt_volume),
        "Vol Spike":    df.apply(
            lambda r: f"{float(r['volume_10d']) / float(r['volume_3m']):.1f}x"
            if r.get("volume_10d") and r.get("volume_3m") and float(r.get("volume_3m", 0)) > 0
            else "N/A",
            axis=1,
        ),
        "52W High Chg": df["week52_high_chg_pct"].apply(_fmt_pct),
        "52W Low Chg":  df["week52_low_chg_pct"].apply(_fmt_pct),
        "Fwd PE":       df["forward_pe"].apply(
            lambda v: f"{float(v):.1f}" if v is not None and not pd.isna(v) else "N/A"
        ),
        "EPS (TTM)":    df["eps_ttm"].apply(
            lambda v: f"{float(v):.2f}" if v is not None and not pd.isna(v) else "N/A"
        ),
        "Spec Score":   df["speculation_score"],
    }

    # Append fundamental columns if any rows have been enriched
    if "has_fundamentals" in df.columns and df["has_fundamentals"].any():
        display_data["Rev Growth"] = df["revenue_growth"].apply(_fmt_growth)
        display_data["Cash Runway"] = df["cash_runway_years"].apply(_fmt_runway)
        display_data["Short % Flt"] = df["short_percent_float"].apply(
            lambda v: f"{float(v) * 100:.1f}%" if v is not None and not pd.isna(v) else "N/A"
        )
        display_data["Analyst Upside"] = df["analyst_upside_pct"].apply(
            lambda v: f"+{float(v) * 100:.0f}%" if v is not None and not pd.isna(v) and float(v) >= 0
            else (f"{float(v) * 100:.0f}%" if v is not None and not pd.isna(v) else "N/A")
        )
        display_data["Has Fundamentals"] = df["has_fundamentals"].apply(
            lambda v: "Yes" if v else "Tech only"
        )

    display_df = pd.DataFrame(display_data)

    st.dataframe(display_df, use_container_width=True, hide_index=True)

    # Show cash-runway warning summary if any red flags present
    if "red_flag_cash_runway" in df.columns:
        danger_tickers = df[df["red_flag_cash_runway"] == True]["ticker"].tolist()
        if danger_tickers:
            st.warning(
                f"**Cash Runway Warning:** The following tickers have < 6 months of cash runway "
                f"based on current FCF burn rate: {', '.join(danger_tickers)}. "
                "Dilution or liquidity risk is elevated."
            )
```

Note: Remove the standalone `st.dataframe(display_df, use_container_width=True, hide_index=True)` line that currently follows the `display_df` construction in the original function — the replacement block above already includes it.

**Why:** Fundamental columns are only appended when at least one row has been enriched (`has_fundamentals` is True for at least one row). This prevents NaN-only columns from appearing on the first load before enrichment. The cash-runway warning banner provides a prominent visual alert for the most critical red flag.

---

#### 4f — Update `_render_stock_detail` to show score breakdown and fundamentals section

**Location:** Inside `_render_stock_detail`, after the `mc5.metric(...)` block and before `st.markdown("---")`. Find this exact text:

```python
        mc5.metric(
            "Spec Score",
            f"{score}/100",
            help="Speculation score 0-100: higher = more speculative (higher risk AND higher potential upside)",
        )

        st.markdown("---")
```

Replace with:
```python
        mc5.metric(
            "Spec Score",
            f"{score}/100",
            help="Speculation score 0-100: higher = more speculative (higher risk AND higher potential upside)",
        )

        # Score breakdown
        import json as _json
        breakdown_raw = row.get("score_breakdown")
        if breakdown_raw:
            try:
                bd = _json.loads(breakdown_raw) if isinstance(breakdown_raw, str) else breakdown_raw
            except (TypeError, ValueError):
                bd = {}
            if bd:
                tech_total = bd.get("technical_total", 0)
                fund_total = bd.get("fundamental_total", 0)
                has_fund = bd.get("has_fundamentals", False)
                with st.expander("Score Breakdown", expanded=False):
                    sb_col1, sb_col2 = st.columns(2)
                    with sb_col1:
                        st.markdown(f"**Technical Sub-score: {tech_total}/50**")
                        st.markdown(f"- 52W Momentum: {bd.get('t1_52w_momentum', 0)}/10")
                        st.markdown(f"- 52W Recovery Room: {bd.get('t2_52w_recovery', 0)}/10")
                        st.markdown(f"- Volume Spike: {bd.get('t3_volume_spike', 0)}/10")
                        st.markdown(f"- Small Cap Premium: {bd.get('t4_small_cap', 0)}/10")
                        st.markdown(f"- Forward PE Discount: {bd.get('t5_fwd_pe', 0)}/10")
                    with sb_col2:
                        if has_fund:
                            st.markdown(f"**Fundamental Sub-score: {fund_total}/50**")
                            st.markdown(f"- Revenue Growth: {bd.get('f1_revenue_growth', 0)}/15")
                            st.markdown(f"- Cash Runway: {bd.get('f2_cash_runway', 0)}/10")
                            st.markdown(f"- Analyst Upside: {bd.get('f3_analyst_upside', 0)}/10")
                            st.markdown(f"- Short Squeeze: {bd.get('f4_short_squeeze', 0)}/10")
                            st.markdown(f"- Debt Health: {bd.get('f5_debt_health', 0)}/5")
                        else:
                            st.markdown("**Fundamentals: Not yet enriched**")
                            st.caption("Score shown is technical-only (×2 scaling). "
                                       "Run enrichment to get the blended 100-pt score.")

        # Cash runway warning badge
        if row.get("red_flag_cash_runway"):
            cash_yr = row.get("cash_runway_years")
            runway_str = f"{float(cash_yr):.1f} yr" if cash_yr is not None else "< 6 mo"
            st.error(
                f"**CASH RUNWAY WARNING ({ticker}):** Estimated cash runway is {runway_str}. "
                "Severe dilution or insolvency risk. Review latest 10-Q before investing."
            )

        # Fundamentals detail section (only when enriched)
        if row.get("has_fundamentals"):
            with st.expander("Fundamentals Detail", expanded=False):
                fd1, fd2, fd3, fd4 = st.columns(4)
                rev_g = row.get("revenue_growth")
                fd1.metric("Rev Growth (YoY)", _fmt_growth(rev_g))
                runway = row.get("cash_runway_years")
                fd2.metric("Cash Runway", _fmt_runway(runway))
                short_pct = row.get("short_percent_float")
                fd3.metric(
                    "Short % Float",
                    f"{float(short_pct) * 100:.1f}%" if short_pct is not None else "N/A",
                )
                analyst_up = row.get("analyst_upside_pct")
                fd4.metric(
                    "Analyst Upside",
                    f"+{float(analyst_up) * 100:.0f}%" if analyst_up is not None and float(analyst_up) >= 0
                    else (f"{float(analyst_up) * 100:.0f}%" if analyst_up is not None else "N/A"),
                )
                fd5, fd6, _, _ = st.columns(4)
                dte = row.get("debt_to_equity")
                fd5.metric("Debt/Equity", f"{float(dte):.2f}" if dte is not None else "N/A")
                insider = row.get("held_percent_insiders")
                fd6.metric(
                    "Insider Own.",
                    f"{float(insider) * 100:.1f}%" if insider is not None else "N/A",
                )

        st.markdown("---")
```

**Why:** The score breakdown expander shows exactly which components drove the total score, split into the technical and fundamental blocks. The cash runway warning badge (`st.error`) provides a bright red alert directly in the stock detail view for the most critical fundamental red flag. The Fundamentals Detail expander provides the raw fundamental numbers for enriched tickers.

---

#### 4g — Update `main()` to unpack the new 7-tuple from `_render_filters` and call `enrich_with_fundamentals`

**Location:** Inside `main()`, find this exact line:
```python
    min_cap, max_cap, min_price, max_price, min_volume, sector = _render_filters()
```

Replace with:
```python
    min_cap, max_cap, min_price, max_price, min_volume, sector, enrich_top_n = _render_filters()
```

Then, find the block where `df` is set from session state and the `_cached_screener` call is made. Find this exact block:

```python
    # On first load OR when run button pressed, fetch results
    if "screener_results_df" not in st.session_state or run_btn:
        with st.spinner("Running screener via Yahoo Finance..."):
            df = _cached_screener(min_cap, max_cap, min_price, max_price, min_volume, sector)
        st.session_state["screener_results_df"] = df
        st.session_state["screener_filter_key"] = (
            min_cap, max_cap, min_price, max_price, min_volume, sector
        )

    if refresh_btn:
        with st.spinner("Refreshing from Yahoo Finance (bypassing cache)..."):
            df = run_screener(
                min_market_cap=min_cap,
                max_market_cap=max_cap,
                min_price=min_price,
                max_price=max_price,
                min_volume=min_volume,
                sector=sector,
                force_refresh=True,
            )
        st.session_state["screener_results_df"] = df
        _cached_screener.clear()
```

Replace with:
```python
    # On first load OR when run button pressed, fetch results
    if "screener_results_df" not in st.session_state or run_btn:
        with st.spinner("Running screener via Yahoo Finance..."):
            df = _cached_screener(min_cap, max_cap, min_price, max_price, min_volume, sector)
        if not df.empty:
            with st.spinner(f"Enriching top {enrich_top_n} candidates with fundamentals (24h cached)..."):
                df = enrich_with_fundamentals(df, top_n=enrich_top_n)
        st.session_state["screener_results_df"] = df
        st.session_state["screener_filter_key"] = (
            min_cap, max_cap, min_price, max_price, min_volume, sector
        )

    if refresh_btn:
        with st.spinner("Refreshing from Yahoo Finance (bypassing cache)..."):
            df = run_screener(
                min_market_cap=min_cap,
                max_market_cap=max_cap,
                min_price=min_price,
                max_price=max_price,
                min_volume=min_volume,
                sector=sector,
                force_refresh=True,
            )
        if not df.empty:
            with st.spinner(f"Enriching top {enrich_top_n} candidates with fundamentals (24h cached)..."):
                df = enrich_with_fundamentals(df, top_n=enrich_top_n)
        st.session_state["screener_results_df"] = df
        _cached_screener.clear()
```

**Why:** After the base screener returns, `enrich_with_fundamentals` is called with the user-selected `enrich_top_n` value. The enriched DataFrame is then stored in session state. Because `enrich_with_fundamentals` checks the SQLite fundamentals cache (24h TTL), repeated runs within a day will not re-fetch from Yahoo; only the first call for each ticker is slow.

---

#### 4h — Update the sidebar help text to describe the new scoring

**Location:** Inside `_render_sidebar`, find this exact `with st.expander("How is the Speculation Score calculated?"):` block:

```python
        with st.expander("How is the Speculation Score calculated?"):
            st.markdown(
                "The score combines five equally weighted factors (0–20 pts each):\n"
                "1. **52-week momentum** — how far above the 52-week low\n"
                "2. **52-week recovery room** — how far below the 52-week high\n"
                "3. **Volume spike** — 10-day vs 3-month average volume ratio\n"
                "4. **Small cap premium** — smaller cap = higher potential\n"
                "5. **Forward PE discount** — low or no PE = unloved / early stage\n\n"
                "Higher score = more speculative (more risk AND more potential upside)."
            )
```

Replace with:
```python
        with st.expander("How is the Speculation Score calculated?"):
            st.markdown(
                "The score is a **blended 100-point scale** split into two blocks:\n\n"
                "**Technical Block (50 pts):** 5 components × 10 pts each:\n"
                "1. **52-week momentum** — how far above the 52-week low\n"
                "2. **52-week recovery room** — how far below the 52-week high\n"
                "3. **Volume spike** — 10-day vs 3-month average volume ratio\n"
                "4. **Small cap premium** — smaller cap = higher potential\n"
                "5. **Forward PE discount** — low or no PE = unloved / early stage\n\n"
                "**Fundamental Block (50 pts):** Fetched via yfinance .info for top N tickers:\n"
                "1. **Revenue growth** — YoY growth (0–15 pts; ≥50% YoY = max)\n"
                "2. **Cash runway** — total cash / annual FCF burn (0–10 pts; ≥2yr = max)\n"
                "3. **Analyst upside** — mean price target vs current price (0–10 pts)\n"
                "4. **Short squeeze** — short % of float (0–10 pts; ≥20% = max)\n"
                "5. **Debt health** — debt/equity ratio (0–5 pts; ≤0.5 = max)\n\n"
                "Tickers not in the top-N enrichment window are shown as 'Tech only' "
                "with their score scaled from the technical block.\n\n"
                "Higher score = more speculative (more risk AND more potential upside)."
            )
```

**Why:** The sidebar help text must accurately describe the new blended scoring system so users understand why scores differ before and after enrichment.

---

## 5. Database Changes

### New table: `screener_fundamentals` in `screener.db`

Exact DDL (added to `db/screener_schema.sql` in Step 1):

```sql
CREATE TABLE IF NOT EXISTS screener_fundamentals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    fundamentals_json TEXT NOT NULL,
    fetched_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sf_ticker
    ON screener_fundamentals (ticker);

CREATE INDEX IF NOT EXISTS idx_sf_ticker_fetched
    ON screener_fundamentals (ticker, fetched_at);
```

**Migration:** None required. The schema is executed as `conn.executescript(ddl)` at import time by both `data/screener.py` and `data/screener_agent.py`, both of which read the full `screener_schema.sql` file. Because all three tables use `CREATE TABLE IF NOT EXISTS`, existing installs will gain the new table on next startup without data loss.

**Rollback:** `DROP TABLE IF EXISTS screener_fundamentals;` run against `db/screener.db`. This has no effect on `screener_results` or `screener_analysis`.

---

## 6. UI/UX Specification

### Filter Row
- Layout: 5 columns (`st.columns(5)`)
- Col 1: `st.selectbox("Market Cap Tier", ...)` — unchanged
- Col 2: `st.slider("Price Range (USD)", ...)` — unchanged
- Col 3: `st.selectbox("Min Avg Daily Volume (3M)", ...)` — unchanged
- Col 4: `st.selectbox("Sector", ...)` — unchanged
- Col 5: `st.selectbox("Enrich with Fundamentals (top N)", options=["Top 10", "Top 25", "Top 50"], index=1, key="screener_enrich_n")` — default "Top 25"

### Results Table
- Columns when no enrichment: Ticker, Name, Price, Day Chg%, Market Cap, Vol (3M Avg), Vol Spike, 52W High Chg, 52W Low Chg, Fwd PE, EPS (TTM), Spec Score
- Additional columns when `has_fundamentals` is True for at least one row: Rev Growth, Cash Runway, Short % Flt, Analyst Upside, Has Fundamentals
- Cash runway warning: `st.warning(...)` displayed below the dataframe listing any tickers with `red_flag_cash_runway == True`

### Stock Detail Expander (each row)
1. Price chart (unchanged)
2. Five metric columns (unchanged)
3. **Score Breakdown** nested expander (collapsed by default):
   - Two columns: left = Technical sub-score breakdown (T1–T5), right = Fundamental sub-score breakdown (F1–F5) or "Not yet enriched" message
4. **Cash Runway Warning** `st.error(...)` badge — shown only when `red_flag_cash_runway` is True
5. **Fundamentals Detail** nested expander (collapsed by default) — shown only when `has_fundamentals` is True; displays 6 metrics: Rev Growth, Cash Runway, Short % Float, Analyst Upside, Debt/Equity, Insider Ownership
6. Gemini analysis section (unchanged)

### Spinner Messages
- Base screener: `"Running screener via Yahoo Finance..."`
- Enrichment: `"Enriching top {N} candidates with fundamentals (24h cached)..."`

---

## 7. Testing Checklist

Run the app using the project venv:
```powershell
cd "C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard"
.\venv\Scripts\python.exe -m streamlit run dashboard.py
```

Verify each item:

1. **Schema migration:** Run the following command. Output must include `screener_fundamentals`:
   ```powershell
   & "C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard\venv\Scripts\python.exe" -c "import sqlite3; conn = sqlite3.connect('db/screener.db'); print([r[0] for r in conn.execute('SELECT name FROM sqlite_master WHERE type=\"table\"').fetchall()])"
   ```
   Expected output: `['screener_results', 'screener_analysis', 'screener_fundamentals']`

2. **Page loads without errors:** Open the app and navigate to the Screener page. No Python traceback in the terminal. The page header "Speculative Stock Screener" renders.

3. **Filter layout:** The filter row now shows 5 controls (not 4). The 5th control is labelled "Enrich with Fundamentals (top N)" with options "Top 10", "Top 25", "Top 50" and default "Top 25".

4. **Sidebar score description:** Click "How is the Speculation Score calculated?" in the sidebar. It should describe a 50-pt technical block and a 50-pt fundamental block (not the old 5×20 structure).

5. **Run Screener — base results:** Click "Run Screener" with default filters. After the base screener spinner, a second spinner "Enriching top 25 candidates…" should appear. Results table renders with at least the base 12 columns.

6. **Fundamental columns visible:** After enrichment completes, the results table should include columns "Rev Growth", "Cash Runway", "Short % Flt", "Analyst Upside", "Has Fundamentals" for rows where `has_fundamentals = Yes`.

7. **"Tech only" rows:** If the screener returns more than 25 results, rows beyond position 25 should show "Tech only" in the "Has Fundamentals" column.

8. **Score re-ordering:** After enrichment, the Spec Score column should reflect blended scores (0–100). Rows with fundamentals should be re-sorted so the highest blended score is row 1.

9. **Score breakdown expander:** Expand any enriched stock. Click "Score Breakdown". Confirm:
   - Left column shows T1–T5 with individual point values summing to `technical_total`
   - Right column shows F1–F5 with individual point values summing to `fundamental_total`
   - `technical_total + fundamental_total == speculation_score` for that row

10. **Fundamentals Detail expander:** Expand any enriched stock. Click "Fundamentals Detail". Confirm 6 metric widgets render: Rev Growth, Cash Runway, Short % Float, Analyst Upside, Debt/Equity, Insider Ownership. At least some values should be non-"N/A" for large-cap-adjacent small caps; expect more N/A for pure micro-caps.

11. **Cash runway warning:** If any enriched ticker has a computed runway < 0.5 years, the results table should show a yellow `st.warning` banner listing those tickers. The stock's expander should show a red `st.error` badge. (May not appear depending on screener results on that day — this is contingent on actual data.)

12. **Non-enriched stock breakdown:** Expand a "Tech only" row (row index > 25 in results). Click "Score Breakdown". Confirm the right column shows "**Fundamentals: Not yet enriched**" and the caption "Score shown is technical-only (×2 scaling)."

13. **Fundamentals SQLite cache:** Run the screener a second time within 24 hours with the same filters. The enrichment spinner should complete much faster (< 2s) because fundamentals are served from the SQLite cache rather than re-fetched from Yahoo.

14. **Gemini prompt includes fundamentals:** If you have Gemini Pro quota, run analysis on an enriched ticker. Check the terminal for no errors. The analysis should reference revenue growth, cash runway, or short interest in its `upside_thesis` or `key_risks` (confirming the new prompt sections are being sent). If you cannot run Gemini, verify by adding a temporary `print(prompt)` to `_build_prompt` and confirming the prompt output contains the `=== FUNDAMENTAL METRICS ===` section with real values (not all "N/A") for an enriched ticker.

15. **Force Refresh:** Click "Force Refresh (bypass cache)". Both spinners should appear (base screener + enrichment). The enrichment phase should be fast if within 24h (cached fundamentals). No Python errors.

16. **Import smoke test:** Verify both modified data modules import cleanly:
    ```powershell
    & "C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard\venv\Scripts\python.exe" -c "from data.screener import run_screener, enrich_with_fundamentals; from data.screener_agent import run_risk_analysis; print('OK')"
    ```
    Expected output: `OK`

17. **Empty results edge case:** Set Price Range to $0.10–$0.20 and Min Volume to 1M+. Click "Run Screener". No enrichment spinner should appear (skipped because `df.empty`). The info box "No candidates found…" should render without errors.

---

## 8. Rollback Plan

If anything goes wrong, undo changes in this order:

1. **Revert `data/screener.py`:** Undo changes in Steps 2a–2e. Specifically:
   - Remove `import concurrent.futures` from the imports block
   - Remove the `_FUNDAMENTALS_TTL_HOURS` and `_DEFAULT_ENRICH_TOP_N` constants
   - Remove all five new function blocks (`_save_fundamentals`, `_load_fundamentals`, `_fetch_single_fundamentals`, `fetch_fundamentals_for_tickers`, `enrich_with_fundamentals`)
   - Replace `_compute_technical_score`, `_compute_fundamental_score`, `_no_fundamentals_score`, and the updated `_compute_speculation_score` wrapper with the original single `_compute_speculation_score` function (as it existed before — its exact text is in `plans/plan_speculative_screener.md` Step 2)
   - Revert `_quotes_to_dataframe` row dict to the original 14-key version without the fundamentals columns or `score_breakdown` key

2. **Revert `data/screener_agent.py`:** Restore the original `_PROMPT_TEMPLATE` and `_build_prompt` function from `plans/plan_speculative_screener.md` Step 3 (they are reproduced verbatim there).

3. **Revert `pages/12_screener.py`:** Undo changes in Steps 4a–4h:
   - Remove `enrich_with_fundamentals` from the `data.screener` import
   - Remove `_ENRICH_N_OPTIONS` and `_DEFAULT_ENRICH_N` constants
   - Remove `_fmt_runway` and `_fmt_growth` helper functions
   - Restore `_render_filters` to 4-column layout, original return tuple `(min_cap, max_cap, min_price, max_price, min_volume, sector)`
   - Restore the original `display_df` construction block in `_render_results_table`
   - Restore the original `_render_stock_detail` body (remove breakdown expander, cash warning, fundamentals detail expander)
   - Restore `main()` to unpack 6-tuple from `_render_filters` and remove `enrich_with_fundamentals` calls
   - Restore the original sidebar help text for score explanation

4. **Revert `db/screener_schema.sql`:** Remove the three lines appended in Step 1 (the `screener_fundamentals` table DDL). The existing `screener.db` file does not need to be deleted; the table will simply become orphaned but harmless.

5. **Optional database cleanup:** To also drop the orphaned table from a live `screener.db`:
   ```powershell
   & "C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard\venv\Scripts\python.exe" -c "import sqlite3; conn = sqlite3.connect('db/screener.db'); conn.execute('DROP TABLE IF EXISTS screener_fundamentals'); conn.commit(); print('done')"
   ```

No other files (`components/ui.py`, `dashboard.py`, etc.) are touched by this plan.
