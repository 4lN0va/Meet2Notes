from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from local_meeting_ai.config import AppSettings
from local_meeting_ai.domain.crm import (
    CrmAgenda,
    CrmAgendaTopic,
    CrmSubmissionResult,
    validate_meeting_code,
)
from local_meeting_ai.domain.errors import (
    CrmConnectionError,
    CrmContractError,
    CrmHTTPError,
    CrmTimeoutError,
)

_MAX_DETAIL_CHARS = 200


class HttpxCrmClient:
    """CRM adapter speaking the JSON contract implemented by the mock CRM.

    The client never retries and never masks failures: transport problems map
    to ``CrmConnectionError``/``CrmTimeoutError``, HTTP error statuses to
    ``CrmHTTPError`` and contract violations to ``CrmContractError`` so the
    caller can decide between retrying and asking for human help.
    """

    def __init__(
        self,
        settings: AppSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not settings.crm_base_url:
            raise ValueError("crm_base_url must be configured to build an HTTP CRM client")
        headers = {"Accept": "application/json", "User-Agent": "Meet2Notes-CRM/1"}
        if settings.crm_token:
            # Kept out of logs; httpx only stores it on the request, never in
            # exceptions or the response.
            headers["Authorization"] = f"Bearer {settings.crm_token}"
        self._client = httpx.AsyncClient(
            base_url=settings.crm_base_url,
            timeout=settings.crm_timeout_seconds,
            headers=headers,
            transport=transport,
            follow_redirects=False,
        )
        self._paths = {
            "agenda": settings.crm_agenda_path,
            "transcripts": settings.crm_transcripts_path,
            "summary": settings.crm_summary_path,
            "ended": settings.crm_meeting_ended_path,
            "health": settings.crm_health_path,
        }

    async def health_check(self) -> dict[str, Any]:
        response = await self._request("GET", self._paths["health"])
        return _ensure_json_object(response)

    async def get_meeting_agenda(self, meeting_code: str) -> CrmAgenda:
        validated = validate_meeting_code(meeting_code)
        path = self._paths["agenda"].format(meeting_code=quote(validated, safe=""))
        response = await self._request("GET", path)
        return _parse_agenda(validated, _ensure_json_object(response))

    async def submit_transcripts(
        self, payload: dict[str, Any], *, idempotency_key: str
    ) -> CrmSubmissionResult:
        return await self._submit(
            "transcript", self._paths["transcripts"], payload, idempotency_key
        )

    async def submit_summary(
        self, payload: dict[str, Any], *, idempotency_key: str
    ) -> CrmSubmissionResult:
        return await self._submit(
            "summary", self._paths["summary"], payload, idempotency_key
        )

    async def submit_meeting_ended(
        self, payload: dict[str, Any], *, idempotency_key: str
    ) -> CrmSubmissionResult:
        meeting = payload.get("meeting")
        code = meeting.get("crm_meeting_code") if isinstance(meeting, dict) else None
        code_text = code if isinstance(code, str) and code.strip() else "local"
        path = self._paths["ended"].format(meeting_code=quote(code_text, safe=""))
        return await self._submit("meeting_ended", path, payload, idempotency_key)

    async def _submit(
        self, kind: str, path: str, payload: dict[str, Any], idempotency_key: str
    ) -> CrmSubmissionResult:
        response = await self._request(
            "POST",
            path,
            json_body=payload,
            headers={"Idempotency-Key": idempotency_key},
        )
        body = _ensure_json_object(response)
        accepted = body.get("ok") is True
        return CrmSubmissionResult(
            kind=kind,
            idempotency_key=idempotency_key,
            accepted=accepted,
            http_status=response.status_code,
            detail=None if accepted else str(body.get("error", "CRM rejected the submission")),
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method, path, json=json_body, headers=headers
            )
        except httpx.TimeoutException as error:
            raise CrmTimeoutError(
                f"CRM request timed out after {self._client.timeout}"
            ) from error
        except httpx.TransportError as error:
            raise CrmConnectionError(f"CRM is unreachable: {error}") from error
        if response.status_code >= 400:
            raise CrmHTTPError(
                status_code=response.status_code,
                detail=_safe_detail(response),
            )
        return response

    async def aclose(self) -> None:
        await self._client.aclose()


def _safe_detail(response: httpx.Response) -> str | None:
    """Short, non-echoing snippet of an error body; never includes headers."""
    text = response.text.strip()
    if not text:
        return None
    return text[:_MAX_DETAIL_CHARS]


def _ensure_json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as error:
        raise CrmContractError("CRM response is not valid JSON") from error
    if not isinstance(body, dict):
        raise CrmContractError("CRM response must be a JSON object")
    return body


def _parse_agenda(meeting_code: str, body: dict[str, Any]) -> CrmAgenda:
    meeting = body.get("meeting")
    topics = body.get("topics")
    if body.get("ok") is not True or not isinstance(meeting, dict) or not isinstance(topics, list):
        raise CrmContractError("Agenda response must contain ok=true, meeting and topics")
    raw_title = meeting.get("title")
    parsed_topics: list[CrmAgendaTopic] = []
    for index, topic in enumerate(topics):
        if not isinstance(topic, dict):
            raise CrmContractError("Each agenda topic must be a JSON object")
        topic_id = topic.get("topic_id")
        name = topic.get("name")
        if not isinstance(topic_id, str) or not topic_id.strip():
            raise CrmContractError("Agenda topics need a non-empty topic_id")
        if not isinstance(name, str) or not name.strip():
            raise CrmContractError("Agenda topics need a non-empty name")
        order_index = topic.get("order_index", index)
        if not isinstance(order_index, int):
            raise CrmContractError("Agenda topic order_index must be an integer")
        parsed_topics.append(
            CrmAgendaTopic(topic_id=topic_id, name=name, order_index=order_index)
        )
    return CrmAgenda(
        meeting_code=meeting_code,
        title=raw_title if isinstance(raw_title, str) and raw_title.strip() else None,
        topics=tuple(parsed_topics),
    )
