# Dream System v3 — Diagnostic Snapshots

## 2026-04-30 23:00 Scheduler Run

### Resource Check Output
```
[SCHEDULER] Resource check: NOT available — 5 dreams already running (safety cap)
{
  "dreams_started": 0,
  "skipped": ["resources: 5 dreams already running (safety cap)"],
  "errors": [],
  "wallclock_killed": []
}
```

### 5 Active Dreams (at safety cap = 5)

| Dream ID | Status File | meta.json fields | Iter | Confidence | Last Activity |
|---|---|---|---|---|---|
| `5f9526f7` | `running` | `iteration: 6`, `best_confidence: 0.5`, NO `started_at` | 6/10 | 0.50 | Apr 29 00:04 |
| `be568b37` | `running` | `iteration: 3`, `best_confidence: 0.167`, NO `started_at` | 3/10 | 0.17 | Apr 29 04:02 |
| `34a54aa9` | `running` | `iteration: 3`, `best_confidence: 0.333`, NO `started_at` | 3/10 | 0.33 | Apr 29 04:02 |
| `ac17b4a2` | `running` | `iteration: 3`, `best_confidence: 0.333`, NO `started_at` | 3/10 | 0.33 | Apr 29 04:02 |
| `8e6cd16e` | `running` | `iteration: 1`, `best_confidence: 0.0`, NO `started_at` | 1/10 | 0.00 | Apr 29 12:27 |

**All 5 lack `started_at` in meta.json — wallclock killer is blind to all of them.**

### Queue State (2026-04-30 23:02)
```
dream_queue.db status breakdown:
  queued:          6044
  completed:          61
  failed:             5
  failed_crash:       8
  killed_wallclock:   8
  running:            5
  done:                2

session_index.db:
  undreamed sessions (last_dreamed_at IS NULL): 347
```

### SYSTEM_PAUSE
Flag file exists at `~/.hermes/state/dream/SYSTEM_PAUSE` — scheduler skips queue processing, running dreams would wait (but they don't check this either).

### Diagnostic Commands
```bash
# Count running dreams (scheduler's method)
find ~/.hermes/state/dream -maxdepth 2 -name "status.txt" -exec sh -c 'd=$(dirname {}); s=$(cat {}); if [ "$s" = "running" ]; then echo "$d"; fi' \; | wc -l

# Show running dream details
for d in ~/.hermes/state/dream/*/; do if [ "$(cat $d/status.txt 2>/dev/null)" = "running" ]; then echo "=== $(basename $d) ==="; cat "$d/meta.json"; fi; done

# Check dream_queue.db
sqlite3 ~/.hermes/state/dream/dream_queue.db "SELECT COUNT(*), status FROM dream_queue GROUP BY status"

# Check session_index.db undreamed
sqlite3 ~/.hermes/state/dream/session_index.db "SELECT COUNT(*) FROM sessions WHERE last_dreamed_at IS NULL OR last_dreamed_at = ''"

# Run scheduler dry-run
python3 ~/.hermes/scripts/dream_scheduler.py --dry-run
```

## Zombie Dream Detection & Recovery (Apr 30 2026 pattern)

**Symptom:** Scheduler reports "5 dreams already running (safety cap)" but no dreams are actually running. Queue backs up, `queued` count stays flat for hours.

**Root cause:** Crashed dreams leave `status.txt=running` but no live process. `count_running_dreams()` reads `status.txt` files directly — not the queue DB — so dead dreams are counted against the 5-concurrent safety cap. **Silent blocking bug** — no errors reported, just queue stalls.

**Diagnosis:**
```bash
# Confirms zombie: status.txt=running but no process
ps aux | grep -E "dream_loop_v3|chat -q" | grep -v grep | wc -l
# OR compare:
ls -lt ~/.hermes/state/dream/*/status.txt | head -10  # running + old timestamps = zombie

# Queue vs status.txt mismatch
sqlite3 ~/.hermes/state/dream/dream_queue.db "SELECT status, COUNT(*) FROM dream_queue GROUP BY status"
```

**Recovery (fix both status.txt AND queue DB together):**
```bash
# 1. Mark dead dreams as failed_crash in status.txt
for id in $(ls -d ~/.hermes/state/dream/*/); do
  if [ "$(cat $id/status.txt 2>/dev/null)" = "running" ]; then
    pid=$(pgrep -f "dream_loop_v3.*$(basename $id)" 2>/dev/null | head -1)
    if [ -z "$pid" ]; then
      echo "ZOMBIE: $(basename $id)"
      echo "failed_crash" > "$id/status.txt"
    fi
  fi
done

# 2. Sync queue DB
sqlite3 ~/.hermes/state/dream/dream_queue.db \
  "UPDATE dream_queue SET status='failed_crash', completed_at=datetime('now') WHERE status='running';"
```

## SYSTEM_PAUSE Stall Pattern

Running dreams check for `~/.hermes/state/dream/SYSTEM_PAUSE` between every MCTS operation. When present:
```
[THROTTLE] SYSTEM_PAUSE detected — waiting for removal...
```
They sleep 30s and re-check in a loop — effectively frozen. Scheduler also skips queue processing.

```bash
ls -la ~/.hermes/state/dream/SYSTEM_PAUSE  # exists = everything frozen
rm ~/.hermes/state/dream/SYSTEM_PAUSE       # resume
```

## Distillation Stall

After `[TERM] Wrapping up` + `[DISTILLATION] Running...`, a dream is in JSON serialization. `status.txt` still shows `running`. If this persists >60s with no further log output, the process died — treat as `failed_crash`.

## Verifying a Dream is Genuinely Alive

```bash
# Must see: process running AND recent log writes AND iteration advancing
ps aux | grep -E "dream_loop_v3" | grep -v grep | wc -l
tail -3 ~/.hermes/state/dream/<id>/dream_output.log
cat ~/.hermes/state/dream/<id>/meta.json | python3 -c "import json,sys; m=json.load(sys.stdin); print(f'iter={m.get(\"iteration\")}')"
```

## Queue DB Never Drains (Apr 28 fix reference)

`sync_dream_status()` is the completion detector — updates queue DB when `meta.json status=done/completed`. If a dream has `status.txt=done` but queue DB shows `running`, the sync function is not firing or the condition check failed. Normal completion drains through `mark_completed()` → `_sync_queue_status()` path.
