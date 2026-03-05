"""Kelly Criterion analysis CLI (sporttip-kelly)."""

from __future__ import annotations

import argparse
import asyncio
import sys

from src.arbitrage.fair_value import (
    aggregate_fair_probs,
    compute_edge,
    fair_value_from_two_providers,
    remove_overround,
)
from src.arbitrage.kelly import analyze_market
from src.cli.compare import (
    _SPORT_CLI_MAP,
    _find_all_match_odds,
    fetch_all_odds,
)
from src.matching import _team_similarity, match_events
from src.models.events import (
    FairValue,
    KellyAnalysis,
    KellyRecommendation,
    MultiBookMarket,
    Sport,
)
from src.providers.polymarket import LEAGUE_TAGS, get_available_sports


# Kelly multiplier flag → numeric value.
_KELLY_MAP: dict[str, float] = {
    "full": 1.0,
    "half": 0.5,
    "quarter": 0.25,
}


def _join_multi_book(
    matched_odds: list[dict],
    multi_book: list[MultiBookMarket],
) -> dict[int, MultiBookMarket]:
    """Join multi-book data to matched events by fuzzy team name + date.

    Returns a dict mapping matched_odds index → MultiBookMarket.
    """
    result: dict[int, MultiBookMarket] = {}
    used: set[int] = set()

    for i, m in enumerate(matched_odds):
        best_score = 0.0
        best_j = -1
        for j, mb in enumerate(multi_book):
            if j in used:
                continue
            # Date must be within 1 day.
            delta = abs((m["start_time"] - mb.start_time).total_seconds())
            if delta > 86400:
                continue
            sim_hh = _team_similarity(m["home"], mb.home_team)
            sim_aa = _team_similarity(m["away"], mb.away_team)
            sim_ha = _team_similarity(m["home"], mb.away_team)
            sim_ah = _team_similarity(m["away"], mb.home_team)

            straight = (sim_hh + sim_aa) / 2.0 if min(sim_hh, sim_aa) >= 0.5 else 0.0
            crossed = (sim_ha + sim_ah) / 2.0 if min(sim_ha, sim_ah) >= 0.5 else 0.0
            score = max(straight, crossed)

            if score > best_score:
                best_score = score
                best_j = j

        if best_score >= 0.6 and best_j >= 0:
            result[i] = multi_book[best_j]
            used.add(best_j)

    return result


def _build_analyses(
    matched_odds: list[dict],
    multi_book_map: dict[int, MultiBookMarket] | None,
    bankroll: float,
    kelly_multiplier: float,
    use_two_provider_fallback: bool,
) -> list[KellyAnalysis]:
    """Build KellyAnalysis for each matched event."""
    analyses: list[KellyAnalysis] = []

    for i, m in enumerate(matched_odds):
        sp_odds = m["sp_odds"]
        pm_odds = m["pm_odds"]
        outcome_names = m["outcome_names"]

        # Determine fair value.
        source = ""
        n_bookmakers: int | None = None
        if multi_book_map and i in multi_book_map:
            mb = multi_book_map[i]
            fv = aggregate_fair_probs(mb.odds_by_outcome)
            # Count common bookmakers (same logic as aggregate_fair_probs).
            bookmaker_sets = [
                {bo.bookmaker for bo in mb.odds_by_outcome[name]}
                for name in mb.odds_by_outcome
            ]
            common = set.intersection(*bookmaker_sets) if bookmaker_sets else set()
            n_bookmakers = len(common)
            source = f"API ({n_bookmakers})"

            # Reorder SP/PM odds to match the fair value outcome order.
            # sp_odds/pm_odds follow the canonical order from outcome_names
            # (e.g. ("1","X","2")), but fv.outcome_names may differ
            # (dict key order from the API).
            name_to_idx = {n: j for j, n in enumerate(outcome_names)}
            if fv.outcome_names != outcome_names:
                sp_odds = tuple(sp_odds[name_to_idx[n]] for n in fv.outcome_names)
                pm_odds = tuple(pm_odds[name_to_idx[n]] for n in fv.outcome_names)
        elif use_two_provider_fallback:
            fv = fair_value_from_two_providers(sp_odds, pm_odds, outcome_names)
            source = "SP/PM"
        else:
            continue

        recs = analyze_market(fv, sp_odds, pm_odds, bankroll, kelly_multiplier, provider="ST")
        if not recs:
            continue

        total_stake_pct = sum(r.fractional_kelly for r in recs) * 100.0

        # Build SP odds lookup keyed by outcome name (aligned to fv order).
        sp_odds_map = {name: sp_odds[j] for j, name in enumerate(fv.outcome_names)}

        mb_ref = multi_book_map.get(i) if multi_book_map else None
        analyses.append(KellyAnalysis(
            home=m["home"],
            away=m["away"],
            league=m["league"],
            kickoff=m["kickoff"],
            fair_value=fv,
            recommendations=recs,
            bankroll=bankroll,
            total_stake_pct=round(total_stake_pct, 1),
            source=source,
            n_bookmakers=n_bookmakers,
            multi_book=mb_ref,
            sp_odds_by_outcome=sp_odds_map,
        ))

    return analyses


