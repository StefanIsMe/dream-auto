#!/usr/bin/env python3
"""
dream_loop_v2.py — Research-backed background thinking loop.

Architecture:
  - Tree-structured exploration (ToT/GoT inspired)
  - Generate-verify loop with external quality gate (arXiv:2505.11807)
  - Adaptive termination with plateau detection
  - Structured episodic memory: insights + failures + open questions
  - Direct API calls (no hermes chat -q dependency)

Usage:
    python3 dream_loop_v2.py <dream_id> "<brief>"

State: ~/.hermes/state/dream/<dream_id>/
    meta.json              — dream metadata, status, confidence, lifecycle
    exploration_tree.json  — tree of reasoning nodes
    insights.json          — distilled key findings
    failures.json          — failure patterns to remember
    pending_questions.json — open questions / uncertainty
    brief.json             — legacy compat
    status.txt             — legacy compat
    iterations.json        — legacy compat
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DREAM_DIR = Path.home() / ".hermes" / "state" / "dream"
CIRCUIT_BREAKER_PATH = DREAM_DIR / "circuit_breaker.json"

MAX_ITERATIONS = 10          # Max reasoning iterations
SLEEP_SECONDS = 120          # 2 minutes between iterations
MIN_CONFIDENCE = 0.75        # Stop when confidence >= 75%
MAX_TREE_DEPTH = 4           # Max exploration depth
MAX_CHILDREN_PER_NODE = 3    # Max branches per node
PLATEAU_WINDOW = 3           # Detect plateau over last N confidence values
PLATEAU_THRESHOLD = 0.05     # Max spread to count as plateau
MAX_CONSECUTIVE_FAILURES = 3 # Abort after N consecutive LLM failures

GMT7 = timezone(timedelta(hours=7))


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

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


def read_meta(dream_id: str) -> dict:
    return read_json(dream_path(dream_id) / "meta.json", default={})


def write_meta(dream_id: str, meta: dict):
    write_json(dream_path(dream_id) / "meta.json", meta)


def read_tree(dream_id: str) -> dict:
    return read_json(dream_path(dream_id) / "exploration_tree.json", default={"nodes": [], "current_path": []})


def write_tree(dream_id: str, tree: dict):
    write_json(dream_path(dream_id) / "exploration_tree.json", tree)


def read_insights(dream_id: str) -> List[str]:
    return read_json(dream_path(dream_id) / "insights.json", default=[])


def write_insights(dream_id: str, insights: List[str]):
    write_json(dream_path(dream_id) / "insights.json", insights)


def read_failures(dream_id: str) -> List[str]:
    return read_json(dream_path(dream_id) / "failures.json", default=[])


def write_failures(dream_id: str, failures: List[str]):
    write_json(dream_path(dream_id) / "failures.json", failures)


def read_questions(dream_id: str) -> List[str]:
    return read_json(dream_path(dream_id) / "pending_questions.json", default=[])


def write_questions(dream_id: str, questions: List[str]):
    write_json(dream_path(dream_id) / "pending_questions.json", questions)




def is_wake_signaled(dream_id: str) -> bool:
    return (dream_path(dream_id) / "wake.txt").exists()


def clear_wake_signal(dream_id: str):
    wp = dream_path(dream_id) / "wake.txt"
    if wp.exists():
        wp.unlink()


# ---------------------------------------------------------------------------
# Direct LLM API (replaces hermes chat -q)
# ---------------------------------------------------------------------------

def _load_api_config() -> str:
    """Load hermes venv path for AIAgent calls."""
    venv_python = str(Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3")
    if Path(venv_python).exists():
        return venv_python
    return sys.executable  # Fallback to system python


_HERMES_VENV = _load_api_config()
_consecutive_llm_failures = 0
_agent = None  # Lazy-initialized AIAgent singleton


def _get_agent():
    """Get or create the shared AIAgent instance."""
    global _agent
    if _agent is None:
        sys.path.insert(0, str(Path.home() / ".hermes" / "hermes-agent"))
        from run_agent import AIAgent
        _agent = AIAgent(
            model="xiaomi/mimo-v2-pro",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True
        )
        print("[dream] AIAgent initialized")
    return _agent


def llm_call(prompt: str, timeout: int = 60) -> str:
    """LLM call via hermes AIAgent class (reuses agent instance)."""
    global _consecutive_llm_failures

    # Adaptive backoff: increase timeout on consecutive failures
    actual_timeout = min(timeout * (2 ** _consecutive_llm_failures), 180)

    try:
        agent = _get_agent()
        response = agent.chat(prompt)
        if response and response.strip():
            _consecutive_llm_failures = 0
            return response.strip()
        else:
            _consecutive_llm_failures += 1
            print(f"[dream] LLM empty response (failures: {_consecutive_llm_failures})")
            if _consecutive_llm_failures >= MAX_CONSECUTIVE_FAILURES:
                raise RuntimeError(f"LLM failed {_consecutive_llm_failures} consecutive times — aborting")
            return ""
    except Exception as e:
        _consecutive_llm_failures += 1
        print(f"[dream] LLM error: {e} (failures: {_consecutive_llm_failures})")
        if _consecutive_llm_failures >= MAX_CONSECUTIVE_FAILURES:
            raise RuntimeError(f"LLM failed {_consecutive_llm_failures} consecutive times — aborting")
        time.sleep(30 * _consecutive_llm_failures)
        return ""


def health_check() -> bool:
    """Test LLM API before starting dream loop."""
    response = llm_call("Reply with exactly: HEALTHY", timeout=45)
    return "HEALTHY" in response.upper()


# ---------------------------------------------------------------------------
# Brief cleaning (P2a)
# ---------------------------------------------------------------------------

def clean_brief(raw_brief: str) -> str:
    """Remove system instructions, extract core topic."""
    brief = raw_brief
    # Strip common system prefixes from cron prompts
    for marker in ["DELIVERY:", "SILENT:", "SYSTEM:", "Your final response will be"]:
        if marker in brief:
            brief = brief.split(marker)[0].strip()
    # Remove "Explore and think deeply about:" prefix
    for prefix in ["Explore and think deeply about:", "Explore and think deeply about"]:
        if brief.startswith(prefix):
            brief = brief[len(prefix):].strip()
    # If still long and messy, try to distill
    if len(brief) > 300:
        distilled = llm_call(
            f"Extract the core problem/topic in 1-2 sentences from this text. Reply with ONLY the topic:\n{brief[:500]}",
            timeout=20
        )
        if distilled and len(distilled) > 10:
            return distilled[:300]
    return brief[:300] if brief else "Think about this problem."


# ---------------------------------------------------------------------------
# Cross-dream learning (P3.5) — arXiv:2303.11366 (Reflexion episodic memory)
# ---------------------------------------------------------------------------

def find_related_dream_insights(new_brief: str, max_dreams: int = 5) -> List[str]:
    """Search completed dreams for relevant insights to seed new dream."""
    related_insights = []

    # Collect completed dreams with insights
    if not DREAM_DIR.exists():
        return related_insights

    completed_dreams = []
    for dream_dir in sorted(DREAM_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not dream_dir.is_dir() or dream_dir.name in ("logs",) or len(dream_dir.name) < 5:
            continue
        meta_path = dream_dir / "meta.json"
        insights_path = dream_dir / "insights.json"
        if not meta_path.exists() or not insights_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
            insights = json.loads(insights_path.read_text())
            if meta.get("status", "").startswith("completed") and insights:
                brief_cleaned = meta.get("brief_cleaned", meta.get("brief", ""))[:200]
                completed_dreams.append({
                    "id": dream_dir.name,
                    "brief": brief_cleaned,
                    "insights": insights,
                    "completed_at": meta.get("ended_at", ""),
                })
        except Exception:
            continue

    if not completed_dreams:
        return related_insights

    # Use LLM to find related dreams (not hardcoded keywords)
    dreams_summary = json.dumps([
        {"id": d["id"], "topic": d["brief"], "count": len(d["insights"])}
        for d in completed_dreams[:max_dreams * 2]
    ])

    prompt = f"""Given this new topic: "{new_brief}"

