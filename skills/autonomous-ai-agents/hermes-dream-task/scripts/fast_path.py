#!/usr/bin/env python3
"""
fast_path.py — SwiftSage-style fast/slow分流 for Dream System v3

Fast path (no LLM): simple greetings, lookups, confirmations
Slow path (Monte Carlo): everything else

Philosophy:
  - Fast path is purely heuristic — NO LLM call, no latency
  - Slow path → delegate to MCTS dream_loop_v3.py
  -分流 decision is stateless and instant

Usage:
    from fast_path import should_dream_fast
    is_fast, reason = should_dream_fast("your question here")
"""

import json
import re
from typing import Tuple

# ── Fast path patterns ────────────────────────────────────────────────────────

# Questions that are definitely simple (no dreaming needed)
SIMPLE_PATTERNS = [
    # Very short queries
    (r"^[a-zA-Z\s]{1,30}\?$", "ultra-short question"),
    # Greetings
    (r"^(hi|hello|hey|howdy|sup|yo|hey there|hi there)\s*[!.]?$", "greeting"),
    # Simple confirmations / acknowledgements
    (r"^(yes|yeah|yep|sure|ok|okay|go ahead|please do)\s*[.!]?$", "simple affirmation"),
    # Thanks
    (r"^(thanks|thank you|thx|ty|cheers)\s*[.!]?$", "thanks"),
    # Single-word commands
    (r"^(help|list|show|who|what|when|where)\s*$", "single-word command"),
    # File lookups
    (r"^(ls|list files|show files|cat|head|tail)\s+[/\w]", "file system command"),
    # Status checks
    (r"^(status|health|ping|version|uptime|stats)\s*$", "status check"),
    # Simple math (no context needed)
    (r"^\s*[\d\+\-\*\/\.\,\s]+\s*$", "simple math"),
    # Current time/date
    (r"^(what'?s?\s+the\s+)?(time|date|today|now)\s*[?.]?$", "time/date query"),
    # Weather (simple)
    (r"^(how'?s?\s+the\s+)?weather\s*[?.]?$", "weather query"),
]

# Technical keywords that suggest complexity → slow path
TECHNICAL_KEYWORDS = [
    "debug", "fix", "error", "crash", "bug", "implement", "build",
    "design", "architecture", "api", "database", "sql", "python",
    "javascript", "typescript", "react", "linux", "server", "deploy",
    "kubernetes", "docker", "cloud", "aws", "git", "refactor",
    "optimize", "performance", "scalability", "security", "auth",
    "encryption", "ml ", "machine learning", "neural", "model",
    "agent", "autonomous", "pipeline", "workflow", "cron",
    "linkedin", "twitter", "social media", "marketing",
    "research", "analyze", "strategy", "planning",
]

# Questions requiring context → slow path
CONTEXT_HEAVY_PATTERNS = [
    r"\bit\b.*\b(broke|broken|not working|failed|issue)\b",
    r"\bhow\s+(do\s+i|can\s+i|to)\b",
    r"\bwhy\s+(does|is|did|didn't|can't)\b",
    r"\bshould\s+i\b",
    r"\bwhich\s+(is|the|one|approach)\b",
    r"\bshould\s+we\s+",
    r"\bwhat('s| is| was)?\s+the\s+best\s+",
    r"\brefactor\b",
    r"\bmigrate\b",
    r"\bevaluat[ei]\b",
    r"\bcompar[ei]\b",
    r"\baudit\b",
]


