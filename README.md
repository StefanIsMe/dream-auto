# Dream Auto — Background Thinking for Hermes Agent

[![Hermes Agent](https://img.shields.io/badge/Built%20for-Hermes%20Agent-blue)](https://hermes-agent.nousresearch.com/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-green)](https://python.org)
[![Platforms](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20WSL-lightgrey)]()
[![License](https://img.shields.io/badge/License-MIT-yellow)]()

> A background-thinking plugin that lets your Hermes agent **dream** while you sleep — analyzing past sessions, surfacing insights, and learning from errors without you asking.

---

## What It Does

Hermes sessions pile up. Errors get fixed and forgotten. Open questions go unanswered. Complex problems never get the deep thinking they deserve because you're already on to the next task.

**Dream Auto fixes that.** It runs in the background to:

- **Index every session** — scans your conversation history for topics, errors, open questions, and complexity signals
- **Grade sessions for "dream potential"** — uses an LLM to score which past conversations (0.0–1.0) are worth thinking about more
- **Spawn background dreams** — when your machine is idle (CPU < 80%, RAM < 90%), it launches MCTS-powered reasoning jobs that explore your hardest sessions
- **Inject insights silently** — distilled findings from completed dreams are added to your active session context automatically. No disruption, just smarter responses
- **Catch errors before they repeat** — when a tool call crashes, it automatically queues a troubleshooting dream so the same bug doesn't bite twice

Think of it as giving your Hermes agent a second brain that thinks about your work while you're not looking.

---

## Highlights

| Feature | What It Means For You |
|---|---|
| **MCTS Dream Engine v3** | Parallelized rollouts + MetaRAG calls. ~5.5x faster per iteration. |
| **UCB1-Tuned Selection** | Variance-aware exploration that prevents chasing high-variance branches. |
| **CI-Width Bonus** | Uncertain nodes get extra exploration nudge, scaled by tree depth. |
| **Staleness Detection** | Dreams spinning without tree growth for 20+ minutes are cut off. |
| **Wallclock Killer** | Scheduler kills any dream exceeding 30 minutes globally. |
| **SQLite Performance Indexes** | Queue drains and session sorts are O(log n), not O(n). |
| **Zero Hardcoded Thresholds** | Heuristic rules for concurrency/cadence. CPU/RAM availability is the only gate. |
| **Error → Dream Pipeline** | Tool crashes automatically trigger troubleshooting dreams. |
| **Rich CLI Dashboard** | `dream-dashboard` shows live stats, insights, queue status, and grades. |

---

## Architecture at a Glance

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│ Session Indexer │────▶│ Session Grader  │────▶│  Dream Queue   │
│   (every 6h)    │     │  (LLM scores)   │     │   (SQLite)     │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                                                       │
                                                       ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Insight        │◀────│  MCTS Dream     │◀────│  Scheduler      │
│  Injection      │     │  Engine v3      │     │  (every 30min)  │
│ (pre_llm_call)  │     │  (parallel)     │     │                 │
└─────────────────┘     └─────────────────┘     └─────────────────┘
        ▲                                               │
        └───────────────────────────────────────────────┘
                    (injects into active sessions)
```

- **Session Indexer** scans `~/.hermes/sessions/` for transcripts, extracts topics, errors, and open questions
- **Session Grader** asks an LLM: "How much would dreaming about this session help future conversations?"
- **Scheduler** checks resources and picks the highest-priority queued dream (with wallclock killer)
- **MCTS Engine v3** runs Monte Carlo Tree Search with UCB1-Tuned selection, parallel rollouts, parallel MetaRAG calls, and Wilson confidence intervals
- **Insight Injection** silently prepends the best findings from completed dreams into your active context

---

## Prerequisites

| Requirement | Check | Notes |
|---|---|---|
| Python 3.10+ | `python3 --version` | Required |
| Hermes Agent CLI | `hermes --version` | Must be installed and in PATH |
| pip | `$HERMES_VENV/bin/python -m pip --version` | Installs into Hermes venv (via `uv pip` fallback) |
| Linux / macOS / WSL | `uname -a` | Native Windows not supported |

Hermes gateway must be running for cron jobs to work:

```bash
hermes gateway install
hermes gateway start
```

---

## Installation

### Let Hermes Install It For You

Copy this prompt and paste it into your Hermes chat:

```
Go to https://github.com/StefanIsMe/dream-auto/blob/main/SETUP.md and follow the setup steps to install Dream Auto v3 on this machine. Execute all steps. Report what was installed.
```

Hermes will clone the repo, copy files, create databases, register cron jobs, and verify.

### Manual / One-Shot Install

```bash
git clone https://github.com/StefanIsMe/dream-auto.git
cd dream-auto
# Then follow the "All-in-One Install / Upgrade Command" block in SETUP.md
```

The SETUP.md contains both the one-line shell command (paste into terminal) and the full step-by-step breakdown for manual or troubleshooting.

### Upgrading

```bash
cd ~/dream-auto
git pull
# Then re-run the copy commands from the upgrade section in SETUP.md
```

The SETUP.md upgrade section has the exact steps. Databases are preserved — only the file copies and DB index additions run.

---

## Usage

### Dashboard

```bash
dream-dashboard              # full overview
dream-dashboard --insights  # recent insights only
dream-dashboard --queue     # dream queue only
dream-dashboard --errors    # error breakdown
dream-dashboard --dry-run   # test without running
```

### Manual Scheduler Check

```bash
$HERMES_PY ~/.hermes/scripts/dream_scheduler.py --dry-run
```

### Manual Session Index

```bash
$HERMES_PY ~/.hermes/scripts/session_indexer.py --limit 50
```

---

## Configuration

Control the plugin with environment variables:

| Variable | Default | Description |
|---|---|---|
| `DREAM_AUTO_ENABLED` | `1` | Set to `0` to disable entirely |
| `DREAM_AUTO_VERBOSE` | `0` | Set to `1` for detailed logging |
| `DREAM_AUTO_MAX_INJECT` | `3` | Max dream insights injected per turn |
| `DREAM_AUTO_THROTTLE_TURNS` | `5` | Fire hook at most every N turns |

Add to your shell profile (`~/.bashrc`, `~/.zshrc`, etc.):

```bash
export DREAM_AUTO_ENABLED=1
export DREAM_AUTO_VERBOSE=1
```

---

## How It Works Day-to-Day

1. **You chat with Hermes normally.** The plugin listens via hooks.
2. **Errors or complex questions** are detected and queued automatically.
3. **Session indexer** (every 6h) scans recent sessions and grades them for dream potential.
4. **Scheduler** (every 30min) checks CPU/RAM. If resources are free, it starts the highest-priority dream.
5. **MCTS engine v3** runs background reasoning with parallel rollouts and MetaRAG calls.
6. **On your next message**, the plugin injects the best insights from completed dreams into your context silently.

You don't interact with it. It just makes Hermes smarter over time.

---

## Platform Notes

| Platform | Status | Notes |
|---|---|---|
| **Linux** | ✅ Full | Tested on Fedora. Uses `hermes cron` → systemd user service. |
| **macOS** | ✅ Full | Uses `hermes cron` → launchd. |
| **Windows WSL** | ✅ Full | Run inside WSL. Install Hermes inside WSL, not Windows native. |
| **Windows Native** | ❌ No | POSIX paths and subprocess behavior not supported. |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `hermes: command not found` | Add Hermes to PATH before installing. |
| `psutil` install fails | `sudo apt install python3-dev` (Linux) or `xcode-select --install` (macOS) |
| Cron jobs not running | `hermes gateway status` → if down, `hermes gateway start` |
| Dashboard shows empty | Run `session_indexer.py` manually once to populate DB |
| Dreams never start | Check resources: `$HERMES_PY ~/.hermes/plugins/dream_auto/resource_monitor.py` |
| Plugin not loading | Verify `DREAM_AUTO_ENABLED=1` and restart Hermes |
| Session index empty after install | Run `$HERMES_PY ~/.hermes/scripts/session_indexer.py --limit 50` manually |

---

## Files

```
dream-auto/
├── README.md              # This file
├── SETUP.md              # Full install/upgrade guide
├── requirements.txt      # Python dependencies
├── plugins/
│   └── dream_auto/     # Plugin source
├── scripts/              # Scheduler, dashboard, indexer, grader
└── skills/               # MCTS engine v3 + documentation
```

---

## Credits

- Built for [Hermes Agent](https://hermes-agent.nousresearch.com/) by [Stefan Carter](https://github.com/StefanIsMe)
- MCTS engine design inspired by SwiftSage and Monte Carlo Tree Search literature
- Version 3: *resource availability is the only gate — no magic numbers, no hardcoded thresholds*

---

## License

MIT
