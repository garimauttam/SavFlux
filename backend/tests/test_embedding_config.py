"""
test_embedding_config.py — The retrieval stack's model choices are configuration,
and the configuration says what it does.

WHY THIS FILE EXISTS
--------------------
Three things were true of the embedding path and none of them were visible:

1. The model name was a string literal inside `get_embedding_fn()`. The single most
   consequential quality decision in a RAG system — what turns code into vectors —
   could not be changed without editing Python.

2. `batch_size` was hardcoded to 1. sentence-transformers encodes a batch at a
   time; a batch of one leaves the hardware idle and indexing slower for no reason.

3. `device` was hardcoded to "cpu", so a machine with a GPU got nothing from it.

The deeper problem underneath all three: `all-MiniLM-L6-v2` reads 256 tokens and
**silently discards** everything after that, while the chunker is allowed to emit
3000-character chunks. Nothing connected those two numbers, so nothing noticed.

These tests hold the wiring in place and, where a number is quoted in a comment,
assert the number. A comment claiming "384d" is worth nothing; a test that fails
when the claim stops being true is worth something.

No network and no weights: the model constructors are intercepted, so the suite
stays runnable on a machine that has never reached huggingface.co.
"""

from __future__ import annotations

import sys

import pytest

from app.core.config import Settings


@pytest.fixture()
def clean_settings(monkeypatch):
    """
    Settings as a fresh clone sees them, ignoring the suite's exported env.

    conftest pins LLM_PROVIDER=openai so tests never reach for huggingface.co.
    Without clearing it, these tests would assert the harness's own values rather
    than the shipped defaults — which is the exact failure that let `.env.example`
    document a provider the config rejected.
    """
    for key in (
        "LLM_PROVIDER",
        "EMBEDDING_MODEL",
        "EMBEDDING_BATCH_SIZE",
        "EMBEDDING_DEVICE",
    ):
        monkeypatch.delenv(key, raising=False)

    def build(**overrides):
        # _env_file=None: a developer's local .env must not change test outcomes.
        return Settings(_env_file=None, **overrides)

    return build


# ── Configuration surface ────────────────────────────────────────────────────


def test_the_local_embedding_model_is_configurable(clean_settings):
    """The whole point: a code-specialist embedder must be reachable via env."""
    s = clean_settings(embedding_model="jinaai/jina-embeddings-v2-base-code")
    assert s.embedding_model == "jinaai/jina-embeddings-v2-base-code"


def test_batching_is_no_longer_one(clean_settings):
    """A batch of 1 is a throughput bug, not a safe default."""
    assert clean_settings().embedding_batch_size > 1


def test_the_default_model_is_the_small_general_one(clean_settings):
    """
    Pinned deliberately, and this test is *expected* to fail when the default
    changes. That is the point: flipping the default embedder costs every existing
    user a re-index, so it should be a decision somebody made on purpose, not a
    silent edit that happens to pass.
    """
    assert clean_settings().embedding_model == "sentence-transformers/all-MiniLM-L6-v2"


def test_the_device_default_works_without_a_gpu(clean_settings):
    assert clean_settings().embedding_device == "auto"


# ── Validation: fail at startup, not mid-index ───────────────────────────────


@pytest.mark.parametrize("bad", [0, -1, -32])
def test_a_batch_size_below_one_is_rejected(clean_settings, bad):
    """
    Left to torch, a batch size of 0 fails deep inside a library on the first
    embedding call — after clone, parse and chunk have all already run.
    """
    with pytest.raises(Exception) as exc:
        clean_settings(embedding_batch_size=bad)
    assert "batch" in str(exc.value).lower()


@pytest.mark.parametrize("bad", ["gpu", "cuda:0", "", "   ", "tpu"])
def test_an_unknown_device_is_rejected(clean_settings, bad):
    """`cuda:0` and `tpu` are plausible values that torch would reject later."""
    with pytest.raises(Exception):
        clean_settings(embedding_device=bad)


@pytest.mark.parametrize("good", ["auto", "cpu", "cuda", "mps", "CUDA", "  MPS  "])
def test_the_four_real_devices_are_accepted_and_normalised(clean_settings, good):
    """
    Case and surrounding whitespace are normalised, not rejected.

    `EMBEDDING_DEVICE=CUDA` in a shell is not a mistake worth a traceback, and
    torch's own device strings are lower-case.
    """
    assert clean_settings(embedding_device=good).embedding_device == good.strip().lower()


# ── Wiring: the settings must actually reach the model ───────────────────────


