# Dream Auto Plugin — Setup Guide

**Version:** 1.0.0  
**Platforms:** Linux, macOS, Windows (WSL)  
**Prerequisite:** [Hermes Agent](https://hermes-agent.nousresearch.com/) must be installed.

---

## What This Is

Dream Auto is a **background thinking plugin** for Hermes Agent. It runs autonomously to:

- **Index your sessions** — scans conversation history for topics, errors, and open questions
- **Grade sessions** — uses an LLM to score which past conversations have "dream potential" (0.0–1.0)
- **Queue background dreams** — when CPU/RAM are free, it spawns MCTS-powered reasoning jobs that explore complex topics from your sessions
- **Inject insights** — distilled findings from completed dreams are silently added to your active session context
- **Error-triggered analysis** — when a tool call crashes, it automatically queues a troubleshooting dream

Think of it as a second brain that thinks about your work while you're not looking.

---

## Prerequisites

Before installing, verify you have:

| Requirement | Check Command | Notes |
|---|---|---|
| Python 3.10+ | `python3 --version` | Required for match/case and modern stdlib |
| Hermes Agent CLI | `hermes --version` | Must be in PATH |
| pip | `pip3 --version` | For installing `psutil` and `rich` |
| Linux / macOS / WSL | `uname -a` | Native Windows is **not supported** |

**Hermes gateway must be running** for cron jobs to work:

```bash
hermes gateway install
hermes gateway start
```

---

## Quick Start (30 seconds)

### Option A: Run in Hermes Agent (Recommended)

Paste the following into your Hermes chat session:

```bash
# 1. Navigate to the extracted distribution folder
cd /path/to/dream-auto-dist

# 2. Run the installer
python3 install.py
```

Hermes will execute the installer, which:
1. Checks prerequisites
2. Installs Python dependencies (`psutil`, `rich`)
3. Copies plugin + skill files to `~/.hermes/`
4. Creates SQLite databases
5. Registers cron jobs (`dream-scheduler` every 30 min, `session-indexer` every 6 hours)
6. Runs an initial session index
7. Verifies everything is in place

### Option B: Run from Terminal

```bash
cd dream-auto-dist
python3 install.py
```

---

## What Gets Installed

```
~/.hermes/
├── plugins/dream_auto/               # Plugin hooks (insight injection, error capture)
│   ├── __init__.py
│   ├── resource_monitor.py
│   └── plugin.yaml
├── scripts/
│   ├── dream_scheduler.py            # Queue manager + dream spawner (cron)
│   ├── dream_insights_dashboard.py   # CLI dashboard (run: dream-dashboard)
│   ├── session_indexer.py            # Scans sessions → session_index.db (cron)
│   └── session_grader.py             # LLM grades sessions for dream potential
├── skills/autonomous-ai-agents/hermes-dream-task/
│   ├── scripts/dream_loop_v3.py      # MCTS reasoning engine
│   ├── scripts/fast_path.py          # Heuristic skip for simple queries
│   └── SKILL.md
├── skills/ops/dream-system-v3/
│   └── SKILL.md                      # Full implementation reference
└── state/dream/
    ├── session_index.db              # Indexed sessions with grades
    ├── dream_queue.db                # Queued dreams
    └── logs/                         # Dream output logs
```

---

## Post-Install Verification

Run these commands to confirm everything is working:

```bash
# Check plugin is registered
ls ~/.hermes/plugins/dream_auto/

# Check databases exist
ls ~/.hermes/state/dream/*.db

# Check cron jobs
hermes cron list

# View the dashboard
dream-dashboard

# Dry-run the scheduler (shows what it would do without starting anything)
python3 ~/.hermes/scripts/dream_scheduler.py --dry-run

# Manually index sessions
python3 ~/.hermes/scripts/session_indexer.py --limit 20
```

---

## Configuration

Control the plugin with environment variables (set in your shell profile or Hermes config):

| Variable | Default | Description |
|---|---|---|
| `DREAM_AUTO_ENABLED` | `1` | Set to `0` to disable the plugin entirely |
| `DREAM_AUTO_VERBOSE` | `0` | Set to `1` for detailed logging |
| `DREAM_AUTO_MAX_INJECT` | `3` | Max dream insights injected per turn |

**Example** (add to `~/.bashrc` or `~/.zshrc`):

```bash
export DREAM_AUTO_ENABLED=1
export DREAM_AUTO_VERBOSE=1
```

---

## Platform Notes

### Linux (Fedora, Ubuntu, Debian, Arch)
- Fully supported.
- Cron jobs use `hermes cron` → systemd user service.
- Make sure `hermes gateway` is running.

### macOS
- Fully supported.
- `hermes cron` uses `launchd` under the hood.
- If `hermes` is installed via Homebrew, the PATH may differ — the installer auto-detects this.

### Windows (WSL2)
- Supported inside WSL only.
- Install Hermes Agent inside your WSL distribution, not Windows native.
- The dashboard and all scripts run inside WSL.

### Native Windows (NOT supported)
- The plugin relies on POSIX paths, shell wrappers, and `hermes chat -q` subprocess behavior that are not tested on native Windows.
- Use WSL2 instead.

---

## How It Works Day-to-Day

1. **You chat with Hermes** normally.
2. **dream_auto** (via plugin hooks) notices errors or complex questions and queues them.
3. **session-indexer** (every 6h) scans your recent sessions and grades them.
4. **dream-scheduler** (every 30min) checks CPU/RAM. If free, it starts the highest-priority dream.
5. **dream_loop_v3.py** runs MCTS reasoning in a background subprocess, writing insights to `state/dream/<id>/`.
6. On your **next message**, the plugin injects the best insights from completed dreams into your context.

You don't interact with it directly — it just makes Hermes smarter over time.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `hermes: command not found` during install | Add Hermes to your PATH first. |
| `psutil` install fails | `sudo apt install python3-dev` (Linux) or `xcode-select --install` (macOS) |
| Cron jobs not running | `hermes gateway status` → if down, run `hermes gateway start` |
| Dashboard shows empty | Run `session_indexer.py` manually once to populate the DB |
| Dreams never start | Check resources: `python3 ~/.hermes/plugins/dream_auto/resource_monitor.py` |
| Plugin not loading | Verify `DREAM_AUTO_ENABLED=1` and restart Hermes |

---

## Files in This Distribution

```
dream-auto-dist/
├── install.py              # Cross-platform installer (run this)
├── requirements.txt        # Python dependencies
├── SETUP.md               # This file
├── plugins/
│   └── dream_auto/        # Plugin source
├── scripts/               # Scheduler, dashboard, indexer
└── skills/                # MCTS engine + documentation
```

To share this plugin with someone else, zip the entire `dream-auto-dist/` folder and send it. They only need to run `python3 install.py`.

---

## Credits

- Built for [Hermes Agent](https://hermes-agent.nousresearch.com/) by Stefan Carter.
- MCTS engine inspired by SwiftSage + Monte Carlo Tree Search literature.
- Version 3 removes all hardcoded thresholds — resource availability is the only gate.
