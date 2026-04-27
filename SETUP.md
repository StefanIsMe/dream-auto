# Dream Auto Plugin — Agent Installation Guide

**For:** Hermes Agent (or any AI assistant with terminal/file access)
**Repo:** https://github.com/StefanIsMe/dream-auto
**Version:** 3.0.0

---

## What Is Dream Auto?

Dream Auto is a background-thinking plugin for Hermes Agent. While you work, it runs MCTS-powered reasoning dreams in the background — analyzing past sessions, surfacing insights, catching errors before they repeat. No magic numbers. No hardcoded thresholds. Just resource availability as the gate.

**Key differences in v3:**
- **~5.5x faster per iteration** — MetaRAG calls and rollouts run in parallel, not sequentially
- **UCB1-Tuned selection** — variance-aware exploration that prevents chasing high-variance branches
- **CI-width bonus** — uncertain nodes get extra exploration nudge, scaled by depth
- **Time-based staleness detection** — dreams that spin for 20+ minutes with no tree growth are cut off automatically
- **Wallclock killer** — scheduler kills any dream exceeding 30 minutes, globally
- **SQLite indexes** on all frequently-queried columns — queue drains and session sorts are O(log n) not O(n)

---

## Agent Task: Install Dream Auto Plugin

Execute the steps below sequentially. Stop and report if any step fails.

---

## STEP 0: Prerequisites Check

```bash
python3 --version  # Must be 3.10+
hermes --version   # Hermes CLI must be installed and in PATH
pip3 --version    # pip must be available
```

Verify Hermes gateway is running (required for cron jobs):

```bash
hermes gateway status
```

If not running:

```bash
hermes gateway install
hermes gateway start
```

---

## STEP 1: Clone the Repository

```bash
cd ~
git clone https://github.com/StefanIsMe/dream-auto.git
```

---

## STEP 2: Install Python Dependencies

```bash
cd ~/dream-auto
pip3 install --user -r requirements.txt
```

Required packages: `psutil>=5.9.0`, `rich>=13.0.0`

If psutil fails to compile, install Python dev headers first:
- Fedora/RHEL: `sudo dnf install python3-devel`
- Debian/Ubuntu: `sudo apt install python3-dev`
- macOS: `xcode-select --install`

---

## STEP 3: Install Plugin Files

Copy these directories from `~/dream-auto/` to `~/.hermes/`:

| Source (in repo) | Destination (on machine) |
|---|---|
| `plugins/dream_auto/` | `~/.hermes/plugins/dream_auto/` |
| `scripts/dream_scheduler.py` | `~/.hermes/scripts/dream_scheduler.py` |
| `scripts/dream_insights_dashboard.py` | `~/.hermes/scripts/dream_insights_dashboard.py` |
| `scripts/session_indexer.py` | `~/.hermes/scripts/session_indexer.py` |
| `scripts/session_grader.py` | `~/.hermes/scripts/session_grader.py` |
| `skills/autonomous-ai-agents/hermes-dream-task/` | `~/.hermes/skills/autonomous-ai-agents/hermes-dream-task/` |
| `skills/ops/dream-system-v3/` | `~/.hermes/skills/ops/dream-system-v3/` |

Use `cp -r` for directories, `cp` for individual files. Create parent directories if they don't exist.

---

## STEP 4: Initialize State Databases

Create the state directory and SQLite databases:

```bash
mkdir -p ~/.hermes/state/dream/logs
```

Create `~/.hermes/state/dream/session_index.db` with this schema:

```sql
CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    created_at       TEXT,
    last_message_at  TEXT,
    message_count   INTEGER DEFAULT 0,
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
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    indexed_at     TEXT,
    session_count  INTEGER,
    errors         INTEGER
);

-- Performance indexes (v3)
CREATE INDEX IF NOT EXISTS idx_sessions_dream_potential ON sessions(dream_potential DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_had_errors     ON sessions(had_errors);
CREATE INDEX IF NOT EXISTS idx_sessions_last_dreamed  ON sessions(last_dreamed_at);
```

Create `~/.hermes/state/dream/dream_queue.db` with this schema:

```sql
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

-- Performance indexes (v3)
CREATE INDEX IF NOT EXISTS idx_queue_status_priority ON dream_queue(status, priority DESC);
CREATE INDEX IF NOT EXISTS idx_queue_dream_id       ON dream_queue(dream_id);
```

