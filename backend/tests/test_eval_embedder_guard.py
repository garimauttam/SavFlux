"""
The benchmark may measure a real embedding model, but it may never pay for one.

Why this file exists: `--embedder model` is the CLI default, `python eval_rag.py` is the
documented way to run it, and the embedder the product uses when `LLM_PROVIDER=openai`
*is* a paid API. Indexing this repo's corpus is ~1,800 embedding calls. A $0 product's
benchmark must not be the thing that spends money by inheriting a setting.

These tests assert the refusal, the local path, and that the refusal happens without ever
constructing the paid client. The last one is the actual claim: "it prints a nice message"
is worth nothing if `OpenAIEmbeddings(...)` was already called by then.
"""

import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import eval_rag  # noqa: E402
from langchain_core.documents import Document  # noqa: E402


def _unit(source: str, content: str, index: int = 0) -> Document:
    """A corpus unit shaped like ingestion's, `chunk_index` included (see test_eval_ground_truth)."""
    return Document(
        page_content=content,
        metadata={"source": source, "file_name": Path(source).name, "chunk_index": index},
    )


class FakePaidEmbeddings:
    """Stands in for `langchain_openai.OpenAIEmbeddings`, and records every construction."""

    built = 0

    def __init__(self, *args, **kwargs):
        type(self).built += 1

    async def aembed_query(self, text):  # pragma: no cover - never reached
        raise AssertionError("the benchmark embedded through a paid API")


class FakeHuggingFaceEmbeddings:
    """Stands in for the local HF class, recording the construction arguments."""

    instances = []

    def __init__(self, *args, **kwargs):
        type(self).instances.append(kwargs)

    async def aembed_query(self, text):  # pragma: no cover - not exercised here
        return [0.0] * 4


@pytest.fixture
def paid_spy(monkeypatch):
    """A fake paid-embeddings class that counts constructions, patched into the import path."""

    class Module:
        OpenAIEmbeddings = FakePaidEmbeddings

    FakePaidEmbeddings.built = 0
    monkeypatch.setitem(sys.modules, "langchain_openai", Module())
    yield FakePaidEmbeddings
    assert FakePaidEmbeddings.built == 0, "a paid embedder was constructed during a benchmark run"


@pytest.fixture
def local_spy(monkeypatch):
    FakeHuggingFaceEmbeddings.instances = []

    class Module:
        HuggingFaceEmbeddings = FakeHuggingFaceEmbeddings

    monkeypatch.setitem(sys.modules, "langchain_huggingface", Module())
    return FakeHuggingFaceEmbeddings


def _provider(monkeypatch, provider: str):
    """Pin what `get_settings().llm_provider` reports, without touching .env."""

    class Settings:
        llm_provider = provider
        embedding_model = "sentence-transformers/all-MiniLM-L6-v2"
        embedding_device = "cpu"
        embedding_batch_size = 32
        openai_api_key = "sk-ci-placeholder"
        openai_embedding_model = "text-embedding-3-small"

    # Both names, because `llm_factory` did `from app.core.config import get_settings` at
    # import time and holds a direct reference: patching only the module attribute would
    # leave the factory reading the real .env, and a test about which embedder gets built
    # would then be measuring this machine's environment.
    monkeypatch.setattr("app.core.config.get_settings", lambda: Settings(), raising=True)
    monkeypatch.setattr("app.services.llm_factory.get_settings", lambda: Settings(), raising=True)
    return Settings