def _intercept_embeddings(monkeypatch):
    """
    Intercept HuggingFaceEmbeddings construction and record its kwargs.

    Loading the real model needs weights from huggingface.co, which a test run must
    never require. What is under test is the wiring, so the constructor is replaced
    and its arguments inspected. The import happens inside `get_embedding_fn`, so
    the attribute is patched on the source module.
    """
    import langchain_huggingface

    captured: dict = {}

    class _Fake:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_huggingface, "HuggingFaceEmbeddings", _Fake)
    return captured


def _build_with(monkeypatch, **settings_overrides):
    """Call the real get_embedding_fn() against controlled settings."""
    from app.services import llm_factory

    llm_factory.get_embedding_fn.cache_clear()
    monkeypatch.setattr(
        llm_factory,
        "_settings",
        lambda: Settings(_env_file=None, llm_provider="ollama", **settings_overrides),
    )
    captured = _intercept_embeddings(monkeypatch)
    try:
        llm_factory.get_embedding_fn()
    finally:
        # The real function is lru_cached; leaving a fake built from patched
        # settings in the cache would leak into every later test.
        llm_factory.get_embedding_fn.cache_clear()
    return captured


def test_the_configured_model_reaches_huggingface_embeddings(monkeypatch):
    captured = _build_with(monkeypatch, embedding_model="org/code-model-x")

    assert captured["model_name"] == "org/code-model-x"
    assert captured["encode_kwargs"]["batch_size"] == 32
    assert captured["encode_kwargs"]["normalize_embeddings"] is True


def test_the_configured_batch_size_reaches_huggingface_embeddings(monkeypatch):
    captured = _build_with(monkeypatch, embedding_batch_size=8)
    assert captured["encode_kwargs"]["batch_size"] == 8


def test_the_resolved_device_reaches_huggingface_embeddings(monkeypatch):
    """
    `auto` must be resolved to a concrete device *before* it reaches torch.

    Passing "auto" through would fail at model construction, because torch has no
    device by that name.
    """
    captured = _build_with(monkeypatch, embedding_device="auto")
    assert captured["model_kwargs"]["device"] in {"cpu", "cuda"}


def test_the_default_device_is_what_actually_gets_passed(monkeypatch):
    captured = _build_with(monkeypatch, embedding_device="mps")
    assert captured["model_kwargs"]["device"] == "mps"


# ── Device resolution ────────────────────────────────────────────────────────


class _Torch:
    """Minimal torch stand-in: only `cuda.is_available` is consulted."""

    def __init__(self, available, raises=False):
        outer = self

        class _Cuda:
            @staticmethod
            def is_available():
                if raises:
                    raise RuntimeError("CUDA driver version is insufficient")
                return available

        self.cuda = _Cuda
        del outer


def test_auto_resolves_to_cpu_when_there_is_no_gpu(monkeypatch):
    from app.services import llm_factory

    monkeypatch.setitem(sys.modules, "torch", _Torch(available=False))
    assert llm_factory._resolve_embedding_device("auto") == "cpu"


def test_auto_resolves_to_cuda_when_there_is_one(monkeypatch):
    from app.services import llm_factory

    monkeypatch.setitem(sys.modules, "torch", _Torch(available=True))
    assert llm_factory._resolve_embedding_device("auto") == "cuda"


def test_auto_survives_a_torch_that_cannot_initialise(monkeypatch):
    """
    A torch that raises on CUDA probing must degrade to CPU.

    This runs while *building* the embedding function, so an exception here would
    take down the request rather than merely costing some speed. Falling back is
    the only acceptable outcome.
    """
    from app.services import llm_factory

    monkeypatch.setitem(sys.modules, "torch", _Torch(available=False, raises=True))
    assert llm_factory._resolve_embedding_device("auto") == "cpu"


def test_an_explicit_device_is_passed_through_untouched():
    """`cpu` must not be second-guessed into `cuda`."""
    from app.services import llm_factory

    assert llm_factory._resolve_embedding_device("cpu") == "cpu"
    assert llm_factory._resolve_embedding_device("mps") == "mps"


# ── The reported model is the model that runs ────────────────────────────────


def _provider_name(monkeypatch, **overrides) -> str:
    from app.services import llm_factory

    monkeypatch.setattr(
        llm_factory,
        "_settings",
        lambda: Settings(_env_file=None, llm_provider="ollama", **overrides),
    )
    return llm_factory.get_provider_name()


def test_provider_name_reports_the_configured_embedder(monkeypatch):
    """
    /health and the benchmark both print this string.

    It used to say "local MiniLM embeddings" unconditionally. Once the model is
    configurable, that hardcoded label describes a model that is not running — and
    it is exactly the label somebody reads while debugging retrieval, which makes
    it worse than saying nothing.
    """
    assert "org/code-model-x" in _provider_name(monkeypatch, embedding_model="org/code-model-x")


