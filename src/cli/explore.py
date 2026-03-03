"""Interactive Sporttip odds explorer.

Browse sports, leagues, and events interactively. View all markets for an
event (one-shot) or watch them update in real-time.

Usage:
    python -m src.cli.explore
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime
from typing import Any

from playwright.async_api import BrowserContext, Page, async_playwright

from src.arbitrage.calculator import ArbOpportunity, find_underdog_arbs
from src.models.events import Event, Sport
from src.providers.sporttip import (
    _EVENT_LINK_SELECTOR,
    LEAGUE_URLS,
    SITE_BASE,
    SPORT_FROM_SLUG,
    SPORT_URL_PARTS,
    Snapshot,
    _connect_and_collect,
    _connect_and_stream,
    _find_1x2_odds,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Discovery — scrape sport / category links from the Sporttip DOM
# ---------------------------------------------------------------------------


async def discover_sports(page: Page) -> list[dict[str, str]]:
    """Scrape top-level sport links from the Sporttip navigation.

    Returns a list of ``{"name": "Football", "url_part": "/football"}``.
    Falls back to the hardcoded ``SPORT_URL_PARTS`` if scraping yields nothing.
    """
    await page.goto(SITE_BASE, wait_until="networkidle", timeout=45_000)

    link_els = await page.query_selector_all('a[href*="/en/sporttip/sports/"]')
    sports: list[dict[str, str]] = []
    seen: set[str] = set()

    for el in link_els:
        href = await el.get_attribute("href")
        text = (await el.inner_text()).strip()
        if not href or not text:
            continue
        # Extract the path after the site base (e.g. "/football")
        url_part = href.split("/en/sporttip/sports")[-1]
        # Only keep top-level segments (single path component)
        if not url_part or url_part.count("/") != 1:
            continue
        if url_part in seen:
            continue
        seen.add(url_part)
        sports.append({"name": text, "url_part": url_part})

    if sports:
        return sports

    # Fallback to hardcoded list
    logger.info("DOM scraping returned no sports — using hardcoded list")
    return [
        {"name": sport.value.replace("_", " ").title(), "url_part": url_part}
        for sport, url_part in SPORT_URL_PARTS.items()
    ]


async def discover_categories(
    page: Page, sport_url_part: str,
) -> list[dict[str, str]]:
    """Scrape league/category links for a given sport.

    Returns a list of dicts with "name" and "url_part" keys.
    Falls back to matching entries in ``LEAGUE_URLS`` if scraping yields nothing.
    """
    await page.goto(
        f"{SITE_BASE}{sport_url_part}",
        wait_until="networkidle",
        timeout=45_000,
    )

    link_els = await page.query_selector_all(
        f'a[href*="/en/sporttip/sports{sport_url_part}/"]'
    )
    categories: list[dict[str, str]] = []
    seen: set[str] = set()

    for el in link_els:
        href = await el.get_attribute("href")
        text = (await el.inner_text()).strip()
        if not href or not text:
            continue
        url_part = href.split("/en/sporttip/sports")[-1]
        if url_part in seen or url_part == sport_url_part:
            continue
        seen.add(url_part)
        categories.append({"name": text, "url_part": url_part})

    if categories:
        return categories

    # Fallback: return hardcoded leagues that match this sport
    logger.info("DOM scraping returned no categories — using hardcoded list")
    return [
        {"name": name, "url_part": url}
        for name, url in LEAGUE_URLS.items()
        if url.startswith(sport_url_part)
    ]


async def discover_event_links(page: Page) -> list[dict[str, str]]:
    """Scrape event detail links from the current page.

    Returns a list of ``{"url": "/en/sporttip/sports/football/..."}``.
    Must be called *after* navigating to a league/category page.
    """
    link_els = await page.query_selector_all(_EVENT_LINK_SELECTOR)
    links: list[dict[str, str]] = []
    seen: set[str] = set()

    for el in link_els:
        href = await el.get_attribute("href")
        if href and href not in seen:
            seen.add(href)
            links.append({"url": href})

    return links


# ---------------------------------------------------------------------------
# Event loading helpers (reuse sporttip.py internals)
# ---------------------------------------------------------------------------


async def load_events(
    category_url_part: str, sport: Sport,
) -> tuple[list[Event], Snapshot]:
    """Load events for a category via WebSocket snapshot (basic mode, fast)."""
    snapshot = Snapshot()
    await _connect_and_collect(snapshot, category_url_part)
    events = snapshot.build_events(sport)
    return events, snapshot


async def fetch_event_detail(
    context: BrowserContext,
    detail_url: str,
    sport: Sport,
) -> Event | None:
    """Load a single event's detail page to get all markets."""
    snapshot = Snapshot()
    received = asyncio.Event()
    page = await context.new_page()

    def on_ws(ws: Any) -> None:
        from src.providers.sporttip import _process_ws_message

        def on_received(payload: Any) -> None:
            if not isinstance(payload, bytes):
                return
            _process_ws_message(payload, snapshot)
            if snapshot.events and snapshot.selections:
                received.set()

        ws.on("framereceived", on_received)

    page.on("websocket", on_ws)

    full_url = detail_url if detail_url.startswith("http") else f"https://www.swisslos.ch{detail_url}"
    try:
        await page.goto(full_url, wait_until="networkidle", timeout=45_000)
        await asyncio.wait_for(received.wait(), timeout=20.0)
        await page.wait_for_timeout(2000)
    except (asyncio.TimeoutError, Exception) as exc:
        logger.warning("Failed to load detail page %s: %s", detail_url, exc)
        await page.close()
        return None

    await page.close()

    events = snapshot.build_events(sport)
    if events:
        return max(events, key=lambda e: len(e.markets))
    return None


