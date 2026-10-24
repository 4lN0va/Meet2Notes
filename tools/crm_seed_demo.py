"""Seed a demo meeting (segments + speakers + summary) for the CRM E2E check.

Run from the Meet2Notes repo root with the project venv:
    ./.venv/Scripts/python.exe tools/crm_seed_demo.py

Creates a demo data directory (default `<repo>/.demo-data`, override with
M2N_DEMO_DATA_DIR) with one meeting, two named speakers, four final zh-TW
transcript segments and one completed structured summary. Safe to re-run:
it aborts when the demo meeting already exists.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from local_meeting_ai.bootstrap import build_container
from local_meeting_ai.config import AppSettings
from local_meeting_ai.domain.entities import SegmentDraft

DATA_DIR = Path(
    os.environ.get("M2N_DEMO_DATA_DIR")
    or Path(__file__).resolve().parents[1] / ".demo-data"
)
DEMO_TITLE = "CRM 整合驗證會談"

SEGMENTS: list[tuple[int, int, str, str]] = [
    (0, 0, 2400, "午安，謝謝兩位今天撥時間開這個會議。"),
    (1, 2400, 5200, "不會，我們也想盡快確認導入時程。"),
    (2, 5200, 9600, "目前規劃分兩階段，第一階段先上客戶資料同步。"),
    (3, 9600, 13200, "了解，那報價的部分我們內部確認後再回覆。"),
]
SUMMARY_STRUCTURED = {
    "discussion": "確認 CRM 導入分兩階段進行，第一階段先上客戶資料同步。",
    "key_points": ["第一階段：客戶資料同步", "報價待客戶內部確認"],
    "decisions": ["導入時程採兩階段規劃"],
    "action_items": [{"task": "回覆報價確認結果", "owner": "客戶端", "due_date": None}],
    "open_questions": ["報價金額尚未確認"],
}


def main() -> int:
    settings = AppSettings(
        data_dir=DATA_DIR,
        models_dir=DATA_DIR / "models",
        testing=True,
        open_browser=False,
        log_level="ERROR",
    )
    container = build_container(settings)

    existing = [m for m in container.meeting_service.list(limit=100) if m.title == DEMO_TITLE]
    if existing:
        print(f"already seeded: meeting id {existing[0].id}")
        return 0

    meeting = container.meeting_service.create(title=DEMO_TITLE, language="zh")
    transcription = container.transcriptions.create(
        meeting_id=meeting.id,
        title=DEMO_TITLE,
        engine="faster-whisper",
        model="demo-seed",
        language="zh",
        settings={},
    )

    speakers: dict[str, int] = {}
    with container.database.transaction() as connection:
        for name in ("主持人", "客戶窗口"):
            cursor = connection.execute(
                "INSERT INTO speakers(meeting_id, stable_key, display_name, created_at)"
                " VALUES (?, ?, ?, datetime('now'))",
                (meeting.id, f"seed:{name}", name),
            )
            speakers[name] = int(cursor.lastrowid)

    for index, (segment_index, start_ms, end_ms, text) in enumerate(SEGMENTS):
        speaker_name = "主持人" if index % 2 == 0 else "客戶窗口"
        draft = SegmentDraft(
            index=segment_index,
            start_ms=start_ms,
            end_ms=end_ms,
            text=text,
            confidence=0.97,
            metadata={"speaker_id": speakers[speaker_name]},
        )
        container.transcriptions.append_segment(transcription.id, draft, is_final=True)
        with container.database.transaction() as connection:
            connection.execute(
                "UPDATE transcript_segments SET speaker_id = ?"
                " WHERE transcription_id = ? AND segment_index = ?",
                (speakers[speaker_name], transcription.id, segment_index),
            )

    container.transcriptions.set_status(transcription.id, "completed")
    container.transcriptions.activate(transcription.id)

    summary = container.summaries.create(
        meeting_id=meeting.id,
        transcription_id=transcription.id,
        provider="llama-cpp",
        model="demo-seed",
    )
    container.summaries.complete(
        summary.id,
        content_markdown=(
            "## 會議摘要\n\n確認 CRM 導入分兩階段：第一階段先上客戶資料同步，"
            "報價由客戶內部確認後回覆。\n\n### 待辦\n\n- 客戶端回覆報價確認結果"
        ),
        structured=SUMMARY_STRUCTURED,
    )
    print(f"seeded meeting id {meeting.id} with transcription {transcription.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