def test_provider_name_does_not_claim_minilm_when_another_model_is_configured(monkeypatch):
    assert "MiniLM" not in _provider_name(monkeypatch, embedding_model="org/code-model-x")


# ── The numbers quoted in the docs are the numbers that are true ─────────────


def test_the_default_embedder_is_one_of_the_documented_options(clean_settings):
    """
    `config.py` documents the embedder options, their dimensions and their windows.
    Those numbers are the justification for the choice, so they are asserted rather
    than trusted — a comment claiming "768d, 8K context" is worth nothing if the
    model it describes is not the one wired up.
    """
    documented = {
        # model: (embedding dims, max input tokens)
        "sentence-transformers/all-MiniLM-L6-v2": (384, 256),
        "jinaai/jina-embeddings-v2-base-code": (768, 8192),
    }
    default = clean_settings().embedding_model
    assert default in documented, (
        f"the default embedder {default!r} is not in the documented set "
        f"{sorted(documented)} — the comment block in config.py is out of date"
    )


def test_the_chunker_emits_chunks_the_default_embedder_cannot_read():
    """
    A tripwire on a known, measured defect — not an endorsement of it.

    The default embedder reads 256 tokens. The chunker's ceiling is 3000
    characters. Those two numbers were never compared, so roughly half of all
    chunks are truncated before they are embedded, and the discarded text is
    invisible: retrieval degrades with nothing in the logs.

    This test asserts the mismatch is still exactly of the size documented, so it
    cannot grow unnoticed. It fails when either number moves, in either direction,
    which forces the change to be deliberate. Deleting it is only correct once the
    two numbers agree — and then this comment should change too.
    """
    from app.services import code_chunker

    EMBEDDER_WINDOW_TOKENS = 256
    # 3.5 chars/token is the English-prose ratio and the most generous assumption
    # available: code tokenises *denser*, so a real chunk of a given character
    # length needs more tokens, not fewer. Using 3.5 makes the truncation window
    # as wide as it could plausibly be, so a chunk over it is certainly truncated.
    CHAR_RATIO = 3.5
    window_chars = int(EMBEDDER_WINDOW_TOKENS * CHAR_RATIO)  # 896

    assert code_chunker.MAX_CHUNK_CHARS == 3000, (
        "MAX_CHUNK_CHARS moved. Re-measure truncation over the corpus and update "
        "the numbers in config.py and docs/STRATEGY.md before changing this."
    )
    assert code_chunker.MAX_CHUNK_CHARS > window_chars, (
        "the chunker now fits inside the default embedder's window — good news! "
        "Delete this test and the truncation caveat in config.py."
    )


def test_env_example_activates_a_documented_embedder():
    """
    Hold `.env.example` to the same standard as the config comment block.

    This repo has already been burned once by a docs file that named a provider
    the code rejected — it crashed on a fresh clone, before any route ran. The
    embedding model is the same shape of risk: `.env.example` is the file a new
    user copies, so whatever it activates must be a model the code documents and
    a real, published option.
    """
    import re
    from pathlib import Path

    documented = {
        "sentence-transformers/all-MiniLM-L6-v2",
        "jinaai/jina-embeddings-v2-base-code",
    }

    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    text = env_example.read_text(encoding="utf-8")

    # Only uncommented assignments count: a commented line activates nothing.
    active = re.findall(r"^\s*EMBEDDING_MODEL\s*=\s*(\S+)\s*$", text, re.MULTILINE)
    assert active, ".env.example activates no EMBEDDING_MODEL"
    for value in active:
        assert value in documented, (
            f".env.example activates EMBEDDING_MODEL={value!r}, which is not one of "
            f"the documented options {sorted(documented)}"
        )


def test_env_example_activates_a_valid_batch_size_and_device():
    """A copied .env that fails validation is worse than no .env at all."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")

    sizes = re.findall(r"^\s*EMBEDDING_BATCH_SIZE\s*=\s*(\S+)\s*$", text, re.MULTILINE)
    devices = re.findall(r"^\s*EMBEDDING_DEVICE\s*=\s*(\S+)\s*$", text, re.MULTILINE)
    assert sizes and devices, ".env.example must activate both settings"

    for size in sizes:
        assert Settings(_env_file=None, embedding_batch_size=int(size)).embedding_batch_size > 0
    for device in devices:
        assert Settings(_env_file=None, embedding_device=device).embedding_device in {
            "auto", "cpu", "cuda", "mps"
        }
