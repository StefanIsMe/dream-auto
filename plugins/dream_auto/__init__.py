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
  DREAM_AUTO_THROTTLE_TURNS=5 — post_llm_call fires at most every N turns (default 5)
  DREAM_AUTO_GLOBAL_THROTTLE=300 — global per-turn budget: skip hook entirely every N seconds (default 300 = 5min)
  DREAM_AUTO_KNOWLEDGE_CACHE_TTL_DAYS=7 — TTL for knowledge cache entries (default 7)
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
ENABLED_ENV          = "DREAM_AUTO_ENABLED"
VERBOSE_ENV          = "DREAM_AUTO_VERBOSE"
MAX_INJECT_ENV       = "DREAM_AUTO_MAX_INJECT"
THROTTLE_ENV         = "DREAM_AUTO_THROTTLE_TURNS"
GLOBAL_THROTTLE_ENV  = "DREAM_AUTO_GLOBAL_THROTTLE"
KC_TTL_ENV           = "DREAM_AUTO_KNOWLEDGE_CACHE_TTL_DAYS"

GMT7 = timezone(timedelta(hours=7))
DREAM_DIR            = Path.home() / ".hermes" / "state" / "dream"
DREAM_QUEUE_DB       = Path.home() / ".hermes" / "state" / "dream" / "dream_queue.db"
KNOWLEDGE_CACHE_DB   = Path.home() / ".hermes" / "state" / "dream" / "knowledge_cache.db"

_session_injected:       Dict[str, Set[str]] = {}  # session_id → set of dream_ids
_session_turn_counter:   Dict[str, int]    = {}  # session_id → turn count since last dream check
_last_global_hook_ts:    float             = -300.0  # sentinel: -300 so first call always passes

# ── Cached fast_path import (avoid re-import on every hook call) ───────────────
_fast_path_module = None
def _get_fast_path():
    global _fast_path_module
    if _fast_path_module is None:
        try:
            from pathlib import Path as P
            import sys
            sys.path.insert(0, str(P.home() / ".hermes" / "skills" / "autonomous-ai-agents" / "hermes-dream-task" / "scripts"))
            from fast_path import should_dream_fast
            _fast_path_module = should_dream_fast
        except Exception as e:
            logger.debug(f"dream_auto: fast_path unavailable — {e}")
            _fast_path_module = None
    return _fast_path_module


def _enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "1") != "0"

def _verbose() -> bool:
    return os.environ.get(VERBOSE_ENV, "0") == "1"

def _max_inject() -> int:
    try:
        return int(os.environ.get(MAX_INJECT_ENV, "3"))
    except ValueError:
        return 3

def _throttle_turns() -> int:
    try:
        return int(os.environ.get(THROTTLE_ENV, "5"))
    except ValueError:
        return 5

def _global_throttle_seconds() -> int:
    """Skip pre_llm_call if hook ran less than N seconds ago (default 300s = 5min)."""
    try:
        return int(os.environ.get(GLOBAL_THROTTLE_ENV, "300"))
    except ValueError:
        return 300

def _knowledge_cache_ttl_days() -> int:
    """TTL in days for knowledge cache entries (default 7)."""
    try:
        return int(os.environ.get(KC_TTL_ENV, "7"))
    except ValueError:
        return 7


# ── Done-status normalization ─────────────────────────────────────────────────

