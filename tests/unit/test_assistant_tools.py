from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from local_meeting_ai.application.assistant_tools import (
    ACTION_MARKER,
    PERMISSION_EDIT_TRANSCRIPT,
    PERMISSION_FULL,
    PERMISSION_READ,
    AssistantToolExecutor,
    apply_action_block,
    normalize_permission,
)
from local_meeting_ai.domain.errors import ValidationError


@dataclass
class FakeSegment:
    id: int
    transcription_id: int
    text: str
    start_ms: int = 0
    end_ms: int = 0


@dataclass
class FakeSummary:
    id: int
    status: str
    content_markdown: str
    created_at: str = "2026-01-01T00:00:00"


@dataclass
class FakeMeeting:
    id: int = 5
    title: str = "Old title"
    description: str | None = "old description"


@dataclass
class FakeRepos:
    segments: dict[int, FakeSegment] = field(default_factory=dict)
    transcription_id: int = 1
    replaced: tuple[str, str] | None = None
    replace_count: int = 3
    summaries: list[FakeSummary] = field(default_factory=list)
    meeting: FakeMeeting = field(default_factory=FakeMeeting)
    updated_title: str | None = None

    # repositories used by the executor
    class _Transcriptions:
        def __init__(self, outer: FakeRepos) -> None:
            self.outer = outer

        def active_for_meeting(self, meeting_id: int):  # type: ignore[no-untyped-def]
            return type("T", (), {"id": self.outer.transcription_id, "status": "completed"})()

        def segments(self, transcription_id: int):  # type: ignore[no-untyped-def]
            return list(self.outer.segments.values())

        def update_segment(self, segment_id: int, text: str):  # type: ignore[no-untyped-def]
            segment = self.outer.segments[segment_id]
            self.outer.segments[segment_id] = FakeSegment(
                id=segment.id, transcription_id=segment.transcription_id, text=text
            )
            return self.outer.segments[segment_id]

        def find_replace(  # type: ignore[no-untyped-def]
            self, transcription_id: int, *, find: str, replacement: str, case_sensitive: bool
        ):
            self.outer.replaced = (find, replacement)
            return self.outer.replace_count

    class _Meetings:
        def __init__(self, outer: FakeRepos) -> None:
            self.outer = outer

        def get(self, meeting_id: int):  # type: ignore[no-untyped-def]
            return self.outer.meeting

        def update(self, meeting_id: int, values: dict):  # type: ignore[no-untyped-def]
            if "title" in values:
                self.outer.updated_title = values["title"]
                self.outer.meeting.title = values["title"]
            return self.outer.meeting

    class _Summaries:
        def __init__(self, outer: FakeRepos) -> None:
            self.outer = outer

        def list_for_meeting(self, meeting_id: int):  # type: ignore[no-untyped-def]
            return [s for s in self.outer.summaries if s.status == "completed"]

        def update_content(self, summary_id: int, content: str):  # type: ignore[no-untyped-def]
            for index, summary in enumerate(self.outer.summaries):
                if summary.id == summary_id:
                    self.outer.summaries[index] = FakeSummary(
                        id=summary.id, status=summary.status,
                        content_markdown=content, created_at=summary.created_at,
                    )
                    return self.outer.summaries[index]
            return None

    def transcriptions_repo(self) -> Any:
        return self._Transcriptions(self)

    def meetings_repo(self) -> Any:
        return self._Meetings(self)

    def summaries_repo(self) -> Any:
        return self._Summaries(self)


@pytest.fixture
def repos() -> FakeRepos:
    repos = FakeRepos()
    repos.segments = {
        101: FakeSegment(id=101, transcription_id=1, text="台灣民眾党一路走來"),
        102: FakeSegment(id=102, transcription_id=1, text="关于健保費用的補助"),
    }
    repos.summaries = [FakeSummary(id=9, status="completed", content_markdown="# old")]
    return repos


def _executor(repos: FakeRepos, permission: str) -> AssistantToolExecutor:
    return AssistantToolExecutor(
        permission=permission,
        meeting_id=5,
        meetings=repos.meetings_repo(),
        transcriptions=repos.transcriptions_repo(),
        summaries=repos.summaries_repo(),
    )


