"""Tests for the continuous arbitrage scanner."""

from src.cli.compare import ComparedMatch
from src.cli.scanner import ArbOpportunity, _update_arbs, find_expired, should_notify
from src.models.events import Sport

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_arb(
    match_key: str = "Arsenal vs Everton",
    margin: float = 3.0,
    last_notified: float = 0.0,
    first_seen: float = 1000.0,
    cooldown: float = 300.0,
) -> ArbOpportunity:
    return ArbOpportunity(
        match_key=match_key,
        league="Premier League",
        margin=margin,
        sp_odds=(1.50, 3.80, 5.00),
        pm_odds=(1.33, 4.26, 6.90),
        best_odds=(1.50, 4.26, 6.90),
        best_providers=("ST", "PM", "PM"),
        stakes=(62.5, 22.0, 15.5),
        first_seen=first_seen,
        last_notified=last_notified,
        cooldown=cooldown,
    )


def _make_compared(
    home: str = "Arsenal",
    away: str = "Everton",
    margin: float = 3.0,
    sp_odds: tuple[float, ...] = (1.50, 3.80, 5.00),
    pm_odds: tuple[float, ...] = (1.33, 4.26, 6.90),
) -> ComparedMatch:
    best = tuple(max(s, p) for s, p in zip(sp_odds, pm_odds))
    providers = tuple("ST" if s >= p else "PM" for s, p in zip(sp_odds, pm_odds))
    return ComparedMatch(
        home=home,
        away=away,
        league="Premier League",
        kickoff="Sat 15:30",
        sport=Sport.FOOTBALL,
        outcome_names=("1", "X", "2"),
        sp_odds=sp_odds,
        pm_odds=pm_odds,
        best_odds=best,
        best_providers=providers,
        stakes=(62.5, 22.0, 15.5),
        arb_margin=margin,
        sp_overround=5.0,
        pm_overround=3.0,
        pm_volume=100000.0,
    )


# ---------------------------------------------------------------------------
# should_notify
# ---------------------------------------------------------------------------

class TestShouldNotify:
    """Tests for the notification decision logic."""

    def test_new_arb_should_notify(self) -> None:
        """First time above threshold → True."""
        active: dict[str, ArbOpportunity] = {}
        assert should_notify(active, "Arsenal vs Everton", 3.0, 1.0, 1000.0) is True

    def test_no_notify_below_threshold(self) -> None:
        """Margin < min_margin → never notify."""
        active: dict[str, ArbOpportunity] = {}
        assert should_notify(active, "Arsenal vs Everton", 0.5, 1.0, 1000.0) is False

    def test_no_notify_at_exact_threshold(self) -> None:
        """Margin exactly equal to min_margin → not strictly above, no notify."""
        active: dict[str, ArbOpportunity] = {}
        assert should_notify(active, "Arsenal vs Everton", 1.0, 1.0, 1000.0) is False

    def test_cooldown_prevents_renotify(self) -> None:
        """Recently notified (within cooldown) → False."""
        now = 1100.0
        arb = _make_arb(last_notified=1000.0, cooldown=300.0)
        active = {arb.match_key: arb}
        # Only 100s passed, cooldown is 300s → should not notify
        assert should_notify(active, arb.match_key, 5.0, 1.0, now) is False

    def test_improved_margin_after_cooldown(self) -> None:
        """Margin +1pp and cooldown expired → True."""
        now = 1500.0
        arb = _make_arb(margin=3.0, last_notified=1000.0, cooldown=300.0)
        active = {arb.match_key: arb}
        # 500s > 300s cooldown, margin 5.0 > 3.0 + 1.0 → should notify
        assert should_notify(active, arb.match_key, 5.0, 1.0, now) is True

    def test_cooldown_expired_but_margin_not_improved(self) -> None:
        """Cooldown expired but margin only slightly higher → False."""
        now = 1500.0
        arb = _make_arb(margin=3.0, last_notified=1000.0, cooldown=300.0)
        active = {arb.match_key: arb}
        # 3.5 is NOT > 3.0 + 1.0 → should not notify
        assert should_notify(active, arb.match_key, 3.5, 1.0, now) is False

    def test_margin_improved_but_cooldown_not_expired(self) -> None:
        """Margin improved by >1pp but cooldown still active → False."""
        now = 1100.0
        arb = _make_arb(margin=3.0, last_notified=1000.0, cooldown=300.0)
        active = {arb.match_key: arb}
        assert should_notify(active, arb.match_key, 5.0, 1.0, now) is False


