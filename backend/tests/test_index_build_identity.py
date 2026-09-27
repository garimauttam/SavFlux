"""
An index has to remember what built it, or "unchanged file" is a lie.

The delta pass skips re-embedding files whose content hash matches. That was the whole
condition, and content is only half of what a vector is a function of: change the
embedding model or the windowing and every stored vector means something else while the
hashes keep matching, so the skip is permanent and the collection ends up holding two
incompatible vector spaces. `get_embedding_fn` warns "re-index after switching"; nothing
enforced it. These tests pin the enforcement, the self-healing of indexes stamped before
it existed, and the per-file counts the progress messages and the trust ledger report —
including `files_skipped`, which was once `len(files) - len(every file seen)`, i.e. always 0.

The integration half drives the real `ingest_github_repo` against a fake Chroma with only
clone/scan/split stubbed, so what is asserted is what is embedded and deleted, not a
transcription of the loop.
"""

import asyncio
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.documents import Document

from app.services import ingestion_service as ing

REPO = "https://github.com/octo/demo"
BUILD_A = "aaaa-aaaa-aaaa-aaaa"
BUILD_B = "bbbb-bbbb-bbbb-bbbb"


class _Settings:
    """Just the fields `index_build_id` reads, with the shipped defaults."""

    def __init__(self, **over):
        self.llm_provider = over.get("llm_provider", "ollama")
        self.embedding_model = over.get("embedding_model", "sentence-transformers/all-MiniLM-L6-v2")
        self.openai_embedding_model = over.get("openai_embedding_model", "text-embedding-3-small")
        self.chunk_size = over.get("chunk_size", 700)
        self.chunk_overlap = over.get("chunk_overlap", 150)


def _chunk(source: str, token: str, *, build: str = BUILD_A, index: int = 0) -> Document:
    """A chunk shaped like `_load_and_split`'s output — `index_build` included."""
    return Document(
        page_content=f"def {token}():\n    return 1\n",
        metadata={
            "source": f"{REPO}::{source}",
            "file_name": source,
            "language": "python",
            "repo_url": REPO,
            "chunk_index": index,
            "content_hash": f"hash-{token}",
            "index_build": build,
        },
    )


def _indexed(hash_: str, ids, *, build: str = BUILD_A, source: str = "a.py",
             extra_hashes=(), extra_builds=()):
    """
    One file's entry in `indexed_map`, in the shape `build_indexed_map` returns.

    `extra_hashes` / `extra_builds` put a second, disagreeing value on the same file —
    what a half-finished rewrite looks like from the read side.
    """
    return {
        f"{REPO}::{source}": {
            "hashes": {hash_, *extra_hashes},
            "builds": {build, *extra_builds},
            "ids": list(ids),
        }
    }


# ── the identity itself ────────────────────────────────────────────────────────