def _extract_matched_odds(matched_events: list) -> list[dict]:
    """Extract odds from matched events into a flat list of dicts."""
    results: list[dict] = []

    for m in matched_events:
        sp_all = _find_all_match_odds(m.sporttip)
        pm_all = _find_all_match_odds(m.polymarket)
        if not sp_all or not pm_all:
            continue

        # Find compatible pair.
        sp_names = sp_odds = pm_odds = None
        for sp_n, sp_o in sp_all:
            for pm_n, pm_o in pm_all:
                if len(sp_n) == len(pm_n):
                    sp_names, sp_odds = sp_n, sp_o
                    pm_odds = pm_o
                    break
            if sp_names is not None:
                break

        if sp_names is None or sp_odds is None or pm_odds is None:
            continue

        # Detect home/away swap.
        straight = (
            _team_similarity(m.sporttip.home_team, m.polymarket.home_team)
            + _team_similarity(m.sporttip.away_team, m.polymarket.away_team)
        )
        crossed = (
            _team_similarity(m.sporttip.home_team, m.polymarket.away_team)
            + _team_similarity(m.sporttip.away_team, m.polymarket.home_team)
        )
        if crossed > straight:
            if len(pm_odds) == 2:
                pm_odds = (pm_odds[1], pm_odds[0])
            elif len(pm_odds) == 3:
                pm_odds = (pm_odds[2], pm_odds[1], pm_odds[0])

        results.append({
            "home": m.sporttip.home_team,
            "away": m.sporttip.away_team,
            "league": m.sporttip.league,
            "kickoff": m.sporttip.start_time.strftime("%a %H:%M"),
            "start_time": m.sporttip.start_time,
            "outcome_names": sp_names,
            "sp_odds": sp_odds,
            "pm_odds": pm_odds,
        })

    return results


def format_kelly_table(analyses: list[KellyAnalysis]) -> tuple[str, list[dict]]:
    """Render Kelly analysis results as a table string.

    Returns (table_string, included_rows) where included_rows preserves
    ``_event_index`` for interactive drill-down.
    """
    if not analyses:
        return "\n No positive-edge bets found.\n", []

    # Flatten all recommendations with their parent match info.
    rows: list[dict] = []
    for ai, a in enumerate(analyses):
        for r in a.recommendations:
            rows.append({
                "match": f"{a.home} vs {a.away}",
                "league": a.league,
                "source": a.source,
                "outcome": r.outcome_name,
                "provider": r.provider,
                "odds": r.offered_odds,
                "fair_pct": r.fair_prob * 100.0,
                "edge_pct": r.edge * 100.0,
                "kelly_pct": r.fractional_kelly * 100.0,
                "stake": r.fractional_kelly * a.bankroll,
                "ev": r.expected_value,
                "bankroll": a.bankroll,
                "_event_index": ai,
            })

    # Sort by edge descending.
    rows.sort(key=lambda r: r["edge_pct"], reverse=True)

    # Cap total stake at bankroll.
    bankroll = rows[0]["bankroll"] if rows else 0.0
    cumulative = 0.0
    included: list[dict] = []
    excluded_count = 0
    for r in rows:
        if cumulative + r["stake"] <= bankroll:
            cumulative += r["stake"]
            included.append(r)
        else:
            # Include partial? No — just stop including further recs.
            excluded_count += 1

    rows = included

    # Compute column widths.
    col_match = "Match"
    col_league = "League"
    col_source = "Source"
    col_out = "Outcome"
    col_prov = "Provider"
    col_odds = "Odds"
    col_fair = "Fair%"
    col_edge = "Edge%"
    col_kelly = "Kelly%"
    col_stake = "Stake"
    col_ev = "EV"

    w_match = max(len(col_match), max(len(r["match"]) for r in rows))
    w_league = max(len(col_league), max(len(r["league"]) for r in rows))
    w_source = max(len(col_source), max(len(r["source"]) for r in rows))

    lines: list[str] = []
    lines.append("")
    lines.append(" Kelly Criterion Analysis")
    lines.append("")

    col_num = "#"
    w_num = max(len(col_num), len(str(len(rows))))

    hdr = (
        f" {col_num:>{w_num}}  {col_match:<{w_match}}  {col_league:<{w_league}}"
        f"  {col_source:<{w_source}}  {col_out:>7}  {col_prov:>8}"
        f"  {col_odds:>6}  {col_fair:>6}  {col_edge:>6}"
        f"  {col_kelly:>6}  {col_stake:>7}  {col_ev:>7}"
    )
    lines.append(hdr)
    lines.append(" " + "─" * (len(hdr) - 1))

    for idx, r in enumerate(rows, 1):
        row = (
            f" {idx:>{w_num}}  {r['match']:<{w_match}}  {r['league']:<{w_league}}"
            f"  {r['source']:<{w_source}}  {r['outcome']:>7}  {r['provider']:>8}"
            f"  {r['odds']:>6.2f}  {r['fair_pct']:>5.1f}%  {r['edge_pct']:>5.1f}%"
            f"  {r['kelly_pct']:>5.1f}%  ${r['stake']:>6.0f}  ${r['ev']:>6.2f}"
        )
        lines.append(row)

    lines.append("")
    total_stake = sum(r["stake"] for r in rows)
    total_ev = sum(r["ev"] for r in rows)
    lines.append(f" Total: ${total_stake:.0f} staked, ${total_ev:.2f} expected value")
    if excluded_count > 0:
        lines.append(
            f" ({excluded_count} additional bet{'s' if excluded_count != 1 else ''}"
            f" excluded — bankroll fully allocated)"
        )
    lines.append("")

    return "\n".join(lines), rows


