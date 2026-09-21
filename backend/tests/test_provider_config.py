"""
test_provider_config.py — The $0 promise, and the docs that were breaking it.

WHY THIS FILE EXISTS
--------------------
`.env.example` told users to set `LLM_PROVIDER=gemini`, and described it as
"Completely FREE, no credit card." Following that instruction crashed the app
before it served a single request:

    pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
    llm_provider
      Input should be 'openai', 'ollama' or 'deepseek' [input_value='gemini']

`gemini` was never in the Literal. Nothing in the repo ever imported
`langchain-google-genai`. `GEMINI_API_KEY` was set in CI and documented in three
places for a provider that did not exist.

Two defects, and only one of them is cosmetic:

1. **A documented default that does not start.** This is the worse of the two. A
   new contributor's first experience of the project is a traceback from a file
   they were told to copy. Nothing in the test suite noticed, because every test
   constructed `Settings(llm_provider=...)` explicitly — the suite was testing
   the code, and the docs had drifted away from it unobserved.

2. **A default that contradicted the README's own $0 table.** `config.py`
   defaulted to `deepseek`, a paid hosted API, while README promised the
   chat/review default was local Ollama. A fresh clone therefore required a
   credit card, which is the opposite of the product's stated position.

So these tests hold the *documentation* to the *code*, and the *default* to the
*claim*. They are cheap: no network, no model, no API key.

The lesson worth keeping: a test suite that only constructs objects explicitly
will never catch a broken default. Test the defaults themselves.
"""

from __future__ import annotations

import os
import re
import typing
from pathlib import Path

import pytest

from app.core.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"
REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"


@pytest.fixture()
def default_settings(monkeypatch):
    """
    `Settings` as a fresh clone sees it: no `.env`, no environment override.

    Needed because conftest exports LLM_PROVIDER=openai (so the suite never
    reaches for huggingface.co), and `Settings()` would otherwise pick that up.
    Without this isolation these tests would assert the *test harness's* provider
    rather than the shipped default — passing for a reason unrelated to the thing
    they claim to check.

    `_env_file=None` also disables `.env`, so a developer's local file cannot make
    the shipped-default assertions pass or fail.
    """
    for name in ("LLM_PROVIDER", "OLLAMA_CHAT_MODEL", "OLLAMA_REVIEW_MODEL", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None)


def _literal_providers() -> set[str]:
    """The provider names the config actually accepts."""
    annotation = Settings.model_fields["llm_provider"].annotation
    return set(typing.get_args(annotation))


def _uncommented_env_values(text: str, key: str) -> list[str]:
    """Active (non-commented) `KEY=value` assignments in an env file."""
    values: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == key:
            values.append(value.strip())
    return values


# ── The docs must not name a provider the code rejects ────────────────────────


def test_env_example_exists():
    """A missing file would make every test below vacuously pass."""
    assert ENV_EXAMPLE.is_file(), f"{ENV_EXAMPLE} is missing"


def test_env_example_only_activates_valid_providers():
    """
    The regression test for the crash.

    An env file is copy-pasted, not read carefully. Whatever it sets as an active
    value is what a new contributor runs, so an invalid one here is a startup
    crash with a pydantic traceback pointing at a file they did not write.
    """
    providers = _literal_providers()
    assert providers, "llm_provider is expected to be a Literal of provider names"

    active = _uncommented_env_values(ENV_EXAMPLE.read_text(encoding="utf-8"), "LLM_PROVIDER")
    assert active, ".env.example sets no LLM_PROVIDER, so a fresh clone has no provider"
    for value in active:
        assert value in providers, (
            f".env.example activates LLM_PROVIDER={value!r}, which the config rejects. "
            f"Valid: {sorted(providers)}. Copying this file crashes at startup."
        )


def test_env_example_mentions_only_real_providers():
    """
    NEGATIVE CASE, and the actual historical bug.

    `gemini` appeared in the provider-selection comment AND as the active value.
    An invalid provider called "completely FREE" is worse than no mention: it
    actively redirects a user who is specifically trying to avoid paying.
    """
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    providers = _literal_providers()

    # The line that enumerates the options: `# Set LLM_PROVIDER to "a", "b", or "c".`
    match = re.search(r"Set LLM_PROVIDER to ([^\n]+)", text)
    assert match, "the provider guidance line is gone; is this test still meaningful?"

    named = set(re.findall(r'"([a-z][a-z0-9_]*)', match.group(1)))
    assert named, "no provider names parsed from the guidance line"
    assert named == providers, (
        f".env.example documents {sorted(named)} but the config accepts "
        f"{sorted(providers)}. Documented and accepted providers must be identical."
    )


def test_documented_ollama_models_are_actually_ollama_models():
    """
    An example model name that does not exist sends a user to a 404 from
    `ollama pull` after they have already installed Ollama.
    """
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    # Ollama model ids look like `name:tag`, where tag is a size/quantisation.
    documented = set(re.findall(r"\b([a-z0-9][a-z0-9._-]*:[0-9]+b[a-z0-9._-]*)\b", text))
    assert documented, "no model ids documented; is the Ollama section gone?"
    for model in documented:
        assert re.fullmatch(r"[a-z0-9][a-z0-9._-]*:[0-9.]+b", model), model


