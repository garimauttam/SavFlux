"""
Tests for the review planner and its caches.

The planner is a *budget*: it decides which files get a language model and which
do not. A budget that is not tested is a silent omission, so the assertions here
are about the properties that make it trustworthy rather than about a particular
routing table:

  * every file ends up with exactly one dispatch, and static files never reach a
    model — that is the whole safety story
  * a file the parser has fully decided does not consume the model budget
  * a file the analyzer proved something about always gets its own call
  * routing is deterministic: the same batch plans identically, twice
  * batching reduces calls without dropping files
  * the cache returns a previous answer only for a byte-identical question, and
    says out loud that it did

The cache tests deliberately mutate one input at a time (content, model, mode,
prompt version) because the failure mode of a content-addressed cache is
answering a question that was never asked.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from app.services import review_cache
from app.services.review_cache import (
    file_key,
    batch_key,
    hash_content,
)
from app.services.review_planner import (
    PLANNER_VERSION,
    ROUTE_SKIP,
    batch_id_for,
    plan_review,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

SENSITIVE = '''\
import hashlib
import requests

TOKEN_SALT = "static-salt"


def fetch_profile(url: str) -> dict:
    response = requests.get(url, verify=False, timeout=10)
    return response.json()


def token_digest(token: str) -> str:
    return hashlib.md5((token + TOKEN_SALT).encode()).hexdigest()
'''

ORDINARY = '''\
"""Small helpers shared by the reporting layer."""


def add(a, b):
    return a + b


def label(value, prefix=""):
    return f"{prefix}{value}"


def clamp(value, low=0, high=100):
    """Keep a value inside an inclusive range."""
    return max(low, min(high, value))


def average(values):
    if not values:
        return 0.0
    return sum(values) / len(values)
'''

CLEAN_LONG = "x = 1\n" * 120
LOCKFILE = '{"lockfileVersion": 3, "packages": {"node_modules/react": {}}}\n' * 20
YAML_CI = "steps:\n  - run: pytest\n  - run: npm run build\n" * 12
HUGE = "def f():\n    return 1\n" * 3000   # > MAX_LLM_FILE_CHARS


def file(name: str, content: str, language: str = "py") -> dict:
    return {"file_name": name, "content": content, "language": language}


def scores_for(files: list[dict]) -> dict[int, int]:
    """Use the production triage score so the planner is tested against reality."""
    from app.services.multi_review_agent import _triage_score

    return {i: _triage_score(f) for i, f in enumerate(files)}


def plan(files: list[dict], **kwargs):
    return plan_review(files, scores=kwargs.pop("scores", scores_for(files)), **kwargs)


# ── Dispatch invariants ───────────────────────────────────────────────────────


def test_every_file_gets_exactly_one_dispatch():
    files = [
        file("auth_service.py", SENSITIVE),
        file("utils.py", ORDINARY),
        file("package-lock.json", LOCKFILE, "json"),
        file("ci.yml", YAML_CI, "yml"),
        file("stub.py", "x = 1\n"),
        file("big.py", HUGE),
    ]
    review_plan = plan(files)

    assert set(review_plan.dispatches) == set(range(len(files)))
    assert len(review_plan.dispatches) == len(files)


def test_static_files_are_never_routed_to_a_model():
    """
    The safety property of the whole optimisation: if a file is dispatched
    static, its route must be the skip sentinel, so no caller can accidentally
    hand it to a provider.
    """
    files = [
        file("package-lock.json", LOCKFILE, "json"),
        file("stub.py", "x = 1\n"),
        file("big.py", HUGE),
    ]
    review_plan = plan(files)

    for index in review_plan.static_indexes:
        assert review_plan.of(index).route == ROUTE_SKIP
        assert review_plan.of(index).kind == "static"


def test_a_generated_lockfile_is_planned_static_with_specific_reasons():
    review_plan = plan([file("package-lock.json", LOCKFILE, "json")])
    dispatch = review_plan.of(0)

    assert dispatch.kind == "static"
    assert any("lock" in reason for reason in dispatch.reasons)


def test_a_trivial_stub_is_static_and_the_reason_says_why():
    review_plan = plan([file("stub.py", "x = 1\n")])
    dispatch = review_plan.of(0)

    assert dispatch.kind == "static"
    assert any("nothing to review" in reason for reason in dispatch.reasons)


def test_an_oversized_file_goes_static_rather_than_being_reviewed_truncated():
    """A review of the first 24k chars of a 60k-char file is a misleading review."""
    review_plan = plan([file("generated_bundle.py", HUGE)])
    dispatch = review_plan.of(0)

    assert dispatch.kind == "static"
    assert any("review window" in reason for reason in dispatch.reasons)


def test_data_files_are_static_but_a_config_with_secrets_is_not():
    """
    `.env`-ish content is data, so the analyzer covers it — unless it carries
    credentials, which is exactly what a reviewer wants a second look at.
    """
    review_plan = plan([
        file("ci.yml", YAML_CI, "yml"),
        file("deploy.yml", "password: hunter2\nsteps:\n  - run: deploy\n" * 8, "yml"),
    ])

    assert review_plan.of(0).kind == "static"
    assert review_plan.of(1).kind in {"single", "batch"}
    assert review_plan.of(1).kind == "single"


def test_a_file_with_proven_findings_always_gets_its_own_call():
    review_plan = plan([file("auth_service.py", SENSITIVE)])
    dispatch = review_plan.of(0)

    assert dispatch.kind == "single"
    assert any("deterministic finding" in reason for reason in dispatch.reasons)


# ── The budget ────────────────────────────────────────────────────────────────


def test_the_budget_bounds_single_reviews_but_never_drops_a_file():
    files = [file(f"auth_service_{i}.py", SENSITIVE) for i in range(20)]
    review_plan = plan(files, llm_budget=5)

    assert len(review_plan.single_indexes) == 5
    # Everything above the budget still has to be accounted for: deferred files
    # keep their reasons so the plan can explain what was put off.
    assert len(review_plan.batched_indexes) == 15
    assert len(review_plan.static_indexes) == 0
    for index in review_plan.batched_indexes:
        assert review_plan.of(index).reasons


def test_a_static_file_does_not_consume_the_model_budget():
    """
    This is the core of the speed-up. A repo full of generated files must still
    spend its budget on the files that need it.
    """
    files = [file(f"package-lock-{i}.json", LOCKFILE, "json") for i in range(10)]
    files += [file("auth_service.py", SENSITIVE)]
    review_plan = plan(files, llm_budget=1)

    assert review_plan.static_indexes == list(range(10))
    assert review_plan.of(10).kind == "single"


# ── Batching ──────────────────────────────────────────────────────────────────


def test_batching_collapses_many_files_into_few_calls():
    files = [file(f"module_{i}.py", ORDINARY) for i in range(12)]
    review_plan = plan(files, llm_budget=0)

    assert review_plan.batched_indexes == list(range(12))
    # 12 files, at most 4 per call
    assert review_plan.batches
    assert review_plan.model_calls == len(review_plan.batches)
    assert review_plan.model_calls < 12
    for batch in review_plan.batches.values():
        assert 3 <= batch.size <= 4


def test_no_file_is_placed_in_two_batches_and_none_is_left_out():
    """
    9 files: two calls of four, and one file left over. The leftover is reviewed
    on its own rather than sharing a prompt with nobody — so the invariant is
    "every file is in exactly one batch, or dispatched singly", not "everything is
    batched".
    """
    files = [file(f"module_{i}.py", ORDINARY) for i in range(9)]
    review_plan = plan(files, llm_budget=0)

    seen: list[int] = []
    for batch in review_plan.batches.values():
        seen.extend(batch.indexes)

    assert len(seen) == len(set(seen))                       # no file twice
    assert set(seen) | set(review_plan.single_indexes) == set(range(9))
    assert not (set(seen) & set(review_plan.single_indexes))  # no file both ways
    assert review_plan.single_indexes == [8]


def test_a_tail_too_small_to_batch_is_reviewed_individually():
    """One leftover file shares a prompt with nobody; give it its own call."""
    files = [file(f"module_{i}.py", ORDINARY) for i in range(5)]
    review_plan = plan(files, llm_budget=0)

    assert len(review_plan.batches) == 1          # four files share one call
    assert review_plan.of(4).kind == "single"     # the fifth stands alone


def test_batch_ids_are_deterministic_for_the_same_content():
    files = [file("a.py", ORDINARY), file("b.py", ORDINARY)]
    assert batch_id_for(files) == batch_id_for(files)
    assert batch_id_for(files) != batch_id_for([file("a.py", ORDINARY + "\n")])


def test_planning_is_deterministic():
    """Same input, same plan — the agent's $0 claim depends on this."""
    files = [file(f"module_{i}.py", ORDINARY) for i in range(12)]
    files.append(file("auth_service.py", SENSITIVE))

    first = plan(files, llm_budget=3)
    second = plan(files, llm_budget=3)

    assert first.dispatches == second.dispatches
    assert first.batches == second.batches
    assert first.stats(len(files)) == second.stats(len(files))


