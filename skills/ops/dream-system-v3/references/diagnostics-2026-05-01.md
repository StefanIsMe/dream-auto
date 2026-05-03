# Dream System v3 — Diagnostic Run 2026-05-01

## DREAM_DIR Path (critical — wrong assumption causes FileNotFoundError)

The dream state root is `~/.hermes/state/dream` — **NOT** `~/.hermes/state/dream/dreams`.

Attempting `~/.hermes/state/dream/dreams` produces:
```
FileNotFoundError: [Errno 2] No such file or directory: '/home/stefan171/.hermes/state/dream/dreams'
```

The scheduler code defines:
```python
DREAM_DIR = Path.home() / ".hermes" / "state" / "dream"
```

Each dream directory lives at `DREAM_DIR / <dream_id> /`. Debug by iterating `DREAM_DIR.iterdir()` and filtering for directories.

## State at time of run (00:00 cycle)

```
Scheduler cycle: 2026-05-01 00:00 GMT+7
Running dreams: 5 zombies (all Apr 24, 6+ days stale)
Queue depth: 6,056 queued
SYSTEM_PAUSE: active since 2026-04-29T12:44
Session indexer: last ran 2026-04-30T18:05 (100 sessions)
Most recent session: 2026-04-30T09:57 (not yet indexed)
dream_potential scored sessions: ~100 out of 54 total (most recent only)
Circuit breaker: active (tripped 2026-04-19, disabled_until passed)
```

## State at 04:30 cycle (post-fix, wallclock kills applied)

```
Scheduler cycle: 2026-05-01 04:30 GMT+7
Running dreams: 3 (newly dispatched)
Queue depth: 6,091 queued
Killed this cycle: 4 (f59e726f, 083568b6, 5f0395e9 @ 55min wallclock; 785b3286 @ 30.5min)
Wallclock cap: 30 minutes (MAX_DREAM_WALLCLOCK_MINUTES)
```

## Queue DB Status Breakdown (04:30 post-fix)

| Status | Count |
|--------|-------|
| queued | 6,091 |
| running | 3 |
| killed_wallclock | 17 |
| failed_crash | 24 |
| completed | 64 |
| done | 2 |
| failed | 5 |

## Running Dream meta.json Pattern (normal, healthy)

Healthy running dreams also have this meta.json shape:
```json
{
  "related_dreams": ["..."],
  "iteration": 1,
  "best_confidence": 0.0
}
```
Key difference from zombies: `started_at` is None in both, but `dream_queue.db` carries the actual `started_at` timestamp. The scheduler's `_parse_ts()` reads from the queue DB as fallback.

Confirmed 04:30: `9ec23f1e`, `1dd505b9`, `44857dc4` — all running normally at 3.3min elapsed.

## Zombie Dream meta.json Pattern (the gap)

All 5 zombies have this meta.json shape:
```json
{
  "related_dreams": ["..."],
  "iteration": 1,
  "best_confidence": 0.0
}
```
Missing: `dream_id`, `status`, `started_at`, `brief`, `session_id`.
The `sync_dream_status()` function's Case 2 guard `if meta_status == "running"` evaluates `"" == "running"` = False, so they never enter the kill branch.

## Queue DB Status Counts (2026-05-01 00:30)

| Status | Count |
|--------|-------|
| queued | 6,056 |
| running | 5 (+ 5 zombies = 10 but 5 are dead) |
| completed | 63 |
| failed_crash | 24 |
| killed_wallclock | 8 |
| failed | 5 |
| done | 2 |

## Session Index — Potential Scoring Gap

```
Last indexed run: 2026-04-30T18:05 — 100 sessions
Sessions with dream_potential = NULL: ~5,000+
Sessions with dream_potential scored: ~100

All sessions created 2026-04-29 onward have NULL potential.
The get_session_with_highest_potential() query filters: WHERE s.dream_potential IS NOT NULL
→ unscored sessions are invisible to the scheduler pipeline.
```

## The Two Bugs Found

### Bug 1: Wallclock killer gap (zombies with incomplete meta.json)
- `sync_dream_status()` — Case 2 requires `meta.get("status") == "running"`
- Incomplete meta.json has no `status` field → passes through as "" → no kill
- `started_at` is None when absent → `elapsed > DREAM_WALLCLOCK_SECONDS` check is skipped
- Fix: add Case 3 — if status.txt=running but meta.json lacks started_at, force-kill

### Bug 2: Queue stagnation (NULL potential = invisible to scheduler)
- `get_session_with_highest_potential()` has `WHERE s.dream_potential IS NOT NULL`
- Session grader (session_grader.py) uses LLM to set dream_potential
- If grader hasn't run or returned NULL, those sessions never enter the queue
- Fix: include `dream_potential IS NULL OR dream_potential > 0.0` with default low priority

## Quick Diagnostic Commands

```bash
# 1. Count running vs actual alive
ls ~/.hermes/state/dream/*/status.txt -r | xargs -I{} sh -c 'echo -n "{}: "; cat "{}"' | grep running | wc -l

# 2. Find zombie dreams (status.txt=running but no started_at in meta.json)
python3 -c "
from pathlib import Path
DD = Path.home() / '.hermes' / 'state' / 'dream'
for d in DD.iterdir():
    if not d.is_dir(): continue
    sf = d / 'status.txt'
    mf = d / 'meta.json'
    if not (sf.exists() and mf.exists()): continue
    if sf.read_text().strip() != 'running': continue
    import json
    try: meta = json.loads(mf.read_text())
    except: continue
    if meta.get('started_at') is None:
        print(f'ZOMBIE: {d.name} - {list(meta.keys())}')
"

# 3. Queue depth
python3 -c "
import sqlite3
from pathlib import Path
conn = sqlite3.connect(str(Path.home()/'/.hermes/state/dream/dream_queue.db'))
cur = conn.execute('SELECT status, COUNT(*) FROM dream_queue GROUP BY status')
for r in cur: print(r)
"

# 4. Session potential scoring gap
python3 -c "
import sqlite3
from pathlib import Path
conn = sqlite3.connect(str(Path.home()/'/.hermes/state/dream/session_index.db'))
cur = conn.execute('SELECT COUNT(*) FROM sessions WHERE dream_potential IS NULL')
print('NULL potential:', cur.fetchone()[0])
cur = conn.execute('SELECT COUNT(*) FROM sessions')
print('Total sessions:', cur.fetchone()[0])
"
```
