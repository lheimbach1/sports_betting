"""Polymarket prediction-market provider (Gamma API)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from src.models.events import Event, Market, Outcome, Sport
from src.providers.base import BaseProvider

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

# Tag IDs for per-league queries (football only).
LEAGUE_TAGS: dict[str, int] = {
    "Premier League": 82,
    "LaLiga": 780,
    "Bundesliga": 1494,
    "Serie A": 101962,
    "Ligue 1": 102070,
    "Champions League": 100977,
    "Europa League": 101787,
    "Conference League": 102763,
    "MLS": 100100,
}

# Sport-specific tag IDs for non-football sports.
# These return all events (futures + games) for each sport.
_SPORT_TAG_IDS: dict[Sport, int] = {
    Sport.BASKETBALL: 745,   # NBA
    Sport.ICE_HOCKEY: 899,   # NHL
}

# The "Games" tag returns individual match events across all sports.
# Used as a fallback for sports without a dedicated tag ID.
_GAMES_TAG_ID = 100639
_PAGE_SIZE = 500

# Mapping from Polymarket event tag labels to our Sport enum.
_TAG_TO_SPORT: dict[str, Sport] = {
    "NBA": Sport.BASKETBALL,
    "WNBA": Sport.BASKETBALL,
    "Euroleague Basketball": Sport.BASKETBALL,
    "NHL": Sport.ICE_HOCKEY,
    "AHL": Sport.ICE_HOCKEY,
    "KHL": Sport.ICE_HOCKEY,
    "SHL": Sport.ICE_HOCKEY,
    "Tennis": Sport.TENNIS,
    "Soccer": Sport.FOOTBALL,
    "Handball": Sport.HANDBALL,
}

# Sports that have per-league tag IDs (queried league-by-league).
SPORT_TAGS: dict[Sport, dict[str, int]] = {
    Sport.FOOTBALL: LEAGUE_TAGS,
}

# Sports where game events are discovered via tag-based queries.
_GAME_TAG_SPORTS: set[Sport] = {
    Sport.BASKETBALL,
    Sport.ICE_HOCKEY,
    Sport.TENNIS,
    Sport.HANDBALL,
}


def get_available_sports() -> list[Sport]:
    """Return sports available on Polymarket (league-based + game-tag-based)."""
    return list(SPORT_TAGS.keys()) + sorted(_GAME_TAG_SPORTS, key=lambda s: s.value)


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


def _detect_sport(raw: dict[str, Any]) -> Sport | None:
    """Detect sport from an event's tags."""
    for tag in raw.get("tags", []):
        label = tag.get("label", "")
        if label in _TAG_TO_SPORT:
            return _TAG_TO_SPORT[label]
    return None


def _detect_league(raw: dict[str, Any]) -> str:
    """Detect league name from an event's tags (first non-generic tag)."""
    for tag in raw.get("tags", []):
        label = tag.get("label", "")
        if label not in ("Sports", "Games") and label not in _TAG_TO_SPORT:
            return label
    return ""


def _market_prob(m: dict[str, Any]) -> float | None:
    """Extract the YES probability from a Polymarket binary market.

    Prefers ``bestAsk`` (the live orderbook price you'd actually pay to buy
    a YES share) over ``lastTradePrice`` and ``outcomePrices``.

    Using ``bestAsk`` is critical for 3-way football markets where each
    outcome is a separate binary contract.  ``lastTradePrice`` can be stale
    and inconsistent across the three contracts, creating phantom arbitrage
    in ~45% of events.  ``bestAsk`` sums to >1.0 across all outcomes (the
    natural overround), eliminating false arb signals.
    """
    # 1) bestAsk — actual executable price on the order book.
    best_ask = m.get("bestAsk")
    if best_ask is not None:
        try:
            val = float(best_ask)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass

    # 2) lastTradePrice — fallback for markets without an active book.
    ltp = m.get("lastTradePrice")
    if ltp is not None:
        try:
            val = float(ltp)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass

    # 3) outcomePrices mid-market — last resort.
    outcome_prices: str | None = m.get("outcomePrices")
    if not outcome_prices:
        return None
    try:
        prices = json.loads(outcome_prices)
        val = float(prices[0])
        return val if val > 0 else None
    except (json.JSONDecodeError, IndexError, TypeError, ValueError):
        return None


def _event_volume(raw: dict[str, Any]) -> float:
    """Compute total USD volume for an event.

    Uses the event-level ``volume`` field when available, falling back to the
    sum of per-market volumes.
    """
    try:
        vol = float(raw.get("volume") or 0)
        if vol > 0:
            return vol
    except (TypeError, ValueError):
        pass

    # Fallback: sum market-level volumes.
    total = 0.0
    for m in raw.get("markets", []):
        try:
            total += float(m.get("volume") or 0)
        except (TypeError, ValueError):
            continue
    return total