def _print_event_detail(analysis: KellyAnalysis) -> None:
    """Print detailed bookmaker odds breakdown for one event."""
    print(f"\n {analysis.home} vs {analysis.away}")
    print(f" League: {analysis.league}  |  Kickoff: {analysis.kickoff}")
    print(f" Source: {analysis.source}\n")

    mb = analysis.multi_book
    fv = analysis.fair_value
    sp_map = analysis.sp_odds_by_outcome or {}

    if mb is not None:
        for name in fv.outcome_names:
            idx = fv.outcome_names.index(name)
            sp_o = sp_map.get(name)
            sp_str = f"  Sporttip: {sp_o:.2f}" if sp_o else ""
            print(f" Outcome: {name}  (fair prob: {fv.fair_probs[idx] * 100:.1f}%"
                  f"  fair odds: {fv.sharp_odds[idx]:.2f}{sp_str})")

            bookie_odds_list = mb.odds_by_outcome.get(name, [])
            if not bookie_odds_list:
                print("   No bookmaker data\n")
                continue

            w_bm = max(10, max(len(bo.bookmaker) for bo in bookie_odds_list))
            print(f"   {'Bookmaker':<{w_bm}}  {'Odds':>6}  {'Impl%':>6}")
            print(f"   {'─' * w_bm}  {'─' * 6}  {'─' * 6}")
            for bo in sorted(bookie_odds_list, key=lambda b: b.odds, reverse=True):
                impl_pct = (1.0 / bo.odds) * 100.0 if bo.odds > 0 else 0.0
                print(f"   {bo.bookmaker:<{w_bm}}  {bo.odds:>6.2f}  {impl_pct:>5.1f}%")
            print()
    else:
        print(f" {'Outcome':>7}  {'Fair%':>6}  {'Fair Odds':>9}  {'SP Odds':>7}")
        print(f" {'─' * 7}  {'─' * 6}  {'─' * 9}  {'─' * 7}")
        for name in fv.outcome_names:
            idx = fv.outcome_names.index(name)
            sp_o = sp_map.get(name)
            sp_str = f"{sp_o:>7.2f}" if sp_o else f"{'—':>7}"
            print(f" {name:>7}  {fv.fair_probs[idx] * 100:>5.1f}%  {fv.sharp_odds[idx]:>9.2f}  {sp_str}")
        print()


