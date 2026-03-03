"""Standalone CLI odds monitor with threshold alerts.

Browse to a category, pick events/markets/outcomes, set threshold alerts,
then stream live odds and get macOS notifications when thresholds are crossed.

Usage:
    python -m src.cli.monitor
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from playwright.async_api import Page, async_playwright

from src.cli.explore import (
    _snapshot_odds,
    _sport_from_url,
    discover_categories,
    discover_sports,
    format_events_table,
    load_events,
    prompt_choice,
)
from src.models.events import Event, Sport
from src.providers.sporttip import LIVE_BASE, Snapshot, _connect_and_collect, _connect_and_stream

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class Direction(str, Enum):
    GTE = ">="
    LTE = "<="


class AlertStatus(str, Enum):
    WATCHING = "watching"
    TRIGGERED = "TRIGGERED"
    COOLDOWN = "cooldown"


@dataclass
class Alert:
    """A single threshold alert on a specific outcome's odds."""

    event_id: str
    event_label: str
    market_name: str
    outcome_name: str
    odds_key: str  # "{market}:{outcome}" — matches _snapshot_odds format
    direction: Direction
    threshold: float
    status: AlertStatus = AlertStatus.WATCHING
    current_odds: float = 0.0
    last_triggered: float = field(default=0.0, repr=False)
    cooldown_seconds: float = 60.0

    def check(self, odds: float) -> bool:
        """Return True if threshold is crossed and alert should fire."""
        self.current_odds = odds
        if self.status != AlertStatus.WATCHING:
            return False
        if self.direction == Direction.GTE:
            return odds >= self.threshold
        return odds <= self.threshold

    def fire(self) -> None:
        """Mark the alert as triggered."""
        self.status = AlertStatus.TRIGGERED
        self.last_triggered = time.monotonic()

    def maybe_exit_cooldown(self) -> None:
        """Transition TRIGGERED -> COOLDOWN -> WATCHING based on elapsed time.

        When odds cross back past the threshold, reset to WATCHING immediately.
        """
        if self.status == AlertStatus.TRIGGERED:
            self.status = AlertStatus.COOLDOWN

        if self.status == AlertStatus.COOLDOWN:
            elapsed = time.monotonic() - self.last_triggered
            if elapsed >= self.cooldown_seconds:
                self.status = AlertStatus.WATCHING
            # Also reset if odds moved back across the threshold
            elif self._odds_crossed_back():
                self.status = AlertStatus.WATCHING

    def _odds_crossed_back(self) -> bool:
        if self.direction == Direction.GTE:
            return self.current_odds < self.threshold
        return self.current_odds > self.threshold


# ---------------------------------------------------------------------------
# macOS notification
# ---------------------------------------------------------------------------