def test_the_plan_explains_itself_for_every_file():
    files = [file("auth_service.py", SENSITIVE), file("package-lock.json", LOCKFILE, "json")]
    review_plan = plan(files)

    for index in range(len(files)):
        explanation = review_plan.explain(index)
        assert explanation["kind"] in {"static", "batch", "single"}
        assert explanation["reasons"], "every dispatch must carry a reason"
        assert explanation["planner_version"] if "planner_version" in explanation else True
    stats = review_plan.stats(len(files))
    assert stats["planner_version"] == PLANNER_VERSION
    assert stats["files"] == 2


def test_plan_stats_account_for_every_file():
    files = [file("auth_service.py", SENSITIVE)] + [file(f"m{i}.py", ORDINARY) for i in range(6)]
    files.append(file("package-lock.json", LOCKFILE, "json"))
    review_plan = plan(files, llm_budget=1)
    stats = review_plan.stats(len(files))

    assert stats["static_only"] + stats["single_reviews"] + stats["batched_files"] == len(files)
    assert stats["model_calls"] == stats["single_reviews"] + stats["batch_count"]
    assert stats["model_calls"] < len(files)


# ── Cache keys ────────────────────────────────────────────────────────────────


def test_a_key_changes_when_anything_that_affects_the_answer_changes():
    """
    Each of these is a different question, so each must be a different key. A
    cache that conflated any pair would serve an answer to a question nobody
    asked.
    """
    base = dict(
        content=SENSITIVE, file_name="auth.py", language="py",
        provider="openai", model="gpt-4o-mini", mode="fast",
    )
    key = file_key(**base)

    assert file_key(**{**base, "content": SENSITIVE + "\n"}) != key
    assert file_key(**{**base, "file_name": "other.py"}) != key
    assert file_key(**{**base, "language": "js"}) != key
    assert file_key(**{**base, "provider": "ollama"}) != key
    assert file_key(**{**base, "model": "qwen2.5-coder:7b"}) != key
    assert file_key(**{**base, "mode": "agentic"}) != key


