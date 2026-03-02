"""
Sporttip (Swisslos) odds provider.

Connects to the Admiral sportsbook WebSocket used by Sporttip to receive
real-time odds data. The protocol uses deflate-compressed JSON over binary
WebSocket frames.

Usage:
    # One-shot fetch
    python -m src.providers.sporttip

    # Live streaming
    python -m src.providers.sporttip --live
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import zlib
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Any

from playwright.async_api import async_playwright

from src.models.events import Event, Market, Outcome, Sport
from src.providers.base import BaseProvider

logger = logging.getLogger(__name__)

# Admiral WebSocket protocol constants
WS_URL = "wss://ws.ch.admiral.at/"
MANDATOR = "sporttip-ch-sl"
DEVICE_URN = "asw:node:admiral:device:1886b7bc-2f48-486a-ac40-4fe5828886a8"

# Map our Sport enum to Admiral URL segments
SPORT_URL_PARTS: dict[Sport, str] = {
    Sport.FOOTBALL: "/football",
    Sport.ICE_HOCKEY: "/ice-hockey",
    Sport.TENNIS: "/tennis",
    Sport.BASKETBALL: "/basketball",
    Sport.HANDBALL: "/handball",
}

SITE_URL = "https://www.swisslos.ch/en/sporttip/sports/football"


def _deflate_decode(data: bytes) -> dict[str, Any]:
    """Decompress a raw-deflate WebSocket frame and parse as JSON."""
    decompressed = zlib.decompress(data, -15)
    return json.loads(decompressed.decode("utf-8"))  # type: ignore[no-any-return]


class Snapshot:
    """Maintains the current state of all sportsbook entities.

    The Admiral WebSocket sends incremental updates to a snapshot. This class
    indexes entities by URN for fast lookups and applies updates as they arrive.
    """

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.markets: dict[str, dict[str, Any]] = {}
        self.selections: dict[str, dict[str, Any]] = {}
        self.competitors: dict[str, dict[str, Any]] = {}
        self.competitions: dict[str, dict[str, Any]] = {}
        self.market_types: dict[str, dict[str, Any]] = {}
        self.selection_types: dict[str, dict[str, Any]] = {}

    def apply_update(self, items: list[dict[str, Any]]) -> list[str]:
        """Apply snapshot update items. Returns URNs of changed selections (odds changes)."""
        changed_selections: list[str] = []
        type_to_store = {
            "Event": self.events,
            "Market": self.markets,
            "Selection": self.selections,
            "Competitor": self.competitors,
            "Competition": self.competitions,
            "MarketType": self.market_types,
            "SelectionType": self.selection_types,
        }

        for item in items:
            entity_type = item.get("type", "")
            entity = item.get("entity", {})
            urn = entity.get("urn", "")
            kind = item.get("kind", 0)  # 0=add/update, 1=remove

            store = type_to_store.get(entity_type)
            if store is None:
                continue

            if kind == 1:
                store.pop(urn, None)
            else:
                if entity_type == "Selection" and urn in store:
                    old_odds = store[urn].get("odds")
                    new_odds = entity.get("odds")
                    if old_odds != new_odds:
                        changed_selections.append(urn)
                store[urn] = entity

        return changed_selections

    def build_events(self, sport: Sport) -> list[Event]:
        """Build Event models from the current snapshot state."""
        result: list[Event] = []

        for urn, raw_event in self.events.items():
            competitors = raw_event.get("eventCompetitors", [])
            home_name = ""
            away_name = ""
            for ec in competitors:
                comp = self.competitors.get(ec.get("competitor", ""), {})
                name = _get_translated_name(comp)
                if ec.get("qualifier") == "home":
                    home_name = name
                elif ec.get("qualifier") == "away":
                    away_name = name

            comp_urn = raw_event.get("competition", "")
            competition = self.competitions.get(comp_urn, {})
            league_name = _get_translated_name(competition)

            start_str = raw_event.get("startTime", "")
            start_time = _parse_iso_datetime(str(start_str))

            market_urns = raw_event.get("markets", [])
            markets = self._build_markets(market_urns, home_name, away_name)

            result.append(Event(
                id=urn,
                sport=sport,
                league=league_name,
                home_team=home_name,
                away_team=away_name,
                start_time=start_time,
                markets=markets,
                provider="sporttip",
            ))

        return result

    def _build_markets(
        self, market_urns: list[str], home: str = "", away: str = ""
    ) -> list[Market]:
        """Resolve market URNs into Market models with odds."""
        markets: list[Market] = []

        for market_urn in market_urns:
            raw_market = self.markets.get(market_urn, {})
            if not raw_market:
                continue

            market_type_urn = raw_market.get("type", "")
            market_type = self.market_types.get(market_type_urn, {})
            market_name = _get_translated_name(market_type)

            # Resolve template variables in market name
            props = raw_market.get("properties", {})
            market_name = _resolve_templates(market_name, home, away, props)

            selection_urns = raw_market.get("selections", [])
            outcomes: list[Outcome] = []
            for sel_urn in selection_urns:
                sel = self.selections.get(sel_urn, {})
                if not sel or sel.get("state") != 1:
                    continue
                sel_type_urn = sel.get("type", "")
                sel_type = self.selection_types.get(sel_type_urn, {})
                sel_name = _resolve_templates(
                    _get_translated_name(sel_type), home, away, props
                )
                odds = sel.get("odds", 0)
                if odds and odds > 0:
                    outcomes.append(Outcome(name=sel_name, odds=float(odds)))

            if outcomes:
                markets.append(Market(name=market_name, outcomes=outcomes))

        return markets


def _get_translated_name(entity: dict[str, Any], lang: str = "en") -> str:
    """Get the English translation of an entity name, falling back to raw name."""
    translations = entity.get("translations", {})
    return str(translations.get(lang, entity.get("name", "")))


def _resolve_templates(
    text: str, home: str, away: str, props: dict[str, Any]
) -> str:
    """Resolve Admiral template variables like {$competitor1}, {total}, {hcp}."""
    text = text.replace("{$competitor1}", home)
    text = text.replace("{$competitor2}", away)
    text = text.replace("{$draw}", "Draw")
    if "{total}" in text:
        total = props.get("total", "")
        text = text.replace("{total}", str(total))
    if "{hcp}" in text:
        hcp = props.get("handicap", props.get("hcp", ""))
        text = text.replace("{hcp}", str(hcp))
    if "{!goalnr}" in text:
        text = text.replace("{!goalnr}", "1st")
    return text


def _parse_iso_datetime(s: str) -> datetime:
    """Parse ISO datetime, handling the trailing Z that Python 3.10 can't."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return datetime.now(tz=timezone.utc)


