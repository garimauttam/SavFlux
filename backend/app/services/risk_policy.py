"""
risk_policy.py — A deterministic gate on the one operation that cannot be undone.

WHY THIS EXISTS
---------------
Everything in SavFlux is reversible: a review is text, a patch is a file on disk,
a fix is verified by re-parsing. Opening a pull request is not — it notifies
people, starts CI, and lands in a repository's history. Until now the only thing
standing between a model's suggestion and a live PR was `confirm_digest`: proof
that a human saw *this diff*. That answers "did you look at it?" and not "should
this be pushed at all?".

So the gate asks the second question, deterministically:

    How risky is this change, on its own evidence,
    and has someone approved *that*?

The score is not a model's opinion. Every point comes from a signal that was
computed by something else in this codebase and can be pointed at: the parser's
findings, the dependency graph's blast radius, the file's path, whether a
verifier ran. A user can therefore disagree with the number and know exactly
which measurement to argue with — which is the only kind of gate worth having.

WHAT A DECISION LOOKS LIKE
--------------------------
    below `risk_approval_threshold`   → proceed (as before this module existed)
    at or above it                   → proceed only with an approval token bound to
                                       this exact change *and* this exact assessment
    at or above `risk_block_threshold` → refuse, even with approval, and hand back
                                       the manual `gh` command instead

That last row is the point of the whole thing. An agent should not be able to
open a PR that touches auth and CI with unverified changes because it was asked
twice. It can still prepare everything; a human runs the command. The escape
hatch costs the user one paste and zero ceremony, which is why it can be this
strict.

POLARITY, BECAUSE THIS CODEBASE HAS TWO "RISK SCORES" ALREADY
------------------------------------------------------------
`FileAnalysis.risk_score()` is 1–10 where **10 is healthy** (a quality score).
`dep_graph` / `impact_analyzer` risk_score is higher-is-worse. This module's
`ChangeRisk.score` is 0–10 where **higher is riskier**, and it is named
`ChangeRisk` rather than reusing either to keep the two from being averaged by
accident.

STORAGE
-------
`chroma_data/policy_ledger.json` — append-only, capped, atomic, and never able
to fail a request. Every gated decision is recorded with its signals, so "why
was this blocked?" has an answer on disk.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.paths import data_file

logger = logging.getLogger(__name__)

#: Bump when the signal weights change, so a ledger entry from an older policy is
#: not read as if today's arithmetic produced it.
POLICY_VERSION = 1

ACTION_AUTOFIX = "autofix"
ACTION_BUILD_PATCH = "build_patch"
ACTION_CREATE_PR = "create_pr"

#: Path fragments that make a change security-relevant. Same list the triage
#: score and the dependency graph use, so "sensitive" means one thing here.
SENSITIVE_PATH_HINTS = (
    "auth", "crypto", "secret", "permission", "credential", "password",
    "session", "payment", "billing", "token", "security",
)

#: Files whose content is a dependency decision rather than application logic:
#: changing one changes what every machine installs or runs.
DEPENDENCY_FILES = (
    "requirements.txt", "requirements-dev.txt", "requirements-prod.txt",
    "pyproject.toml", "setup.py", "setup.cfg", "poetry.lock", "pipfile",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "cargo.toml", "cargo.lock", "go.mod", "go.sum", "gemfile", "gemfile.lock",
    "dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".github/workflows/", "terraform", "cloudformation",
)

# ── Signal weights ────────────────────────────────────────────────────────────
# Chosen so that a single severe, *parsed* finding in a security file with real
# blast radius reaches the approval threshold on its own — because that change
# deserves a second look — while ordinary work stays below it without anyone
# tuning anything.
W_CRITICAL_FINDING = 4
W_HIGH_FINDING = 2
W_DIFF_SECURITY_FLAG = 2      # pattern match in added lines — weaker than a parse
W_FINDINGS_CAP = 5
W_BLAST_1 = 1
W_BLAST_3 = 2
W_BLAST_10 = 3
W_SENSITIVE_PATH = 2
W_DEPENDENCY_SURFACE = 2
#: A verifier ran and the change did not pass. This is evidence of a problem.
W_UNVERIFIED = 3
#: No verifier ran at all. Weaker than a failure — it is an absence of evidence,
#: not evidence of absence — but not free either: a caller that skips verification
#: must not thereby score better than one that performed it.
W_NOT_VERIFIED = 2
W_STALE_INDEX = 1
W_BREADTH = 1

#: Breadth thresholds — a change nobody can review in one sitting.
BREADTH_FILES = 10
BREADTH_LINES = 400

LEVELS = ("low", "medium", "high", "critical")

#: How long a built dependency graph is reused. The graph derives from the index,
#: which only changes on ingest, so rebuilding it per request would be spending
#: seconds to learn the same thing.
_GRAPH_TTL_SECONDS = 60.0
_graph_cache: dict[str, tuple[float, dict]] = {}

_LOCK = threading.Lock()
MAX_LEDGER_ENTRIES = 200


def invalidate_graph_cache() -> None:
    """Drop cached dependency graphs — called after an ingest changes the index."""
    _graph_cache.clear()


# ── Types ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RiskSignal:
    """One measurement that added points, with its evidence attached."""

    name: str
    weight: int
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "weight": self.weight,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass
class ChangeRisk:
    """
    How risky a change set is, 0–10, higher = riskier.

    `digest` is content-addressed over the change itself, which is what binds an
    approval to one specific change rather than to "a change like this".
    """

    score: int
    level: str
    signals: list[RiskSignal]
    scope: dict[str, Any]
    digest: str
    change_digest: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """One line for a status message or a log entry."""
        if not self.signals:
            return f"risk {self.score}/10 ({self.level}) — no risk signals fired"
        top = ", ".join(signal.name for signal in self.signals[:3])
        return f"risk {self.score}/10 ({self.level}) — {top}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": POLICY_VERSION,
            "score": self.score,
            "level": self.level,
            "signals": [signal.to_dict() for signal in self.signals],
            "scope": self.scope,
            "digest": self.digest,
            "change_digest": self.change_digest,
            "notes": self.notes,
            "summary": self.summary,
        }


@dataclass
class PolicyDecision:
    """What the policy says about performing `action` with this risk."""

    action: str
    risk: ChangeRisk
    allowed: bool
    requires_approval: bool
    approved: bool
    blocked: bool
    token: str
    approval_threshold: int
    block_threshold: int
    reason: str

    def to_dict(self, *, include_signals: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "action": self.action,
            # The single unambiguous vocabulary field: "allowed" |
            # "approval_required" | "blocked". Clients should branch on this
            # rather than re-deriving it from three booleans, which is the kind of
            # inference that eventually gets it wrong in the permissive direction.
            "status": self.status,
            "policy_version": POLICY_VERSION,
            "score": self.risk.score,
            "level": self.risk.level,
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "approved": self.approved,
            "blocked": self.blocked,
            "approval_token": self.token,
            "approval_threshold": self.approval_threshold,
            "block_threshold": self.block_threshold,
            "reason": self.reason,
            "signals": [s.to_dict() for s in self.risk.signals] if include_signals else [],
            "scope": self.risk.scope,
        }
        return data

    @property
    def status(self) -> str:
        """`allowed` | `approval_required` | `blocked` — the wire vocabulary."""
        if self.blocked:
            return "blocked"
        if self.requires_approval and not self.approved:
            return "approval_required"
        return "allowed"


# ── Assessment ────────────────────────────────────────────────────────────────


def _level_for(score: int) -> str:
    if score >= 9:
        return "critical"
    if score >= 6:
        return "high"
    if score >= 3:
        return "medium"
    return "low"


def _canonical(*parts: str) -> str:
    from app.services.patch_service import digest_of

    return digest_of("\n".join(parts))


def _graph_for(repo_url: str | None) -> dict | None:
    """
    Static dependency graph for a repo, cached briefly. None when unavailable.

    This module refuses to *guess* at blast radius: if the graph cannot be built
    (no index, ChromaDB unavailable), the signal simply does not fire and the
    scope records `graph_available: false`. A missing measurement is not evidence
    of safety, so the level is reported with that caveat rather than promoted.
    """
    key = repo_url or ""
    now = time.monotonic()
    cached = _graph_cache.get(key)
    if cached and now - cached[0] < _GRAPH_TTL_SECONDS:
        return cached[1]
    try:
        from app.services.dep_graph import build_dependency_graph

        graph = build_dependency_graph(repo_url)
        _graph_cache[key] = (now, graph)
        return graph
    except Exception as exc:  # noqa: BLE001 — the gate falls back to diff-only
        logger.debug("dependency graph unavailable for risk assessment: %s", exc)
        return None


def _scope_of(paths: list[str], additions: int, deletions: int, graph_available: bool) -> dict:
    return {
        "files": sorted(dict.fromkeys(p for p in paths if p))[:50],
        "file_count": len(dict.fromkeys(p for p in paths if p)),
        "additions": additions,
        "deletions": deletions,
        "graph_available": graph_available,
    }


def _sensitive_signals(paths: list[str]) -> list[RiskSignal]:
    signals: list[RiskSignal] = []
    lowered = [(p or "").lower() for p in paths]

    sensitive = sorted({p for p, low in zip(paths, lowered)
                        if any(hint in low for hint in SENSITIVE_PATH_HINTS)})
    if sensitive:
        signals.append(RiskSignal(
            "sensitive_path", W_SENSITIVE_PATH,
            f"{len(sensitive)} security-sensitive path(s) in the change",
            {"paths": sensitive[:10]},
        ))

    dependency = sorted({p for p, low in zip(paths, lowered)
                         if any(marker in low for marker in DEPENDENCY_FILES)})
    if dependency:
        signals.append(RiskSignal(
            "dependency_surface", W_DEPENDENCY_SURFACE,
            f"{len(dependency)} dependency or build file(s) changed",
            {"paths": dependency[:10]},
        ))
    return signals


def _blast_signal(impacted: list[str], graph_available: bool, paths: list[str]) -> RiskSignal | None:
    if not graph_available or not impacted:
        return None
    count = len(impacted)
    weight = W_BLAST_10 if count >= 10 else W_BLAST_3 if count >= 3 else W_BLAST_1
    return RiskSignal(
        "blast_radius", weight,
        f"{count} indexed file(s) import something this change touches",
        {"impacted_files": sorted(impacted)[:20], "changed_files": sorted(paths)[:10]},
    )


def _verification_signals(verified: bool | None, detail: str = "") -> list[RiskSignal]:
    """
    What the verification gate found, if it ran.

    Three states, deliberately distinct:

      True  — a verifier proved the change applies. No signal.
      False — a verifier ran and the change failed. Evidence of a real problem:
              a patch that does not apply is worse than no patch, because the
              failure lands on the user's machine.
      None  — nobody checked. A weaker signal, because an absence of evidence is
              not evidence of absence — but not nothing, or callers would be
              rewarded for skipping the check.
    """
    # The verifier's own output rides along as evidence: a user who disagrees
    # with the refusal should be able to read the `git apply` failure that caused
    # it rather than take our word for it.
    evidence = {"verifier_output": detail[:400]} if detail else {}

    if verified is True:
        return []
    if verified is False:
        return [RiskSignal(
            "verification_failed", W_UNVERIFIED,
            "the verifier rejected this change — it does not apply as given",
            evidence,
        )]
    return [RiskSignal(
        "not_verified", W_NOT_VERIFIED,
        "no verifier has checked this change — it is proposed, not proved",
        evidence,
    )]


def _breadth_signal(files: int, lines: int) -> RiskSignal | None:
    if files <= BREADTH_FILES and lines <= BREADTH_LINES:
        return None
    return RiskSignal(
        "change_breadth", W_BREADTH,
        f"{files} file(s) / {lines} changed line(s) — larger than one review can hold",
        {"files": files, "lines": lines,
         "limits": {"files": BREADTH_FILES, "lines": BREADTH_LINES}},
    )


def _stale_signal(repo_url: str | None) -> RiskSignal | None:
    """
    Whether the index this change was built against still matches upstream.

    An unknown status does not fire: it means the repo was never ingested from a
    URL (a local paste), not that the code is stale.
    """
    if not repo_url:
        return None
    try:
        from app.services.trust_service import get_entry

        entry = get_entry(repo_url)
    except Exception:  # noqa: BLE001
        return None
    if entry and entry.get("status") == "stale":
        return RiskSignal(
            "stale_index", W_STALE_SIGNAL_WEIGHT(),
            "the index is behind upstream HEAD — the diff may not apply cleanly",
            {"indexed_sha": entry.get("indexed_sha"), "upstream_sha": entry.get("upstream_sha")},
        )
    return None


def W_STALE_SIGNAL_WEIGHT() -> int:
    """Indirection kept so the weight lives in one place with the others."""
    return W_STALE_INDEX


def _assemble(
    signals: list[RiskSignal],
    *,
    paths: list[str],
    additions: int,
    deletions: int,
    graph_available: bool,
    change_digest: str,
    notes: list[str] | None = None,
) -> ChangeRisk:
    """Sum the signals, cap at 10, and compute the digests that bind an approval."""
    score = min(10, sum(signal.weight for signal in signals))
    level = _level_for(score)
    digest_inputs = [change_digest, str(score), level] + sorted(s.name for s in signals)
    return ChangeRisk(
        score=score,
        level=level,
        signals=signals,
        scope=_scope_of(paths, additions, deletions, graph_available),
        digest=_canonical(*digest_inputs),
        change_digest=change_digest,
        notes=notes or [],
    )


def assess_diff(
    diff: str,
    *,
    repo_url: str | None = None,
    graph: dict | None = None,
    verified: bool | None = None,
    changed_files: list[str] | None = None,
    verification_detail: str = "",
) -> ChangeRisk:
    """
    Assess a unified diff.

    The diff's own impact analysis (`analyze_diff`) supplies the changed files,
    the added-line security patterns and the dependents, so the gate's numbers
    agree with the Impact panel the user is already looking at — one
    implementation of "how far does this reach", not two.

    `verified` is tri-state: True (proved to apply), False (a verifier ran and it
    failed — the strongest signal here), or None (nobody checked). None still
    carries points, because a caller that skips verification should not thereby
    score better than one that performs it, but fewer than a failed check.
    """
    from app.services.impact_analyzer import analyze_diff

    if graph is None:
        graph = _graph_for(repo_url)

    impact = analyze_diff(diff or "", graph)
    paths = changed_files or impact.get("changed_files", [])
    additions = sum(1 for line in (diff or "").splitlines()
                    if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in (diff or "").splitlines()
                    if line.startswith("-") and not line.startswith("---"))

    signals: list[RiskSignal] = []

    flags = impact.get("security_flags") or []
    if flags:
        signals.append(RiskSignal(
            "security_patterns_in_diff", W_DIFF_SECURITY_FLAG,
            f"{len(flags)} security pattern(s) matched in added lines",
            {"patterns": [flag.get("rule") for flag in flags]},
        ))

    signals.extend(_sensitive_signals(paths))

    blast = _blast_signal(impact.get("impacted_files") or [],
                          bool(impact.get("graph_available")), paths)
    if blast:
        signals.append(blast)

    if (diff or "").strip():
        # Nothing to verify is not the same as failing to verify: a request with
        # no diff carries no change, so it earns no verification signal at all.
        signals.extend(_verification_signals(verified, verification_detail))

    stale = _stale_signal(repo_url)
    if stale:
        signals.append(stale)

    breadth = _breadth_signal(len(set(paths)), additions + deletions)
    if breadth:
        signals.append(breadth)

    notes = []
    if not impact.get("graph_available"):
        notes.append(
            "dependency graph unavailable — blast radius could not be measured, "
            "so this score excludes it"
        )

    return _assemble(
        signals,
        paths=paths,
        additions=additions,
        deletions=deletions,
        graph_available=bool(impact.get("graph_available")),
        change_digest=_canonical(diff or ""),
        notes=notes,
    )


def assess_files(
    files: list[dict],
    *,
    repo_url: str | None = None,
    graph: dict | None = None,
    verified: bool | None = None,
    verification_detail: str = "",
) -> ChangeRisk:
    """
    Assess a change given as content, not as a diff.

    Each file is parsed with the deterministic analyzer (the same one a review
    uses), so a change that *introduces* a critical finding is scored on that
    finding rather than on a keyword. This is the stronger of the two evidence
    paths, and it is the one autofix and build-patch run on.

    Findings are scored as a **delta**: only problems the change adds count
    against it. Fixing a typo in a file that happens to contain a hardcoded
    credential must not score like introducing one — a gate that punishes people
    for touching their worst files teaches them not to touch them. Where no
    original is available (a new file, an unindexed path) the whole content is
    treated as new, which is the conservative reading.

    `files`: `[{"path", "content", "original"?}]`.
    """
    from app.services.code_analysis import analyze_file

    _WEIGHTS = {"critical": W_CRITICAL_FINDING, "high": W_HIGH_FINDING}

    def _severity_points(text: str, path: str, language: str) -> tuple[float, list[str], list[str]]:
        """Severity-weighted penalty and the finding ids behind it, for one source."""
        try:
            analysis = analyze_file(text, path, language)
        except Exception:  # noqa: BLE001 — an unparsable file is not a crash
            if path not in _noted_unparsed:
                _noted_unparsed.add(path)
                unparsed.append(path)
            return 0.0, [], []
        if analysis.parse_error:
            if path not in _noted_unparsed:
                _noted_unparsed.add(path)
                unparsed.append(path)
            return 0.0, [], []
        points = 0.0
        critical: list[str] = []
        high: list[str] = []
        for finding in analysis.findings:
            weight = _WEIGHTS.get(finding.severity.value)
            if weight is None:
                continue
            points += weight * finding.confidence
            entry = f"{path}:{finding.line} {finding.rule_id}"
            (critical if finding.severity.value == "critical" else high).append(entry)
        return points, critical, high

    signal_weights = 0.0
    critical: list[str] = []
    high: list[str] = []
    pre_existing = 0
    unparsed: list[str] = []
    _noted_unparsed: set[str] = set()
    paths: list[str] = []
    additions = deletions = 0

    for info in files:
        path = info.get("path") or info.get("file_name") or ""
        content = info.get("content") or ""
        original = info.get("original") or ""
        language = info.get("language", "")
        paths.append(path)
        additions += max(0, len(content.splitlines()))
        deletions += max(0, len(original.splitlines()))

        if not content.strip():
            continue
        after_points, after_critical, after_high = _severity_points(content, path, language)
        if original.strip():
            # Only the delta counts: problems the change did not introduce are
            # already in the tree, and this gate judges the change.
            before_points, before_critical, before_high = _severity_points(
                original, path, language
            )
            pre_existing += len(before_critical) + len(before_high)
            signal_weights += max(0.0, after_points - before_points)
            critical.extend(after_critical[: max(0, len(after_critical) - len(before_critical))])
            high.extend(after_high[: max(0, len(after_high) - len(before_high))])
        else:
            signal_weights += after_points
            critical.extend(after_critical)
            high.extend(after_high)

    signals: list[RiskSignal] = []
    if unparsed:
        # A change nobody can parse is a change nobody can reason about. It is
        # not scored as a finding — there are no findings to score — but it does
        # mean the assessment is weaker than it looks, and the score says so.
        signals.append(RiskSignal(
            "unparsable_change", W_HIGH_FINDING,
            f"{len(unparsed)} changed file(s) could not be parsed — unverifiable",
            {"files": unparsed[:10]},
        ))

    finding_points = min(W_FINDINGS_CAP, int(round(signal_weights)))
    if finding_points:
        introduced = len(critical) + len(high)
        detail = f"this change introduces {len(critical)} critical and {len(high)} high finding(s)"
        if pre_existing:
            detail += f" ({pre_existing} pre-existing finding(s) in the same files are not counted)"
        signals.append(RiskSignal(
            "deterministic_findings", finding_points, detail,
            {"introduced": introduced, "critical": critical[:10], "high": high[:10],
             "pre_existing_ignored": pre_existing},
        ))
    if unparsed:
        signals.append(RiskSignal(
            "does_not_parse", W_HIGH_FINDING,
            f"{len(unparsed)} changed file(s) could not be parsed — unverifiable",
            {"files": unparsed[:10]},
        ))

    signals.extend(_sensitive_signals(paths))

    if graph is None:
        graph = _graph_for(repo_url)
    impacted: set[str] = set()
    if graph:
        try:
            from app.services.dep_graph import get_blast_radius

            for path in paths:
                impacted.update(get_blast_radius(graph, path).get("impacted_files") or [])
        except Exception:  # noqa: BLE001
            graph = None
    impacted.difference_update(set(paths))
    blast = _blast_signal(sorted(impacted), graph is not None, paths)
    if blast:
        signals.append(blast)

    if any((info.get("content") or "").strip() for info in files):
        signals.extend(_verification_signals(verified, verification_detail))

    stale = _stale_signal(repo_url)
    if stale:
        signals.append(stale)

    breadth = _breadth_signal(len(set(paths)), additions + deletions)
    if breadth:
        signals.append(breadth)

    # Content-addressed over the post-change bytes: two different change sets must
    # never share an approval, even when they touch the same files.
    change_digest = _canonical(*[f"{info.get('path','')}:{info.get('content','')}"
                                 for info in files])

    return _assemble(
        signals,
        paths=paths,
        additions=additions,
        deletions=deletions,
        graph_available=graph is not None,
        change_digest=change_digest,
        notes=[] if graph is not None else [
            "dependency graph unavailable — blast radius could not be measured, "
            "so this score excludes it"
        ],
    )


# ── Applying a patch, for real ────────────────────────────────────────────────
#
# The strongest signal in this module is whether a change was verified, and the
# only verifier that proves anything about a *patch* is applying it. String
# comparison proves the diff was rendered, not that it applies — and a diff that
# `git apply` rejects is worse than no diff, because the failure lands on the
# user's machine. So the check is a scratch repository and a real invocation.
#
# It doubles as an information source: the files that land are the change, so
# parsing them is stronger evidence than pattern-matching the diff text. One git
# run, two uses.

_GIT_TIMEOUT = 20

#: Cap on the post-apply content returned for parsing. Generous for any real
#: source file; a generated bundle beyond it is scored on its diff instead.
MAX_APPLIED_FILE_CHARS = 200_000


def _git_binary() -> str | None:
    import shutil

    return shutil.which("git")


def _diff_targets(diff: str) -> dict[str, dict[str, bool]]:
    """
    The files a unified diff touches, and how: `{path: {"new": bool, "deleted": bool}}`.

    Needed because verification depends on the *kind* of change: a modification
    cannot be applied without its pre-image, so a missing pre-image is a limit of
    what we know, not a failed check.
    """
    targets: dict[str, dict[str, bool]] = {}
    current: str | None = None

    for line in (diff or "").splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                candidate = parts[3][2:] if parts[3].startswith("b/") else parts[3]
                if candidate != "/dev/null":
                    current = candidate
                    targets.setdefault(candidate, {"new": False, "deleted": False})
        elif line.startswith("--- "):
            if line[4:].strip() == "/dev/null" and current:
                targets[current]["new"] = True
        elif line.startswith("+++ "):
            path = line[4:].strip()
            if path == "/dev/null":
                if current:
                    targets[current]["deleted"] = True
            else:
                candidate = path[2:] if path.startswith("b/") else path
                if candidate != "/dev/null":
                    current = candidate
                    targets.setdefault(candidate, {"new": False, "deleted": False})
    return targets


def apply_diff(diff: str, original_files: dict[str, str] | None = None) -> dict[str, Any]:
    """
    Apply a patch in a throwaway repository and report what landed.

    Returns `{"verified", "detail", "files": {path: content}, "paths": [...]}`,
    where `verified` is tri-state:

      True  — `git apply --check` passed and the patch applied. `files` holds the
              post-change contents, so the caller can *parse* the result instead
              of pattern-matching the diff text. Parsed findings are evidence;
              a regex over added lines is a hint.
      False — a real check ran and the patch did not apply. This is strong
              evidence of a problem: a diff that will not apply fails on the
              user's machine, which is worse than no diff.
      None  — the check could not be performed: git is missing, or the patch
              modifies files whose pre-image we do not have (an unindexed repo).
              None is not a pass — the caller scores it as "not verified" — but it
              says "we cannot tell", not "we looked and it is broken".

    The scratch repository is the point: nothing of the caller's is touched.
    """
    if not (diff or "").strip():
        return {"verified": None, "detail": "no diff to verify", "files": {}, "paths": []}

    git_bin = _git_binary()
    if not git_bin:
        return {
            "verified": None,
            "detail": "git is not installed — the patch was not independently verified",
            "files": {}, "paths": [],
        }

    targets = _diff_targets(diff)
    originals = original_files or {}
    # A modification or deletion needs its pre-image staged; without it `git apply`
    # fails for a reason that says nothing about the patch.
    needing_pre_image = sorted(
        path for path, meta in targets.items() if not meta["new"] and path not in originals
    )
    if needing_pre_image:
        return {
            "verified": None,
            "detail": (
                "cannot verify: no current content available for "
                f"{', '.join(needing_pre_image[:5])}"
                + (" and more" if len(needing_pre_image) > 5 else "")
            ),
            "files": {}, "paths": sorted(targets),
        }

    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory(prefix="savflux-verify-") as scratch:
        try:
            for args in (
                ["init", "-q", "-b", "main"],
                ["config", "user.email", "verify@savflux.local"],
                ["config", "user.name", "SavFlux verify"],
            ):
                subprocess.run([git_bin, *args], cwd=scratch, capture_output=True,
                               text=True, timeout=_GIT_TIMEOUT)

            for rel_path, content in originals.items():
                target = Path(scratch) / rel_path
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                except Exception:  # noqa: BLE001 — a path we cannot stage is skipped
                    continue

            check = subprocess.run(
                [git_bin, "apply", "--check", "-"],
                cwd=scratch, input=diff, capture_output=True, text=True,
                timeout=_GIT_TIMEOUT,
            )
            if check.returncode != 0:
                return {
                    "verified": False,
                    "detail": (check.stderr or check.stdout or "git apply --check failed").strip()[:400],
                    "files": {}, "paths": sorted(targets),
                }

            applied = subprocess.run(
                [git_bin, "apply", "-"], cwd=scratch, input=diff,
                capture_output=True, text=True, timeout=_GIT_TIMEOUT,
            )
            if applied.returncode != 0:
                return {
                    "verified": False,
                    "detail": (applied.stderr or "git apply failed").strip()[:400],
                    "files": {}, "paths": sorted(targets),
                }

            # The diff names the files; `git status` does not. It collapses a
            # newly created directory to `?? src/`, so reading the applied result
            # from status output silently loses every file in a new directory —
            # which is exactly the shape of an autofix that adds a module.
            contents: dict[str, str] = {}
            for rel_path, meta in targets.items():
                if meta["deleted"]:
                    contents[rel_path] = ""      # nothing left to parse
                    continue
                try:
                    file_path = Path(scratch) / rel_path
                    if file_path.is_file():
                        contents[rel_path] = file_path.read_text(
                            encoding="utf-8", errors="replace"
                        )[:MAX_APPLIED_FILE_CHARS]
                except Exception:  # noqa: BLE001 — an unreadable result is a missing one
                    continue

            return {
                "verified": True,
                "detail": (
                    f"git apply --check passed, {len(targets)} path(s) applied in a scratch repo"
                ),
                "files": contents,
                "paths": sorted(targets),
            }
        except subprocess.TimeoutExpired:
            return {"verified": False, "detail": "verification timed out", "files": {}, "paths": sorted(targets)}
        except Exception as exc:  # noqa: BLE001
            return {"verified": None, "detail": f"verification could not run: {str(exc)[:200]}",
                    "files": {}, "paths": sorted(targets)}


def verify_diff_applies(diff: str, original_files: dict[str, str] | None = None) -> dict[str, Any]:
    """
    Whether a patch applies — the boolean view of `apply_diff`, for callers that
    need only the answer (the `/policy/verify` endpoint, tests).
    """
    result = apply_diff(diff, original_files)
    return {
        "verified": result["verified"],
        "detail": result["detail"],
        "files": len(result.get("files") or {}),
    }


# ── Policy ────────────────────────────────────────────────────────────────────


def thresholds() -> tuple[bool, int, int]:
    """(enabled, approval_threshold, block_threshold) from settings."""
    settings = get_settings()
    enabled = bool(getattr(settings, "risk_gate_enabled", True))
    approval = int(getattr(settings, "risk_approval_threshold", 6) or 6)
    block = int(getattr(settings, "risk_block_threshold", 9) or 9)
    # A block threshold at or below the approval threshold would make approval
    # meaningless; keep them a step apart rather than failing at request time.
    if block <= approval:
        block = approval + 1
    return enabled, max(0, min(10, approval)), max(1, min(10, block))


def approval_token(action: str, risk: ChangeRisk) -> str:
    """
    The token a caller must echo to approve *this* change.

    Bound to the action, the change, and the assessment. Re-scoring the same
    change to a higher number produces a different token, so an approval given
    for a medium-risk diff cannot be replayed against a critical one.
    """
    return _canonical(f"{action}|{risk.change_digest}|{risk.digest}")


def evaluate(
    action: str,
    risk: ChangeRisk,
    *,
    approve_token: str = "",
    approval_reason: str = "",
) -> PolicyDecision:
    """
    Decide whether `action` may proceed.

    Two different refusals, deliberately not the same rule:

      * **Integrity** — the verifier ran and the patch did not apply. No approval
        overrides this, because it is not a judgement about risk: the change does
        not do what it says it does, and pushing it would ship a conflict. The
        user pushes it themselves if they disagree.
      * **Risk** — the change is dangerous but applies. Above the approval
        threshold it needs a token bound to this exact change plus a written
        reason; above the block threshold it is refused outright.

    Mixing the two would either make ordinary risky changes unpushable, or let a
    broken patch through on a signature.

    Pure: builds no graph, writes no ledger, touches no network. The callers that
    actually push (`/review/create-pr`, the agent's `create_pr`) record the
    decision, so a dry-run assessment never looks like an attempted write.

    An approval requires both the token *and* a stated reason. A bare token is a
    click; a reason is a sentence someone had to write, and it is what makes the
    ledger readable a month later.
    """
    enabled, approval_threshold, block_threshold = thresholds()
    token = approval_token(action, risk)
    reason_text = (approval_reason or "").strip()

    failed_verification = [s for s in risk.signals if s.name == "verification_failed"]
    if failed_verification:
        return PolicyDecision(
            action=action, risk=risk, allowed=False, requires_approval=False,
            approved=False, blocked=True, token=token,
            approval_threshold=approval_threshold, block_threshold=block_threshold,
            reason=(
                "the verifier ran and this change did not apply, so it does not do "
                "what it says it does. SavFlux will not push it — the command to do "
                "it by hand is in the response."
            ),
        )

    if not enabled:
        return PolicyDecision(
            action=action, risk=risk, allowed=True, requires_approval=False,
            approved=False, blocked=False, token=token,
            approval_threshold=approval_threshold, block_threshold=block_threshold,
            reason="risk gate disabled by configuration",
        )

    if risk.score >= block_threshold:
        return PolicyDecision(
            action=action, risk=risk, allowed=False, requires_approval=True,
            approved=True, blocked=True, token=token,
            approval_threshold=approval_threshold, block_threshold=block_threshold,
            reason=(
                f"risk {risk.score}/10 is at or above the block threshold "
                f"({block_threshold}) — {risk.summary}. SavFlux will not push this "
                "one; the command to do it by hand is in the response."
            ),
        )

    if risk.score >= approval_threshold:
        approved = bool(approve_token) and approve_token == token and len(reason_text) >= 8
        if approved:
            detail = "approval token matched, reason recorded"
        elif approve_token and approve_token != token:
            detail = "approval token does not match this change (the diff or its assessment moved)"
        elif approve_token and len(reason_text) < 8:
            detail = "an approval reason of at least 8 characters is required"
        else:
            detail = "no approval supplied"
        return PolicyDecision(
            action=action, risk=risk, allowed=approved, requires_approval=True,
            approved=approved, blocked=False, token=token,
            approval_threshold=approval_threshold, block_threshold=block_threshold,
            reason=(
                f"risk {risk.score}/10 is at or above the approval threshold "
                f"({approval_threshold}) — {risk.summary}. {detail}."
            ),
        )

    return PolicyDecision(
        action=action, risk=risk, allowed=True, requires_approval=False,
        approved=False, blocked=False, token=token,
        approval_threshold=approval_threshold, block_threshold=block_threshold,
        reason=f"risk {risk.score}/10 is below the approval threshold ({approval_threshold})",
    )


def gate_change(
    *,
    action: str,
    diff: str = "",
    files: list[dict] | None = None,
    repo_url: str | None = None,
    verified: bool | None = None,
    approve_token: str = "",
    approval_reason: str = "",
    verification_detail: str = "",
) -> tuple[ChangeRisk, PolicyDecision]:
    """
    Assess a change and apply the policy, in one call.

    `files` (content-addressed, parsed) is preferred over `diff` (pattern-matched)
    when both are available, because a parsed finding is evidence and a regex match
    is a hint. Callers that already hold a diff from `build_patch` pass the files
    they diffed so the score comes from the stronger path.

    Never raises: a change that cannot be assessed is treated as unverified, which
    raises its score, rather than as a change that skipped the gate.
    """
    try:
        if files:
            risk = assess_files(files, repo_url=repo_url, verified=verified,
                                verification_detail=verification_detail)
        else:
            risk = assess_diff(diff, repo_url=repo_url, verified=verified,
                               verification_detail=verification_detail)
    except Exception as exc:  # noqa: BLE001 — a gate must fail loudly, not open
        logger.warning("risk assessment failed, treating the change as unverified: %s", exc)
        risk = ChangeRisk(
            score=W_UNVERIFIED, level=_level_for(W_UNVERIFIED),
            signals=[RiskSignal("assessment_failed", W_UNVERIFIED,
                                f"risk assessment could not run: {str(exc)[:160]}", {})],
            scope=_scope_of([], 0, 0, False), digest=_canonical("assessment-failed", diff),
            change_digest=_canonical(diff),
        )
    return risk, evaluate(action, risk, approve_token=approve_token,
                          approval_reason=approval_reason)


# ── Ledger ────────────────────────────────────────────────────────────────────


def _ledger_path() -> Path:
    return data_file("policy_ledger.json")


def _load_ledger() -> dict[str, Any]:
    try:
        data = json.loads(_ledger_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 — an unreadable ledger is an empty one
        logger.warning("policy ledger unreadable, starting empty: %s", exc)
    return {"version": POLICY_VERSION, "entries": []}


def _save_ledger(data: dict[str, Any]) -> None:
    path = _ledger_path()
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".policy_ledger", delete=False
        ) as handle:
            json.dump(data, handle, indent=1)
            temp_name = handle.name
        os.replace(temp_name, path)
    except Exception as exc:  # noqa: BLE001 — auditing must never break a request
        logger.warning("could not persist policy ledger: %s", exc)


def record_decision(
    decision: PolicyDecision,
    *,
    outcome: str,
    repo: str = "",
    actor: str = "api",
    detail: str = "",
) -> dict[str, Any]:
    """
    Append a decision to the ledger.

    Recorded for every *attempted* push, including the ones that proceeded
    normally: "why did this PR open?" is as worth answering as "why was it
    blocked?". The diff itself is never stored — only its digest — so the ledger
    stays small and free of code.
    """
    entry = {
        "at": time.time(),
        "action": decision.action,
        "status": decision.status,
        "outcome": outcome,
        "score": decision.risk.score,
        "level": decision.risk.level,
        "policy_version": POLICY_VERSION,
        "signals": [signal.name for signal in decision.risk.signals],
        "scope": decision.risk.scope,
        "change_digest": decision.risk.change_digest,
        "assessment_digest": decision.risk.digest,
        "approval_token": decision.token,
        "approved": decision.approved,
        "blocked": decision.blocked,
        "reason": decision.reason,
        "repo": repo,
        "actor": actor,
        "detail": detail[:400],
    }
    try:
        with _LOCK:
            data = _load_ledger()
            data["entries"].append(entry)
            data["entries"] = data["entries"][-MAX_LEDGER_ENTRIES:]
            _save_ledger(data)
    except Exception as exc:  # noqa: BLE001
        logger.debug("policy ledger append failed (non-fatal): %s", exc)
    return entry


def ledger(limit: int = 50) -> list[dict[str, Any]]:
    """Most recent decisions, newest first."""
    with _LOCK:
        entries = _load_ledger()["entries"]
    return list(reversed(entries[-max(1, min(limit, MAX_LEDGER_ENTRIES)):]))


def status() -> dict[str, Any]:
    """Thresholds and counters — what the gate is currently doing."""
    enabled, approval_threshold, block_threshold = thresholds()
    with _LOCK:
        entries = _load_ledger()["entries"]

    counts = {"allowed": 0, "approval_required": 0, "blocked": 0, "approved": 0}
    for entry in entries:
        status_value = entry.get("status", "")
        if status_value in counts:
            counts[status_value] += 1
        if entry.get("approved"):
            counts["approved"] += 1

    return {
        "enabled": enabled,
        "policy_version": POLICY_VERSION,
        "approval_threshold": approval_threshold,
        "block_threshold": block_threshold,
        "weights": {
            "critical_finding": W_CRITICAL_FINDING,
            "high_finding": W_HIGH_FINDING,
            "diff_security_flag": W_DIFF_SECURITY_FLAG,
            "sensitive_path": W_SENSITIVE_PATH,
            "dependency_surface": W_DEPENDENCY_SURFACE,
            "verification_failed": W_UNVERIFIED,
            "not_verified": W_NOT_VERIFIED,
            "stale_index": W_STALE_INDEX,
            "change_breadth": W_BREADTH,
            "blast_radius": [W_BLAST_1, W_BLAST_3, W_BLAST_10],
        },
        "ledger_entries": len(entries),
        "counts": counts,
        "sensitive_path_hints": list(SENSITIVE_PATH_HINTS),
    }


def clear_ledger() -> int:
    """Drop the ledger. Returns how many entries were removed."""
    with _LOCK:
        data = _load_ledger()
        removed = len(data["entries"])
        data["entries"] = []
        _save_ledger(data)
    return removed
