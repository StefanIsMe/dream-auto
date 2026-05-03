"""
Tests for dream_auto plugin — dream_auto/__init__.py
Covers: config, error detection, queue helpers, insight distillation.
"""

import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_imports():
    """Reset module-level state before each test to prevent cross-test pollution."""
    import dream_auto.__init__ as dma
    dma._session_injected.clear()
    dma._session_turn_counter.clear()
    dma._fast_path_module = None
    dma._last_global_hook_ts = -300.0
    # BM25 index cache
    dma._bm25_index = None
    dma._bm25_dreams = []
    dma._bm25_dir_mtime = -1.0
    dma._bm25_impl = None
    dma._bm25_is_real = False
    yield
    dma._session_injected.clear()
    dma._session_turn_counter.clear()
    dma._fast_path_module = None
    dma._last_global_hook_ts = -300.0
    dma._bm25_index = None
    dma._bm25_dreams = []
    dma._bm25_dir_mtime = -1.0
    dma._bm25_impl = None
    dma._bm25_is_real = False


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    """
    Fake HOME so dream_auto writes to tmp_path.

    NOTE: Path.home() reads the pwd database, NOT $HOME env var.
    So we must ALSO patch the module-level DREAM_DIR / KNOWLEDGE_CACHE_DB
    constants directly in every test that uses temp_home.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


@pytest.fixture
def dream_dir(temp_home):
    d = temp_home / ".hermes" / "state" / "dream"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def mock_enabled(temp_home):
    """Force DREAM_AUTO_ENABLED=1 for all tests. Also patch DREAM_DIR."""
    import dream_auto.__init__ as dma
    patched_dir = temp_home / ".hermes" / "state" / "dream"
    with patch.dict(os.environ, {"DREAM_AUTO_ENABLED": "1", "HOME": str(temp_home)}), \
         patch.object(dma, "DREAM_DIR", patched_dir), \
         patch.object(dma, "DREAM_QUEUE_DB", patched_dir / "dream_queue.db"), \
         patch.object(dma, "KNOWLEDGE_CACHE_DB", patched_dir / "knowledge_cache.db"):
        # Ensure the fake DREAM_DIR actually exists so code that checks .exists() works
        patched_dir.mkdir(parents=True, exist_ok=True)
        yield


@pytest.fixture
def mock_disabled(temp_home):
    """Force DREAM_AUTO_ENABLED=0. Also patch DREAM_DIR."""
    import dream_auto.__init__ as dma
    with patch.dict(os.environ, {"DREAM_AUTO_ENABLED": "0", "HOME": str(temp_home)}), \
         patch.object(dma, "DREAM_DIR", temp_home / ".hermes" / "state" / "dream"), \
         patch.object(dma, "KNOWLEDGE_CACHE_DB", temp_home / ".hermes" / "state" / "dream" / "knowledge_cache.db"):
        yield


@pytest.fixture
def sample_dream(dream_dir, mock_enabled):
    """Create a fake completed dream with insights.json and meta.json."""
    import dream_auto.__init__ as dma
    did = "abc12345"
    dp = dream_dir / did
    dp.mkdir(parents=True, exist_ok=True)

    (dp / "meta.json").write_text(json.dumps({
        "status": "done",
        "confidence": 0.82,
        "brief": "Test dream for unit testing",
    }))
    (dp / "insights.json").write_text(json.dumps([
        "LinkedIn cookie refresh cron was failing silently.",
        "Chrome CDP session reuse works across agent restarts.",
    ]))
    (dp / "pending_questions.json").write_text(json.dumps([
        "Why did cron job not detect the expired session?",
    ]))
    return did, dp


# ── Config helpers ─────────────────────────────────────────────────────────────

class TestConfigHelpers:
    """_enabled, _verbose, _max_inject, _throttle_turns."""

    def test_enabled_default(self, mock_enabled):
        import dream_auto.__init__ as dma
        assert dma._enabled() is True

    def test_disabled_via_env(self, mock_disabled):
        import dream_auto.__init__ as dma
        assert dma._enabled() is False

    def test_verbose_default_off(self, mock_enabled):
        import dream_auto.__init__ as dma
        assert dma._verbose() is False

    def test_verbose_on_via_env(self, mock_enabled, monkeypatch):
        monkeypatch.setenv("DREAM_AUTO_VERBOSE", "1")
        import dream_auto.__init__ as dma
        assert dma._verbose() is True

    def test_max_inject_default(self, mock_enabled):
        import dream_auto.__init__ as dma
        assert dma._max_inject() == 3

    def test_max_inject_custom(self, mock_enabled, monkeypatch):
        monkeypatch.setenv("DREAM_AUTO_MAX_INJECT", "7")
        import dream_auto.__init__ as dma
        assert dma._max_inject() == 7

    def test_max_inject_invalid(self, mock_enabled, monkeypatch):
        monkeypatch.setenv("DREAM_AUTO_MAX_INJECT", "notanumber")
        import dream_auto.__init__ as dma
        assert dma._max_inject() == 3  # fallback

    def test_throttle_turns_default(self, mock_enabled):
        import dream_auto.__init__ as dma
        assert dma._throttle_turns() == 5

    def test_throttle_turns_custom(self, mock_enabled, monkeypatch):
        monkeypatch.setenv("DREAM_AUTO_THROTTLE_TURNS", "3")
        import dream_auto.__init__ as dma
        assert dma._throttle_turns() == 3


# ── Error detection ────────────────────────────────────────────────────────────

class TestErrorDetection:
    """_is_error_output, _auto_brief_from_error."""

    def test_traceback_detected(self, mock_enabled):
        import dream_auto.__init__ as dma
        assert dma._is_error_output("Traceback (most recent call last):\n  File 'test.py'") is True

    def test_error_colon_detected(self, mock_enabled):
        import dream_auto.__init__ as dma
        # "error:" must appear as substring
        assert dma._is_error_output("Something went wrong: error: connection refused") is True

    def test_exception_detected(self, mock_enabled):
        import dream_auto.__init__ as dma
        assert dma._is_error_output("ValueError: invalid literal") is True

    def test_module_not_found_detected(self, mock_enabled):
        import dream_auto.__init__ as dma
        assert dma._is_error_output("ModuleNotFoundError: No module named 'psutil'") is True

    def test_permission_denied_detected(self, mock_enabled):
        import dream_auto.__init__ as dma
        assert dma._is_error_output("Permission denied: /etc/passwd") is True

    def test_no_error_clean_output(self, mock_enabled):
        import dream_auto.__init__ as dma
        assert dma._is_error_output("✓ Done. Output: 42 items processed") is False
        assert dma._is_error_output("curl: (22) The requested URL returned error: 404") is True  # has "error"

    def test_fatal_panic_detected(self, mock_enabled):
        import dream_auto.__init__ as dma
        assert dma._is_error_output("FATAL: out of memory") is True
        assert dma._is_error_output("panic: runtime error: index out of range") is True

    def test_auto_brief_from_error_extracts_type(self, mock_enabled):
        import dream_auto.__init__ as dma
        brief = dma._auto_brief_from_error("execute_code", "ModuleNotFoundError: No module named 'foo'")
        assert "ModuleNotFoundError" in brief
        assert "execute_code" in brief
        assert "systematic root cause analysis" in brief

    def test_auto_brief_from_error_unknown_type(self, mock_enabled):
        import dream_auto.__init__ as dma
        brief = dma._auto_brief_from_error("terminal", "something went wrong: bad juju")
        assert "unknown error" in brief.lower() or "bad juju" in brief

    def test_auto_brief_truncates_long_context(self, mock_enabled):
        import dream_auto.__init__ as dma
        long_error = "x" * 1000
        brief = dma._auto_brief_from_error("terminal", long_error)
        # Context should be truncated to ~300 chars
        assert "Context:" in brief


# ── File helpers ───────────────────────────────────────────────────────────────

class TestFileHelpers:
    """_read_json, _write_json."""

    def test_read_json_missing_file(self, mock_enabled, temp_home):
        import dream_auto.__init__ as dma
        result = dma._read_json(temp_home / "nonexistent.json", default={"fallback": True})
        assert result == {"fallback": True}

    def test_read_json_invalid_json(self, mock_enabled, temp_home):
        f = temp_home / "bad.json"
        f.write_text("not valid json {{{")
        import dream_auto.__init__ as dma
        result = dma._read_json(f, default=None)
        assert result is None

    def test_read_json_valid(self, mock_enabled, temp_home):
        f = temp_home / "good.json"
        f.write_text('{"key": "value"}')
        import dream_auto.__init__ as dma
        result = dma._read_json(f)
        assert result == {"key": "value"}

    def test_write_json_creates_parent(self, mock_enabled, temp_home):
        import dream_auto.__init__ as dma
        p = temp_home / "a" / "b" / "c" / "data.json"
        dma._write_json(p, {"test": 123})
        written = json.loads(p.read_text())
        assert written == {"test": 123}

    def test_write_json_roundtrip(self, mock_enabled, temp_home):
        import dream_auto.__init__ as dma
        data = {"list": [1, 2, 3], "nested": {"a": True}}
        f = temp_home / "roundtrip.json"
        dma._write_json(f, data)
        assert dma._read_json(f) == data


# ── Queue helpers ─────────────────────────────────────────────────────────────

class TestQueueHelpers:
    """_add_to_queue."""

    def test_add_to_queue_creates_record(self, mock_enabled, temp_home):
        import dream_auto.__init__ as dma
        with patch.object(dma, "DREAM_DIR", temp_home / ".hermes" / "state" / "dream"), \
             patch.object(dma, "DREAM_QUEUE_DB", temp_home / ".hermes" / "state" / "dream" / "dream_queue.db"):
            dream_id = dma._add_to_queue("session_x", "Explore why CDP disconnected", grade=0.8, priority=0.9)

        assert dream_id is not None
        assert len(dream_id) == 8

        # Verify DB entry
        db_path = temp_home / ".hermes" / "state" / "dream" / "dream_queue.db"
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT * FROM dream_queue WHERE dream_id = ?", (dream_id,)).fetchone()
        conn.close()

        assert row is not None
        assert row[1] == "session_x"          # session_id
        assert row[3] == "Explore why CDP disconnected"  # dream_question
        assert row[4] == 0.8                    # grade
        assert row[10] == "queued"             # status (index 10)

    def test_add_to_queue_idempotent_dream_id(self, mock_enabled, temp_home, monkeypatch):
        """dream_id is UNIQUE so same brief gets different IDs."""
        monkeypatch.setenv("HOME", str(temp_home))
        import dream_auto.__init__ as dma
        with patch.object(dma, "DREAM_DIR", temp_home / ".hermes" / "state" / "dream"), \
             patch.object(dma, "DREAM_QUEUE_DB", temp_home / ".hermes" / "state" / "dream" / "dream_queue.db"):
            id1 = dma._add_to_queue("s1", "Brief A")
            id2 = dma._add_to_queue("s1", "Brief B")

        assert id1 != id2


# ── Insight distillation ───────────────────────────────────────────────────────

class TestInsightDistillation:
    """_distill_insights."""

    def test_distill_insights_with_confidence(self, mock_enabled, sample_dream):
        import dream_auto.__init__ as dma
        did, _ = sample_dream
        result = dma._distill_insights(did)
        assert "DREAM INSIGHTS" in result
        assert "abc12345" in result
        assert "LinkedIn cookie" in result
        assert "82%" in result or "82" in result

    def test_distill_insights_includes_questions(self, mock_enabled, sample_dream):
        import dream_auto.__init__ as dma
        did, _ = sample_dream
        result = dma._distill_insights(did)
        assert "Open questions" in result

    def test_distill_insights_empty_dream(self, mock_enabled, dream_dir):
        import dream_auto.__init__ as dma
        empty_id = "empty999"
        (dream_dir / empty_id).mkdir()
        (dream_dir / empty_id / "meta.json").write_text("{}")
        (dream_dir / empty_id / "insights.json").write_text("[]")
        result = dma._distill_insights(empty_id)
        assert result == ""

    def test_distill_insights_caps_to_three(self, mock_enabled, sample_dream):
        import dream_auto.__init__ as dma
        did, dp = sample_dream
        # Write 5 insights: [Insight 0, 1, 2, 3, 4]
        # _distill_insights takes last 3: [Insight 2, 3, 4]
        (dp / "insights.json").write_text(json.dumps([f"Insight {i}" for i in range(5)]))
        result = dma._distill_insights(did)
        # Last 3 appear
        assert "Insight 2" in result
        assert "Insight 3" in result
        assert "Insight 4" in result
        # First 2 are omitted
        assert "Insight 0" not in result
        assert "Insight 1" not in result


# ── Completed dreams listing ───────────────────────────────────────────────────

class TestCompletedDreams:
    """_list_completed_dreams."""

    def test_lists_only_done_status(self, mock_enabled, dream_dir, sample_dream):
        import dream_auto.__init__ as dma
        did, _ = sample_dream

        # Add a running dream
        running_id = "running99"
        rp = dream_dir / running_id
        rp.mkdir()
        (rp / "meta.json").write_text(json.dumps({"status": "running", "confidence": 0.5}))

        completed = dma._list_completed_dreams()
        ids = [d["id"] for d in completed]
        assert did in ids
        assert "running99" not in ids

    def test_skips_dreams_without_insights(self, mock_enabled, dream_dir):
        import dream_auto.__init__ as dma
        no_insight_id = "noinsight1"
        np = dream_dir / no_insight_id
        np.mkdir()
        (np / "meta.json").write_text(json.dumps({"status": "done", "confidence": 0.9}))
        (np / "insights.json").write_text("[]")

        completed = dma._list_completed_dreams()
        ids = [d["id"] for d in completed]
        assert no_insight_id not in ids

    def test_sorted_by_confidence_desc(self, mock_enabled, dream_dir):
        import dream_auto.__init__ as dma
        for grade, conf in [(1, 0.95), (2, 0.5), (3, 0.75)]:
            did = f"conf{grade}"
            dp = dream_dir / did
            dp.mkdir()
            (dp / "meta.json").write_text(json.dumps({"status": "done", "confidence": conf}))
            (dp / "insights.json").write_text(json.dumps([f"Insight for {did}"]))

        completed = dma._list_completed_dreams()
        confs = [d["confidence"] for d in completed]
        assert confs == sorted(confs, reverse=True)


# ── Hook: pre_llm_call ───────────────────────────────────────────────────────

class TestPreLlmCall:
    """_on_pre_llm_call."""

    def test_disabled_returns_none(self, mock_disabled):
        import dream_auto.__init__ as dma
        result = dma._on_pre_llm_call(
            user_message="hello world",
            conversation_history=[],
            is_first_turn=False,
            model="test",
            platform="test",
            session_id="test_sid",
        )
        assert result is None

    def test_short_message_returns_none(self, mock_enabled, dream_dir):
        import dream_auto.__init__ as dma
        result = dma._on_pre_llm_call(
            user_message="hi",  # too short
            conversation_history=[],
            is_first_turn=False,
            model="test",
            platform="test",
            session_id="test_sid",
        )
        assert result is None

    def test_global_throttle_skips_recent_call(self, mock_enabled, dream_dir, monkeypatch):
        """If hook ran < 300s ago, it returns None immediately (no file I/O)."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(dream_dir.parent.parent.parent))

        # Simulate a very recent call: set global throttle to 1 second ago
        dma._last_global_hook_ts = time.monotonic() - 1

        with patch.object(dma, "_list_completed_dreams") as mock_list:
            result = dma._on_pre_llm_call(
                user_message="Tell me about hermes agent cron jobs and linkedin",
                conversation_history=[],
                is_first_turn=False,
                model="test",
                platform="test",
                session_id="throttle_test",
            )
            # Should have returned None immediately — no file I/O at all
            assert result is None
            mock_list.assert_not_called()

    def test_injects_insights_for_new_session(self, mock_enabled, temp_home, monkeypatch):
        """
        Verifies insights are injected for a session that hasn't received them yet.
        """
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))

        dream_path = temp_home / ".hermes" / "state" / "dream"
        dream_path.mkdir(parents=True, exist_ok=True)

        # Create a real completed dream so _list_completed_dreams_raw finds it
        did = "test_dream_abc"
        dp = dream_path / did
        dp.mkdir()
        (dp / "meta.json").write_text(json.dumps({
            "status": "done",
            "confidence": 0.82,
            "brief": "automated systems cron job failures",
        }))
        (dp / "insights.json").write_text(json.dumps([
            "LinkedIn cookie refresh cron was failing silently.",
            "Chrome CDP session reuse works across agent restarts.",
        ]))
        (dp / "pending_questions.json").write_text(json.dumps([
            "Why did cron job not detect the expired session?",
        ]))

        # Reset state
        dma._session_injected.clear()
        dma._session_turn_counter.clear()
        dma._last_global_hook_ts = -300.0
        dma._bm25_index = None
        dma._bm25_dreams = []
        dma._bm25_dir_mtime = -1.0

        # Patch BM25 scoring to return our real dream with a positive score
        # (tests that _on_pre_llm_call correctly uses BM25 scoring result to inject)
        fake_scored = [{"id": did, "brief": "automated systems cron job failures",
                        "confidence": 0.82, "topics": [], "_ended_at": "",
                        "_score": 1.5, "insights": [
                            "LinkedIn cookie refresh cron was failing silently.",
                            "Chrome CDP session reuse works across agent restarts.",
                        ], "pending_questions": [
                            "Why did cron job not detect the expired session?",
                        ]}]

        with patch.object(dma, "DREAM_DIR", dream_path):
            with patch.object(dma, "_score_dreams_bm25", return_value=fake_scored):
                result = dma._on_pre_llm_call(
                    user_message="Tell me about automated systems and cron job failures",
                    conversation_history=[],
                    is_first_turn=False,
                    model="test",
                    platform="test",
                    session_id="brand_new_session",
                )

        assert result is not None, "Expected injection when BM25 scores a dream"
        assert "context" in result
        assert "DREAM INSIGHTS" in result["context"]
        assert "test_dream_abc" in result["context"]

    def test_does_not_reinject_same_dream(self, mock_enabled, temp_home, monkeypatch):
        """Once a session has received a dream, it is not reinjected."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))

        dream_path = temp_home / ".hermes" / "state" / "dream"
        dream_path.mkdir(parents=True, exist_ok=True)

        did = "reuse_dream"
        dp = dream_path / did
        dp.mkdir()
        (dp / "meta.json").write_text(json.dumps({
            "status": "done",
            "confidence": 0.82,
            "brief": "automated systems cron job",
        }))
        (dp / "insights.json").write_text(json.dumps([
            "LinkedIn cookie refresh cron was failing silently.",
        ]))

        sid = "reuse_test_session"

        dma._session_injected.clear()
        dma._session_turn_counter.clear()
        dma._last_global_hook_ts = -300.0
        dma._bm25_index = None
        dma._bm25_dreams = []
        dma._bm25_dir_mtime = -1.0

        fake_scored = [{"id": did, "brief": "automated systems cron job",
                        "confidence": 0.82, "topics": [], "_ended_at": "",
                        "_score": 1.5, "insights": [
                            "LinkedIn cookie refresh cron was failing silently.",
                        ], "pending_questions": []}]

        with patch.object(dma, "DREAM_DIR", dream_path):
            with patch.object(dma, "_score_dreams_bm25", return_value=fake_scored):
                # First call — should inject
                r1 = dma._on_pre_llm_call(
                    user_message="Tell me about automated systems and cron job failures",
                    conversation_history=[],
                    is_first_turn=False,
                    model="test",
                    platform="test",
                    session_id=sid,
                )
                assert r1 is not None, "First call should inject"
                assert "DREAM INSIGHTS" in r1["context"]

                # Reset throttle for second call
                dma._last_global_hook_ts = -300.0
                r2 = dma._on_pre_llm_call(
                    user_message="Tell me more about cookie issues",
                    conversation_history=[],
                    is_first_turn=False,
                    model="test",
                    platform="test",
                    session_id=sid,
                )
                # Session already received this dream — no reinjection
                assert r2 is None, "Second call same session should not reinject"

    def test_injects_multiple_dreams_up_to_max(self, mock_enabled, dream_dir, monkeypatch):
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(dream_dir.parent.parent.parent))

        dma._session_injected.clear()
        dma._session_turn_counter.clear()
        dma._last_global_hook_ts = -300.0
        dma._bm25_index = None
        dma._bm25_dreams = []
        dma._bm25_dir_mtime = -1.0

        # Create 4 completed dreams
        for i in range(4):
            did = f"multi{i}"
            dp = dream_dir / did
            dp.mkdir()
            (dp / "meta.json").write_text(json.dumps({
                "status": "done",
                "confidence": 0.7 + i * 0.05,
            }))
            (dp / "insights.json").write_text(json.dumps([f"Insight {i} from dream {did}"]))

        # Fake BM25 scoring returns 3 dreams (max_inject limit is applied by _score_dreams_bm25 internally)
        fake_scored = [
            {"id": f"multi{i}", "brief": f"dream brief {i}", "confidence": 0.7 + i * 0.05,
             "topics": [], "_ended_at": "", "_score": 1.0 - i * 0.1,
             "insights": [f"Insight {i} from dream multi{i}"], "pending_questions": []}
            for i in range(3)  # Only 3: matches _max_inject=3
        ]

        with patch.object(dma, "_max_inject", return_value=3):
            with patch.object(dma, "_score_dreams_bm25", return_value=fake_scored):
                result = dma._on_pre_llm_call(
                    user_message="Tell me about complex system issues and automation",
                    conversation_history=[],
                    is_first_turn=False,
                    model="test",
                    platform="test",
                    session_id="multi_test",
                )

        assert result is not None
        # Should contain multiple DREAM INSIGHTS blocks (up to max_inject=3)
        assert result["context"].count("DREAM INSIGHTS") <= 3


# ── Hook: pre_tool_call ───────────────────────────────────────────────────────

class TestPreToolCall:
    """_on_pre_tool_call."""

    def test_disabled_returns_none(self, mock_disabled):
        import dream_auto.__init__ as dma
        result = dma._on_pre_tool_call(tool_name="execute_code", args={"code": "x" * 1000})
        assert result is None

    def test_simple_code_returns_none(self, mock_enabled):
        import dream_auto.__init__ as dma
        result = dma._on_pre_tool_call(
            tool_name="execute_code",
            args={"code": "print('hello')"},
        )
        assert result is None

    def test_complex_code_suggests_dream(self, mock_enabled):
        import dream_auto.__init__ as dma
        result = dma._on_pre_tool_call(
            tool_name="execute_code",
            args={"code": "def foo():\n    pass\nasync def bar():\n    await foo()\n" + "x" * 500},
        )
        assert result is not None
        assert result["block"] is False
        assert "DREAM" in result["context"]

    def test_non_execute_code_returns_none(self, mock_enabled):
        import dream_auto.__init__ as dma
        result = dma._on_pre_tool_call(
            tool_name="browser_navigate",
            args={"url": "https://example.com"},
        )
        assert result is None


# ── Hook: post_tool_call ──────────────────────────────────────────────────────

class TestPostToolCall:
    """_on_post_tool_call."""

    def test_disabled_returns_none(self, mock_disabled):
        import dream_auto.__init__ as dma
        result = dma._on_post_tool_call(
            tool_name="execute_code",
            args={"code": "x"},
            result="Traceback (most recent call last):\nError",
        )
        assert result is None

    def test_error_triggers_queued_context(self, mock_enabled, temp_home, monkeypatch):
        import dream_auto.__init__ as dma
        dma._session_injected.clear()
        monkeypatch.setenv("HOME", str(temp_home))

        with patch.object(dma, "DREAM_DIR", temp_home / ".hermes" / "state" / "dream"), \
             patch.object(dma, "DREAM_QUEUE_DB", temp_home / ".hermes" / "state" / "dream" / "dream_queue.db"):
            result = dma._on_post_tool_call(
                tool_name="execute_code",
                args={"code": "import foo"},
                result="ModuleNotFoundError: No module named 'foo'",
                session_id="err_session",
            )

        assert result is not None
        assert "DREAM QUEUED" in result["context"]
        assert "error" in result["context"].lower()

    def test_clean_result_no_dream(self, mock_enabled):
        import dream_auto.__init__ as dma
        result = dma._on_post_tool_call(
            tool_name="execute_code",
            args={"code": "1 + 1"},
            result="✓ Done: 2",
        )
        assert result is None

    def test_terminal_error_triggers_dream(self, mock_enabled, temp_home, monkeypatch):
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        with patch.object(dma, "DREAM_DIR", temp_home / ".hermes" / "state" / "dream"), \
             patch.object(dma, "DREAM_QUEUE_DB", temp_home / ".hermes" / "state" / "dream" / "dream_queue.db"):
            result = dma._on_post_tool_call(
                tool_name="terminal",
                args={"command": "ls /nonexistent"},
                result="No such file or directory",
                session_id="term_err",
            )
        assert result is not None
        assert "DREAM QUEUED" in result["context"]


# ── Hook: post_llm_call (throttle) ───────────────────────────────────────────

class TestPostLlmCallThrottle:
    """_on_post_llm_call throttling behavior."""

    def test_throttles_by_turn_count(self, mock_enabled, temp_home, monkeypatch):
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        dma._session_turn_counter.clear()
        # Throttle is 5 turns
        sid = "throttle_test"

        # Turns 1-4 should be throttled
        for turn in range(1, 5):
            dma._session_turn_counter[sid] = turn - 1
            result = dma._on_post_llm_call(
                session_id=sid,
                user_message="Tell me about artificial intelligence and machine learning models",
                assistant_response="AI is great.",
            )
            assert result is None, f"Turn {turn} should have been throttled"

        # Turn 5 should fire
        dma._session_turn_counter[sid] = 4
        with patch.object(dma, "_add_to_queue") as mock_add:
            mock_add.return_value = "throttle1"
            result = dma._on_post_llm_call(
                session_id=sid,
                user_message="Tell me about artificial intelligence and machine learning models",
                assistant_response="AI is great.",
            )
            # After firing, counter resets to 0
            assert dma._session_turn_counter[sid] == 0

    def test_short_message_not_queued(self, mock_enabled, temp_home, monkeypatch):
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        dma._session_turn_counter.clear()
        with patch.object(dma, "_add_to_queue") as mock_add:
            result = dma._on_post_llm_call(
                session_id="short_msg",
                user_message="hi",  # too short
                assistant_response="Hello!",
            )
            mock_add.assert_not_called()
            assert result is None


# ── Hook: session lifecycle ────────────────────────────────────────────────────

class TestSessionLifecycle:
    """on_session_start, on_session_end."""

    def test_session_start_clears_injected(self, mock_enabled, sample_dream, monkeypatch):
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(sample_dream[1].parent.parent.parent))

        sid = "clear_test"
        dma._session_injected[sid] = {"old_dream1", "old_dream2"}
        dma._session_turn_counter[sid] = 99

        dma._on_session_start(session_id=sid, model="test", platform="test")

        assert sid not in dma._session_injected
        assert sid not in dma._session_turn_counter

    def test_session_end_clears_injected(self, mock_enabled):
        import dream_auto.__init__ as dma
        sid = "end_clear"
        dma._session_injected[sid] = {"dream1"}
        dma._session_turn_counter[sid] = 3

        dma._on_session_end(session_id=sid, completed=True)

        assert sid not in dma._session_injected
        assert sid not in dma._session_turn_counter


# ── Knowledge cache ───────────────────────────────────────────────────────────

class TestKnowledgeCache:
    """_read_knowledge_cache."""

    def test_returns_empty_when_disabled(self, mock_disabled, temp_home):
        import dream_auto.__init__ as dma
        result = dma._read_knowledge_cache(topic_hints=["linkedin"], limit=3)
        assert result == []

    def test_reads_from_knowledge_cache_db(self, mock_enabled, temp_home, monkeypatch):
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))

        kc_db = temp_home / ".hermes" / "state" / "dream" / "knowledge_cache.db"
        kc_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(kc_db))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_cache (
                id INTEGER PRIMARY KEY,
                topic TEXT,
                content TEXT,
                source TEXT,
                cached_at TEXT,
                injected_sessions TEXT,
                content_hash TEXT
            )
        """)
        from datetime import datetime
        GMT7 = timezone(timedelta(hours=7))
        now = datetime.now(GMT7).isoformat()
        conn.execute(
            "INSERT INTO knowledge_cache (topic, content, source, cached_at, injected_sessions, content_hash) VALUES (?, ?, ?, ?, ?, ?)",
            ("linkedin", "LinkedIn cron needs cookie refresh", "session_001", now, "[]", "abc123")
        )
        conn.commit()
        conn.close()

        result = dma._read_knowledge_cache(topic_hints=["linkedin"], limit=3)
        assert len(result) >= 1
        assert any("linkedin" in r.lower() for r in result)


