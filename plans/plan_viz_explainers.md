# Plan: Visualization Explainer Dropdowns

## Overview

Add collapsed "How to read this" dropdowns next to every confusing visualization in the dashboard, so a non-finance reader can tell what the marks, colours, axes and scores actually mean and what to conclude from them. This is **purely additive UI** — it calls the already-existing `explainer()` helper in `components/ui.py` and inserts nothing else. No calculation, no layout, no naming, and no existing line of code changes.

Total scope: **3 files modified, 23 `explainer(...)` calls inserted, 2 import lines extended.**

---

## ⚠️ READ THIS BEFORE WRITING ANY CODE — string formatting rule

Every `explainer(...)` body in this plan is written as **implicitly-concatenated Python string literals**. Copy them EXACTLY as printed, including every `\n` escape and every leading space.

**The rule that has already broken this task once:**

- Any line break you want *inside the rendered markdown* must be typed as the two characters `\` and `n` **inside** a string literal.
- You must **never** press Enter between two quote characters to make a new markdown line.
- A continuation line must **never** begin with a bare `"` at column 0. Every continuation line is indented to match the first string literal.

✅ CORRECT (this is the style used throughout this plan):

```python
    explainer(
        "**First paragraph** of the body text which is long enough to wrap "
        "onto a second physical line.\n\n"
        "**Second paragraph** starts after the two escaped newlines above."
    )
```

❌ WRONG — do not do any of these:

```python
    explainer(
        "**First paragraph**
        **Second paragraph**"          # real line break inside a literal -> SyntaxError
    )

    explainer(
"**First paragraph**\n\n"              # continuation quote at column 0 -> IndentationError
"**Second paragraph**"
    )
```

**Indentation matters.** Each step below states the exact number of leading spaces for the `explainer(` line. Python will raise `IndentationError` if you get it wrong. Where a step shows surrounding context lines, reproduce the indentation exactly as shown.

---

## Files to Modify

| File | Change |
|---|---|
| `stock-dashboard/pages/13_seasonality.py` | Insert 10 `explainer(...)` calls. Import already present — do NOT touch the import block. |
| `stock-dashboard/pages/9_portfolio.py` | Add `explainer` to the `components.ui` import; insert 10 `explainer(...)` calls. |
| `stock-dashboard/pages/10_technical_analysis.py` | Add `explainer` to the `components.ui` import; insert 3 `explainer(...)` calls. |

## Files NOT to Modify

- `stock-dashboard/components/ui.py` — the `explainer()` helper already exists at the bottom of the file (just above `plotly_dark_layout`). Do not edit it.
- `stock-dashboard/pages/12_screener.py` — **intentionally out of scope.** That page already carries thorough "About This Page" sidebar expanders. Leave it completely alone.
- The four chart explainers already present in `13_seasonality.py` (inside `_render_seasonality_heatmap`, `_render_month_profile`, `_render_regime_history_chart`, `_render_exposure_chart`) are **done**. Do not add, edit, or duplicate them.

## Database Changes

None.

## Prerequisites

None. No new pip packages, no env vars, no migrations. `explainer` is already defined and already imported in `13_seasonality.py`.

---

# Part A — `stock-dashboard/pages/13_seasonality.py`

`explainer` is already in the import list at the top of this file (line 12). **Do not modify the import block on this page.**

### Step A1: Outlook tab — the four regime metric cards

**File:** `stock-dashboard/pages/13_seasonality.py`
**Find this exact block** (inside `_render_outlook_tab`):

```python
    cols[3].metric("Inflation pressure", f"{regime.get('inflation_score', 0):+.0f}")

    previous = get_previous_regime()
```

**Replace it with:**

```python
    cols[3].metric("Inflation pressure", f"{regime.get('inflation_score', 0):+.0f}")

    explainer(
        "**Regime** is a plain-English label for what kind of market we are in right "
        "now. The three numbers beside it are scores from **-100 to +100**, each "
        "built from how a basket of prices has been behaving over the last few "
        "months. Zero means no clear signal.\n\n"
        "- **Risk appetite** — positive means money is flowing into riskier assets "
        "(stocks over bonds, small caps over large). Negative means money is hiding.\n"
        "- **Rates (easing +)** — positive means the bond market expects rates to "
        "come down, which usually helps long-duration growth stocks. Negative means "
        "tightening.\n"
        "- **Inflation pressure** — positive means reflation (commodities, energy, "
        "materials tend to do better). Negative means disinflation.\n\n"
        "**What to do with it:** treat roughly **±40 or more as a real tilt** and "
        "anything inside ±20 as noise. At +40 risk appetite you can lean into the "
        "sectors the model favours below; at -40 the sensible response is usually to "
        "do less — fewer new positions, smaller ones — rather than to bet the other "
        "way. These are descriptions of the current weather, not forecasts."
    )

    previous = get_previous_regime()
```

**Why:** the four cards are the very first thing on the page and the ±score scale is meaningless without a key.

---

### Step A2: Outlook tab — "Sectors the model wants more of" cards

**File:** `stock-dashboard/pages/13_seasonality.py`
**Find this exact block** (the end of the `for row in top_adds:` loop in `_render_outlook_tab`):

```python
                f"Today {exposure_note}.</span></div>",
                unsafe_allow_html=True,
            )

    st.markdown("---")
    _render_ai_section(payload, tickers)
```

**Replace it with:**

```python
                f"Today {exposure_note}.</span></div>",
                unsafe_allow_html=True,
            )

        explainer(
            "**Each card is one sector the model scored above +10** for the month "
            "ahead. The **score** is a single blended number out of roughly ±100 that "
            "mixes four things: how that sector usually performs in this calendar "
            "month, how it has been performing against the S&P recently, whether it "
            "fits the current macro regime, and how far your own holdings sit below "
            "the S&P's weight in it.\n\n"
            "The bold word after the score (**add**, **overweight**, **hold** and so "
            "on) is the suggested direction, and the coloured left edge matches it — "
            "green for add, blue for hold, orange/red for trim. The grey line "
            "underneath tells you which of the four ingredients did the work, plus "
            "what you currently hold versus the S&P.\n\n"
            "**The caveat:** a high score often just means you own none of it. That "
            "is a diversification argument, not a prediction. Check the full "
            "scorecard in the Sector Rotation tab to see which component is driving "
            "the number before acting on it."
        )

    st.markdown("---")
    _render_ai_section(payload, tickers)
```

