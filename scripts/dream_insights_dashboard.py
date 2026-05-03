#!/usr/bin/env python3
"""
Dream Insights Dashboard — v2
Run: python3 ~/.hermes/scripts/dream_insights_dashboard.py

Flags:
  --errors     Show only error breakdown
  --queue      Show only dream queue
  --sessions   Show only session index
  --runs       Show only dream runs
  --insights   Show only recent insights
  --all        Full dashboard (default)
"""

import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from rich.console import Console, Group
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich import box
import argparse

DREAM_STATE = Path.home() / ".hermes" / "state" / "dream"
SESSION_DB  = DREAM_STATE / "session_index.db"
QUEUE_DB    = DREAM_STATE / "dream_queue.db"
LOGS_DIR    = DREAM_STATE / "logs"
console     = Console()


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_dt(dt_str_or_dt):
    if not dt_str_or_dt:
        return "—"
    if isinstance(dt_str_or_dt, str):
        try:
            dt = datetime.fromisoformat(dt_str_or_dt)
        except Exception:
            return dt_str_or_dt[:16]
    else:
        dt = dt_str_or_dt
    return dt.strftime("%Y-%m-%d %H:%M")


def fmt_age(dt_str_or_dt) -> str:
    """Return human-readable age like '2h', '4d'."""
    if not dt_str_or_dt:
        return "—"
    if isinstance(dt_str_or_dt, str):
        try:
            dt = datetime.fromisoformat(dt_str_or_dt)
        except Exception:
            return "?"
    else:
        dt = dt_str_or_dt

    now = datetime.now(timezone(timedelta(hours=7)))
    delta = now - dt
    total_sec = delta.total_seconds()
    if total_sec < 0:
        return "future"
    if total_sec < 3600:
        return f"{int(total_sec / 60)}m"
    if total_sec < 86400:
        return f"{int(total_sec / 3600)}h"
    return f"{int(total_sec / 86400)}d"


def read_json_file(path):
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def truncate(text: str, max_len: int) -> str:
    """Truncate at word boundary nearest to max_len."""
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > max_len * 0.6:
        return truncated[:last_space] + "…"
    return truncated + "…"


def dir_mtime(dream_dir: Path):
    try:
        mtimes = [f.stat().st_mtime for f in dream_dir.iterdir() if f.is_file()]
        return datetime.fromtimestamp(max(mtimes), tz=timezone(timedelta(hours=7))) if mtimes else None
    except Exception:
        return None


def status_style(s: str) -> str:
    return {
        "success": "green", "completed": "green", "done": "green",
        "failed": "red", "failed_crash": "red", "crashed": "red",
        "stale": "yellow", "completed_stale": "yellow",
        "running": "blue", "queued": "yellow",
        "killed_wallclock": "red",
        "incomplete": "dim", "unknown": "dim",
    }.get(s, "white")


def score_bar(counts: dict, total: int, width: int = 12) -> str:
    """Return an ASCII bar for a frequency distribution."""
    if total == 0:
        return " " * width
    parts = []
    for key in ["success", "failed", "crashed", "stale", "incomplete", "running"]:
        if key in counts and counts[key] > 0:
            frac = counts[key] / total
            bars = max(1, round(frac * width))
            parts.append(f"[{status_style(key)}]{'█' * bars}[/]")
    return "".join(parts)


# ── Parsers ───────────────────────────────────────────────────────────────────

def parse_v2_dream(dream_dir: Path):
    meta = read_json_file(dream_dir / "meta.json") or {}
    status_txt = ""
    if (dream_dir / "status.txt").exists():
        status_txt = (dream_dir / "status.txt").read_text(encoding="utf-8").strip()
    insights = read_json_file(dream_dir / "insights.json") or []
    failures = read_json_file(dream_dir / "failures.json") or []

    raw = (status_txt or meta.get("status", "")).lower().strip()
    if raw in ("completed", "completed_success", "done"):
        status = "success"
    elif raw in ("failed", "failed_crash", "failed_restart", "circuit_breaker",
                 "completed_killed", "health_check_failed"):
        status = "failed"
    elif raw in ("completed_stale", "stale_completed", "completed_empty"):
        status = "stale"
    else:
        status = raw or "unknown"

    return {
        "dream_id":      dream_dir.name,
        "version":       "v2",
        "status":        status,
        "confidence":    meta.get("confidence") or meta.get("best_confidence", 0),
        "insights_count": len(insights) if isinstance(insights, list) else 0,
        "failures_count": len(failures) if isinstance(failures, list) else 0,
        "completed_at":  dir_mtime(dream_dir),
        "error":         None,
        "iterations":    meta.get("iteration", 0),
    }


def parse_v3_dream(dream_dir: Path):
    log_path = dream_dir / "dream_output.log"
    if not log_path.exists():
        return None
    content = log_path.read_text(encoding="utf-8", errors="ignore")

    data = None
    for m in re.finditer(r'\{.*?"dream_id".*?\}', content, re.DOTALL):
        try:
            candidate = json.loads(m.group())
            if "dream_id" in candidate:
                data = candidate
        except Exception:
            continue

    has_traceback = "Traceback" in content
    error = None
    if has_traceback and not data:
        status = "crashed"
        lines = content.splitlines()
        error = next((line.strip() for line in reversed(lines) if line.strip().startswith("File ")), "Traceback occurred")
    elif data:
        status = "completed"
    else:
        status = "incomplete"

    mtime = datetime.fromtimestamp(log_path.stat().st_mtime, tz=timezone(timedelta(hours=7)))
    return {
        "dream_id":       dream_dir.name,
        "version":        "v3",
        "status":         status,
        "confidence":     data.get("confidence", 0) if data else 0,
        "insights_count": len(data.get("insights", [])) if data else 0,
        "failures_count": len(data.get("failures", [])) if data else 0,
        "completed_at":   mtime,
        "error":          error if has_traceback else None,
        "iterations":     data.get("iterations", 0) if data else 0,
    }


