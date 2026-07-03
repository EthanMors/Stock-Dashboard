# Plan: Fix Stock News "No Data" Issue

This plan analyzes why stock news queries return "No articles found" for individual stocks in the dashboard while the "Market Pulse" tab still works. It outlines the root causes and provides a step-by-step resolution.

---

## 🔍 Root Cause Analysis

We verified via a scratch script ([test_news.py](file:///C:/Users/ethan/.gemini/antigravity-cli/brain/a887da77-9e87-441b-9f4f-8be566c71d89/scratch/test_news.py)) that the Massive.com API key in `.env` is **valid** and returns news successfully for individual stocks (like `AAPL` and `MU`) when queried directly.

The individual stock news fails in the Streamlit application due to a combination of four flaws:

### 1. Market Pulse RSS Fallback Masking
The "Market Pulse" tab combines two news sources:
* **RSS Feeds** (Reuters, MarketWatch), which do not require an API key.
* **ETF Proxies** (`XLK`, `XLF`), which call the Massive API.
When the Massive API fails, the ETF proxy news returns `[]`, but the RSS feeds continue to load articles successfully. This makes the "Market Pulse" tab appear functional, masking the fact that the Massive API is failing globally.

### 2. Context-Dependent `.env` Path Resolution
In [news_fetcher.py](file:///C:/Users/ethan/Downloads/Stock-Dashboard/stock-dashboard/data/news_fetcher.py), `.env` is loaded using:
```python
load_dotenv()
```
By default, this searches the current working directory. If the Streamlit server is launched from the workspace root (`C:\Users\ethan\Downloads\Stock-Dashboard`) instead of the subdirectory (`stock-dashboard/`), `load_dotenv` fails to locate the `.env` file. Consequently, `MASSIVE_API_KEY` remains empty, and the function returns `[]`.

### 3. Malformed Comments in `.env`
The [.env](file:///C:/Users/ethan/Downloads/Stock-Dashboard/stock-dashboard/.env) file has plain text comments on lines 1 and 4 without a `#` prefix:
```
Massive.com API key — get yours at https://massive.com/
MASSIVE_API_KEY=vISSUjGJsLJo3eHop6kFYirlNz1Z3bZR
```
Depending on the version of `python-dotenv` and OS, parsing failures on line 1 can corrupt key loading or crash the parser during server startup.

### 4. Silent Exception Swallowing & Cache Poisoning
In [news_fetcher.py](file:///C:/Users/ethan/Downloads/Stock-Dashboard/stock-dashboard/data/news_fetcher.py), the `except` block catches all exceptions silently:
```python
    except Exception:
        return []
```
If a request fails (e.g. SSL verification issue, DNS resolution failure, or malformed API header), the function silently returns `[]`. 
Because the function is decorated with `@st.cache_data(ttl=900)`, Streamlit **caches the empty list `[]` for 15 minutes**. Even if the network or key issue is resolved, Streamlit will serve the cached empty list until the TTL expires.

---

## 🛠️ Step-by-Step Fix Plan

### Step 1: Fix comment syntax in `.env`
Add a `#` prefix to all comment lines in [stock-dashboard/.env](file:///C:/Users/ethan/Downloads/Stock-Dashboard/stock-dashboard/.env).

**Before:**
```
Massive.com API key — get yours at https://massive.com/
MASSIVE_API_KEY=vISSUjGJsLJo3eHop6kFYirlNz1Z3bZR
```

**After:**
```
# Massive.com API key — get yours at https://massive.com/
MASSIVE_API_KEY=vISSUjGJsLJo3eHop6kFYirlNz1Z3bZR
```

---

### Step 2: Make `.env` resolution robust across all fetchers
Update `news_fetcher.py` and other data fetchers to resolve `.env` using absolute paths relative to `__file__`.

**File:** [news_fetcher.py](file:///C:/Users/ethan/Downloads/Stock-Dashboard/stock-dashboard/data/news_fetcher.py)
**Change:**
```python
import os
from pathlib import Path
from urllib.parse import urlparse
# ...
from dotenv import load_dotenv

# Resolve absolute path to .env (located in parent stock-dashboard directory)
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

_API_KEY = os.getenv("MASSIVE_API_KEY", "")
```

Apply the same change to `fred_fetcher.py`, `twitter_fetcher.py`, and `webull_positions.py`.

---

### Step 3: Implement proper error logging in `fetch_news`
Modify `news_fetcher.py` to log exceptions to `streamlit.warning` or print them, rather than swallowing them silently.

**File:** [news_fetcher.py](file:///C:/Users/ethan/Downloads/Stock-Dashboard/stock-dashboard/data/news_fetcher.py)
**Change:**
```python
    except Exception as exc:
        st.warning(f"Error fetching news for {ticker}: {exc}")
        return []
```

---

### Step 4: Clear Streamlit Cache and Restart
1. Run a script or button click to clear the Streamlit cache (e.g. using `st.cache_data.clear()`).
2. Restart the background Streamlit process to reload the module-level variables.