Which of these past thinking sessions are relevant? Return ONLY a JSON array of dream IDs, or empty array [].
{dreams_summary}"""

    response = llm_call(prompt, timeout=30)
    try:
        related_ids = json.loads(response) if response else []
        if not isinstance(related_ids, list):
            related_ids = []
    except Exception:
        related_ids = []

    # Collect insights from related dreams
    for dream in completed_dreams:
        if dream["id"] in related_ids:
            for insight in dream["insights"][:3]:  # Top 3 from each
                related_insights.append(f"[from {dream['brief'][:50]}] {insight}")

    return related_insights[:5]  # Max 5 total


# ---------------------------------------------------------------------------
# Plateau detection (P2b)
# ---------------------------------------------------------------------------

def is_confidence_plateau(history: List[float], window: int = PLATEAU_WINDOW,
                          threshold: float = PLATEAU_THRESHOLD) -> bool:
    """Detect if confidence has plateaued (flat for N iterations)."""
    if len(history) < window:
        return False
    recent = history[-window:]
    return (max(recent) - min(recent)) < threshold


# ---------------------------------------------------------------------------
# External quality gate (P2c) — arXiv:2505.11807
# ---------------------------------------------------------------------------

def external_quality_gate(dream_id: str, prev_state: dict) -> dict:
    """Score dream progress using objective metrics, not LLM self-eval."""
    tree = read_tree(dream_id)
    insights = read_insights(dream_id)

    def count_nodes(nodes):
        return sum(1 + count_nodes(n.get("children", [])) for n in nodes)

    node_count = count_nodes(tree.get("nodes", []))

    metrics = {
        "insight_count": len(insights),
        "tree_branches": node_count,
        "insight_delta": len(insights) - prev_state.get("insight_count", 0),
        "branch_delta": node_count - prev_state.get("node_count", 0),
    }

    has_progress = metrics["insight_delta"] > 0 or metrics["branch_delta"] > 0

    return {
        "has_progress": has_progress,
        "metrics": metrics,
        "should_continue": has_progress or (node_count < 3),
    }


# ---------------------------------------------------------------------------
# Circuit breaker (P1) — persistent state
# ---------------------------------------------------------------------------

def read_circuit_breaker() -> dict:
    if CIRCUIT_BREAKER_PATH.exists():
        try:
            return json.loads(CIRCUIT_BREAKER_PATH.read_text())
        except Exception:
            pass
    return {"consecutive_failures": 0, "disabled_until": None, "total_failures": 0, "total_successes": 0}


def write_circuit_breaker(state: dict):
    CIRCUIT_BREAKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    CIRCUIT_BREAKER_PATH.write_text(json.dumps(state, indent=2))


def circuit_breaker_is_open() -> bool:
    """Check if circuit breaker prevents new dreams."""
    state = read_circuit_breaker()
    if state.get("disabled_until"):
        try:
            disabled_until = datetime.fromisoformat(state["disabled_until"])
            if datetime.now(GMT7) < disabled_until:
                return True
        except Exception:
            pass
    return False


def record_dream_outcome(success: bool):
    """Update circuit breaker after dream completes."""
    state = read_circuit_breaker()
    if success:
        state["consecutive_failures"] = 0
        state["disabled_until"] = None
        state["total_successes"] = state.get("total_successes", 0) + 1
    else:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        state["total_failures"] = state.get("total_failures", 0) + 1
        if state["consecutive_failures"] >= 3:
            disabled_until = datetime.now(GMT7) + timedelta(hours=1)
            state["disabled_until"] = disabled_until.isoformat()
            print(f"[dream] CIRCUIT BREAKER OPEN — disabled until {disabled_until.strftime('%H:%M')}")
    write_circuit_breaker(state)


# ---------------------------------------------------------------------------
# Lifecycle tracking (P3)
# ---------------------------------------------------------------------------

def set_status(dream_id: str, status: str, extra: dict = None):
    """Update dream status with full lifecycle tracking."""
    meta = read_meta(dream_id)
    meta["status"] = status
    now = datetime.now(GMT7)

    if status in ("completed_success", "completed_empty", "failed",
                   "health_check_failed", "circuit_breaker", "paused"):
        meta["ended_at"] = now.isoformat()
        if meta.get("started_at"):
            try:
                started = datetime.fromisoformat(meta["started_at"])
                meta["duration_seconds"] = int((now - started).total_seconds())
            except Exception:
                pass

    if extra:
        meta.update(extra)

    write_meta(dream_id, meta)
    # Legacy compat
    (dream_path(dream_id) / "status.txt").write_text(status)


def extract_json(text: str, default=None):
    """Extract JSON object from LLM response."""
    if not text:
        return default
    match = re.search(r'\{[^}]+\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    # Try array
    match = re.search(r'\[[^\]]+\]', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    return default


# ---------------------------------------------------------------------------
# Exploration Tree Operations
# ---------------------------------------------------------------------------

def get_current_node(tree: dict) -> Optional[dict]:
    """Get the node at the current exploration path."""
    path = tree.get("current_path", [])
    if not path:
        return None

    nodes = tree.get("nodes", [])
    current = None
    for node_id in path:
        found = None
        for n in (nodes if current is None else current.get("children", [])):
            if n.get("id") == node_id:
                found = n
                break
        if found is None:
            return None
        current = found
    return current


def get_path_context(tree: dict) -> str:
    """Get text summary of the current exploration path."""
    path = tree.get("current_path", [])
    if not path:
        return "(root — starting exploration)"

    nodes = tree.get("nodes", [])
    context_parts = []
    current_list = nodes

    for node_id in path:
        for n in current_list:
            if n.get("id") == node_id:
                context_parts.append(f"[confidence={n.get('confidence', 0):.0%}] {n.get('thought', '')[:200]}")
                current_list = n.get("children", [])
                break

    return " → ".join(context_parts) if context_parts else "(empty path)"


def add_node(tree: dict, thought: str, confidence: float, evaluation: str,
             parent_path: List[str] = None) -> str:
    """Add a node to the exploration tree. Returns the new node ID."""
    import uuid
    node_id = f"n{uuid.uuid4().hex[:6]}"

    node = {
        "id": node_id,
        "thought": thought,
        "confidence": confidence,
        "evaluation": evaluation,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "children": [],
    }

    if parent_path is None:
        parent_path = tree.get("current_path", [])

    if not parent_path:
        # Root level
        tree["nodes"].append(node)
    else:
        # Find parent and add as child
        current_list = tree["nodes"]
        for pid in parent_path:
            found = None
            for n in current_list:
                if n.get("id") == pid:
                    found = n
                    break
            if found is None:
                # Parent not found, add at root
                tree["nodes"].append(node)
                return node_id
            current_list = found.get("children", [])
        # current_list is now parent's children list — but we need to modify the parent
        # Re-traverse to get the actual parent reference
        current_list = tree["nodes"]
        for pid in parent_path[:-1]:
            for n in current_list:
                if n.get("id") == pid:
                    current_list = n.get("children", [])
                    break
        for n in current_list:
            if n.get("id") == parent_path[-1]:
                n.setdefault("children", []).append(node)
                break

    return node_id


def backtrack(tree: dict) -> bool:
    """Backtrack one level in the exploration tree. Returns True if successful."""
    path = tree.get("current_path", [])
    if len(path) <= 1:
        # Can't backtrack from root — explore alternative at root level
        tree["current_path"] = []
        return True
    tree["current_path"] = path[:-1]
    return True


def advance(tree: dict, node_id: str):
    """Advance the current path to include a child node."""
    tree.setdefault("current_path", []).append(node_id)


def get_unexplored_siblings(tree: dict) -> List[dict]:
    """Get sibling nodes that haven't been explored yet."""
    path = tree.get("current_path", [])
    if not path:
        return tree.get("nodes", [])

    # Navigate to parent
    nodes = tree["nodes"]
    for pid in path[:-1]:
        for n in nodes:
            if n.get("id") == pid:
                nodes = n.get("children", [])
                break

    # Return children of the parent (siblings of current)
    for n in nodes:
        if n.get("id") == path[-1]:
            return n.get("children", [])

    return []