**Note the indentation:** the new `explainer(` line has **8 leading spaces** — it sits inside the `if top_adds:` block, after the `for` loop, not inside the loop.

**Why:** the score has no visible scale and the exposure note is easy to misread as a prediction.

---

### Step A3: Seasonality tab — the per-month statistics table

**File:** `stock-dashboard/pages/13_seasonality.py`
**Find this exact block** (in `_render_seasonality_tab`):

```python
        st.dataframe(frame, use_container_width=True)

    quarters = stats.get("quarters") or {}
```

**Replace it with:**

```python
        st.dataframe(frame, use_container_width=True)

        explainer(
            "**One row per calendar month**, built from every year of history this "
            "ticker has.\n\n"
            "- **Avg %** — the mean return in that month. Easily distorted by one "
            "huge year.\n"
            "- **Median %** — the middle year. If median is far below average, one "
            "or two outliers are inflating the average.\n"
            "- **Win rate %** — the share of years that month finished positive.\n"
            "- **Best / Worst %** — the two extreme years, i.e. the realistic range.\n"
            "- **Stdev %** — how spread out the years were. Big stdev = unreliable.\n"
            "- **Years** — how many observations the row is based on.\n\n"
            "**Read win rate first, average second.** A month averaging +1.0% that "
            "was positive 80% of the time is a far more usable pattern than one "
            "averaging +4.0% that was positive 45% of the time — the second is one "
            "lucky year wearing a disguise. Cross-check Avg against Median and "
            "Stdev: when all three agree the pattern is real; when Best and Worst are "
            "20 points apart, the average is not telling you much.\n\n"
            "Even 30 years gives only 30 observations per month, so treat every row "
            "as a mild tiebreaker, never a standalone reason to trade."
        )

    quarters = stats.get("quarters") or {}
```

**Note the indentation:** `explainer(` has **8 leading spaces** (inside `if months:`).

**Why:** this table is the page's most information-dense object and the average/win-rate distinction is the single most important thing a reader can learn here.

---

### Step A4: Seasonality tab — the Q1–Q4 metric row

**File:** `stock-dashboard/pages/13_seasonality.py`
**Find this exact block** (the last lines of `_render_seasonality_tab`):

```python
        cols = st.columns(len(quarters))
        for col, (quarter, data) in zip(cols, quarters.items()):
            col.metric(quarter, f"{data['avg']:+.2f}%", f"{data['win_rate']:.0f}% win rate")
```

**Replace it with:**

```python
        cols = st.columns(len(quarters))
        for col, (quarter, data) in zip(cols, quarters.items()):
            col.metric(quarter, f"{data['avg']:+.2f}%", f"{data['win_rate']:.0f}% win rate")

        explainer(
            "**The same history rolled up into three-month blocks.** Q1 is "
            "January–March, Q2 April–June, Q3 July–September, Q4 "
            "October–December. The big number is the average total return across "
            "that quarter; the smaller line below it is the share of years the "
            "quarter finished green.\n\n"
            "Quarters are worth a glance because they are less noisy than single "
            "months — three times as much data goes into each one, so a quarterly "
            "tilt that shows up with a 70%+ win rate is more trustworthy than the "
            "same tilt in one month.\n\n"
            "**Use it as a sanity check on the monthly numbers.** If a month looks "
            "strong but its whole quarter is weak, the monthly result is probably "
            "noise. If the month and the quarter agree, you are looking at something "
            "steadier — a genuine seasonal rhythm rather than a single good year."
        )
```

**Note the indentation:** `explainer(` has **8 leading spaces** (inside `if quarters:`), placed *after* the `for` loop — not inside it.

**Why:** users do not know which months map to which quarter, or why the quarterly view is more reliable.

---

### Step A5: Macro Regime tab — the four score bars

**File:** `stock-dashboard/pages/13_seasonality.py`
**Find this exact block** (in `_render_regime_tab`, right after the `for key, label, note in (...)` loop):

```python
    favored = regime.get("favored_sectors") or []
    pressured = regime.get("pressured_sectors") or []
```

**Replace it with:**

```python
    explainer(
        "**Each bar is centred on zero.** The centre line is neutral; the bar grows "
        "to the right for the label on the right of the arrow and to the left for "
        "the label on the left. The number beside it is the same value on a **-100 "
        "to +100** scale, so a bar filling half of one side is roughly ±50.\n\n"
        "- **Risk appetite** — right = risk-on, money moving into stocks and "
        "riskier corners of the market. Left = risk-off, money moving to safety.\n"
        "- **Rate direction** — right = easing expected (helps growth and long-dated "
        "assets). Left = tightening.\n"
        "- **Growth** — right = the economy is expanding on these price signals. "
        "Left = contracting.\n"
        "- **Inflation** — right = reflation (energy, materials, commodities). "
        "Left = disinflation.\n\n"
        "**How far is far?** Past about ±40 the tilt is worth acting on. Inside ±20, "
        "treat it as flat. Two bars pointing the same way (say risk-on plus easing) "
        "is a much stronger read than one big bar on its own. All four are derived "
        "from recent price behaviour across asset classes — they describe what "
        "markets are currently doing, not what they will do next."
    )

    favored = regime.get("favored_sectors") or []
    pressured = regime.get("pressured_sectors") or []
```

**Note the indentation:** `explainer(` has **4 leading spaces** (function body level).

**Why:** a centred bar with no axis labels is the least self-explanatory widget on the page.

---

### Step A6: Macro Regime tab — the "favours / pressures" boxes

**File:** `stock-dashboard/pages/13_seasonality.py`
**Find this exact block:**

```python
        right.error("**Regime pressures:** " + (", ".join(pressured) or "nothing clearly"))
```

**Replace it with:**