def test_normalize_permission_rejects_unknown() -> None:
    assert normalize_permission("edit_transcript") == "edit_transcript"
    assert normalize_permission(None) == PERMISSION_READ
    with pytest.raises(ValidationError):
        normalize_permission("delete_everything")


def test_read_level_rejects_edit_actions(repos: FakeRepos) -> None:
    executor = _executor(repos, PERMISSION_READ)
    with pytest.raises(ValidationError):
        executor.execute({"action": "update_segment", "segment_id": 101, "text": "x"})
    with pytest.raises(ValidationError):
        executor.execute({"action": "batch_replace", "find": "a", "replace": "b"})


def test_edit_level_updates_segment(repos: FakeRepos) -> None:
    executor = _executor(repos, PERMISSION_EDIT_TRANSCRIPT)
    edit = executor.execute(
        {"action": "update_segment", "segment_id": 101, "text": "台灣民眾黨一路走來"}
    )
    assert repos.segments[101].text == "台灣民眾黨一路走來"
    assert edit.before == "台灣民眾党一路走來"
    assert edit.after == "台灣民眾黨一路走來"


def test_edit_level_rejects_foreign_segment(repos: FakeRepos) -> None:
    repos.segments[999] = FakeSegment(id=999, transcription_id=42, text="other meeting")
    executor = _executor(repos, PERMISSION_EDIT_TRANSCRIPT)
    with pytest.raises(ValidationError):
        executor.execute({"action": "update_segment", "segment_id": 999, "text": "x"})


def test_edit_level_batch_replace(repos: FakeRepos) -> None:
    executor = _executor(repos, PERMISSION_EDIT_TRANSCRIPT)
    edit = executor.execute({"action": "batch_replace", "find": "民众党", "replace": "民眾黨"})
    assert repos.replaced == ("民众党", "民眾黨")
    assert "3 segment(s)" in edit.detail


def test_edit_level_cannot_touch_summary_or_meeting(repos: FakeRepos) -> None:
    executor = _executor(repos, PERMISSION_EDIT_TRANSCRIPT)
    with pytest.raises(ValidationError):
        executor.execute({"action": "update_summary", "content_markdown": "# new"})
    with pytest.raises(ValidationError):
        executor.execute({"action": "update_meeting", "title": "New"})


def test_full_level_updates_summary_and_meeting(repos: FakeRepos) -> None:
    executor = _executor(repos, PERMISSION_FULL)
    executor.execute({"action": "update_summary", "content_markdown": "# new summary"})
    assert repos.summaries_repo is not None
    assert repos.summaries[0].content_markdown == "# new summary"
    executor.execute({"action": "update_meeting", "title": "新標題"})
    assert repos.updated_title == "新標題"


def test_unknown_action_rejected(repos: FakeRepos) -> None:
    executor = _executor(repos, PERMISSION_FULL)
    with pytest.raises(ValidationError):
        executor.execute({"action": "delete_meeting"})


def test_claims_unapplied_completion_detection() -> None:
    # The failure mode reported live: the model SAYS the edit is done but
    # emits no ###ACTIONS### block, so nothing was actually modified.
    from local_meeting_ai.application.assistant_tools import (
        claims_unapplied_completion,
        looks_like_suggestion,
    )

    sample = "已將該段修正為：關於健保費用的補助事宜"  # noqa: RUF001
    assert claims_unapplied_completion(sample, "把X改成Y")
    assert not claims_unapplied_completion("這場會議討論健保", "把X改成Y")
    # advisory replies are covered by the sibling detector
    assert looks_like_suggestion("建議將『党』改為『黨』", "把X改成Y")


def test_extract_replace_actions_quoted_pairs() -> None:
    from local_meeting_ai.application.assistant_tools import extract_replace_actions

    question = "把逐字稿中『白紙黑字清清楚楚的宰名』的『宰名』改成『載明』，完成後回報改了幾處。"  # noqa: RUF001
    reply = "已將「白紙黑字清清楚楚的宰名」中的「宰名」改為「載明」。共修改 1 處。"
    assert extract_replace_actions(question) == [
        {"action": "batch_replace", "find": "宰名", "replace": "載明", "case_sensitive": False}
    ]
    assert extract_replace_actions(reply)[0]["find"] == "宰名"
    multi = extract_replace_actions("把「九年一毛」改成「九牛一毛」跟「吝色」改為「吝嗇」")
    assert [(action["find"], action["replace"]) for action in multi] == [
        ("九年一毛", "九牛一毛"),
        ("吝色", "吝嗇"),
    ]
    # Unquoted prose yields nothing: garbled segmentation must not produce edits.
    assert extract_replace_actions("把九年一毛改成九牛一毛") == []
    assert extract_replace_actions("這場會議討論健保") == []


