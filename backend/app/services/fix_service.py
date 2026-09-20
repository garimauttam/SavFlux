"""
fix_service.py — Verified repairs behind one function.

The pipeline "analyse → apply deterministic fixes → prove it improved → emit a
patch" lived inline in `POST /review/autofix`. That was fine while the endpoint
was the only caller. It is not the only caller any more: the deterministic agent
runs the same pipeline as the `autofix` tool, and the review UI chains the result
into `build_patch`.

Copying the pipeline into the agent would have been the easy move and the wrong
one. Autofix is only trustworthy because of its gates — re-parse, re-analyse,
refuse if the finding survives or a worse one appears — and a second, thinner
copy of those gates is exactly how a tool earns a reputation for breaking code.
So the sequence lives here, once, and every caller goes through it.

Everything in this module is CPU-only and offline: no model call, no network, no
cost. On a 400-line file the whole pipeline is single-digit milliseconds, which
is why the answer to "should the LLM fix this?" is often "the parser already
did, and it can prove it".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.services.code_analysis import analyze_file
from app.services.code_analysis.autofix import AppliedFix, autofix_python
from app.services.patch_service import FileChange, PatchError, build_patch

#: Extensions this module can rewrite. Other languages are analysed but not
#: edited — a fixer that has never been verified for a language is worse than
#: no fixer at all.
PYTHON_EXTENSIONS = frozenset({"py", "pyw", "pyi"})


class FixError(Exception):
    """Base class for expected, caller-facing failures."""


class MissingContentError(FixError):
    """No source available: not on disk, not in the index, not supplied."""


class UnsupportedLanguageError(FixError):
    """Deterministic fixes are not implemented for this language."""


@dataclass
class FixOutcome:
    """
    Everything one autofix pass learned, in a shape both callers can use.

    `content` is the repaired source. The review UI needs it to chain several
    files into a single patch, and returning it here keeps the client from
    re-deriving it — a client that recomputes the fix would be a second fixer.
    """

    path: str
    language: str
    original: str
    content: str
    fixes: list[AppliedFix] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    findings_before: int = 0
    findings_after: int = 0
    score_before: int = 0
    score_after: int = 0
    #: `PatchResult.to_dict()` when something changed, else None. A patch for an
    #: unchanged file would be an empty diff implying success.
    patch: dict | None = None

    @property
    def changed(self) -> bool:
        return bool(self.fixes)

    @property
    def diff(self) -> str:
        return (self.patch or {}).get("diff", "")

    @property
    def digest(self) -> str:
        return (self.patch or {}).get("digest", "")

    def to_dict(self, *, include_content: bool = True) -> dict:
        """
        JSON-ready view.

        Merged rather than nested at the top level so the existing
        `/review/autofix` response contract is preserved key-for-key: callers
        that only read `fixed` / `fixes` / `patch` keep working unchanged.
        """
        data: dict = {
            "path": self.path,
            "fixed": self.changed,
            "fixes": [
                {
                    "rule_id": f.rule_id,
                    "line": f.line,
                    "description": f.description,
                    "before": f.before,
                    "after": f.after,
                }
                for f in self.fixes
            ],
            "skipped": self.skipped,
            "findings_before": self.findings_before,
            "findings_after": self.findings_after,
            "score_before": self.score_before,
            "score_after": self.score_after,
            "patch": self.patch,
        }
        if include_content:
            data["content"] = self.content
        return data


def detect_language(path: str) -> str:
    """Extension without the dot, matching how ingestion records `language`."""
    name = (path or "").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _apply(original: str, path: str, language: str) -> FixOutcome:
    """The synchronous core — analysis, fixes, verification, patch."""
    analysis = analyze_file(original, path, language)
    outcome = FixOutcome(
        path=path,
        language=language,
        original=original,
        content=original,
        findings_before=len(analysis.findings),
        score_before=analysis.risk_score(),
    )

    result = autofix_python(original, analysis.findings, path)
    outcome.fixes = result.fixes
    outcome.skipped = result.rejected
    outcome.content = result.content

    if not result.changed:
        # Honest empty result: nothing was safe to fix, so there is no patch.
        outcome.findings_after = outcome.findings_before
        outcome.score_after = outcome.score_before
        return outcome

    # Re-analyse the repaired source so before/after is a measurement, not a
    # claim. `_verify` already proved the individual findings cleared; this is
    # the file-level number the UI shows.
    after = analyze_file(result.content, path, language)
    outcome.findings_after = len(after.findings)
    outcome.score_after = after.risk_score()

    try:
        patch = build_patch([FileChange(path, original, result.content)])
    except PatchError:
        # Unreachable while `changed` is true, but a fix that cannot be rendered
        # as a diff must not be presented as one.
        return outcome

    outcome.patch = patch.to_dict()
    return outcome


async def apply_fixes(
    path: str,
    *,
    content: str | None = None,
    source: str | None = None,
    repo_url: str | None = None,
) -> FixOutcome:
    """
    Repair every finding in `path` that has exactly one correct form.

    Content resolution order: explicit `content` (paste review), then the index
    via `source` / `repo_url` / `path`. Raises `MissingContentError` when none of
    them has the file, and `UnsupportedLanguageError` for non-Python — both are
    facts the caller should report rather than swallow.

    Runs the CPU work in a thread so a 4000-line file does not block the event
    loop that is simultaneously streaming someone else's review.
    """
    language = detect_language(path)
    if language not in PYTHON_EXTENSIONS:
        raise UnsupportedLanguageError(
            "Automatic fixes are currently implemented for Python only. "
            "Other languages are analysed but not rewritten."
        )

    original = content or ""
    if not original:
        from app.services.indexed_content import read_indexed_file

        original = await asyncio.to_thread(
            read_indexed_file, path, source=source, repo_url=repo_url
        )
    if not original:
        raise MissingContentError(
            f"No content for {path!r}. Pass `content` explicitly or index the file first."
        )

    return await asyncio.to_thread(_apply, original, path, language)