def parse_log_file(log_path: Path):
    content = log_path.read_text(encoding="utf-8", errors="ignore")
    status = "unknown"
    confidence = 0
    for line in content.splitlines():
        if "Completed after" in line or "Final distillation" in line:
            status = "completed"
        elif "Confidence threshold met" in line:
            m = re.search(r'(\d+)%', line)
            if m:
                confidence = int(m.group(1)) / 100.0
        elif "Traceback" in line:
            status = "crashed"
    mtime = datetime.fromtimestamp(log_path.stat().st_mtime, tz=timezone(timedelta(hours=7)))
    return {
        "dream_id":       log_path.stem,
        "version":        "log",
        "status":         status,
        "confidence":     confidence,
        "insights_count": 0,
        "failures_count": 0,
        "completed_at":   mtime,
        "error":          None,
        "iterations":     0,
    }


# ── Collectors ────────────────────────────────────────────────────────────────

def collect_dreams():
    dreams = []
    seen   = set()
    for entry in os.listdir(DREAM_STATE):
        dream_dir = DREAM_STATE / entry
        if not dream_dir.is_dir() or not re.match(r'^[a-f0-9]{8}$', entry):
            continue
        seen.add(entry)
        v3 = parse_v3_dream(dream_dir)
        dreams.append(v3 if v3 else parse_v2_dream(dream_dir))

    if LOGS_DIR.exists():
        for log_file in LOGS_DIR.iterdir():
            if log_file.is_file() and log_file.suffix == ".log" and log_file.stem not in seen:
                dreams.append(parse_log_file(log_file))
                seen.add(log_file.stem)
    return dreams


def collect_queue():
    if not QUEUE_DB.exists():
        return []
    conn  = sqlite3.connect(str(QUEUE_DB))
    conn.row_factory = sqlite3.Row
    cur   = conn.cursor()
    cur.execute("SELECT * FROM dream_queue ORDER BY created_at DESC")
    rows  = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def collect_sessions():
    if not SESSION_DB.exists():
        return []
    conn  = sqlite3.connect(str(SESSION_DB))
    conn.row_factory = sqlite3.Row
    cur   = conn.cursor()
    cur.execute("SELECT * FROM sessions ORDER BY created_at DESC LIMIT 500")
    rows  = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def collect_recent_insights(limit=20, days_back=7):
    cutoff  = datetime.now(timezone(timedelta(hours=7))) - timedelta(days=days_back)
    insights = []

    for entry in os.listdir(DREAM_STATE):
        dream_dir = DREAM_STATE / entry
        if not dream_dir.is_dir() or not re.match(r'^[a-f0-9]{8}$', entry):
            continue

        completed_at = None
        status       = "unknown"
        dream_insights = []

        # v3 path
        log_path = dream_dir / "dream_output.log"
        if log_path.exists():
            mtime   = datetime.fromtimestamp(log_path.stat().st_mtime, tz=timezone(timedelta(hours=7)))
            completed_at = mtime
            content  = log_path.read_text(encoding="utf-8", errors="ignore")
            has_tb   = "Traceback" in content
            data     = None
            for m in re.finditer(r'\{.*?"dream_id".*?\}', content, re.DOTALL):
                try:
                    candidate = json.loads(m.group())
                    if "dream_id" in candidate:
                        data = candidate
                except Exception:
                    continue
            if data and not has_tb:
                status       = "success"
                dream_insights = data.get("insights", [])
            elif has_tb:
                status = "crashed"

        # v2 fallback
        if status == "unknown":
            meta = read_json_file(dream_dir / "meta.json") or {}
            status_txt = (dream_dir / "status.txt").read_text(encoding="utf-8").strip() if (dream_dir / "status.txt").exists() else ""
            raw = (status_txt or meta.get("status", "")).lower().strip()
            if raw in ("completed", "completed_success", "done"):
                status = "success"
            v2_file = dream_dir / "insights.json"
            if v2_file.exists():
                try:
                    v2 = json.loads(v2_file.read_text())
                    if isinstance(v2, list):
                        dream_insights = v2
                except Exception:
                    pass
            completed_at = dir_mtime(dream_dir) or completed_at

        if status != "success" or not dream_insights:
            continue
        if completed_at and completed_at < cutoff:
            continue

        for ins in dream_insights:
            if isinstance(ins, str) and ins.strip():
                insights.append({
                    "dream_id":     entry,
                    "completed_at": completed_at,
                    "text":         ins.strip(),
                    "version":      "v3" if log_path.exists() else "v2",
                })

    insights.sort(key=lambda x: x["completed_at"] or datetime.min.replace(tzinfo=timezone(timedelta(hours=7))), reverse=True)
    return insights[:limit]


# ── Stat generators ────────────────────────────────────────────────────────────

