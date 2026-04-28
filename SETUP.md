# Dream Auto Plugin — Setup Guide

**For:** Hermes Agent (or any AI assistant with terminal/file access)
**Repo:** https://github.com/StefanIsMe/dream-auto
**Version:** 3.0.1

---

## Before You Start

Dream Auto runs background MCTS reasoning dreams on your machine. It needs:
- Python 3.10+
- Hermes Agent installed (`hermes` CLI in PATH)
- Hermes gateway running (for cron jobs)
- Linux, macOS, or Windows WSL

---

## Quick Install (Let Hermes Do It)

Paste this into your Hermes chat:

```
Go to https://github.com/StefanIsMe/dream-auto/blob/main/SETUP.md and follow the setup steps to install Dream Auto v3 on this machine. Execute all steps. Report what was installed.
```

Existing Dream Auto user? Same prompt works — it detects what is already there and upgrades cleanly.

---

## All-in-One Install / Upgrade Command

If you prefer to run it yourself in one shot, copy and paste this entire block into your terminal:

```bash
# ── Dream Auto v3 — Install or Upgrade ──
set -e

HERMES_HOME="$HOME/.hermes"
DREAM_AUTO_REPO="$HOME/dream-auto"

# Clone if not present
if [ ! -d "$DREAM_AUTO_REPO" ]; then
    git clone https://github.com/StefanIsMe/dream-auto.git "$DREAM_AUTO_REPO"
fi

cd "$DREAM_AUTO_REPO"
git pull

# Install deps
pip3 install --user -r requirements.txt

# Copy plugin files
rm -rf "$HERMES_HOME/plugins/dream_auto"
cp -r "$DREAM_AUTO_REPO/plugins/dream_auto" "$HERMES_HOME/plugins/"

# Copy scripts
for script in dream_scheduler.py dream_insights_dashboard.py session_indexer.py session_grader.py; do
    cp "$DREAM_AUTO_REPO/scripts/$script" "$HERMES_HOME/scripts/"
done

# Copy skills
rm -rf "$HERMES_HOME/skills/autonomous-ai-agents/hermes-dream-task"
cp -r "$DREAM_AUTO_REPO/skills/autonomous-ai-agents/hermes-dream-task" \
    "$HERMES_HOME/skills/autonomous-ai-agents/"

rm -rf "$HERMES_HOME/skills/ops/dream-system-v3"
cp -r "$DREAM_AUTO_REPO/skills/ops/dream-system-v3" \
    "$HERMES_HOME/skills/ops/"

# Init DBs (safe to run on existing DBs — adds indexes, preserves data)
python3 - <<'PYEOF'
import sqlite3, os
STATE = os.path.expanduser("~/.hermes/state/dream")
os.makedirs(STATE, exist_ok=True)
os.makedirs(os.path.join(STATE, "logs"), exist_ok=True)

DB_SCHEMAS = {
    "session_index.db": {
        "tables": """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, created_at TEXT, last_message_at TEXT,
                message_count INTEGER DEFAULT 0, topics TEXT DEFAULT '[]',
                had_errors INTEGER DEFAULT 0, error_count INTEGER DEFAULT 0,
                was_complex INTEGER DEFAULT 0, open_questions TEXT DEFAULT '[]',
                unresolved TEXT DEFAULT '[]', dream_potential REAL,
                dream_potential_reason TEXT, dreams_run TEXT DEFAULT '[]',
                last_dreamed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS indexed_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                indexed_at TEXT, session_count INTEGER, errors INTEGER
            );
        """,
        "indexes": [
            "CREATE INDEX IF NOT EXISTS idx_sessions_dream_potential ON sessions(dream_potential DESC)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_had_errors     ON sessions(had_errors)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_last_dreamed  ON sessions(last_dreamed_at)",
        ],
    },
    "dream_queue.db": {
        "tables": """
            CREATE TABLE IF NOT EXISTS dream_queue (
                queue_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
                dream_id TEXT UNIQUE, dream_question TEXT, grade REAL,
                resource_cost INTEGER DEFAULT 1, priority REAL, created_at TEXT,
                started_at TEXT, completed_at TEXT, status TEXT DEFAULT 'queued'
            );
        """,
        "indexes": [
            "CREATE INDEX IF NOT EXISTS idx_queue_status_priority ON dream_queue(status, priority DESC)",
            "CREATE INDEX IF NOT EXISTS idx_queue_dream_id       ON dream_queue(dream_id)",
        ],
    },
    "knowledge_cache.db": {
        "tables": """
            CREATE TABLE IF NOT EXISTS knowledge_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT, content TEXT,
                source TEXT, cached_at TEXT, content_hash TEXT UNIQUE
            );
        """,
        "indexes": [
            "CREATE INDEX IF NOT EXISTS idx_topic_cached ON knowledge_cache(topic, cached_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_cached       ON knowledge_cache(cached_at DESC)",
        ],
    },
}

for db_name, schema in DB_SCHEMAS.items():
    path = os.path.join(STATE, db_name)
    conn = sqlite3.connect(path)
    conn.executescript(schema["tables"])
    for idx in schema["indexes"]:
        conn.execute(idx)
    conn.commit()
    conn.close()
    print(f"OK: {db_name}")
PYEOF

# Dashboard wrapper
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/dream-dashboard" <<'WRAPPER'
#!/usr/bin/env bash
exec python3 "$HOME/.hermes/scripts/dream_insights_dashboard.py" "$@"
WRAPPER
chmod +x "$HOME/.local/bin/dream-dashboard"

# Register cron jobs (skip if already registered)
hermes cron list 2>/dev/null | grep -q "dream-scheduler" || \
    hermes cron create --name "dream-scheduler" \
    --schedule "*/30 * * * *" \
    --command "python3 $HOME/.hermes/scripts/dream_scheduler.py"

hermes cron list 2>/dev/null | grep -q "session-indexer" || \
    hermes cron create --name "session-indexer" \
    --schedule "0 */6 * * *" \
    --command "python3 $HOME/.hermes/scripts/session_indexer.py"

# Env vars
grep -q "DREAM_AUTO_ENABLED" "$HOME/.bashrc" 2>/dev/null || \
    printf '\n# Dream Auto v3\nexport DREAM_AUTO_ENABLED=1\nexport DREAM_AUTO_VERBOSE=0\nexport DREAM_AUTO_MAX_INJECT=3\n' >> "$HOME/.bashrc"

echo "Done. Run: python3 $HOME/.hermes/scripts/session_indexer.py --limit 50"
```