Create `~/.hermes/state/dream/knowledge_cache.db` with this schema:

```sql
CREATE TABLE IF NOT EXISTS knowledge_cache (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    topic          TEXT,
    content        TEXT,
    source         TEXT,
    cached_at      TEXT,
    content_hash   TEXT UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_topic_cached ON knowledge_cache(topic, cached_at DESC);
CREATE INDEX IF NOT EXISTS idx_cached       ON knowledge_cache(cached_at DESC);
```

You can create all three via Python:

```python
import sqlite3, os

STATE = os.path.expanduser("~/.hermes/state/dream")
os.makedirs(STATE, exist_ok=True)
os.makedirs(os.path.join(STATE, "logs"), exist_ok=True)

DB_SCHEMAS = {
    "session_index.db": """
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
        CREATE INDEX IF NOT EXISTS idx_sessions_dream_potential ON sessions(dream_potential DESC);
        CREATE INDEX IF NOT EXISTS idx_sessions_had_errors     ON sessions(had_errors);
        CREATE INDEX IF NOT EXISTS idx_sessions_last_dreamed  ON sessions(last_dreamed_at);
    """,
    "dream_queue.db": """
        CREATE TABLE IF NOT EXISTS dream_queue (
            queue_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
            dream_id TEXT UNIQUE, dream_question TEXT, grade REAL,
            resource_cost INTEGER DEFAULT 1, priority REAL, created_at TEXT,
            started_at TEXT, completed_at TEXT, status TEXT DEFAULT 'queued'
        );
        CREATE INDEX IF NOT EXISTS idx_queue_status_priority ON dream_queue(status, priority DESC);
        CREATE INDEX IF NOT EXISTS idx_queue_dream_id       ON dream_queue(dream_id);
    """,
    "knowledge_cache.db": """
        CREATE TABLE IF NOT EXISTS knowledge_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT, content TEXT,
            source TEXT, cached_at TEXT, content_hash TEXT UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_topic_cached ON knowledge_cache(topic, cached_at DESC);
        CREATE INDEX IF NOT EXISTS idx_cached       ON knowledge_cache(cached_at DESC);
    """,
}

for db_name, schema in DB_SCHEMAS.items():
    conn = sqlite3.connect(os.path.join(STATE, db_name))
    conn.executescript(schema)
    conn.commit()
    conn.close()
    print(f"Created: {db_name}")
```

---

## STEP 5: Create Dashboard Wrapper

Create the file `~/.local/bin/dream-dashboard` with this content:

```bash
#!/usr/bin/env bash
exec python3 "$HOME/.hermes/scripts/dream_insights_dashboard.py" "$@"
```

Then make it executable:

```bash
chmod +x ~/.local/bin/dream-dashboard
```

Ensure `~/.local/bin` is in your PATH.

---

## STEP 6: Register Cron Jobs

Use the Hermes cron system to register two jobs:

**Job 1: Dream Scheduler** (every 30 minutes — adaptive interval, LLM decides actual check frequency)

```bash
hermes cron create \
  --name "dream-scheduler" \
  --schedule "*/30 * * * *" \
  --command "python3 $HOME/.hermes/scripts/dream_scheduler.py"
```

**Job 2: Session Indexer** (every 6 hours)

```bash
hermes cron create \
  --name "session-indexer" \
  --schedule "0 */6 * * *" \
  --command "python3 $HOME/.hermes/scripts/session_indexer.py"
```

If a job with the same name already exists, skip creation and report it.

---

## STEP 7: Set Environment Variables

Add these to your shell profile (`~/.bashrc`, `~/.zshrc`, or equivalent):

```bash
export DREAM_AUTO_ENABLED=1
export DREAM_AUTO_VERBOSE=0
export DREAM_AUTO_MAX_INJECT=3
```

Reload the profile or export them in the current session.

---

## STEP 8: Run Initial Session Index

```bash
python3 ~/.hermes/scripts/session_indexer.py --limit 50
```

This populates `session_index.db` with your recent sessions and grades them for dream potential.

---

## STEP 9: Verify Installation

Run these checks and report the results:

