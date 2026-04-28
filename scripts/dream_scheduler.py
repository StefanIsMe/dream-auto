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

CREATE INDEX IF NOT EXISTS idx_queue_status_priority ON dream_queue(status, priority DESC);
CREATE INDEX IF NOT EXISTS idx_queue_dream_id ON dream_queue(dream_id);
"""

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_DREAM_WALLCLOCK_MINUTES = 30   # hard cap — any dream running longer is killed
DREAM_WALLCLOCK_SECONDS = MAX_DREAM_WALLCLOCK_MINUTES * 60


# ── Stale dream killer ─────────────────────────────────────────────────────────

def kill_stale_dreams() -> list[dict]:
    """
    Ralph-loop wallclock enforcer: kill any dream running longer than MAX_DREAM_WALLCLOCK_MINUTES.
    Called at the start of each scheduler cycle. Returns list of killed dream info.
    """
    if not DREAM_DIR.exists():
        return []

    killed = []
    now = time.time()

    for d in DREAM_DIR.iterdir():
        if not d.is_dir():
            continue
        meta_file = d / "meta.json"
        status_file = d / "status.txt"

        if not meta_file.exists():
            continue

        try:
            meta = json.loads(meta_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        # Only kill if status is running
        status = meta.get("status", "")
        if status != "running":
            continue

        started_at = meta.get("started_at")
        if started_at is None:
            continue

        elapsed = now - started_at
        if elapsed > DREAM_WALLCLOCK_SECONDS:
            dream_id = meta.get("dream_id", d.name)
            wallclock_min = elapsed / 60
            print(f"  [WALLCLOCK KILL] {dream_id} ran for {wallclock_min:.0f}min > {MAX_DREAM_WALLCLOCK_MINUTES}min — killing")

            # Mark as killed in status
            (status_file).write_text("killed_wallclock")
            meta["status"] = "killed_wallclock"
            meta["killed_at"] = now
            meta["wallclock_minutes"] = round(wallclock_min, 1)
            try:
                write_json(meta_file, meta)
            except Exception:
                pass

            # Also mark queue entry complete so it doesn't retry
            mark_completed(dream_id, killed=True)

            killed.append({
                "dream_id": dream_id,
                "wallclock_min": round(wallclock_min, 1),
                "reason": f"exceeded {MAX_DREAM_WALLCLOCK_MINUTES}min wallclock"
            })

    return killed


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


def mark_completed(dream_id: str, killed: bool = False):
    status = "killed_wallclock" if killed else "completed"
    conn = sqlite3.connect(str(DREAM_QUEUE_DB))
    conn.execute("""
        UPDATE dream_queue SET status=?, completed_at=? WHERE dream_id=?
    """, (status, datetime.now(GMT7).isoformat(), dream_id))
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
    # Attach dream_queue.db so we can LEFT JOIN across both databases
    conn.execute(f"ATTACH DATABASE '{DREAM_QUEUE_DB}' AS dream_queue_db")
    rows = conn.execute("""
        SELECT s.session_id, s.dream_potential, s.dream_potential_reason,
               s.open_questions, s.topics, s.unresolved,
               sq.dream_id IS NULL as undreamed
        FROM sessions s
        LEFT JOIN (
            SELECT DISTINCT session_id, dream_id FROM dream_queue_db.dream_queue
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


# ── Dynamic concurrency ───────────────────────────────────────────────────────

def decide_concurrency(state: dict) -> int:
    """
    Rule-based concurrency decision — no LLM call needed.
    CPU/RAM thresholds determine how many additional dreams can run.
    """
    cpu = state["cpu_percent"]
    ram = state["ram_percent"]
    active = state.get("active_sessions", 0)
    running = state["active_dreams"]

    # Critical: no new dreams
    if cpu >= 90 or ram >= 95:
        return 0
    # High load: allow 1 more at most
    if cpu >= 75 or ram >= 85:
        return max(0, min(1, 5 - running))
    # Moderate load: allow 2 more
    if cpu >= 50 or ram >= 70:
        return max(0, min(2, 5 - running))
    # Low load: allow up to 3 more (but not more than available slots)
    return max(0, min(3, 5 - running))


def llm_decide_concurrency(state: dict) -> int:
    """
    Deprecated: hermes chat -q hangs in non-TTY subprocess context.
    Use decide_concurrency() instead — same logic, no LLM call.
    """
    return decide_concurrency(state)


def check_resources_and_concurrency():
    """
    Check resources and decide how many dreams can start this cycle.
    Returns (available: bool, reason: str, max_additional: int)
    """
    try:
        from resource_monitor import ResourceMonitor
        rm = ResourceMonitor()
        state = rm.get_state()
        cpu = state["cpu_percent"]
        ram = state["ram_percent"]

        # Hard stop: clearly overloaded
        if cpu >= 90 or ram >= 95:
            return False, f"CPU={cpu:.0f}% or RAM={ram:.0f}% critical", 0

        # Hard stop: too many already running (safety cap)
        running = count_running_dreams()
        if running >= 5:
            return False, f"{running} dreams already running (safety cap)", 0

        # Dynamic: rule-based concurrency decision
        state["active_dreams"] = running
        max_additional = decide_concurrency(state)

        if max_additional <= 0:
            return False, f"no concurrency slots available", 0

        return True, f"Resources OK, {max_additional} slot(s) available", max_additional

    except Exception as e:
        print(f"  [RESOURCE] Fallback check: {e}")
        running = count_running_dreams()
        return running == 0, f"{running} dreams running (fallback)", 1 if running == 0 else 0


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
        # Line-buffered stdout + unbuffered python so dream_output.log shows live progress
        log_fp = open(dp / "dream_output.log", "w", buffering=1)
        env["PYTHONUNBUFFERED"] = "1"
        subprocess.Popen(
            [
                sys.executable, "-u", str(DREAM_LOOP_V3),
                dream_id, brief[:800]
            ],
            env=env,
            cwd=str(Path.home()),
            stdout=log_fp,
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
    results = {"dreams_started": 0, "skipped": [], "errors": [], "wallclock_killed": []}

    # 0. Ralph-loop wallclock enforcer: kill dreams that have been running too long
    killed = kill_stale_dreams()
    if killed:
        results["wallclock_killed"] = killed
        print(f"[SCHEDULER] Killed {len(killed)} stale dreams")

    # 1. Check resources + get dynamic concurrency limit
    available, reason, max_additional = check_resources_and_concurrency()
    print(f"[SCHEDULER] Resource check: {'available' if available else 'NOT available'} — {reason}")

    if not available or max_additional <= 0:
        results["skipped"].append(f"resources: {reason}")
        return results

    # 2. Pull top N dreams from queue (up to max_additional)
    queued = get_top_queued(limit=max_additional)
    started = 0

    for item in queued:
        print(f"[SCHEDULER] Starting queued dream: {item['dream_id']}")
        if not dry_run:
            brief = item.get("question", "Explore this topic deeply")
            ok = start_dream_via_delegate(item["dream_id"], brief, item.get("session_id", ""))
            if ok:
                started += 1
            else:
                results["errors"].append(f"spawn failed: {item['dream_id']}")
        else:
            started += 1

    # 3. If queue had fewer than max_additional, fill remaining slots from new sessions
    remaining = max_additional - started
    if remaining > 0:
        print(f"[SCHEDULER] Queue gave {started}, need {remaining} more — checking sessions...")
        sessions = get_session_with_highest_potential(limit=remaining)

        for sess in sessions:
            potential = sess.get("potential", 0)
            if potential < 0.4:
                print(f"[SCHEDULER] Potential {potential:.2f} < 0.4 — skipping remaining")
                results["skipped"].append(f"low potential: {potential:.2f}")
                break

            brief = build_dream_brief(sess)
            print(f"[SCHEDULER] New dream: session={sess['session_id']} potential={potential:.2f}")

            if not dry_run:
                dream_id = add_to_queue(sess["session_id"], brief, potential)
                ok = start_dream_via_delegate(dream_id, brief, sess["session_id"])
                if ok:
                    started += 1
                else:
                    results["errors"].append("spawn failed")
            else:
                started += 1

    results["dreams_started"] = started
    return results


def decide_sleep_seconds(base_minutes: int, cpu: float, ram: float, running: int, queued_count: int) -> int:
    """
    Rule-based adaptive sleep — no LLM call needed.
    Returns seconds to sleep. Shorter when queue backs up + resources free.
    """
    slots_free = max(0, 5 - running)

    # Queue backed up + resources free → fast turnaround
    if queued_count > 100 and cpu < 50 and ram < 70:
        return 2 * 60
    if queued_count > 100 and (cpu < 75 and ram < 85):
        return 5 * 60

    # Moderate queue + resources OK
    if queued_count > 10:
        return 10 * 60

    # Queue nearly empty or resources tight → slow down
    if queued_count > 0:
        return base_minutes * 60

    # Nothing queued → long sleep
    return 60 * 60


def adaptive_sleep(base_minutes: int = 30) -> int:
    """
    Rule-based adaptive sleep — no LLM call needed.
    Returns seconds to sleep. Range: 2 min (turbo) to 60 min (idle).
    Shorter sleep when queue backs up and resources are free.
    """
    try:
        from resource_monitor import ResourceMonitor
        rm = ResourceMonitor()
        state = rm.get_state()
        cpu = state["cpu_percent"]
        ram = state["ram_percent"]
        running = count_running_dreams()

        # Count queued dreams
        conn = sqlite3.connect(str(DREAM_QUEUE_DB))
        queued_count = conn.execute("SELECT COUNT(*) FROM dream_queue WHERE status = 'queued'").fetchone()[0]
        conn.close()

        seconds = decide_sleep_seconds(base_minutes, cpu, ram, running, queued_count)
        print(f"  [SCHEDULER] Next check in {seconds // 60} min (queue={queued_count}, cpu={cpu:.0f}%, ram={ram:.0f}%)")
        return seconds
    except Exception as e:
        print(f"  [SCHEDULER] Adaptive sleep error: {e} — using base {base_minutes} min")
    return base_minutes * 60


def run_daemon(interval_minutes: int = 30):
    """Run scheduler as a daemon with adaptive sleep intervals."""
    print(f"[DAEMON] Dream scheduler running (adaptive intervals, base={interval_minutes} min)")
    while True:
        results = run_scheduler_cycle()
        print(f"[DAEMON] Cycle done: {results}")
        sleep_seconds = adaptive_sleep(base_minutes=interval_minutes)
        print(f"[DAEMON] Sleeping {sleep_seconds // 60} minutes...")
        time.sleep(sleep_seconds)


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
