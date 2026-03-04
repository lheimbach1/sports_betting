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
    """A matched event enriched with best-of-both-providers odds and arbitrage margin."""

    home: str
    away: str
    league: str
    kickoff: str
    sp_odds: tuple[float, float, float]  # (1, X, 2)
    pm_odds: tuple[float, float, float]
    best_odds: tuple[float, float, float]  # best of SP/PM per outcome
    arb_margin: float  # arbitrage profit margin (%), positive = profit
    pm_volume: float  # Polymarket total USD volume


def _find_1x2(event: Event) -> tuple[float, float, float] | None:
    """Extract (home, draw, away) odds from the 1X2 market."""
    for m in event.markets:
        if m.name.upper() in ("1X2", "FINAL RESULT"):
            odds_map: dict[str, float] = {}
            for o in m.outcomes:
                odds_map[o.name] = o.odds
            # Try canonical "1"/"X"/"2" outcome names first.
            h = odds_map.get("1")
            d = odds_map.get("X")
            a = odds_map.get("2")
            if h and d and a:
                return (h, d, a)
            # Fall back to team-name outcomes (e.g. "Bournemouth", "Draw").
            d = odds_map.get("Draw")
            if d and len(m.outcomes) == 3:
                home_lower = event.home_team.lower()
                away_lower = event.away_team.lower()
                for o in m.outcomes:
                    if o.name.lower() == home_lower:
                        h = o.odds
                    elif o.name.lower() == away_lower:
                        a = o.odds
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


def compute_arb_margin(
    sp_odds: tuple[float, float, float],
    pm_odds: tuple[float, float, float],
) -> tuple[tuple[float, float, float], float]:
    """Compute the cross-provider arbitrage margin for a 1X2 market.

    For each outcome, pick the higher odds between the two providers.
    The arbitrage margin is: (1 / sum_of_implied_probs - 1) * 100.
    Positive values indicate a guaranteed-profit arbitrage opportunity.
    """
    best = tuple(max(s, p) for s, p in zip(sp_odds, pm_odds))
    implied_sum = sum(1.0 / o for o in best)
    margin = (1.0 / implied_sum - 1.0) * 100.0
    return (best[0], best[1], best[2]), round(margin, 2)


def compute_diffs(matched: list[MatchedEvent]) -> list[ComparedMatch]:
    """Compute arbitrage profit margins for matched events."""
    results: list[ComparedMatch] = []
    for m in matched:
        sp_1x2 = _find_1x2(m.sporttip)
        pm_1x2 = _find_1x2(m.polymarket)
        if sp_1x2 is None or pm_1x2 is None:
            continue

        best_odds, margin = compute_arb_margin(sp_1x2, pm_1x2)
        kickoff = m.sporttip.start_time.strftime("%a %H:%M")

        results.append(ComparedMatch(
            home=m.sporttip.home_team,
            away=m.sporttip.away_team,
            league=m.sporttip.league,
            kickoff=kickoff,
            sp_odds=sp_1x2,
            pm_odds=pm_1x2,
            best_odds=best_odds,
            arb_margin=margin,
            pm_volume=m.polymarket.volume or 0.0,
        ))

    results.sort(key=lambda c: c.arb_margin, reverse=True)
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
    col_b1 = "Best1"
    col_bx = "BestX"
    col_b2 = "Best2"
    col_arb = "Arb%"
    col_vol = "PM Vol"

    # Compute column widths.
    match_names = [f"{c.home} vs {c.away}" for c in compared]
    w_match = max(len(col_match), max(len(n) for n in match_names))
    w_league = max(len(col_league), max(len(c.league) for c in compared))
    w_kick = max(len(col_kick), max(len(c.kickoff) for c in compared))

    def fmt_odds(v: float) -> str:
        return f"{v:.2f}"

    def fmt_vol(v: float) -> str:
        if v >= 1_000_000:
            return f"${v / 1_000_000:.1f}M"
        if v >= 1_000:
            return f"${v / 1_000:.0f}K"
        return f"${v:.0f}"

    hdr = (
        f" {col_match:<{w_match}}  {col_league:<{w_league}}  {col_kick:<{w_kick}}"
        f"  {col_sp1:>5} {col_spx:>5} {col_sp2:>5}"
        f"  {col_pm1:>5} {col_pmx:>5} {col_pm2:>5}"
        f"  {col_b1:>5} {col_bx:>5} {col_b2:>5}"
        f"  {col_arb:>7}"
        f"  {col_vol:>8}"
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
            f"  {fmt_odds(c.best_odds[0]):>5} {fmt_odds(c.best_odds[1]):>5}"
            f" {fmt_odds(c.best_odds[2]):>5}"
            f"  {c.arb_margin:>6.2f}%"
            f"  {fmt_vol(c.pm_volume):>8}"
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


async def _async_main(leagues: list[str], min_margin: float) -> None:
    """Async entry point."""
    print("Fetching odds from Sporttip and Polymarket…")
    sp_events, pm_events = await fetch_all_odds(leagues)
    print(f"  Sporttip: {len(sp_events)} events, Polymarket: {len(pm_events)} events")

    matched = match_events(sp_events, pm_events)
    compared = compute_diffs(matched)

    if min_margin > -100:
        compared = [c for c in compared if c.arb_margin >= min_margin]

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
        "--min-margin",
        type=float,
        default=-100.0,
        help="Only show matches with arb margin >= this threshold (%%)",
    )
    args = parser.parse_args()

    leagues = OVERLAPPING_LEAGUES
    if args.league:
        if args.league not in LEAGUE_TAGS:
            print(f"Unknown league: {args.league}", file=sys.stderr)
            print(f"Available: {', '.join(LEAGUE_TAGS)}", file=sys.stderr)
            sys.exit(1)
        leagues = [args.league]

    asyncio.run(_async_main(leagues, args.min_margin))


if __name__ == "__main__":
    cli()
