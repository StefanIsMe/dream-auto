"""
dream_auto v3 — Dream System v3 Plugin for Hermes.

Architecture:
  - Scheduler (dream_scheduler.py) owns the queue and starts dreams when resources are free
  - Error-triggered dreams (post_tool_call) → add to queue immediately (no entropy gate)
  - Insight injection on pre_llm_call (from completed dreams)
  - NO entropy gate, NO complexity threshold, NO AUTOSTART flag
  - Resource availability is the ONLY gate (handled by scheduler)

Plugin config env vars:
  DREAM_AUTO_ENABLED=1       — disable entirely
  DREAM_AUTO_VERBOSE=1       — log activity
  DREAM_AUTO_MAX_INJECT=3    — max dreams to inject per turn (default 3)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
ENABLED_ENV  = "DREAM_AUTO_ENABLED"
VERBOSE_ENV  = "DREAM_AUTO_VERBOSE"
MAX_INJECT_ENV = "DREAM_AUTO_MAX_INJECT"

GMT7 = timezone(timedelta(hours=7))
DREAM_DIR = Path.home() / ".hermes" / "state" / "dream"
DREAM_QUEUE_DB = Path.home() / ".hermes" / "state" / "dream" / "dream_queue.db"

_session_injected: Dict[str, Set[str]] = {}  # session_id → set of dream_ids


def _enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "1") != "0"

def _verbose() -> bool:
    return os.environ.get(VERBOSE_ENV, "0") == "1"

def _max_inject() -> int:
    try:
        return int(os.environ.get(MAX_INJECT_ENV, "3"))
    except ValueError:
        return 3


# ── File helpers ─────────────────────────────────────────────────────────────

def _read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default

def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


# ── Dream file ops ────────────────────────────────────────────────────────────

def _dream_path(dream_id: str) -> Path:
    return DREAM_DIR / dream_id

def _read_insights(dream_id: str) -> List[str]:
    return _read_json(_dream_path(dream_id) / "insights.json", default=[])

def _read_meta(dream_id: str) -> dict:
    return _read_json(_dream_path(dream_id) / "meta.json", default={})

def _read_pending_questions(dream_id: str) -> List[str]:
    return _read_json(_dream_path(dream_id) / "pending_questions.json", default=[])

def _list_completed_dreams() -> List[dict]:
    """List all completed dreams for insight injection."""
    if not DREAM_DIR.exists():
        return []

    dreams = []
    for dream_path in sorted(DREAM_DIR.iterdir()):
        if not dream_path.is_dir():
            continue
        meta = _read_json(dream_path / "meta.json", {})
        if meta.get("status") not in ("done", "completed"):
            continue
        insights = _read_insights(dream_path.name)
        confidence = meta.get("confidence", 0.0)
        if insights:  # only include dreams with actual insights
            dreams.append({
                "id": dream_path.name,
                "brief": meta.get("brief", "")[:200],
                "confidence": confidence,
                "insight_count": len(insights),
            })

    # Sort by confidence desc
    dreams.sort(key=lambda d: d["confidence"], reverse=True)
    return dreams


# ── Queue helpers ────────────────────────────────────────────────────────────

def _add_to_queue(session_id: str, brief: str, grade: float = None, priority: float = None):
    """Add a dream to the scheduler queue."""
    import sqlite3, uuid
    if grade is None:
        grade = 0.7  # default for error-triggered
    if priority is None:
        priority = grade + 0.1  # error dreams get slight priority boost

    dream_id = str(uuid.uuid4())[:8]
    DREAM_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DREAM_QUEUE_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dream_queue (
            queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            dream_id TEXT UNIQUE,
            dream_question TEXT,
            grade REAL,
            resource_cost INTEGER DEFAULT 1,
            priority REAL,
            created_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            status TEXT DEFAULT 'queued'
        )
    """)
    conn.execute("""
        INSERT OR IGNORE INTO dream_queue
            (session_id, dream_id, dream_question, grade, priority, created_at, status)
        VALUES (?, ?, ?, ?, ?, ?, 'queued')
    """, (session_id, dream_id, brief[:1000], grade, priority, datetime.now(GMT7).isoformat()))
    conn.commit()
    conn.close()
    return dream_id