def compute_trends(dreams, queue):
    """Derive trend / health stats from collected data."""
    now = datetime.now(timezone(timedelta(hours=7)))
    total = len(dreams)

    status_counts = Counter(d["status"] for d in dreams)

    # Completed in last 7 days
    cutoff_7d = now - timedelta(days=7)
    cutoff_1d = now - timedelta(days=1)

    completed_7d = [d for d in dreams if d["status"] == "completed"
                    and d["completed_at"] and d["completed_at"] >= cutoff_7d]
    completed_1d = [d for d in dreams if d["status"] == "completed"
                    and d["completed_at"] and d["completed_at"] >= cutoff_1d]

    success_7d   = [d for d in completed_7d if d["confidence"] and d["confidence"] >= 0.6]
    avg_conf_7d  = sum(d["confidence"] for d in completed_7d) / len(completed_7d) if completed_7d else 0
    high_conf    = sum(1 for d in completed_7d if d["confidence"] and d["confidence"] >= 0.75)

    # Completions per day (last 7 days from queue DB)
    completions_by_day = {}
    for entry in collect_queue():
        if entry.get("status") not in ("completed", "done"):
            continue
        ca = entry.get("completed_at")
        if not ca:
            continue
        try:
            dt = datetime.fromisoformat(ca)
        except Exception:
            continue
        day = dt.strftime("%m-%d")
        completions_by_day[day] = completions_by_day.get(day, 0) + 1

    # Sparkline data (last 7 days)
    spark_keys = [(now - timedelta(days=i)).strftime("%m-%d") for i in range(6, -1, -1)]
    spark_vals = [completions_by_day.get(k, 0) for k in spark_keys]

    # Queue stats
    queued   = [q for q in queue if q.get("status") == "queued"]
    running  = [q for q in queue if q.get("status") == "running"]
    queued_ages = []
    for q in queued:
        try:
            dt = datetime.fromisoformat(q["created_at"])
        except Exception:
            continue
        age_h = (now - dt).total_seconds() / 3600
        queued_ages.append(age_h)

    # Detect zombie running dreams (running > 2h from actual start time)
    # Use started_at if available, else created_at
    zombies = []
    for r in running:
        start_str = r.get("started_at") or r.get("created_at")
        if not start_str:
            continue
        try:
            start_dt = datetime.fromisoformat(start_str)
        except Exception:
            continue
        if (now - start_dt).total_seconds() > 7200:
            zombies.append(r)

    avg_queue_age_h = sum(queued_ages) / len(queued_ages) if queued_ages else 0
    max_queue_age_h = max(queued_ages) if queued_ages else 0

    # Throughput: this week vs last week (from queue completions)
    completed_all = [q for q in queue if q.get("status") in ("completed", "done")]
    by_day_full = {}
    for q in completed_all:
        ca = q.get("completed_at")
        if not ca:
            continue
        try:
            dt = datetime.fromisoformat(ca)
            by_day_full[dt.strftime("%Y-%m-%d")] = by_day_full.get(dt.strftime("%Y-%m-%d"), 0) + 1
        except Exception:
            continue

    days_14 = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]
    vals_14 = [by_day_full.get(d, 0) for d in days_14]
    # days_14 is oldest->newest; [:7]=oldest half (last week), [7:]=newest half (this week)
    this_week_c = sum(vals_14[7:])   # most recent 7 days
    last_week_c = sum(vals_14[:7])   # prior 7 days
    throughput_delta = this_week_c - last_week_c

    # Wallclock kill rate (from queue DB)
    killed_count = sum(1 for q in queue if q.get("status") == "killed_wallclock")
    terminal_count = (
        sum(1 for q in queue if q.get("status") in ("completed", "done")) +
        sum(1 for q in queue if q.get("status") == "killed_wallclock") +
        sum(1 for q in queue if q.get("status") in ("failed", "failed_crash"))
    )
    kill_rate = (killed_count / terminal_count * 100) if terminal_count else 0
    success_rate = (sum(1 for q in queue if q.get("status") in ("completed", "done")) / terminal_count * 100) if terminal_count else 0
    fail_rate = (sum(1 for q in queue if q.get("status") in ("failed", "failed_crash")) / terminal_count * 100) if terminal_count else 0

    # Queue grade stats
    graded_queue = [q["grade"] for q in queued if q.get("grade") is not None]
    avg_grade_queued = (sum(graded_queue) / len(graded_queue)) if graded_queue else None

    # Health score (0-100)
    # Components: queue depth 25% + success rate 25% + kill rate 25% + scoring coverage 25%
    # Queue: 50 baseline, -1pt per 100 queued, floor 0
    q_score = max(0, 50 - round(len(queued) / 100))
    s_score = success_rate  # 0-100
    k_score = max(0, 100 - kill_rate * 2)  # -2pts per 1% kill rate
    # Scoring coverage: unscored comes from sessions via sess_stats (passed in via extra param)
    # For now, leave scoring component out of the trend-level health score
    # and surface it clearly in the Session Index panel
    sc_pct = 0  # filled in by caller via compute_trends_extra
    health_score = round(q_score * 0.30 + s_score * 0.30 + k_score * 0.40)  # no sc yet

    return {
        "total":          total,
        "status_counts":  dict(status_counts),
        "completed_7d":    len(completed_7d),
        "completed_1d":   len(completed_1d),
        "success_7d":     len(success_7d),
        "avg_conf_7d":    avg_conf_7d,
        "high_conf":      high_conf,
        "spark_vals":      spark_vals,
        "spark_keys":      spark_keys,
        "queue_size":     len(queued),
        "queue_ages":     queued_ages,
        "avg_queue_age_h": avg_queue_age_h,
        "max_queue_age_h": max_queue_age_h,
        "running":        len(running),
        "zombies":        zombies,
        "total_queued":   len(queue),
        "total_completed": sum(1 for q in queue if q.get("status") in ("completed", "done")),
        "failed_queue":   sum(1 for q in queue if q.get("status") in ("failed", "failed_crash")),
        "killed_wallclock": killed_count,
        "kill_rate":      kill_rate,
        "success_rate":   success_rate,
        "fail_rate":      fail_rate,
        "this_week_c":    this_week_c,
        "last_week_c":    last_week_c,
        "throughput_delta": throughput_delta,
        "avg_grade_queued": avg_grade_queued,
        "health_score":   health_score,
    }



