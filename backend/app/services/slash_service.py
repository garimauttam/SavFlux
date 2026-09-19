"""
slash_service.py — P2 Slash Commands & Quick Actions ($0, local)

Provides slash command registry for chat: /explain, /review, /fix, /test, etc.
Each command is a prompt template with args. No LLM — pure string expansion.

Used by:
  GET  /slash/commands          — list all commands
  POST /slash/expand            — expand {command, args} -> {prompt, description}
  Frontend ChatWindow autocomplete when typing /

All $0 — static registry, no deps.
"""

from __future__ import annotations

from typing import Any

# Registry: command -> {description, template, args_hint, kind}
COMMANDS: dict[str, dict[str, Any]] = {
    "explain": {
        "description": "Explain code in plain English",
        "template": "Explain this code in detail, like I'm a new joiner:\n\n{args}",
        "args_hint": "<code or file path>",
        "kind": "chat",
        "examples": ["/explain @auth.py", "/explain def hello(): ..."],
    },
    "review": {
        "description": "Security & quality review",
        "template": "Review this code for security vulnerabilities, bugs, and performance issues. Be concise and actionable:\n\n{args}",
        "args_hint": "<code or file>",
        "kind": "review",
        "examples": ["/review src/auth.py", "/review @db.py"],
    },
    "fix": {
        "description": "Suggest a fix for the issue",
        "template": "Suggest a minimal fix for this issue. Show the corrected code:\n\n{args}",
        "args_hint": "<error or code>",
        "kind": "write",
        "examples": ["/fix TypeError in auth.py", "/fix {args}"],
    },
    "test": {
        "description": "Generate tests for this code",
        "template": "Generate comprehensive unit tests (pytest) for this code. Cover edge cases:\n\n{args}",
        "args_hint": "<function or file>",
        "kind": "write",
        "examples": ["/test src/utils.py", "/test def fib(n): ..."],
    },
    "doc": {
        "description": "Generate documentation",
        "template": "Generate clear documentation (docstring + README section) for this code:\n\n{args}",
        "args_hint": "<code>",
        "kind": "write",
        "examples": ["/doc @api.py", "/doc class Foo: ..."],
    },
    "summarize": {
        "description": "Summarize file or module",
        "template": "Summarize what this file/module does, its key functions, and dependencies:\n\n{args}",
        "args_hint": "<file path>",
        "kind": "chat",
        "examples": ["/summarize src/ingest.py"],
    },
    "ask": {
        "description": "Ask about codebase (RAG)",
        "template": "{args}",
        "args_hint": "<question>",
        "kind": "chat",
        "examples": ["/ask How is auth handled?", "/ask Where is token validated?"],
    },
    "health": {
        "description": "Repo health check",
        "template": "Give me a health report for {args} — risks, debt, and next steps.",
        "args_hint": "<repo or file>",
        "kind": "chat",
        "examples": ["/health SavFlux", "/health src/auth.py"],
    },
    "diff": {
        "description": "Compare two files",
        "template": "Compare these two files and highlight key differences in logic and risk:\n\n{args}",
        "args_hint": "<fileA vs fileB>",
        "kind": "chat",
        "examples": ["/diff src/a.py vs src/b.py"],
    },
    "snippet": {
        "description": "Save as snippet",
        "template": "Save this as a reusable snippet with a good title and tags:\n\n{args}",
        "args_hint": "<code>",
        "kind": "general",
        "examples": ["/snippet def hello(): ..."],
    },
    "prompt": {
        "description": "Save as prompt",
        "template": "{args}",
        "args_hint": "<prompt text>",
        "kind": "general",
        "examples": ["/prompt Explain like I'm 5: {args}"],
    },
    "help": {
        "description": "Show available slash commands",
        "template": "Available slash commands:\n" + "\n".join([f"/{k} — {v['description']}" for k, v in {
            "explain": {"description": "Explain code"},
            "review": {"description": "Security review"},
            "fix": {"description": "Suggest fix"},
            "test": {"description": "Generate tests"},
            "doc": {"description": "Generate docs"},
            "summarize": {"description": "Summarize"},
            "ask": {"description": "Ask codebase"},
            "health": {"description": "Health check"},
            "diff": {"description": "Compare files"},
            "snippet": {"description": "Save snippet"},
            "prompt": {"description": "Save prompt"},
            "help": {"description": "Show help"},
        }.items()]),
        "args_hint": "",
        "kind": "general",
        "examples": ["/help"],
    },
}

def list_commands() -> list[dict[str, Any]]:
    return [
        {"command": f"/{k}", "name": k, **v}
        for k, v in COMMANDS.items()
    ]

def expand_command(command: str, args: str = "") -> dict[str, Any]:
    """Expand /command + args into prompt."""
    cmd = (command or "").strip().lstrip("/").lower()
    if not cmd:
        raise ValueError("command is required (e.g. /explain)")
    if cmd not in COMMANDS:
        raise ValueError(f"Unknown command /{cmd}. Try /help for list.")
    entry = COMMANDS[cmd]
    template = entry["template"]
    # Support {args} placeholder; if no placeholder, append
    if "{args}" in template:
        prompt = template.replace("{args}", (args or "").strip())
    else:
        prompt = template + ("\n\n" + args.strip() if args and args.strip() else "")
    # Clean up empty args case for some commands
    if not args or not args.strip():
        # For /help, return template as is
        # For others, keep placeholder hint
        prompt = prompt.replace("{args}", "").strip()
        if not prompt or prompt.endswith(":"):
            prompt = prompt + " " + (entry.get("args_hint", "") or "").strip()
            prompt = prompt.strip()
    return {
        "command": f"/{cmd}",
        "prompt": prompt,
        "description": entry["description"],
        "kind": entry["kind"],
        "args": args or "",
    }

def search_commands(query: str, limit: int = 10) -> list[dict[str, Any]]:
    if not query:
        return list_commands()[:limit]
    q = query.lower().strip().lstrip("/")
    scored: list[tuple[int, dict]] = []
    for cmd, entry in COMMANDS.items():
        hay = f"{cmd} {entry['description']} {entry.get('args_hint','')}".lower()
        score = -1
        if q == cmd:
            score = 100
        elif cmd.startswith(q):
            score = 80
        elif q in cmd or q in hay:
            score = 60
        else:
            # subsequence
            qi = 0
            for ch in hay:
                if qi < len(q) and ch == q[qi]:
                    qi += 1
            if qi == len(q):
                score = 30
        if score >= 0:
            scored.append((score, {"command": f"/{cmd}", "name": cmd, **entry}))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [v for _, v in scored[:limit]]
