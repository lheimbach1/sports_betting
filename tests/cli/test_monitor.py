"""Tests for the odds monitor — pure logic only, no browser."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from src.cli.monitor import (
    Alert,
    AlertStatus,
    Direction,
    _escape_applescript,
    _match_time,
    render_summary,
    send_notification,
)

# ---------------------------------------------------------------------------
# Alert.check — direction logic
# ---------------------------------------------------------------------------


class TestAlertCheck:
    def test_gte_triggers_when_above(self) -> None:
        a = Alert(
            event_id="e1", event_label="A vs B", market_name="M",
            outcome_name="A", odds_key="M:A", direction=Direction.GTE,
            threshold=2.00,
        )
        assert a.check(2.10) is True

    def test_gte_triggers_when_equal(self) -> None:
        a = Alert(
            event_id="e1", event_label="A vs B", market_name="M",
            outcome_name="A", odds_key="M:A", direction=Direction.GTE,
            threshold=2.00,
        )
        assert a.check(2.00) is True

    def test_gte_does_not_trigger_below(self) -> None:
        a = Alert(
            event_id="e1", event_label="A vs B", market_name="M",
            outcome_name="A", odds_key="M:A", direction=Direction.GTE,
            threshold=2.00,
        )
        assert a.check(1.90) is False

    def test_lte_triggers_when_below(self) -> None:
        a = Alert(
            event_id="e1", event_label="A vs B", market_name="M",
            outcome_name="A", odds_key="M:A", direction=Direction.LTE,
            threshold=3.00,
        )
        assert a.check(2.80) is True

    def test_lte_triggers_when_equal(self) -> None:
        a = Alert(
            event_id="e1", event_label="A vs B", market_name="M",
            outcome_name="A", odds_key="M:A", direction=Direction.LTE,
            threshold=3.00,
        )
        assert a.check(3.00) is True

    def test_lte_does_not_trigger_above(self) -> None:
        a = Alert(
            event_id="e1", event_label="A vs B", market_name="M",
            outcome_name="A", odds_key="M:A", direction=Direction.LTE,
            threshold=3.00,
        )
        assert a.check(3.50) is False

    def test_check_does_not_trigger_when_not_watching(self) -> None:
        a = Alert(
            event_id="e1", event_label="A vs B", market_name="M",
            outcome_name="A", odds_key="M:A", direction=Direction.GTE,
            threshold=2.00, status=AlertStatus.TRIGGERED,
        )
        assert a.check(5.00) is False

    def test_check_updates_current_odds(self) -> None:
        a = Alert(
            event_id="e1", event_label="A vs B", market_name="M",
            outcome_name="A", odds_key="M:A", direction=Direction.GTE,
            threshold=2.00,
        )
        a.check(1.50)
        assert a.current_odds == 1.50


# ---------------------------------------------------------------------------
# Cooldown behaviour
# ---------------------------------------------------------------------------


class TestCooldown:
    def _make_alert(self, cooldown: float = 60.0) -> Alert:
        return Alert(
            event_id="e1", event_label="A vs B", market_name="M",
            outcome_name="A", odds_key="M:A", direction=Direction.GTE,
            threshold=2.00, cooldown_seconds=cooldown,
        )

    def test_fire_sets_triggered(self) -> None:
        a = self._make_alert()
        a.fire()
        assert a.status == AlertStatus.TRIGGERED
        assert a.last_triggered > 0

    def test_cooldown_prevents_retrigger(self) -> None:
        a = self._make_alert()
        assert a.check(2.50) is True
        a.fire()
        # Still in TRIGGERED, should not fire again
        assert a.check(3.00) is False

    def test_cooldown_expires_after_timeout(self) -> None:
        a = self._make_alert(cooldown=0.01)
        a.check(2.50)
        a.fire()
        time.sleep(0.02)
        a.maybe_exit_cooldown()
        assert a.status == AlertStatus.WATCHING
        # Now it can fire again
        assert a.check(2.50) is True

    def test_cooldown_resets_when_odds_cross_back(self) -> None:
        a = self._make_alert(cooldown=999.0)  # long cooldown
        a.check(2.50)
        a.fire()
        # Odds drop back below threshold
        a.current_odds = 1.80
        a.maybe_exit_cooldown()
        assert a.status == AlertStatus.WATCHING


# ---------------------------------------------------------------------------
# AppleScript escaping
# ---------------------------------------------------------------------------


class TestEscapeApplescript:
    def test_escapes_quotes(self) -> None:
        assert _escape_applescript('say "hello"') == 'say \\"hello\\"'

    def test_escapes_backslashes(self) -> None:
        assert _escape_applescript("a\\b") == "a\\\\b"

    def test_plain_text_unchanged(self) -> None:
        assert _escape_applescript("hello world") == "hello world"

    def test_both_quotes_and_backslashes(self) -> None:
        assert _escape_applescript('"a\\b"') == '\\"a\\\\b\\"'


# ---------------------------------------------------------------------------
# send_notification — subprocess mock
# ---------------------------------------------------------------------------


class TestSendNotification:
    @patch("src.cli.monitor.subprocess.Popen")
    def test_calls_osascript(self, mock_popen: object) -> None:
        send_notification("Title", "Message")
        assert mock_popen.called  # type: ignore[union-attr]
        args = mock_popen.call_args  # type: ignore[union-attr]
        cmd = args[0][0]  # positional arg: the command list
        assert cmd[0] == "osascript"
        assert cmd[1] == "-e"
        assert "Title" in cmd[2]
        assert "Message" in cmd[2]
        assert "display alert" in cmd[2]

    @patch("src.cli.monitor.subprocess.Popen")
    def test_escapes_special_chars_in_args(self, mock_popen: object) -> None:
        send_notification('Ti"tle', 'Msg "with" quotes')
        args = mock_popen.call_args  # type: ignore[union-attr]
        script = args[0][0][2]
        assert '\\"' in script
        # Original unescaped quotes should not appear
        assert 'Ti"tle' not in script


# ---------------------------------------------------------------------------
# render_summary
# ---------------------------------------------------------------------------


class TestRenderSummary:
    def _make_alerts(self) -> list[Alert]:
        a1 = Alert(
            event_id="e1", event_label="Real Madrid vs Getafe",
            market_name="Final Result", outcome_name="Real Madrid",
            odds_key="Final Result:Real Madrid", direction=Direction.GTE,
            threshold=2.00, current_odds=1.85,
        )
        a2 = Alert(
            event_id="e1", event_label="Real Madrid vs Getafe",
            market_name="Final Result", outcome_name="Draw",
            odds_key="Final Result:Draw", direction=Direction.LTE,
            threshold=3.20, current_odds=3.40,
            status=AlertStatus.TRIGGERED,
        )
        return [a1, a2]

    def test_contains_header(self) -> None:
        out = render_summary(self._make_alerts(), 42)
        assert "Odds Monitor" in out
        assert "updates: 42" in out

    def test_contains_all_alert_info(self) -> None:
        out = render_summary(self._make_alerts(), 0)
        assert "Real Madrid vs Getafe" in out
        assert "Final Result" in out
        assert "Real Madrid" in out
        assert "Draw" in out
        assert ">=" in out
        assert "<=" in out
        assert "2.00" in out
        assert "3.20" in out

    def test_shows_correct_status(self) -> None:
        out = render_summary(self._make_alerts(), 0)
        assert "watching" in out
        assert "TRIGGERED" in out

    def test_shows_current_odds(self) -> None:
        out = render_summary(self._make_alerts(), 0)
        assert "1.85" in out
        assert "3.40" in out

    def test_shows_ctrl_c_hint(self) -> None:
        out = render_summary([], 0)
        assert "Ctrl+C" in out

    @pytest.mark.parametrize("count", [0, 1, 999])
    def test_update_count_shown(self, count: int) -> None:
        out = render_summary([], count)
        assert f"updates: {count}" in out

    def test_shows_match_time(self) -> None:
        alerts = self._make_alerts()
        times = {"e1": "2H 67'"}
        out = render_summary(alerts, 1, match_times=times)
        assert "2H 67'" in out

    def test_shows_time_column_header(self) -> None:
        out = render_summary(self._make_alerts(), 0)
        assert "Time" in out


# ---------------------------------------------------------------------------
# _match_time
# ---------------------------------------------------------------------------


class TestMatchTime:
    def test_halftime_label(self) -> None:
        assert _match_time({"phase": "asw:phase:8"}) == "HT"

    def test_full_time_label(self) -> None:
        assert _match_time({"phase": "asw:phase:14"}) == "FT"

    def test_pre_match_label(self) -> None:
        assert _match_time({"phase": "asw:phase:1"}) == "Pre"

    def test_unknown_phase_empty(self) -> None:
        assert _match_time({"phase": "asw:phase:999"}) == ""

    def test_no_phase_empty(self) -> None:
        assert _match_time({}) == ""

    def test_first_half_shows_minute(self) -> None:
        from datetime import datetime, timezone

        # Started 20 minutes ago
        start = datetime.now(tz=timezone.utc).replace(microsecond=0)
        start = start.replace(
            minute=start.minute - 20 if start.minute >= 20 else start.minute + 40,
            hour=start.hour if start.minute >= 20 else start.hour - 1,
        )
        raw = {
            "phase": "asw:phase:6",
            "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        result = _match_time(raw)
        assert result.startswith("1H")
        assert "'" in result