def _parse_football_event(  # noqa: C901
    raw: dict[str, Any],
    league_name: str,
) -> Event | None:
    """Parse a football event fetched via league tag IDs.

    Uses groupItemTitle to identify home/draw/away binary contracts.
    """
    title: str = raw.get("title", "")
    teams = _parse_teams(title)
    if teams is None:
        return None
    home, away = teams

    markets_raw: list[dict[str, Any]] = raw.get("markets", [])
    if len(markets_raw) < 2:
        return None

    home_outcome: Outcome | None = None
    draw_outcome: Outcome | None = None
    away_outcome: Outcome | None = None

    for m in markets_raw:
        group_title: str = m.get("groupItemTitle", "")
        prob = _market_prob(m)
        if prob is None or prob <= 0:
            continue

        odds = round(1.0 / prob, 2)

        if "draw" in group_title.lower():
            draw_outcome = Outcome(name="X", odds=odds)
        elif home.lower() in group_title.lower():
            home_outcome = Outcome(name="1", odds=odds)
        elif away.lower() in group_title.lower():
            away_outcome = Outcome(name="2", odds=odds)

    if home_outcome is None or away_outcome is None:
        return None

    if draw_outcome is not None:
        market = Market(name="1X2", outcomes=[home_outcome, draw_outcome, away_outcome])
    else:
        market = Market(name="Moneyline", outcomes=[home_outcome, away_outcome])

    end_date_str: str = raw.get("endDate", "")
    try:
        kickoff = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        kickoff = datetime.now(tz=timezone.utc)

    event_id: str = raw.get("id", raw.get("slug", title))
    volume = _event_volume(raw)

    return Event(
        id=str(event_id),
        sport=Sport.FOOTBALL,
        league=league_name,
        home_team=home,
        away_team=away,
        start_time=kickoff,
        markets=[market],
        provider="polymarket",
        volume=volume,
    )


def _parse_game_event(raw: dict[str, Any]) -> Event | None:
    """Parse a game event fetched via the Games tag (tag_id=100639).

    The moneyline market is the first market with team-name outcomes
    (e.g. ["Nuggets", "Grizzlies"]) and no groupItemTitle.
    """
    title: str = raw.get("title", "")
    teams = _parse_teams(title)
    if teams is None:
        return None
    home, away = teams

    sport = _detect_sport(raw)
    if sport is None:
        return None

    markets_raw: list[dict[str, Any]] = raw.get("markets", [])
    if not markets_raw:
        return None

    # Find the moneyline market: first market with exactly 2 outcomes
    # that match the team names, and no groupItemTitle prefix like "Spread"
    # or "O/U".
    moneyline: Market | None = None
    for m in markets_raw:
        group_title: str = m.get("groupItemTitle", "")
        # Skip spread, over/under, and player prop markets.
        if group_title and any(
            kw in group_title.lower()
            for kw in ("spread", "o/u", "over", "under", "total")
        ):
            continue

        outcomes_raw: list[str] | str = m.get("outcomes", [])
        if isinstance(outcomes_raw, str):
            try:
                outcomes_raw = json.loads(outcomes_raw)
            except (json.JSONDecodeError, TypeError):
                continue
        if len(outcomes_raw) != 2:
            continue

        # For 2-way game events (single binary contract):
        #   Home = buy YES at bestAsk
        #   Away = buy NO  at (1 - bestBid)
        # This gives the actual executable price for each side.
        best_ask = m.get("bestAsk")
        best_bid = m.get("bestBid")
        ltp_home = m.get("lastTradePrice")
        outcome_prices: str | None = m.get("outcomePrices")
        try:
            if best_ask is not None and float(best_ask) > 0:
                prob_home = float(best_ask)
            elif ltp_home:
                prob_home = float(ltp_home)
            elif outcome_prices:
                prob_home = float(json.loads(outcome_prices)[0])
            else:
                continue
            if best_bid is not None:
                prob_away = 1.0 - float(best_bid)
            else:
                prob_away = 1.0 - prob_home
        except (json.JSONDecodeError, IndexError, TypeError, ValueError):
            continue

        if prob_home <= 0 or prob_away <= 0:
            continue

        odds_home = round(1.0 / prob_home, 2)
        odds_away = round(1.0 / prob_away, 2)

        moneyline = Market(
            name="Moneyline",
            outcomes=[
                Outcome(name="1", odds=odds_home),
                Outcome(name="2", odds=odds_away),
            ],
        )
        break

    if moneyline is None:
        return None

    # Derive league from tags.
    league = ""
    for tag in raw.get("tags", []):
        label = tag.get("label", "")
        if label not in ("Sports", "Games") and label not in _TAG_TO_SPORT:
            league = label
            break

    end_date_str: str = raw.get("endDate", "")
    try:
        kickoff = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        kickoff = datetime.now(tz=timezone.utc)

    event_id: str = raw.get("id", raw.get("slug", title))
    volume = _event_volume(raw)

    return Event(
        id=str(event_id),
        sport=sport,
        league=league,
        home_team=home,
        away_team=away,
        start_time=kickoff,
        markets=[moneyline],
        provider="polymarket",
        volume=volume,
    )


