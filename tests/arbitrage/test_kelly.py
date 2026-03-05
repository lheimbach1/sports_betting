"""Tests for the Kelly Criterion module."""

import pytest

from src.arbitrage.kelly import (
    analyze_market,
    fractional_kelly,
    kelly_ev,
    kelly_fraction,
)
from src.models.events import FairValue


# ---------------------------------------------------------------------------
# kelly_fraction
# ---------------------------------------------------------------------------


class TestKellyFraction:
    def test_positive_edge(self):
        """Known example: odds 3.0, fair_prob 0.40 → f* = (2*0.4 - 0.6)/2 = 0.10."""
        f = kelly_fraction(3.0, 0.40)
        assert f == pytest.approx(0.10)

    def test_zero_edge(self):
        """When odds exactly match fair value, Kelly fraction should be 0."""
        # odds 2.0, fair_prob 0.5 → b=1, f* = (1*0.5 - 0.5)/1 = 0
        f = kelly_fraction(2.0, 0.5)
        assert f == pytest.approx(0.0)

    def test_negative_edge(self):
        """When fair_prob is too low, Kelly should return 0."""
        f = kelly_fraction(2.0, 0.4)
        assert f == 0.0

    def test_very_high_edge(self):
        """Even with extreme edge, Kelly should not exceed 1.0."""
        f = kelly_fraction(100.0, 0.99)
        assert f <= 1.0
        assert f > 0.0

    def test_odds_at_1(self):
        """Odds of 1.0 (b=0) should return 0."""
        f = kelly_fraction(1.0, 0.9)
        assert f == 0.0

    def test_long_shot_with_edge(self):
        """Long shot with fair value edge."""
        # odds 10.0, fair_prob 0.12 → b=9, f* = (9*0.12 - 0.88)/9 = 0.0222
        f = kelly_fraction(10.0, 0.12)
        assert f == pytest.approx((9 * 0.12 - 0.88) / 9)


# ---------------------------------------------------------------------------
# fractional_kelly
# ---------------------------------------------------------------------------


class TestFractionalKelly:
    def test_half_kelly(self):
        """Half-Kelly should halve the fraction."""
        assert fractional_kelly(0.10) == pytest.approx(0.05)

    def test_quarter_kelly(self):
        """Quarter-Kelly should quarter the fraction."""
        assert fractional_kelly(0.10, 0.25) == pytest.approx(0.025)

    def test_full_kelly(self):
        """Full-Kelly (multiplier=1.0) should return the same fraction."""
        assert fractional_kelly(0.10, 1.0) == pytest.approx(0.10)

    def test_zero_fraction(self):
        """Zero fraction stays zero regardless of multiplier."""
        assert fractional_kelly(0.0, 0.5) == 0.0


# ---------------------------------------------------------------------------
# kelly_ev
# ---------------------------------------------------------------------------


class TestKellyEv:
    def test_positive_ev(self):
        """EV should be positive when odds * prob > 1."""
        ev = kelly_ev(3.0, 0.40, 100.0)
        # 100 * (3.0 * 0.4 - 1.0) = 100 * 0.2 = 20.0
        assert ev == pytest.approx(20.0)

    def test_zero_ev(self):
        """EV should be 0 at fair odds."""
        ev = kelly_ev(2.0, 0.5, 100.0)
        assert ev == pytest.approx(0.0)

    def test_negative_ev(self):
        """EV should be negative when odds * prob < 1."""
        ev = kelly_ev(2.0, 0.4, 100.0)
        # 100 * (2.0 * 0.4 - 1.0) = 100 * -0.2 = -20.0
        assert ev == pytest.approx(-20.0)


# ---------------------------------------------------------------------------
# analyze_market
# ---------------------------------------------------------------------------