---

## Step-by-Step Breakdown

If you are running this manually or need to debug a specific step, here is what each part does.

---

### STEP 1: Check Prerequisites

```bash
python3 --version    # Must be 3.10+
hermes --version     # Must respond
pip3 --version       # Must respond
```

If Hermes gateway is not running:

```bash
hermes gateway status
# If down:
hermes gateway install
hermes gateway start
```

---

### STEP 2: Clone or Update the Repo

**First time:**

```bash
cd ~
git clone https://github.com/StefanIsMe/dream-auto.git
cd dream-auto
```

**Already installed — to upgrade:**

```bash
cd ~/dream-auto
git pull
```

---

### STEP 3: Install Python Dependencies

```bash
pip3 install --user -r requirements.txt
```

If `psutil` fails to compile, install Python dev headers first:
- Fedora/RHEL: `sudo dnf install python3-devel`
- Debian/Ubuntu: `sudo apt install python3-dev`
- macOS: `xcode-select --install`

---

### STEP 4: Copy Files

**First time:**

```bash
# Plugin
cp -r ~/dream-auto/plugins/dream_auto ~/.hermes/plugins/

# Scripts
cp ~/dream-auto/scripts/dream_scheduler.py ~/.hermes/scripts/
cp ~/dream-auto/scripts/dream_insights_dashboard.py ~/.hermes/scripts/
cp ~/dream-auto/scripts/session_indexer.py ~/.hermes/scripts/
cp ~/dream-auto/scripts/session_grader.py ~/.hermes/scripts/

# Skills
cp -r ~/dream-auto/skills/autonomous-ai-agents/hermes-dream-task \
    ~/.hermes/skills/autonomous-ai-agents/
cp -r ~/dream-auto/skills/ops/dream-system-v3 \
    ~/.hermes/skills/ops/
```

**Upgrading — overwrite everything:**

```bash
# Remove old versions first, then copy (ensures deleted files are gone)
rm -rf ~/.hermes/plugins/dream_auto
rm -rf ~/.hermes/skills/autonomous-ai-agents/hermes-dream-task
rm -rf ~/.hermes/skills/ops/dream-system-v3

# Copy fresh
cp -r ~/dream-auto/plugins/dream_auto ~/.hermes/plugins/
cp ~/dream-auto/scripts/*.py ~/.hermes/scripts/
cp -r ~/dream-auto/skills/autonomous-ai-agents/hermes-dream-task \
    ~/.hermes/skills/autonomous-ai-agents/
cp -r ~/dream-auto/skills/ops/dream-system-v3 \
    ~/.hermes/skills/ops/
```

---

### STEP 5: Initialize Databases

Safe to run on existing databases — adds v3 indexes and creates `knowledge_cache.db` if missing. Existing data is preserved.

