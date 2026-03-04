"""Tests for the cross-provider comparison CLI."""

from datetime import datetime, timezone

import pytest

from src.cli.compare import (
    ComparedMatch,
    _find_match_odds,
    compute_arb_margin,
    compute_diffs,
    format_comparison_table,
)
from src.matching import MatchedEvent
from src.models.events import Event, Market, Outcome, Sport


def _make_event(
    home: str,
    away: str,
    odds: tuple[float, ...],
    provider: str = "sporttip",
    league: str = "Premier League",
    sport: Sport = Sport.FOOTBALL,
) -> Event:
    if len(odds) == 3:
        market = Market(
            name="1X2",
            outcomes=[
                Outcome(name="1", odds=odds[0]),
                Outcome(name="X", odds=odds[1]),
                Outcome(name="2", odds=odds[2]),
            ],
        )
    else:
        market = Market(
            name="Moneyline",
            outcomes=[
                Outcome(name="1", odds=odds[0]),
                Outcome(name="2", odds=odds[1]),
            ],
        )
    return Event(
        id=f"{home}-{away}",
        sport=sport,
        league=league,
        home_team=home,
        away_team=away,
        start_time=datetime(2026, 3, 15, 15, 30, tzinfo=timezone.utc),
        markets=[market],
        provider=provider,
    )


class TestFindMatchOdds:
    """Unit tests for _find_match_odds extraction."""

    def test_3way_1x2(self) -> None:
        ev = _make_event("A", "B", (1.50, 3.80, 5.00))
        result = _find_match_odds(ev)
        assert result is not None
        names, odds = result
        assert names == ("1", "X", "2")
        assert odds == (1.50, 3.80, 5.00)

    def test_2way_moneyline(self) -> None:
        ev = _make_event("Lakers", "Celtics", (1.80, 2.10), sport=Sport.BASKETBALL)
        result = _find_match_odds(ev)
        assert result is not None
        names, odds = result
        assert names == ("1", "2")
        assert odds == (1.80, 2.10)

    def test_no_match_odds_returns_none(self) -> None:
        ev = Event(
            id="test",
            sport=Sport.FOOTBALL,
            league="Test",
            home_team="A",
            away_team="B",
            start_time=datetime(2026, 3, 15, 15, 30, tzinfo=timezone.utc),
            markets=[
                Market(name="Over/Under", outcomes=[
                    Outcome(name="Over 2.5", odds=1.80),
                    Outcome(name="Under 2.5", odds=2.00),
                ]),
            ],
            provider="sporttip",
        )
        assert _find_match_odds(ev) is None

    def test_winner_market_name(self) -> None:
        """Markets named 'Winner' or 'Match Winner' should be recognized."""
        ev = Event(
            id="test",
            sport=Sport.TENNIS,
            league="ATP",
            home_team="Djokovic",
            away_team="Nadal",
            start_time=datetime(2026, 3, 15, 15, 30, tzinfo=timezone.utc),
            markets=[
                Market(name="Winner", outcomes=[
                    Outcome(name="1", odds=1.40),
                    Outcome(name="2", odds=3.00),
                ]),
            ],
            provider="sporttip",
        )
        result = _find_match_odds(ev)
        assert result is not None
        assert result[0] == ("1", "2")
        assert result[1] == (1.40, 3.00)

    def test_winner_incl_overtime_with_team_names(self) -> None:
        """Sporttip basketball uses 'Winner (incl. Overtime)' with team-name outcomes."""
        ev = Event(
            id="test",
            sport=Sport.BASKETBALL,
            league="NBA",
            home_team="New York Knicks",
            away_team="Oklahoma City Thunder",
            start_time=datetime(2026, 3, 15, 15, 30, tzinfo=timezone.utc),
            markets=[
                Market(name="Winner (incl. Overtime)", outcomes=[
                    Outcome(name="New York Knicks", odds=2.50),
                    Outcome(name="Oklahoma City Thunder", odds=1.52),
                ]),
            ],
            provider="sporttip",
        )
        result = _find_match_odds(ev)
        assert result is not None
        assert result[0] == ("1", "2")
        assert result[1] == (2.50, 1.52)

    def test_3way_team_names_with_draw(self) -> None:
        """NHL uses 'Final Result' with team-name outcomes and draw."""
        ev = Event(
            id="test",
            sport=Sport.ICE_HOCKEY,
            league="NHL",
            home_team="Detroit Red Wings",
            away_team="Vegas Golden Knights",
            start_time=datetime(2026, 3, 15, 15, 30, tzinfo=timezone.utc),
            markets=[
                Market(name="Final Result", outcomes=[
                    Outcome(name="Detroit Red Wings", odds=2.20),
                    Outcome(name="Draw", odds=4.05),
                    Outcome(name="Vegas Golden Knights", odds=2.65),
                ]),
            ],
            provider="sporttip",
        )
        result = _find_match_odds(ev)
        assert result is not None
        assert result[0] == ("1", "X", "2")
        assert result[1] == (2.20, 4.05, 2.65)


