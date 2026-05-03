# Dream Dashboard — UI/UX Implementation Notes

**Date:** 2026-05-03
**Status:** v2 implemented, working

## What the Dashboard Does

`~/.hermes/scripts/dream_insights_dashboard.py` — Rich TUI dashboard for the dream system.
Run: `dream-dashboard` (wrapper) or `python3 ~/.hermes/scripts/dream_insights_dashboard.py`

## CRITICAL: Data Source Architecture

**The filesystem scan (`collect_dreams()`) finds NOTHING for v3 dreams.**

Dream directories are named `cron_<8-char-hash>_<YYYYMMDD>_<HHMMSS>` (e.g., `cron_98adb85e9819_20260501_010659`).
The regex `[a-f0-9]{8}` only matches v2-style 8-char hex names. v3 dream dirs don't match.

**All real data comes from `dream_queue.db`**, which is the source of truth for:
- Completed/failed/running/queued counts
- `status` field (includes `killed_wallclock`, `completed_killed`, `failed_crash`, etc.)
- `grade` and `confidence` (v3 grade is in the queue DB, not filesystem)
- Throughput calculations (completions per day)

`collect_dreams()` reads filesystem dirs — it populates `dreams` list but these are stale/empty for v3.
MCTS Performance panel now falls back to queue DB `grade` when filesystem scan returns nothing.

**Queue DB schema (confirmed 2026-05-03):**
```
Columns: queue_id, session_id, dream_id, dream_question, grade, resource_cost,
         priority, created_at, started_at, completed_at, status
```
Status values: `queued`, `running`, `completed`, `done`, `killed_wallclock`, `failed_crash`,
`completed_killed`, `failed`, `incomplete`, `stale`

**`killed_wallclock` count:** `SELECT COUNT(*) FROM dream_queue WHERE status = 'killed_wallclock'`
— 140 wallclock kills found (42% of all completion attempts — system is severely constrained by timeout)

## Architecture

**Data sources:**
- `state/dream/<id>/meta.json` — v2 confidence, status, iteration
- `state/dream/<id>/status.txt` — v2 status text
- `state/dream/<id>/insights.json` — v2 insights list
- `state/dream/<id>/dream_output.log` — v3 MCTS output + JSON result block
- `state/dream/session_index.db` — sessions table with dream_potential
- `state/dream/dream_queue.db` — queued/running/completed dreams

**Parsers:**
- `parse_v3_dream()` — reads `dream_output.log` for JSON result block (regex `\{"dream_id".*?\}`)
  - Status: "completed" if JSON found, "crashed" if Traceback present, "incomplete" otherwise
- `parse_v2_dream()` — reads `meta.json` + `status.txt` + `insights.json`
- `parse_log_file()` — legacy log parser for `state/dream/logs/*.log`

## Key Implementation Details

### v3 JSON Extraction
```python
for m in re.finditer(r'\{.*?"dream_id".*?\}', content, re.DOTALL):
    candidate = json.loads(m.group())
    if "dream_id" in candidate:
        data = candidate
```
Must use DOTALL flag. Non-greedy `.*?` can miss nested JSON — greedy `.*` then json.loads validation is more robust.

### Zombie Detection Fix
Old bug: used `created_at` (queue creation time) instead of `started_at` (when dream actually began).
```python
# CORRECT — use started_at when available
start_str = r.get("started_at") or r.get("created_at")
```
Running dreams created days ago but only started minutes ago should NOT be flagged as zombies.

### Session Table Date Overflow
Rich table column without `no_wrap=True` causes datetime strings to wrap mid-date.
Fix: add `no_wrap=True, width=20` to Created column.

### Sparkline Rendering
Block characters (▁▂▃▄▅▆▇█) via `chr(0x2581 + height)`. Scale to 5 heights max (0-4).
```python
height = round((v / max_v) * 4)
char = chr(0x2581 + height) if height > 0 else "·"
```
Max value → full block (█, height=4). Zero → middle dot (·).

### Insight Text Truncation
Rich tables wrap at word boundaries by default, but when a column is too narrow for any word, it cuts mid-word.
Fix: pre-truncate insight text to 120 chars before adding to table row.
```python
table.add_row(..., r["text"][:120])
```

### MCTS Confidence Parsing
`dream_output.log` JSON block has `confidence` key (not `best_confidence`). v2 uses `meta.json` with `best_confidence`.
```python
# v3
confidence = data.get("confidence", 0)
# v2
confidence = meta.get("confidence") or meta.get("best_confidence", 0)
```

## Panels and What They Show

