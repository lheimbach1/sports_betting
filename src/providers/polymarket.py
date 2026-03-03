"""Polymarket prediction-market provider (Gamma API)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from src.models.events import Event, Market, Outcome, Sport
from src.providers.base import BaseProvider

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

# Polymarket tag IDs for football leagues.
LEAGUE_TAGS: dict[str, int] = {
    "Premier League": 82,
    "LaLiga": 780,
    "Bundesliga": 1494,
    "Serie A": 101962,
    "Ligue 1": 102070,
}


def _parse_teams(title: str) -> tuple[str, str] | None:
    """Extract (home, away) from an event title like 'Everton FC vs. Burnley FC'."""
    if " vs. " not in title:
        return None
    parts = title.split(" vs. ", maxsplit=1)
    if len(parts) != 2:
        return None
    home = parts[0].strip()
    away = parts[1].strip()
    if not home or not away:
        return None
    return home, away


def _parse_event(raw: dict[str, Any], league_name: str) -> Event | None:  # noqa: C901
    """Parse a Gamma API event dict into an Event model.

    Returns None if the event doesn't have valid 1x2 markets.
    """
    title: str = raw.get("title", "")
    teams = _parse_teams(title)
    if teams is None:
        return None
    home, away = teams

    markets_raw: list[dict[str, Any]] = raw.get("markets", [])
    if len(markets_raw) < 3:
        return None

    home_outcome: Outcome | None = None
    draw_outcome: Outcome | None = None
    away_outcome: Outcome | None = None

    for m in markets_raw:
        group_title: str = m.get("groupItemTitle", "")
        outcome_prices: str | None = m.get("outcomePrices")
        if not outcome_prices:
            continue

        # outcomePrices is a JSON-encoded list like '[0.615, 0.385]'
        # The first element is the "Yes" probability for this binary contract.
        try:
            import json

            prices = json.loads(outcome_prices)
            prob = float(prices[0])
        except (json.JSONDecodeError, IndexError, TypeError, ValueError):
            continue

        if prob <= 0:
            continue

        odds = round(1.0 / prob, 2)

        if "draw" in group_title.lower():
            draw_outcome = Outcome(name="X", odds=odds)
        elif home.lower() in group_title.lower():
            home_outcome = Outcome(name="1", odds=odds)
        elif away.lower() in group_title.lower():
            away_outcome = Outcome(name="2", odds=odds)

    if home_outcome is None or draw_outcome is None or away_outcome is None:
        return None

    # Parse kickoff time from endDate.
    end_date_str: str = raw.get("endDate", "")
    try:
        kickoff = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        kickoff = datetime.now(tz=timezone.utc)

    event_id: str = raw.get("id", raw.get("slug", title))

    return Event(
        id=str(event_id),
        sport=Sport.FOOTBALL,
        league=league_name,
        home_team=home,
        away_team=away,
        start_time=kickoff,
        markets=[Market(name="1X2", outcomes=[home_outcome, draw_outcome, away_outcome])],
        provider="polymarket",
    )


class PolymarketProvider(BaseProvider):
    """Fetch 1x2 football odds from Polymarket's Gamma API."""

    @property
    def name(self) -> str:
        return "Polymarket"

    async def fetch_events(
        self,
        sport: Sport,
        leagues: list[str] | None = None,
    ) -> list[Event]:
        """Fetch current 1x2 events from Polymarket.

        Only supports Sport.FOOTBALL — returns empty list for other sports.
        """
        if sport != Sport.FOOTBALL:
            return []

        tags = LEAGUE_TAGS
        if leagues:
            tags = {k: v for k, v in LEAGUE_TAGS.items() if k in leagues}

        events: list[Event] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            for league_name, tag_id in tags.items():
                url = f"{GAMMA_BASE}/events"
                params = {
                    "active": "true",
                    "closed": "false",
                    "tag_id": str(tag_id),
                    "limit": "100",
                }
                try:
                    resp = await client.get(url, params=params)
                    resp.raise_for_status()
                except httpx.HTTPError:
                    logger.warning("Failed to fetch Polymarket events for %s", league_name)
                    continue

                for raw_event in resp.json():
                    ev = _parse_event(raw_event, league_name)
                    if ev is not None:
                        events.append(ev)

        return events
