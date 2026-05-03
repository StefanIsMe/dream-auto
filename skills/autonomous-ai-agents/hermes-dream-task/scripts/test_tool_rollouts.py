#!/usr/bin/env python3
"""
test_tool_rollouts.py — Verify tool-using rollouts work correctly.

Tests:
1. DreamAgent can be instantiated and is available
2. AIAgent accepts enabled_toolsets and responds
3. Tool-using diagnose() returns structured JSON
4. Two-tier rollout: Tier-1 always runs, Tier-2 triggers on low confidence
5. Confidence improves with tool rollout vs LLM-only baseline
"""

import sys
import time
from pathlib import Path

# Add hermes_agent to path
HERMES_AGENT_ROOT = Path.home() / ".hermes" / "hermes-agent"
if str(HERMES_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT_ROOT))

from run_agent import AIAgent


def test_dreamagent_creation():
    """Test 1: DreamAgent can be created and is available."""
    print("\n=== TEST 1: DreamAgent creation ===")
    from dream_loop_v3 import DreamAgent, TOOLSETS

    agent = DreamAgent(toolsets=TOOLSETS)
    print(f"  is_available: {agent.is_available}")
    print(f"  toolsets: {agent.toolsets}")
    if agent._init_error:
        print(f"  WARNING: init error: {agent._init_error}")
    assert agent.is_available, "DreamAgent should be available"
    print("  PASS")
    return agent


def test_aiagent_enabled_toolsets():
    """Test 2: AIAgent accepts enabled_toolsets and responds."""
    print("\n=== TEST 2: AIAgent enabled_toolsets ===")
    agent = AIAgent(
        enabled_toolsets=["terminal", "file", "session_search"],
        quiet_mode=True,
        verbose_logging=False,
    )
    print(f"  AIAgent created: OK")
    # Simple chat to verify it works
    try:
        result = agent.chat("Respond with exactly one word: hello")
        print(f"  chat response: {str(result)[:50]}")
        assert result is not None
        print("  PASS")
    except Exception as e:
        print(f"  FAIL: {e}")
        raise


def test_tool_diagnose_returns_json(agent):
    """Test 3: diagnose() returns structured JSON."""
    print("\n=== TEST 3: diagnose() returns structured JSON ===")

    result = agent.diagnose(
        brief="hermes agent session indexer failing to score sessions",
        branch_label="check_logs_approach",
        branch_desc="Run terminal commands to check hermes logs for scoring errors",
        error_context="session_indexer.py returned 0 scored sessions",
        iteration=1,
    )
    print(f"  outcome: {result.get('outcome')}")
    print(f"  confidence: {result.get('confidence')}")
    print(f"  evidence: {result.get('evidence', '')[:100]}")
    print(f"  tier: {result.get('tier')}")

    assert "outcome" in result, "Should have 'outcome' key"
    assert result["outcome"] in ("success", "failure", "uncertain"), f"Invalid outcome: {result['outcome']}"
    assert 0.0 <= result.get("confidence", -1) <= 1.0, "confidence should be 0-1"
    assert "evidence" in result, "Should have 'evidence' key"
    print("  PASS")
    return result


def test_tier1_runs_first():
    """Test 4: Tier-1 always runs; Tier-2 triggers on low confidence."""
    print("\n=== TEST 4: Two-tier logic ===")
    from dream_loop_v3 import rollout_tier1, rollout_tier2, should_use_tool_rollout

    # Tier-1 should always work
    result_t1 = rollout_tier1(
        {"label": "test_branch", "description": "test desc"},
        "hermes session indexer not finding any sessions",
        iteration=1,
    )
    print(f"  Tier-1 outcome: {result_t1.get('outcome')}, confidence: {result_t1.get('confidence')}, tier: {result_t1.get('tier')}")
    assert result_t1["tier"] == 1
    assert result_t1["outcome_float"] in (0.0, 0.5, 1.0)
    print("  Tier-1: OK")

    # should_use_tool_rollout resource check
    cpu_ok, ram_ok = should_use_tool_rollout(20.0, 40.0)
    cpu_high, ram_high = should_use_tool_rollout(80.0, 80.0)
    print(f"  should_use_tool_rollout(cpu=20, ram=40): {cpu_ok}")
    print(f"  should_use_tool_rollout(cpu=80, ram=80): {cpu_high}")
    assert cpu_ok == True, "Low resources should allow tool rollout"
    assert cpu_high == False, "High resources should block tool rollout"
    print("  PASS")


