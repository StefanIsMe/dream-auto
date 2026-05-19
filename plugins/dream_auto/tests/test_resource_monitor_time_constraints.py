"""
Tests for ResourceMonitor time window constraints.
"""

import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import pytz

# Mock dependencies before importing
import sys
sys.modules['psutil'] = MagicMock()

from resource_monitor import ResourceMonitor


class TestParseTimeWindow:
    """Test time window parsing."""

    def test_parse_valid_normal_window(self):
        rm = ResourceMonitor()
        result = rm._parse_time_window("09:00-18:00")
        assert result == (540, 1080)

    def test_parse_valid_midnight_wrap(self):
        rm = ResourceMonitor()
        result = rm._parse_time_window("22:00-06:00")
        assert result == (1320, 360)

    def test_parse_none_returns_none(self):
        rm = ResourceMonitor()
        result = rm._parse_time_window(None)
        assert result is None

    def test_parse_empty_string_returns_none(self):
        rm = ResourceMonitor()
        result = rm._parse_time_window("")
        assert result is None

    def test_parse_invalid_format_returns_none(self, capsys):
        rm = ResourceMonitor()
        result = rm._parse_time_window("22:00-06")  # Missing minute
        assert result is None
        captured = capsys.readouterr()
        assert "WARNING" in captured.out

    def test_parse_invalid_hour_returns_none(self, capsys):
        rm = ResourceMonitor()
        result = rm._parse_time_window("25:00-06:00")
        assert result is None
        captured = capsys.readouterr()
        assert "WARNING" in captured.out

    def test_parse_invalid_minute_returns_none(self, capsys):
        rm = ResourceMonitor()
        result = rm._parse_time_window("22:75-06:00")
        assert result is None
        captured = capsys.readouterr()
        assert "WARNING" in captured.out

    def test_parse_whitespace_tolerance(self):
        rm = ResourceMonitor()
        result = rm._parse_time_window("  22:00-06:00  ")
        assert result == (1320, 360)


class TestIsInTimeWindow:
    """Test time window membership check."""

    def test_in_normal_window(self):
        rm = ResourceMonitor()
        window = (540, 1080)  # 09:00-18:00
        now = datetime(2025, 5, 19, 12, 0)  # Noon
        assert rm._is_in_time_window(window, now) is True

    def test_before_normal_window(self):
        rm = ResourceMonitor()
        window = (540, 1080)  # 09:00-18:00
        now = datetime(2025, 5, 19, 8, 0)  # 8 AM
        assert rm._is_in_time_window(window, now) is False

    def test_after_normal_window(self):
        rm = ResourceMonitor()
        window = (540, 1080)  # 09:00-18:00
        now = datetime(2025, 5, 19, 19, 0)  # 7 PM
        assert rm._is_in_time_window(window, now) is False

    def test_at_start_normal_window(self):
        rm = ResourceMonitor()
        window = (540, 1080)  # 09:00-18:00
        now = datetime(2025, 5, 19, 9, 0)  # 9:00 AM
        assert rm._is_in_time_window(window, now) is True

    def test_at_end_normal_window_exclusive(self):
        rm = ResourceMonitor()
        window = (540, 1080)  # 09:00-18:00
        now = datetime(2025, 5, 19, 18, 0)  # 6:00 PM (end, exclusive)
        assert rm._is_in_time_window(window, now) is False

    def test_in_midnight_wrap_after_start(self):
        rm = ResourceMonitor()
        window = (1320, 360)  # 22:00-06:00
        now = datetime(2025, 5, 19, 23, 30)  # 11:30 PM
        assert rm._is_in_time_window(window, now) is True

    def test_in_midnight_wrap_before_end(self):
        rm = ResourceMonitor()
        window = (1320, 360)  # 22:00-06:00
        now = datetime(2025, 5, 19, 4, 0)  # 4 AM
        assert rm._is_in_time_window(window, now) is True

    def test_outside_midnight_wrap_middle_of_day(self):
        rm = ResourceMonitor()
        window = (1320, 360)  # 22:00-06:00
        now = datetime(2025, 5, 19, 12, 0)  # Noon
        assert rm._is_in_time_window(window, now) is False

    def test_at_start_midnight_wrap(self):
        rm = ResourceMonitor()
        window = (1320, 360)  # 22:00-06:00
        now = datetime(2025, 5, 19, 22, 0)  # 10:00 PM
        assert rm._is_in_time_window(window, now) is True

    def test_at_end_midnight_wrap_exclusive(self):
        rm = ResourceMonitor()
        window = (1320, 360)  # 22:00-06:00
        now = datetime(2025, 5, 19, 6, 0)  # 6:00 AM (exclusive)
        assert rm._is_in_time_window(window, now) is False