def test_whitespace_matters_because_the_model_sees_whitespace():
    assert file_key("a = 1\n", "x.py") != file_key("a = 1", "x.py")


def test_batch_key_depends_on_member_order():
    a, b = hash_content(SENSITIVE), hash_content(ORDINARY)
    assert batch_key([a, b]) != batch_key([b, a])
    assert batch_key([a, b]) == batch_key([a, b])


def test_prompt_version_is_part_of_the_identity(monkeypatch):
    """Bumping the question the model is asked must invalidate old answers."""
    before = file_key("x = 1\n", "x.py")
    monkeypatch.setattr(review_cache, "PROMPT_VERSION", review_cache.PROMPT_VERSION + 1)
    assert file_key("x = 1\n", "x.py") != before


# ── Cache behaviour ───────────────────────────────────────────────────────────


def test_cache_round_trip(isolated_data_dir):
    key = file_key("x = 1\n", "x.py")
    assert review_cache.get(key) is None

    review_cache.put(key, "review text", kind="static", meta={"file": "x.py"})
    entry = review_cache.get(key)

    assert entry is not None
    assert entry["value"] == "review text"
    assert entry["meta"]["file"] == "x.py"
    assert entry["hits"] == 1


def test_a_hit_records_that_it_was_read(isolated_data_dir):
    """The hit count is what makes eviction LRU rather than FIFO."""
    key = file_key("x = 1\n", "x.py")
    review_cache.put(key, "v", kind="static")

    review_cache.get(key)
    review_cache.get(key)
    assert review_cache.get(key)["hits"] == 3


