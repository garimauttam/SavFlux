"""
patch_service.py — Build reviewable git patches from proposed file changes.

The gap this fills
------------------
`pr_service` could open a pull request, but only if the caller already had a
unified diff. Nothing in the product produced one: the write agent emitted a
rewritten file as a markdown code block, and the review agent emitted prose.
So "create a PR" meant "paste the new file into your editor by hand, then run
git yourself" — the agent stopped exactly where the tedious part began.

This module closes that gap without shelling out to `git`. A unified diff is a
text format; `difflib` produces it correctly, and building it in-process means
no temp clone, no working tree, no subprocess, and no way for a crafted path to
reach a shell. The output applies cleanly with `git apply` or `patch -p1`.

Everything here is stdlib and offline, so the $0 path is the default path
rather than a degraded fallback.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

#: Refuse to build a patch larger than this. A runaway diff is far more likely
#: to be a bug (an LLM emitting the whole repo) than a legitimate change, and
#: GitHub rejects oversized PR bodies anyway.
MAX_PATCH_BYTES = 1_000_000

#: Lines of unchanged context around each hunk. Three is the git default and
#: what `git apply` expects when fuzz matching.
DEFAULT_CONTEXT = 3

#: A path may not escape the repository root or name a git internal.
_UNSAFE_PATH = re.compile(r"(^/)|(^[A-Za-z]:)|(\.\.)|(^\.git/)|(/\.git/)|([\x00-\x1f])|(^~)")


class PatchError(ValueError):
    """Raised when a change set cannot be turned into a safe patch."""


@dataclass
class FileChange:
    """
    One file's before/after state.

    `original` of None means the file is being created; `modified` of None
    means it is being deleted. Both None is meaningless and rejected.
    """

    path: str
    original: str | None
    modified: str | None

    @property
    def is_new(self) -> bool:
        return self.original is None and self.modified is not None

    @property
    def is_deleted(self) -> bool:
        return self.modified is None and self.original is not None

    @property
    def added_lines(self) -> int:
        if self.is_deleted:
            return 0
        new = (self.modified or "").splitlines()
        if self.is_new:
            return len(new)
        old = (self.original or "").splitlines()
        matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
        return sum(j2 - j1 for tag, _, _, j1, j2 in matcher.get_opcodes() if tag in ("insert", "replace"))

    @property
    def removed_lines(self) -> int:
        if self.is_new:
            return 0
        old = (self.original or "").splitlines()
        if self.is_deleted:
            return len(old)
        new = (self.modified or "").splitlines()
        matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
        return sum(i2 - i1 for tag, i1, i2, _, _ in matcher.get_opcodes() if tag in ("delete", "replace"))


@dataclass
class PatchResult:
    """A complete patch plus the numbers a reviewer needs before applying it."""

    diff: str
    files_changed: int
    additions: int
    deletions: int
    #: Per-file summary for the UI, so the panel need not re-parse the diff.
    file_summaries: list[dict] = field(default_factory=list)
    #: Short digest of the diff, used to detect a stale preview before apply.
    digest: str = ""

    def to_dict(self) -> dict:
        return {
            "diff": self.diff,
            "files_changed": self.files_changed,
            "additions": self.additions,
            "deletions": self.deletions,
            "files": self.file_summaries,
            "digest": self.digest,
        }


def digest_of(diff: str) -> str:
    """
    Short, stable fingerprint of a diff.

    This is the token a caller echoes back to confirm that a specific change set
    is what they reviewed. It is a property of the content, so a diff that
    changed after the preview produces a different digest and the confirmation
    no longer applies — which is the whole point. Both `build_patch` and the
    confirmation checks in `review.py` / `agent_tools.py` go through here, so the
    two sides can never disagree about what was approved.
    """
    return hashlib.sha256((diff or "").encode("utf-8")).hexdigest()[:16]


def normalise_path(path: str) -> str:
    """
    Validate a repository-relative path.

    A patch names the files it writes, so an unchecked path is an arbitrary
    file write on whoever applies it. `../../.ssh/authorized_keys` in a diff
    header is a real attack against the reviewer's machine, not a theoretical
    one — and this content can be LLM-generated, so it is untrusted by default.
    """
    cleaned = (path or "").strip().replace("\\", "/")
    if not cleaned:
        raise PatchError("file path is required")
    if len(cleaned) > 400:
        raise PatchError("file path is too long")
    # Strip a redundant leading "./" before validating.
    cleaned = re.sub(r"^\./", "", cleaned)
    if _UNSAFE_PATH.search(cleaned):
        raise PatchError(
            f"unsafe file path: {path!r}. Paths must be relative to the repository "
            "root and may not traverse upward or touch .git/"
        )
    return cleaned


def _split_keepends(text: str) -> list[str]:
    """
    Split into lines for difflib, preserving terminators.

    difflib compares the strings it is given, so keeping the line endings makes
    a missing trailing newline a visible change rather than a silent one.
    """
    return text.splitlines(keepends=True)


#: git's marker for a file whose last line has no terminator.
_NO_NEWLINE = "\\ No newline at end of file\n"


def _annotate_missing_newlines(body: list[str], old_ends: bool, new_ends: bool) -> list[str]:
    """
    Insert git's no-newline marker after each affected line of a diff body.

    The marker is positional, not a trailer: git emits it immediately after the
    specific `-` or `+` line that lacks a terminator, and it may appear twice in
    one hunk (once for each side). Appending it once at the end of the patch
    produces a diff that `git apply` rejects outright with "patch does not
    apply" — which is exactly what the round-trip test caught.
    """
    if old_ends and new_ends:
        return body

    # Find the last line belonging to each side in ONE pass. Scanning ahead
    # from every line instead makes this O(n²): on a 200k-line diff that took
    # 96 seconds, which a test caught before a user could.
    last_removed = last_added = last_context = -1
    for index, line in enumerate(body):
        if line.startswith("-"):
            last_removed = index
        elif line.startswith("+"):
            last_added = index
        elif line.startswith(" "):
            last_context = index

    # The final line of a side is whichever of its own marker or a trailing
    # context line comes last.
    old_final = max(last_removed, last_context)
    new_final = max(last_added, last_context)

    annotated: list[str] = []
    for index, line in enumerate(body):
        annotated.append(line if line.endswith("\n") else line + "\n")
        if not old_ends and index == old_final:
            annotated.append(_NO_NEWLINE)
        # A context line ends both sides at once, but the marker is only
        # emitted once for it.
        if not new_ends and index == new_final and index != old_final:
            annotated.append(_NO_NEWLINE)

    return annotated


def build_file_diff(change: FileChange, context: int = DEFAULT_CONTEXT) -> str:
    """Render one file's change as a git-style unified diff section."""
    path = normalise_path(change.path)

    if change.original is None and change.modified is None:
        raise PatchError(f"{path}: a change must have content on at least one side")

    old_lines = _split_keepends(change.original or "")
    new_lines = _split_keepends(change.modified or "")

    # Record whether each side ends with a newline before normalising, so the
    # marker can be placed on exactly the lines that need it.
    old_ends_with_newline = not old_lines or old_lines[-1].endswith("\n")
    new_ends_with_newline = not new_lines or new_lines[-1].endswith("\n")

    from_path = "/dev/null" if change.is_new else f"a/{path}"
    to_path = "/dev/null" if change.is_deleted else f"b/{path}"

    body = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=from_path,
            tofile=to_path,
            n=context,
            lineterm="\n",
        )
    )

    if not body:
        return ""  # identical content — nothing to say about this file

    # git's header lines. `git apply` tolerates their absence but `gh pr` and
    # most review tools render the diff correctly only when they are present.
    header = [f"diff --git a/{path} b/{path}\n"]
    if change.is_new:
        header.append("new file mode 100644\n")
    elif change.is_deleted:
        header.append("deleted file mode 100644\n")

    # difflib emits no "\ No newline at end of file" markers at all, so they
    # are inserted here, positioned per affected line the way git does it.
    # The first two entries of `body` are the ---/+++ file headers, which must
    # not be annotated.
    file_headers, hunks = body[:2], body[2:]
    return "".join(
        header + file_headers + _annotate_missing_newlines(hunks, old_ends_with_newline, new_ends_with_newline)
    )


