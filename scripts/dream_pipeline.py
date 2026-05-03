#!/usr/bin/env python3
"""
Dream Pipeline — Merged Session Indexer + Grader

Indexes new sessions AND grades ungraded ones in a single run.
Replaces two separate cron jobs with one coordinated pipeline.

Usage:
    python3 dream_pipeline.py              # index + grade all ungraded
    python3 dream_pipeline.py --index-only    # index only, no grading
    python3 dream_pipeline.py --grade-only     # grade only, no indexing
    python3 dream_pipeline.py --all            # index ALL sessions + grade ungraded
    python3 dream_pipeline.py --grade-limit 50  # grade up to 50 sessions
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

# ── paths ────────────────────────────────────────────────────────────────────
SESSIONS_DIR = Path.home() / ".hermes" / "sessions"
DB_PATH = Path.home() / ".hermes" / "state" / "dream" / "session_index.db"
DREAM_DIR = Path.home() / ".hermes" / "state" / "dream"
HERMES_BIN = Path.home() / ".local" / "bin" / "hermes"
HERMES_AGENT_DIR = Path.home() / ".hermes" / "hermes-agent"

GMT7 = timezone(timedelta(hours=7))

# ── schema ───────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    created_at       TEXT,
    last_message_at  TEXT,
    message_count    INTEGER DEFAULT 0,
    topics           TEXT DEFAULT '[]',
    had_errors       INTEGER DEFAULT 0,
    error_count      INTEGER DEFAULT 0,
    was_complex      INTEGER DEFAULT 0,
    open_questions   TEXT DEFAULT '[]',
    unresolved       TEXT DEFAULT '[]',
    dream_potential  REAL,
    dream_potential_reason TEXT,
    systemic_value       REAL,
    deferred_depth       REAL,
    reasoning_novelty   REAL,
    actionability       REAL,
    error_quality       REAL,
    dreams_run       TEXT DEFAULT '[]',
    last_dreamed_at  TEXT
);
CREATE TABLE IF NOT EXISTS indexed_runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    indexed_at       TEXT,
    session_count    INTEGER,
    errors           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sessions_dream_potential ON sessions(dream_potential DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_had_errors ON sessions(had_errors);
CREATE INDEX IF NOT EXISTS idx_sessions_last_dreamed ON sessions(last_dreamed_at);
"""

# ── LLM grading prompt ────────────────────────────────────────────────────────
GRADER_PROMPT_V2 = """Analyze this Hermes session for dream potential.

Score each dimension 0.0–1.0, then compute weighted potential.

SESSION METRICS:
- session_id: {session_id}
- messages: {message_count} total
- topics: {topics}
- had_errors: {had_errors} (count: {error_count})
- unresolved: {unresolved}
- open_questions: {open_questions}
- recent_user_messages:
{user_msgs}

RUBRIC:

1. SYSTEMIC VALUE (30%% weight)
Does this expose a FRAGILITY PATTERN or ARCHITECTURAL GAP, not just an isolated bug?
  0.0 = Single issue, one-off
  0.5 = Spans 2+ components or recurring error type
  1.0 = Systemic failure affecting core infrastructure

2. DEFERRED DEPTH (25%% weight)
Was a significant DECISION or IMPLEMENTATION explicitly deferred?
  0.0 = Reached conclusion
  0.5 = Non-trivial decision deferred
  1.0 = Major architectural decision/rewrite deferred

3. REASONING NOVELTY (20%% weight)
Did the session make NON-OBVIOUS CONNECTIONS or reveal unexpected patterns?
  0.0 = Standard Q&A
  0.5 = Unexpected cross-topic connections made
  1.0 = Breakthrough insight about system behavior

4. ACTIONABILITY (15%% weight)
Would the open questions require SUBSTANTIAL IMPLEMENTATION to resolve?
  0.0 = Answerable in one tool call
  0.5 = Multi-step implementation needed
  1.0 = Would require architectural design + rewrite

5. ERROR QUALITY (10%% weight)
Do errors reveal SYSTEMIC FRAGILITY rather than transient issues?
  0.0 = No errors or trivial typos
  0.5 = Non-obvious errors requiring investigation
  1.0 = Errors indicating core system brokenness

THRESHOLD: score >= 0.70 → worth dreaming. score < 0.30 → skip.

Respond with ONLY JSON (no markdown, no explanation):
{{"systemic_value": 0.0-1.0, "deferred_depth": 0.0-1.0, "reasoning_novelty": 0.0-1.0, "actionability": 0.0-1.0, "error_quality": 0.0-1.0, "weighted_potential": 0.0-1.0, "reason": "...", "dream_questions": ["?", "?", "?"]}}
"""

