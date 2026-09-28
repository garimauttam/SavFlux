from __future__ import annotations

from app.core.tenant import current_user_id, reset_current_user_id, set_current_user_id


def test_account_data_directories_are_distinct_and_do_not_use_raw_user_ids(tmp_path, monkeypatch):
    from app.core.config import Settings
    import app.core.paths as paths

    monkeypatch.setattr(paths, "get_settings", lambda: Settings(_env_file=None, chroma_persist_directory=str(tmp_path)))
    first_token = set_current_user_id("user-one@example.com")
    try:
        first = paths.data_dir()
        paths.data_file("private.txt").write_text("first user's data", encoding="utf-8")
    finally:
        reset_current_user_id(first_token)

    second_token = set_current_user_id("user-two@example.com")
    try:
        second = paths.data_dir()
        assert first != second
        assert "user-one" not in str(first)
        assert "user-two" not in str(second)
        assert not paths.data_file("private.txt").exists()
    finally:
        reset_current_user_id(second_token)

    assert paths.data_dir() == tmp_path
    assert current_user_id() is None


def test_streaming_response_retains_verified_tenant_context(client):
    from fastapi.responses import StreamingResponse
    from main import app
    from app.core.tenant import current_user_id

    route_path = "/api/v1/_test/tenant-stream"

    async def stream():
        async def body():
            yield (current_user_id() or "missing-tenant").encode()
        return StreamingResponse(body(), media_type="text/plain")

    app.router.add_api_route(route_path, stream, methods=["GET"], include_in_schema=False)
    try:
        response = client.get(route_path)
        assert response.status_code == 200
        assert response.text == "test-supabase-user"
    finally:
        app.router.routes[:] = [route for route in app.router.routes if getattr(route, "path", None) != route_path]


def test_dependency_graph_cache_is_keyed_by_tenant(tmp_path, monkeypatch):
    import app.services.dep_graph as dep_graph
    import app.services.retrieval_service as retrieval

    collection = type("Collection", (), {"count": lambda self: 4})()
    vectorstore = type("VectorStore", (), {"_collection": collection})()
    built_for = []
    retrieval._dep_graph_cache.clear()
    monkeypatch.setattr(retrieval, "data_dir", lambda: tmp_path / (current_user_id() or "legacy"))
    monkeypatch.setattr(dep_graph, "build_dependency_graph", lambda repo_url=None: built_for.append(current_user_id()) or {"nodes": [], "edges": []})

    for user_id in ("user-one", "user-two", "user-one"):
        token = set_current_user_id(user_id)
        try:
            retrieval._get_dep_graph_hints("find the account module", vectorstore)
        finally:
            reset_current_user_id(token)

    assert built_for == ["user-one", "user-two"]
    retrieval._dep_graph_cache.clear()


def test_chat_model_cache_is_keyed_by_tenant(monkeypatch):
    from app.services import llm_factory, model_service

    llm_factory.get_chat_llm.cache_clear()
    constructed = []

    def build(provider, streaming=False, *, review=False):
        result = object()
        constructed.append((current_user_id(), provider, result))
        return result

    monkeypatch.setattr(model_service, "active_provider", lambda: "ollama")
    monkeypatch.setattr(llm_factory, "_build_chat_llm", build)
    try:
        one_token = set_current_user_id("user-one")
        one = llm_factory.get_chat_llm()
        reset_current_user_id(one_token)
        two_token = set_current_user_id("user-two")
        two = llm_factory.get_chat_llm()
        reset_current_user_id(two_token)

        again_token = set_current_user_id("user-one")
        one_again = llm_factory.get_chat_llm()
        reset_current_user_id(again_token)

        assert one is one_again
        assert two is not one
        assert [item[0] for item in constructed] == ["user-one", "user-two"]
    finally:
        llm_factory.get_chat_llm.cache_clear()