class TestIndexBuildId:
    def test_the_same_settings_produce_the_same_id(self):
        assert ing.index_build_id() == ing.index_build_id()

    def _id_with(self, monkeypatch, **over):
        """
        One id, from settings patched rather than mutated.

        Ids are always compared in pairs from this same helper: comparing a patched id
        against one computed from the real .env would pass for the wrong reason — the two
        differ in every field, so a rule that ignores one of them still shows up as a
        difference.
        """

        def compute():
            monkeypatch.setattr(ing, "get_settings", lambda: _Settings(**over))
            return ing.index_build_id()

        return compute

    def test_a_different_embedding_model_is_a_different_id(self, monkeypatch):
        """The case the skip used to survive: same bytes, different vector space."""
        # (base, field, new value). The base matters: `openai_embedding_model` is only
        # read under a paid provider, and a change to a field the run never looks at must
        # not move the id — which is asserted in the test below this one.
        for base, key, value in (
            ({}, "embedding_model", "sentence-transformers/other-model"),
            ({"llm_provider": "openai"}, "openai_embedding_model", "text-embedding-3-large"),
            ({}, "chunk_size", 400),
            ({}, "chunk_overlap", 0),
        ):
            # `llm_provider` itself is deliberately absent from the list: two providers
            # that both embed locally must NOT invalidate the index, and that is a test of
            # its own below.
            one = self._id_with(monkeypatch, **base)()
            other = self._id_with(monkeypatch, **{**base, key: value})()
            assert one != other, f"{key} must be part of the identity that forces a rebuild"

    def test_only_the_model_field_the_provider_actually_uses_moves_the_id(self, monkeypatch):
        """
        Under a paid provider the embedding model is `OPENAI_EMBEDDING_MODEL`; the local
        one is what the free path reads. Reading the wrong field is invisible until the
        day the paid model is swapped and every stored vector quietly still says "unchanged".
        """
        paid_a = self._id_with(monkeypatch, llm_provider="openai",
                               openai_embedding_model="text-embedding-3-small")()
        paid_b = self._id_with(monkeypatch, llm_provider="openai",
                               openai_embedding_model="text-embedding-3-large")()
        assert paid_a != paid_b

        paid_other_local_field = self._id_with(
            monkeypatch, llm_provider="openai", openai_embedding_model="text-embedding-3-small",
            embedding_model="sentence-transformers/whatever",
        )()
        assert paid_a == paid_other_local_field, (
            "an unused setting must not invalidate the whole index either"
        )

        local_a = self._id_with(monkeypatch, llm_provider="ollama",
                                embedding_model="sentence-transformers/all-MiniLM-L6-v2")()
        local_b = self._id_with(monkeypatch, llm_provider="ollama",
                                embedding_model="sentence-transformers/other-model")()
        assert local_a != local_b

    def test_changing_the_window_constants_changes_the_id(self, monkeypatch):
        """
        Windowing decides the text handed to the embedder, so it decides the vector.

        `children_of_all` takes these as defaults, which means an index built before a
        constant changed and one built after are not the same index — and the rows are
        indistinguishable from the file side.
        """
        from app.services import parent_child

        first = ing.index_build_id()

        monkeypatch.setattr(parent_child, "EMBED_WINDOW_CHARS", 1200, raising=False)
        assert ing.index_build_id() != first
        monkeypatch.undo()

        monkeypatch.setattr(parent_child, "CHILD_OVERLAP_CHARS", 0, raising=False)
        assert ing.index_build_id() != first

    def test_the_id_is_short_hex_like_the_content_hash_it_sits_next_to(self):
        value = ing.index_build_id()
        assert len(value) == 16
        int(value, 16)  # must not raise: hex, like content_hash

    def test_the_paid_provider_is_named_by_its_own_embedding_model(self, monkeypatch):
        """
        Which model field is read depends on the provider, and the id must follow it.

        Reading `embedding_model` while `openai` is configured would leave the paid
        model's name out of the identity — and swapping text-embedding-3-small for
        -3-large is exactly the change that invalidates every vector in the collection.
        """
        monkeypatch.setattr(ing, "get_settings", lambda: _Settings(llm_provider="openai"))
        openai_id = ing.index_build_id()
        monkeypatch.setattr(ing, "get_settings", lambda: _Settings(llm_provider="ollama"))
        ollama_id = ing.index_build_id()

        assert openai_id != ollama_id

    def test_moving_between_two_local_providers_is_not_a_reindex(self, monkeypatch):
        """
        The promise in the README: "switching chat providers needs no re-index".

        Both providers embed with the same local model, so no vector changes meaning — and
        an identity keyed on the provider name would throw away a whole corpus of embedding
        work to switch which model writes the answer text.
        """
        monkeypatch.setattr(ing, "get_settings", lambda: _Settings(llm_provider="ollama"))
        ollama_id = ing.index_build_id()
        monkeypatch.setattr(ing, "get_settings", lambda: _Settings(llm_provider="deepseek"))

        assert ing.index_build_id() == ollama_id

    def test_a_local_and_a_paid_embedder_that_share_a_name_are_still_distinguished(
        self, monkeypatch
    ):
        """Why the identity carries the embedder *path* and not just a model name."""
        same = "text-embedding-3-small"
        monkeypatch.setattr(
            ing, "get_settings",
            lambda: _Settings(llm_provider="openai", openai_embedding_model=same, embedding_model=same),
        )
        api_id = ing.index_build_id()
        monkeypatch.setattr(
            ing, "get_settings",
            lambda: _Settings(llm_provider="ollama", openai_embedding_model=same, embedding_model=same),
        )

        assert ing.index_build_id() != api_id


# ── reading what is already in the collection ─────────────────────────────────