```bash
python3 - <<'PYEOF'
import sqlite3, os
STATE = os.path.expanduser("~/.hermes/state/dream")
os.makedirs(STATE, exist_ok=True)
os.makedirs(os.path.join(STATE, "logs"), exist_ok=True)

DB_SCHEMAS = {
    "session_index.db": {
        "tables": """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, created_at TEXT, last_message_at TEXT,
                message_count INTEGER DEFAULT 0, topics TEXT DEFAULT '[]',
                had_errors INTEGER DEFAULT 0, error_count INTEGER DEFAULT 0,
                was_complex INTEGER DEFAULT 0, open_questions TEXT DEFAULT '[]',
                unresolved TEXT DEFAULT '[]', dream_potential REAL,
                dream_potential_reason TEXT, dreams_run TEXT DEFAULT '[]',
                last_dreamed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS indexed_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                indexed_at TEXT, session_count INTEGER, errors INTEGER
            );
        """,
        "indexes": [
            "CREATE INDEX IF NOT EXISTS idx_sessions_dream_potential ON sessions(dream_potential DESC)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_had_errors     ON sessions(had_errors)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_last_dreamed  ON sessions(last_dreamed_at)",
        ],
    },
    "dream_queue.db": {
        "tables": """
            CREATE TABLE IF NOT EXISTS dream_queue (
                queue_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
                dream_id TEXT UNIQUE, dream_question TEXT, grade REAL,
                resource_cost INTEGER DEFAULT 1, priority REAL, created_at TEXT,
                started_at TEXT, completed_at TEXT, status TEXT DEFAULT 'queued'
            );
        """,
        "indexes": [
            "CREATE INDEX IF NOT EXISTS idx_queue_status_priority ON dream_queue(status, priority DESC)",
            "CREATE INDEX IF NOT EXISTS idx_queue_dream_id       ON dream_queue(dream_id)",
        ],
    },
    "knowledge_cache.db": {
        "tables": """
            CREATE TABLE IF NOT EXISTS knowledge_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT, content TEXT,
                source TEXT, cached_at TEXT, content_hash TEXT UNIQUE
            );
        """,
        "indexes": [
            "CREATE INDEX IF NOT EXISTS idx_topic_cached ON knowledge_cache(topic, cached_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_cached       ON knowledge_cache(cached_at DESC)",
        ],
    },
}

for db_name, schema in DB_SCHEMAS.items():
    path = os.path.join(STATE, db_name)
    conn = sqlite3.connect(path)
    conn.executescript(schema["tables"])
    for idx in schema["indexes"]:
        conn.execute(idx)
    conn.commit()
    conn.close()
    print(f"OK: {db_name}")
PYEOF
```

---

### STEP 6: Dashboard Wrapper

```bash
mkdir -p ~/.local/bin
cat > ~/.local/bin/dream-dashboard <<'WRAPPER'
#!/usr/bin/env bash
exec python3 "$HOME/.hermes/scripts/dream_insights_dashboard.py" "$@"
WRAPPER
chmod +x ~/.local/bin/dream-dashboard
```

Ensure `~/.local/bin` is in your PATH. If `dream-dashboard` command is not found after this, use the full path:

```bash
python3 ~/.hermes/scripts/dream_insights_dashboard.py
```

---

### STEP 7: Register Cron Jobs

```bash
hermes cron create \
  --name "dream-scheduler" \
  --schedule "*/30 * * * *" \
  --command "python3 $HOME/.hermes/scripts/dream_scheduler.py"

hermes cron create \
  --name "session-indexer" \
  --schedule "0 */6 * * *" \
  --command "python3 $HOME/.hermes/scripts/session_indexer.py"
```

If a job with that name already exists, the cron system will reject the create — this is fine, skip it.

---

### STEP 8: Environment Variables

Add to `~/.bashrc` (or `~/.zshrc`):

```bash
export DREAM_AUTO_ENABLED=1
export DREAM_AUTO_VERBOSE=0
export DREAM_AUTO_MAX_INJECT=3
```

Reload in current session:

```bash
export DREAM_AUTO_ENABLED=1
export DREAM_AUTO_VERBOSE=0
export DREAM_AUTO_MAX_INJECT=3
```

---

### STEP 9: Initial Session Index

```bash
python3 ~/.hermes/scripts/session_indexer.py --limit 50
```

This scans your recent sessions and grades them for dream potential. Run once after install to populate the database.

---

### STEP 10: Verify

