"""The Odds API provider — fetches multi-bookmaker odds for fair value estimation."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import httpx

from src.models.events import BookmakerOdds, MultiBookMarket, Sport

logger = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4/sports"

# Sport enum → The Odds API sport keys.
SPORT_KEY_MAP: dict[Sport, list[str]] = {
    Sport.FOOTBALL: [
        "soccer_epl",
        "soccer_germany_bundesliga",
        "soccer_spain_la_liga",
        "soccer_italy_serie_a",
        "soccer_france_ligue_one",
        "soccer_uefa_champs_league",
        "soccer_uefa_europa_league",
        "soccer_usa_mls",
    ],
    Sport.BASKETBALL: ["basketball_nba"],
    Sport.ICE_HOCKEY: ["icehockey_nhl"],
    Sport.TENNIS: ["tennis_atp_french_open", "tennis_wta_french_open"],
    Sport.HANDBALL: [],
    Sport.MOTOR_SPORTS: [],
}

# The Odds API league labels → our league names.
_LEAGUE_LABEL_MAP: dict[str, str] = {
    "soccer_epl": "Premier League",
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_spain_la_liga": "LaLiga",
    "soccer_italy_serie_a": "Serie A",
    "soccer_france_ligue_one": "Ligue 1",
    "soccer_uefa_champs_league": "Champions League",
    "soccer_uefa_europa_league": "Europa League",
    "soccer_usa_mls": "MLS",
    "basketball_nba": "NBA",
    "icehockey_nhl": "NHL",
}

# In-memory cache: sport_key → (timestamp, data).
_cache: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 60.0  # seconds


class TheOddsProvider:
    """Fetch multi-bookmaker odds from The Odds API (free tier)."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("THE_ODDS_API_KEY", "")

    async def fetch_multi_book_odds(
        self,
        sport: Sport,
        leagues: list[str] | None = None,
    ) -> list[MultiBookMarket]:
        """Fetch h2h odds from all bookmakers for a sport.

        Returns a list of MultiBookMarket with odds_by_outcome populated
        per bookmaker.
        """
        if not self.api_key:
            logger.warning("THE_ODDS_API_KEY not set — skipping The Odds API")
            return []

        sport_keys = SPORT_KEY_MAP.get(sport, [])
        if not sport_keys:
            return []

        # Filter to specific league keys if requested.
        if leagues:
            league_to_key = {v: k for k, v in _LEAGUE_LABEL_MAP.items()}
            filtered = [league_to_key[lg] for lg in leagues if lg in league_to_key]
            if filtered:
                sport_keys = [k for k in sport_keys if k in filtered]

        markets: list[MultiBookMarket] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            for sport_key in sport_keys:
                raw_events = await self._fetch_sport_key(client, sport_key)
                league = _LEAGUE_LABEL_MAP.get(sport_key, sport_key)
                for raw in raw_events:
                    market = _parse_event(raw, sport, league)
                    if market is not None:
                        markets.append(market)

        return markets

    async def _fetch_sport_key(
        self,
        client: httpx.AsyncClient,
        sport_key: str,
    ) -> list[dict]:
        """Fetch odds for a single sport key with caching."""
        now = time.monotonic()
        cached = _cache.get(sport_key)
        if cached is not None:
            ts, data = cached
            if now - ts < _CACHE_TTL:
                return data

        params = {
            "apiKey": self.api_key,
            "regions": "eu,uk,us",
            "markets": "h2h",
            "oddsFormat": "decimal",
        }
        try:
            resp = await client.get(f"{BASE_URL}/{sport_key}/odds", params=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError:
            logger.warning("Failed to fetch The Odds API for %s", sport_key)
            return []

        _cache[sport_key] = (now, data)
        return data


def _parse_event(raw: dict, sport: Sport, league: str) -> MultiBookMarket | None:
    """Parse a single event from The Odds API response into a MultiBookMarket."""
    home_team = raw.get("home_team", "")
    away_team = raw.get("away_team", "")
    if not home_team or not away_team:
        return None

    commence_time = raw.get("commence_time", "")
    try:
        start_time = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        start_time = datetime.now(tz=timezone.utc)

    # Determine outcome names from the first bookmaker.
    bookmakers = raw.get("bookmakers", [])
    if not bookmakers:
        return None

    # Detect 2-way vs 3-way from first bookmaker's outcomes.
    first_outcomes = []
    for bm in bookmakers:
        for market in bm.get("markets", []):
            if market.get("key") == "h2h":
                first_outcomes = market.get("outcomes", [])
                break
        if first_outcomes:
            break

    if len(first_outcomes) < 2:
        return None

    # Map outcome names: home=1, away=2, Draw=X.
    outcome_names: list[str] = []
    for oc in first_outcomes:
        name = oc.get("name", "")
        if name == home_team:
            outcome_names.append("1")
        elif name == away_team:
            outcome_names.append("2")
        elif name.lower() == "draw":
            outcome_names.append("X")
        else:
            outcome_names.append(name)

    outcome_names_tuple = tuple(outcome_names)
    odds_by_outcome: dict[str, list[BookmakerOdds]] = {n: [] for n in outcome_names_tuple}

    # Build a name → canonical outcome mapping.
    name_map = {}
    for oc, canonical in zip(first_outcomes, outcome_names):
        name_map[oc.get("name", "")] = canonical

    for bm in bookmakers:
        bm_name = bm.get("title", bm.get("key", "unknown"))
        for market in bm.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for oc in market.get("outcomes", []):
                oc_name = oc.get("name", "")
                canonical = name_map.get(oc_name)
                if canonical is None:
                    # Try draw detection for bookmakers that label it differently.
                    if oc_name.lower() == "draw":
                        canonical = "X"
                    else:
                        continue
                if canonical not in odds_by_outcome:
                    continue
                try:
                    price = float(oc.get("price", 0))
                except (TypeError, ValueError):
                    continue
                if price > 1.0:
                    odds_by_outcome[canonical].append(BookmakerOdds(bookmaker=bm_name, odds=price))

    # Require at least one bookmaker per outcome.
    if any(not v for v in odds_by_outcome.values()):
        return None

    return MultiBookMarket(
        event_id=raw.get("id", ""),
        sport=sport,
        league=league,
        home_team=home_team,
        away_team=away_team,
        start_time=start_time,
        outcome_names=outcome_names_tuple,
        odds_by_outcome=odds_by_outcome,
    )
