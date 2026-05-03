# Dream System v3 — Diagnostic Run 2026-05-01 (11:00 Cycle)

## State at 11:00 GMT+7

```
Scheduler cycle: 2026-05-01 11:00 GMT+7
Pre-script spawned: 3 dreams (7a4ea33e, 7b453e67, b1f56d04) at ~11:00:19
First scheduler run (11:09): started 654cf1e6
Running dreams (before this cycle's wallclock check): 5 total
  - 0f59a643: zombie — started_at=1777606261.56284 (~35min elapsed, 5min over cap)
  - 7a4ea33e, 7b453e67, b1f56d04: spawned at 11:00:19 (~2min elapsed)
  - 654cf1e6: spawned 11:00:39 (~1.6min elapsed)
Queue: 6,076 queued | 5 running | 35 killed_wallclock | 24 failed_crash | 83 completed
```

## The Third Zombie Pattern: Complete meta.json Still Not Killed

### 0f59a643 meta.json (complete — should have been killed)

```json
{
  "dream_id": "0f59a643",
  "brief": "Troubleshoot this error that occurred during terminal...",
  "status": "running",
  "started_at": 1777606261.56284,
  "started_at_human": "2026-05-01T10:31:01.562841+07:00",
  "iteration": 1,
  "confidence": 0.0,
  "best_confidence": 0.0
}
```

**This dream had:**
- `dream_id` ✓
- `status = "running"` ✓
- `started_at` as Unix float ✓ (valid, ~34.79 min elapsed at time of scheduler run)

Yet `sync_dream_status()` reported `wallclock_killed: []`.

### Independent simulation proved the kill SHOULD have fired

Running the same logic independently:
```
0f59a643: elapsed: 2087.4s > 1800s → True
  meta.json started_at: 1777606261.56284 (float)
  queue started_at: 2026-05-01T10:31:01.458751+07:00 → parsed: 1777606261.458751
  queue age: 2087.5s = 34.79min
  should kill: True
```

### Root Cause Hypothesis: `now` captured before full iteration

`sync_dream_status()` line ~92: `now = time.time()` is captured **once** at the top, before iterating all ~400 dream directories. If:
1. `now` is captured early in the scheduler cycle
2. The iteration reaches `0f59a643` 5+ seconds later
3. During those 5 seconds, other processing occurs (including spawning new dreams)

Then `now - started_at` would be `captured_now - started_at` where `captured_now` is 5 seconds stale. At 34.79 min this is negligible — but the simulation used a fresh `now` and the math was unambiguous.

**More likely:** The scheduler itself ran `sync_dream_status()` in a context where the wallclock math was somehow bypassed for that specific dream. The pre-script that spawned 3 dreams (`7a4ea33e`, `7b453e67`, `b1f56d04`) ran at `11:00:19`. The scheduler itself ran at `11:09` (the cron trigger). These are separate processes — the pre-script and scheduler do not share a `time.time()` call. The scheduler's `sync_dream_status()` at 11:09 should have killed `0f59a643` at 34.79 min. It didn't.

**Unresolved:** Why did the scheduler's own execution not kill `0f59a643` despite the math being correct?

### Manual Kill Applied

```python
# Status.txt
echo "killed_wallclock" > ~/.hermes/state/dream/0f59a643/status.txt

# meta.json (updated in-place)
{
  "status": "killed_wallclock",
  "killed_at": <now>,
  "wallclock_minutes": 34.8
}

# Queue DB
UPDATE dream_queue SET status='killed_wallclock', completed_at=datetime('now')
WHERE dream_id='0f59a643' AND status='running';
```

After manual kill → scheduler immediately started `ddfaeff7` to fill the freed slot.

## The Three Zombie Patterns — Consolidated

| Pattern | meta.json | status.txt | started_at | Why not killed | Fix |
|---------|-----------|------------|------------|----------------|-----|
| 1: Incomplete | missing `status`, `started_at` | says `running` | absent | `meta_status="" != "running"` | Case 3 (zombie detect) |
| 2: NULL potential | any | any | any | `WHERE s.dream_potential IS NOT NULL` filter | Lower threshold in query |
| 3: Complete meta but still zombie | has all fields | `running` | valid float | `time.time()` ordering or scheduler bug | Manual kill + investigate `sync_dream_status` timing |

## System State After Cleanup

```
Running: 5/5 (slots full) — 7a4ea33e, 7b453e67, b1f56d04, 654cf1e6, ddfaeff7
Queued:  6,077
Session index: 349 total | 40 scored | 309 unscored (NULL grade)
```

## Key Diagnostic: Can Kill But Didn't

The critical distinction for Pattern 3: the kill SHOULD have worked per the math, but the scheduler's own execution didn't trigger it. This rules out:
- Missing `started_at` → we have a float
- `meta_status != "running"` → it equals "running"
- `elapsed < 1800s` → elapsed is 2087s

The problem is in the scheduler's execution path, not the kill logic itself. If this recurs:
1. Don't trust `wallclock_killed: []` from scheduler output
2. Independently verify any dream with `status.txt=running` and `meta.json.status=running` at >30min
3. If confirmed stale: kill manually rather than waiting for next scheduler cycle
