"""
Sporttip (Swisslos) odds provider.

Connects to the Admiral sportsbook WebSocket used by Sporttip to receive
real-time odds data. The protocol uses deflate-compressed JSON over binary
WebSocket frames.

Usage:
    # One-shot fetch with all markets (default: Bundesliga + 2. Bundesliga)
    python -m src.providers.sporttip

    # Display all markets (default only shows Final Result / 1X2)
    python -m src.providers.sporttip --all-markets

    # Fast mode — only load 1X2 from league page (no detail pages)
    python -m src.providers.sporttip --basic

    # Specific leagues
    python -m src.providers.sporttip --leagues "Premier League,LaLiga"

    # Live streaming
    python -m src.providers.sporttip --live

    # Watch 1X2 odds for a specific game (interactive picker)
    python -m src.providers.sporttip --watch

    # Watch by team name (auto-selects matching event)
    python -m src.providers.sporttip --watch "Bayern"
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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

# Reverse mapping: URL slug -> Sport enum
SPORT_FROM_SLUG: dict[str, Sport] = {
    v.lstrip("/"): k for k, v in SPORT_URL_PARTS.items()
}

SITE_BASE = "https://www.swisslos.ch/en/sporttip/sports"

# Known league URL paths (sport/country/league segments)
LEAGUE_URLS: dict[str, str] = {
    "Bundesliga": "/football/germany/bundesliga",
    "2. Bundesliga": "/football/germany/2-bundesliga",
    "Super League": "/football/switzerland/super-league",
    "Premier League": "/football/england/premier-league",
    "LaLiga": "/football/spain/laliga",
    "Serie A": "/football/italy/serie-a",
    "Ligue 1": "/football/france/ligue-1",
}
DEFAULT_LEAGUES: list[str] = ["Bundesliga", "2. Bundesliga"]

# Max parallel browser tabs for loading event detail pages
_DETAIL_CONCURRENCY = 3

# CSS selector for event detail links on league pages
_EVENT_LINK_SELECTOR = "a.mini-scoreboard-tap-area"


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

    async def fetch_events(
        self,
        sport: Sport,
        leagues: list[str] | None = None,
        all_markets: bool = True,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[Event]:
        """Fetch current events and odds for a sport (one-shot).

        Args:
            sport: The sport to fetch events for.
            leagues: Optional list of league names to filter by.
                     Defaults to DEFAULT_LEAGUES. Pass an empty list to fetch
                     the sport landing page (all highlighted events).
            all_markets: If True (default), visit each event's detail page
                         to load all available markets. If False, only load
                         the primary market (1X2) from the league page.
            on_progress: Optional callback(current, total) for progress updates.
        """
        if leagues is None:
            leagues = DEFAULT_LEAGUES

        if not leagues:
            url_part = SPORT_URL_PARTS.get(sport)
            if url_part is None:
                logger.warning("No URL configured for sport %s", sport)
                return []
            snapshot = Snapshot()
            await _connect_and_collect(snapshot, url_part)
            return snapshot.build_events(sport)

        all_events: list[Event] = []
        seen_urns: set[str] = set()
        for league in leagues:
            url_path = _resolve_league_url(league, sport)
            if all_markets:
                events = await _collect_all_markets(
                    url_path, sport, on_progress=on_progress,
                )
            else:
                snapshot = Snapshot()
                await _connect_and_collect(snapshot, url_path)
                events = snapshot.build_events(sport)
            for event in events:
                if event.id not in seen_urns:
                    seen_urns.add(event.id)
                    all_events.append(event)
        return all_events

    async def stream_updates(
        self,
        sport: Sport,
        on_update: Callable[[list[Event], list[str]], None],
        leagues: list[str] | None = None,
    ) -> None:
        """Stream live odds updates. Calls on_update(events, changed_urns) on each change."""
        if leagues is None:
            leagues = DEFAULT_LEAGUES

        if leagues:
            url_part = _resolve_league_url(leagues[0], sport)
        else:
            sport_url = SPORT_URL_PARTS.get(sport)
            if sport_url is None:
                return
            url_part = sport_url

        snapshot = Snapshot()
        initial = True

        async for changed in _connect_and_stream(snapshot, url_part):
            events = snapshot.build_events(sport)
            if initial:
                on_update(events, [])
                initial = False
            elif changed:
                on_update(events, changed)


def _slugify_league(name: str) -> str:
    """Convert a league name to a URL slug (lowercase, hyphens, no special chars)."""
    slug = name.lower()
    slug = slug.replace(".", "")
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def _resolve_league_url(league: str, sport: Sport) -> str:
    """Resolve a league name to a URL path, using LEAGUE_URLS or slugifying as fallback."""
    if league in LEAGUE_URLS:
        return LEAGUE_URLS[league]
    sport_segment = SPORT_URL_PARTS.get(sport, "/football")
    return f"{sport_segment}/{_slugify_league(league)}"


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

        site_url = f"{SITE_BASE}{url_part}"
        logger.info("Connecting to %s", site_url)
        await page.goto(site_url, wait_until="domcontentloaded", timeout=45_000)

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


async def _collect_all_markets(
    league_url_part: str,
    sport: Sport,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[Event]:
    """Load a league page, then visit each event detail page for full markets.

    Opens parallel browser tabs (up to _DETAIL_CONCURRENCY) to load each
    event's detail page, which triggers the WebSocket to deliver all markets
    for that event.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="en-CH", timezone_id="Europe/Zurich"
        )

        # Step 1: Load the league page to get the event list and detail links
        page = await context.new_page()
        league_snapshot = Snapshot()
        received = asyncio.Event()

        def on_ws(ws: Any) -> None:
            def on_received(payload: Any) -> None:
                if not isinstance(payload, bytes):
                    return
                _process_ws_message(payload, league_snapshot)
                if league_snapshot.events and league_snapshot.selections:
                    received.set()

            ws.on("framereceived", on_received)

        page.on("websocket", on_ws)

        site_url = f"{SITE_BASE}{league_url_part}"
        logger.info("Loading league page: %s", site_url)
        await page.goto(site_url, wait_until="domcontentloaded", timeout=45_000)

        try:
            await asyncio.wait_for(received.wait(), timeout=20.0)
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for league snapshot")

        await page.wait_for_timeout(2000)

        # Step 2: Extract event detail links from the DOM
        link_elements = await page.query_selector_all(_EVENT_LINK_SELECTOR)
        detail_urls: list[str] = []
        for el in link_elements:
            href = await el.get_attribute("href")
            if href:
                detail_urls.append(href)

        logger.info(
            "Found %d events, %d detail links",
            len(league_snapshot.events),
            len(detail_urls),
        )

        if not detail_urls:
            # Fallback: return basic events from the league page
            await browser.close()
            return league_snapshot.build_events(sport)

        await page.close()

        # Step 3: Visit each detail page in parallel tabs
        sem = asyncio.Semaphore(_DETAIL_CONCURRENCY)
        progress_count = 0
        progress_lock = asyncio.Lock()

        async def load_detail(url: str) -> Snapshot | None:
            nonlocal progress_count
            async with sem:
                detail_snap = Snapshot()
                detail_received = asyncio.Event()
                detail_page = await context.new_page()

                def on_detail_ws(ws: Any) -> None:
                    def on_detail_received(payload: Any) -> None:
                        if not isinstance(payload, bytes):
                            return
                        _process_ws_message(payload, detail_snap)
                        if detail_snap.events and detail_snap.selections:
                            detail_received.set()

                    ws.on("framereceived", on_detail_received)

                detail_page.on("websocket", on_detail_ws)

                full_url = (
                    url
                    if url.startswith("http")
                    else f"https://www.swisslos.ch{url}"
                )
                try:
                    await detail_page.goto(
                        full_url, wait_until="networkidle", timeout=45_000,
                    )
                    await asyncio.wait_for(
                        detail_received.wait(), timeout=20.0,
                    )
                    await detail_page.wait_for_timeout(2000)
                except (asyncio.TimeoutError, Exception) as exc:
                    logger.warning("Failed to load detail page %s: %s", url, exc)
                    await detail_page.close()
                    return None
                finally:
                    async with progress_lock:
                        progress_count += 1
                        if on_progress:
                            on_progress(progress_count, len(detail_urls))

                await detail_page.close()
                return detail_snap

        tasks = [load_detail(url) for url in detail_urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        await browser.close()

    # Step 4: For each detail snapshot, extract the focused event
    # (the one with the most markets — the detail page expands only that event)
    all_events: list[Event] = []
    seen_urns: set[str] = set()

    for result in results:
        if not isinstance(result, Snapshot):
            continue
        events = result.build_events(sport)
        if not events:
            continue
        best = max(events, key=lambda e: len(e.markets))
        if best.id not in seen_urns:
            seen_urns.add(best.id)
            all_events.append(best)

    logger.info(
        "Collected %d events with full markets", len(all_events),
    )
    return all_events


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

        site_url = f"{SITE_BASE}{url_part}"
        logger.info("Streaming from %s", site_url)
        await page.goto(site_url, wait_until="domcontentloaded", timeout=45_000)

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


def _format_event_line(
    event: Event, show_all_markets: bool = False,
) -> str:
    """Format a single event for display.

    By default only shows the "Final Result" (1X2) market.
    Pass show_all_markets=True to display every loaded market.
    """
    lines = [f"  {event.home_team} vs {event.away_team}"]
    lines.append(f"    League: {event.league} | Kickoff: {event.start_time}")

    markets = event.markets
    if not show_all_markets:
        markets = [m for m in markets if m.name.lower() in {"final result", "1x2", "3-weg", "3-way (regular playing time)"}]

    for market in markets:
        odds_str = " | ".join(
            f"{o.name}: {o.odds:.2f}" for o in market.outcomes
        )
        lines.append(f"    [{market.name}] {odds_str}")

    if not show_all_markets and len(event.markets) > len(markets):
        lines.append(
            f"    ... +{len(event.markets) - len(markets)} more markets"
        )

    return "\n".join(lines)


def _find_1x2_odds(event: Event) -> tuple[float, float, float] | None:
    """Extract 1X2 odds from an event's Final Result market.

    Returns (home, draw, away) odds or None if not found.
    """
    for market in event.markets:
        if market.name.lower() not in {"final result", "1x2", "3-weg", "3-way (regular playing time)"}:
            continue
        odds: dict[str, float] = {}
        for outcome in market.outcomes:
            name_lower = outcome.name.lower()
            if name_lower in ("draw", "x"):
                odds["X"] = outcome.odds
            elif event.home_team.lower() in name_lower or name_lower == "1":
                odds["1"] = outcome.odds
            elif event.away_team.lower() in name_lower or name_lower == "2":
                odds["2"] = outcome.odds
        if len(odds) == 3:
            return (odds["1"], odds["X"], odds["2"])
    return None


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    live_mode = "--live" in sys.argv
    basic_mode = "--basic" in sys.argv
    show_all_markets = "--all-markets" in sys.argv
    watch_mode = "--watch" in sys.argv
    leagues: list[str] | None = None

    # Parse --leagues argument
    for i, arg in enumerate(sys.argv):
        if arg == "--leagues" and i + 1 < len(sys.argv):
            leagues = [name.strip() for name in sys.argv[i + 1].split(",")]
            break

    # Parse --watch argument (optional team name value)
    watch_filter: str | None = None
    if watch_mode:
        for i, arg in enumerate(sys.argv):
            if arg == "--watch" and i + 1 < len(sys.argv):
                next_arg = sys.argv[i + 1]
                if not next_arg.startswith("--"):
                    watch_filter = next_arg
                break

    if leagues is None:
        leagues = DEFAULT_LEAGUES

    provider = SporttipProvider()
    league_display = ", ".join(leagues) if leagues else "all"

    if watch_mode:
        # Step 1: Quick fetch to get event list
        print(f"Loading events for: {league_display} ...\n")
        events = await provider.fetch_events(
            Sport.FOOTBALL, leagues=leagues, all_markets=False,
        )
        if not events:
            print("No events found.")
            return

        # Step 2: Select event
        selected: Event | None = None

        if watch_filter:
            # Auto-select by team name substring (case-insensitive)
            needle = watch_filter.lower()
            matches = [
                e for e in events
                if needle in e.home_team.lower() or needle in e.away_team.lower()
            ]
            if not matches:
                print(f'No event matching "{watch_filter}" found.\n')
                print("Available events:")
                for e in events:
                    print(f"  {e.home_team} vs {e.away_team}")
                return
            if len(matches) > 1:
                print(f'Multiple events match "{watch_filter}":')
                for i, e in enumerate(matches, 1):
                    print(f"  {i}. {e.home_team} vs {e.away_team} ({e.league})")
                try:
                    choice = int(input("\nSelect number: ")) - 1
                    selected = matches[choice]
                except (ValueError, IndexError):
                    print("Invalid selection.")
                    return
            else:
                selected = matches[0]
        else:
            # Interactive: show numbered list
            print("Select an event to watch:\n")
            for i, e in enumerate(events, 1):
                odds = _find_1x2_odds(e)
                odds_str = ""
                if odds:
                    odds_str = f"  (1: {odds[0]:.2f} | X: {odds[1]:.2f} | 2: {odds[2]:.2f})"
                print(f"  {i:>2}. {e.home_team} vs {e.away_team} — {e.league}{odds_str}")
            print()
            try:
                choice = int(input("Select number: ")) - 1
                selected = events[choice]
            except (ValueError, IndexError):
                print("Invalid selection.")
                return

        # Step 3: Stream updates filtered to the selected event
        event_urn = selected.id
        print(f"\nWatching: {selected.home_team} vs {selected.away_team} ({selected.league})")
        print("Press Ctrl+C to stop.\n")

        prev_odds: tuple[float, float, float] | None = None

        def on_watch_update(events: list[Event], changed: list[str]) -> None:
            nonlocal prev_odds
            # Find our event in the updated list
            target = next((e for e in events if e.id == event_urn), None)
            if target is None:
                return
            current = _find_1x2_odds(target)
            if current is None:
                return
            if current == prev_odds:
                return

            now = datetime.now().strftime("%H:%M:%S")

            # Determine which odds changed
            changes: list[str] = []
            if prev_odds is not None:
                if current[0] != prev_odds[0]:
                    changes.append("1")
                if current[1] != prev_odds[1]:
                    changes.append("X")
                if current[2] != prev_odds[2]:
                    changes.append("2")

            line = f"{now}  1: {current[0]:<5.2f} |  X: {current[1]:<5.2f} |  2: {current[2]:<5.2f}"
            if changes:
                line += f"  \u2190 {', '.join(changes)} changed"

            print(line)
            prev_odds = current

        try:
            await provider.stream_updates(Sport.FOOTBALL, on_watch_update, leagues=leagues)
        except KeyboardInterrupt:
            print("\nStopped.")

    elif live_mode:
        mode_display = "basic (1X2 only)" if basic_mode else "full (all markets)"
        print(f"Leagues: {league_display}")
        print(f"Mode: {mode_display}\n")
        print("=== Sporttip Live Odds Stream (Ctrl+C to stop) ===\n")
        update_count = 0

        def on_update(events: list[Event], changed: list[str]) -> None:
            nonlocal update_count
            update_count += 1
            if not changed:
                print(f"--- Initial snapshot: {len(events)} events ---\n")
                for event in events:
                    print(_format_event_line(event, show_all_markets))
                    print()
            else:
                print(f"\n--- Update #{update_count}: {len(changed)} odds changed ---")
                print(f"    Total events: {len(events)}")

        try:
            await provider.stream_updates(Sport.FOOTBALL, on_update, leagues=leagues)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        mode_display = "basic (1X2 only)" if basic_mode else "full (all markets)"
        print(f"Leagues: {league_display}")
        print(f"Mode: {mode_display}\n")
        print("=== Sporttip Current Odds ===\n")

        def on_progress(current: int, total: int) -> None:
            print(f"\r  Loading event details... ({current}/{total})", end="", flush=True)

        events = await provider.fetch_events(
            Sport.FOOTBALL,
            leagues=leagues,
            all_markets=not basic_mode,
            on_progress=on_progress if not basic_mode else None,
        )
        if not basic_mode:
            print()  # newline after progress
        print(f"Found {len(events)} events:\n")
        for event in events:
            print(_format_event_line(event, show_all_markets))
            print()


if __name__ == "__main__":
    asyncio.run(main())
