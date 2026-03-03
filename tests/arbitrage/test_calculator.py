"""Tests for the arbitrage calculator module."""

from datetime import datetime, timezone

import pytest

from src.arbitrage.calculator import (
    _find_1x2_odds,
    calculate_event_overrounds,
    calculate_margin,
    calculate_stakes,
    calculate_target_odds,
    calculate_winner_overround,
    find_underdog_arbs,
    snap_to_tick,
)
from src.models.events import Event, Market, Outcome, Sport

# ---------------------------------------------------------------------------
# snap_to_tick
# ---------------------------------------------------------------------------


def test_snap_to_tick_rounds_up():
    """Odds like 1.678 should snap up to 1.70."""
    assert snap_to_tick(1.678) == pytest.approx(1.70)


def test_snap_to_tick_exact_value():
    """Odds already on a tick should stay unchanged."""
    assert snap_to_tick(2.50) == pytest.approx(2.50)


def test_snap_to_tick_rounds_down():
    """Direction 'down' should round down."""
    assert snap_to_tick(1.678, direction="down") == pytest.approx(1.65)


def test_snap_to_tick_small_increment():
    """1.01 should snap up to 1.05."""
    assert snap_to_tick(1.01) == pytest.approx(1.05)


# ---------------------------------------------------------------------------
# calculate_target_odds
# ---------------------------------------------------------------------------


def test_calculate_target_odds_basic():
    """Known example: draw 4.50, away 5.50 → target ≈ 1.678."""
    # 1 / (1 - 1/4.50 - 1/5.50) = 1 / 0.5960 ≈ 1.678
    target = calculate_target_odds(4.50, 5.50)
    assert target is not None
    assert target == pytest.approx(1.678, abs=0.01)


def test_calculate_target_odds_high_underdog_odds():
    """Higher underdog odds → lower target (easier to hit)."""
    target = calculate_target_odds(6.00, 8.00)
    assert target is not None
    # 1 - 1/6 - 1/8 = 1 - 0.1667 - 0.125 = 0.7083 → 1/0.7083 ≈ 1.412
    assert target == pytest.approx(1.412, abs=0.01)


def test_calculate_target_odds_impossible():
    """When 1/oX + 1/o2 >= 1, no arbitrage is possible."""
    # 1/1.50 + 1/2.00 = 0.667 + 0.500 = 1.167 > 1
    assert calculate_target_odds(1.50, 2.00) is None


def test_calculate_target_odds_barely_impossible():
    """Exactly 1.0 implied probability → impossible."""
    # 1/2.0 + 1/2.0 = 1.0
    assert calculate_target_odds(2.0, 2.0) is None


def test_calculate_target_odds_barely_possible():
    """Just under 1.0 implied probability → very high target."""
    # 1/2.0 + 1/2.1 ≈ 0.500 + 0.476 = 0.976 → target ≈ 41.67
    target = calculate_target_odds(2.0, 2.1)
    assert target is not None
    assert target > 40


# ---------------------------------------------------------------------------
# calculate_stakes
# ---------------------------------------------------------------------------


def test_calculate_stakes_sum_to_budget():
    """Stakes should sum to approximately the budget."""
    stakes = calculate_stakes(2.50, 4.50, 5.50)
    total = stakes["1"] + stakes["X"] + stakes["2"]
    assert total == pytest.approx(100.0, abs=0.5)


def test_calculate_stakes_equal_payout():
    """Each outcome should pay out the same amount."""
    o1, ox, o2 = 2.50, 4.50, 5.50
    stakes = calculate_stakes(o1, ox, o2, budget=100.0)
    payout_1 = stakes["1"] * o1
    payout_x = stakes["X"] * ox
    payout_2 = stakes["2"] * o2
    assert payout_1 == pytest.approx(payout_x, abs=0.6)
    assert payout_x == pytest.approx(payout_2, abs=0.6)


def test_calculate_stakes_custom_budget():
    """Stakes should scale with budget."""
    stakes_100 = calculate_stakes(2.00, 3.00, 4.00, budget=100.0)
    stakes_200 = calculate_stakes(2.00, 3.00, 4.00, budget=200.0)
    assert stakes_200["1"] == pytest.approx(stakes_100["1"] * 2, abs=0.5)


def test_calculate_stakes_favorite_gets_largest():
    """The favorite (lowest odds) should get the largest stake."""
    stakes = calculate_stakes(1.50, 4.50, 5.50)
    assert stakes["1"] > stakes["X"]
    assert stakes["1"] > stakes["2"]


# ---------------------------------------------------------------------------
# calculate_margin
# ---------------------------------------------------------------------------


def test_calculate_margin_positive():
    """When sum of implied probs < 1, margin is positive (arb exists)."""
    # 1/3 + 1/4 + 1/5 = 0.333 + 0.25 + 0.2 = 0.783 → margin 0.217
    margin = calculate_margin(3.0, 4.0, 5.0)
    assert margin == pytest.approx(0.217, abs=0.01)


def test_calculate_margin_negative():
    """Typical bookmaker odds have negative margin (overround)."""
    margin = calculate_margin(1.90, 3.40, 4.00)
    assert margin < 0