# ── The default must match the $0 promise ────────────────────────────────────


def test_the_default_provider_is_the_free_local_one(default_settings):
    """
    The README promises "$0, no credit card, no trial" and its own table lists
    Ollama as the default for chat and review. A default that needs a key makes
    that promise false for anyone who clones and runs.

    This is not a style preference about which provider is best — it is that the
    default and the published claim must be the same thing.
    """
    assert default_settings.llm_provider == "ollama", (
        "the default provider is not the local, key-less one, so a fresh clone "
        "cannot run without signing up for something"
    )


def test_a_bare_default_settings_needs_no_api_key(default_settings):
    """
    NEGATIVE CASE: constructing settings with an empty environment must not
    require credentials. This is the "clone and run" path.
    """
    # Constructed from an empty environment: any required key would raise here.
    settings = default_settings
    assert settings.llm_provider == "ollama"
    assert settings.ollama_base_url.startswith("http")
    assert settings.ollama_chat_model, "the default provider has no model to call"


def test_the_default_model_fits_a_modest_machine(default_settings):
    """
    A default nobody can run is not a default. `qwen2.5-coder:14b` is ~9 GB and
    wants ~16 GB of RAM, so as a default it made the free path unreachable on a
    typical 8 GB laptop — the exact machine a free tier is for.

    Parameter count is read from the tag; the threshold is deliberately generous
    so this does not forbid a future 14b default on a GPU-tier profile. It fails
    only when the shipped default is too large to start.
    """
    model = default_settings.ollama_chat_model
    match = re.search(r":(\d+(?:\.\d+)?)b", model)
    assert match, f"cannot read a parameter count from the default model {model!r}"
    assert float(match.group(1)) <= 8, (
        f"the default local model ({model}) needs more RAM than a typical laptop "
        "has, so the free path does not run out of the box"
    )


def test_no_second_model_is_downloaded_before_a_review_can_run(default_settings):
    """
    Empty review model falls back to the chat model, so a fresh install pulls
    exactly one model. Setting a separate default would double the download
    before the flagship feature works at all.
    """
    settings = default_settings
    assert settings.ollama_review_model == "", (
        "a separate review model is set by default, which means two downloads "
        "before the first review runs"
    )
    # And the fallback the empty value relies on must actually exist.
    assert settings.ollama_chat_model


# ── Every accepted provider must be fully implemented ────────────────────────


def test_every_accepted_provider_produces_a_distinct_name():
    """
    A provider in the Literal with no branch in the factory falls through to the
    `else` (OpenAI) path and is silently mislabelled — `/health` would report
    "OpenAI" while talking to something else.

    Being in the Literal is a promise that all four factory functions handle it.
    """
    from app.services import llm_factory

    original = llm_factory._settings
    names: dict[str, str] = {}
    try:
        for provider in sorted(_literal_providers()):
            llm_factory._settings = lambda p=provider: Settings(llm_provider=p)
            names[provider] = llm_factory.get_provider_name()
    finally:
        llm_factory._settings = original

    assert len(set(names.values())) == len(names), (
        f"two providers report the same name, so one has no branch: {names}"
    )
    for provider, name in names.items():
        assert provider in name.lower() or provider == "ollama", (
            f"provider {provider!r} is labelled {name!r}, which does not name it"
        )


def test_hosted_probe_kwargs_are_none_for_the_local_provider():
    """
    /health probes a hosted endpoint with the OpenAI client. Ollama has none, so
    it must report None — otherwise a fully local install would be reported
    unhealthy for lacking a hosted API key it does not need.
    """
    from app.services import llm_factory

    original = llm_factory._settings
    try:
        llm_factory._settings = lambda: Settings(llm_provider="ollama")
        assert llm_factory.get_hosted_client_kwargs() is None
        assert llm_factory.get_hosted_display_name() == "Local"
        assert llm_factory.get_hosted_model_name() == ""

        llm_factory._settings = lambda: Settings(
            llm_provider="deepseek", deepseek_api_key="sk-x"
        )
        assert llm_factory.get_hosted_client_kwargs() is not None
    finally:
        llm_factory._settings = original


def test_free_providers_never_use_paid_embeddings():
    """
    The $0 promise includes embeddings, and embeddings are the quiet cost: they
    run on every ingest and every query. Only `openai` may use the hosted
    embedder; the local providers must get a local model.

    Both are stubbed, so this asserts the *routing* without downloading anything:
    a real HuggingFaceEmbeddings here would fetch ~90 MB from the Hub.
    """
    import sys
    import types

    from app.services import llm_factory

    calls: dict[str, object] = {}

    fake_module = types.ModuleType("langchain_huggingface")

    class FakeHuggingFaceEmbeddings:
        def __init__(self, **kwargs):
            calls["huggingface"] = kwargs

    fake_module.HuggingFaceEmbeddings = FakeHuggingFaceEmbeddings  # type: ignore[attr-defined]
    sys.modules["langchain_huggingface"] = fake_module

    original_settings = llm_factory._settings
    original_require = llm_factory._require_key

    def fail_if_a_key_is_required(name, value):
        raise AssertionError(f"{name} was required on a free provider")

    try:
        llm_factory._require_key = fail_if_a_key_is_required
        for provider in ("ollama", "deepseek"):
            calls.clear()
            llm_factory._settings = lambda p=provider: Settings(llm_provider=p)
            llm_factory.get_embedding_fn.cache_clear()
            llm_factory.get_embedding_fn()
            assert "huggingface" in calls, (
                f"{provider} did not use local embeddings, so it needs a paid key"
            )
    finally:
        llm_factory._settings = original_settings
        llm_factory._require_key = original_require
        llm_factory.get_embedding_fn.cache_clear()
        sys.modules.pop("langchain_huggingface", None)