```python
        right.error("**Regime pressures:** " + (", ".join(pressured) or "nothing clearly"))

        explainer(
            "**These two boxes are a lookup, not a forecast.** The four scores above "
            "get mapped to the sectors that have historically behaved well or badly "
            "in that kind of environment — for example, easing rates have tended to "
            "favour technology and real estate, while reflation has tended to favour "
            "energy and materials.\n\n"
            "Green is the tailwind list, red is the headwind list. If a box says "
            "\"nothing clearly\", the scores are too close to zero to point anywhere, "
            "which is itself useful information: it means sector selection is "
            "unlikely to be where your returns come from this month.\n\n"
            "**Do not treat the red list as a sell list.** A headwind is a reason to "
            "avoid *adding*, and at most a reason to trim a position you already "
            "doubt. Regimes also flip faster than portfolios should."
        )
```

**Note the indentation:** `explainer(` has **8 leading spaces** (inside `if favored or pressured:`).

**Why:** these boxes look like instructions; they need framing as a historical association.

---

### Step A7: Macro Regime tab — the indicator detail table

**File:** `stock-dashboard/pages/13_seasonality.py`
**Find this exact block:**

```python
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("Indicator data unavailable — price history could not be loaded.")
```

**Replace it with:**

```python
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        explainer(
            "**This is the raw material the four scores are built from.** Each row is "
            "one market-based indicator — things like credit spreads, the shape of "
            "the yield curve, commodity prices, or the ratio of one index to "
            "another.\n\n"
            "- **Level** — the current reading, in that indicator's own units.\n"
            "- **1m change %** — how much it has moved in the last month. Direction "
            "usually matters more than the level.\n"
            "- **1y percentile** — where the current level sits against the last "
            "year of readings. 90 means it is near a one-year high, 10 near a "
            "one-year low. This is the quickest way to see whether something is "
            "actually unusual.\n"
            "- **What it tells you** — a one-line note saying which direction is "
            "bullish for stocks, because it is not the same for every row. For some "
            "indicators (risk-on ratios) higher is bullish; for others (volatility, "
            "credit spreads) higher is bearish. Always read this column before "
            "judging a number.\n\n"
            "Expect rows to disagree. One contradictory indicator is normal; several "
            "moving the same way at once is what actually shifts the regime."
        )
    else:
        st.info("Indicator data unavailable — price history could not be loaded.")
```

**Note the indentation:** `explainer(` has **8 leading spaces** (inside `if indicators:`).

**Why:** without the "which direction is bullish" warning, readers will assume higher is always better.

---

### Step A8: Sector Rotation tab — the rotation scorecard table

**File:** `stock-dashboard/pages/13_seasonality.py`
**Find this exact block** (in `_render_rotation_tab` — note the `} for row in scorecard])` line above it makes this occurrence unique):

```python
        } for row in scorecard])
        st.dataframe(frame, use_container_width=True, hide_index=True)

        st.markdown("**Why each sector scores what it does**")
```

**Replace it with:**

```python
        } for row in scorecard])
        st.dataframe(frame, use_container_width=True, hide_index=True)

        explainer(
            "**One row per sector, ranked by a single blended Score.** The Score is "
            "four ingredients weighted together: the seasonal edge for the month "
            "ahead (**25%**), relative strength versus SPY (**30%**), fit with the "
            "current macro regime (**25%**), and how underweight you already are "
            "versus the S&P 500 (**20%**). Everything is on a roughly ±100 scale.\n\n"
            "- **Your wt % / S&P wt %** — your share of that sector versus the "
            "index's share. **Gap pts** is the difference; negative means you hold "
            "less than the index.\n"
            "- **Avg [month] %** — the sector ETF's average return in that month "
            "historically.\n"
            "- **RS vs SPY** — recent performance against the S&P, scored.\n"
            "- **Regime fit** — how well the sector matches the four macro scores.\n\n"
            "**Action words**, strongest to weakest: *add* and *overweight* mean buy "
            "more; *accumulate on weakness* means only on a dip; *hold* and *neutral* "
            "mean do nothing; *underweight*, *trim* and *hedge* mean reduce; *exit* "
            "and *avoid* are the strongest negative.\n\n"
            "Because underweight counts for 20%, an empty sector can score well "
            "purely for being empty. Read the per-sector reasons underneath to see "
            "which ingredient actually earned the score."
        )

        st.markdown("**Why each sector scores what it does**")
```

**Note the indentation:** `explainer(` has **8 leading spaces** (inside `if scorecard:`).

**Why:** ten numeric columns plus an unexplained weighting scheme and an unexplained action vocabulary.

---

### Step A9: Sector Rotation tab — the sector relative-strength table

**File:** `stock-dashboard/pages/13_seasonality.py`
**Find this exact block** (the `} for row in momentum])` line makes this occurrence unique):

```python
        } for row in momentum])
        st.dataframe(frame, use_container_width=True, hide_index=True)
    else:
        st.info("Momentum data unavailable.")
```

**Replace it with:**

```python
        } for row in momentum])
        st.dataframe(frame, use_container_width=True, hide_index=True)

        explainer(
            "**Left half: raw returns. Right half: returns after subtracting the "
            "market.**\n\n"
            "The **1m / 3m / 6m / 12m %** columns are what the sector ETF actually "
            "did over those windows. The **vs SPY** columns take the same windows "
            "and subtract SPY's return, so +3.0 means the sector beat the S&P by "
            "three percentage points. That is the more useful set: a sector up 8% in "
            "a market up 10% is a laggard, even though 8% looks fine on its own.\n\n"
            "**Blended RS** rolls the vs-SPY columns into one number, weighted "
            "towards the more recent windows. **Above 200DMA** is a yes/no trend "
            "filter — whether the ETF is trading above its 200-day average price, "
            "which is the crudest possible \"is this in an uptrend\" test.\n\n"
            "**Reading it:** strong short windows with weak long windows is a turn "
            "that may not stick; all four positive plus above the 200DMA is a "
            "durable leader. Momentum tends to persist over months and then reverse "
            "sharply, so a sector at the very top of this table is not automatically "
            "the best thing to buy today."
        )
    else:
        st.info("Momentum data unavailable.")
```

**Note the indentation:** `explainer(` has **8 leading spaces** (inside `if momentum:`).

**Why:** the "vs SPY" concept is the whole point of the table and is nowhere stated.

