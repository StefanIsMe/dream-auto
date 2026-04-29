# Dream Auto v3.0.1 — Setup Guide

**For:** Hermes Agent (or any AI assistant with terminal/file access)
**Repo:** https://github.com/StefanIsMe/dream-auto
**Version:** 3.0.1

---

## Execute These Steps in Order

If Dream Auto is already installed, running these steps upgrades it. If it is not installed, these steps install it from scratch. Either way the result is the same: latest version, all files in place, databases ready.

---

### STEP 1 — Prep

```bash
HERMES_HOME="$HOME/.hermes"
DREAM_AUTO_REPO="$HOME/dream-auto"
STATE_DIR="$HERMES_HOME/state/dream"

# Use the Python and Hermes CLI from the Hermes Agent virtual environment.
# This keeps Dream Auto dependencies visible to Hermes plugins, cron jobs, and scripts.
HERMES_VENV="${HERMES_VENV:-$HERMES_HOME/hermes-agent/venv}"
HERMES_PY="${HERMES_PY:-$HERMES_VENV/bin/python}"
HERMES_BIN="${HERMES_BIN:-$HERMES_VENV/bin/hermes}"

if [ ! -x "$HERMES_PY" ]; then
    echo "ERROR: Hermes Python not found at $HERMES_PY"
    echo "Set HERMES_VENV or HERMES_PY to your Hermes virtualenv before continuing."
    exit 1
fi

if [ ! -x "$HERMES_BIN" ]; then
    HERMES_BIN="$(command -v hermes || true)"
fi

if [ -z "$HERMES_BIN" ] || [ ! -x "$HERMES_BIN" ]; then
    echo "ERROR: Hermes CLI not found. Set HERMES_BIN before continuing."
    exit 1
fi
```

---

### STEP 2 — Clone or Update the Repo

```bash
if [ ! -d "$DREAM_AUTO_REPO" ]; then
    git clone https://github.com/StefanIsMe/dream-auto.git "$DREAM_AUTO_REPO"
else
    cd "$DREAM_AUTO_REPO" && git pull
fi
```

---

### STEP 3 — Install Python Dependencies

```bash
"$HERMES_PY" -m pip install -r "$DREAM_AUTO_REPO/requirements.txt"
```

You do **not** need to activate the virtualenv first when calling the venv's Python directly. `"$HERMES_PY" -m pip ...` installs into that same Hermes virtualenv. If you prefer an interactive shell, this equivalent form also works:

```bash
source "$HERMES_VENV/bin/activate"
python -m pip install -r "$DREAM_AUTO_REPO/requirements.txt"
```

If `psutil` fails to compile, install Python dev headers first:
- Fedora/RHEL: `sudo dnf install python3-devel`
- Debian/Ubuntu: `sudo apt install python3-dev`
- macOS: `xcode-select --install`

---

### STEP 4 — Copy All Files to Hermes

```bash
# Remove old versions first (ensures deleted files are gone)
rm -rf "$HERMES_HOME/plugins/dream_auto"
rm -rf "$HERMES_HOME/skills/autonomous-ai-agents/hermes-dream-task"
rm -rf "$HERMES_HOME/skills/ops/dream-system-v3"

# Copy plugin
cp -r "$DREAM_AUTO_REPO/plugins/dream_auto" "$HERMES_HOME/plugins/"

# Copy all scripts
cp "$DREAM_AUTO_REPO/scripts/dream_scheduler.py" "$HERMES_HOME/scripts/"
cp "$DREAM_AUTO_REPO/scripts/dream_insights_dashboard.py" "$HERMES_HOME/scripts/"
cp "$DREAM_AUTO_REPO/scripts/session_indexer.py" "$HERMES_HOME/scripts/"
cp "$DREAM_AUTO_REPO/scripts/session_grader.py" "$HERMES_HOME/scripts/"

# Copy skills
cp -r "$DREAM_AUTO_REPO/skills/autonomous-ai-agents/hermes-dream-task" \
    "$HERMES_HOME/skills/autonomous-ai-agents/"
cp -r "$DREAM_AUTO_REPO/skills/ops/dream-system-v3" \
    "$HERMES_HOME/skills/ops/"
```

---

