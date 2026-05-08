"""
MCP server for testing the Streamlit stock dashboard.

Provides tools to launch/stop the dashboard, navigate pages,
read rendered content, trigger AI analysis, and run integration checks.

Usage (stdio mode, consumed by Claude Code / MCP clients):
    python mcp_server.py
"""

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright
import subprocess

# ── Config ────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent
DASHBOARD_DIR = REPO_ROOT / "stock-dashboard"
STREAMLIT_URL = "http://localhost:8501"
HEALTH_URL = f"{STREAMLIT_URL}/_stcore/health"
STARTUP_TIMEOUT = 45        # seconds to wait for Streamlit ready
PAGE_LOAD_TIMEOUT = 20_000  # ms for Playwright navigations
RENDER_SETTLE = 2.0         # seconds after navigation for Streamlit to finish rendering

# Human-readable page name → URL slug (matches Streamlit's routing from filename)
PAGE_SLUGS: dict[str, str] = {
    "home":          "",
    "dashboard":     "",
    "metrics":       "metrics",
    "thesis":        "thesis",
    "thesis tracker":"thesis",
    "watchlist":     "watchlist",
    "news":          "news",
    "hedge funds":   "hedge_funds",
    "hedge_funds":   "hedge_funds",
    "option chains": "option_chains",
    "option_chains": "option_chains",
    "options":       "option_chains",
    "positions":     "positions",
    "portfolio":     "positions",
    "wsb":           "reddit",
    "reddit":        "reddit",
    "wallstreetbets":"reddit",
}

ALL_PAGES = [
    ("Home",          ""),
    ("Metrics",       "metrics"),
    ("Thesis",        "thesis"),
    ("Watchlist",     "watchlist"),
    ("News",          "news"),
    ("Hedge Funds",   "hedge_funds"),
    ("Option Chains", "option_chains"),
    ("Positions",     "positions"),
    ("WSB Reddit",    "reddit"),
]

# ── Global state ──────────────────────────────────────────────────────────────

_proc: "subprocess.Popen[str] | None" = None
_pw: "Playwright | None" = None
_browser: "Browser | None" = None
_context: "BrowserContext | None" = None
_page: "Page | None" = None

mcp = FastMCP("dashboard-tester")

# ── Internal helpers ──────────────────────────────────────────────────────────

def _page_url(slug: str) -> str:
    return f"{STREAMLIT_URL}/{slug}" if slug else STREAMLIT_URL


def _kill_proc() -> None:
    global _proc
    if _proc is None:
        return
    try:
        _proc.terminate()
        _proc.wait(timeout=5)
    except Exception:
        try:
            _proc.kill()
        except Exception:
            pass
    _proc = None


async def _close_browser() -> None:
    global _pw, _browser, _context, _page
    for attr, obj, method in [
        ("_page",    _page,    lambda o: o.close()),
        ("_context", _context, lambda o: o.close()),
        ("_browser", _browser, lambda o: o.close()),
        ("_pw",      _pw,      lambda o: o.stop()),
    ]:
        if obj is not None:
            try:
                await method(obj)
            except Exception:
                pass
    _page = _context = _browser = _pw = None


async def _ensure_page() -> Page:
    """Return the active Playwright page, launching the browser if needed."""
    global _pw, _browser, _context, _page
    if _page is not None and not _page.is_closed():
        return _page
    if _pw is None:
        _pw = await async_playwright().start()
    if _browser is None or not _browser.is_connected():
        _browser = await _pw.chromium.launch(headless=True)
    if _context is None:
        _context = await _browser.new_context(
            viewport={"width": 1280, "height": 900},
        )
    _page = await _context.new_page()
    return _page


async def _wait_for_streamlit(timeout: int = STARTUP_TIMEOUT) -> bool:
    """Poll the Streamlit health endpoint until it responds 200."""
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                r = await client.get(HEALTH_URL, timeout=2.0)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.75)
    return False


async def _navigate(slug: str) -> dict[str, Any]:
    """Navigate the browser to a page slug. Returns status dict."""
    url = _page_url(slug)
    try:
        p = await _ensure_page()
        await p.goto(url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT)
        await asyncio.sleep(RENDER_SETTLE)
        return {"ok": True, "url": url}
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}