# ---------------------------------------------------------------------------
# Core Thinking Loop
# ---------------------------------------------------------------------------

def generate_thought(dream_id: str, brief: str, iteration: int) -> dict:
    """Generate a new thought for the current exploration path."""
    tree = read_tree(dream_id)
    insights = read_insights(dream_id)
    failures = read_failures(dream_id)
    questions = read_questions(dream_id)

    path_context = get_path_context(tree)

    # Build context from previous work
    context_parts = []
    if insights:
        context_parts.append(f"KEY INSIGHTS SO FAR:\n" + "\n".join(f"  - {i}" for i in insights[-5:]))
    if failures:
        context_parts.append(f"FAILED APPROACHES:\n" + "\n".join(f"  - {f}" for f in failures[-3:]))
    if questions:
        context_parts.append(f"OPEN QUESTIONS:\n" + "\n".join(f"  - {q}" for q in questions[-3:]))

    context = "\n\n".join(context_parts) if context_parts else "(no previous work)"

    prompt = f"""You are exploring this problem through structured thinking:

BRIEF: {brief}

CURRENT EXPLORATION PATH: {path_context}

PREVIOUS WORK:
{context}

ITERATION: {iteration}/{MAX_ITERATIONS}

Generate ONE new thought that advances understanding. This should:
- NOT repeat what's already been established
- Explore a new angle, approach, or connection
- Be specific and actionable (not vague)
- Consider what might be wrong with current thinking

Respond with ONLY a JSON object:
{{"thought": "<your new thinking — 2-4 sentences>", "approach": "<what angle you're taking>"}}
"""
    response = llm_call(prompt, timeout=45)
    result = extract_json(response, default={"thought": response[:500] if response else "No response", "approach": "unknown"})

    return result