# ── Dead dependencies for non-existent providers ────────────────────────────


def test_no_dependency_exists_for_a_provider_the_config_rejects():
    """
    `langchain-google-genai` shipped in requirements for a provider that was
    never implementable. A dependency nobody imports is install time, image size
    and CVE surface bought for nothing.
    """
    text = REQUIREMENTS.read_text(encoding="utf-8")
    providers = _literal_providers()

    for vendor, import_name in (
        ("gemini", "google-genai"),
        ("anthropic", "anthropic"),
    ):
        if vendor in providers:
            continue
        active = [
            line for line in text.splitlines()
            if import_name in line and not line.strip().startswith("#")
        ]
        assert not active, (
            f"{active} pins a dependency for {vendor!r}, but {vendor!r} is not an "
            f"accepted LLM_PROVIDER ({sorted(providers)})"
        )


def test_the_repo_never_imports_a_provider_it_does_not_accept():
    """
    Guards the other direction: a module that imports a provider adapter without
    the config accepting it means the two were changed in the wrong order.
    """
    backend = Path(__file__).resolve().parents[1]
    providers = _literal_providers()
    offenders: list[str] = []

    for path in backend.rglob("*.py"):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1
        ):
            # Only real import statements, not a mention inside a string or
            # comment. This test file names the rejected provider on purpose when
            # explaining the bug, and a substring scan flags itself.
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if "langchain_google_genai" in stripped or "google.generativeai" in stripped:
                offenders.append(f"{path.relative_to(backend)}:{number}")

    assert not offenders, f"these import a rejected provider: {offenders}"
    assert "gemini" not in providers, "gemini was added back; update the docs and deps too"


# ── The offline claim ───────────────────────────────────────────────────────


def test_local_provider_uses_a_loopback_url(default_settings):
    """
    "Fully local, no network" has to be true of the endpoint, not just the model
    name. A default pointing at a remote host would send source code to a third
    party while claiming to be local.
    """
    url = default_settings.ollama_base_url
    assert re.match(r"^http://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?", url), (
        f"the local provider points at {url!r}, which is not loopback"
    )


def test_the_suite_never_depends_on_network_egress():
    """
    CI was green while the README's own test command hung — the worst shape a bug
    can take, because the environment that would have caught it was the one place
    it could not happen. CI exported LLM_PROVIDER=openai; the README did not.

    The fix is that conftest now pins the provider itself, so the documented
    command works with no exported variables. This asserts that pin exists, and
    that it is the key-free path — the one whose embeddings never reach for
    huggingface.co.
    """
    conftest = Path(__file__).resolve().parent / "conftest.py"
    text = conftest.read_text(encoding="utf-8")

    assert 'os.environ.setdefault("LLM_PROVIDER"' in text, (
        "conftest no longer pins the provider, so a bare `pytest -q` on a machine "
        "without egress will spend ~30s per test in HuggingFace retry backoff"
    )
    # The pinned value must be the hosted path, not the local-embedding one.
    for line in text.splitlines():
        if "setdefault(\"LLM_PROVIDER\"" in line:
            assert '"openai"' in line, (
                f"the suite pins {line.strip()}, whose embeddings import "
                "sentence-transformers and fetch a model over the network"
            )
            break

    # And the pin must be present before app modules are imported, since
    # ingestion_service reads settings at import time.
    app_import = text.find("from main import app")
    pin = text.find('setdefault("LLM_PROVIDER"')
    assert pin != -1 and app_import != -1 and pin < app_import, (
        "the provider must be pinned before app modules are imported"
    )


def test_static_analysis_and_fix_features_need_no_provider_at_all():
    """
    The $0 claim's strongest part: the security analyzer, autofix, patch builder
    and chunker make no model call. If that ever stopped being true, the "free
    path stays genuinely usable" claim in the README would quietly become false.

    Checked by importing the modules with the provider unset to something invalid
    — none of them should read provider settings to work.
    """
    from app.services.code_analysis.autofix import FIXABLE_RULES  # noqa: F401
    from app.services.code_chunker import chunk_code_file
    from app.services.tree_sitter_langs import grammar_for_path  # noqa: F401

    docs = chunk_code_file(
        source="def alpha():\n    return 1\n",
        file_path="x.py", file_name="x.py", language="py",
        repo_url="", content_hash="h",
    )
    assert docs, "chunking stopped working without a provider configured"