def compute_session_stats(sessions):
    scored    = [s for s in sessions if s.get("dream_potential") is not None]
    unscored  = [s for s in sessions if s.get("dream_potential") is None]
    dreamed   = [s for s in sessions if s.get("last_dreamed_at") is not None]

    if scored:
        pots = [s["dream_potential"] for s in scored]
        avg_pot  = sum(pots) / len(pots)
        max_pot  = max(pots)
        high_pot = sum(1 for p in pots if p >= 0.8)
        mid_pot  = sum(1 for p in pots if 0.5 <= p < 0.8)
        low_pot  = sum(1 for p in pots if p < 0.5)
    else:
        avg_pot = max_pot = high_pot = mid_pot = low_pot = 0

    # Sessions per day (last 7 days)
    now = datetime.now(timezone(timedelta(hours=7)))
    by_day = defaultdict(int)
    for s in sessions:
        try:
            dt = datetime.fromisoformat(s["created_at"])
        except Exception:
            continue
        if (now - dt).days <= 7:
            by_day[dt.strftime("%m-%d")] += 1

    spark_sess = [(now - timedelta(days=i)).strftime("%m-%d") for i in range(6, -1, -1)]
    spark_sess_vals = [by_day.get(k, 0) for k in spark_sess]

    return {
        "total":      len(sessions),
        "scored":     len(scored),
        "unscored":   len(unscored),
        "dreamed":    len(dreamed),
        "avg_pot":    avg_pot,
        "max_pot":    max_pot,
        "high_pot":   high_pot,
        "mid_pot":    mid_pot,
        "low_pot":    low_pot,
        "spark_sess": spark_sess_vals,
        "spark_keys": spark_sess,
    }


# ── UI Components ─────────────────────────────────────────────────────────────

def panel_health_score(trends):
    """Composite health score 0-100 with color-coded verdict."""
    score = trends.get("health_score", 0)
    if score >= 80:
        color = "green"
        verdict = "HEALTHY"
    elif score >= 60:
        color = "yellow"
        verdict = "FAIR"
    elif score >= 40:
        color = "red"
        verdict = "DEGRADED"
    else:
        color = "red bold"
        verdict = "CRITICAL"

    # Compose score bar
    bar_width = 20
    filled = round(score / 100 * bar_width)
    bar = f"[{color}]{'█' * filled}[/][dim]{'░' * (bar_width - filled)}[/]"

    lines = [
        f"[bold {color}]{score}[/bold {color}]  {verdict}",
        bar,
        "",
        f"[dim]Success:   {trends.get('success_rate',0):.0f}%[/dim]",
        f"[dim]Kill rate: {trends.get('kill_rate',0):.0f}%[/dim]",
        f"[dim]Scored:    {trends.get('scoring_pct',0):.0f}% sessions[/dim]",
        f"[dim]Queue:     {trends.get('queue_size',0):,} pending[/dim]",
    ]
    return Panel("\n".join(lines), title="[bold]System Health Score[/bold]",
                 border_style=color, padding=(1, 2), width=30)


def panel_throughput(trends, sess_stats):
    """Week-over-week throughput comparison."""
    this_w = trends.get("this_week_c", 0)
    last_w = trends.get("last_week_c", 0)
    delta = trends.get("throughput_delta", 0)

    if delta > 0:
        d_color = "green"
        d_arrow = "↑"
    elif delta < 0:
        d_color = "red"
        d_arrow = "↓"
    else:
        d_color = "dim"
        d_arrow = "→"

    lines = [
        f"[bold cyan]{this_w}[/bold cyan] this week",
        f"[dim]{last_w} last week[/dim]",
        f"[{d_color}]{d_arrow} {abs(delta):+d} wk/wk[/{d_color}]",
        "",
        f"[dim]Today done: {trends.get('completed_1d', 0)}[/dim]",
        f"[dim]Queued: {trends.get('queue_size', 0):,}[/dim]",
        f"[dim]Avg queue wait: {trends.get('avg_queue_age_h', 0):.0f}h[/dim]",
    ]
    return Panel("\n".join(lines), title="[bold]Throughput (14d)[/bold]",
                 border_style="cyan", padding=(1, 2), width=30)


