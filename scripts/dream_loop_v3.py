#!/usr/bin/env python3
"""
dream_loop_v3.py — Monte Carlo Dream Engine for Dream System v3

Architecture:
  - MCTS (Monte Carlo Tree Search) with branching exploration
  - Each node: N rollouts, win rate, confidence interval, uncertainty
  - MetaRAG Monitor/Evaluate/Plan loop per iteration
  - Reflexion: semantic cross-dream learning (find_related_dreams)
  - Uncertainty-aware distillation (N runs → consensus)
  - Two-tier rollouts: fast LLM-only + on-demand tool-using AIAgent

Usage:
    python3 dream_loop_v3.py <dream_id> "<brief>"

State: ~/.hermes/state/dream/<dream_id>/
    meta.json              — dream metadata, status, confidence
    exploration_tree.json   — MCTS tree (nodes with win rates + CI)
    insights.json           — distilled insights
    failures.json           — failure patterns
    pending_questions.json   — open questions
    monte_carlo_runs.json   — rollout data
    uncertainty.json        — confidence intervals
"""

import json
import os
import psutil
import re
import sqlite3
import subprocess
import sys
import time
import uuid as _uuid
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, ALL_COMPLETED
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

# ── Add hermes_agent to path for DreamAgent ──────────────────────────────────
HERMES_AGENT_ROOT = Path.home() / ".hermes" / "hermes-agent"
if str(HERMES_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT_ROOT))

# ── paths ────────────────────────────────────────────────────────────────────
DREAM_DIR = Path.home() / ".hermes" / "state" / "dream"
SYSTEM_PAUSE_FLAG = DREAM_DIR / "SYSTEM_PAUSE"
DB_PATH = Path.home() / ".hermes" / "state" / "dream" / "session_index.db"
SESSIONS_DIR = Path.home() / ".hermes" / "sessions"
HERMES_BIN = Path.home() / ".local" / "bin" / "hermes"
DREAM_SKILL_DIR = Path.home() / ".hermes" / "skills" / "autonomous-ai-agents" / "hermes-dream-task"

GMT7 = timezone(timedelta(hours=7))

# ── MCTS config ──────────────────────────────────────────────────────────────
import math as _math
MAX_ITERATIONS = 10
ROLLOUTS_PER_NODE = 3        # N rollouts per branch
MAX_CHILDREN_PER_NODE = _math.ceil(ROLLOUTS_PER_NODE / 2)  # branches per expand
MAX_TREE_DEPTH = 4
CONFIDENCE_STOP = 0.80       # Stop when CI width < 0.1 (very confident)
MIN_CONFIDENCE = 0.60        # Stop when confidence >= this AND CI narrow
DISTILLATION_RUNS = 5        # N times to run distillation for consensus
SLEEP_SECONDS = 60           # between iterations

# ── Two-tier rollout config ───────────────────────────────────────────────────
TOOLSETS = ["terminal", "file", "session_search", "memory"]
TOOL_ROLLOUT_THRESHOLD = 0.30   # Tier-1 confidence below this → try tool rollout
TOOL_ROLLOUT_BRANCH_LIMIT = 1   # Only top-1 UCB1 branch gets tool rollout per iter
TOOL_CALL_LIMIT = 5             # Max tool calls per tool-using rollout
TOOL_CALL_TIMEOUT = 30           # Seconds per individual tool call
TOOL_ROLLOUT_TIMEOUT = 120       # Hard cap for entire tool-using rollout
TOOL_ROLLOUT_LIGHTWEIGHT_TIMEOUT = 60  # Pre-flight LLM decision call timeout


# ── MCTS data structures ───────────────────────────────────────────────────────

@dataclass
class MCTSNode:
    node_id: str
    parent_id: Optional[str]
    depth: int
    approach: str              # description of this branch's approach
    n_visits: int = 0
    wins: float = 0.0          # win count (for success probability)
    sum_squared: float = 0.0  # for CI calculation
    confidence: float = 0.0    # win rate
    ci_width: float = 1.0     # confidence interval width
    children: list = field(default_factory=list)
    rollout_result: Optional[dict] = None  # final rollout result

    def win_rate(self) -> float:
        return self.wins / self.n_visits if self.n_visits > 0 else 0.5

    def update(self, outcome: float):
        """Update node stats with rollout outcome (0-1)."""
        self.n_visits += 1
        self.wins += outcome
        self.sum_squared += outcome * outcome
        n = self.n_visits
        if n > 1:
            mean = self.wins / n
            var = (self.sum_squared / n) - (mean * mean)
            var = max(0, var)  # numerical stability
            self.confidence = mean
            # Wilson score CI approximation
            z = 1.96  # 95% CI
            denom = 1 + z*z/n
            center = mean + z*z/(2*n)
            margin = z * _math.sqrt(var/n + z*z/(4*n*n))
            self.ci_width = 2 * margin / denom
        else:
            self.confidence = outcome
            self.ci_width = 1.0


# ── File helpers ──────────────────────────────────────────────────────────────

def dream_path(dream_id: str) -> Path:
    return DREAM_DIR / dream_id

def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default

def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


# ── LLM helpers ──────────────────────────────────────────────────────────────

