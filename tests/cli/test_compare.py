"""Tests for the cross-provider comparison CLI."""

from datetime import datetime, timezone

from src.cli.compare import (
    ComparedMatch,
    compute_arb_margin,
    compute_diffs,
    format_comparison_table,
)
from src.matching import MatchedEvent
from src.models.events import Event, Market, Outcome, Sport


def _make_event(
    home: str,
    away: str,
    odds: tuple[float, float, float],
    provider: str = "sporttip",
    league: str = "Premier League",
) -> Event:
    return Event(
        id=f"{home}-{away}",
        sport=Sport.FOOTBALL,
        league=league,
        home_team=home,
        away_team=away,
        start_time=datetime(2026, 3, 15, 15, 30, tzinfo=timezone.utc),
        markets=[
            Market(
                name="1X2",
                outcomes=[
                    Outcome(name="1", odds=odds[0]),
                    Outcome(name="X", odds=odds[1]),
                    Outcome(name="2", odds=odds[2]),
                ],
            )
        ],
        provider=provider,
    )


class TestComputeArbMargin:
    """Unit tests for the pure arbitrage margin calculation."""

    def test_identical_odds_negative_margin(self) -> None:
        """When both providers offer the same odds, margin equals the bookmaker vig (negative)."""
        best, margin = compute_arb_margin((2.0, 3.0, 4.0), (2.0, 3.0, 4.0))
        assert best == (2.0, 3.0, 4.0)
        # implied = 1/2 + 1/3 + 1/4 = 13/12 ≈ 1.0833 → margin ≈ -7.69%
        assert margin < 0

    def test_complementary_odds_positive_margin(self) -> None:
        """Providers offering complementary high odds create an arb opportunity."""
        # SP favours home, PM favours away → best of both exceeds fair value.
        sp = (1.50, 5.00, 6.00)
        pm = (3.50, 4.00, 2.00)
        best, margin = compute_arb_margin(sp, pm)
        assert best == (3.50, 5.00, 6.00)
        # implied = 1/3.5 + 1/5.0 + 1/6.0 ≈ 0.2857 + 0.2 + 0.1667 = 0.6524
        # margin = (1/0.6524 - 1) * 100 ≈ 53.27%
        assert margin > 50

    def test_picks_best_per_outcome(self) -> None:
        """Best odds tuple should take the max from each provider per outcome."""
        sp = (1.80, 3.50, 4.20)
        pm = (2.10, 3.30, 5.00)
        best, _ = compute_arb_margin(sp, pm)
        assert best == (2.10, 3.50, 5.00)

    def test_margin_precision(self) -> None:
        """Margin is rounded to two decimal places."""
        _, margin = compute_arb_margin((2.0, 3.0, 4.0), (2.0, 3.0, 4.0))
        assert margin == round(margin, 2)

    def test_fair_odds_zero_margin(self) -> None:
        """When best odds sum to exactly 100% implied probability, margin is 0."""
        # Construct odds whose implied probs sum to 1.0: 1/2 + 1/4 + 1/4 = 1.
        sp = (2.0, 4.0, 4.0)
        pm = (2.0, 4.0, 4.0)
        _, margin = compute_arb_margin(sp, pm)
        assert margin == 0.0


class TestComputeDiffs:
    def test_computes_arb_margin(self) -> None:
        sp = _make_event("Arsenal", "Everton", (1.50, 3.80, 5.00), provider="sporttip")
        pm = _make_event("Arsenal FC", "Everton FC", (1.33, 4.26, 6.90), provider="polymarket")
        matched = [MatchedEvent(sporttip=sp, polymarket=pm, similarity=0.9)]
        result = compute_diffs(matched)
        assert len(result) == 1
        c = result[0]
        assert c.home == "Arsenal"
        assert c.away == "Everton"
        assert c.sp_odds == (1.50, 3.80, 5.00)
        assert c.pm_odds == (1.33, 4.26, 6.90)
        # Best: (1.50, 4.26, 6.90) → implied = 1/1.5 + 1/4.26 + 1/6.9
        assert c.best_odds == (1.50, 4.26, 6.90)
        assert c.arb_margin < 0  # still negative (no arb here)

    def test_sorted_by_margin_descending(self) -> None:
        sp1 = _make_event("A", "B", (2.0, 3.0, 4.0), provider="sporttip")
        pm1 = _make_event("A", "B", (2.0, 3.0, 4.0), provider="polymarket")
        # Second pair: SP favours home, PM favours away → high arb margin.
        sp2 = _make_event("C", "D", (1.50, 5.00, 6.00), provider="sporttip")
        pm2 = _make_event("C", "D", (3.50, 4.00, 2.00), provider="polymarket")
        matched = [
            MatchedEvent(sporttip=sp1, polymarket=pm1, similarity=1.0),
            MatchedEvent(sporttip=sp2, polymarket=pm2, similarity=0.9),
        ]
        result = compute_diffs(matched)
        assert len(result) == 2
        assert result[0].arb_margin >= result[1].arb_margin

    def test_empty_matched(self) -> None:
        assert compute_diffs([]) == []


class TestFormatComparisonTable:
    def test_contains_all_fields(self) -> None:
        compared = [
            ComparedMatch(
                home="Arsenal",
                away="Everton",
                league="Premier League",
                kickoff="Sat 15:30",
                sp_odds=(1.50, 3.80, 5.00),
                pm_odds=(1.33, 4.26, 6.90),
                best_odds=(1.50, 4.26, 6.90),
                arb_margin=-2.50,
                pm_volume=0.0,
            ),
        ]
        table = format_comparison_table(compared, 10, 8)
        assert "Arsenal vs Everton" in table
        assert "Premier League" in table
        assert "Sat 15:30" in table
        assert "1.50" in table
        assert "-2.50%" in table
        assert "Arb%" in table
        assert "Best1" in table
        assert "1 matched" in table
        assert "10 Sporttip" in table
        assert "8 Polymarket" in table

    def test_sorted_descending(self) -> None:
        c1 = ComparedMatch(
            home="A", away="B", league="L", kickoff="Sat 10:00",
            sp_odds=(2.0, 3.0, 4.0), pm_odds=(2.0, 3.0, 4.0),
            best_odds=(2.0, 3.0, 4.0), arb_margin=-7.69, pm_volume=0.0,
        )
        c2 = ComparedMatch(
            home="C", away="D", league="L", kickoff="Sat 11:00",
            sp_odds=(1.5, 5.0, 6.0), pm_odds=(3.5, 4.0, 2.0),
            best_odds=(3.5, 5.0, 6.0), arb_margin=53.27, pm_volume=0.0,
        )
        # Pass already sorted (by compute_diffs), verify order in output.
        table = format_comparison_table([c2, c1], 5, 5)
        pos_c = table.index("C vs D")
        pos_a = table.index("A vs B")
        assert pos_c < pos_a

    def test_empty_shows_message(self) -> None:
        table = format_comparison_table([], 5, 3)
        assert "No matched events found" in table
        assert "5 unmatched Sporttip" in table
        assert "3 unmatched Polymarket" in table