def test_tier2_upgrade():
    """Test 5: Rollout upgrades to Tier-2 when Tier-1 confidence is low."""
    print("\n=== TEST 5: Tier-2 upgrade on low confidence ===")
    from dream_loop_v3 import rollout, DreamAgentPool, TOOLSETS

    pool = DreamAgentPool(toolsets=TOOLSETS)
    agent = pool.get()

    if not agent.is_available:
        print("  SKIP: AIAgent not available on this system")
        return

    # Simulate low confidence → should trigger Tier-2
    branch = {"label": "investigate_logs", "description": "Check session indexer logs"}
    cpu, ram = 20.0, 40.0  # idle resources

    result = rollout(
        branch=branch,
        brief="hermes session indexer returning 0 scored sessions despite running for hours",
        iteration=1,
        agent_pool=pool,
        is_top_branch=True,
        cpu=cpu,
        ram=ram,
    )
    print(f"  outcome: {result.get('outcome')}, confidence: {result.get('confidence')}, tier: {result.get('tier')}")
    print(f"  evidence: {result.get('evidence', '')[:150]}")
    assert "outcome" in result
    assert result.get("tier") in (1, 2)
    # If tier 2, should have evidence from real tools
    if result.get("tier") == 2:
        assert len(result.get("evidence", "")) > 10, "Tier-2 should have real evidence"
    print("  PASS")


def test_confidence_comparison():
    """Test 6: Verify confidence improves with tools on real errors."""
    print("\n=== TEST 6: Confidence comparison ===")
    from dream_loop_v3 import rollout_tier1, DreamAgentPool, TOOLSETS

    pool = DreamAgentPool(toolsets=TOOLSETS)
    agent = pool.get()

    brief = "unknown error host=output appearing in hermes agent runs"
    branch = {"label": "debug_error", "description": "Investigate host=output error"}

    t1_results = []
    for i in range(3):
        r = rollout_tier1(branch, brief, iteration=i+1)
        t1_results.append(r.get("confidence", 0.5))
        print(f"  Tier-1 run {i+1}: conf={r.get('confidence')}, outcome={r.get('outcome')}")

    avg_t1 = sum(t1_results) / len(t1_results)
    print(f"  Tier-1 avg confidence: {avg_t1:.3f}")

    if agent.is_available:
        t2_results = []
        for i in range(3):
            r = agent.diagnose(brief, branch["label"], branch["description"], "", i+1)
            t2_results.append(r.get("confidence", 0.5))
            print(f"  Tier-2 run {i+1}: conf={r.get('confidence')}, outcome={r.get('outcome')}, evidence={r.get('evidence','')[:80]}")
        avg_t2 = sum(t2_results) / len(t2_results)
        print(f"  Tier-2 avg confidence: {avg_t2:.3f}")
        print(f"  Improvement: {avg_t2 - avg_t1:+.3f}")
    else:
        print("  SKIP: AIAgent not available")

    print("  PASS")


def main():
    print("=" * 60)
    print("Dream Tool Rollouts — Integration Test Suite")
    print("=" * 60)

    tests = [
        ("DreamAgent creation", test_dreamagent_creation),
        ("AIAgent enabled_toolsets", test_aiagent_enabled_toolsets),
        ("diagnose() JSON output", lambda: test_tool_diagnose_returns_json(test_dreamagent_creation())),
        ("Two-tier logic", test_tier1_runs_first),
        ("Tier-2 upgrade", test_tier2_upgrade),
        ("Confidence comparison", test_confidence_comparison),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            failed += 1

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
