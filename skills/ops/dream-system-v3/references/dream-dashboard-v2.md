# Dream Dashboard — v2 Architecture

**File:** `~/.hermes/scripts/dream_insights_dashboard.py`
**Wrapper:** `~/.local/bin/dream-dashboard`

## What the Dashboard Measures

The dashboard reads from three live data sources and derives actionable health signals.

### Data Sources

| Source | What it contains | Key field |
|--------|-----------------|-----------|
| `state/dream/<id>/dream_output.log` | v3 MCTS JSON result block | `confidence`, `insights[]`, `iterations` |
| `state/dream/<id>/meta.json` | v2 metadata | `confidence`, `best_confidence`, `iteration` |
| `state/dream/<id>/status.txt` | Raw status string | running/completed/failed |
| `state/dream/session_index.db` | Sessions with potential | `dream_potential`, `message_count`, `error_count` |
| `state/dream/dream_queue.db` | Queue entries | `status`, `grade`, `priority`, `created_at`, `started_at` |

### Queue DB Schema (critical — two time fields)

```
dream_queue columns:
  queue_id, session_id, dream_id, dream_question, grade, resource_cost,
  priority, created_at, started_at, completed_at, status
```

- `created_at` = when dream was **enqueued** (session indexed + graded)
- `started_at` = when scheduler actually **dispatched** the dream subprocess
- **Zombie detection must use `started_at`**, not `created_at`. Using `created_at` causes false positives: a dream just dispatched (e.g. 2026-05-03 01:00) shows `created_at` = days ago (when it was queued), making it look like a zombie running >48h when it's actually brand new.

```python
# CORRECT zombie detection (use started_at, fallback to created_at)
start_str = r.get("started_at") or r.get("created_at")
start_dt = datetime.fromisoformat(start_str)
if (now - start_dt).total_seconds() > 7200:  # 2h threshold
    zombies.append(r)
```

### Derived Health Stats

| Stat | How computed |
|------|-------------|
| Success rate | `success_all / (success + failed + crashed + stale) * 100` |
| Terminal rate | `terminal / total * 100` (how many reached a final state) |
| Avg conf 7d | Mean `confidence` of dreams completed in last 7 days |
| Kill rate | `killed_wallclock / total * 100` |
| Queue backlog severity | Red: >2000, Yellow: >500, Green: <=500 |
| Avg queue wait | Mean `(now - created_at)` for all queued entries |
| Scoring coverage | `scored / total_sessions * 100` |

### Insight Categorization

Insights from `dream_output.log` JSON block (v3) or `insights.json` (v2) are keyword-categorized:

```python
debug_kw  = ["error", "traceback", "crash", "bug", "fix", "debug",
             "timeout", "kill", "pipe", "buffer", "deadlock", "zombie"]
arch_kw   = ["architecture", "schema", "plugin", "hook", "memory",
             "system", "engine", "mcts", "design", "thread", "queue"]
data_kw   = ["database", "db", "table", "column", "query", "sql",
             "json", "path", "org2", "session", "index", "sqlite"]
```

## Adding a New Panel

1. Compute the stat in `compute_trends()` or `compute_session_stats()`
2. Add a `Panel()` or `Columns()` builder function
3. Call it from `main()` under the `if show_all:` block

Panel pattern:
```python
def panel_my_metric(trends):
    lines = [
        f"[dim]Primary: {trends['my_metric']}[/dim]",
        f"[dim]Secondary: {trends['other']}[/dim]",
    ]
    return Panel("\n".join(lines),
                 title="[bold]My Metric[/bold]",
                 border_style="cyan",
                 padding=(1, 2),
                 width=50)
```

## Completion Sparkline (Unicode bar chart)

Uses Unicode block characters for a no-dependency sparkline:

```python
def spark_str(vals, keys, color):
    if not vals:
        return "[dim]no data[/dim]"
    max_v = max(vals) or 1
    scaled = [min(9, round((v / max_v) * 9)) for v in vals]
    block  = "".join(chr(0x2581 + s) for s in scaled)
    total_c = sum(vals)
    return f"[{color}]{block}[/{color}]  [dim]{keys[0]}→{keys[-1]} ({total_c} total)[/dim]"
```

## Running Specific Views

```bash
dream-dashboard              # full dashboard
dream-dashboard --runs       # dream runs only
dream-dashboard --queue      # queue only
dream-dashboard --sessions   # session index only
dream-dashboard --errors     # error breakdown only
dream-dashboard --insights   # recent insights only
```
