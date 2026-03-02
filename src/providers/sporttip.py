"""
Sporttip (Swisslos) odds scraper.

Sporttip has no public API. Their website loads odds via an embedded 'admiral-widget'
which makes internal API calls. We use Playwright to intercept these network requests
and extract the odds data.

Usage:
    python -m src.providers.sporttip
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from playwright.async_api import Response, async_playwright

from src.models.events import Event, Market, Outcome, Sport
from src.providers.base import BaseProvider

logger = logging.getLogger(__name__)

BASE_URL = "https://www.swisslos.ch"

SPORT_URLS: dict[Sport, str] = {
    Sport.FOOTBALL: f"{BASE_URL}/en/sporttip/sports/football",
    Sport.ICE_HOCKEY: f"{BASE_URL}/en/sporttip/sports/ice-hockey",
    Sport.TENNIS: f"{BASE_URL}/en/sporttip/sports/tennis",
    Sport.BASKETBALL: f"{BASE_URL}/en/sporttip/sports/basketball",
    Sport.HANDBALL: f"{BASE_URL}/en/sporttip/sports/handball",
}


class SporttipProvider(BaseProvider):
    """Scrapes odds from the Sporttip (Swisslos) website."""

    @property
    def name(self) -> str:
        return "Sporttip"

    async def fetch_events(self, sport: Sport) -> list[Event]:
        """Fetch events by intercepting network requests from the Sporttip widget."""
        url = SPORT_URLS.get(sport)
        if url is None:
            logger.warning("No URL configured for sport %s", sport)
            return []

        api_responses = await self._intercept_widget_requests(url)
        return self._parse_events(api_responses, sport)

    async def _intercept_widget_requests(self, url: str) -> list[dict[str, Any]]:
        """Navigate to a Sporttip page and capture API responses from the widget."""
        captured: list[dict[str, Any]] = []

        async def on_response(response: Response) -> None:
            req_url = response.url
            # Capture JSON responses that look like sports data API calls
            if response.status == 200 and _is_odds_api_response(req_url):
                try:
                    body = await response.json()
                    captured.append({"url": req_url, "data": body})
                    logger.info("Captured API response: %s", req_url)
                except Exception:
                    pass

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                locale="en-CH",
                timezone_id="Europe/Zurich",
            )
            page = await context.new_page()
            page.on("response", on_response)

            logger.info("Navigating to %s", url)
            await page.goto(url, wait_until="networkidle", timeout=30_000)

            # Give the widget extra time to load dynamic content
            await page.wait_for_timeout(3000)

            await browser.close()

        logger.info("Captured %d API responses", len(captured))
        return captured

    def _parse_events(
        self, api_responses: list[dict[str, Any]], sport: Sport
    ) -> list[Event]:
        """Parse captured API responses into Event models.

        The actual response structure will need to be mapped once we can inspect
        real API traffic. This method provides the parsing framework and handles
        common betting data structures.
        """
        events: list[Event] = []

        for response in api_responses:
            data = response["data"]
            url = response["url"]

            # Try to find event data — structure depends on the actual API
            raw_events = _extract_event_list(data)
            if not raw_events:
                logger.debug("No events found in response from %s", url)
                continue

            for raw in raw_events:
                try:
                    event = _parse_single_event(raw, sport)
                    if event is not None:
                        events.append(event)
                except Exception:
                    logger.debug("Failed to parse event: %s", raw, exc_info=True)

        return events


def _is_odds_api_response(url: str) -> bool:
    """Heuristic: does this URL look like a sports data API call?"""
    keywords = ["sport", "event", "match", "odds", "market", "fixture", "feed", "offer"]
    url_lower = url.lower()
    return any(kw in url_lower for kw in keywords)


def _extract_event_list(data: Any) -> list[Any]:
    """Try to locate event data in various common API response shapes."""
    if isinstance(data, list):
        return list(data)

    if isinstance(data, dict):
        # Common patterns: {"events": [...]}, {"data": {"events": [...]}}, {"matches": [...]}
        for key in ("events", "matches", "fixtures", "data", "items", "results"):
            if key in data:
                nested = data[key]
                if isinstance(nested, list):
                    return list(nested)
                if isinstance(nested, dict):
                    return _extract_event_list(nested)

    return []


def _parse_single_event(raw: dict[str, Any], sport: Sport) -> Event | None:
    """Parse a single event from raw API data.

    This attempts to handle common field naming conventions in sports data APIs.
    The exact mapping will be refined once we can inspect real Sporttip API responses.
    """
    event_id = str(
        raw.get("id", raw.get("eventId", raw.get("matchId", "")))
    )
    if not event_id:
        return None

    home = raw.get("homeTeam", raw.get("home", raw.get("team1", {})))
    away = raw.get("awayTeam", raw.get("away", raw.get("team2", {})))

    home_name = home.get("name", home) if isinstance(home, dict) else str(home)
    away_name = away.get("name", away) if isinstance(away, dict) else str(away)

    start_str = raw.get("startTime", raw.get("startDate", raw.get("scheduledTime", "")))
    try:
        start_time = datetime.fromisoformat(str(start_str))
    except (ValueError, TypeError):
        start_time = datetime.now(tz=timezone.utc)

    league = raw.get("league", raw.get("competition", raw.get("tournament", {}))),
    if isinstance(league, tuple):
        league = league[0]
    league_name = league.get("name", league) if isinstance(league, dict) else str(league)

    markets = _parse_markets(raw)

    return Event(
        id=event_id,
        sport=sport,
        league=league_name,
        home_team=home_name,
        away_team=away_name,
        start_time=start_time,
        markets=markets,
        provider="sporttip",
    )


def _parse_markets(raw: dict[str, Any]) -> list[Market]:
    """Extract betting markets from raw event data."""
    markets: list[Market] = []
    raw_markets = raw.get("markets", raw.get("odds", raw.get("offers", [])))

    if isinstance(raw_markets, list):
        for rm in raw_markets:
            if not isinstance(rm, dict):
                continue
            market_name = rm.get("name", rm.get("type", "unknown"))
            outcomes = []
            raw_outcomes = rm.get(
                "outcomes", rm.get("selections", rm.get("options", []))
            ) or []
            for ro in raw_outcomes:
                if isinstance(ro, dict):
                    name = ro.get("name", ro.get("label", ""))
                    odds_val = ro.get("odds", ro.get("price", ro.get("decimal", 0)))
                    try:
                        outcomes.append(Outcome(name=str(name), odds=float(odds_val or 0)))
                    except (ValueError, TypeError):
                        continue
            if outcomes:
                markets.append(Market(name=str(market_name), outcomes=outcomes))

    return markets


async def discover_api_endpoints(sport: Sport = Sport.FOOTBALL) -> None:
    """Utility to discover what API endpoints the Sporttip widget calls.

    Run this to inspect the network traffic and understand the API structure
    before refining the parser.
    """
    url = SPORT_URLS[sport]
    print(f"Navigating to {url} and capturing network requests...\n")

    all_requests: list[dict[str, str]] = []

    async def on_response(response: Response) -> None:
        content_type = response.headers.get("content-type", "")
        if "json" in content_type or "javascript" in content_type:
            all_requests.append({
                "url": response.url,
                "status": str(response.status),
                "content_type": content_type,
            })
            if "json" in content_type:
                try:
                    body = await response.json()
                    print(f"[JSON] {response.status} {response.url}")
                    keys = list(body.keys()) if isinstance(body, dict) else type(body).__name__
                    print(f"  Keys: {keys}")
                    print(f"  Preview: {json.dumps(body, default=str)[:200]}")
                    print()
                except Exception:
                    pass

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale="en-CH", timezone_id="Europe/Zurich")
        page = await context.new_page()
        page.on("response", on_response)

        await page.goto(url, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(5000)
        await browser.close()

    print(f"\n--- All JSON/JS requests ({len(all_requests)}) ---")
    for req in all_requests:
        print(f"  {req['status']} {req['url']}")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print("=== Sporttip API Endpoint Discovery ===\n")
    await discover_api_endpoints()


if __name__ == "__main__":
    asyncio.run(main())