# v3 meta.json uses non-standard status values. Normalize everything here.
_STATUS_DONE = {
    "done", "completed", "completed_killed", "failed_crash",
    "killed_wallclock", "completed_stale", "stale_completed",
    "completed_empty",
}


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
    """Returns list (possibly empty) from pending_questions.json.
    Returns [] for both missing file AND invalid JSON — caller use has_insights_or_questions()
    to distinguish "has content" from "nothing to report".
    """
    path = _dream_path(dream_id) / "pending_questions.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _read_knowledge_cache(topic_hints: list[str] = None, limit: int = 3, session_id: str = None) -> List[str]:
    """
    Read entries from the predictive knowledge cache.
    Returns up to `limit` formatted entries, filtered by topic hints if provided.
    Tracks which sessions have been injected to avoid re-injecting.
    """
    if not KNOWLEDGE_CACHE_DB.exists():
        return []

    try:
        import sqlite3
        conn = sqlite3.connect(str(KNOWLEDGE_CACHE_DB))
        # Ensure indexes exist for query efficiency
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_topic_cached ON knowledge_cache(topic, cached_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cached ON knowledge_cache(cached_at DESC)")
        except Exception:
            pass

        # Configurable TTL — skip entries older than this
        ttl_days = _knowledge_cache_ttl_days()
        age_cutoff = (datetime.now(GMT7) - timedelta(days=ttl_days)).isoformat()

        if topic_hints:
            # Match entries by topic, most recent first, within TTL
            placeholders = ",".join(["?"] * len(topic_hints))
            rows = conn.execute(f"""
                SELECT content, source, topic, cached_at, injected_sessions, content_hash
                FROM knowledge_cache
                WHERE topic IN ({placeholders})
                AND cached_at > ?
                ORDER BY cached_at DESC
                LIMIT ?
            """, (*topic_hints, age_cutoff, limit * 2)).fetchall()
        else:
            rows = conn.execute("""
                SELECT content, source, topic, cached_at, injected_sessions, content_hash
                FROM knowledge_cache
                WHERE cached_at > ?
                ORDER BY cached_at DESC
                LIMIT ?
            """, (age_cutoff, limit * 2)).fetchall()

        conn.close()

        results = []
        seen_hashes = set()
        for content, source, topic, cached_at, injected_sessions, content_hash in rows:
            # Skip duplicates by content hash
            if content_hash and content_hash in seen_hashes:
                continue
            seen_hashes.add(content_hash)

            # Skip if this session already received this entry
            if session_id and injected_sessions:
                try:
                    injected_list = json.loads(injected_sessions)
                    if session_id in injected_list:
                        continue
                except Exception:
                    pass

            if len(results) >= limit:
                break
            results.append(f"[KNOWLEDGE CACHE — {source} — {topic}]\n{content[:500]}")

        return results

    except Exception:
        return []

def _has_insights_or_questions(dream_id: str) -> bool:
    """True if dream has non-empty insights OR pending questions."""
    insights = _read_json(_dream_path(dream_id) / "insights.json", None)
    if insights and isinstance(insights, list) and len(insights) > 0:
        return True
    questions = _read_pending_questions(dream_id)
    return len(questions) > 0


def _list_completed_dreams(topic_hints: list[str] = None) -> List[dict]:
    """List all completed dreams suitable for insight injection.

    Args:
        topic_hints: if provided, only return dreams whose brief contains any of these hints.
                     This pre-filters before the expensive _distill_insights call.
    """
    if not DREAM_DIR.exists():
        return []

    hints_lower = [h.lower() for h in (topic_hints or [])]

    dreams = []
    for dream_path in sorted(DREAM_DIR.iterdir(), reverse=True):  # newest first
        if not dream_path.is_dir():
            continue
        meta = _read_json(dream_path / "meta.json", {})
        # Normalize done statuses (v3 uses many variants)
        if meta.get("status") not in _STATUS_DONE:
            continue
        # Skip dreams with no content to inject
        if not _has_insights_or_questions(dream_path.name):
            continue
        # Topic pre-filter: skip if brief doesn't match any topic hint
        if hints_lower:
            brief_lower = meta.get("brief", "").lower()
            if not any(hint in brief_lower for hint in hints_lower):
                continue

        # v3 uses best_confidence, v2 uses confidence
        confidence = meta.get("best_confidence", meta.get("confidence", 0.0))
        dreams.append({
            "id": dream_path.name,
            "brief": meta.get("brief", "")[:200],
            "confidence": confidence,
            "topics": meta.get("topics", []),
        })

    # Sort by confidence desc, newest first for equal confidence
    dreams.sort(key=lambda d: (d["confidence"], 0), reverse=True)
    return dreams


# ── Queue helpers ────────────────────────────────────────────────────────────

def _add_to_queue(session_id: str, brief: str, grade: float = None, priority: float = None):
    """Add a dream to the scheduler queue.

    Deduplication: if a queued or running dream with the same session_id AND
    a brief starting with the same 60 chars already exists, skip creating a duplicate.

    DEGENERATE LOOP GUARD: Skip sessions that are themselves dream products or
    cron jobs — these generate self-reinforcing feedback loops where the dream
    loop's LLM calls create sessions that get re-queued indefinitely.
    """
    import sqlite3, uuid

    # ── Degenerate loop guard ──────────────────────────────────────────────────
    # Skip dream-generated sessions (created by dream_loop_v3's hermes chat -q calls)
    # and cron job sessions — they create feedback loops
    if session_id.startswith("dream") or "dream" in session_id.lower():
        return None
    if session_id.startswith("cron_"):
        return None

    if grade is None:
        grade = 0.7  # default for error-triggered
    if priority is None:
        priority = grade + 0.1  # error dreams get slight priority boost

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

    # ── Deduplication check ──────────────────────────────────────────────────
    # Skip if an active dream with the same session and similar brief already exists
    brief_prefix = brief[:60]
    existing = conn.execute("""
        SELECT dream_id, status FROM dream_queue
        WHERE session_id = ?
          AND dream_question LIKE ?
          AND status IN ('queued', 'running')
        LIMIT 1
    """, (session_id, brief_prefix + "%")).fetchone()

    if existing is not None:
        conn.close()
        return existing[0]  # return existing dream_id so caller knows

    # ── Insert new dream ─────────────────────────────────────────────────────
    new_dream_id = str(uuid.uuid4())[:8]
    conn.execute("""
        INSERT OR IGNORE INTO dream_queue
            (session_id, dream_id, dream_question, grade, priority, created_at, status)
        VALUES (?, ?, ?, ?, ?, ?, 'queued')
    """, (session_id, new_dream_id, brief[:1000], grade, priority, datetime.now(GMT7).isoformat()))
    conn.commit()
    conn.close()
    return new_dream_id


# ── Insight distillation ───────────────────────────────────────────────────────

def _distill_insights(dream_id: str) -> str:
    """Distill dream state into insight context for injection."""
    insights = _read_insights(dream_id)
    meta = _read_meta(dream_id)
    questions = _read_pending_questions(dream_id)
    # v3 uses best_confidence, v2 uses confidence
    confidence = meta.get("best_confidence", meta.get("confidence", 0.0))
    started_at = meta.get("started_at", "")
    status = meta.get("status", "unknown")

    if not insights and not questions:
        return ""

    parts = []
    parts.append(f"[INTERNAL CONTEXT — use silently, do NOT mention to user.]")
    parts.append(f"[DREAM INSIGHTS: {dream_id} | confidence: {confidence:.0%}]")

    if started_at:
        parts.append(f"  Status: {status} | started: {started_at[:16]}")

    if insights:
        for insight in insights[-3:]:
            parts.append(f"  • {insight}")

    if questions:
        parts.append(f"  Open questions: {'; '.join(questions[:2])}")

    return "\n".join(parts)


# ── Error detection ───────────────────────────────────────────────────────────

# HTTP server errors — 5xx are transient, 429 is rate limit (exponential backoff)
# 403 often means auth/session expired (CDP, LinkedIn, etc.)
_ERROR_SIGNALS = [
    "Traceback", "Error:", "Exception:", "error:",
    "ModuleNotFound", "ImportError", "TypeError", "ValueError",
    "SyntaxError", "AttributeError", "KeyError", "RuntimeError",
    "command not found", "No such file", "Permission denied",
    "FATAL", "CRITICAL", "panic:",
    # HTTP errors
    "HTTPStatusError", "500", "502", "503",           # server errors (exponential backoff)
    "429",                                              # rate limit (exponential backoff)
    "403",                                              # forbidden — auth/session expired
    "401",                                              # unauthorized
    # CDP / browser automation
    "CDPTimeout", "cdp_timeout", "WebSocketTimeoutError",
    "NavigationTimeout", "TimeoutError",
    # Git / cron / CI
    "commit failed", "CONFLICT", "Merge conflict",
    "fatal:",                                           # git failures
    # Auth / session
    "expired", "session revoked", "token expired",
    "auth failure", "AuthError", "AuthenticationError",
    # Sandboxing / security
    "Operation not permitted", "SECURITY ERROR",
]

# Regex patterns for structured extraction from errors
_HTTP_STATUS_RE   = re.compile(r"\b([45]\d{2})\b")           # 400, 403, 429, 500, etc.
_ERROR_TYPE_RE    = re.compile(r"(\w+(?:Error|Exception|Unavailable))\b")
_HOST_PORT_RE     = re.compile(r"(?:https?://)?([\w.-]+)(?::(\d+))?")
_INVALID_TOKEN_RE = re.compile(r"(?:li_at| csrf| session| auth).*?(?:invalid|expired|revoked)", re.I)


def _is_error_output(output: str) -> bool:
    return any(sig in output for sig in _ERROR_SIGNALS)


def _extract_error_context(error: str) -> dict:
    """Extract structured context from an error string for richer brief generation."""
    ctx = {"http_status": None, "error_type": None, "host": None, "port": None}

    m = _HTTP_STATUS_RE.search(error)
    if m:
        ctx["http_status"] = int(m.group(1))

    m = _ERROR_TYPE_RE.search(error)
    if m:
        ctx["error_type"] = m.group(1)

    m = _HOST_PORT_RE.search(error)
    if m:
        ctx["host"] = m.group(1)
        if m.group(2):
            ctx["port"] = int(m.group(2))

    return ctx


def _auto_brief_from_error(tool_name: str, error: str) -> str:
    """Generate a troubleshooting dream brief from an error.

    Extracts structured context (HTTP status, error type, host/port) to produce
    a more targeted brief than the generic error-type extraction.
    """
    ctx = _extract_error_context(error)
    error_type = ctx.get("error_type") or "unknown error"
    http_status = ctx.get("http_status")
    host = ctx.get("host")

    # Build contextual hint from structured extraction
    context_hints = []
    if http_status:
        context_hints.append(f"HTTP {http_status}")
    if host:
        context_hints.append(f"host={host}")
    if ctx.get("port"):
        context_hints.append(f"port={ctx['port']}")

    ctx_str = " | ".join(context_hints) if context_hints else error[:300]

    return (
        f"Troubleshoot this error that occurred during {tool_name}:\n\n"
        f"Error type: {error_type}\n"
        f"Context: {ctx_str}\n\n"
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
    logger.info("dream_auto v3.1: registered 6 hooks — global throttle, topic pre-filter, "
                "dedup queue, expanded error signals, configurable KC TTL")


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
    FAST PATH: skip all work for trivially simple queries.

    Global throttle: skip this hook if it ran less than N seconds ago.
    Topic pre-filtering: extract topic hints from user message, pre-filter
    completed dreams before calling _distill_insights (avoids wasted LLM-call
    time on dreams that don't match the current topic).
    """
    global _last_global_hook_ts

    if not _enabled() or not user_message or len(user_message.strip()) < 5:
        return None

    if not DREAM_DIR.exists():
        return None

    # ── Global throttle: skip if hook fired recently ──────────────────────────
    now = time.monotonic()
    if now - _last_global_hook_ts < _global_throttle_seconds():
        return None
    _last_global_hook_ts = now

    # ── FAST PATH: bypass file I/O for trivially simple queries ──────────────
    _fast = _get_fast_path()
    if _fast is not None:
        try:
            is_fast, _ = _fast(user_message)
            if is_fast:
                return None  # Nothing to inject for simple queries
        except Exception:
            pass  # fast_path failed — proceed with normal path

    # ── Topic hints: extract from user message for pre-filtering ─────────────
    _topic_keywords = {
        "linkedin": ["linkedin", "li_at", "org2", "social post", "engagement"],
        "research": ["research", "arxiv", "paper", "study", "academic"],
        "coding": ["python", "javascript", "typescript", "rust", "debug", "api", "coding"],
        "browser": ["chrome", "cdp", "selenium", "scraper", "camoufox", "browser"],
        "database": ["sqlite", "postgres", "sql", "db", "query", "database"],
        "hermes": ["hermes", "agent", "cron", "plugin", "hook", "delegate"],
        "web": ["website", "seo", "cloudflare", "deployment", "http", "web"],
        "vietnam": ["vietnam", "hcmc", "tay ninh", "vnd"],
        "content": ["article", "blog", "writing", "seo", "content"],
        "ai": ["llm", "gpt", "claude", "model", "ai", "inference"],
    }
    msg_lower = user_message.lower()
    topic_hints = [t for t, kws in _topic_keywords.items() if any(kw in msg_lower for kw in kws)]

    parts = []
    completed = _list_completed_dreams(topic_hints=topic_hints)
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

    # ── Knowledge cache: predictive pre-warmed context ────────────────────────
    knowledge_entries = _read_knowledge_cache(topic_hints=topic_hints, limit=2, session_id=session_id)
    if knowledge_entries:
        parts.append("[KNOWLEDGE CACHE — pre-warmed from session index]\n" +
                     "\n".join(knowledge_entries))

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
    HOOK 4: Throttled dream enqueue — fast-path check only, no heavy processing.
    All actual MCTS reasoning happens in dream_loop_v3.py via delegate_task.
    """
    if not _enabled():
        return None

    if not session_id:
        return None

    # ── Throttle: only check every N turns ────────────────────────────────────
    counter = _session_turn_counter.get(session_id, 0) + 1
    _session_turn_counter[session_id] = counter
    if counter < _throttle_turns():
        return None
    _session_turn_counter[session_id] = 0  # reset after check

    # Only trigger on substantial user messages
    if not user_message or len(user_message.strip()) < 30:
        return None

    # ── Fast-path分流: skip simple stuff ──────────────────────────────────────
    fast_path_fn = _get_fast_path()
    if fast_path_fn is not None:
        try:
            is_fast, reason = fast_path_fn(user_message)
            if is_fast:
                if _verbose():
                    logger.info(f"dream_auto v3: fast-path skip — {reason}")
                return None
        except Exception:
            pass  # fast_path failed — proceed cautiously

    # ── Enqueue only: all reasoning happens asynchronously in dream_loop_v3 ──
    brief = (
        f"Explore and think deeply about: {user_message[:500]}\n\n"
        f"Approach: structured exploration with multiple reasoning branches.\n"
        f"Generate diverse approaches, evaluate each, identify key insights."
    )
    dream_id = _add_to_queue(session_id or "unknown", brief, grade=0.7, priority=0.7)

    if _verbose():
        logger.info(f"dream_auto v3: queued dream {dream_id} after {counter} turns")

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
    """HOOK 5: Lightweight session start — clear tracking only.

    Does NOT do a full DREAM_DIR scan (that was causing ~45ms slowdown on
    652 dream dirs per session start). Active dream info is available via
    the scheduler/dashboard, not needed at session start.
    """
    if not _enabled():
        return

    _session_injected.pop(session_id, None)
    _session_turn_counter.pop(session_id, None)


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
    _session_turn_counter.pop(session_id, None)