```bash
# 1. Plugin files exist
ls ~/.hermes/plugins/dream_auto/__init__.py
ls ~/.hermes/plugins/dream_auto/resource_monitor.py
ls ~/.hermes/plugins/dream_auto/plugin.yaml

# 2. Scripts exist
ls ~/.hermes/scripts/dream_scheduler.py
ls ~/.hermes/scripts/dream_insights_dashboard.py
ls ~/.hermes/scripts/session_indexer.py
ls ~/.hermes/scripts/session_grader.py

# 3. Skills exist
ls ~/.hermes/skills/autonomous-ai-agents/hermes-dream-task/scripts/dream_loop_v3.py
ls ~/.hermes/skills/autonomous-ai-agents/hermes-dream-task/scripts/fast_path.py
ls ~/.hermes/skills/ops/dream-system-v3/SKILL.md

# 4. Databases exist
ls ~/.hermes/state/dream/session_index.db
ls ~/.hermes/state/dream/dream_queue.db
ls ~/.hermes/state/dream/knowledge_cache.db

# 5. Dashboard works
dream-dashboard --dry-run 2>/dev/null || python3 ~/.hermes/scripts/dream_insights_dashboard.py --dry-run

# 6. Cron jobs registered
hermes cron list
```

All checks must pass. If any fail, diagnose and fix before reporting completion.

---

## STEP 10: Report Completion

Tell the user:
- "Dream Auto plugin v3.0.0 installed successfully."
- List what was installed (plugin, scripts, skills, databases, cron jobs)
- Remind them: dreams will start running automatically when resources are free
- Tell them to run `dream-dashboard` anytime to check status

---

## One-Line Install Prompt (Copy-Paste for Users)

Give this exact prompt to any Hermes Agent user. They paste it into their chat:

```
Go to https://github.com/StefanIsMe/dream-auto/blob/main/SETUP.md and follow the agent installation steps to install the Dream Auto v3 background-thinking plugin on this machine. Execute all steps sequentially and report back what was installed.
```

---

## Files in This Distribution

```
dream-auto/
├── install.py              # Alternative: run this instead of manual steps
├── SETUP.md                # This file — agent-executable instructions
├── README.md               # Human-readable overview
├── requirements.txt        # psutil, rich
├── plugins/
│   └── dream_auto/        # Plugin source (__init__.py, resource_monitor.py, plugin.yaml)
├── scripts/
│   ├── dream_scheduler.py       # Adaptive queue manager + wallclock killer
│   ├── dream_insights_dashboard.py  # Rich CLI dashboard
│   ├── session_indexer.py       # Session scanner + grader
│   └── session_grader.py       # LLM-based potential scorer
└── skills/
    ├── autonomous-ai-agents/hermes-dream-task/
    │   ├── scripts/dream_loop_v3.py  # MCTS engine v3 (parallelized, UCB1-Tuned)
    │   ├── scripts/fast_path.py      # Heuristic分流 for simple queries
    │   └── SKILL.md                 # Task skill reference
    └── ops/dream-system-v3/
        └── SKILL.md                 # Full implementation reference
```

---

## Troubleshooting (Agent Reference)

| Failure | Fix |
|---|---|
| `hermes: command not found` | Hermes Agent is not installed or not in PATH. Stop installation. |
| `pip3: command not found` | Python pip not installed. Stop installation. |
| `psutil` install fails | Missing Python headers. Run `sudo apt install python3-dev` (Debian/Ubuntu) or `sudo dnf install python3-devel` (Fedora) or `xcode-select --install` (macOS). |
| `dream-dashboard` not found | `~/.local/bin` is not in PATH. Add it or use full path to script. |
| Cron create fails | `hermes gateway` may not be running. Start it with `hermes gateway start`. |
| Plugin files missing after copy | Check that `~/.hermes/` directory exists and is writable. |
| Database locked | Another process is using the SQLite DB. Wait and retry. |
| Dreams never start | Check resources: `python3 ~/.hermes/plugins/dream_auto/resource_monitor.py` |
| Session index empty after install | Run `python3 ~/.hermes/scripts/session_indexer.py --limit 50` manually to populate the DB |

---

## Platform Notes

- **Linux:** Fully supported. Uses `hermes cron` (systemd user service).
- **macOS:** Fully supported. Uses `hermes cron` (launchd).
- **Windows WSL:** Supported inside WSL only. Install Hermes inside WSL.
- **Windows Native:** Not supported. Use WSL2.