class SporttipProvider(BaseProvider):
    """Fetches odds from Sporttip via the Admiral WebSocket protocol."""

    @property
    def name(self) -> str:
        return "Sporttip"

    async def fetch_events(self, sport: Sport) -> list[Event]:
        """Fetch current events and odds for a sport (one-shot)."""
        url_part = SPORT_URL_PARTS.get(sport)
        if url_part is None:
            logger.warning("No URL configured for sport %s", sport)
            return []

        snapshot = Snapshot()
        await _connect_and_collect(snapshot, url_part)
        return snapshot.build_events(sport)

    async def stream_updates(
        self,
        sport: Sport,
        on_update: Callable[[list[Event], list[str]], None],
    ) -> None:
        """Stream live odds updates. Calls on_update(events, changed_urns) on each change."""
        url_part = SPORT_URL_PARTS.get(sport)
        if url_part is None:
            return

        snapshot = Snapshot()
        initial = True

        async for changed in _connect_and_stream(snapshot, url_part):
            events = snapshot.build_events(sport)
            if initial:
                on_update(events, [])
                initial = False
            elif changed:
                on_update(events, changed)


async def _connect_and_collect(snapshot: Snapshot, url_part: str) -> None:
    """Connect via Playwright, collect the initial snapshot, then disconnect."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="en-CH", timezone_id="Europe/Zurich"
        )
        page = await context.new_page()

        received_snapshot = asyncio.Event()

        def on_ws(ws: Any) -> None:
            def on_received(payload: Any) -> None:
                if not isinstance(payload, bytes):
                    return
                _process_ws_message(payload, snapshot)
                # The big snapshot has events and selections
                if snapshot.events and snapshot.selections:
                    received_snapshot.set()

            ws.on("framereceived", on_received)

        page.on("websocket", on_ws)

        site_url = f"https://www.swisslos.ch/en/sporttip/sports{url_part}"
        logger.info("Connecting to %s", site_url)
        await page.goto(site_url, wait_until="networkidle", timeout=45_000)

        # Wait for the snapshot data to arrive (with timeout)
        try:
            await asyncio.wait_for(received_snapshot.wait(), timeout=20.0)
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for snapshot data")

        # Give a moment for any trailing updates
        await page.wait_for_timeout(2000)

        await browser.close()

    logger.info(
        "Collected %d events, %d markets, %d selections",
        len(snapshot.events),
        len(snapshot.markets),
        len(snapshot.selections),
    )


async def _connect_and_stream(
    snapshot: Snapshot, url_part: str
) -> AsyncIterator[list[str]]:
    """Connect and yield lists of changed selection URNs as updates arrive."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="en-CH", timezone_id="Europe/Zurich"
        )
        page = await context.new_page()

        update_queue: asyncio.Queue[list[str]] = asyncio.Queue()

        def on_ws(ws: Any) -> None:
            def on_received(payload: Any) -> None:
                if not isinstance(payload, bytes):
                    return
                changed = _process_ws_message(payload, snapshot)
                if changed is not None:
                    update_queue.put_nowait(changed)

            ws.on("framereceived", on_received)

        page.on("websocket", on_ws)

        site_url = f"https://www.swisslos.ch/en/sporttip/sports{url_part}"
        logger.info("Streaming from %s", site_url)
        await page.goto(site_url, wait_until="networkidle", timeout=45_000)

        try:
            while True:
                changed = await update_queue.get()
                yield changed
        except asyncio.CancelledError:
            pass
        finally:
            await browser.close()


