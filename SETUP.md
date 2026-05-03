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
    echo "ERROR: Hermes CLI not found. Make sure Hermes Agent is installed and in your PATH."
    echo "If Hermes is installed in a custom location, set HERMES_BIN to the hermes binary path."
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
if "$HERMES_PY" -m pip --version >/dev/null 2>&1; then
    "$HERMES_PY" -m pip install -r "$DREAM_AUTO_REPO/requirements.txt"
elif command -v uv >/dev/null 2>&1; then
    uv pip install --python "$HERMES_PY" -r "$DREAM_AUTO_REPO/requirements.txt"
else
    "$HERMES_PY" -m ensurepip --upgrade
    "$HERMES_PY" -m pip install -r "$DREAM_AUTO_REPO/requirements.txt"
fi

"$HERMES_PY" -c "import psutil, rich" 2>/dev/null || echo "WARNING: psutil or rich not importable — check Step 3"
```

You do **not** need to activate the virtualenv first when calling the venv's Python directly. `"$HERMES_PY" -m pip ...` installs into that same Hermes virtualenv. If the venv does not have `pip` but `uv` is installed, `uv pip install --python "$HERMES_PY" ...` installs into the same Hermes venv. If you prefer an interactive shell, this equivalent form also works:

```bash
source "$HERMES_VENV/bin/activate"
python -m pip install -r "$DREAM_AUTO_REPO/requirements.txt"
# Or, with uv:
uv pip install -r "$DREAM_AUTO_REPO/requirements.txt"
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
cp "$DREAM_AUTO_REPO/scripts/dream_pipeline.py" "$HERMES_HOME/scripts/"

# Copy skills (MCTS engine + fast_path分流)
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
cat > "$HOME/.local/bin/dream-dashboard" <<'WRAPPER'
#!/usr/bin/env bash
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_VENV="${HERMES_VENV:-$HERMES_HOME/hermes-agent/venv}"
HERMES_PY="${HERMES_PY:-$HERMES_VENV/bin/python}"
exec "$HERMES_PY" "$HERMES_HOME/scripts/dream_insights_dashboard.py" "$@"
WRAPPER
chmod +x "$HOME/.local/bin/dream-dashboard"
```

---

### STEP 7 — Register Cron Jobs

Dream Auto needs two recurring jobs:
- **dream-scheduler** — picks the highest-priority queued dream and starts it (every 30 min)
- **session-indexer** — scans recent Hermes sessions and grades them for dream potential (every 6h)

Choose how to schedule them:

```bash
echo "=== Dream Auto Cron Scheduling ==="
echo "How should the background jobs be scheduled?"
echo ""
echo "  [1] Recommended  — scheduler every 30 min, indexer every 6h"
echo "                    (low resource impact, good for most users)"
echo ""
echo "  [2] Custom      — specify your own cron expressions"
echo "                    (you choose the exact times)"
echo ""
echo "  [3] Auto-detect — analyze my Hermes history and pick optimal quiet hours"
echo "                    (uses LLM API calls to find when your machine is idle)"
echo ""
read -p "Select [1/2/3]: " CRON_CHOICE

if [ "$CRON_CHOICE" = "1" ]; then
    # Recommended defaults
    SCHEDULER_CRON="*/30 * * * *"
    PIPELINE_CRON="0 */2 * * *"
    echo "Scheduler: every 30 min | Pipeline: every 2h"

elif [ "$CRON_CHOICE" = "2" ]; then
    echo "Enter dream-scheduler cron expression (e.g. */30 * * * *):"
    read SCHEDULER_CRON
    echo "Enter dream-pipeline cron expression (e.g. 0 */2 * * *):"
    read PIPELINE_CRON

elif [ "$CRON_CHOICE" = "3" ]; then
    echo "Analyzing your Hermes session history for optimal quiet hours..."
    # The LLM reads hermes sessions list + resource patterns and picks quiet windows
    RECOMMENDATION=$("$HERMES_BIN" chat -q \
        "Analyze the user's Hermes session history from 'hermes sessions list' output. \
Find the typical active hours and idle/quiet windows. \
Recommend cron schedules that minimise interference with the user's active work. \
The dream-scheduler ideally runs every 30 minutes. \
The dream-pipeline (indexer + grader) ideally runs every 2 hours. \
Output ONLY this exact format, nothing else: \
SCHEDULER=<cron_expr> PIPELINE=<cron_expr>" 2>/dev/null)
    SCHEDULER_CRON=$(echo "$RECOMMENDATION" | grep -oP 'SCHEDULER=\K[^ ]+')
    PIPELINE_CRON=$(echo "$RECOMMENDATION" | grep -oP 'PIPELINE=\K[^ ]+')
    echo "Auto-detected — Scheduler: $SCHEDULER_CRON | Pipeline: $PIPELINE_CRON"

    # Fallback if LLM returns nothing useful
    if [ -z "$SCHEDULER_CRON" ] || [ -z "$PIPELINE_CRON" ]; then
        echo "WARNING: Could not determine optimal schedule. Using recommended defaults."
        SCHEDULER_CRON="*/30 * * * *"
        PIPELINE_CRON="0 */2 * * *"
    fi