def evaluate_thought(dream_id: str, brief: str, thought: dict) -> dict:
    """Evaluate a thought with metacognitive monitoring."""
    tree = read_tree(dream_id)
    insights = read_insights(dream_id)

    prompt = f"""You are evaluating the quality of this thinking about a problem.

PROBLEM: {brief}

THOUGHT TO EVALUATE: {thought.get('thought', '')}
APPROACH: {thought.get('approach', '')}

EXISTING INSIGHTS: {json.dumps(insights[-5:]) if insights else 'none'}

Evaluate this thought with metacognitive awareness:
1. Is this genuinely new or does it repeat existing insights?
2. How confident are you in this direction (0.0 to 1.0)?
3. What specifically does this thought reveal?
4. What are the weaknesses or gaps in this reasoning?
5. Should we go deeper here or explore alternatives?

Respond with ONLY a JSON object:
{{"confidence": <0.0-1.0>, "is_novel": <true/false>, "key_revelation": "<what this reveals>", "weakness": "<main weakness>", "recommendation": "<go_deeper|explore_alternatives|distill_and_stop>"}}
"""
    response = llm_call(prompt, timeout=45)
    result = extract_json(response, default={
        "confidence": 0.0,
        "is_novel": False,
        "key_revelation": "",
        "weakness": "evaluation failed (LLM returned no response)",
        "recommendation": "explore_alternatives"
    })

    return result


