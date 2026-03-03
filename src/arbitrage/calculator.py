"""Pre-match → in-play arbitrage calculator for 1X2 football markets.

Finds "mild favorite" games and calculates what the favorite's odds need to
reach during live play for an arbitrage opportunity. The strategy:

1. Pre-match: bet on underdog outcomes (draw + away) at current odds.
2. In-play: when the underdog scenario materialises, the favorite's live odds spike.
3. Arbitrage: if the favorite's new odds are high enough, all 3 outcomes are covered.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from src.models.events import Event

logger = logging.getLogger(__name__)

# Known aliases for the 1X2 / Final Result market
_1X2_MARKET_NAMES = {"final result", "1x2", "3-weg", "3-way (regular playing time)"}

# Sporttip odds move in 0.05 increments
TICK_SIZE = 0.05


def snap_to_tick(odds: float, direction: str = "up") -> float:
    """Round odds to the nearest Sporttip tick (0.05 increment).

    Args:
        odds: Raw odds value.
        direction: "up" to round up (conservative), "down" to round down.
    """
    if direction == "up":
        return round(math.ceil(odds / TICK_SIZE) * TICK_SIZE, 2)
    return round(math.floor(odds / TICK_SIZE) * TICK_SIZE, 2)


def _find_1x2_odds(event: Event) -> tuple[float, float, float] | None:
    """Extract 1X2 odds from an event's Final Result market.

    Returns (home, draw, away) odds or None if not found.
    """
    for market in event.markets:
        if market.name.lower() not in _1X2_MARKET_NAMES:
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


@dataclass
class ArbOpportunity:
    """A potential underdog-arbitrage opportunity for a single event."""

    home_team: str
    away_team: str
    league: str
    start_time: str
    favorite: str  # "1", "X", or "2"
    current_fav_odds: float
    draw_odds: float
    away_odds: float
    home_odds: float
    target_fav_odds: float  # breakeven favorite odds
    example_fav_odds: float  # target with desired margin
    overround: float = 0.0  # sum of implied probabilities − 1 (e.g. 0.05 = 5% over)
    stakes: dict[str, float] = field(default_factory=dict)  # % of budget


def calculate_target_odds(ox: float, o2: float) -> float | None:
    """Calculate breakeven favorite odds given draw and away odds.

    Returns None if arbitrage is impossible (1/oX + 1/o2 >= 1).
    """
    implied = 1 / ox + 1 / o2
    if implied >= 1:
        return None
    return 1 / (1 - implied)


def calculate_stakes(
    o1: float, ox: float, o2: float, budget: float = 100.0,
) -> dict[str, float]:
    """Calculate optimal stake allocation for 3-way arbitrage.

    Returns stakes as amounts (not percentages) that sum to ``budget``.
    Each outcome pays out the same total.
    """
    inv_sum = 1 / o1 + 1 / ox + 1 / o2
    return {
        "1": round((1 / o1) / inv_sum * budget, 1),
        "X": round((1 / ox) / inv_sum * budget, 1),
        "2": round((1 / o2) / inv_sum * budget, 1),
    }


def calculate_margin(o1: float, ox: float, o2: float) -> float:
    """Calculate guaranteed profit margin for a 3-way arbitrage.

    Positive means profit, negative means overround (no arb).
    """
    return 1 - (1 / o1 + 1 / ox + 1 / o2)


def find_underdog_arbs(
    events: list[Event],
    min_fav_odds: float = 0.0,
    max_fav_odds: float = float("inf"),
    target_margin: float = 0.05,
) -> list[ArbOpportunity]:
    """Scan events for arbitrage targets based on 1X2 odds.

    Args:
        events: List of events with 1X2 markets.
        min_fav_odds: Minimum favorite odds to consider (0 = no lower bound).
        max_fav_odds: Maximum favorite odds to consider (inf = no upper bound).
        target_margin: Desired profit margin (0.05 = 5%).

    Returns:
        List of ArbOpportunity sorted by overround (ascending).
    """
    opportunities: list[ArbOpportunity] = []

    for event in events:
        odds = _find_1x2_odds(event)
        if odds is None:
            logger.debug(
                "No 1X2 market found for %s vs %s (markets: %s)",
                event.home_team,
                event.away_team,
                [m.name for m in event.markets],
            )
            continue

        o1, ox, o2 = odds

        # Find the favorite (lowest odds)
        min_odds = min(o1, ox, o2)
        if min_odds == ox:
            # Draw is favorite — bet on home + away pre-match,
            # need draw to spike in-play
            favorite = "X"
            fav_odds = ox
            target = calculate_target_odds(o1, o2)
        elif min_odds == o2:
            # Away favorite — the "underdog" outcomes are 1 (home) and X (draw)
            favorite = "2"
            fav_odds = o2
            target = calculate_target_odds(ox, o1)
        else:
            # Home favorite (most common)
            favorite = "1"
            fav_odds = o1
            target = calculate_target_odds(ox, o2)

        if fav_odds < min_fav_odds or fav_odds > max_fav_odds:
            continue
        if target is None:
            continue

        # Snap to Sporttip tick grid (0.05 increments, round up = conservative)
        target_snapped = snap_to_tick(target)
        example_odds = snap_to_tick(target * (1 + target_margin))

        # Calculate stakes at the example odds
        if favorite == "1":
            stakes = calculate_stakes(example_odds, ox, o2)
        elif favorite == "2":
            stakes = calculate_stakes(example_odds, ox, o1)
        else:
            # favorite == "X"
            stakes = calculate_stakes(o1, example_odds, o2)

        # Overround: how far the implied probabilities exceed 100%
        overround = (1 / o1 + 1 / ox + 1 / o2) - 1

        opportunities.append(
            ArbOpportunity(
                home_team=event.home_team,
                away_team=event.away_team,
                league=event.league,
                start_time=event.start_time.strftime("%a %H:%M"),
                favorite=favorite,
                current_fav_odds=fav_odds,
                draw_odds=ox,
                away_odds=o2,
                home_odds=o1,
                target_fav_odds=target_snapped,
                example_fav_odds=example_odds,
                overround=overround,
                stakes=stakes,
            )
        )

    # Sort by overround (ascending — closest to fair odds first)
    opportunities.sort(key=lambda o: o.overround)
    return opportunities