# ══════════════════════════════════════════════════════════════════════════════
# INDEXER
# ══════════════════════════════════════════════════════════════════════════════

def error_signals() -> list[str]:
    return [
        "Traceback", "Error:", "Exception:", "error:",
        "ModuleNotFound", "ImportError", "TypeError", "ValueError",
        "SyntaxError", "AttributeError", "KeyError", "RuntimeError",
        "command not found", "No such file", "Permission denied",
        "FATAL", "CRITICAL", "panic:", "ConnectionError",
        "TimeoutError", "OperationalError",
    ]

def topic_keywords() -> dict[str, list[str]]:
    return {
        "linkedin":     ["linkedin", "li_at", "org2", "social post", "engagement"],
        "research":     ["research", "arXiv", "paper", "study", "academic"],
        "coding":       ["python", "javascript", "typescript", "rust", "debug", "api"],
        "browser":      ["chrome", "cdp", "selenium", "scraper", "camoufox"],
        "database":     ["sqlite", "postgres", "sql", "db", "query"],
        "hermes":       ["hermes", "agent", "cron", "plugin", "hook", "delegate"],
        "web":          ["website", "seo", "cloudflare", "deployment", "http"],
        "vietnam":      ["vietnam", "hcmc", "tay ninh", "vnd"],
        "content":      ["article", "blog", "writing", "seo", "content"],
        "ai":           ["llm", "gpt", "claude", "model", "ai", "inference"],
    }

def parse_session_file(path: Path) -> dict[str, Any]:
    try:
        lines = path.read_text().strip().split("\n")
    except Exception:
        return {}

    messages = []
    for line in lines:
        try:
            obj = json.loads(line)
            if obj.get("role") in ("user", "assistant", "tool"):
                messages.append(obj)
        except Exception:
            continue

    if not messages:
        return {}

    meta = messages[0] if messages[0].get("role") == "session_meta" else {}
    user_msgs = [m for m in messages if m.get("role") == "user"]
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    tool_msgs = [m for m in messages if m.get("role") == "tool"]

    created_at = None
    last_message_at = None
    for m in messages:
        ts = m.get("timestamp", "")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if created_at is None:
                created_at = dt
            last_message_at = dt
        except Exception:
            continue

    all_text = " ".join(
        m.get("content", "")[:2000] for m in messages if isinstance(m.get("content"), str)
    ).lower()

    detected_topics = []
    for topic, keywords in topic_keywords().items():
        if any(kw in all_text for kw in keywords):
            detected_topics.append(topic)

    had_errors = 0
    error_count = 0
    for m in tool_msgs:
        content = m.get("content", "")
        if isinstance(content, str):
            for sig in error_signals():
                if sig in content:
                    had_errors = 1
                    error_count += content.count(sig)
                    break

    tool_call_count = 0
    for m in assistant_msgs:
        tc = m.get("tool_calls", [])
        if isinstance(tc, list):
            tool_call_count += len(tc)

    was_complex = 1 if (tool_call_count >= 10 or len(messages) >= 30) else 0

    open_questions = []
    for m in user_msgs:
        content = m.get("content", "")
        if isinstance(content, str):
            questions = re.findall(r'[^.!?]*\?', content)
            for q in questions:
                q = q.strip()
                if len(q) > 15 and len(q) < 300:
                    open_questions.append(q[:200])

    return {
        "session_id":       path.stem,
        "created_at":       created_at.astimezone(GMT7).isoformat() if created_at else None,
        "last_message_at":  last_message_at.astimezone(GMT7).isoformat() if last_message_at else None,
        "message_count":    len(messages),
        "topics":           json.dumps(detected_topics[:10]),
        "had_errors":       had_errors,
        "error_count":      min(error_count, 99),
        "was_complex":      was_complex,
        "open_questions":  json.dumps(open_questions[:5]),
        "unresolved":      json.dumps([]),
    }

