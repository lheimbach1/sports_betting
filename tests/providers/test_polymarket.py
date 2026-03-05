"""Tests for the Polymarket provider."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.models.events import Sport
from src.providers.polymarket import (
    PolymarketProvider,
    _detect_league,
    _event_volume,
    _market_prob,
    _parse_football_event,
    _parse_game_event,
    _parse_teams,
)


def _make_raw_event(
    title: str = "Everton FC vs. Burnley FC",
    home_prob: float = 0.615,
    draw_prob: float = 0.25,
    away_prob: float = 0.135,
    end_date: str = "2026-03-15T15:00:00Z",
    event_id: str = "evt-123",
    volume: float = 50000.0,
) -> dict:
    """Build a minimal Gamma API event fixture.

    Each market includes ``bestAsk`` (live orderbook price the parser
    prefers), ``lastTradePrice``, and ``outcomePrices`` (mid-market).
    """
    home_team = title.split(" vs. ")[0].strip()
    away_team = title.split(" vs. ")[1].strip() if " vs. " in title else "Unknown"
    return {
        "id": event_id,
        "title": title,
        "endDate": end_date,
        "volume": volume,
        "markets": [
            {
                "groupItemTitle": f"{home_team} Win",
                "outcomePrices": json.dumps([home_prob, 1 - home_prob]),
                "bestAsk": home_prob,
                "lastTradePrice": home_prob,
                "volume": volume * 0.4,
            },
            {
                "groupItemTitle": "Draw",
                "outcomePrices": json.dumps([draw_prob, 1 - draw_prob]),
                "bestAsk": draw_prob,
                "lastTradePrice": draw_prob,
                "volume": volume * 0.2,
            },
            {
                "groupItemTitle": f"{away_team} Win",
                "outcomePrices": json.dumps([away_prob, 1 - away_prob]),
                "bestAsk": away_prob,
                "lastTradePrice": away_prob,
                "volume": volume * 0.4,
            },
        ],
    }


class TestParseTeams:
    def test_standard_title(self) -> None:
        assert _parse_teams("Everton FC vs. Burnley FC") == ("Everton FC", "Burnley FC")

    def test_no_separator(self) -> None:
        assert _parse_teams("Some random event") is None

    def test_empty_parts(self) -> None:
        assert _parse_teams(" vs. ") is None


class TestParseEvent:
    def test_valid_event(self) -> None:
        raw = _make_raw_event()
        ev = _parse_football_event(raw, "Premier League")
        assert ev is not None
        assert ev.home_team == "Everton FC"
        assert ev.away_team == "Burnley FC"
        assert ev.league == "Premier League"
        assert ev.provider == "polymarket"
        assert ev.sport == Sport.FOOTBALL
        assert ev.start_time == datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)

        market = ev.markets[0]
        assert market.name == "1X2"
        assert len(market.outcomes) == 3
        # 1 / 0.615 ≈ 1.63
        assert market.outcomes[0].name == "1"
        assert market.outcomes[0].odds == round(1.0 / 0.615, 2)
        # Draw
        assert market.outcomes[1].name == "X"
        assert market.outcomes[1].odds == round(1.0 / 0.25, 2)
        # Away
        assert market.outcomes[2].name == "2"
        assert market.outcomes[2].odds == round(1.0 / 0.135, 2)

    def test_too_few_markets(self) -> None:
        raw = _make_raw_event()
        raw["markets"] = raw["markets"][:2]
        assert _parse_football_event(raw, "Premier League") is None

    def test_zero_probability(self) -> None:
        raw = _make_raw_event(home_prob=0.0)
        assert _parse_football_event(raw, "Premier League") is None

    def test_probability_to_odds_exact(self) -> None:
        assert round(1.0 / 0.615, 2) == 1.63
        assert round(1.0 / 0.5, 2) == 2.0
        assert round(1.0 / 0.1, 2) == 10.0

    def test_no_vs_in_title(self) -> None:
        raw = _make_raw_event()
        raw["title"] = "Premier League Winner"
        assert _parse_football_event(raw, "Premier League") is None


class TestParseGameEvent:
    def test_valid_nba_game(self) -> None:
        raw = {
            "id": "game-1",
            "title": "Knicks vs. Thunder",
            "endDate": "2026-03-10T00:00:00Z",
            "tags": [
                {"label": "Sports"},
                {"label": "NBA"},
                {"label": "Games"},
            ],
            "markets": [
                {
                    "outcomes": json.dumps(["Knicks", "Thunder"]),
                    "outcomePrices": json.dumps([0.45, 0.55]),
                },
            ],
        }
        ev = _parse_game_event(raw)
        assert ev is not None
        assert ev.sport == Sport.BASKETBALL
        assert ev.home_team == "Knicks"
        assert ev.away_team == "Thunder"
        assert ev.markets[0].name == "Moneyline"
        assert len(ev.markets[0].outcomes) == 2
        assert ev.markets[0].outcomes[0].odds == round(1.0 / 0.45, 2)
        assert ev.markets[0].outcomes[1].odds == round(1.0 / 0.55, 2)

    def test_uses_best_ask_and_bid_for_odds(self) -> None:
        raw = {
            "id": "game-1b",
            "title": "Knicks vs. Thunder",
            "endDate": "2026-03-10T00:00:00Z",
            "tags": [
                {"label": "Sports"},
                {"label": "NBA"},
                {"label": "Games"},
            ],
            "markets": [
                {
                    "outcomes": json.dumps(["Knicks", "Thunder"]),
                    "outcomePrices": json.dumps([0.45, 0.55]),
                    "bestAsk": 0.46,
                    "bestBid": 0.44,
                },
            ],
        }
        ev = _parse_game_event(raw)
        assert ev is not None
        # Home uses bestAsk (0.46), away uses 1 - bestBid (1 - 0.44 = 0.56).
        assert ev.markets[0].outcomes[0].odds == round(1.0 / 0.46, 2)
        assert ev.markets[0].outcomes[1].odds == round(1.0 / 0.56, 2)

    def test_no_sport_tag_returns_none(self) -> None:
        raw = {
            "id": "game-2",
            "title": "Team A vs. Team B",
            "endDate": "2026-03-10T00:00:00Z",
            "tags": [{"label": "Sports"}, {"label": "Games"}],
            "markets": [
                {
                    "outcomes": json.dumps(["Team A", "Team B"]),
                    "outcomePrices": json.dumps([0.5, 0.5]),
                },
            ],
        }
        assert _parse_game_event(raw) is None

    def test_no_vs_returns_none(self) -> None:
        raw = {
            "id": "game-3",
            "title": "NBA Champion 2026",
            "endDate": "2026-06-01T00:00:00Z",
            "tags": [{"label": "NBA"}],
            "markets": [],
        }
        assert _parse_game_event(raw) is None


class TestFetchEvents:
    async def test_returns_events(self) -> None:
        raw = _make_raw_event(title="Arsenal vs. Everton")
        # httpx.Response.json() is sync, so use MagicMock for the response.
        mock_response = MagicMock()
        mock_response.json.return_value = [raw]

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.providers.polymarket.httpx.AsyncClient", return_value=mock_client):
            provider = PolymarketProvider()
            events = await provider.fetch_events(Sport.FOOTBALL, leagues=["Premier League"])
        assert len(events) == 1
        assert events[0].home_team == "Arsenal"

    async def test_empty_response(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = []

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.providers.polymarket.httpx.AsyncClient", return_value=mock_client):
            provider = PolymarketProvider()
            events = await provider.fetch_events(Sport.FOOTBALL, leagues=["Premier League"])
        assert events == []

    async def test_non_configured_sport_returns_empty(self) -> None:
        provider = PolymarketProvider()
        events = await provider.fetch_events(Sport.MOTOR_SPORTS)
        assert events == []


class TestMarketProb:
    def test_prefers_best_ask(self) -> None:
        m = {
            "bestAsk": 0.35,
            "lastTradePrice": 0.34,
            "outcomePrices": json.dumps([0.345, 0.655]),
        }
        assert _market_prob(m) == 0.35

    def test_falls_back_to_ltp_when_no_ask(self) -> None:
        m = {
            "lastTradePrice": 0.59,
            "outcomePrices": json.dumps([0.585, 0.415]),
        }
        assert _market_prob(m) == 0.59

    def test_falls_back_to_outcome_prices(self) -> None:
        m = {"outcomePrices": json.dumps([0.45, 0.55])}
        assert _market_prob(m) == 0.45

    def test_skips_zero_best_ask(self) -> None:
        m = {
            "bestAsk": 0,
            "lastTradePrice": 0.3,
            "outcomePrices": json.dumps([0.3, 0.7]),
        }
        assert _market_prob(m) == 0.3

    def test_returns_none_when_no_prices(self) -> None:
        assert _market_prob({}) is None


class TestEventVolume:
    def test_uses_event_level_volume(self) -> None:
        raw = {"volume": 165000.0, "markets": [{"volume": 80000}]}
        assert _event_volume(raw) == 165000.0

    def test_falls_back_to_market_sum(self) -> None:
        raw = {
            "volume": 0,
            "markets": [{"volume": 500.0}, {"volume": 300.0}],
        }
        assert _event_volume(raw) == 800.0

    def test_handles_missing_volume(self) -> None:
        raw = {"markets": []}
        assert _event_volume(raw) == 0.0


class TestDetectLeague:
    def test_returns_league_tag(self) -> None:
        raw = {
            "tags": [
                {"label": "Sports"},
                {"label": "Soccer"},
                {"label": "Games"},
                {"label": "EFL Championship"},
            ],
        }
        assert _detect_league(raw) == "EFL Championship"

    def test_skips_generic_tags(self) -> None:
        raw = {"tags": [{"label": "Sports"}, {"label": "Games"}]}
        assert _detect_league(raw) == ""

    def test_skips_sport_tags(self) -> None:
        raw = {"tags": [{"label": "NBA"}, {"label": "Western Conference"}]}
        assert _detect_league(raw) == "Western Conference"


class TestParseFootballEventFromGamesTag:
    """Football events from the Games tag use the same 3-way market structure."""

    def test_games_tag_football_event(self) -> None:
        raw = {
            "id": "games-football-1",
            "title": "Portsmouth FC vs. Ipswich Town FC",
            "endDate": "2026-03-15T15:00:00Z",
            "tags": [
                {"label": "Sports"},
                {"label": "Soccer"},
                {"label": "Games"},
                {"label": "EFL Championship"},
            ],
            "markets": [
                {
                    "groupItemTitle": "Portsmouth FC",
                    "outcomePrices": json.dumps([0.265, 0.735]),
                },
                {
                    "groupItemTitle": "Draw (Portsmouth FC vs. Ipswich Town FC)",
                    "outcomePrices": json.dumps([0.19, 0.81]),
                },
                {
                    "groupItemTitle": "Ipswich Town FC",
                    "outcomePrices": json.dumps([0.575, 0.425]),
                },
            ],
        }
        ev = _parse_football_event(raw, "EFL Championship")
        assert ev is not None
        assert ev.home_team == "Portsmouth FC"
        assert ev.away_team == "Ipswich Town FC"
        assert ev.league == "EFL Championship"
        assert ev.markets[0].name == "1X2"
        assert len(ev.markets[0].outcomes) == 3
