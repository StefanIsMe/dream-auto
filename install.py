#!/usr/bin/env python3
"""
Dream Auto Plugin — Cross-Platform Installer
=============================================
Installs the Dream System v3 plugin + skill into a Hermes Agent environment.

Supported platforms: Linux, macOS, Windows (WSL)
Prerequisite: Hermes Agent must already be installed (hermes CLI available).

Usage:
    python3 install.py              # interactive install
    python3 install.py --dry-run    # preview changes
    python3 install.py --force      # overwrite existing files
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
INSTALLER_VERSION = "1.0.0"
GMT7 = timezone(timedelta(hours=7))

# ── Paths ─────────────────────────────────────────────────────────────────────
HERMES_HOME = Path.home() / ".hermes"
PLUGINS_DIR = HERMES_HOME / "plugins"
SCRIPTS_DIR = HERMES_HOME / "scripts"
SKILLS_DIR = HERMES_HOME / "skills"
STATE_DIR = HERMES_HOME / "state" / "dream"
LOCAL_BIN = Path.home() / ".local" / "bin"

# Distribution paths (relative to this script)
DIST_ROOT = Path(__file__).parent.resolve()
DIST_PLUGIN = DIST_ROOT / "plugins" / "dream_auto"
DIST_SCRIPTS = DIST_ROOT / "scripts"
DIST_SKILL = DIST_ROOT / "skills" / "autonomous-ai-agents" / "hermes-dream-task"
DIST_SKILL_OPS = DIST_ROOT / "skills" / "ops" / "dream-system-v3"

# ── Colors ────────────────────────────────────────────────────────────────────
class C:
    OK = "\033[92m"
    WARN = "\033[93m"
    ERR = "\033[91m"
    INFO = "\033[94m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def _print(status: str, msg: str):
    color = {"OK": C.OK, "WARN": C.WARN, "ERR": C.ERR, "INFO": C.INFO}.get(status, "")
    print(f"{color}[{status}]{C.RESET} {msg}")


# ── Prerequisite checks ───────────────────────────────────────────────────────

def check_python_version() -> bool:
    v = sys.version_info
    if v.major < 3 or (v.major == 3 and v.minor < 10):
        _print("ERR", f"Python {v.major}.{v.minor} is too old. Need 3.10+.")
        return False
    _print("OK", f"Python {v.major}.{v.minor}.{v.micro}")
    return True


def check_hermes_cli() -> bool:
    hermes = shutil.which("hermes")
    if not hermes:
        _print("ERR", "hermes CLI not found in PATH. Install Hermes Agent first.")
        _print("INFO", "  https://hermes-agent.nousresearch.com/docs/install")
        return False
    try:
        result = subprocess.run([hermes, "--version"], capture_output=True, text=True, timeout=10)
        ver = result.stdout.strip() or result.stderr.strip() or "unknown"
        _print("OK", f"Hermes CLI found: {hermes} ({ver})")
        return True
    except Exception as e:
        _print("WARN", f"hermes CLI found but --version failed: {e}")
        return True  # still proceed


def check_pip() -> bool:
    pip = shutil.which("pip3") or shutil.which("pip")
    if not pip:
        _print("ERR", "pip not found. Install Python pip first.")
        return False
    _print("OK", f"pip found: {pip}")
    return True


def check_platform() -> str:
    plat = platform.system().lower()
    if plat == "linux":
        _print("OK", "Platform: Linux")
        return "linux"
    elif plat == "darwin":
        _print("OK", "Platform: macOS")
        return "macos"
    elif plat == "windows":
        _print("WARN", "Platform: Windows native detected. WSL is strongly recommended.")
        return "windows"
    else:
        _print("WARN", f"Platform: {plat} (untested)")
        return plat


# ── Dependency install ────────────────────────────────────────────────────────

def install_dependencies() -> bool:
    req_file = DIST_ROOT / "requirements.txt"
    if not req_file.exists():
        _print("WARN", "requirements.txt not found in distribution. Skipping pip install.")
        return True

    pip = shutil.which("pip3") or shutil.which("pip")
    try:
        result = subprocess.run(
            [pip, "install", "--user", "-r", str(req_file)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            _print("OK", "Python dependencies installed (psutil, rich)")
            return True
        else:
            _print("ERR", f"pip install failed:\n{result.stderr[:500]}")
            return False
    except Exception as e:
        _print("ERR", f"pip install error: {e}")
        return False


# ── File installation ─────────────────────────────────────────────────────────

def install_files(force: bool = False) -> bool:
    targets = []

    # Plugin
    plugin_dest = PLUGINS_DIR / "dream_auto"
    if plugin_dest.exists() and not force:
        _print("WARN", f"Plugin already exists at {plugin_dest}. Use --force to overwrite.")
    else:
        targets.append((DIST_PLUGIN, plugin_dest))

    # Scripts
    for src in DIST_SCRIPTS.iterdir():
        if src.is_file():
            dest = SCRIPTS_DIR / src.name
            if dest.exists() and not force:
                _print("WARN", f"Script exists: {dest.name} (skipped, use --force)")
                continue
            targets.append((src, dest))

    # Skill (autonomous-ai-agents)
    skill_dest = SKILLS_DIR / "autonomous-ai-agents" / "hermes-dream-task"
    if skill_dest.exists() and not force:
        _print("WARN", f"Skill already exists at {skill_dest}. Use --force to overwrite.")
    else:
        targets.append((DIST_SKILL, skill_dest))

    # Skill (ops)
    skill_ops_dest = SKILLS_DIR / "ops" / "dream-system-v3"
    if skill_ops_dest.exists() and not force:
        _print("WARN", f"Skill already exists at {skill_ops_dest}. Use --force to overwrite.")
    else:
        targets.append((DIST_SKILL_OPS, skill_ops_dest))

    # Copy
    for src, dest in targets:
        try:
            if dest.exists():
                shutil.rmtree(dest)
            if src.is_dir():
                shutil.copytree(src, dest)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
            _print("OK", f"Installed: {dest.relative_to(HERMES_HOME)}")
        except Exception as e:
            _print("ERR", f"Failed to install {src.name}: {e}")
            return False

    return True


# ── State / DB initialization ─────────────────────────────────────────────────

def init_state() -> bool:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "logs").mkdir(exist_ok=True)

        # session_index.db schema + indexes
        db_path = STATE_DIR / "session_index.db"
        conn = sqlite3.connect(str(db_path))
        if not db_path.exists():
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at TEXT,
                    last_message_at TEXT,
                    message_count INTEGER DEFAULT 0,
                    topics TEXT DEFAULT '[]',
                    had_errors INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,
                    was_complex INTEGER DEFAULT 0,
                    open_questions TEXT DEFAULT '[]',
                    unresolved TEXT DEFAULT '[]',
                    dream_potential REAL,
                    dream_potential_reason TEXT,
                    dreams_run TEXT DEFAULT '[]',
                    last_dreamed_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS indexed_runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    indexed_at TEXT,
                    session_count INTEGER,
                    errors INTEGER
                )
            """)
            _print("OK", f"Created session_index.db")
        # Always create indexes (idempotent — CREATE INDEX IF NOT EXISTS is safe on existing tables)
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_sessions_dream_potential ON sessions(dream_potential DESC)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_had_errors     ON sessions(had_errors)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_last_dreamed  ON sessions(last_dreamed_at)",
        ]:
            conn.execute(idx_sql)
        conn.commit()
        conn.close()

        # dream_queue.db schema + indexes
        queue_db = STATE_DIR / "dream_queue.db"
        conn2 = sqlite3.connect(str(queue_db))
        if not queue_db.exists():
            conn2.execute("""
                CREATE TABLE IF NOT EXISTS dream_queue (
                    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    dream_id TEXT UNIQUE,
                    dream_question TEXT,
                    grade REAL,
                    resource_cost INTEGER DEFAULT 1,
                    priority REAL,
                    created_at TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    status TEXT DEFAULT 'queued'
                )
            """)
            _print("OK", f"Created dream_queue.db")
        # Always create indexes
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_queue_status_priority ON dream_queue(status, priority DESC)",
            "CREATE INDEX IF NOT EXISTS idx_queue_dream_id       ON dream_queue(dream_id)",
        ]:
            conn2.execute(idx_sql)
        conn2.commit()
        conn2.close()

        # knowledge_cache.db (v3 new — always create if missing, always add indexes)
        cache_db = STATE_DIR / "knowledge_cache.db"
        conn3 = sqlite3.connect(str(cache_db))
        if not cache_db.exists():
            conn3.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT,
                    content TEXT,
                    source TEXT,
                    cached_at TEXT,
                    content_hash TEXT UNIQUE
                )
            """)
            _print("OK", f"Created knowledge_cache.db")
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_topic_cached ON knowledge_cache(topic, cached_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_cached       ON knowledge_cache(cached_at DESC)",
        ]:
            conn3.execute(idx_sql)
        conn3.commit()
        conn3.close()

        return True
    except Exception as e:
        _print("ERR", f"State init failed: {e}")
        return False


# ── Cron setup ────────────────────────────────────────────────────────────────

def setup_cron(plat: str) -> bool:
    hermes = shutil.which("hermes")
    if not hermes:
        _print("WARN", "hermes CLI not found. Skipping cron setup. Add manually later.")
        return True

    # Use hermes cron if available (cross-platform abstraction)
    try:
        result = subprocess.run([hermes, "cron", "list"], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            _print("WARN", "hermes cron not available. You will need to set up scheduling manually.")
            _print("INFO", "  Linux/macOS/WSL: add to crontab or use hermes gateway")
            _print("INFO", "  Command: python3 ~/.hermes/scripts/dream_scheduler.py")
            _print("INFO", "  Command: python3 ~/.hermes/scripts/session_indexer.py")
            return True
    except Exception:
        _print("WARN", "hermes cron check failed. Skipping automatic cron setup.")
        return True

    jobs = [
        {
            "name": "dream-scheduler",
            "schedule": "*/30 * * * *",
            "command": f"{sys.executable} {SCRIPTS_DIR / 'dream_scheduler.py'}",
        },
        {
            "name": "session-indexer",
            "schedule": "0 */6 * * *",
            "command": f"{sys.executable} {SCRIPTS_DIR / 'session_indexer.py'}",
        },
    ]

    for job in jobs:
        try:
            # Check if job already exists
            result = subprocess.run(
                [hermes, "cron", "list", "--json"],
                capture_output=True, text=True, timeout=10,
            )
            existing = []
            if result.returncode == 0:
                try:
                    data = json.loads(result.stdout)
                    existing = [j.get("name", "") for j in data.get("jobs", [])]
                except Exception:
                    pass

            if job["name"] in existing:
                _print("INFO", f"Cron job '{job['name']}' already exists. Skipping.")
                continue

            result = subprocess.run(
                [hermes, "cron", "create", "--name", job["name"],
                 "--schedule", job["schedule"], "--command", job["command"]],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                _print("OK", f"Created cron job: {job['name']} ({job['schedule']})")
            else:
                _print("WARN", f"Cron create failed for {job['name']}: {result.stderr[:200]}")
        except Exception as e:
            _print("WARN", f"Cron setup error for {job['name']}: {e}")

    return True


# ── Dashboard wrapper ─────────────────────────────────────────────────────────

def setup_dashboard_wrapper() -> bool:
    wrapper = LOCAL_BIN / "dream-dashboard"
    script = SCRIPTS_DIR / "dream_insights_dashboard.py"
    if not script.exists():
        _print("WARN", f"Dashboard script not found at {script}")
        return True

    content = f"""#!/usr/bin/env bash