def ensure_db():
    DREAM_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(SCHEMA)
    conn.close()

def get_indexed_ids() -> set[str]:
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("SELECT session_id FROM sessions").fetchall()
    conn.close()
    return {r[0] for r in rows}

def upsert_session(data: dict):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO sessions (
            session_id, created_at, last_message_at, message_count,
            topics, had_errors, error_count, was_complex,
            open_questions, unresolved, dream_potential, dream_potential_reason,
            dreams_run, last_dreamed_at
        ) VALUES (
            :session_id, :created_at, :last_message_at, :message_count,
            :topics, :had_errors, :error_count, :was_complex,
            :open_questions, :unresolved, NULL, NULL,
            '[]', NULL
        )
        ON CONFLICT(session_id) DO UPDATE SET
            created_at      = excluded.created_at,
            last_message_at = excluded.last_message_at,
            message_count   = excluded.message_count,
            topics          = excluded.topics,
            had_errors      = excluded.had_errors,
            error_count     = excluded.error_count,
            was_complex     = excluded.was_complex,
            open_questions  = excluded.open_questions
    """, data)
    conn.commit()
    conn.close()

def write_top_topics(filepath: Path, limit: int = 5):
    if not filepath.parent.exists():
        filepath.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute("""
            SELECT topics FROM sessions
            WHERE topics IS NOT NULL AND topics != '[]'
            ORDER BY
                CASE WHEN dream_potential IS NOT NULL THEN dream_potential ELSE 0 END DESC,
                message_count DESC
            LIMIT ?
        """, (limit * 2,)).fetchall()
        conn.close()

        topic_counts: dict[str, int] = {}
        for (topics_json,) in rows:
            try:
                topics = json.loads(topics_json)
                for t in topics:
                    topic_counts[t] = topic_counts.get(t, 0) + 1
            except Exception:
                continue

        sorted_topics = sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)
        top = [t for t, _ in sorted_topics[:limit]]
        filepath.write_text(json.dumps({"topics": top, "written_at": datetime.now(GMT7).isoformat()}, indent=2))
        print(f"[TOPICS] Wrote: {top}")
    except Exception as e:
        print(f"[TOPICS] Error: {e}")

def run_indexer(all_sessions: bool = False, limit: int = 100, rescan: bool = False) -> int:
    """Run the indexer phase. Returns number of sessions indexed."""
    ensure_db()
    already_indexed = get_indexed_ids() if not rescan else set()

    session_files = sorted(
        SESSIONS_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )

    if not all_sessions:
        session_files = session_files[:limit]

    to_index = [f for f in session_files if f.stem not in already_indexed]
    print(f"[INDEXER] {len(session_files)} recent, {len(to_index)} to index"
          f"{' (rescan mode)' if rescan else ''}")

    if not to_index:
        print("[INDEXER] Nothing to index.")
        return 0

    errors = 0
    for i, path in enumerate(to_index):
        data = parse_session_file(path)
        if not data:
            errors += 1
            continue
        upsert_session(data)
        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{len(to_index)}] indexed...")

    # log run
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO indexed_runs (indexed_at, session_count, errors)
        VALUES (:now, :count, :errors)
    """, {
        "now": datetime.now(GMT7).isoformat(),
        "count": len(to_index) - errors,
        "errors": errors,
    })
    conn.commit()
    conn.close()

    total = len(to_index) - errors
    print(f"[INDEXER] Done: {total} sessions indexed, {errors} errors")
    return total

# ══════════════════════════════════════════════════════════════════════════════
# GRADER — uses subprocess.run (proven pattern, works in cron/background)
# ══════════════════════════════════════════════════════════════════════════════

