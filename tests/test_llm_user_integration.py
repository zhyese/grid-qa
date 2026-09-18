import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


def test_llm_user_callback_signature(monkeypatch):
    from app.config import settings
    from app.routers import integrations

    monkeypatch.setattr(settings, "LLM_USER_CALLBACK_SECRET", "test-secret")
    monkeypatch.setattr(settings, "LLM_USER_CALLBACK_MAX_SKEW_SECONDS", 300)
    raw = b'{"eventId":"evt"}'
    timestamp = str(int(time.time()))
    signature = hmac.new(b"test-secret", timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    integrations._verify(raw, timestamp, signature)
    with pytest.raises(HTTPException):
        integrations._verify(raw, timestamp, "bad")


@pytest.mark.asyncio
async def test_evaluation_router_sends_knowledge_gap_to_evolution(monkeypatch):
    from app.services import llm_user_integration_service as service

    emitted, collected, scans = [], [], []

    async def fake_emit(source, type, payload, tenant="default"):
        emitted.append((source, type, tenant))
        return "qe-1"

    async def fake_collect(*args):
        collected.append(args)
        return "gap-1"

    async def fake_scan(tenant, since_hours=168, model_type=None):
        scans.append((tenant, since_hours))
        return {"taskId": "t-1"}

    monkeypatch.setattr(service.quality_event_bus, "emit", fake_emit)
    monkeypatch.setattr("app.services.evidence_gap_service.collect", fake_collect)
    monkeypatch.setattr("app.services.knowledge_evolution_service.enqueue_evolution_scan", fake_scan)
    result = await service.handle_evaluation(
        {"runId": "run-1", "scenarioVersionId": "sv-1", "rootCause": "knowledge_gap", "query": "q", "answer": "a"},
        SimpleNamespace(tenant_id="tenant-a"),
    )
    assert result == {"routed": "evidence_gap", "gapId": "gap-1"}
    assert emitted == [("llm_user_eval", "replay_failed", "tenant-a")]
    assert collected and scans == [("tenant-a", 720)]


@pytest.mark.asyncio
async def test_stability_result_does_not_mutate_knowledge(monkeypatch):
    from app.services import llm_user_integration_service as service

    async def fake_emit(*args, **kwargs): return "qe-1"
    monkeypatch.setattr(service.quality_event_bus, "emit", fake_emit)
    result = await service.handle_evaluation(
        {"runId": "run-2", "rootCause": "stability", "query": "q"},
        SimpleNamespace(tenant_id="tenant-a"),
    )
    assert result["routed"] == "report_only"


@pytest.mark.asyncio
async def test_online_completeness_uses_shared_judge_wrapper(monkeypatch):
    from app.services import online_eval_service

    async def fake(query, answer, model_type=None):
        assert query == "q" and answer == "a"
        return 0.91

    monkeypatch.setattr("app.rag.judge.judge_completeness", fake)
    assert await online_eval_service._judge_completeness("q", "a", "ollama") == 0.91


def test_observer_nested_secrets_are_redacted(monkeypatch):
    from app.config import settings
    from app.core.llm_user_observer import _flatten

    monkeypatch.setattr(settings, "LLM_USER_OBSERVER_USER_HASH_SECRET", "observer-secret")
    attrs = _flatten({"request": {"password": "do-not-keep", "headers": {"Authorization": "Bearer hidden"}}})
    text = json.dumps(attrs)
    assert "do-not-keep" not in text and "Bearer hidden" not in text


def test_observer_redacts_credentials_embedded_in_text():
    from app.core.llm_user_observer import _redact_text

    value = _redact_text(
        "Authorization: Bearer opaque-value; api_key=sk-examplecredential123456; "
        "Cookie: session=private-value"
    )
    assert "opaque-value" not in value
    assert "examplecredential" not in value
    assert "private-value" not in value


@pytest.mark.asyncio
async def test_indexed_draft_notification_is_hmac_signed(monkeypatch):
    from app.config import settings
    from app.services import llm_user_integration_service as service

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"runId": "rerun-1"}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, content, headers):
            captured.update(url=url, content=content, headers=headers)
            return Response()

    monkeypatch.setattr(settings, "LLM_USER_SUITE_EVENT_URL", "http://llm-user-suite/events")
    monkeypatch.setattr(settings, "LLM_USER_SUITE_EVENT_SECRET", "event-secret")
    monkeypatch.setattr(service.httpx, "AsyncClient", Client)
    result = await service.forward_evolution_indexed(
        {"draftId": "draft-1", "runId": "run-1", "scenarioVersionId": "sv-1"},
        SimpleNamespace(event_id="evt-1", tenant_id="tenant-a"),
    )
    timestamp = captured["headers"]["X-LLM-User-Timestamp"]
    expected = hmac.new(
        b"event-secret", timestamp.encode() + b"." + captured["content"], hashlib.sha256,
    ).hexdigest()
    assert captured["headers"]["X-LLM-User-Signature"] == expected
    assert result == {"status": "sent", "suiteRunId": "rerun-1"}


@pytest.mark.asyncio
async def test_evolution_draft_correlates_durable_evaluation_event(test_db):
    from app.models.domain_event import DomainEvent
    from app.services.knowledge_evolution_service import _llm_user_links

    test_db.add(DomainEvent(
        id="event-1", tenant_id="tenant-a", event_type="llm_user.eval.completed",
        source="llm-user-suite", aggregate_type="llm_user_run", aggregate_id="run-1",
        payload={"runId": "run-1", "scenarioVersionId": "sv-1", "query": "断路器漏气"},
        status="published",
    ))
    await test_db.commit()
    links = await _llm_user_links(test_db, "tenant-a", ["断路器漏气"])
    assert links[0]["runId"] == "run-1"
    assert links[0]["scenarioVersionId"] == "sv-1"
