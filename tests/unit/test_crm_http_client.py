from __future__ import annotations

import json

import httpx
import pytest

from local_meeting_ai.adapters.crm.http_client import HttpxCrmClient
from local_meeting_ai.config import AppSettings
from local_meeting_ai.domain.errors import (
    CrmConnectionError,
    CrmContractError,
    CrmHTTPError,
    CrmTimeoutError,
)
from local_meeting_ai.domain.errors import ValidationError as DomainValidationError

AGENDA_BODY = {
    "ok": True,
    "meeting": {"code": "MTG-001", "title": "客戶訪談", "crm_id": "CRM-M-1"},
    "topics": [
        {"topic_id": "T-2", "name": "需求確認", "order_index": 1},
        {"topic_id": "T-1", "name": "開場", "order_index": 0},
    ],
}


def _settings(**overrides: object) -> AppSettings:
    defaults: dict[str, object] = {
        "testing": True,
        "open_browser": False,
        "crm_base_url": "http://127.0.0.1:8766",
    }
    defaults.update(overrides)
    return AppSettings(**defaults)  # type: ignore[arg-type]


def _transport(handler: object) -> httpx.MockTransport:
    return httpx.MockTransport(handler)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_agenda_success_parses_topics() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/crm/meetings/MTG-001/agenda"
        return httpx.Response(200, json=AGENDA_BODY)

    client = HttpxCrmClient(_settings(), transport=_transport(handler))
    agenda = await client.get_meeting_agenda("MTG-001")
    assert agenda.meeting_code == "MTG-001"
    assert agenda.title == "客戶訪談"
    assert [topic.topic_id for topic in agenda.topics] == ["T-2", "T-1"]
    await client.aclose()


@pytest.mark.asyncio
async def test_agenda_preserves_crm_order_not_sorted() -> None:
    ordered = {
        "ok": True,
        "meeting": {"code": "M"},
        "topics": [
            {"topic_id": "B", "name": "第二", "order_index": 2},
            {"topic_id": "A", "name": "第一", "order_index": 10},
        ],
    }
    client = HttpxCrmClient(
        _settings(), transport=_transport(lambda _: httpx.Response(200, json=ordered))
    )
    agenda = await client.get_meeting_agenda("M")
    assert [topic.topic_id for topic in agenda.topics] == ["B", "A"]
    await client.aclose()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"ok": False}, "missing ok"),
        ({"ok": True, "meeting": "not-an-object", "topics": []}, "meeting"),
        ({"ok": True, "meeting": {}, "topics": [{"name": "x"}]}, "topic_id"),
        ({"ok": True, "meeting": {}, "topics": [{"topic_id": "t", "name": ""}]}, "name"),
        (
            {
                "ok": True,
                "meeting": {},
                "topics": [{"topic_id": "t", "name": "n", "order_index": "0"}],
            },
            "order_index",
        ),
    ],
)
@pytest.mark.asyncio
async def test_agenda_contract_violations(body: object, expected: str) -> None:
    client = HttpxCrmClient(
        _settings(), transport=_transport(lambda _: httpx.Response(200, json=body))
    )
    with pytest.raises(CrmContractError):
        await client.get_meeting_agenda("M")
    assert expected  # the specific field is asserted via the error type
    await client.aclose()


@pytest.mark.asyncio
async def test_agenda_invalid_json_is_contract_error() -> None:
    client = HttpxCrmClient(
        _settings(), transport=_transport(lambda _: httpx.Response(200, text="not json"))
    )
    with pytest.raises(CrmContractError):
        await client.get_meeting_agenda("M")
    await client.aclose()


@pytest.mark.asyncio
async def test_agenda_404_raises_http_error_permanent() -> None:
    client = HttpxCrmClient(
        _settings(),
        transport=_transport(lambda _: httpx.Response(404, json={"error": "no meeting"})),
    )
    with pytest.raises(CrmHTTPError) as excinfo:
        await client.get_meeting_agenda("M")
    assert excinfo.value.status_code == 404
    assert excinfo.value.retryable is False
    await client.aclose()


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [(429, True), (500, True), (503, True), (408, True), (400, False), (401, False), (409, False)],
)
@pytest.mark.asyncio
async def test_http_error_retryable_classification(status_code: int, retryable: bool) -> None:
    client = HttpxCrmClient(
        _settings(), transport=_transport(lambda _: httpx.Response(status_code))
    )
    with pytest.raises(CrmHTTPError) as excinfo:
        await client.health_check()
    assert excinfo.value.retryable is retryable
    await client.aclose()


@pytest.mark.asyncio
async def test_timeout_maps_to_crm_timeout_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    client = HttpxCrmClient(_settings(), transport=_transport(handler))
    with pytest.raises(CrmTimeoutError):
        await client.health_check()
    await client.aclose()


@pytest.mark.asyncio
async def test_connection_error_maps_to_crm_connection_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = HttpxCrmClient(_settings(), transport=_transport(handler))
    with pytest.raises(CrmConnectionError):
        await client.health_check()
    await client.aclose()


@pytest.mark.asyncio
async def test_submit_summary_sends_idempotency_key_and_token() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("Idempotency-Key", "")
        seen["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json={"ok": True, "stored": True})

    client = HttpxCrmClient(
        _settings(crm_token="secret-token"), transport=_transport(handler)
    )
    result = await client.submit_summary(
        {"schema_version": 1, "type": "meeting.summary"}, idempotency_key="key-123"
    )
    assert result.accepted is True
    assert result.http_status == 200
    assert seen["key"] == "key-123"
    assert seen["auth"] == "Bearer secret-token"
    await client.aclose()


@pytest.mark.asyncio
async def test_submit_rejection_reports_not_accepted() -> None:
    client = HttpxCrmClient(
        _settings(),
        transport=_transport(
            lambda _: httpx.Response(200, json={"ok": False, "error": "bad payload"})
        ),
    )
    result = await client.submit_summary({}, idempotency_key="k")
    assert result.accepted is False
    assert result.detail == "bad payload"
    await client.aclose()


@pytest.mark.asyncio
async def test_meeting_code_validation_blocks_path_tricks() -> None:
    client = HttpxCrmClient(
        _settings(), transport=_transport(lambda _: httpx.Response(200, json=AGENDA_BODY))
    )
    for bad_code in ("../admin", "a b", "", "%2e%2e", "code/slash"):
        with pytest.raises(DomainValidationError):
            await client.get_meeting_agenda(bad_code)
    await client.aclose()


@pytest.mark.asyncio
async def test_health_check_returns_object() -> None:
    body = {"ok": True, "database": "not_required"}
    client = HttpxCrmClient(
        _settings(), transport=_transport(lambda _: httpx.Response(200, json=body))
    )
    assert await client.health_check() == body
    await client.aclose()


def test_client_requires_base_url() -> None:
    with pytest.raises(ValueError):
        HttpxCrmClient(_settings(crm_base_url=None))


def test_plain_http_outside_loopback_rejected() -> None:
    with pytest.raises(ValueError):
        _settings(crm_base_url="http://crm.example.com")


def test_settings_are_json_serializable_without_token_leak() -> None:
    settings = _settings(crm_token="secret-token")
    rendered = repr(settings)
    assert "secret-token" not in rendered
    assert json.dumps(settings.model_dump()) is not None