def panel_health_summary(trends):
    """Top-level health summary: total, success rate, avg conf, queue size."""
    sc    = trends["status_counts"]
    total = trends["total"]

    # Success rate from all known outcomes
    success_all = sc.get("success", 0) + sc.get("completed", 0)
    done_all    = success_all + sc.get("failed", 0) + sc.get("crashed", 0) + sc.get("stale", 0)
    success_rate = (success_all / done_all * 100) if done_all else 0

    # Completion rate vs total (how many reached a terminal state)
    terminal = success_all + sc.get("failed", 0) + sc.get("crashed", 0) + sc.get("stale", 0)
    completion_rate = (terminal / total * 100) if total else 0

    # Wallclock kill rate (system health indicator)
    killed = trends["killed_wallclock"]
    kill_rate = (killed / total * 100) if total else 0

    panels = [
        Panel(f"[bold cyan]{total}[/bold cyan]\n[dim]Total Dreams[/dim]", border_style="white"),
        Panel(f"[bold green]{success_all}[/bold green]\n[dim]Successful[/dim]", border_style="green"),
        Panel(f"[bold red]{sc.get('failed',0)}[/bold red]\n[dim]Failed[/dim]", border_style="red"),
        Panel(f"[bold red]{sc.get('crashed',0)}[/bold red]\n[dim]Crashed[/dim]", border_style="red"),
        Panel(f"[bold yellow]{sc.get('stale',0)}[/bold yellow]\n[dim]Stale[/dim]", border_style="yellow"),
        Panel(f"[bold blue]{trends['running']}[/bold blue]\n[dim]Running[/dim]", border_style="blue"),
        Panel(f"[bold magenta]{trends['queue_size']:,}[/bold magenta]\n[dim]Queued[/dim]", border_style="magenta"),
        Panel(f"[bold]{success_rate:.0f}%[/bold]\n[dim]Success Rate[/dim]", border_style="cyan"),
        Panel(f"[bold]{completion_rate:.0f}%[/bold]\n[dim]Terminal Rate[/dim]", border_style="cyan"),
        Panel(f"[bold]{trends['avg_conf_7d']:.2f}[/bold]\n[dim]Avg Conf 7d[/dim]", border_style="green"),
        Panel(f"[bold yellow]{killed}[/bold yellow]\n[dim]Wallclock Kills[/dim]", border_style="yellow"),
    ]
    return Columns(panels, equal=True, expand=True)


def panel_trend_sparklines(trends, sess_stats):
    """Sparklines for completions and sessions."""
    sv = trends["spark_vals"]
    ssv = sess_stats["spark_sess"]

    # Build mini sparkline with labels
    def spark_str(vals, keys, color, total_label):
        if not vals:
            return "[dim]no data[/dim]"
        total_c = sum(vals)
        # Simple ASCII histogram: scale vals to 0-9, use / \ | for bars
        max_v = max(vals) or 1
        bars = ""
        for v in vals:
            height = round((v / max_v) * 4)  # 0-4 bars
            bars += chr(0x2581 + height) if height > 0 else "·"
        return f"[{color}]{bars}[/{color}]  [dim]{keys[0]}–{keys[-1]}: {total_c} {total_label}[/dim]"

    completion_spark = spark_str(sv, trends["spark_keys"], "green", "completions")
    session_spark    = spark_str(ssv, sess_stats["spark_keys"], "magenta", "sessions")

    completion_panel = Panel(
        f"[bold green]↗ {sum(sv)}[/bold green] completions 7d\n{completion_spark}",
        title="Completions / day", border_style="green", padding=(1, 2)
    )
    session_panel = Panel(
        f"[bold magenta]↗ {sum(ssv)}[/bold magenta] sessions 7d\n{session_spark}",
        title="Sessions / day", border_style="magenta", padding=(1, 2)
    )
    return Columns([completion_panel, session_panel], equal=True, expand=True)


def panel_queue_health(trends):
    """Queue depth, age, backlog alerts."""
    qs    = trends["queue_size"]
    avg_h = trends["avg_queue_age_h"]
    max_h = trends["max_queue_age_h"]
    zombies = trends["zombies"]

    # Health verdict
    if qs > 2000:
        q_color = "red"
        q_verdict = f"[red]⚠ SEVERE BACKLOG — {qs:,} queued[/red]"
    elif qs > 500:
        q_color = "yellow"
        q_verdict = f"[yellow]⚠ Moderate backlog — {qs:,} queued[/yellow]"
    else:
        q_color = "green"
        q_verdict = f"[green]✓ Queue healthy — {qs:,} queued[/green]"

    if avg_h > 48:
        age_color = "red"
        age_verdict = f"[red]⚠ Avg wait {avg_h:.0f}h — severely stale[/red]"
    elif avg_h > 12:
        age_color = "yellow"
        age_verdict = f"[yellow]⚠ Avg wait {avg_h:.0f}h — aging[/yellow]"
    else:
        age_color = "green"
        age_verdict = f"[green]✓ Avg wait {avg_h:.1f}h — healthy[/green]"

    zombie_info = ""
    if zombies:
        z_ids = ", ".join(z["dream_id"] for z in zombies[:3])
        zombie_info = f"\n[red]⚠ {len(zombies)} zombie(s) running >2h: {z_ids}[/red]"

    kill_rt = trends.get("kill_rate", 0)
    if kill_rt > 30:
        kill_info = f"[red]⚠ Kill rate {kill_rt:.0f}% — ↑ wallclock timeout[red]"
    elif kill_rt > 15:
        kill_info = f"[yellow]⚠ Kill rate {kill_rt:.0f}%[yellow]"
    else:
        kill_info = f"[dim]Kill rate: {kill_rt:.0f}%[/dim]"

    lines = [
        q_verdict,
        age_verdict,
        f"[dim]Max wait: {max_h:.1f}h[/dim]",
        kill_info,
        f"[dim]Today: {trends['completed_1d']} done, {trends['completed_7d']} this week[/dim]",
        zombie_info,
    ]

    return Panel(
        "\n".join(l for l in lines if l),
        title="[bold]Queue Health[/bold]",
        border_style=q_color,
        padding=(1, 2),
        width=50,
    )


