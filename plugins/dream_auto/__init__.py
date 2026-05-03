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

# ── BM25 (optional dependency, graceful fallback) ────────────────────────────
# rank-bm25 is pure Python, no native deps — works on macOS/Linux/Windows WSL.
# Fallback: simple word-overlap scorer if not installed.
_bm25_available: bool = False
_bm25_impl = None

def _get_bm25():
    """
    Lazily import rank-bm25. Returns (scorer_func, is_bm25) tuple.

    Cached via _bm25_impl — call _reset_bm25() to force re-import.
    """
    global _bm25_available, _bm25_impl
    if _bm25_impl is not None:
        return _bm25_impl

    _bm25_available = False
    _bm25_impl = (None, False)
    try:
        from rank_bm25 import BM25Okapi
        _bm25_available = True
        _bm25_impl = (BM25Okapi, True)
        logger.debug("dream_auto: rank-bm25 available — using BM25 scoring")
    except ImportError:
        logger.debug("dream_auto: rank-bm25 not installed — using word-overlap fallback")

    return _bm25_impl

# ── Config ────────────────────────────────────────────────────────────────────
ENABLED_ENV          = "DREAM_AUTO_ENABLED"
VERBOSE_ENV          = "DREAM_AUTO_VERBOSE"
MAX_INJECT_ENV       = "DREAM_AUTO_MAX_INJECT"
THROTTLE_ENV         = "DREAM_AUTO_THROTTLE_TURNS"
GLOBAL_THROTTLE_ENV  = "DREAM_AUTO_GLOBAL_THROTTLE"

GMT7 = timezone(timedelta(hours=7))
DREAM_DIR            = Path.home() / ".hermes" / "state" / "dream"
DREAM_QUEUE_DB       = Path.home() / ".hermes" / "state" / "dream" / "dream_queue.db"

_session_injected:       Dict[str, Set[str]] = {}  # session_id → set of dream_ids
_session_turn_counter:   Dict[str, int]    = {}  # session_id → turn count since last dream check
_last_global_hook_ts:    float             = -300.0  # sentinel: -300 so first call always passes

# ── BM25 index cache (mtime-invalidated) ─────────────────────────────────────
_bm25_index = None            # built index (BM25 instance or fallback tokenized list)
_bm25_dreams = []            # list of dream dicts matching the index (same order)
_bm25_dir_mtime: float = -1.0  # DREAM_DIR mtime when index was built
_bm25_is_real: bool = False    # True only when _bm25_index is a real BM25 instance

def _tokenize(text: str) -> List[str]:
    """Simple whitespace tokenizer used by both BM25 and fallback."""
    return text.lower().split()

def _build_bm25_index(dreams: List[dict]):
    """Build (or rebuild) the BM25 index from a list of dream dicts."""
    global _bm25_index, _bm25_dreams, _bm25_dir_mtime

    if not dreams:
        _bm25_index = None
        _bm25_dreams = []
        _bm25_dir_mtime = -1.0
        return

    _bm25_dreams = dreams
    tokenized = [_tokenize(d["brief"]) for d in dreams]

    bm25_cls, is_bm25 = _get_bm25()
    if is_bm25 and bm25_cls is not None:
        try:
            _bm25_index = bm25_cls(tokenized)
            _bm25_is_real = True
        except Exception as e:
            # Guard against: ZeroDivisionError (singleton corpus with unique terms),
            # import errors, or any other rank-bm25 edge case.
            # Fall back to tokenized corpus for word-overlap scoring.
            logger.debug(f"dream_auto: BM25 index build failed ({e}) — using word-overlap fallback")
            _bm25_index = tokenized
            _bm25_is_real = False
    else:
        # rank-bm25 not available — word-overlap fallback
        _bm25_index = tokenized
        _bm25_is_real = False

    # Record mtime after building so any new dream created during build
    # correctly triggers a rebuild on next call
    try:
        _bm25_dir_mtime = DREAM_DIR.stat().st_mtime if DREAM_DIR.exists() else -1.0
    except OSError:
        _bm25_dir_mtime = -1.0

