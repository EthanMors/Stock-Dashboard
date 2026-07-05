---
name: portfolio_page_structure
description: Key structural facts about pages/9_portfolio.py needed for planning changes to the Dashboard tab / Covered Options Strategy Desk
metadata:
  type: project
---

`pages/9_portfolio.py` is a large single-file script (4700+ lines) organized
into `st.tabs([...])` assigned to `_tab_dash, _tab_news, _tab_opt, _tab_ta,
_tab_reddit, _tab_smart, _tab_pulse, _tab_econ` (defined ~line 4022). Each tab
body is a `with _tab_x:` block. The "Covered Options Strategy Desk" section
lives at the very end of `with _tab_dash:` (~line 4425 onward, ending ~4611
right before `with _tab_news:` begins).

Cash figure source of truth: `_cash = _to_float(selected_balance.get("total_cash_balance"))`
(~line 2690), where `selected_balance = accounts[selected_label]` and
`accounts[label] = _cached_balance(aid)` → `data.webull_positions.get_balance(account_id)`.
`_to_float` returns `None` (not 0.0) on missing/bad values — this is the
correct signal for "balance fetch failed" when building graceful-degradation
UI. This is the same `_cash` used for the "Cash" KPI metric card at the top
of the page — any new feature needing account cash should reuse this
module-level variable rather than re-fetching balance.

`data/covered_options_agent.py` owns all Covered Options Desk business logic
(pure functions + Gemini roundtable orchestration). Position field extraction
helpers (`_extract_ticker`, `_extract_position_fields`, `_first_float`) live
there and handle Webull's string-typed dict values — reuse these helpers for
any new pure function that reads from `positions_result` (list of raw Webull
position dicts) rather than re-implementing field lookup.

See [[covered_options_desk_extension_plan]] for a worked example of extending
this section (near-eligible positions table + cash-based strategy capability
panel), written 2026-07-05.