| Panel | Key Stats | Alert Threshold |
|-------|-----------|-----------------|
| Health Summary | Total, Success, Failed, Crashed, Stale, Running, Queued, Success Rate, Terminal Rate, Avg Conf 7d, Wallclock Kills | — |
| Trend Sparklines | 7-day completion and session counts with histogram bars | — |
| Queue Health | Backlog severity, avg/max wait, today's throughput, zombie count | qs>2000=red, avg_wait>48h=yellow, zombies>0=red |
| Session Index | Scoring coverage, potential distribution bars | unscored>200=red |
| MCTS Performance | Avg confidence, tier breakdown (0.75+/0.50-0.74/0.01-0.49/0.00), insights/dream | avg_conf<0.5=yellow |
| Actionable Alerts | Red/yellow specific alerts with exact IDs/numbers | Any threshold breach |

## CLI Flags

```
--all       Full dashboard (default)
--errors    Error breakdown only
--queue     Dream queue only
--sessions  Session index only
--runs      Dream runs only
--insights  Recent insights only
```

## Common Issues Found During v2 Development

1. **`error` UnboundLocalError** — `parse_v3_dream()` referenced `error` variable before assignment. Add `error = None` before the if/elif block.

2. **Zombie false positives** — Running dreams started <2h ago but created days earlier were flagged as zombies. Fixed by preferring `started_at` over `created_at`.

3. **last_dreamed_at always "—"** — `session_index.db` had no sessions with `last_dreamed_at` set. This is expected — the field tracks sessions that have been processed by a dream. The column exists but is just empty in current data.

4. **Insights truncated mid-word** — Rich table wraps at word boundaries only when the full word fits. With narrow columns and long tokens (file paths), words get cut.
   Fix: pre-truncate with word-boundary detection:
   ```python
   def truncate(text: str, max_len: int) -> str:
       if len(text) <= max_len:
           return text
       truncated = text[:max_len]
       last_space = truncated.rfind(" ")
       if last_space > max_len * 0.6:
           return truncated[:last_space] + "…"
       return truncated + "…"
   ```

5. **`from rich.sparkline import Sparkline`** — Removed; not needed. Custom spark_str() function with block chars is simpler and more controllable.

6. **Throughput week-slicing was REVERSED** — `vals_14[:7]` was labeled "this week" but contained the oldest 7 days. `days_14` is oldest→newest (range(13,-1,-1)), so `[:7]` = last week, `[7:]` = this week. Fixed.

7. **Stale bytecode (Python 3.14)** — `__pycache__/*.cpython-314.pyc` caused the module to load the old compiled version even after edits. Clear with: `find ~/.hermes/scripts -name "*dashboard*" -name "*.pyc" -delete`

8. **`collect_dreams()` filesystem scan is dead code for v3** — The MCTS Performance panel was showing no data because the filesystem scan finds 0 v3 dreams. Fixed by using queue DB `grade` as confidence proxy when `dreams` list is empty.

9. **`killed_wallclock` status not in `status_counts`** — The `compute_trends()` function never added `killed_wallclock` to the status counter, so the alert `"killed_wallclock > 50"` never triggered. Fixed: added to status_counts.

10. **Health Score scoping bug** — `compute_trends()` returned `scoring_pct` but the health score formula in `panel_health_score()` referenced `scored` and `sessions` variables not in scope. Fixed by injecting `trends["scoring_pct"]` before calling panels.

## New Panels Added (2026-05-03)

### System Health Score (0-100 composite)
Components: queue depth 25% + success rate 25% + kill rate 25% + scoring coverage 25%
```python
q_score = max(0, 50 - round(len(queue) / 100))   # queue penalty
s_score = success_rate                            # success rate directly
k_score = max(0, 100 - 2 * kill_rate)            # kill rate penalty
sc_score = scoring_pct                            # scoring coverage
health = (q_score + s_score + k_score + sc_score) / 4
```
Score ≥80=green/GOOD, ≥50=yellow/WARNING, <50=red/CRITICAL.

### Throughput Comparison (14-day window)
Week-over-week dream completion comparison using queue DB `completed_at` timestamps.
"this week" = most recent 7 days, "last week" = preceding 7 days.
Shows delta: `↑ +N wk/wk` or `↓ -N wk/wk`.

### Wallclock Kill Rate
Kill rate = `killed_wallclock / (completed + killed_wallclock) * 100`.
Displayed in Queue Health panel. Threshold: >30% = red alert.
Current state: 140 kills / 333 total = **42% kill rate** — extremely high, indicates timeout too short.

## Adding New Stats

To add a new stat panel:
1. Compute the stat in `compute_trends()` or `compute_session_stats()`
2. Add a new `panel_*()` function returning a `Panel`
3. Add to the `console.print(Columns([...]))` call in `main()`
4. If it needs new DB queries, add them to `collect_queue()` or `collect_sessions()`
5. If the stat is needed in multiple panels, add it to `trends` dict in `compute_trends()` so it's computed once

## File Locations

- Dashboard script: `~/.hermes/scripts/dream_insights_dashboard.py`
- Wrapper: `~/.local/bin/dream-dashboard`
- Session DB: `~/.hermes/state/dream/session_index.db`
- Queue DB: `~/.hermes/state/dream/dream_queue.db`
- Dream dirs: `~/.hermes/state/dream/<id>/`