---

### Step A10: Calendar tab — event kinds and urgency colours

**File:** `stock-dashboard/pages/13_seasonality.py`
**Find this exact block** (in `_render_calendar_tab`):

```python
        key="seasonality_catalyst_kinds",
    )

    for event in catalysts:
```

**Replace it with:**

```python
        key="seasonality_catalyst_kinds",
    )

    explainer(
        "**Every scheduled event in the next 60 days that tends to move prices**, "
        "newest first. Use the filter above to hide kinds you do not care about.\n\n"
        "- 🏛️ **fed** — Federal Reserve meetings and decisions. These set interest "
        "rate expectations and move the whole market at once.\n"
        "- 📊 **macro** — scheduled economic data such as inflation (CPI) and jobs "
        "(payrolls). Also market-wide, and the reaction usually comes from the gap "
        "between the number and what was expected.\n"
        "- 📄 **earnings** — a single company reporting results. Moves that stock, "
        "and sometimes its sector.\n"
        "- 🔔 **market** — structural dates such as option expiries and quarter ends, "
        "which can cause flow-driven moves with no news attached.\n\n"
        "**The coloured left border is a countdown, not a judgement.** Orange means "
        "**7 days or fewer**, yellow means **within 21 days**, blue means further "
        "out. Nothing here says whether an event is good or bad — the point is to "
        "know it is coming. The usual practical response to an orange bar is to "
        "avoid adding risk into it, since events are precisely when the market "
        "stops following the slower patterns on the rest of this page."
    )

    for event in catalysts:
```

**Note the indentation:** `explainer(` has **4 leading spaces** (function body level).

**Why:** the border colours read as a good/bad signal unless explicitly labelled as an urgency countdown.

---

# Part B — `stock-dashboard/pages/9_portfolio.py`

### Step B0: Add `explainer` to the imports

**File:** `stock-dashboard/pages/9_portfolio.py`
**Find this exact line** (line 22):

```python
from components.ui import inject_global_css, page_header, render_sidebar_nav, section_header
```

**Replace it with:**

```python
from components.ui import explainer, inject_global_css, page_header, render_sidebar_nav, section_header
```

**Why:** every step in Part B calls `explainer`. Do this first or every later step raises `NameError`.

---

### Step B1: Technical Analysis tab — the candlestick chart

**File:** `stock-dashboard/pages/9_portfolio.py`
**Find this exact block** (around line 825, inside the portfolio TA renderer):

```python
    st.plotly_chart(ta_fig, use_container_width=True)

    st.caption(
        f"{len(ta_df):,} bars · {interval} interval · "
```

**Replace it with:**

```python
    st.plotly_chart(ta_fig, use_container_width=True)

    explainer(
        "**Each candle is one time period.** The thick body spans the open and close "
        "prices; the thin wicks above and below show the highest and lowest price "
        "reached. **Green means the period closed higher than it opened, red means "
        "lower.** A long body is a decisive period, a tiny body with long wicks is "
        "indecision. The bars along the bottom are volume, coloured to match.\n\n"
        "**The EMA lines** are exponential moving averages over 9, 21 and 50 periods "
        "— smoothed versions of price that weight recent bars more heavily. Their "
        "usual use is stacking order: short above long and both rising is an uptrend; "
        "the short line crossing below the long one is the classic warning sign. "
        "Price tends to bounce off these lines in a trend and cut straight through "
        "them when the trend is over.\n\n"
        "**The Bollinger Bands** are the shaded envelope, drawn two standard "
        "deviations either side of a 20-period average. They widen when the stock is "
        "volatile and squeeze when it is quiet. Price touching the upper band means "
        "\"unusually high *for recent conditions*\" — not \"overpriced\", and not a "
        "sell signal. A long squeeze often precedes a big move, but the bands say "
        "nothing about which direction it will go."
    )

    st.caption(
        f"{len(ta_df):,} bars · {interval} interval · "
```

**Note the indentation:** `explainer(` has **4 leading spaces**.

**Why:** candles, EMAs and Bollinger bands are three separate conventions stacked on one chart with no legend explaining any of them.

---

### Step B2: Technical Analysis tab — Pattern Detection table

**File:** `stock-dashboard/pages/9_portfolio.py`
**Find this exact block** (around line 878–884 — the `"Vol ✓"` column config and the AI Analysis comment make it unique):

```python
                "Vol":        st.column_config.TextColumn("Vol ✓",      width="small"),
                "Notes":      st.column_config.TextColumn("Notes",      width="large"),
            },
        )

    # ── AI Analysis section ───────────────────────────────────────────────
```

**Replace it with:**

```python
                "Vol":        st.column_config.TextColumn("Vol ✓",      width="small"),
                "Notes":      st.column_config.TextColumn("Notes",      width="large"),
            },
        )

        explainer(
            "**An algorithm scanned the chart for recognised shapes** — flags, "
            "triangles, double tops, head-and-shoulders and similar. Each row is one "
            "shape it thinks it found.\n\n"
            "- **Dir** — ↑ the pattern points up, ↓ it points down.\n"
            "- **Confidence** — 0 to 1, how cleanly the shape matched. Only patterns "
            "at 0.55 and above are drawn on the chart above.\n"
            "- **Entry / Stop / Target** — the textbook price to act at, the price "
            "that would prove the pattern wrong, and the price the pattern projects.\n"
            "- **R/R** — reward-to-risk. 3.0:1 means the distance to target is three "
            "times the distance to the stop. Higher is better; below about 1.5:1 the "
            "trade needs to be right most of the time to pay.\n"
            "- **Vol ✓** — a tick means volume behaved the way the pattern expects, "
            "which is the single best filter on this table.\n\n"
            "**Be sceptical.** Pattern recognition finds shapes in random data too, "
            "and confidence measures how well the shape matched — not how likely it "
            "is to work. A 0.9 confidence pattern is a *cleaner drawing*, not a "
            "better bet. Use these as a prompt to look closer, and weight anything "
            "without volume confirmation very lightly."
        )

    # ── AI Analysis section ───────────────────────────────────────────────
```

**Note the indentation:** `explainer(` has **8 leading spaces** (inside `if ta_port_show_patterns and ta_patterns:`).

