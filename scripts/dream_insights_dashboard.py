#!/usr/bin/env python3
"""
Dream Insights Dashboard — CLI/TUI version
Run: python3 ~/.hermes/scripts/dream_insights_dashboard.py
"""

import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich import box

import argparse

DREAM_STATE = Path.home() / ".hermes" / "state" / "dream"
SESSION_DB = DREAM_STATE / "session_index.db"
QUEUE_DB = DREAM_STATE / "dream_queue.db"
LOGS_DIR = DREAM_STATE / "logs"
console = Console()


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


def read_json_file(path):
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def dir_mtime(dream_dir: Path):
    try:
        mtimes = [f.stat().st_mtime for f in dream_dir.iterdir() if f.is_file()]
        return datetime.fromtimestamp(max(mtimes), tz=timezone(timedelta(hours=7))) if mtimes else None
    except Exception:
        return None


def parse_v2_dream(dream_dir: Path):
    meta = read_json_file(dream_dir / "meta.json") or {}
    status_txt = ""
    if (dream_dir / "status.txt").exists():
        status_txt = (dream_dir / "status.txt").read_text(encoding="utf-8").strip()
    insights = read_json_file(dream_dir / "insights.json") or []
    failures = read_json_file(dream_dir / "failures.json") or []

    raw_status = status_txt or meta.get("status", "")
    canonical = raw_status.lower().strip()
    if canonical in ("completed", "completed_success", "done"):
        display_status = "success"
    elif canonical in ("failed", "failed_crash", "failed_restart", "circuit_breaker",
                       "completed_killed", "health_check_failed"):
        display_status = "failed"
    elif canonical in ("completed_stale", "stale_completed", "completed_empty"):
        display_status = "stale"
    elif canonical == "running":
        display_status = "running"
    else:
        display_status = canonical or "unknown"

    return {
        "dream_id": dream_dir.name,
        "version": "v2",
        "status": display_status,
        "confidence": meta.get("confidence") or meta.get("best_confidence", 0),
        "insights_count": len(insights) if isinstance(insights, list) else 0,
        "failures_count": len(failures) if isinstance(failures, list) else 0,
        "completed_at": dir_mtime(dream_dir),
        "error": None,
    }


def parse_v3_dream(dream_dir: Path):
    log_path = dream_dir / "dream_output.log"
    if not log_path.exists():
        return None
    content = log_path.read_text(encoding="utf-8", errors="ignore")

    data = None
    for m in re.finditer(r'\{.*"dream_id".*?\}', content, re.DOTALL):
        try:
            candidate = json.loads(m.group())
            if "dream_id" in candidate:
                data = candidate
        except Exception:
            continue

    has_traceback = "Traceback" in content
    error = None
    status = "unknown"
    if has_traceback and not data:
        status = "crashed"
        lines = content.splitlines()
        for line in reversed(lines):
            if line.strip().startswith("File "):
                error = line.strip()
                break
        if not error:
            error = "Traceback occurred"
    elif data:
        status = "completed"
    else:
        status = "incomplete"

    mtime = datetime.fromtimestamp(log_path.stat().st_mtime, tz=timezone(timedelta(hours=7)))
    return {
        "dream_id": dream_dir.name,
        "version": "v3",
        "status": status,
        "confidence": data.get("confidence", 0) if data else 0,
        "insights_count": len(data.get("insights", [])) if data else 0,
        "failures_count": len(data.get("failures", [])) if data else 0,
        "completed_at": mtime,
        "error": error,
    }


def parse_log_file(log_path: Path):
    content = log_path.read_text(encoding="utf-8", errors="ignore")
    dream_id = log_path.stem
    status = "unknown"
    confidence = 0
    for line in content.splitlines():
        if "Completed after" in line:
            status = "completed"
        elif "Confidence threshold met" in line:
            m = re.search(r'(\d+)%', line)
            if m:
                confidence = int(m.group(1)) / 100.0
        elif "Traceback" in line:
            status = "crashed"
        elif line.startswith("[dream ") and "Starting" in line:
            status = "running"
        elif "Final distillation" in line:
            status = "completed"
    mtime = datetime.fromtimestamp(log_path.stat().st_mtime, tz=timezone(timedelta(hours=7)))
    return {
        "dream_id": dream_id,
        "version": "log",
        "status": status,
        "confidence": confidence,
        "insights_count": 0,
        "failures_count": 0,
        "completed_at": mtime,
        "error": None,
    }