def panel_session_health(sess_stats):
    """Session scoring rate and potential distribution."""
    total    = sess_stats["total"]
    scored   = sess_stats["scored"]
    unscored = sess_stats["unscored"]
    dreamed  = sess_stats["dreamed"]

    # Scoring coverage
    if total > 0:
        score_pct = scored / total * 100
        dream_pct = dreamed / total * 100 if dreamed else 0
    else:
        score_pct = dream_pct = 0

    if unscored > 200:
        sc_color = "red"
        sc_verdict = f"[red]⚠ {unscored} unscored — scheduler blind to these[/red]"
    elif unscored > 50:
        sc_color = "yellow"
        sc_verdict = f"[yellow]⚠ {unscored} unscored sessions[/yellow]"
    else:
        sc_color = "green"
        sc_verdict = f"[green]✓ {scored}/{total} sessions scored[/green]"

    # Potential distribution mini bars
    hp = sess_stats["high_pot"]
    mp = sess_stats["mid_pot"]
    lp = sess_stats["low_pot"]

    def mini_bar(count, color, label):
        if count == 0:
            return f"[dim]{label}: 0[/dim]"
        return f"[{color}]{'█' * min(count, 20)}[/{color}] [dim]{label}: {count}[/dim]"

    lines = [
        sc_verdict,
        f"[dim]Scored: {scored}/{total} ({score_pct:.0f}%)[/dim]",
        f"[dim]Dreamed: {dreamed}/{total} ({dream_pct:.0f}%)[/dim]",
        "",
        "[dim]Potential distribution (scored):[/dim]",
        mini_bar(hp, "green", "0.8+"),
        mini_bar(mp, "cyan",  "0.5-0.8"),
        mini_bar(lp, "red",   "<0.5"),
        "",
        f"[dim]Avg potential: {sess_stats['avg_pot']:.2f}  Max: {sess_stats['max_pot']:.2f}[/dim]",
    ]

    return Panel(
        "\n".join(lines),
        title="[bold]Session Index[/bold]",
        border_style=sc_color,
        padding=(1, 2),
        width=50,
    )


def panel_mcts_performance(dreams, queue=None):
    """MCTS quality breakdown — confidence tiers, insights yield.
    
    Uses queue DB 'grade' as confidence proxy when dreams list is empty.
    """
    completed_q = [q for q in (queue or []) if q.get("status") in ("completed", "done")]
    completed_d = [d for d in dreams if d["status"] == "completed"]

    # Prefer filesystem data, fall back to queue
    if completed_d and any(d.get("confidence") for d in completed_d):
        confs = [d["confidence"] for d in completed_d if d.get("confidence")]
        total_ins = sum(d["insights_count"] for d in completed_d)
        total_fail = sum(d["failures_count"] for d in completed_d)
        n = len(completed_d)
        data_source = "dreams"
    elif completed_q:
        # Use queue 'grade' as a confidence proxy (0-1 scale)
        grades = [q["grade"] for q in completed_q if q.get("grade") is not None]
        confs = grades if grades else []
        total_ins = 0
        total_fail = sum(1 for q in completed_q if q.get("status") in ("failed", "failed_crash"))
        n = len(completed_q)
        data_source = "queue"
    else:
        return Panel("[dim]No completed dreams to analyze[/dim]",
                     title="[bold]MCTS Performance[/bold]", border_style="white", padding=(1, 2))

    avg_c = sum(confs) / len(confs) if confs else 0

    tier_high   = sum(1 for c in confs if c >= 0.75)
    tier_mid    = sum(1 for c in confs if 0.5 <= c < 0.75)
    tier_low    = sum(1 for c in confs if 0 < c < 0.5)
    tier_zero   = sum(1 for c in confs if c == 0)
    total_c     = len(confs)

    def tier_bar(count, color, label):
        if count == 0:
            return f"  [dim]{label}: 0[/dim]"
        pct = count / total_c * 100 if total_c else 0
        bars = "█" * max(1, round(pct / 5))
        return f"  [{color}]{bars}[/{color}] [dim]{label}: {count} ({pct:.0f}%)[/dim]"

    ins_per = total_ins / n if n else 0
    source_label = "[dim]Source: queue DB (grade)[/dim]" if data_source == "queue" else f"[dim]Source: {len(completed_d)} dreams[/dim]"

    lines = [
        f"[dim]Avg confidence: [bold]{avg_c:.2f}[/bold] (n={total_c})[/dim]",
        source_label,
        f"[dim]Insights extracted: {total_ins} ({ins_per:.1f}/dream)[/dim]" if total_ins else "",
        f"[dim]Failures: {total_fail}[/dim]",
        "",
        "[dim]Confidence tier breakdown:[/dim]",
        tier_bar(tier_high, "green", "0.75+"),
        tier_bar(tier_mid,  "cyan",  "0.50-0.74"),
        tier_bar(tier_low,  "yellow","0.01-0.49"),
        tier_bar(tier_zero, "red",   "0.00"),
    ]

    return Panel(
        "\n".join(l for l in lines if l),
        title="[bold]MCTS Performance[/bold]",
        border_style="cyan",
        padding=(1, 2),
        width=56,
    )


