"""Kelly Criterion position sizing for sports betting.

Pure functions for computing optimal bet sizes based on edge over fair value.
"""

from __future__ import annotations

from src.models.events import FairValue, KellyRecommendation


def kelly_fraction(odds: float, fair_prob: float) -> float:
    """Compute full Kelly fraction: f* = (b*p - q) / b.

    Where b = odds - 1, p = fair probability of winning, q = 1 - p.
    Returns 0.0 if edge <= 0. Clamped to [0, 1].
    """
    b = odds - 1.0
    if b <= 0:
        return 0.0
    p = fair_prob
    q = 1.0 - p
    f = (b * p - q) / b
    return max(0.0, min(1.0, f))


def fractional_kelly(full_fraction: float, multiplier: float = 0.5) -> float:
    """Apply fractional Kelly scaling.

    Half-Kelly (multiplier=0.5) is the common default: retains ~75% of the
    growth rate with ~50% of the variance.
    """
    return full_fraction * multiplier


def kelly_ev(odds: float, fair_prob: float, stake: float) -> float:
    """Expected value of a bet given odds, fair probability, and stake."""
    return stake * (odds * fair_prob - 1.0)


def analyze_market(
    fair_value: FairValue,
    sp_odds: tuple[float, ...],
    pm_odds: tuple[float, ...],
    bankroll: float,
    kelly_multiplier: float = 0.5,
    provider: str | None = "ST",
) -> list[KellyRecommendation]:
    """Analyze a market and return Kelly recommendations for positive-edge outcomes.

    For each outcome, selects odds based on the ``provider`` parameter:
    - ``"ST"`` (default): always use Sporttip odds
    - ``"PM"``: always use Polymarket odds
    - ``None``: pick best (highest) odds across providers

    Only returns outcomes with positive edge.
    """
    recommendations: list[KellyRecommendation] = []

    for i, name in enumerate(fair_value.outcome_names):
        sp_o = sp_odds[i] if i < len(sp_odds) else 0.0
        pm_o = pm_odds[i] if i < len(pm_odds) else 0.0

        if provider == "ST":
            best_odds = sp_o
            chosen_provider = "ST"
        elif provider == "PM":
            best_odds = pm_o
            chosen_provider = "PM"
        else:
            # Pick best odds across providers.
            if sp_o >= pm_o:
                best_odds = sp_o
                chosen_provider = "ST"
            else:
                best_odds = pm_o
                chosen_provider = "PM"

        if best_odds <= 1.0:
            continue

        fair_prob = fair_value.fair_probs[i]
        edge = best_odds * fair_prob - 1.0

        if edge <= 0:
            continue

        full_f = kelly_fraction(best_odds, fair_prob)
        frac_f = fractional_kelly(full_f, kelly_multiplier)
        stake = frac_f * bankroll
        ev = kelly_ev(best_odds, fair_prob, stake)

        recommendations.append(KellyRecommendation(
            outcome_name=name,
            provider=chosen_provider,
            offered_odds=best_odds,
            fair_prob=fair_prob,
            edge=edge,
            full_kelly_fraction=full_f,
            fractional_kelly=frac_f,
            expected_value=round(ev, 2),
        ))

    recommendations.sort(key=lambda r: r.edge, reverse=True)
    return recommendations