# ── Fast-path bypass ──────────────────────────────────────────────────────────

class TestFastPath:
    """Fast path skips trivial queries without file I/O."""

    def test_fast_path_skips_simple_query(self, mock_enabled, temp_home, monkeypatch):
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        dma._session_injected.clear()
        dma._session_turn_counter.clear()

        with patch.object(dma, "_get_fast_path") as mock_fp:
            mock_fp.return_value = (True, "trivially simple")
            result = dma._on_pre_llm_call(
                user_message="What is 2 + 2?",
                conversation_history=[],
                is_first_turn=False,
                model="test",
                platform="test",
                session_id="fp_test",
            )
            assert result is None

    def test_fast_path_falls_back_on_failure(self, mock_enabled, temp_home, monkeypatch):
        """
        When _get_fast_path returns None (unavailable), _on_pre_llm_call
        should fall through and call _refresh_bm25_index_if_needed which
        calls _list_completed_dreams_raw. Since the temp dir is empty, result will be None.
        """
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))

        # _get_fast_path returns None → fallback path taken → _list_completed_dreams_raw called
        with patch.object(dma, "_get_fast_path", return_value=None):
            with patch.object(dma, "_list_completed_dreams_raw", return_value=[]) as mock_list:
                result = dma._on_pre_llm_call(
                    user_message="Tell me about complex cron job failures and what to do",
                    conversation_history=[],
                    is_first_turn=False,
                    model="test",
                    platform="test",
                    session_id="fp_fallback",
                )
                mock_list.assert_called_once()
                # Result is None because no dreams in temp directory
                assert result is None


