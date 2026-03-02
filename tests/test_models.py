"""Tests for shared data models."""

from datetime import datetime, timezone

from src.models.events import Event, Market, Outcome, Sport


class TestEvent:
    def test_create_event(self) -> None:
        event = Event(
            id="1",
            sport=Sport.FOOTBALL,
            league="Super League",
            home_team="FC Basel",
            away_team="FC Zurich",
            start_time=datetime(2025, 3, 15, 18, 0, tzinfo=timezone.utc),
            markets=[
                Market(
                    name="1X2",
                    outcomes=[
                        Outcome(name="1", odds=2.10),
                        Outcome(name="X", odds=3.40),
                        Outcome(name="2", odds=3.20),
                    ],
                )
            ],
            provider="sporttip",
        )
        assert event.home_team == "FC Basel"
        assert len(event.markets) == 1
        assert event.markets[0].outcomes[0].odds == 2.10

    def test_sport_enum(self) -> None:
        assert Sport.FOOTBALL.value == "football"
        assert Sport.ICE_HOCKEY.value == "ice_hockey"
