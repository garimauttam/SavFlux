"""
Tests for the risk policy gate.

The gate decides whether an irreversible thing happens, so the assertions here
are about the properties that make it *safe to have*, not about the exact score
any particular change receives:

  * polarity — a higher score must mean more risk, everywhere. Two other
    "risk_score" values in this codebase run the other way (10 = healthy), and
    mixing them up would invert the gate rather than break it.
  * an approval is bound to one change — re-score, edit, or reorder and the token
    stops matching
  * the block threshold cannot be talked around — no token, no reason, no
    approval overrides it
  * a decision is explainable — every point names a signal with evidence
  * verification is real — `git apply` in a scratch repo, and a failure to verify
    is reported as a failure rather than absorbed
  * the gate never fails open — an exception in assessment must not let a change
    through unchallenged
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.services import risk_policy
from app.services.risk_policy import (
    ACTION_CREATE_PR,
    ChangeRisk,
    PolicyDecision,
    RiskSignal,
    approval_token,
    assess_diff,
    assess_files,
    evaluate,
    gate_change,
    ledger,
    record_decision,
    status,
    verify_diff_applies,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

ORDINARY_CHANGE = """\
import logging

logger = logging.getLogger(__name__)


def render_row(values):
    return ", ".join(str(v) for v in values)
"""

#: Measured against the real analyzer: PY-SEC-HARDCODED (critical, 0.85),
#: PY-SEC-NOVERIFY (high, 0.95), PY-SEC-WEAKHASH (medium, 0.75) → 5 points.
RISKY_CHANGE = """\
import hashlib
import requests

TOKEN_SALT = "hardcoded-salt"


def fetch(url, token):
    return requests.get(url, verify=False, timeout=5).json()


def digest(token):
    return hashlib.md5((token + TOKEN_SALT).encode()).hexdigest()
"""

#: A patch whose *added lines* match the diff-level security patterns
#: (shell-execution and unsafe-deserialization) without a parser run — the weaker
#: evidence path, so it can be compared against the stronger one.
RISKY_ADDED_LINES = """\
import subprocess
import yaml


def run_report(path):
    return subprocess.run(f"wc -l {path}", shell=True, capture_output=True)


def load_settings(payload):
    return yaml.load(payload)