def test_cache_evicts_least_recently_used_when_full(isolated_data_dir, monkeypatch):
    monkeypatch.setattr(review_cache, "MAX_ENTRIES", 3)
    for i in range(3):
        review_cache.put(f"k{i}", f"v{i}", kind="static")

    review_cache.get("k0")            # k0 becomes the most recently used
    review_cache.put("k3", "v3", kind="static")   # evicts k1

    assert review_cache.get("k0") is not None
    assert review_cache.get("k1") is None
    assert review_cache.get("k3") is not None


def test_a_corrupt_cache_file_is_a_cold_cache_not_a_crash(isolated_data_dir):
    review_cache._cache_path().write_text("{ this is not json", encoding="utf-8")
    assert review_cache.get("anything") is None
    review_cache.put("k", "v", kind="static")
    assert review_cache.get("k")["value"] == "v"


def test_stats_report_a_hit_rate(isolated_data_dir):
    review_cache.put(file_key("a\n", "a.py"), "v", kind="static")
    review_cache.get(file_key("a\n", "a.py"))
    review_cache.get(file_key("missing\n", "b.py"))

    stats = review_cache.stats()
    assert stats["entries"] >= 1
    assert stats["writes"] >= 1
    assert 0.0 <= stats["hit_rate"] <= 1.0
    assert "session" in stats


def test_clear_empties_the_cache(isolated_data_dir):
    review_cache.put("k", "v", kind="static")
    assert review_cache.stats()["entries"] >= 1
    assert review_cache.clear() >= 1
    assert review_cache.stats()["entries"] == 0


# ── End to end through the orchestrator ───────────────────────────────────────


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        review_mode="fast",
        review_max_full_files=80,
        review_llm_budget=12,
        review_concurrency=2,
        llm_provider="ollama",
        summary_mixture_models="",
        review_cache_enabled=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _DeadLLM:
    """
    A summary model that fails instantly.

    The repo summary is one more model call that none of these tests are about,
    and letting it reach a real provider would mean a connection timeout per test
    (and a network dependency in CI). Failing it fast exercises the deterministic
    summary fallback, which is the path production takes when a provider is down.
    """

    def astream(self, messages):  # noqa: D102 - duck-typed LangChain surface
        async def gen():
            raise RuntimeError("summary disabled in tests")
            yield ""  # pragma: no cover - makes this an async generator
        return gen()


def _run_multi_review(files: list[dict], settings, fake_stream=None):
    """Drive the real orchestrator with a fake model and no network."""
    from app.services import multi_review_agent as mra

    calls: list[str] = []

    async def stream(file_name, content, language, repo_context="", model_override=""):
        calls.append(file_name)
        yield f"model review for {file_name}"

    async def batch(files_slice, repo_context_map=None, model_override=""):
        names = [f["file_name"] for f in files_slice]
        calls.append("+".join(names))
        yield "".join(
            f"=== FILE: {n} ===\n## 🐛 Bugs & Risks\nNone found\n=== END FILE ===\n" for n in names
        )

    async def collect():
        with patch.object(mra, "stream_fast_code_review", fake_stream or stream), \
             patch.object(mra, "stream_code_review", fake_stream or stream), \
             patch.object(mra, "stream_batch_code_review", batch), \
             patch.object(mra, "get_settings", return_value=settings), \
             patch.object(mra, "get_chat_llm", lambda *a, **k: _DeadLLM()):
            return "".join([token async for token in mra.stream_multi_review(files)])

    return asyncio.run(collect()), calls


def test_generated_files_never_reach_the_model(isolated_data_dir):
    files = [
        file("auth_service.py", SENSITIVE),
        file("package-lock.json", LOCKFILE, "json"),
        file("stub.py", "x = 1\n"),
    ]
    output, calls = _run_multi_review(files, _settings())

    assert calls == ["auth_service.py"]
    assert "package-lock.json" in output          # still reported, just not by a model
    assert "Static analysis" in output