def should_dream_fast(query: str) -> Tuple[bool, str]:
    """
    Decide if a query should use the fast path (no dreaming) or slow path (Monte Carlo).

    Returns: (is_fast: bool, reason: str)

    Fast path (returns True): simple greeting, lookup, confirmation
    Slow path (returns False): complex reasoning, decisions, research
    """
    query_clean = query.strip()
    query_lower = query_clean.lower()

    # Check length — very short queries are usually simple
    if len(query_clean) < 30 and " " not in query_clean:
        return True, "single short word/phrase"

    # Check simple patterns
    for pattern, reason in SIMPLE_PATTERNS:
        if re.match(pattern, query_clean, re.IGNORECASE):
            # But verify it's not context-heavy
            if any(re.search(c, query_lower) for c in CONTEXT_HEAVY_PATTERNS):
                return False, "simple pattern but context-heavy"
            return True, reason

    # Technical keywords → slow path
    tech_count = sum(1 for kw in TECHNICAL_KEYWORDS if kw in query_lower)
    if tech_count >= 2:
        return False, f"multiple technical topics ({tech_count})"

    # Context-heavy patterns → slow path
    for pattern in CONTEXT_HEAVY_PATTERNS:
        if re.search(pattern, query_lower):
            return False, "context-heavy question"

    # Long queries → likely complex
    if len(query_clean) > 500:
        return False, f"long query ({len(query_clean)} chars)"

    # Contains question mark with interrogative → maybe complex
    if "?" in query_clean:
        # Check for specific complexity indicators
        complexity_indicators = [
            "explain", "describe", "compare", "evaluate", "analyze",
            "design", "implement", "create", "build", "think about",
            "opinion", "strategy", "approach", "recommend",
        ]
        if any(ind in query_lower for ind in complexity_indicators):
            return False, "complex reasoning requested"

    # Default: assume complex (better to think more than miss something)
    return False, "default: assume complex"


def fast_response(query: str) -> str | None:
    """
    Generate a fast-path response for trivially simple queries.
    Returns None if not simple enough for fast path.
    """
    query_clean = query.strip()
    query_lower = query_clean.lower()

    # Greetings
    if re.match(r"^(hi|hello|hey|howdy|sup)\s*[!.]?$", query_lower):
        return "Hey! What are you working on today? (◕‿◕)"

    # Thanks
    if re.match(r"^(thanks|thank you|thx|ty|cheers)\s*[!.]?$", query_lower):
        return "You're welcome! Let me know if you need anything else ~"

    # Status check
    if re.match(r"^(status|health|ping)\s*$", query_lower):
        return "[SYSTEM] All systems operational. Dream engine standing by."

    # Single-word help
    if query_lower.strip() in ("help", "?"):
        return "I'm here to help! Ask me anything — I can research, code, debug, plan, or just chat."

    return None  # Not simple enough for fast path


# ──分流 decision ─────────────────────────────────────────────────────────────

def classify(query: str) -> dict:
    """
    Full classification of a query for the dream system.
    Returns a dict with classification details.
    """
    is_fast, reason = should_dream_fast(query)
    fast_resp = fast_response(query) if is_fast else None

    return {
        "query": query[:100],
        "is_fast": is_fast,
        "reason": reason,
        "fast_response": fast_resp,
        "should_dream": not is_fast,  # slow path = dream
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
    else:
        # Demo with test cases
        test_queries = [
            "hi",
            "hello there!",
            "yes",
            "thanks",
            "what's the weather",
            "list files",
            "how do I debug a Python script",
            "design a scalable API architecture",
            "why is my cron job failing",
            "should I use Kubernetes or Docker for my app",
            "compare LinkedIn vs Twitter for B2B marketing",
            "implement a MCTS algorithm in Python",
            "analyze the LinkedIn engagement data from last week",
            "write a brief for an article about AI agents",
            "hello? anyone there?",
        ]
        for q in test_queries:
            result = classify(q)
            status = "⚡ FAST" if result["is_fast"] else "🧠 SLOW"
            print(f"{status}: {q[:60]}")
            print(f"  reason: {result['reason']}")
            if result["fast_response"]:
                print(f"  response: {result['fast_response']}")
            print()
        sys.exit(0)

    result = classify(query)
    print(json.dumps(result, indent=2, ensure_ascii=False))
