from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from local_meeting_ai.api.app import create_app
from local_meeting_ai.config import AppSettings
from local_meeting_ai.domain.crm import CrmAgenda, CrmAgendaTopic, CrmSubmissionResult
from local_meeting_ai.domain.errors import CrmHTTPError, CrmTimeoutError

AGENDA = CrmAgenda(
    meeting_code="MTG-001",
    title="客戶需求訪談",
    topics=(
        CrmAgendaTopic(topic_id="T-1", name="開場", order_index=0),
        CrmAgendaTopic(topic_id="T-2", name="需求確認", order_index=1),
    ),
)


@dataclass
class FakeCRMClient:
    """In-memory CRMClient double that records every submission."""

    agenda: CrmAgenda = AGENDA
    accepted: bool = True
    error: Exception | None = None
    health: dict[str, Any] = field(default_factory=lambda: {"ok": True})
    agenda_calls: list[str] = field(default_factory=list)
    submissions: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)
    closed: bool = False

    async def health_check(self) -> dict[str, Any]:
        if self.error:
            raise self.error
        return self.health

    async def get_meeting_agenda(self, meeting_code: str) -> CrmAgenda:
        self.agenda_calls.append(meeting_code)
        if self.error:
            raise self.error
        return self.agenda

    async def submit_transcripts(
        self, payload: dict[str, Any], *, idempotency_key: str
    ) -> CrmSubmissionResult:
        self.submissions.append(("transcript", payload, idempotency_key))
        if self.error:
            raise self.error
        return CrmSubmissionResult(
            kind="transcript", idempotency_key=idempotency_key, accepted=self.accepted,
            http_status=200,
        )

    async def submit_summary(
        self, payload: dict[str, Any], *, idempotency_key: str
    ) -> CrmSubmissionResult:
        self.submissions.append(("summary", payload, idempotency_key))
        if self.error:
            raise self.error
        return CrmSubmissionResult(
            kind="summary", idempotency_key=idempotency_key, accepted=self.accepted,
            http_status=200,
        )

    async def submit_meeting_ended(
        self, payload: dict[str, Any], *, idempotency_key: str
    ) -> CrmSubmissionResult:
        self.submissions.append(("meeting_ended", payload, idempotency_key))
        if self.error:
            raise self.error
        return CrmSubmissionResult(
            kind="meeting_ended", idempotency_key=idempotency_key, accepted=self.accepted,
            http_status=200,
        )

    async def aclose(self) -> None:
        self.closed = True

    def payload(self, kind: str) -> dict[str, Any]:
        return next(payload for name, payload, _ in self.submissions if name == kind)

    def key(self, kind: str) -> str:
        return next(key for name, _, key in self.submissions if name == kind)


def _client_fixture(settings: AppSettings, crm: FakeCRMClient) -> TestClient:
    app = create_app(settings, crm_client=crm)  # type: ignore[arg-type]
    return TestClient(app)


@pytest.fixture
def crm_settings(data_dir):  # type: ignore[no-untyped-def]
    return AppSettings(
        data_dir=data_dir,
        models_dir=data_dir / "models",
        testing=True,
        open_browser=False,
        max_upload_mb=2,
        log_level="WARNING",
    )


def _seed_meeting(settings: AppSettings) -> int:
    """Creates a meeting + transcription + segments + completed summary."""
    from local_meeting_ai.bootstrap import build_container
    from local_meeting_ai.domain.entities import SegmentDraft

    container = build_container(settings)
    meeting = container.meeting_service.create(title="CRM 驗證會談")
    transcription = container.transcriptions.create(
        meeting_id=meeting.id,
        title="CRM 驗證會談",
        engine="faster-whisper",
        model="tiny",
        language="zh",
        settings={},
    )
    container.transcriptions.append_segment(
        transcription.id,
        SegmentDraft(
            index=0,
            start_ms=0,
            end_ms=2100,
            text="你好，我們開始今天的會議",  # noqa: RUF001
        ),
        is_final=True,
    )
    container.transcriptions.append_segment(
        transcription.id,
        SegmentDraft(index=1, start_ms=2100, end_ms=4300, text="首先確認專案時程"),
        is_final=True,
    )
    container.transcriptions.set_status(transcription.id, "completed")
    summary = container.summaries.create(
        meeting_id=meeting.id,
        transcription_id=transcription.id,
        provider="llama-cpp",
        model="LFM2.5-1.2B",
    )
    container.summaries.complete(
        summary.id,
        content_markdown="# 會議摘要\n\n確認專案時程。",
        structured={
            "discussion": "確認專案時程",
            "key_points": ["時程確認"],
            "decisions": [],
            "action_items": [],
            "open_questions": [],
        },
    )
    return meeting.id


def test_status_reports_unconfigured_without_client(crm_settings: AppSettings) -> None:
    app = create_app(crm_settings)
    with TestClient(app) as client:
        response = client.get("/api/crm/status")
    assert response.status_code == 200
    assert response.json() == {"configured": False, "base_url": None}


def test_agenda_passes_validated_code(crm_settings: AppSettings) -> None:
    crm = FakeCRMClient()
    with _client_fixture(crm_settings, crm) as client:
        response = client.get("/api/crm/meetings/MTG-001/agenda")
    assert response.status_code == 200
    body = response.json()
    assert body["meeting_code"] == "MTG-001"
    assert body["topics"][1] == {"topic_id": "T-2", "name": "需求確認", "order_index": 1}
    assert crm.agenda_calls == ["MTG-001"]