def test_ordinary_files_share_one_model_call(isolated_data_dir):
    """Four ordinary files: four sections in the output, one call to produce them."""
    files = [file(f"module_{i}.py", ORDINARY) for i in range(4)]
    output, calls = _run_multi_review(files, _settings())

    assert len(calls) == 1, calls
    assert calls[0] == "module_0.py+module_1.py+module_2.py+module_3.py"
    for i in range(4):
        assert f'"file_name": "module_{i}.py"' in output
    assert output.count("## 🐛 Bugs & Risks") == 4   # each file got its own section
    assert "Cached review" not in output


def test_an_unchanged_file_is_answered_from_the_cache_and_says_so(isolated_data_dir):
    """
    The headline behaviour: the second run of the same repo makes no model calls,
    and every file says why it did not need one.
    """
    files = [file("auth_service.py", SENSITIVE), file("module_0.py", ORDINARY)]

    first_output, first_calls = _run_multi_review(files, _settings())
    assert first_calls, "the first run must make the calls"
    assert "Cached review" not in first_output

    second_output, second_calls = _run_multi_review(files, _settings())

    assert second_calls == [], "an unchanged repo must not call the model again"
    assert second_output.count("Cached review") == 2
    assert "sha256" in second_output          # names the digest it matched
    assert "no model call was made" in second_output.lower()


def test_changing_one_file_re_reviews_only_that_file(isolated_data_dir):
    """The delta property: editing one file must not re-review the repo."""
    files = [file("module_0.py", ORDINARY), file("module_1.py", ORDINARY)]
    _run_multi_review(files, _settings())

    changed = [file("module_0.py", ORDINARY + "\n\ndef extra():\n    return 2\n"),
               file("module_1.py", ORDINARY)]
    output, calls = _run_multi_review(changed, _settings())

    assert calls == ["module_0.py"], calls   # the edited file, and only it
    assert "Cached review" in output          # the untouched one came from cache
    # The unchanged file still has a full section — a hit is not a gap.
    assert "module_1.py" in output


def test_the_cache_does_not_answer_a_different_model(isolated_data_dir):
    """Switching provider must not be served the previous provider's review."""
    files = [file("auth_service.py", SENSITIVE)]
    _run_multi_review(files, _settings(llm_provider="ollama"))

    _, calls = _run_multi_review(files, _settings(llm_provider="openai"))
    assert calls, "a different provider is a different question"


def test_switching_mode_is_not_a_cache_hit(isolated_data_dir):
    files = [file("auth_service.py", SENSITIVE)]
    _run_multi_review(files, _settings(review_mode="fast"))

    _, calls = _run_multi_review(files, _settings(review_mode="agentic"))
    assert calls, "the agentic review is a different review"


def test_cache_can_be_turned_off(isolated_data_dir):
    files = [file("auth_service.py", SENSITIVE)]
    settings = _settings(review_cache_enabled=False)

    _run_multi_review(files, settings)
    _, calls = _run_multi_review(files, settings)

    assert calls, "with the cache off, every run is a fresh call"


def test_a_batch_that_omits_a_file_falls_back_to_static_for_that_file(isolated_data_dir):
    """
    Attributing one file's findings to another is the worst failure a batched
    review can have, so a missing section must produce a static report — never a
    neighbour's text.
    """
    from app.services import multi_review_agent as mra

    files = [file(f"module_{i}.py", ORDINARY) for i in range(4)]
    settings = _settings()

    async def partial_batch(files_slice, repo_context_map=None, model_override=""):
        # The model answers about three of the four files.
        yield "".join(
            f"=== FILE: {f['file_name']} ===\n## 🔒 Security\nNone found\n=== END FILE ===\n"
            for f in files_slice[:3]
        )

    async def collect():
        with patch.object(mra, "stream_batch_code_review", partial_batch), \
             patch.object(mra, "get_settings", return_value=settings), \
             patch.object(mra, "get_chat_llm", lambda *a, **k: _DeadLLM()):
            return "".join([token async for token in mra.stream_multi_review(files)])

    output = asyncio.run(collect())

    assert "module_3.py" in output
    assert "Static analysis" in output
    assert "no section returned in the batched review" in output