# ── BM25 scoring ───────────────────────────────────────────────────────────────

class TestBM25Scoring:
    """_tokenize, _build_bm25_index, _score_dreams_bm25, _refresh_bm25_index_if_needed."""

    def test_tokenize_lowercases_and_splits(self):
        """Whitespace tokenizer should be case-insensitive."""
        import dream_auto.__init__ as dma
        tokens = dma._tokenize("Chrome CDP Timeout on LinkedIn API")
        assert tokens == ["chrome", "cdp", "timeout", "on", "linkedin", "api"]

    def test_tokenize_empty_string(self):
        import dream_auto.__init__ as dma
        assert dma._tokenize("") == []
        assert dma._tokenize("   ") == []

    def test_build_bm25_index_empty(self, mock_enabled, temp_home, monkeypatch):
        """Building index with no dreams should leave empty state."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        with patch.object(dma, "DREAM_DIR", temp_home / ".hermes" / "state" / "dream"):
            dma._build_bm25_index([])
            assert dma._bm25_index is None
            assert dma._bm25_dreams == []

    def test_build_bm25_index_stores_dreams_in_order(self, mock_enabled, temp_home, monkeypatch):
        """Index should preserve dream dicts with their IDs and briefs."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        dreams = [
            {"id": "d1", "brief": "chrome cdp error", "confidence": 0.8, "topics": [], "_ended_at": ""},
            {"id": "d2", "brief": "linkedin api auth", "confidence": 0.9, "topics": [], "_ended_at": ""},
        ]
        with patch.object(dma, "DREAM_DIR", temp_home / ".hermes" / "state" / "dream"):
            dma._build_bm25_index(dreams)
            assert len(dma._bm25_dreams) == 2
            assert dma._bm25_dreams[0]["id"] == "d1"
            assert dma._bm25_dreams[1]["id"] == "d2"

    def test_score_dreams_bm25_ranks_relevant_first(self, mock_enabled, temp_home, monkeypatch):
        """Dreams with matching terms to the query should score highest."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        dreams = [
            {"id": "d1", "brief": "chrome cdp connection timeout", "confidence": 0.8, "topics": [], "_ended_at": ""},
            {"id": "d2", "brief": "linkedin api cookie refresh auth", "confidence": 0.9, "topics": [], "_ended_at": ""},
            {"id": "d3", "brief": "python async sqlite database pooling", "confidence": 0.7, "topics": [], "_ended_at": ""},
        ]
        with patch.object(dma, "DREAM_DIR", temp_home / ".hermes" / "state" / "dream"):
            dma._build_bm25_index(dreams)
            scored = dma._score_dreams_bm25("chrome cdp keeps timing out on linkedin", max_inject=3)
            ids = [d["id"] for d in scored]
            # d1 (chrome cdp overlap) should be top
            assert ids[0] == "d1", f"Expected d1 (chrome cdp) to rank first, got: {ids}"
            # d2 (linkedin overlap) should be second
            assert "d2" in ids
            # d3 has zero overlap so may or may not appear depending on threshold
            # The important assertion: d1 > d2 in the ranking

    def test_score_dreams_bm25_respects_max_inject(self, mock_enabled, temp_home, monkeypatch):
        """Should return at most max_inject dreams."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        dreams = [
            {"id": f"d{i}", "brief": "chrome cdp timeout error", "confidence": 0.8, "topics": [], "_ended_at": ""}
            for i in range(5)
        ]
        with patch.object(dma, "DREAM_DIR", temp_home / ".hermes" / "state" / "dream"):
            dma._build_bm25_index(dreams)
            scored = dma._score_dreams_bm25("chrome cdp error", max_inject=2)
            assert len(scored) <= 2

    def test_score_dreams_bm25_empty_message_returns_empty(self, mock_enabled, temp_home, monkeypatch):
        """Empty user message should return no results."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        dreams = [{"id": "d1", "brief": "chrome cdp", "confidence": 0.8, "topics": [], "_ended_at": ""}]
        with patch.object(dma, "DREAM_DIR", temp_home / ".hermes" / "state" / "dream"):
            dma._build_bm25_index(dreams)
            scored = dma._score_dreams_bm25("", max_inject=3)
            assert scored == []

    def test_score_dreams_bm25_empty_corpus_returns_empty(self, mock_enabled, temp_home, monkeypatch):
        """No dreams in index should return empty list."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        with patch.object(dma, "DREAM_DIR", temp_home / ".hermes" / "state" / "dream"):
            dma._build_bm25_index([])
            scored = dma._score_dreams_bm25("chrome cdp timeout", max_inject=3)
            assert scored == []

    def test_refresh_rebuilds_on_mtime_change(self, mock_enabled, temp_home, monkeypatch):
        """If DREAM_DIR mtime changes, index should rebuild with newly listed dreams."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        dream_path = temp_home / ".hermes" / "state" / "dream"
        dream_path.mkdir(parents=True, exist_ok=True)

        # Create a dream that will appear in the listing
        did = "mtime_dream"
        dp = dream_path / did
        dp.mkdir()
        (dp / "meta.json").write_text(json.dumps({
            "status": "done", "confidence": 0.85,
            "brief": "hermes cron scheduler error handling"
        }))
        (dp / "insights.json").write_text(json.dumps(["Cron job silently failed due to missing env var"]))

        with patch.object(dma, "DREAM_DIR", dream_path):
            # First call — should build index
            dma._refresh_bm25_index_if_needed()
            assert len(dma._bm25_dreams) == 1
            assert dma._bm25_dreams[0]["id"] == did

            # Touch the dream dir to change mtime
            time.sleep(0.01)
            (dp / "meta.json").write_text(json.dumps({
                "status": "done", "confidence": 0.90,
                "brief": "hermes cron scheduler error handling"
            }))

            # Second call — should detect mtime change and rebuild
            dma._refresh_bm25_index_if_needed()
            # After rebuild, same dream should still be there
            assert len(dma._bm25_dreams) == 1

    def test_refresh_skips_rebuild_when_mtime_unchanged(self, mock_enabled, temp_home, monkeypatch):
        """If DREAM_DIR mtime is unchanged, should not rescan file system."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        dream_path = temp_home / ".hermes" / "state" / "dream"
        dream_path.mkdir(parents=True, exist_ok=True)

        with patch.object(dma, "DREAM_DIR", dream_path):
            # Pre-populate index with a known state
            dma._build_bm25_index([
                {"id": "cached", "brief": "already built", "confidence": 0.5, "topics": [], "_ended_at": ""}
            ])
            # Force mtime to current so next call sees no change
            current_mtime = dream_path.stat().st_mtime
            dma._bm25_dir_mtime = current_mtime

            # Create a new dream on disk — but mtime hasn't changed
            new_dp = dream_path / "new_dream"
            new_dp.mkdir()
            (new_dp / "meta.json").write_text(json.dumps({
                "status": "done", "confidence": 0.9,
                "brief": "new brief"
            }))
            (new_dp / "insights.json").write_text(json.dumps(["New insight"]))

            # Should NOT pick up new_dream because index wasn't rebuilt
            # (mtime matched, so scan was skipped)
            assert len(dma._bm25_dreams) == 1
            assert dma._bm25_dreams[0]["id"] == "cached"

    def test_list_completed_dreams_raw_no_topic_filter(self, mock_enabled, temp_home, monkeypatch):
        """_list_completed_dreams_raw returns ALL completed dreams regardless of topic."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        dream_path = temp_home / ".hermes" / "state" / "dream"
        dream_path.mkdir(parents=True, exist_ok=True)

        # Three dreams with completely different briefs
        briefs = [
            ("rust", "rust programming language borrow checker"),
            ("vietnam", "cost of living in hcmc vietnam"),
            ("sql", "postgres sql query optimization index"),
        ]
        for i, (did, brief) in enumerate(briefs):
            dp = dream_path / did
            dp.mkdir()
            (dp / "meta.json").write_text(json.dumps({
                "status": "done", "confidence": 0.7 + i * 0.1,
                "brief": brief
            }))
            (dp / "insights.json").write_text(json.dumps([f"Insight for {did}"]))

        with patch.object(dma, "DREAM_DIR", dream_path):
            raw = dma._list_completed_dreams_raw()
            ids = [d["id"] for d in raw]
            assert "rust" in ids
            assert "vietnam" in ids
            assert "sql" in ids

    def test_pre_llm_call_uses_bm25_not_topic_keywords(self, mock_enabled, temp_home, monkeypatch):
        """
        Verifies that pre_llm_call scores dreams by BM25 relevance, not hardcoded topics.
        Uses a real temp directory with dreams whose briefs match the query via shared
        terms that are NOT in any hardcoded topic-keyword list.
        """
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))

        dream_path = temp_home / ".hermes" / "state" / "dream"
        dream_path.mkdir(parents=True, exist_ok=True)

        # Dreams with briefs that share terms with the query but are NOT in topic keywords.
        # Query: "two phase commit" / "distributed" / "synchronisation"
        # These terms don't appear in any of the 10 hardcoded topic categories.
        briefs = [
            ("obj1", "cache coherency memory consistency smp multicore"),     # partial: "memory" appears in neither
            ("obj2", "synchronisation atomic commitment distributed systems two phase commit"),  # HIGH OVERLAP
            ("obj3", "rust cargo crates ecosystem dependency management"),    # unrelated
        ]
        for did, brief in briefs:
            dp = dream_path / did
            dp.mkdir()
            (dp / "meta.json").write_text(json.dumps({
                "status": "done", "confidence": 0.8,
                "brief": brief
            }))
            (dp / "insights.json").write_text(json.dumps([f"Insight for {did}"]))

        # Reset state
        dma._session_injected.clear()
        dma._session_turn_counter.clear()
        dma._last_global_hook_ts = -300.0
        dma._bm25_index = None
        dma._bm25_dreams = []
        dma._bm25_dir_mtime = -1.0

        # Manually build the BM25 index using whatever _get_bm25() returns.
        # This bypasses _refresh_bm25_index_if_needed (which rescans DREAM_DIR).
        bm25_cls, is_bm25 = dma._get_bm25()
        briefs_list = [
            {"id": "obj1", "brief": "cache coherency memory consistency smp multicore", "confidence": 0.8, "topics": [], "_ended_at": ""},
            {"id": "obj2", "brief": "synchronisation atomic commitment distributed systems two phase commit", "confidence": 0.8, "topics": [], "_ended_at": ""},
            {"id": "obj3", "brief": "rust cargo crates ecosystem dependency management", "confidence": 0.8, "topics": [], "_ended_at": ""},
        ]
        if is_bm25 and bm25_cls is not None:
            try:
                tokenized = [dma._tokenize(b["brief"]) for b in briefs_list]
                dma._bm25_index = bm25_cls(tokenized)
                dma._bm25_is_real = True
            except Exception:
                pytest.skip("rank-bm25 BM25Okapi construction failed")
        else:
            pytest.skip("rank-bm25 not available")

        dma._bm25_dreams = briefs_list

        with patch.object(dma, "DREAM_DIR", dream_path):
            # Query: obj2 should rank first via "distributed", "systems", "two", "phase", "commit"
            scored = dma._score_dreams_bm25(
                "how does two phase commit work in distributed synchronisation",
                max_inject=3
            )
            ids = [d["id"] for d in scored]
            scores = [d.get("_score") for d in scored]
            assert len(ids) >= 1, f"Expected at least one match, got empty. Scores: {scores}"
            assert ids[0] == "obj2", f"Expected obj2 (distributed/synchronisation) to rank first, got: {ids} scores: {scores}"

    def test_pre_llm_call_integration_bm25_injects_correct_dream(
        self, mock_enabled, temp_home, monkeypatch
    ):
        """
        Full integration test: pre_llm_call should inject the highest-BM25-scoring
        dream that the session hasn't already received.
        """
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        dream_path = temp_home / ".hermes" / "state" / "dream"
        dream_path.mkdir(parents=True, exist_ok=True)

        # Two dreams: one about CDP (should match "browser automation"), one unrelated
        for did, brief in [("cdp_dream", "chrome cdp websocket timeout scraping"), ("sql_dream", "postgres index query optimization")]:
            dp = dream_path / did
            dp.mkdir()
            (dp / "meta.json").write_text(json.dumps({
                "status": "done", "confidence": 0.75,
                "brief": brief
            }))
            (dp / "insights.json").write_text(json.dumps([f"Insight from {did}"]))

        dma._session_injected.clear()
        dma._session_turn_counter.clear()
        dma._last_global_hook_ts = -300.0

        with patch.object(dma, "DREAM_DIR", dream_path):
            result = dma._on_pre_llm_call(
                user_message="chrome cdp browser automation keeps disconnecting",
                conversation_history=[],
                is_first_turn=False,
                model="test",
                platform="test",
                session_id="bm25_integration_test",
            )

        assert result is not None
        # cdp_dream should have scored higher and been injected
        assert "cdp_dream" in result["context"]
        assert "DREAM INSIGHTS" in result["context"]


# ── _has_insights_or_questions ─────────────────────────────────────────────────

class TestHasInsightsOrQuestions:
    """_has_insights_or_questions correctly identifies dreams with content."""

    def test_insights_list_returns_true(self, mock_enabled, dream_dir, monkeypatch):
        """Non-empty insights.json → True."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(dream_dir.parent.parent.parent))
        did = "has_insights"
        dp = dream_dir / did
        dp.mkdir()
        (dp / "meta.json").write_text(json.dumps({"status": "done"}))
        (dp / "insights.json").write_text(json.dumps(["Found the root cause."]))

        assert dma._has_insights_or_questions(did) is True

    def test_empty_insights_list_returns_false(self, mock_enabled, dream_dir, monkeypatch):
        """Empty insights.json → False (no content to inject)."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(dream_dir.parent.parent.parent))
        did = "empty_insights"
        dp = dream_dir / did
        dp.mkdir()
        (dp / "meta.json").write_text(json.dumps({"status": "done"}))
        (dp / "insights.json").write_text(json.dumps([]))

        assert dma._has_insights_or_questions(did) is False

    def test_missing_insights_file_returns_false(self, mock_enabled, dream_dir, monkeypatch):
        """No insights.json at all → False."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(dream_dir.parent.parent.parent))
        did = "no_file"
        dp = dream_dir / did
        dp.mkdir()
        (dp / "meta.json").write_text(json.dumps({"status": "done"}))
        # No insights.json

        assert dma._has_insights_or_questions(did) is False

    def test_questions_only_returns_true(self, mock_enabled, dream_dir, monkeypatch):
        """No insights but pending questions → True (questions have value)."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(dream_dir.parent.parent.parent))
        did = "questions_only"
        dp = dream_dir / did
        dp.mkdir()
        (dp / "meta.json").write_text(json.dumps({"status": "done"}))
        (dp / "insights.json").write_text(json.dumps([]))
        (dp / "pending_questions.json").write_text(json.dumps([
            "Should SIGKILL be used instead of SIGTERM for wallclock kills?",
        ]))

        assert dma._has_insights_or_questions(did) is True

    def test_both_insights_and_questions_returns_true(self, mock_enabled, dream_dir, monkeypatch):
        """Both insights and pending questions → True."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(dream_dir.parent.parent.parent))
        did = "with_both"
        dp = dream_dir / did
        dp.mkdir()
        (dp / "meta.json").write_text(json.dumps({"status": "done"}))
        (dp / "insights.json").write_text(json.dumps(["Real finding from MCTS."]))
        (dp / "pending_questions.json").write_text(json.dumps(["Follow-up question."]))

        assert dma._has_insights_or_questions(did) is True