def _escape_applescript(text: str) -> str:
    """Escape a string for use inside AppleScript double quotes."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def send_notification(title: str, message: str, sound: str = "Glass") -> None:
    """Send a macOS notification via osascript (non-blocking)."""
    safe_title = _escape_applescript(title)
    safe_msg = _escape_applescript(message)
    script = (
        f'display notification "{safe_msg}" '
        f'with title "{safe_title}" '
        f'sound name "{sound}"'
    )
    try:
        subprocess.Popen(  # noqa: S603
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        logger.debug("osascript not found — skipping notification")


# ---------------------------------------------------------------------------
# Live summary table
# ---------------------------------------------------------------------------


def render_summary(alerts: list[Alert], update_count: int) -> str:
    """Render the live alert summary table."""
    now = datetime.now().strftime("%H:%M:%S")
    lines: list[str] = [
        f"=== Odds Monitor === (updates: {update_count}, last: {now})",
        "",
    ]

    # Column headers
    hdr = (
        f"  {'#':>3}  {'Event':<30} {'Market':<18} {'Outcome':<16}"
        f" {'Odds':>5} {'Dir':>3} {'Thr':>6}   {'Status':<9}"
    )
    lines.append(hdr)
    lines.append("  " + "-" * (len(hdr) - 2))

    for i, a in enumerate(alerts, 1):
        ev = a.event_label[:30] if len(a.event_label) > 30 else a.event_label
        mk = a.market_name[:18] if len(a.market_name) > 18 else a.market_name
        oc = a.outcome_name[:16] if len(a.outcome_name) > 16 else a.outcome_name
        odds_str = f"{a.current_odds:.2f}" if a.current_odds else "  -  "
        lines.append(
            f"  {i:>3}  {ev:<30} {mk:<18} {oc:<16}"
            f" {odds_str:>5} {a.direction.value:>3} {a.threshold:>6.2f}   {a.status.value:<9}"
        )

    lines.append("")
    lines.append("  Press Ctrl+C to stop.")
    return "\n".join(lines)


def _redraw(text: str) -> None:
    """Clear the terminal and redraw the given text."""
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Interactive setup
# ---------------------------------------------------------------------------


def _filter_pick(
    items: list[dict[str, str]],
    key: str,
    needle: str,
    prompt_label: str,
) -> dict[str, str] | None:
    """Filter *items* by *needle* on *key* and let the user pick if ambiguous."""
    filtered = [it for it in items if needle.lower() in it[key].lower()]
    if len(filtered) == 1:
        return filtered[0]
    if filtered:
        choice2 = prompt_choice(
            [it[key] for it in filtered],
            prompt=prompt_label,
            allow_back=False,
        )
        if isinstance(choice2, int) and 0 <= choice2 < len(filtered):
            return filtered[choice2]
    else:
        print(f'  No match for "{needle}".')
    return None


async def _discover_live_sports(page: Page) -> list[dict[str, str]]:
    """Scrape sport links from the Sporttip live page."""
    await page.goto(LIVE_BASE, wait_until="networkidle", timeout=45_000)
    link_els = await page.query_selector_all('a[href*="/en/sporttip/live/"]')
    sports: list[dict[str, str]] = []
    seen: set[str] = set()
    for el in link_els:
        href = await el.get_attribute("href") or ""
        text = (await el.inner_text()).strip()
        if not href or not text:
            continue
        # Extract path after /live/ (e.g. "football")
        suffix = href.split("/en/sporttip/live/")[-1]
        # Only keep top-level sport slugs (no query params, no sub-paths)
        if not suffix or "/" in suffix or "?" in suffix:
            continue
        if suffix in seen:
            continue
        seen.add(suffix)
        name, _count = _split_name_count(text)
        sports.append({"name": name, "url_part": f"/live/{suffix}", "slug": suffix})
    return sports


def _split_name_count(text: str) -> tuple[str, str]:
    """Split "Football\\n3" into ("Football", "3")."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 2 and lines[-1].isdigit():
        return " ".join(lines[:-1]), lines[-1]
    return " ".join(lines), ""


async def _load_live_events(
    url: str, sport: Sport,
) -> list[Event]:
    """Load events from a full live URL via WebSocket snapshot."""
    snapshot = Snapshot()
    await _connect_and_collect(snapshot, url)
    return snapshot.build_events(sport)


