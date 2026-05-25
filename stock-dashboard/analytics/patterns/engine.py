"""
PatternDetectionEngine — orchestrates all detectors in priority order
and applies confluence bonuses.
"""

from __future__ import annotations

from typing import List

import pandas as pd

from .helpers import (
    DetectedPattern,
    apply_confluence_bonus,
    cluster_sr_levels,
    preprocess,
)
from .structure import detect_bos_choch, detect_fvgs, detect_order_blocks
from .breakouts import (
    detect_ascending_triangle,
    detect_bear_flag,
    detect_bull_flag,
    detect_cup_handle,
    detect_descending_triangle,
    detect_double_bottom,
    detect_double_top,
    detect_falling_wedge,
    detect_head_and_shoulders,
    detect_inv_head_and_shoulders,
    detect_pennant,
    detect_range_consolidation_breakout,
    detect_rising_wedge,
    detect_sr_breakout,
    detect_symmetrical_triangle,
)


class PatternDetectionEngine:
    """
    Run detect_all() to execute the full 5-phase pipeline and return a list
    of DetectedPattern objects sorted by confidence (descending).
    """

    def __init__(
        self,
        df: pd.DataFrame,
        ticker: str = "",
        swing_n: int = 5,
        timeframe: str = "daily",
    ) -> None:
        self.df        = preprocess(df, swing_n)
        self.ticker    = ticker
        self.timeframe = timeframe
        self.patterns:      List[DetectedPattern] = []
        self.sr_levels:     List[dict]             = []
        self.fvgs:          List[dict]             = []
        self.order_blocks:  List[dict]             = []

    # ── Public API ───────────────────────────────────────────────────────────

    def detect_all(self) -> List[DetectedPattern]:
        kw = dict(ticker=self.ticker, timeframe=self.timeframe)

        # Phase 1 — Market Structure
        self.patterns.extend(detect_bos_choch(self.df, **kw))
        self.order_blocks = detect_order_blocks(self.df)
        self.fvgs         = detect_fvgs(self.df)
        self.sr_levels    = cluster_sr_levels(self.df)

        # Phase 2 — Simple Breakout Patterns
        self.patterns.extend(detect_range_consolidation_breakout(self.df, **kw))
        self.patterns.extend(detect_sr_breakout(self.df, **kw))

        # Phase 3 — Trend Continuation
        self.patterns.extend(detect_bull_flag(self.df, **kw))
        self.patterns.extend(detect_bear_flag(self.df, **kw))
        self.patterns.extend(detect_pennant(self.df, **kw))

        # Phase 4 — Triangles & Wedges
        self.patterns.extend(detect_ascending_triangle(self.df, **kw))
        self.patterns.extend(detect_descending_triangle(self.df, **kw))
        self.patterns.extend(detect_symmetrical_triangle(self.df, **kw))
        self.patterns.extend(detect_rising_wedge(self.df, **kw))
        self.patterns.extend(detect_falling_wedge(self.df, **kw))

        # Phase 5 — Reversal Patterns
        self.patterns.extend(detect_double_top(self.df, **kw))
        self.patterns.extend(detect_double_bottom(self.df, **kw))
        self.patterns.extend(detect_head_and_shoulders(self.df, **kw))
        self.patterns.extend(detect_inv_head_and_shoulders(self.df, **kw))
        self.patterns.extend(detect_cup_handle(self.df, **kw))

        # Phase 6 — Confluence enhancement
        self.patterns = [
            apply_confluence_bonus(p, self.df, self.sr_levels, self.fvgs, self.order_blocks)
            for p in self.patterns
        ]

        # Sort by confidence descending
        self.patterns.sort(key=lambda p: p.confidence_score, reverse=True)
        return self.patterns

    def get_high_confidence(self, threshold: float = 0.70) -> List[DetectedPattern]:
        return [p for p in self.patterns if p.confidence_score >= threshold]

    def get_latest(self, n: int = 5) -> List[DetectedPattern]:
        return sorted(self.patterns, key=lambda p: p.detected_at_bar, reverse=True)[:n]