**Why:** the confidence number is routinely misread as a probability of success.

---

### Step B3: MPT section header — plain-language framing

**File:** `stock-dashboard/pages/9_portfolio.py`
**Find this exact block** (around line 1595, at the top of the MPT renderer):

```python
    st.markdown("### Modern Portfolio Theory Analysis")
    st.caption(
        "Pre-computes covariance, correlation, Sharpe ratio, beta, and optimal weights in Python, "
        "then Gemini 2.5 Pro interprets the results. Results cached 4 hours."
    )
```

**Replace it with:**

```python
    st.markdown("### Modern Portfolio Theory Analysis")
    st.caption(
        "Pre-computes covariance, correlation, Sharpe ratio, beta, and optimal weights in Python, "
        "then Gemini 2.5 Pro interprets the results. Results cached 4 hours."
    )

    explainer(
        "**The one idea behind all of this:** risk is not the sum of your individual "
        "stocks' risks. If two holdings tend to fall on the same days, owning both is "
        "barely safer than owning one. If they move independently, the combination is "
        "genuinely steadier than either alone. Everything below is machinery for "
        "measuring that.\n\n"
        "The **efficient frontier** is the curve of the best possible portfolios — "
        "for any level of risk you accept, the frontier is the highest return anyone "
        "could have got from these same holdings by weighting them differently. Your "
        "portfolio is plotted against it. Sitting *below the frontier* means some "
        "reshuffling of weights would have given you the same return with less "
        "swing — that is the entire finding, and it is a statement about the past "
        "year, not a prediction.\n\n"
        "**Two numbers do most of the work.** **Sharpe ratio** is return per unit of "
        "risk — above 1.0 is good, below 0.5 means you are being paid badly for the "
        "volatility you are living with. **Beta** is sensitivity to the market — 1.0 "
        "moves with the S&P, 1.5 moves half again as hard in both directions.\n\n"
        "All of it is computed from one year of daily returns, so it describes how "
        "these stocks behaved recently. Correlations in particular rise sharply in "
        "crashes, which is exactly when you were counting on them not to."
    )
```

**Note the indentation:** `explainer(` has **4 leading spaces**.

**Why:** MPT is the most jargon-dense feature in the app and the frontier concept is never defined anywhere on screen.

---

### Step B4: MPT metrics — the four portfolio-level cards

**File:** `stock-dashboard/pages/9_portfolio.py`
**Find this exact block** (inside `_render_mpt_metrics_tables`, around line 1870–1878):

```python
        help="Herfindahl index of weights. Lower = more diversified. Equal-weight N stocks = 1/N.",
    )

    # ── Per-ticker table ──────────────────────────────────────────────────
```

**Replace it with:**

```python
        help="Herfindahl index of weights. Lower = more diversified. Equal-weight N stocks = 1/N.",
    )

    explainer(
        "**These four numbers describe the whole portfolio, computed from one year "
        "of daily prices.**\n\n"
        "- **Expected Annual Return** — your holdings' last-12-month returns, "
        "combined at your current weights and annualised. It is backward-looking. "
        "The word \"expected\" is a statistics term, not a promise.\n"
        "- **Portfolio Volatility** — roughly how much the portfolio's value swings "
        "in a typical year. 20% means moves of ±20% are ordinary, not alarming.\n"
        "- **Sharpe Ratio** — return above cash, divided by that volatility. It "
        "answers \"am I being paid for the stress?\" Above 1.0 is good, 0.5–1.0 is "
        "ordinary, below 0.5 means a calmer portfolio would likely have done as "
        "well.\n"
        "- **HHI Concentration** — how much of the portfolio sits in a few names. "
        "The caption shows the equal-weight baseline for comparison; anything well "
        "above it means one or two positions dominate everything.\n\n"
        "**What to actually take away:** a high return with a low Sharpe means you "
        "got there by taking a lot of risk, and a bad year would hurt "
        "correspondingly. HHI well above baseline is the clearest actionable signal "
        "here — it means your results are really one stock's results."
    )

    # ── Per-ticker table ──────────────────────────────────────────────────
```

**Note the indentation:** `explainer(` has **4 leading spaces**.

**Why:** Sharpe and HHI are opaque without a scale to compare against.

---

### Step B5: MPT metrics — the correlation heatmap

**File:** `stock-dashboard/pages/9_portfolio.py`
**Find this exact block** (the end of `_render_mpt_metrics_tables`, around line 1921–1923):

```python
        styled_corr = corr_df.style.map(_corr_color).format("{:.3f}")
        st.dataframe(styled_corr, use_container_width=True,
                     height=_mpt_hdr_h + _mpt_row_h * len(corr_df))
```

**Replace it with:**

```python
        styled_corr = corr_df.style.map(_corr_color).format("{:.3f}")
        st.dataframe(styled_corr, use_container_width=True,
                     height=_mpt_hdr_h + _mpt_row_h * len(corr_df))

        explainer(
            "**Every cell answers one question: when this stock moves, does that one "
            "move with it?** Find a row, read across to a column, and the number is "
            "how tightly those two have moved together over the past year. **1.000** "
            "is perfect lockstep, **0** is no relationship at all, and negative means "
            "they tend to move in opposite directions. The diagonal is always 1.000 "
            "because every stock matches itself.\n\n"
            "**The colours:** red is 0.70 and above (these two are nearly the same "
            "bet), orange 0.40–0.70, yellow 0.10–0.40, green around zero (genuinely "
            "independent — this is what you want), blue below -0.10 (they hedge each "
            "other).\n\n"
            "**What to do with it:** a wall of red means your portfolio is less "
            "diversified than the number of tickers suggests, and it will move as one "
            "block on a bad day. Green and blue cells are where real diversification "
            "lives.\n\n"
            "**The honest caveat:** correlation is not causation, and these are "
            "one-year figures that shift over time. In a serious sell-off almost "
            "everything correlates towards 1.0 — the diversification you see here is "
            "weakest exactly when you need it most."
        )
```

**Note the indentation:** `explainer(` has **8 leading spaces** (inside `if len(tickers) >= 2:`).