"""


def settings(**overrides) -> SimpleNamespace:
    base = {
        "risk_gate_enabled": True,
        "risk_approval_threshold": 6,
        "risk_block_threshold": 9,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def with_settings(**overrides):
    """Patch the settings the gate reads, for the duration of a test."""
    return patch("app.services.risk_policy.get_settings", return_value=settings(**overrides))


def diff_for(path: str, body: str, original: str = "def existing():\n    return 1\n") -> str:
    """A real unified diff, built by the same code path the product uses."""
    from app.services.patch_service import FileChange, build_patch

    return build_patch([FileChange(path, original, body)]).diff


# ── Polarity ──────────────────────────────────────────────────────────────────


def test_a_higher_score_means_more_risk():
    """
    This codebase already has two things called risk_score: FileAnalysis.risk_score()
    is 1–10 where 10 is *healthy*, and the impact analyzers are higher-is-worse.
    ChangeRisk is specifically higher-is-worse, and this test pins that so a future
    refactor cannot quietly invert the gate.
    """
    ordinary = assess_files([{"path": "src/utils/rows.py", "content": ORDINARY_CHANGE,
                              "original": ""}], verified=True)
    risky = assess_files([{"path": "src/auth/tokens.py", "content": RISKY_CHANGE,
                           "original": ""}], verified=False)

    assert risky.score > ordinary.score
    assert risky.level in ("high", "critical")
    assert ordinary.level == "low"


def test_the_level_tracks_the_score():
    for score, expected in ((0, "low"), (3, "medium"), (6, "high"), (9, "critical"), (10, "critical")):
        risk = ChangeRisk(score=score, level=risk_policy._level_for(score),
                          signals=[], scope={}, digest="")
        assert risk.level == expected


# ── Signals ───────────────────────────────────────────────────────────────────


def test_a_parsed_critical_finding_outweighs_a_keyword_match():
    """
    Evidence graded by strength: a finding proved by the parser versus a regex
    that matched an added line. The parser must win, or the score is just
    pattern-matching with extra steps.
    """
    parsed = assess_files([{"path": "net.py", "content": RISKY_CHANGE, "original": ""}],
                          verified=True)
    names = {signal.name for signal in parsed.signals}
    assert "deterministic_findings" in names

    # The same kinds of problem, described only as added text — no parse, so no
    # findings signal, only the diff-level pattern match.
    text_only = assess_diff(diff_for("net.py", RISKY_ADDED_LINES), verified=True)
    text_names = {signal.name for signal in text_only.signals}
    assert "deterministic_findings" not in text_names
    assert "security_patterns_in_diff" in text_names

    finding_weight = next(s.weight for s in parsed.signals if s.name == "deterministic_findings")
    pattern_weight = next(s.weight for s in text_only.signals
                          if s.name == "security_patterns_in_diff")
    assert finding_weight > pattern_weight


def test_every_signal_carries_its_own_evidence():
    risk = assess_files([{"path": "src/auth/tokens.py", "content": RISKY_CHANGE, "original": ""}])
    assert risk.signals
    for signal in risk.signals:
        assert signal.detail, f"{signal.name} has no human-readable detail"
        assert isinstance(signal.weight, int) and signal.weight > 0


def test_a_security_sensitive_path_is_a_signal_on_its_own():
    risk = assess_files([{"path": "src/auth/session.py", "content": ORDINARY_CHANGE,
                          "original": ""}], verified=True)
    assert any(signal.name == "sensitive_path" for signal in risk.signals)


def test_dependency_and_build_files_are_called_out():
    risk = assess_files([{"path": "requirements.txt", "content": "requests==2.31.0\n",
                          "original": ""}])
    assert any(signal.name == "dependency_surface" for signal in risk.signals)


def test_a_failed_verification_outweighs_one_that_never_ran():
    """
    Three states, three scores. A check that ran and failed is evidence of a real
    problem; a check that never ran is an absence of evidence. They must not
    collapse into one number, and neither may be free.
    """
    files = [{"path": "src/auth/tokens.py", "content": ORDINARY_CHANGE, "original": ""}]
    verified = assess_files(files, verified=True)
    never = assess_files(files, verified=None)
    failed = assess_files(files, verified=False)

    assert verified.score < never.score < failed.score
    assert verified.score + risk_policy.W_NOT_VERIFIED == never.score
    assert never.score + (risk_policy.W_UNVERIFIED - risk_policy.W_NOT_VERIFIED) == failed.score
    assert [s.name for s in never.signals if "verif" in s.name or "verif" in s.detail] == ["not_verified"]
    assert [s.name for s in failed.signals if "verif" in s.name] == ["verification_failed"]


def test_a_verified_change_carries_no_verification_signal():
    risk = assess_files([{"path": "src/auth/tokens.py", "content": ORDINARY_CHANGE,
                          "original": ""}], verified=True)
    assert not any("verif" in signal.name for signal in risk.signals)


def test_a_change_that_does_not_parse_is_flagged():
    risk = assess_files([{"path": "broken.py", "content": "def f(:\n    pass\n", "original": ""}])
    assert any(signal.name == "unparsable_change" for signal in risk.signals)


def test_the_score_is_capped_at_ten():
    risk = assess_files(
        [{"path": f"src/auth/service_{i}.py", "content": RISKY_CHANGE, "original": ""}
         for i in range(12)],
        verified=False,
    )
    assert risk.score == 10


def test_a_large_change_is_flagged_for_breadth():
    files = [{"path": f"src/module_{i}.py", "content": "x = 1\n" * 60, "original": ""}
             for i in range(12)]
    risk = assess_files(files, verified=True)
    assert any(signal.name == "change_breadth" for signal in risk.signals)


def test_an_ordinary_small_change_fires_no_signals():
    risk = assess_files([{"path": "src/utils/formatting.py", "content": ORDINARY_CHANGE,
                          "original": ""}], verified=True)
    assert risk.signals == []
    assert risk.score == 0
    assert risk.summary == "risk 0/10 (low) — no risk signals fired"


# ── Approval binding ──────────────────────────────────────────────────────────


def _risky_change(*, verified: bool = True, path: str = "src/auth/tokens.py") -> ChangeRisk:
    """
    Measured: a provider-verified patch that introduces a hardcoded credential,
    a disabled TLS check and a weak hash into an auth file scores 7/10 — above the
    approval threshold (6) and below the block threshold (9). That is the case
    this whole module exists for: real, parsed problems, in a sensitive file, that
    a human should sign off on rather than have refused outright.
    """
    return assess_files([{"path": path, "content": RISKY_CHANGE, "original": ""}],
                        verified=verified)


def _unverifiable_risky_change() -> ChangeRisk:
    """The same change whose patch failed verification: 7 + 3 = blocked."""
    return assess_files([{"path": "src/auth/tokens.py", "content": RISKY_CHANGE,
                          "original": ""}], verified=False)


def test_a_score_below_the_threshold_needs_nothing():
    with with_settings():
        risk = assess_files([{"path": "src/utils/rows.py", "content": ORDINARY_CHANGE,
                              "original": ""}], verified=True)
        decision = evaluate(ACTION_CREATE_PR, risk)
    assert decision.allowed and not decision.requires_approval
    assert decision.status == "allowed"


def test_a_risky_change_requires_an_approval():
    with with_settings():
        risk = _risky_change()
        assert 6 <= risk.score < 9, f"fixture drifted: {risk.score}"
        decision = evaluate(ACTION_CREATE_PR, risk)
    assert not decision.allowed
    assert decision.requires_approval and not decision.blocked
    assert decision.status == "approval_required"
    assert decision.token


def test_the_correct_token_plus_a_reason_is_approved():
    with with_settings():
        risk = _risky_change()
        decision = evaluate(ACTION_CREATE_PR, risk, approve_token=approval_token(ACTION_CREATE_PR, risk),
                            approval_reason="Rotation tested in staging; reviewed with the team.")
    assert decision.allowed and decision.approved
    assert decision.status == "allowed"


def test_a_token_alone_is_not_an_approval():
    """A token is a click. The reason is the sentence someone had to write."""
    with with_settings():
        risk = _risky_change()
        decision = evaluate(ACTION_CREATE_PR, risk, approve_token=approval_token(ACTION_CREATE_PR, risk))
    assert not decision.allowed
    assert "reason" in decision.reason.lower()


def test_a_token_for_a_different_change_is_refused():
    """
    An approval cannot be replayed onto another diff. Both changes here are
    otherwise identical in shape — same findings, same verification state, same
    score — so only the change digest distinguishes them, which is the point.
    """
    with with_settings():
        a = assess_files([{"path": "src/auth/tokens.py", "content": RISKY_CHANGE, "original": ""}],
                         verified=True)
        b = assess_files([{"path": "src/auth/sessions.py", "content": RISKY_CHANGE, "original": ""}],
                         verified=True)
        assert a.score == b.score, "the two changes must differ only in what they touch"
        decision = evaluate(ACTION_CREATE_PR, b,
                            approve_token=approval_token(ACTION_CREATE_PR, a),
                            approval_reason="Approved after review with the platform team.")
    assert not decision.allowed
    assert "does not match" in decision.reason


def test_editing_the_change_invalidates_its_approval():
    """The whole point of binding the token to the bytes, not to the intent."""
    with with_settings():
        before = _risky_change()
        token = approval_token(ACTION_CREATE_PR, before)

        after = assess_files([{"path": "src/auth/tokens.py",
                               "content": RISKY_CHANGE + "\n# one more line\n",
                               "original": ""}], verified=False)
        decision = evaluate(ACTION_CREATE_PR, after, approve_token=token,
                            approval_reason="Approved after review with the platform team.")

    assert before.change_digest != after.change_digest
    assert not decision.allowed


def test_re_scoring_the_same_change_higher_invalidates_its_approval():
    """
    Same change, worse evidence. The assessment is part of the token, so an
    approval given for a 7/10 picture cannot be spent on a 7/10 picture *plus* a
    new signal — the approver agreed to the first one, not to this one.
    """
    with with_settings():
        agreed = _risky_change()                     # 7/10, approval required
        worse = ChangeRisk(                          # same bytes, one more signal
            score=8, level="high",
            signals=agreed.signals + [RiskSignal("new_evidence", 1, "found later", {})],
            scope=agreed.scope, digest="assessment-moved",
            change_digest=agreed.change_digest,
        )
        decision = evaluate(ACTION_CREATE_PR, worse,
                            approve_token=approval_token(ACTION_CREATE_PR, agreed),
                            approval_reason="Approved after review with the platform team.")

    assert worse.change_digest == agreed.change_digest   # the same change…
    assert worse.digest != agreed.digest                 # …assessed differently
    assert not decision.allowed
    assert "does not match" in decision.reason


# ── The block threshold ───────────────────────────────────────────────────────


def test_the_block_threshold_cannot_be_approved_around():
    """
    The escape hatch is not a bigger token — it is a human running the command.
    Anything else would make the threshold decorative.
    """
    with with_settings():
        risk = ChangeRisk(score=10, level="critical", signals=[], scope={}, digest="d")
        token = approval_token(ACTION_CREATE_PR, risk)
        decision = evaluate(ACTION_CREATE_PR, risk, approve_token=token,
                            approval_reason="I really do want this to go out anyway.")

    assert decision.blocked and not decision.allowed
    assert decision.status == "blocked"
    assert "will not push" in decision.reason


def test_a_blocked_decision_keeps_the_thresholds_visible():
    with with_settings():
        decision = evaluate(ACTION_CREATE_PR, ChangeRisk(score=9, level="critical",
                                                        signals=[], scope={}, digest="d"))
    assert decision.approval_threshold == 6
    assert decision.block_threshold == 9


def test_the_gate_can_be_disabled_without_changing_the_code():
    with with_settings(risk_gate_enabled=False):
        decision = evaluate(ACTION_CREATE_PR, ChangeRisk(score=10, level="critical",
                                                        signals=[], scope={}, digest="d"))
    assert decision.allowed and not decision.blocked
    assert "disabled" in decision.reason


def test_a_block_threshold_below_the_approval_threshold_is_corrected():
    """
    A misconfiguration that would make approval meaningless is repaired rather
    than obeyed — and the response reports the thresholds actually in force.
    """
    with with_settings(risk_approval_threshold=7, risk_block_threshold=3):
        enabled, approval_threshold, block_threshold = risk_policy.thresholds()
    assert enabled
    assert block_threshold > approval_threshold


# ── Assessment never fails open ───────────────────────────────────────────────


def test_an_assessment_error_is_treated_as_unverified_rather_than_allowed():
    """A gate that crashes must not become a gate that opens."""
    with patch("app.services.risk_policy.assess_files", side_effect=RuntimeError("boom")):
        risk, decision = gate_change(action=ACTION_CREATE_PR, files=[{"path": "x.py", "content": "x"}])

    assert any(signal.name == "assessment_failed" for signal in risk.signals)
    assert risk.score >= risk_policy.W_UNVERIFIED


def test_a_change_with_no_files_and_no_diff_is_still_assessed():
    risk = assess_files([], verified=True)
    assert risk.score == 0
    assert risk.scope["file_count"] == 0


# ── Verification ──────────────────────────────────────────────────────────────


def test_verification_applies_a_real_patch_in_a_scratch_repo():
    original = "def add(a, b):\n    return a + b\n"
    modified = "def add(a, b):\n    \"\"\"Sum two numbers.\"\"\"\n    return a + b\n"
    diff = diff_for("maths.py", modified, original)

    result = verify_diff_applies(diff, {"maths.py": original})

    assert result["verified"] is True
    assert result["files"] == 1
    assert "git apply" in result["detail"]


def test_verification_reports_a_patch_that_does_not_apply():
    """A diff that cannot land must be reported, not absorbed into a pass."""
    diff = diff_for("maths.py", "def add(a, b):\n    return a + b + 1\n",
                    "def add(a, b):\n    return a + b\n")
    # The file on disk is different from the one the diff was built against.
    result = verify_diff_applies(diff, {"maths.py": "def add(a, b):\n    return 0\n"})

    assert result["verified"] is False
    assert result["detail"]


def test_an_empty_diff_is_reported_as_nothing_to_verify():
    """"We could not check" and "we checked and it broke" must not look alike."""
    result = verify_diff_applies("")
    assert result["verified"] is None
    assert "no diff" in result["detail"]


def test_a_machine_without_git_reports_unverified_rather_than_verified(monkeypatch):
    """
    An infrastructure limitation is not a pass and not a failure. Reporting True
    here would be claiming a check that never ran; reporting False would call the
    patch broken. The gate is told "unknown", and trades that for 2 points rather
    than 0.
    """
    monkeypatch.setattr(risk_policy, "_git_binary", lambda: None)
    result = verify_diff_applies("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")
    assert result["verified"] is None
    assert "git is not installed" in result["detail"]


def test_a_modification_without_its_pre_image_is_unverifiable_not_broken():
    """
    `git apply` fails on a modification whose original we do not have — but that
    is our missing information, not a defect in the patch, and reporting it as a
    failure would raise the risk of a change nobody managed to check.
    """
    diff = diff_for("maths.py", "def add(a, b):\n    return a + b + 1\n",
                    "def add(a, b):\n    return a + b\n")
    result = risk_policy.apply_diff(diff, {})
    assert result["verified"] is None
    assert "no current content" in result["detail"]


def test_a_new_file_is_verifiable_without_a_pre_image():
    """An added file has no pre-image to miss, so the check is meaningful."""
    from app.services.patch_service import FileChange, build_patch

    diff = build_patch([FileChange("src/auth/tokens.py", None, RISKY_CHANGE)]).diff
    result = risk_policy.apply_diff(diff, {})
    assert result["verified"] is True
    # The applied content comes back, which is what the gate parses.
    assert "TOKEN_SALT" in result["files"]["src/auth/tokens.py"]


def test_verification_does_not_touch_the_working_tree(tmp_path):
    """The scratch repo is the point: a check must not modify anything of ours."""
    from app.services.patch_service import FileChange, build_patch

    real = tmp_path / "real.py"
    real.write_text("def f():\n    return 1\n", encoding="utf-8")
    before = real.read_text(encoding="utf-8")

    diff = build_patch([FileChange("real.py", before, "def f():\n    return 2\n")]).diff
    verify_diff_applies(diff, {"real.py": before})

    assert real.read_text(encoding="utf-8") == before


# ── Ledger ────────────────────────────────────────────────────────────────────


def test_a_decision_is_recorded_with_its_reasoning(isolated_data_dir):
    risk = _risky_change()
    with with_settings():
        decision = evaluate(ACTION_CREATE_PR, risk)
    record_decision(decision, outcome="refused_no_approval", repo="octo/demo",
                    actor="agent", detail="unit test")

    entries = ledger()
    assert entries
    entry = entries[0]
    assert entry["status"] == "approval_required"
    assert entry["score"] == risk.score
    assert entry["signals"]
    assert entry["change_digest"] == risk.change_digest
    assert entry["repo"] == "octo/demo"
    assert entry["actor"] == "agent"
    # The diff itself is never stored — only its digest.
    assert "patch" not in entry and "diff" not in entry


def test_the_ledger_keeps_the_most_recent_entries(isolated_data_dir):
    risk = ChangeRisk(score=1, level="low", signals=[], scope={}, digest="d")
    for i in range(risk_policy.MAX_LEDGER_ENTRIES + 5):
        record_decision(PolicyDecision(
            action=ACTION_CREATE_PR, risk=risk, allowed=True, requires_approval=False,
            approved=False, blocked=False, token=f"t{i}", approval_threshold=6,
            block_threshold=9, reason=f"entry {i}",
        ), outcome="allowed")

    entries = ledger(limit=500)
    assert len(entries) == risk_policy.MAX_LEDGER_ENTRIES
    assert entries[0]["reason"] == f"entry {risk_policy.MAX_LEDGER_ENTRIES + 4}"


def test_a_corrupt_ledger_is_an_empty_ledger_not_a_crash(isolated_data_dir):
    risk_policy._ledger_path().write_text("{ not json", encoding="utf-8")
    assert ledger() == []
    assert risk_policy.clear_ledger() == 0


def test_status_reports_the_policy_in_force(isolated_data_dir):
    payload = status()
    assert payload["approval_threshold"] < payload["block_threshold"]
    assert payload["weights"]["critical_finding"] > payload["weights"]["diff_security_flag"]
    assert "auth" in payload["sensitive_path_hints"]
    assert "counts" in payload


def test_the_ledger_never_breaks_the_request_that_wrote_it(isolated_data_dir, monkeypatch):
    """An audit trail that can fail a push is worse than no audit trail."""
    monkeypatch.setattr(risk_policy, "_save_ledger", lambda data: (_ for _ in ()).throw(OSError("disk full")))
    with with_settings():
        decision = evaluate(ACTION_CREATE_PR, _risky_change())
    entry = record_decision(decision, outcome="refused_no_approval")
    assert entry["action"] == ACTION_CREATE_PR


# ── Over HTTP, where a user actually meets the gate ───────────────────────────


def _create_pr(client, payload: dict):
    return client.post("/api/v1/review/create-pr", json=payload)


def test_policy_status_is_readable_without_a_key(client):
    """
    How the gate behaves is not a secret: someone deciding whether to trust it
    should be able to read the thresholds before handing over an API key.
    """
    response = client.get("/api/v1/policy")
    assert response.status_code == 200
    body = response.json()
    assert body["approval_threshold"] < body["block_threshold"]
    assert body["weights"]


def test_assess_previews_a_change_without_recording_an_attempt(isolated_data_dir, client):
    """
    Assessment is a question. If previewing a change wrote to the ledger, the
    audit trail would show attempts that never happened.
    """
    diff = diff_for("src/auth/tokens.py", RISKY_CHANGE * 3)
    response = client.post("/api/v1/policy/assess", json={"diff": diff})

    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] in ("allowed", "approval_required", "blocked")
    assert body["risk"]["signals"]
    assert ledger() == []


def test_assess_rejects_a_change_that_was_not_supplied(isolated_data_dir, client):
    response = client.post("/api/v1/policy/assess", json={})
    assert response.status_code == 400


def test_assess_rejects_an_unknown_action(isolated_data_dir, client):
    response = client.post("/api/v1/policy/assess", json={"diff": "x", "action": "delete_repo"})
    assert response.status_code == 422


def test_verify_endpoint_proves_a_real_patch_applies(isolated_data_dir, client):
    original = "def add(a, b):\n    return a + b\n"
    diff = diff_for("maths.py", "def add(a, b):\n    return a + b  # sums\n", original)

    response = client.post("/api/v1/policy/verify",
                           json={"diff": diff, "originals": {"maths.py": original}})
    assert response.status_code == 200
    assert response.json()["verified"] is True


def _new_file_diff(path: str, content: str) -> str:
    """A patch that adds a file — the shape an autofix takes when it writes one."""
    from app.services.patch_service import FileChange, build_patch

    return build_patch([FileChange(path, None, content)]).diff


def test_create_pr_refuses_a_risky_change_and_explains_why(isolated_data_dir, client):
    """
    End to end, over HTTP: a patch that adds an auth module containing a hardcoded
    credential is proved to apply, parsed, and then stopped — the endpoint answers
    with the signals, the score, and the command instead of the push.
    """
    from app.services.patch_service import digest_of

    diff = _new_file_diff("src/auth/tokens.py", RISKY_CHANGE)
    response = _create_pr(client, {
        "repo": "octo/demo", "head": "savflux/fix", "base": "main",
        "diff": diff, "confirm_digest": digest_of(diff),
    })

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "manual"
    assert body["policy"]["requires_approval"] is True
    assert body["policy"]["blocked"] is False
    assert "gh_command" in body
    # The score comes from parsing the file the patch produces — the same evidence
    # standard a review is held to, not a pattern match on the diff text.
    assert body["verification"]["verified"] is True
    assert body["risk"]["score"] >= 6
    assert "deterministic_findings" in [s["name"] for s in body["risk"]["signals"]]
    assert "nothing was pushed" in body["reason"].lower() or "approval" in body["reason"].lower()
    assert ledger()[0]["outcome"] == "refused_no_approval"


def test_a_patch_that_does_not_apply_is_refused_even_with_a_perfect_approval(isolated_data_dir, client):
    """
    The integrity rule, over HTTP. A patch the verifier proves does not apply is
    never pushed by SavFlux — not because it is risky, but because it does not do
    what it says it does. No signature changes that; the user pushes it by hand.
    """
    from app.services.patch_service import digest_of

    diff = _new_file_diff("src/auth/tokens.py", RISKY_CHANGE)
    # Corrupt the hunk header's line count: the patch is now internally
    # inconsistent, which is what a truncated or hand-edited patch looks like.
    corrupt = diff.replace("@@ -0,0 +1,", "@@ -0,0 +1,9999 @@\n", 1)
    assert corrupt != diff

    verification = risk_policy.apply_diff(corrupt, {})
    assert verification["verified"] is False, "the fixture must fail a real git apply"

    response = _create_pr(client, {
        "repo": "octo/demo", "head": "savflux/fix", "diff": corrupt,
        "confirm_digest": digest_of(corrupt),
    })

    body = response.json()
    assert body["status"] == "manual"
    assert body["policy"]["blocked"] is True
    assert body["policy"]["requires_approval"] is False
    assert "did not apply" in body["reason"]
    assert ledger()[0]["outcome"] == "blocked"


def test_a_change_at_the_maximum_score_is_refused_outright(isolated_data_dir):
    """
    The block threshold is the last-resort state above the approval gate, and it
    is deliberately set at the maximum: a change that is dangerous on every axis
    at once is not one a signature should be able to wave through. Exercised at
    the policy level because reaching 10 requires every signal to fire at once,
    which is exactly why it is reserved for that case.
    """
    with with_settings():
        impossible = ChangeRisk(
            score=10, level="critical",
            signals=[RiskSignal("everything", 10, "every signal fired at once", {})],
            scope={}, digest="extreme",
        )
        decision = evaluate(
            ACTION_CREATE_PR, impossible,
            approve_token=approval_token(ACTION_CREATE_PR, impossible),
            approval_reason="I accept all of it, every risk, please push anyway.",
        )
    assert decision.blocked and not decision.allowed
    assert "will not push" in decision.reason


def test_an_approval_does_not_replace_the_confirmation_digest(isolated_data_dir, client):
    """
    Two gates, not one: approving a risky change is not the same as confirming
    the diff, and the second requirement must survive the first being satisfied.
    """
    diff = diff_for("src/auth/tokens.py", ORDINARY_CHANGE)
    preview = _create_pr(client, {"repo": "octo/demo", "head": "savflux/fix", "diff": diff}).json()

    response = _create_pr(client, {
        "repo": "octo/demo", "head": "savflux/fix", "diff": diff,
        "approval_token": preview["policy"]["approval_token"],
        "approval_reason": "Reviewed the diff with the team before approving this change.",
    }).json()

    assert response["status"] == "manual"
    assert "Confirmation required" in response["reason"]


def test_build_patch_returns_the_assessment_with_the_patch(isolated_data_dir, client):
    """
    The caller should not have to ask twice: building a patch returns its score,
    and the token an approval would need, bound to that exact diff.
    """
    response = client.post("/api/v1/review/build-patch", json={
        "changes": [{"path": "src/auth/tokens.py", "content": RISKY_CHANGE}],
        "title": "Rotate the token salt",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["digest"]
    assert body["risk"]["score"] >= 6
    assert body["risk"]["signals"]
    assert body["policy"]["approval_token"]
    assert body["policy"]["requires_approval"] is True


def test_the_policy_ledger_is_clearable_only_with_a_key(isolated_data_dir, client):
    risk = ChangeRisk(score=1, level="low", signals=[], scope={}, digest="d")
    record_decision(PolicyDecision(
        action=ACTION_CREATE_PR, risk=risk, allowed=True, requires_approval=False,
        approved=False, blocked=False, token="t", approval_threshold=6,
        block_threshold=9, reason="test entry",
    ), outcome="allowed")

    assert client.get("/api/v1/policy/ledger").json()["entries"]
    assert client.delete("/api/v1/policy/ledger").status_code == 200
    assert client.get("/api/v1/policy/ledger").json()["entries"] == []


def test_assess_and_create_pr_reach_the_same_conclusion(isolated_data_dir, client):
    """
    A preview that disagrees with the gate is worse than no preview. Both
    endpoints apply the patch, parse the result and score it the same way, so the
    same change must not get two answers — the first version of this module
    scored a bare diff four points lower, which would have shown a user "allowed"
    and then refused the push.
    """
    from app.services.patch_service import digest_of

    diff = _new_file_diff("src/auth/tokens.py", RISKY_CHANGE)

    assessed = client.post("/api/v1/policy/assess", json={"diff": diff}).json()
    pushed = _create_pr(client, {
        "repo": "octo/demo", "head": "savflux/fix", "diff": diff,
        "confirm_digest": digest_of(diff),
    }).json()

    assert assessed["risk"]["score"] == pushed["risk"]["score"]
    assert assessed["verdict"] == pushed["policy"]["status"]
    assert [s["name"] for s in assessed["risk"]["signals"]] == \
           [s["name"] for s in pushed["risk"]["signals"]]
    assert assessed["policy"]["approval_token"] == pushed["policy"]["approval_token"]


def test_assess_reports_the_verification_it_performed(isolated_data_dir, client):
    diff = _new_file_diff("src/auth/tokens.py", RISKY_CHANGE)
    body = client.post("/api/v1/policy/assess", json={"diff": diff}).json()
    assert body["verification"]["verified"] is True
    assert "git apply" in body["verification"]["detail"]


def test_build_patch_verifies_and_scores_the_diff_it_returns(isolated_data_dir, client):
    """
    The dialog's job is to show one screen that is true. Verifying at build time
    is what makes the "verified" badge on that screen meaningful: by the time the
    user reads it, the same `git apply` that will gate the push has already run.
    """
    response = client.post("/api/v1/review/build-patch", json={
        "changes": [{"path": "src/auth/tokens.py", "content": RISKY_CHANGE}],
        "title": "Harden token handling",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["verification"]["verified"] is True
    assert body["verification"]["files"] == 1
    # Scored from the applied file, with the not_verified signal resolved.
    assert "not_verified" not in [s["name"] for s in body["risk"]["signals"]]
    assert body["risk"]["score"] == 7
    assert body["policy"]["status"] == "approval_required"
