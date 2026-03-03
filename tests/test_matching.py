"""Tests for the fuzzy event matching module."""

from datetime import datetime, timezone

from src.matching import _team_similarity, match_events, normalize_team
from src.models.events import Event, Market, Outcome, Sport


def _make_event(
    home: str,
    away: str,
    dt: datetime,
    provider: str = "sporttip",
    league: str = "Premier League",
) -> Event:
    return Event(
        id=f"{home}-{away}",
        sport=Sport.FOOTBALL,
        league=league,
        home_team=home,
        away_team=away,
        start_time=dt,
        markets=[
            Market(
                name="1X2",
                outcomes=[
                    Outcome(name="1", odds=2.0),
                    Outcome(name="X", odds=3.0),
                    Outcome(name="2", odds=4.0),
                ],
            )
        ],
        provider=provider,
    )


class TestNormalizeTeam:
    def test_strips_fc(self) -> None:
        assert normalize_team("Arsenal FC") == "arsenal"

    def test_strips_afc(self) -> None:
        assert normalize_team("AFC Bournemouth") == "bournemouth"

    def test_koln(self) -> None:
        assert normalize_team("1. FC Köln") == "1. köln"

    def test_collapses_spaces(self) -> None:
        assert normalize_team("  Real   Madrid   ") == "real madrid"

    def test_lowercase(self) -> None:
        assert normalize_team("BAYERN MÜNCHEN") == "bayern münchen"


class TestTeamSimilarity:
    def test_identical(self) -> None:
        assert _team_similarity("Arsenal", "Arsenal") == 1.0

    def test_totally_different(self) -> None:
        assert _team_similarity("Arsenal", "Xyzzy Krakow") < 0.3

    def test_with_suffix(self) -> None:
        assert _team_similarity("Arsenal", "Arsenal FC") > 0.9


DT = datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)


class TestMatchEvents:
    def test_matches_similar_names_same_date(self) -> None:
        sp = [_make_event("Arsenal", "Everton", DT, provider="sporttip")]
        pm = [_make_event("Arsenal FC", "Everton FC", DT, provider="polymarket")]
        result = match_events(sp, pm)
        assert len(result) == 1
        assert result[0].sporttip.home_team == "Arsenal"
        assert result[0].polymarket.home_team == "Arsenal FC"
        assert result[0].similarity > 0.6

    def test_rejects_different_dates(self) -> None:
        dt2 = datetime(2026, 3, 20, 15, 0, tzinfo=timezone.utc)
        sp = [_make_event("Arsenal", "Everton", DT, provider="sporttip")]
        pm = [_make_event("Arsenal FC", "Everton FC", dt2, provider="polymarket")]
        result = match_events(sp, pm)
        assert result == []

    def test_no_duplicates(self) -> None:
        sp = [
            _make_event("Arsenal", "Everton", DT, provider="sporttip"),
            _make_event("Arsenal", "Chelsea", DT, provider="sporttip"),
        ]
        pm = [_make_event("Arsenal FC", "Everton FC", DT, provider="polymarket")]
        result = match_events(sp, pm)
        assert len(result) == 1
        # The best match should be Arsenal-Everton, not Arsenal-Chelsea.
        assert result[0].sporttip.away_team == "Everton"

    def test_empty_lists(self) -> None:
        assert match_events([], []) == []
        sp = [_make_event("Arsenal", "Everton", DT)]
        assert match_events(sp, []) == []
        assert match_events([], sp) == []
