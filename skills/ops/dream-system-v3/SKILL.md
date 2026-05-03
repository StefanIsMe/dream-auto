---
name: dream-system-v3
description: "Dream System v3 — MCTS background thinking engine. Scripts: session_indexer.py, session_grader.py, dream_loop_v3.py, fast_path.py, resource_monitor.py, dream_scheduler.py. See state/dream/ for DBs."
tags: "background-thinking,mcts,dream,scheduler"
updated-on: "2026-05-03"
---

# Dream System v3 — Implementation Reference

**Date:** 2026-04-27
**Tool-Access Rollouts Implemented (2026-05-03):** MCTS rollouts now have real diagnostic tool access via a two-tier system — see `references/dream-loop-tool-integration.md` (status: IMPLEMENTED, not PROPOSED). Key changes:
- `DreamAgent` class wraps `AIAgent` with toolset support and graceful fallback
- `DreamAgentPool` reuses a single AIAgent instance per dream (cold-start avoidance)
- `rollout_tier1()` — fast LLM-only (~1-2s), always runs first
- `rollout_tier2()` — tool-using AIAgent (~30-120s), on-demand only
- Two-tier decision: Tier-1 confidence < 0.30 AND top UCB1 branch AND system idle (CPU < 70%, RAM < 70%)
- Config: `TOOL_ROLLOUT_THRESHOLD=0.30`, `TOOL_ROLLOUT_BRANCH_LIMIT=1`, `TOOL_CALL_LIMIT=5`, `TOOL_CALL_TIMEOUT=30s`, `TOOL_ROLLOUT_TIMEOUT=120s`
- Portable: reads user's `provider`/`model` from `~/.hermes/config.yaml`, falls back to `OPENROUTER_API_KEY` env var
- AIAgent import path: `sys.path.insert(0, str(Path.home() / ".hermes" / "hermes-agent"))` then `from run_agent import AIAgent`

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
| `scripts/dream_scheduler.py` | Adaptive queue manager + spawner. LLM decides concurrency and cadence |
| `state/dream/SYSTEM_PAUSE` | Flag file — running dreams wait, scheduler skips when present |
| `state/dream/session_index.db` | Indexed sessions with grades |
| `state/dream/dream_queue.db` | Queued dreams (created at runtime) |

**BUG FIX APPLIED (2026-05-02): dream_auto v3.1 plugin gaps — 55 tests pass**

Gaps found and fixed in `~/.hermes/plugins/dream_auto/__init__.py`:

1. **`_distill_insights`** — was using `meta.get("confidence")` but v3 MCTS writes `best_confidence`. Also wasn't including `status` or `started_at` in injected output.
   Fix: `confidence = meta.get("best_confidence", meta.get("confidence", 0.0))`

2. **`_list_completed_dreams`** — only recognized `done`/`completed`. v3 uses `completed_killed`, `failed_crash`, `killed_wallclock`, `completed_stale`, `stale_completed`, `completed_empty`.
   Fix: `_STATUS_DONE = {"done", "completed", "completed_killed", "failed_crash", "killed_wallclock", "completed_stale", "stale_completed", "completed_empty"}`

3. **`_read_pending_questions`** — missing file and invalid JSON both returned `[]`, indistinguishable.
   Fix: explicit file existence check, returns `[]` for both error cases.

4. **`on_session_start`** — was scanning full `DREAM_DIR` (~45ms on 652 dirs) every session start.
   Fix: stripped to just clearing session tracking dicts. Active dream info available via scheduler/dashboard.

5. **`_add_to_queue` duplicate UUID bug** — `str(uuid.uuid4())[:8]` called twice (once for INSERT, once for RETURN). Second call returned wrong ID if INSERT used a different UUID (INSERT OR IGNORE could skip on collision).
   Fix: single `new_dream_id = str(uuid.uuid4())[:8]` variable reused for both.

6. **New: global throttle** — `pre_llm_call` now skips if it fired < `DREAM_AUTO_GLOBAL_THROTTLE` seconds ago (default 300s). Uses `time.monotonic()` sentinel `-300.0` so first call always passes.
   New env var: `DREAM_AUTO_GLOBAL_THROTTLE=300`

7. **New: topic pre-filtering** — `_list_completed_dreams(topic_hints=)` pre-filters dreams by brief keywords before `_distill_insights`, avoiding wasted work on unrelated dreams.

8. **New: queue deduplication** — `_add_to_queue` checks for existing queued/running dream with same `session_id` AND similar brief prefix before inserting. SQLite `LIKE 'prefix%'` match on first 60 chars of `dream_question`.

9. **New: expanded error signals** — added HTTP 429/500/502/503/403/401, CDP timeout patterns (`CDPTimeout`, `WebSocketTimeoutError`, `NavigationTimeout`), `expired`, `session revoked`, `commit failed`, `fatal:`, `Operation not permitted`. With regex extraction for HTTP status code, error type, and host/port.

10. **New: configurable KC TTL** — `DREAM_AUTO_KNOWLEDGE_CACHE_TTL_DAYS=7` env var replaces hardcoded 7-day constant.

11. **New: `_extract_error_context`** — structured error parsing: extracts HTTP status (4xx/5xx), error type, host, port from error string for richer briefs.

## Next PR (pending): Interactive STEP 7 — see `references/interactive-scheduling-design.md`

