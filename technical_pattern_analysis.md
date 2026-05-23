# Technical Chart Pattern Analysis
## Implementation Spec for Python/Streamlit Stock Dashboard

> **Version:** 1.0 | **Date:** 2026-05-22  
> **Purpose:** Complete algorithmic specification for detecting BOS, CHoCH, and breakout patterns from OHLCV data in a pandas-based pipeline.

---

## Table of Contents

1. [Part 1 — Break of Structure (BOS) & Market Structure](#part-1)
   - 1.1 What is Break of Structure?
   - 1.2 Bullish BOS — Exact Criteria
   - 1.3 Bearish BOS — Exact Criteria
   - 1.4 Change of Character (CHoCH)
   - 1.5 Algorithmic Swing High/Low Detection
   - 1.6 Key Levels: Order Blocks, FVGs, Support/Resistance

2. [Part 2 — Breakout Patterns](#part-2)
   - 2.1 Classic Resistance/Support Breakout
   - 2.2 Bull Flag & Bear Flag
   - 2.3 Pennant
   - 2.4 Ascending Triangle
   - 2.5 Descending Triangle
   - 2.6 Symmetrical Triangle
   - 2.7 Rising Wedge
   - 2.8 Falling Wedge
   - 2.9 Cup and Handle
   - 2.10 Head and Shoulders
   - 2.11 Inverse Head and Shoulders
   - 2.12 Double Top
   - 2.13 Double Bottom
   - 2.14 Range Consolidation Breakout

3. [Part 3 — Algorithmic Implementation Guide](#part-3)
   - 3.1 DataFrame Conventions
   - 3.2 Detected Pattern Data Structure
   - 3.3 Confidence Scoring
   - 3.4 Detection Order & Layering

4. [Part 4 — Implementation Priority](#part-4)
   - 4.1 Pattern Reliability Rankings
   - 4.2 Recommended Build Sequence

---

<a name="part-1"></a>
# Part 1 — Break of Structure (BOS) & Market Structure

---

## 1.1 What is Break of Structure?

Break of Structure (BOS) is a market structure concept from Smart Money / ICT methodology. It describes the moment when price decisively breaks through a prior swing high or swing low, confirming that the prevailing trend is intact and likely to continue.

BOS is **not** a reversal signal — it is a **continuation** signal. It tells you the market is still respecting the existing trend direction.

The core principle: healthy trends are defined by sequences of higher highs + higher lows (bullish) or lower highs + lower lows (bearish). A BOS occurs when price breaks the most recent significant swing point in the direction of the trend, confirming that the structure is continuing.

### BOS vs. CHoCH (Change of Character)

| Event | Meaning | Implication |
|-------|---------|-------------|
| BOS (Bullish) | Price breaks above the most recent swing high | Trend continuation — still bullish |
| BOS (Bearish) | Price breaks below the most recent swing low | Trend continuation — still bearish |
| CHoCH (Bullish → Bearish) | In an uptrend, price breaks below the most recent swing low | Possible trend reversal — watch for confirmation |
| CHoCH (Bearish → Bullish) | In a downtrend, price breaks above the most recent swing high | Possible trend reversal — watch for confirmation |

---

## 1.2 Bullish BOS — Exact Criteria

A **Bullish BOS** confirms the uptrend is continuing.

### Required Market Condition
The market must already be in a bullish structure: at least **two confirmed swing lows where each successive swing low is higher than the prior one** (Higher Low sequence).

### Detection Criteria (All must be true)

1. **Prior structure is bullish:** Identify the sequence of swing highs (SH) and swing lows (SL). The most recent confirmed SL must be higher than the SL before it: `SL[n] > SL[n-1]`.

2. **Swing high target identified:** The most recent confirmed swing high (`SH[n]`) becomes the BOS target level. This is the price level that, when broken, constitutes a BOS.

3. **Candle close above the swing high:** A candle closes above `SH[n]` by at least a minimum threshold:
   ```
   close[i] > SH[n] * (1 + min_breakout_pct)
   ```
   where `min_breakout_pct` is typically `0.001` to `0.003` (0.1%–0.3%) to filter wicks.

4. **The break is not a wick — it is a close:** A wick above the level does NOT count as BOS. Only a candle body close (i.e., `close[i]`, not `high[i]`) crossing the level triggers BOS.

5. **Volume confirmation (optional but recommended):** The breaking candle's volume is above the N-bar average:
   ```
   volume[i] > volume[-N:].mean() * volume_multiplier
   ```
   where `N=20`, `volume_multiplier=1.2`.

6. **Time window:** The BOS must occur within a reasonable number of bars after the swing high was established. Typically within `max_bars_to_break = 50` bars. If it takes more than 50 bars, the swing high has likely already been consumed by lower structure and should be discarded.

### What a Bullish BOS Looks Like
```
Price:
        SH[n]  ←─── BOS level (must close above this)
         ↑
   /\/\/\  ←─── consolidation / pullback forming HLs
  /        \
SL[n-1]   SL[n]  ← Higher Low (HL) confirms uptrend

After BOS:
Price continues up:   /\/\/\/\/\   new SH[n+1]
```

### Parameters
| Parameter | Default | Range |
|-----------|---------|-------|
| `swing_lookback` | 5 | 3–10 bars |
| `min_breakout_pct` | 0.001 | 0.0005–0.003 |
| `volume_multiplier` | 1.2 | 1.0–2.0 |
| `max_bars_to_break` | 50 | 20–100 |

---

## 1.3 Bearish BOS — Exact Criteria

A **Bearish BOS** confirms the downtrend is continuing.

### Required Market Condition
The market must already be in a bearish structure: at least **two confirmed swing highs where each successive swing high is lower than the prior one** (Lower High sequence).

### Detection Criteria (All must be true)

1. **Prior structure is bearish:** The most recent confirmed SH must be lower than the SH before it: `SH[n] < SH[n-1]`.

2. **Swing low target identified:** The most recent confirmed swing low (`SL[n]`) becomes the BOS target level.

3. **Candle close below the swing low:**
   ```
   close[i] < SL[n] * (1 - min_breakout_pct)
   ```

4. **Close-based break only:** Only candle body close counts, not wicks.

5. **Volume confirmation:**
   ```
   volume[i] > volume[-N:].mean() * volume_multiplier
   ```

6. **Time window:** Break occurs within `max_bars_to_break` bars after swing low was established.

---

## 1.4 Change of Character (CHoCH)

CHoCH is the **first warning** that the trend may be reversing. It is structurally identical to a BOS but occurs **against** the prevailing trend direction.

### Bullish CHoCH (Bearish → Potential Bullish Reversal)

Occurs during a confirmed **downtrend** (sequence of LHs and LLs), when:

1. Price makes a **new confirmed swing low** (`SL[n]`), but then:
2. Price **closes above the most recent swing high** (`SH[n]`) — the swing high that was established between `SL[n-1]` and `SL[n]`.

```
  SH[n]  ← CHoCH level (close above this = CHoCH signal)
   ↑
SL[n-1]  \  /  ← price bounces and breaks above SH[n]
            SL[n]  ← lower low (downtrend appeared intact)
```

Exact criteria:
```
bearish_structure_confirmed = (SH[n] < SH[n-1]) AND (SL[n] < SL[n-1])
choch_bullish = close[i] > SH[n] * (1 + min_breakout_pct)
             AND bearish_structure_confirmed
             AND i occurs AFTER SL[n] is confirmed
```

### Bearish CHoCH (Bullish → Potential Bearish Reversal)

Occurs during a confirmed **uptrend** (sequence of HHs and HLs), when:

1. Price makes a **new confirmed swing high** (`SH[n]`), but then:
2. Price **closes below the most recent swing low** (`SL[n]`) — the swing low established between `SH[n-1]` and `SH[n]`.

```
          SH[n]  ← new high (uptrend appeared intact)
         /
SH[n-1]/
        \      ← price fails and breaks below SL[n]
         SL[n]  ← CHoCH level (close below this = CHoCH)
```

Exact criteria:
```
bullish_structure_confirmed = (SL[n] > SL[n-1]) AND (SH[n] > SH[n-1])
choch_bearish = close[i] < SL[n] * (1 - min_breakout_pct)
             AND bullish_structure_confirmed
             AND i occurs AFTER SH[n] is confirmed
```

### CHoCH vs. BOS — Disambiguation Table

| Signal | In which trend? | Which level breaks? | Implication |
|--------|----------------|---------------------|-------------|
| Bullish BOS | Uptrend | Prior swing high | Continue long |
| Bearish BOS | Downtrend | Prior swing low | Continue short |
| Bullish CHoCH | Downtrend | Prior swing high | Watch for reversal up |
| Bearish CHoCH | Uptrend | Prior swing low | Watch for reversal down |

**Important:** A single CHoCH is insufficient for a confirmed reversal. Look for CHoCH **followed by** a BOS in the new direction: CHoCH (bearish) → new HL forms → BOS above the CHoCH high = confirmed bullish reversal.

---

## 1.5 Algorithmic Swing High/Low Detection

This is the foundational computation. Every pattern detection module depends on accurate swing point identification.

### The Left/Right Bar Lookback Method

A bar at index `i` is a **Swing High** if:
```python
is_swing_high(i, n) = all(high[i] > high[i-k] for k in range(1, n+1))  # left bars
                    AND all(high[i] > high[i+k] for k in range(1, n+1))  # right bars
```

A bar at index `i` is a **Swing Low** if:
```python
is_swing_low(i, n) = all(low[i] < low[i-k] for k in range(1, n+1))   # left bars
                   AND all(low[i] < low[i+k] for k in range(1, n+1))   # right bars
```

Where `n` is the lookback/lookahead window (typically `n = 5` for swing detection on daily charts).

**Critical implementation note:** Because the right-bar check requires `n` future bars to be seen, swing highs and lows are confirmed with a **lag of `n` bars**. On a live chart, the most recent `n` bars are always "pending" — you cannot confirm a swing in real-time without look-ahead bias.

### Python Implementation

```python
import pandas as pd
import numpy as np

def find_swing_highs_lows(df: pd.DataFrame, n: int = 5) -> pd.DataFrame:
    """
    Detect swing highs and lows using left/right bar lookback method.
    
    Parameters
    ----------
    df : pd.DataFrame with columns ['open', 'high', 'low', 'close', 'volume']
    n  : int — number of bars to left AND right that must be exceeded
    
    Returns
    -------
    df with added columns: 'swing_high' (price or NaN), 'swing_low' (price or NaN)
    """
    highs = df['high'].values
    lows  = df['low'].values
    length = len(df)
    
    swing_high = np.full(length, np.nan)
    swing_low  = np.full(length, np.nan)
    
    for i in range(n, length - n):
        # Swing High check
        left_ok  = all(highs[i] > highs[i - k] for k in range(1, n + 1))
        right_ok = all(highs[i] > highs[i + k] for k in range(1, n + 1))
        if left_ok and right_ok:
            swing_high[i] = highs[i]
        
        # Swing Low check
        left_ok  = all(lows[i] < lows[i - k] for k in range(1, n + 1))
        right_ok = all(lows[i] < lows[i + k] for k in range(1, n + 1))
        if left_ok and right_ok:
            swing_low[i] = lows[i]
    
    df = df.copy()
    df['swing_high'] = swing_high
    df['swing_low']  = swing_low
    return df
```

### Filtering Swing Points — Significance Threshold

Not all detected swings are meaningful. Apply a minimum price distance filter:

```python
def filter_significant_swings(df: pd.DataFrame, min_swing_pct: float = 0.005) -> pd.DataFrame:
    """
    Remove swing points that are too close to the prior swing of the same type.
    
    min_swing_pct : minimum % move from prior swing to be considered significant
                    Default 0.5% — eliminates noise swings
    """
    sh_indices = df.index[df['swing_high'].notna()].tolist()
    sl_indices = df.index[df['swing_low'].notna()].tolist()
    
    # Filter swing highs
    filtered_sh = []
    for idx in sh_indices:
        if not filtered_sh:
            filtered_sh.append(idx)
        else:
            prev = filtered_sh[-1]
            price_diff_pct = abs(df.loc[idx, 'swing_high'] - df.loc[prev, 'swing_high']) \
                             / df.loc[prev, 'swing_high']
            if price_diff_pct >= min_swing_pct:
                filtered_sh.append(idx)
            else:
                # Keep the higher one
                if df.loc[idx, 'swing_high'] > df.loc[prev, 'swing_high']:
                    filtered_sh[-1] = idx
    
    # Filter swing lows (keep lower of duplicates within threshold)
    filtered_sl = []
    for idx in sl_indices:
        if not filtered_sl:
            filtered_sl.append(idx)
        else:
            prev = filtered_sl[-1]
            price_diff_pct = abs(df.loc[idx, 'swing_low'] - df.loc[prev, 'swing_low']) \
                             / df.loc[prev, 'swing_low']
            if price_diff_pct >= min_swing_pct:
                filtered_sl.append(idx)
            else:
                if df.loc[idx, 'swing_low'] < df.loc[prev, 'swing_low']:
                    filtered_sl[-1] = idx
    
    # Mask out non-significant swings
    df = df.copy()
    df.loc[~df.index.isin(filtered_sh), 'swing_high'] = np.nan
    df.loc[~df.index.isin(filtered_sl), 'swing_low']  = np.nan
    return df
```

### Multi-Timeframe Swing Detection

Use different `n` values to capture swings of different magnitudes:

| Timeframe Context | `n` value | Captures |
|-------------------|-----------|---------|
| Micro swings | 2–3 | Short-term noise swings |
| Standard swings | 5 | Default structure swings |
| Major swings | 10–15 | Significant structural pivots |
| Primary trend | 20+ | Major trend-defining pivots |

---

## 1.6 Key Levels — Order Blocks, Fair Value Gaps, Support/Resistance

### Order Blocks (OB)

An order block is the **last bullish (or bearish) candle before a significant impulsive move** in the opposite direction. It represents a zone where institutional orders were placed.

**Bullish Order Block:** The last **bearish** candle before a bullish impulse that results in a BOS above a swing high.

Detection:
```python
def find_bullish_order_blocks(df: pd.DataFrame, swing_n: int = 5,
                               min_impulse_pct: float = 0.02) -> list:
    """
    Bullish OB = last bearish candle (close < open) immediately before a bullish
    impulse leg that breaks a prior swing high.
    
    Returns list of dicts: {bar_index, ob_high, ob_low, ob_mid, broken: bool}
    """
    order_blocks = []
    bos_bars = df.index[df['bullish_bos'] == True].tolist()  # pre-computed BOS flags
    
    for bos_bar in bos_bars:
        # Walk back to find the start of the impulse leg
        i = df.index.get_loc(bos_bar)
        while i > 0:
            i -= 1
            bar = df.iloc[i]
            if bar['close'] < bar['open']:  # bearish candle
                order_blocks.append({
                    'bar_index'  : df.index[i],
                    'ob_high'    : bar['high'],
                    'ob_low'     : bar['low'],
                    'ob_mid'     : (bar['high'] + bar['low']) / 2,
                    'direction'  : 'bullish',
                    'broken'     : False,
                })
                break  # take only the LAST bearish candle before impulse
    
    return order_blocks
```

**Bearish Order Block:** The last **bullish** candle before a bearish impulse that results in a BOS below a swing low.

### Fair Value Gaps (FVG / Imbalance)

A Fair Value Gap (also called an imbalance or liquidity void) is a three-candle pattern where the wicks of candle 1 and candle 3 do NOT overlap, leaving a gap in price action.

**Bullish FVG:**
```
Condition:  low[i] > high[i-2]
Gap zone:   high[i-2]  to  low[i]
```

**Bearish FVG:**
```
Condition:  high[i] < low[i-2]
Gap zone:   high[i]  to  low[i-2]
```

Detection:
```python
def find_fvgs(df: pd.DataFrame, min_gap_pct: float = 0.001) -> pd.DataFrame:
    """
    Detect Fair Value Gaps (three-candle imbalances).
    
    min_gap_pct : minimum gap size as % of price (filter noise)
    """
    fvgs = []
    highs = df['high'].values
    lows  = df['low'].values
    closes= df['close'].values
    
    for i in range(2, len(df)):
        # Bullish FVG: candle i's low is above candle i-2's high
        if lows[i] > highs[i-2]:
            gap_size_pct = (lows[i] - highs[i-2]) / highs[i-2]
            if gap_size_pct >= min_gap_pct:
                fvgs.append({
                    'bar_index' : df.index[i],
                    'direction' : 'bullish',
                    'fvg_low'   : highs[i-2],
                    'fvg_high'  : lows[i],
                    'fvg_mid'   : (highs[i-2] + lows[i]) / 2,
                    'filled'    : False,
                })
        
        # Bearish FVG: candle i's high is below candle i-2's low
        elif highs[i] < lows[i-2]:
            gap_size_pct = (lows[i-2] - highs[i]) / lows[i-2]
            if gap_size_pct >= min_gap_pct:
                fvgs.append({
                    'bar_index' : df.index[i],
                    'direction' : 'bearish',
                    'fvg_low'   : highs[i],
                    'fvg_high'  : lows[i-2],
                    'fvg_mid'   : (highs[i] + lows[i-2]) / 2,
                    'filled'    : False,
                })
    
    return fvgs
```

An FVG is **filled** when subsequent price action trades through the entire gap zone (high side for bearish FVG, low side for bullish FVG). Unfilled FVGs act as magnet levels for price.

### Support and Resistance from Prior Structure

Horizontal S/R levels are derived from **swing highs and lows** with clustering:

```python
def cluster_sr_levels(df: pd.DataFrame, n_clusters: int = 10,
                       tolerance_pct: float = 0.005) -> list:
    """
    Cluster swing highs and lows into horizontal S/R zones.
    
    tolerance_pct : two levels within this % of each other are merged into one zone
    """
    sh_levels = df['swing_high'].dropna().values.tolist()
    sl_levels = df['swing_low'].dropna().values.tolist()
    all_levels = sorted(sh_levels + sl_levels)
    
    zones = []
    for level in all_levels:
        merged = False
        for zone in zones:
            if abs(level - zone['mid']) / zone['mid'] < tolerance_pct:
                zone['touches'] += 1
                zone['levels'].append(level)
                zone['mid'] = np.mean(zone['levels'])
                merged = True
                break
        if not merged:
            zones.append({'mid': level, 'touches': 1, 'levels': [level]})
    
    # Sort by number of touches (most tested = strongest)
    zones.sort(key=lambda z: z['touches'], reverse=True)
    return zones
```

**Zone strength scoring:**
- 1–2 touches → weak level
- 3–4 touches → moderate level
- 5+ touches → strong level (high confluence)

---

<a name="part-2"></a>
# Part 2 — Breakout Patterns

All patterns below assume the DataFrame `df` has columns: `open`, `high`, `low`, `close`, `volume` with a DatetimeIndex.

---

## 2.1 Classic Resistance/Support Breakout

### Description
The simplest and most fundamental breakout pattern. Price consolidates below a horizontal resistance level (or above support), then breaks through it with conviction. The former resistance becomes new support (and vice versa for support breakouts).

Visually:
```
─────────────────  ← resistance line
  /\/\/\/\/\        ← consolidation under resistance
Price breaks:
─────────────────
                 /  ← breakout candle closes above
```

### Detection Criteria

**Resistance Breakout (Bullish):**

1. **Identify resistance level:** Find the highest swing high within the lookback window of `lookback_bars` (default: 50):
   ```python
   resistance = df['high'][-lookback_bars:].max()
   resistance_bar = df['high'][-lookback_bars:].idxmax()
   ```

2. **Consolidation period:** Price must have touched or approached the resistance level at least `min_touches = 2` times without closing above it, over the past `lookback_bars` bars.
   
   Define "touch" as: `high[i] >= resistance * (1 - touch_tolerance)` where `touch_tolerance = 0.005` (0.5%).
   
   Count touches: `touches = sum(1 for i in range(-lookback_bars, 0) if high[i] >= resistance * 0.995)`

3. **Breakout candle:** The most recent candle closes above resistance:
   ```python
   close[-1] > resistance * (1 + min_breakout_pct)  # min_breakout_pct = 0.002
   ```
   AND the candle body (not just wick) closes above: `close[-1] > resistance`

4. **Volume confirmation:** 
   ```python
   volume[-1] > volume[-20:].mean() * 1.5
   ```

5. **Consolidation below resistance:** In the `lookback_bars` bars prior to breakout, at least 70% of closes must be below resistance: 
   ```python
   pct_below = sum(1 for c in df['close'][-lookback_bars:-1] if c < resistance) / (lookback_bars - 1)
   pct_below >= 0.70
   ```

**Support Breakout (Bearish):** Mirror all criteria using `low` and `min()`.

### Key Parameters
| Parameter | Default | Description |
|-----------|---------|-------------|
| `lookback_bars` | 50 | Window to find resistance level |
| `min_touches` | 2 | Min number of resistance tests |
| `touch_tolerance` | 0.005 | % proximity to count as a touch |
| `min_breakout_pct` | 0.002 | Min close above resistance |
| `volume_multiplier` | 1.5 | Volume vs. 20-bar average |
| `pct_below_threshold` | 0.70 | Min % of prior candles below resistance |

### Confirmation Signals

**Validates the pattern:**
- Breakout candle is large-bodied (body is >60% of total range): `abs(close - open) / (high - low) > 0.6`
- Next candle retests resistance-turned-support and bounces
- Volume is expanding over the prior 3 days heading into the breakout
- Breakout occurs after 10+ bars of tight consolidation (ATR contracting)

**Invalidates the pattern (false breakout signals):**
- Candle closes back below resistance within 2 bars → stop-hunt / false breakout
- Volume is below average on the breakout candle
- Wide spread between high and close (long upper wick) = rejection
- Breakout happens after <3 bars of consolidation (not enough base)

### Risk/Reward Profile

- **Entry:** On candle close above resistance, or on retest of resistance-as-support
- **Stop Loss:** Below the most recent swing low before breakout, OR below resistance level: `stop = resistance * (1 - stop_buffer_pct)` where `stop_buffer_pct = 0.005`
- **Target:** Measure the height of the consolidation range and project upward:
  ```python
  consolidation_height = resistance - df['low'][-lookback_bars:].min()
  target = resistance + consolidation_height
  ```
- **Typical R/R:** 1.5:1 to 3:1 depending on consolidation depth

### False Signal Scenarios
- **News spikes:** Price briefly spikes above resistance on news, then reverses. Filter: require candle to CLOSE above, not just wick above.
- **End-of-day/low volume breakouts:** Occur in last 30 min of session or on low volume — often fade overnight.
- **Overextended run-up:** Price already up 10%+ before reaching resistance — less buyers left, more likely to fail.
- **Broad market weakness:** A stock breaking out while the index is down 2% has a high failure rate.

---

## 2.2 Bull Flag & Bear Flag

### Description

**Bull Flag:** A sharp, near-vertical upward move (the "flagpole"), followed by a brief downward-sloping or sideways channel (the "flag"), then a breakout above the flag's upper boundary in the direction of the original move.

```
         /|  ← flagpole (fast, strong move)
        / |
       /  |  ────  ← flag upper boundary (descending)
      /    \  /
     /      \/    ← flag consolidation (parallel channel, slightly down)
    /       ────  ← flag lower boundary (descending)
```

**Bear Flag:** Mirror of bull flag — sharp drop (flagpole), brief upward-sloping consolidation (flag), then breakdown below flag's lower boundary.

### Detection Criteria — Bull Flag

**Step 1: Identify the Flagpole**
```python
# Flagpole = sharp bullish impulse
# Conditions:
pole_bars = 3 to 10  # flagpole must form in 3–10 bars
pole_move_pct = (high_of_pole - low_of_pole) / low_of_pole

# Minimum flagpole height:
pole_move_pct >= 0.05  # at least 5% move (daily), or 2% intraday

# Slope of flagpole (average gain per bar):
avg_gain_per_bar = pole_move_pct / pole_bars
avg_gain_per_bar >= 0.01  # at least 1% per bar average = steep enough
```

**Step 2: Identify the Flag**
```python
# Flag must form AFTER the flagpole
flag_start = bar where flagpole ended (highest close of pole)
flag_bars = 5 to 25  # flag duration

# Flag must drift DOWN (or sideways) against the pole
# Use linear regression on closes during flag period:
from numpy.polynomial import polynomial as P
flag_closes = df['close'][flag_start : flag_start + flag_bars]
slope, intercept = np.polyfit(range(len(flag_closes)), flag_closes, 1)

# Slope must be negative (bear flag = positive slope)
slope < 0  # for bull flag
-0.5 < slope / flag_closes.mean() * flag_bars < 0  # slope normalized: <-50% of mean

# Flag retracement: must not give back more than 50% of flagpole
flag_low = flag_closes.min()
flag_high = flag_closes.max()  # = pole high approximately
pole_high = df['high'][pole_start:flag_start].max()
pole_low  = df['low'][pole_start:flag_start].min()
retracement = (pole_high - flag_low) / (pole_high - pole_low)
retracement <= 0.50  # flag cannot retrace more than 50% of flagpole

# Flag channel must be parallel — upper and lower boundaries slope similarly
# Fit lines to flag highs and flag lows, compare slopes:
flag_highs = df['high'][flag_start : flag_start + flag_bars]
flag_lows  = df['low'][flag_start : flag_start + flag_bars]
slope_hi, _ = np.polyfit(range(len(flag_highs)), flag_highs, 1)
slope_lo, _ = np.polyfit(range(len(flag_lows)),  flag_lows,  1)
slope_diff_pct = abs(slope_hi - slope_lo) / abs(slope_hi)
slope_diff_pct <= 0.30  # slopes within 30% of each other = roughly parallel
```

**Step 3: Breakout from the Flag**
```python
# Upper boundary of flag (linear regression line through flag highs):
flag_upper_at_breakout = slope_hi * flag_bars + intercept_hi

# Breakout condition:
close[-1] > flag_upper_at_breakout * (1 + min_breakout_pct)

# Volume on breakout should expand:
volume[-1] > volume[flag_start : -1].mean() * 1.3
# Flag volume should contract during consolidation (average volume declining)
flag_volume_trend = np.polyfit(range(flag_bars), df['volume'][flag_start:-1], 1)[0]
flag_volume_trend < 0  # decreasing volume during flag = healthy
```

**Detection Criteria — Bear Flag** (mirror of above):
- Flagpole: sharp decline of at least 5% in 3–10 bars
- Flag: upward sloping channel (slope > 0), retracing no more than 50% of pole
- Breakout: close below lower boundary of flag with expanding volume

### Key Parameters
| Parameter | Default |
|-----------|---------|
| `pole_min_bars` | 3 |
| `pole_max_bars` | 10 |
| `pole_min_move_pct` | 0.05 |
| `flag_min_bars` | 5 |
| `flag_max_bars` | 25 |
| `max_retracement` | 0.50 |
| `max_slope_divergence_pct` | 0.30 |
| `volume_expansion_multiplier` | 1.3 |

### Confirmation Signals
- Volume contracts during flag, then expands sharply on breakout (classic signature)
- Breakout candle opens near low (bull flag) or high (bear flag) — gap continuation
- Flag takes 7–15 bars (too short = not digested; too long = pattern weakens)

### Risk/Reward Profile
- **Stop Loss:** Below the lowest point of the flag (bull flag) — typically `flag_low * (1 - 0.005)`
- **Target:** Add flagpole height to breakout point:
  ```python
  flagpole_height = pole_high - pole_low
  target = flag_upper_at_breakout + flagpole_height
  ```
- **Typical R/R:** 2:1 to 4:1 — one of the highest R/R patterns when flagpole is strong

### False Signal Scenarios
- **Reversal disguised as flag:** Flag retraces >50% and forms a new lower low → not a flag, trend is reversing
- **Choppy flag:** Flag has no clear slope, bounces violently — flag channels should be orderly
- **Failed continuation after earnings:** Flagpole was an earnings gap-up, flag breaks down as traders sell into strength
- **Insufficient flagpole:** A 1–2% "flagpole" is too weak; pattern requires a strong impulsive move

---

## 2.3 Pennant

### Description
A pennant is similar to a flag but the consolidation forms a **symmetrical converging triangle** (rather than a parallel channel). It has a distinct flagpole followed by converging highs and lows that form a small triangle, then a breakout in the direction of the pole.

```
      /|
     / |  ────────\   ← converging upper boundary (falling)
    /  |     X     \
   /   |    / \     ── breakout
  /    |   /   \───   ← converging lower boundary (rising)
```

### Detection Criteria

**Step 1: Flagpole** — Same as flag (5%+ move in 3–10 bars)

**Step 2: Pennant Formation**
```python
pennant_bars = 5 to 20  # shorter than flags typically

# Fit lines to pennant highs (descending) and pennant lows (ascending):
pnnt_highs = df['high'][pennant_start : pennant_start + pennant_bars]
pnnt_lows  = df['low'][pennant_start  : pennant_start + pennant_bars]

slope_hi, intercept_hi = np.polyfit(range(len(pnnt_highs)), pnnt_highs, 1)
slope_lo, intercept_lo = np.polyfit(range(len(pnnt_lows)),  pnnt_lows,  1)

# Upper boundary slopes DOWN (negative slope)
slope_hi < 0

# Lower boundary slopes UP (positive slope)
slope_lo > 0

# Lines must be converging toward an apex:
# At pennant_start, upper > lower (obviously)
# At pennant_end, the gap between lines must be smaller:
gap_at_start = (slope_hi * 0 + intercept_hi) - (slope_lo * 0 + intercept_lo)
gap_at_end   = (slope_hi * pennant_bars + intercept_hi) - (slope_lo * pennant_bars + intercept_lo)
convergence_ratio = gap_at_end / gap_at_start
convergence_ratio < 0.6  # must converge by at least 40%
convergence_ratio > 0    # must not have crossed yet (no apex reached)

# Pennant must not give back too much of the flagpole:
max_retracement = (pole_high - pnnt_lows.min()) / (pole_high - pole_low)
max_retracement <= 0.50
```

**Step 3: Breakout**
```python
# Upper line value at current bar:
upper_boundary = slope_hi * pennant_bars + intercept_hi  # bull pennant
close[-1] > upper_boundary * (1 + 0.002)
volume[-1] > volume[pennant_start:-1].mean() * 1.3
```

### Key Parameters
| Parameter | Default |
|-----------|---------|
| `pennant_min_bars` | 5 |
| `pennant_max_bars` | 20 |
| `min_convergence_ratio` | 0.40 |
| `max_retracement` | 0.50 |

### Confirmation Signals
- Volume dries up near the apex of the pennant
- Breakout occurs before the apex (if price reaches the apex without breaking, pattern is invalidated)
- Breakout candle engulfs the last 2–3 pennant candles

### Risk/Reward Profile
- **Stop:** Below the lowest point of pennant (bull) or above highest (bear)
- **Target:** Flagpole height added to breakout point (same as flag)
- **R/R:** 2:1 to 3.5:1

### False Signal Scenarios
- Breaking out sideways past the apex = no clear direction, pattern failed
- "Pennant" that is actually just random noise; ensure there is a clear flagpole
- Breakout against the flagpole direction (e.g., flag forms after an up move but breaks down) = reversal, not continuation

---

## 2.4 Ascending Triangle

### Description
Ascending triangle has a **flat resistance level** at the top and an **ascending support trendline** at the bottom. Each successive low is higher, indicating buyers are becoming more aggressive. The pattern resolves in a breakout above flat resistance.

```
──────────────────────────────────  ← flat resistance (horizontal)
    /\    /\    /\   
   /  \  /  \  /  \  ← ascending lows (trendline rising)
  /    \/    \/    
```

### Detection Criteria

```python
# Step 1: Identify flat resistance (horizontal upper boundary)
# Find swing highs within lookback window
swing_highs_in_window = df['swing_high'][-lookback_bars:].dropna()

# Upper boundary = the level that multiple swing highs cluster near
# Use the maximum swing high and verify others are within tolerance:
resistance_level = swing_highs_in_window.max()
touches_at_resistance = sum(
    1 for sh in swing_highs_in_window
    if abs(sh - resistance_level) / resistance_level < 0.01  # within 1%
)
touches_at_resistance >= 2  # need at least 2 touches of flat resistance

# Resistance "flatness" check: standard deviation of touching swing highs
touching_highs = [sh for sh in swing_highs_in_window
                  if abs(sh - resistance_level) / resistance_level < 0.01]
sh_std = np.std(touching_highs)
sh_std / resistance_level < 0.005  # std < 0.5% of price = flat enough

# Step 2: Identify ascending lower trendline
# Find swing lows in same window — they must be ascending
swing_lows_in_window = df.loc[df['swing_low'].notna()].tail(lookback_bars)
sl_values = swing_lows_in_window['swing_low'].values
sl_indices = range(len(sl_values))

slope_lo, intercept_lo = np.polyfit(sl_indices, sl_values, 1)
slope_lo > 0  # must be positive (ascending)

# R² of fit must be high (lows should be well-aligned on the trendline):
sl_predicted = [slope_lo * i + intercept_lo for i in sl_indices]
ss_res = sum((sl_values[i] - sl_predicted[i])**2 for i in range(len(sl_values)))
ss_tot = sum((sl_values[i] - sl_values.mean())**2 for i in range(len(sl_values)))
r_squared = 1 - ss_res / ss_tot
r_squared >= 0.70  # lows fit the ascending line with 70%+ R²

# Minimum number of swing lows: at least 2 (ideally 3)
len(sl_values) >= 2

# Pattern duration: minimum 15 bars, maximum 100 bars
pattern_duration = len(df) - pattern_start_index
15 <= pattern_duration <= 100

# Step 3: Breakout
close[-1] > resistance_level * (1 + min_breakout_pct)  # 0.002
volume[-1] > volume[-20:].mean() * 1.3
```

### Key Parameters
| Parameter | Default |
|-----------|---------|
| `lookback_bars` | 60 |
| `min_resistance_touches` | 2 |
| `resistance_flatness_pct` | 0.01 |
| `min_ascending_r_squared` | 0.70 |
| `min_swing_lows` | 2 |
| `min_pattern_bars` | 15 |
| `max_pattern_bars` | 100 |

### Confirmation Signals
- Price compresses into a tighter and tighter range near resistance before breakout
- Breakout accompanied by gap up or large candle body
- Prior volume declining into triangle, exploding on breakout

### Risk/Reward Profile
- **Stop:** Below the last swing low within the triangle, or below the ascending trendline
- **Target:** Add the widest part of the triangle (at the left edge) to the breakout point:
  ```python
  triangle_height = resistance_level - first_swing_low_in_pattern
  target = resistance_level + triangle_height
  ```
- **R/R:** 1.5:1 to 2.5:1

### False Signal Scenarios
- **Flat resistance is actually resistance of a prior downtrend** — triangle forms but broader structure is bearish; these fail more often
- **Ascending lows are too steep** — if the trendline is rising very fast, the pattern is compressing too rapidly and the breakout may be explosive but also exhausted
- **False breakout on opening gap** — stock gaps above resistance on market open but gives it back within 30 min

---

## 2.5 Descending Triangle

### Description
Mirror of ascending triangle. Flat **support** level at the bottom, **descending resistance trendline** at the top. Each successive high is lower. Resolves as a breakdown below the flat support — bearish pattern.

```
  \    /\    /\
   \  /  \  /  \  ← descending highs (trendline falling)
    \/    \/    
─────────────────  ← flat support (horizontal)
```

### Detection Criteria

```python
# Step 1: Flat support level — cluster of swing lows near the same price
support_level = swing_lows_in_window.min()
touching_lows = [sl for sl in swing_lows_in_window
                 if abs(sl - support_level) / support_level < 0.01]
len(touching_lows) >= 2
np.std(touching_lows) / support_level < 0.005

# Step 2: Descending upper trendline
slope_hi, intercept_hi = np.polyfit(sh_indices, sh_values, 1)
slope_hi < 0  # must be negative (descending)
r_squared_hi >= 0.70

# Step 3: Breakdown
close[-1] < support_level * (1 - min_breakout_pct)
volume[-1] > volume[-20:].mean() * 1.3
```

All other parameters are mirrors of ascending triangle.

### Risk/Reward Profile
- **Stop:** Above the last swing high within the triangle
- **Target:** Subtract triangle height from breakdown point:
  ```python
  triangle_height = first_swing_high_in_pattern - support_level
  target = support_level - triangle_height
  ```

### False Signal Scenarios
- **Descending triangle in an uptrend** — can resolve as a bullish continuation (flag-like) rather than breakdown; less reliable in uptrending markets
- **Support held for too long (>100 bars)** — very old support levels attract more buyers; may turn into a base rather than breaking

---

## 2.6 Symmetrical Triangle

### Description
Converging trendlines where highs make lower highs and lows make higher lows, forming a triangle pointing to an apex. Neither buyers nor sellers have control. Resolves with a breakout in either direction — **neutral pattern** that requires the breakout direction to determine trade bias.

```
    \    /
     \  /   ← descending upper trendline
      \/  ← apex approaching
      /\  ← ascending lower trendline
     /  \
```

### Detection Criteria

```python
# Step 1: Descending upper boundary
slope_hi < 0  # descending
r_squared_hi >= 0.65

# Step 2: Ascending lower boundary
slope_lo > 0  # ascending
r_squared_lo >= 0.65

# Step 3: Convergence toward apex
# Predict where the two lines would intersect:
# Upper: y = slope_hi * x + intercept_hi
# Lower: y = slope_lo * x + intercept_lo
# Intersection: slope_hi * x + intercept_hi = slope_lo * x + intercept_lo
apex_x = (intercept_lo - intercept_hi) / (slope_hi - slope_lo)
apex_x > pattern_bars  # apex must be in the future

# Price must be within the triangle currently (not yet broken):
current_upper = slope_hi * pattern_bars + intercept_hi
current_lower = slope_lo * pattern_bars + intercept_lo
current_lower < close[-1] < current_upper

# Convergence ratio: triangle must have narrowed by at least 30%:
gap_at_start = (slope_hi * 0 + intercept_hi) - (slope_lo * 0 + intercept_lo)
gap_at_end   = current_upper - current_lower
gap_at_end / gap_at_start <= 0.70

# Pattern must have at least 4 swing points (2 highs + 2 lows):
num_swing_highs_in_pattern >= 2
num_swing_lows_in_pattern  >= 2

# Duration: 15–80 bars (shorter triangles are less reliable)
15 <= pattern_bars <= 80

# Step 4: Breakout (direction determines bias)
bull_breakout = close[-1] > current_upper * (1 + 0.002)
bear_breakout = close[-1] < current_lower * (1 - 0.002)
volume_confirmed = volume[-1] > volume[-20:].mean() * 1.2
```

### Key Parameters
| Parameter | Default |
|-----------|---------|
| `min_r_squared` | 0.65 |
| `min_convergence_reduction` | 0.30 |
| `min_swing_points` | 4 |
| `min_pattern_bars` | 15 |
| `max_pattern_bars` | 80 |

### Confirmation Signals
- Breakout in the direction of the prior trend is more reliable (trend continuation)
- Breakout should occur before the apex — if price drifts to the apex without breaking, pattern degenerates
- Best breakouts occur with 50%–75% of the way to the apex

### Risk/Reward Profile
- **Stop:** Other side of the triangle after breakout (if bull breakout, stop below lower trendline)
- **Target:**
  ```python
  triangle_max_height = gap_at_start  # height at the left edge
  target = breakout_price ± triangle_max_height  # ± depends on direction
  ```

### False Signal Scenarios
- **Whipsaw:** Price breaks upper boundary, retests lower boundary, breaks lower — very common in symmetrical triangles
- **Breakout into earnings:** High IV can cause a gap that appears to be a valid breakout but is news-driven
- **Low volume breakout** — symmetrical triangles require strong volume confirmation because the direction is not pre-determined by structure

---

## 2.7 Rising Wedge

### Description
Both the upper and lower trendlines are **rising** (positive slope), but the lower trendline rises **more steeply** than the upper trendline — causing the pattern to converge while moving upward. Despite the upward price movement, this is a **bearish** pattern. The pattern signals weakening bullish momentum and typically breaks down.

```
         ────/  ← upper boundary (rising slowly)
        /       
  ────/   ← lower boundary (rising faster)
 /         → converges as pattern progresses → breakdown
```

### Detection Criteria

```python
# Both slopes must be POSITIVE (rising)
slope_hi > 0
slope_lo > 0

# Lower boundary must rise FASTER than upper:
slope_lo > slope_hi * (1 + min_slope_divergence)  # min_slope_divergence = 0.10

# Or equivalently, the channel is compressing upward:
gap_at_start = (slope_hi * 0 + intercept_hi) - (slope_lo * 0 + intercept_lo)
gap_at_end   = (slope_hi * n + intercept_hi) - (slope_lo * n + intercept_lo)
gap_at_end < gap_at_start  # gap is narrowing

# Lines must not cross within pattern duration:
gap_at_end > 0

# R² fit quality for both lines:
r_squared_hi >= 0.65
r_squared_lo >= 0.65

# Pattern duration: 15–80 bars
15 <= pattern_bars <= 80

# Minimum 2 swing highs + 2 swing lows within pattern:
num_swing_highs >= 2
num_swing_lows  >= 2

# Step: Breakdown signal
breakdown = close[-1] < (slope_lo * pattern_bars + intercept_lo) * (1 - 0.002)
volume[-1] > volume[-20:].mean() * 1.2

# Confirmation: rising wedge in a downtrend (counter-trend bounce) is more reliable bearishly
# Rising wedge in an uptrend = potential trend reversal
```

### Risk/Reward Profile
- **Stop:** Above the most recent swing high within the wedge
- **Target:** Typically back to the origin of the wedge (where the two lines started):
  ```python
  wedge_start_price = slope_lo * 0 + intercept_lo  # lower trendline at start
  target = breakout_price - (breakout_price - wedge_start_price)
  ```
- **R/R:** 2:1 to 4:1 (rising wedges often break hard because of built-up selling pressure)

### False Signal Scenarios
- **Wedge in a strong uptrend** — sometimes resolves as continuation after brief break below lower line
- **Short wedge (<10 bars)** — too brief to be reliable; may just be normal volatility
- **Breakdown on low volume** — rising wedges need volume to confirm the breakdown

---

## 2.8 Falling Wedge

### Description
Both trendlines are **falling** (negative slope), but the upper boundary falls **more steeply** than the lower — pattern converges downward. Despite the downward price movement, this is a **bullish** pattern. It indicates selling pressure is exhausting and a breakout to the upside is likely.

```
\────  ← upper boundary (falling faster)
  \
   \────  ← lower boundary (falling slower)
    \     → converges → breakout upward
```

### Detection Criteria

```python
# Both slopes must be NEGATIVE (falling)
slope_hi < 0
slope_lo < 0

# Upper boundary must fall FASTER than lower (more negative slope):
slope_hi < slope_lo  # slope_hi is more negative

# Channel is compressing downward:
gap_at_end < gap_at_start
gap_at_end > 0  # lines haven't crossed

# Same quality, duration, and swing point requirements as rising wedge

# Breakout signal:
breakout = close[-1] > (slope_hi * pattern_bars + intercept_hi) * (1 + 0.002)
volume[-1] > volume[-20:].mean() * 1.2
```

### Risk/Reward Profile
- **Stop:** Below the most recent swing low within the wedge
- **Target:** Back to the origin/start of the wedge (upper trendline at bar 0)
- **R/R:** 2:1 to 4:1

### False Signal Scenarios
- **Continuation of downtrend** — falling wedge within a very strong downtrend sometimes breaks lower; look for it in an established uptrend (correction) for highest reliability
- **Premature breakout with low volume** — needs strong volume on the break

---

## 2.9 Cup and Handle

### Description
A long base-building pattern with a **rounded bottom** (the "cup") followed by a smaller, shorter **downward drift** (the "handle"), then a breakout above the cup's rim (resistance). Bullish continuation or reversal pattern. Typically forms over weeks to months on daily charts.

```
\            /  ← cup rim (resistance at same level left and right)
 \          /  ← cup sides (gradual, rounded)
  \        /
   \──────/   ← cup bottom (rounded, U-shaped)
          |\
          | \  ← handle (slight pullback, <50% of cup depth)
          |  |
          |  breakout above rim
```

### Detection Criteria

```python
# ─── CUP DETECTION ───

# Step 1: Find the left peak (cup left rim)
# The cup starts at a local swing high
cup_left_peak_idx = index of swing high at start of cup
cup_left_peak_price = swing_high at that index

# Step 2: Find the cup bottom
# Cup bottom = lowest close in the window after the left peak
cup_bottom_idx   = df['close'][cup_left_peak_idx:].idxmin()
cup_bottom_price = df['close'][cup_bottom_idx]

# Cup depth: must be meaningful
cup_depth_pct = (cup_left_peak_price - cup_bottom_price) / cup_left_peak_price
0.15 <= cup_depth_pct <= 0.50  # cup must be 15%–50% deep

# Step 3: Find the right peak (cup right rim)
# The cup right side should approach the left rim within a tolerance
cup_right_peak_idx = first bar after cup_bottom_idx where:
    close approaches cup_left_peak_price within rim_tolerance
    rim_tolerance = cup_left_peak_price * 0.05  # within 5% of left rim

cup_right_peak_price = close at cup_right_peak_idx
price_symmetry = abs(cup_right_peak_price - cup_left_peak_price) / cup_left_peak_price
price_symmetry <= 0.05  # right rim within 5% of left rim

# Cup duration: must be substantial (no thin V-shapes)
cup_duration_bars = cup_right_peak_idx - cup_left_peak_idx
cup_duration_bars >= 30  # at least 30 bars (6 weeks on daily)

# Cup shape — "roundedness" check:
# The cup bottom should be rounded (not V-shaped)
# Measure curvature: fit a parabola to the cup section
cup_section = df['close'][cup_left_peak_idx : cup_right_peak_idx + 1]
x = np.arange(len(cup_section))
coeffs = np.polyfit(x, cup_section, 2)  # quadratic fit
# Coefficient of x² should be positive (upward-opening parabola = U-shape):
coeffs[0] > 0
# R² of parabolic fit should be high:
y_pred = np.polyval(coeffs, x)
ss_res = np.sum((cup_section.values - y_pred)**2)
ss_tot = np.sum((cup_section.values - cup_section.mean())**2)
r_squared_cup = 1 - ss_res / ss_tot
r_squared_cup >= 0.70  # well-fitted parabola = rounded cup

# ─── HANDLE DETECTION ───

# Handle forms AFTER the right rim
handle_start_idx = cup_right_peak_idx
handle_end_idx   = handle_start_idx + handle_bars  # variable

# Handle must drift DOWN (or sideways)
handle_closes = df['close'][handle_start_idx : handle_end_idx]
handle_slope, _ = np.polyfit(range(len(handle_closes)), handle_closes, 1)
handle_slope <= 0  # downward or flat drift

# Handle must not retrace more than 50% of cup depth (ideally <33%)
handle_low   = handle_closes.min()
handle_depth_pct = (cup_right_peak_price - handle_low) / cup_depth_pct / cup_right_peak_price
handle_depth_pct <= 0.50

# Handle duration: 5–30 bars
5 <= handle_bars <= 30

# ─── BREAKOUT ───
cup_rim = max(cup_left_peak_price, cup_right_peak_price)
breakout = close[-1] > cup_rim * (1 + 0.002)
volume[-1] > volume[-20:].mean() * 1.5  # cup-handle breakouts need strong volume
```

### Key Parameters
| Parameter | Default |
|-----------|---------|
| `min_cup_depth_pct` | 0.15 |
| `max_cup_depth_pct` | 0.50 |
| `min_cup_bars` | 30 |
| `rim_tolerance_pct` | 0.05 |
| `min_cup_r_squared` | 0.70 |
| `max_handle_retracement` | 0.50 |
| `min_handle_bars` | 5 |
| `max_handle_bars` | 30 |

### Confirmation Signals
- Handle forms in the upper third of the cup (not halfway down the cup)
- Volume contracted throughout the cup and dried up in the handle, then surged on breakout
- Prior trend before cup was upward (cup-handle is continuation of uptrend)

### Risk/Reward Profile
- **Stop:** Below the handle low: `stop = handle_low * (1 - 0.005)`
- **Target:** Add the cup depth to the breakout point:
  ```python
  target = cup_rim + (cup_rim - cup_bottom_price)
  ```
- **R/R:** 1.5:1 to 3:1

### False Signal Scenarios
- **V-shaped cup** — sharp drop and sharp recovery is NOT a cup; the rounded bottom is essential
- **Handle too deep** — handle retracing >50% of cup depth suggests supply is heavy at the rim
- **No prior uptrend** — cup without a prior trend is just a base; reliability drops significantly
- **Very long cup (>1 year on daily)** — not invalid, but longer patterns have more noise and lower reliability in backtests

---

## 2.10 Head and Shoulders

### Description
Three peaks: the middle peak (head) is the highest, flanked by two lower peaks (shoulders). A neckline connects the troughs between the peaks. Breakdown below the neckline confirms the pattern — classic bearish reversal.

```
        HEAD
       /    \
LSHDR /      \ RSHDR
  /\/          \/\
 /    NECKLINE   \  ← neckline (can be horizontal or slightly sloped)
/                 \  ← breakdown below neckline = signal
```

### Detection Criteria

```python
# Step 1: Find three peaks in sequence
# Identify all swing highs in lookback window
sh_list = df.loc[df['swing_high'].notna()].tail(lookback_bars)

# Need at least 3 swing highs
len(sh_list) >= 3

# Iterate over triplets of swing highs:
for i in range(len(sh_list) - 2):
    ls_idx, head_idx, rs_idx = sh_list.index[i], sh_list.index[i+1], sh_list.index[i+2]
    ls_price   = sh_list['swing_high'].iloc[i]
    head_price = sh_list['swing_high'].iloc[i+1]
    rs_price   = sh_list['swing_high'].iloc[i+2]
    
    # HEAD must be highest:
    head_is_highest = (head_price > ls_price) and (head_price > rs_price)
    
    # SHOULDERS should be within tolerance of each other (symmetric):
    shoulder_symmetry = abs(ls_price - rs_price) / head_price
    shoulder_symmetry <= 0.05  # shoulders within 5% of each other's price
    
    # Both shoulders must be lower than head:
    ls_below_head = (head_price - ls_price) / head_price >= 0.02  # at least 2% lower
    rs_below_head = (head_price - rs_price) / head_price >= 0.02
    
    # Time symmetry: distance from LS to HEAD should be roughly similar to HEAD to RS
    time_left  = head_idx - ls_idx
    time_right = rs_idx - head_idx
    time_symmetry_ratio = min(time_left, time_right) / max(time_left, time_right)
    time_symmetry_ratio >= 0.50  # right side takes between 50% and 200% of left-side time
    
    # Step 2: Define the neckline
    # Neckline connects the TROUGH between LS-HEAD and the TROUGH between HEAD-RS
    trough_1_idx = df['low'][ls_idx:head_idx].idxmin()
    trough_2_idx = df['low'][head_idx:rs_idx].idxmin()
    neckline_p1  = (df.index.get_loc(trough_1_idx), df['low'][trough_1_idx])
    neckline_p2  = (df.index.get_loc(trough_2_idx), df['low'][trough_2_idx])
    
    # Neckline slope:
    x1, y1 = neckline_p1
    x2, y2 = neckline_p2
    neckline_slope     = (y2 - y1) / (x2 - x1)
    neckline_intercept = y1 - neckline_slope * x1
    
    # Neckline must not slope too steeply:
    neckline_slope_pct = abs(neckline_slope) / ((y1 + y2) / 2)
    neckline_slope_pct <= 0.003  # less than 0.3% per bar
    
    # Step 3: Breakdown detection
    # Neckline value at current bar:
    current_bar_x = len(df) - 1
    neckline_at_current = neckline_slope * current_bar_x + neckline_intercept
    
    breakdown = close[-1] < neckline_at_current * (1 - 0.002)
    # Breakdown must occur within max_bars_after_rs after right shoulder forms:
    bars_since_rs = len(df) - df.index.get_loc(rs_idx)
    bars_since_rs <= max_bars_after_rs  # default = 30
    
    volume_confirmed = volume[-1] > volume[-20:].mean() * 1.2
```

### Key Parameters
| Parameter | Default |
|-----------|---------|
| `lookback_bars` | 100 |
| `max_shoulder_symmetry_pct` | 0.05 |
| `min_shoulder_below_head_pct` | 0.02 |
| `min_time_symmetry_ratio` | 0.50 |
| `max_neckline_slope_pct` | 0.003 |
| `max_bars_after_rs` | 30 |

### Confirmation Signals
- Right shoulder forms on lower volume than left shoulder (distribution phase)
- Breakdown candle is large-bodied and closes near its low
- Price retests neckline from below after breakdown and then continues down

### Risk/Reward Profile
- **Stop:** Above the right shoulder: `stop = rs_price * (1 + 0.005)`
- **Target:** Subtract the head-to-neckline distance from the neckline:
  ```python
  neckline_at_head = neckline_slope * df.index.get_loc(head_idx) + neckline_intercept
  pattern_height = head_price - neckline_at_head
  target = neckline_at_current - pattern_height
  ```
- **R/R:** 1.5:1 to 3:1

### False Signal Scenarios
- **Neckline never breaks** — price forms H&S but grinds back up; pattern fails if price recovers above the head's level
- **Asymmetric volume** — if right shoulder volume is HIGHER than left, this signals accumulation, not distribution → pattern less reliable
- **H&S in strong uptrend on good fundamentals** — more likely to fail; H&S most reliable after a long, extended run-up

---

## 2.11 Inverse Head and Shoulders

**Description:** Mirror of H&S — three troughs with the middle (head) being the deepest, flanked by two shallower troughs (shoulders). Neckline connects the peaks between troughs. Breakout above neckline = bullish reversal.

**Detection Criteria:** Mirror all H&S criteria using `low` values for the head and shoulders and finding peaks for the neckline troughs.

```python
# Inverse H&S (bullish reversal)
# LS, HEAD, RS are swing LOWS
# HEAD must be LOWEST:
head_is_lowest = (head_price < ls_price) and (head_price < rs_price)

# Shoulders must be within tolerance:
shoulder_symmetry = abs(ls_price - rs_price) / head_price
shoulder_symmetry <= 0.05

# Neckline connects PEAKS between troughs (instead of troughs between peaks):
peak_1_idx = df['high'][ls_idx:head_idx].idxmax()
peak_2_idx = df['high'][head_idx:rs_idx].idxmax()

# Breakout above neckline:
breakout = close[-1] > neckline_at_current * (1 + 0.002)
volume[-1] > volume[-20:].mean() * 1.3  # strong volume on bullish neckline break
```

### Risk/Reward Profile
- **Stop:** Below the right shoulder (swing low)
- **Target:** Add head-to-neckline distance above breakout point
- **R/R:** 1.5:1 to 3:1

---

## 2.12 Double Top

### Description
Two peaks at approximately the same price level, separated by a trough. Bearish reversal pattern. Resembles the letter "M".

```
 /\    /\   ← two peaks at approximately the same price
/  \  /  \
    \/     ← trough (neckline / support)
           \  ← breakdown below trough = signal
```

### Detection Criteria

```python
# Step 1: Find two swing highs at similar price levels
sh_list = df.loc[df['swing_high'].notna()].tail(lookback_bars)

for i in range(len(sh_list) - 1):
    peak1_idx   = sh_list.index[i]
    peak2_idx   = sh_list.index[i + 1]
    peak1_price = sh_list['swing_high'].iloc[i]
    peak2_price = sh_list['swing_high'].iloc[i + 1]
    
    # Peaks must be within price tolerance of each other:
    price_diff_pct = abs(peak1_price - peak2_price) / peak1_price
    price_diff_pct <= 0.03  # peaks within 3% of each other
    
    # Peaks must be separated by a meaningful trough:
    trough_low = df['low'][peak1_idx:peak2_idx].min()
    trough_idx = df['low'][peak1_idx:peak2_idx].idxmin()
    
    # Trough must be meaningfully below the peaks:
    trough_depth_pct = (min(peak1_price, peak2_price) - trough_low) / min(peak1_price, peak2_price)
    trough_depth_pct >= 0.03  # trough must be at least 3% below peaks
    
    # Time between peaks: minimum separation
    time_between_peaks = df.index.get_loc(peak2_idx) - df.index.get_loc(peak1_idx)
    time_between_peaks >= 10  # at least 10 bars between peaks
    time_between_peaks <= 60  # but not too far apart
    
    # Second peak should be on LOWER VOLUME than first (distribution exhausting):
    vol_peak1 = df['volume'][peak1_idx]
    vol_peak2 = df['volume'][peak2_idx]
    vol_ratio = vol_peak2 / vol_peak1
    # vol_ratio < 0.80 is ideal, but not strictly required
    
    # Step 2: Neckline = trough level
    neckline = trough_low
    
    # Step 3: Breakdown
    # Pattern confirmed when price breaks below the trough (neckline):
    breakdown = close[-1] < neckline * (1 - 0.002)
    
    # Breakdown must happen within max_bars_after_peak2 after second peak:
    bars_since_peak2 = df.index.get_loc(df.index[-1]) - df.index.get_loc(peak2_idx)
    bars_since_peak2 <= 20
    
    volume_confirmed = volume[-1] > volume[-20:].mean() * 1.2
```

### Key Parameters
| Parameter | Default |
|-----------|---------|
| `max_peak_price_diff_pct` | 0.03 |
| `min_trough_depth_pct` | 0.03 |
| `min_bars_between_peaks` | 10 |
| `max_bars_between_peaks` | 60 |
| `max_bars_after_peak2` | 20 |

### Risk/Reward Profile
- **Stop:** Above the highest of the two peaks: `stop = max(peak1, peak2) * (1 + 0.005)`
- **Target:** Subtract the peak-to-neckline distance from the neckline:
  ```python
  pattern_height = max(peak1_price, peak2_price) - neckline
  target = neckline - pattern_height
  ```

### False Signal Scenarios
- **Third peak forms** — double top becomes triple top (still bearish but be aware)
- **Neckline breaks but immediately recovers** — false breakdown; wait for a candle close confirmation
- **Double top in a strong uptrend** — if broader market/sector is strong, the double top may resolve as a continuation

---

## 2.13 Double Bottom

**Description:** Mirror of double top — two troughs at approximately the same price, separated by a peak. Bullish reversal. Resembles the letter "W".

**Detection Criteria:** Mirror of double top, using swing lows:

```python
# Two swing lows at similar price levels
price_diff_pct = abs(trough1_price - trough2_price) / trough1_price
price_diff_pct <= 0.03

# Peak between troughs is meaningful (resistance = neckline)
peak_high = df['high'][trough1_idx:trough2_idx].max()
peak_pct_above = (peak_high - max(trough1, trough2)) / max(trough1, trough2)
peak_pct_above >= 0.03

# Breakout above the peak (neckline):
neckline = peak_high
breakout = close[-1] > neckline * (1 + 0.002)
volume[-1] > volume[-20:].mean() * 1.3  # strong volume on breakout
```

### Risk/Reward Profile
- **Stop:** Below the lowest of the two troughs: `stop = min(t1, t2) * (1 - 0.005)`
- **Target:** Add the trough-to-neckline distance to the neckline breakout point:
  ```python
  pattern_height = neckline - min(trough1_price, trough2_price)
  target = neckline + pattern_height
  ```

---

## 2.14 Range Consolidation Breakout

### Description
Price enters a horizontal consolidation range (often called a "box" or "rectangle") where it oscillates between a clearly defined resistance ceiling and support floor without trending. A breakout from this range signals the beginning of a new directional move.

```
──────────────────────────  ← resistance ceiling
 /\/\/\/\/\/\/\/\/\/\/\/   ← price oscillating in range
──────────────────────────  ← support floor
```

### Detection Criteria

```python
# Step 1: Identify the consolidation range
# Detect period of low directional movement (ADX or volatility contraction)

# Define range boundaries from recent swing highs and lows:
recent_sh = df['swing_high'][-lookback_bars:].dropna()
recent_sl = df['swing_low'][-lookback_bars:].dropna()

range_high = recent_sh.max()
range_low  = recent_sl.min()
range_width_pct = (range_high - range_low) / range_low

# Range must be defined (not too wide, not too narrow):
0.03 <= range_width_pct <= 0.20  # 3%–20% width is a valid range

# All swing highs must be near the range ceiling (within touch_tolerance):
sh_in_range = all(
    abs(sh - range_high) / range_high < 0.02
    for sh in recent_sh
)

# All swing lows must be near the range floor:
sl_in_range = all(
    abs(sl - range_low) / range_low < 0.02
    for sl in recent_sl
)

# Minimum number of touches (oscillations):
min_sh_touches = len(recent_sh[recent_sh >= range_high * 0.98])
min_sl_touches = len(recent_sl[recent_sl <= range_low  * 1.02])
min_sh_touches >= 2
min_sl_touches >= 2

# Duration of consolidation:
consolidation_bars = lookback_bars  # at least 15 bars
consolidation_bars >= 15

# Volume should be contracting during the range (trend of declining volume):
vol_slope, _ = np.polyfit(range(lookback_bars), df['volume'][-lookback_bars:], 1)
vol_slope <= 0  # ideal: volume declining during consolidation

# Step 2: Breakout detection
# Bullish breakout (above resistance ceiling):
bull_breakout = (
    close[-1] > range_high * (1 + 0.002) and
    volume[-1] > df['volume'][-20:].mean() * 1.5
)

# Bearish breakdown (below support floor):
bear_breakout = (
    close[-1] < range_low * (1 - 0.002) and
    volume[-1] > df['volume'][-20:].mean() * 1.5
)

# Step 3: False breakout filter
# Require the breakout candle to close BEYOND the boundary (not just pierce it)
# AND the candle body (not wick) to be outside the range:
bull_body_outside = min(open[-1], close[-1]) > range_high  # entire body above
```

### Key Parameters
| Parameter | Default |
|-----------|---------|
| `lookback_bars` | 50 |
| `min_range_width_pct` | 0.03 |
| `max_range_width_pct` | 0.20 |
| `touch_tolerance_pct` | 0.02 |
| `min_touches_per_side` | 2 |
| `min_consolidation_bars` | 15 |
| `volume_expansion_multiplier` | 1.5 |

### Confirmation Signals
- Prior trend before range aligns with breakout direction (continuation)
- Volume is at multi-week low during range, then surges on breakout
- Candle that breaks out has a small upper wick (for bullish) — buyers in control all day
- ATR (Average True Range) contracted during range, then expands on breakout

### Risk/Reward Profile
- **Stop:** Back inside the range: `stop = range_high - (range_width * 0.2)` for bull breakout
- **Target:** Add range width to the breakout level:
  ```python
  range_width = range_high - range_low
  target_bull = range_high + range_width
  target_bear = range_low  - range_width
  ```
- **R/R:** 1.5:1 to 2.5:1

### False Signal Scenarios
- **News-driven spike** — earnings or macro event spikes out of range but price returns; wait for the candle to CLOSE before entering
- **Wide range (>20%)** — not a consolidation, just a downtrend; pattern unreliable
- **Very brief range (<10 bars)** — insufficient base; breakout lacks conviction
- **Multiple prior false breakouts** — if the range has already seen 2–3 false breaks, the next one has a lower probability of succeeding

---

<a name="part-3"></a>
# Part 3 — Algorithmic Implementation Guide

---

## 3.1 DataFrame Conventions

All detection functions expect a pandas DataFrame with the following standardized column names (lowercase):

```python
required_columns = ['open', 'high', 'low', 'close', 'volume']

# After preprocessing, additional computed columns are added:
computed_columns = [
    'swing_high',        # float or NaN — swing high price
    'swing_low',         # float or NaN — swing low price
    'atr',               # Average True Range (14-period default)
    'ema_20',            # 20-period EMA
    'ema_50',            # 50-period EMA
    'ema_200',           # 200-period EMA
    'volume_sma_20',     # 20-period SMA of volume
    'volume_ratio',      # volume / volume_sma_20
    'adx',               # ADX (14-period) — trend strength
    'bullish_bos',       # bool — bullish BOS at this bar
    'bearish_bos',       # bool — bearish BOS at this bar
    'bullish_choch',     # bool — bullish CHoCH at this bar
    'bearish_choch',     # bool — bearish CHoCH at this bar
    'fvg_bullish',       # bool — bullish FVG starts at this bar
    'fvg_bearish',       # bool — bearish FVG starts at this bar
]
```

### Preprocessing Pipeline

```python
import pandas as pd
import numpy as np

def preprocess(df: pd.DataFrame, swing_n: int = 5) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    df.sort_index(inplace=True)
    
    # ATR
    high_low   = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close  = (df['low']  - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.ewm(span=14, adjust=False).mean()
    
    # EMAs
    for span in [20, 50, 200]:
        df[f'ema_{span}'] = df['close'].ewm(span=span, adjust=False).mean()
    
    # Volume ratio
    df['volume_sma_20'] = df['volume'].rolling(20).mean()
    df['volume_ratio']  = df['volume'] / df['volume_sma_20']
    
    # Swing points
    df = find_swing_highs_lows(df, n=swing_n)
    df = filter_significant_swings(df, min_swing_pct=0.005)
    
    return df
```

---

## 3.2 Detected Pattern Data Structure

```python
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
import pandas as pd

@dataclass
class DetectedPattern:
    """
    Represents a single detected technical pattern on an OHLCV chart.
    """
    # ─── Identity ─────────────────────────────────────────────────────────────
    pattern_type: str
    """
    Pattern name. One of:
    'bos_bullish', 'bos_bearish', 'choch_bullish', 'choch_bearish',
    'breakout_resistance', 'breakout_support',
    'bull_flag', 'bear_flag', 'pennant_bull', 'pennant_bear',
    'asc_triangle', 'desc_triangle', 'sym_triangle',
    'rising_wedge', 'falling_wedge',
    'cup_handle',
    'head_shoulders', 'inv_head_shoulders',
    'double_top', 'double_bottom',
    'range_breakout_bull', 'range_breakout_bear'
    """

    direction: str
    """'bullish' or 'bearish'"""

    # ─── Location ─────────────────────────────────────────────────────────────
    start_index: Any
    """Index (timestamp or integer) where the pattern begins"""

    end_index: Any
    """Index (timestamp or integer) where the pattern is detected/completed"""

    detected_at_bar: Any
    """The bar index at which the pattern was detected (usually = end_index)"""

    pattern_bars: int
    """Total number of bars the pattern spans"""

    # ─── Key Levels ───────────────────────────────────────────────────────────
    key_levels: Dict[str, float] = field(default_factory=dict)
    """
    Dictionary of critical price levels for this pattern. Keys vary by pattern type:

    For BOS/CHoCH:
        'trigger_level'   : price level that was broken
        'broken_swing'    : price of the swing high/low that was broken

    For flags/pennants:
        'pole_high'       : top of the flagpole
        'pole_low'        : bottom of the flagpole
        'flag_high'       : upper boundary of flag at breakout bar
        'flag_low'        : lower boundary of flag at breakout bar

    For triangles/wedges:
        'upper_trendline' : price of upper trendline at detected_at_bar
        'lower_trendline' : price of lower trendline at detected_at_bar
        'apex_price'      : projected apex price
        'apex_bar'        : projected apex bar index

    For H&S / Double patterns:
        'left_peak'       : price of left shoulder/peak
        'head'            : price of head
        'right_peak'      : price of right shoulder/peak
        'neckline'        : neckline price at detected_at_bar
        'trough_1'        : price of first trough
        'trough_2'        : price of second trough (double top/bottom)

    For cup-handle:
        'cup_left_rim'    : price of left cup rim
        'cup_right_rim'   : price of right cup rim
        'cup_bottom'      : price of cup bottom
        'handle_low'      : lowest price of handle

    For ranges:
        'range_high'      : resistance ceiling
        'range_low'       : support floor
        'range_width'     : range_high - range_low
    """

    # ─── Trade Parameters ─────────────────────────────────────────────────────
    entry_price: Optional[float] = None
    """Suggested entry price (usually the close of the detected_at_bar)"""

    stop_loss: Optional[float] = None
    """Calculated stop loss price"""

    target: Optional[float] = None
    """Calculated price target"""

    risk_reward_ratio: Optional[float] = None
    """Calculated R/R = abs(target - entry) / abs(entry - stop_loss)"""

    # ─── Quality Metrics ──────────────────────────────────────────────────────
    confidence_score: float = 0.0
    """
    Composite score from 0.0 to 1.0 representing pattern quality.
    See Section 3.3 for scoring methodology.
    """

    volume_confirmed: bool = False
    """True if breakout volume meets or exceeds the volume threshold"""

    close_confirmed: bool = True
    """True if the trigger was a candle close (not just a wick)"""

    # ─── Component Scores (for transparency/debugging) ────────────────────────
    component_scores: Dict[str, float] = field(default_factory=dict)
    """
    Individual scoring components. E.g.:
    {
        'structure_clarity' : 0.85,
        'volume_quality'    : 0.70,
        'trendline_fit'     : 0.90,
        'time_symmetry'     : 0.75,
        'breakout_strength' : 0.80,
    }
    """

    # ─── Metadata ─────────────────────────────────────────────────────────────
    timeframe: str = 'daily'
    """Timeframe of the source data: 'intraday', 'daily', 'weekly'"""

    ticker: str = ''
    """Stock ticker symbol"""

    notes: str = ''
    """Human-readable notes or warnings about the detection"""


def compute_risk_reward(pattern: DetectedPattern) -> DetectedPattern:
    """Compute R/R after entry, stop, and target are set."""
    if all(x is not None for x in [pattern.entry_price, pattern.stop_loss, pattern.target]):
        risk   = abs(pattern.entry_price - pattern.stop_loss)
        reward = abs(pattern.target - pattern.entry_price)
        if risk > 0:
            pattern.risk_reward_ratio = round(reward / risk, 2)
    return pattern
```

---

## 3.3 Confidence Scoring (0.0 – 1.0)

Confidence is a weighted average of several component scores, each ranging from 0.0 to 1.0.

### Universal Components (apply to all patterns)

```python
def score_volume(volume_ratio: float) -> float:
    """
    Score based on volume ratio (volume / 20-bar avg) at breakout bar.
    """
    if volume_ratio >= 3.0:  return 1.0
    if volume_ratio >= 2.0:  return 0.9
    if volume_ratio >= 1.5:  return 0.75
    if volume_ratio >= 1.2:  return 0.6
    if volume_ratio >= 1.0:  return 0.4
    return 0.2  # below-average volume = low confidence


def score_breakout_strength(close: float, level: float,
                             direction: str, atr: float) -> float:
    """
    Score based on how far the close is beyond the trigger level, in ATR units.
    direction: 'bullish' or 'bearish'
    """
    if direction == 'bullish':
        dist_atr = (close - level) / atr
    else:
        dist_atr = (level - close) / atr
    
    if dist_atr >= 1.0:  return 1.0
    if dist_atr >= 0.5:  return 0.8
    if dist_atr >= 0.2:  return 0.6
    if dist_atr >= 0.1:  return 0.4
    return 0.2


def score_candle_body(open_: float, close: float,
                       high: float, low: float) -> float:
    """
    Score based on body-to-range ratio of the breakout candle.
    A large body = strong directional conviction.
    """
    total_range = high - low
    if total_range == 0:
        return 0.5
    body = abs(close - open_)
    ratio = body / total_range
    
    if ratio >= 0.80: return 1.0
    if ratio >= 0.60: return 0.8
    if ratio >= 0.40: return 0.6
    if ratio >= 0.20: return 0.4
    return 0.2


def score_trend_alignment(df: pd.DataFrame, direction: str) -> float:
    """
    Score based on whether the breakout aligns with the higher-timeframe EMA trend.
    Bullish breakout + price above EMA 50 + EMA 50 > EMA 200 = aligned.
    """
    current_close = df['close'].iloc[-1]
    ema50  = df['ema_50'].iloc[-1]
    ema200 = df['ema_200'].iloc[-1]
    
    if direction == 'bullish':
        score = 0.0
        if current_close > ema50:  score += 0.4
        if ema50 > ema200:         score += 0.4
        if current_close > ema200: score += 0.2
        return score
    else:  # bearish
        score = 0.0
        if current_close < ema50:  score += 0.4
        if ema50 < ema200:         score += 0.4
        if current_close < ema200: score += 0.2
        return score
```

### Pattern-Specific Component Scores

```python
def score_trendline_fit(r_squared: float) -> float:
    """For triangle/wedge/flag patterns — how well price fits the trendlines."""
    if r_squared >= 0.95: return 1.0
    if r_squared >= 0.85: return 0.85
    if r_squared >= 0.75: return 0.70
    if r_squared >= 0.65: return 0.55
    return 0.30


def score_symmetry(ratio: float) -> float:
    """
    For H&S, double top/bottom — time/price symmetry of the pattern.
    ratio: min(left, right) / max(left, right) → 1.0 = perfect symmetry
    """
    if ratio >= 0.90: return 1.0
    if ratio >= 0.75: return 0.8
    if ratio >= 0.60: return 0.6
    if ratio >= 0.50: return 0.4
    return 0.2


def score_pattern_duration(bars: int, ideal_min: int, ideal_max: int) -> float:
    """
    Score based on whether pattern duration falls in the ideal window.
    """
    if ideal_min <= bars <= ideal_max:
        # Within ideal range: interpolate (centered = best score)
        mid = (ideal_min + ideal_max) / 2
        distance_from_mid = abs(bars - mid) / (ideal_max - ideal_min)
        return max(0.5, 1.0 - distance_from_mid)
    elif bars < ideal_min:
        ratio = bars / ideal_min
        return max(0.1, ratio * 0.5)
    else:  # bars > ideal_max
        ratio = ideal_max / bars
        return max(0.1, ratio * 0.6)
```

### Composite Confidence Score — Weights by Pattern

```python
CONFIDENCE_WEIGHTS = {
    'bos_bullish': {
        'volume_score'      : 0.25,
        'breakout_strength' : 0.30,
        'candle_body'       : 0.20,
        'trend_alignment'   : 0.25,
    },
    'bull_flag': {
        'volume_score'      : 0.25,
        'breakout_strength' : 0.20,
        'candle_body'       : 0.15,
        'trend_alignment'   : 0.15,
        'trendline_fit'     : 0.25,  # flag channel quality
    },
    'asc_triangle': {
        'volume_score'      : 0.20,
        'breakout_strength' : 0.20,
        'candle_body'       : 0.10,
        'trend_alignment'   : 0.15,
        'trendline_fit'     : 0.20,
        'pattern_duration'  : 0.15,
    },
    'head_shoulders': {
        'volume_score'      : 0.20,
        'breakout_strength' : 0.20,
        'candle_body'       : 0.15,
        'trend_alignment'   : 0.10,
        'symmetry'          : 0.20,
        'pattern_duration'  : 0.15,
    },
    'double_top': {
        'volume_score'      : 0.20,
        'breakout_strength' : 0.20,
        'candle_body'       : 0.15,
        'trend_alignment'   : 0.10,
        'symmetry'          : 0.25,
        'pattern_duration'  : 0.10,
    },
    'cup_handle': {
        'volume_score'      : 0.25,
        'breakout_strength' : 0.20,
        'candle_body'       : 0.10,
        'trend_alignment'   : 0.15,
        'trendline_fit'     : 0.15,  # cup roundedness
        'pattern_duration'  : 0.15,
    },
    # Default weights for unlisted patterns:
    'default': {
        'volume_score'      : 0.25,
        'breakout_strength' : 0.25,
        'candle_body'       : 0.20,
        'trend_alignment'   : 0.30,
    },
}


def compute_confidence(pattern_type: str, component_scores: dict) -> float:
    """
    Compute weighted confidence score from component scores.
    Returns float in [0.0, 1.0].
    """
    weights = CONFIDENCE_WEIGHTS.get(pattern_type, CONFIDENCE_WEIGHTS['default'])
    total_weight = 0.0
    weighted_sum = 0.0
    
    for component, weight in weights.items():
        if component in component_scores:
            weighted_sum += component_scores[component] * weight
            total_weight += weight
    
    if total_weight == 0:
        return 0.0
    
    return round(weighted_sum / total_weight, 3)
```

### Confidence Score Interpretation

| Score | Label | Interpretation |
|-------|-------|----------------|
| 0.85 – 1.00 | Very High | Near-perfect pattern; all conditions met with strong volume |
| 0.70 – 0.84 | High | Strong pattern; most conditions met; volume confirms |
| 0.55 – 0.69 | Moderate | Decent pattern; some conditions marginal; use with caution |
| 0.40 – 0.54 | Low | Pattern detected but weak; likely false signal; do not trade alone |
| 0.00 – 0.39 | Very Low | Pattern barely qualifies; informational only |

---

## 3.4 Detection Order & Layering

### Recommended Detection Pipeline

The order matters because some patterns depend on others being detected first, and because checking simpler patterns first allows for early exits that reduce computation.

```
Step 1: Preprocessing (always first)
    └── Compute ATR, EMAs, volume SMA
    └── Detect swing highs/lows
    └── Filter significant swings
    └── Classify market structure (uptrend / downtrend / ranging)

Step 2: BOS & CHoCH Detection (market structure layer)
    └── detect_bos_bullish()
    └── detect_bos_bearish()
    └── detect_choch_bullish()
    └── detect_choch_bearish()
    └── detect_order_blocks()
    └── detect_fvgs()

Step 3: Simple Breakout Patterns (fewest dependencies)
    └── detect_range_consolidation_breakout()
    └── detect_sr_breakout()

Step 4: Trend Continuation Patterns
    └── detect_bull_flag()
    └── detect_bear_flag()
    └── detect_pennant()

Step 5: Triangle & Wedge Patterns
    └── detect_ascending_triangle()
    └── detect_descending_triangle()
    └── detect_symmetrical_triangle()
    └── detect_rising_wedge()
    └── detect_falling_wedge()

Step 6: Reversal Patterns (most complex, require prior trend context)
    └── detect_double_top()
    └── detect_double_bottom()
    └── detect_head_and_shoulders()
    └── detect_inv_head_and_shoulders()
    └── detect_cup_and_handle()

Step 7: Confluence Scoring (optional enhancement)
    └── For each detected pattern, check if it aligns with:
        - BOS/CHoCH from Step 2
        - Key S/R levels from prior structure
        - FVG/Order block proximity
        - EMA alignment from preprocessing
        - Add confluence bonus to confidence score
```

### Layering Patterns for Confluence

```python
def apply_confluence_bonus(pattern: DetectedPattern, df: pd.DataFrame,
                            sr_levels: list, fvgs: list,
                            order_blocks: list) -> DetectedPattern:
    """
    Boost confidence score if pattern coincides with other technical factors.
    Max bonus: +0.15 to confidence score (capped at 1.0)
    """
    bonus = 0.0
    entry = pattern.entry_price
    notes = []
    
    # EMA alignment bonus (already in trend_alignment, but double-weight if strong)
    ema50 = df['ema_50'].iloc[-1]
    if pattern.direction == 'bullish' and entry > ema50:
        bonus += 0.03
        notes.append('Above EMA50')
    
    # S/R level confluence: entry or stop near a major S/R zone
    for zone in sr_levels[:5]:  # check top 5 strongest zones
        proximity = abs(entry - zone['mid']) / entry
        if proximity < 0.01 and zone['touches'] >= 3:
            bonus += 0.05
            notes.append(f"Near S/R zone ({zone['mid']:.2f}, {zone['touches']} touches)")
            break
    
    # FVG confluence: price breaking into an unfilled FVG
    for fvg in fvgs:
        if not fvg['filled']:
            if pattern.direction == 'bullish' and fvg['direction'] == 'bullish':
                if fvg['fvg_low'] <= entry <= fvg['fvg_high']:
                    bonus += 0.04
                    notes.append('Inside bullish FVG')
                    break
    
    # Order block confluence: entry near an order block
    for ob in order_blocks:
        if not ob['broken'] and ob['direction'] == pattern.direction:
            if ob['ob_low'] <= entry <= ob['ob_high']:
                bonus += 0.05
                notes.append('Inside order block')
                break
    
    # BOS alignment: if a BOS was detected in the same direction recently
    if pattern.direction == 'bullish':
        recent_bos = df['bullish_bos'][-10:].any()
        if recent_bos:
            bonus += 0.03
            notes.append('Recent bullish BOS')
    
    pattern.confidence_score = min(1.0, pattern.confidence_score + bonus)
    pattern.notes += ' | '.join(notes)
    return pattern
```

---

<a name="part-4"></a>
# Part 4 — Implementation Priority

---

## 4.1 Pattern Reliability Rankings

Rankings based on academic backtesting data (Bulkowski's Encyclopedia of Chart Patterns), practical trading evidence, and alignment with algorithmic detectability.

| Rank | Pattern | Avg Win Rate | Avg R/R | Algorithmic Complexity | Recommended Priority |
|------|---------|-------------|---------|----------------------|---------------------|
| 1 | BOS (Bullish / Bearish) | 65%–72% | 2.5:1 | Low | ⭐⭐⭐⭐⭐ Implement First |
| 2 | Bull Flag / Bear Flag | 63%–68% | 2.5:1–4:1 | Low-Medium | ⭐⭐⭐⭐⭐ Implement First |
| 3 | Range Consolidation Breakout | 60%–65% | 1.5:1–2.5:1 | Low | ⭐⭐⭐⭐⭐ Implement First |
| 4 | Falling Wedge (bullish) | 61%–64% | 2.5:1 | Medium | ⭐⭐⭐⭐ |
| 5 | Ascending Triangle | 60%–63% | 1.8:1 | Medium | ⭐⭐⭐⭐ |
| 6 | Double Bottom | 59%–63% | 2:1 | Medium | ⭐⭐⭐⭐ |
| 7 | Inverse Head and Shoulders | 58%–63% | 2:1–2.5:1 | High | ⭐⭐⭐⭐ |
| 8 | Cup and Handle | 58%–62% | 2:1 | High | ⭐⭐⭐ |
| 9 | Pennant | 58%–62% | 2:1–3:1 | Medium | ⭐⭐⭐ |
| 10 | CHoCH | 55%–62% | 2:1 | Low-Medium | ⭐⭐⭐⭐ |
| 11 | Symmetrical Triangle | 54%–58% | 1.5:1 | Medium | ⭐⭐⭐ |
| 12 | Head and Shoulders | 54%–58% | 2:1 | High | ⭐⭐⭐ |
| 13 | Double Top | 53%–58% | 1.5:1–2:1 | Medium | ⭐⭐⭐ |
| 14 | Descending Triangle | 52%–57% | 1.5:1 | Medium | ⭐⭐⭐ |
| 15 | Rising Wedge (bearish) | 52%–56% | 2:1 | Medium | ⭐⭐ |
| 16 | Classic S/R Breakout | 50%–55% | 1.5:1 | Low | ⭐⭐⭐⭐ (simple to implement) |

**Notes on rankings:**
- Win rate estimates assume patterns are traded at confirmed breakout closes, not anticipatory entries
- R/R assumes standard measured-move targets
- Rankings shift meaningfully with volume confirmation: volume-confirmed patterns consistently outperform unconfirmed by 10%–15% win rate
- BOS ranked #1 because it is the structural foundation — detecting it improves every other pattern's reliability when used as a filter

---

## 4.2 Recommended Build Sequence

Given that the existing dashboard already has **EMA and Bollinger Band** infrastructure (EMAs already computed, price-relative-to-BB already tracked), the build sequence should maximize re-use of existing computations and incrementally layer pattern detection.

### Phase 1: Foundation (Week 1) — Build structural backbone

**1. Swing High/Low Detection**  
Everything else depends on this. Build `find_swing_highs_lows()` and `filter_significant_swings()` first. Validate visually on charts before proceeding.

**2. BOS Detection (Bullish + Bearish)**  
Once swings are reliable, BOS detection is just a few conditionals. This immediately gives the dashboard useful structural context.

**3. CHoCH Detection**  
Almost free once BOS is implemented — same swing point logic, different structural context check.

**4. Market Structure Classification**  
Using swing points + BOS/CHoCH, classify each bar's prevailing structure as `UPTREND | DOWNTREND | RANGING | TRANSITION`. Surface this as a dashboard indicator.

### Phase 2: High-Value Continuation Patterns (Weeks 2–3)

**5. Range Consolidation Breakout**  
Depends only on swing points and volume. Pairs naturally with the BB infrastructure (BB squeeze can confirm consolidation). Implement next.

**6. Bull Flag / Bear Flag**  
Linear regression on flag channel, flagpole detection. Re-uses EMA slope logic. High R/R, good for swing trading signals.

**7. Ascending Triangle**  
Re-uses swing high clustering (horizontal resistance detection) + linear regression on lows. Very common and reliable.

**8. Descending Triangle**  
Mirror of ascending; implement simultaneously.

### Phase 3: Reversal Patterns (Weeks 3–4)

**9. Double Top / Double Bottom**  
Straightforward once swing points work; cluster two swings at similar prices. Implement together.

**10. Pennant**  
Extension of flag detection — add converging channel check to flag flagpole logic.

**11. Symmetrical Triangle**  
Extension of triangle detection — replace flat boundary with sloping boundary check.

### Phase 4: Complex Patterns (Weeks 4–6)

**12. Rising Wedge / Falling Wedge**  
Extension of triangle: both lines slope the same direction, check compression direction.

**13. Head and Shoulders (+ Inverse)**  
Most complex due to symmetry requirements and neckline calculation. Best implemented with robust swing point history.

**14. Cup and Handle**  
Requires parabolic regression + duration handling. Save for last due to complexity.

### Phase 5: Order Blocks, FVG & Confluence Engine (Ongoing)

**15. Order Block Detection**  
Depends on BOS being already implemented. Add after Phase 1.

**16. Fair Value Gap Detection**  
Three-candle calculation; can add immediately after Phase 1.

**17. S/R Zone Clustering**  
Aggregate swing highs/lows into clustered zones; add to dashboard as horizontal lines.

**18. Confluence Scoring**  
Apply `apply_confluence_bonus()` to all detected patterns to boost confidence scores based on multi-factor alignment.

### Integration Architecture

```python
class PatternDetectionEngine:
    """
    Master class orchestrating all pattern detections.
    Call detect_all() to run the full pipeline.
    """
    def __init__(self, df: pd.DataFrame, ticker: str = '',
                 swing_n: int = 5, timeframe: str = 'daily'):
        self.df       = preprocess(df, swing_n)
        self.ticker   = ticker
        self.timeframe = timeframe
        self.patterns : list[DetectedPattern] = []
        self.sr_levels: list  = []
        self.fvgs     : list  = []
        self.order_blocks: list = []
    
    def detect_all(self) -> list[DetectedPattern]:
        # Phase 1: Structure
        self._detect_bos_choch()
        self._detect_order_blocks()
        self._detect_fvgs()
        self.sr_levels = cluster_sr_levels(self.df)
        
        # Phase 2–4: Breakout & Reversal Patterns
        self._detect_range_breakout()
        self._detect_sr_breakout()
        self._detect_flags()
        self._detect_pennants()
        self._detect_triangles()
        self._detect_wedges()
        self._detect_double_patterns()
        self._detect_hs_patterns()
        self._detect_cup_handle()
        
        # Phase 5: Confluence enhancement
        self.patterns = [
            apply_confluence_bonus(p, self.df, self.sr_levels,
                                   self.fvgs, self.order_blocks)
            for p in self.patterns
        ]
        
        # Sort by confidence descending
        self.patterns.sort(key=lambda p: p.confidence_score, reverse=True)
        return self.patterns
    
    def get_high_confidence(self, threshold: float = 0.70) -> list[DetectedPattern]:
        return [p for p in self.patterns if p.confidence_score >= threshold]
    
    def get_latest(self, n: int = 5) -> list[DetectedPattern]:
        """Return n most recently detected patterns."""
        sorted_by_bar = sorted(self.patterns, key=lambda p: p.detected_at_bar, reverse=True)
        return sorted_by_bar[:n]
```

### Streamlit Dashboard Integration

```python
# In your Streamlit app:
import streamlit as st

@st.cache_data(ttl=300)
def run_pattern_detection(ticker: str, df: pd.DataFrame) -> list:
    engine = PatternDetectionEngine(df, ticker=ticker, swing_n=5)
    return engine.detect_all()

# Render detected patterns on a Plotly/mplfinance chart:
def render_patterns(fig, patterns: list[DetectedPattern]):
    for p in patterns:
        if p.confidence_score >= 0.60:
            # Add annotation at detected_at_bar
            fig.add_annotation(
                x=p.detected_at_bar,
                y=p.entry_price,
                text=f"{p.pattern_type} ({p.confidence_score:.2f})",
                bgcolor='green' if p.direction == 'bullish' else 'red',
                font=dict(color='white', size=10),
            )
            # Draw stop and target lines
            if p.stop_loss:
                fig.add_hline(y=p.stop_loss, line_dash='dash', line_color='red',
                              annotation_text='SL')
            if p.target:
                fig.add_hline(y=p.target, line_dash='dash', line_color='green',
                              annotation_text='Target')
```

---

## Final Implementation Notes

1. **Avoid look-ahead bias:** All swing point detection requires `n` future bars. In live scanning, only scan the last `n+1` bars for final confirmation. Use `df.iloc[:-n]` sections when computing historical patterns to avoid look-ahead.

2. **Parameter sensitivity:** Run a grid search on historical data for each pattern's key parameters (primarily `swing_n`, `min_breakout_pct`, `volume_multiplier`) before hardcoding defaults. Parameters that work on SPY may need adjustment for small-cap stocks.

3. **Multi-timeframe confirmation:** A pattern detected on the daily chart that aligns with the weekly chart structure (same direction BOS on weekly) is significantly more reliable. Consider adding a weekly-data analysis layer.

4. **Pattern invalidation:** For each active detected pattern, define the price level at which it is "busted" (e.g., a bull flag is invalidated if price closes below the flagpole's base). Maintain an `active_patterns` list and mark patterns as `invalidated` when their busted level is breached.

5. **Backtesting before deployment:** Use `vectorbt` or a rolling-window simulation to backtest each pattern type on 5+ years of data before trusting the confidence scores in live use.

---

*End of Technical Pattern Analysis Specification v1.0*