def _score_dreams_bm25(user_message: str, max_inject: int) -> List[dict]:
    """
    Score completed dreams against user_message using BM25 (or word-overlap fallback).
    Returns up to `max_inject` dreams sorted by relevance score, with
    session dedup handled separately by the caller.
    """
    global _bm25_index, _bm25_dreams, _bm25_dir_mtime

    if not _bm25_dreams:
        return []

    user_tokens = _tokenize(user_message)
    if not user_tokens:
        return []

    if _bm25_is_real and _bm25_index is not None:
        # BM25 path: score all docs, take top-k
        scores = _bm25_index.get_scores(user_tokens)
        for d, s in zip(_bm25_dreams, scores):
            d["_score"] = s
    else:
        # Fallback word-overlap: TF-based Jaccard-ish score
        user_set = set(user_tokens)
        user_len = len(user_set)
        for d in _bm25_dreams:
            doc_tokens = set(_tokenize(d["brief"]))
            overlap = len(user_set & doc_tokens)
            d["_score"] = overlap / user_len if user_len else 0

    # Filter to minimum threshold and sort
    scored = [d for d in _bm25_dreams if d.get("_score", 0) > 0]
    scored.sort(key=lambda x: x.get("_score", 0), reverse=True)
    return scored[:max_inject]

def _refresh_bm25_index_if_needed():
    """Invalidate and rebuild BM25 index if DREAM_DIR has changed."""
    global _bm25_dir_mtime
    try:
        current_mtime = DREAM_DIR.stat().st_mtime if DREAM_DIR.exists() else -1.0
    except OSError:
        current_mtime = -1.0

    if current_mtime != _bm25_dir_mtime:
        # Re-scan completed dreams and rebuild index
        all_dreams = _list_completed_dreams_raw()  # no topic filter, just done+has_content
        _build_bm25_index(all_dreams)

def _list_completed_dreams_raw() -> List[dict]:
    """List all completed dreams that have non-empty insights or questions.
    No topic filtering — used to build the BM25 corpus.
    """
    if not DREAM_DIR.exists():
        return []

    dreams = []
    for dream_path in sorted(DREAM_DIR.iterdir(), reverse=True):
        if not dream_path.is_dir():
            continue
        meta = _read_json(dream_path / "meta.json", {})
        if meta.get("status") not in _STATUS_DONE:
            continue
        if not _has_insights_or_questions(dream_path.name):
            continue

        confidence = meta.get("best_confidence", meta.get("confidence", 0.0))
        ended_at = meta.get("ended_at", meta.get("started_at", ""))
        dreams.append({
            "id": dream_path.name,
            "brief": meta.get("brief", "")[:200],
            "confidence": confidence,
            "topics": meta.get("topics", []),
            "_ended_at": ended_at,
        })

    dreams.sort(key=lambda d: (d["confidence"], d.get("_ended_at", "")), reverse=True)
    return dreams

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

# ── Done-status normalization ─────────────────────────────────────────────────

# v3 meta.json uses non-standard status values. Normalize everything here.
_STATUS_DONE = {
    # Normal completion statuses
    "done", "completed", "completed_success",
    # Killed/stale variants
    "completed_killed", "killed_wallclock", "completed_stale", "stale_completed", "completed_empty",
    # Failure variants — these may have useful partial insights
    "failed", "failed_crash", "failed_restart", "health_check_failed", "circuit_breaker",
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


def _has_insights_or_questions(dream_id: str) -> bool:
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
        ended_at = meta.get("ended_at", meta.get("started_at", ""))
        dreams.append({
            "id": dream_path.name,
            "brief": meta.get("brief", "")[:200],
            "confidence": confidence,
            "topics": meta.get("topics", []),
            "_ended_at": ended_at,
        })

    # Sort by confidence desc, newest first for equal confidence
    dreams.sort(key=lambda d: (d["confidence"], d.get("_ended_at", "")), reverse=True)
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
    logger.info("dream_auto v3.4: registered 6 hooks — BM25 scoring, mtime index cache, global throttle")


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
    BM25 scoring: completed dreams are ranked by BM25 against the user message
    (with graceful word-overlap fallback if rank-bm25 is not installed).
    Knowledge cache entries are still topic-hinted for pre-filtering.
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

    # ── BM25: refresh index if needed, then score dreams ───────────────────────
    _refresh_bm25_index_if_needed()
    scored_dreams = _score_dreams_bm25(user_message, _max_inject())

    # ── Session deduplication ───────────────────────────────────────────────────
    injected = _session_injected.get(session_id, set())

    parts = []
    for dream in scored_dreams:
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
        logger.info(f"dream_auto v3.4 pre_llm_call: injected {len(combined)} chars from {len(parts)} dreams")

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
