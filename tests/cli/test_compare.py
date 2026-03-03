"""Tests for the cross-provider comparison CLI."""

from datetime import datetime, timezone

from src.cli.compare import ComparedMatch, compute_diffs, format_comparison_table
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


class TestComputeDiffs:
    def test_correct_percentages(self) -> None:
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
        # Max diff: |5.00 - 6.90| / 5.00 * 100 = 38.0%
        assert c.max_diff_pct == 38.0

    def test_sorted_by_diff_descending(self) -> None:
        sp1 = _make_event("A", "B", (2.0, 3.0, 4.0), provider="sporttip")
        pm1 = _make_event("A", "B", (2.0, 3.0, 4.0), provider="polymarket")
        sp2 = _make_event("C", "D", (1.50, 3.80, 5.00), provider="sporttip")
        pm2 = _make_event("C", "D", (1.33, 4.26, 6.90), provider="polymarket")
        matched = [
            MatchedEvent(sporttip=sp1, polymarket=pm1, similarity=1.0),
            MatchedEvent(sporttip=sp2, polymarket=pm2, similarity=0.9),
        ]
        result = compute_diffs(matched)
        assert len(result) == 2
        assert result[0].max_diff_pct >= result[1].max_diff_pct

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
                max_diff_pct=38.0,
            ),
        ]
        table = format_comparison_table(compared, 10, 8)
        assert "Arsenal vs Everton" in table
        assert "Premier League" in table
        assert "Sat 15:30" in table
        assert "1.50" in table
        assert "38.0%" in table
        assert "1 matched" in table
        assert "10 Sporttip" in table
        assert "8 Polymarket" in table

    def test_sorted_descending(self) -> None:
        c1 = ComparedMatch(
            home="A", away="B", league="L", kickoff="Sat 10:00",
            sp_odds=(2.0, 3.0, 4.0), pm_odds=(2.0, 3.0, 4.0), max_diff_pct=0.0,
        )
        c2 = ComparedMatch(
            home="C", away="D", league="L", kickoff="Sat 11:00",
            sp_odds=(1.5, 3.8, 5.0), pm_odds=(1.3, 4.3, 6.9), max_diff_pct=38.0,
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
