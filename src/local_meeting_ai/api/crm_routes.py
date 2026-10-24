from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from local_meeting_ai.api.dependencies import get_container
from local_meeting_ai.bootstrap import Container
from local_meeting_ai.domain.crm import validate_meeting_code
from local_meeting_ai.domain.errors import CrmError, CrmHTTPError, CrmTimeoutError

router = APIRouter(prefix="/api/crm", tags=["crm"])
ContainerDependency = Annotated[Container, Depends(get_container)]


class CrmStatusResponse(BaseModel):
    configured: bool
    base_url: str | None = None


class CrmAgendaTopicResponse(BaseModel):
    topic_id: str
    name: str
    order_index: int


class CrmAgendaResponse(BaseModel):
    meeting_code: str
    title: str | None
    topics: list[CrmAgendaTopicResponse]


class CrmDeliverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_transcript: bool = False
    meeting_code: str | None = Field(default=None, min_length=1, max_length=64)


class CrmSubmissionResponse(BaseModel):
    kind: str
    idempotency_key: str
    accepted: bool
    http_status: int | None = None
    detail: str | None = None


class CrmDeliverResponse(BaseModel):
    meeting_id: int
    summary: CrmSubmissionResponse
    transcript: CrmSubmissionResponse | None


class CrmEndedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meeting_code: str | None = Field(default=None, min_length=1, max_length=64)


def _crm_error_response(error: CrmError) -> JSONResponse:
    """Maps CRM failures to an explicit, retryable-aware JSON error."""
    status_code = 503
    if isinstance(error, CrmTimeoutError):
        status_code = 504
    elif isinstance(error, CrmHTTPError):
        status_code = 502
    body: dict[str, Any] = {
        "error": type(error).__name__,
        "detail": str(error),
        "retryable": getattr(error, "retryable", isinstance(error, CrmTimeoutError)),
    }
    upstream_status = getattr(error, "status_code", None)
    if upstream_status is not None:
        body["upstream_status"] = upstream_status
    return JSONResponse(status_code=status_code, content=body)


@router.get("/status")
async def crm_status(container: ContainerDependency) -> CrmStatusResponse:
    base_url = container.settings.crm_base_url
    return CrmStatusResponse(
        configured=container.crm_service.configured,
        base_url=base_url,
    )


@router.get("/health")
async def crm_health(container: ContainerDependency) -> object:
    try:
        payload = await container.crm_service.health_check()
    except CrmError as error:
        return _crm_error_response(error)
    return {"ok": True, "crm": payload}


@router.get("/meetings/{meeting_code}/agenda")
async def crm_agenda(
    meeting_code: str, container: ContainerDependency
) -> object:
    validated = validate_meeting_code(meeting_code)
    try:
        agenda = await container.crm_service.fetch_agenda(validated)
    except CrmError as error:
        return _crm_error_response(error)
    return CrmAgendaResponse(
        meeting_code=agenda.meeting_code,
        title=agenda.title,
        topics=[
            CrmAgendaTopicResponse(
                topic_id=topic.topic_id,
                name=topic.name,
                order_index=topic.order_index,
            )
            for topic in agenda.topics
        ],
    )


@router.post("/meetings/{meeting_id}/deliver")
async def crm_deliver(
    meeting_id: int,
    request: CrmDeliverRequest,
    container: ContainerDependency,
) -> object:
    try:
        outcome = await container.crm_service.deliver(
            meeting_id,
            include_transcript=request.include_transcript,
            meeting_code=request.meeting_code,
        )
    except CrmError as error:
        return _crm_error_response(error)
    return CrmDeliverResponse(
        meeting_id=outcome.meeting_id,
        summary=CrmSubmissionResponse(
            kind=outcome.summary.kind,
            idempotency_key=outcome.summary.idempotency_key,
            accepted=outcome.summary.accepted,
            http_status=outcome.summary.http_status,
            detail=outcome.summary.detail,
        ),
        transcript=None
        if outcome.transcript is None
        else CrmSubmissionResponse(
            kind=outcome.transcript.kind,
            idempotency_key=outcome.transcript.idempotency_key,
            accepted=outcome.transcript.accepted,
            http_status=outcome.transcript.http_status,
            detail=outcome.transcript.detail,
        ),
    )


@router.post("/meetings/{meeting_id}/ended")
async def crm_meeting_ended(
    meeting_id: int,
    request: CrmEndedRequest,
    container: ContainerDependency,
) -> object:
    try:
        result = await container.crm_service.send_meeting_ended(
            meeting_id, meeting_code=request.meeting_code
        )
    except CrmError as error:
        return _crm_error_response(error)
    return CrmSubmissionResponse(
        kind=result.kind,
        idempotency_key=result.idempotency_key,
        accepted=result.accepted,
        http_status=result.http_status,
        detail=result.detail,
    )
