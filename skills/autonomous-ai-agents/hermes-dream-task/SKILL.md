---
name: hermes-dream-task
description: >
  Research-backed background thinking system for Hermes agents. Uses structured
  exploration trees, generate-verify loops, metacognitive monitoring, and adaptive
  termination to produce high-quality reasoning in the background.
version: 2.0.0
author: Hermes (architecture from Reflexion, SwiftSage, SPOC, MAPS, ToT)
license: MIT
metadata:
  hermes:
    tags: [dream, background, thinking, deliberative, system2, metacognition]
    triggers:
      - "dream: "
      - "dream about"
      - "background thinking"
      - "think about this"
      - "proactive research"
prerequisites:
  tools: [terminal, read_file, write_file]
---

# HERMES DREAM TASK v2

## Architecture

Based on peer-reviewed research: Reflexion (Shinn et al.), SwiftSage, SPOC, MAPS,
Tree-of-Thoughts, MetaRAG, Branch-and-Browse.

### Core Components

1. **ENTROPY GATE** — `hermes chat -q` assesses query complexity (1-10). Only starts
   dreams for score >= 7. No more auto-dreaming for simple questions.

2. **TREE-STRUCTURED EXPLORATION** — Each dream maintains a JSON exploration tree
   (not flat log). Each node has: thought, confidence score, evaluation, children.
   Enables backtracking and alternative path exploration.

3. **GENERATE-VERIFY LOOP** — Each iteration:
   - Generate a thought for the current exploration path
   - Evaluate with metacognitive monitoring (confidence, novelty, weakness)
   - Add to tree, update path based on recommendation
   - Track failures (low-confidence nodes) separately

4. **ADAPTIVE TERMINATION** — Stops when:
   - Self-evaluated confidence >= 75%
   - LLM recommends "distill_and_stop"
   - Max iterations (10) reached
   - Non-novel thoughts force backtracking past root

5. **EPISODIC MEMORY** — Three structured files per dream:
   - `insights.json` — key findings (distilled, not raw logs)
   - `failures.json` — failure patterns to avoid
   - `pending_questions.json` — open questions

6. **DISTILLED INJECTION** — When injecting into conversations, the plugin extracts
   3-5 bullet insights + confidence score. NEVER raw dream logs.

## File Structure

```
~/.hermes/state/dream/<dream_id>/
  meta.json                — metadata, status, confidence, iteration count
  exploration_tree.json    — tree of reasoning nodes
  insights.json            — distilled key findings
  failures.json            — failure patterns
  pending_questions.json   — open questions
  brief.json               — legacy compat
  status.txt               — legacy compat
  iterations.json          — legacy compat
```

## Plugin Hooks

The `dream_auto` plugin fires at 6 hook points:

| Hook | Behavior |
|------|----------|
| pre_llm_call | Inject distilled insights (never raw logs) |
| pre_tool_call | Non-blocking suggestion for complex code |
| post_tool_call | Auto-start troubleshooting dream on errors (gated) |
| post_llm_call | Entropy gate via `hermes chat -q`, auto-start if score >= 7 |
| on_session_start | Log active dreams |
| on_session_end | Clean up tracking |

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| DREAM_AUTO_ENABLED | 1 | Set to 0 to disable |
| DREAM_AUTO_VERBOSE | 0 | Set to 1 for logging |
| DREAM_AUTO_MAX_INJECT | 3 | Max dreams to inject per turn |
| DREAM_AUTO_AUTOSTART | 1 | Set to 0 to disable auto-start |
| DREAM_AUTO_MIN_COMPLEXITY | 7 | Min complexity score (1-10) to start dream |

## Dream Loop v2

`scripts/dream_loop_v2.py` implements the structured reasoning loop:

```
For each iteration:
  1. GENERATE — hermes chat -q generates a thought for current path
  2. EVALUATE — hermes chat -q evaluates with metacognitive monitoring
  3. UPDATE TREE — add node, update path based on recommendation
  4. TRACK — failures go to failures.json, confidence tracked
  5. CHECK TERMINATION — confidence threshold, distill recommendation
  6. SLEEP — 2 minutes between iterations
```

## Checking Dreams

```bash
# List active dreams
cat ~/.hermes/state/dream/*/meta.json | python3 -c "
import sys, json
for line in sys.stdin:
    try:
        m = json.loads(line)
        print(f\"{m['dream_id']}: conf={m.get('confidence',0):.0%} iter={m.get('iteration',0)} status={m.get('status','?')}\")
    except: pass
"

# Read insights
cat ~/.hermes/state/dream/<id>/insights.json | python3 -m json.tool

# Read exploration tree
cat ~/.hermes/state/dream/<id>/exploration_tree.json | python3 -m json.tool
```

## Research Papers

Key papers informing this architecture:

- **Reflexion** (2303.11366) — Verbal reinforcement + episodic memory buffer
- **SwiftSage** (2305.17390) — Fast/slow dual-process agent architecture
- **SPOC** (2506.06923) — Interleaved generate-verify in single pass
- **MAPS** (2506.23888) — Adaptive-depth iterative self-reflection
- **Tree-of-Thoughts** (2305.10601) — Deliberate problem solving with exploration
- **MetaRAG** (2402.11626) — Monitor → Evaluate → Plan metacognitive pipeline
- **Branch-and-Browse** (2510.19838) — Background reasoning + action memory
- **ReTreVal** (2601.02880) — ToT + self-refinement + reflexion memory
- **LLM Metacognition** (2505.13763) — LLMs can monitor their own thinking
