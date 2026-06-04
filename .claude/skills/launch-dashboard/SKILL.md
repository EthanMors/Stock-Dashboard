---
description: Launch the Stock Dashboard Streamlit app using the project venv (Python 3.13). Use this skill whenever the user asks to run, start, or launch the dashboard.
triggers:
  - run the dashboard
  - start the dashboard
  - launch the dashboard
  - open the dashboard
---

# Launch Dashboard

Launch the Streamlit dashboard using the project's venv (Python 3.13). Do NOT use system Python — it is 3.14 and incompatible with anyio.

## Steps

1. Kill any existing Streamlit process on port 8501/8502 (optional, only if user requests a fresh restart):
```powershell
Get-Process -Name python* | Where-Object { $_.CommandLine -like "*streamlit*" } | Stop-Process -Force 2>$null
```

2. Launch in the background using the venv:
```powershell
cd "C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard"
& ".\venv\Scripts\python.exe" -m streamlit run dashboard.py
```
Run this with `run_in_background: true`.

3. Wait 5 seconds, then read the tail of the output file to confirm startup:
- Look for `Local URL: http://localhost:850X`
- If you see anyio errors or `TypeError: cannot create weak reference`, it means system Python was used instead of the venv — stop and retry with the venv path above.

4. Report the URL to the user.

## Key facts

- Venv location: `stock-dashboard/venv/Scripts/python.exe`
- Venv Python version: 3.13.7
- System Python is 3.14.4 — DO NOT USE, breaks anyio/uvicorn
- Default port: 8501 (increments to 8502 if already in use)
- `streamlit` is not on PATH — always invoke via `python -m streamlit`
