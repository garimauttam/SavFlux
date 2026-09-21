"""
test_model_provenance.py — A benchmark result must say which models produced it.

WHY THIS FILE EXISTS
--------------------
`eval_rag.py` is a CI gate and had no tests. Its output was also missing the one
field that makes it a *benchmark* rather than a number: which models ran.

The immediate reason that matters is a planned change. The embedding model is
about to be swapped for a code-specialist one, and the whole question is whether
retrieval improves. Two JSON artefacts that both describe themselves as
"BM25 + RRF + cross-encoder reranking" cannot answer that question after the fact
— and a CI artifact stored for 30 days will outlive everyone's memory of which
run used which model.

The second reason is quieter. `citation_service` calibrates its trust thresholds
against the cross-encoder's raw logit range. Swapping the reranker does not error;
it keeps printing "high confidence" using thresholds from a model that is no
longer running. Naming the reranker as a module constant — and printing it — is
what makes that visible instead of mysterious.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── The reranker's name has one home ─────────────────────────────────────────


def _get_cross_encoder_code() -> str:
    """
    The body of `_get_cross_encoder`, with its docstring removed.

    Read from the FILE rather than by calling or introspecting the function,
    because this suite cannot reach the real one: `conftest.py` patches
    `app.services.reranker._get_cross_encoder` for the entire session inside a
    `with` block that stays open for every test, so that no test ever triggers an
    80 MB download from huggingface.co. Any attempt to call it here gets a
    MagicMock, which is exactly how the first version of this test failed —
    passing alone, and failing in the full suite.

    Reading the source is also the stronger assertion for what is actually being
    claimed: not "the right argument arrives at runtime" but "there is nowhere
    else for the name to come from".
    """
    import ast
    from pathlib import Path

    from app.services import reranker

    source = Path(reranker.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    func = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_get_cross_encoder"
    )

    body = list(func.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # the docstring names the model legitimately
    return "\n".join(ast.unparse(stmt) for stmt in body)


def test_the_reranker_model_is_a_module_constant():
    from app.services import reranker

    assert isinstance(reranker.RERANKER_MODEL, str) and reranker.RERANKER_MODEL


def test_get_cross_encoder_holds_no_model_literal_of_its_own():
    """
    The name must have exactly one home *structurally*, not merely agree today.

    Asserting `CrossEncoder(RERANKER_MODEL)` at runtime cannot catch the
    interesting case: if someone writes the literal back in, the value passed
    still equals the constant, so nothing fails. The defect shows up later, when
    the constant is updated and the literal is not — provenance then reports one
    model while the machine runs another.

    That is an equivalent mutation today and a real bug tomorrow, so it is worth
    asserting directly: the function must reference `RERANKER_MODEL`, and must
    contain no model id of its own.
    """
    import re

    code = _get_cross_encoder_code()

    assert "RERANKER_MODEL" in code, "_get_cross_encoder no longer uses RERANKER_MODEL"

    quoted = re.findall(r"['\"]([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)['\"]", code)
    assert not quoted, (
        f"_get_cross_encoder hardcodes a model id {quoted} instead of using "
        "RERANKER_MODEL — the constant and the running model can now drift apart"
    )


def test_the_reranker_is_still_the_documented_one():
    """
    Pinned like the default embedder, and expected to fail when it changes.

    `citation_service.HIGH_TRUST_SCORE` and `MEDIUM_TRUST_SCORE` are thresholds in
    *this* model's logit space. Changing the model without revisiting them is the
    kind of change that passes every test and quietly makes every trust level wrong.
    """
    from app.services.reranker import RERANKER_MODEL

    assert RERANKER_MODEL == "cross-encoder/ms-marco-MiniLM-L-6-v2"


# ── The benchmark reports its own provenance ─────────────────────────────────


@pytest.fixture()
def eval_rag_module():
    """
    Import `eval_rag` from the repo root.

    It is a script, not a package module, so the root goes on sys.path. Import is
    safe: the models inside it are built lazily inside functions, never at import.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import eval_rag

    return eval_rag


def _patch_settings(monkeypatch, **overrides):
    """Point eval_rag's config lookup at controlled settings."""
    from app.core import config as config_module

    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: Settings(_env_file=None, **overrides),
    )


def test_provenance_reports_the_configured_embedder(eval_rag_module, monkeypatch):
    _patch_settings(monkeypatch, llm_provider="ollama", embedding_model="org/code-model-x")
    provenance = eval_rag_module._model_provenance()
    assert provenance["embedder"] == "org/code-model-x"


def test_provenance_reports_the_device_and_batch_size(eval_rag_module, monkeypatch):
    """
    Batch size changes indexing speed, not vectors — so a latency regression with
    an unchanged embedder is diagnosable only if the batch size is recorded.
    """
    _patch_settings(
        monkeypatch,
        llm_provider="ollama",
        embedding_device="cpu",
        embedding_batch_size=8,
    )
    provenance = eval_rag_module._model_provenance()
    assert provenance["embedder_device"] == "cpu"
    assert provenance["embedder_batch_size"] == "8"


def test_provenance_reports_the_reranker(eval_rag_module, monkeypatch):
    _patch_settings(monkeypatch, llm_provider="ollama")
    from app.services.reranker import RERANKER_MODEL

    assert eval_rag_module._model_provenance()["reranker"] == RERANKER_MODEL


def test_provenance_names_the_openai_embedder_when_openai_is_active(
    eval_rag_module, monkeypatch
):
    """
    The local embedder setting is ignored on the paid path, so reporting it would
    be actively misleading — the vector store is being written by OpenAI.
    """
    _patch_settings(
        monkeypatch,
        llm_provider="openai",
        openai_api_key="sk-test",
        openai_embedding_model="text-embedding-3-small",
    )
    provenance = eval_rag_module._model_provenance()
    assert provenance["embedder"] == "text-embedding-3-small"
    assert "embedder_device" not in provenance


def test_provenance_never_breaks_a_report(eval_rag_module, monkeypatch):
    """
    A provenance block is metadata. If reading config raises, the benchmark must
    still print its numbers and say the model is unknown — not die after doing all
    the work. Losing a 42-query run to a metadata lookup would be a poor trade.

    Note what is deliberately NOT asserted here: that the reranker also reports
    "unavailable". It should not. The reranker's name is a module constant, not a
    setting, so it survives a config failure and is still reported correctly. The
    two lookups are independent on purpose — one bad config read should not blank
    out provenance that is still knowable.
    """
    from app.core import config as config_module
    from app.services.reranker import RERANKER_MODEL

    def _explode():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(config_module, "get_settings", _explode)
    provenance = eval_rag_module._model_provenance()

    assert "unavailable" in provenance["embedder"], "config failure must be reported, not raised"
    assert provenance["reranker"] == RERANKER_MODEL, (
        "the reranker name does not come from config and must survive this"
    )
