"""
backtest_engine.py

Core backtest orchestration. Accepts a run configuration, extracts signals
from backtest_signals.py, fetches forward returns from yfinance, scores
all signals against outcomes, computes per-signal-type metrics, and persists
everything to backtest.db.

Public API
----------
run_backtest(run_config: dict) -> str
    Orchestrates a full run and returns the run_id string.

get_run_signals(run_id: str) -> list[dict]
    Returns all signals for a completed run.

get_run_outcomes(run_id: str) -> list[dict]
    Returns all outcomes (joined with their signals) for a completed run.

get_run_metrics(run_id: str) -> list[dict]
    Returns all metrics rows for a completed run.

compute_equity_curve(signals_df: pd.DataFrame, outcomes_df: pd.DataFrame) -> pd.DataFrame
    Returns a date-indexed DataFrame of cumulative portfolio value by signal type.
"""

import json
import os
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import spearmanr

from data.backtest_signals import (
    get_hedge_fund_signals,
    get_macro_signals,
    get_mpt_signals,
    get_news_signals,
    get_options_ai_signals,
    get_reddit_signals,
    get_technical_signals,
)

# ---------------------------------------------------------------------------
# DB path + connection
# ---------------------------------------------------------------------------

_BACKTEST_DB  = os.path.join(os.path.dirname(__file__), "..", "db", "backtest.db")
_SCHEMA_PATH  = os.path.join(os.path.dirname(__file__), "..", "db", "backtest_schema.sql")

_RISK_FREE_DAILY = 0.045 / 252  # 4.5% annual risk-free rate, dailized


