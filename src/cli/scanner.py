"""Continuous arbitrage scanner: polls Sporttip + Polymarket for live arb detection.

Monitors matched events for odds changes and sends desktop notifications
when cross-provider arbitrage opportunities exceed a configurable margin.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime

from src.cli.compare import ComparedMatch, _format_section, compute_diffs, fetch_all_odds
from src.cli.monitor import send_notification
from src.matching import match_events
from src.models.events import Sport

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Arb state tracking
# ---------------------------------------------------------------------------

@dataclass
class ArbOpportunity:
    """Tracks a single live arbitrage opportunity."""

    match_key: str  # "Arsenal vs Everton"
    league: str
    margin: float
    sp_odds: tuple[float, ...]
    pm_odds: tuple[float, ...]
    best_odds: tuple[float, ...]
    best_providers: tuple[str, ...]
    stakes: tuple[float, ...]
    first_seen: float  # monotonic timestamp
    last_notified: float = 0.0  # 0 = never notified
    cooldown: float = 300.0  # seconds between re-notifications


def should_notify(
    active: dict[str, ArbOpportunity],
    match_key: str,
    margin: float,
    min_margin: float,
    now: float,
) -> bool:
    """Decide whether to send a notification for an arb opportunity.

    Returns True when:
    - The opportunity is new (not yet in active dict)
    - The margin improved by >1pp since last notification AND cooldown expired
    """
    if margin <= min_margin:
        return False

    prev = active.get(match_key)
    if prev is None:
        return True

    cooldown_ok = (now - prev.last_notified) >= prev.cooldown
    improved = margin > prev.margin + 1.0
    return cooldown_ok and improved


def find_expired(
    active: dict[str, ArbOpportunity],
    current_keys: set[str],
    min_margin: float,
    current_margins: dict[str, float],
) -> list[str]:
    """Return match keys for arbs that should be removed.

    An arb expires when its match key is no longer present in current matches,
    or its margin dropped below the threshold.
    """
    expired: list[str] = []
    for key in list(active):
        if key not in current_keys:
            expired.append(key)
        elif current_margins.get(key, 0.0) <= min_margin:
            expired.append(key)
    return expired


# ---------------------------------------------------------------------------
# Terminal dashboard
# ---------------------------------------------------------------------------

_prev_line_count = 0


def _redraw(text: str) -> None:
    """Redraw the dashboard in-place using ANSI escape codes."""
    global _prev_line_count  # noqa: PLW0603
    lines = text.split("\n")

    if _prev_line_count > 0:
        sys.stdout.write(f"\033[{_prev_line_count}A")

    for line in lines:
        sys.stdout.write(f"\r{line}\033[K\n")

    extra = _prev_line_count - len(lines)
    for _ in range(extra):
        sys.stdout.write("\033[K\n")
    if extra > 0:
        sys.stdout.write(f"\033[{extra}A")

    sys.stdout.flush()
    _prev_line_count = len(lines)


def render_dashboard(
    compared: list[ComparedMatch],
    total_matched: int,
    scan_count: int,
    min_margin: float,
    n_active: int,
) -> str:
    """Build the terminal dashboard string.

    Shows the full comparison table (same format as ``sporttip-compare``),
    sorted best-to-worst by arb margin, plus a header with scan metadata.
    """
    ts = datetime.now().strftime("%H:%M:%S")
    lines: list[str] = []
    header = (
        f" Arb Scanner — {total_matched} matched"
        f" | {n_active} arbs above {min_margin:.1f}%"
        f" | Scan #{scan_count}"
        f" | Updated {ts}"
    )
    lines.append(header)
    lines.append("")

    if compared:
        table_lines = _format_section(compared, "All Matches")
        lines.extend(table_lines)
    else:
        lines.append(" No matched events found.")

    lines.append("")
    lines.append(" Ctrl+C to stop.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main scan loop
# ---------------------------------------------------------------------------

def _match_key(cm: ComparedMatch) -> str:
    return f"{cm.home} vs {cm.away}"


def _update_arbs(
    compared: list[ComparedMatch],
    active: dict[str, ArbOpportunity],
    min_margin: float,
    now: float,
) -> list[ComparedMatch]:
    """Update active arb dict from compared matches. Returns list of newly notified matches."""
    notified: list[ComparedMatch] = []
    current_keys: set[str] = set()
    current_margins: dict[str, float] = {}

    for cm in compared:
        key = _match_key(cm)
        current_keys.add(key)
        current_margins[key] = cm.arb_margin

        if should_notify(active, key, cm.arb_margin, min_margin, now):
            notified.append(cm)
            opp = ArbOpportunity(
                match_key=key,
                league=cm.league,
                margin=cm.arb_margin,
                sp_odds=cm.sp_odds,
                pm_odds=cm.pm_odds,
                best_odds=cm.best_odds,
                best_providers=cm.best_providers,
                stakes=cm.stakes,
                first_seen=active[key].first_seen if key in active else now,
                last_notified=now,
            )
            active[key] = opp
        elif cm.arb_margin >= min_margin and key in active:
            # Update odds/margin without re-notifying
            active[key].margin = cm.arb_margin
            active[key].sp_odds = cm.sp_odds
            active[key].pm_odds = cm.pm_odds
            active[key].best_odds = cm.best_odds
            active[key].best_providers = cm.best_providers
            active[key].stakes = cm.stakes
        elif cm.arb_margin >= min_margin and key not in active:
            # New arb that was already notified in this batch via should_notify
            pass

    # Remove expired arbs
    for expired_key in find_expired(active, current_keys, min_margin, current_margins):
        logger.info("Arb expired: %s", expired_key)
        del active[expired_key]

    return notified


def _send_arb_notification(cm: ComparedMatch) -> None:
    """Send a desktop notification for an arb opportunity."""
    title = f"Arb: {cm.home} vs {cm.away} (+{cm.arb_margin:.1f}%)"
    sp_str = " / ".join(f"{o:.2f}" for o in cm.sp_odds)
    pm_str = " / ".join(f"{o:.2f}" for o in cm.pm_odds)
    stakes_str = " / ".join(
        f"{s:.1f}%@{p}" for s, p in zip(cm.stakes, cm.best_providers)
    )
    body = f"ST: {sp_str}  PM: {pm_str}\nBet: {stakes_str}"
    send_notification(title, body)


async def _run_scanner(sport: Sport, interval: float, min_margin: float) -> None:
    """Main scanner coroutine.

    Polls both Sporttip (all leagues via ``fetch_all_odds``) and Polymarket
    in parallel every ``interval`` seconds.  Uses the same league-discovery
    logic as ``sporttip-compare`` so event coverage is identical.
    """
    active: dict[str, ArbOpportunity] = {}
    scan_count = 0

    msg = f" Starting arb scanner for {sport.value}"
    msg += f" (poll every {interval:.0f}s, min {min_margin:.1f}%)"
    print(msg)
    print(" Fetching initial data (all leagues)...\n")

    while True:
        try:
            sp_events, pm_events = await fetch_all_odds(sport)
            scan_count += 1

            matched = match_events(sp_events, pm_events)
            compared = compute_diffs(matched)
            now = time.monotonic()
            notified = _update_arbs(compared, active, min_margin, now)
            for cm in notified:
                _send_arb_notification(cm)

            dashboard = render_dashboard(
                compared, len(matched), scan_count, min_margin,
                n_active=len(active),
            )
            _redraw(dashboard)
        except Exception:
            logger.exception("Scan cycle failed")

        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_SPORT_CHOICES = {s.value: s for s in Sport}


def cli() -> None:
    """Entry point for sporttip-scan."""
    parser = argparse.ArgumentParser(
        description="Continuous arbitrage scanner: Sporttip + Polymarket",
    )
    parser.add_argument(
        "--sport",
        choices=list(_SPORT_CHOICES),
        default="football",
        help="Sport to scan (default: football)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="Seconds between Polymarket polls (default: 30)",
    )
    parser.add_argument(
        "--min-margin",
        type=float,
        default=0.0,
        help="Minimum arb margin %% for notifications (default: 0.0)",
    )
    args = parser.parse_args()

    sport = _SPORT_CHOICES[args.sport]

    try:
        asyncio.run(_run_scanner(sport, args.interval, args.min_margin))
    except KeyboardInterrupt:
        print("\n Stopped.")