else
    echo "Invalid choice. Using recommended defaults."
    SCHEDULER_CRON="*/30 * * * *"
    PIPELINE_CRON="0 */2 * * *"
fi

# Register dream-scheduler
"$HERMES_BIN" cron create "$SCHEDULER_CRON" \
    --name "dream-scheduler" \
    --script "dream_scheduler.py" \
    "Run the Dream Auto scheduler." \
    2>/dev/null || echo "dream-scheduler cron already registered (skipping)"

# Register dream-pipeline (merged indexer + grader)
"$HERMES_BIN" cron create "$PIPELINE_CRON" \
    --name "dream-pipeline" \
    --script "dream_pipeline.py" \
    "Run the Dream Pipeline — merged session indexer + grader." \
    2>/dev/null || echo "dream-pipeline cron already registered (skipping)"
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
"$HERMES_PY" "$HERMES_HOME/scripts/dream_pipeline.py" --index-only
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
ls "$HERMES_HOME/scripts/dream_pipeline.py"

echo "=== Skills ==="
ls "$HERMES_HOME/skills/autonomous-ai-agents/hermes-dream-task/scripts/dream_loop_v3.py"
ls "$HERMES_HOME/skills/autonomous-ai-agents/hermes-dream-task/scripts/fast_path.py"
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
├── pytest.ini             # Test configuration
├── plugins/
│   └── dream_auto/
│       ├── __init__.py           # Main plugin (6 hooks)
│       ├── plugin.yaml           # Plugin manifest
│       ├── resource_monitor.py   # CPU/RAM monitoring
│       └── tests/
│           ├── __init__.py
│           ├── test_dream_auto_plugin.py
│           └── test_resource_monitor.py
├── scripts/
│   ├── dream_scheduler.py         # Queue manager + wallclock killer
│   ├── dream_insights_dashboard.py # CLI dashboard
│   ├── dream_pipeline.py            # Merged session indexer + grader (v2 rubric)
│   └── dream_loop_v3.py            # MCTS engine v3 (also in skills/)
└── skills/
    └── autonomous-ai-agents/
        └── hermes-dream-task/
            ├── SKILL.md
            └── scripts/
                ├── dream_loop_v3.py  # MCTS engine v3 — two-tier AIAgent
                ├── fast_path.py       # Heuristic分流 (fast/slow path)
                └── test_tool_rollouts.py
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Hermes venv not found | Set `HERMES_VENV`, `HERMES_PY`, or reinstall Hermes before continuing |
| `hermes: command not found` | Set `HERMES_BIN` or add Hermes to PATH before installing |
| `pip` missing from Hermes venv | Install `uv` and rerun Step 3, or let Step 3 try `ensurepip` automatically |
| `uv: command not found` | Install uv from https://docs.astral.sh/uv/ or ensure the Hermes venv has pip |
| `psutil` install fails | `sudo dnf install python3-devel` (Fedora) or `sudo apt install python3-dev` (Debian/Ubuntu) or `xcode-select --install` (macOS) |
| `dream-dashboard` not found | `~/.local/bin` not in PATH. Use `$HERMES_PY ~/.hermes/scripts/dream_insights_dashboard.py` instead |
| Cron jobs not firing | Run `$HERMES_BIN gateway start` |
| Dashboard shows no sessions | Run `$HERMES_PY ~/.hermes/scripts/dream_pipeline.py --index-only` to populate the session index |
| Dreams never start | Check resources: CPU must be < 70% AND RAM < 70% for scheduler to start dreams |
| Queue shows dreams stuck "running" | Run `$HERMES_PY ~/.hermes/scripts/dream_scheduler.py` once manually — the completion-detection fix (v3.0.1) syncs them |
| Upgrade: old files persist | Run Step 4 again — the `rm -rf` before each `cp` ensures clean overwrite |
| DB errors after upgrade | Run Step 5 again — `CREATE INDEX IF NOT EXISTS` and `CREATE TABLE IF NOT EXISTS` are safe to re-run |

---

## Version History

| Version | What changed |
|---------|-------------|
| 3.0.1   | **Bug fixes**: completion detection (queue DB now synced when dreams finish normally), LLM timeout bug fixed (rule-based scheduler), 5 engine bugs in dream_loop_v3.py, ATTACH DATABASE fix. No DB schema changes. |
| 3.0.0   | Initial v3 release — MCTS engine, parallel rollouts, UCB1-Tuned, CI-width bonus, wallclock killer, SQLite indexes, knowledge cache |