def _call_hermes_chat(query: str, timeout: float = 90.0) -> str:
    """Call hermes chat -q via subprocess.run — works reliably in cron/background."""
    env = os.environ.copy()
    env.pop("HERMES_SESSION", None)
    env["HERMES_QUIET"] = "1"
    env["MEMORY_AUTO_ENABLED"] = "0"  # prevent 30-60s session search hang

    try:
        result = subprocess.run(
            [str(HERMES_BIN), "chat", "-q", query, "--max-turns", "1"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(Path.home()),
            env=env,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            print(f"  stderr: {result.stderr[:100]}", flush=True)
        return output
    except subprocess.TimeoutExpired:
        print(f"  timeout after {timeout}s", flush=True)
        return ""
    except FileNotFoundError:
        print(f"  hermes binary not found at {HERMES_BIN}")
        return ""
    except Exception as e:
        print(f"  hermes call failed: {e}")
        return ""


def _parse_json_response(raw: str) -> Optional[dict]:
    """Parse LLM JSON response from TUI-flooded stdout.

    The TUI renders box-drawing characters that fill the stdout buffer,
    pushing JSON off the end. We extract only alphanumeric/punctuation
    characters from the raw output, then find balanced {...} blocks.
    """
    if not raw:
        return None

    # Extract only printable ASCII + JSON punctuation — strips all TUI noise
    cleaned = re.sub(r'[^\x20-\x7E\n\r\t{}[\],:\".] ', ' ', raw)

    # Find all {...} blocks with balanced braces
    depth = 0
    start = None
    for i, c in enumerate(cleaned):
        if c == '{':
            if depth == 0:
                start = i
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0 and start is not None:
                candidate = cleaned[start:i+1]
                try:
                    parsed = json.loads(candidate)
                    # Validate it's a grader response
                    if "potential" in parsed or "dream_questions" in parsed:
                        return parsed
                except json.JSONDecodeError:
                    pass
                start = None

    return None

def get_session_summary(session_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    conn.close()

    if not row:
        return {}

    cols = [c[0] for c in sqlite3.connect(str(DB_PATH)).execute(
        "SELECT * FROM sessions LIMIT 0"
    ).description]
    data = dict(zip(cols, row))

    session_path = SESSIONS_DIR / f"{session_id}.jsonl"
    user_msgs = []
    if session_path.exists():
        try:
            for line in session_path.read_text().strip().split("\n"):
                obj = json.loads(line)
                if obj.get("role") == "user":
                    content = obj.get("content", "")
                    if isinstance(content, str):
                        user_msgs.append(content[:500])
        except Exception:
            pass

    return {
        "session_id":  data.get("session_id", ""),
        "message_count": data.get("message_count", 0),
        "topics":       ", ".join(json.loads(data.get("topics", "[]"))),
        "had_errors":   data.get("had_errors", 0),
        "error_count":  data.get("error_count", 0),
        "was_complex":  data.get("was_complex", 0),
        "open_questions": json.loads(data.get("open_questions", "[]")),
        "unresolved":   json.loads(data.get("unresolved", "[]")),
        "user_msgs":    user_msgs[-3:],
    }

def update_session_grade(session_id: str, potential: float, reason: str,
                        questions: list, dims: Optional[dict] = None):
    conn = sqlite3.connect(str(DB_PATH))
    # Migrate columns if they don't exist (for existing DBs)
    for col in ("systemic_value", "deferred_depth", "reasoning_novelty",
                "actionability", "error_quality"):
        try:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} REAL")
        except sqlite3.OperationalError:
            pass  # already exists
    set_parts = ["dream_potential = :potential",
                 "dream_potential_reason = :reason",
                 "unresolved = :questions"]
    params = {
        "session_id": session_id,
        "potential": potential,
        "reason": reason[:500],
        "questions": json.dumps(questions[:5]),
    }
    if dims:
        for k, v in dims.items():
            set_parts.append(f"{k} = :{k}")
            params[k] = v
    conn.execute(
        f"UPDATE sessions SET {', '.join(set_parts)} WHERE session_id = :session_id",
        params
    )
    conn.commit()
    conn.close()

def get_ungraded_ids(force: bool = False) -> list[str]:
    conn = sqlite3.connect(str(DB_PATH))
    if force:
        rows = conn.execute("SELECT session_id FROM sessions").fetchall()
    else:
        rows = conn.execute(
            "SELECT session_id FROM sessions WHERE dream_potential IS NULL"
        ).fetchall()
    conn.close()
    return [r[0] for r in rows]

def run_grader(limit: int = 50, force: bool = False) -> tuple[int, int]:
    """Run the grader phase. Returns (success, failures)."""
    if not DB_PATH.exists():
        print("[GRADER] DB not found. Run indexer first.")
        return 0, 0

    session_ids = get_ungraded_ids(force=force)
    if not session_ids:
        print("[GRADER] No sessions to grade.")
        return 0, 0

    session_ids = session_ids[:limit]
    print(f"[GRADER] {len(session_ids)} sessions to grade (force={force})")

    success = 0
    failures = 0
    for i, sid in enumerate(session_ids):
        summary = get_session_summary(sid)
        if not summary:
            failures += 1
            continue

        print(f"  [{i+1}/{len(session_ids)}] {sid} ({summary['message_count']} msgs, "
              f"{summary['topics'][:40]})...", end=" ", flush=True)

        raw = _call_hermes_chat(GRADER_PROMPT_V2.format(**summary), timeout=120.0)

        if not raw:
            print("FAILED (empty output)")
            failures += 1
            continue

        result = _parse_json_response(raw)
        if result:
            potential   = float(result.get("weighted_potential", result.get("potential", 0.5)))
            reason      = str(result.get("reason", ""))
            questions   = result.get("dream_questions", [])
            # new V2 dimensions (fallback to None if absent — old parses still work)
            dims = {
                k: float(v) for k, v in result.items()
                if k in ("systemic_value", "deferred_depth",
                         "reasoning_novelty", "actionability", "error_quality")
                and v is not None
            }
            update_session_grade(
                sid, potential, reason, questions, dims
            )
            sv  = dims.get("systemic_value", None)
            dd  = dims.get("deferred_depth", None)
            rn  = dims.get("reasoning_novelty", None)
            act = dims.get("actionability", None)
            eq  = dims.get("error_quality", None)
            dim_str = (f"sv={sv:.2f} dd={dd:.2f} rn={rn:.2f} "
                       f"act={act:.2f} eq={eq:.2f}" if dims else "")
            print(f"potential={potential:.2f} {dim_str}")
            success += 1
        else:
            print(f"FAILED (parse error: {raw[:80]})")
            failures += 1

        time.sleep(1)

    print(f"[GRADER] Done: {success} graded, {failures} failed")
    return success, failures

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Dream Pipeline — merged indexer + grader")
    parser.add_argument("--all", action="store_true",
                        help="Index ALL sessions (not just recent 100)")
    parser.add_argument("--limit", type=int, default=100,
                        help="Max sessions to index (default 100)")
    parser.add_argument("--rescan", action="store_true",
                        help="Re-index already-indexed sessions")
    parser.add_argument("--index-only", action="store_true",
                        help="Run indexer only, skip grading")
    parser.add_argument("--grade-only", action="store_true",
                        help="Run grader only, skip indexing")
    parser.add_argument("--grade-limit", type=int, default=50,
                        help="Max sessions to grade per run (default 50)")
    parser.add_argument("--grade-force", action="store_true",
                        help="Re-grade already-graded sessions")
    parser.add_argument("--write-topics", action="store_true",
                        help="Write top topics to topics_for_cache.json")
    parser.add_argument("--top-topics", type=int, default=5,
                        help="Number of top topics to write (default 5)")
    args = parser.parse_args()

    print(f"[PIPELINE] Starting at {datetime.now(GMT7).isoformat()}")
    print(f"[PIPELINE] Modes: index_only={args.index_only}, grade_only={args.grade_only}")

    total_indexed = 0
    total_graded = 0
    total_grade_failures = 0

    # Phase 1: Index
    if not args.grade_only:
        total_indexed = run_indexer(
            all_sessions=args.all,
            limit=args.limit,
            rescan=args.rescan,
        )
    else:
        ensure_db()  # ensure DB exists even in grade-only mode

    # Phase 2: Grade
    if not args.index_only:
        g_success, g_failures = run_grader(
            limit=args.grade_limit,
            force=args.grade_force,
        )
        total_graded = g_success
        total_grade_failures = g_failures

    # Write topics file
    if args.write_topics:
        TOPICS_FILE = DREAM_DIR / "topics_for_cache.json"
        write_top_topics(TOPICS_FILE, limit=args.top_topics)

    print(f"\n[PIPELINE] Done at {datetime.now(GMT7).isoformat()}")
    print(f"[PIPELINE] Summary: {total_indexed} indexed, {total_graded} graded, "
          f"{total_grade_failures} grade failures")


if __name__ == "__main__":
    main()