def build_patch(changes: list[FileChange], context: int = DEFAULT_CONTEXT) -> PatchResult:
    """
    Combine file changes into one patch.

    Files that are unchanged are dropped rather than emitted as empty sections,
    because a PR listing files with no modifications wastes the reviewer's
    attention — the most expensive resource in this whole pipeline.
    """
    if not changes:
        raise PatchError("no changes supplied")

    seen: set[str] = set()
    sections: list[str] = []
    summaries: list[dict] = []
    additions = deletions = 0

    for change in changes:
        path = normalise_path(change.path)
        if path in seen:
            raise PatchError(f"duplicate path in change set: {path}")
        seen.add(path)

        section = build_file_diff(change, context=context)
        if not section:
            continue

        sections.append(section)
        added, removed = change.added_lines, change.removed_lines
        additions += added
        deletions += removed
        summaries.append(
            {
                "path": path,
                "status": "added" if change.is_new else "deleted" if change.is_deleted else "modified",
                "additions": added,
                "deletions": removed,
            }
        )

    if not sections:
        raise PatchError("all supplied files are identical to their current content")

    diff = "".join(sections)
    if len(diff.encode("utf-8")) > MAX_PATCH_BYTES:
        raise PatchError(
            f"patch is {len(diff) // 1024} KB, over the {MAX_PATCH_BYTES // 1024} KB limit. "
            "Split the change into smaller pull requests."
        )

    return PatchResult(
        diff=diff,
        files_changed=len(summaries),
        additions=additions,
        deletions=deletions,
        file_summaries=summaries,
        digest=digest_of(diff),
    )