def test_agenda_rejects_invalid_code(crm_settings: AppSettings) -> None:
    crm = FakeCRMClient()
    with _client_fixture(crm_settings, crm) as client:
        response = client.get("/api/crm/meetings/%2E%2E/agenda")
    assert response.status_code == 422
    assert crm.agenda_calls == []


def test_deliver_default_sends_summary_only(crm_settings: AppSettings) -> None:
    crm = FakeCRMClient()
    meeting_id = _seed_meeting(crm_settings)
    with _client_fixture(crm_settings, crm) as client:
        response = client.post(
            f"/api/crm/meetings/{meeting_id}/deliver",
            json={"include_transcript": False},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["accepted"] is True
    assert body["transcript"] is None
    kinds = [name for name, _, _ in crm.submissions]
    assert kinds == ["summary"]
    payload = crm.payload("summary")
    assert payload["type"] == "meeting.summary"
    assert payload["summary"]["structured"]["key_points"] == ["時程確認"]
    assert "segments" not in payload


def test_deliver_with_transcript_includes_speaker_and_timestamps(
    crm_settings: AppSettings,
) -> None:
    crm = FakeCRMClient()
    meeting_id = _seed_meeting(crm_settings)
    with _client_fixture(crm_settings, crm) as client:
        response = client.post(
            f"/api/crm/meetings/{meeting_id}/deliver",
            json={"include_transcript": True, "meeting_code": "MTG-001"},
        )
    assert response.status_code == 200
    kinds = [name for name, _, _ in crm.submissions]
    assert kinds == ["summary", "transcript"]
    payload = crm.payload("transcript")
    segment = payload["segments"][0]
    assert segment["text"] == "你好，我們開始今天的會議"  # noqa: RUF001
    assert segment["start_ms"] == 0 and segment["end_ms"] == 2100
    assert segment["is_final"] is True
    assert "speaker" in segment
    assert payload["meeting"]["crm_meeting_code"] == "MTG-001"


def test_deliver_without_summary_fails_cleanly(crm_settings: AppSettings) -> None:
    crm = FakeCRMClient()
    from local_meeting_ai.bootstrap import build_container

    container = build_container(crm_settings)
    meeting = container.meeting_service.create(title="尚無摘要")
    with _client_fixture(crm_settings, crm) as client:
        response = client.post(f"/api/crm/meetings/{meeting.id}/deliver", json={})
    assert response.status_code == 422
    assert crm.submissions == []


def test_deliver_unknown_meeting_is_404(crm_settings: AppSettings) -> None:
    crm = FakeCRMClient()
    with _client_fixture(crm_settings, crm) as client:
        response = client.post("/api/crm/meetings/999999/deliver", json={})
    assert response.status_code == 404


def test_deliver_idempotency_key_stable_for_same_content(
    crm_settings: AppSettings,
) -> None:
    crm = FakeCRMClient()
    meeting_id = _seed_meeting(crm_settings)
    with _client_fixture(crm_settings, crm) as client:
        first = client.post(f"/api/crm/meetings/{meeting_id}/deliver", json={})
        second = client.post(f"/api/crm/meetings/{meeting_id}/deliver", json={})
    assert first.json()["summary"]["idempotency_key"] == (
        second.json()["summary"]["idempotency_key"]
    )


def test_deliver_maps_crm_http_error(crm_settings: AppSettings) -> None:
    crm = FakeCRMClient(error=CrmHTTPError(429, "rate limited"))
    meeting_id = _seed_meeting(crm_settings)
    with _client_fixture(crm_settings, crm) as client:
        response = client.post(f"/api/crm/meetings/{meeting_id}/deliver", json={})
    assert response.status_code == 502
    body = response.json()
    assert body["upstream_status"] == 429
    assert body["retryable"] is True


def test_deliver_maps_crm_timeout(crm_settings: AppSettings) -> None:
    crm = FakeCRMClient(error=CrmTimeoutError("timed out"))
    meeting_id = _seed_meeting(crm_settings)
    with _client_fixture(crm_settings, crm) as client:
        response = client.post(f"/api/crm/meetings/{meeting_id}/deliver", json={})
    assert response.status_code == 504
    assert response.json()["retryable"] is True


def test_meeting_ended_submission(crm_settings: AppSettings) -> None:
    crm = FakeCRMClient()
    meeting_id = _seed_meeting(crm_settings)
    with _client_fixture(crm_settings, crm) as client:
        response = client.post(
            f"/api/crm/meetings/{meeting_id}/ended",
            json={"meeting_code": "MTG-001"},
        )
    assert response.status_code == 200
    payload = crm.payload("meeting_ended")
    assert payload["type"] == "meeting.ended"
    assert payload["counts"]["segments"] == 2
    assert payload["counts"]["has_summary"] is True
    assert payload["meeting"]["crm_meeting_code"] == "MTG-001"
    # A meeting without a stored end time reports "now" in ISO-8601.
    assert isinstance(payload["ended_at"], str) and "T" in payload["ended_at"]


def test_http_client_closed_on_shutdown(crm_settings: AppSettings) -> None:
    crm = FakeCRMClient()
    with _client_fixture(crm_settings, crm):
        pass
    assert crm.closed is True