### STEP 5 — Initialize Databases

```bash
"$HERMES_PY" - <<'PYEOF'
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
            CREATE TABLE IF NOT EXISTS dream_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT,
                content TEXT,
                source TEXT,
                content_hash TEXT UNIQUE,
                cached_at TEXT,
                injected_sessions TEXT DEFAULT '[]'
            );
        """,
        "indexes": [
            "CREATE INDEX IF NOT EXISTS idx_dream_cache_topic     ON dream_cache(topic, cached_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_dream_cache_cached    ON dream_cache(cached_at DESC)",
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

# Migrate dream_queue.db schema if it already exists (v3.0.0 had a different schema)
DQ_PATH = os.path.join(STATE, "dream_queue.db")
conn = sqlite3.connect(DQ_PATH)
try:
    conn.execute("ALTER TABLE dream_queue ADD COLUMN IF NOT EXISTS dream_question TEXT")
except Exception:
    pass
try:
    conn.execute("ALTER TABLE dream_queue ADD COLUMN IF NOT EXISTS resource_cost INTEGER DEFAULT 1")
except Exception:
    pass
conn.commit()
conn.close()
print("OK: dream_queue.db migrated if needed")
PYEOF
```

---

### STEP 6 — Dashboard Wrapper

```bash
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/dream-dashboard" <<WRAPPER
#!/usr/bin/env bash
exec "$HERMES_PY" "$HERMES_HOME/scripts/dream_insights_dashboard.py" "\$@"
WRAPPER
chmod +x "$HOME/.local/bin/dream-dashboard"
```

---

### STEP 7 — Register Cron Jobs

```bash
# dream-scheduler: every 30 minutes — picks highest-priority queued dream and starts it.
# Cron scripts are executed by Hermes with the same Python runtime as Hermes itself.
"$HERMES_BIN" cron create "*/30 * * * *" \
    --name "dream-scheduler" \
    --script "$HERMES_HOME/scripts/dream_scheduler.py" \
    "Run the Dream Auto scheduler script and report its result briefly." \
    2>/dev/null || echo "dream-scheduler cron already registered (skipping)"

# session-indexer: every 6 hours — scans sessions, grades them for dream potential.
"$HERMES_BIN" cron create "0 */6 * * *" \
    --name "session-indexer" \
    --script "$HERMES_HOME/scripts/session_indexer.py" \
    "Run the Dream Auto session indexer script and report its result briefly." \
    2>/dev/null || echo "session-indexer cron already registered (skipping)"
```

---

### STEP 8 — Environment Variables

```bash
if ! grep -q "DREAM_AUTO_ENABLED" "$HOME/.bashrc" 2>/dev/null; then
    printf '\n# Dream Auto v3\n' >> "$HOME/.bashrc"
    printf 'export DREAM_AUTO_ENABLED=1\n' >> "$HOME/.bashrc"
    printf 'export DREAM_AUTO_VERBOSE=0\n' >> "$HOME/.bashrc"
    printf 'export DREAM_AUTO_MAX_INJECT=3\n' >> "$HOME/.bashrc"
fi
export DREAM_AUTO_ENABLED=1
export DREAM_AUTO_VERBOSE=0
export DREAM_AUTO_MAX_INJECT=3
```

---

### STEP 9 — First Run: Populate Session Index

```bash
"$HERMES_PY" "$HERMES_HOME/scripts/session_indexer.py" --limit 50
```

---

### STEP 10 — Verify

```bash
echo "=== Plugin files ==="
ls "$HERMES_HOME/plugins/dream_auto/__init__.py"
ls "$HERMES_HOME/plugins/dream_auto/plugin.yaml"
ls "$HERMES_HOME/plugins/dream_auto/resource_monitor.py"

echo "=== Scripts ==="
ls "$HERMES_HOME/scripts/dream_scheduler.py"
ls "$HERMES_HOME/scripts/dream_insights_dashboard.py"
ls "$HERMES_HOME/scripts/session_indexer.py"
ls "$HERMES_HOME/scripts/session_grader.py"

echo "=== Skills ==="
ls "$HERMES_HOME/skills/autonomous-ai-agents/hermes-dream-task/scripts/dream_loop_v3.py"
ls "$HERMES_HOME/skills/ops/dream-system-v3/SKILL.md"

echo "=== Databases ==="
ls "$HERMES_HOME/state/dream/session_index.db"
ls "$HERMES_HOME/state/dream/dream_queue.db"
ls "$HERMES_HOME/state/dream/knowledge_cache.db"

echo "=== Dashboard ==="
"$HERMES_PY" "$HERMES_HOME/scripts/dream_insights_dashboard.py" --dry-run 2>/dev/null && echo "dashboard: OK" || echo "dashboard: FAILED"

echo "=== Cron jobs ==="
"$HERMES_BIN" cron list 2>/dev/null | grep -E "dream-scheduler|session-indexer"
```

