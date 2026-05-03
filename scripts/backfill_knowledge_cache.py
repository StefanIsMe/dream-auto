#!/usr/bin/env python3
"""
Backfill knowledge_cache.db with insights from all completed dreams
that were never synced (BUG 4: sync only fired for status=='completed').
Run once. Safe to re-run — uses content_hash dedup.
"""
import sqlite3, json, hashlib, os
from pathlib import Path
from datetime import datetime, timezone, timedelta

GMT7 = timezone(timedelta(hours=7))

DREAM_DIR = Path.home() / ".hermes" / "state" / "dream"
KNOWLEDGE_CACHE_DB = DREAM_DIR / "knowledge_cache.db"
DREAM_QUEUE_DB = DREAM_DIR / "dream_queue.db"
SESSION_INDEX_DB = DREAM_DIR / "session_index.db"

DONE_STATUSES = {
    "done", "completed", "completed_success",
    "completed_killed", "killed_wallclock", "completed_stale", "stale_completed", "completed_empty",
    "failed", "failed_crash", "failed_restart", "health_check_failed", "circuit_breaker",
}

def now_iso():
    return datetime.now(GMT7).isoformat()

def read_json(path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default

def extract_topic(dream_id):
    """Extract topic from session_index.db using the session_id that triggered the dream."""
    if not SESSION_INDEX_DB.exists():
        return "general"
    try:
        conn = sqlite3.connect(str(SESSION_INDEX_DB))
        # Try to find the session that generated this dream
        row = conn.execute(
            "SELECT topics FROM sessions WHERE session_id=?", (dream_id,)
        ).fetchone()
        conn.close()
        if row and row[0]:
            topics = json.loads(row[0])
            if topics:
                return topics[0]
    except Exception:
        pass
    return "general"

def extract_brief_topic(dream_id):
    """Fallback: extract topic from dream_queue brief text."""
    if not DREAM_QUEUE_DB.exists():
        return "general"
    try:
        conn = sqlite3.connect(str(DREAM_QUEUE_DB))
        row = conn.execute(
            "SELECT dream_question FROM dream_queue WHERE dream_id=?", (dream_id,)
        ).fetchone()
        conn.close()
        if row and row[0]:
            brief = row[0].lower()
            kw_map = {
                "linkedin": ["linkedin", "li_at", "org2", "social post"],
                "research": ["research", "arxiv", "paper", "academic"],
                "coding": ["python", "javascript", "debug", "api", "coding"],
                "browser": ["chrome", "cdp", "selenium", "scraper", "camoufox"],
                "database": ["sqlite", "postgres", "sql", "db", "database"],
                "hermes": ["hermes", "agent", "cron", "plugin", "hook"],
                "web": ["website", "seo", "cloudflare", "deployment"],
                "vietnam": ["vietnam", "hcmc", "tay ninh", "vnd"],
                "content": ["article", "blog", "writing", "seo", "content"],
                "ai": ["llm", "gpt", "claude", "model", "ai", "inference"],
            }
            for topic, kws in kw_map.items():
                if any(kw in brief for kw in kws):
                    return topic
    except Exception:
        pass
    return "general"

def sync_dream(dream_id):
    """Sync a single dream's insights to knowledge_cache.db. Returns True if cached."""
    dp = DREAM_DIR / dream_id
    if not dp.is_dir():
        return False

    meta = read_json(dp / "meta.json", {})
    if not meta:
        return False

    if meta.get("status") not in DONE_STATUSES:
        return False

    insights_file = dp / "insights.json"
    insights = read_json(insights_file, [])
    if not insights:
        return False

    questions = read_json(dp / "pending_questions.json", []) or []

    # Build content
    content_parts = []
    for insight in insights:
        content_parts.append(f"insight: {insight}")
    for q in questions[:2]:
        content_parts.append(f"open_question: {q}")

    content = "\n".join(content_parts)
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    topic = extract_topic(dream_id)
    if topic == "general":
        topic = extract_brief_topic(dream_id)

    conn = sqlite3.connect(str(KNOWLEDGE_CACHE_DB))
    try:
        existing = conn.execute(
            "SELECT id FROM knowledge_cache WHERE content_hash=?", (content_hash,)
        ).fetchone()
        if existing:
            conn.close()
            return False  # already cached

        now = now_iso()
        conn.execute("""
            INSERT INTO knowledge_cache (topic, content, content_hash, source, cached_at, injected_sessions)
            VALUES (?, ?, ?, ?, ?, '[]')
        """, (topic, content, content_hash, f"dream:{dream_id}", now))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        conn.close()
        print(f"  [ERROR] {dream_id}: {e}")
        return False

def main():
    if not DREAM_DIR.exists():
        print(f"ERROR: DREAM_DIR not found at {DREAM_DIR}")
        return

    # Ensure schema
    conn = sqlite3.connect(str(KNOWLEDGE_CACHE_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT,
            content TEXT,
            source TEXT,
            content_hash TEXT UNIQUE,
            cached_at TEXT,
            injected_sessions TEXT DEFAULT '[]'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kc_hash ON knowledge_cache(content_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kc_topic ON knowledge_cache(topic, cached_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kc_cached ON knowledge_cache(cached_at DESC)")
    conn.commit()
    conn.close()

    # Count existing dream-sourced entries
    conn = sqlite3.connect(str(KNOWLEDGE_CACHE_DB))
    existing_count = conn.execute(
        "SELECT COUNT(*) FROM knowledge_cache WHERE source LIKE 'dream:%'"
    ).fetchone()[0]
    conn.close()

    print(f"Knowledge cache: {existing_count} dream entries already cached")
    print(f"Scanning DREAM_DIR: {DREAM_DIR}")
    print(f"Total items: {len(list(DREAM_DIR.iterdir()))}")

    synced = 0
    skipped_not_done = 0
    skipped_no_insights = 0
    skipped_already_cached = 0
    errors = 0

    for item in DREAM_DIR.iterdir():
        if not item.is_dir():
            continue
        dream_id = item.name

        # Check if already cached
        conn = sqlite3.connect(str(KNOWLEDGE_CACHE_DB))
        already = conn.execute(
            "SELECT id FROM knowledge_cache WHERE source=?", (f"dream:{dream_id}",)
        ).fetchone()
        conn.close()
        if already:
            skipped_already_cached += 1
            continue

        meta = read_json(item / "meta.json", None)
        if not meta:
            errors += 1
            continue

        status = meta.get("status", "")
        if status not in DONE_STATUSES:
            skipped_not_done += 1
            continue

        insights_file = item / "insights.json"
        insights = read_json(insights_file, [])
        if not insights:
            skipped_no_insights += 1
            continue

        if sync_dream(dream_id):
            synced += 1
            print(f"  [SYNCED] {dream_id} | status={status} | {len(insights)} insights | topic={extract_topic(dream_id)}")

    print(f"\n=== Backfill Complete ===")
    print(f"  New entries added:     {synced}")
    print(f"  Already cached:        {skipped_already_cached}")
    print(f"  Not done-status:       {skipped_not_done}")
    print(f"  No insights:           {skipped_no_insights}")
    print(f"  Errors:                {errors}")
    print(f"  Total dream entries:   {existing_count + synced}")

if __name__ == "__main__":
    main()
