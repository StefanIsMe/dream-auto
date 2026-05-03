# Dream System v3 — Diagnostic Run 2026-05-01 (Second Cycle)

## State at time of run (04:30 GMT+7)

```
Scheduler cycle: 2026-05-01 04:30 GMT+7
Running dreams: 3 (freshly started at 04:30:42)
Previous cycle: 4 wallclock kills at 04:30:40 (f59e726f, 083568b6, 5f0395e9, 785b3286)
SYSTEM_PAUSE: still active (exists since 2026-04-29T12:44)
```

## Wallclock Kills This Cycle

All 4 killed dreams had identical `meta.json` — incomplete, no `dream_id`, `status`, or `started_at`:

```json
{
  "related_dreams": ["..."],
  "iteration": 1,
  "best_confidence": 0.0
}
```

The scheduler's `sync_dream_status()` handled them via:
1. `status.txt` fallback (status.txt said `running`)
2. `dream_queue.db` `started_at` fallback (DB had the timestamp from enqueue time)

Kill details:
| dream_id | ran for | started_at (DB) | killed at |
|----------|---------|-----------------|-----------|
| f59e726f | 55.1 min | 2026-05-01T03:35:36.083463+07:00 | 04:30:40 |
| 083568b6 | 55.1 min | 2026-05-01T03:35:36.094796+07:00 | 04:30:40 |
| 5f0395e9 | 55.1 min | 2026-05-01T03:35:36.106874+07:00 | 04:30:40 |
| 785b3286 | 30.5 min | 2026-05-01T04:00:12.418924+07:00 | 04:30:40 |

## New Dreams Started This Cycle

| dream_id | started_at (DB) | meta.json keys | status.txt |
|----------|-----------------|----------------|------------|
| 9ec23f1e | 04:30:42.925575+07:00 | related_dreams, iteration, best_confidence | running |
| 1dd505b9 | 04:30:42.935668+07:00 | related_dreams, iteration, best_confidence | running |
| 44857dc4 | 04:30:42.947355+07:00 | related_dreams, iteration, best_confidence | running |

All 3 new dreams: `meta.json` has `iteration: 1, best_confidence: 0.0` — same incomplete pattern. None have `started_at` written by `dream_loop_v3.py` (the DB fallback is what the scheduler will use next cycle).

## Queue DB Status Counts (2026-05-01 04:30)

| Status | Count | Change |
|--------|-------|--------|
| queued | 6,091 | +35 |
| running | 3 | -2 (killed) |
| killed_wallclock | 17 | +4 |
| failed_crash | 24 | — |
| completed | 64 | +1 |
| failed | 5 | — |
| done | 2 | — |

## Key Findings This Cycle

1. **Wallclock kill mechanism works** — 4 zombies killed in consecutive cycles
2. **DB fallback for `started_at` works** — when `meta.json` lacks it, scheduler reads from `dream_queue.db`
3. **`dream_loop_v3.py` still does NOT write `started_at` to `meta.json`** — confirmed again. All active dreams have `meta.json` with no `started_at`
4. **Newly started dreams also lack `started_at` in `meta.json`** — the fix in `dream_loop_v3.py` was never applied

## Open Items

- `dream_loop_v3.py` needs to write `started_at` to `meta.json` on init and update each iteration
- `dream_loop_v3.py` needs to write `status` to `meta.json` (currently only `status.txt` is updated)
- `meta.json` never gets `dream_id` written — it's always derived from directory name in scheduler code