# ---------------------------------------------------------------------------
# find_underdog_arbs
# ---------------------------------------------------------------------------


def _make_event(
    home: str,
    away: str,
    o1: float,
    ox: float,
    o2: float,
    league: str = "Test League",
) -> Event:
    """Helper to create an Event with a 1X2 Final Result market."""
    return Event(
        id=f"urn:test:{home.lower()}_{away.lower()}",
        sport=Sport.FOOTBALL,
        league=league,
        home_team=home,
        away_team=away,
        start_time=datetime(2026, 3, 15, 15, 30, tzinfo=timezone.utc),
        markets=[
            Market(
                name="Final Result",
                outcomes=[
                    Outcome(name="1", odds=o1),
                    Outcome(name="X", odds=ox),
                    Outcome(name="2", odds=o2),
                ],
            ),
        ],
        provider="test",
    )


def test_find_underdog_arbs_finds_mild_favorite():
    """A home favorite at 1.50 should be detected."""
    events = [
        _make_event("Bayern", "Dortmund", 1.50, 4.50, 5.50),
    ]
    opps = find_underdog_arbs(events)
    assert len(opps) == 1
    assert opps[0].favorite == "1"
    assert opps[0].home_team == "Bayern"
    # Raw target ≈ 1.678, snapped up to nearest 0.05 → 1.70
    assert opps[0].target_fav_odds == pytest.approx(1.70)
    # Example odds: raw ≈ 1.678 * 1.05 ≈ 1.762, snapped up → 1.80
    assert opps[0].example_fav_odds == pytest.approx(1.80)


def test_find_underdog_arbs_away_favorite():
    """An away favorite should also be detected."""
    events = [
        _make_event("Norwich", "Man City", 8.00, 5.00, 1.40),
    ]
    opps = find_underdog_arbs(events)
    assert len(opps) == 1
    assert opps[0].favorite == "2"


def test_find_underdog_arbs_filters_by_odds_range():
    """Events outside the min/max range should be excluded."""
    events = [
        _make_event("TeamA", "TeamB", 1.10, 8.00, 12.00),  # too low
        _make_event("TeamC", "TeamD", 1.50, 4.50, 5.50),  # in range
        _make_event("TeamE", "TeamF", 2.00, 3.20, 3.50),  # too high
    ]
    opps = find_underdog_arbs(events, min_fav_odds=1.20, max_fav_odds=1.80)
    assert len(opps) == 1
    assert opps[0].home_team == "TeamC"


def test_find_underdog_arbs_no_1x2_market():
    """Events without a Final Result market should be skipped."""
    event = Event(
        id="urn:test:no_market",
        sport=Sport.FOOTBALL,
        league="Test",
        home_team="A",
        away_team="B",
        start_time=datetime(2026, 3, 15, 15, 30, tzinfo=timezone.utc),
        markets=[],
        provider="test",
    )
    opps = find_underdog_arbs([event])
    assert len(opps) == 0


def test_find_underdog_arbs_stakes_present():
    """Opportunities should include stake allocations."""
    events = [_make_event("Bayern", "Dortmund", 1.50, 4.50, 5.50)]
    opps = find_underdog_arbs(events)
    assert len(opps) == 1
    stakes = opps[0].stakes
    assert "1" in stakes
    assert "X" in stakes
    assert "2" in stakes
    total = stakes["1"] + stakes["X"] + stakes["2"]
    assert total == pytest.approx(100.0, abs=0.5)


# ---------------------------------------------------------------------------
# _find_1x2_odds — market name aliases
# ---------------------------------------------------------------------------


def _make_event_with_market_name(market_name: str) -> Event:
    """Helper to create an Event with a custom market name for 1X2 odds."""
    return Event(
        id="urn:test:alias",
        sport=Sport.FOOTBALL,
        league="Test",
        home_team="Home",
        away_team="Away",
        start_time=datetime(2026, 3, 15, 15, 30, tzinfo=timezone.utc),
        markets=[
            Market(
                name=market_name,
                outcomes=[
                    Outcome(name="1", odds=1.50),
                    Outcome(name="X", odds=3.80),
                    Outcome(name="2", odds=5.00),
                ],
            ),
        ],
        provider="test",
    )


@pytest.mark.parametrize(
    "market_name",
    ["Final Result", "1x2", "1X2", "3-Weg", "3-way (regular playing time)"],
)
def test_find_1x2_odds_accepts_market_aliases(market_name: str):
    """_find_1x2_odds should match all known market name aliases."""
    event = _make_event_with_market_name(market_name)
    result = _find_1x2_odds(event)
    assert result == (1.50, 3.80, 5.00)


def test_find_1x2_odds_rejects_unknown_market():
    """_find_1x2_odds should return None for unrecognised market names."""
    event = _make_event_with_market_name("Over/Under 2.5")
    assert _find_1x2_odds(event) is None


def test_find_underdog_arbs_with_1x2_market_name():
    """find_underdog_arbs should work when market is named '1x2'."""
    event = _make_event_with_market_name("1x2")
    opps = find_underdog_arbs([event])
    assert len(opps) == 1
    assert opps[0].favorite == "1"


