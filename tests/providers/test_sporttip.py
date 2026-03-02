"""Tests for the Sporttip provider's parsing logic."""


from src.models.events import Sport
from src.providers.sporttip import (
    SporttipProvider,
    _extract_event_list,
    _is_odds_api_response,
    _parse_markets,
    _parse_single_event,
)


class TestIsOddsApiResponse:
    def test_matches_sport_urls(self) -> None:
        assert _is_odds_api_response("https://api.example.com/sports/football/events")
        assert _is_odds_api_response("https://cdn.example.com/feed/matches")
        assert _is_odds_api_response("https://example.com/v1/odds?sport=1")

    def test_rejects_unrelated_urls(self) -> None:
        assert not _is_odds_api_response("https://fonts.googleapis.com/css")
        assert not _is_odds_api_response("https://www.google-analytics.com/collect")
        assert not _is_odds_api_response("https://cdn.example.com/styles.css")


class TestExtractEventList:
    def test_extracts_from_list(self) -> None:
        data = [{"id": 1}, {"id": 2}]
        assert _extract_event_list(data) == data

    def test_extracts_from_events_key(self) -> None:
        data = {"events": [{"id": 1}]}
        assert _extract_event_list(data) == [{"id": 1}]

    def test_extracts_from_matches_key(self) -> None:
        data = {"matches": [{"id": 1}]}
        assert _extract_event_list(data) == [{"id": 1}]

    def test_extracts_nested(self) -> None:
        data = {"data": {"events": [{"id": 1}]}}
        assert _extract_event_list(data) == [{"id": 1}]

    def test_returns_empty_for_unknown_structure(self) -> None:
        assert _extract_event_list({"foo": "bar"}) == []
        assert _extract_event_list("string") == []


class TestParseMarkets:
    def test_parses_standard_market(self) -> None:
        raw = {
            "markets": [
                {
                    "name": "1X2",
                    "outcomes": [
                        {"name": "Home", "odds": 2.10},
                        {"name": "Draw", "odds": 3.40},
                        {"name": "Away", "odds": 3.20},
                    ],
                }
            ]
        }
        markets = _parse_markets(raw)
        assert len(markets) == 1
        assert markets[0].name == "1X2"
        assert len(markets[0].outcomes) == 3
        assert markets[0].outcomes[0].odds == 2.10

    def test_parses_alternative_keys(self) -> None:
        raw = {
            "odds": [
                {
                    "type": "match_result",
                    "selections": [
                        {"label": "1", "price": 1.85},
                        {"label": "X", "price": 3.60},
                        {"label": "2", "price": 4.00},
                    ],
                }
            ]
        }
        markets = _parse_markets(raw)
        assert len(markets) == 1
        assert markets[0].name == "match_result"
        assert markets[0].outcomes[1].odds == 3.60

    def test_skips_invalid_outcomes(self) -> None:
        raw = {
            "markets": [
                {
                    "name": "1X2",
                    "outcomes": [
                        {"name": "Home", "odds": "not_a_number"},
                        {"name": "Away", "odds": 2.50},
                    ],
                }
            ]
        }
        markets = _parse_markets(raw)
        assert len(markets[0].outcomes) == 1

    def test_returns_empty_for_no_markets(self) -> None:
        assert _parse_markets({}) == []


class TestParseSingleEvent:
    def test_parses_complete_event(self) -> None:
        raw = {
            "id": "12345",
            "homeTeam": {"name": "FC Basel"},
            "awayTeam": {"name": "FC Zurich"},
            "startTime": "2025-03-15T18:00:00+01:00",
            "league": {"name": "Super League"},
            "markets": [
                {
                    "name": "1X2",
                    "outcomes": [
                        {"name": "1", "odds": 2.10},
                        {"name": "X", "odds": 3.40},
                        {"name": "2", "odds": 3.20},
                    ],
                }
            ],
        }
        event = _parse_single_event(raw, Sport.FOOTBALL)
        assert event is not None
        assert event.id == "12345"
        assert event.home_team == "FC Basel"
        assert event.away_team == "FC Zurich"
        assert event.league == "Super League"
        assert event.provider == "sporttip"
        assert len(event.markets) == 1

    def test_handles_flat_team_names(self) -> None:
        raw = {
            "eventId": "99",
            "home": "Team A",
            "away": "Team B",
            "startDate": "2025-04-01T20:00:00Z",
            "competition": "Cup",
        }
        event = _parse_single_event(raw, Sport.FOOTBALL)
        assert event is not None
        assert event.home_team == "Team A"
        assert event.away_team == "Team B"

    def test_returns_none_without_id(self) -> None:
        raw = {"homeTeam": {"name": "A"}, "awayTeam": {"name": "B"}}
        assert _parse_single_event(raw, Sport.FOOTBALL) is None


class TestSporttipProvider:
    def test_provider_name(self) -> None:
        provider = SporttipProvider()
        assert provider.name == "Sporttip"