def call_hermes(prompt: str, timeout: int = 90) -> str:
    """Call hermes chat -q and return text response."""
    try:
        env = os.environ.copy()
        env.pop("HERMES_SESSION", None)
        env["HERMES_QUIET"] = "1"
        result = subprocess.run(
            [str(HERMES_BIN), "chat", "-q", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(Path.home()),
            env=env,
        )
        return result.stdout.strip()
    except Exception as e:
        return f"[LLM_ERROR: {e}]"

def parse_json_response(text: str) -> Optional[dict]:
    """Parse JSON from LLM output."""
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                pass
    # Try regex extract
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            for i in range(len(match.group()), 0, -1):
                return json.loads(match.group()[:i])
        except Exception:
            pass
    return None


# ── DreamAgent: tool-using AIAgent wrapper ────────────────────────────────────

class DreamAgent:
    """
    Lightweight AIAgent wrapper for dream rollouts.

    Reuses a single AIAgent instance per dream (cold-start avoidance).
    diagnostic prompt instructs the agent to use tools and return structured JSON.
    """

    def __init__(self, toolsets: list[str] = None):
        self.toolsets = toolsets or TOOLSETS
        self._agent = None
        self._init_error: Optional[str] = None
        self._init_agent()

    def _init_agent(self):
        """Initialize AIAgent with toolset access. Fails gracefully if no API access."""
        try:
            from run_agent import AIAgent

            # Read user's configured model/provider from config.yaml (portable)
            import yaml
            config_path = Path.home() / ".hermes" / "config.yaml"
            cfg = {}
            if config_path.exists():
                try:
                    cfg = yaml.safe_load(config_path.read_text()) or {}
                except Exception:
                    pass

            model_cfg = cfg.get("model", {}) or {}
            provider_cfg = model_cfg.get("provider") or "minimax"
            model_cfg_default = model_cfg.get("default") or ""

            # Also check for openrouter as a bypass option (users can set OPENROUTER_API_KEY)
            openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
            use_openrouter = bool(openrouter_key)

            if use_openrouter:
                # OpenRouter bypass — requires OPENROUTER_API_KEY env var
                self._agent = AIAgent(
                    provider="openrouter",
                    model="anthropic/claude-sonnet-4",
                    enabled_toolsets=self.toolsets,
                    quiet_mode=True,
                    verbose_logging=False,
                    max_iterations=TOOL_CALL_LIMIT,
                    tool_delay=TOOL_CALL_TIMEOUT,
                )
            else:
                # Use user's configured provider (needs active gateway session for API key)
                self._agent = AIAgent(
                    provider=provider_cfg,
                    model=model_cfg_default,
                    enabled_toolsets=self.toolsets,
                    quiet_mode=True,
                    verbose_logging=False,
                    max_iterations=TOOL_CALL_LIMIT,
                    tool_delay=TOOL_CALL_TIMEOUT,
                )
        except Exception as e:
            self._init_error = str(e)
            self._agent = None

    @property
    def is_available(self) -> bool:
        return self._agent is not None

    def think(self, prompt: str, timeout: int = 90) -> str:
        """Fast LLM-only response (no tool calls). Used for pre-flight decisions."""
        if not self.is_available:
            return call_hermes(prompt, timeout=timeout)
        try:
            # Use a fresh chat session (no session_id = ephemeral)
            result = self._agent.chat(prompt)
            return result if isinstance(result, str) else str(result)
        except Exception as e:
            # Fallback to subprocess
            return call_hermes(prompt, timeout=timeout)

    def diagnose(self, brief: str, branch_label: str = "", branch_desc: str = "",
                 error_context: str = "", iteration: int = 0) -> dict:
        """
        Tool-using diagnostic rollout.

        Instructs AIAgent to run actual diagnostic commands and return
        structured JSON with evidence. Falls back to LLM-only on failure.
        """
        if not self.is_available:
            return self._diagnose_fallback(brief, branch_label, branch_desc, iteration)

        # Build diagnostic prompt with lean context
        prompt = self._build_diagnostic_prompt(brief, branch_label, branch_desc, error_context, iteration)

        try:
            result = self._agent.chat(prompt)
            text = result if isinstance(result, str) else str(result)
            parsed = self._parse_diagnostic_response(text)
            if parsed:
                return parsed
            # Fallback: if no parseable JSON, treat as uncertain
            return {
                "outcome": "uncertain",
                "confidence": 0.5,
                "key_factors": [],
                "evidence": text[:200] if text else "no response",
                "reason": "no parseable JSON from tool rollout",
                "remaining_uncertainty": "response format unexpected",
            }
        except Exception as e:
            return {
                "outcome": "uncertain",
                "confidence": 0.5,
                "key_factors": [],
                "evidence": f"tool rollout error: {e}",
                "reason": f"exception during tool rollout: {e}",
                "remaining_uncertainty": "unknown — error occurred",
            }

    def _build_diagnostic_prompt(self, brief: str, branch_label: str, branch_desc: str,
                                 error_context: str, iteration: int) -> str:
        """Build lean diagnostic prompt for tool-using rollout."""
        return f"""You are debugging a dream (iteration {iteration}).

OBJECTIVE: {brief}
BRANCH: {branch_label}
DESCRIPTION: {branch_desc}
ERROR CONTEXT: {error_context or 'none'}

Your task: Investigate using tools. You have access to: terminal, file read, session_search, memory.
Do NOT just reason. Actually investigate.

Investigate steps (choose up to {TOOL_CALL_LIMIT}):
1. terminal: grep ERROR logs — try: grep -r ERROR ~/.hermes/logs/ 2>/dev/null | tail -20
2. terminal: check recent cron runs — try: ls -lt ~/.hermes/state/dream/ 2>/dev/null | head -10
3. session_search: search past sessions for related errors — try: session_search("<brief snippet>")
4. read_file: check FAILED_DREAMS.md or recent dream meta.json files

Return STRICTLY ONLY this JSON at the end (no prose before it):
{{"outcome": "success", "failure", or "uncertain",
  "confidence": 0.0-1.0,
  "key_factors": ["factor 1", "factor 2"],
  "evidence": "what you actually found via tools (be specific)",
  "reason": "one sentence explanation",
  "remaining_uncertainty": "what's still unclear"}}

If tools fail, still return JSON but set evidence="tool failed: <error>".
Do NOT return anything except the JSON block."""

    def _parse_diagnostic_response(self, text: str) -> Optional[dict]:
        """Extract structured JSON from DreamAgent response."""
        # Look for JSON block anywhere in response
        match = re.search(r'\{.*?"outcome".*?\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        # Try to find any {...} block and parse from start
        brace_start = text.find("{")
        if brace_start != -1:
            try:
                return json.loads(text[brace_start:])
            except json.JSONDecodeError:
                pass
        return None

    def _diagnose_fallback(self, brief: str, branch_label: str, branch_desc: str, iteration: int) -> dict:
        """Fallback when AIAgent unavailable — use LLM-only reasoning."""
        prompt = f"""You are running a mental simulation of this reasoning approach:

APPROACH: {branch_label}
DESCRIPTION: {branch_desc}

BRIEF: {brief}

Simulate this approach mentally from start to finish.
What would happen? Would it succeed or fail?
What are the key factors?

Respond with ONLY JSON:
{{"outcome": "success", "failure", or "uncertain",
  "confidence": 0.0-1.0,
  "key_factors": ["factor 1", "factor 2"],
  "evidence": "reasoning",
  "reason": "one sentence",
  "remaining_uncertainty": "what's still unclear"}}
"""
        response = call_hermes(prompt, timeout=90)
        result = parse_json_response(response)
        if result:
            return result
        return {"outcome": "uncertain", "confidence": 0.5, "key_factors": [],
                "evidence": "parse failed", "reason": "LLM parse failed",
                "remaining_uncertainty": "unknown"}


# ── DreamAgent pool (cold-start avoidance) ────────────────────────────────────

class DreamAgentPool:
    """Reuse a single AIAgent instance across all tool rollouts in one dream."""

    def __init__(self, toolsets: list[str] = None):
        self.toolsets = toolsets or TOOLSETS
        self._agent: Optional[DreamAgent] = None

    def get(self) -> DreamAgent:
        if self._agent is None:
            self._agent = DreamAgent(toolsets=self.toolsets)
        return self._agent

    def reset(self):
        self._agent = None


# ── Tool-using rollout helpers ────────────────────────────────────────────────

def should_use_tool_rollout(cpu: float, ram: float) -> bool:
    """Resource-aware check: only use expensive tool rollouts when system is idle."""
    if cpu > 70 or ram > 70:
        return False  # too expensive, stay fast
    if cpu < 30 and ram < 50:
        return True   # plenty of headroom
    return False      # moderate — skip to preserve resources


def get_resource_usage() -> tuple[float, float]:
    """Get current CPU and RAM usage."""
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        ram = psutil.virtual_memory().percent
        return cpu, ram
    except Exception:
        return 0.0, 0.0


def preflight_tool_decision(brief: str, branch_label: str, agent_pool: DreamAgentPool) -> bool:
    """
    Cheap LLM call (~1s) to decide if tools would help for this rollout.

    Returns True if tools recommended, False if not needed.
    """
    prompt = f"""Given this error/brief: "{brief[:200]}"
Branch: "{branch_label}"

Would running actual diagnostic commands (terminal, session_search) help investigate this?
Answer in 2 sentences max. Then respond: TOOL_CHOICE=yes or TOOL_CHOICE=no"""
    try:
        response = agent_pool.get().think(prompt, timeout=TOOL_ROLLOUT_LIGHTWEIGHT_TIMEOUT)
        return "TOOL_CHOICE=yes" in response[:100]
    except Exception:
        return False


def collect_error_context(brief: str, agent_pool: DreamAgentPool) -> str:
    """
    Pre-gather lean context before tool rollout:
    - Recent related session summaries via session_search
    - Last few lines of hermes log
    - Recent failed dreams
    This feeds lean context into the diagnostic prompt.
    """
    context_parts = []

    # Quick session search for brief keywords (best effort)
    prompt = f"Find sessions related to: {brief[:150]}. Return up to 3 session IDs and their summaries."
    try:
        session_res = agent_pool.get().think(prompt, timeout=15)
        if session_res and len(session_res) > 20:
            context_parts.append(f"RELATED SESSIONS:\n{session_res[:300]}")
    except Exception:
        pass

    # Recent log lines
    try:
        log_path = Path.home() / ".hermes" / "logs" / "hermes.log"
        if log_path.exists():
            lines = log_path.read_text().strip().split("\n")
            context_parts.append(f"RECENT LOG:\n" + "\n".join(lines[-5:]))
    except Exception:
        pass

    return "\n\n".join(context_parts) if context_parts else ""


# ── Branch generation (EXPAND) ────────────────────────────────────────────────

def generate_branches(brief: str, depth: int, parent_approaches: list[str] = None) -> list[dict]:
    """Generate N candidate branches for the given brief."""
    parent_str = ""
    if parent_approaches:
        parent_str = "\n".join(f"- {a}" for a in parent_approaches[-3:])

    previous = ("PREVIOUS APPROACHES (don't repeat these):\n" + parent_str) if parent_str else ""

    prompt = f"""You are exploring different reasoning approaches for this problem:

BRIEF: {brief}

{previous}

Generate {MAX_CHILDREN_PER_NODE} distinct reasoning approaches to explore.
Each approach should be a different angle, method, or strategy.
Be specific and concrete — not abstract.

Respond with ONLY JSON — an array of approach objects:
[
  {{
    "approach_id": "A1",
    "label": "One-line label for this approach",
    "description": "What this approach does and why it might work"
  }},
  ... (4 more)
]

Keep labels to 10 words max. Total response under 500 words.
"""
    response = call_hermes(prompt, timeout=60)
    data = parse_json_response(response)
    if data and isinstance(data, list):
        return data[:4]
    return [{"approach_id": "fallback", "label": "Direct analysis", "description": brief}]


# ── Anti-thrashing (Ralph-loop style) ─────────────────────────────────────────

_iteration_history: list = []  # Module-level for persistence across iterations

def detect_staleness(tree: dict, max_minutes: int = 20, no_progress_minutes: int = 8) -> dict:
    """
    Time-based staleness detection: a dream is stale if wallclock exceeded max_minutes
    AND no new nodes were added in the last no_progress_minutes.
    Returns dict with 'stale' bool and 'reason' string.
    """
    if not tree.get("wallclock_start"):
        return {"stale": False}

    try:
        start = datetime.fromisoformat(tree["wallclock_start"])
        last_added = datetime.fromisoformat(tree["last_node_added_at"])
    except (ValueError, TypeError):
        return {"stale": False}

    now = datetime.now(GMT7)
    total_minutes = (now - start).total_seconds() / 60
    stale_minutes = (now - last_added).total_seconds() / 60

    if total_minutes > max_minutes and stale_minutes > no_progress_minutes:
        return {
            "stale": True,
            "reason": f"wallclock={total_minutes:.0f}min > {max_minutes}min AND "
                      f"no new nodes for {stale_minutes:.0f}min > {no_progress_minutes}min"
        }
    return {"stale": False}


def detect_thrashing(tree: dict, history: list, recent_confidences: list) -> bool:
    """
    Ralph-loop style: detect if we're spinning without making progress.
    Same low-confidence result 3x in a row = thrashing → pivot to wrap_up.
    """
    if len(recent_confidences) < 3:
        return False
    # All last 3 iterations had same low confidence?
    last3 = recent_confidences[-3:]
    if len(set(round(c, 2) for c in last3)) == 1 and last3[0] < 0.55:
        return True
    # Confidence going down 2x in a row
    if len(recent_confidences) >= 3:
        if recent_confidences[-1] < recent_confidences[-2] < recent_confidences[-3]:
            return True
    return False


# ── Rollout engine (ROLLOUT) — Two-tier ─────────────────────────────────────

def rollout_tier1(branch: dict, brief: str, iteration: int) -> dict:
    """
    Tier 1 — fast LLM-only rollout (~1-2s).
    Used for all rollouts by default.
    """
    approach_label = branch.get("label", "analysis")
    approach_desc = branch.get("description", "")

    prompt = f"""You are running a mental simulation of this reasoning approach:

APPROACH: {approach_label}
DESCRIPTION: {approach_desc}

BRIEF: {brief}

Simulate this approach mentally from start to finish.
What would happen? Would it succeed or fail?
What are the key factors that determine success or failure?

Respond with ONLY JSON:
{{"outcome": "success", "failure", or "uncertain",
  "confidence": 0.0-1.0,
  "key_factors": ["factor 1", "factor 2", "factor 3"],
  "reason": "one sentence explanation",
  "remaining_uncertainty": "what's still unclear"}}
"""
    response = call_hermes(prompt, timeout=90)
    result = parse_json_response(response)
    if not result:
        result = {"outcome": "uncertain", "confidence": 0.5, "key_factors": [],
                  "reason": "LLM parse failed", "remaining_uncertainty": "parse error"}
    # Normalize outcome to float
    outcome_map = {"success": 1.0, "uncertain": 0.5, "failure": 0.0}
    result["outcome_float"] = outcome_map.get(result.get("outcome", "uncertain"), 0.5)
    result["tier"] = 1
    return result


def rollout_tier2(branch: dict, brief: str, iteration: int,
                  agent_pool: DreamAgentPool, error_context: str = "") -> dict:
    """
    Tier 2 — tool-using AIAgent rollout (~30-120s).
    Triggered only when Tier 1 confidence < TOOL_ROLLOUT_THRESHOLD
    and only on the best UCB1 node per iteration.
    """
    agent = agent_pool.get()
    if not agent.is_available:
        # AIAgent unavailable — fall back to tier1
        return rollout_tier1(branch, brief, iteration)

    result = agent.diagnose(
        brief=brief,
        branch_label=branch.get("label", ""),
        branch_desc=branch.get("description", ""),
        error_context=error_context,
        iteration=iteration,
    )

    outcome_map = {"success": 1.0, "uncertain": 0.5, "failure": 0.0}
    result["outcome_float"] = outcome_map.get(result.get("outcome", "uncertain"), 0.5)
    result["tier"] = 2
    return result


def rollout(branch: dict, brief: str, iteration: int,
            agent_pool: Optional[DreamAgentPool] = None,
            is_top_branch: bool = False,
            cpu: float = 0.0, ram: float = 0.0) -> dict:
    """
    Two-tier rollout entry point.

    Tier 1 (always): fast LLM-only, 1-2s
    Tier 2 (on-demand): tool-using, 30-120s, triggered when:
      - Tier 1 confidence < TOOL_ROLLOUT_THRESHOLD
      - is_top_branch (only the best UCB1 node gets tool access per iter)
      - system resources allow (cpu < 70%, ram < 70%)
    """
    # Tier 1 always runs first
    result_t1 = rollout_tier1(branch, brief, iteration)
    confidence_t1 = result_t1.get("confidence", 0.5)

    # Decision: should we upgrade to Tier 2?
    should_upgrade = (
        is_top_branch
        and confidence_t1 < TOOL_ROLLOUT_THRESHOLD
        and should_use_tool_rollout(cpu, ram)
        and agent_pool is not None
    )

    if not should_upgrade:
        return result_t1

    # Optional: pre-flight decision (adds ~1s)
    # Uncomment to enable pre-flight:
    # if preflight_tool_decision(brief, branch.get("label", ""), agent_pool):
    #     pass  # proceed to tier2
    # else:
    #     return result_t1

    # Pre-gather lean error context
    error_context = ""
    try:
        error_context = collect_error_context(brief, agent_pool)
    except Exception:
        pass

    # Run Tier 2 tool-using rollout
    result_t2 = rollout_tier2(branch, brief, iteration, agent_pool, error_context)

    # Use Tier 2 if it has better signal; otherwise stick with Tier 1
    if result_t2.get("confidence", 0) > confidence_t1:
        return result_t2
    return result_t1


# ── MCTS core ────────────────────────────────────────────────────────────────

def mcts_init_tree(brief: str) -> dict:
    """Initialize MCTS tree with root."""
    root_id = "root"
    tree = {
        "nodes": [
            {
                "node_id": root_id,
                "parent_id": None,
                "depth": 0,
                "approach": brief,
                "n_visits": 0,
                "wins": 0.0,
                "confidence": 0.5,
                "ci_width": 1.0,
                "children": [],
            }
        ],
        "current_root": root_id,
        "last_node_added_at": datetime.now(GMT7).isoformat(),
        "wallclock_start": datetime.now(GMT7).isoformat(),
    }
    return tree


def mcts_select(tree: dict) -> Optional[str]:
    """SELECT: traverse tree, pick highest-value unexplored child.
    Uses UCB1-Tuned (variance-aware) with CI-width bonus for uncertain nodes.
    Adaptive C: higher at low visits (exploration), lower at high visits (exploitation).
    """
    nodes_by_id = {n["node_id"]: n for n in tree["nodes"]}
    root = nodes_by_id.get(tree["current_root"], nodes_by_id["root"])
    children = [nodes_by_id[c] for c in root["children"] if c in nodes_by_id]

    if not children:
        return None  # Leaf — expand here

    # Adaptive C: shrinks as tree gets more visited (more exploitation, less exploration)
    # C = C_base * (1 + 1/sqrt(max(parent_visits,1)))
    C_BASE = 1.414  # sqrt(2)
    parent_visits = max(root.get("n_visits", 1), 1)
    C_ADAPTIVE = C_BASE * (1.0 + 1.0 / _math.sqrt(parent_visits))

    best_child = None
    best_ucb = -float("inf")

    for child in children:
        depth = child.get("depth", 1)
        ci_width = child.get("ci_width", 1.0)

        if child["n_visits"] == 0:
            ucb = float("inf")  # unexplored = always try
        else:
            win_rate = child["wins"] / child["n_visits"]
            n = child["n_visits"]

            # UCB1-Tuned: use min() to cap exploration term by variance
            var = win_rate - win_rate * win_rate
            ucb1_term = 2.0 * _math.log(parent_visits) / n
            tuned_cap = 1.0 / n
            tuned_exploration = min(ucb1_term, tuned_cap + var)

            depth_factor = 1.0 / _math.sqrt(depth)
            ci_bonus = 0.15 * ci_width * depth_factor * C_ADAPTIVE

            ucb = win_rate + C_ADAPTIVE * _math.sqrt(tuned_exploration) + ci_bonus

        if ucb > best_ucb:
            best_ucb = ucb
            best_child = child

    return best_child["node_id"] if best_child else None


def mcts_expand(tree: dict, node_id: str, brief: str, depth: int) -> list[str]:
    """EXPAND: add N child nodes from the given node."""
    max_children = _math.ceil(ROLLOUTS_PER_NODE / 2)

    nodes_by_id = {n["node_id"]: n for n in tree["nodes"]}
    parent = nodes_by_id.get(node_id)
    if not parent or depth >= MAX_TREE_DEPTH:
        return []

    # Get parent approach chain for context
    parent_chain = []
    cid = node_id
    for _ in range(5):
        p = nodes_by_id.get(cid)
        if not p or p["parent_id"] is None:
            break
        parent_chain.append(p["approach"])
        cid = p["parent_id"]

    branches = generate_branches(brief, depth, parent_chain)
    new_ids = []

    for branch in branches[:max_children]:
        child_id = f"{node_id}_{branch.get('approach_id', str(_uuid.uuid4())[:8])}"
        child = {
            "node_id": child_id,
            "parent_id": node_id,
            "depth": depth,
            "approach": branch.get("label", "analysis"),
            "approach_desc": branch.get("description", ""),
            "n_visits": 0,
            "wins": 0.0,
            "confidence": 0.5,
            "ci_width": 1.0,
            "children": [],
        }
        tree["nodes"].append(child)
        nodes_by_id[child_id] = child
        parent["children"].append(child_id)
        new_ids.append(child_id)

    # Track last expansion time for staleness detection
    if new_ids:
        tree["last_node_added_at"] = datetime.now(GMT7).isoformat()

    return new_ids


def mcts_backpropagate(tree: dict, node_id: str, outcome: float):
    """BACKPROPAGATE: update win rates up the tree."""
    nodes_by_id = {n["node_id"]: n for n in tree["nodes"]}
    cid = node_id
    while cid is not None:
        node = nodes_by_id.get(cid)
        if not node:
            break
        node["n_visits"] = node.get("n_visits", 0) + 1
        node["wins"] = node.get("wins", 0.0) + outcome
        # Recalculate confidence
        n = node["n_visits"]
        if n > 1:
            mean = node["wins"] / n
            node["confidence"] = round(mean, 3)
            if n > 2:
                node["ci_width"] = round(1.0 / _math.sqrt(n), 3)
        cid = node.get("parent_id")


# ── Meta / file helpers ──────────────────────────────────────────────────────

def read_meta(dream_id: str) -> dict:
    return read_json(dream_path(dream_id) / "meta.json", default={})

def write_meta(dream_id: str, meta_info: dict):
    write_json(dream_path(dream_id) / "meta.json", meta_info)


# ── MetaRAG: Monitor / Evaluate / Plan ───────────────────────────────────────

def metarag_monitor(state: dict) -> dict:
    """Monitor: assess if current exploration is productive."""
    prompt = f"""MONITOR — assess current dream state:

Current brief: {state.get('brief', '')[:300]}
Iteration: {state.get('iteration', 0)}/{MAX_ITERATIONS}
Current best confidence: {state.get('best_confidence', 0)}
Insights so far: {state.get('insights', [])}
Active branches: {state.get('active_branches', 0)}

Is this exploration productive? Are we getting closer to useful conclusions?
Respond with ONLY JSON:
{{"productive": true/false, "reason": "...", "concerns": ["?", "?"]}}
"""
    response = call_hermes(prompt, timeout=60)
    result = parse_json_response(response)
    return result or {"productive": True, "reason": "continuing", "concerns": []}


def metarag_evaluate(state: dict, alternatives: list) -> dict:
    """Evaluate: deep assessment of current trajectory vs alternatives."""
    prompt = f"""EVALUATE — compare current approach vs alternatives:

Brief: {state.get('brief', '')[:300]}

Current trajectory confidence: {state.get('best_confidence', 0)}
Alternatives to consider: {alternatives[:3]}

Is the current approach better than alternatives? What would switching cost?
Respond with ONLY JSON:
{{"stay_the_course": true/false, "switch_to": "alternative_id or null", "reason": "..."}}
"""
    response = call_hermes(prompt, timeout=60)
    result = parse_json_response(response)
    return result or {"stay_the_course": True, "switch_to": None, "reason": "no signal"}


def metarag_plan(state: dict) -> dict:
    """Plan: decide next action based on evaluation."""
    prompt = f"""PLAN — decide next action:

Brief: {state.get('brief', '')[:300]}
Iteration: {state.get('iteration', 0)}/{MAX_ITERATIONS}
Current tree: {state.get('tree_summary', 'shallow')}

What should we do next?
Options:
- expand_more: Explore more branches from current position
- go_deeper: Follow the most promising path deeper
- pivot: Try a completely different angle
- wrap_up: We're confident enough, distill insights

Respond with ONLY JSON:
{{"action": "expand_more|go_deeper|pivot|wrap_up", "reason": "...", "target_node": "node_id or null"}}
"""
    response = call_hermes(prompt, timeout=60)
    result = parse_json_response(response)
    return result or {"action": "expand_more", "reason": "continuing exploration", "target_node": None}


# ── Semantic Cross-Dream Learning (Reflexion) ─────────────────────────────────

def find_related_dreams(brief: str) -> list[dict]:
    """Find past dreams related to the current brief using LLM semantic search."""
    if not DREAM_DIR.exists():
        return []

    past_dreams = []
    for d in DREAM_DIR.iterdir():
        if not d.is_dir():
            continue
        meta = read_json(d / "meta.json", {})
        insights = read_json(d / "insights.json", [])
        if meta.get("status") in ("done", "completed") and insights:
            past_dreams.append({
                "dream_id": d.name,
                "brief": meta.get("brief", "")[:200],
                "insights": insights[:3],
                "confidence": meta.get("confidence", 0),
            })

    if not past_dreams:
        return []

    prompt = f"""Find past dreams related to: {brief[:300]}

PAST DREAMS:
{json.dumps(past_dreams[:10], indent=2)}

Respond with the 3 most related. Explain relevance in 1 sentence each.
Return ONLY JSON:
{{"related": [{{"dream_id": "...", "relevance": "..."}}]}}
"""
    response = call_hermes(prompt, timeout=60)
    result = parse_json_response(response)
    if result and "related" in result:
        return result["related"][:3]
    return []


def incorporate_related_insights(related: list[dict]) -> list[str]:
    """Incorporate insights from related dreams."""
    insights = []
    for rel in related:
        dream_id = rel.get("dream_id", "")
        dp = dream_path(dream_id)
        if dp.exists():
            past = read_json(dp / "insights.json", [])
            insights.extend([f"[from {dream_id}] {i}" for i in past[:2]])
    return insights[:6]


# ── Uncertainty-aware distillation ────────────────────────────────────────────

def distill_insights_n_times(tree: dict, brief: str, n: int = DISTILLATION_RUNS) -> dict:
    """Run distillation N times in parallel, aggregate results for consensus."""
    all_insights = []
    all_failures = []
    all_questions = []

    def run_distillation_pass(i: int) -> dict:
        run_prompt = f"""Distill the key insights from this MCTS exploration:

BRIEF: {brief}

EXPLORATION SUMMARY:
{tree_summary(tree)}

Focus on:
- The most confident successful approaches (win rate > 0.6)
- Key success factors that appeared across multiple branches
- What NOT to do (failures with high confidence)
- Open questions that remain

Respond with ONLY JSON:
{{"insights": ["insight 1", "insight 2", "insight 3"],
  "failures": ["failure pattern 1"],
  "questions": ["open question 1"]}}
"""
        response = call_hermes(run_prompt, timeout=90)
        result = parse_json_response(response)
        if result:
            return result
        return {"insights": [], "failures": [], "questions": []}

    # Parallelize N distillation passes (5x speedup)
    with ThreadPoolExecutor(max_workers=min(n, 5)) as executor:
        futures = [executor.submit(run_distillation_pass, i) for i in range(n)]
        for future in as_completed(futures):
            result = future.result()
            all_insights.extend(result.get("insights", []))
            all_failures.extend(result.get("failures", []))
            all_questions.extend(result.get("questions", []))

    # Find consensus: insights appearing 2+ times
    insight_counts: dict[str, int] = {}
    for ins in all_insights:
        key = ins.lower()[:80]
        insight_counts[key] = insight_counts.get(key, 0) + 1

    consensus_insights = [k for k, v in insight_counts.items() if v >= 2]
    novel_insights = [k for k, v in insight_counts.items() if v == 1]

    # Take top 3 consensus + up to 2 novel
    final_insights = consensus_insights[:3] + novel_insights[:2]

    return {
        "consensus_insights": consensus_insights[:5],
        "novel_insights": novel_insights[:3],
        "all_insights": all_insights,
        "failures": list(set(all_failures))[:5],
        "questions": list(set(all_questions))[:5],
        "n_runs": n,
    }


def tree_summary(tree: dict) -> str:
    """Generate a text summary of the MCTS tree."""
    nodes = tree.get("nodes", [])
    if not nodes:
        return "Empty tree"

    lines = []
    for node in nodes:
        ci = node.get("ci_width", 1.0)
        conf = node.get("confidence", 0)
        visits = node.get("n_visits", 0)
        depth = node.get("depth", 0)
        label = node.get("approach", "")[:50]
        lines.append(f"  {'  ' * depth}[depth={depth}, visits={visits}, conf={conf:.2f}±{ci:.2f}] {label}")

    return "\n".join(lines[:30])  # limit output size


# ── Self-throttling between iterations ────────────────────────────────────────

def self_throttle(base_sleep: int = SLEEP_SECONDS) -> float:
    """
    Check system resources and pause flag before continuing next iteration.
    Returns actual seconds slept. If SYSTEM_PAUSE exists, waits until removed.
    If CPU/RAM are high, extends sleep or asks LLM for guidance.
    """
    # 1. Check for system pause flag
    if SYSTEM_PAUSE_FLAG.exists():
        print(f"  [THROTTLE] SYSTEM_PAUSE detected — waiting for removal...")
        waited = 0
        while SYSTEM_PAUSE_FLAG.exists():
            time.sleep(30)
            waited += 30
            if waited % 120 == 0:
                print(f"  [THROTTLE] Still paused after {waited}s...")
        print(f"  [THROTTLE] Pause lifted after {waited}s, resuming")
        return waited

    # 2. Check resources
    try:
        cpu = psutil.cpu_percent(interval=1.0)
        ram = psutil.virtual_memory().percent
    except Exception:
        cpu, ram = 0, 0

    # 3. Hard cap: critical resources → long sleep
    if cpu >= 90 or ram >= 95:
        sleep_time = 120
        print(f"  [THROTTLE] CPU={cpu:.0f}% RAM={ram:.0f}% critical — sleeping {sleep_time}s")
        time.sleep(sleep_time)
        return sleep_time

    # 4. High but not critical → moderate sleep
    if cpu >= 75 or ram >= 85:
        sleep_time = 90
        print(f"  [THROTTLE] CPU={cpu:.0f}% RAM={ram:.0f}% high — sleeping {sleep_time}s")
        time.sleep(sleep_time)
        return sleep_time

    # 5. Moderate → base sleep
    if cpu >= 50 or ram >= 70:
        sleep_time = base_sleep
        print(f"  [THROTTLE] CPU={cpu:.0f}% RAM={ram:.0f}% moderate — sleeping {sleep_time}s")
        time.sleep(sleep_time)
        return sleep_time

    # 6. Low → short sleep (turbo)
    sleep_time = max(10, base_sleep // 2)
    print(f"  [THROTTLE] CPU={cpu:.0f}% RAM={ram:.0f}% low — sleeping {sleep_time}s")
    time.sleep(sleep_time)
    return sleep_time


# ── Main MCTS loop ───────────────────────────────────────────────────────────

def mcts_loop(dream_id: str, brief: str) -> dict:
    """Run the full MCTS dream loop."""
    dp = dream_path(dream_id)
    dp.mkdir(parents=True, exist_ok=True)

    # Initialize
    tree = mcts_init_tree(brief)
    all_insights = []
    all_failures = []
    all_questions = []
    run_results = []

    write_json(dp / "exploration_tree.json", tree)
    write_json(dp / "insights.json", [])
    write_json(dp / "failures.json", [])
    write_json(dp / "pending_questions.json", [])
    write_json(dp / "monte_carlo_runs.json", [])
    write_json(dp / "uncertainty.json", {})

    # Initialize DreamAgent pool (cold-start avoidance)
    agent_pool = DreamAgentPool(toolsets=TOOLSETS)

    # Check for related dreams
    related = find_related_dreams(brief)
    if related:
        related_insights = incorporate_related_insights(related)
        all_insights.extend(related_insights)
        write_meta(dream_id, {"related_dreams": [r.get("dream_id") for r in related]})

    best_confidence = 0.0
    consecutive_no_progress = 0

    for iteration in range(1, MAX_ITERATIONS + 1):
        print(f"[MCTS iter {iteration}/{MAX_ITERATIONS}]")

        # Update meta
        meta = read_meta(dream_id)
        meta["iteration"] = iteration
        meta["best_confidence"] = best_confidence
        write_meta(dream_id, meta)

        # MetaRAG state (shared across all three calls)
        state = {
            "brief": brief,
            "iteration": iteration,
            "best_confidence": best_confidence,
            "insights": all_insights[-5:],
            "active_branches": len([n for n in tree["nodes"] if n.get("n_visits", 0) > 0]),
            "tree_summary": tree_summary(tree),
        }
        # MetaRAG: run all three in parallel
        alternatives = [n["approach"][:50] for n in tree["nodes"][1:6]]
        with ThreadPoolExecutor(max_workers=3) as executor:
            f_monitor = executor.submit(metarag_monitor, state)
            f_evaluate = executor.submit(metarag_evaluate, state, alternatives)
            f_plan = executor.submit(metarag_plan, state)
            wait([f_monitor, f_evaluate, f_plan], return_when=ALL_COMPLETED)
            monitor = f_monitor.result()
            eval_result = f_evaluate.result()
            plan = f_plan.result()
        del executor  # Ensure cleanup before next iteration

        if iteration > 3 and not monitor.get("productive", True):
            print(f"  [MONITOR] Not productive: {monitor.get('reason', '')}")
            concerns = monitor.get("concerns", [])
            if concerns:
                all_questions.extend(concerns)

        if iteration > 2 and not eval_result.get("stay_the_course", True):
            switch_to = eval_result.get("switch_to")
            print(f"  [EVALUATE] Switching approach: {switch_to}")

        planned_action = plan.get("action", "expand_more")
        print(f"  [PLAN] {planned_action}: {plan.get('reason', '')}")

        # MCTS SELECT: pick a node to expand
        node_to_expand = None

        # Try to go deeper from a promising node first
        promising = [n for n in tree["nodes"] if n.get("n_visits", 0) >= 2 and n.get("depth", 0) < MAX_TREE_DEPTH]
        if promising and planned_action != "wrap_up":
            promising.sort(key=lambda n: n.get("confidence", 0), reverse=True)
            node_to_expand = promising[0]["node_id"]

        if not node_to_expand:
            selected = mcts_select(tree)
            if selected:
                node_to_expand = selected
            else:
                node_to_expand = tree["current_root"]

        # EXPAND: generate branches
        parent_node = next((n for n in tree["nodes"] if n["node_id"] == node_to_expand), None)
        depth = (parent_node.get("depth", 0) + 1) if parent_node else 1

        planned_action = plan.get("action", "expand_more")
        child_ids = mcts_expand(tree, node_to_expand, brief, depth)
        if not child_ids:
            print(f"  [EXPAND] No children — wrapping up")
            planned_action = "wrap_up"

        # Get resource usage for this iteration (for tool rollout decision)
        cpu_usage, ram_usage = get_resource_usage()

        # Identify best UCB1 child for tool rollout targeting
        best_child_ucb = None
        best_ucb_score = -float("inf")
        if child_ids:
            nodes_by_id = {n["node_id"]: n for n in tree["nodes"]}
            for cid in child_ids:
                child = nodes_by_id.get(cid)
                if not child:
                    continue
                # Calculate UCB1 score (same as mcts_select)
                if child["n_visits"] == 0:
                    ucb = float("inf")
                else:
                    win_rate = child["wins"] / child["n_visits"]
                    n = child["n_visits"]
                    parent_visits = max(parent_node.get("n_visits", 1), 1) if parent_node else 1
                    var = win_rate - win_rate * win_rate
                    ucb1_term = 2.0 * _math.log(parent_visits) / n
                    tuned_cap = 1.0 / n
                    tuned_exploration = min(ucb1_term, tuned_cap + var)
                    depth_factor = 1.0 / _math.sqrt(child.get("depth", 1))
                    ci_width = child.get("ci_width", 1.0)
                    C_ADAPTIVE = 1.414 * (1.0 + 1.0 / _math.sqrt(parent_visits))
                    ci_bonus = 0.15 * ci_width * depth_factor * C_ADAPTIVE
                    ucb = win_rate + C_ADAPTIVE * _math.sqrt(tuned_exploration) + ci_bonus
                if ucb > best_ucb_score:
                    best_ucb_score = ucb
                    best_child_ucb = cid

        # ROLLOUT: run all rollouts in parallel, then backpropagate sequentially
        rollout_tasks = []
        rollout_ex = ThreadPoolExecutor(max_workers=ROLLOUTS_PER_NODE)
        for child_id in child_ids:
            child = next((n for n in tree["nodes"] if n["node_id"] == child_id), None)
            if not child:
                continue
            child_approach = {
                "approach_id": child_id,
                "label": child.get("approach", ""),
                "description": child.get("approach_desc", ""),
            }
            is_top = (child_id == best_child_ucb)
            for r in range(ROLLOUTS_PER_NODE):
                fid = rollout_ex.submit(
                    rollout, child_approach, brief, iteration,
                    agent_pool, is_top, cpu_usage, ram_usage
                )
                rollout_tasks.append((fid, child_id, child, is_top))

        # Wait for all rollouts to complete
        wait([f for f, _, _, _ in rollout_tasks], return_when=ALL_COMPLETED)
        rollout_ex.shutdown(wait=False)
        for fid, child_id, child, is_top in rollout_tasks:
            try:
                result = fid.result()
                outcome = result.get("outcome_float", 0.5)
                child["n_visits"] = child.get("n_visits", 0) + 1
                child["wins"] = child.get("wins", 0.0) + outcome
                mcts_backpropagate(tree, child_id, outcome)
                run_results.append(result)
                tier = result.get("tier", 1)
                tool_tag = " [TOOL]" if tier == 2 else ""
                print(f"  [ROLLOUT{tool_tag}] {child.get('approach', '')[:40]} → {result.get('outcome', '?')} ({outcome:.2f})")
            except Exception as e:
                print(f"  [ROLLOUT] Error: {e}")

        # Update best confidence
        for node in tree["nodes"]:
            conf = node.get("confidence", 0)
            if conf > best_confidence:
                best_confidence = conf

        # Write current state
        write_json(dp / "exploration_tree.json", tree)
        write_json(dp / "monte_carlo_runs.json", run_results)

        # Termination check
        if planned_action == "wrap_up" or best_confidence >= MIN_CONFIDENCE:
            print(f"  [TERM] Wrapping up — confidence={best_confidence:.2f}")
            break

        # Track progress
        if len(run_results) == 0:
            consecutive_no_progress += 1
        else:
            consecutive_no_progress = 0

        if consecutive_no_progress >= 3:
            print(f"  [TERM] No progress for 3 iterations — wrapping up")
            break

        # Ralph-loop time-based staleness check
        stale = detect_staleness(tree)
        if stale.get("stale"):
            print(f"  [TERM] Stale dream detected: {stale.get('reason')} — wrapping up")
            break

        print(f"  Confidence so far: {best_confidence:.2f}")
        self_throttle()

    # ── Final distillation ────────────────────────────────────────────────────
    print("[DISTILLATION] Running uncertainty-aware distillation...")
    distillation = distill_insights_n_times(tree, brief, n=DISTILLATION_RUNS)

    # Final insights
    final_insights = (distillation.get("consensus_insights", []) +
                      distillation.get("novel_insights", []))
    all_insights.extend(final_insights)

    write_json(dp / "insights.json", all_insights[:20])
    write_json(dp / "failures.json", distillation.get("failures", []))
    write_json(dp / "pending_questions.json", distillation.get("questions", []))

    uncertainty = {
        "final_confidence": best_confidence,
        "ci_width": min(n.get("ci_width", 1.0) for n in tree["nodes"] if n.get("n_visits", 0) > 0) if tree["nodes"] else 1.0,
        "n_nodes": len(tree["nodes"]),
        "n_runs": len([n for n in tree["nodes"] if n.get("n_visits", 0) > 0]),
        "distillation_runs": distillation.get("n_runs", 0),
    }
    write_json(dp / "uncertainty.json", uncertainty)

    # Update meta
    meta = read_meta(dream_id)
    meta["status"] = "done"
    meta["confidence"] = best_confidence
    meta["completed_at"] = datetime.now(GMT7).isoformat()
    meta["n_iterations"] = iteration
    write_meta(dream_id, meta)
    (dp / "status.txt").write_text("done")

    print(f"[DONE] Dream {dream_id} — confidence={best_confidence:.2f}, "
          f"insights={len(all_insights)}, failures={len(distillation.get('failures', []))}")

    return {
        "dream_id": dream_id,
        "confidence": best_confidence,
        "insights": all_insights[:10],
        "failures": distillation.get("failures", []),
        "questions": distillation.get("questions", []),
        "n_iterations": iteration,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 dream_loop_v3.py <dream_id> <brief>")
        sys.exit(1)

    dream_id = sys.argv[1]
    brief = " ".join(sys.argv[2:])

    dp = dream_path(dream_id)
    dp.mkdir(parents=True, exist_ok=True)
    write_meta(dream_id, {
        "dream_id": dream_id,
        "brief": brief,
        "status": "running",
        "started_at": time.time(),
        "started_at_human": datetime.now(GMT7).isoformat(),
        "iteration": 0,
        "confidence": 0.0,
    })
    (dp / "status.txt").write_text("running")

    result = mcts_loop(dream_id, brief)
    print(json.dumps(result, indent=2))
