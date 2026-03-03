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
from src.providers.sporttip import Snapshot, _connect_and_stream

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


async def setup_alerts(page: Page) -> tuple[list[Alert], str, Sport] | None:
    """Interactive flow: pick sport -> category -> events -> markets -> thresholds.

    Returns (alerts, category_url_part, sport) or None if user quits.
    """
    # --- Sport selection ---
    print("\nLoading sports...")
    sports = await discover_sports(page)
    if not sports:
        print("No sports found.")
        return None

    choice: int | str | None = None
    selected_sport: dict[str, str] | None = None
    while selected_sport is None:
        choice = prompt_choice(
            [s["name"] for s in sports],
            prompt="Select sport",
            allow_back=False,
            extras=[s.get("count", "") for s in sports],
            extras_header="Events",
        )
        if choice is None:
            return None
        if isinstance(choice, str):
            selected_sport = _filter_pick(sports, "name", choice, "Select sport")
            continue
        selected_sport = sports[choice]

    sport_enum = _sport_from_url(selected_sport["url_part"])

    # --- Category selection (with sub-category drill-down) ---
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
    # Filter out the parent itself and anything at the same level
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
                # Use the parent category
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

    # --- Event + market + threshold loop ---
    alerts: list[Alert] = []
    events: list[Event] | None = None

    while True:
        # Load events (reuse if already loaded)
        if events is None:
            print(f"Loading events for {selected_cat['name']}...")
            events, _ = await load_events(category_url_part, sport_enum)
            if not events:
                print("No events found.")
                return None

        print(f"\n{selected_cat['name']} — {len(events)} events:\n")
        print(format_events_table(events))

        event_labels = [
            f"{e.home_team} vs {e.away_team}" if e.away_team else e.home_team
            for e in events
        ]
        ev_choice = prompt_choice(
            event_labels,
            prompt="Select event",
            allow_back=False,
        )
        if ev_choice is None:
            if alerts:
                break  # proceed with existing alerts
            return None
        if isinstance(ev_choice, str):
            # Text filter on events
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

    return alerts, category_url_part, sport_enum


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