def suggest_branch_name(title: str, prefix: str = "savflux") -> str:
    """
    Derive a valid git branch name from a PR title.

    git refs forbid a specific set of characters and sequences; rather than
    enumerating them, this keeps only an explicit safe alphabet. The date
    suffix keeps repeated runs on the same title from colliding.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:40].strip("-") or "change"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{prefix}/{slug}-{stamp}"


def build_pr_body(
    summary: str,
    result: PatchResult,
    findings: list[dict] | None = None,
    include_diff: bool = False,
) -> str:
    """
    Compose the PR description.

    A reviewer opening this should be able to answer "what changed and why"
    without reading the diff, so the summary leads and the file table follows.
    The diff itself is opt-in: inlining a large one pushes the discussion
    thread below the fold on GitHub.
    """
    lines = [summary.strip() or "_No summary provided._", ""]

    lines.append("### Changes")
    lines.append("")
    lines.append("| File | Status | +/- |")
    lines.append("| --- | --- | --- |")
    for entry in result.file_summaries:
        lines.append(
            f"| `{entry['path']}` | {entry['status']} | "
            f"+{entry['additions']} / −{entry['deletions']} |"
        )
    lines.append("")
    lines.append(
        f"**{result.files_changed} file(s) changed, "
        f"{result.additions} insertion(s), {result.deletions} deletion(s).**"
    )

    if findings:
        lines.extend(["", "### Issues addressed", ""])
        for finding in findings[:10]:
            rule = finding.get("rule_id", "")
            title = finding.get("title", "issue")
            line_no = finding.get("line")
            location = f" (`L{line_no}`)" if line_no else ""
            tag = f" `{rule}`" if rule else ""
            lines.append(f"- **{title}**{location}{tag}")

    if include_diff:
        lines.extend(["", "<details><summary>Full diff</summary>", "", "```diff", result.diff.rstrip("\n"), "```", "", "</details>"])

    lines.extend(
        ["", "---", "", "🤖 Generated by [SavFlux](https://github.com/garimauttam/SavFlux) — review before merging."]
    )
    return "\n".join(lines)