def test_a_failing_batch_does_not_lose_its_files(isolated_data_dir):
    from app.services import multi_review_agent as mra

    files = [file(f"module_{i}.py", ORDINARY) for i in range(4)]

    async def exploding_batch(files_slice, repo_context_map=None, model_override=""):
        raise RuntimeError("provider exploded")
        yield  # pragma: no cover - makes this an async generator

    async def collect():
        with patch.object(mra, "stream_batch_code_review", exploding_batch), \
             patch.object(mra, "get_settings", return_value=_settings()), \
             patch.object(mra, "get_chat_llm", lambda *a, **k: _DeadLLM()):
            return "".join([token async for token in mra.stream_multi_review(files)])

    output = asyncio.run(collect())

    for i in range(4):
        assert f'"file_name": "module_{i}.py"' in output
    assert "Static analysis" in output
    assert "provider exploded" not in output  # the provider's words stay in the log


def test_run_telemetry_reports_calls_and_cache_hits(isolated_data_dir):
    files = [file("auth_service.py", SENSITIVE), file("module_0.py", ORDINARY)]
    _run_multi_review(files, _settings())
    output, _ = _run_multi_review(files, _settings())

    assert '"step": "timing"' in output
    assert '"cache_hits": 2' in output
    assert '"step": "planned"' in output


def test_the_cache_endpoints_report_and_clear(isolated_data_dir, client):
    """
    The cache is user-visible state, so it is reachable over the API: stats
    without a key (it is a read), clearing with one (it throws away paid work).
    """
    from app.services import review_cache

    review_cache.put(file_key("a\n", "a.py"), "v", kind="static")

    stats = client.get("/api/v1/review/cache")
    assert stats.status_code == 200
    body = stats.json()
    assert body["entries"] >= 1
    assert set(body) >= {"entries", "bytes", "hits", "writes", "hit_rate"}

    cleared = client.delete("/api/v1/review/cache")
    assert cleared.status_code == 200
    assert cleared.json()["entries"] == 0
    assert review_cache.get(file_key("a\n", "a.py")) is None


# ── The provider circuit ──────────────────────────────────────────────────────


def test_the_circuit_opens_after_repeated_failures_and_stops_calling():
    """
    Without this, a review of 60 files against an unreachable provider pays a
    connection timeout per file — the exact "review takes 30s and reports nothing
    the analyzer hadn't already found" experience the planner exists to remove.
    """
    from app.services.multi_review_agent import _ProviderCircuit

    circuit = _ProviderCircuit(failure_threshold=3, cooldown_seconds=60.0)

    assert circuit.allows_call()
    for _ in range(3):
        circuit.record_failure("provider call failed")

    assert circuit.is_open
    assert not circuit.allows_call()
    assert circuit.snapshot()["open"] is True


def test_a_success_closes_the_circuit():
    from app.services.multi_review_agent import _ProviderCircuit

    circuit = _ProviderCircuit(failure_threshold=2, cooldown_seconds=60.0)
    circuit.record_failure("a")
    circuit.record_failure("b")
    assert circuit.is_open

    circuit.record_success()

    assert not circuit.is_open
    assert circuit.allows_call()
    assert circuit.snapshot()["consecutive_failures"] == 0


def test_exactly_one_probe_is_allowed_after_the_cooldown(monkeypatch):
    """A recovered provider is found again, but not by every queued file at once."""
    from app.services import multi_review_agent as mra

    circuit = mra._ProviderCircuit(failure_threshold=2, cooldown_seconds=30.0)
    circuit.record_failure("a")
    circuit.record_failure("b")
    assert not circuit.allows_call()

    # Move past the cooldown window.
    real = mra._time.monotonic()
    monkeypatch.setattr(mra._time, "monotonic", lambda: real + 31)

    assert circuit.allows_call()          # the single probe
    assert not circuit.allows_call()      # and nothing else, until it reports back