async def setup_alerts(page: Page) -> tuple[list[Alert], str, Sport] | None:
    """Interactive flow: pick sport -> category -> events -> markets -> thresholds.

    Returns (alerts, category_url_part, sport) or None if user quits.
    """
    # --- Sport selection (with Live as first option) ---
    print("\nLoading sports...")
    sports = await discover_sports(page)
    if not sports:
        print("No sports found.")
        return None

    # Prepend "Live" as the first option
    live_entry: dict[str, str] = {"name": "** Live **", "url_part": "/live"}
    all_sports = [live_entry] + sports

    choice: int | str | None = None
    selected_sport: dict[str, str] | None = None
    while selected_sport is None:
        choice = prompt_choice(
            [s["name"] for s in all_sports],
            prompt="Select sport",
            allow_back=False,
            extras=[s.get("count", "") for s in all_sports],
            extras_header="Events",
        )
        if choice is None:
            return None
        if isinstance(choice, str):
            selected_sport = _filter_pick(all_sports, "name", choice, "Select sport")
            continue
        selected_sport = all_sports[choice]

    is_live = selected_sport is live_entry

    # --- Live path: pick live sport -> load events directly ---
    if is_live:
        print("\nLoading live sports...")
        live_sports = await _discover_live_sports(page)
        if not live_sports:
            print("No live sports found.")
            return None

        selected_live: dict[str, str] | None = None
        while selected_live is None:
            choice = prompt_choice(
                [ls["name"] for ls in live_sports],
                prompt="Select live sport",
                allow_back=False,
            )
            if choice is None:
                return None
            if isinstance(choice, str):
                selected_live = _filter_pick(
                    live_sports, "name", choice, "Select live sport",
                )
                continue
            selected_live = live_sports[choice]

        sport_enum = _sport_from_url(f"/{selected_live['slug']}")
        live_url = f"{LIVE_BASE}/{selected_live['slug']}"
        category_url_part = live_url  # full URL — sporttip.py handles this

        print(f"\nLoading live events for {selected_live['name']}...")
        events = await _load_live_events(live_url, sport_enum)
        if not events:
            print("No live events found.")
            return None

        cat_label = f"Live {selected_live['name']}"
        print(f"\nAll alerts must be within: {cat_label}")
        print("  (one WebSocket connection)\n")

        return _pick_alerts(events, cat_label, category_url_part, sport_enum)

    # --- Regular path: sport -> category -> events ---
    sport_enum = _sport_from_url(selected_sport["url_part"])

    print(f"\nLoading categories for {selected_sport['name']}...")
    categories = await discover_categories(page, selected_sport["url_part"])
    if not categories:
        print("No categories found.")
        return None

    selected_cat: dict[str, str] | None = None
    while selected_cat is None:
        choice = prompt_choice(
            [c["name"] for c in categories],
            prompt="Select category",
            allow_back=False,
            extras=[c.get("count", "") for c in categories],
            extras_header="Events",
        )
        if choice is None:
            return None
        if isinstance(choice, str):
            selected_cat = _filter_pick(categories, "name", choice, "Select category")
            continue
        selected_cat = categories[choice]

    # Check for sub-categories (e.g. International -> International Rest)
    category_url_part = selected_cat["url_part"]
    print(f"\nChecking for sub-categories in {selected_cat['name']}...")
    sub_cats = await discover_categories(page, category_url_part)
    sub_cats = [sc for sc in sub_cats if sc["url_part"] != category_url_part]

    if sub_cats:
        print(f"  Found {len(sub_cats)} sub-category(ies).")
        selected_sub: dict[str, str] | None = None
        while selected_sub is None:
            choice = prompt_choice(
                [sc["name"] for sc in sub_cats],
                prompt="Select sub-category (or 'q' to use parent)",
                allow_back=False,
                extras=[sc.get("count", "") for sc in sub_cats],
                extras_header="Events",
            )
            if choice is None:
                break
            if isinstance(choice, str):
                selected_sub = _filter_pick(
                    sub_cats, "name", choice, "Select sub-category",
                )
                continue
            selected_sub = sub_cats[choice]

        if selected_sub is not None:
            selected_cat = selected_sub
            category_url_part = selected_sub["url_part"]

    print(f"\nAll alerts must be within: {selected_cat['name']}")
    print("  (one WebSocket connection per category)\n")

    # Load events for the regular path
    print(f"Loading events for {selected_cat['name']}...")
    events, _ = await load_events(category_url_part, sport_enum)
    if not events:
        print("No events found.")
        return None

    return _pick_alerts(events, selected_cat["name"], category_url_part, sport_enum)