class TestAnalyzeMarket:
    def _make_fair_value(self) -> FairValue:
        return FairValue(
            outcome_names=("1", "X", "2"),
            fair_probs=(0.50, 0.25, 0.25),
            sharp_odds=(2.0, 4.0, 4.0),
        )

    def test_returns_only_positive_edge(self):
        """Should only return outcomes where ST odds beat fair value."""
        fv = self._make_fair_value()
        # ST odds: home 2.20 > fair 2.0 → edge; draw 3.80 < fair 4.0 → no edge;
        # away 4.20 > fair 4.0 → edge.
        recs = analyze_market(fv, (2.20, 3.80, 4.20), (2.00, 3.90, 4.50), 1000.0, 0.5)
        outcome_names = {r.outcome_name for r in recs}
        assert "1" in outcome_names
        assert "2" in outcome_names
        assert "X" not in outcome_names
        # All should use ST provider.
        for r in recs:
            assert r.provider == "ST"

    def test_defaults_to_sporttip_odds(self):
        """Default provider='ST' should always use Sporttip odds."""
        fv = self._make_fair_value()
        recs = analyze_market(fv, (2.20, 3.80, 3.80), (2.10, 3.90, 4.50), 1000.0, 0.5)
        for r in recs:
            assert r.provider == "ST"
        home_rec = next(r for r in recs if r.outcome_name == "1")
        assert home_rec.offered_odds == 2.20

    def test_provider_none_picks_best(self):
        """provider=None should pick the provider with higher odds per outcome."""
        fv = self._make_fair_value()
        recs = analyze_market(fv, (2.20, 3.80, 3.80), (2.10, 3.90, 4.50), 1000.0, 0.5, provider=None)
        home_rec = next(r for r in recs if r.outcome_name == "1")
        assert home_rec.provider == "ST"
        assert home_rec.offered_odds == 2.20
        away_rec = next(r for r in recs if r.outcome_name == "2")
        assert away_rec.provider == "PM"
        assert away_rec.offered_odds == 4.50

    def test_provider_pm_uses_polymarket(self):
        """provider='PM' should always use Polymarket odds."""
        fv = self._make_fair_value()
        recs = analyze_market(fv, (2.20, 3.80, 3.80), (2.10, 3.90, 4.50), 1000.0, 0.5, provider="PM")
        for r in recs:
            assert r.provider == "PM"

    def test_half_kelly_sizing(self):
        """Fractional Kelly should be half of full Kelly."""
        fv = self._make_fair_value()
        recs = analyze_market(fv, (2.20, 3.80, 3.80), (2.00, 3.90, 4.50), 1000.0, 0.5)
        for r in recs:
            assert r.fractional_kelly == pytest.approx(r.full_kelly_fraction * 0.5)

    def test_no_edge_returns_empty(self):
        """When no outcome has positive edge, return empty list."""
        fv = self._make_fair_value()
        # All odds below fair value.
        recs = analyze_market(fv, (1.90, 3.50, 3.50), (1.85, 3.60, 3.60), 1000.0, 0.5)
        assert recs == []

    def test_sorted_by_edge_descending(self):
        """Recommendations should be sorted by edge descending."""
        fv = self._make_fair_value()
        recs = analyze_market(fv, (2.20, 4.50, 3.80), (2.10, 3.90, 4.50), 1000.0, 0.5)
        if len(recs) >= 2:
            assert recs[0].edge >= recs[1].edge

    def test_2way_market(self):
        """Should work with 2-way markets."""
        fv = FairValue(
            outcome_names=("1", "2"),
            fair_probs=(0.55, 0.45),
            sharp_odds=(1.82, 2.22),
        )
        # Default provider="ST": 1.90*0.55=1.045 edge, 2.10*0.45=0.945 no edge
        recs = analyze_market(fv, (1.90, 2.10), (1.85, 2.30), 1000.0, 0.5)
        assert len(recs) == 1
        assert recs[0].provider == "ST"
        # With provider=None: picks best odds per outcome
        recs_best = analyze_market(fv, (1.90, 2.10), (1.85, 2.30), 1000.0, 0.5, provider=None)
        assert len(recs_best) == 2
