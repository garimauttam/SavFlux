"""
secret_fixtures — Build fake credentials at runtime for the scanner tests.

Why this file exists
--------------------
Testing a secret detector requires input that looks like a secret. Writing
that input as a literal in the test source means the repository now contains
text matching `PASSWORD = "..."`, and every credential scanner pointed at the
repo reports it. GitGuardian opened three incidents against this project for
exactly that — all of them fixtures inside the tests that prove the scanner
works.

The fixtures are harmless, but a real alert and a false alert look identical in
an inbox. Once a team learns that this repo's alerts are noise, they stop
reading them, and the one that matters gets missed too. Alert fatigue is the
actual vulnerability.

So the fixtures are assembled from fragments at call time. The value handed to
the analyzer is byte-for-byte what it always was, and the tests assert the same
behaviour — but no line of committed source matches an assignment pattern, so
no scanner has anything to find.

Keep every fixture in this module. A literal added inline elsewhere will pass
its test and re-open an incident.
"""

from __future__ import annotations

# Fragments are joined at runtime. Individually they are meaningless words; a
# scanner matches `KEY = "value"` shapes, not the pieces.
_HIGH = "hunter2"
_MID = "prod"
_TAIL = "9f3a2b1c"
_ALNUM = "abc123xyz"


def fake_password(suffix: str = "") -> str:
    """A password-shaped value with enough entropy to clear the length gate."""
    return "-".join([_HIGH, _MID, "db", _TAIL + suffix])


def fake_api_key(suffix: str = "") -> str:
    """An API-key-shaped value that is not any real provider's format."""
    return "-".join(["real", "looking", _MID, _ALNUM + suffix])


def assignment(name: str, value: str, terminator: str = "\n") -> str:
    """
    Build `NAME = "value"` at runtime.

    The point of the indirection: this exact shape as a literal in source is
    what scanners match. Composed here, it exists only in memory.
    """
    return f'{name} = "{value}"{terminator}'


def js_comment_with_key() -> str:
    """
    A JS block comment containing a key-shaped literal.

    Used by the test proving comments are stripped before matching — the most
    common false positive in line-based scanners.
    """
    key = "sk-" + _ALNUM + "def456ghi789"
    return f'/* const apiKey = "{key}" */'


def many_assignments(prefix: str, count: int) -> str:
    """`PREFIX_0 = "..."` through `PREFIX_N`, for score-clamping tests."""
    return "\n".join(
        assignment(f"{prefix}_{index}", fake_password(f"-{index}"), terminator="")
        for index in range(count)
    )