# ---------------------------------------------------------------------------
# Display formatting
# ---------------------------------------------------------------------------


def format_events_table(events: list[Event]) -> str:
    """Format events as a numbered table with primary market (1X2) odds."""
    lines: list[str] = []
    lines.append(f"  {'#':>3}  {'Match':<40} {'1':>6} {'X':>6} {'2':>6}  Kickoff")
    lines.append("  " + "-" * 78)

    for i, event in enumerate(events, 1):
        match_str = f"{event.home_team} vs {event.away_team}"
        if len(match_str) > 40:
            match_str = match_str[:37] + "..."

        odds = _find_1x2_odds(event)
        if odds:
            o1, ox, o2 = f"{odds[0]:.2f}", f"{odds[1]:.2f}", f"{odds[2]:.2f}"
        else:
            o1 = ox = o2 = "  -  "

        kickoff = event.start_time.strftime("%a %H:%M")
        lines.append(f"  {i:>3}  {match_str:<40} {o1:>6} {ox:>6} {o2:>6}  {kickoff}")

    return "\n".join(lines)


def format_all_markets(event: Event) -> str:
    """Format all markets for an event in a readable layout."""
    lines: list[str] = []
    lines.append(f"{event.home_team} vs {event.away_team}")
    kickoff = event.start_time.strftime("%a %d %b %H:%M")
    lines.append(f"  League: {event.league} | Kickoff: {kickoff}")
    lines.append("")

    for market in event.markets:
        odds_parts = [f"{o.name}: {o.odds:.2f}" for o in market.outcomes]
        odds_str = " | ".join(odds_parts)
        lines.append(f"  [{market.name}]")
        lines.append(f"    {odds_str}")

    if not event.markets:
        lines.append("  No markets available.")

    return "\n".join(lines)