def test_a_failed_probe_starts_a_fresh_cooldown(monkeypatch):
    from app.services import multi_review_agent as mra

    circuit = mra._ProviderCircuit(failure_threshold=1, cooldown_seconds=30.0)
    circuit.record_failure("a")

    real = mra._time.monotonic()
    monkeypatch.setattr(mra._time, "monotonic", lambda: real + 31)
    assert circuit.allows_call()

    circuit.record_failure("probe failed")
    monkeypatch.setattr(mra._time, "monotonic", lambda: real + 40)
    assert circuit.is_open, "the window restarts, so the provider is left alone"


def test_an_open_circuit_still_produces_a_review(isolated_data_dir):
    """
    The circuit is a latency measure, not a coverage one: every file still gets
    its deterministic review and the output says the model was skipped.
    """
    from app.services import multi_review_agent as mra

    files = [file("auth_service.py", SENSITIVE), file("module_0.py", ORDINARY)]
    circuit = mra._ProviderCircuit(failure_threshold=1, cooldown_seconds=600.0)
    circuit.record_failure("provider call failed")

    attempts: list[str] = []

    async def should_not_be_called(*args, **kwargs):
        attempts.append("called")
        yield "model review"

    with patch.object(mra, "_PROVIDER_CIRCUIT", circuit):
        output, calls = _run_multi_review(files, _settings(), fake_stream=should_not_be_called)

    assert attempts == [], "no model call may be attempted while the circuit is open"
    assert "Static analysis" in output
    assert "model provider is failing; call skipped" in output
    for name in ("auth_service.py", "module_0.py"):
        assert name in output


def test_coverage_does_not_count_a_cached_static_report_as_an_llm_review(isolated_data_dir):
    """
    Overstating model coverage is the one number in this pipeline a user cannot
    verify by eye, so it is tested directly: a static report served from cache is
    static analysis, not a model review.
    """
    from app.services import multi_review_agent as mra

    files = [file("package-lock.json", LOCKFILE, "json"), file("stub.py", "x = 1\n")]
    settings = _settings()

    async def collect():
        with patch.object(mra, "get_settings", return_value=settings), \
             patch.object(mra, "get_chat_llm", lambda *a, **k: _DeadLLM()):
            return "".join([tok async for tok in mra.stream_multi_review(files)])

    asyncio.run(collect())            # cold: both files analysed and cached
    output = asyncio.run(collect())   # warm: both served from cache

    assert output.count("Cached review") == 2
    assert '"llm": 0' in output
    assert '"static": 2' in output
    assert '"cache_hits": 2' in output


def test_an_open_circuit_skips_the_summary_call_too(isolated_data_dir):
    """
    The summary is one more model call, and on an unchanged repo it is often the
    only thing left to pay for. When the provider has been failing all review
    long, asking it once more buys a connection timeout at the end of an
    otherwise instant run.
    """
    from app.services import multi_review_agent as mra

    files = [file(f"module_{i}.py", ORDINARY) for i in range(4)]
    circuit = mra._ProviderCircuit(failure_threshold=1, cooldown_seconds=600.0)
    circuit.record_failure("provider call failed")

    with patch.object(mra, "_PROVIDER_CIRCUIT", circuit):
        output, _ = _run_multi_review(files, _settings())

    assert "deterministic summary (provider not answering)" in output
    assert "Generating repo summary" not in output
    assert "Repo summary ready" in output          # the summary still arrives
    assert output.count("Repo summary ready") == 1  # exactly once


def test_a_healthy_circuit_still_asks_the_model_for_the_summary(isolated_data_dir):
    """The short-circuit must be conditional on failures, not a blanket change."""
    from app.services import multi_review_agent as mra

    files = [file(f"module_{i}.py", ORDINARY) for i in range(4)]

    with patch.object(mra, "_PROVIDER_CIRCUIT", mra._ProviderCircuit()):
        output, _ = _run_multi_review(files, _settings())

    # The fake summary model fails, so the deterministic fallback is used — but
    # the run had to *try* the model first, which is what this asserts.
    assert "Generating repo summary" in output
    assert "deterministic summary (provider not answering)" not in output