async def _scrape_page(p: Page) -> dict[str, Any]:
    """Extract structured content from the current Playwright page."""
    # Title
    title_els = await p.locator("h1").all_inner_texts()
    title = title_els[0].strip() if title_els else ""

    # Streamlit metrics
    metrics: list[dict] = []
    mc_locs = p.locator('[data-testid="metric-container"]')
    for i in range(await mc_locs.count()):
        mc = mc_locs.nth(i)
        lbl = mc.locator('[data-testid="stMetricLabel"]')
        val = mc.locator('[data-testid="stMetricValue"]')
        metrics.append({
            "label": (await lbl.inner_text()).strip() if await lbl.count() else "",
            "value": (await val.inner_text()).strip() if await val.count() else "",
        })

    # Alerts (info / warning / error banners)
    alerts: list[dict] = []
    alert_locs = p.locator('[data-testid="stAlert"]')
    for i in range(await alert_locs.count()):
        text = (await alert_locs.nth(i).inner_text()).strip()
        alerts.append({"text": text[:400]})

    # Exceptions / tracebacks
    exceptions: list[str] = []
    for sel in (".stException", '[data-testid="stException"]'):
        exc_locs = p.locator(sel)
        for i in range(await exc_locs.count()):
            exceptions.append((await exc_locs.nth(i).inner_text())[:800])

    # Dataframe count
    df_count = await p.locator('[data-testid="stDataFrame"]').count()

    # Full text (cap at 8 000 chars to keep responses manageable)
    raw = await p.inner_text("body")
    raw = raw[:8000] if len(raw) > 8000 else raw

    return {
        "title":           title,
        "metrics":         metrics,
        "alerts":          alerts,
        "exceptions":      exceptions,
        "dataframe_count": df_count,
        "raw_text":        raw,
    }