# ---------------------------------------------------------------------------
# find_expired
# ---------------------------------------------------------------------------

class TestFindExpired:
    """Tests for detecting expired arb opportunities."""

    def test_expired_arb_removed_when_missing(self) -> None:
        """Match no longer present in current data → expired."""
        arb = _make_arb()
        active = {arb.match_key: arb}
        expired = find_expired(active, set(), 1.0, {})
        assert arb.match_key in expired

    def test_expired_arb_removed_when_below_threshold(self) -> None:
        """Margin dropped below threshold → expired."""
        arb = _make_arb(margin=3.0)
        active = {arb.match_key: arb}
        current = {arb.match_key}
        margins = {arb.match_key: 0.5}
        expired = find_expired(active, current, 1.0, margins)
        assert arb.match_key in expired

    def test_active_arb_not_expired(self) -> None:
        """Arb still above threshold → not expired."""
        arb = _make_arb(margin=3.0)
        active = {arb.match_key: arb}
        current = {arb.match_key}
        margins = {arb.match_key: 2.5}
        expired = find_expired(active, current, 1.0, margins)
        assert expired == []


# ---------------------------------------------------------------------------
# _update_arbs
# ---------------------------------------------------------------------------

class TestUpdateArbs:
    """Integration tests for the arb update pipeline."""

    def test_new_arb_added_and_notified(self) -> None:
        """A new arb above threshold should be added to active and returned as notified."""
        active: dict[str, ArbOpportunity] = {}
        compared = [_make_compared(margin=3.0)]
        notified = _update_arbs(compared, active, 1.0, 1000.0)
        assert len(notified) == 1
        assert "Arsenal vs Everton" in active
        assert active["Arsenal vs Everton"].last_notified == 1000.0

    def test_below_threshold_not_added(self) -> None:
        """An arb below threshold should not be added."""
        active: dict[str, ArbOpportunity] = {}
        compared = [_make_compared(margin=0.5)]
        notified = _update_arbs(compared, active, 1.0, 1000.0)
        assert len(notified) == 0
        assert "Arsenal vs Everton" not in active

    def test_existing_arb_updated_without_renotify(self) -> None:
        """An existing arb within cooldown should update margin but not re-notify."""
        arb = _make_arb(margin=3.0, last_notified=900.0)
        active = {arb.match_key: arb}
        compared = [_make_compared(margin=3.5)]
        notified = _update_arbs(compared, active, 1.0, 1000.0)
        assert len(notified) == 0
        assert active["Arsenal vs Everton"].margin == 3.5

    def test_expired_arb_removed_on_update(self) -> None:
        """An arb that drops below threshold gets removed."""
        arb = _make_arb(margin=3.0, last_notified=500.0)
        active = {arb.match_key: arb}
        # New comparison has margin below threshold
        compared = [_make_compared(margin=0.2)]
        _update_arbs(compared, active, 1.0, 1000.0)
        assert arb.match_key not in active

    def test_disappeared_match_removes_arb(self) -> None:
        """An arb whose match is no longer present gets removed."""
        arb = _make_arb(match_key="Arsenal vs Everton")
        active = {arb.match_key: arb}
        # Different match in compared list
        compared = [_make_compared(home="Como", away="Inter", margin=2.0)]
        _update_arbs(compared, active, 1.0, 1000.0)
        assert "Arsenal vs Everton" not in active
        assert "Como vs Inter" in active