def panel_actionable_alerts(trends, sess_stats):
    """Red/yellow alerts that need action."""
    alerts = []

    qs    = trends["queue_size"]
    zombies = trends["zombies"]

    if qs > 2000:
        alerts.append(f"[red]QUEUE BACKLOG: {qs:,} dreams queued — system flooding[/red]")
    if zombies:
        z_ids = ", ".join(z["dream_id"] for z in zombies)
        alerts.append(f"[red]ZOMBIE DREAMS: {len(zombies)} running >2h — {z_ids}[/red]")
    if trends["max_queue_age_h"] > 72:
        alerts.append(f"[yellow]OLDEST QUEUED: {trends['max_queue_age_h']:.0f}h wait — check scheduler[/yellow]")
    if sess_stats["unscored"] > 200:
        alerts.append(f"[yellow]UNSCORED: {sess_stats['unscored']} sessions unscored — scheduler blind[/yellow]")
    if trends["killed_wallclock"] > 50 or trends.get("kill_rate", 0) > 30:
        alerts.append(f"[red]KILL RATE: {trends['kill_rate']:.0f}% ({trends['killed_wallclock']} killed) — ↑ wallclock timeout[/red]")
    if trends.get("success_rate", 0) < 50 and trends.get("total_completed", 0) > 10:
        alerts.append(f"[yellow]LOW SUCCESS: {trends['success_rate']:.0f}% terminal success rate[/yellow]")
    if trends["avg_conf_7d"] < 0.5 and trends["completed_7d"] > 5:
        alerts.append(f"[yellow]LOW CONFIDENCE: avg {trends['avg_conf_7d']:.2f} — MCTS struggling[/yellow]")

    if not alerts:
        alerts = ["[green]✓ No critical alerts — system healthy[/green]"]

    return Panel(
        "\n".join(alerts),
        title="[bold red]⚠ Actionable Alerts[/bold red]",
        border_style="red",
        padding=(1, 2),
    )


# ── Tables ────────────────────────────────────────────────────────────────────

def make_dream_runs_table(dreams):
    table = Table(title="Dream Runs (latest 30)", box=box.SIMPLE_HEAD, show_lines=False, row_styles=["", "dim"])
    table.add_column("Dream ID",  style="cyan", no_wrap=True)
    table.add_column("Ver",      style="dim",  width=4)
    table.add_column("Status",   style="bold")
    table.add_column("Conf",     justify="right")
    table.add_column("Iter",     justify="right", style="dim")
    table.add_column("Insights",  justify="right")
    table.add_column("Failures", justify="right")
    table.add_column("Age",      style="dim",  no_wrap=True)
    table.add_column("Error",    style="red",  max_width=35)

    now = datetime.now(timezone(timedelta(hours=7)))
    sorted_dreams = sorted(
        dreams,
        key=lambda x: x.get("completed_at") or datetime.min.replace(tzinfo=timezone(timedelta(hours=7))),
        reverse=True
    )

    for d in sorted_dreams[:30]:
        color = status_style(d["status"])
        age   = fmt_age(d.get("completed_at"))
        conf  = f"{d['confidence']:.2f}" if d["confidence"] else "—"
        table.add_row(
            d["dream_id"],
            d["version"],
            f"[{color}]{d['status']}[/{color}]",
            conf,
            str(d.get("iterations", "—")),
            str(d["insights_count"]),
            str(d["failures_count"]),
            age,
            d.get("error") or "—",
        )
    return table


def make_queue_table(queue):
    table = Table(title="Dream Queue (latest 30)", box=box.SIMPLE_HEAD, show_lines=False, row_styles=["", "dim"])
    table.add_column("Dream ID",  style="cyan")
    table.add_column("Session ID", style="dim", max_width=24)
    table.add_column("Status",     style="bold")
    table.add_column("Grade",     justify="right")
    table.add_column("Priority",  justify="right")
    table.add_column("Age",       style="dim")
    table.add_column("Started",   style="dim")
    table.add_column("Completed", style="dim")

    now = datetime.now(timezone(timedelta(hours=7)))
    for q in queue[:30]:
        color = status_style(q.get("status", "unknown"))
        age   = fmt_age(q.get("created_at"))
        grade = f"{q['grade']:.2f}" if q.get("grade") else "—"
        prio  = f"{q['priority']:.2f}" if q.get("priority") else "—"
        table.add_row(
            q["dream_id"],
            q["session_id"][:22] + ".." if len(q["session_id"]) > 24 else q["session_id"],
            f"[{color}]{q['status']}[/{color}]",
            grade,
            prio,
            age,
            fmt_dt(q.get("started_at")),
            fmt_dt(q.get("completed_at")),
        )
    return table


def make_sessions_table(sessions):
    table = Table(title="Session Index (latest 30)", box=box.SIMPLE_HEAD, show_lines=False, row_styles=["", "dim"])
    table.add_column("Session ID",  style="cyan", max_width=28)
    table.add_column("Created",     style="dim",  no_wrap=True, width=20)
    table.add_column("Msgs",       justify="right")
    table.add_column("Potential",  justify="right")
    table.add_column("Errors",      justify="right", style="dim")
    table.add_column("Last Dream", style="dim")

    for s in sessions[:30]:
        last_dreamed = fmt_age(s.get("last_dreamed_at")) if s.get("last_dreamed_at") else "—"
        table.add_row(
            s["session_id"],
            fmt_dt(s.get("created_at")),
            str(s.get("message_count", 0)),
            f"{s['dream_potential']:.2f}" if s.get("dream_potential") else "—",
            str(s.get("error_count", 0)),
            last_dreamed,
        )
    return table