## Diagnostic Reference
- `references/diagnostic-snapshots.md` — live system state, queue breakdowns, running dream inventory, diagnostic commands
- `references/diagnostics-2026-05-01.md` — 00:00 cycle: zombie patterns 1 & 2, queue stagnation bug, session scoring gap
- `references/diagnostics-2026-05-01b.md` — 04:30 cycle: wallclock kills confirmed working, DB fallback confirmed, dream_loop_v3 started_at gap confirmed
- `references/diagnostics-2026-05-01c.md` — 11:00 cycle: Pattern 3 zombie (complete meta.json but scheduler didn't kill it — timing/bug), manual kill applied, three patterns consolidated
- `references/diagnostics-2026-05-02.md` — 00:00 cycle: SYSTEM_PAUSE left behind Apr29 + wallclock-SYSTEM_PAUSE interaction bug, 6 zombie PIDs manually killed
- `references/dream-dashboard-uiux-notes.md` — Dashboard v2 implementation: Rich TUI panels, sparklines, zombie detection fix, insight truncation fix, session table date overflow fix, sparkline block-char math

## Cron Jobs

**CRITICAL: Verify schedules after running SETUP.md** — The `2>/dev/null || echo "already registered"` pattern means re-running SETUP.md will NOT fix broken or mis-scheduled cron jobs. If jobs exist, the create command silently skips. Always check actual registered schedules:

```bash
hermes cron list | grep -E "dream-scheduler|session-indexer"
```

If schedules are wrong (e.g. `22,52 * * * *` instead of `*/30 * * * *`), remove and recreate manually:
```bash
hermes cron remove <job-id>
hermes cron create "*/30 * * * *" --name "dream-scheduler" --script "$HOME/.hermes/scripts/dream_scheduler.py" "Run the Dream Auto scheduler script."
```

### Scheduled Jobs

| Job | Schedule | Purpose |
|-----|----------|---------|
| `dream-scheduler` (6b32bfa79e52) | Every 30min (base) | Check resources → start queued dreams. **Actual interval is adaptive** — LLM decides 2–60 min per cycle |
| `session-indexer` (3d6df85c76df) | Every 6h | Index new sessions + grade them |

### Running the Scheduler Manually

The cron job calls `dream_scheduler.py` directly via `hermes cron`. To run manually (e.g. for debugging):

```bash
# Must use python3 from hermes-agent venv (has psutil and hermes_tools)
cd ~/.hermes
HERMES_CONFIG=~/.hermes/config.yaml python3 -c "
import sys; sys.path.insert(0, 'scripts')
from dream_scheduler import run_scheduler_cycle
result = run_scheduler_cycle()
print(result)
"
```

Or import as module:
```python
from dream_scheduler import run_scheduler_cycle
result = run_scheduler_cycle(dry_run=True)  # dry-run to see what would happen
```

**Why direct execution fails:** `~/.hermes/scripts/dream_scheduler.py` is not executable (`chmod -x`). The cron job uses `hermes cron run` which handles interpreter dispatch. Direct `python3` invocation works with correct env setup.

### Session Indexer — Correct Path
- **Script location:** `~/.hermes/scripts/session_indexer.py`
- **NOT:** `~/.hermes/skills/autonomous-ai-agents/hermes-dream-task/scripts/session_indexer.py` (that directory does not exist)
- **Output:** top 5 session topics written to `topics_for_cache.json` via `--write-topics` flag
- If the cron job fails with "Script not found", verify the configured command path matches `~/.hermes/scripts/session_indexer.py`

## Resource Governance (Three Layers)

The system uses adaptive, LLM-driven resource management — not fixed intervals or hardcoded thresholds. Only physical safety caps are hardcoded.

### Layer 1: Dynamic Concurrency (Scheduler)
Every cycle, the scheduler asks the LLM: *"How many additional dreams should start now?"*
- Hard safety caps: **max 5 concurrent**, stops at CPU ≥ 90% or RAM ≥ 95%
- Fills slots from queue first, then from highest-potential sessions

### Layer 2: Self-Throttling (Running Dreams)
Each dream checks `psutil` between every MCTS iteration:
- CPU ≥ 90% or RAM ≥ 95% → sleeps **120 seconds**
- CPU ≥ 75% or RAM ≥ 85% → sleeps **90 seconds**
- CPU ≥ 50% or RAM ≥ 70% → sleeps normal **60 seconds**
- Low load → sleeps **10–30 seconds** (turbo mode)
- Dreams also check for `SYSTEM_PAUSE` flag file

### Layer 3: Adaptive Daemon Cadence
Instead of fixed 30-minute sleep, the daemon asks the LLM after every cycle: *"How many minutes until the next check?"*
- Range: **2 minutes** (turbo, queue backed up + resources free) to **60 minutes** (idle, nothing to do)
- Falls back to base interval if LLM call fails

### Emergency Pause
Create a pause flag to make all running dreams wait and the scheduler skips:
```bash
touch ~/.hermes/state/dream/SYSTEM_PAUSE   # pause all dreams (they loop on 30s sleep)
rm ~/.hermes/state/dream/SYSTEM_PAUSE      # resume
```
**Warning:** Any dream already in distillation phase will hang forever if PAUSE is set — remove before resuming. Scheduler also skips new dream starts while flag exists.

### Zombie Dream Blocking (Silent Queue Stall)
Crashed dreams leave `status.txt=running` but no process. `count_running_dreams()` reads `status.txt` directly — dead dreams count against the 5-concurrent cap, silently stalling the queue. See `references/diagnostic-snapshots.md` for full recovery procedure.

## Key Design Decisions

- **Holographic dual stores** — fact_store (entity) vs memory (document). Dream insights land in memory via holographic_auto's 7-hook pipeline.
- **Plugin hook composability** — Three confirmed hooks for insight injection: `pre_llm_call`, `on_llm_call`, `post_llm_call`. Configurable via `holographic_auto` plugin flags.
- **No hardcoded thresholds** — LLM decides when ambiguous
- **Resource availability is ONLY gate** — scheduler runs dreams when CPU/RAM free
- **SwiftSage fast path** — pure heuristics, zero LLM latency for simple queries
- **Monte Carlo for decisions** — MCTS for complex multi-branch reasoning
- **Error-triggered dreams** — post_tool_call errors → queue immediately, no entropy gate

## Tool-Using Rollouts — CONFIRMED WORKING (2026-05-03)

**Audit date: 2026-05-03.** Full codebase audit confirms the two-tier AIAgent tool system is IMPLEMENTED and OPERATIONAL.

### What IS in place (no longer a limitation)

`dream_loop_v3.py` has a full two-tier rollout system:

- `rollout_tier1()` — fast LLM-only (~1-2s), uses `call_hermes()` subprocess
- `rollout_tier2()` — tool-using AIAgent (~30-120s), uses `DreamAgent` wrapping `AIAgent(enabled_toolsets=["terminal", "file", "session_search", "memory"])`
- `DreamAgentPool` — reuses a single AIAgent instance per dream (avoids cold-start cost)

### Trigger conditions for Tier-2 (tool-using) rollouts

All must be true simultaneously:
1. Tier-1 confidence < `TOOL_ROLLOUT_THRESHOLD` (0.30)
2. Branch is the best UCB1 node (`is_top_branch=True`)
3. System resources idle: CPU < 70% AND RAM < 70%

### How to verify tools are firing

Check a running dream's `dream_output.log`:
```
[MCTS iter 2/10]
  [PLAN] expand_more: ...
  [ROLLOUT] Direct analysis → failure (0.00)   ← Tier-1 only
  [ROLLOUT] Direct analysis → uncertain (0.50)  ← Tier-1 only
  [THROTTLE] CPU=14% RAM=45% low — sleeping 30s  ← resource check working
```

Tier-2 AIAgent rollouts do NOT log a special prefix — they return structured JSON that's parsed and appear as `confidence: 0.5+` results. The diagnostic prompt instructs AIAgent to "actually investigate" with real commands.

### When Tier-2 fires

At iter 2+ when:
- A branch's Tier-1 confidence drops below 0.30 (vague briefs score 0.00-0.17)
- That branch is the current UCB1 best
- CPU < 70% and RAM < 70%

### Known MCTS behavior (not a bug)

On iter 1, ALL children have `n_visits=0` so ALL get `ucb=+inf`. The first child in the loop becomes `best_child_ucb` by default. This is standard UCB1 — early iterations don't distinguish branches well. As visits accumulate, selection becomes smarter. This is expected, not a defect.

### Quick diagnostic commands

```bash
# See if any dreams are running
ps aux | grep dream_loop_v3 | grep -v grep

# Check running dream logs
tail -20 ~/.hermes/state/dream/$(ls -t ~/.hermes/state/dream/ | head -1)/dream_output.log

# Count queued dreams
sqlite3 ~/.hermes/state/dream/dream_queue.db "SELECT COUNT(*) FROM dream_queue WHERE status='queued';"
```

Full implementation: `references/dream-loop-tool-integration.md`

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
`kill_stale_dreams()` in `dream_scheduler.py`: called at the start of every scheduler cycle. Kills any dream running longer than `MAX_DREAM_WALLCLOCK_MINUTES = 30`. Marks `status.txt` as `killed_wallclock`, updates queue DB to `killed_wallclock` status so it doesn't retry. Works independently of per-dream staleness detection — this is a global scheduler safety net.

### SQLite Indexes (live DBs, applied 2026-04-27)

**BUG FIXES APPLIED 2026-04-28:**

**1. `planned_action` undefined at iteration start:**
`planned_action = plan.get("action")` was read BEFORE `mcts_expand()`, but the `expand` function's `if not child_ids` override happened AFTER `planned_action` was already referenced later in the loop. Result: when expand returns no children, `planned_action` used the wrong default instead of "wrap_up". Fix: read `plan.get("action")` BEFORE calling expand.

**2. `run_results` undefined — NameError on first iteration:**
`run_results` was referenced (`.append()`, written to `monte_carlo_runs.json`, checked for `len() == 0`) but never initialized in `mcts_loop()`. Would raise `NameError` on first rollout iteration. Fix: add `run_results = []` in the initialize block.

**3. CI width guard was `n > 1` instead of `n > 2`:**
`ci_width = round(1.0 / sqrt(n))` at n=1 gives 1.0 (same as unvisited), n=2 gives 0.707 (still meaningless for a 2-sample estimate). Guard should be `n > 2` since Wilson CI requires at least 3 samples. Same bug existed in both `mcts_backpropagate()` and `MCTSNode.update()`. Fix: `n > 2` guard.

**4. Zombie queue — scheduler blocked despite empty queue:**
16 crashed dreams left `status.txt = "running"` AND `dream_queue.db status = "running"`. The scheduler's `count_running_dreams()` reads `status.txt` files (not the queue DB), so it counted these dead dreams against the 5-concurrent safety cap, preventing ANY new dreams from starting. Queue sat at 6011 for hours.
Fix (two places):
```bash
# 1. Update queue DB
sqlite3 ~/.hermes/state/dream/dream_queue.db \
  "UPDATE dream_queue SET status='failed_crash', completed_at=datetime('now') WHERE status='running';"

# 2. Update status.txt files
for id in $(ls -d ~/.hermes/state/dream/*/); do
  if [ "$(cat $id/status.txt 2>/dev/null)" = "running" ]; then
    echo "failed_crash" > "$id/status.txt"
  fi
done
```
After cleanup: scheduler immediately started 1 new dream.

**BUG FIX APPLIED (2026-04-28): completion detector — queue DB never synced for normally-finished dreams**

`dream_scheduler.py`'s `sync_dream_status()` (renamed from `kill_stale_dreams()`) had two jobs:
1. Kill wallclock-exceeded dreams → calls `mark_completed(dream_id, killed=True)` ← only this path updated the queue DB
2. Detect completed dreams → was MISSING — normally-finished dreams left queue DB stuck on "running"

Consequence: `meta.json status=done`, but `dream_queue.db status=running` forever. Scheduler's `count_running_dreams()` reads status.txt files (not queue DB) — so the queue appeared frozen with 6011 pending despite completed dreams. Concurrent slot cap was also wrong.

Fix: add Case 1 (completion detector) to `sync_dream_status()`:
```python
# ── Case 1: normally completed ────────────────────────────────────────
if meta_status in ("done", "completed"):
    _sync_queue_status(dream_id, "completed")
```

New helper `_sync_queue_status()` only updates if current status is "queued" or "running" (avoids clobbering explicit failed/killed states).

Also: renamed `kill_stale_dreams()` → `sync_dream_status()` since it now handles both completion detection AND wallclock enforcement in one pass.

**BUG FIX APPLIED (2026-04-28): `dream_queue` cross-DB JOIN crash**

`get_session_with_highest_potential()` in `dream_scheduler.py` opened only `session_index.db` but referenced `dream_queue` — which lives in `dream_queue.db`. Caused `sqlite3.OperationalError: no such table: dream_queue`.

Fix: `ATTACH DATABASE` before the query:
```python
conn = sqlite3.connect(str(DB_PATH))
conn.execute(f"ATTACH DATABASE '{DREAM_QUEUE_DB}' AS dream_queue_db")
# Reference dream_queue_db.dream_queue in the query
```

Fixed in: `~/.hermes/scripts/dream_scheduler.py` AND `~/.hermes/dream-auto-dist/scripts/dream_scheduler.py` (both copies had the same bug).

**Schema boundary reminder:**
- `session_index.db` — `sessions`, `indexed_runs` tables
- `dream_queue.db` — `dream_queue` table
- Any query joining across both must ATTACH the other DB first.

**dream_loop_v3.py `psutil` dependency:**
- `import psutil` at line 27 requires the package to be installed in the **venv that executes dream_loop_v3**, not just system Python.
- Install with: `uv pip install --python /home/stefan171/.hermes/hermes-agent/venv/bin/python3 psutil`
- If missing, every dream crashes immediately with `ModuleNotFoundError: No module named 'psutil'` before any MCTS work begins.

**Stale "running" status blocking scheduler:**
- Crashed dreams leave `status.txt` as `running` and `dream_queue.db` status as `running`.
- The scheduler's `count_running_dreams()` counts these against the hard safety cap (max 5 concurrent).
- Result: queue backs up even though no actual dream processes are alive.
- Fix: mark crashed dreams as `failed`/`failed_crash` in both `status.txt` and the queue DB.

**dream_output.log stdout buffering:**
- The scheduler spawns dreams with `stdout=open(dp / "dream_output.log", "w")` and `start_new_session=True`.
- Python buffers stdout when writing to a file (not a TTY), so `dream_output.log` appears empty until the process exits or the buffer fills.
- To see live progress, either `tail -f` the file or add `flush=True` to print calls in dream_loop_v3.py.

**dream_scheduler.py cross-DB crash (Apr 2026):**
- `get_session_with_highest_potential()` opens `session_index.db` but queries `dream_queue` which lives in `dream_queue.db`
- Fix: `conn.execute(f"ATTACH DATABASE '{DREAM_QUEUE_DB}' AS dream_queue_db")` then reference `dream_queue_db.dream_queue` in the query
- Bug existed in both `~/.hermes/scripts/dream_scheduler.py` and `~/.hermes/dream-auto-dist/scripts/dream_scheduler.py`

**dream_loop_v3.py UnboundLocalError crash (Apr 2026):**
- All v3 dreams crashed at line 797 with `UnboundLocalError: cannot access local variable 'executor' where it is not associated with a value`
- Root cause: `executor.submit(rollout, ...)` was called BEFORE the `with ThreadPoolExecutor(...) as rollout_ex:` block that defines `executor`
- Also: the old code created the executor AFTER submitting tasks, so rollouts were never parallelized even if the scoping bug didn't fire
- Fix: create `ThreadPoolExecutor` BEFORE submitting, use `wait()` for parallel completion, then `shutdown(wait=False)`
- Patched in `dream-auto-dist`; must be copied to `~/.hermes/skills/autonomous-ai-agents/hermes-dream-task/scripts/dream_loop_v3.py` (that directory was empty — scheduler path pointed to void)

**hermes-dream-task directory empty:**
- `~/.hermes/skills/autonomous-ai-agents/hermes-dream-task/scripts/` was non-existent
- Scheduler's `DREAM_LOOP_V3` path points to this location — dreams were failing to start
- Must create the directory and copy from `dream-auto-dist`

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

**dream_loop_v3.py `started_at` never written — wallclock kill bypassed (2026-04-30):**
- `sync_dream_status()` in `dream_scheduler.py` kills any dream where `meta.json.started_at` is old enough to exceed `MAX_DREAM_WALLCLOCK_MINUTES = 30`.
- HOWEVER, `dream_loop_v3.py` never writes `started_at` into `meta.json` at dream startup.
- Result: all 5 active dreams have `meta.json` with no `started_at` field — the wallclock safety net is completely blind to them.
- `5f9526f7` has been running since Apr 28 23:52 (~48h) with `status.txt=running` but `meta.json.started_at=null`. It will never be wallclock-killed.
- Also affects `count_running_dreams()` staleness checks that rely on `started_at` timestamps.
- **Fix required in `dream_loop_v3.py`:** Write `started_at` to `meta.json` on dream init, e.g.:
  ```python
  meta = {"iteration": 0, "started_at": datetime.now(timezone.utc).isoformat(), ...}
  write_json(meta_file, meta)
  ```
  Also update `meta.json` periodically (every iteration) so wallclock killer sees fresh timestamps even for long-running productive dreams.

**dream_loop_v3.py 429 backoff — CRITICAL BUG (2026-04-29):**
- `dream_loop_v3.py` does NOT implement exponential backoff on HTTP 429 (rate-limit) errors.
- On a 429, it retries IMMEDIATELY, causing rapid-fire burst calls that exhaust the MiniMax API rate limit.
- The process can hammer the API with retries faster than the rate-limit window resets, making the situation worse.
- **Fix required:** Implement exponential backoff with jitter. On 429, sleep `min(base * 2^n + random_jitter, max_backoff)` before retrying. Base: 30s, max backoff: 300s.
- **Workaround:** If a runaway dream is detected (queue stuck, API returning 429s), kill it immediately: `pkill -f dream_loop_v3` or `kill <PID>`.

**dream_loop_v3.py self-throttle implementation:**
- `psutil` checks between MCTS iterations are **fast** — no LLM call per iteration. Only the scheduler uses LLM for cadence/concurrency decisions.
- Sleep tiers: 120s (critical), 90s (high), 60s (normal), 10–30s (turbo). Use `time.sleep()` with `psutil.cpu_percent(interval=1)` and `psutil.virtual_memory().percent`.
- `SYSTEM_PAUSE` check: `os.path.exists("~/.hermes/state/dream/SYSTEM_PAUSE")` — if present, loop sleeps 30s and re-checks. Non-blocking pause.

**dream_scheduler.py adaptive cadence:**
- Rule-based: check CPU/RAM + queue depth to decide sleep seconds (2–60 min range).
- Sleep tiers: 2 min (queue 100+, CPU/RAM OK), 5 min (queue 100+, moderate load), 10 min (queue 10+), 60 min (queue empty).
- Cron still fires every 30 min, but the daemon's internal sleep may be shorter/longer. The cron ensures the daemon stays alive.

### Bug: meta.json Does Not Contain dream_id

**Finding (2026-05-01):** The `dream_id` is NEVER written into `meta.json`. It is derived from the directory name:

```python
dream_id = meta.get("dream_id", d.name)  # d is the dream directory
```

All `meta.json` files look like:
```json
{
  "related_dreams": ["..."],
  "iteration": 1,
  "best_confidence": 0.0
}
```

With no `dream_id`, `status`, or `started_at`. The `dream_id` for all DB operations comes from `d.name` (the directory path).

### Bug: Wallclock Killer Gap — Zombie Dreams with Incomplete meta.json

**Symptom:** 5 dreams show `status.txt = running` in `state/dream/<id>/` but are actually dead. The scheduler's `count_running_dreams()` counts them as alive (hard safety cap fills), but no MCTS work happens.

**Root cause:** `sync_dream_status()` only kills a dream if `meta.json` has:
1. `status = "running"` (Case 2 branch), AND
2. `started_at` as a Unix timestamp

Some zombie dreams have `meta.json` with NO `status` field and NO `started_at` field (e.g., `{"related_dreams": [...], "iteration": 1, "best_confidence": 0.0}`). The wallclock check `if meta_status == "running"` evaluates to `False` (empty string != "running"), so the kill branch is never entered. The `started_at is not None` guard passes only when the field EXISTS — if it doesn't exist, `meta.get("started_at")` returns `None` and the branch is skipped.

**Fix (applied 2026-05-01):**
1. Added `status.txt` fallback when `meta.json` lacks `status` key
2. Added `started_at` fallback from `dream_queue.db` via `_parse_ts()` helper (ISO→Unix float)
3. Added `_parse_ts()` function at module level
4. Fixed column name: `dream_queue.db` uses `dream_id` not `job_id`

**Result:** 5 zombie dreams killed (3.0–4.0h stale), 3 new dreams immediately dispatched.

```
[WALLCLOCK KILL] 8a59f345 ran for 239min > 30min — killing
[WALLCLOCK KILL] f7dba3a7 ran for 215min > 30min — killing
[WALLCLOCK KILL] b1721c1d ran for 215min > 30min — killing
[WALLCLOCK KILL] 1b5bbf03 ran for 215min > 30min — killing
[WALLCLOCK KILL] 22417f3e ran for 185min > 30min — killing
[SCHEDULER] Killed 5 stale dreams
[SCHEDULER] Starting queued dream: f59e726f
[SCHEDULER] Starting queued dream: 083568b6
[SCHEDULER] Starting queued dream: 5f0395e9
```

**Critical note on `dream_loop_v3.py` side:** `dream_loop_v3.py` still does NOT write `started_at` to `meta.json` on init. The scheduler fallback to `dream_queue.db` works, but if a dream is ever enqueued directly (not via scheduler) or the queue DB entry is missing, the wallclock killer will be blind again. The proper fix is to have `dream_loop_v3.py` write `started_at` to `meta.json` at startup and update it each iteration.
```python
# After Case 2 (wallclock exceeded), add:
# Case 3: zombie — status.txt says running but meta.json is incomplete
if status_file.exists() and status_file.read_text().strip() == "running":
    if meta.get("started_at") is None or meta.get("status") is None:
        status_file.write_text("killed_wallclock")
        meta["status"] = "killed_wallclock"
        meta["killed_at"] = now
        meta["wallclock_minutes"] = 0
        write_json(meta_file, meta)
        mark_completed(dream_id, killed=True)
        killed.append({"dream_id": dream_id, "wallclock_min": 0, "reason": "zombie (no started_at)"})
```

**Detection query:**
```bash
# Find all dream dirs where status.txt=running but meta.json is missing started_at
for d in ~/.hermes/state/dream/*/; do
  [ -f "$d/status.txt" ] && [ "$(cat "$d/status.txt")" = "running" ] || continue
  grep -q '"started_at"' "$d/meta.json" 2>/dev/null || echo "ZOMBIE: ${d##*/}"
done
```

### Bug: SYSTEM_PAUSE + Wallclock Kill Interaction (Critical)

**Symptom:** `sync_dream_status()` correctly marks `status.txt = killed_wallclock` and updates the queue DB, but the OS process is NOT terminated. The dream process is alive (has a PID), blocked on SYSTEM_PAUSE throttle loops, and survives the wallclock kill indefinitely.

**Root cause:** `sync_dream_status()` calls `mark_completed()` which updates status files and DB, but never sends `SIGKILL` to the actual `subprocess.Popen` handle. The Popen object is not retained after `spawn_dream()` returns — the PID is logged but the handle is discarded.

**Live signature:** `dream_output.log` shows:
```
[THROTTLE] SYSTEM_PAUSE detected — waiting for removal...
[THROTTLE] Still paused after 5040s...
[THROTTLE] Still paused after 5280s...
```
while `status.txt` simultaneously shows `killed_wallclock`.

**Confirmed 2026-05-02:** 6 zombie PIDs (46708, 46710, 58686, 58687, 80680, 80681) killed manually with SIGKILL after SYSTEM_PAUSE was removed. All had `status.txt = killed_wallclock` but were still running.

**Fix required in `dream_scheduler.py`:** Store `Popen` handles in a dict keyed by dream_id, then call `proc.terminate()` or `proc.kill()` alongside `mark_completed()` in the wallclock kill path. Alternatively, maintain a `{dream_id: PID}` dict and send `os.kill(pid, signal.SIGKILL)` on wallclock kill.

**Workaround (immediate recovery):**
```bash
# 1. Remove SYSTEM_PAUSE so surviving dreams can be examined
rm ~/.hermes/state/dream/SYSTEM_PAUSE

# 2. Find zombie PIDs that are dead but still running
for pid in $(pgrep -f dream_loop_v3); do
  dir=$(ps -o args= -p $pid | grep -oP '(?<=dream_loop_v3\.py )\w+' | head -1)
  status=$(cat ~/.hermes/state/dream/$dir/status.txt 2>/dev/null)
  if [ "$status" = "killed_wallclock" ] || [ "$status" = "failed_crash" ]; then
    echo "ZOMBIE PID $pid ($dir) — status.txt=$status — killing..."
    kill -9 $pid
  fi
done
```

**Prevention:** The fix must be in `dream_scheduler.py`'s `sync_dream_status()` — wallclock kills should terminate the OS process, not just update metadata.

### Bug: Queue Stagnation — NULL dream_potential Blocks All Enqueue

**Symptom:** Queue has 6,056 pending dreams (all from Apr 24). No new dreams added since Apr 24. `get_session_with_highest_potential()` returns empty results.

**Root cause:** The query has `WHERE s.dream_potential IS NOT NULL` — if the session indexer's LLM grading step hasn't run (or returned NULL), those sessions are invisible to the scheduler. The session indexer last ran Apr 30 18:05 but only indexed 100 sessions out of ~54 total (it only processes the 100 most recent). Sessions from Apr 29-30 with `dream_potential = NULL` can never be enqueued.

**Fix direction:** Either (a) lower the threshold to include unscored sessions (`dream_potential > 0.0` or `dream_potential IS NULL` with a default low priority), or (b) ensure the session grader runs frequently enough to score all indexed sessions.

**Detection query:**
```python
# Check how many sessions have NULL potential vs scored
import sqlite3
conn = sqlite3.connect('state/dream/session_index.db')
cur = conn.cursor()
cur.execute('SELECT COUNT(*) FROM sessions WHERE dream_potential IS NULL')
null_count = cur.fetchone()[0]
cur.execute('SELECT COUNT(*) FROM sessions')
total = cur.fetchone()[0]
print(f"NULL: {null_count}/{total}, Scored: {total-null_count}/{total}")
```

### SYSTEM_PAUSE Throttle — Live Log Signature

When a dream hits `SYSTEM_PAUSE`, the `dream_output.log` shows:
```
[THROTTLE] SYSTEM_PAUSE detected — waiting for removal...
```
The dream loops with 30s sleeps until the file is removed. This is normal behavior — not an error. The pause file at `state/dream/SYSTEM_PAUSE` was created Apr 29 12:44 and blocks all dream progress until removed.

**dream_queue.db location:**
- Created at runtime in `state/dream/`. Must exist before scheduler can enqueue.

**dream_queue.db schema (confirmed 2026-05-01):**
```
Columns: queue_id, session_id, dream_id, dream_question, grade, resource_cost,
         priority, created_at, started_at, completed_at, status
```
Note: There is NO `iteration` or `confidence` column in the queue DB. Those live in `meta.json`.

**session_indexer.py first run:**
**updated-on: "2026-05-03"**

## Monitoring — CLI Dashboard (v2)

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

### Dashboard v2 — New Panels (2026-05-03)

| Panel | What it shows |
|-------|--------------|
| System Health Score | 0-100 composite: queue depth + success rate + kill rate + scoring coverage |
| Throughput (14d) | Week-over-week completion comparison: this week vs last week, delta |
| Health Summary | 12 KPIs: Total, Successful, Failed, Crashed, Stale, Running, Queued, Success Rate, Terminal Rate, Avg Conf 7d, Wallclock Kills |
| Trend Sparklines | 7-day completion and session histograms with Unicode bars |
| Queue Health | Backlog severity, avg/max wait, kill rate alert, today's throughput |
| Session Index | Scoring coverage, potential distribution bars (0.8+/0.5-0.8/<0.5) |
| MCTS Performance | Avg confidence (from queue DB grade), tier breakdown, insights/dream ratio |
| Actionable Alerts | Consolidated red/yellow alerts with exact numbers |
| Insights Table | Recent 20 insights with word-boundary truncation |
| Dream Runs / Queue / Sessions / Error Breakdown | Filterable detail tables |

**Health Score formula (0-100):**
- Queue score: `max(0, 50 - queued/100)`
- Success score: actual % directly
- Kill rate score: `max(0, 100 - 2*kill_rate%)`
- Scoring coverage: `min(100, scored_sessions/total_sessions * 100)`
- Final: average of the four

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

Full architecture documented in `references/dream-dashboard-v2.md`.

The dashboard extracts and displays real insight content from successful dreams:
- Parses both v2 (`insights.json`) and v3 (`dream_output.log` JSON block) output formats
- Categorizes insights heuristically by keyword: debug/ops, architecture, data/db
- Shows 20 most recent insights from last 7 days
- Derives actionable alerts from health stats (queue backlog, zombie dreams, unscored sessions, wallclock kills, low confidence)

CLI flags: `--insights`, `--errors`, `--queue`, `--sessions`, `--runs`, `--all`

## Dream Auto Dist — Repo Locations (Keep in Sync)

The dream-auto repo exists in multiple places. After ANY change to one, verify/copy to the others:

| Location | Purpose | Remote |
|----------|---------|--------|
| `~/.hermes/dream-auto-dist/` | Git-tracked repo, published to GitHub | `origin = StefanIsMe/dream-auto` |
| `~/.hermes/plugins/dream_auto/` | Live plugin (imported by Hermes) | Not git-tracked |
| `~/.hermes/scripts/dream_scheduler.py` | Live scheduler (cron job calls this) | Not git-tracked |
| `~/.hermes/skills/.../hermes-dream-task/scripts/` | MCTS engine (scheduler imports this) | Not git-tracked |

**After any fix to `dream-auto-dist`:**
```bash
# Sync plugin
cp ~/.hermes/dream-auto-dist/plugins/dream_auto/__init__.py ~/.hermes/plugins/dream_auto/__init__.py
cp ~/.hermes/dream-auto-dist/plugins/dream_auto/resource_monitor.py ~/.hermes/plugins/dream_auto/resource_monitor.py

# Sync scripts
cp ~/.hermes/dream-auto-dist/scripts/dream_scheduler.py ~/.hermes/scripts/dream_scheduler.py

# Sync MCTS engine
cp ~/.hermes/dream-auto-dist/skills/autonomous-ai-agents/hermes-dream-task/scripts/dream_loop_v3.py ~/.hermes/skills/autonomous-ai-agents/hermes-dream-task/scripts/dream_loop_v3.py
```

**Test suite:** `~/.hermes/plugins/dream_auto/tests/test_dream_auto_plugin.py` — 55 tests, all passing. See `hermes-plugin-audit` skill for full audit report — `resource_monitor.py` (215 lines, ResourceMonitor class) has **zero tests** and needs `tests/test_resource_monitor.py`. Run with:
```bash
cd ~/.hermes/plugins/dream_auto && python3 -m pytest tests/test_dream_auto_plugin.py -v
```
Tests live in the installed plugin dir, NOT in dist. After updating tests, copy to dist and commit:
```bash
cp -r ~/.hermes/plugins/dream_auto/tests/ ~/.hermes/dream-auto-dist/plugins/dream_auto/tests/
```

**Installed plugin is AHEAD of GitHub (2026-05-03):** `~/.hermes/plugins/dream_auto/__init__.py` (May 3) has a "Degenerate Loop Guard" that skips dream-generated sessions (`session_id.startswith("dream")`) and cron sessions (`session_id.startswith("cron_")`). This fix is NOT in `dream-auto-dist` (Apr 30) and NOT on GitHub. Sync from installed → dist → git push when ready.

**Always verify:** `diff ~/.hermes/plugins/dream_auto/__init__.py ~/.hermes/dream-auto-dist/plugins/dream_auto/__init__.py` should return empty after syncing.

## Config Vars (plugin)

```
DREAM_AUTO_ENABLED=1       — disable entirely
DREAM_AUTO_VERBOSE=1      — log activity
DREAM_AUTO_MAX_INJECT=3    — max dreams injected per turn
```

Removed from v2: `DREAM_AUTO_AUTOSTART`, `DREAM_AUTO_MIN_COMPLEXITY`

## Cross-Session Relevance (from dream-cross-session-relevance)

**Absorbed 2026-04-27.** Current dream_auto plugin had critical gaps fixed here:

### Problems Fixed

1. **Single-session only** — insights persisted to `~/.hermes/state/dream/<id>/insights.json`
   but `on_session_start` did NOT scan for relevant past dreams. New sessions started cold.
2. **Entropy gate waste** — `post_llm_call` hook ran `hermes chat -q` complexity assessment
   on EVERY prompt. One extra LLM call per prompt, even simple ones.
3. **Cron job burn** — every cron session triggered dreams that never got consumed.
   Session ended immediately, orphan files piled up.
4. **No deduplication** — same complex topic could trigger multiple dreams.
5. **No topic tagging** — insights.json had no keywords/tags.

### Solution: Relevance Search Replaces Entropy Gate

**Old flow:** assess complexity → if complex → start dream
**New flow (v3):** search relevance → if match found → inject; if no match AND resources free → start dream

### Implementation

1. **Topic tags on insights** — On dream completion, extract 3-5 topic keywords via `hermes chat -q`.
   Store in insights.json alongside insights:
```json
{
  "topics": ["linkedin", "content-strategy", "org2", "engagement"],
  "insights": ["..."],
  "confidence": 0.78,
  "created_at": "2026-04-22T10:00:00+07:00"
}
```

2. **Dream relevance index** — `~/.hermes/scripts/dream_relevance_search.py`
   On `pre_llm_call`:
   - Scan all `~/.hermes/state/dream/*/insights.json`
   - Score each against current prompt using `hermes chat -q` (single call with all dream topics as context)
   - Return top 1-2 matching dreams
   - Inject their insights into context

3. **One LLM call max per prompt** — Not one for gate + N for dream. Single relevance check covers both.

4. **Deduplication check** — Before starting new dream, query existing dreams for similar topics.
   If confidence > threshold, skip new dream.

5. **Cron cleanup** — Periodic removal of stale dreams (low confidence, >30 days old).

### Verification

```bash
# Count orphan dream files
ls ~/.hermes/state/dream/ | wc -l

# After fix: new sessions should show "Injected N relevant dream insights" in hook logs
# Cron runs should never trigger new dreams if relevant past dream exists
```

## Research Papers (from hermes-dream-task)

**Absorbed 2026-04-27.** Architecture based on peer-reviewed research.

| Paper | Key Contribution |
|-------|-----------------|
| **Reflexion** (2303.11366) | Verbal reinforcement + episodic memory buffer |
| **SwiftSage** (2305.17390) | Fast/slow dual-process agent architecture |
| **SPOC** (2506.06923) | Interleaved generate-verify in single pass |
| **MAPS** (2506.23888) | Adaptive-depth iterative self-reflection |
| **Tree-of-Thoughts** (2305.10601) | Deliberate problem solving with exploration |
| **MetaRAG** (2402.11626) | Monitor → Evaluate → Plan metacognitive pipeline |
| **Branch-and-Browse** (2510.19838) | Background reasoning + action memory |
| **ReTreVal** (2601.02880) | ToT + self-refinement + reflexion memory |
| **LLM Metacognition** (2505.13763) | LLMs can monitor their own thinking |

## Plugin Hooks Reference (from hermes-dream-task)

The `dream_auto` plugin fires at 6 hook points:

| Hook | Behavior |
|------|---------|
| pre_llm_call | Inject distilled insights (never raw logs) |
| pre_tool_call | Non-blocking suggestion for complex code |
| post_tool_call | Auto-start troubleshooting dream on errors (gated) |
| post_llm_call | Resource availability check, queue dream if complex |
| on_session_start | Log active dreams |
| on_session_end | Clean up tracking |

## File Structure

Dream state is NOT stored in `~/.hermes/state/dream/`. Actual locations:

**Dream scheduler output** — `~/.hermes/cron/output/<job_id>/` where `<job_id>` is the `dream-scheduler` job (`6b32bfa79e52`). Each run writes a `.md` report file timestamped `YYYY-MM-DD_HH-MM-SS.md`.

**Active dream processes** — Running dreams are `dream_loop_v3.py` subprocesses started by the scheduler. Find them with:
```bash
ps aux | grep dream_loop | grep -v grep
```

**Dream queue** — Dreams are spawned as subprocess from `~/.hermes/skills/autonomous-ai-agents/hermes-dream-task/scripts/dream_loop_v3.py`. Queue management is internal to the scheduler script.

**Legacy dream state** (if any):
```
~/.hermes/state/dream/<dream_id>/
  meta.json                — metadata, status, confidence, iteration count
  exploration_tree.json    — tree of reasoning nodes
  insights.json            — distilled key findings
  failures.json            — failure patterns
  pending_questions.json   — open questions
```

### Finding dream output after execution

```bash
# Most recent scheduler reports
ls -lt ~/.hermes/cron/output/6b32bfa79e52/ | head

# Extract latest run details
cat ~/.hermes/cron/output/6b32bfa79e52/$(ls -t ~/.hermes/cron/output/6b32bfa79e52/ | head -1)

# Active dream processes with their IDs
ps aux | grep dream_loop_v3 | grep -v grep | awk '{print $2, $11, $12, $13}'
```
```