async def _async_main(
    sport: Sport,
    leagues: list[str] | None,
    bankroll: float,
    kelly_multiplier: float,
    min_edge: float,
    no_fair_value: bool,
) -> None:
    """Async entry point for the Kelly CLI."""
    print(f"Fetching odds from Sporttip and Polymarket…")
    sp_events, pm_events = await fetch_all_odds(sport, leagues=leagues)
    print(f"  Sporttip: {len(sp_events)} events, Polymarket: {len(pm_events)} events")

    matched = match_events(sp_events, pm_events)
    print(f"  Matched: {len(matched)} events")

    matched_odds = _extract_matched_odds(matched)

    multi_book_map: dict[int, MultiBookMarket] | None = None

    if not no_fair_value:
        from src.providers.theodds import TheOddsProvider

        print("Fetching multi-book odds from The Odds API…")
        provider = TheOddsProvider()
        multi_book = await provider.fetch_multi_book_odds(sport, leagues=leagues)
        if multi_book:
            print(f"  The Odds API: {len(multi_book)} events")
            multi_book_map = _join_multi_book(matched_odds, multi_book)
            print(f"  Matched to Sporttip: {len(multi_book_map)}/{len(matched_odds)} events")
        else:
            print("  No multi-book data available — falling back to SP/PM fair value")

    analyses = _build_analyses(
        matched_odds,
        multi_book_map,
        bankroll,
        kelly_multiplier,
        use_two_provider_fallback=(no_fair_value or multi_book_map is None),
    )

    # Print fair value source summary.
    api_analyses = [a for a in analyses if a.n_bookmakers is not None]
    sp_pm_count = sum(1 for a in analyses if a.source == "SP/PM")
    if api_analyses:
        bm_counts = [a.n_bookmakers for a in api_analyses]
        bm_min, bm_max = min(bm_counts), max(bm_counts)
        bm_range = str(bm_min) if bm_min == bm_max else f"{bm_min}-{bm_max}"
        parts = [f"{len(api_analyses)} events via The Odds API ({bm_range} bookmakers)"]
        if sp_pm_count:
            parts.append(f"{sp_pm_count} events via SP/PM fallback")
        print(f"  Fair value: {', '.join(parts)}")
    elif sp_pm_count:
        print(f"  Fair value: {sp_pm_count} events via SP/PM fallback")

    # Filter by minimum edge.
    if min_edge > 0:
        for a in analyses:
            a.recommendations = [r for r in a.recommendations if r.edge * 100 >= min_edge]
        analyses = [a for a in analyses if a.recommendations]

    # Sort by best edge descending.
    analyses.sort(
        key=lambda a: max(r.edge for r in a.recommendations) if a.recommendations else 0,
        reverse=True,
    )

    total_with_fair_value = len(api_analyses) + sp_pm_count
    print(f"  Positive edge: {len(analyses)}/{total_with_fair_value} events")

    table, table_rows = format_kelly_table(analyses)
    print(table)

    # Interactive drill-down loop.
    if table_rows:
        while True:
            try:
                answer = input("Enter row # to inspect (or Enter to skip): ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not answer:
                break
            try:
                row_num = int(answer)
            except ValueError:
                print(f"Invalid input: {answer!r}")
                continue
            if row_num < 1 or row_num > len(table_rows):
                print(f"Row must be between 1 and {len(table_rows)}")
                continue
            event_idx = table_rows[row_num - 1]["_event_index"]
            _print_event_detail(analyses[event_idx])


def cli() -> None:
    """CLI entry point for sporttip-kelly."""
    parser = argparse.ArgumentParser(
        description="Kelly Criterion analysis for sports betting.",
    )
    parser.add_argument(
        "--bankroll",
        type=float,
        required=True,
        help="Total bankroll amount",
    )
    parser.add_argument(
        "--kelly",
        type=str,
        default="half",
        choices=["full", "half", "quarter"],
        help="Kelly fraction: full, half, quarter (default: half)",
    )
    parser.add_argument(
        "--sport",
        type=str,
        default="football",
        choices=list(_SPORT_CLI_MAP.keys()),
        help="Sport to analyze (default: football)",
    )
    parser.add_argument(
        "--league",
        type=str,
        default=None,
        help="Filter to a single league (e.g. 'Premier League')",
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=0.0,
        help="Minimum edge %% to display (default: 0.0)",
    )
    parser.add_argument(
        "--no-fair-value",
        action="store_true",
        default=False,
        help="Skip The Odds API; use SP/PM-only fair value",
    )
    args = parser.parse_args()

    sport = _SPORT_CLI_MAP[args.sport]
    kelly_multiplier = _KELLY_MAP[args.kelly]

    leagues: list[str] | None = None
    if args.league:
        if args.league not in LEAGUE_TAGS:
            print(f"Unknown league: {args.league}", file=sys.stderr)
            print(f"Available: {', '.join(LEAGUE_TAGS)}", file=sys.stderr)
            sys.exit(1)
        leagues = [args.league]

    asyncio.run(_async_main(sport, leagues, args.bankroll, kelly_multiplier, args.min_edge, args.no_fair_value))


if __name__ == "__main__":
    cli()