def _get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_BACKTEST_DB), exist_ok=True)
    conn = sqlite3.connect(_BACKTEST_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create backtest.db tables from backtest_schema.sql if they don't exist."""
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        ddl = fh.read()
    conn = _get_connection()
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()


init_db()  # run at import time


# ---------------------------------------------------------------------------
# Forward return fetching
# ---------------------------------------------------------------------------

def fetch_forward_returns(
    ticker: str,
    signal_dates: list[date],
    horizon_days: int,
    ohlcv_df: Optional[pd.DataFrame] = None,
) -> dict[date, float]:
    """Compute lookahead-safe forward returns for each signal date.

    Lookahead prevention protocol:
      - Entry price: first available OPEN on T+1 (day after signal date)
      - Exit price: CLOSE on T+N (where N = horizon_days trading days after entry)
      - Forward return = (exit_close - entry_open) / entry_open
      - Signal date itself (T) is NEVER used as entry or exit

    Parameters
    ----------
    ticker       : Uppercase ticker string.
    signal_dates : List of date objects for which to compute forward returns.
    horizon_days : Number of trading days after T+1 open to measure exit.
    ohlcv_df     : Optional pre-fetched OHLCV DataFrame. If None, fetches from yfinance.

    Returns
    -------
    Dict mapping each signal_date to its forward_return float.
    Returns empty dict if data fetch fails. Dates with insufficient forward
    data (e.g., too close to today) are omitted from the result.
    """
    if ohlcv_df is None:
        try:
            df = yf.Ticker(ticker.upper()).history(period="2y")
        except Exception:
            return {}
        if df is None or df.empty:
            return {}
    else:
        df = ohlcv_df.copy()

    # Normalize index to tz-naive
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    df.columns = [c.lower() for c in df.columns]

    result: dict[date, float] = {}
    trading_dates = [ts.date() for ts in df.index]

    for sig_date in signal_dates:
        # Find index of first trading day AFTER signal date (T+1)
        try:
            entry_idx = next(
                i for i, d in enumerate(trading_dates) if d > sig_date
            )
        except StopIteration:
            continue  # No trading day after signal date

        # Find index of T+N exit (horizon_days trading days after entry)
        exit_idx = entry_idx + horizon_days
        if exit_idx >= len(trading_dates):
            continue  # Not enough forward data

        entry_price = float(df.iloc[entry_idx]["open"])
        exit_price  = float(df.iloc[exit_idx]["close"])

        if entry_price <= 0:
            continue

        fwd_return = (exit_price - entry_price) / entry_price
        result[sig_date] = round(fwd_return, 6)

    return result


def fetch_spy_returns(
    signal_dates: list[date],
    horizon_days: int,
) -> dict[date, float]:
    """Compute SPY forward returns for the same signal dates and horizon.

    Uses the same lookahead-safe protocol as fetch_forward_returns().

    Parameters
    ----------
    signal_dates : List of date objects.
    horizon_days : Number of trading days for the forward window.

    Returns
    -------
    Dict mapping each signal_date to its SPY forward_return.
    """
    try:
        df = yf.Ticker("SPY").history(period="2y")
    except Exception:
        return {}
    if df is None or df.empty:
        return {}

    return fetch_forward_returns("SPY", signal_dates, horizon_days, ohlcv_df=df)


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------

def compute_ic(
    scores: list[float],
    forward_returns: list[float],
) -> tuple[float, float]:
    """Compute the Information Coefficient (IC) as Spearman rank correlation.

    IC is the Spearman rank correlation between signal scores and forward returns.
    IC > 0.05 is meaningful; IC > 0.10 is strong.

    Parameters
    ----------
    scores          : List of numeric signal scores in [-1, 1].
    forward_returns : Corresponding list of forward return floats.

    Returns
    -------
    Tuple of (ic, ic_std) where:
      ic      = Spearman rank correlation coefficient
      ic_std  = Standard deviation of IC (approximated as sqrt((1 - ic^2) / (n - 2)))
                for single-period IC; returns 0.0 if n < 3.
    """
    n = len(scores)
    if n < 3:
        return 0.0, 0.0

    corr, _ = spearmanr(scores, forward_returns)
    ic = float(corr) if not np.isnan(corr) else 0.0

    # Approximate standard error of Spearman rho
    if n > 2:
        ic_std = float(np.sqrt((1.0 - ic ** 2) / max(1, n - 2)))
    else:
        ic_std = 0.0

    return round(ic, 6), round(ic_std, 6)


def compute_hit_rate(
    directions: list[str],
    forward_returns: list[float],
) -> float:
    """Compute directional hit rate, excluding neutral signals.

    Hit = 1 if (direction == 'bullish' and forward_return > 0)
               OR (direction == 'bearish' and forward_return < 0)
    Hit = 0 otherwise.
    Neutral signals are excluded from the denominator.

    Parameters
    ----------
    directions      : List of 'bullish'|'bearish'|'neutral' strings.
    forward_returns : Corresponding forward return floats.

    Returns
    -------
    Hit rate as a float in [0, 1]. Returns 0.0 if no directional signals.
    """
    hits = 0
    total = 0
    for d, r in zip(directions, forward_returns):
        if d == "neutral":
            continue
        total += 1
        if d == "bullish" and r > 0:
            hits += 1
        elif d == "bearish" and r < 0:
            hits += 1

    return round(hits / total, 4) if total > 0 else 0.0


def compute_profit_factor(
    directions: list[str],
    forward_returns: list[float],
) -> float:
    """Compute profit factor: gross_profit / gross_loss from directional signals.

    Profit  = sum of |forward_return| where direction was correct
    Loss    = sum of |forward_return| where direction was wrong
    Neutral signals are excluded.
    Returns 0.0 if no losses (undefined); returns gross_profit / gross_loss otherwise.

    Parameters
    ----------
    directions      : List of 'bullish'|'bearish'|'neutral' strings.
    forward_returns : Corresponding forward return floats.

    Returns
    -------
    Profit factor as a float. Returns float('inf') if there are profits but no losses.
    Returns 0.0 if no directional signals.
    """
    gross_profit = 0.0
    gross_loss   = 0.0

    for d, r in zip(directions, forward_returns):
        if d == "neutral":
            continue
        correct = (d == "bullish" and r > 0) or (d == "bearish" and r < 0)
        if correct:
            gross_profit += abs(r)
        else:
            gross_loss += abs(r)

    if gross_loss == 0.0:
        return float("inf") if gross_profit > 0 else 0.0
    return round(gross_profit / gross_loss, 4)


def compute_equity_curve(
    signals_df: pd.DataFrame,
    outcomes_df: pd.DataFrame,
) -> pd.DataFrame:
    """Simulate a $10,000 portfolio following each signal type and return equity curves.

    Trading rules for the simulation:
      - Start with $10,000 per signal type.
      - On each signal date T, the strategy enters at T+1 open and exits at T+N close
        (forward return already computed in outcomes_df).
      - Each trade invests the entire current portfolio value / N_concurrent_signals
        (equal-weighted among all signals active on that date for a given type).
      - Simplified: treat each signal independently (size = $10,000 / total_signals).
        Sum the position-level gains to get total portfolio change per signal type per date.
      - Neutral signals are excluded (do not trade).
      - If direction == 'bullish' → long → gain = forward_return
      - If direction == 'bearish' → short → gain = -forward_return
      - SPY buy-and-hold: compute SPY's compound return over the same date range
        by chaining (1 + spy_daily_return) for each day.

    Parameters
    ----------
    signals_df  : DataFrame with columns [id, run_id, ticker, signal_date,
                  signal_type, direction, score, source_db, raw_json]
    outcomes_df : DataFrame with columns [id, signal_id, forward_return,
                  benchmark_return, alpha, price_at_signal, price_at_horizon, correct]

    Returns
    -------
    DataFrame with columns: date (datetime.date), signal_type (str), portfolio_value (float).
    Sorted by signal_type, date. Also includes a 'SPY' row per date for the benchmark.
    """
    if signals_df.empty or outcomes_df.empty:
        return pd.DataFrame(columns=["date", "signal_type", "portfolio_value"])

    # outcomes_df may come from get_run_outcomes() which is already a joined query
    # (contains signal-level columns like direction, ticker, signal_type).
    # Keep only the pure outcome columns to avoid duplicate-column suffixing on merge.
    _OUTCOME_COLS = {"id", "signal_id", "forward_return", "benchmark_return",
                     "alpha", "price_at_signal", "price_at_horizon", "correct"}
    outcomes_clean = outcomes_df[[c for c in outcomes_df.columns if c in _OUTCOME_COLS]].copy()

    # Join signals and outcomes
    merged = signals_df.merge(
        outcomes_clean,
        left_on="id",
        right_on="signal_id",
        how="inner",
    )

    # Exclude rows with missing forward_return or neutral direction
    merged = merged[
        merged["forward_return"].notna() &
        (merged["direction"] != "neutral")
    ].copy()

    if merged.empty:
        return pd.DataFrame(columns=["date", "signal_type", "portfolio_value"])

    # Ensure signal_date is a date object
    merged["signal_date"] = pd.to_datetime(merged["signal_date"]).dt.date

    signal_types = merged["signal_type"].unique().tolist()
    all_signal_dates = sorted(merged["signal_date"].unique().tolist())

    records = []
    _INITIAL_CAPITAL = 10_000.0

    for stype in signal_types:
        subset = merged[merged["signal_type"] == stype].sort_values("signal_date")
        if subset.empty:
            continue

        total_trades = len(subset)
        if total_trades == 0:
            continue

        # Equal-sized allocation per trade
        trade_size = _INITIAL_CAPITAL / total_trades
        portfolio_value = _INITIAL_CAPITAL

        cum_records = []
        running = _INITIAL_CAPITAL

        for _, trade_row in subset.iterrows():
            fwd = float(trade_row["forward_return"])
            direction = trade_row["direction"]

            # P&L per trade: long if bullish, short if bearish
            if direction == "bullish":
                pnl = trade_size * fwd
            else:  # bearish
                pnl = trade_size * (-fwd)

            running += pnl
            cum_records.append({
                "date":           trade_row["signal_date"],
                "signal_type":    stype,
                "portfolio_value": round(running, 4),
            })

        records.extend(cum_records)

    # SPY benchmark: chain daily SPY returns over the signal date range
    # Use benchmark_return from the first signal type as the SPY series
    spy_rows = merged[merged["direction"] != "neutral"].drop_duplicates(
        subset=["signal_date"]
    ).sort_values("signal_date")[["signal_date", "benchmark_return"]].dropna()

    if not spy_rows.empty:
        spy_value = _INITIAL_CAPITAL
        for _, spy_row in spy_rows.iterrows():
            spy_return = float(spy_row["benchmark_return"])
            spy_value *= (1.0 + spy_return)
            records.append({
                "date":           spy_row["signal_date"],
                "signal_type":    "SPY",
                "portfolio_value": round(spy_value, 4),
            })

    if not records:
        return pd.DataFrame(columns=["date", "signal_type", "portfolio_value"])

    result = pd.DataFrame(records)
    result = result.sort_values(["signal_type", "date"]).reset_index(drop=True)
    return result


def _compute_sharpe(returns: list[float]) -> float:
    """Compute annualized Sharpe ratio for a series of per-trade returns.

    Assumes each return represents one period of horizon_days trading days.
    Annualizes by multiplying daily Sharpe by sqrt(252).

    Parameters
    ----------
    returns : List of forward return floats (not compounded).

    Returns
    -------
    Annualized Sharpe ratio. Returns 0.0 if std == 0 or len < 2.
    """
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns, dtype=float)
    mean_r = np.mean(arr)
    std_r  = np.std(arr, ddof=1)
    if std_r == 0.0:
        return 0.0
    sharpe_per_period = (mean_r - _RISK_FREE_DAILY) / std_r
    # Annualize (each return spans horizon_days; 252/horizon_days periods/year)
    return round(float(sharpe_per_period * np.sqrt(252)), 4)


