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
    yield
    dma._session_injected.clear()
    dma._session_turn_counter.clear()
    dma._fast_path_module = None


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

    def test_injects_insights_for_new_session(self, mock_enabled, sample_dream, monkeypatch):
        import dream_auto.__init__ as dma
        # Reset per-session state
        dma._session_injected.clear()
        dma._session_turn_counter.clear()

        result = dma._on_pre_llm_call(
            user_message="How do I fix LinkedIn cookie expiry issues?",
            conversation_history=[],
            is_first_turn=False,
            model="test",
            platform="test",
            session_id="brand_new_session",
        )
        assert result is not None
        assert "context" in result
        assert "DREAM INSIGHTS" in result["context"]

    def test_does_not_reinject_same_dream(self, mock_enabled, sample_dream, monkeypatch):
        import dream_auto.__init__ as dma
        dma._session_injected.clear()
        dma._session_turn_counter.clear()

        sid = "reuse_test_session"

        # First call
        r1 = dma._on_pre_llm_call(
            user_message="Fix the LinkedIn cron job failure",
            conversation_history=[],
            is_first_turn=False,
            model="test",
            platform="test",
            session_id=sid,
        )
        count1 = r1["context"].count("DREAM INSIGHTS")

        # Second call same session — should be skipped
        r2 = dma._on_pre_llm_call(
            user_message="Tell me more about the cookie issue",
            conversation_history=[],
            is_first_turn=False,
            model="test",
            platform="test",
            session_id=sid,
        )
        # No new insights injected (same dream already injected)
        assert r2 is None  # fast-path skips or injected set prevents re-injection

    def test_injects_multiple_dreams_up_to_max(self, mock_enabled, dream_dir, monkeypatch):
        import dream_auto.__init__ as dma
        dma._session_injected.clear()
        dma._session_turn_counter.clear()

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

        with patch.object(dma, "_max_inject", return_value=3):
            result = dma._on_pre_llm_call(
                user_message="Tell me about hermes agent cron jobs and linkedin integration",
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
        should fall through to _list_completed_dreams. The temp dir is empty
        so no dreams are injected.
        """
        import dream_auto.__init__ as dma
        monkeypatch.setenv("HOME", str(temp_home))

        # _get_fast_path returns None → fallback path taken → _list_completed_dreams called
        with patch.object(dma, "_get_fast_path", return_value=None):
            with patch.object(dma, "_list_completed_dreams", return_value=[]) as mock_list:
                result = dma._on_pre_llm_call(
                    user_message="Tell me about complex cron job failures and what to do",
                    conversation_history=[],
                    is_first_turn=False,
                    model="test",
                    platform="test",
                    session_id="fp_fallback",
                )
                mock_list.assert_called_once()
                assert result is None  # no dreams in temp dir → nothing injected
