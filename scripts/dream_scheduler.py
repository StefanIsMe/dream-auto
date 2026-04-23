#!/usr/bin/env python3
"""
dream_scheduler.py — Dream Queue + Scheduler for Dream System v3

Runs every 30 minutes via cron. Checks resources → picks top session → starts dream.

Usage:
    python3 dream_scheduler.py              # one-shot run
    python3 dream_scheduler.py --daemon      # run continuously
    python3 dream_scheduler.py --dry-run    # show what would happen
"""

import json
import os
import sqlite3
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── paths ────────────────────────────────────────────────────────────────────
DREAM_DIR = Path.home() / ".hermes" / "state" / "dream"
DB_PATH = Path.home() / ".hermes" / "state" / "dream" / "session_index.db"
DREAM_QUEUE_DB = Path.home() / ".hermes" / "state" / "dream" / "dream_queue.db"
SESSIONS_DIR = Path.home() / ".hermes" / "sessions"
HERMES_BIN = shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")
DREAM_LOOP_V3 = Path.home() / ".hermes" / "skills" / "autonomous-ai-agents" / "hermes-dream-task" / "scripts" / "dream_loop_v3.py"

GMT7 = timezone(timedelta(hours=7))

# Add scripts dir to path for resource_monitor import
sys.path.insert(0, str(Path.home() / ".hermes" / "plugins" / "dream_auto"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS dream_queue (
    queue_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT,
    dream_id       TEXT UNIQUE,
    dream_question TEXT,
    grade          REAL,
    resource_cost  INTEGER DEFAULT 1,
    priority       REAL,
    created_at     TEXT,
    started_at     TEXT,
    completed_at   TEXT,
    status         TEXT DEFAULT 'queued'
);
"""

# ── Queue management ───────────────────────────────────────────────────────────

def ensure_queue_db():
    DREAM_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DREAM_QUEUE_DB))
    conn.executescript(SCHEMA)
    conn.close()


def get_top_queued(limit: int = 1) -> list[dict]:
    """Get highest-priority queued dreams."""
    conn = sqlite3.connect(str(DREAM_QUEUE_DB))
    rows = conn.execute("""
        SELECT queue_id, session_id, dream_id, dream_question, grade, priority, status
        FROM dream_queue
        WHERE status = 'queued'
        ORDER BY priority DESC, grade DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [
        {"queue_id": r[0], "session_id": r[1], "dream_id": r[2],
         "question": r[3], "grade": r[4], "priority": r[5], "status": r[6]}
        for r in rows
    ]


def mark_started(dream_id: str):
    conn = sqlite3.connect(str(DREAM_QUEUE_DB))
    conn.execute("""
        UPDATE dream_queue SET status='running', started_at=? WHERE dream_id=?
    """, (datetime.now(GMT7).isoformat(), dream_id))
    conn.commit()
    conn.close()


def mark_completed(dream_id: str):
    conn = sqlite3.connect(str(DREAM_QUEUE_DB))
    conn.execute("""
        UPDATE dream_queue SET status='completed', completed_at=? WHERE dream_id=?
    """, (datetime.now(GMT7).isoformat(), dream_id))
    conn.commit()
    conn.close()


def add_to_queue(session_id: str, question: str, grade: float, priority: float = None):
    """Add a dream to the queue."""
    if priority is None:
        priority = grade  # default: use grade as priority
    dream_id = str(uuid.uuid4())[:8]
    conn = sqlite3.connect(str(DREAM_QUEUE_DB))
    conn.execute("""
        INSERT OR IGNORE INTO dream_queue
            (session_id, dream_id, dream_question, grade, priority, created_at, status)
        VALUES (?, ?, ?, ?, ?, ?, 'queued')
    """, (session_id, dream_id, question[:1000], grade, priority, datetime.now(GMT7).isoformat()))
    conn.commit()
    conn.close()
    return dream_id


def get_session_with_highest_potential(limit: int = 10) -> list[dict]:
    """Get sessions that haven't been dreamed on yet, sorted by dream potential."""
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("""
        SELECT s.session_id, s.dream_potential, s.dream_potential_reason,
               s.open_questions, s.topics, s.unresolved,
               sq.dream_id IS NULL as undreamed
        FROM sessions s
        LEFT JOIN (
            SELECT DISTINCT session_id, dream_id FROM dream_queue
            UNION
            SELECT session_id, session_id as dream_id FROM sessions
            WHERE last_dreamed_at IS NOT NULL
        ) sq ON sq.session_id = s.session_id
        WHERE s.dream_potential IS NOT NULL
        AND sq.dream_id IS NULL
        ORDER BY s.dream_potential DESC, s.message_count DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [
        {"session_id": r[0], "potential": r[1], "reason": r[2],
         "open_questions": json.loads(r[3]) if r[3] else [],
         "topics": json.loads(r[4]) if r[4] else [],
         "unresolved": json.loads(r[5]) if r[5] else []}
        for r in rows
    ]


def count_running_dreams() -> int:
    """Count currently running dreams."""
    if not DREAM_DIR.exists():
        return 0
    count = 0
    for d in DREAM_DIR.iterdir():
        if not d.is_dir():
            continue
        status_file = d / "status.txt"
        meta_file = d / "meta.json"
        if status_file.exists() and status_file.read_text().strip() == "running":
            count += 1
            continue
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
                if meta.get("status") == "running":
                    count += 1
            except Exception:
                pass
    return count


def build_dream_brief(session_data: dict) -> str:
    """Build a dream brief from session data."""
    sq = session_data.get("open_questions", [])
    top_questions = sq[:3] if sq else []

    topics = session_data.get("topics", [])
    topic_str = ", ".join(topics[:5]) if topics else "general"

    brief = f"""Explore and think deeply about session: {session_data['session_id']}

Topics: {topic_str}
Dream potential: {session_data.get('potential', 0):.2f}
Reason: {session_data.get('reason', 'not evaluated')[:200]}

"""
    if top_questions:
        brief += "Open questions to investigate:\n"
        for q in top_questions:
            brief += f"  - {q}\n"

    unresolved = session_data.get("unresolved", [])
    if unresolved:
        brief += "Unresolved items:\n"
        for u in unresolved[:3]:
            brief += f"  - {u}\n"

    return brief


# ── Resource check ───────────────────────────────────────────────────────────

def check_resources():
    """Check if we have resources to start a dream."""
    try:
        from resource_monitor import ResourceMonitor
        rm = ResourceMonitor()
        available, reason = rm.can_start_dream()
        return available, reason
    except Exception as e:
        print(f"  [RESOURCE] Fallback check: {e}")
        # Simple fallback: just check if any dreams are running
        running = count_running_dreams()
        return running == 0, f"{running} dreams running"


# ── Start a dream via delegate ───────────────────────────────────────────────

def start_dream_via_delegate(dream_id: str, brief: str, session_id: str):
    """Start a dream by writing the delegate goal and spawning a subagent."""
    dp = DREAM_DIR / dream_id
    dp.mkdir(parents=True, exist_ok=True)

    # Write meta
    meta = {
        "dream_id": dream_id,
        "brief": brief[:500],
        "session_id": session_id,
        "status": "running",
        "started_at": time.time(),
        "started_at_human": datetime.now(GMT7).isoformat(),
        "iteration": 0,
        "confidence": 0.0,
    }
    write_json(dp / "meta.json", meta)
    (dp / "status.txt").write_text("running")

    # Initialize empty files
    write_json(dp / "exploration_tree.json", {"nodes": [], "current_root": "root"})
    write_json(dp / "insights.json", [])
    write_json(dp / "failures.json", [])
    write_json(dp / "pending_questions.json", [])
    write_json(dp / "monte_carlo_runs.json", [])
    write_json(dp / "uncertainty.json", {})

    # Mark queue as started
    mark_started(dream_id)

    # Spawn the dream_loop_v3 as a background process
    # The subagent will run it and update meta.json each iteration
    env = os.environ.copy()
    env.pop("HERMES_SESSION", None)
    env["DREAM_LOOP_ACTIVE"] = "1"

    try:
        subprocess.Popen(
            [
                sys.executable, str(DREAM_LOOP_V3),
                dream_id, brief[:800]
            ],
            env=env,
            cwd=str(Path.home()),
            stdout=open(dp / "dream_output.log", "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        print(f"  [SPAWN] Started dream {dream_id} via subprocess")
        return True
    except Exception as e:
        print(f"  [SPAWN ERROR] {e}")
        return False


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


# ── Main scheduler ──────────────────────────────────────────────────────────

def run_scheduler_cycle(dry_run: bool = False) -> dict:
    """Run one scheduler cycle. Returns summary."""
    ensure_queue_db()
    results = {"dreams_started": 0, "skipped": [], "errors": []}

    # 1. Check resources
    available, reason = check_resources()
    print(f"[SCHEDULER] Resource check: {'available' if available else 'NOT available'} — {reason}")

    if not available:
        results["skipped"].append(f"resources: {reason}")
        return results

    # 2. Check if we're already running max dreams
    running = count_running_dreams()
    if running >= 1:
        print(f"[SCHEDULER] Already {running} dreams running — skipping")
        results["skipped"].append(f"already {running} running")
        return results

    # 3. Check queue for pending dreams
    queued = get_top_queued(limit=1)
    if queued:
        # Start queued dream
        item = queued[0]
        print(f"[SCHEDULER] Starting queued dream: {item['dream_id']}")
        if not dry_run:
            brief = item.get("question", "Explore this topic deeply")
            ok = start_dream_via_delegate(item["dream_id"], brief, item.get("session_id", ""))
            if ok:
                results["dreams_started"] = 1
            else:
                results["errors"].append(f"spawn failed: {item['dream_id']}")
        else:
            results["dreams_started"] = 1
        return results

    # 4. If queue empty, pick best un-dreamed session and add to queue
    print("[SCHEDULER] Queue empty — checking for new sessions...")
    sessions = get_session_with_highest_potential(limit=3)

    if not sessions:
        print("[SCHEDULER] No sessions with dream potential found.")
        results["skipped"].append("no sessions")
        return results

    best = sessions[0]
    potential = best.get("potential", 0)

    # Only start dreams for sessions with reasonable potential
    if potential < 0.4:
        print(f"[SCHEDULER] Best potential {potential:.2f} < 0.4 — skipping")
        results["skipped"].append(f"low potential: {potential:.2f}")
        return results

    brief = build_dream_brief(best)
    print(f"[SCHEDULER] New dream: session={best['session_id']} potential={potential:.2f}")
    print(f"  Brief: {brief[:200]}...")

    if not dry_run:
        # Add to queue then start
        dream_id = add_to_queue(best["session_id"], brief, potential)
        ok = start_dream_via_delegate(dream_id, brief, best["session_id"])
        if ok:
            results["dreams_started"] = 1
        else:
            results["errors"].append("spawn failed")
    else:
        results["dreams_started"] = 0
        results["dry_run"] = True
        results["would_start"] = {
            "session": best["session_id"],
            "potential": potential,
            "brief_preview": brief[:200],
        }

    return results


def run_daemon(interval_minutes: int = 30):
    """Run scheduler as a daemon."""
    print(f"[DAEMON] Dream scheduler running every {interval_minutes} minutes")
    while True:
        results = run_scheduler_cycle()
        print(f"[DAEMON] Cycle done: {results}")
        time.sleep(interval_minutes * 60)


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Dream Scheduler for Dream System v3")
    parser.add_argument("--daemon", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=30, help="Minutes between cycles (daemon mode)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen")
    args = parser.parse_args()

    if args.daemon:
        run_daemon(interval_minutes=args.interval)
    else:
        result = run_scheduler_cycle(dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