def _compute_max_drawdown(equity_curve: list[float]) -> float:
    """Compute maximum peak-to-trough drawdown from an equity curve.

    Parameters
    ----------
    equity_curve : List of portfolio values in chronological order.

    Returns
    -------
    Maximum drawdown as a negative float in (-inf, 0]. E.g., -0.25 = 25% drawdown.
    Returns 0.0 if curve has fewer than 2 points.
    """
    if len(equity_curve) < 2:
        return 0.0
    arr = np.array(equity_curve, dtype=float)
    peak = arr[0]
    max_dd = 0.0
    for val in arr[1:]:
        if val > peak:
            peak = val
        dd = (val - peak) / peak if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd
    return round(float(max_dd), 6)


# ---------------------------------------------------------------------------
# Score (compute metrics for a completed run)
# ---------------------------------------------------------------------------

def score_run(run_id: str) -> dict:
    """Compute all metrics for a completed run and write them to backtest_metrics.

    Reads backtest_signals and backtest_outcomes from backtest.db for the given
    run_id, computes per-signal-type metrics, writes to backtest_metrics, and
    returns a summary dict.

    Parameters
    ----------
    run_id : The UUID string identifying the run.

    Returns
    -------
    Dict with keys: run_id, metrics (list of metric dicts, one per signal_type).
    """
    conn = _get_connection()
    try:
        run_row = conn.execute(
            "SELECT * FROM backtest_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run_row is None:
            return {"error": f"No run found with id {run_id}"}

        horizon_days = int(dict(run_row)["horizon_days"])

        sig_rows = conn.execute(
            "SELECT * FROM backtest_signals WHERE run_id = ?", (run_id,)
        ).fetchall()
        out_rows = conn.execute(
            """
            SELECT o.*
            FROM backtest_outcomes o
            JOIN backtest_signals s ON s.id = o.signal_id
            WHERE s.run_id = ?
            """,
            (run_id,),
        ).fetchall()
    finally:
        conn.close()

    if not sig_rows or not out_rows:
        return {"run_id": run_id, "metrics": []}

    signals_df  = pd.DataFrame([dict(r) for r in sig_rows])
    outcomes_df = pd.DataFrame([dict(r) for r in out_rows])

    # Merge signals with outcomes on id -> signal_id
    merged = signals_df.merge(
        outcomes_df,
        left_on="id",
        right_on="signal_id",
        how="inner",
        suffixes=("_sig", "_out"),
    )

    # Only include rows with a valid forward_return
    merged = merged[merged["forward_return"].notna()].copy()

    _MIN_SIGNALS = 5  # Must have at least 5 valid signals to compute metrics
    metrics_list = []

    for stype in merged["signal_type"].unique():
        sdf = merged[merged["signal_type"] == stype]
        n   = len(sdf)
        if n < _MIN_SIGNALS:
            metrics_list.append({
                "signal_type":  stype,
                "signal_count": n,
                "insufficient": True,
            })
            continue

        fwd_returns = sdf["forward_return"].tolist()
        scores      = sdf["score"].tolist()
        directions  = sdf["direction"].tolist()
        alphas      = sdf["alpha"].dropna().tolist()

        # IC
        ic, ic_std = compute_ic(scores, fwd_returns)
        icir = round(ic / ic_std, 4) if ic_std > 0 else 0.0
        t_stat = round(ic / (ic_std / np.sqrt(n)), 4) if ic_std > 0 else 0.0

        # Hit rate
        hit_rate = compute_hit_rate(directions, fwd_returns)

        # Average returns by direction
        bull_rets = [r for d, r in zip(directions, fwd_returns) if d == "bullish"]
        bear_rets = [r for d, r in zip(directions, fwd_returns) if d == "bearish"]
        neut_rets = [r for d, r in zip(directions, fwd_returns) if d == "neutral"]

        avg_bull = round(float(np.mean(bull_rets)), 6) if bull_rets else None
        avg_bear = round(float(np.mean(bear_rets)), 6) if bear_rets else None
        avg_neut = round(float(np.mean(neut_rets)), 6) if neut_rets else None

        # Profit factor
        pf = compute_profit_factor(directions, fwd_returns)

        # Alpha
        avg_alpha = round(float(np.mean(alphas)), 6) if alphas else None

        # Sharpe (on all directional returns as if long/short)
        directional_pnls = []
        for d, r in zip(directions, fwd_returns):
            if d == "bullish":
                directional_pnls.append(r)
            elif d == "bearish":
                directional_pnls.append(-r)
        sharpe = _compute_sharpe(directional_pnls)

        # Max drawdown from equity curve
        equity_curve_df = compute_equity_curve(signals_df, outcomes_df)
        stype_curve = equity_curve_df[
            equity_curve_df["signal_type"] == stype
        ]["portfolio_value"].tolist()
        max_dd = _compute_max_drawdown(stype_curve) if stype_curve else 0.0

        row = {
            "run_id":          run_id,
            "signal_type":     stype,
            "horizon_days":    horizon_days,
            "signal_count":    n,
            "hit_rate":        hit_rate,
            "avg_return_bull": avg_bull,
            "avg_return_bear": avg_bear,
            "avg_return_neutral": avg_neut,
            "ic":              ic,
            "ic_std":          ic_std,
            "icir":            icir,
            "t_stat":          t_stat,
            "profit_factor":   pf if pf != float("inf") else 9999.0,
            "avg_alpha":       avg_alpha,
            "sharpe":          sharpe,
            "max_drawdown":    max_dd,
        }
        metrics_list.append(row)

    # Write to backtest_metrics (replace any prior run's metrics rows)
    conn = _get_connection()
    try:
        conn.execute(
            "DELETE FROM backtest_metrics WHERE run_id = ?", (run_id,)
        )
        for m in metrics_list:
            if m.get("insufficient"):
                continue
            conn.execute(
                """
                INSERT INTO backtest_metrics (
                    run_id, signal_type, horizon_days, signal_count,
                    hit_rate, avg_return_bull, avg_return_bear, avg_return_neutral,
                    ic, ic_std, icir, t_stat, profit_factor, avg_alpha,
                    sharpe, max_drawdown
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    m["signal_type"],
                    m["horizon_days"],
                    m["signal_count"],
                    m["hit_rate"],
                    m["avg_return_bull"],
                    m["avg_return_bear"],
                    m["avg_return_neutral"],
                    m["ic"],
                    m["ic_std"],
                    m["icir"],
                    m["t_stat"],
                    m["profit_factor"],
                    m["avg_alpha"],
                    m["sharpe"],
                    m["max_drawdown"],
                ),
            )
        conn.commit()
    finally:
        conn.close()

    return {"run_id": run_id, "metrics": metrics_list}


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_backtest(run_config: dict) -> str:
    """Orchestrate a full backtest run and return the run_id.

    Parameters
    ----------
    run_config : Dict with keys:
        tickers      : list[str] — uppercase ticker strings
        date_from    : datetime.date — start date
        date_to      : datetime.date — end date
        horizon_days : int — 1|5|10|20
        signal_types : list[str] — subset of ['options_ai','news','reddit',
                       'hedge_fund','macro','mpt','technical']

    Returns
    -------
    run_id string (UUID4). Raises ValueError on invalid config.

    Side effects
    ------------
    Writes rows to backtest_runs, backtest_signals, backtest_outcomes,
    and backtest_metrics in backtest.db.
    """
    tickers      = [t.upper() for t in run_config["tickers"]]
    date_from    = run_config["date_from"]
    date_to      = run_config["date_to"]
    horizon_days = int(run_config["horizon_days"])
    signal_types = run_config["signal_types"]

    if not tickers:
        raise ValueError("At least one ticker is required.")
    if date_from > date_to:
        raise ValueError("date_from must be <= date_to.")
    if horizon_days not in (1, 5, 10, 20):
        raise ValueError("horizon_days must be 1, 5, 10, or 20.")

    run_id   = str(uuid.uuid4())
    now_iso  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    # Persist the run metadata
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO backtest_runs (run_id, tickers, date_from, date_to,
                                       horizon_days, signal_types, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                json.dumps(tickers),
                date_from.isoformat(),
                date_to.isoformat(),
                horizon_days,
                json.dumps(signal_types),
                now_iso,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # ── Step 1: Gather all signals ──────────────────────────────────────────
    all_signals: list[dict] = []

    if "options_ai" in signal_types:
        all_signals.extend(get_options_ai_signals(tickers, date_from, date_to))

    if "news" in signal_types:
        all_signals.extend(get_news_signals(tickers, date_from, date_to))

    if "reddit" in signal_types:
        all_signals.extend(get_reddit_signals(tickers, date_from, date_to))

    if "hedge_fund" in signal_types:
        all_signals.extend(get_hedge_fund_signals(tickers, date_from, date_to))

    if "macro" in signal_types:
        all_signals.extend(get_macro_signals(date_from, date_to))

    if "mpt" in signal_types:
        all_signals.extend(get_mpt_signals(tickers, date_from, date_to))

    if "technical" in signal_types:
        all_signals.extend(get_technical_signals(tickers, date_from, date_to))

    if not all_signals:
        return run_id  # No signals found; run exists but will be empty

    # ── Step 2: Persist signals ─────────────────────────────────────────────
    signal_id_map: dict[int, dict] = {}  # row insertion order -> signal dict

    conn = _get_connection()
    try:
        for sig in all_signals:
            cursor = conn.execute(
                """
                INSERT INTO backtest_signals
                    (run_id, ticker, signal_date, signal_type, direction,
                     score, source_db, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    sig["ticker"],
                    sig["signal_date"].isoformat(),
                    sig["signal_type"],
                    sig["direction"],
                    sig["score"],
                    sig.get("source_db", ""),
                    sig.get("raw_json", "{}"),
                ),
            )
            inserted_id = cursor.lastrowid
            signal_id_map[inserted_id] = sig
        conn.commit()
    finally:
        conn.close()

    # ── Step 3: Fetch forward returns ───────────────────────────────────────
    # Group signal dates by ticker to minimize yfinance calls (one fetch per ticker)
    from collections import defaultdict
    ticker_to_dates: dict[str, list[date]] = defaultdict(list)
    for sig_id, sig in signal_id_map.items():
        ticker_to_dates[sig["ticker"]].append(sig["signal_date"])

    # Fetch OHLCV once per ticker
    ticker_ohlcv: dict[str, pd.DataFrame] = {}
    for ticker in ticker_to_dates:
        try:
            df = yf.Ticker(ticker).history(period="2y")
            if df is not None and not df.empty:
                df.index = pd.to_datetime(df.index).tz_localize(None)
                df.columns = [c.lower() for c in df.columns]
                ticker_ohlcv[ticker] = df
        except Exception:
            pass

    # Fetch SPY once
    spy_ohlcv: Optional[pd.DataFrame] = None
    try:
        spy_df = yf.Ticker("SPY").history(period="2y")
        if spy_df is not None and not spy_df.empty:
            spy_df.index = pd.to_datetime(spy_df.index).tz_localize(None)
            spy_df.columns = [c.lower() for c in spy_df.columns]
            spy_ohlcv = spy_df
    except Exception:
        pass

    # Compute forward returns per ticker
    ticker_fwd_returns: dict[str, dict[date, float]] = {}
    for ticker, dates in ticker_to_dates.items():
        ohlcv = ticker_ohlcv.get(ticker)
        fwd = fetch_forward_returns(ticker, list(set(dates)), horizon_days, ohlcv_df=ohlcv)
        ticker_fwd_returns[ticker] = fwd

    # Compute SPY forward returns for all unique signal dates
    all_signal_dates = list(set(sig["signal_date"] for sig in signal_id_map.values()))
    spy_fwd_returns = fetch_spy_returns(all_signal_dates, horizon_days) if spy_ohlcv is None else \
        fetch_forward_returns("SPY", all_signal_dates, horizon_days, ohlcv_df=spy_ohlcv)

    # ── Step 4: Persist outcomes ────────────────────────────────────────────
    conn = _get_connection()
    try:
        for sig_id, sig in signal_id_map.items():
            ticker     = sig["ticker"]
            sig_date   = sig["signal_date"]
            direction  = sig["direction"]

            fwd_return = ticker_fwd_returns.get(ticker, {}).get(sig_date)
            bench_ret  = spy_fwd_returns.get(sig_date)
            alpha      = (fwd_return - bench_ret) if (fwd_return is not None and bench_ret is not None) else None

            # price_at_signal: close price on signal date T
            price_at_signal  = None
            price_at_horizon = None
            ohlcv = ticker_ohlcv.get(ticker)
            if ohlcv is not None:
                sig_date_ts = pd.Timestamp(sig_date)
                exact_rows = ohlcv[ohlcv.index.date == sig_date]
                if not exact_rows.empty:
                    price_at_signal = float(exact_rows.iloc[-1]["close"])

                # horizon price: close on T+N trading days after T+1 open
                trading_dates = [ts.date() for ts in ohlcv.index]
                try:
                    entry_idx = next(i for i, d in enumerate(trading_dates) if d > sig_date)
                    exit_idx  = entry_idx + horizon_days
                    if exit_idx < len(trading_dates):
                        price_at_horizon = float(ohlcv.iloc[exit_idx]["close"])
                except StopIteration:
                    pass

            # correct: 1 if direction matched, 0 if not, NULL if neutral
            if direction == "neutral" or fwd_return is None:
                correct = None
            elif direction == "bullish" and fwd_return > 0:
                correct = 1
            elif direction == "bearish" and fwd_return < 0:
                correct = 1
            else:
                correct = 0

            conn.execute(
                """
                INSERT INTO backtest_outcomes
                    (signal_id, forward_return, benchmark_return, alpha,
                     price_at_signal, price_at_horizon, correct)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sig_id,
                    fwd_return,
                    bench_ret,
                    alpha,
                    price_at_signal,
                    price_at_horizon,
                    correct,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    # ── Step 5: Compute and persist metrics ─────────────────────────────────
    score_run(run_id)

    return run_id


# ---------------------------------------------------------------------------
# Read helpers (used by page)
# ---------------------------------------------------------------------------

def get_run_signals(run_id: str) -> list[dict]:
    """Return all backtest_signals rows for a run as a list of dicts."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM backtest_signals WHERE run_id = ?", (run_id,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_run_outcomes(run_id: str) -> list[dict]:
    """Return backtest_outcomes joined with backtest_signals for a run."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT s.id AS signal_id, s.ticker, s.signal_date, s.signal_type,
                   s.direction, s.score, s.raw_json,
                   o.forward_return, o.benchmark_return, o.alpha,
                   o.price_at_signal, o.price_at_horizon, o.correct
            FROM backtest_signals s
            JOIN backtest_outcomes o ON o.signal_id = s.id
            WHERE s.run_id = ?
            ORDER BY s.signal_date ASC
            """,
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_run_metrics(run_id: str) -> list[dict]:
    """Return all backtest_metrics rows for a run as a list of dicts."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM backtest_metrics WHERE run_id = ?", (run_id,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def list_recent_runs(limit: int = 20) -> list[dict]:
    """Return the most recent N backtest runs as a list of dicts."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