def _process_ws_message(
    payload: bytes, snapshot: Snapshot
) -> list[str] | None:
    """Decode a WS frame and apply any snapshot updates. Returns changed selection URNs."""
    try:
        msg = _deflate_decode(payload)
    except Exception:
        return None

    raw_payload = msg.get("payload")
    if not isinstance(raw_payload, str):
        return None

    try:
        inner = json.loads(raw_payload)
    except (json.JSONDecodeError, TypeError):
        return None

    all_changed: list[str] = []
    for item in inner:
        if item.get("type") != "SportsbookSnapshotUpdated":
            continue
        body = item.get("body", {})
        update = body.get("snapshotUpdate", {})
        update_items = update.get("snapshotUpdateItems", [])
        if update_items:
            changed = snapshot.apply_update(update_items)
            all_changed.extend(changed)

    return all_changed if all_changed else None


def _format_event_line(event: Event) -> str:
    """Format a single event for display."""
    lines = [f"  {event.home_team} vs {event.away_team}"]
    lines.append(f"    League: {event.league} | Kickoff: {event.start_time}")
    for market in event.markets:
        odds_str = " | ".join(f"{o.name}: {o.odds:.2f}" for o in market.outcomes)
        lines.append(f"    [{market.name}] {odds_str}")
    return "\n".join(lines)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    live_mode = "--live" in sys.argv
    provider = SporttipProvider()

    if live_mode:
        print("=== Sporttip Live Odds Stream (Ctrl+C to stop) ===\n")
        update_count = 0

        def on_update(events: list[Event], changed: list[str]) -> None:
            nonlocal update_count
            update_count += 1
            if not changed:
                print(f"--- Initial snapshot: {len(events)} events ---\n")
                for event in events:
                    print(_format_event_line(event))
                    print()
            else:
                print(f"\n--- Update #{update_count}: {len(changed)} odds changed ---")
                for event in events:
                    for market in event.markets:
                        for outcome in market.outcomes:
                            # Simple check: just print events that have active markets
                            pass
                # For now just show the count; refinement can show specific changes
                print(f"    Total events: {len(events)}")

        try:
            await provider.stream_updates(Sport.FOOTBALL, on_update)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        print("=== Sporttip Current Odds ===\n")
        events = await provider.fetch_events(Sport.FOOTBALL)
        print(f"Found {len(events)} events:\n")
        for event in events:
            print(_format_event_line(event))
            print()


if __name__ == "__main__":
    asyncio.run(main())
