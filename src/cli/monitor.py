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
from datetime import timezone as tz
from enum import Enum
from typing import Any

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

# Admiral phase URN -> display label (football-centric)
_PHASE_LABELS: dict[str, str] = {
    "asw:phase:1": "Pre",
    "asw:phase:6": "1H",
    "asw:phase:7": "2H",
    "asw:phase:8": "HT",
    "asw:phase:9": "ET1",
    "asw:phase:10": "ET2",
    "asw:phase:11": "Pen",
    "asw:phase:14": "FT",
}

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
# Cross-platform notification
# ---------------------------------------------------------------------------


def _escape_applescript(text: str) -> str:
    """Escape a string for use inside AppleScript double quotes."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def send_notification(title: str, message: str) -> None:
    """Send a persistent, audible notification (macOS + Windows).

    macOS: ``display alert`` (stays until clicked) + system sound via ``afplay``.
    Windows: PowerShell ``MessageBox`` (stays until clicked) + system beep.
    """
    if sys.platform == "darwin":
        _notify_macos(title, message)
    elif sys.platform == "win32":
        _notify_windows(title, message)
    else:
        logger.debug("Unsupported platform for notifications: %s", sys.platform)


def _notify_macos(title: str, message: str) -> None:
    safe_title = _escape_applescript(title)
    safe_msg = _escape_applescript(message)
    script = f'display alert "{safe_title}" message "{safe_msg}"'
    try:
        subprocess.Popen(  # noqa: S603
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        logger.debug("osascript not found")
    # Play system alert sound
    try:
        subprocess.Popen(  # noqa: S603
            ["afplay", "/System/Library/Sounds/Glass.aiff"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        logger.debug("afplay not found")


def _notify_windows(title: str, message: str) -> None:
    safe_title = title.replace("'", "''")
    safe_msg = message.replace("'", "''")
    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName PresentationCore;"
        "[System.Media.SystemSounds]::Exclamation.Play();"
        f"[System.Windows.Forms.MessageBox]::Show('{safe_msg}','{safe_title}',"
        "'OK','Exclamation')"
    )
    try:
        subprocess.Popen(  # noqa: S603
            ["powershell", "-WindowStyle", "Hidden", "-Command", ps_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        logger.debug("powershell not found")


# ---------------------------------------------------------------------------
# Live summary table
# ---------------------------------------------------------------------------


def _match_time(raw_event: dict[str, Any]) -> str:
    """Compute an approximate match clock string from raw snapshot event data.

    Returns e.g. "1H 23'" or "2H 67'" or "HT" or "" if unknown.
    """
    phase = raw_event.get("phase", "")
    label = _PHASE_LABELS.get(phase, "")

    # Non-playing phases — just show the label
    if label in ("Pre", "HT", "FT", "Pen", ""):
        return label

    start_str = str(raw_event.get("startTime", ""))
    if not start_str:
        return label

    if start_str.endswith("Z"):
        start_str = start_str[:-1] + "+00:00"
    try:
        start = datetime.fromisoformat(start_str)
    except (ValueError, TypeError):
        return label

    elapsed_min = (datetime.now(tz=tz.utc) - start).total_seconds() / 60

    # Rough football minute estimate
    if label == "1H":
        minute = min(int(elapsed_min), 45)
    elif label == "2H":
        minute = 45 + min(int(elapsed_min - 60), 45)  # ~15 min halftime
    elif label == "ET1":
        minute = 90 + min(int(elapsed_min - 120), 15)
    elif label == "ET2":
        minute = 105 + min(int(elapsed_min - 135), 15)
    else:
        minute = int(elapsed_min)

    return f"{label} {max(minute, 1)}'"


def render_summary(
    alerts: list[Alert],
    update_count: int,
    match_times: dict[str, str] | None = None,
) -> str:
    """Render the live alert summary table."""
    import shutil

    term_w = shutil.get_terminal_size((100, 24)).columns
    now = datetime.now().strftime("%H:%M:%S")
    times = match_times or {}

    lines: list[str] = [
        f" Odds Monitor  |  updates: {update_count}  |  {now}",
        "",
    ]

    # Adaptive column widths based on terminal width
    # Minimum: #(3) + Event(20) + Clock(7) + Odds(5) + Dir(2) + Thr(5) + Status(9) = ~60
    # With market/outcome we need more
    ev_w = min(max(term_w - 70, 20), 36)
    mk_w = min(max(term_w - 90, 12), 20)
    oc_w = min(max(term_w - 100, 10), 16)

    hdr = (
        f" {'#':>2}"
        f"  {'Event':<{ev_w}}"
        f"  {'Clock':<7}"
        f"  {'Market':<{mk_w}}"
        f"  {'Outcome':<{oc_w}}"
        f"  {'Odds':>5}"
        f" {'':>2}"
        f" {'Thr':>5}"
        f"  {'Status':<9}"
    )
    lines.append(hdr)
    lines.append(" " + "-" * (len(hdr) - 1))

    for i, a in enumerate(alerts, 1):
        ev = (a.event_label[:ev_w - 2] + "..") if len(a.event_label) > ev_w else a.event_label
        mt = times.get(a.event_id, "")[:7]
        mk = (a.market_name[:mk_w - 2] + "..") if len(a.market_name) > mk_w else a.market_name
        oc = (a.outcome_name[:oc_w - 2] + "..") if len(a.outcome_name) > oc_w else a.outcome_name
        odds_str = f"{a.current_odds:.2f}" if a.current_odds else "  -  "

        status = a.status.value
        if a.status == AlertStatus.TRIGGERED:
            status = f"\033[1;31m{status}\033[0m"  # bold red
        elif a.status == AlertStatus.COOLDOWN:
            status = f"\033[33m{status}\033[0m"  # yellow

        lines.append(
            f" {i:>2}"
            f"  {ev:<{ev_w}}"
            f"  {mt:<7}"
            f"  {mk:<{mk_w}}"
            f"  {oc:<{oc_w}}"
            f"  {odds_str:>5}"
            f" {a.direction.value:>2}"
            f" {a.threshold:>5.2f}"
            f"  {status}"
        )

    lines.append("")
    lines.append(" Ctrl+C to stop")
    return "\n".join(lines)


_prev_line_count = 0


def _redraw(text: str) -> None:
    """Redraw the summary in-place without clearing the screen.

    Moves the cursor up to overwrite previous output, then clears any
    leftover lines from the previous frame.
    """
    global _prev_line_count  # noqa: PLW0603
    lines = text.split("\n")

    # Move cursor to the start of the previous output
    if _prev_line_count > 0:
        sys.stdout.write(f"\033[{_prev_line_count}A")

    # Write each line, clearing to end of line to remove stale content
    for line in lines:
        sys.stdout.write(f"\r{line}\033[K\n")

    # Clear any extra lines left from a previous longer frame
    extra = _prev_line_count - len(lines)
    for _ in range(extra):
        sys.stdout.write("\033[K\n")
    if extra > 0:
        sys.stdout.write(f"\033[{extra}A")

    sys.stdout.flush()
    _prev_line_count = len(lines)


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

            # Compute match times from raw snapshot data
            match_times: dict[str, str] = {}
            for urn, raw in snapshot.events.items():
                match_times[urn] = _match_time(raw)

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

            _redraw(render_summary(alerts, update_count, match_times))
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