class TestStamping:
    """
    The producer side of the contract. A skip decision is only as good as the stamp every
    chunk carries, and `_load_and_split` has two exits — the AST chunker and the character
    splitter — so "stamped at the return" is tested through both.
    """

    def test_every_chunk_the_splitter_returns_carries_this_runs_build_id(self, tmp_path):
        (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")

        docs = ing._load_and_split([tmp_path / "a.py"], REPO, tmp_path, "build-xyz")

        assert docs, "fixture must produce chunks"
        assert all(d.metadata["index_build"] == "build-xyz" for d in docs)

    def test_a_stampless_call_leaves_chunks_unattributed_rather_than_wrong(self, tmp_path):
        """
        No build id is not build id "". An unattributed chunk re-embeds on the next run;
        a chunk stamped with a guess would be skipped forever by a pipeline that never made
        it, which is the failure mode the whole mechanism exists to avoid.
        """
        (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")

        docs = ing._load_and_split([tmp_path / "a.py"], REPO, tmp_path)

        assert all("index_build" not in d.metadata for d in docs)

    def test_the_ast_chunker_path_is_stamped_too(self, tmp_path):
        """
        The exit that `continue`s past the fallback loop, i.e. the one a stamp placed at
        the metadata update of the character-splitter branch would have missed — and the
        AST path is what every real Python file in this repo actually takes.
        """
        body = "\n\n".join(f"def f_{i}():\n    return {i}\n" for i in range(30))
        (tmp_path / "big.py").write_text(body, encoding="utf-8")

        docs = ing._load_and_split([tmp_path / "big.py"], REPO, tmp_path, "build-ast")

        assert len(docs) > 1, "need the AST chunker to have produced several chunks"
        assert all(d.metadata["index_build"] == "build-ast" for d in docs)


class TestBuildIndexedMap:
    def test_all_ids_and_values_of_a_file_are_collected_together(self):
        ids = ["r1", "r2", "r3"]
        metas = [
            {"repo_url": REPO, "source": f"{REPO}::a.py", "content_hash": "h1", "index_build": BUILD_A},
            {"repo_url": REPO, "source": f"{REPO}::a.py", "content_hash": "h1", "index_build": BUILD_A},
            {"repo_url": REPO, "source": f"{REPO}::a.py", "content_hash": "h1", "index_build": BUILD_A},
        ]

        indexed = ing.build_indexed_map(ids, metas, REPO)

        entry = indexed[f"{REPO}::a.py"]
        assert entry["ids"] == ids
        assert entry["hashes"] == {"h1"}, "identical rows must collapse to one value"
        assert entry["builds"] == {BUILD_A}

    def test_an_index_written_before_the_stamp_existed_reads_as_unattributed(self):
        """Not as `None`, not as a guess — "" is what forces exactly one rebuild."""
        indexed = ing.build_indexed_map(
            ["r1"], [{"repo_url": REPO, "source": f"{REPO}::a.py", "content_hash": "h1"}], REPO
        )

        assert indexed[f"{REPO}::a.py"]["builds"] == {""}

    def test_another_repos_rows_are_not_here(self):
        metas = [{"repo_url": "https://github.com/other/repo", "source": "x", "content_hash": "h"}]

        assert ing.build_indexed_map(["r1"], metas, REPO) == {}

    def test_url_variants_of_one_repo_are_one_repo(self):
        """The normalisation is why removed files get purged instead of duplicated."""
        metas = [
            {"repo_url": f"{REPO}.git", "source": f"{REPO}::a.py", "content_hash": "h1"},
            {"repo_url": f"{REPO}/", "source": f"{REPO}::b.py", "content_hash": "h2"},
        ]

        indexed = ing.build_indexed_map(["r1", "r2"], metas, REPO)

        assert set(indexed) == {f"{REPO}::a.py", f"{REPO}::b.py"}

    def test_missing_keys_in_the_payload_do_not_raise(self):
        """A collection with no `ids` key is an empty index, not a crash mid-ingest."""
        assert ing.build_indexed_map(None, None, REPO) == {}


# ── the decision ──────────────────────────────────────────────────────────────


def _plan(documents, indexed_map, current_sources=None, build_id=BUILD_A):
    return ing.plan_delta(
        indexed_map=indexed_map,
        documents=documents,
        current_sources=current_sources if current_sources is not None
        else {d.metadata["source"] for d in documents},
        build_id=build_id,
    )


class TestPlanDelta:
    def test_unchanged_bytes_and_unchanged_pipeline_are_skipped(self):
        plan = _plan(
            [_chunk("a.py", "alpha")],
            _indexed("hash-alpha", ["r1", "r2"]),
        )

        assert plan.new_docs == []
        assert plan.stale_ids == []
        assert plan.files_skipped == 1

    def test_changed_bytes_rebuild_and_purge(self):
        plan = _plan(
            [_chunk("a.py", "beta")],
            _indexed("hash-alpha", ["r1"]),
        )

        assert [d.metadata["source"] for d in plan.new_docs] == [f"{REPO}::a.py"]
        assert plan.stale_ids == ["r1"]
        assert (plan.files_changed, plan.files_rebuilt, plan.files_skipped) == (1, 0, 0)

    def test_same_bytes_under_a_different_embedder_is_not_unchanged(self):
        """
        The defect this file exists for.

        Before, `content_hash` was the only condition: an embedder swap left every vector
        in the collection produced by a model the queries are no longer using, and because
        the hashes still matched, no later run would ever fix it.
        """
        plan = _plan(
            [_chunk("a.py", "alpha")],
            _indexed("hash-alpha", ["r1", "r2"], build=BUILD_B),
        )

        assert len(plan.new_docs) == 1, "a file whose vectors came from another pipeline must be re-embedded"
        assert plan.stale_ids == ["r1", "r2"], "its old rows must go, or the collection mixes spaces"
        assert plan.files_rebuilt == 1
        assert plan.files_skipped == 0

    def test_an_unstamped_index_is_rebuilt_once_then_attributed(self):
        plan = _plan([_chunk("a.py", "alpha")], _indexed("hash-alpha", ["r1"], build=""))

        assert plan.files_rebuilt == 1
        assert plan.stale_ids == ["r1"]
        # The run that rebuilds is also the run that stamps, so the next one skips.
        stamped = _plan([_chunk("a.py", "alpha")], _indexed("hash-alpha", ["r9"]))
        assert stamped.files_skipped == 1 and stamped.new_docs == []

    def test_every_chunk_of_a_rebuilt_file_is_rewritten_not_just_one(self):
        docs = [
            _chunk("a.py", "alpha", index=0),
            _chunk("a.py", "alpha", index=1),
            _chunk("a.py", "alpha", index=2),
        ]
        plan = _plan(docs, _indexed("hash-alpha", ["r1", "r2", "r3"], build=BUILD_B))

        assert len(plan.new_docs) == 3, "half a file in the new space is worse than all of it in the old"
        assert plan.files_rebuilt == 1, "three chunks of one file is one file, not three"

    def test_a_removed_file_is_purged_even_though_it_cannot_match_anything(self):
        plan = _plan([], _indexed("hash-alpha", ["r1"]), current_sources=set())

        assert plan.stale_ids == ["r1"]

    def test_rows_that_disagree_with_each_other_are_rewritten_not_trusted(self):
        """
        A file whose stored chunks disagree was mid-rewrite when something died.

        Either value could be picked as "the" hash and one of the two row sets then looks
        current forever, which is how a partially embedded file becomes permanent. The
        disagreement itself is the signal: rewrite the file and purge both row sets.
        """
        plan = _plan(
            [_chunk("a.py", "alpha")],
            _indexed("hash-alpha", ["r1", "r2"], extra_hashes=["hash-old"]),
        )

        assert len(plan.new_docs) == 1
        assert plan.stale_ids == ["r1", "r2"]
        assert plan.files_skipped == 0

    def test_the_stored_values_are_compared_as_sets_not_sampled(self):
        """
        "The file's rows disagree" is a property of the whole set, so peeking at one
        element is a bug that only shows up in whatever order Python hashed them into
        today — a test built on a two-element set would pass or fail by luck.

        The fake below refuses to be iterated, which turns that luck into a hard failure
        for any implementation that samples instead of comparing, and says nothing at all
        about the production one, which never asks.
        """

        class NoPeekingSet(set):
            def __iter__(self):
                raise AssertionError("stored hashes were sampled; disagreement can hide in the order")

            def __eq__(self, other):
                return set.__eq__(self, other)

            def __ne__(self, other):
                return not self.__eq__(other)

            __hash__ = None

        source = f"{REPO}::a.py"
        plan = ing.plan_delta(
            indexed_map={source: {"hashes": NoPeekingSet({"hash-alpha", "hash-old"}),
                                 "builds": {BUILD_A}, "ids": ["r1", "r2"]}},
            documents=[_chunk("a.py", "alpha")],
            current_sources={source},
            build_id=BUILD_A,
        )

        assert plan.files_changed == 1, "a file with two hashes is mid-rewrite, not unchanged"
        assert plan.stale_ids == ["r1", "r2"]

    def test_the_count_a_user_reads_is_the_number_of_files_not_chunks(self):
        docs = [_chunk("a.py", "alpha"), _chunk("a.py", "alpha", index=1), _chunk("b.py", "beta")]
        plan = _plan(docs, _indexed("hash-alpha", ["r1", "r2"]), current_sources={f"{REPO}::a.py", f"{REPO}::b.py"})

        assert plan.files_new == 1 and plan.files_skipped == 1
        assert plan.files_touched == 1, "only b.py was written this run"
        assert len(plan.new_docs) == 1


# ── the wiring, against the real ingest path ──────────────────────────────────


def _fake_store(existing=None):
    class FakeCollection:
        def __init__(self, store):
            self._store = store

        def get(self, include=None):
            return {
                "ids": [rid for rid, _ in self._store.rows],
                "metadatas": [doc.metadata for _, doc in self._store.rows],
            }

        def delete(self, ids):
            doomed = set(ids)
            self._store.deleted.extend(doomed)
            self._store.rows = [(r, d) for r, d in self._store.rows if r not in doomed]

    class FakeVectorstore:
        def __init__(self):
            self.rows = list(existing or [])
            self.batches = []
            self.deleted = []
            self._collection = FakeCollection(self)

        def add_documents(self, documents):
            self.batches.append(list(documents))
            added = []
            for doc in documents:
                rid = f"row-{len(self.rows)}"
                self.rows.append((rid, doc))
                added.append(rid)
            return added

    return FakeVectorstore()


def _run_ingest(store, documents, files, *, build_id=BUILD_A):
    """Real `ingest_github_repo`; only clone, file scan, splitter and side effects stubbed."""

    def _collect(root):
        made = []
        for name in files:
            path = Path(root) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x = 1\n")
            made.append(path)
        return made

    def _split(_files, _repo_url, _root=None, build_id=""):
        """
        The stand-in for `_load_and_split` that behaves like it: copies, stamped with THIS
        run's build id.

        Returning the fixture documents untouched would make the stamp assertions below
        compare a fixture against itself. Deep copies because two runs in one test must not
        share one metadata dict — an alias there hides the exact bug being pinned.
        """
        import copy as _copy

        return ing._stamp_build([_copy.deepcopy(d) for d in documents], build_id)

    async def go():
        # `normalize_repo_url` is left alone on purpose: REPO is already in its own
        # normalised form, so patching it would hide a mismatch between the sources the
        # fixture builds and the ones the production code stores.
        # A MagicMock clone rather than `return_value=object()`: the trust ledger reads
        # `Repo(tmp_dir).head.commit.hexsha` inside a bare `except Exception: pass`, so a
        # repo stub without those attributes makes the ledger silently never run — and an
        # assertion on it would then be asserting on an absent call, i.e. nothing.
        with patch.object(ing.git, "Repo"), \
             patch.object(ing, "_collect_files", side_effect=_collect), \
             patch.object(ing, "_load_and_split", side_effect=_split), \
             patch.object(ing, "index_build_id", return_value=build_id), \
             patch.object(ing, "_get_vectorstore", return_value=store), \
             patch("app.services.trust_service.record_index") as record, \
             patch("app.services.history_service.seed_mirror_from_tmp"):
            result = await ing.ingest_github_repo(REPO)
        result["_recorded_files_indexed"] = record.call_args.kwargs.get("files_indexed")
        return result

    return asyncio.run(go())


class TestIngestPath:
    def test_a_re_ingest_under_a_changed_embedder_re_embeds_and_reports_why(self):
        store = _fake_store()
        docs = [_chunk("a.py", "alpha", index=0), _chunk("a.py", "alpha", index=1)]
        first = _run_ingest(store, docs, ["a.py"])
        assert first["files_skipped"] == 0
        rows_after_first = len(store.rows)
        batches_after_first = len(store.batches)
        assert rows_after_first > 0

        second = _run_ingest(store, docs, ["a.py"], build_id=BUILD_B)

        assert len(store.batches) == batches_after_first + 1, (
            "the second run embedded nothing, so its vectors came from a model "
            "that is no longer configured"
        )
        assert second["files_rebuilt"] == 1
        assert second["files_skipped"] == 0
        assert len(store.deleted) == rows_after_first, "the old-space rows must be gone"
        assert all(doc.metadata["index_build"] == BUILD_B for _, doc in store.rows)

    def test_a_second_identical_run_embeds_nothing_and_says_so_with_a_real_number(self):
        store = _fake_store()
        docs = [_chunk("a.py", "alpha"), _chunk("b.py", "beta")]
        _run_ingest(store, docs, ["a.py", "b.py"])
        batches = len(store.batches)
        rows = len(store.rows)

        second = _run_ingest(store, docs, ["a.py", "b.py"])

        assert len(store.batches) == batches, "an unchanged re-ingest must not embed"
        assert len(store.rows) == rows
        assert second["files_skipped"] == 2, "the count used to be arithmetically impossible"
        # The trust ledger gets the same number the user does, not `len(files)` — it is
        # what an answer is later verified against, so an inflated count there is a claim
        # that more of the repo was reviewed than ever had vectors.
        assert second["_recorded_files_indexed"] == 2
        assert second["index_build"] == BUILD_A, "the response says which pipeline wrote it"
        assert second["chunks_created"] == 0
        assert second["files_indexed"] == 2, "carried-over files are indexed, just not rewritten"

    def test_the_progress_message_names_the_skipped_files(self):
        messages = []

        async def collect(event):
            messages.append(event.get("message", ""))

        store = _fake_store()
        docs = [_chunk("a.py", "alpha"), _chunk("b.py", "beta")]
        _run_ingest(store, docs, ["a.py", "b.py"])

        # A MagicMock clone rather than `return_value=object()`: the trust ledger reads
        # `Repo(tmp_dir).head.commit.hexsha` inside a bare `except Exception: pass`, so a
        # repo stub without those attributes makes the ledger silently never run — and an
        # assertion on it would then be asserting on an absent call, i.e. nothing.
        with patch.object(ing.git, "Repo"), \
             patch.object(ing, "_collect_files", side_effect=_one_file_that_writes(["a.py", "b.py"])), \
             patch.object(ing, "_load_and_split", return_value=docs), \
             patch.object(ing, "index_build_id", return_value=BUILD_A), \
             patch.object(ing, "_get_vectorstore", return_value=store), \
             patch("app.services.trust_service.record_index"), \
             patch("app.services.history_service.seed_mirror_from_tmp"):
            asyncio.run(ing.ingest_github_repo(REPO, progress_callback=collect))

        assert any("2 files unchanged, skipped" in m for m in messages), messages
        assert not any("Embedding 2 chunks (this may take a minute)" in m for m in messages), (
            "the pre-decision message announced the whole corpus before the delta pass ran"
        )

    def test_a_file_that_produced_no_chunks_is_not_reported_as_skipped(self):
        """
        The old formula was `len(files) - len(seen_sources)`, which is exactly how a
        file that yielded nothing became a phantom "skipped for reuse" — a number that
        reads as a saving and describes a parse failure.
        """
        store = _fake_store()
        _run_ingest(store, [_chunk("a.py", "alpha")], ["a.py"])

        result = _run_ingest(store, [_chunk("a.py", "alpha")], ["a.py", "empty.py"])

        assert result["files_without_chunks"] == 1
        assert result["files_skipped"] == 1, "a.py, which really was reused"
        assert result["files_rebuilt"] == 0
        # The ledger's number has to be the index's, not the clone's: `len(files)` here is
        # 2, and only one of those files has any vectors.
        assert result["_recorded_files_indexed"] == 1


def _one_file_that_writes(names):
    def _collect(root):
        made = []
        for name in names:
            path = Path(root) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x = 1\n")
            made.append(path)
        return made

    return _collect
