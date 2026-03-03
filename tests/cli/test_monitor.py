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
    _split_name_count,
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
    @patch("src.cli.monitor.sys")
    @patch("src.cli.monitor.subprocess.Popen")
    def test_macos_calls_osascript_and_afplay(
        self, mock_popen: object, mock_sys: object,
    ) -> None:
        mock_sys.platform = "darwin"  # type: ignore[union-attr]
        send_notification("Title", "Message")
        calls = mock_popen.call_args_list  # type: ignore[union-attr]
        assert len(calls) == 2
        # First call: display alert
        cmd_alert = calls[0][0][0]
        assert cmd_alert[0] == "osascript"
        assert "display alert" in cmd_alert[2]
        assert "Title" in cmd_alert[2]
        assert "Message" in cmd_alert[2]
        # Second call: afplay sound
        cmd_sound = calls[1][0][0]
        assert cmd_sound[0] == "afplay"

    @patch("src.cli.monitor.sys")
    @patch("src.cli.monitor.subprocess.Popen")
    def test_macos_escapes_special_chars(
        self, mock_popen: object, mock_sys: object,
    ) -> None:
        mock_sys.platform = "darwin"  # type: ignore[union-attr]
        send_notification('Ti"tle', 'Msg "with" quotes')
        calls = mock_popen.call_args_list  # type: ignore[union-attr]
        script = calls[0][0][0][2]
        assert '\\"' in script
        assert 'Ti"tle' not in script

    @patch("src.cli.monitor.sys")
    @patch("src.cli.monitor.subprocess.Popen")
    def test_windows_calls_powershell(
        self, mock_popen: object, mock_sys: object,
    ) -> None:
        mock_sys.platform = "win32"  # type: ignore[union-attr]
        send_notification("Title", "Message")
        assert mock_popen.called  # type: ignore[union-attr]
        cmd = mock_popen.call_args[0][0]  # type: ignore[union-attr]
        assert cmd[0] == "powershell"
        # Command string should contain both title and message
        ps_cmd = cmd[-1]
        assert "Title" in ps_cmd
        assert "Message" in ps_cmd
        assert "MessageBox" in ps_cmd


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
        times = {"e1": "2H ~67'"}
        out = render_summary(alerts, 1, match_times=times)
        assert "2H ~67'" in out

    def test_shows_market_odds(self) -> None:
        alerts = self._make_alerts()
        mkt = {
            "Final Result:Real Madrid": [
                ("Real Madrid", 1.85), ("Draw", 3.40), ("Getafe", 4.20),
            ],
        }
        out = render_summary(alerts, 1, market_odds=mkt)
        assert "3.40" in out
        assert "4.20" in out
        assert "Getafe" in out

    def test_shows_all_outcomes_for_each_alert(self) -> None:
        alerts = self._make_alerts()
        mkt = {
            "Final Result:Real Madrid": [
                ("Real Madrid", 1.85), ("Draw", 3.40), ("Getafe", 4.20),
            ],
            "Final Result:Draw": [
                ("Real Madrid", 1.85), ("Draw", 3.40), ("Getafe", 4.20),
            ],
        }
        out = render_summary(alerts, 1, market_odds=mkt)
        # Both alerts should show the full market line
        assert out.count("Getafe") >= 2


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

    def test_second_half_shows_minute(self) -> None:
        from datetime import datetime, timedelta, timezone

        # Started 75 minutes ago -> should be around 2H ~57'
        start = datetime.now(tz=timezone.utc) - timedelta(minutes=75)
        raw = {
            "phase": "asw:phase:7",
            "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        result = _match_time(raw)
        assert result.startswith("2H")
        assert "~" in result
        # Should be ≥ 45 and reasonable
        minute = int(result.split("~")[1].rstrip("'"))
        assert 50 <= minute <= 65

    def test_extra_time_first_half(self) -> None:
        from datetime import datetime, timedelta, timezone

        start = datetime.now(tz=timezone.utc) - timedelta(minutes=130)
        raw = {
            "phase": "asw:phase:9",
            "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        result = _match_time(raw)
        assert result.startswith("ET1")
        assert "~" in result
        minute = int(result.split("~")[1].rstrip("'"))
        assert minute >= 90

    def test_extra_time_second_half(self) -> None:
        from datetime import datetime, timedelta, timezone

        start = datetime.now(tz=timezone.utc) - timedelta(minutes=148)
        raw = {
            "phase": "asw:phase:10",
            "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        result = _match_time(raw)
        assert result.startswith("ET2")
        assert "~" in result
        minute = int(result.split("~")[1].rstrip("'"))
        assert minute >= 105

    def test_invalid_start_time_returns_label_only(self) -> None:
        raw = {"phase": "asw:phase:6", "startTime": "not-a-date"}
        assert _match_time(raw) == "1H"

    def test_missing_start_time_returns_label_only(self) -> None:
        raw = {"phase": "asw:phase:7"}
        assert _match_time(raw) == "2H"

    def test_iso_format_without_z_suffix(self) -> None:
        from datetime import datetime, timedelta, timezone

        start = datetime.now(tz=timezone.utc) - timedelta(minutes=10)
        raw = {
            "phase": "asw:phase:6",
            "startTime": start.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        }
        result = _match_time(raw)
        assert result.startswith("1H")
        assert "~" in result

    def test_penalty_phase_label(self) -> None:
        assert _match_time({"phase": "asw:phase:11"}) == "Pen"

    def test_first_half_capped_at_50(self) -> None:
        from datetime import datetime, timedelta, timezone

        # Started 55 minutes ago — should cap at 50
        start = datetime.now(tz=timezone.utc) - timedelta(minutes=55)
        raw = {
            "phase": "asw:phase:6",
            "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        result = _match_time(raw)
        minute = int(result.split("~")[1].rstrip("'"))
        assert minute <= 50


# ---------------------------------------------------------------------------
# Alert — additional edge cases
# ---------------------------------------------------------------------------


class TestAlertEdgeCases:
    def test_check_does_not_trigger_when_in_cooldown(self) -> None:
        a = Alert(
            event_id="e1", event_label="A vs B", market_name="M",
            outcome_name="A", odds_key="M:A", direction=Direction.GTE,
            threshold=2.00, status=AlertStatus.COOLDOWN,
        )
        assert a.check(5.00) is False

    def test_check_always_updates_current_odds_even_when_not_triggered(self) -> None:
        a = Alert(
            event_id="e1", event_label="A vs B", market_name="M",
            outcome_name="A", odds_key="M:A", direction=Direction.GTE,
            threshold=2.00, status=AlertStatus.TRIGGERED,
        )
        a.check(3.50)
        assert a.current_odds == 3.50

    def test_odds_crossed_back_lte(self) -> None:
        """LTE alert resets when odds rise back above threshold."""
        a = Alert(
            event_id="e1", event_label="A vs B", market_name="M",
            outcome_name="A", odds_key="M:A", direction=Direction.LTE,
            threshold=3.00, cooldown_seconds=999.0,
        )
        a.check(2.50)
        a.fire()
        # Odds rise back above threshold
        a.current_odds = 3.50
        a.maybe_exit_cooldown()
        assert a.status == AlertStatus.WATCHING

    def test_odds_not_crossed_back_stays_in_cooldown(self) -> None:
        """GTE alert stays in cooldown when odds remain above threshold."""
        a = Alert(
            event_id="e1", event_label="A vs B", market_name="M",
            outcome_name="A", odds_key="M:A", direction=Direction.GTE,
            threshold=2.00, cooldown_seconds=999.0,
        )
        a.check(2.50)
        a.fire()
        a.maybe_exit_cooldown()
        assert a.status == AlertStatus.COOLDOWN

    def test_default_values(self) -> None:
        a = Alert(
            event_id="e1", event_label="A vs B", market_name="M",
            outcome_name="A", odds_key="M:A", direction=Direction.GTE,
            threshold=2.00,
        )
        assert a.status == AlertStatus.WATCHING
        assert a.current_odds == 0.0
        assert a.cooldown_seconds == 60.0
        assert a.last_triggered == 0.0

    def test_fire_records_monotonic_time(self) -> None:
        a = Alert(
            event_id="e1", event_label="A vs B", market_name="M",
            outcome_name="A", odds_key="M:A", direction=Direction.GTE,
            threshold=2.00,
        )
        before = time.monotonic()
        a.fire()
        after = time.monotonic()
        assert before <= a.last_triggered <= after


# ---------------------------------------------------------------------------
# Direction / AlertStatus enum values
# ---------------------------------------------------------------------------


class TestEnums:
    def test_direction_gte_value(self) -> None:
        assert Direction.GTE.value == ">="
        assert str(Direction.GTE) == "Direction.GTE"

    def test_direction_lte_value(self) -> None:
        assert Direction.LTE.value == "<="

    def test_alert_status_values(self) -> None:
        assert AlertStatus.WATCHING.value == "watching"
        assert AlertStatus.TRIGGERED.value == "TRIGGERED"
        assert AlertStatus.COOLDOWN.value == "cooldown"


# ---------------------------------------------------------------------------
# send_notification — additional cases
# ---------------------------------------------------------------------------


class TestSendNotificationEdgeCases:
    @patch("src.cli.monitor.sys")
    @patch("src.cli.monitor.subprocess.Popen")
    def test_unsupported_platform_does_nothing(
        self, mock_popen: object, mock_sys: object,
    ) -> None:
        mock_sys.platform = "linux"  # type: ignore[union-attr]
        send_notification("Title", "Message")
        assert not mock_popen.called  # type: ignore[union-attr]

    @patch("src.cli.monitor.sys")
    @patch("src.cli.monitor.subprocess.Popen", side_effect=FileNotFoundError)
    def test_macos_file_not_found_does_not_raise(
        self, mock_popen: object, mock_sys: object,
    ) -> None:
        mock_sys.platform = "darwin"  # type: ignore[union-attr]
        # Should not raise
        send_notification("Title", "Message")

    @patch("src.cli.monitor.sys")
    @patch("src.cli.monitor.subprocess.Popen", side_effect=FileNotFoundError)
    def test_windows_file_not_found_does_not_raise(
        self, mock_popen: object, mock_sys: object,
    ) -> None:
        mock_sys.platform = "win32"  # type: ignore[union-attr]
        send_notification("Title", "Message")

    @patch("src.cli.monitor.sys")
    @patch("src.cli.monitor.subprocess.Popen")
    def test_windows_escapes_single_quotes(
        self, mock_popen: object, mock_sys: object,
    ) -> None:
        mock_sys.platform = "win32"  # type: ignore[union-attr]
        send_notification("It's", "O'Brien's bet")
        cmd = mock_popen.call_args[0][0]  # type: ignore[union-attr]
        ps_cmd = cmd[-1]
        assert "It''s" in ps_cmd
        assert "O''Brien''s bet" in ps_cmd


# ---------------------------------------------------------------------------
# render_summary — additional edge cases
# ---------------------------------------------------------------------------


class TestRenderSummaryEdgeCases:
    def test_empty_alerts(self) -> None:
        out = render_summary([], 0)
        assert "Odds Monitor" in out
        assert "Ctrl+C" in out

    def test_long_event_name_truncated(self) -> None:
        a = Alert(
            event_id="e1",
            event_label="A Very Long Team Name United vs Another Very Long Team Name City",
            market_name="M", outcome_name="Home",
            odds_key="M:Home", direction=Direction.GTE,
            threshold=2.00, current_odds=1.50,
        )
        out = render_summary([a], 0)
        assert ".." in out

    def test_zero_current_odds_shows_dash(self) -> None:
        a = Alert(
            event_id="e1", event_label="A vs B", market_name="M",
            outcome_name="A", odds_key="M:A", direction=Direction.GTE,
            threshold=2.00, current_odds=0.0,
        )
        out = render_summary([a], 0)
        assert " - " in out

    def test_cooldown_status_in_output(self) -> None:
        a = Alert(
            event_id="e1", event_label="A vs B", market_name="M",
            outcome_name="A", odds_key="M:A", direction=Direction.GTE,
            threshold=2.00, current_odds=2.10, status=AlertStatus.COOLDOWN,
        )
        out = render_summary([a], 0)
        assert "cooldown" in out

    def test_no_match_time_shows_empty_brackets(self) -> None:
        a = Alert(
            event_id="e1", event_label="A vs B", market_name="M",
            outcome_name="A", odds_key="M:A", direction=Direction.GTE,
            threshold=2.00, current_odds=1.50,
        )
        out = render_summary([a], 0)
        assert "[]" in out

    def test_market_odds_not_provided(self) -> None:
        a = Alert(
            event_id="e1", event_label="A vs B", market_name="M",
            outcome_name="A", odds_key="M:A", direction=Direction.GTE,
            threshold=2.00, current_odds=1.50,
        )
        # No market_odds passed — should not crash
        out = render_summary([a], 0, market_odds=None)
        assert "A vs B" in out

    def test_header_contains_current_time(self) -> None:
        from datetime import datetime

        out = render_summary([], 0)
        now_prefix = datetime.now().strftime("%H:%M")
        assert now_prefix in out


# ---------------------------------------------------------------------------
# _split_name_count
# ---------------------------------------------------------------------------


class TestSplitNameCount:
    def test_name_with_count(self) -> None:
        assert _split_name_count("Football\n3") == ("Football", "3")

    def test_name_without_count(self) -> None:
        assert _split_name_count("Tennis") == ("Tennis", "")

    def test_multiline_name_with_count(self) -> None:
        assert _split_name_count("Ice\nHockey\n5") == ("Ice Hockey", "5")

    def test_empty_string(self) -> None:
        assert _split_name_count("") == ("", "")

    def test_whitespace_only(self) -> None:
        assert _split_name_count("   \n  ") == ("", "")
