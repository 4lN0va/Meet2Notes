from __future__ import annotations

import re
from dataclasses import dataclass

from local_meeting_ai.domain.errors import ValidationError

MEETING_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_meeting_code(meeting_code: str) -> str:
    """Validates a CRM meeting code before it may be used in an HTTP path."""
    code = meeting_code.strip()
    if not MEETING_CODE_PATTERN.fullmatch(code):
        raise ValidationError(
            "A CRM meeting code must be 1-64 characters of letters, digits, "
            "dots, dashes or underscores and must not start with a symbol"
        )
    return code


@dataclass(frozen=True, slots=True)
class CrmAgendaTopic:
    topic_id: str
    name: str
    order_index: int


@dataclass(frozen=True, slots=True)
class CrmAgenda:
    """Normalized agenda snapshot returned by a CRM for a meeting code."""

    meeting_code: str
    title: str | None
    topics: tuple[CrmAgendaTopic, ...]


@dataclass(frozen=True, slots=True)
class CrmSubmissionResult:
    """Outcome of one CRM delivery attempt (transcript, summary or ended)."""

    kind: str
    idempotency_key: str
    accepted: bool
    http_status: int | None = None
    detail: str | None = None
