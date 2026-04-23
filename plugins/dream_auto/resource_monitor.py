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
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

DREAM_DIR = Path.home() / ".hermes" / "state" / "dream"
DB_PATH = Path.home() / ".hermes" / "state" / "dream" / "session_index.db"
HERMES_BIN = shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")

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
                ["hermes", "sessions", "list", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                return len(data.get("sessions", []))
        except Exception:
            pass
        return 0

    def _count_active_crons(self) -> int:
        """Count active cron jobs."""
        try:
            result = subprocess.run(
                ["hermes", "cron", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                jobs = data.get("jobs", [])
                return sum(1 for j in jobs if j.get("status") == "running")
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

        Decision tree:
          - CPU >= 80% OR RAM >= 90%  → definitely NO
          - CPU <= 30%                → definitely YES (RAM doesn't matter much)
          - RAM in 50-90%, CPU 30-80% → ambiguous → LLM decides
          - Otherwise                  → YES
        """
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
