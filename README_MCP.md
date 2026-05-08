# Dashboard MCP Server

An MCP (Model Context Protocol) server that lets an AI agent (Claude) launch,
navigate, and read the Streamlit stock dashboard — useful for automated
testing, feature verification, and integration checks.

---

## Prerequisites

Python 3.11+ and all project dependencies installed.

```bash
pip install -r stock-dashboard/requirements.txt
python -m playwright install chromium
```

---

## Connecting via Claude Code

The `.mcp.json` file at the repo root is auto-detected by Claude Code. Open
this repo in Claude Code and the `dashboard-tester` MCP server will be
available immediately — no manual configuration needed.

To connect manually, add this block to your `claude_desktop_config.json`
(or equivalent MCP client config):

```json
{
  "mcpServers": {
    "dashboard-tester": {
      "type": "stdio",
      "command": "python",
      "args": ["mcp_server.py"],
      "cwd": "/absolute/path/to/Stock Dashboard"
    }
  }
}
```

---

## Running the server directly (for debugging)

```bash
cd "Stock Dashboard"
python mcp_server.py
```

The server communicates over stdio (MCP protocol). You won't see output in
normal usage — attach an MCP client to interact with it.

---

## Available tools

| Tool | Description |
|------|-------------|
| `start_dashboard(page?)` | Launch Streamlit, wait until ready, open browser. Pass a `page` name to land on a specific page. |
| `stop_dashboard()` | Kill the Streamlit process and close the browser. |
| `navigate_to_page(page_name)` | Navigate the browser to a named page (`"metrics"`, `"option chains"`, `"positions"`, `"wsb"`, etc.). |
| `read_page_output()` | Return structured content from the current page: title, metrics, alerts, exceptions, dataframe count, and full text. |
| `check_ai_analysis(ticker)` | Enter a ticker on the Metrics page, wait for analysis, also run the WSB Reddit page for that ticker. Returns full scraped output. |
| `get_component_status()` | Visit every page and report load success, data presence, exception count, and alert messages. |
| `run_integration_check()` | Full E2E pass/fail report across all 9 pages. |

### Page name aliases

`navigate_to_page` and `start_dashboard` accept any of these names:

| Page | Accepted names |
|------|---------------|
| Home | `home`, `dashboard` |
| Metrics | `metrics` |
| Thesis | `thesis`, `thesis tracker` |
| Watchlist | `watchlist` |
| News | `news` |
| Hedge Funds | `hedge funds`, `hedge_funds` |
| Option Chains | `option chains`, `option_chains`, `options` |
| Positions | `positions`, `portfolio` |
| WSB Reddit | `wsb`, `reddit`, `wallstreetbets` |

---

## Typical agent workflow

```
1. start_dashboard()                        # launch + open browser
2. run_integration_check()                  # quick pass/fail per page
3. navigate_to_page("option chains")        # drill into a specific page
4. read_page_output()                       # read what rendered
5. check_ai_analysis("NVDA")               # trigger + read AI output
6. stop_dashboard()                         # clean up
```

---

## Notes

- Streamlit runs on `http://localhost:8501` (default port).
- If port 8501 is already in use by an external Streamlit instance,
  `start_dashboard` will connect to it instead of launching a new one.
- `read_page_output` and `run_integration_check` cap raw text at 8 000
  characters to keep responses concise.
- The Playwright browser runs headless. Set `headless=False` in
  `_ensure_page()` inside `mcp_server.py` if you want to watch it.