exec python3 "{script}" "$@"
"""
    try:
        LOCAL_BIN.mkdir(parents=True, exist_ok=True)
        wrapper.write_text(content)
        wrapper.chmod(0o755)
        _print("OK", f"Created dashboard wrapper: {wrapper}")
        return True
    except Exception as e:
        _print("WARN", f"Dashboard wrapper failed: {e}")
        return True


# ── Verification ──────────────────────────────────────────────────────────────

def verify_install() -> bool:
    checks = []

    # Plugin files
    plugin_files = ["__init__.py", "resource_monitor.py", "plugin.yaml"]
    for f in plugin_files:
        exists = (PLUGINS_DIR / "dream_auto" / f).exists()
        checks.append((f"plugin/{f}", exists))

    # Scripts
    script_files = ["dream_scheduler.py", "dream_insights_dashboard.py", "session_indexer.py", "session_grader.py"]
    for f in script_files:
        exists = (SCRIPTS_DIR / f).exists()
        checks.append((f"scripts/{f}", exists))

    # Skill
    skill_files = ["scripts/dream_loop_v3.py", "scripts/fast_path.py", "SKILL.md"]
    for f in skill_files:
        exists = (SKILLS_DIR / "autonomous-ai-agents" / "hermes-dream-task" / f).exists()
        checks.append((f"skill/{f}", exists))

    # DBs
    checks.append(("state/session_index.db", (STATE_DIR / "session_index.db").exists()))
    checks.append(("state/dream_queue.db", (STATE_DIR / "dream_queue.db").exists()))

    all_ok = True
    _print("INFO", "Verification results:")
    for name, ok in checks:
        status = "OK" if ok else "ERR"
        if not ok:
            all_ok = False
        _print(status, f"  {name}")

    return all_ok


# ── Initial index ─────────────────────────────────────────────────────────────

def run_initial_index() -> bool:
    indexer = SCRIPTS_DIR / "session_indexer.py"
    if not indexer.exists():
        _print("WARN", "session_indexer.py not found. Skipping initial index.")
        return True
    try:
        result = subprocess.run(
            [sys.executable, str(indexer), "--limit", "50"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            _print("OK", "Initial session index completed")
            print(C.DIM + result.stdout[-500:] + C.RESET)
            return True
        else:
            _print("WARN", f"Initial index had errors:\n{result.stderr[:300]}")
            return True
    except Exception as e:
        _print("WARN", f"Initial index failed: {e}")
        return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Dream Auto Plugin Installer")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without installing")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    parser.add_argument("--skip-cron", action="store_true", help="Skip cron setup")
    parser.add_argument("--skip-deps", action="store_true", help="Skip pip dependency install")
    args = parser.parse_args()

    print(f"\n{C.INFO}Dream Auto Plugin Installer v{INSTALLER_VERSION}{C.RESET}")
    print(f"{C.DIM}Platform: {platform.system()} | Python: {sys.version.split()[0]}{C.RESET}\n")

    if args.dry_run:
        _print("INFO", "DRY RUN MODE — no changes will be made")

    # Prerequisites
    ok = True
    ok = check_python_version() and ok
    ok = check_hermes_cli() and ok
    ok = check_pip() and ok
    plat = check_platform()

    if not ok:
        _print("ERR", "Prerequisite checks failed. Fix the issues above and re-run.")
        sys.exit(1)

    if args.dry_run:
        _print("INFO", "Dry run complete. Re-run without --dry-run to install.")
        sys.exit(0)

    # Install
    print()
    _print("INFO", "Installing files...")
    if not install_files(force=args.force):
        sys.exit(1)

    if not args.skip_deps:
        print()
        _print("INFO", "Installing Python dependencies...")
        install_dependencies()

    print()
    _print("INFO", "Initializing state databases...")
    if not init_state():
        sys.exit(1)

    if not args.skip_cron:
        print()
        _print("INFO", "Setting up cron jobs...")
        setup_cron(plat)

    print()
    _print("INFO", "Setting up dashboard wrapper...")
    setup_dashboard_wrapper()

    print()
    _print("INFO", "Running initial session index...")
    run_initial_index()

    print()
    _print("INFO", "Verifying installation...")
    if verify_install():
        _print("OK", "Installation complete!")
        print(f"\n{C.INFO}Next steps:{C.RESET}")
        print(f"  1. Enable the plugin in your Hermes config or ensure DREAM_AUTO_ENABLED=1")
        print(f"  2. Run: hermes cron status       # verify scheduler is queued")
        print(f"  3. Run: dream-dashboard          # view the dashboard")
        print(f"  4. Check docs: {SKILLS_DIR / 'ops' / 'dream-system-v3' / 'SKILL.md'}")
    else:
        _print("WARN", "Installation finished with warnings. Review output above.")


if __name__ == "__main__":
    main()
