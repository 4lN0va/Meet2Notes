"""Assistant tool access: permission-gated transcript/summary editing.

The post-meeting assistant can be granted three permission levels:

- ``read``: interaction only (default; the historical behaviour).
- ``edit_transcript``: the model may update transcript segments of the
  selected meeting (single segment rewrite or batch find/replace).
- ``full``: everything above, plus editing the meeting's AI summary and
  its title/description metadata.

The model never talks to the database directly. It ends a reply with an
``###ACTIONS###`` JSON block (a provider-agnostic protocol that works on
any OpenAI-compatible endpoint); :class:`AssistantToolExecutor` validates
every action against the permission level and the meeting scope before
touching a repository, and returns a per-action result the caller feeds
back to the model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from local_meeting_ai.domain.errors import ValidationError

PERMISSION_READ = "read"
PERMISSION_EDIT_TRANSCRIPT = "edit_transcript"
PERMISSION_FULL = "full"
PERMISSION_LEVELS = (PERMISSION_READ, PERMISSION_EDIT_TRANSCRIPT, PERMISSION_FULL)

ACTION_MARKER = "###ACTIONS###"
MAX_ACTIONS_PER_REPLY = 20


def normalize_permission(value: Any) -> str:
    level = str(value or PERMISSION_READ).strip()
    if level not in PERMISSION_LEVELS:
        raise ValidationError(
            "assistant_permission must be one of: " + ", ".join(PERMISSION_LEVELS)
        )
    return level


def permission_allows_edits(permission: str) -> bool:
    return permission in {PERMISSION_EDIT_TRANSCRIPT, PERMISSION_FULL}


@dataclass(frozen=True, slots=True)
class AppliedEdit:
    action: str
    detail: str
    before: str | None = None
    after: str | None = None


class AssistantToolExecutor:
    """Validates and applies assistant actions for one meeting."""

    def __init__(
        self,
        *,
        permission: str,
        meeting_id: int,
        meetings: Any,
        transcriptions: Any,
        summaries: Any,
    ) -> None:
        self.permission = normalize_permission(permission)
        self.meeting_id = meeting_id
        self._meetings = meetings
        self._transcriptions = transcriptions
        self._summaries = summaries
        meeting = self._meetings.get(meeting_id)
        if meeting is None:
            raise ValidationError(f"Meeting {meeting_id} does not exist")
        transcription = self._transcriptions.active_for_meeting(meeting_id)
        if transcription is None or transcription.status != "completed":
            raise ValidationError("This meeting does not have a completed transcript")
        self.transcription_id = transcription.id
        self._segments = {
            segment.id: segment
            for segment in self._transcriptions.segments(transcription.id)
        }

    # ------------------------------------------------------------------
    # Prompt helpers
    # ------------------------------------------------------------------

    def tool_instructions(self) -> str:
        """Protocol text appended to the assistant context for this level."""
        lines = [
            "## ASSISTANT TOOLS",
            f"Permission level: {self.permission}.",
            "When the user asks for any modification, you MUST apply it yourself:",
            "end that reply with a line '###ACTIONS###' followed by a JSON array",
            "of actions. Actions run immediately after your reply, then you",
            "receive their results. NEVER merely describe or suggest a change —",
            "a reply without ###ACTIONS### leaves the data untouched. Without a",
            "modification request, reply normally.",
        ]
        if self.permission == PERMISSION_EDIT_TRANSCRIPT:
            lines.append(
                "Available actions:\n"
                '- {"action": "update_segment", "segment_id": <id>, "text": "<text>"}\n'
                + '- {"action": "batch_replace", "find": "<find>",\n'
                + '   "replace": "<replace>", "case_sensitive": false}'
            )
        if self.permission == PERMISSION_FULL:
            lines.append(
                "Available actions:\n"
                '- {"action": "update_segment", "segment_id": <id>, "text": "<text>"}\n'
                + '- {"action": "batch_replace", "find": "<find>",\n'
                + '   "replace": "<replace>", "case_sensitive": false}\n'
                + '- {"action": "update_summary",\n'
                + '   "content_markdown": "<new notes markdown>"}\n'
                + '- {"action": "update_meeting", "title": "<new title>",\n'
                + '   "description": "<new description>"}'
            )
        lines.append(
            "Rules: never invent segment ids; use only ids from the SEGMENT MAP; "
            "keep timestamps and speaker labels unchanged; "
            "write in the transcript's language."
        )
        lines.append(self._segment_map())
        return "\n".join(lines)

    def _segment_map(self) -> str:
        lines = ["SEGMENT MAP (id | start | text):"]
        for segment in self._segments.values():
            minutes, seconds = divmod(segment.start_ms // 1000, 60)
            text = " ".join(segment.text.split())[:120]
            lines.append(f"#{segment.id} | {minutes:02d}:{seconds:02d} | {text}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse_reply(self, reply: str) -> tuple[str, list[dict[str, Any]]]:
        """Splits a model reply into (clean answer, validated action list)."""
        marker = reply.rfind(ACTION_MARKER)
        if marker < 0:
            return reply, []
        head = reply[:marker].rstrip()
        payload = reply[marker + len(ACTION_MARKER):].strip()
        if payload.startswith("```"):
            payload = payload.strip("`")
            if payload.lower().startswith("json"):
                payload = payload[4:]
        try:
            parsed = json.loads(payload.strip())
        except ValueError as error:
            raise ValidationError(
                f"The assistant produced an invalid action block: {error}"
            ) from error
        if not isinstance(parsed, list):
            raise ValidationError("The action block must be a JSON array")
        actions = parsed[:MAX_ACTIONS_PER_REPLY]
        for action in actions:
            if not isinstance(action, dict) or not isinstance(action.get("action"), str):
                raise ValidationError("Each action must be an object with an 'action' name")
        return head, actions

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, action: dict[str, Any]) -> AppliedEdit:
        if not permission_allows_edits(self.permission):
            raise ValidationError(
                f"The assistant is in '{self.permission}' mode and cannot modify data"
            )
        name = str(action.get("action") or "")
        if name == "update_segment":
            return self._update_segment(action)
        if name == "batch_replace":
            return self._batch_replace(action)
        if name == "update_summary" and self.permission == PERMISSION_FULL:
            return self._update_summary(action)
        if name == "update_meeting" and self.permission == PERMISSION_FULL:
            return self._update_meeting(action)
        raise ValidationError(
            f"Action '{name}' is not available at permission level '{self.permission}'"
        )

    def _update_segment(self, action: dict[str, Any]) -> AppliedEdit:
        segment_id = int(action.get("segment_id") or 0)
        text = str(action.get("text") or "").strip()
        segment = self._segments.get(segment_id)
        if segment is None or segment.transcription_id != self.transcription_id:
            raise ValidationError(f"Segment #{segment_id} does not belong to this meeting")
        if not text:
            raise ValidationError("update_segment requires non-empty text")
        if segment.text == text:
            return AppliedEdit(
                action="update_segment",
                detail=f"segment #{segment_id} already up to date",
            )
        updated = self._transcriptions.update_segment(segment_id, text)
        if updated is None:
            raise ValidationError(f"Segment #{segment_id} could not be updated")
        return AppliedEdit(
            action="update_segment",
            detail=f"segment #{segment_id} updated",
            before=segment.text,
            after=updated.text,
        )

    def _batch_replace(self, action: dict[str, Any]) -> AppliedEdit:
        find = str(action.get("find") or "")
        replace = str(action.get("replace") or "")
        case_sensitive = bool(action.get("case_sensitive") or False)
        if not find:
            raise ValidationError("batch_replace requires a non-empty 'find'")
        count = self._transcriptions.find_replace(
            self.transcription_id,
            find=find,
            replacement=replace,
            case_sensitive=case_sensitive,
        )
        return AppliedEdit(
            action="batch_replace",
            detail=f"'{find}' -> '{replace}' replaced in {count} segment(s)",
        )

    def _update_summary(self, action: dict[str, Any]) -> AppliedEdit:
        content = str(action.get("content_markdown") or "").strip()
        if not content:
            raise ValidationError("update_summary requires non-empty content_markdown")
        summary = self._latest_summary()
        if summary is None:
            raise ValidationError("This meeting has no AI summary to update")
        updated = self._summaries.update_content(summary.id, content)
        if updated is None:
            raise ValidationError("The summary could not be updated")
        return AppliedEdit(
            action="update_summary",
            detail=f"summary #{summary.id} rewritten",
            before=(summary.content_markdown or "")[:120],
            after=content[:120],
        )

    def _update_meeting(self, action: dict[str, Any]) -> AppliedEdit:
        values: dict[str, Any] = {}
        title = action.get("title")
        description = action.get("description")
        if isinstance(title, str) and title.strip():
            values["title"] = title.strip()
        if isinstance(description, str):
            values["description"] = description.strip() or None
        if not values:
            raise ValidationError("update_meeting requires a title and/or description")
        meeting = self._meetings.update(self.meeting_id, values)
        if meeting is None:
            raise ValidationError("The meeting could not be updated")
        return AppliedEdit(
            action="update_meeting",
            detail="meeting metadata updated: " + ", ".join(sorted(values)),
        )

    def _latest_summary(self) -> Any:
        completed = [
            summary
            for summary in self._summaries.list_for_meeting(self.meeting_id)
            if summary.status == "completed"
        ]
        if not completed:
            return None
        return max(completed, key=lambda summary: (summary.created_at, summary.id))


def apply_action_block(
    executor: AssistantToolExecutor, actions: list[dict[str, Any]]
) -> list[AppliedEdit]:
    """Applies every action; a failed action aborts the remaining ones."""
    applied: list[AppliedEdit] = []
    for action in actions:
        applied.append(executor.execute(action))
    return applied


_ACTION_RESULTS_TEMPLATE = "ACTION RESULTS (applied {} edit(s)):\n{}"

_EDIT_INTENT = re.compile(
    r"改成|改為|修正|替換|取代|更新|編輯|fix|replace|update|correct", re.IGNORECASE
)
_SUGGESTION_MARKER = re.compile(
    r"建議|建议|should be|suggest|可以改|可改為|would (?:change|replace)", re.IGNORECASE
)
_CLAIMS_COMPLETION = re.compile(
    r"已將|已修正|已改|已更新|已套用|已經改|已完成修正"
    r"|has been (?:updated|corrected|changed|applied)"
    r"|successfully (?:updated|corrected|changed)",
    re.IGNORECASE,
)


def looks_like_suggestion(reply: str, question: str) -> bool:
    """True when the reply describes edits the user actually requested.

    Both the question must look like an edit request and the reply must
    read like advice; an assistant answering a genuine "what would you
    change?" question is never nagged.
    """
    return bool(_SUGGESTION_MARKER.search(reply)) and bool(_EDIT_INTENT.search(question))


def claims_unapplied_completion(reply: str, question: str) -> bool:
    """True when the reply SAYS the edit was made but no action ran.

    Models sometimes answer "已將…修正為…" without an ###ACTIONS### block;
    nothing was modified, so the claim is false and the caller must ask the
    model to actually apply the change.
    """
    return bool(_CLAIMS_COMPLETION.search(reply)) and bool(_EDIT_INTENT.search(question))


def action_results_turn(applied: list[AppliedEdit]) -> dict[str, str]:
    detail = "\n".join(f"- {edit.action}: {edit.detail}" for edit in applied)
    return {
        "role": "user",
        "content": _ACTION_RESULTS_TEMPLATE.format(len(applied), detail),
    }
