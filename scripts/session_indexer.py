#!/usr/bin/env python3
"""
Session Indexer — Phase 1 of Dream System v3

Scans ~/.hermes/sessions/ for session transcripts and indexes them into session_index.db.
Extracts: message count, topics, error signals, complexity indicators, open questions.

Usage:
    python3 session_indexer.py              # index last 100 sessions
    python3 session_indexer.py --all         # index all sessions
    python3 session_indexer.py --limit 50    # index last 50
    python3 session_indexer.py --rescan      # re-index already-indexed sessions
"""

import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

# ── paths ────────────────────────────────────────────────────────────────────
SESSIONS_DIR = Path.home() / ".hermes" / "sessions"
DB_PATH = Path.home() / ".hermes" / "state" / "dream" / "session_index.db"
DREAM_DIR = Path.home() / ".hermes" / "state" / "dream"

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
    dreams_run       TEXT DEFAULT '[]',
    last_dreamed_at  TEXT
);
CREATE TABLE IF NOT EXISTS indexed_runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    indexed_at       TEXT,
    session_count    INTEGER,
    errors           INTEGER
);
"""

# ── helpers ───────────────────────────────────────────────────────────────────

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
        "linkedin":      ["linkedin", "li_at", "org2", "social post", "engagement"],
        "research":      ["research", "arXiv", "paper", "study", "academic"],
        "coding":        ["python", "javascript", "typescript", "rust", "debug", "api"],
        "browser":       ["chrome", "cdp", "selenium", "scraper", "camoufox"],
        "database":      ["sqlite", "postgres", "sql", "db", "query"],
        "hermes":        ["hermes", "agent", "cron", "plugin", "hook", "delegate"],
        "web":           ["website", "seo", "cloudflare", "deployment", "http"],
        "vietnam":       ["vietnam", "hcmc", "tay ninh", "vnd"],
        "content":       ["article", "blog", "writing", "seo", "content"],
        "ai":            ["llm", "gpt", "claude", "model", "ai", "inference"],
    }

def parse_session_file(path: Path) -> dict[str, Any]:
    """Parse a session .jsonl file and extract structured signals."""
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

    # first line is session_meta
    meta = messages[0] if messages[0].get("role") == "session_meta" else {}

    # user messages
    user_msgs = [m for m in messages if m.get("role") == "user"]
    # assistant responses
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    # tool results
    tool_msgs = [m for m in messages if m.get("role") == "tool"]

    # timestamps
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

    # detect topics
    all_text = " ".join(
        m.get("content", "")[:2000] for m in messages if isinstance(m.get("content"), str)
    ).lower()

    detected_topics = []
    for topic, keywords in topic_keywords().items():
        if any(kw in all_text for kw in keywords):
            detected_topics.append(topic)

    # detect errors in tool outputs
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

    # complexity: multiple tool calls, long exchanges
    tool_call_count = 0
    for m in assistant_msgs:
        tc = m.get("tool_calls", [])
        if isinstance(tc, list):
            tool_call_count += len(tc)

    was_complex = 1 if (tool_call_count >= 10 or len(messages) >= 30) else 0

    # extract open questions (interrogative sentences in user msgs)
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
        "unresolved":       json.dumps([]),
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


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Session Indexer for Dream System v3")
    parser.add_argument("--all", action="store_true", help="Index ALL sessions (default: last 100)")
    parser.add_argument("--limit", type=int, default=100, help="Max sessions to index (default 100)")
    parser.add_argument("--rescan", action="store_true", help="Re-index already-indexed sessions")
    args = parser.parse_args()

    ensure_db()
    already_indexed = get_indexed_ids() if not args.rescan else set()

    # collect session files
    session_files = sorted(
        SESSIONS_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )

    if not args.all:
        session_files = session_files[:args.limit]

    to_index = [f for f in session_files if f.stem not in already_indexed]
    print(f"Session indexer: {len(session_files)} recent, {len(to_index)} to index"
          f"{' (rescan mode)' if args.rescan else ''}")

    if not to_index:
        print("Nothing to index.")
        return

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
    print(f"\nDone: {total} sessions indexed, {errors} errors")
    print(f"DB: {DB_PATH}")


if __name__ == "__main__":
    main()
