---
name: dream-system-v3
description: "Dream System v3 — MCTS background thinking engine. Scripts: session_indexer.py, session_grader.py, dream_loop_v3.py, fast_path.py, resource_monitor.py, dream_scheduler.py. See state/dream/ for DBs."
tags: "background-thinking,mcts,dream,scheduler"
updated-on: "2026-04-28"
---

# Dream System v3 — Implementation Reference

**Date:** 2026-04-27
**Status:** FULLY OPTIMIZED — all known improvements implemented

## Holographic Memory Context

Dream System v3 operates within the Hermes holographic memory architecture:

- **`fact_store`** — entity-level store (concepts, agents, sessions). Granular, structured recall.
- **`memory`** — document-level store (full content, summaries). Chunks of knowledge.

Dream insights (from distillation) flow into `memory` via `holographic_auto` plugin hooks. The dream loop reads session context from `fact_store` and writes distilled insights back through `holographic_auto`'s 7-hook pipeline (`pre_tool_call`, `post_tool_call`, `on_llm_call`, `pre_llm_call`, `post_llm_call`, `on_context`, `on_user_message`).

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
| `scripts/dream_scheduler.py` | Adaptive queue manager + spawner. Rule-based concurrency (CPU/RAM) and cadence (queue depth). Detects dream completion and syncs queue DB. |
| `state/dream/SYSTEM_PAUSE` | Flag file — running dreams wait, scheduler skips when present |
| `state/dream/session_index.db` | Indexed sessions with grades |
| `state/dream/dream_queue.db` | Queued dreams (created at runtime) |

## Cron Jobs

| Job | Schedule | Purpose |
|-----|----------|---------|
| `dream-scheduler` (c1ca480f0de4) | Every 30min (base, adaptive 2–60 min via rule-based sleep) | Check resources → start queued dreams. Syncs queue DB for completed dreams. |
| `session-indexer` (8c86be7295a1) | Every 6h | Index new sessions + grade them |

## Resource Governance (Three Layers)

The system uses adaptive, rule-based resource management — physical safety caps + heuristic thresholds for concurrency and cadence. LLM only used for session grading (session_grader.py), not scheduler decisions.

### Layer 1: Rule-Based Concurrency (Scheduler)
Every cycle, the scheduler uses a heuristic: CPU free ≥ 50% AND RAM free ≥ 30% → recommend 0-3 additional concurrent dreams based on load magnitude. No LLM call.
- Hard safety caps: **max 5 concurrent**, stops at CPU ≥ 90% or RAM ≥ 95%
- Fills slots from queue first, then from highest-potential sessions

### Layer 2: Self-Throttling (Running Dreams)
Each dream checks `psutil` between every MCTS iteration:
- CPU ≥ 90% or RAM ≥ 95% → sleeps **120 seconds**
- CPU ≥ 75% or RAM ≥ 85% → sleeps **90 seconds**
- CPU ≥ 50% or RAM ≥ 70% → sleeps normal **60 seconds**
- Low load → sleeps **10–30 seconds** (turbo mode)
- Dreams also check for `SYSTEM_PAUSE` flag file

### Layer 3: Adaptive Daemon Cadence (Rule-Based)
After every cycle, the daemon uses a rule-based sleep: 2 min (queue backed up 100+, CPU/RAM OK) → 5 min (queue 100+, moderate load) → 10 min (queue 10+) → 60 min (queue empty). No LLM call.
- Falls back to base interval if resource check fails

### Emergency Pause
Create a pause flag to make all running dreams wait and the scheduler skip:
```bash
touch ~/.hermes/state/dream/SYSTEM_PAUSE   # pause all dreams
rm ~/.hermes/state/dream/SYSTEM_PAUSE      # resume
```

## Key Design Decisions

- **Holographic dual stores** — fact_store (entity) vs memory (document). Dream insights land in memory via holographic_auto's 7-hook pipeline.
- **Plugin hook composability** — Three confirmed hooks for insight injection: `pre_llm_call`, `on_llm_call`, `post_llm_call`. Configurable via `holographic_auto` plugin flags.
- **No hardcoded thresholds** — heuristic rules for scheduler decisions
- **Resource availability is ONLY gate** — scheduler runs dreams when CPU/RAM free
- **SwiftSage fast path** — pure heuristics, zero LLM latency for simple queries
- **Monte Carlo for decisions** — MCTS for complex multi-branch reasoning
- **Error-triggered dreams** — post_tool_call errors → queue immediately, no entropy gate

## Performance Optimization (2026-04-27)

**Result: ~5.5× per-iteration speedup — 840s → ~150s**

### MetaRAG Parallelization
`dream_loop_v3.py` lines ~683-690 had a critical bug: ThreadPoolExecutor submitted 3 futures, then called `.result()` on each sequentially. This is sequential execution, not parallel. Fixed by using `concurrent.futures.wait()` with `ALL_COMPLETED` before collecting results.

