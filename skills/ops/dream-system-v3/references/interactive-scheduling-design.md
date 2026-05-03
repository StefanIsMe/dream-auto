# Interactive Cron Scheduling — STEP 7 Design

**Date:** 2026-04-30
**Status:** Designed, not yet implemented

## Problem

SETUP.md hardcodes `*/30 * * * *` for dream-scheduler and `0 */6 * * *` for session-indexer.
This assumes every user's machine is equally idle at the same times.
Also: the original SETUP.md had broken Hermes cron CLI syntax (`--command`/`--schedule` flags that don't exist),
causing registered schedules to be mangled. Wysie's PR #1 fixed the syntax, but existing installs still have wrong schedules.

## Solution — Three Scheduling Modes

Replace the hardcoded cron registration in STEP 7 with an interactive choice:

```
echo "=== Cron Job Scheduling ==="
echo "How should Dream Auto schedule its background jobs?"
echo ""
echo "  [1] Recommended — scheduler every 30 min, indexer every 6h"
echo "      (low resource impact, good for most users)"
echo ""
echo "  [2] Custom — I'll specify the schedule myself"
echo "      (you choose the times)"
echo ""
echo "  [3] Auto-detect — scan my Hermes history and recommend optimal times"
echo "      (uses LLM API calls, finds when my machine is most idle)"
echo ""
read -p "Select [1/2/3]: " SCHEDULE_CHOICE
```

### Option 1: Recommended Defaults
```bash
SCHEDULER_CRON="*/30 * * * *"
INDEXER_CRON="0 */6 * * *"
```

### Option 2: User-Specified
```bash
echo "Enter scheduler cron expression (e.g. */30 * * * *):"
read SCHEDULER_CRON
echo "Enter indexer cron expression (e.g. 0 */6 * * *):"
read INDEXER_CRON
```

### Option 3: Auto-Detect via LLM
```bash
echo "Analyzing Hermes session history..."
RECOMMENDATION=$("$HERMES_BIN" chat -q \
    "Based on the user's typical active hours from their Hermes session history, \
    recommend the optimal cron schedule for a background dream scheduler \
    (runs every 30min ideally) and a session indexer (runs every 6h ideally). \
    Output ONLY the cron expressions in this exact format: \
    SCHEDULER=<expr> INDEXER=<expr>" 2>/dev/null)
SCHEDULER_CRON=$(echo "$RECOMMENDATION" | grep -oP 'SCHEDULER=\K[^ ]+')
INDEXER_CRON=$(echo "$RECOMMENDATION" | grep -oP 'INDEXER=\K[^ ]+')
```

### Then Register Both Jobs
```bash
"$HERMES_BIN" cron create "$SCHEDULER_CRON" \
    --name "dream-scheduler" \
    --script "$HERMES_HOME/scripts/dream_scheduler.py" \
    "Run the Dream Auto scheduler script." \
    2>/dev/null || echo "dream-scheduler already registered (skipping)"

"$HERMES_BIN" cron create "$INDEXER_CRON" \
    --name "session-indexer" \
    --script "$HERMES_HOME/scripts/session_indexer.py" \
    "Run the Dream Auto session indexer." \
    2>/dev/null || echo "session-indexer already registered (skipping)"
```

## Implementation Notes

- Option 3 requires `hermes chat -q` to work (LLM API call + cost)
- The auto-detect prompt should parse `hermes sessions list` history to find quiet hours
- Consider caching the LLM's recommendation in `~/.hermes/state/dream/.schedule Recommendation` to avoid re-calling on re-runs
- After registration, always display the actual registered schedule for user confirmation

## PR History

- **PR #1** (Wysie): Fixed Hermes cron CLI syntax (`--command`/`--schedule` → positional schedule + `--script`)
- **Next PR** (pending): Interactive scheduling choice in STEP 7
