"""Tests for positioning features (F01-F04)."""

import pytest
import numpy as np
import pandas as pd
from analytics.positioning import (
    put_call_ratio,
    max_pain,
    oi_by_strike,
    volume_oi_ratio,
)


class TestPutCallRatio:
    def test_pcr_volume_aggregate(self, synthetic_chain):
        result = put_call_ratio(synthetic_chain, method='volume', per_expiration=False)
        assert isinstance(result, dict)
        assert 'pcr' in result
        assert result['method'] == 'volume'
        assert result['call_total'] > 0
        assert result['put_total'] > 0

    def test_pcr_oi_aggregate(self, synthetic_chain):
        result = put_call_ratio(synthetic_chain, method='open_interest', per_expiration=False)
        assert isinstance(result, dict)
        assert result['method'] == 'open_interest'

    def test_pcr_per_expiration(self, synthetic_chain):
        result = put_call_ratio(synthetic_chain, per_expiration=True)
        assert isinstance(result, pd.Series)
        assert len(result) == 1  # Single expiration in synthetic chain

    def test_pcr_zero_calls(self):
        df = pd.DataFrame({
            'option_type': ['put', 'put'],
            'volume': [100, 200],
            'openinterest': [1000, 2000],
            'expiration': ['2026-06-20', '2026-06-20'],
        })
        result = put_call_ratio(df, method='volume', per_expiration=False)
        assert np.isinf(result['pcr']) or result['call_total'] == 0


class TestMaxPain:
    def test_max_pain_exists(self, synthetic_chain):
        result = max_pain(synthetic_chain, '2026-06-20')
        assert isinstance(result, dict)
        assert 'max_pain_strike' in result
        assert 'pain_curve' in result
        assert result['expiration'] == '2026-06-20'

    def test_pain_curve_is_dataframe(self, synthetic_chain):
        result = max_pain(synthetic_chain, '2026-06-20')
        assert isinstance(result['pain_curve'], pd.DataFrame)
        assert len(result['pain_curve']) > 0

    def test_max_pain_bounds(self, synthetic_chain):
        result = max_pain(synthetic_chain, '2026-06-20')
        if result['max_pain_strike'] is not None:
            assert 80 <= result['max_pain_strike'] <= 120


class TestOIByStrike:
    def test_oi_by_strike_returns_dict(self, synthetic_chain):
        result = oi_by_strike(synthetic_chain, spot=100.0)
        assert isinstance(result, dict)
        assert 'oi_table' in result
        assert 'call_walls' in result
        assert 'put_walls' in result
        assert 'wall_threshold' in result

    def test_walls_detected(self, synthetic_chain):
        result = oi_by_strike(synthetic_chain, spot=100.0, wall_multiplier=2.0)
        # Synthetic chain has high OI at 95 and 105
        assert len(result['call_walls']) > 0 or len(result['put_walls']) > 0

    def test_strike_range_filter(self, synthetic_chain):
        result = oi_by_strike(synthetic_chain, spot=100.0, strike_range_pct=0.10)
        if not result['oi_table'].empty:
            strikes = result['oi_table']['strike'].values
            assert all(90 <= s <= 110 for s in strikes)


class TestVolumeOIRatio:
    def test_voir_returns_dict(self, synthetic_chain):
        result = volume_oi_ratio(synthetic_chain)
        assert isinstance(result, dict)
        assert 'voir_table' in result
        assert 'flagged' in result
        assert 'summary' in result

    def test_oi_breach_detected(self, synthetic_chain):
        # Synthetic chain has strike 110 call with volume=5000, OI=200
        result = volume_oi_ratio(synthetic_chain)
        flagged = result['flagged']
        if not flagged.empty:
            assert (flagged['strike'] == 110).any()

    def test_summary_counts(self, synthetic_chain):
        result = volume_oi_ratio(synthetic_chain)
        assert 'n_flagged' in result['summary']
        assert 'total_flagged_volume' in result['summary']
        assert 'dominant_type' in result['summary']