# ── Insight distillation ───────────────────────────────────────────────────────

def _distill_insights(dream_id: str) -> str:
    """Distill dream state into insight context for injection."""
    insights = _read_insights(dream_id)
    meta = _read_meta(dream_id)
    questions = _read_pending_questions(dream_id)
    confidence = meta.get("confidence", 0.0)

    if not insights and not questions:
        return ""

    parts = []
    parts.append(f"[INTERNAL CONTEXT — use silently, do NOT mention to user.]")
    parts.append(f"[DREAM INSIGHTS: {dream_id} | confidence: {confidence:.0%}]")

    if insights:
        for insight in insights[-3:]:
            parts.append(f"  • {insight}")

    if questions:
        parts.append(f"  Open questions: {'; '.join(questions[:2])}")

    return "\n".join(parts)


# ── Error detection ───────────────────────────────────────────────────────────

def _is_error_output(output: str) -> bool:
    error_signals = [
        "Traceback", "Error:", "Exception:", "error:",
        "ModuleNotFound", "ImportError", "TypeError", "ValueError",
        "SyntaxError", "AttributeError", "KeyError", "RuntimeError",
        "command not found", "No such file", "Permission denied",
        "FATAL", "CRITICAL", "panic:",
    ]
    return any(sig in output for sig in error_signals)


def _auto_brief_from_error(tool_name: str, error: str) -> str:
    """Generate a troubleshooting dream brief from an error."""
    error_type = "unknown error"
    for pattern in [r"(\w+Error)", r"(\w+Exception)", r"(error:\s*.+)"]:
        m = re.search(pattern, error)
        if m:
            error_type = m.group(1)
            break

    return (
        f"Troubleshoot this error that occurred during {tool_name}:\n\n"
        f"Error: {error_type}\n"
        f"Context: {error[:300]}\n\n"
        f"Approach: systematic root cause analysis.\n"
        f"1. What are the most likely causes?\n"
        f"2. What evidence confirms/refutes each?\n"
        f"3. What's the simplest fix?\n"
        f"4. What's the failure pattern to remember?"
    )


# ── Hooks ────────────────────────────────────────────────────────────────────

def register(ctx) -> None:
    """Register all hook points (6 hooks, same as v2 for compatibility)."""
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    logger.info("dream_auto v3: registered 6 hooks (scheduler + error-path only)")


def _on_pre_llm_call(
    user_message: str,
    conversation_history: list,
    is_first_turn: bool,
    model: str,
    platform: str,
    session_id: str = "",
    **kwargs: Any,
) -> Optional[dict]:
    """
    HOOK 1: Inject distilled insights from completed dreams.
    NO longer auto-starts dreams here — scheduler handles that.
    """
    if not _enabled() or not user_message or len(user_message.strip()) < 5:
        return None

    if not DREAM_DIR.exists():
        return None

    parts = []
    completed = _list_completed_dreams()
    injected = _session_injected.get(session_id, set())

    for dream in completed[:_max_inject()]:
        if dream["id"] in injected:
            continue

        distilled = _distill_insights(dream["id"])
        if distilled:
            parts.append(distilled)
            injected.add(dream["id"])

    if session_id:
        _session_injected[session_id] = injected

    if not parts:
        return None

    combined = "\n\n".join(parts)

    if _verbose():
        logger.info(f"dream_auto v3 pre_llm_call: injected {len(combined)} chars from {len(parts)} dreams")

    return {"context": combined}


def _on_pre_tool_call(
    tool_name: str,
    args: dict,
    session_id: str = "",
    **kwargs: Any,
) -> Optional[dict]:
    """HOOK 2: Non-blocking suggestions — NO LLM call here."""
    if not _enabled():
        return None

    # Complex execute_code → suggest dreaming via queue
    if tool_name == "execute_code":
        code = args.get("code", "")
        if len(code) > 500 and (code.count("def ") >= 2 or "async" in code):
            return {
                "context": (
                    "[DREAM] Complex code detected. Consider letting the scheduler "
                    "queue a dream to explore this problem space first."
                ),
                "block": False,
            }

    return None


