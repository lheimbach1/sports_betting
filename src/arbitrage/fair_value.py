"""Fair value estimation from multi-bookmaker odds.

Converts bookmaker odds to implied probabilities, removes overround,
and aggregates across bookmakers to derive consensus fair values.
"""

from __future__ import annotations

from statistics import median

from src.models.events import BookmakerOdds, FairValue


def remove_overround(odds: tuple[float, ...], method: str = "multiplicative") -> tuple[float, ...]:
    """Convert bookmaker odds to implied fair probabilities.

    Uses the multiplicative method: divide each implied probability by the
    sum of all implied probabilities so they sum to 1.0.

    Works for 2-way and 3-way markets.
    """
    implied = tuple(1.0 / o for o in odds)
    total = sum(implied)
    if total == 0:
        return tuple(1.0 / len(odds) for _ in odds)
    return tuple(p / total for p in implied)


def aggregate_fair_probs(
    odds_by_outcome: dict[str, list[BookmakerOdds]],
    method: str = "median",
) -> FairValue:
    """Aggregate multi-bookmaker odds into consensus fair probabilities.

    For each bookmaker, removes overround across all outcomes, then takes
    the median probability per outcome (robust to outliers). Re-normalizes
    to sum to 1.0.
    """
    outcome_names = tuple(odds_by_outcome.keys())
    n_outcomes = len(outcome_names)

    # Collect all bookmakers that have odds for every outcome.
    bookmaker_sets = [
        {bo.bookmaker for bo in odds_by_outcome[name]}
        for name in outcome_names
    ]
    common_bookmakers = set.intersection(*bookmaker_sets) if bookmaker_sets else set()

    if not common_bookmakers:
        # Fallback: use whatever odds are available per outcome.
        fair_probs_raw = []
        for name in outcome_names:
            bookie_odds = odds_by_outcome[name]
            if bookie_odds:
                fair_probs_raw.append(median(1.0 / bo.odds for bo in bookie_odds))
            else:
                fair_probs_raw.append(1.0 / n_outcomes)
        total = sum(fair_probs_raw)
        fair_probs = tuple(p / total for p in fair_probs_raw)
        sharp_odds = tuple(round(1.0 / p, 2) for p in fair_probs)
        return FairValue(outcome_names=outcome_names, fair_probs=fair_probs, sharp_odds=sharp_odds)

    # For each common bookmaker, remove overround and collect per-outcome probs.
    probs_per_outcome: dict[str, list[float]] = {name: [] for name in outcome_names}

    for bm in sorted(common_bookmakers):
        bm_odds = tuple(
            next(bo.odds for bo in odds_by_outcome[name] if bo.bookmaker == bm)
            for name in outcome_names
        )
        fair = remove_overround(bm_odds)
        for i, name in enumerate(outcome_names):
            probs_per_outcome[name].append(fair[i])

    # Take median per outcome and re-normalize.
    raw = [median(probs_per_outcome[name]) for name in outcome_names]
    total = sum(raw)
    fair_probs = tuple(p / total for p in raw)
    sharp_odds = tuple(round(1.0 / p, 2) for p in fair_probs)

    return FairValue(outcome_names=outcome_names, fair_probs=fair_probs, sharp_odds=sharp_odds)


def fair_value_from_two_providers(
    sp_odds: tuple[float, ...],
    pm_odds: tuple[float, ...],
    outcome_names: tuple[str, ...] | None = None,
) -> FairValue:
    """Derive fair value from Sporttip and Polymarket odds only.

    Removes overround from each provider independently, averages the fair
    probs, and re-normalizes to sum to 1.0.
    """
    sp_fair = remove_overround(sp_odds)
    pm_fair = remove_overround(pm_odds)

    avg = tuple((s + p) / 2.0 for s, p in zip(sp_fair, pm_fair))
    total = sum(avg)
    fair_probs = tuple(p / total for p in avg)
    sharp_odds = tuple(round(1.0 / p, 2) for p in fair_probs)

    if outcome_names is None:
        if len(fair_probs) == 3:
            outcome_names = ("1", "X", "2")
        else:
            outcome_names = tuple(str(i + 1) for i in range(len(fair_probs)))

    return FairValue(outcome_names=outcome_names, fair_probs=fair_probs, sharp_odds=sharp_odds)


def compute_edge(fair_value: FairValue, offered_odds: tuple[float, ...]) -> tuple[float, ...]:
    """Compute per-outcome edge: offered_odds_i * fair_prob_i - 1.

    Positive values indicate the offered odds exceed fair value (mispriced
    in the bettor's favor).
    """
    return tuple(
        odds * prob - 1.0
        for odds, prob in zip(offered_odds, fair_value.fair_probs)
    )