def distill_insights(dream_id: str, brief: str) -> List[str]:
    """Distill the entire exploration into key insights."""
    tree = read_tree(dream_id)
    old_insights = read_insights(dream_id)
    failures = read_failures(dream_id)

    # Gather all node thoughts
    all_thoughts = []
    def collect_thoughts(nodes):
        for n in nodes:
            all_thoughts.append(f"[conf={n.get('confidence', 0):.0%}] {n.get('thought', '')}")
            collect_thoughts(n.get("children", []))
    collect_thoughts(tree.get("nodes", []))

    prompt = f"""You are distilling a deep thinking session into key insights.

PROBLEM: {brief}

ALL THINKING GENERATED:
{json.dumps(all_thoughts[-20:])}

EXISTING INSIGHTS: {json.dumps(old_insights) if old_insights else 'none'}
FAILED APPROACHES: {json.dumps(failures) if failures else 'none'}

Distill this into 3-5 NEW key insights that would be most useful to someone answering this problem.
Each insight should be:
- Specific and actionable (not vague)
- Novel (don't repeat existing insights)
- Concise (1-2 sentences max)

Respond with ONLY a JSON array of strings:
["insight 1", "insight 2", "insight 3"]
"""
    response = llm_call(prompt, timeout=45)
    result = extract_json(response, default=[])

    if isinstance(result, list):
        return [str(i) for i in result[:5]]
    return []