def test_direct_actions_from_question_permission_tiers(repos: FakeRepos) -> None:
    repos.segments[101].text = "關於健保起付的差別待遇"  # make the phrase exist
    quoted_q = "把「台灣民眾党」改成「台灣民眾黨」"
    unquoted_q = "把健保起付改成健保給付"

    edit_executor = _executor(repos, PERMISSION_EDIT_TRANSCRIPT)
    # quoted pairs apply at every edit tier
    actions = edit_executor.direct_actions_from_question(quoted_q)
    assert actions and actions[0]["find"] == "台灣民眾党"
    # unquoted pairs are NOT honoured below full permission
    assert edit_executor.direct_actions_from_question(unquoted_q) == []

    full_executor = _executor(repos, PERMISSION_FULL)
    actions = full_executor.direct_actions_from_question(unquoted_q)
    assert actions and actions[0] == {
        "action": "batch_replace", "find": "健保起付",
        "replace": "健保給付", "case_sensitive": False,
    }


def test_direct_actions_existence_and_hypothetical_guards(repos: FakeRepos) -> None:
    executor = _executor(repos, PERMISSION_FULL)
    # unquoted phrase that does not exist in the transcript → dropped
    assert executor.direct_actions_from_question("把不存在的詞改成任何字") == []
    # hypothetical phrasing must never produce edits
    assert executor.direct_actions_from_question(
        "如果把「台灣民眾党」改成「台灣民眾黨」會怎樣"
    ) == []
    assert executor.direct_actions_from_question(
        "確認一下「台灣民眾党」改成「台灣民眾黨」對不對"
    ) == []


def test_parse_reply_splits_actions_and_cleans_text(repos: FakeRepos) -> None:
    executor = _executor(repos, PERMISSION_EDIT_TRANSCRIPT)
    reply = (
        "我把『党』修正為『黨』。\n\n"
        + ACTION_MARKER
        + '\n[{"action": "update_segment", "segment_id": 101, "text": "台灣民眾黨一路走來"}]'
    )
    clean, actions = executor.parse_reply(reply)
    assert "###ACTIONS###" not in clean
    assert clean.startswith("我把")
    assert len(actions) == 1
    assert actions[0]["segment_id"] == 101


def test_parse_reply_without_actions(repos: FakeRepos) -> None:
    executor = _executor(repos, PERMISSION_READ)
    clean, actions = executor.parse_reply("這場會議討論了健保。")
    assert actions == []
    assert clean == "這場會議討論了健保。"


def test_parse_reply_invalid_json_raises(repos: FakeRepos) -> None:
    executor = _executor(repos, PERMISSION_EDIT_TRANSCRIPT)
    with pytest.raises(ValidationError):
        executor.parse_reply(ACTION_MARKER + "\n[{not json}]")


def test_apply_action_block_aborts_on_failure(repos: FakeRepos) -> None:
    """A failed action aborts the batch: safer than applying the rest blindly.

    The tool loop feeds the error back to the model for a corrective round.
    """
    executor = _executor(repos, PERMISSION_EDIT_TRANSCRIPT)
    actions = [
        {"action": "update_segment", "segment_id": 101, "text": "修正一"},
        {"action": "update_segment", "segment_id": 555, "text": "不存在的段落"},
        {"action": "update_segment", "segment_id": 102, "text": "不會執行到"},
    ]
    with pytest.raises(ValidationError):
        apply_action_block(executor, actions)
    assert repos.segments[101].text == "修正一"
    assert repos.segments[102].text == "关于健保費用的補助"


def test_tool_instructions_include_segment_map(repos: FakeRepos) -> None:
    executor = _executor(repos, PERMISSION_FULL)
    instructions = executor.tool_instructions()
    assert "#101 |" in instructions
    assert "update_summary" in instructions
    edit_only = _executor(repos, PERMISSION_EDIT_TRANSCRIPT).tool_instructions()
    assert "update_summary" not in edit_only
