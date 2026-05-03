"""
Tests for dream_auto.resource_monitor — ResourceMonitor class.
Covers: thresholds, decision tree, LLM fallback, session/cron/dream counting.
"""

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


GMT7 = timezone(timedelta(hours=7))


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_imports():
    """Reset module-level state before each test."""
    import dream_auto.resource_monitor as rm
    rm._fast_path_module = None
    yield
    rm._fast_path_module = None


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    """Fake HOME so resource_monitor writes to tmp_path."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


@pytest.fixture
def mock_psutil_free(monkeypatch):
    """CPU=20%, RAM=40% — clearly free resources."""
    monkeypatch.setattr("psutil.cpu_percent", lambda interval: 20.0)
    monkeypatch.setattr("psutil.virtual_memory", lambda: MagicMock(percent=40.0))


@pytest.fixture
def mock_psutil_high_cpu(monkeypatch):
    """CPU=85%, RAM=40% — too busy (CPU over threshold)."""
    monkeypatch.setattr("psutil.cpu_percent", lambda interval: 85.0)
    monkeypatch.setattr("psutil.virtual_memory", lambda: MagicMock(percent=40.0))


@pytest.fixture
def mock_psutil_high_ram(monkeypatch):
    """CPU=20%, RAM=95% — too busy (RAM over threshold)."""
    monkeypatch.setattr("psutil.cpu_percent", lambda interval: 20.0)
    monkeypatch.setattr("psutil.virtual_memory", lambda: MagicMock(percent=95.0))


@pytest.fixture
def mock_psutil_gray_zone(monkeypatch):
    """CPU=50%, RAM=65% — gray zone, needs LLM decision."""
    monkeypatch.setattr("psutil.cpu_percent", lambda interval: 50.0)
    monkeypatch.setattr("psutil.virtual_memory", lambda: MagicMock(percent=65.0))


@pytest.fixture
def mock_hermes_sessions_none(monkeypatch):
    """No active sessions."""
    def run_mock(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "Preview   Last Active   Src   ID\n─────────────────────────────────────\n"
        return result
    monkeypatch.setattr("subprocess.run", run_mock)


@pytest.fixture
def mock_hermes_sessions_two(monkeypatch):
    """Two active sessions."""
    def run_mock(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        if "sessions" in cmd:
            result.stdout = (
                "Preview   Last Active   Src   ID\n"
                "─────────────────────────────────────\n"
                "cli      10:25        tg    abc123\n"
                "cli      10:24        tg    def456\n"
            )
        elif "cron" in cmd:
            result.stdout = "2 active job(s)\n"
        return result
    monkeypatch.setattr("subprocess.run", run_mock)


@pytest.fixture
def mock_hermes_sessions_zero_crons(monkeypatch):
    """Zero active cron jobs."""
    def run_mock(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        if "sessions" in cmd:
            result.stdout = "Preview   Last Active   Src   ID\n─────────────────────────────────────\n"
        elif "cron" in cmd:
            result.stdout = "0 active job(s)\n"
        return result
    monkeypatch.setattr("subprocess.run", run_mock)


@pytest.fixture
def mock_hermes_chat_llm_yes(monkeypatch):
    """Hermes chat -q returns yes."""
    def run_mock(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = '{"can_start": true, "reason": "Resources are available"}\n'
        return result
    monkeypatch.setattr("subprocess.run", run_mock)


@pytest.fixture
def mock_hermes_chat_llm_no(monkeypatch):
    """Hermes chat -q returns no."""
    def run_mock(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = '{"can_start": false, "reason": "System is busy"}\n'
        return result
    monkeypatch.setattr("subprocess.run", run_mock)


@pytest.fixture
def mock_hermes_chat_llm_parse_fail(monkeypatch):
    """Hermes chat -q returns non-JSON."""
    def run_mock(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "some random output\n"
        return result
    monkeypatch.setattr("subprocess.run", run_mock)


@pytest.fixture
def mock_hermes_chat_llm_timeout(monkeypatch):
    """Hermes chat -q times out."""
    import subprocess
    def run_mock(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 30)
    monkeypatch.setattr("subprocess.run", run_mock)


@pytest.fixture
def dream_dir_with_status(temp_home):
    """Create DREAM_DIR with dreams in various states."""
    import dream_auto.resource_monitor as rm
    dd = temp_home / ".hermes" / "state" / "dream"
    dd.mkdir(parents=True, exist_ok=True)

    # Running dream via status.txt
    running1 = dd / "running001"
    running1.mkdir()
    (running1 / "status.txt").write_text("running")

    # Running dream via meta.json
    running2 = dd / "running002"
    running2.mkdir()
    (running2 / "meta.json").write_text(json.dumps({"status": "running"}))

    # Done dream (should not be counted)
    done1 = dd / "done001"
    done1.mkdir()
    (done1 / "meta.json").write_text(json.dumps({"status": "done"}))

    # Non-dream file (should be ignored)
    (dd / "not_a_dream.txt").write_text("ignore")

    return dd


# ── get_state ─────────────────────────────────────────────────────────────────

class TestGetState:
    """get_state returns correct keys and values."""

    def test_returns_all_expected_keys(self, temp_home, mock_psutil_free,
                                       mock_hermes_sessions_none, mock_hermes_sessions_zero_crons,
                                       dream_dir_with_status):
        import dream_auto.resource_monitor as rm
        with patch.object(rm, "DREAM_DIR", dream_dir_with_status):
            state = rm.ResourceMonitor().get_state()

        assert "cpu_percent" in state
        assert "ram_percent" in state
        assert "active_sessions" in state
        assert "active_crons" in state
        assert "active_dreams" in state
        assert "timestamp" in state

    def test_cpu_ram_reported(self, temp_home, mock_psutil_free,
                              mock_hermes_sessions_none, mock_hermes_sessions_zero_crons,
                              dream_dir_with_status):
        import dream_auto.resource_monitor as rm
        with patch.object(rm, "DREAM_DIR", dream_dir_with_status):
            state = rm.ResourceMonitor().get_state()

        assert state["cpu_percent"] == 20.0
        assert state["ram_percent"] == 40.0

    def test_sessions_parsed(self, temp_home, mock_psutil_free,
                               mock_hermes_sessions_two, dream_dir_with_status):
        import dream_auto.resource_monitor as rm
        with patch.object(rm, "DREAM_DIR", dream_dir_with_status):
            state = rm.ResourceMonitor().get_state()

        assert state["active_sessions"] == 2

    def test_crons_parsed(self, temp_home, mock_psutil_free,
                           mock_hermes_sessions_zero_crons, dream_dir_with_status):
        import dream_auto.resource_monitor as rm
        with patch.object(rm, "DREAM_DIR", dream_dir_with_status):
            state = rm.ResourceMonitor().get_state()

        assert state["active_crons"] == 0


# ── _count_active_dreams ─────────────────────────────────────────────────────

class TestCountActiveDreams:
    """_count_active_dreams counts status.txt=running and meta.json status=running."""

    def test_counts_status_txt_running(self, temp_home, mock_psutil_free,
                                       mock_hermes_sessions_none, mock_hermes_sessions_zero_crons):
        import dream_auto.resource_monitor as rm
        dd = temp_home / ".hermes" / "state" / "dream"
        dd.mkdir(parents=True, exist_ok=True)
        (dd / "dream1").mkdir()
        (dd / "dream1" / "status.txt").write_text("running")

        with patch.object(rm, "DREAM_DIR", dd):
            count = rm.ResourceMonitor()._count_active_dreams()

        assert count == 1

    def test_counts_meta_json_running(self, temp_home, mock_psutil_free,
                                      mock_hermes_sessions_none, mock_hermes_sessions_zero_crons):
        import dream_auto.resource_monitor as rm
        dd = temp_home / ".hermes" / "state" / "dream"
        dd.mkdir(parents=True, exist_ok=True)
        (dd / "dream2").mkdir()
        (dd / "dream2" / "meta.json").write_text(json.dumps({"status": "running"}))

        with patch.object(rm, "DREAM_DIR", dd):
            count = rm.ResourceMonitor()._count_active_dreams()

        assert count == 1

    def test_skips_done_dreams(self, temp_home, mock_psutil_free,
                               mock_hermes_sessions_none, mock_hermes_sessions_zero_crons):
        import dream_auto.resource_monitor as rm
        dd = temp_home / ".hermes" / "state" / "dream"
        dd.mkdir(parents=True, exist_ok=True)
        (dd / "dream_done").mkdir()
        (dd / "dream_done" / "meta.json").write_text(json.dumps({"status": "done"}))

        with patch.object(rm, "DREAM_DIR", dd):
            count = rm.ResourceMonitor()._count_active_dreams()

        assert count == 0

    def test_counts_both_status_and_meta(self, temp_home, mock_psutil_free,
                                         mock_hermes_sessions_none, mock_hermes_sessions_zero_crons):
        """Two running dreams: one via status.txt, one via meta.json."""
        import dream_auto.resource_monitor as rm
        dd = temp_home / ".hermes" / "state" / "dream"
        dd.mkdir(parents=True, exist_ok=True)
        (dd / "dream_a").mkdir()
        (dd / "dream_a" / "status.txt").write_text("running")
        (dd / "dream_b").mkdir()
        (dd / "dream_b" / "meta.json").write_text(json.dumps({"status": "running"}))

        with patch.object(rm, "DREAM_DIR", dd):
            count = rm.ResourceMonitor()._count_active_dreams()

        assert count == 2


# ── can_start_dream — decision tree ──────────────────────────────────────────

class TestCanStartDream:
    """Decision tree: CPU>=80/RAM>=90 NO, CPU<=30 YES, gray zone LLM, else YES."""

    def test_high_cpu_returns_no(self, temp_home, mock_psutil_high_cpu,
                                  mock_hermes_sessions_none, mock_hermes_sessions_zero_crons,
                                  dream_dir_with_status):
        import dream_auto.resource_monitor as rm
        with patch.object(rm, "DREAM_DIR", dream_dir_with_status):
            available, reason = rm.ResourceMonitor().can_start_dream()

        assert available is False
        assert "85" in reason or "CPU" in reason

    def test_high_ram_returns_no(self, temp_home, mock_psutil_high_ram,
                                  mock_hermes_sessions_none, mock_hermes_sessions_zero_crons,
                                  dream_dir_with_status):
        import dream_auto.resource_monitor as rm
        with patch.object(rm, "DREAM_DIR", dream_dir_with_status):
            available, reason = rm.ResourceMonitor().can_start_dream()

        assert available is False
        assert "95" in reason or "RAM" in reason

    def test_low_cpu_returns_yes(self, temp_home, mock_psutil_free,
                                  mock_hermes_sessions_none, mock_hermes_sessions_zero_crons,
                                  dream_dir_with_status):
        import dream_auto.resource_monitor as rm
        with patch.object(rm, "DREAM_DIR", dream_dir_with_status):
            available, reason = rm.ResourceMonitor().can_start_dream()

        assert available is True
        assert "20" in reason or "free" in reason.lower()

    def test_gray_zone_calls_llm(self, temp_home, mock_psutil_gray_zone,
                                   mock_hermes_sessions_none, mock_hermes_sessions_zero_crons,
                                   mock_hermes_chat_llm_yes, dream_dir_with_status):
        import dream_auto.resource_monitor as rm
        with patch.object(rm, "DREAM_DIR", dream_dir_with_status):
            available, reason = rm.ResourceMonitor().can_start_dream()

        assert available is True
        assert "available" in reason.lower()

    def test_gray_zone_llm_says_no(self, temp_home, mock_psutil_gray_zone,
                                     mock_hermes_sessions_none, mock_hermes_sessions_zero_crons,
                                     mock_hermes_chat_llm_no, dream_dir_with_status):
        import dream_auto.resource_monitor as rm
        with patch.object(rm, "DREAM_DIR", dream_dir_with_status):
            available, reason = rm.ResourceMonitor().can_start_dream()

        assert available is False
        assert "busy" in reason.lower()

    def test_gray_zone_llm_parse_fail_defaults_no(self, temp_home, mock_psutil_gray_zone,
                                                    mock_hermes_sessions_none,
                                                    mock_hermes_sessions_zero_crons,
                                                    mock_hermes_chat_llm_parse_fail,
                                                    dream_dir_with_status):
        import dream_auto.resource_monitor as rm
        with patch.object(rm, "DREAM_DIR", dream_dir_with_status):
            available, reason = rm.ResourceMonitor().can_start_dream()

        assert available is False
        assert "parse" in reason.lower() or "defaulting" in reason.lower()

    def test_gray_zone_llm_timeout_defers(self, temp_home, mock_psutil_gray_zone,
                                            mock_hermes_sessions_none,
                                            mock_hermes_sessions_zero_crons,
                                            mock_hermes_chat_llm_timeout,
                                            dream_dir_with_status):
        import dream_auto.resource_monitor as rm
        with patch.object(rm, "DREAM_DIR", dream_dir_with_status):
            available, reason = rm.ResourceMonitor().can_start_dream()

        assert available is False
        assert "timeout" in reason.lower() or "defer" in reason.lower()

    def test_both_high_cpu_and_ram_no(self, temp_home, monkeypatch,
                                       mock_hermes_sessions_none, mock_hermes_sessions_zero_crons,
                                       dream_dir_with_status):
        """If BOTH exceed thresholds, still returns False."""
        import dream_auto.resource_monitor as rm
        monkeypatch.setattr("psutil.cpu_percent", lambda interval: 85.0)
        monkeypatch.setattr("psutil.virtual_memory", lambda: MagicMock(percent=95.0))

        with patch.object(rm, "DREAM_DIR", dream_dir_with_status):
            available, reason = rm.ResourceMonitor().can_start_dream()

        assert available is False


# ── get_queue_priority ───────────────────────────────────────────────────────

class TestGetQueuePriority:
    """get_queue_priority returns correct health string."""

    def test_excellent(self, temp_home, monkeypatch, mock_hermes_sessions_none,
                        mock_hermes_sessions_zero_crons, dream_dir_with_status):
        import dream_auto.resource_monitor as rm
        monkeypatch.setattr("psutil.cpu_percent", lambda interval: 15.0)
        monkeypatch.setattr("psutil.virtual_memory", lambda: MagicMock(percent=35.0))

        with patch.object(rm, "DREAM_DIR", dream_dir_with_status):
            priority = rm.ResourceMonitor().get_queue_priority()

        assert priority == "excellent"

    def test_good(self, temp_home, monkeypatch, mock_hermes_sessions_none,
                    mock_hermes_sessions_zero_crons, dream_dir_with_status):
        import dream_auto.resource_monitor as rm
        monkeypatch.setattr("psutil.cpu_percent", lambda interval: 35.0)
        monkeypatch.setattr("psutil.virtual_memory", lambda: MagicMock(percent=55.0))

        with patch.object(rm, "DREAM_DIR", dream_dir_with_status):
            priority = rm.ResourceMonitor().get_queue_priority()

        assert priority == "good"

    def test_moderate(self, temp_home, monkeypatch, mock_hermes_sessions_none,
                        mock_hermes_sessions_zero_crons, dream_dir_with_status):
        import dream_auto.resource_monitor as rm
        monkeypatch.setattr("psutil.cpu_percent", lambda interval: 55.0)
        monkeypatch.setattr("psutil.virtual_memory", lambda: MagicMock(percent=70.0))

        with patch.object(rm, "DREAM_DIR", dream_dir_with_status):
            priority = rm.ResourceMonitor().get_queue_priority()

        assert priority == "moderate"

    def test_busy(self, temp_home, monkeypatch, mock_hermes_sessions_none,
                    mock_hermes_sessions_zero_crons, dream_dir_with_status):
        import dream_auto.resource_monitor as rm
        monkeypatch.setattr("psutil.cpu_percent", lambda interval: 65.0)
        monkeypatch.setattr("psutil.virtual_memory", lambda: MagicMock(percent=85.0))

        with patch.object(rm, "DREAM_DIR", dream_dir_with_status):
            priority = rm.ResourceMonitor().get_queue_priority()

        assert priority == "busy"


# ── Threshold configuration ───────────────────────────────────────────────────

class TestThresholdConfiguration:
    """Thresholds are configurable via constructor args."""

    def test_custom_thresholds_used(self, temp_home, monkeypatch, mock_hermes_sessions_none,
                                     mock_hermes_sessions_zero_crons, dream_dir_with_status):
        import dream_auto.resource_monitor as rm
        # Set custom thresholds that our mock values will trigger differently
        monitor = rm.ResourceMonitor()
        monitor.cpu_clear_high = 50.0   # 85% will now exceed this
        monitor.cpu_clear_low = 10.0
        monitor.ram_clear_high = 95.0
        monitor.ram_clear_low = 10.0

        monkeypatch.setattr("psutil.cpu_percent", lambda interval: 85.0)
        monkeypatch.setattr("psutil.virtual_memory", lambda: MagicMock(percent=40.0))

        with patch.object(rm, "DREAM_DIR", dream_dir_with_status):
            available, reason = monitor.can_start_dream()

        # CPU=85 > cpu_clear_high=50 → should be False
        assert available is False

    def test_custom_clear_low_allows_low_cpu(self, temp_home, monkeypatch, mock_hermes_sessions_none,
                                             mock_hermes_sessions_zero_crons, dream_dir_with_status):
        import dream_auto.resource_monitor as rm
        monitor = rm.ResourceMonitor()
        monitor.cpu_clear_high = 50.0
        monitor.cpu_clear_low = 30.0   # 20% is now below this, so clearly free
        monitor.ram_clear_high = 95.0
        monitor.ram_clear_low = 50.0

        monkeypatch.setattr("psutil.cpu_percent", lambda interval: 20.0)
        monkeypatch.setattr("psutil.virtual_memory", lambda: MagicMock(percent=40.0))

        with patch.object(rm, "DREAM_DIR", dream_dir_with_status):
            available, reason = monitor.can_start_dream()

        assert available is True


# ── Error handling ───────────────────────────────────────────────────────────

class TestErrorHandling:
    """Subprocess failures fall back gracefully."""

    def test_hermes_sessions_cmd_fails(self, temp_home, monkeypatch,
                                        mock_hermes_sessions_zero_crons, dream_dir_with_status):
        import dream_auto.resource_monitor as rm

        def run_fake(cmd, **kwargs):
            result = MagicMock()
            if "sessions" in cmd:
                result.returncode = 1
                result.stdout = ""
                result.stderr = "command not found"
            else:
                result.returncode = 0
                result.stdout = "0 active job(s)\n"
            return result

        monkeypatch.setattr("subprocess.run", run_fake)
        monkeypatch.setattr("psutil.cpu_percent", lambda interval: 20.0)
        monkeypatch.setattr("psutil.virtual_memory", lambda: MagicMock(percent=40.0))

        with patch.object(rm, "DREAM_DIR", dream_dir_with_status):
            state = rm.ResourceMonitor().get_state()

        # Should return 0 sessions when command fails, not crash
        assert state["active_sessions"] == 0

    def test_dream_dir_missing(self, temp_home, mock_psutil_free,
                                mock_hermes_sessions_none, mock_hermes_sessions_zero_crons):
        import dream_auto.resource_monitor as rm
        missing_dir = temp_home / ".hermes" / "state" / "dream_nonexistent"

        with patch.object(rm, "DREAM_DIR", missing_dir):
            count = rm.ResourceMonitor()._count_active_dreams()

        assert count == 0



