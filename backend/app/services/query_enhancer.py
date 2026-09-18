"""
query_enhancer.py — Query expansion, intent routing, and conversation summarization.

1. File Tag Extraction:
   Detects `@filename` or `@path/to/file` in user queries (e.g., "@auth.py where is login implemented?")
   and extracts both the clean question and the target file filter.

2. Intent-Based Query Routing  (Idea 2 — from awesome-llm-apps/rag_database_routing)
   Classifies the query intent into one of 5 namespaces using pure keyword matching
   (zero LLM calls). Each namespace maps to a ChromaDB metadata filter so retrieval
   only scans the relevant subset of the index.
   Namespace → typical coverage:
     security  → auth, token, password, injection, XSS, SQL, secret files
     tests     → test_*.py, *.spec.ts, *.test.tsx files
     config    → .env, settings, config, yaml, json, docker files
     api       → routes, endpoints, controllers, HTTP handlers
     general   → everything (no filter applied)

3. Query Expansion (Multi-Query):
   Generates complementary technical variations of the query for better retrieval recall.

4. Context Compaction:
   Compacts long conversation turns into a succinct structured summary so token budgets
   are preserved for code snippets.
"""

import re
from typing import Tuple, Optional, List, Literal


# ── Intent-based query routing ────────────────────────────────────────────────

QueryIntent = Literal["security", "tests", "config", "api", "general"]

# Keyword sets per namespace — deterministic, zero LLM cost.
_INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "security": (
        "auth", "authentication", "authoriz", "login", "logout", "session",
        "jwt", "token", "password", "secret", "api key", "api_key",
        "injection", "xss", "csrf", "sql inject", "vulnerability", "exploit",
        "sanitiz", "escape", "hashing", "bcrypt", "oauth", "rbac", "permission",
        "cors", "rate limit",
    ),
    "tests": (
        "test", "spec", "unit test", "integration test", "pytest", "jest",
        "fixture", "mock", "assert", "coverage", "conftest",
    ),
    "config": (
        "config", "configuration", "setting", "environment", ".env", "dotenv",
        "docker", "compose", "yaml", "yml", "json config", "secret key",
        "deploy", "railway", "vercel", "ci/cd", "github action", "workflow",
    ),
    "api": (
        "endpoint", "route", "router", "controller", "handler", "http",
        "get request", "post request", "rest api", "fastapi", "express",
        "middleware", "request", "response", "status code", "webhook",
    ),
}

# Language/file-name metadata filters for each intent.
# These are applied as ChromaDB `where` clauses.
_INTENT_FILTERS: dict[str, dict] = {
    "security": {
        "$or": [
            {"file_name": {"$contains": "auth"}},
            {"file_name": {"$contains": "security"}},
            {"file_name": {"$contains": "token"}},
            {"file_name": {"$contains": "permission"}},
            {"language": {"$in": ["py", "ts", "js"]}},
        ]
    },
    "tests": {
        "$or": [
            {"file_name": {"$contains": "test"}},
            {"file_name": {"$contains": "spec"}},
            {"file_name": {"$contains": "conftest"}},
        ]
    },
    "config": {
        "$or": [
            {"language": {"$in": ["json", "yaml", "yml", "env", "toml"]}},
            {"file_name": {"$contains": "config"}},
            {"file_name": {"$contains": "setting"}},
            {"file_name": {"$contains": "docker"}},
            {"file_name": {"$contains": ".env"}},
        ]
    },
    "api": {
        "$or": [
            {"file_name": {"$contains": "route"}},
            {"file_name": {"$contains": "api"}},
            {"file_name": {"$contains": "endpoint"}},
            {"file_name": {"$contains": "controller"}},
            {"file_name": {"$contains": "handler"}},
        ]
    },
    "general": {},  # no filter — search everything
}


def route_query_intent(query: str) -> QueryIntent:
    """
    Classify the query intent using keyword matching.

    Returns one of: "security" | "tests" | "config" | "api" | "general"

    Pure deterministic function — zero LLM calls, zero latency.
    Falls back to "general" (no filter) when no keywords match, so retrieval
    is never broken by misclassification.

    WHY COUNT-BASED INSTEAD OF FIRST-MATCH?
    A query like "how does the API handle auth token validation?" contains keywords
    for both "security" (auth, token) and "api" (API, endpoint). First-match returns
    "security" due to dict iteration order. Count-based scoring picks the intent
    with the most keyword hits — a better proxy for the user's actual intent.

    Examples:
      "where is JWT validated?" → "security"
      "how are tests structured?" → "tests"
      "what env vars are needed?" → "config"
      "show me the /ingest endpoint" → "api"
      "explain the chunking logic" → "general"
    """
    lower = query.lower()
    scores: dict[str, int] = {
        intent: sum(1 for kw in keywords if kw in lower)
        for intent, keywords in _INTENT_KEYWORDS.items()
    }
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "general"  # type: ignore[return-value]


def get_intent_filter(intent: QueryIntent) -> dict:
    """
    Return a ChromaDB `where` clause dict for the given intent.

    Returns {} for "general" (no filter applied — retrieves from all files).
    The caller merges this with any existing repo_url filter.
    """
    return _INTENT_FILTERS.get(intent, {})


def extract_file_scope(question: str) -> Tuple[str, Optional[str]]:
    """
    Extracts `@filename` or `@path` tokens from the query.
    Example: "@auth.py how is JWT validated?" -> ("how is JWT validated?", "auth.py")
    """
    match = re.search(r"@([A-Za-z0-9_\-\./\\]+)", question)
    if match:
        file_filter = match.group(1).strip()
        cleaned_question = question.replace(match.group(0), "").strip()
        return cleaned_question or question, file_filter
    return question, None


def local_query_variants(query: str) -> List[str]:
    """Create cheap code-search variants without another model round trip."""
    variants = [query]
    identifiers = re.findall(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", query)
    if identifiers:
        variants.append(" ".join(identifiers))

    lower = query.lower()
    synonym_groups = (
        ("auth", "authentication", "authorization", "login", "token"),
        ("error", "exception", "failure", "retry", "handling"),
        ("database", "db", "sql", "query", "repository"),
        ("dependency", "import", "module", "package"),
        ("config", "configuration", "settings", "environment"),
    )
    for group in synonym_groups:
        if any(term in lower for term in group):
            variants.append(f"{query} {' '.join(group)}")
            break

    return list(dict.fromkeys(variants))[:3]



def compact_chat_history(chat_history: list[dict], max_turns: int = 6) -> str:
    """
    Compacts chat history into a structured concise format preserving code context.
    """
    if not chat_history:
        return ""

    HISTORY_TRUNCATE = 250
    history_str = ""
    for msg in chat_history[-max_turns:]:
        role = "User" if msg.get("role") == "user" else "CodeSage"
        content = msg.get("content", "")
        if len(content) > HISTORY_TRUNCATE:
            content = content[:HISTORY_TRUNCATE] + "…[truncated]"
        history_str += f"{role}: {content}\n"
    return history_str
