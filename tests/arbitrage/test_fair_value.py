"""Tests for the fair value estimation module."""

import pytest

from src.arbitrage.fair_value import (
    aggregate_fair_probs,
    compute_edge,
    fair_value_from_two_providers,
    remove_overround,
)
from src.models.events import BookmakerOdds


# ---------------------------------------------------------------------------
# remove_overround
# ---------------------------------------------------------------------------


class TestRemoveOverround:
    def test_fair_odds_unchanged(self):
        """Odds that already sum to 100% implied should return those probs."""
        # 1/2.0 + 1/2.0 = 1.0 → already fair.
        result = remove_overround((2.0, 2.0))
        assert result == pytest.approx((0.5, 0.5))

    def test_3way_with_overround(self):
        """Standard 3-way football odds with ~5% overround."""
        odds = (2.10, 3.40, 3.60)
        result = remove_overround(odds)
        assert sum(result) == pytest.approx(1.0)
        # Home should have highest prob.
        assert result[0] > result[1] > result[2]

    def test_2way_with_overround(self):
        """2-way moneyline with overround."""
        odds = (1.80, 2.10)
        result = remove_overround(odds)
        assert sum(result) == pytest.approx(1.0)
        assert result[0] > result[1]

    def test_high_overround(self):
        """Even with 20%+ overround, probs should normalize to 1."""
        odds = (1.50, 3.00, 4.00)
        result = remove_overround(odds)
        assert sum(result) == pytest.approx(1.0)

    def test_preserves_relative_order(self):
        """Lower odds → higher probability."""
        odds = (1.50, 4.00, 6.00)
        result = remove_overround(odds)
        assert result[0] > result[1] > result[2]


# ---------------------------------------------------------------------------
# aggregate_fair_probs
# ---------------------------------------------------------------------------


class TestAggregateFairProbs:
    def test_single_bookmaker(self):
        """With one bookmaker, should just remove overround from their odds."""
        odds_by_outcome = {
            "1": [BookmakerOdds("bk1", 2.10)],
            "X": [BookmakerOdds("bk1", 3.40)],
            "2": [BookmakerOdds("bk1", 3.60)],
        }
        fv = aggregate_fair_probs(odds_by_outcome)
        assert fv.outcome_names == ("1", "X", "2")
        assert sum(fv.fair_probs) == pytest.approx(1.0)
        assert fv.fair_probs[0] > fv.fair_probs[1] > fv.fair_probs[2]

    def test_multiple_bookmakers_median(self):
        """Median should be robust to one outlier bookmaker."""
        odds_by_outcome = {
            "1": [
                BookmakerOdds("bk1", 2.10),
                BookmakerOdds("bk2", 2.15),
                BookmakerOdds("bk3", 1.50),  # outlier
            ],
            "X": [
                BookmakerOdds("bk1", 3.40),
                BookmakerOdds("bk2", 3.35),
                BookmakerOdds("bk3", 3.50),
            ],
            "2": [
                BookmakerOdds("bk1", 3.60),
                BookmakerOdds("bk2", 3.55),
                BookmakerOdds("bk3", 5.00),  # outlier
            ],
        }
        fv = aggregate_fair_probs(odds_by_outcome)
        assert sum(fv.fair_probs) == pytest.approx(1.0)
        # Median should track bk1/bk2 rather than the outlier bk3.
        assert fv.fair_probs[0] == pytest.approx(0.457, abs=0.05)

    def test_two_bookmakers(self):
        """With two bookmakers, median == average of the two."""
        odds_by_outcome = {
            "1": [BookmakerOdds("bk1", 2.00), BookmakerOdds("bk2", 2.20)],
            "2": [BookmakerOdds("bk1", 2.00), BookmakerOdds("bk2", 1.85)],
        }
        fv = aggregate_fair_probs(odds_by_outcome)
        assert sum(fv.fair_probs) == pytest.approx(1.0)

    def test_sharp_odds_are_inverse_of_fair_probs(self):
        """sharp_odds[i] should equal round(1/fair_probs[i], 2)."""
        odds_by_outcome = {
            "1": [BookmakerOdds("bk1", 2.10)],
            "X": [BookmakerOdds("bk1", 3.40)],
            "2": [BookmakerOdds("bk1", 3.60)],
        }
        fv = aggregate_fair_probs(odds_by_outcome)
        for prob, sharp in zip(fv.fair_probs, fv.sharp_odds):
            assert sharp == pytest.approx(round(1.0 / prob, 2))


# ---------------------------------------------------------------------------
# fair_value_from_two_providers
# ---------------------------------------------------------------------------


class TestFairValueFromTwoProviders:
    def test_identical_odds(self):
        """If both providers have the same odds, fair probs = overround-removed."""
        sp = (2.10, 3.40, 3.60)
        pm = (2.10, 3.40, 3.60)
        fv = fair_value_from_two_providers(sp, pm)
        assert sum(fv.fair_probs) == pytest.approx(1.0)
        expected = remove_overround(sp)
        assert fv.fair_probs == pytest.approx(expected)

    def test_different_odds_averages(self):
        """Fair probs should be average of the two overround-removed prob sets."""
        sp = (2.10, 3.40, 3.60)
        pm = (2.30, 3.20, 3.40)
        fv = fair_value_from_two_providers(sp, pm)
        assert sum(fv.fair_probs) == pytest.approx(1.0)

    def test_2way_market(self):
        """Should work with 2-way markets."""
        sp = (1.80, 2.10)
        pm = (1.75, 2.20)
        fv = fair_value_from_two_providers(sp, pm, outcome_names=("1", "2"))
        assert sum(fv.fair_probs) == pytest.approx(1.0)
        assert len(fv.fair_probs) == 2

    def test_default_3way_outcome_names(self):
        """3-way market should default to ('1', 'X', '2')."""
        fv = fair_value_from_two_providers((2.0, 3.0, 4.0), (2.1, 3.1, 3.9))
        assert fv.outcome_names == ("1", "X", "2")


# ---------------------------------------------------------------------------
# compute_edge
# ---------------------------------------------------------------------------


class TestComputeEdge:
    def test_no_edge_at_fair_odds(self):
        """If offered odds match fair value, edge should be ~0."""
        fv = fair_value_from_two_providers((2.0, 2.0), (2.0, 2.0))
        edges = compute_edge(fv, (2.0, 2.0))
        assert edges[0] == pytest.approx(0.0)
        assert edges[1] == pytest.approx(0.0)

    def test_positive_edge(self):
        """Offered odds above fair value should yield positive edge."""
        from src.models.events import FairValue

        fv = FairValue(
            outcome_names=("1", "2"),
            fair_probs=(0.5, 0.5),
            sharp_odds=(2.0, 2.0),
        )
        edges = compute_edge(fv, (2.20, 1.90))
        # 2.20 * 0.5 - 1 = 0.10 (10% edge)
        assert edges[0] == pytest.approx(0.10)
        # 1.90 * 0.5 - 1 = -0.05 (negative edge)
        assert edges[1] == pytest.approx(-0.05)

    def test_3way_edge(self):
        """Edge computation should work for 3-way markets."""
        from src.models.events import FairValue

        fv = FairValue(
            outcome_names=("1", "X", "2"),
            fair_probs=(0.5, 0.25, 0.25),
            sharp_odds=(2.0, 4.0, 4.0),
        )
        # Offer 2.20 on home: 2.20 * 0.5 - 1 = 0.10
        edges = compute_edge(fv, (2.20, 4.0, 4.0))
        assert edges[0] == pytest.approx(0.10)
        assert edges[1] == pytest.approx(0.0)
        assert edges[2] == pytest.approx(0.0)
