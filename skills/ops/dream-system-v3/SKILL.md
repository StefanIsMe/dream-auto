---
name: dream-system-v3
description: "Dream System v3 — MCTS background thinking engine. Scripts: session_indexer.py, session_grader.py, dream_loop_v3.py, fast_path.py, resource_monitor.py, dream_scheduler.py. See state/dream/ for DBs."
tags: "background-thinking,mcts,dream,scheduler"
updated-on: "2026-04-23"
---

# Dream System v3 — Implementation Reference

**Date:** 2026-04-23
**Status:** IMPLEMENTED

## Architecture

```
SESSION INDEXER (session_indexer.py)
  → session_index.db (SQLite)
SESSION GRADER (session_grader.py, hermes chat -q)
  → dream_potential 0.0-1.0 per session
  ↓
DREAM QUEUE (dream_queue.db)
  ↓
RESOURCE MONITOR (resource_monitor.py, psutil + LLM)
  ↓ (CPU < 80%, RAM < 90%)
MONTE CARLO DREAM ENGINE (dream_loop_v3.py, MCTS)
  - SELECT: UCB1 traverse
  - EXPAND: generate branches
  - ROLLOUT: N simulations per branch (3 rollouts)
  - BACKPROPAGATE: Wilson CI confidence intervals
  ↓
MetaRAG: Monitor → Evaluate → Plan (per iteration)
  ↓
DISTILLATION: 5 runs → consensus insights
  ↓
INSIGHTS → injected on pre_llm_call
```

## Files

| File | Purpose |
|------|---------|
| `skills/.../scripts/session_indexer.py` | Scan sessions → session_index.db |
| `skills/.../scripts/session_grader.py` | LLM grade per session (potential 0-1) |
| `skills/.../scripts/dream_loop_v3.py` | MCTS engine, MetaRAG, distillation |
| `skills/.../scripts/fast_path.py` | Heuristic分流 (no LLM) for simple queries |
| `plugins/dream_auto/resource_monitor.py` | CPU/RAM check + LLM fallback |
| `plugins/dream_auto/__init__.py` | Plugin v3: insight injection + error→queue |
| `scripts/dream_scheduler.py` | Queue manager + spawner (every 30min) |
| `state/dream/session_index.db` | Indexed sessions with grades |
| `state/dream/dream_queue.db` | Queued dreams (created at runtime) |

## Cron Jobs

| Job | Schedule | Purpose |
|-----|----------|---------|
| `dream-scheduler` (c1ca480f0de4) | Every 30min | Check resources → start queued dreams |
| `session-indexer` (8c86be7295a1) | Every 6h | Index new sessions + grade them |

## Key Design Decisions

- **No hardcoded thresholds** — LLM decides when ambiguous
- **Resource availability is ONLY gate** — scheduler runs dreams when CPU/RAM free
- **SwiftSage fast path** — pure heuristics, zero LLM latency for simple queries
- **Monte Carlo for decisions** — MCTS for complex multi-branch reasoning
- **Error-triggered dreams** — post_tool_call errors → queue immediately, no entropy gate

## Pitfalls (discovered through implementation)

**dream_loop_v3.py module-level requirements:**
- `read_meta` and `MAX_CHILDREN_PER_NODE` MUST be defined at module level, NOT inside `if __name__ == "__main__"` block. The script is imported as a module by scheduler.py, so code inside `if __name__` blocks is NOT available.
- `import math` inside a function shadows the module-level `math` namespace. If you add a local `import math` inside a function, subsequent calls to `math.sqrt()` or `math.log()` in OTHER functions will fail with `AttributeError`. Use `_math.sqrt()` / `_math.log()` if you need local math imports.

**resource_monitor.py output format:**
- `hermes chat -q` output is wrapped in box-drawing characters (┌─┐│└─┘). Parse with `json_parse()` from `hermes_tools`, not raw `json.loads()`.

**resource_monitor.py threshold logic:**
- Threshold is CPU free < 80% means CPU usage > 20%. Use `100 - cpu_percent` for "free" comparison.
- If CPU free < threshold AND RAM not critical → still safe to dream (low CPU use = free).

**dream_queue.db location:**
- Created at runtime in `state/dream/`. Must exist before scheduler can enqueue.

**session_indexer.py first run:**
- Creates `session_index.db` from scratch. Safe to re-run anytime.

## Monitoring — CLI Dashboard

A `rich`-based terminal dashboard is available for live state inspection:

```bash
dream-dashboard              # full dashboard
dream-dashboard --runs       # dream runs only
dream-dashboard --queue      # dream queue only
dream-dashboard --sessions   # session index only
dream-dashboard --errors     # error breakdown only
```

**Script:** `~/.hermes/scripts/dream_insights_dashboard.py`
**Wrapper:** `~/.local/bin/dream-dashboard`

### Data sources the dashboard scans

| Source | What it contains | Parsed by |
|--------|------------------|-----------|
| `state/dream/<id>/meta.json` | v2 confidence, status, pending questions | v2 parser |
| `state/dream/<id>/status.txt` | v2 status text | v2 parser |
| `state/dream/<id>/insights.json` | v2 insights list | v2 parser |
| `state/dream/<id>/failures.json` | v2 failures list | v2 parser |
| `state/dream/<id>/dream_output.log` | v3 MCTS output + JSON result block | v3 parser |
| `state/dream/logs/<id>.log` | Legacy log files | log parser |
| `state/dream/session_index.db` | Sessions table with dream_potential | SQLite |
| `state/dream/dream_queue.db` | Queued dreams with grade/priority/status | SQLite |

### v2 status normalization

Raw v2 statuses are normalized for display:
- `success` ← `completed`, `completed_success`, `done`
- `failed` ← `failed`, `failed_crash`, `failed_restart`, `circuit_breaker`, `completed_killed`, `health_check_failed`
- `stale` ← `completed_stale`, `stale_completed`, `completed_empty`
- `running`, `queued` pass through

### When to use

- Queue backed up? Run `dream-dashboard --queue` to see grade/priority of queued items
- Dreams crashing? Run `dream-dashboard --errors` for grouped traceback summary
- High-potential sessions not being dreamed? Check `dream-dashboard --sessions` for `last_dreamed_at`

## Config Vars (plugin)

```
DREAM_AUTO_ENABLED=1       — disable entirely
DREAM_AUTO_VERBOSE=1      — log activity  
DREAM_AUTO_MAX_INJECT=3    — max dreams injected per turn
```

Removed from v2: `DREAM_AUTO_AUTOSTART`, `DREAM_AUTO_MIN_COMPLEXITY`
