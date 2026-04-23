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
| **MCTS Dream Engine (v3)** | Uses Monte Carlo Tree Search to explore multiple reasoning branches, not just one. More thorough, less shallow. |
| **Zero Hardcoded Thresholds** | No magic numbers. Resource availability is the only gate. The LLM decides everything else. |
| **Error → Dream Pipeline** | Tool crashes automatically trigger troubleshooting dreams. Fixed once, remembered forever. |
| **Resource-Aware Scheduling** | Dreams only run when your machine is free. Never slows down active work. |
| **Rich CLI Dashboard** | `dream-dashboard` shows live stats, recent insights, queue status, and session grades. |
| **Cross-Platform** | Works on Linux, macOS, and Windows WSL. Auto-detects your Hermes installation. |

---

## Architecture at a Glance

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│ Session Indexer │────▶│ Session Grader  │────▶│  Dream Queue    │
│  (every 6h)     │     │  (LLM scores)   │     │  (SQLite)       │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                                                       │
                                                       ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Insight        │◀────│  MCTS Dream     │◀────│  Scheduler      │
│  Injection      │     │  Engine (v3)    │     │  (every 30min)  │
│  (pre_llm_call) │     │                 │     │                 │
└─────────────────┘     └─────────────────┘     └─────────────────┘
        ▲                                               │
        └───────────────────────────────────────────────┘
                    (injects into active sessions)
```

- **Session Indexer** scans `~/.hermes/sessions/` for transcripts, extracts topics, errors, and open questions
- **Session Grader** asks an LLM: "How much would dreaming about this session help future conversations?"
- **Scheduler** checks resources and picks the highest-priority queued dream
- **MCTS Engine** runs Monte Carlo Tree Search with UCB1 selection, branch expansion, rollouts, and Wilson confidence intervals
- **Insight Injection** silently prepends the best findings from completed dreams into your active context

---

## Prerequisites

| Requirement | Check | Notes |
|---|---|---|
| Python 3.10+ | `python3 --version` | Required for modern syntax |
| Hermes Agent CLI | `hermes --version` | Must be installed and in PATH |
| pip | `pip3 --version` | For `psutil` and `rich` |
| Linux / macOS / WSL | `uname -a` | Native Windows not supported |

Hermes gateway must be running for cron jobs to work:

```bash
hermes gateway install
hermes gateway start
```

---

## Installation

### One-Liner (Recommended)

```bash
git clone https://github.com/StefanIsMe/dream-auto.git
cd dream-auto
python3 install.py
```

### What the Installer Does

1. Checks Python 3.10+, Hermes CLI, and pip
2. Installs Python dependencies (`psutil`, `rich`)
3. Copies plugin + scripts + skills to `~/.hermes/`
4. Creates SQLite databases (`session_index.db`, `dream_queue.db`)
5. Registers two cron jobs:
   - `dream-scheduler` — every 30 minutes
   - `session-indexer` — every 6 hours
6. Creates the `dream-dashboard` CLI wrapper
7. Runs an initial session index
8. Prints a verification report

### Dry Run (Preview Without Installing)

```bash
python3 install.py --dry-run
```

---

## Usage

### Dashboard

```bash
dream-dashboard              # full overview
dream-dashboard --insights   # recent insights only
dream-dashboard --queue      # dream queue only
dream-dashboard --errors     # error breakdown
```

### Manual Scheduler Check

```bash
python3 ~/.hermes/scripts/dream_scheduler.py --dry-run
```

### Manual Session Index

```bash
python3 ~/.hermes/scripts/session_indexer.py --limit 50
```

### Verify Plugin Is Active

```bash
ls ~/.hermes/plugins/dream_auto/
# Should show: __init__.py  plugin.yaml  resource_monitor.py
```

---

## Configuration

Control the plugin with environment variables:

| Variable | Default | Description |
|---|---|---|
| `DREAM_AUTO_ENABLED` | `1` | Set to `0` to disable entirely |
| `DREAM_AUTO_VERBOSE` | `0` | Set to `1` for detailed logging |
| `DREAM_AUTO_MAX_INJECT` | `3` | Max dream insights injected per turn |

Add to your shell profile (`~/.bashrc`, `~/.zshrc`, etc.):

```bash
export DREAM_AUTO_ENABLED=1
export DREAM_AUTO_VERBOSE=1
```

---

## How It Works Day-to-Day

1. **You chat with Hermes normally.** The plugin listens via 6 hooks (`pre_llm_call`, `pre_tool_call`, `post_tool_call`, `post_llm_call`, `on_session_start`, `on_session_end`).
2. **Errors or complex questions** are detected and queued automatically.
3. **Session indexer** (every 6h) scans recent sessions and grades them for dream potential.
4. **Scheduler** (every 30min) checks CPU/RAM. If resources are free, it starts the highest-priority dream.
5. **MCTS engine** runs background reasoning, exploring multiple branches and distilling insights.
6. **On your next message**, the plugin injects the best insights from completed dreams into your context silently.

You don't interact with it. It just makes Hermes smarter over time.

---

## Platform Notes

| Platform | Status | Notes |
|---|---|---|
| **Linux** | ✅ Full | Tested on Fedora. Uses `hermes cron` → systemd user service. |
| **macOS** | ✅ Full | Uses `hermes cron` → launchd. Auto-detects Hermes path. |
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
| Dreams never start | Check resources: `python3 ~/.hermes/plugins/dream_auto/resource_monitor.py` |
| Plugin not loading | Verify `DREAM_AUTO_ENABLED=1` and restart Hermes |

---

## Files

```
dream-auto/
├── README.md              # This file
├── SETUP.md              # Detailed setup guide
├── install.py            # Cross-platform installer
├── requirements.txt      # Python dependencies
├── plugins/
│   └── dream_auto/       # Plugin source
├── scripts/              # Scheduler, dashboard, indexer, grader
└── skills/               # MCTS engine + documentation
```

---

## Credits

- Built for [Hermes Agent](https://hermes-agent.nousresearch.com/) by [Stefan Carter](https://github.com/StefanIsMe)
- MCTS engine design inspired by SwiftSage and Monte Carlo Tree Search literature
- Version 3 philosophy: *resource availability is the only gate — no hardcoded thresholds*

---

## License

MIT