class TestComputeArbMargin:
    """Unit tests for the pure arbitrage margin calculation."""

    def test_identical_odds_negative_margin(self) -> None:
        """When both providers offer the same odds, margin equals the bookmaker vig (negative)."""
        best, providers, stakes, margin = compute_arb_margin((2.0, 3.0, 4.0), (2.0, 3.0, 4.0))
        assert best == (2.0, 3.0, 4.0)
        # implied = 1/2 + 1/3 + 1/4 = 13/12 ≈ 1.0833 → margin ≈ -7.69%
        assert margin < 0
        # With identical odds, all providers are "ST" (sp >= pm)
        assert providers == ("ST", "ST", "ST")
        # Stakes should sum to 100%
        assert sum(stakes) == pytest.approx(100.0, abs=0.5)

    def test_complementary_odds_positive_margin(self) -> None:
        """Providers offering complementary high odds create an arb opportunity."""
        # SP favours home, PM favours away → best of both exceeds fair value.
        sp = (1.50, 5.00, 6.00)
        pm = (3.50, 4.00, 2.00)
        best, providers, stakes, margin = compute_arb_margin(sp, pm)
        assert best == (3.50, 5.00, 6.00)
        # implied = 1/3.5 + 1/5.0 + 1/6.0 ≈ 0.2857 + 0.2 + 0.1667 = 0.6524
        # margin = (1/0.6524 - 1) * 100 ≈ 53.27%
        assert margin > 50
        # PM has best home, SP has best draw+away
        assert providers == ("PM", "ST", "ST")
        assert sum(stakes) == pytest.approx(100.0, abs=0.5)

    def test_picks_best_per_outcome(self) -> None:
        """Best odds tuple should take the max from each provider per outcome."""
        sp = (1.80, 3.50, 4.20)
        pm = (2.10, 3.30, 5.00)
        best, providers, _, _ = compute_arb_margin(sp, pm)
        assert best == (2.10, 3.50, 5.00)
        assert providers == ("PM", "ST", "PM")

    def test_margin_precision(self) -> None:
        """Margin is rounded to two decimal places."""
        _, _, _, margin = compute_arb_margin((2.0, 3.0, 4.0), (2.0, 3.0, 4.0))
        assert margin == round(margin, 2)

    def test_fair_odds_zero_margin(self) -> None:
        """When best odds sum to exactly 100% implied probability, margin is 0."""
        # Construct odds whose implied probs sum to 1.0: 1/2 + 1/4 + 1/4 = 1.
        sp = (2.0, 4.0, 4.0)
        pm = (2.0, 4.0, 4.0)
        _, _, _, margin = compute_arb_margin(sp, pm)
        assert margin == 0.0

    def test_stakes_favor_lowest_odds(self) -> None:
        """The outcome with the lowest best odds should get the largest stake."""
        sp = (1.50, 3.80, 5.00)
        pm = (1.33, 4.26, 6.90)
        _, _, stakes, _ = compute_arb_margin(sp, pm)
        # Outcome 1 has lowest odds (1.50), should get largest stake
        assert stakes[0] > stakes[1]
        assert stakes[0] > stakes[2]

    def test_2way_identical_odds(self) -> None:
        """2-way market with identical odds should produce negative margin."""
        best, providers, stakes, margin = compute_arb_margin((1.80, 2.10), (1.80, 2.10))
        assert best == (1.80, 2.10)
        assert providers == ("ST", "ST")
        assert len(stakes) == 2
        assert sum(stakes) == pytest.approx(100.0, abs=0.5)
        assert margin < 0

    def test_2way_arb_opportunity(self) -> None:
        """2-way market where each provider favours a different side."""
        sp = (2.20, 1.60)
        pm = (1.70, 2.30)
        best, providers, stakes, margin = compute_arb_margin(sp, pm)
        assert best == (2.20, 2.30)
        assert providers == ("ST", "PM")
        # implied = 1/2.2 + 1/2.3 ≈ 0.4545 + 0.4348 = 0.8893
        # margin = (1/0.8893 - 1) * 100 ≈ 12.45%
        assert margin > 10
        assert sum(stakes) == pytest.approx(100.0, abs=0.5)


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
        assert c.sport == Sport.FOOTBALL
        assert c.outcome_names == ("1", "X", "2")
        assert c.sp_odds == (1.50, 3.80, 5.00)
        assert c.pm_odds == (1.33, 4.26, 6.90)
        # Best: (1.50, 4.26, 6.90) → implied = 1/1.5 + 1/4.26 + 1/6.9
        assert c.best_odds == (1.50, 4.26, 6.90)
        assert c.best_providers == ("ST", "PM", "PM")
        assert sum(c.stakes) == pytest.approx(100.0, abs=0.5)
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

    def test_skips_mismatched_outcome_count(self) -> None:
        """Should skip pairs where one is 3-way and the other is 2-way."""
        sp = _make_event("A", "B", (1.50, 3.80, 5.00), provider="sporttip")
        pm = _make_event("A", "B", (1.80, 2.10), provider="polymarket", sport=Sport.BASKETBALL)
        matched = [MatchedEvent(sporttip=sp, polymarket=pm, similarity=0.9)]
        result = compute_diffs(matched)
        assert len(result) == 0

    def test_2way_diffs(self) -> None:
        """Should compute diffs for 2-way matches."""
        sp = _make_event("Lakers", "Celtics", (1.80, 2.10), provider="sporttip", sport=Sport.BASKETBALL)
        pm = _make_event("Lakers", "Celtics", (1.70, 2.30), provider="polymarket", sport=Sport.BASKETBALL)
        matched = [MatchedEvent(sporttip=sp, polymarket=pm, similarity=1.0)]
        result = compute_diffs(matched)
        assert len(result) == 1
        c = result[0]
        assert c.outcome_names == ("1", "2")
        assert c.best_odds == (1.80, 2.30)