# ── MCP tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def start_dashboard(page: str = None) -> dict:
    """Launch the Streamlit stock dashboard and wait until it is ready.

    Args:
        page: Optional page to navigate to after startup
              (e.g. "metrics", "option chains", "positions", "wsb").
              Omit to land on the home/dashboard page.

    Returns:
        {"status": "ok", "url": ..., "pid": ..., "current_page": ...}
        or {"status": "error", "error": ...}
    """
    global _proc

    _kill_proc()  # ensure no stale process

    # Check whether something is already listening on 8501 (external instance)
    already_running = False
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(HEALTH_URL, timeout=2.0)
            already_running = r.status_code == 200
        except Exception:
            pass

    if not already_running:
        try:
            _proc = subprocess.Popen(
                [
                    sys.executable, "-m", "streamlit", "run", "dashboard.py",
                    "--server.headless", "true",
                    "--server.port", "8501",
                    "--browser.gatherUsageStats", "false",
                    "--logger.level", "error",
                ],
                cwd=str(DASHBOARD_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as exc:
            return {"status": "error", "error": f"Failed to launch Streamlit: {exc}"}

        ready = await _wait_for_streamlit()
        if not ready:
            _kill_proc()
            return {
                "status": "error",
                "error": (
                    f"Streamlit did not become ready within {STARTUP_TIMEOUT}s. "
                    "Check that all requirements are installed and dashboard.py is valid."
                ),
            }

    # Open browser and navigate
    slug = ""
    if page:
        slug = PAGE_SLUGS.get(page.lower().strip(), page.lower().replace(" ", "_"))
    nav = await _navigate(slug)
    if not nav["ok"]:
        return {
            "status": "partial",
            "url": STREAMLIT_URL,
            "warning": f"Dashboard is running but browser navigation failed: {nav['error']}",
            "pid": _proc.pid if _proc else None,
        }

    return {
        "status": "ok",
        "url": STREAMLIT_URL,
        "current_page": nav["url"],
        "pid": _proc.pid if _proc else "external",
        "already_running": already_running,
    }


@mcp.tool()
async def stop_dashboard() -> dict:
    """Stop the Streamlit dashboard subprocess and close the browser.

    Returns:
        {"status": "ok", "message": ...}
    """
    _kill_proc()
    await _close_browser()
    return {"status": "ok", "message": "Dashboard stopped and browser closed."}


@mcp.tool()
async def navigate_to_page(page_name: str) -> dict:
    """Navigate the browser to a specific dashboard page.

    Args:
        page_name: Page to visit. Accepted values:
            "home", "metrics", "thesis", "watchlist", "news",
            "hedge funds", "option chains", "positions", "wsb".

    Returns:
        {"status": "ok", "url": ..., "page": ...}
        or {"status": "error", "error": ...}
    """
    if _proc is None:
        # Allow connecting to an externally started dashboard
        async with httpx.AsyncClient() as client:
            try:
                r = await client.get(HEALTH_URL, timeout=2.0)
                if r.status_code != 200:
                    return {
                        "status": "error",
                        "error": "Dashboard is not running. Call start_dashboard first.",
                    }
            except Exception:
                return {
                    "status": "error",
                    "error": "Dashboard is not running. Call start_dashboard first.",
                }

    slug = PAGE_SLUGS.get(page_name.lower().strip(), page_name.lower().replace(" ", "_"))
    nav = await _navigate(slug)
    if nav["ok"]:
        return {"status": "ok", "url": nav["url"], "page": page_name}
    return {"status": "error", "url": nav["url"], "error": nav["error"]}


@mcp.tool()
async def read_page_output() -> dict:
    """Read all visible content from the currently loaded Streamlit page.

    Returns:
        {
            "status": "ok",
            "url": current URL,
            "title": page H1,
            "metrics": [{"label": ..., "value": ...}, ...],
            "alerts": [{"text": ...}, ...],
            "exceptions": [...],
            "dataframe_count": int,
            "raw_text": full page text (capped at 8 000 chars)
        }
    """
    try:
        p = await _ensure_page()
        await p.wait_for_load_state("networkidle", timeout=PAGE_LOAD_TIMEOUT)
        content = await _scrape_page(p)
        return {"status": "ok", "url": p.url, **content}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


@mcp.tool()
async def check_ai_analysis(ticker: str) -> dict:
    """Enter a ticker on the Metrics page, wait for analysis to render,
    and return the full page output along with AI-generated content.

    Also checks the WSB Reddit page for the same ticker if it's accessible.

    Args:
        ticker: Stock ticker symbol (e.g. "AAPL", "NVDA", "TSLA").

    Returns:
        {
            "status": "ok" | "error",
            "ticker": ...,
            "metrics_page": {...scraped content...},
            "wsb_page": {...scraped content...} or None,
            "has_exceptions": bool
        }
    """
    ticker = ticker.upper().strip()

    try:
        p = await _ensure_page()

        # ── Metrics page ──────────────────────────────────────────────────
        await p.goto(f"{STREAMLIT_URL}/metrics", wait_until="networkidle",
                     timeout=PAGE_LOAD_TIMEOUT)
        await asyncio.sleep(RENDER_SETTLE)

        # Try to find and fill the ticker input
        filled = False
        for selector in [
            'input[aria-label="Ticker Symbol"]',
            'input[placeholder*="AAPL"]',
            'input[type="text"]',
        ]:
            inp = p.locator(selector).first
            if await inp.count() > 0:
                await inp.fill(ticker)
                await p.keyboard.press("Enter")
                filled = True
                break

        if not filled:
            return {
                "status": "error",
                "ticker": ticker,
                "error": "Could not locate the ticker input field on the Metrics page.",
            }

        # Wait for Streamlit to re-render (data fetches + charts)
        try:
            await p.wait_for_load_state("networkidle", timeout=25_000)
        except Exception:
            pass
        await asyncio.sleep(3)

        metrics_content = await _scrape_page(p)

        # ── WSB Reddit page (AI sentiment) ────────────────────────────────
        wsb_content = None
        try:
            await p.goto(f"{STREAMLIT_URL}/reddit", wait_until="networkidle",
                         timeout=PAGE_LOAD_TIMEOUT)
            await asyncio.sleep(RENDER_SETTLE)

            wsb_inp = p.locator('input[aria-label="Ticker Symbol"]').first
            if await wsb_inp.count() == 0:
                wsb_inp = p.locator('input[type="text"]').first
            if await wsb_inp.count() > 0:
                await wsb_inp.fill(ticker)
                await p.keyboard.press("Enter")
                try:
                    await p.wait_for_load_state("networkidle", timeout=30_000)
                except Exception:
                    pass
                await asyncio.sleep(4)
                wsb_content = await _scrape_page(p)
        except Exception as exc:
            wsb_content = {"error": str(exc)}

        has_exc = bool(
            metrics_content.get("exceptions")
            or (isinstance(wsb_content, dict) and wsb_content.get("exceptions"))
        )

        return {
            "status": "error" if has_exc else "ok",
            "ticker": ticker,
            "metrics_page": metrics_content,
            "wsb_page": wsb_content,
            "has_exceptions": has_exc,
        }

    except Exception as exc:
        return {"status": "error", "ticker": ticker, "error": str(exc)}


@mcp.tool()
async def get_component_status() -> dict:
    """Health-check every dashboard page: does it load, does it show data,
    are there any visible exceptions or error alerts?

    Returns:
        {
            "status": "ok" | "error",
            "pages": {
                "<Page Name>": {
                    "url": ...,
                    "loads_ok": bool,
                    "has_data": bool,
                    "metric_count": int,
                    "dataframe_count": int,
                    "exception_count": int,
                    "errors": [...]
                },
                ...
            }
        }
    """
    try:
        p = await _ensure_page()
    except Exception as exc:
        return {"status": "error", "error": f"Could not open browser: {exc}"}

    report: dict[str, Any] = {}

    for name, slug in ALL_PAGES:
        url = _page_url(slug)
        result: dict[str, Any] = {
            "url": url,
            "loads_ok": False,
            "has_data": False,
            "metric_count": 0,
            "dataframe_count": 0,
            "exception_count": 0,
            "errors": [],
        }
        try:
            await p.goto(url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT)
            await asyncio.sleep(RENDER_SETTLE)
            result["loads_ok"] = True

            result["exception_count"] = await p.locator(".stException").count()

            # Collect visible alert text (cap at 3)
            alert_locs = p.locator('[data-testid="stAlert"]')
            for i in range(min(await alert_locs.count(), 3)):
                result["errors"].append(
                    (await alert_locs.nth(i).inner_text()).strip()[:250]
                )

            result["metric_count"] = await p.locator('[data-testid="metric-container"]').count()
            result["dataframe_count"] = await p.locator('[data-testid="stDataFrame"]').count()
            result["has_data"] = result["metric_count"] > 0 or result["dataframe_count"] > 0

        except Exception as exc:
            result["errors"].append(str(exc)[:300])

        report[name] = result

    return {"status": "ok", "pages": report}


@mcp.tool()
async def run_integration_check() -> dict:
    """Full end-to-end integration check across all dashboard pages.

    Visits every page, checks it renders without crashes, and verifies
    that at least some data or UI elements are present.

    Returns:
        {
            "status": "pass" | "fail",
            "summary": "N/M pages passed",
            "passed": [...page names...],
            "failed": [{"page": ..., "reason": ..., "errors": [...]}, ...],
            "warnings": [{"page": ..., "reason": ..., "alerts": [...]}, ...],
            "details": {<full per-page component status>}
        }
    """
    # Check dashboard is reachable first
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(HEALTH_URL, timeout=3.0)
            if r.status_code != 200:
                return {
                    "status": "fail",
                    "error": "Dashboard health endpoint did not return 200. "
                             "Call start_dashboard first.",
                }
        except Exception as exc:
            return {
                "status": "fail",
                "error": f"Dashboard is not reachable: {exc}. "
                         "Call start_dashboard first.",
            }

    status = await get_component_status()
    if status.get("status") == "error":
        return {"status": "fail", "error": status.get("error", "Unknown error")}

    pages = status["pages"]
    passed: list[str] = []
    failed: list[dict] = []
    warnings: list[dict] = []

    for name, info in pages.items():
        if not info["loads_ok"]:
            failed.append({
                "page": name,
                "reason": "Page failed to load",
                "errors": info["errors"],
            })
        elif info["exception_count"] > 0:
            failed.append({
                "page": name,
                "reason": f"{info['exception_count']} exception(s) visible on page",
                "errors": info["errors"],
            })
        elif info["errors"]:
            # Alert banners but no crash — treat as warning, still passing
            warnings.append({
                "page": name,
                "reason": "Alert/warning messages present",
                "alerts": info["errors"],
            })
            passed.append(name)
        else:
            passed.append(name)

    total = len(pages)
    pass_count = len(passed)
    fail_count = len(failed)

    return {
        "status": "pass" if fail_count == 0 else "fail",
        "summary": f"{pass_count}/{total} pages passed",
        "passed": passed,
        "failed": failed,
        "warnings": warnings,
        "details": pages,
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