```bash
# Plugin files
ls ~/.hermes/plugins/dream_auto/__init__.py
ls ~/.hermes/plugins/dream_auto/resource_monitor.py
ls ~/.hermes/plugins/dream_auto/plugin.yaml

# Scripts
ls ~/.hermes/scripts/dream_scheduler.py
ls ~/.hermes/scripts/dream_insights_dashboard.py
ls ~/.hermes/scripts/session_indexer.py
ls ~/.hermes/scripts/session_grader.py

# Skills
ls ~/.hermes/skills/autonomous-ai-agents/hermes-dream-task/scripts/dream_loop_v3.py
ls ~/.hermes/skills/autonomous-ai-agents/hermes-dream-task/scripts/fast_path.py
ls ~/.hermes/skills/ops/dream-system-v3/SKILL.md

# Databases (all 3 should exist)
ls ~/.hermes/state/dream/session_index.db
ls ~/.hermes/state/dream/dream_queue.db
ls ~/.hermes/state/dream/knowledge_cache.db

# Dashboard
dream-dashboard --dry-run 2>/dev/null || python3 ~/.hermes/scripts/dream_insights_dashboard.py --dry-run

# Cron jobs
hermes cron list
```

All should respond without error.

---

## Upgrading from v1 or v2

```bash
cd ~/dream-auto
git pull
```

Then re-run Steps 4 and 5 from above:

```bash
# Step 4 — overwrite all files
rm -rf ~/.hermes/plugins/dream_auto
rm -rf ~/.hermes/skills/autonomous-ai-agents/hermes-dream-task
rm -rf ~/.hermes/skills/ops/dream-system-v3
cp -r ~/dream-auto/plugins/dream_auto ~/.hermes/plugins/
cp ~/dream-auto/scripts/*.py ~/.hermes/scripts/
cp -r ~/dream-auto/skills/autonomous-ai-agents/hermes-dream-task ~/.hermes/skills/autonomous-ai-agents/
cp -r ~/dream-auto/skills/ops/dream-system-v3 ~/.hermes/skills/ops/

# Step 5 — run the DB init Python script (Step 5 above)
# This adds v3 indexes to your existing DBs and creates knowledge_cache.db.
# It does NOT touch your existing session data or pending dreams.

# Step 9 — re-index sessions with v3 grading
python3 ~/.hermes/scripts/session_indexer.py --limit 50
```

Pending dreams from v1/v2 survive the upgrade. The scheduler handles them normally.

---

## Files in This Distribution

```
dream-auto/
├── SETUP.md               # This file — full install/upgrade guide
├── README.md              # Human-readable overview
├── requirements.txt       # psutil, rich
├── plugins/
│   └── dream_auto/        # __init__.py, resource_monitor.py, plugin.yaml
├── scripts/
│   ├── dream_scheduler.py         # Queue manager + wallclock killer
│   ├── dream_insights_dashboard.py # CLI dashboard
│   ├── session_indexer.py          # Session scanner + grader
│   └── session_grader.py           # LLM-based potential scorer
└── skills/
    ├── autonomous-ai-agents/hermes-dream-task/
    │   ├── scripts/dream_loop_v3.py  # MCTS engine v3
    │   ├── scripts/fast_path.py       # Fast-path分流 heuristic
    │   └── SKILL.md
    └── ops/dream-system-v3/
        └── SKILL.md
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `hermes: command not found` | Install Hermes Agent first. Stop here. |
| `pip3: command not found` | Install Python pip first. Stop here. |
| `psutil` install fails | `sudo dnf install python3-devel` (Fedora) or `sudo apt install python3-dev` (Debian/Ubuntu) or `xcode-select --install` (macOS) |
| `dream-dashboard` not found | `~/.local/bin` not in your PATH. Use `python3 ~/.hermes/scripts/dream_insights_dashboard.py` instead. |
| Queue shows dreams stuck "running" after completion | The scheduler's `sync_dream_status()` (v3.0.1+) syncs completed dreams from meta.json to the queue DB. Run `python3 ~/.hermes/scripts/dream_scheduler.py` once manually to trigger the sync. |
| Cron jobs fail to register | Run `hermes gateway start` first. |
| Dashboard shows no sessions | Run `python3 ~/.hermes/scripts/session_indexer.py --limit 50` manually. |
| Dreams never start | Check resources: `python3 ~/.hermes/plugins/dream_auto/resource_monitor.py` |
| Upgrade: old files persist | Make sure to `rm -rf` the old directories before copying (Step 4 upgrade instructions). |
| DB errors after upgrade | Run Step 5 again. `CREATE INDEX IF NOT EXISTS` is safe to re-run on any existing DB. |
| knowledge_cache.db missing | Run Step 5. It creates the table and indexes even on an existing partial install. |

---

## Platform Notes

- **Linux:** Full support. Uses `hermes cron` (systemd user service).
- **macOS:** Full support. Uses `hermes cron` (launchd).
- **Windows WSL:** Full support inside WSL. Install Hermes inside WSL.
- **Windows Native:** Not supported. Use WSL2.