**Why:** a coloured matrix of decimals is meaningless without the row/column reading instruction and the colour key.

---

### Step B6: Dashboard tab — Portfolio Performance chart vs SPY

**File:** `stock-dashboard/pages/9_portfolio.py`
**Find this exact block** (around line 3447–3451):

```python
            st.caption(
                "Current holdings held throughout period · normalized to first trading day · SPY for reference"
            )
```

**Replace it with:**

```python
            st.caption(
                "Current holdings held throughout period · normalized to first trading day · SPY for reference"
            )
            explainer(
                "**Both lines start at the same point on the left.** Everything is "
                "rebased to the first trading day of the period you picked above, so "
                "the vertical axis is percentage change since then, not dollars. "
                "That is the only fair way to compare a portfolio against an index "
                "of a different size.\n\n"
                "One line is your holdings; the **SPY line is the S&P 500**, the "
                "default \"what if I had just bought the whole market\" comparison. "
                "The gap between the lines at the right edge is the entire story: "
                "above SPY you beat the market over this window, below it you did "
                "not.\n\n"
                "**Two things to keep in mind.** First, this assumes you held today's "
                "positions for the whole period — it does not know when you actually "
                "bought, so it is not your real return. Second, change the period "
                "selector and the answer can flip completely; a portfolio can beat "
                "the market over one year and trail badly over three. Check at least "
                "two periods before concluding anything."
            )
```

**Note the indentation:** `explainer(` has **12 leading spaces** (inside `with _chart_col:` → `if close_df_perf is not None...`).

**Why:** the normalisation is the most misunderstood part — users read the chart as dollar value.

---

### Step B7: Dashboard tab — the two allocation donuts

**File:** `stock-dashboard/pages/9_portfolio.py`
**Find this exact block** (around line 3455–3462):

```python
    with _donut_col:
        fig_weights, fig_sectors = _build_allocation_charts(positions)
        section_header("Position Weights")
        st.plotly_chart(fig_weights, use_container_width=True)
        section_header("Sector Allocation")
        st.plotly_chart(fig_sectors, use_container_width=True)

    st.markdown("---")
```

**Replace it with:**

```python
    with _donut_col:
        fig_weights, fig_sectors = _build_allocation_charts(positions)
        section_header("Position Weights")
        st.plotly_chart(fig_weights, use_container_width=True)
        section_header("Sector Allocation")
        st.plotly_chart(fig_sectors, use_container_width=True)
        explainer(
            "**Two views of the same money.** The top ring splits your total value "
            "by individual position — each slice is one ticker's share of the "
            "portfolio. The bottom ring groups those same positions by industry "
            "sector, so several holdings can merge into one slice.\n\n"
            "Slice size is share of current market value, not what you paid. Hover "
            "any slice for the exact percentage.\n\n"
            "**Compare the two rings — that is the point.** The top one can look "
            "beautifully spread across eight names while the bottom one shows a "
            "single sector taking 60% of the ring. That is concentration hiding in "
            "plain sight: eight tech stocks is closer to one bet than eight. The "
            "correlation heatmap in the MPT section confirms whether that is "
            "actually true for your holdings.\n\n"
            "Positions the data source could not classify are grouped into an "
            "\"Unknown\" or \"Other\" slice — usually ETFs, cash, or options, not an "
            "error.",
            title="How to read these allocation rings",
        )

    st.markdown("---")
```

**Note the indentation:** `explainer(` has **8 leading spaces** (inside `with _donut_col:`). A single explainer covers both rings because this column is only one-third of the page width and two stacked dropdowns would crowd it; the custom `title` makes the plural scope clear.

**Why:** the top/bottom relationship is the insight, and it is invisible if each ring is explained separately.

---

### Step B8: Dashboard tab — Signal Matrix

**File:** `stock-dashboard/pages/9_portfolio.py`
**Find this exact block** (around line 3901–3910 — the column-config dict and the following comment make it unique):

```python
                "Smart Money":  st.column_config.TextColumn("Smart Money",  width="medium"),
            },
        )

    # ── Details expander — per-ticker news cards + MPT card ────────────────────
```

**Replace it with:**

```python
                "Smart Money":  st.column_config.TextColumn("Smart Money",  width="medium"),
            },
        )

        explainer(
            "**One row per holding, one column per independent source of opinion.** "
            "The point is not any single cell — it is whether the columns agree.\n\n"
            "- **News** — sentiment the AI read out of recent articles, scored -1.00 "
            "to +1.00.\n"
            "- **Reddit** — the same idea applied to retail chatter. Loud, fast, and "
            "frequently wrong, but it moves small caps.\n"
            "- **Options** — what the options market is positioned for, from the "
            "options analysis. Real money, and usually the least emotional column.\n"
            "- **Smart Money** — how many hedge funds hold it from 13F filings, and "
            "whether that holding is a straight bullish stake, a hedge, or a put. "
            "Note that 13Fs are filed up to 45 days late.\n\n"
            "**The marks:** ▲ green is positive, ▼ red is negative, ● yellow is "
            "neutral, and a grey **—** means that analysis has not been run yet "
            "(not that the signal is neutral — use ⚡ Analyze Everything to fill "
            "them in).\n\n"
            "**Reading it:** a row where all four point the same way is a real "
            "consensus. A row where news is green and options are red is the "
            "interesting case — it usually means the story is already priced in, or "
            "that people with money at stake disagree with the headlines. Disagreement "
            "is a reason to look closer, not a reason to average the cells together."
        )

    # ── Details expander — per-ticker news cards + MPT card ────────────────────
```

**Note the indentation:** `explainer(` has **8 leading spaces** (inside `if _sig_rows:`).

**Why:** four different scoring systems shown side by side with no key, and the "—" is easily misread as neutral.

---

### Step B9: Details expander — the MPT & Portfolio Analytics card

**File:** `stock-dashboard/pages/9_portfolio.py`
**Find this exact block** (around line 3933):

```python
            section_header("MPT & Portfolio Analytics")
            _mpt_r  = _mpt_ss.get("result", {})
```

**Replace it with:**