class TestPaidEmbeddersAreRefused:
    @pytest.mark.asyncio
    async def test_the_configured_paid_provider_is_refused_before_anything_is_built(self, monkeypatch, paid_spy):
        _provider(monkeypatch, "openai")

        with pytest.raises(RuntimeError) as exc:
            eval_rag._build_embedder("model")

        message = str(exc.value)
        assert "paid" in message.lower()
        # Every refusal in this file owes the reader a way out, and there are three.
        assert "--embedding-model" in message
        assert "ollama" in message
        assert "--embedder offline" in message

    @pytest.mark.asyncio
    async def test_a_provider_gate_cannot_be_bypassed_by_the_object_it_returns(self, monkeypatch, paid_spy):
        """
        The second layer, which is the one that actually matters.

        If the factory's branching ever says "local" and hands back a paid client anyway,
        the returned type has to be refused. Here the provider says `ollama` and the fake
        factory returns the paid class, so only the type check can stop it.
        """
        _provider(monkeypatch, "ollama")

        class PaidLookingObject:
            __module__ = "langchain_openai.embeddings.base"

        monkeypatch.setattr(
            "app.services.llm_factory.get_embedding_fn",
            lambda: PaidLookingObject(),
            raising=True,
        )

        with pytest.raises(RuntimeError) as exc:
            eval_rag._build_embedder("model")
        assert "paid API" in str(exc.value)

    @pytest.mark.asyncio
    async def test_unreadable_settings_are_a_refusal_not_a_permission(self, monkeypatch, paid_spy):
        """A config that cannot be read must not be read as "probably local"."""

        def boom():
            raise RuntimeError("no .env")

        monkeypatch.setattr("app.core.config.get_settings", boom, raising=True)

        with pytest.raises(RuntimeError) as exc:
            eval_rag._build_embedder("model")
        assert "LLM_PROVIDER" in str(exc.value)


class TestLocalPathsWork:
    @pytest.mark.asyncio
    async def test_a_pinned_model_is_built_locally_with_production_settings(self, monkeypatch, local_spy):
        """
        The pin is the whole point: naming a model must produce the object production
        would produce, so the measurement transfers. Asserting on the construction
        arguments is how a silent divergence in normalisation gets caught.
        """
        _provider(monkeypatch, "openai")  # a paid provider must not stop a pinned local model

        eval_rag._build_embedder("model", "sentence-transformers/all-MiniLM-L6-v2")

        assert len(local_spy.instances) == 1
        kwargs = local_spy.instances[0]
        assert kwargs["model_name"] == "sentence-transformers/all-MiniLM-L6-v2"
        assert kwargs["encode_kwargs"]["normalize_embeddings"] is True
        assert kwargs["encode_kwargs"]["batch_size"] == 32
        assert kwargs["model_kwargs"]["device"] in ("cpu", "cuda")

    @pytest.mark.asyncio
    async def test_the_local_provider_path_is_not_refused(self, monkeypatch, local_spy):
        _provider(monkeypatch, "ollama")

        eval_rag._build_embedder("model")  # must not raise

        assert len(local_spy.instances) == 1
        assert local_spy.instances[0]["model_name"] == "sentence-transformers/all-MiniLM-L6-v2"

    @pytest.mark.asyncio
    async def test_a_missing_model_fails_loudly_instead_of_becoming_the_offline_one(self, monkeypatch, local_spy):
        """
        The rule the file has always had, re-asserted for the new entry point: a quality
        number from a lexical stand-in still looks like evidence, so the pin has to fail
        with the fix in the message rather than quietly substitute.
        """

        class Exploding:
            def __init__(self, *args, **kwargs):
                raise OSError("huggingface.co unreachable and nothing in the cache")

        class Module:
            HuggingFaceEmbeddings = Exploding

        monkeypatch.setitem(sys.modules, "langchain_huggingface", Module())

        with pytest.raises(RuntimeError) as exc:
            eval_rag._build_embedder("model", "some/model")

        message = str(exc.value)
        assert "--embedding-model some/model" in message
        assert "offline" in message.lower()

    @pytest.mark.asyncio
    async def test_naming_a_model_and_the_lexical_stand_in_at_once_is_rejected(self):
        with pytest.raises(ValueError) as exc:
            eval_rag._build_embedder("offline", "sentence-transformers/all-MiniLM-L6-v2")
        assert "Pick one" in str(exc.value)