def format_arb_scan(opportunities: list[ArbOpportunity]) -> str:
    """Format arbitrage scan results as a sorted table."""
    if not opportunities:
        return "  No opportunities found in the current odds range."

    from src.arbitrage.calculator import calculate_margin

    # Sort by ascending overround (lowest = closest to fair odds)
    sorted_opps = sorted(opportunities, key=lambda o: o.overround)

    # Build row data
    rows: list[dict[str, str]] = []
    for opp in sorted_opps:
        fav_label = opp.favorite
        match_name = f"{opp.home_team} vs {opp.away_team}"

        # Breakeven and example margins — substitute example odds for the favorite
        if opp.favorite == "1":
            margin_at_example = calculate_margin(opp.example_fav_odds, opp.draw_odds, opp.away_odds)
        elif opp.favorite == "2":
            margin_at_example = calculate_margin(opp.home_odds, opp.draw_odds, opp.example_fav_odds)
        else:  # "X"
            margin_at_example = calculate_margin(opp.home_odds, opp.example_fav_odds, opp.away_odds)

        rows.append({
            "Event": match_name,
            "Time": opp.start_time,
            "1": f"{opp.home_odds:.2f}",
            "X": f"{opp.draw_odds:.2f}",
            "2": f"{opp.away_odds:.2f}",
            "Fav": fav_label,
            "Over%": f"{opp.overround * 100:+.1f}%",
            "BrkEvn": f"{opp.target_fav_odds:.2f}",
            "Tgt+5%": f"{opp.example_fav_odds:.2f}",
            "Profit": f"{margin_at_example * 100:.1f}%",
        })

    # Column widths — use max of header and data
    columns = ["Event", "Time", "1", "X", "2", "Fav", "Over%", "BrkEvn", "Tgt+5%", "Profit"]
    widths: dict[str, int] = {}
    for col in columns:
        widths[col] = max(len(col), *(len(r[col]) for r in rows))

    # Build table
    header = "  ".join(
        col.rjust(widths[col]) if col != "Event" else col.ljust(widths[col])
        for col in columns
    )
    separator = "  ".join("-" * widths[col] for col in columns)

    lines: list[str] = [
        f"Found {len(sorted_opps)} opportunity(ies), sorted by overround (ascending):\n",
        f"  {header}",
        f"  {separator}",
    ]

    for row in rows:
        line = "  ".join(
            row[col].rjust(widths[col]) if col != "Event" else row[col].ljust(widths[col])
            for col in columns
        )
        lines.append(f"  {line}")

    lines.append("")

    # Legend
    lines.append("  Over%  = overround (how far implied probabilities exceed 100%)")
    lines.append("  BrkEvn = favorite odds needed in-play for breakeven")
    lines.append("  Tgt+5% = favorite odds for 5% profit margin")

    return "\n".join(lines)


def _format_watch_snapshot(
    event: Event,
    prev_odds: dict[str, float],
    num_changed: int,
) -> str:
    """Format all markets for watch mode, marking changed odds with *."""
    now = datetime.now().strftime("%H:%M:%S")
    lines: list[str] = []

    if num_changed > 0:
        lines.append(f"--- {now} ({num_changed} odds changed) ---")
    else:
        lines.append(f"--- {now} ---")

    for market in event.markets:
        parts: list[str] = []
        for outcome in market.outcomes:
            key = f"{market.name}:{outcome.name}"
            val = f"{outcome.odds:.2f}"
            if key in prev_odds and prev_odds[key] != outcome.odds:
                val += "*"
            parts.append(f"{outcome.name}: {val}")
        name = market.name
        if len(name) > 22:
            name = name[:19] + "..."
        lines.append(f"  {name:<22} | {' '.join(parts)}")

    return "\n".join(lines)


def _snapshot_odds(event: Event) -> dict[str, float]:
    """Build a dict of market:outcome -> odds for change tracking."""
    result: dict[str, float] = {}
    for market in event.markets:
        for outcome in market.outcomes:
            result[f"{market.name}:{outcome.name}"] = outcome.odds
    return result


# ---------------------------------------------------------------------------
# Interactive menu helpers
# ---------------------------------------------------------------------------


def _sport_from_url(url_part: str) -> Sport:
    """Resolve a URL slug like '/football' to a Sport enum value."""
    slug = url_part.lstrip("/").split("/")[0]
    sport = SPORT_FROM_SLUG.get(slug)
    if sport is None:
        logger.warning("Unknown sport slug '%s' — defaulting to FOOTBALL", slug)
        return Sport.FOOTBALL
    return sport


