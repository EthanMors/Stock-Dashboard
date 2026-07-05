---
name: covered_options_desk_extension_plan
description: Pointer to the plan file that added near-eligible positions + cash-capability panel to the Covered Options Strategy Desk
metadata:
  type: project
---

On 2026-07-05, a plan was written to
`C:\Users\ethan\Downloads\Stock-Dashboard\plans\plan_near_eligible_cash.md`
extending the Covered Options Strategy Desk (`pages/9_portfolio.py`,
`data/covered_options_agent.py`) with:
1. `find_near_eligible_positions()` — positions with 80-99 shares (constant
   `NEAR_ELIGIBLE_MIN_SHARES = 80`), showing shares needed + cost to complete
   a full 100-share lot.
2. `assess_strategy_capabilities(cash, eligible, near_eligible)` — pure
   Python (no Gemini/agy calls) capability assessment returning
   covered-call/cash-secured-put/collar/wheel booleans + plain-English notes,
   rendered as a small panel using the page's existing `_cash` variable.

Check whether that plan was actually executed (`git log` on
`data/covered_options_agent.py` and `pages/9_portfolio.py`) before assuming
these functions exist — this memory only records that the plan was written,
not confirmed merged. See [[portfolio_page_structure]] for the underlying
architectural facts (cash source, tab structure) that remain true regardless
of whether this specific plan was executed.