class TestGetNextEligibleTime:
    """Test next eligible time calculation."""

    def test_before_normal_window(self):
        rm = ResourceMonitor()
        window = (540, 1080)  # 09:00-18:00
        now = datetime(2025, 5, 19, 8, 0)  # 8 AM
        result = rm._get_next_eligible_time(window, now)
        assert "09:00" in result
        assert "1h 0m" in result

    def test_in_normal_window(self):
        rm = ResourceMonitor()
        window = (540, 1080)  # 09:00-18:00
        now = datetime(2025, 5, 19, 12, 0)  # Noon
        result = rm._get_next_eligible_time(window, now)
        assert "now" in result.lower()

    def test_after_normal_window(self):
        rm = ResourceMonitor()
        window = (540, 1080)  # 09:00-18:00
        now = datetime(2025, 5, 19, 19, 0)  # 7 PM
        result = rm._get_next_eligible_time(window, now)
        assert "09:00" in result  # Next day
        assert "14h 0m" in result  # 14 hours until 09:00

    def test_in_midnight_wrap_after_start(self):
        rm = ResourceMonitor()
        window = (1320, 360)  # 22:00-06:00
        now = datetime(2025, 5, 19, 23, 30)  # 11:30 PM
        result = rm._get_next_eligible_time(window, now)
        assert "now" in result.lower()

    def test_in_midnight_wrap_before_end(self):
        rm = ResourceMonitor()
        window = (1320, 360)  # 22:00-06:00
        now = datetime(2025, 5, 19, 4, 0)  # 4 AM
        result = rm._get_next_eligible_time(window, now)
        assert "now" in result.lower()

    def test_outside_midnight_wrap(self):
        rm = ResourceMonitor()
        window = (1320, 360)  # 22:00-06:00
        now = datetime(2025, 5, 19, 12, 0)  # Noon
        result = rm._get_next_eligible_time(window, now)
        assert "22:00" in result
        assert "10h 0m" in result