```python
            section_header("MPT & Portfolio Analytics")
            explainer(
                "**A three-number summary of the full MPT analysis** (the detailed "
                "version lives in the Options & MPT tab).\n\n"
                "The coloured banner is an overall grade for how efficiently your "
                "portfolio is built, plus a rebalancing priority — how urgently the "
                "model thinks the weights need changing.\n\n"
                "- **Return** — the last twelve months, annualised, at your current "
                "weights. Backward-looking.\n"
                "- **Volatility** — how much the portfolio typically swings in a "
                "year. 20% means ±20% moves are normal.\n"
                "- **Sharpe** — return per unit of that volatility. Above 1.0 is "
                "good; below 0.5 says you are taking on swings you are not being "
                "paid for.\n\n"
                "The rebalancing actions underneath are the model's suggested weight "
                "changes, coloured green to increase and red to reduce. They are "
                "optimised against the **past** year of price behaviour, which is a "
                "real limitation — an optimiser will happily tell you to pile into "
                "whatever happened to do well recently. Treat them as a prompt to "
                "check your concentration, not a trade list."
            )
            _mpt_r  = _mpt_ss.get("result", {})
```

**Note the indentation:** `explainer(` has **12 leading spaces** (inside `with st.expander(...)` → `if _mpt_ss:`).

**Why:** this compact card is where most users first meet Sharpe, with no explanation attached.

---

### Step B10: Options analysis — the five metric cards

**File:** `stock-dashboard/pages/9_portfolio.py`
**Find this exact block** (inside `_render_options_analysis`, around line 3302–3305):

```python
    m5.metric("Net Dealer GEX", f"${net_gex_val/1e6:.2f}M" if net_gex_val is not None else "N/A",
              help="Positive = dealers long gamma (pinning). Negative = vol amplification.")

    st.markdown("---")
```

**Replace it with:**

```python
    m5.metric("Net Dealer GEX", f"${net_gex_val/1e6:.2f}M" if net_gex_val is not None else "N/A",
              help="Positive = dealers long gamma (pinning). Negative = vol amplification.")

    explainer(
        "**Five readings taken from the whole options chain for this expiry.** They "
        "describe how other people are positioned — not what the stock is worth.\n\n"
        "- **P/C OI Ratio** — puts versus calls among all contracts currently open. "
        "Above 1.0 means more puts exist than calls, usually read as caution or "
        "hedging. Around 0.7–1.0 is ordinary for most large stocks.\n"
        "- **P/C Vol Ratio** — the same ratio for *today's* trading only. This is the "
        "fast-moving one; a big gap between it and the OI ratio means today's "
        "positioning is unusual.\n"
        "- **Max Pain** — the price at which the largest dollar amount of options "
        "would expire worthless. The caption shows the distance from the current "
        "price. Prices sometimes drift towards it in the last day or two of an "
        "expiry. This is a weak, much-overhyped effect.\n"
        "- **IV Skew** — how much more traders are paying for downside protection "
        "than for upside. Positive means fear is priced in; a large positive skew "
        "means puts are expensive right now.\n"
        "- **Net Dealer GEX** — positive tends to dampen moves (market makers sell "
        "rallies and buy dips to stay hedged); negative tends to amplify them.\n\n"
        "**All five are positioning, not prediction.** Crowded positioning gets "
        "unwound as often as it gets confirmed, and every number here resets at "
        "expiry."
    )

    st.markdown("---")
```

**Note the indentation:** `explainer(` has **4 leading spaces**.

**Why:** this single function renders the metrics in both the per-ticker analysis and the "Options Analysis Results" summary expanders, so one insertion covers both surfaces.

---

# Part C — `stock-dashboard/pages/10_technical_analysis.py`

### Step C0: Add `explainer` to the imports

**File:** `stock-dashboard/pages/10_technical_analysis.py`
**Find this exact line** (line 12):

```python
from components.ui import inject_global_css, page_header, render_sidebar_nav
```

**Replace it with:**

```python
from components.ui import explainer, inject_global_css, page_header, render_sidebar_nav
```

**Why:** required before either of the following steps will run.

---

### Step C1: The candlestick chart and volume subplot

**File:** `stock-dashboard/pages/10_technical_analysis.py`
**Find this exact block** (around line 971–974):

```python
    st.plotly_chart(fig, use_container_width=True)

    # ── Footer ────────────────────────────────────────────────────────────
```

**Replace it with:**

```python
    st.plotly_chart(fig, use_container_width=True)

    explainer(
        "**Each candle is one time period** — one day on the daily view, five "
        "minutes on the 1D view. The thick body runs between the opening and closing "
        "price; the thin wicks show the highest and lowest price touched. **Green "
        "closed above its open, red closed below.** Long bodies mean conviction; "
        "small bodies with long wicks mean the period was fought over and settled "
        "nowhere. The panel underneath is volume, coloured to match — a big move on "
        "low volume is much less convincing than the same move on heavy volume.\n\n"
        "**EMA lines (9 / 21 / 50)** are smoothed averages of price that weight "
        "recent bars more heavily, so they turn faster than a plain average. The "
        "standard read is their order: short line above long line and both rising is "
        "an uptrend. When the short crosses *below* the long, that is the classic "
        "trend-change warning — and also the classic false alarm in a sideways "
        "market.\n\n"
        "**Bollinger Bands** are the shaded channel, two standard deviations either "
        "side of a 20-period average. Wide bands mean a volatile stretch, a narrow "
        "squeeze means a quiet one and often precedes a large move — though the "
        "bands give no hint which way. Touching the upper band means \"high relative "
        "to its own recent range\", which in a strong uptrend can go on for weeks. It "
        "is not a sell signal.\n\n"
        "Every one of these is a description of what price has already done. None of "
        "them knows anything about the company."
    )

    # ── Footer ────────────────────────────────────────────────────────────
```

**Note the indentation:** `explainer(` has **4 leading spaces**.

**Why:** this is the page's primary chart, layering three separate visual conventions with no on-screen key.

**Do not** add an RSI or MACD explainer on this page. `_build_chart()` builds a two-row subplot of **price and volume only** — there are no RSI or MACD panels in this codebase. The volume panel is covered by the text above.

---

### Step C2: Pattern Detection table