def collect_dreams():
    dreams = []
    seen = set()
    for entry in os.listdir(DREAM_STATE):
        dream_dir = DREAM_STATE / entry
        if not dream_dir.is_dir() or not re.match(r'^[a-f0-9]{8}$', entry):
            continue
        seen.add(entry)
        v3 = parse_v3_dream(dream_dir)
        if v3:
            dreams.append(v3)
        else:
            dreams.append(parse_v2_dream(dream_dir))

    if LOGS_DIR.exists():
        for log_file in LOGS_DIR.iterdir():
            if not log_file.is_file() or not log_file.suffix == ".log":
                continue
            if log_file.stem not in seen:
                dreams.append(parse_log_file(log_file))
                seen.add(log_file.stem)
    return dreams


def collect_queue():
    if not QUEUE_DB.exists():
        return []
    conn = sqlite3.connect(str(QUEUE_DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM dream_queue ORDER BY created_at DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def collect_sessions():
    if not SESSION_DB.exists():
        return []
    conn = sqlite3.connect(str(SESSION_DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM sessions ORDER BY created_at DESC LIMIT 200")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def collect_recent_insights(limit=15, days_back=7):
    """Extract actual insight text from recent successful dreams."""
    insights = []
    cutoff = datetime.now(timezone(timedelta(hours=7))) - timedelta(days=days_back)

    for entry in os.listdir(DREAM_STATE):
        dream_dir = DREAM_STATE / entry
        if not dream_dir.is_dir() or not re.match(r'^[a-f0-9]{8}$', entry):
            continue

        # Determine completion time and status
        completed_at = None
        status = "unknown"
        dream_insights = []

        # Try v3 first
        log_path = dream_dir / "dream_output.log"
        if log_path.exists():
            mtime = datetime.fromtimestamp(log_path.stat().st_mtime, tz=timezone(timedelta(hours=7)))
            completed_at = mtime
            content = log_path.read_text(encoding="utf-8", errors="ignore")
            has_traceback = "Traceback" in content
            data = None
            for m in re.finditer(r'\{.*"dream_id".*?\}', content, re.DOTALL):
                try:
                    candidate = json.loads(m.group())
                    if "dream_id" in candidate:
                        data = candidate
                except Exception:
                    continue
            if data and not has_traceback:
                status = "success"
                dream_insights = data.get("insights", [])
            elif has_traceback:
                status = "crashed"
            else:
                status = "incomplete"

        # Fall back to v2
        if status == "unknown":
            meta = read_json_file(dream_dir / "meta.json") or {}
            status_txt = ""
            if (dream_dir / "status.txt").exists():
                status_txt = (dream_dir / "status.txt").read_text(encoding="utf-8").strip()
            raw_status = status_txt or meta.get("status", "")
            canonical = raw_status.lower().strip()
            if canonical in ("completed", "completed_success", "done"):
                status = "success"
            insights_file = dream_dir / "insights.json"
            if insights_file.exists():
                try:
                    v2_insights = json.loads(insights_file.read_text())
                    if isinstance(v2_insights, list):
                        dream_insights = v2_insights
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
                    "dream_id": entry,
                    "completed_at": completed_at,
                    "text": ins.strip(),
                    "version": "v3" if (dream_dir / "dream_output.log").exists() else "v2",
                })

    insights.sort(key=lambda x: x["completed_at"] or datetime.min.replace(tzinfo=timezone(timedelta(hours=7))), reverse=True)
    return insights[:limit]


def status_style(status):
    return {
        "success": "green",
        "completed": "green",
        "failed": "red",
        "crashed": "red",
        "stale": "yellow",
        "running": "blue",
        "queued": "yellow",
        "unknown": "dim",
        "incomplete": "dim",
    }.get(status, "white")


def main():
    parser = argparse.ArgumentParser(description="AutoDream Insights Dashboard")
    parser.add_argument("--errors", action="store_true", help="Show only error breakdown")
    parser.add_argument("--queue", action="store_true", help="Show only dream queue")
    parser.add_argument("--sessions", action="store_true", help="Show only session index")
    parser.add_argument("--runs", action="store_true", help="Show only dream runs")
    parser.add_argument("--insights", action="store_true", help="Show only recent insights")
    parser.add_argument("--all", action="store_true", help="Show full dashboard (default)")
    args = parser.parse_args()

    # If no filter flags, show everything
    show_all = not any([args.errors, args.queue, args.sessions, args.runs, args.insights])

    if show_all:
        console.print("[bold cyan]AutoDream Insights Dashboard[/bold cyan]", justify="center")
        console.print(f"[dim]Generated: {datetime.now(timezone(timedelta(hours=7))).strftime('%Y-%m-%d %H:%M:%S')} GMT+7[/dim]\n", justify="center")

    dreams = collect_dreams()
    queue = collect_queue()
    sessions = collect_sessions()

    total = len(dreams)
    success = sum(1 for d in dreams if d["status"] == "success")
    failed = sum(1 for d in dreams if d["status"] == "failed")
    crashed = sum(1 for d in dreams if d["status"] == "crashed")
    stale = sum(1 for d in dreams if d["status"] == "stale")
    running = sum(1 for d in dreams if d["status"] == "running")
    queued = sum(1 for d in dreams if d["status"] == "queued")
    avg_conf = sum(d["confidence"] for d in dreams) / total if total else 0
    total_insights = sum(d["insights_count"] for d in dreams)
    total_failures = sum(d["failures_count"] for d in dreams)

    if show_all:
        cards = [
            Panel(f"[bold]{total}[/bold]\n[dim]Total Dreams[/dim]", border_style="white"),
            Panel(f"[bold green]{success}[/bold green]\n[dim]Success[/dim]", border_style="green"),
            Panel(f"[bold red]{failed}[/bold red]\n[dim]Failed[/dim]", border_style="red"),
            Panel(f"[bold red]{crashed}[/bold red]\n[dim]Crashed[/dim]", border_style="red"),
            Panel(f"[bold yellow]{stale}[/bold yellow]\n[dim]Stale[/dim]", border_style="yellow"),
            Panel(f"[bold blue]{running}[/bold blue]\n[dim]Running[/dim]", border_style="blue"),
            Panel(f"[bold yellow]{queued}[/bold yellow]\n[dim]Queued[/dim]", border_style="yellow"),
            Panel(f"[bold]{avg_conf:.2f}[/bold]\n[dim]Avg Conf[/dim]", border_style="cyan"),
            Panel(f"[bold]{total_insights}[/bold]\n[dim]Insights[/dim]", border_style="green"),
            Panel(f"[bold]{total_failures}[/bold]\n[dim]Failures[/dim]", border_style="red"),
            Panel(f"[bold]{len(sessions)}[/bold]\n[dim]Sessions[/dim]", border_style="magenta"),
            Panel(f"[bold]{len(queue)}[/bold]\n[dim]Queue Size[/dim]", border_style="magenta"),
        ]
        console.print(Columns(cards, equal=True, expand=True))
        console.print()

    # RECENT INSIGHTS TABLE
    if show_all or args.insights:
        recent = collect_recent_insights(limit=15, days_back=7)
        if recent:
            # Impact mini-stats
            debug_keywords = ["error", "traceback", "crash", "bug", "fix", "debug", "timeout", "kill", "pipe", "buffer"]
            arch_keywords = ["architecture", "schema", "plugin", "hook", "memory", "system", "engine", "mcts", "design"]
            data_keywords = ["database", "db", "table", "column", "query", "sql", "json", "schema", "path", "org2"]

            debug_count = sum(1 for r in recent if any(k in r["text"].lower() for k in debug_keywords))
            arch_count = sum(1 for r in recent if any(k in r["text"].lower() for k in arch_keywords))
            data_count = sum(1 for r in recent if any(k in r["text"].lower() for k in data_keywords))

            impact_cards = [
                Panel(f"[bold green]{len(recent)}[/bold green]\n[dim]Recent Insights[/dim]", border_style="green"),
                Panel(f"[bold yellow]{debug_count}[/bold yellow]\n[dim]Debug / Ops Tips[/dim]", border_style="yellow"),
                Panel(f"[bold cyan]{arch_count}[/bold cyan]\n[dim]Architecture Notes[/dim]", border_style="cyan"),
                Panel(f"[bold magenta]{data_count}[/bold magenta]\n[dim]Data / DB Clues[/dim]", border_style="magenta"),
            ]
            console.print(Columns(impact_cards, equal=True, expand=True))
            console.print()

            itable = Table(title="Recent Dream Insights (last 7 days)" if not show_all else None, box=box.SIMPLE_HEAD, show_lines=False, row_styles=["", "dim"])
            itable.add_column("When", style="dim", no_wrap=True, width=16)
            itable.add_column("Ver", style="dim", width=4)
            itable.add_column("Insight", style="green", max_width=100)

            for r in recent:
                when = fmt_dt(r["completed_at"])
                # Truncate long insight text gracefully
                text = r["text"]
                itable.add_row(when, r["version"], text)
            console.print(itable)
            console.print()
        else:
            console.print("[dim]No insights from successful dreams in the last 7 days.[/dim]\n")

    # DREAM RUNS TABLE
    if show_all or args.runs:
        table = Table(title="Dream Runs" if not show_all else None, box=box.SIMPLE_HEAD, show_lines=False, row_styles=["", "dim"])
        table.add_column("Dream ID", style="cyan", no_wrap=True)
        table.add_column("Ver", style="dim")
        table.add_column("Status", style="bold")
        table.add_column("Conf")
        table.add_column("Insights", justify="right")
        table.add_column("Failures", justify="right")
        table.add_column("Completed", style="dim", no_wrap=True)
        table.add_column("Error", style="red", max_width=40)

        for d in sorted(dreams, key=lambda x: x.get("completed_at") or datetime.min.replace(tzinfo=timezone(timedelta(hours=7))), reverse=True)[:50]:
            color = status_style(d["status"])
            table.add_row(
                d["dream_id"],
                d["version"],
                f"[{color}]{d['status']}[/{color}]",
                f"{d['confidence']:.2f}" if d["confidence"] else "—",
                str(d["insights_count"]),
                str(d["failures_count"]),
                fmt_dt(d.get("completed_at")),
                d.get("error") or "—"
            )
        console.print(table)
        console.print()

    # QUEUE TABLE
    if show_all or args.queue:
        qtable = Table(title="Dream Queue (latest 30)" if not show_all else None, box=box.SIMPLE_HEAD, show_lines=False, row_styles=["", "dim"])
        qtable.add_column("Dream ID", style="cyan")
        qtable.add_column("Session ID", style="dim", max_width=24)
        qtable.add_column("Status", style="bold")
        qtable.add_column("Grade")
        qtable.add_column("Priority")
        qtable.add_column("Created", style="dim")
        qtable.add_column("Started", style="dim")
        qtable.add_column("Completed", style="dim")

        for q in queue[:30]:
            color = status_style(q.get("status", "unknown"))
            qtable.add_row(
                q["dream_id"],
                q["session_id"][:22] + ".." if len(q["session_id"]) > 24 else q["session_id"],
                f"[{color}]{q['status']}[/{color}]",
                str(q.get("grade", "—")),
                str(q.get("priority", "—")),
                fmt_dt(q.get("created_at")),
                fmt_dt(q.get("started_at")),
                fmt_dt(q.get("completed_at")),
            )
        console.print(qtable)
        console.print()

    # SESSIONS TABLE
    if show_all or args.sessions:
        stable = Table(title="Session Index (latest 30)" if not show_all else None, box=box.SIMPLE_HEAD, show_lines=False, row_styles=["", "dim"])
        stable.add_column("Session ID", style="cyan", max_width=28)
        stable.add_column("Created", style="dim")
        stable.add_column("Msgs", justify="right")
        stable.add_column("Potential")
        stable.add_column("Errors", justify="right")
        stable.add_column("Last Dreamed", style="dim")

        for s in sessions[:30]:
            stable.add_row(
                s["session_id"],
                fmt_dt(s.get("created_at")),
                str(s.get("message_count", 0)),
                f"{s['dream_potential']:.2f}" if s.get("dream_potential") else "—",
                str(s.get("error_count", 0)),
                fmt_dt(s.get("last_dreamed_at")),
            )
        console.print(stable)
        console.print()

    # ERROR BREAKDOWN
    if show_all or args.errors:
        errors = [d["error"] for d in dreams if d.get("error")]
        if errors:
            etable = Table(title="Error Breakdown" if not show_all else None, box=box.SIMPLE_HEAD, show_lines=False)
            etable.add_column("Error", style="red")
            etable.add_column("Count", justify="right")
            for err, count in Counter(errors).most_common(10):
                etable.add_row(err, str(count))
            console.print(etable)


if __name__ == "__main__":
    main()