class TestCheckTimeConstraints:
    """Test time constraint checking."""

    def test_allow_hours_outside_window(self):
        rm = ResourceMonitor()
        rm.allow_hours = (540, 1080)  # 09:00-18:00
        rm.deny_hours = None
        
        with patch('resource_monitor.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2025, 5, 19, 8, 0)  # 8 AM
            allowed, reason = rm._check_time_constraints()
            assert allowed is False
            assert "Outside allow window" in reason

    def test_allow_hours_inside_window(self):
        rm = ResourceMonitor()
        rm.allow_hours = (540, 1080)  # 09:00-18:00
        rm.deny_hours = None
        
        with patch('resource_monitor.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2025, 5, 19, 12, 0)  # Noon
            allowed, reason = rm._check_time_constraints()
            assert allowed is True
            assert "OK" in reason or "constraints" in reason.lower()

    def test_deny_hours_inside_window(self):
        rm = ResourceMonitor()
        rm.allow_hours = None
        rm.deny_hours = (540, 1080)  # 09:00-18:00
        
        with patch('resource_monitor.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2025, 5, 19, 12, 0)  # Noon
            allowed, reason = rm._check_time_constraints()
            assert allowed is False
            assert "In deny window" in reason

    def test_deny_hours_outside_window(self):
        rm = ResourceMonitor()
        rm.allow_hours = None
        rm.deny_hours = (540, 1080)  # 09:00-18:00
        
        with patch('resource_monitor.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2025, 5, 19, 22, 0)  # 10 PM
            allowed, reason = rm._check_time_constraints()
            assert allowed is True
            assert "OK" in reason or "constraints" in reason.lower()

    def test_force_allow_marker_clears(self, tmp_path):
        rm = ResourceMonitor()
        rm.allow_hours = (540, 1080)  # 09:00-18:00
        rm.deny_hours = None
        
        marker = tmp_path / ".force_allow_next_run"
        marker.touch()
        
        with patch('resource_monitor.DREAM_DIR', tmp_path):
            allowed, reason = rm._check_time_constraints()
            assert allowed is True
            assert "Force-allow" in reason
            assert not marker.exists()

    def test_deny_takes_precedence_over_allow(self):
        rm = ResourceMonitor()
        rm.allow_hours = (0, 1440)  # All day
        rm.deny_hours = (540, 1080)  # 09:00-18:00
        
        with patch('resource_monitor.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2025, 5, 19, 12, 0)  # Noon
            allowed, reason = rm._check_time_constraints()
            assert allowed is False
            assert "In deny window" in reason


class TestCheckDailyDreamCount:
    """Test daily dream cap checking."""

    def test_no_cap_set(self):
        rm = ResourceMonitor()
        rm.max_daily_dreams = 0
        allowed, reason = rm._check_daily_dream_count()
        assert allowed is True
        assert "No daily cap" in reason

    def test_db_not_found(self):
        rm = ResourceMonitor()
        rm.max_daily_dreams = 5
        
        with patch('resource_monitor.DB_PATH', Path("/nonexistent/path/db.sqlite")):
            allowed, reason = rm._check_daily_dream_count()
            # Should allow if DB doesn't exist (first run)
            assert allowed is True

    def test_cap_not_reached(self):
        rm = ResourceMonitor()
        rm.max_daily_dreams = 5
        
        with patch('sqlite3.connect') as mock_connect:
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = [3]  # 3 dreams completed
            mock_connect.return_value.cursor.return_value = mock_cursor
            
            with patch('resource_monitor.DB_PATH', Path("/tmp/test.db")):
                allowed, reason = rm._check_daily_dream_count()
                assert allowed is True
                assert "3/5" in reason

    def test_cap_reached(self):
        rm = ResourceMonitor()
        rm.max_daily_dreams = 5
        
        with patch('sqlite3.connect') as mock_connect:
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = [5]  # 5 dreams completed
            mock_connect.return_value.cursor.return_value = mock_cursor
            
            with patch('resource_monitor.DB_PATH', Path("/tmp/test.db")):
                allowed, reason = rm._check_daily_dream_count()
                assert allowed is False
                assert "Daily cap reached" in reason


class TestCanStartDream:
    """Test main decision logic."""

    def test_time_constraint_blocks_dream(self):
        rm = ResourceMonitor()
        rm.allow_hours = (540, 1080)  # 09:00-18:00
        rm.deny_hours = None
        rm.max_daily_dreams = 0
        
        with patch('resource_monitor.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2025, 5, 19, 22, 0)  # 10 PM (outside)
            allowed, reason = rm.can_start_dream()
            assert allowed is False
            assert "Outside allow window" in reason

    def test_daily_cap_blocks_dream(self):
        rm = ResourceMonitor()
        rm.allow_hours = None
        rm.deny_hours = None
        rm.max_daily_dreams = 5
        
        with patch('sqlite3.connect') as mock_connect:
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = [5]
            mock_connect.return_value.cursor.return_value = mock_cursor
            
            with patch('resource_monitor.DB_PATH', Path("/tmp/test.db")):
                allowed, reason = rm.can_start_dream()
                assert allowed is False
                assert "Daily cap reached" in reason

    def test_all_constraints_pass(self):
        rm = ResourceMonitor()
        rm.allow_hours = None
        rm.deny_hours = None
        rm.max_daily_dreams = 0
        
        with patch.object(rm, 'get_state') as mock_state:
            mock_state.return_value = {
                "cpu_percent": 25.0,
                "ram_percent": 40.0,
                "active_sessions": 0,
                "active_crons": 0,
                "active_dreams": 0,
                "timestamp": datetime.now().isoformat(),
            }
            allowed, reason = rm.can_start_dream()
            assert allowed is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