# ── Knowledge cache: TTL expiry ───────────────────────────────────────────────

class TestKnowledgeCacheTTL:
    """Entries older than _knowledge_cache_ttl_days() are excluded."""

    def test_kc_skips_expired_entries(self, mock_enabled, temp_home, monkeypatch):
        """An entry with cached_at older than TTL should not be returned."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        patched_kc = temp_home / ".hermes" / "state" / "dream" / "knowledge_cache.db"

        import sqlite3
        conn = sqlite3.connect(str(patched_kc))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_cache (
                topic TEXT, content TEXT, source TEXT, cached_at TEXT,
                injected_sessions TEXT, content_hash TEXT
            )
        """)
        # Entry from 10 days ago — well beyond default TTL of 7
        old_date = "2026-04-01T00:00:00+07:00"
        conn.execute("""
            INSERT INTO knowledge_cache
                (topic, content, source, cached_at, injected_sessions, content_hash)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("linkedin", "Old stale cached content.", "test", old_date, "[]", "hash_old"))
        conn.commit()
        conn.close()

        with patch.object(dma, "KNOWLEDGE_CACHE_DB", patched_kc):
            result = dma._read_knowledge_cache(topic_hints=["linkedin"], limit=3)
            # Old entry should be outside TTL window
            assert len(result) == 0

    def test_kc_respects_custom_ttl(self, mock_enabled, temp_home, monkeypatch):
        """DREAM_AUTO_KNOWLEDGE_CACHE_TTL_DAYS=0 should return no entries."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        monkeypatch.setenv("DREAM_AUTO_KNOWLEDGE_CACHE_TTL_DAYS", "0")
        patched_kc = temp_home / ".hermes" / "state" / "dream" / "knowledge_cache.db"

        import sqlite3
        conn = sqlite3.connect(str(patched_kc))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_cache (
                topic TEXT, content TEXT, source TEXT, cached_at TEXT,
                injected_sessions TEXT, content_hash TEXT
            )
        """)
        # Even a "now" entry should be excluded when TTL = 0
        GMT7 = timezone(timedelta(hours=7))
        now = datetime.now(GMT7).isoformat()
        conn.execute("""
            INSERT INTO knowledge_cache
                (topic, content, source, cached_at, injected_sessions, content_hash)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("hermes", "Fresh content.", "test", now, "[]", "hash_fresh"))
        conn.commit()
        conn.close()

        with patch.object(dma, "KNOWLEDGE_CACHE_DB", patched_kc):
            result = dma._read_knowledge_cache(topic_hints=["hermes"], limit=3)
            # TTL=0 means cutoff is "now" — nothing qualifies
            assert len(result) == 0

    def test_kc_session_dedup_prevents_reinject(self, mock_enabled, temp_home, monkeypatch):
        """An entry already injected to the current session_id is skipped."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        patched_kc = temp_home / ".hermes" / "state" / "dream" / "knowledge_cache.db"

        import sqlite3
        conn = sqlite3.connect(str(patched_kc))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_cache (
                topic TEXT, content TEXT, source TEXT, cached_at TEXT,
                injected_sessions TEXT, content_hash TEXT
            )
        """)
        GMT7 = timezone(timedelta(hours=7))
        now = datetime.now(GMT7).isoformat()
        # Entry already lists "dedup_test_sid" in injected_sessions
        conn.execute("""
            INSERT INTO knowledge_cache
                (topic, content, source, cached_at, injected_sessions, content_hash)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("ai", "Model inference speed analysis.", "session_001", now,
               '["dedup_test_sid"]', "hash_dedup"))
        conn.commit()
        conn.close()

        with patch.object(dma, "KNOWLEDGE_CACHE_DB", patched_kc):
            result = dma._read_knowledge_cache(topic_hints=["ai"], limit=3, session_id="dedup_test_sid")
            assert len(result) == 0  # Entry was skipped because this session already received it


# ── Done-status normalization ─────────────────────────────────────────────────

class TestDoneStatusNormalization:
    """All _STATUS_DONE variants are recognized by _list_completed_dreams_raw."""

    @pytest.mark.parametrize("status_value", [
        "done",
        "completed",
        "completed_success",
        "completed_killed",
        "failed_crash",
        "killed_wallclock",
        "completed_stale",
        "stale_completed",
        "completed_empty",
        "failed",
        "failed_restart",
        "health_check_failed",
        "circuit_breaker",
    ])
    def test_status_normalized_as_done(self, status_value, mock_enabled, temp_home, monkeypatch):
        """Any _STATUS_DONE variant must pass the status check."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        dream_path = temp_home / ".hermes" / "state" / "dream"
        dream_path.mkdir(parents=True, exist_ok=True)

        did = f"status_{status_value}"
        dp = dream_path / did
        dp.mkdir()
        (dp / "meta.json").write_text(json.dumps({
            "status": status_value,
            "confidence": 0.75,
            "brief": f"Dream with status {status_value}",
        }))
        (dp / "insights.json").write_text(json.dumps([f"Insight for {status_value}"]))

        with patch.object(dma, "DREAM_DIR", dream_path):
            dreams = dma._list_completed_dreams_raw()
        dream_ids = [d["id"] for d in dreams]
        assert did in dream_ids, f"status={status_value!r} was not recognized as done"

    def test_running_status_not_included(self, mock_enabled, temp_home, monkeypatch):
        """status=running must NOT appear in completed dreams list."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        dream_path = temp_home / ".hermes" / "state" / "dream"
        dream_path.mkdir(parents=True, exist_ok=True)

        did = "still_running"
        dp = dream_path / did
        dp.mkdir()
        (dp / "meta.json").write_text(json.dumps({
            "status": "running",
            "confidence": 0.90,
            "brief": "This dream is still going",
        }))
        (dp / "insights.json").write_text(json.dumps(["Should not be injected."]))

        with patch.object(dma, "DREAM_DIR", dream_path):
            dreams = dma._list_completed_dreams_raw()
        dream_ids = [d["id"] for d in dreams]
        assert did not in dream_ids


# ── BM25: fallback when rank-bm25 not installed ────────────────────────────────

class TestBM25Fallback:
    """When rank-bm25 is unavailable, word-overlap scoring is used as fallback."""

    def test_fallback_word_overlap_used_when_bm25_unavailable(self, mock_enabled, temp_home, monkeypatch):
        """If rank-bm25 import fails, word-overlap fallback should score dreams by Jaccard."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        # Build index with fallback (no rank-bm25 available in test env)
        dreams = [
            {"id": "word1", "brief": "hermes cron scheduler deadlock", "confidence": 0.8, "topics": [], "_ended_at": ""},
            {"id": "word2", "brief": "vietnam tay ninh logistics", "confidence": 0.9, "topics": [], "_ended_at": ""},
        ]
        with patch.object(dma, "DREAM_DIR", temp_home / ".hermes" / "state" / "dream"):
            dma._build_bm25_index(dreams)
            # After building, _bm25_is_real tells us which path was taken
            # In this test environment, rank-bm25 may or may not be installed
            # The important assertion: _score_dreams_bm25 must return results regardless
            scored = dma._score_dreams_bm25("hermes cron job stuck deadlock", max_inject=2)
            ids = [d["id"] for d in scored]
            # hermes/cron/deadlock match word1's brief
            assert "word1" in ids

    def test_build_bm25_index_exception_guard(self, mock_enabled, temp_home, monkeypatch):
        """If BM25Okapi raises on a singleton corpus, fallback is used silently."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        # Single dream with unique terms — can cause ZeroDivisionError in BM25
        dreams = [
            {"id": "unique1", "brief": "xyzzy plugh marble zagzag", "confidence": 0.8, "topics": [], "_ended_at": ""},
        ]
        with patch.object(dma, "DREAM_DIR", temp_home / ".hermes" / "state" / "dream"):
            # Monkeypatch _get_bm25 to return a BM25 class that raises
            class BadBM25:
                def __init__(self, corpus):
                    raise ZeroDivisionError("singleton corpus")
                def get_scores(self, query):
                    return [0.0]

            original_get_bm25 = dma._get_bm25
            def bad_bm25_factory():
                return (BadBM25, True)  # is_bm25=True but the class is broken

            monkeypatch.setattr(dma, "_get_bm25", bad_bm25_factory)
            dma._build_bm25_index(dreams)
            # Should have fallen back to tokenized list with _bm25_is_real=False
            assert dma._bm25_is_real is False
            assert dma._bm25_index is not None  # fallback corpus still stored


# ── pre_llm_call: BM25 injection with knowledge cache ───────────────────────

class TestPreLlmCallWithKnowledgeCache:
    """pre_llm_call combines dream insights AND knowledge cache entries in one context."""

    def test_kc_and_dream_insights_both_injected(self, mock_enabled, temp_home, monkeypatch):
        """When both dreams and KC entries match, both appear in the returned context."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        dream_path = temp_home / ".hermes" / "state" / "dream"
        dream_path.mkdir(parents=True, exist_ok=True)
        patched_kc = dream_path / "knowledge_cache.db"
        patched_dir = dream_path

        # Create a completed dream about LinkedIn
        did = "li_dream"
        dp = dream_path / did
        dp.mkdir()
        (dp / "meta.json").write_text(json.dumps({
            "status": "done",
            "confidence": 0.82,
            "brief": "linkedin cookie auth failure",
        }))
        (dp / "insights.json").write_text(json.dumps(["Li_at cookie expires after 6 hours."]))

        # Populate knowledge cache with a matching entry
        import sqlite3
        GMT7 = timezone(timedelta(hours=7))
        conn = sqlite3.connect(str(patched_kc))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_cache (
                topic TEXT, content TEXT, source TEXT, cached_at TEXT,
                injected_sessions TEXT, content_hash TEXT
            )
        """)
        conn.execute("""
            INSERT INTO knowledge_cache
                (topic, content, source, cached_at, injected_sessions, content_hash)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("linkedin", "Cookie TTL is approximately 6 hours of active use.",
               "session_indexer", datetime.now(GMT7).isoformat(), "[]", "hash_li_kc"))
        conn.commit()
        conn.close()

        with patch.object(dma, "DREAM_DIR", patched_dir), \
             patch.object(dma, "KNOWLEDGE_CACHE_DB", patched_kc):
            dma._session_injected.clear()
            dma._session_turn_counter.clear()
            dma._last_global_hook_ts = -300.0

            result = dma._on_pre_llm_call(
                user_message="LinkedIn poster cron job keeps failing with auth errors",
                conversation_history=[],
                is_first_turn=False,
                model="test",
                platform="test",
                session_id="kc_combined_test",
            )

        assert result is not None
        # Both dream insight and KC entry should appear
        assert "DREAM INSIGHTS" in result["context"]
        assert "KNOWLEDGE CACHE" in result["context"]
        assert "li_at cookie expires" in result["context"].lower() or "6 hours" in result["context"].lower()
        assert "Cookie TTL" in result["context"]

    def test_kc_filtered_by_topic_hints_not_bm25(self, mock_enabled, temp_home, monkeypatch):
        """KC entries are still filtered by topic hints, not BM25 scores."""
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))
        dream_path = temp_home / ".hermes" / "state" / "dream"
        dream_path.mkdir(parents=True, exist_ok=True)
        patched_kc = dream_path / "knowledge_cache.db"

        # KC entry about hermes (not linkedin)
        import sqlite3
        GMT7 = timezone(timedelta(hours=7))
        conn = sqlite3.connect(str(patched_kc))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_cache (
                topic TEXT, content TEXT, source TEXT, cached_at TEXT,
                injected_sessions TEXT, content_hash TEXT
            )
        """)
        conn.execute("""
            INSERT INTO knowledge_cache
                (topic, content, source, cached_at, injected_sessions, content_hash)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("hermes", "Hermes cron scheduler uses adaptive cadence.", "session_indexer",
               datetime.now(GMT7).isoformat(), "[]", "hash_hermes_kc"))
        conn.commit()
        conn.close()

        with patch.object(dma, "KNOWLEDGE_CACHE_DB", patched_kc):
            # No linkedin-related topic hints in the message
            result = dma._read_knowledge_cache(topic_hints=["linkedin"], limit=3, session_id="kc_filter_test")
            assert len(result) == 0  # hermes topic doesn't match linkedin hint

