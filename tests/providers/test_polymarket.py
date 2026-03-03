"""Tests for the Polymarket provider."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.models.events import Sport
from src.providers.polymarket import (
    PolymarketProvider,
    _parse_event,
    _parse_teams,
)


def _make_raw_event(
    title: str = "Everton FC vs. Burnley FC",
    home_prob: float = 0.615,
    draw_prob: float = 0.25,
    away_prob: float = 0.135,
    end_date: str = "2026-03-15T15:00:00Z",
    event_id: str = "evt-123",
) -> dict:
    """Build a minimal Gamma API event fixture."""
    home_team = title.split(" vs. ")[0].strip()
    away_team = title.split(" vs. ")[1].strip() if " vs. " in title else "Unknown"
    return {
        "id": event_id,
        "title": title,
        "endDate": end_date,
        "markets": [
            {
                "groupItemTitle": f"{home_team} Win",
                "outcomePrices": json.dumps([home_prob, 1 - home_prob]),
            },
            {
                "groupItemTitle": "Draw",
                "outcomePrices": json.dumps([draw_prob, 1 - draw_prob]),
            },
            {
                "groupItemTitle": f"{away_team} Win",
                "outcomePrices": json.dumps([away_prob, 1 - away_prob]),
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
        ev = _parse_event(raw, "Premier League")
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
        assert _parse_event(raw, "Premier League") is None

    def test_zero_probability(self) -> None:
        raw = _make_raw_event(home_prob=0.0)
        assert _parse_event(raw, "Premier League") is None

    def test_probability_to_odds_exact(self) -> None:
        assert round(1.0 / 0.615, 2) == 1.63
        assert round(1.0 / 0.5, 2) == 2.0
        assert round(1.0 / 0.1, 2) == 10.0

    def test_no_vs_in_title(self) -> None:
        raw = _make_raw_event()
        raw["title"] = "Premier League Winner"
        assert _parse_event(raw, "Premier League") is None


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

    async def test_non_football_returns_empty(self) -> None:
        provider = PolymarketProvider()
        events = await provider.fetch_events(Sport.TENNIS)
        assert events == []
