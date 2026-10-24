from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL

from local_meeting_ai.domain.crm import CrmAgenda, CrmSubmissionResult
from local_meeting_ai.domain.entities import Meeting, Summary, Transcription
from local_meeting_ai.domain.errors import (
    CapabilityUnavailableError,
    NotFoundError,
    ValidationError,
)
from local_meeting_ai.domain.protocols import CRMClient
from local_meeting_ai.infrastructure.database.repositories import (
    MeetingRepository,
    SummaryRepository,
    TranscriptionRepository,
)

_PAYLOAD_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class DeliverOutcome:
    meeting_id: int
    summary: CrmSubmissionResult
    transcript: CrmSubmissionResult | None


class CrmService:
    """Builds CRM deliveries from locally stored meetings and submits them.

    Transcript upload is strictly opt-in via ``include_transcript`` — the
    default delivery contains the summary only. Idempotency keys are derived
    from the meeting UUID plus a SHA-256 hash of the canonical payload, so
    retrying unchanged content reuses the same key while edited content gets
    a fresh one.
    """

    def __init__(
        self,
        *,
        meetings: MeetingRepository,
        transcriptions: TranscriptionRepository,
        summaries: SummaryRepository,
        client: CRMClient | None,
    ) -> None:
        self._meetings = meetings
        self._transcriptions = transcriptions
        self._summaries = summaries
        self._client = client

    @property
    def configured(self) -> bool:
        return self._client is not None

    def _require_client(self) -> CRMClient:
        if self._client is None:
            raise CapabilityUnavailableError(
                "CRM integration is not configured (set M2N_CRM_BASE_URL)"
            )
        return self._client

    async def fetch_agenda(self, meeting_code: str) -> CrmAgenda:
        return await self._require_client().get_meeting_agenda(meeting_code)

    async def health_check(self) -> dict[str, Any]:
        return await self._require_client().health_check()

    async def deliver(
        self,
        meeting_id: int,
        *,
        include_transcript: bool = False,
        meeting_code: str | None = None,
    ) -> DeliverOutcome:
        client = self._require_client()
        meeting = self._meetings.get(meeting_id)
        if meeting is None:
            raise NotFoundError(f"Meeting {meeting_id} does not exist")
        summary = self._latest_completed_summary(meeting.id)
        if summary is None:
            raise ValidationError(
                "The meeting has no completed summary to deliver yet"
            )
        meeting_ref = _meeting_ref(meeting, meeting_code)
        summary_payload = {
            "schema_version": _PAYLOAD_SCHEMA_VERSION,
            "type": "meeting.summary",
            "meeting": meeting_ref,
            "summary": {
                "content_markdown": summary.content_markdown or "",
                "structured": summary.structured,
                "provider": summary.provider,
                "model": summary.model,
                "completed_at": summary.completed_at,
            },
        }
        summary_result = await client.submit_summary(
            summary_payload,
            idempotency_key=_idempotency_key(meeting.uuid, "summary", summary_payload),
        )
        transcript_result: CrmSubmissionResult | None = None
        if include_transcript:
            transcription = self._active_transcription(meeting.id)
            segments = self._transcriptions.segments(transcription.id)
            if not segments:
                raise ValidationError(
                    "The active transcription has no segments to deliver"
                )
            speakers = {
                speaker.id: speaker.display_name
                for speaker in self._transcriptions.speakers_for_transcription(
                    transcription.id
                )
            }
            transcript_payload = {
                "schema_version": _PAYLOAD_SCHEMA_VERSION,
                "type": "meeting.transcript",
                "meeting": meeting_ref,
                "transcription": {
                    "id": transcription.id,
                    "title": transcription.title,
                    "language": transcription.language,
                },
                "segments": [
                    {
                        "segment_id": segment.id,
                        "segment_index": segment.segment_index,
                        "text": segment.text,
                        "speaker": (
                            speakers.get(segment.speaker_id)
                            if segment.speaker_id is not None
                            else None
                        ),
                        "start_ms": segment.start_ms,
                        "end_ms": segment.end_ms,
                        "is_final": segment.is_final,
                    }
                    for segment in segments
                ],
            }
            transcript_result = await client.submit_transcripts(
                transcript_payload,
                idempotency_key=_idempotency_key(
                    meeting.uuid, "transcript", transcript_payload
                ),
            )
        return DeliverOutcome(
            meeting_id=meeting.id,
            summary=summary_result,
            transcript=transcript_result,
        )

    async def send_meeting_ended(
        self, meeting_id: int, *, meeting_code: str | None = None
    ) -> CrmSubmissionResult:
        client = self._require_client()
        meeting = self._meetings.get(meeting_id)
        if meeting is None:
            raise NotFoundError(f"Meeting {meeting_id} does not exist")
        transcription = self._active_transcription(meeting.id)
        segments = self._transcriptions.segments(transcription.id)
        summary = self._latest_completed_summary(meeting.id)
        payload = {
            "schema_version": _PAYLOAD_SCHEMA_VERSION,
            "type": "meeting.ended",
            "meeting": _meeting_ref(meeting, meeting_code),
            # Meetings that never recorded an end time report "now" — the
            # ended event is being generated at this moment.
            "ended_at": meeting.ended_at or datetime.now(UTC).isoformat(),
            "counts": {
                "segments": len(segments),
                "has_summary": summary is not None,
            },
        }
        # The idempotency key deliberately excludes ended_at: for meetings
        # without a stored end time the timestamp is synthesized per call, and
        # a repeated ended event must reuse the same key, not look like new
        # content.
        key_payload = {key: value for key, value in payload.items() if key != "ended_at"}
        return await client.submit_meeting_ended(
            payload,
            idempotency_key=_idempotency_key(meeting.uuid, "ended", key_payload),
        )

    def _latest_completed_summary(self, meeting_id: int) -> Summary | None:
        completed = [
            summary
            for summary in self._summaries.list_for_meeting(meeting_id)
            if summary.status == "completed"
            and (summary.content_markdown or summary.structured)
        ]
        if not completed:
            return None
        return max(completed, key=lambda summary: (summary.created_at, summary.id))

    def _active_transcription(self, meeting_id: int) -> Transcription:
        transcription = self._transcriptions.active_for_meeting(meeting_id)
        if transcription is None:
            listed = self._transcriptions.list_for_meeting(meeting_id)
            transcription = listed[-1] if listed else None
        if transcription is None:
            raise ValidationError("The meeting has no transcription to deliver")
        return transcription


def _meeting_ref(meeting: Meeting, meeting_code: str | None) -> dict[str, Any]:
    return {
        "local_id": meeting.id,
        "meeting_uuid": meeting.uuid,
        "title": meeting.title,
        "crm_meeting_code": meeting_code,
    }


def _idempotency_key(meeting_uuid: str, kind: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return str(uuid.uuid5(NAMESPACE_URL, f"urn:meet2notes:crm:{kind}:{meeting_uuid}:{digest}"))