def make_insights_table(insights):
    table = Table(title="Recent Dream Insights (last 7 days)", box=box.SIMPLE_HEAD, show_lines=False, row_styles=["", "dim"])
    table.add_column("When",    style="dim",   no_wrap=True, width=16)
    table.add_column("Ver",     style="dim",   width=4)
    table.add_column("Insight", style="green")

    for r in insights:
        table.add_row(fmt_dt(r["completed_at"]), r["version"], truncate(r["text"], 72))
    return table


def make_errors_table(dreams):
    errors = [d["error"] for d in dreams if d.get("error")]
    if not errors:
        return None
    table = Table(title="Error Breakdown", box=box.SIMPLE_HEAD, show_lines=False)
    table.add_column("Error",    style="red")
    table.add_column("Count",    justify="right")
    for err, count in Counter(errors).most_common(15):
        table.add_row(err, str(count))
    return table


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AutoDream Insights Dashboard v2")
    parser.add_argument("--errors",   action="store_true", help="Show only error breakdown")
    parser.add_argument("--queue",    action="store_true", help="Show only dream queue")
    parser.add_argument("--sessions", action="store_true", help="Show only session index")
    parser.add_argument("--runs",     action="store_true", help="Show only dream runs")
    parser.add_argument("--insights", action="store_true", help="Show only recent insights")
    parser.add_argument("--all",      action="store_true", help="Show full dashboard (default)")
    args = parser.parse_args()

    show_all = not any([args.errors, args.queue, args.sessions, args.runs, args.insights])

    # ── Collect data ──────────────────────────────────────────────────────────
    dreams    = collect_dreams()
    queue     = collect_queue()
    sessions  = collect_sessions()

    # Compute stats
    trends     = compute_trends(dreams, queue)
    sess_stats = compute_session_stats(sessions)

    # Inject session scoring coverage into trends for health score
    trends["scoring_pct"] = (
        round(sess_stats["scored"] / sess_stats["total"] * 100)
        if sess_stats["total"] > 0 else 0
    )

    if show_all:
        console.print("[bold cyan]AutoDream Insights Dashboard v2[/bold cyan]", justify="center")
        console.print(f"[dim]Generated: {datetime.now(timezone(timedelta(hours=7))).strftime('%Y-%m-%d %H:%M:%S')} GMT+7[/dim]\n", justify="center")

        # Top row: Health Score + Throughput (side by side)
        console.print(Columns([
            panel_health_score(trends),
            panel_throughput(trends, sess_stats),
        ], equal=True, expand=True))
        console.print()

        console.print(panel_health_summary(trends))
        console.print()
        console.print(panel_trend_sparklines(trends, sess_stats))
        console.print()

        # Side-by-side: Queue Health | Session Health
        console.print(Columns([
            panel_queue_health(trends),
            panel_session_health(sess_stats),
        ], equal=True, expand=True))
        console.print()

        # Side-by-side: MCTS Performance | Actionable Alerts
        console.print(Columns([
            panel_mcts_performance(dreams, queue),
            panel_actionable_alerts(trends, sess_stats),
        ], equal=True, expand=True))
        console.print()

    # ── Insights ───────────────────────────────────────────────────────────────
    if show_all or args.insights:
        recent = collect_recent_insights(limit=20, days_back=7)

        # Categorization
        debug_kw  = ["error", "traceback", "crash", "bug", "fix", "debug", "timeout", "kill", "pipe", "buffer", "deadlock", "zombie", "orphan"]
        arch_kw   = ["architecture", "schema", "plugin", "hook", "memory", "system", "engine", "mcts", "design", "thread", "queue", "scheduler"]
        data_kw   = ["database", "db", "table", "column", "query", "sql", "json", "path", "org2", "session", "index", "sqlite"]

        debug_ct  = sum(1 for r in recent if any(k in r["text"].lower() for k in debug_kw))
        arch_ct   = sum(1 for r in recent if any(k in r["text"].lower() for k in arch_kw))
        data_ct   = sum(1 for r in recent if any(k in r["text"].lower() for k in data_kw))

        console.print(Columns([
            Panel(f"[bold green]{len(recent)}[/bold green]\n[dim]Recent Insights[/dim]", border_style="green"),
            Panel(f"[bold yellow]{debug_ct}[/bold yellow]\n[dim]Debug / Ops Tips[/dim]", border_style="yellow"),
            Panel(f"[bold cyan]{arch_ct}[/bold cyan]\n[dim]Architecture Notes[/dim]", border_style="cyan"),
            Panel(f"[bold magenta]{data_ct}[/bold magenta]\n[dim]Data / DB Clues[/dim]", border_style="magenta"),
        ], equal=True, expand=True))
        console.print()
        console.print(make_insights_table(recent))
        console.print()

    # ── Dream runs ────────────────────────────────────────────────────────────
    if show_all or args.runs:
        console.print(make_dream_runs_table(dreams))
        console.print()

    # ── Queue ─────────────────────────────────────────────────────────────────
    if show_all or args.queue:
        console.print(make_queue_table(queue))
        console.print()

    # ── Sessions ──────────────────────────────────────────────────────────────
    if show_all or args.sessions:
        console.print(make_sessions_table(sessions))
        console.print()

    # ── Errors ────────────────────────────────────────────────────────────────
    if show_all or args.errors:
        etable = make_errors_table(dreams)
        if etable:
            console.print(etable)
        else:
            console.print("[dim]No errors recorded.[/dim]")


if __name__ == "__main__":
    main()
