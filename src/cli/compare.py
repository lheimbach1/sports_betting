"""Cross-provider odds comparison CLI (Sporttip vs Polymarket)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

from src.matching import MatchedEvent, _team_similarity, match_events
from src.models.events import Event, Sport
from src.providers.polymarket import (
    LEAGUE_TAGS,
    PolymarketProvider,
    get_available_sports,
)
from src.providers.sporttip import SporttipProvider

# Leagues available on both providers (football only).
OVERLAPPING_LEAGUES = list(LEAGUE_TAGS.keys())

# Human-readable sport labels for the CLI and table output.
_SPORT_LABELS: dict[Sport, str] = {
    Sport.FOOTBALL: "Football",
    Sport.ICE_HOCKEY: "Ice Hockey",
    Sport.TENNIS: "Tennis",
    Sport.BASKETBALL: "Basketball",
    Sport.HANDBALL: "Handball",
    Sport.MOTOR_SPORTS: "Motor Sports",
}

# Valid --sport flag values mapped to Sport enums.
# Currently only football has match-level markets on Polymarket.
# Other sports only have futures/championship markets, so they can't be
# compared with Sporttip match odds.  More sports will appear here as
# Polymarket adds match-level markets for them.
_SPORT_CLI_MAP: dict[str, Sport] = {s.value: s for s in get_available_sports()}

# Market names recognized as match-odds markets.
_MATCH_ODDS_NAMES: frozenset[str] = frozenset({
    "1X2",
    "FINAL RESULT",
    "MONEYLINE",
    "WINNER",
    "MATCH WINNER",
    "MONEY LINE",
    "WINNER (INCL. OVERTIME)",
    "WINNER (INCL. OVERTIME/PENALTIES)",
    "3-WAY (REGULAR PLAYING TIME)",
})


@dataclass
class ComparedMatch:
    """A matched event enriched with best-of-both-providers odds and arbitrage margin."""

    home: str
    away: str
    league: str
    kickoff: str
    sport: Sport
    outcome_names: tuple[str, ...]  # ("1","X","2") or ("1","2")
    sp_odds: tuple[float, ...]
    pm_odds: tuple[float, ...]
    best_odds: tuple[float, ...]  # best of SP/PM per outcome
    best_providers: tuple[str, ...]  # which provider has the best odds ("ST"/"PM")
    stakes: tuple[float, ...]  # budget allocation % per outcome
    arb_margin: float  # arbitrage profit margin (%), positive = profit
    sp_overround: float  # Sporttip overround (%)
    pm_overround: float  # Polymarket overround (%)
    pm_volume: float  # Polymarket total USD volume


def _find_match_odds(event: Event) -> tuple[tuple[str, ...], tuple[float, ...]] | None:
    """Extract match-odds outcomes from an event.

    Returns the first valid interpretation.  Use ``_find_all_match_odds``
    when you need every available interpretation (2-way *and* 3-way).
    """
    results = _find_all_match_odds(event)
    return results[0] if results else None


def _find_all_match_odds(
    event: Event,
) -> list[tuple[tuple[str, ...], tuple[float, ...]]]:
    """Return *all* valid match-odds interpretations for an event.

    An event may carry both a 3-way market (e.g. "Final Result" with draw)
    and a 2-way market (e.g. "Winner incl. Overtime").  Returns a list so
    the caller can pick the interpretation that is compatible with the other
    provider's market.
    """
    found: list[tuple[tuple[str, ...], tuple[float, ...]]] = []
    seen_counts: set[int] = set()

    for m in event.markets:
        if m.name.upper() not in _MATCH_ODDS_NAMES:
            continue

        odds_map: dict[str, float] = {}
        for o in m.outcomes:
            odds_map[o.name] = o.odds

        # Try canonical 3-way: "1"/"X"/"2"
        h = odds_map.get("1")
        d = odds_map.get("X")
        a = odds_map.get("2")
        if h and d and a and 3 not in seen_counts:
            found.append((("1", "X", "2"), (h, d, a)))
            seen_counts.add(3)
            continue

        # Try canonical 2-way: "1"/"2" (no draw)
        if h and a and d is None and 2 not in seen_counts:
            found.append((("1", "2"), (h, a)))
            seen_counts.add(2)
            continue

        # Fall back to team-name outcomes.
        home_lower = event.home_team.lower()
        away_lower = event.away_team.lower()
        h_fb = a_fb = None
        d = odds_map.get("Draw")
        for o in m.outcomes:
            if o.name == "Draw" or o.name == "X":
                continue
            if o.name.lower() == home_lower:
                h_fb = o.odds
            elif o.name.lower() == away_lower:
                a_fb = o.odds

        # 3-way with draw
        if h_fb and d and a_fb and 3 not in seen_counts:
            found.append((("1", "X", "2"), (h_fb, d, a_fb)))
            seen_counts.add(3)
        # 2-way (no draw)
        elif h_fb and a_fb and d is None and 2 not in seen_counts:
            found.append((("1", "2"), (h_fb, a_fb)))
            seen_counts.add(2)

    return found


# Sporttip league names to fetch per sport (must exist in LEAGUE_URLS).
_SPORTTIP_LEAGUES: dict[Sport, list[str]] = {
    Sport.BASKETBALL: ["NBA"],
    Sport.ICE_HOCKEY: ["NHL"],
    Sport.TENNIS: ["ATP Singles", "WTA Singles"],
}


async def fetch_all_odds(
    sport: Sport,
    leagues: list[str] | None = None,
) -> tuple[list[Event], list[Event]]:
    """Fetch Sporttip and Polymarket events in parallel for a given sport."""
    sp = SporttipProvider()
    pm = PolymarketProvider()

    # For Sporttip: pass leagues=[] to trigger dynamic league discovery —
    # it visits the sport page, finds all league URLs, and fetches each.
    # This catches events from all leagues, not just the ~9 in LEAGUE_TAGS.
    # When the user filters to a specific league via --league, pass it through.
    sp_leagues: list[str]
    if sport == Sport.FOOTBALL:
        if leagues is not None and len(leagues) == 1:
            sp_leagues = leagues  # user-requested single league
        else:
            sp_leagues = []  # sport landing page → all events
    else:
        sp_leagues = _SPORTTIP_LEAGUES.get(sport, [])

    sp_events, pm_events = await asyncio.gather(
        sp.fetch_events(sport, leagues=sp_leagues, all_markets=False),
        pm.fetch_events(sport, leagues=leagues),
    )
    return sp_events, pm_events


def compute_arb_margin(
    sp_odds: tuple[float, ...],
    pm_odds: tuple[float, ...],
) -> tuple[tuple[float, ...], tuple[str, ...], tuple[float, ...], float]:
    """Compute the cross-provider arbitrage margin for a match-odds market.

    Works for any number of outcomes (2-way, 3-way, etc.).
    For each outcome, pick the higher odds between the two providers.
    The arbitrage margin is: (1 / sum_of_implied_probs - 1) * 100.
    Positive values indicate a guaranteed-profit arbitrage opportunity.

    Returns (best_odds, best_providers, stakes_pct, margin).
    """
    best = tuple(max(s, p) for s, p in zip(sp_odds, pm_odds))
    providers = tuple("ST" if s >= p else "PM" for s, p in zip(sp_odds, pm_odds))
    implied_sum = sum(1.0 / o for o in best)
    margin = (1.0 / implied_sum - 1.0) * 100.0
    # A positive margin from a single provider isn't a real cross-provider arb —
    # it just means that provider's overround is negative. Cap at 0.
    if len(set(providers)) < 2 and margin > 0:
        margin = 0.0
    stakes = tuple(round((1.0 / o) / implied_sum * 100.0, 1) for o in best)
    return best, providers, stakes, round(margin, 2)


def compute_diffs(matched: list[MatchedEvent]) -> list[ComparedMatch]:
    """Compute arbitrage profit margins for matched events."""
    results: list[ComparedMatch] = []
    for m in matched:
        sp_all = _find_all_match_odds(m.sporttip)
        pm_all = _find_all_match_odds(m.polymarket)
        if not sp_all or not pm_all:
            continue

        # Find a compatible pair (matching outcome count).
        sp_names = sp_odds = pm_names = pm_odds = None
        for sp_n, sp_o in sp_all:
            for pm_n, pm_o in pm_all:
                if len(sp_n) == len(pm_n):
                    sp_names, sp_odds = sp_n, sp_o
                    pm_names, pm_odds = pm_n, pm_o
                    break
            if sp_names is not None:
                break

        if sp_names is None or pm_names is None:
            continue

        # Detect home/away swap between providers.  If Polymarket has the
        # teams in the opposite order, reverse PM odds so outcome "1" aligns
        # with the same team on both sides.
        straight = (
            _team_similarity(m.sporttip.home_team, m.polymarket.home_team)
            + _team_similarity(m.sporttip.away_team, m.polymarket.away_team)
        )
        crossed = (
            _team_similarity(m.sporttip.home_team, m.polymarket.away_team)
            + _team_similarity(m.sporttip.away_team, m.polymarket.home_team)
        )
        if crossed > straight:
            # Swap PM odds: reverse home/away (keep draw in the middle for 3-way).
            if len(pm_odds) == 2:
                pm_odds = (pm_odds[1], pm_odds[0])
            elif len(pm_odds) == 3:
                pm_odds = (pm_odds[2], pm_odds[1], pm_odds[0])

        best_odds, best_providers, stakes, margin = compute_arb_margin(sp_odds, pm_odds)
        kickoff = m.sporttip.start_time.strftime("%a %H:%M")

        # Overround: how much the implied probabilities exceed 100%.
        sp_overround = round((sum(1.0 / o for o in sp_odds) - 1.0) * 100.0, 1)
        pm_overround = round((sum(1.0 / o for o in pm_odds) - 1.0) * 100.0, 1)

        results.append(ComparedMatch(
            home=m.sporttip.home_team,
            away=m.sporttip.away_team,
            league=m.sporttip.league,
            kickoff=kickoff,
            sport=m.sporttip.sport,
            outcome_names=sp_names,
            sp_odds=sp_odds,
            pm_odds=pm_odds,
            best_odds=best_odds,
            best_providers=best_providers,
            stakes=stakes,
            arb_margin=margin,
            sp_overround=sp_overround,
            pm_overround=pm_overround,
            pm_volume=m.polymarket.volume or 0.0,
        ))

    results.sort(key=lambda c: c.arb_margin, reverse=True)
    return results


def _format_section(
    compared: list[ComparedMatch],
    sport_label: str,
) -> list[str]:
    """Render a single sport section of the comparison table."""
    if not compared:
        return []

    lines: list[str] = []
    is_3way = len(compared[0].outcome_names) == 3

    # Column headers.
    col_match = "Match"
    col_league = "League"
    col_kick = "Kickoff"
    col_arb = "Arb%"
    col_vol = "PM Vol"

    match_names = [f"{c.home} vs {c.away}" for c in compared]
    w_match = max(len(col_match), max(len(n) for n in match_names))
    w_league = max(len(col_league), max(len(c.league) for c in compared))
    w_kick = max(len(col_kick), max(len(c.kickoff) for c in compared))

    def fmt_odds(v: float) -> str:
        return f"{v:.2f}"

    def fmt_odds_stake(v: float, stake: float | None) -> str:
        """Format odds with optional stake % when this is the best provider."""
        if stake is not None:
            return f"{v:.2f} {stake:.0f}%"
        return f"{v:.2f}"

    def fmt_vol(v: float) -> str:
        if v >= 1_000_000:
            return f"${v / 1_000_000:.1f}M"
        if v >= 1_000:
            return f"${v / 1_000:.0f}K"
        return f"${v:.0f}"

    def fmt_or(v: float) -> str:
        return f"{v:.1f}%"

    # Odds+stake cells are wider: "1.50 34%" = 8 chars
    w_os = 8  # width for odds+stake columns

    lines.append(f"  [{sport_label}]")
    lines.append("")

    if is_3way:
        hdr = (
            f" {col_match:<{w_match}}  {col_league:<{w_league}}  {col_kick:<{w_kick}}"
            f"  {'ST 1':>{w_os}} {'X':>{w_os}} {'2':>{w_os}}"
            f"  {'PM 1':>{w_os}} {'X':>{w_os}} {'2':>{w_os}}"
            f"  {'ST OR':>6} {'PM OR':>6}"
            f"  {col_arb:>7}"
            f"  {col_vol:>8}"
        )
    else:
        hdr = (
            f" {col_match:<{w_match}}  {col_league:<{w_league}}  {col_kick:<{w_kick}}"
            f"  {'ST 1':>{w_os}} {'2':>{w_os}}"
            f"  {'PM 1':>{w_os}} {'2':>{w_os}}"
            f"  {'ST OR':>6} {'PM OR':>6}"
            f"  {col_arb:>7}"
            f"  {col_vol:>8}"
        )

    lines.append(hdr)
    lines.append(" " + "─" * (len(hdr) - 1))

    for c, name in zip(compared, match_names):
        # Build odds cells: append stake % to the best provider's cell.
        sp_cells = []
        pm_cells = []
        for i in range(len(c.outcome_names)):
            if c.best_providers[i] == "ST":
                sp_cells.append(fmt_odds_stake(c.sp_odds[i], c.stakes[i]))
                pm_cells.append(fmt_odds(c.pm_odds[i]))
            else:
                sp_cells.append(fmt_odds(c.sp_odds[i]))
                pm_cells.append(fmt_odds_stake(c.pm_odds[i], c.stakes[i]))

        if is_3way:
            row = (
                f" {name:<{w_match}}  {c.league:<{w_league}}  {c.kickoff:<{w_kick}}"
                f"  {sp_cells[0]:>{w_os}} {sp_cells[1]:>{w_os}}"
                f" {sp_cells[2]:>{w_os}}"
                f"  {pm_cells[0]:>{w_os}} {pm_cells[1]:>{w_os}}"
                f" {pm_cells[2]:>{w_os}}"
                f"  {fmt_or(c.sp_overround):>6} {fmt_or(c.pm_overround):>6}"
                f"  {c.arb_margin:>6.2f}%"
                f"  {fmt_vol(c.pm_volume):>8}"
            )
        else:
            row = (
                f" {name:<{w_match}}  {c.league:<{w_league}}  {c.kickoff:<{w_kick}}"
                f"  {sp_cells[0]:>{w_os}} {sp_cells[1]:>{w_os}}"
                f"  {pm_cells[0]:>{w_os}} {pm_cells[1]:>{w_os}}"
                f"  {fmt_or(c.sp_overround):>6} {fmt_or(c.pm_overround):>6}"
                f"  {c.arb_margin:>6.2f}%"
                f"  {fmt_vol(c.pm_volume):>8}"
            )
        lines.append(row)

    return lines


def format_comparison_table(
    compared: list[ComparedMatch],
    n_sp: int,
    n_pm: int,
) -> str:
    """Render the comparison table as a string."""
    n_matched = len(compared)
    lines: list[str] = []

    header = (
        f" Sporttip vs Polymarket — odds comparison"
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

    # Group by sport for separate sections.
    sports_seen: list[Sport] = []
    by_sport: dict[Sport, list[ComparedMatch]] = {}
    for c in compared:
        if c.sport not in by_sport:
            sports_seen.append(c.sport)
            by_sport[c.sport] = []
        by_sport[c.sport].append(c)

    for sport in sports_seen:
        label = _SPORT_LABELS.get(sport, sport.value.replace("_", " ").title())
        section = _format_section(by_sport[sport], label)
        lines.extend(section)
        lines.append("")

    n_unmatched_sp = n_sp - n_matched
    n_unmatched_pm = n_pm - n_matched
    lines.append(
        f" {n_unmatched_sp} unmatched Sporttip events,"
        f" {n_unmatched_pm} unmatched Polymarket events."
    )
    return "\n".join(lines)


async def _async_main(sports: list[Sport], leagues: list[str] | None, min_margin: float) -> None:
    """Async entry point."""
    all_sp: list[Event] = []
    all_pm: list[Event] = []
    all_matched: list[MatchedEvent] = []

    for sport in sports:
        sport_label = _SPORT_LABELS.get(sport, sport.value)
        print(f"Fetching {sport_label} odds from Sporttip and Polymarket…")
        sp_events, pm_events = await fetch_all_odds(sport, leagues=leagues)
        print(f"  Sporttip: {len(sp_events)} events, Polymarket: {len(pm_events)} events")
        all_sp.extend(sp_events)
        all_pm.extend(pm_events)
        # Match per sport to avoid cross-sport false matches
        # (e.g. handball "Guadalajara" matching soccer "CD Guadalajara").
        all_matched.extend(match_events(sp_events, pm_events))

    compared = compute_diffs(all_matched)

    if min_margin > -100:
        compared = [c for c in compared if c.arb_margin >= min_margin]

    table = format_comparison_table(compared, len(all_sp), len(all_pm))
    print(table)


def cli() -> None:
    """CLI entry point for sporttip-compare."""
    parser = argparse.ArgumentParser(
        description="Compare Sporttip vs Polymarket odds.",
    )
    parser.add_argument(
        "--sport",
        type=str,
        default="all",
        choices=["all"] + list(_SPORT_CLI_MAP.keys()),
        help="Sport to compare (default: all configured sports)",
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

    if args.sport == "all":
        sports = get_available_sports()
    else:
        sports = [_SPORT_CLI_MAP[args.sport]]

    leagues: list[str] | None = None
    if args.league:
        if args.league not in LEAGUE_TAGS:
            print(f"Unknown league: {args.league}", file=sys.stderr)
            print(f"Available: {', '.join(LEAGUE_TAGS)}", file=sys.stderr)
            sys.exit(1)
        leagues = [args.league]
    # When no --league flag, leagues stays None: Sporttip fetches all events
    # via the sport landing page, Polymarket fetches all league tags + Games tag.

    asyncio.run(_async_main(sports, leagues, args.min_margin))


if __name__ == "__main__":
    cli()