class TestProvenance:
    def test_a_pinned_run_says_it_was_pinned(self, monkeypatch):
        _provider(monkeypatch, "ollama")

        provenance = eval_rag._model_provenance("model", "jinaai/jina-embeddings-v2-base-code")

        assert provenance["embedder"] == "jinaai/jina-embeddings-v2-base-code"
        assert "not the configured model" in provenance["embedder_source"]

    def test_an_unpinned_run_reports_the_configured_model(self, monkeypatch):
        _provider(monkeypatch, "ollama")

        provenance = eval_rag._model_provenance("model")

        assert "embedder_source" not in provenance
        assert provenance["embedder"] == "sentence-transformers/all-MiniLM-L6-v2"


class TestRunWiring:
    @pytest.mark.asyncio
    async def test_the_pin_reaches_the_builder_and_the_report_labels_it(self, monkeypatch):
        """
        A flag nobody passes is worse than no flag: it advertises a capability that does
        not exist. So the pin is followed all the way from the CLI-level argument through
        `evaluate_pipeline` into the builder and the label a reader sees.
        """
        seen = {}

        def fake_build(mode, pin=None):
            seen["mode"], seen["pin"] = mode, pin
            from app.services.offline_embedder import OfflineEmbedder

            return OfflineEmbedder()

        monkeypatch.setattr(eval_rag, "_build_embedder", fake_build)

        result = await eval_rag.evaluate_pipeline(
            dataset=[{"query": "where is the retry logic", "ground_truth_file": "x.py",
                      "expected_symbols": ["retry_with_backoff"]}],
            corpus_docs=[
                _unit("x.py", "def retry_with_backoff(): pass", 0),
                _unit("y.py", "def other(): pass", 0),
            ],
            top_k=2,
            verbose=False,
            embedder_mode="model",
            embedder_pin="sentence-transformers/all-MiniLM-L6-v2",
            rerank_enabled=False,
        )

        assert seen == {"mode": "model", "pin": "sentence-transformers/all-MiniLM-L6-v2"}
        assert result["metrics"]["dense_leg_embedder"] == (
            "local model sentence-transformers/all-MiniLM-L6-v2 (pinned)"
        )

    @pytest.mark.asyncio
    async def test_indexing_cost_is_measured_and_separate_from_query_latency(self, monkeypatch):
        """
        The number an embedder decision actually turns on. A real model makes the dense
        leg's one-time corpus embedding, not its per-query maths, the expensive part, so
        both are reported — and `embed_index_ms` is a sibling of the latency stats rather
        than inside them, because a per-query mean polluted by a once-per-re-index cost
        would misdescribe the product.
        """
        monkeypatch.setattr(eval_rag, "_build_embedder", lambda mode, pin=None: _SlowEmbedder())

        result = await eval_rag.evaluate_pipeline(
            dataset=[{"query": "q", "ground_truth_file": "x.py", "expected_symbols": ["retry"]}],
            corpus_docs=[_unit("x.py", "def retry(): pass", i) for i in range(3)],
            top_k=2,
            verbose=False,
            embedder_mode="model",
            rerank_enabled=False,
        )

        metrics = result["metrics"]
        assert metrics["embed_index_ms"] >= 20, "a 25 ms sleep should have been timed"
        assert metrics["embed_units_per_second"] > 0
        # The corpus size it was paid over travels with it, or the time is meaningless.
        assert result["evaluation"]["dense_corpus"]["windows"] == 3
        assert "embed_index_ms" not in (metrics["latency_ms"] or {})


class _SlowEmbedder:
    """
    An embedder that takes 25 ms per call, so a build that was never timed is obvious.

    Sync on purpose: `DenseIndex` embeds the corpus through `embed_documents` in its
    constructor and scores queries through `embed_query`. A fake with only async methods
    would prove the timing works while measuring a call pattern production never makes.
    """

    def embed_documents(self, texts):
        time.sleep(0.025)
        return [[0.0, 1.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0]
