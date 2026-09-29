import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from app.services import review_suggestions as svc

CODE = 'def f():\n    return 1\n'
PROPOSAL = dict(line=2, end_line=2, original='    return 1', replacement='    return 2')
BODY = dict(source='repo::a.py', content_sha256=svc.source_hash(CODE), line=2, end_line=2,
            title='Fix return', reason='Example', remediation='Return two')


def validate(p):
    return svc.validate_proposal(json.dumps(p), CODE, 2, 2, 1, 3)


def test_proposal_is_source_checked_but_never_applied():
    result = validate(PROPOSAL)
    assert result['original'] == '    return 1'
    assert result['applied'] is False
    assert result['validation'] == 'source-match-only'
    assert result['content_sha256'] == svc.source_hash(CODE)
    assert validate({**PROPOSAL, 'replacement': ''})['replacement'] == ''


@pytest.mark.parametrize('changes', [dict(line=True), dict(line=0), dict(end_line=9),
    dict(original='return 1'), dict(replacement='    return 1'), dict(replacement='x' * 20001)])
def test_invalid_edits_are_rejected(changes):
    with pytest.raises(svc.SuggestionError):
        validate({**PROPOSAL, **changes})


@pytest.mark.parametrize('raw', ['not json', '[]', '{"unavailable":true}', 'x' * 60001])
def test_malformed_or_unsupported_proposals_are_rejected(raw):
    with pytest.raises(svc.SuggestionError):
        svc.validate_proposal(raw, CODE, 2, 2, 1, 3)


@pytest.mark.asyncio
async def test_configured_model_receives_bounded_untrusted_context(monkeypatch):
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps(PROPOSAL))))
    factory = Mock(return_value=model)
    monkeypatch.setattr(svc, 'get_review_llm', factory)
    result = await svc.propose_fix(CODE, 'repo::a.py', 2, 2, 'Fix', 'Why', 'How')
    factory.assert_called_once_with(streaming=False)
    messages = model.ainvoke.call_args.args[0]
    assert 'untrusted' in messages[0].content
    assert 'Do not repeat the original code' in messages[0].content
    assert '2:     return 1' in messages[1].content
    assert result['replacement'] == '    return 2'


@pytest.mark.asyncio
@pytest.mark.parametrize('disconnect', [True, False])
async def test_disconnect_and_timeout_cancel_provider(disconnect):
    cancelled = asyncio.Event()
    async def work():
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
    with pytest.raises(asyncio.CancelledError if disconnect else TimeoutError):
        await svc.until_disconnect(work(), AsyncMock(return_value=disconnect), timeout=.01)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_success_cleans_up_disconnect_watcher():
    assert await svc.until_disconnect(AsyncMock(return_value=42)(), AsyncMock(return_value=False)) == 42


def test_exact_source_does_not_use_basename_fallback(monkeypatch):
    from app.services import ingestion_service
    collection = Mock()
    collection.get.return_value = {'documents': ['other user source'], 'metadatas': [{'source': 'other::a.py'}]}
    monkeypatch.setattr(ingestion_service, '_get_vectorstore', lambda: SimpleNamespace(_collection=collection))
    assert svc.read_exact_source('repo::a.py') is None
    collection.get.assert_called_once_with(where={'source': 'repo::a.py'}, include=['documents', 'metadatas'])


def test_endpoint_checks_snapshot_and_never_calls_provider_for_stale_code(client, monkeypatch):
    monkeypatch.setattr(svc, 'read_exact_source', lambda _: 'changed')
    provider = AsyncMock()
    monkeypatch.setattr(svc, 'propose_fix', provider)
    response = client.post('/api/v1/review/suggestion', json=BODY)
    assert response.status_code == 409
    provider.assert_not_called()


def test_endpoint_returns_checked_proposal(client, monkeypatch):
    monkeypatch.setattr(svc, 'read_exact_source', lambda _: CODE)
    monkeypatch.setattr(svc, 'propose_fix', AsyncMock(return_value=validate(PROPOSAL)))
    response = client.post('/api/v1/review/suggestion', json=BODY)
    assert response.status_code == 200, response.text
    assert response.json()['applied'] is False


def test_endpoint_requires_authentication(client):
    assert client.post('/api/v1/review/suggestion', json=BODY, headers={'Authorization': ''}).status_code == 401


def test_compact_proposal_derives_original_from_snapshot():
    proposal = {k: v for k, v in PROPOSAL.items() if k != "original"}
    assert validate(proposal) == validate(PROPOSAL)
    assert validate({**proposal, "replacement": ""})["original"] == PROPOSAL["original"]
    with pytest.raises(svc.SuggestionError, match="unchanged"):
        validate({**proposal, "replacement": PROPOSAL["original"]})


@pytest.mark.parametrize("changes", [dict(line=0), dict(end_line=99), dict(line=3, end_line=3), dict(original=None)])
def test_compact_proposals_keep_range_and_echo_validation(changes):
    proposal = {k: v for k, v in PROPOSAL.items() if k != "original"}
    with pytest.raises(svc.SuggestionError):
        validate({**proposal, **changes})


def test_endpoint_timeout_gives_actionable_guidance(client, monkeypatch):
    monkeypatch.setattr(svc, "read_exact_source", lambda _: CODE)
    monkeypatch.setattr(svc, "propose_fix", AsyncMock(side_effect=TimeoutError))
    response = client.post("/api/v1/review/suggestion", json=BODY)
    assert response.status_code == 504
    assert "No changes were made" in response.json()["detail"]
    assert "smaller local Review model" in response.json()["detail"]