```python
# WRONG (sequential):
f_monitor = executor.submit(metarag_monitor, state)
f_evaluate = executor.submit(metarag_evaluate, state, alternatives)
f_plan = executor.submit(metarag_plan, state)
monitor = f_monitor.result()    # blocks
eval_result = f_evaluate.result()   # then this
plan = f_plan.result()          # then this = 180s total

# RIGHT (parallel):
wait([f_monitor, f_evaluate, f_plan], return_when=ALL_COMPLETED)
monitor = f_monitor.result()
eval_result = f_evaluate.result()
plan = f_plan.result()
```

### Rollout Parallelization
Sequential rollouts: 6 rollouts × ~90s = ~540s. Fixed with ThreadPoolExecutor — rollouts run in parallel, backprop runs sequentially (tree dict is shared, not thread-safe).

```python
rollout_tasks = [(fid, child_id, child) for child_id in child_ids for ...]
with ThreadPoolExecutor(max_workers=ROLLOUTS_PER_NODE) as rollout_ex:
    for fid, child_id, child in rollout_tasks:
        result = fid.result()  # all run in parallel
        # then sequential backprop
```

### UCB1 Formula
Standard UCB1: `win_rate + sqrt(2 * log(N_parent) / n_child)`. Previous code used non-standard C=1.4 without the `2*` multiplier. Fixed to `C_UCB = 1.414` (sqrt(2)).

### Staleness Detection (Ralph-loop)
Added `detect_staleness()` function: a dream is stale if wallclock > 20min AND no new nodes added in last 8min. Wired into the main loop termination check. Prevents wasting LLM calls on unproductive branches.

```python
def detect_staleness(tree, max_minutes=20, no_progress_minutes=8) -> dict:
    # Returns {"stale": bool, "reason": str}
```

### SQLite Indexes (live DBs, applied 2026-04-27)
Applied to all three DBs:

| DB | Index | Query helped |
|----|-------|-------------|
| `session_index.db` | `idx_sessions_dream_potential` | `ORDER BY dream_potential DESC` |
| `session_index.db` | `idx_sessions_had_errors` | `WHERE had_errors = 1` |
| `session_index.db` | `idx_sessions_last_dreamed` | `WHERE last_dreamed_at IS NULL` |
| `dream_queue.db` | `idx_queue_status_priority` | `WHERE status='queued' ORDER BY priority DESC` |
| `dream_queue.db` | `idx_queue_dream_id` | `WHERE dream_id = ?` (UNIQUE lookup) |
| `knowledge_cache.db` | `idx_topic_cached` | `WHERE topic=? ORDER BY cached_at DESC` |
| `knowledge_cache.db` | `idx_cached` | `ORDER BY cached_at DESC` (unfiltered) |

The `CREATE INDEX IF NOT EXISTS` statements are now in the schema definitions in `session_indexer.py` and `dream_scheduler.py` — they apply to new DBs. Existing DBs were patched directly.

### UCB1-Tuned + CI-Width Bonus (2026-04-27)
`mcts_select()` upgraded from basic UCB1 to UCB1-Tuned with CI-width bonus:

**UCB1-Tuned:** instead of `sqrt(2*log(N)/n)`, uses `min(2*log(N)/n, 1/n + var)` capped by variance. Prevents over-exploring high-variance nodes.

**CI-width bonus:** Wilson score CI (`ci_width`, already computed per node) now adds `0.15 * ci_width * depth_factor * C_adaptive` on top of UCB1. Uncertain nodes (wide CI) get extra exploration nudge, but scaled by depth so deep branches don't go wild.

**Adaptive C:** `C_adaptive = 1.414 * (1 + 1/sqrt(parent_visits))` — starts higher for young trees (aggressive exploration), shrinks as tree matures (exploitation).

### Wallclock Killer — Scheduler Enforcement (2026-04-27)
`sync_dream_status()` in `dream_scheduler.py`: called at the start of every scheduler cycle. Does two jobs in one pass over DREAM_DIR:
1. **Completion detector** — if `meta.json` shows `status=done/completed` but queue DB says `running`, promotes queue DB to `completed`. This is the code path that drains the queue when dreams finish normally.
2. **Wallclock enforcer** — kills any dream running longer than `MAX_DREAM_WALLCLOCK_MINUTES = 30`. Marks `status.txt` as `killed_wallclock`, updates queue DB to `killed_wallclock` status so it doesn't retry.

### SQLite Indexes (live DBs, applied 2026-04-27)

## Pitfalls (discovered through implementation)

