"""Tests for The Odds API provider."""

import pytest

from src.models.events import Sport
from src.providers.theodds import TheOddsProvider, _parse_event


def _make_raw_event(
    home: str = "Arsenal",
    away: str = "Everton",
    sport_key: str = "soccer_epl",
    n_bookmakers: int = 3,
    has_draw: bool = True,
) -> dict:
    """Build a minimal The Odds API event fixture."""
    outcomes_base = [
        {"name": home, "price": 1.65},
        {"name": away, "price": 5.50},
    ]
    if has_draw:
        outcomes_base.insert(1, {"name": "Draw", "price": 3.80})

    bookmakers = []
    for i in range(n_bookmakers):
        # Vary odds slightly per bookmaker.
        factor = 1.0 + (i - 1) * 0.02
        outcomes = []
        for oc in outcomes_base:
            outcomes.append({"name": oc["name"], "price": round(oc["price"] * factor, 2)})
        bookmakers.append({
            "key": f"bookmaker_{i}",
            "title": f"Bookmaker {i}",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": outcomes,
                }
            ],
        })

    return {
        "id": "event-123",
        "sport_key": sport_key,
        "commence_time": "2026-03-15T15:00:00Z",
        "home_team": home,
        "away_team": away,
        "bookmakers": bookmakers,
    }


class TestParseEvent:
    def test_3way_football(self):
        """Should parse a 3-way football event correctly."""
        raw = _make_raw_event()
        market = _parse_event(raw, Sport.FOOTBALL, "Premier League")
        assert market is not None
        assert market.home_team == "Arsenal"
        assert market.away_team == "Everton"
        assert market.league == "Premier League"
        assert market.outcome_names == ("1", "X", "2")
        assert len(market.odds_by_outcome["1"]) == 3
        assert len(market.odds_by_outcome["X"]) == 3
        assert len(market.odds_by_outcome["2"]) == 3

    def test_2way_basketball(self):
        """Should parse a 2-way basketball event."""
        raw = _make_raw_event(
            home="Lakers", away="Celtics", sport_key="basketball_nba", has_draw=False,
        )
        market = _parse_event(raw, Sport.BASKETBALL, "NBA")
        assert market is not None
        assert market.outcome_names == ("1", "2")
        assert "X" not in market.odds_by_outcome

    def test_no_bookmakers_returns_none(self):
        """Event with no bookmakers should return None."""
        raw = _make_raw_event()
        raw["bookmakers"] = []
        assert _parse_event(raw, Sport.FOOTBALL, "Premier League") is None

    def test_missing_team_returns_none(self):
        """Event with empty team name should return None."""
        raw = _make_raw_event()
        raw["home_team"] = ""
        assert _parse_event(raw, Sport.FOOTBALL, "Premier League") is None

    def test_bookmaker_odds_are_correct(self):
        """Parsed odds should match the input fixture."""
        raw = _make_raw_event(n_bookmakers=1)
        market = _parse_event(raw, Sport.FOOTBALL, "Premier League")
        assert market is not None
        home_odds = market.odds_by_outcome["1"]
        assert len(home_odds) == 1
        assert home_odds[0].bookmaker == "Bookmaker 0"
        # n_bookmakers=1 → factor = 1.0 + (0-1)*0.02 = 0.98
        assert home_odds[0].odds == pytest.approx(1.65 * 0.98, abs=0.01)


class TestFetchMultiBookOdds:
    async def test_no_api_key_returns_empty(self):
        """Without API key, should return empty list."""
        provider = TheOddsProvider(api_key="")
        result = await provider.fetch_multi_book_odds(Sport.FOOTBALL)
        assert result == []

    async def test_unsupported_sport_returns_empty(self):
        """Handball has no sport keys, should return empty."""
        provider = TheOddsProvider(api_key="test-key")
        result = await provider.fetch_multi_book_odds(Sport.HANDBALL)
        assert result == []
