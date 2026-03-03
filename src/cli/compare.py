"""Cross-provider odds comparison CLI (Sporttip vs Polymarket)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

from src.matching import MatchedEvent, match_events
from src.models.events import Event, Sport
from src.providers.polymarket import LEAGUE_TAGS, PolymarketProvider
from src.providers.sporttip import SporttipProvider

# Leagues available on both providers.
OVERLAPPING_LEAGUES = list(LEAGUE_TAGS.keys())


@dataclass
class ComparedMatch:
    """A matched event with odds from both providers and computed diff."""

    home: str
    away: str
    league: str
    kickoff: str
    sp_odds: tuple[float, float, float]  # (1, X, 2)
    pm_odds: tuple[float, float, float]
    max_diff_pct: float  # largest |diff| / sporttip * 100


def _find_1x2(event: Event) -> tuple[float, float, float] | None:
    """Extract (home, draw, away) odds from the 1X2 market."""
    for m in event.markets:
        if m.name.upper() in ("1X2", "FINAL RESULT"):
            odds_map: dict[str, float] = {}
            for o in m.outcomes:
                odds_map[o.name] = o.odds
            h = odds_map.get("1")
            d = odds_map.get("X")
            a = odds_map.get("2")
            if h and d and a:
                return (h, d, a)
    return None


async def fetch_all_odds(
    leagues: list[str],
) -> tuple[list[Event], list[Event]]:
    """Fetch Sporttip and Polymarket events in parallel."""
    sp = SporttipProvider()
    pm = PolymarketProvider()
    sp_events, pm_events = await asyncio.gather(
        sp.fetch_events(Sport.FOOTBALL, leagues=leagues, all_markets=False),
        pm.fetch_events(Sport.FOOTBALL, leagues=leagues),
    )
    return sp_events, pm_events


def compute_diffs(matched: list[MatchedEvent]) -> list[ComparedMatch]:
    """Compute per-outcome odds differences for matched events."""
    results: list[ComparedMatch] = []
    for m in matched:
        sp_1x2 = _find_1x2(m.sporttip)
        pm_1x2 = _find_1x2(m.polymarket)
        if sp_1x2 is None or pm_1x2 is None:
            continue

        diffs: list[float] = []
        for sp_o, pm_o in zip(sp_1x2, pm_1x2):
            if sp_o > 0:
                diffs.append(abs(sp_o - pm_o) / sp_o * 100)
            else:
                diffs.append(0.0)

        kickoff = m.sporttip.start_time.strftime("%a %H:%M")

        results.append(ComparedMatch(
            home=m.sporttip.home_team,
            away=m.sporttip.away_team,
            league=m.sporttip.league,
            kickoff=kickoff,
            sp_odds=sp_1x2,
            pm_odds=pm_1x2,
            max_diff_pct=round(max(diffs), 1),
        ))

    results.sort(key=lambda c: c.max_diff_pct, reverse=True)
    return results


def format_comparison_table(
    compared: list[ComparedMatch],
    n_sp: int,
    n_pm: int,
) -> str:
    """Render the comparison table as a string."""
    n_matched = len(compared)
    lines: list[str] = []

    header = (
        f" Sporttip vs Polymarket — soccer odds comparison"
        f" ({n_matched} matched / {n_sp} Sporttip / {n_pm} Polymarket)"
    )
    lines.append("")
    lines.append(header)
    lines.append("")

    if not compared:
        lines.append(" No matched events found.")
        n_unmatched_sp = n_sp - n_matched
        n_unmatched_pm = n_pm - n_matched
        if n_unmatched_sp or n_unmatched_pm:
            lines.append(
                f" {n_unmatched_sp} unmatched Sporttip events,"
                f" {n_unmatched_pm} unmatched Polymarket events."
            )
        return "\n".join(lines)

    # Column headers.
    col_match = "Match"
    col_league = "League"
    col_kick = "Kickoff"
    col_sp1 = "ST 1"
    col_spx = "X"
    col_sp2 = "2"
    col_pm1 = "PM 1"
    col_pmx = "X"
    col_pm2 = "2"
    col_diff = "Diff%"

    # Compute column widths.
    match_names = [f"{c.home} vs {c.away}" for c in compared]
    w_match = max(len(col_match), max(len(n) for n in match_names))
    w_league = max(len(col_league), max(len(c.league) for c in compared))
    w_kick = max(len(col_kick), max(len(c.kickoff) for c in compared))

    def fmt_odds(v: float) -> str:
        return f"{v:.2f}"

    hdr = (
        f" {col_match:<{w_match}}  {col_league:<{w_league}}  {col_kick:<{w_kick}}"
        f"  {col_sp1:>5} {col_spx:>5} {col_sp2:>5}"
        f"  {col_pm1:>5} {col_pmx:>5} {col_pm2:>5}"
        f"  {col_diff:>6}"
    )
    lines.append(hdr)
    lines.append(" " + "─" * (len(hdr) - 1))

    for c, name in zip(compared, match_names):
        row = (
            f" {name:<{w_match}}  {c.league:<{w_league}}  {c.kickoff:<{w_kick}}"
            f"  {fmt_odds(c.sp_odds[0]):>5} {fmt_odds(c.sp_odds[1]):>5}"
            f" {fmt_odds(c.sp_odds[2]):>5}"
            f"  {fmt_odds(c.pm_odds[0]):>5} {fmt_odds(c.pm_odds[1]):>5}"
            f" {fmt_odds(c.pm_odds[2]):>5}"
            f"  {c.max_diff_pct:>5.1f}%"
        )
        lines.append(row)

    lines.append("")
    n_unmatched_sp = n_sp - n_matched
    n_unmatched_pm = n_pm - n_matched
    lines.append(
        f" {n_unmatched_sp} unmatched Sporttip events,"
        f" {n_unmatched_pm} unmatched Polymarket events."
    )
    return "\n".join(lines)


async def _async_main(leagues: list[str], min_diff: float) -> None:
    """Async entry point."""
    print("Fetching odds from Sporttip and Polymarket…")
    sp_events, pm_events = await fetch_all_odds(leagues)
    print(f"  Sporttip: {len(sp_events)} events, Polymarket: {len(pm_events)} events")

    matched = match_events(sp_events, pm_events)
    compared = compute_diffs(matched)

    if min_diff > 0:
        compared = [c for c in compared if c.max_diff_pct >= min_diff]

    table = format_comparison_table(compared, len(sp_events), len(pm_events))
    print(table)


def cli() -> None:
    """CLI entry point for sporttip-compare."""
    parser = argparse.ArgumentParser(
        description="Compare Sporttip vs Polymarket soccer odds.",
    )
    parser.add_argument(
        "--league",
        type=str,
        default=None,
        help="Filter to a single league (e.g. 'Premier League')",
    )
    parser.add_argument(
        "--min-diff",
        type=float,
        default=0.0,
        help="Only show matches with diff%% >= this threshold",
    )
    args = parser.parse_args()

    leagues = OVERLAPPING_LEAGUES
    if args.league:
        if args.league not in LEAGUE_TAGS:
            print(f"Unknown league: {args.league}", file=sys.stderr)
            print(f"Available: {', '.join(LEAGUE_TAGS)}", file=sys.stderr)
            sys.exit(1)
        leagues = [args.league]

    asyncio.run(_async_main(leagues, args.min_diff))


if __name__ == "__main__":
    cli()