**dream_loop_v3.py `psutil` dependency:**
- `import psutil` at line 27 requires the package to be installed in the **venv that executes dream_loop_v3**, not just system Python.
- Install with: `uv pip install --python /home/stefan171/.hermes/hermes-agent/venv/bin/python3 psutil`
- If missing, every dream crashes immediately with `ModuleNotFoundError: No module named 'psutil'` before any MCTS work begins.

**Stale "running" status blocking scheduler:**
- Crashed dreams (or pre-fix completions) leave `status.txt` as `running` and `dream_queue.db` status as `running`.
- The scheduler's `count_running_dreams()` counts these against the hard safety cap (max 5 concurrent).
- Result: queue backs up even though no actual dream processes are alive.
- Fix: `sync_dream_status()` handles this on every cycle — completed dreams (meta.json status=done) get promoted to `completed` in queue DB. Crashed dreams (wallclock exceeded) get marked `killed_wallclock`.

**dream_output.log stdout buffering:**
- The scheduler spawns dreams with `stdout=open(dp / "dream_output.log", "w")` and `start_new_session=True`.
- Python buffers stdout when writing to a file (not a TTY), so `dream_output.log` appears empty until the process exits or the buffer fills.
- To see live progress, either `tail -f` the file or add `flush=True` to print calls in dream_loop_v3.py.

**Old v2 dreams (google_token.json auth):**
- Pre-v3 dreams relied on `~/.hermes/google_token.json` for direct Anthropic API calls.
- That auth path is dead — all v2 dreams fail with `[LLM unavailable — no access token]`.
- v3 uses `hermes chat -q` (subprocess to the local hermes binary) which inherits the active session's auth — no separate token file needed.

**dream_loop_v3.py module-level requirements:**
- `read_meta` and `MAX_CHILDREN_PER_NODE` MUST be defined at module level, NOT inside `if __name__ == "__main__"` block. The script is imported as a module by scheduler.py, so code inside `if __name__` blocks is NOT available.
- `import math` inside a function shadows the module-level `math` namespace. If you add a local `import math` inside a function, subsequent calls to `math.sqrt()` or `math.log()` in OTHER functions will fail with `AttributeError`. Use `_math.sqrt()` / `_math.log()` if you need local math imports.

**resource_monitor.py output format:**
- `hermes chat -q` output is wrapped in box-drawing characters (┌─┐│└─┘). Parse with `json_parse()` from `hermes_tools`, not raw `json.loads()`.

**resource_monitor.py threshold logic:**
- Threshold is CPU free < 80% means CPU usage > 20%. Use `100 - cpu_percent` for "free" comparison.
- If CPU free < threshold AND RAM not critical → still safe to dream (low CPU use = free).

**dream_loop_v3.py self-throttle implementation:**
- `psutil` checks between MCTS iterations are **fast** — no LLM call per iteration. Only the scheduler uses LLM for cadence/concurrency decisions.
- Sleep tiers: 120s (critical), 90s (high), 60s (normal), 10–30s (turbo). Use `time.sleep()` with `psutil.cpu_percent(interval=1)` and `psutil.virtual_memory().percent`.
- `SYSTEM_PAUSE` check: `os.path.exists("~/.hermes/state/dream/SYSTEM_PAUSE")` — if present, loop sleeps 30s and re-checks. Non-blocking pause.

**dream_scheduler.py adaptive cadence:**
- Rule-based: check CPU/RAM + queue depth to decide sleep seconds (2–60 min range).
- Sleep tiers: 2 min (queue 100+, CPU/RAM OK), 5 min (queue 100+, moderate load), 10 min (queue 10+), 60 min (queue empty).
- Cron still fires every 30 min, but the daemon's internal sleep may be shorter/longer. The cron ensures the daemon stays alive.

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
- Want to see what value dreams are producing? Run `dream-dashboard --insights` to surface actual insight text from recent successful dreams, categorized by type (Debug/Ops, Architecture, Data/DB)

## Insight Extraction Pattern

The dashboard now extracts and displays real insight content from successful dreams (not just counts). Key design:
- Parses both v2 (`insights.json`) and v3 (`dream_output.log` JSON block) output formats
- Categorizes insights heuristically by keyword: debug/ops, architecture, data/db
- Shows 15 most recent insights from last 7 days
- Proves plugin value by surfacing concrete knowledge produced (e.g. "org2.db stub-vs-real path confusion", "CDP zombie process pattern", "subprocess pipe buffer deadlock")
- New CLI flag: `--insights`

This answers the question: "Is this dream plugin actually producing useful knowledge?" with evidence, not just activity metrics.

## Config Vars (plugin)

```
DREAM_AUTO_ENABLED=1       — disable entirely
DREAM_AUTO_VERBOSE=1      — log activity  
DREAM_AUTO_MAX_INJECT=3    — max dreams injected per turn
```

Removed from v2: `DREAM_AUTO_AUTOSTART`, `DREAM_AUTO_MIN_COMPLEXITY`