def test_find_underdog_arbs_with_basketball_market_name():
    """find_underdog_arbs should work with basketball 3-way market name."""
    event = _make_event_with_market_name("3-way (regular playing time)")
    opps = find_underdog_arbs([event])
    assert len(opps) == 1


# ---------------------------------------------------------------------------
# calculate_winner_overround
# ---------------------------------------------------------------------------


def _make_f1_event(
    name: str,
    market_name: str = "Winner",
    odds: list[float] | None = None,
) -> Event:
    """Helper to create an F1-style event with an N-way winner market."""
    if odds is None:
        odds = [2.50, 3.00, 5.00, 10.00, 15.00]
    return Event(
        id=f"urn:test:{name.lower().replace(' ', '_')}",
        sport=Sport.MOTOR_SPORTS,
        league="Formula 1",
        home_team=name,
        start_time=datetime(2026, 3, 15, 15, 30, tzinfo=timezone.utc),
        markets=[
            Market(
                name=market_name,
                outcomes=[
                    Outcome(name=f"Driver {i+1}", odds=o)
                    for i, o in enumerate(odds)
                ],
            ),
        ],
        provider="test",
    )


def test_calculate_winner_overround_basic():
    """Overround for known odds should match manual calculation."""
    # 1/2.5 + 1/3.0 + 1/5.0 + 1/10.0 + 1/15.0 = 0.4+0.333+0.2+0.1+0.067 = 1.1 -> 10%
    event = _make_f1_event("Australian GP", odds=[2.50, 3.00, 5.00, 10.00, 15.00])
    results = calculate_winner_overround([event])
    assert len(results) == 1
    assert results[0].event_name == "Australian GP"
    assert results[0].market_name == "Winner"
    assert results[0].selection_count == 5
    assert results[0].overround == pytest.approx(0.10, abs=0.01)


def test_calculate_winner_overround_multiple_events():
    """Multiple events should all be returned, sorted by overround."""
    event_low = _make_f1_event("Low Over GP", odds=[3.00, 4.00, 5.00])
    # 1/3 + 1/4 + 1/5 = 0.333 + 0.25 + 0.2 = 0.783 -> -21.7% (under-round)
    event_high = _make_f1_event("High Over GP", odds=[1.50, 2.00, 3.00])
    # 1/1.5 + 1/2.0 + 1/3.0 = 0.667 + 0.5 + 0.333 = 1.5 -> 50%
    results = calculate_winner_overround([event_high, event_low])
    assert len(results) == 2
    # Should be sorted ascending: low overround first
    assert results[0].event_name == "Low Over GP"
    assert results[1].event_name == "High Over GP"


def test_calculate_winner_overround_no_markets():
    """Events without markets should be skipped."""
    event = Event(
        id="urn:test:no_market_f1",
        sport=Sport.MOTOR_SPORTS,
        league="Formula 1",
        home_team="Empty GP",
        start_time=datetime(2026, 3, 15, 15, 30, tzinfo=timezone.utc),
        markets=[],
        provider="test",
    )
    results = calculate_winner_overround([event])
    assert len(results) == 0


def test_calculate_winner_overround_skips_two_way_markets():
    """Markets with fewer than 3 outcomes should be skipped."""
    event = Event(
        id="urn:test:two_way",
        sport=Sport.MOTOR_SPORTS,
        league="Formula 1",
        home_team="Two-Way GP",
        start_time=datetime(2026, 3, 15, 15, 30, tzinfo=timezone.utc),
        markets=[
            Market(
                name="Head to Head",
                outcomes=[
                    Outcome(name="Driver A", odds=1.80),
                    Outcome(name="Driver B", odds=2.00),
                ],
            ),
        ],
        provider="test",
    )
    results = calculate_winner_overround([event])
    assert len(results) == 0


# ---------------------------------------------------------------------------
# calculate_event_overrounds
# ---------------------------------------------------------------------------


def test_calculate_event_overrounds_all_markets():
    """Should return overround for every market with 2+ outcomes."""
    event = Event(
        id="urn:test:multi_market",
        sport=Sport.MOTOR_SPORTS,
        league="Formula 1",
        home_team="Australian GP",
        start_time=datetime(2026, 3, 15, 15, 30, tzinfo=timezone.utc),
        markets=[
            Market(
                name="Winner",
                outcomes=[
                    Outcome(name="Driver A", odds=2.50),
                    Outcome(name="Driver B", odds=3.00),
                    Outcome(name="Driver C", odds=5.00),
                ],
            ),
            Market(
                name="Head to Head",
                outcomes=[
                    Outcome(name="Driver A", odds=1.80),
                    Outcome(name="Driver B", odds=2.00),
                ],
            ),
            Market(
                name="Single outcome",
                outcomes=[
                    Outcome(name="Yes", odds=1.50),
                ],
            ),
        ],
        provider="test",
    )
    results = calculate_event_overrounds(event)
    # Single-outcome market should be excluded
    assert len(results) == 2
    market_names = {r.market_name for r in results}
    assert "Winner" in market_names
    assert "Head to Head" in market_names
