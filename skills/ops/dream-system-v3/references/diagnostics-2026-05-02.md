# Dream System v3 — Diagnostic Snapshot 2026-05-02

## 00:00 Scheduler Cycle

### Pre-run state
```
[WALLCLOCK KILL] 1c1d6e45 ran for 59min > 30min — killing
[WALLCLOCK KILL] d1845c37 ran for 59min > 30min — killing
[WALLCLOCK KILL] c7c7b454 ran for 30min > 30min — killing
[WALLCLOCK KILL] 7a9c15c3 ran for 30min > 30min — killing
[SCHEDULER] Killed 4 stale dreams
[SCHEDULER] Resource check: available — Resources OK, 3 slot(s) available
[SCHEDULER] Starting queued dream: bf944ad1
[SPAWN] Started dream bf944ad1 via subprocess
[SCHEDULER] Starting queued dream: 9eeeb969
[SPAWN] Started dream 9eeeb969 via subprocess
[SCHEDULER] Starting queued dream: 6494fc1e
[SPAWN] Started dream 6494fc1e via subprocess
```

### Running processes at cycle start (9 PIDs total)
| PID | Dream ID | Notes |
|-----|----------|-------|
| 46708 | 39246eac | Zombie — THROTTLE on SYSTEM_PAUSE since Apr29, status.txt=killed_wallclock |
| 46710 | ea132047 | Zombie — THROTTLE on SYSTEM_PAUSE since Apr29, status.txt=killed_wallclock |
| 58686 | 1c1d6e45 | Zombie — THROTTLE on SYSTEM_PAUSE, status.txt=killed_wallclock |
| 58687 | d1845c37 | Zombie — THROTTLE on SYSTEM_PAUSE, status.txt=killed_wallclock |
| 80680 | c7c7b454 | Zombie — THROTTLE on SYSTEM_PAUSE, status.txt=killed_wallclock |
| 80681 | 7a9c15c3 | Zombie — THROTTLE on SYSTEM_PAUSE, status.txt=killed_wallclock |
| 90184 | bf944ad1 | New — just spawned, iter 1/10 |
| 90188 | 9eeeb969 | New — just spawned, iter 1/10 |
| 90189 | 6494fc1e | New — just spawned, iter 1/10 |

### Root cause: SYSTEM_PAUSE existed since 2026-04-29 12:44
```bash
$ stat ~/.hermes/state/dream/SYSTEM_PAUSE
Modify: 2026-04-29 12:44:44.857301987 +0700
 Birth: 2026-04-29 12:44:44.857301987 +0700
```

All 9 dream PIDs were blocked on:
```
[THROTTLE] SYSTEM_PAUSE detected — waiting for removal...
[THROTTLE] Still paused after NNNNs...
```

The 4 wallclock kills (1c1d6e45, d1845c37, c7c7b454, 7a9c15c3) updated status.txt + queue DB correctly, but the OS processes were never SIGKILLed — they survived because they were blocked on SYSTEM_PAUSE, not because they were doing MCTS work.

### Manual intervention taken
1. **Removed SYSTEM_PAUSE** — `rm ~/.hermes/state/dream/SYSTEM_PAUSE`
2. **Killed 6 zombie PIDs** — `kill -9 46708 46710 58686 58687 80680 80681`
3. **Ran scheduler cycle manually** — 2 more dreams started (b0122f44, b28dba1c)

### Post-fix state (5 active dreams)
| Dream ID | Status | Activity |
|----------|--------|----------|
| bf944ad1 | running | MCTS iter 2/10 |
| 9eeeb969 | running | MCTS iter 2/10 |
| 6494fc1e | running | MCTS iter 2/10 |
| b0122f44 | running | MCTS iter 1/10 |
| b28dba1c | running | MCTS iter 1/10 |

### Queue state
```
completed: 94
done: 2
failed: 5
failed_crash: 24
killed_wallclock: 99
queued: 6201
running: 5
```

## Key findings

### Finding 1: Wallclock kill does NOT SIGKILL the process
`sync_dream_status()` updates metadata but the `Popen` handle is discarded. A dream blocked on SYSTEM_PAUSE during a wallclock kill survives as a zombie. Must maintain PID handle map and call `proc.kill()` in wallclock kill path.

### Finding 2: SYSTEM_PAUSE leaves no automated recovery path
If SYSTEM_PAUSE is created for debugging and forgotten, all dreams freeze indefinitely. The scheduler cycle still runs (SKIP section), but no dreams start. Only manual removal recovers the system.

### Finding 3: Scheduler script is not directly executable
`~/.hermes/scripts/dream_scheduler.py` is permission 644 (not executable). Cron dispatches via `hermes cron run`. Manual invocation requires:
```bash
cd ~/.hermes
HERMES_CONFIG=~/.hermes/config.yaml python3 -c "
import sys; sys.path.insert(0, 'scripts')
from dream_scheduler import run_scheduler_cycle
print(run_scheduler_cycle())
"
```