If all paths respond without error, Dream Auto is installed.

---

## What to Do After Install

**Trigger a manual scheduler run** (syncs any completed dreams stuck in "running" state):

```bash
"$HERMES_PY" "$HERMES_HOME/scripts/dream_scheduler.py"
```

**Check the dashboard:**

```bash
dream-dashboard              # full overview
dream-dashboard --insights  # recent insights only
dream-dashboard --queue     # queue only
dream-dashboard --errors    # error breakdown
```

**Restart Hermes gateway if cron jobs are not firing:**

```bash
"$HERMES_BIN" gateway status
# If down:
"$HERMES_BIN" gateway install && "$HERMES_BIN" gateway start
```

---

## Files in This Distribution

```
dream-auto/
├── SETUP.md               # This file — agent-executable install/upgrade guide
├── README.md              # Human-readable overview
├── requirements.txt       # psutil, rich
├── plugins/
│   └── dream_auto/       # __init__.py, resource_monitor.py, plugin.yaml
├── scripts/
│   ├── dream_scheduler.py         # Queue manager + wallclock killer + knowledge cache sync
│   ├── dream_insights_dashboard.py # CLI dashboard
│   ├── session_indexer.py          # Session scanner + grader
│   └── session_grader.py           # LLM-based potential scorer
└── skills/
    ├── autonomous-ai-agents/hermes-dream-task/
    │   ├── scripts/dream_loop_v3.py  # MCTS engine v3
    │   ├── scripts/fast_path.py       # Heuristic分流
    │   └── SKILL.md
    └── ops/dream-system-v3/
        └── SKILL.md
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Hermes venv not found | Set `HERMES_VENV`, `HERMES_PY`, or reinstall Hermes before continuing |
| `hermes: command not found` | Set `HERMES_BIN` or add Hermes to PATH before installing |
| `psutil` install fails | `sudo dnf install python3-devel` (Fedora) or `sudo apt install python3-dev` (Debian/Ubuntu) or `xcode-select --install` (macOS) |
| `dream-dashboard` not found | `~/.local/bin` not in PATH. Use `$HERMES_PY ~/.hermes/scripts/dream_insights_dashboard.py` instead |
| Cron jobs not firing | Run `$HERMES_BIN gateway start` |
| Dashboard shows no sessions | Run `$HERMES_PY ~/.hermes/scripts/session_indexer.py --limit 50` manually |
| Dreams never start | Check `~/.hermes/plugins/dream_auto/resource_monitor.py` for CPU/RAM availability |
| Queue shows dreams stuck "running" | Run `$HERMES_PY ~/.hermes/scripts/dream_scheduler.py` once manually — the completion-detection fix (v3.0.1) syncs them |
| Upgrade: old files persist | Run Step 4 again — the `rm -rf` before each `cp` ensures clean overwrite |
| DB errors after upgrade | Run Step 5 again — `CREATE INDEX IF NOT EXISTS` and `CREATE TABLE IF NOT EXISTS` are safe to re-run |

---

## Version History

| Version | What changed |
|---------|-------------|
| 3.0.1   | **Bug fixes**: completion detection (queue DB now synced when dreams finish normally), LLM timeout bug fixed (rule-based scheduler), 5 engine bugs in dream_loop_v3.py, ATTACH DATABASE fix. No DB schema changes. |
| 3.0.0   | Initial v3 release — MCTS engine, parallel rollouts, UCB1-Tuned, CI-width bonus, wallclock killer, SQLite indexes, knowledge cache |
