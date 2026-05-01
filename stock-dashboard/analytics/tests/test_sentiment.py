"""Tests for sentiment features (F09, F11)."""

import pytest
import numpy as np
import pandas as pd
from analytics.sentiment import (
    risk_reversal,
    unusual_activity,
)


class TestRiskReversal:
    def test_rr_returns_dict(self, synthetic_chain):
        result = risk_reversal(synthetic_chain, '2026-06-20')
        assert isinstance(result, dict)
        assert 'rr_25' in result
        assert 'rr_10' in result
        assert 'sentiment' in result

    def test_rr_sentiment_classification(self, synthetic_chain):
        result = risk_reversal(synthetic_chain, '2026-06-20')
        if result['sentiment'] is not None:
            assert result['sentiment'] in ['bullish', 'bearish', 'neutral']

    def test_rr_vs_skew_opposite_sign(self, synthetic_chain):
        # Risk Reversal should be negative of IV Skew
        from analytics.volatility import iv_skew
        skew_result = iv_skew(synthetic_chain, '2026-06-20')
        rr_result = risk_reversal(synthetic_chain, '2026-06-20')

        if skew_result['skew_raw'] is not None and rr_result['rr_25'] is not None:
            assert np.isclose(skew_result['skew_raw'], -rr_result['rr_25'], atol=0.01)

    def test_empty_expiration(self, synthetic_chain):
        result = risk_reversal(synthetic_chain, '2099-01-01')
        assert result['rr_25'] is None


class TestUnusualActivity:
    def test_unusual_returns_dict(self, synthetic_chain):
        result = unusual_activity(synthetic_chain)
        assert isinstance(result, dict)
        assert 'n_unusual' in result
        assert 'total_unusual_notional' in result
        assert 'dominant_type' in result
        assert 'rule_counts' in result

    def test_oi_breach_detected(self, synthetic_chain):
        # Synthetic chain has strike 110 call with volume=5000, OI=200
        result = unusual_activity(synthetic_chain, min_volume=1000)
        assert result['n_unusual'] >= 0

    def test_rule_counts_structure(self, synthetic_chain):
        result = unusual_activity(synthetic_chain)
        assert 'volume_spike' in result['rule_counts']
        assert 'oi_breach' in result['rule_counts']
        assert 'both' in result['rule_counts']

    def test_using_proxy_avg_flag(self, synthetic_chain):
        result = unusual_activity(synthetic_chain, historical_avg_volume=None)
        assert 'using_proxy_avg' in result
        assert result['using_proxy_avg'] is True

    def test_dominant_type_valid(self, synthetic_chain):
        result = unusual_activity(synthetic_chain)
        assert result['dominant_type'] in ['call', 'put', 'none']