class TestFormatComparisonTable:
    def test_contains_all_fields(self) -> None:
        compared = [
            ComparedMatch(
                home="Arsenal",
                away="Everton",
                league="Premier League",
                kickoff="Sat 15:30",
                sport=Sport.FOOTBALL,
                outcome_names=("1", "X", "2"),
                sp_odds=(1.50, 3.80, 5.00),
                pm_odds=(1.33, 4.26, 6.90),
                best_odds=(1.50, 4.26, 6.90),
                best_providers=("ST", "PM", "PM"),
                stakes=(62.5, 22.0, 13.6),
                arb_margin=-2.50,
                sp_overround=5.0,
                pm_overround=3.2,
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
        # Stake % is embedded in odds cells (e.g. "1.50 63%"), no separate Bet columns
        assert "62%" in table  # stake for outcome 1 (best at ST)
        assert "Bet 1" not in table
        assert "ST OR" in table
        assert "PM OR" in table
        assert "5.0%" in table
        assert "3.2%" in table
        assert "1 matched" in table
        assert "10 Sporttip" in table
        assert "8 Polymarket" in table
        assert "Football" in table

    def test_sorted_descending(self) -> None:
        c1 = ComparedMatch(
            home="A", away="B", league="L", kickoff="Sat 10:00",
            sport=Sport.FOOTBALL, outcome_names=("1", "X", "2"),
            sp_odds=(2.0, 3.0, 4.0), pm_odds=(2.0, 3.0, 4.0),
            best_odds=(2.0, 3.0, 4.0), best_providers=("ST", "ST", "ST"),
            stakes=(46.2, 30.8, 23.1), arb_margin=-7.69,
            sp_overround=8.3, pm_overround=8.3, pm_volume=0.0,
        )
        c2 = ComparedMatch(
            home="C", away="D", league="L", kickoff="Sat 11:00",
            sport=Sport.FOOTBALL, outcome_names=("1", "X", "2"),
            sp_odds=(1.5, 5.0, 6.0), pm_odds=(3.5, 4.0, 2.0),
            best_odds=(3.5, 5.0, 6.0), best_providers=("PM", "ST", "ST"),
            stakes=(43.8, 30.6, 25.5), arb_margin=53.27,
            sp_overround=8.3, pm_overround=8.3, pm_volume=0.0,
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

    def test_2way_table_no_draw_columns(self) -> None:
        """2-way matches should not show X/draw columns."""
        compared = [
            ComparedMatch(
                home="Lakers", away="Celtics", league="NBA",
                kickoff="Sat 20:00",
                sport=Sport.BASKETBALL,
                outcome_names=("1", "2"),
                sp_odds=(1.80, 2.10),
                pm_odds=(1.70, 2.30),
                best_odds=(1.80, 2.30),
                best_providers=("ST", "PM"),
                stakes=(56.1, 43.9),
                arb_margin=1.23,
                sp_overround=3.2,
                pm_overround=2.8,
                pm_volume=50000.0,
            ),
        ]
        table = format_comparison_table(compared, 5, 5)
        assert "Lakers vs Celtics" in table
        assert "Basketball" in table
        # Stake % is embedded in odds cells, no separate Bet columns
        assert "56%" in table  # stake for outcome 1 (best at ST)
        assert "44%" in table  # stake for outcome 2 (best at PM)
        assert "Bet 1" not in table

    def test_multi_sport_sections(self) -> None:
        """Multiple sports should produce separate sections."""
        football = ComparedMatch(
            home="Arsenal", away="Everton", league="Premier League",
            kickoff="Sat 15:30",
            sport=Sport.FOOTBALL, outcome_names=("1", "X", "2"),
            sp_odds=(1.50, 3.80, 5.00), pm_odds=(1.33, 4.26, 6.90),
            best_odds=(1.50, 4.26, 6.90), best_providers=("ST", "PM", "PM"),
            stakes=(62.5, 22.0, 13.6), arb_margin=-2.50,
            sp_overround=5.0, pm_overround=3.2, pm_volume=0.0,
        )
        basketball = ComparedMatch(
            home="Lakers", away="Celtics", league="NBA",
            kickoff="Sat 20:00",
            sport=Sport.BASKETBALL, outcome_names=("1", "2"),
            sp_odds=(1.80, 2.10), pm_odds=(1.70, 2.30),
            best_odds=(1.80, 2.30), best_providers=("ST", "PM"),
            stakes=(56.1, 43.9), arb_margin=1.23,
            sp_overround=3.2, pm_overround=2.8, pm_volume=50000.0,
        )
        table = format_comparison_table([football, basketball], 10, 10)
        assert "[Football]" in table
        assert "[Basketball]" in table
        # Football section before basketball (order of appearance)
        assert table.index("[Football]") < table.index("[Basketball]")