def prompt_choice(
    items: list[str],
    prompt: str = "Select",
    allow_back: bool = True,
) -> int | str | None:
    """Display a numbered list and get user input.

    Returns:
        int — selected index (0-based)
        str — text filter typed by the user
        None — user chose to go back or quit
    """
    print()
    for i, item in enumerate(items, 1):
        print(f"  {i:>3}. {item}")

    controls = []
    if allow_back:
        controls.append("'b' back")
    controls.append("'q' quit")
    hint = f" ({', '.join(controls)})"
    print()

    try:
        raw = input(f"{prompt}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None

    if not raw or raw.lower() == "q":
        return None
    if raw.lower() == "b" and allow_back:
        return -1  # type: ignore[return-value]  # sentinel for "back"

    try:
        idx = int(raw) - 1
        if 0 <= idx < len(items):
            return idx
        print(f"  Number out of range (1-{len(items)}).")
        return prompt_choice(items, prompt, allow_back)
    except ValueError:
        # Treat as text filter
        return raw


# ---------------------------------------------------------------------------
# Interactive loop
# ---------------------------------------------------------------------------


async def interactive_loop(page: Page, context: BrowserContext) -> None:
    """Main interactive drill-down loop: sports -> categories -> events -> action."""

    while True:
        # --- Sport selection ---
        print("\nLoading sports...")
        sports = await discover_sports(page)
        if not sports:
            print("No sports found.")
            return

        choice = prompt_choice(
            [s["name"] for s in sports],
            prompt="Select sport",
            allow_back=False,
        )
        if choice is None:
            return
        if isinstance(choice, str):
            # Text filter on sports
            filtered = [s for s in sports if choice.lower() in s["name"].lower()]
            if len(filtered) == 1:
                selected_sport = filtered[0]
            elif filtered:
                choice2 = prompt_choice(
                    [s["name"] for s in filtered],
                    prompt="Select sport",
                    allow_back=False,
                )
                if choice2 is None or isinstance(choice2, str):
                    continue
                selected_sport = filtered[choice2]
            else:
                print(f'  No sport matching "{choice}".')
                continue
        else:
            selected_sport = sports[choice]

        sport_enum = _sport_from_url(selected_sport["url_part"])

        # --- Category selection loop ---
        while True:
            print(f"\nLoading categories for {selected_sport['name']}...")
            categories = await discover_categories(page, selected_sport["url_part"])
            if not categories:
                print("No categories found.")
                break

            print("\n  Type 'a' for arbitrage scan across ALL leagues")

            choice = prompt_choice(
                [c["name"] for c in categories],
                prompt="Select category",
            )
            if choice is None:
                return
            if choice == -1:
                break  # back to sport selection
            if isinstance(choice, str) and choice.lower() == "a":
                # Load events from every category and run arb scan
                print("\n=== Arbitrage Scanner — All Leagues ===")
                all_events: list[Event] = []
                seen_ids: set[str] = set()
                for cat in categories:
                    print(f"  Loading {cat['name']}...")
                    cat_events, _ = await load_events(cat["url_part"], sport_enum)
                    for ev in cat_events:
                        if ev.id not in seen_ids:
                            seen_ids.add(ev.id)
                            all_events.append(ev)
                print(f"\n  Loaded {len(all_events)} events across {len(categories)} leagues.\n")
                opps = find_underdog_arbs(all_events)
                print(format_arb_scan(opps))
                input("Press Enter to continue...")
                continue
            if isinstance(choice, str):
                filtered = [c for c in categories if choice.lower() in c["name"].lower()]
                if len(filtered) == 1:
                    selected_cat = filtered[0]
                elif filtered:
                    choice2 = prompt_choice(
                        [c["name"] for c in filtered],
                        prompt="Select category",
                    )
                    if choice2 is None or isinstance(choice2, str):
                        continue
                    if choice2 == -1:
                        break
                    selected_cat = filtered[choice2]
                else:
                    print(f'  No category matching "{choice}".')
                    continue
            else:
                selected_cat = categories[choice]

            # --- Events view loop ---
            while True:
                print(f"\nLoading events for {selected_cat['name']}...")
                events, _snapshot = await load_events(selected_cat["url_part"], sport_enum)

                # Also grab event detail links for later
                # We need to navigate to the category page to scrape them
                await page.goto(
                    f"{SITE_BASE}{selected_cat['url_part']}",
                    wait_until="networkidle",
                    timeout=45_000,
                )
                event_links = await discover_event_links(page)

                if not events:
                    print("No events found in this category.")
                    break

                print(f"\n{selected_cat['name']} — {len(events)} events:\n")
                print(format_events_table(events))
                print("\n  Type 'a' for arbitrage scan")

                choice = prompt_choice(
                    [f"{e.home_team} vs {e.away_team}" for e in events],
                    prompt="Select event (number/filter)",
                )
                if choice is None:
                    return
                if choice == -1:
                    break  # back to category selection
                if isinstance(choice, str) and choice.lower() == "a":
                    print("\n=== Arbitrage Scanner ===\n")
                    opps = find_underdog_arbs(events)
                    print(format_arb_scan(opps))
                    input("Press Enter to continue...")
                    continue
                if isinstance(choice, str):
                    # Text filter on events
                    needle = choice.lower()
                    filtered_events = [
                        (i, e) for i, e in enumerate(events)
                        if needle in e.home_team.lower()
                        or needle in e.away_team.lower()
                    ]
                    if not filtered_events:
                        print(f'  No event matching "{choice}".')
                        continue
                    if len(filtered_events) == 1:
                        selected_idx = filtered_events[0][0]
                    else:
                        choice2 = prompt_choice(
                            [f"{e.home_team} vs {e.away_team}" for _, e in filtered_events],
                            prompt="Select event",
                        )
                        if choice2 is None or isinstance(choice2, str):
                            continue
                        if choice2 == -1:
                            break
                        selected_idx = filtered_events[choice2][0]
                else:
                    selected_idx = choice

                selected_event = events[selected_idx]

                # Find the detail link for this event
                detail_url: str | None = None
                if selected_idx < len(event_links):
                    detail_url = event_links[selected_idx]["url"]

                # --- Event action menu ---
                await event_action_menu(
                    context, selected_event, detail_url,
                    selected_cat["url_part"], sport_enum,
                )


async def event_action_menu(
    context: BrowserContext,
    event: Event,
    detail_url: str | None,
    category_url_part: str,
    sport: Sport,
) -> None:
    """Show action menu for a selected event: fetch all markets or watch."""
    print(f"\n{event.home_team} vs {event.away_team}")
    print(f"  {event.league} — {event.start_time.strftime('%a %d %b %H:%M')}")

    while True:
        print("\n  (f) Fetch all markets (one-shot)")
        print("  (w) Watch all markets (live stream)")
        print("  (b) Back")

        try:
            raw = input("\nChoice: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if raw == "b":
            return

        if raw == "f":
            if detail_url:
                print("\nLoading all markets...")
                detailed = await fetch_event_detail(context, detail_url, sport)
                if detailed:
                    print(f"\n{format_all_markets(detailed)}")
                    print(f"\n  ({len(detailed.markets)} markets total)")
                else:
                    print("\n  Could not load event details.")
            else:
                # No detail URL — show what we have from the league page
                print(f"\n{format_all_markets(event)}")
                print("\n  (Detail link not found — showing basic markets only)")
            input("\nPress Enter to continue...")

        elif raw == "w":
            url_part = category_url_part
            if detail_url:
                # Use the detail page URL for richer data
                url_part = detail_url.split("/en/sporttip/sports")[-1]

            await watch_event(url_part, sport, event.id)

        else:
            print("  Invalid choice. Use 'f', 'w', or 'b'.")


async def watch_event(
    url_part: str, sport: Sport, event_urn: str,
) -> None:
    """Stream all markets for a specific event, printing updates."""
    print("\nConnecting... (Ctrl+C to stop)\n")

    snapshot = Snapshot()
    prev_odds: dict[str, float] = {}
    first = True

    try:
        async for changed in _connect_and_stream(snapshot, url_part):
            events = snapshot.build_events(sport)
            target = next((e for e in events if e.id == event_urn), None)
            if target is None:
                continue

            current_odds = _snapshot_odds(target)
            num_changed = sum(
                1 for k, v in current_odds.items()
                if k in prev_odds and prev_odds[k] != v
            )

            if first or num_changed > 0:
                output = _format_watch_snapshot(target, prev_odds, 0 if first else num_changed)
                print(output)
                print()
                first = False

            prev_odds = current_odds
    except KeyboardInterrupt:
        print("\nStopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    level = logging.DEBUG if "--debug" in sys.argv else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    print("=== Sporttip Explorer ===")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="en-CH", timezone_id="Europe/Zurich",
        )
        page = await context.new_page()

        try:
            await interactive_loop(page, context)
        except KeyboardInterrupt:
            pass
        finally:
            print("\nClosing browser...")
            await browser.close()

    print("Bye!")


def cli() -> None:
    """Sync entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
