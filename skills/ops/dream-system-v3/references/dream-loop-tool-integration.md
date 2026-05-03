# Dream Loop Tool Integration — Implementation Notes

**Date:** 2026-05-03
**Status:** CONFIRMED WORKING (verified 2026-05-03 via live system audit)
**Location:** `~/.hermes/skills/autonomous-ai-agents/hermes-dream-task/scripts/dream_loop_v3.py`

---

## What Was Built

A two-tier rollout system that gives MCTS rollouts real tool access without penalizing the common case:

### Tier 1 — LLM-only (default, ~1-2s)
All rollouts run Tier-1 first via `rollout_tier1()`, which calls `call_hermes()` — the existing subprocess approach. No tool access, no cold-start cost.

### Tier 2 — Tool-using (on-demand, ~30-120s)
When Tier-1 returns confidence < 0.30 AND the branch is the top UCB1 node AND system resources are idle (CPU < 70%, RAM < 70%), `rollout_tier2()` fires. It uses `DreamAgent` which wraps `AIAgent` with `enabled_toolsets=["terminal", "file", "session_search", "memory"]`.

### Key Classes

```
DreamAgent — wraps AIAgent, handles init errors gracefully
DreamAgentPool — reuses a single DreamAgent per dream (avoids 3-5s cold-start on every rollout)
```

### Config (all in dream_loop_v3.py)
```
TOOLSETS = ["terminal", "file", "session_search", "memory"]
TOOL_ROLLOUT_THRESHOLD = 0.30   # Tier-1 conf below this → Tier-2 eligible
TOOL_ROLLOUT_BRANCH_LIMIT = 1    # Only top UCB1 node gets tools
TOOL_CALL_LIMIT = 5              # Max tool calls per Tier-2 rollout
TOOL_CALL_TIMEOUT = 30           # Seconds per individual tool call
TOOL_ROLLOUT_TIMEOUT = 120       # Hard cap for entire Tier-2 rollout
```

---

## Portable Provider Handling

DreamAgent reads the user's configured provider/model from `~/.hermes/config.yaml`:

```python
cfg = yaml.safe_load(config_path.read_text())
provider_cfg = cfg.get("model", {}).get("provider") or "minimax"
model_cfg_default = cfg.get("model", {}).get("default") or ""
```

Falls back to `OPENROUTER_API_KEY` env var if set (bypasses the need for the user's default provider).

---

## Why AIAgent Direct Import Works

- `hermes chat -q` → `AIAgent.chat()` → single text round-trip, no tool loop
- `AIAgent(enabled_toolsets=[...])` → `AIAgent.run_conversation()` → full tool-dispatch loop
- `agent.chat(message)` returns a String (same interface as `call_hermes`)

---

## Error Handling

- If AIAgent init fails (no API key, missing module): `is_available = False`, Tier-2 silently falls back to Tier-1
- If tool call fails/times out: returns `uncertain(0.5)` with evidence="tool failed: <error>"
- If no parseable JSON in response: returns `uncertain(0.5)` with evidence=raw_text[:200]

---

## Sync Locations

After any change to `dream_loop_v3.py`, copy to:
```
~/.hermes/dream-auto-dist/scripts/dream_loop_v3.py        ← git-tracked, GitHub
~/.hermes/skills/autonomous-ai-agents/hermes-dream-task/scripts/  ← scheduler imports from here
```

The plugin at `~/.hermes/plugins/dream_auto/__init__.py` references the skills path directly (line 58), so it picks up changes automatically.

---

## Testing

Test script: `~/.hermes/dream-auto-dist/scripts/test_tool_rollouts.py`

```bash
cd ~/.hermes/hermes-agent
python3 ~/.hermes/dream-auto-dist/scripts/test_tool_rollouts.py
```

Note: Tests require a running gateway session (AIAgent needs API key). In build environments without `MINIMAX_API_KEY`, the Tier-1 fallback is verified instead.

---

## Performance Expectations

- ~70-80% of rollouts stay at Tier-1 (1-2s each)
- Only borderline cases (Tier-1 conf < 0.30) trigger Tier-2
- Only top UCB1 branch per iteration gets Tier-2 access
- AIAgent instance reused across all Tier-2 rollouts in one dream (DreamAgentPool)

Expected net effect: avg dream runtime increases by ~20-30% but confidence scores on hard briefs should jump from 0.167 to 0.4-0.7 range.