def _pick_alerts(
    events: list[Event],
    label: str,
    category_url_part: str,
    sport: Sport,
) -> tuple[list[Alert], str, Sport] | None:
    """Event -> market -> outcome -> threshold loop. Shared by live & regular paths."""
    alerts: list[Alert] = []

    while True:
        print(f"\n{label} — {len(events)} events:\n")
        print(format_events_table(events))

        event_labels = [
            f"{e.home_team} vs {e.away_team}" if e.away_team else e.home_team
            for e in events
        ]
        ev_choice: int | str | None = prompt_choice(
            event_labels,
            prompt="Select event",
            allow_back=False,
        )
        if ev_choice is None:
            if alerts:
                break  # proceed with existing alerts
            return None
        if isinstance(ev_choice, str):
            needle = ev_choice.lower()
            filtered_events = [
                (i, e) for i, e in enumerate(events)
                if needle in e.home_team.lower() or needle in e.away_team.lower()
            ]
            if not filtered_events:
                print(f'  No event matching "{ev_choice}".')
                continue
            if len(filtered_events) == 1:
                ev_choice = filtered_events[0][0]
            else:
                choice2 = prompt_choice(
                    [event_labels[i] for i, _ in filtered_events],
                    prompt="Select event",
                    allow_back=False,
                )
                if not isinstance(choice2, int):
                    continue
                ev_choice = filtered_events[choice2][0]
        selected_event = events[ev_choice]
        event_label = event_labels[ev_choice]

        # --- Market selection ---
        if not selected_event.markets:
            print("  No markets for this event.")
            continue

        market_names = [m.name for m in selected_event.markets]
        mk_choice = prompt_choice(
            market_names,
            prompt="Select market",
            allow_back=False,
        )
        if mk_choice is None or isinstance(mk_choice, str):
            continue
        selected_market = selected_event.markets[mk_choice]

        # --- Outcome selection ---
        outcome_labels = [
            f"{o.name} ({o.odds:.2f})" for o in selected_market.outcomes
        ]
        oc_choice = prompt_choice(
            outcome_labels,
            prompt="Select outcome",
            allow_back=False,
        )
        if oc_choice is None or isinstance(oc_choice, str):
            continue
        selected_outcome = selected_market.outcomes[oc_choice]

        # --- Direction and threshold ---
        dir_choice = prompt_choice(
            [">= (odds rise to or above)", "<= (odds drop to or below)"],
            prompt="Direction",
            allow_back=False,
        )
        if dir_choice is None or isinstance(dir_choice, str):
            continue
        direction = Direction.GTE if dir_choice == 0 else Direction.LTE

        try:
            raw = input("Threshold odds value: ").strip()
            threshold = float(raw)
        except (ValueError, EOFError, KeyboardInterrupt):
            print("  Invalid value.")
            continue

        odds_key = f"{selected_market.name}:{selected_outcome.name}"
        alert = Alert(
            event_id=selected_event.id,
            event_label=event_label,
            market_name=selected_market.name,
            outcome_name=selected_outcome.name,
            odds_key=odds_key,
            direction=direction,
            threshold=threshold,
        )
        alerts.append(alert)
        print(
            f"\n  Alert #{len(alerts)}: {event_label} — "
            f"{selected_market.name} / {selected_outcome.name} "
            f"{direction.value} {threshold:.2f}"
        )

        # Add another?
        try:
            again = input("\nAdd another alert? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if again != "y":
            break

    if not alerts:
        return None

    return alerts, category_url_part, sport


# ---------------------------------------------------------------------------
# Monitor loop
# ---------------------------------------------------------------------------


async def run_monitor(
    alerts: list[Alert],
    category_url_part: str,
    sport: Sport,
) -> None:
    """Stream odds and check alerts, redrawing the summary table on each update."""
    snapshot = Snapshot()
    update_count = 0

    try:
        async for _changed in _connect_and_stream(snapshot, category_url_part):
            update_count += 1
            events = snapshot.build_events(sport)

            # Build a lookup by event id
            events_by_id: dict[str, Event] = {e.id: e for e in events}

            for alert in alerts:
                event = events_by_id.get(alert.event_id)
                if event is None:
                    continue

                odds_dict = _snapshot_odds(event)
                current = odds_dict.get(alert.odds_key)
                if current is None:
                    continue

                alert.maybe_exit_cooldown()

                if alert.check(current):
                    alert.fire()
                    title = "Odds Alert"
                    msg = (
                        f"{alert.outcome_name} {alert.current_odds:.2f} "
                        f"{alert.direction.value} {alert.threshold:.2f}"
                    )
                    send_notification(title, msg)
                    print("\a", end="", flush=True)  # terminal bell
                else:
                    # Update current_odds even when not firing
                    alert.current_odds = current

            _redraw(render_summary(alerts, update_count))
    except KeyboardInterrupt:
        print("\nStopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    level = logging.DEBUG if "--debug" in sys.argv else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    print("=== Sporttip Odds Monitor ===")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="en-CH", timezone_id="Europe/Zurich",
        )
        page = await context.new_page()

        try:
            result = await setup_alerts(page)
            if result is None:
                print("\nNo alerts configured. Bye!")
                return

            alerts, category_url_part, sport = result
            print(f"\n  Starting monitor with {len(alerts)} alert(s)...\n")
            await browser.close()

            # run_monitor opens its own browser via _connect_and_stream
            await run_monitor(alerts, category_url_part, sport)

        except KeyboardInterrupt:
            pass
        finally:
            if browser.is_connected():
                await browser.close()

    print("Bye!")


def cli() -> None:
    """Sync entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