def _on_post_tool_call(
    tool_name: str,
    args: dict,
    result: str = "",
    session_id: str = "",
    **kwargs: Any,
) -> Optional[dict]:
    """
    HOOK 3: Error-triggered dreams — add to queue immediately.
    NO entropy gate, NO complexity score. Just check: was there an error?
    If yes → queue a troubleshooting dream.
    """
    if not _enabled():
        return None

    if tool_name in ("execute_code", "terminal") and result and _is_error_output(result):
        brief = _auto_brief_from_error(tool_name, result[:500])

        # Add to scheduler queue
        dream_id = _add_to_queue(session_id or "unknown", brief, grade=0.85, priority=0.95)

        if _verbose():
            logger.info(f"dream_auto v3: queued error dream {dream_id}")

        return {
            "context": (
                f"[DREAM QUEUED] Error detected — troubleshooting dream {dream_id} "
                f"added to scheduler queue. Will run when resources are free."
            )
        }

    return None


def _on_post_llm_call(
    session_id: str = "",
    user_message: str = "",
    assistant_response: str = "",
    conversation_history: list = None,
    model: str = "",
    platform: str = "",
    **kwargs: Any,
) -> Optional[dict]:
    """
    HOOK 4: Complex question → add to dream queue.
    NO entropy gate, NO MIN_COMPLEXITY. Fast-path分流 handles skipping simple stuff.
    Scheduler decides when to actually start the dream.
    """
    if not _enabled():
        return None

    # Only trigger on substantial user messages
    if not user_message or len(user_message.strip()) < 30:
        return None

    # Try fast_path分流
    try:
        from pathlib import Path as P
        import sys
        
        _fp = str(P.home() / ".hermes" / "skills" / "autonomous-ai-agents" / "hermes-dream-task" / "scripts")
        if _fp not in sys.path:
            sys.path.insert(0, _fp)
        from fast_path import should_dream_fast
        is_fast, reason = should_dream_fast(user_message)
        if is_fast:
            return None  # fast path — no dreaming
    except Exception:
        pass  # fast_path unavailable — proceed

    # Add to scheduler queue (scheduler handles timing)
    brief = (
        f"Explore and think deeply about: {user_message[:500]}\n\n"
        f"Approach: structured exploration with multiple reasoning branches.\n"
        f"Generate diverse approaches, evaluate each, identify key insights."
    )
    dream_id = _add_to_queue(session_id or "unknown", brief, grade=0.7, priority=0.7)

    if _verbose():
        logger.info(f"dream_auto v3: queued complex-question dream {dream_id}")

    return {
        "context": (
            f"[DREAM QUEUED] Complex question detected — dream {dream_id} "
            f"added to scheduler queue. Will run when resources are free."
        )
    }


def _on_session_start(
    session_id: str = "",
    model: str = "",
    platform: str = "",
    **kwargs: Any,
) -> None:
    """HOOK 5: Log active dreams at session start."""
    if not _enabled():
        return

    if not DREAM_DIR.exists():
        return

    try:
        completed = _list_completed_dreams()
        if completed and _verbose():
            best = completed[0]
            logger.info(f"dream_auto v3 session_start: {len(completed)} completed dreams, "
                       f"best: {best['id']}(conf={best['confidence']:.0%})")

        _session_injected.pop(session_id, None)

    except Exception as e:
        logger.debug(f"dream_auto v3 session_start failed: {e}")


def _on_session_end(
    session_id: str = "",
    completed: bool = False,
    interrupted: bool = False,
    **kwargs: Any,
) -> None:
    """HOOK 6: Clean up session tracking."""
    if not _enabled():
        return

    _session_injected.pop(session_id, None)