class PolymarketProvider(BaseProvider):
    """Fetch match-odds from Polymarket's Gamma API."""

    @property
    def name(self) -> str:
        return "Polymarket"

    async def fetch_events(
        self,
        sport: Sport,
        leagues: list[str] | None = None,
    ) -> list[Event]:
        """Fetch current match-odds events from Polymarket.

        Football uses per-league tag queries.  Sports with a dedicated tag ID
        (NBA, NHL) use that tag directly.  Other sports fall back to the
        Games tag (100639).
        """
        if sport == Sport.FOOTBALL:
            return await self._fetch_football(leagues)
        if sport in _SPORT_TAG_IDS:
            return await self._fetch_via_sport_tag(sport)
        if sport in _GAME_TAG_SPORTS:
            return await self._fetch_via_games_tag(sport)
        return []

    async def _fetch_football(self, leagues: list[str] | None = None) -> list[Event]:
        """Fetch football events via per-league tag IDs.

        When no league filter is given, also fetches from the Games tag to
        discover events from leagues beyond the configured ``LEAGUE_TAGS``
        (e.g. EFL Championship, Eredivisie, Liga MX, …).
        """
        tags = LEAGUE_TAGS
        if leagues:
            tags = {k: v for k, v in LEAGUE_TAGS.items() if k in leagues}

        events: list[Event] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            for league_name, tag_id in tags.items():
                params = {
                    "active": "true",
                    "closed": "false",
                    "tag_id": str(tag_id),
                    "limit": "100",
                }
                try:
                    resp = await client.get(f"{GAMMA_BASE}/events", params=params)
                    resp.raise_for_status()
                except httpx.HTTPError:
                    logger.warning("Failed to fetch Polymarket events for %s", league_name)
                    continue

                for raw_event in resp.json():
                    ev = _parse_football_event(raw_event, league_name)
                    if ev is not None:
                        events.append(ev)

        # When fetching all leagues, also check the Games tag for football
        # events from leagues not covered by LEAGUE_TAGS.
        if not leagues:
            seen_ids = {ev.id for ev in events}
            extra = await self._fetch_football_from_games_tag()
            for ev in extra:
                if ev.id not in seen_ids:
                    seen_ids.add(ev.id)
                    events.append(ev)

        return events

    async def _fetch_football_from_games_tag(self) -> list[Event]:
        """Fetch football events from the Games tag (covers extra leagues).

        Paginates through all results since the Games tag can contain
        thousands of events across all sports.
        """
        events: list[Event] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            offset = 0
            while True:
                params = {
                    "active": "true",
                    "closed": "false",
                    "tag_id": str(_GAMES_TAG_ID),
                    "limit": str(_PAGE_SIZE),
                    "offset": str(offset),
                }
                try:
                    resp = await client.get(f"{GAMMA_BASE}/events", params=params)
                    resp.raise_for_status()
                except httpx.HTTPError:
                    logger.warning("Failed to fetch Polymarket football game events (offset=%d)", offset)
                    break

                page = resp.json()
                if not page:
                    break

                for raw_event in page:
                    if _detect_sport(raw_event) != Sport.FOOTBALL:
                        continue
                    league = _detect_league(raw_event)
                    ev = _parse_football_event(raw_event, league)
                    if ev is not None:
                        events.append(ev)

                if len(page) < _PAGE_SIZE:
                    break
                offset += _PAGE_SIZE

        return events

    async def _fetch_via_sport_tag(self, sport: Sport) -> list[Event]:
        """Fetch game events via a sport-specific tag ID (e.g. NBA=745, NHL=899)."""
        tag_id = _SPORT_TAG_IDS[sport]
        events: list[Event] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            params = {
                "active": "true",
                "closed": "false",
                "tag_id": str(tag_id),
                "limit": "200",
            }
            try:
                resp = await client.get(f"{GAMMA_BASE}/events", params=params)
                resp.raise_for_status()
            except httpx.HTTPError:
                logger.warning("Failed to fetch Polymarket %s events", sport.value)
                return []

            for raw_event in resp.json():
                ev = _parse_game_event(raw_event)
                if ev is not None and ev.sport == sport:
                    events.append(ev)

        return events

    async def _fetch_via_games_tag(self, sport: Sport) -> list[Event]:
        """Fetch game events via the Games tag, filtered to a specific sport.

        Paginates through all results since the Games tag can contain
        thousands of events across all sports.
        """
        events: list[Event] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            offset = 0
            while True:
                params = {
                    "active": "true",
                    "closed": "false",
                    "tag_id": str(_GAMES_TAG_ID),
                    "limit": str(_PAGE_SIZE),
                    "offset": str(offset),
                }
                try:
                    resp = await client.get(f"{GAMMA_BASE}/events", params=params)
                    resp.raise_for_status()
                except httpx.HTTPError:
                    logger.warning("Failed to fetch Polymarket game events (offset=%d)", offset)
                    break

                page = resp.json()
                if not page:
                    break

                for raw_event in page:
                    ev = _parse_game_event(raw_event)
                    if ev is not None and ev.sport == sport:
                        events.append(ev)

                if len(page) < _PAGE_SIZE:
                    break
                offset += _PAGE_SIZE

        return events