def extract_failures(dream_id: str, brief: str) -> List[str]:
    """Extract failure patterns from low-confidence nodes."""
    tree = read_tree(dream_id)

    # Find low-confidence nodes
    failures = []
    def collect_failures(nodes):
        for n in nodes:
            if n.get("confidence", 0) < 0.3:
                failures.append(f"{n.get('thought', '')[:100]} (conf={n.get('confidence', 0):.0%})")
            collect_failures(n.get("children", []))
    collect_failures(tree.get("nodes", []))

    if not failures:
        return []

    prompt = f"""These thinking paths had low confidence — extract the failure pattern:

FAILED THINKING:
{json.dumps(failures[:10])}

What went wrong? Extract 1-2 concise failure patterns to remember.

Respond with ONLY a JSON array:
["failure pattern 1", "failure pattern 2"]
"""
    response = llm_call(prompt, timeout=30)
    result = extract_json(response, default=[])

    if isinstance(result, list):
        return [str(f) for f in result[:3]]
    return []


def extract_questions(dream_id: str, brief: str) -> List[str]:
    """Extract open questions / uncertainty from the exploration."""
    tree = read_tree(dream_id)
    insights = read_insights(dream_id)

    prompt = f"""Based on this exploration, what questions remain unanswered?

PROBLEM: {brief}

INSIGHTS FOUND: {json.dumps(insights[-5:]) if insights else 'none'}

Generate 1-3 specific open questions that would help resolve remaining uncertainty.

Respond with ONLY a JSON array:
["question 1", "question 2"]
"""
    response = llm_call(prompt, timeout=30)
    result = extract_json(response, default=[])

    if isinstance(result, list):
        return [str(q) for q in result[:3]]
    return []


# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 dream_loop_v2.py <dream_id> [brief]")
        sys.exit(1)

    dream_id = sys.argv[1]
    raw_brief = sys.argv[2] if len(sys.argv) > 2 else "Think about this problem."

    dp = dream_path(dream_id)
    dp.mkdir(parents=True, exist_ok=True)

    # --- P1: Check circuit breaker ---
    if circuit_breaker_is_open():
        state = read_circuit_breaker()
        print(f"[dream {dream_id}] CIRCUIT BREAKER OPEN — skipping dream")
        # Write a minimal status so plugin doesn't keep retrying
        meta = {
            "dream_id": dream_id, "brief": raw_brief, "status": "circuit_breaker",
            "started_at": datetime.now(GMT7).isoformat(),
            "ended_at": datetime.now(GMT7).isoformat(),
            "iteration": 0, "confidence": 0.0,
            "failure_reason": f"circuit_breaker_open (consecutive_failures={state.get('consecutive_failures', 0)})",
        }
        write_meta(dream_id, meta)
        (dp / "status.txt").write_text("circuit_breaker")
        return

    # --- P0: Health check ---
    print(f"[dream {dream_id}] Testing LLM API...")
    if not health_check():
        print(f"[dream {dream_id}] HEALTH CHECK FAILED — LLM API unreachable")
        meta = {
            "dream_id": dream_id, "brief": raw_brief, "status": "health_check_failed",
            "started_at": datetime.now(GMT7).isoformat(),
            "ended_at": datetime.now(GMT7).isoformat(),
            "iteration": 0, "confidence": 0.0,
            "failure_reason": "llm_api_unreachable",
        }
        write_meta(dream_id, meta)
        (dp / "status.txt").write_text("health_check_failed")
        record_dream_outcome(success=False)
        return

    # --- P2a: Clean brief ---
    brief = clean_brief(raw_brief)
    print(f"[dream {dream_id}] Cleaned brief: {brief[:120]}...")

    # --- P3.5: Cross-dream learning ---
    related_insights = find_related_dream_insights(brief)
    if related_insights:
        print(f"[dream {dream_id}] Found {len(related_insights)} related insights from past dreams")
        brief = f"{brief}\n\nPREVIOUS RELATED THINKING:\n" + "\n".join(f"- {i}" for i in related_insights)
    else:
        print(f"[dream {dream_id}] No related past dreams found")

    # Initialize if needed
    meta = read_meta(dream_id)
    if not meta:
        meta = {
            "dream_id": dream_id,
            "brief_raw": raw_brief[:500],
            "brief_cleaned": brief,
            "status": "running",
            "started_at": datetime.now(GMT7).isoformat(),
            "iteration": 0,
            "confidence": 0.0,
            "insight_count": 0,
        }
        write_meta(dream_id, meta)

    tree = read_tree(dream_id)
    if not tree.get("nodes"):
        tree = {"nodes": [], "current_path": []}
        write_tree(dream_id, tree)

    set_status(dream_id, "running")

    print(f"[dream {dream_id}] Starting structured exploration...")
    print(f"[dream {dream_id}] Brief: {brief[:100]}...")

    iteration = meta.get("iteration", 0)
    max_confidence = meta.get("confidence", 0.0)
    confidence_history = []
    quality_gate_state = {"insight_count": 0, "node_count": 0}
    no_progress_streak = 0

    while iteration < MAX_ITERATIONS:
        # Check for wake signal
        if is_wake_signaled(dream_id):
            clear_wake_signal(dream_id)
            set_status(dream_id, "waiting")
            print(f"[dream {dream_id}] Woken — waiting for continue signal")
            while not is_wake_signaled(dream_id):
                time.sleep(10)
            clear_wake_signal(dream_id)
            set_status(dream_id, "running")
            print(f"[dream {dream_id}] Continuing...")

        iteration += 1
        print(f"[dream {dream_id}] Iteration {iteration}/{MAX_ITERATIONS} (best conf: {max_confidence:.0%})")

        try:
            # --- GENERATE ---
            thought = generate_thought(dream_id, brief, iteration)
            thought_text = thought.get("thought", "")
            if not thought_text or len(thought_text) < 10:
                print(f"[dream {dream_id}] Empty thought — stopping")
                break

            # --- EVALUATE (metacognitive monitoring) ---
            evaluation = evaluate_thought(dream_id, brief, thought)
            confidence = float(evaluation.get("confidence", 0.0))
            is_novel = evaluation.get("is_novel", False)
            recommendation = evaluation.get("recommendation", "explore_alternatives")

            print(f"[dream {dream_id}] Thought: {thought_text[:80]}...")
            print(f"[dream {dream_id}] Confidence: {confidence:.0%} | Novel: {is_novel} | Rec: {recommendation}")

            # --- ADD TO TREE ---
            tree = read_tree(dream_id)
            node_id = add_node(
                tree,
                thought=thought_text,
                confidence=confidence,
                evaluation=json.dumps(evaluation),
                parent_path=tree.get("current_path", None),
            )

            # --- TRACK FAILURES ---
            if confidence < 0.3:
                failures = read_failures(dream_id)
                failures.append(f"{thought_text[:100]} — {evaluation.get('weakness', 'low confidence')}")
                failures = failures[-10:]
                write_failures(dream_id, failures)

            # --- P2b: PLATEAU DETECTION ---
            confidence_history.append(confidence)
            if is_confidence_plateau(confidence_history) and max_confidence < MIN_CONFIDENCE:
                print(f"[dream {dream_id}] CONFIDENCE PLATEAU DETECTED — stopping")
                recommendation = "distill_and_stop"

            # --- P2c: EXTERNAL QUALITY GATE ---
            gate_result = external_quality_gate(dream_id, quality_gate_state)
            if not gate_result["should_continue"]:
                no_progress_streak += 1
                print(f"[dream {dream_id}] No progress streak: {no_progress_streak} (gate: {gate_result['metrics']})")
                if no_progress_streak >= 3:
                    print(f"[dream {dream_id}] 3 iterations with no progress — stopping")
                    recommendation = "distill_and_stop"
            else:
                no_progress_streak = 0
            quality_gate_state = gate_result["metrics"]

            # --- UPDATE PATH BASED ON RECOMMENDATION ---
            if recommendation == "go_deeper" and confidence >= 0.4:
                advance(tree, node_id)
            elif recommendation == "explore_alternatives":
                backtrack(tree)
            elif recommendation == "distill_and_stop":
                write_tree(dream_id, tree)
                print(f"[dream {dream_id}] Recommendation: distill and stop")

                # Distill insights
                new_insights = distill_insights(dream_id, brief)
                if new_insights:
                    existing = read_insights(dream_id)
                    existing.extend(new_insights)
                    existing = existing[-20:]
                    write_insights(dream_id, existing)
                    print(f"[dream {dream_id}] Distilled {len(new_insights)} insights")

                # Extract failure patterns
                new_failures = extract_failures(dream_id, brief)
                if new_failures:
                    existing = read_failures(dream_id)
                    existing.extend(new_failures)
                    existing = existing[-10:]
                    write_failures(dream_id, existing)

                # Extract open questions
                new_questions = extract_questions(dream_id, brief)
                if new_questions:
                    existing = read_questions(dream_id)
                    existing.extend(new_questions)
                    existing = existing[-10:]
                    write_questions(dream_id, existing)

                all_insights = read_insights(dream_id)
                has_content = len(all_insights) > 0
                set_status(dream_id, "completed_success" if has_content else "completed_empty",
                           extra={"iteration": iteration, "confidence": confidence,
                                  "insight_count": len(all_insights)})
                record_dream_outcome(success=has_content)
                print(f"[dream {dream_id}] Completed after {iteration} iterations (conf={confidence:.0%}, insights={len(all_insights)})")
                return

            write_tree(dream_id, tree)

            # --- TRACK MAX CONFIDENCE ---
            if confidence > max_confidence:
                max_confidence = confidence

            # --- ADAPTIVE TERMINATION ---
            if max_confidence >= MIN_CONFIDENCE:
                print(f"[dream {dream_id}] Confidence threshold met ({max_confidence:.0%} >= {MIN_CONFIDENCE:.0%})")

                new_insights = distill_insights(dream_id, brief)
                if new_insights:
                    existing = read_insights(dream_id)
                    existing.extend(new_insights)
                    existing = existing[-20:]
                    write_insights(dream_id, existing)
                    print(f"[dream {dream_id}] Final distillation: {len(new_insights)} insights")

                all_insights = read_insights(dream_id)
                set_status(dream_id, "completed_success",
                           extra={"iteration": iteration, "confidence": max_confidence,
                                  "insight_count": len(all_insights)})
                record_dream_outcome(success=True)
                print(f"[dream {dream_id}] Completed after {iteration} iterations")
                return

            # --- NON-NOVEL THOUGHT → BACKTRACK ---
            if not is_novel:
                print(f"[dream {dream_id}] Non-novel thought — backtracking")
                backtrack(tree)
                write_tree(dream_id, tree)

            # --- UPDATE METADATA ---
            meta["iteration"] = iteration
            meta["confidence"] = max_confidence
            write_meta(dream_id, meta)

            # --- UPDATE LEGACY FILES ---
            iter_path = dp / "iterations.json"
            iterations = read_json(iter_path, default=[])
            iterations.append({
                "iteration": iteration,
                "timestamp": datetime.now(GMT7).strftime("%Y-%m-%d %H:%M:%S"),
                "confidence": confidence,
                "thought_preview": thought_text[:100],
            })
            write_json(iter_path, iterations)

            # --- SLEEP ---
            if iteration < MAX_ITERATIONS:
                time.sleep(SLEEP_SECONDS)

        except KeyboardInterrupt:
            print(f"[dream {dream_id}] Interrupted — saving state")
            meta["iteration"] = iteration
            meta["confidence"] = max_confidence
            write_meta(dream_id, meta)
            set_status(dream_id, "paused")
            return
        except RuntimeError as e:
            # LLM abort (consecutive failures)
            print(f"[dream {dream_id}] ABORTED: {e}")
            all_insights = read_insights(dream_id)
            set_status(dream_id, "failed",
                       extra={"iteration": iteration, "confidence": max_confidence,
                              "insight_count": len(all_insights),
                              "failure_reason": str(e)})
            record_dream_outcome(success=False)
            return
        except Exception as e:
            print(f"[dream {dream_id}] Error: {e} — retrying in 60s")
            time.sleep(60)

    # Max iterations reached — final distillation
    print(f"[dream {dream_id}] Max iterations reached ({MAX_ITERATIONS})")

    new_insights = distill_insights(dream_id, brief)
    if new_insights:
        existing = read_insights(dream_id)
        existing.extend(new_insights)
        existing = existing[-20:]
        write_insights(dream_id, existing)
        print(f"[dream {dream_id}] Final distillation: {len(new_insights)} insights")

    all_insights = read_insights(dream_id)
    has_content = len(all_insights) > 0
    set_status(dream_id, "completed_success" if has_content else "completed_empty",
               extra={"iteration": iteration, "confidence": max_confidence,
                      "insight_count": len(all_insights)})
    record_dream_outcome(success=has_content)
    print(f"[dream {dream_id}] Completed after {iteration} iterations (best conf: {max_confidence:.0%}, insights={len(all_insights)})")


if __name__ == "__main__":
    main()