**File:** `stock-dashboard/pages/10_technical_analysis.py`
**Find this exact block** (around line 1058–1063 — the `"Vol ✓"` config plus the detail-expander comment make it unique):

```python
            "Vol":        st.column_config.TextColumn("Vol ✓",      width="small"),
            "Notes":      st.column_config.TextColumn("Notes",      width="large"),
        },
    )

    # Detail expander for highest-confidence pattern
```

**Replace it with:**

```python
            "Vol":        st.column_config.TextColumn("Vol ✓",      width="small"),
            "Notes":      st.column_config.TextColumn("Notes",      width="large"),
        },
    )

    explainer(
        "**An algorithm scanned the price history for classic chart shapes** — "
        "flags, wedges, triangles, double tops and bottoms, head-and-shoulders. Each "
        "row is one shape it believes it found, best match first. Patterns scoring "
        "0.55 or higher are also drawn on the chart above.\n\n"
        "- **Dir** — ↑ the shape points higher, ↓ it points lower.\n"
        "- **Confidence** — 0 to 1, how cleanly the price action matched the "
        "textbook shape.\n"
        "- **Entry** — the price at which the pattern is considered triggered.\n"
        "- **Stop** — the price that would show the pattern has failed.\n"
        "- **Target** — where the pattern projects price to go.\n"
        "- **R/R** — how far the target is compared with how far the stop is. 3.0:1 "
        "means three units of potential gain per unit risked; under about 1.5:1 the "
        "setup has to work most of the time just to break even.\n"
        "- **Vol ✓** — volume behaved as the pattern requires. This is the most "
        "useful filter in the table; patterns without it fail far more often.\n\n"
        "**The caveat that matters:** confidence measures how neat the drawing is, "
        "not the odds of it working. Random price data produces these shapes too. "
        "Use a detected pattern as a reason to examine the chart yourself, and give "
        "real weight only to ones with volume confirmation and a sensible R/R."
    )

    # Detail expander for highest-confidence pattern
```

**Note the indentation:** `explainer(` has **4 leading spaces** (function body level of `_render_pattern_section`).

**Why:** identical reasoning to Step B2 — the confidence score is read as a win probability.

---

## Verification

Run these in order from `stock-dashboard/` with the venv active.

### 1. Syntax check — do this before launching anything

```powershell
python -m py_compile pages/13_seasonality.py pages/9_portfolio.py pages/10_technical_analysis.py
```

Expected: no output at all. If you get a `SyntaxError` or `IndentationError`, you almost certainly broke the string-literal rule at the top of this plan — go back to the offending step and check that (a) no line break sits between two quotes, and (b) no continuation line begins with a bare `"` at column 0.

### 2. Import check

```powershell
python -c "from components.ui import explainer; print('ok')"
```

Expected: `ok`.

### 3. Launch and click through

```powershell
streamlit run dashboard.py
```

| # | Page | Action | Expected |
|---|---|---|---|
| 1 | Seasonality → Outlook | Look under the four metric cards | A collapsed `❓ How to read this` row. Click it — it expands with the ±100 explanation. |
| 2 | Seasonality → Outlook | Scroll to the sector cards | A second dropdown below the last card (only appears if any sector scores > 10). |
| 3 | Seasonality → Seasonality | Scroll to the month table | Dropdown under the table, and a second one under the Q1–Q4 row. |
| 4 | Seasonality → Macro Regime | Below the four bars | Dropdown present; one more under the favours/pressures boxes; one more under the indicator table. |
| 5 | Seasonality → Sector Rotation | Under each of the two tables | One dropdown each. |
| 6 | Seasonality → Calendar | Between the filter and the event list | One dropdown. |
| 7 | Portfolio → Dashboard | Under the performance chart | Dropdown present, text is readable, chart is unchanged. |
| 8 | Portfolio → Dashboard | Under the two donuts | One dropdown titled `❓ How to read these allocation rings`. |
| 9 | Portfolio → Dashboard | Under the Signal Matrix | One dropdown. |
| 10 | Portfolio → Dashboard | Open "Details — per-ticker news & MPT analytics" | Dropdown directly under the MPT & Portfolio Analytics header (only if MPT has been run). |
| 11 | Portfolio → Options & MPT | Scroll to the MPT header | Dropdown under the caption, above the Run button. |
| 12 | Portfolio → Options & MPT | Run MPT, then look at the metrics and heatmap | One dropdown under the four cards, one under the correlation heatmap. |
| 13 | Portfolio → Technical Analysis | Under the candle chart | Dropdown present; another under the pattern table when patterns are found. |
| 14 | Technical Analysis page | Under the candle chart and pattern table | One dropdown each. |
| 15 | Screener page | Open it | **Unchanged.** If anything on this page is different, you edited the wrong file — revert it. |

### 4. Edge cases

- Load the Seasonality page with **no holdings** — the explainers in empty sections should simply not render (they are all inside `if` blocks that already guard the visual). No errors, no orphan dropdowns above an "unavailable" info box.
- Open the Portfolio Technical Analysis tab with **pattern detection toggled off** — the pattern explainer must not appear.
- Open Portfolio before running any AI analysis — the Signal Matrix explainer should still render (the table renders with `—` cells), while the MPT card explainer should not (no MPT result yet).

**Failure looks like:** a dropdown appearing where its chart or table did not render, a Streamlit exception page, or any change in a number, colour, or chart. All three of those mean an insertion landed at the wrong indentation level.

---

## Rollback Plan

All changes are additive and confined to three files. To undo everything:

```powershell
git checkout -- stock-dashboard/pages/10_technical_analysis.py
git diff stock-dashboard/pages/9_portfolio.py
git diff stock-dashboard/pages/13_seasonality.py
```

`10_technical_analysis.py` and `9_portfolio.py` are tracked and unmodified/committed in places, so `git checkout --` restores them. `13_seasonality.py` is **untracked** (`??` in git status) — `git checkout` will not restore it. To undo a single bad insertion there, delete the `explainer(...)` call block you added and nothing else; every step in Part A is a pure insertion between two unchanged lines, so removing the inserted block returns the file to its previous state.

To roll back one step only: delete the `explainer(` line, its string literals, and its closing `)`. Nothing else in this plan depends on it.
