# Dream System v3 — Diagnostic Snapshot 2026-05-02b (01:00 cycle)

## Scheduler Cycle Run

Manual scheduler invocation at ~00:31:

```
[WALLCLOCK KILL] 9eeeb969 ran for 31min > 30min — killing
[WALLCLOCK KILL] 6494fc1e ran for 31min > 30min — killing
[SCHEDULER] Killed 2 stale dreams
[SCHEDULER] Resource check: available — Resources OK, 2 slot(s) available
[SCHEDULER] Starting queued dream: 420fd215
  [SPAWN] Started dream 420fd215 via subprocess
[SCHEDULER] Starting queued dream: 22818007
  [SPAWN] Started dream 22818007 via subprocess
```

## Active State After Cycle

**dream_loop_v3 PIDs alive (7 total):**
| PID | Started | Dream ID | Notes |
|-----|---------|----------|-------|
| 90188 | 00:00 | 9eeeb969 | ZOMBIE — wallclock killed but still running |
| 90189 | 00:00 | 6494fc1e | ZOMBIE — wallclock killed but still running |
| 147725 | 00:30 | 5dacc591 | OK (ModuleNotFoundError in process) |
| 147726 | 00:30 | 45d8c5bd | OK (ModuleNotFoundError in process) |
| 147727 | 00:30 | 38411a07 | OK (AttributeError in process) |
| 151710 | 00:31 | 420fd215 | OK — just spawned |
| 151718 | 00:31 | 22818007 | OK — just spawned |

**Queue DB (5 running, matches 5 status.txt=running dirs):**
- 22818007, 38411a07, 420fd215, 45d8c5bd, 5dacc591

## Critical Issue: Orphaned Zombie PIDs

PIDs 90188 and 90189 were spawned at 00:00 as 9eeeb969 and 6494fc1e. The scheduler wallclock-killed them at 00:31 but they are still running. This means:

1. `sync_dream_status()` updated status.txt and queue DB correctly
2. But `proc.kill()` / `os.kill(pid, SIGKILL)` was never called — the OS process survived
3. These 2 zombie PIDs consume 2 of the 5 concurrent slots with zero MCTS work happening
4. Effective throughput reduced by 40% (2/5 slots are dead weight)

## New Dreams: meta.json now has started_at

`420fd215` and `22818007` both have:
```json
{"iteration": 0, "started_at": 1777656682.4240355}
```

The `_parse_ts()` fallback fix from 2026-05-01 is working — these new dreams have valid Unix timestamps from the queue DB. The wallclock killer will be able to read their `started_at` correctly.

## Still Unfixed: meta.json never written by dream_loop_v3

`meta.json` is still NOT written by `dream_loop_v3.py` at init. The `started_at` field in new dreams comes entirely from the `dream_queue.db` fallback in `sync_dream_status()`. If a dream is ever started without a queue DB entry, the wallclock killer will be blind again.

## Status Summary

| Status | Count |
|--------|-------|
| queued | 6202 |
| killed_wallclock | 101 |
| completed | 97 |
| failed_crash | 24 |
| running | 5 |
| failed | 5 |
| done | 2 |

## Action Items

1. **Kill orphaned PIDs 90188 and 90189** — `kill -9 90188 90189`
2. **Fix sync_dream_status() wallclock kill** — must call `os.kill(pid, SIGKILL)` alongside metadata update
3. **Propagate fix to dream-auto-dist** — sync the corrected `dream_scheduler.py`
4. **Fix meta.json init in dream_loop_v3.py** — write `started_at` at dream startup, not rely on queue DB fallback
