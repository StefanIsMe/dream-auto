#!/usr/bin/env python3
"""
Session Grader — Phase 1 of Dream System v3

Grades each indexed session for dream potential using LLM reasoning.
One LLM call per session. Updates session_index.db.

Usage:
    python3 session_grader.py              # grade all ungraded sessions
    python3 session_grader.py --limit 20   # grade 20 sessions
    python3 session_grader.py --force      # re-grade already-graded sessions
"""

import json
import re
import sqlite3
import subprocess
import sys
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

# ── paths ────────────────────────────────────────────────────────────────────
DB_PATH = Path.home() / ".hermes" / "state" / "dream" / "session_index.db"
SESSIONS_DIR = Path.home() / ".hermes" / "sessions"
HERMES_BIN = Path.home() / ".local" / "bin" / "hermes"

GMT7 = timezone(timedelta(hours=7))

# ── LLM grading prompt ───────────────────────────────────────────────────────

GRADER_PROMPT = """Analyze this Hermes session for dream potential.

SESSION SUMMARY:
- Session ID: {session_id}
- Messages: {message_count} total
- Topics: {topics}
- Had errors: {had_errors} (count: {error_count})
- Was complex (10+ tool calls or 30+ messages): {was_complex}
- Open questions from user: {open_questions}
- Unresolved topics: {unresolved}
- Recent user messages (last 3):
{user_msgs}

A session has high dream potential if:
- It explored a complex topic without reaching a conclusion
- It hit errors that weren't fully resolved
- It involved a decision that was deferred
- It touched on something you'd want to think more about
- It generated open questions

Rate dream potential 0.0 - 1.0 and explain in 1 sentence why.
Also list 2-3 specific dream questions worth exploring.

Respond with ONLY JSON (no markdown, no explanation):
{{"potential": 0.0-1.0, "reason": "...", "dream_questions": ["?", "?", "?"]}}
"""


def get_session_summary(session_id: str) -> dict[str, Any]:
    """Get session summary for grading."""
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

    # Load recent user messages from the session file
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
        "session_id": data.get("session_id", ""),
        "message_count": data.get("message_count", 0),
        "topics": ", ".join(json.loads(data.get("topics", "[]"))),
        "had_errors": data.get("had_errors", 0),
        "error_count": data.get("error_count", 0),
        "was_complex": data.get("was_complex", 0),
        "open_questions": json.loads(data.get("open_questions", "[]")),
        "unresolved": json.loads(data.get("unresolved", "[]")),
        "user_msgs": user_msgs[-3:],
    }


def grade_with_hermes_chat(session_id: str, summary: dict) -> Optional[dict]:
    """Call hermes chat -q to grade a session. Uses subprocess with explicit PATH."""
    prompt = GRADER_PROMPT.format(**summary)

    # Build clean environment
    env = os.environ.copy()
    env.pop("HERMES_SESSION", None)
    env["HERMES_QUIET"] = "1"

    try:
        result = subprocess.run(
            [str(HERMES_BIN), "chat", "-q", prompt],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=str(Path.home()),
            env=env,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            print(f"  stderr: {result.stderr[:100]}")
    except subprocess.TimeoutExpired:
        print(f"  timeout after 90s — skipping")
        return None
    except FileNotFoundError:
        print(f"  hermes binary not found at {HERMES_BIN}")
        return None
    except Exception as e:
        print(f"  hermes call failed: {e}")
        return None

    # Extract JSON from output
    # Try to find the JSON object in the output
    for line in output.split("\n"):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                pass

    # Try to find JSON in the full output
    match = re.search(r"\{[\s\S]*\}", output)
    if match:
        try:
            # Try to extract just the JSON part
            text = match.group()
            # Remove any trailing text after closing brace
            for i in range(len(text), 0, -1):
                try:
                    return json.loads(text[:i])
                except Exception:
                    continue
        except Exception:
            pass

    print(f"  Could not parse output (first 200 chars): {output[:200]}")
    return None


def update_session_grade(session_id: str, potential: float, reason: str, questions: list):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        UPDATE sessions SET
            dream_potential = :potential,
            dream_potential_reason = :reason,
            unresolved = :questions
        WHERE session_id = :session_id
    """, {
        "session_id": session_id,
        "potential": potential,
        "reason": reason[:500],
        "questions": json.dumps(questions[:5]),
    })
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


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Session Grader for Dream System v3")
    parser.add_argument("--limit", type=int, default=50, help="Max sessions to grade")
    parser.add_argument("--force", action="store_true", help="Re-grade already-graded sessions")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}. Run session_indexer.py first.")
        sys.exit(1)

    session_ids = get_ungraded_ids(force=args.force)
    if not session_ids:
        print("No sessions to grade.")
        return

    session_ids = session_ids[:args.limit]
    print(f"Session grader: {len(session_ids)} sessions to grade")

    success = 0
    failures = 0
    for i, sid in enumerate(session_ids):
        summary = get_session_summary(sid)
        if not summary:
            failures += 1
            continue

        print(f"  [{i+1}/{len(session_ids)}] {sid} ({summary['message_count']} msgs, {summary['topics'][:40]})...", end=" ", flush=True)
        result = grade_with_hermes_chat(sid, summary)

        if result:
            potential = float(result.get("potential", 0.5))
            reason = str(result.get("reason", ""))
            questions = result.get("dream_questions", [])
            update_session_grade(sid, potential, reason, questions)
            print(f"potential={potential:.2f}")
            success += 1
        else:
            print("FAILED")
            failures += 1

        # Small delay between calls
        time.sleep(1)

    print(f"\nDone: {success} graded, {failures} failed")


if __name__ == "__main__":
    main()
