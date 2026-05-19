"""
Resource Monitor — Phase 2 of Dream System v3

Checks CPU, RAM, active sessions, cron jobs, and dream count.
Uses LLM only when resources are ambiguous (CPU 30-70% or RAM 50-80%).

Usage:
    from resource_monitor import ResourceMonitor
    rm = ResourceMonitor()
    available, reason = rm.can_start_dream()
    details = rm.get_state()
"""

import json
import os
import psutil
import pytz
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

DREAM_DIR = Path.home() / ".hermes" / "state" / "dream"
DB_PATH = Path.home() / ".hermes" / "state" / "dream" / "session_index.db"
HERMES_BIN = Path.home() / ".local" / "bin" / "hermes"

GMT7 = timezone(timedelta(hours=7))

# Clear thresholds — use LLM for ambiguous, auto for clear
CPU_CLEAR_HIGH = 80.0      # definitely too busy
CPU_CLEAR_LOW = 30.0        # definitely free
RAM_CLEAR_HIGH = 90.0       # definitely too busy
RAM_CLEAR_LOW = 50.0        # definitely free


class ResourceMonitor:
    def __init__(self):
        self.cpu_clear_high = CPU_CLEAR_HIGH
        self.cpu_clear_low = CPU_CLEAR_LOW
        self.ram_clear_high = RAM_CLEAR_HIGH
        self.ram_clear_low = RAM_CLEAR_LOW

        # Time window constraints (env vars)
        self.allow_hours = self._parse_time_window(
            os.getenv("DREAM_AUTO_ALLOW_HOURS")
        )
        self.deny_hours = self._parse_time_window(
            os.getenv("DREAM_AUTO_DENY_HOURS")
        )
        self.tz_name = os.getenv("DREAM_AUTO_TIMEZONE", "UTC")
        try:
            self.tz = pytz.timezone(self.tz_name)
        except pytz.exceptions.UnknownTimeZoneError:
            self.tz = pytz.UTC
            self.tz_name = "UTC"
        self.max_daily_dreams = int(os.getenv("DREAM_AUTO_MAX_DAILY_DREAMS", "0"))


    def get_state(self) -> dict:
        """Get full resource state."""
        cpu = psutil.cpu_percent(interval=1.0)
        ram = psutil.virtual_memory()
        ram_pct = ram.percent

        n_sessions = self._count_active_sessions()
        n_crons = self._count_active_crons()
        n_dreams = self._count_active_dreams()

        return {
            "cpu_percent": cpu,
            "ram_percent": ram_pct,
            "active_sessions": n_sessions,
            "active_crons": n_crons,
            "active_dreams": n_dreams,
            "timestamp": datetime.now(GMT7).isoformat(),
        }

    def _count_active_sessions(self) -> int:
        """Count active hermes sessions (TTY users)."""
        try:
            result = subprocess.run(
                ["hermes", "sessions", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                # Output format: "Preview ... Last Active ... Src ... ID" with separator line
                # Count non-empty, non-header, non-separator lines
                lines = [l for l in result.stdout.strip().split("\n")
                         if l.strip() and not l.strip().startswith("─") and not l.strip().startswith("Preview")]
                return len(lines)
        except Exception:
            pass
        return 0

    def _count_active_crons(self) -> int:
        """Count active cron jobs."""
        try:
            result = subprocess.run(
                ["hermes", "cron", "status"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                # Parse "N active job(s)" from output
                match = re.search(r"(\d+)\s+active\s+job", result.stdout)
                if match:
                    return int(match.group(1))
        except Exception:
            pass
        return 0

    def _count_active_dreams(self) -> int:
        """Count currently running dreams."""
        if not DREAM_DIR.exists():
            return 0
        count = 0
        for d in DREAM_DIR.iterdir():
            if not d.is_dir():
                continue
            status_file = d / "status.txt"
            meta_file = d / "meta.json"
            if status_file.exists() and status_file.read_text().strip() == "running":
                count += 1
                continue
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text())
                    if meta.get("status") == "running":
                        count += 1
                except Exception:
                    pass
        return count

    def _parse_time_window(self, window_str: Optional[str]) -> Optional[tuple[int, int]]:
        """
        Parse 'HH:MM-HH:MM' into (start_mins, end_mins).
        Handles midnight wrap: '22:00-06:00' → (1320, 360) where 1320 > 360.
        """
        if not window_str:
            return None
        try:
            match = re.match(r'^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$', window_str.strip())
            if not match:
                raise ValueError(f"Invalid time window format: {window_str}")
            start_h, start_m, end_h, end_m = map(int, match.groups())
            start_mins = start_h * 60 + start_m
            end_mins = end_h * 60 + end_m
            if not (0 <= start_h < 24 and 0 <= start_m < 60 and
                    0 <= end_h < 24 and 0 <= end_m < 60):
                raise ValueError(f"Time values out of range: {window_str}")
            return (start_mins, end_mins)
        except Exception as e:
            print(f"WARNING: Failed to parse time window '{window_str}': {e}")
            return None

    def _is_in_time_window(self, window: tuple[int, int], now: datetime) -> bool:
        """Check if now falls inside a time window (handles midnight wrap)."""
        start_mins, end_mins = window
        now_mins = now.hour * 60 + now.minute
        if start_mins <= end_mins:
            return start_mins <= now_mins < end_mins
        else:
            return now_mins >= start_mins or now_mins < end_mins

    def _get_next_eligible_time(self, window: tuple[int, int], now: datetime) -> str:
        """Calculate when the next eligible window opens."""
        start_mins, end_mins = window
        now_mins = now.hour * 60 + now.minute
        if start_mins <= end_mins:
            if now_mins < start_mins:
                delta = start_mins - now_mins
            else:
                delta = (24 * 60) - now_mins + start_mins
        else:
            if now_mins >= start_mins:
                delta = (24 * 60) - now_mins
            elif now_mins < end_mins:
                return "now (in allowed window)"
            else:
                delta = start_mins - now_mins
        hours, mins = divmod(delta, 60)
        next_time_mins = (now_mins + delta) % (24 * 60)
        next_h, next_m = divmod(int(next_time_mins), 60)
        return f"{next_h:02d}:{next_m:02d} (in {hours}h {mins}m)"

    def _check_time_constraints(self) -> tuple[bool, str]:
        """Check if current time is allowed for dream execution."""
        force_allow_marker = DREAM_DIR / ".force_allow_next_run"
        if force_allow_marker.exists():
            try:
                force_allow_marker.unlink()
                return True, "Force-allow override (one-time, cleared)"
            except Exception:
                pass

        try:
            now = datetime.now(self.tz)
        except Exception as e:
            return False, f"Timezone error: {e}"

        if self.deny_hours:
            if self._is_in_time_window(self.deny_hours, now):
                next_time = self._get_next_eligible_time(self.deny_hours, now)
                start_h, start_m = divmod(self.deny_hours[0], 60)
                end_h, end_m = divmod(self.deny_hours[1], 60)
                return False, f"In deny window {start_h:02d}:{start_m:02d}–{end_h:02d}:{end_m:02d}. Next: {next_time}"

        if self.allow_hours:
            if not self._is_in_time_window(self.allow_hours, now):
                next_time = self._get_next_eligible_time(self.allow_hours, now)
                start_h, start_m = divmod(self.allow_hours[0], 60)
                end_h, end_m = divmod(self.allow_hours[1], 60)
                return False, f"Outside allow window {start_h:02d}:{start_m:02d}–{end_h:02d}:{end_m:02d}. Next: {next_time}"

        return True, "Time constraints OK"

    def _check_daily_dream_count(self) -> tuple[bool, str]:
        """Check if we've hit the daily dream limit."""
        if self.max_daily_dreams <= 0:
            return True, "No daily cap set"

        try:
            if not DB_PATH.exists():
                return True, "Dream queue DB not found (first run?)"

            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()

            now = datetime.now(self.tz)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

            cursor.execute("""
                SELECT COUNT(*) FROM dream_queue
                WHERE status IN ('completed_success', 'completed_partial', 'completed_error', 'done')
                AND completed_at > ?
            """, (today_start,))

            count = cursor.fetchone()[0]
            conn.close()

            if count >= self.max_daily_dreams:
                return False, f"Daily cap reached: {count}/{self.max_daily_dreams} dreams completed today"

            remaining = self.max_daily_dreams - count
            return True, f"Daily cap: {count}/{self.max_daily_dreams} ({remaining} remaining)"

        except Exception as e:
            return True, f"Could not check daily cap: {e}"

    def _llm_availability_decision(self, state: dict) -> tuple[bool, str]:
        """
        Use LLM to decide if resources are available when ambiguous.
        Only called when CPU or RAM is in the gray zone.
        """
        prompt = (
            f"System check: CPU={state['cpu_percent']:.0f}% RAM={state['ram_percent']:.0f}%. "
            f"Active sessions={state['active_sessions']}, cron jobs={state['active_crons']}, "
            f"dreams running={state['active_dreams']}. "
            f"Should we start 1 background reasoning dream? Answer JSON only: "
            f"{{\"can_start\": true, \"reason\": \"brief explanation\"}}"
        )
        try:
            result = subprocess.run(
                [str(HERMES_BIN), "chat", "-q", prompt],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(Path.home()),
            )
            output = result.stdout
            # Find last JSON object with can_start
            for match in re.finditer(r'\{"can_start":\s*(true|false),\s*"reason":\s*"[^"]*"\}', output):
                try:
                    data = json.loads(match.group())
                    return data.get("can_start", False), data.get("reason", "LLM unclear")
                except Exception:
                    pass
        except subprocess.TimeoutExpired:
            # LLM timed out — defer
            return False, "LLM timed out — deferring"
        except Exception as e:
            return False, f"LLM check failed: {e}"
        return False, "LLM parse failed, defaulting to no"

    def can_start_dream(self) -> tuple[bool, str]:
        """
        Main entry point: should we start a new dream?
        Returns (available: bool, reason: str)

        Decision tree (in order):
          1. Check time constraints (allow/deny hours)
          2. Check daily dream limit
          3. Check resource thresholds (CPU/RAM)

        Returns first NO reason, or (True, OK) if all pass.
        """
        # Time constraints first (fail fast, avoids wasting resources)
        time_ok, time_reason = self._check_time_constraints()
        if not time_ok:
            return False, time_reason

        # Daily cap check
        cap_ok, cap_reason = self._check_daily_dream_count()
        if not cap_ok:
            return False, cap_reason

        # Resource checks (existing logic)
        state = self.get_state()
        cpu = state["cpu_percent"]
        ram = state["ram_percent"]

        # Hard stop: CPU or RAM clearly stressed
        if cpu >= self.cpu_clear_high or ram >= self.ram_clear_high:
            return False, f"CPU={cpu:.0f}% or RAM={ram:.0f}% too high — deferring"

        # Clear free: CPU is low, RAM is moderate → yes
        if cpu <= self.cpu_clear_low:
            return True, f"CPU={cpu:.0f}% is free"

        # Ambiguous: CPU in middle range and RAM also moderate → LLM
        ambiguous = (
            (self.cpu_clear_low < cpu < self.cpu_clear_high) and
            (self.ram_clear_low < ram < self.ram_clear_high)
        )
        if ambiguous:
            return self._llm_availability_decision(state)

        # Default: resources are OK
        return True, f"Resources OK (CPU={cpu:.0f}%, RAM={ram:.0f}%)"

    def get_queue_priority(self) -> str:
        """Get current resource health as a string."""
        state = self.get_state()
        cpu, ram = state["cpu_percent"], state["ram_percent"]
        if cpu < 20 and ram < 40:
            return "excellent"
        elif cpu < 40 and ram < 60:
            return "good"
        elif cpu < 60 and ram < 75:
            return "moderate"
        else:
            return "busy"


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import pprint
    rm = ResourceMonitor()
    state = rm.get_state()
    pprint.pprint(state)
    available, reason = rm.can_start_dream()
    print(f"\nCan start dream: {available}")
    print(f"Reason: {reason}")
