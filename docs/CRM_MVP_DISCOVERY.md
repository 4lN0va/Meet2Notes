# CRM MVP 調查報告（Phase 0 Discovery）

- 日期：2026-10-24
- 調查基準：`estebanstifli/Meet2Notes` `main` @ `e05c0f0`（"fix: keep GGUF metadata inspection optional in minimal installations"），版本 0.9.0
- 工作目錄：`D:\CRM_Project\Meet2Notes`（全新 clone，原始工作目錄乾淨，未覆蓋任何既有變更）
- 目的：在既有 Meet2Notes 上實作「CRM 導向會議工作流程 MVP」前的 repository 調查，並評估使用者提供之驗證前端（`E:\Default\Desktop\handy-integration-verification-tool`）作為 CRM 替身的可行性。
- 語言：台灣繁體中文（zh-TW）

---

## 0. 摘要（TL;DR）

1. Meet2Notes 是一個**本機優先**的會議轉錄工作站：FastAPI（單一 Python 套件 `local_meeting_ai`，hexagonal 分層）+ Jinja2/vanilla JS 前端 + SQLite（WAL）。錄音（Windows WASAPI）、即時/最終轉錄（faster-whisper 等）、講者分離（sherpa-onnx / pyannote）、本機 LLM 摘要（llama.cpp GGUF / litellm / Ollama）、持久化工作佇列與**出站 Webhook outbox**皆已存在且可重用。
2. 規格所需的 CRM 概念（agenda、consent、section、delivery、idempotency）**全部不存在**，需要新建；但每一項都有可直接沿用的內部模式（見 §4、§7）。
3. 使用者的驗證前端目前實作的是 **Handy 桌面端 → HTTP bridge 的即時逐字稿契約**（heartbeat + transcripts），**尚未包含**本規格 CRM adapter 所需的議程查詢、摘要送出、會談結束端點。可直接沿用作為 E2E 驗證台與未來 mock CRM 的基底，但需要擴充（見 §9）。
4. **埠衝突**：Meet2Notes 預設 API 埠 `8765`（`M2N_PORT`），驗證前端預設埠也是 `8765`，同一台機器無法同時執行，必須錯開（見 §9.4）。
5. 本機僅安裝 Python 3.14.6；`install.ps1` 探測的是 3.11–3.13，且 `dev` extras 內含 `httpx2`（非常見套件）。基準測試以核心依賴 + dev 工具完成（重量級引擎皆為延遲載入，測試不需要），結果見 §12。

---

## 1. 調查範圍與方法

- `git clone https://github.com/estebanstifli/Meet2Notes.git` → `git status/branch/log/remote` 檢查（全新 clone，狀態乾淨）。
- 閱讀：`README.md`、`CONTRIBUTING.md`、`CHANGELOG.md`、`pyproject.toml`、`.env.example`、`docs/`（architecture、audio-capture、privacy、webhooks、localization、plugins、mcp、updating 等；repository 無獨立 `API.md` 與 "safe updates" 文件，對應文件為 `docs/updating.md`）、`install.ps1`、`install.sh`、`start.bat`。
- 逐層掃描 `src/local_meeting_ai/`（domain / application / adapters / infrastructure / api / web / mcp / plugins）與 `tests/`。
- 另行調查使用者提供的 `E:\Default\Desktop\handy-integration-verification-tool`（原始碼、README、Rust 整合片段、Python 模擬器、git 狀態）。
- 依賴安裝與測試執行結果見 §12。

## 2. 現有架構圖

```
瀏覽器（Jinja2 模板 + vanilla JS，i18n 目錄 en/es）
  /（transcript.html 主工作區）、/meetings、/settings、/prompt、/speakers
     │  fetch /api/* ＋ SSE GET /api/events（jobs、activity 事件）
     ▼
FastAPI（src/local_meeting_ai/api/）
  app.py create_app(...)：bootstrap.build_container 組裝、lifespan 啟動 queue/
    webhook_service/live_assistant_service、TrustedHost（僅 localhost）+
    安全標頭 middleware、DomainError → 400/404/409/413/422/503
  routes.py（/api 前綴）、library_routes.py、live_assistant_routes.py、
  webhook_routes.py（/api/webhooks/*）、dependencies.py（get_container DI）
     ▼
Application 層（application/）
  MeetingService／ImportService／TranscriptionService（即時 + 最終）／
  DiarizationService／SummaryService／LiveCaptureService（單一活動 session）／
  WebhookService（CloudEvents 出站 + outbox dispatch）／RagService／
  LiveAssistantService／FinalProcessingPipeline（佇列 terminal handler 串接
  轉錄→分離→摘要）／summary_templates（9 個內建範本，含 Sales Call）
     ▼
Domain 層（domain/）
  凍結 dataclass 實體 + StrEnum（MeetingStatus: draft|importing|ready|failed；
  JobStatus: queued|running|paused|completed|failed|cancelled）+
  protocols.py 六角埠（TranscriptionEngine、AudioCaptureBackend、
  DiarizationEngine、SummaryEngine、EmbeddingProvider…）
     ▼
Infrastructure 層（infrastructure/）
  SQLite（WAL、busy timeout 5s、sqlite-vec 可選）：自製 MigrationRunner +
  編號 SQL migration 001–013（infrastructure/database/sql/）
  repositories.py（Meeting/Recording/Job/Settings/Transcription/Summary/…）
  LocalJobQueue（jobs 表持久化 + asyncio.Queue worker；重啟時
  recover_interrupted + 重新入列 queued；無自動重試）
  keyring 憑證（webhook 簽章 secret、litellm API key；testing 時換 Memory 版）
  FFmpegClient、Ollama 探測、Bonsai（受管 LLM runtime）、硬體偵測
     ▼
Adapters
  audio_capture：windows_wasapi.py（PyAudioWPatch，WASAPI loopback）+
    combined.py（麥克風＋系統音訊雙來源 AudioMixer 混音）
  transcription：faster_whisper.py／nvidia_asr.py／vibevoice.py + router
  diarization：sherpa_onnx.py／diarize_cpu.py／pyannote_community.py／
    nemotron3.py + router + 聲紋比對（speaker_profiles）
  summary：llama_cpp.py（本機 GGUF／litellm 遠端／Ollama 三合一，1315 行，
    長逐字稿階層式 map-reduce）+ router
  embeddings：fastembed／local／litellm
     ▼
外掛系統（plugins/）：entry point 群組 "meet2notes.plugins"；
  hooks：analysis.before/after、final_transcription.*、diarization.*、
  analysis.*、pipeline.finished、vector-store
MCP gateway（mcp/）：唯讀工具（list_meetings、get_transcript、get_summary…），
  integrations/anna 即以此模式做外部整合
```

## 3. 相關程式檔案及責任

| 領域 | 主要檔案 | 責任／現況 |
|---|---|---|
| API | `api/app.py` | `create_app(settings, *, 各引擎 fake 注入點)`；lifespan；網頁路由；錯誤映射 |
| API | `api/routes.py`（約 2512 行） | `/api` 全部路由：meetings、transcriptions、summaries、summary-templates、jobs、settings、engines/models、live-capture、events(SSE)、health/info |
| API | `api/webhook_routes.py` | `/api/webhooks`：端點 CRUD、rotate-secret、test、deliveries 查詢/重試、insights |
| API | `api/schemas.py`（約 825 行） | 全部 Pydantic request/response 模型 |
| API | `api/dependencies.py` | 唯一 DI：`get_container`；測試以 `create_app(...)` 參數注入 fake，不用 dependency_overrides |
| Application | `application/services.py` | MeetingService（CRUD、狀態）、ImportService（匯入→正規化→READY） |
| Application | `application/capture_service.py` | `LiveCaptureService`：單一活動即時錄音 session；start/stop/pause/resume/discard；3 秒窗 + 1 秒重叠即時轉錄、暫存 segment 去重提交 |
| Application | `application/transcription_service.py` | 最終轉錄 job（FFmpeg 正規化 → engine → segments）；`begin_realtime`／`finalize_realtime`（保留既有 segments 的最終校準） |
| Application | `application/ai_services.py` | DiarizationService、SummaryService（SUMMARIZE job；template 快照進 payload；plugin 過濾 hook） |
| Application | `application/final_pipeline.py` | 佇列 terminal handler：轉錄完成→（分離）→（摘要）→ 發 plugin hooks + webhooks |
| Application | `application/webhooks.py` | WebhookService：CloudEvents structured mode、HMAC-SHA256 簽章、outbox 派送、指數退避重試 |
| Application | `application/summary_templates.py` | 9 個內建範本（General/Sales Call/Formal Minutes…）；`render_summary_template` |
| Domain | `domain/enums.py`、`domain/entities.py` | 現有全部狀態列舉與凍結實體；**無** agenda/consent/section/delivery 概念 |
| Domain | `domain/protocols.py` | 六角埠介面（CRMClient 埠應新增於此） |
| Domain | `domain/webhooks.py` | 16 種出站事件目錄（live.*、transcription.final.*、summary.*、meeting.processing.completed） |
| Infrastructure | `infrastructure/database/migrations.py` + `database/sql/001–013` | 自製編號 migration（`schema_migrations` 表追蹤）；無 Alembic |
| Infrastructure | `infrastructure/database/repositories.py`（1778 行） | 各 repository；`JobRepository.recover_interrupted` |
| Infrastructure | `infrastructure/database/webhooks.py` | **outbox 模式**：`enqueue_event`、`claim_due`、`finish_delivery`、`retry_delivery`、`recover_delivering` |
| Infrastructure | `infrastructure/jobs.py` | `LocalJobQueue`：持久化 job、asyncio worker（`max_heavy_jobs`，預設 1）、cooperative cancel、無自動重試 |
| Infrastructure | `infrastructure/config.py` | `AppSettings`（pydantic-settings，env 前綴 `M2N_`）；host/port 預設 `127.0.0.1:8765` |
| Infrastructure | `infrastructure/webhook_secrets.py`、`adapters/summary/credentials.py` | Windows Credential Manager（keyring）儲存機密；`testing=True` 換 Memory 實作 |
| Audio | `adapters/audio_capture/windows_wasapi.py` | PyAudioWPatch WASAPI：`list_sources()`（標記 loopback=`kind:"system"`）、`start(session_id, source_id, destination, additional_source_id=...)`、`drain_frames()`、`pause/resume/stop()→CapturedAudio`、`probe_level()` 音量探測 |
| Audio | `adapters/audio_capture/combined.py`、`mixer.py` | 麥克風＋系統 loopback 雙來源同時擷取、背景執行緒混音成單一 WAV |
| Transcription | `adapters/transcription/*` | 引擎皆延遲載入（`importlib`）；`TranscriptionEngineRouter` 依請求 engine 分派 |
| Summary | `adapters/summary/llama_cpp.py` | 本機 GGUF／litellm（含 Ollama `ollama_chat/` 前綴）／長文階層式摘要 |
| Web UI | `web/templates/*.html`、`web/static/js/app.js`（782 行）＋ `transcript.js`（3148 行）、`settings.js`（2952 行） | 主工作區、即時錄音狀態機、SSE 訂閱、i18n `t()` |
| i18n | `web/static/locales/{en,es}.json`、`scripts/check_ui_i18n.py` | 靜態 JSON 目錄 + 一致性檢查（**無 zh-TW**） |
| Tests | `tests/unit|api|integration|web`、`tests/conftest.py` | tmp 目錄真 SQLite + migrations；`TestClient(create_app(settings))`；fake 引擎注入 |
| 整合範例 | `integrations/anna/` | 外部（Anna marketplace）唯讀整合：走 MCP gateway，非寫入路徑範本 |

## 4. 可直接重用的服務與 API

| 規格需求 | 可重用元件 | 重用方式 |
|---|---|---|
| 會談實體與儲存 | `MeetingService` + `meetings` 表（已含 started_at/ended_at/status） | 直接重用；CRM 工作流狀態另建連結表，**不動**現有 `MeetingStatus` 語意 |
| 錄音同意閘門 | `LiveCaptureService.start` 是唯一錄音入口；`ConfirmationRequiredError`（409）既有模式 | 在 service/API 層加同意檢查，未同意拋 `ConfirmationRequiredError` |
| 麥克風＋系統音訊 | `CombinedCaptureBackend` + `WindowsWasapiCaptureBackend` | 已支援雙來源同時錄音、`probe_level()` 可做零音量提示 |
| 即時逐字稿（預設隱藏） | 即時轉錄 + `transcript_segments(is_final=0/1)`；UI 為主動渲染 | 資料層照舊；UI 預設收合即符合「預設隱藏」 |
| 結束本節→摘要 | `FinalProcessingPipeline`（轉錄→分離→摘要串接）+ `SummaryService` | 摘要工作觸發、進度（SSE `/api/events`）、取消/重試皆現成 |
| 摘要結構化格式 | `SummaryTemplate`（sections 可自訂）＋ `summaries.structured` 欄位 | 新增 CRM 範本（discussion/key_points/decisions/action_items/open_questions） |
| 本機 LLM | `LlamaCppSummaryEngine`（GGUF／Ollama/litellm）+ `summary_engine` 設定 | 直接重用；provider 不可用時既有診斷路徑（`/api/engines/summary/*`） |
| 持久化送出任務 | `webhook_events`/`webhook_deliveries` outbox + `WebhookRepository.claim_due/finish_delivery/retry_delivery` + `recover_delivering` | **模式與程式碼可直接仿造**為 CRM delivery outbox（見 §7） |
| 重試策略 | `application/webhooks.py`：5s/30s/120s/600s/1800s 指數退避、429/5xx/408/425 重試、尊重 `Retry-After` | CRM 重試沿用同一策略類別/常數 |
| Token 安全儲存 | keyring store 模式（`Meet2Notes` service、per-endpoint account） | CRM token 用同一模式；`testing` 換 Memory |
| 設定 | `AppSettings` + `settings` KV 表 + `/api/settings` | CRM 設定走同一機制（base URL、endpoint 路徑、timeout、retry、環境） |
| UI 進度/狀態 | SSE `/api/events` + `window.Meet2Notes` 共用層 + job 進度元件 | CRM 送出狀態用同一管線顯示 |
| 測試基建 | conftest（tmp SQLite + migrations + TestClient）、fake 引擎注入、webhook HMAC 測試範例 | CRM 測試比照：mock CRM 用 httpx fake transport / 注入 fake CRMClient |

## 5. 需要新增或修改的模組（最小改動方案）

**新增**（皆為新檔案/新表/新路由前綴，不重寫既有機制）：

1. `domain/crm.py` — CRM 值物件：`CrmMeetingState`、`SectionStatus`（pending/active/processing/awaiting_review/confirmed/delivery_pending/delivered/delivery_failed）、`CrmDelivery`、agenda snapshot 模型、同意紀錄模型。
2. `domain/protocols.py` 新增 `CRMClient` 埠（`get_meeting_agenda` / `submit_summary` / `submit_meeting_ended` / `health_check`；同步/非同步與錯誤型別配合現有 async 慣例）。
3. `adapters/crm/http_client.py` — httpx 實作（timeout、錯誤分類 → `domain/errors.py` 新增 CRM 錯誤類型）、`adapters/crm/mock_client.py` — 測試用。
4. `application/crm_workflow.py` — 會談工作流協調：載入議程快照、同意閘門、節（section）生命週期、摘要版本（AI 原始/人工編輯/已確認送出）、送出任務建立（與摘要確認同 transaction）、meeting-ended 事件。
5. `application/crm_delivery.py` — 送出 dispatcher（仿 `application/webhooks.py` 的 outbox 派送：claim → POST → 退避重試 → 狀態機）。
6. `infrastructure/database/sql/014_crm_mvp.sql` + `infrastructure/database/crm.py`（repository）+ `repositories.py` 掛接 — 新表見 §6。
7. `api/crm_routes.py`（前綴 `/api/crm`）+ `api/schemas.py` 新增模型 — 會談代碼輸入、議程載入、同意、節操作（開始/結束/新增臨時議題/改名/排序）、摘要確認/編輯、送出/重試、狀態查詢。
8. UI：新頁面 `web/templates/crm.html` + `web/static/js/crm.js`（沿用 `app.js` 共用層與 SSE）；主工作區加「CRM 會談」入口；i18n 新增 zh-TW 目錄（見 §11 風險）。
9. 設定：`AppSettings` 新增 `crm_*` 欄位（或存 `settings` KV）：base URL、各 endpoint 路徑、驗證方式、timeout、retry 上限、是否允許送逐字稿（**預設 false**）、環境（dev/test/prod）；token 入 keyring。
10. 測試：`tests/unit/test_crm_*.py`、`tests/api/test_crm_*.py`、`tests/integration/test_crm_recovery.py`（重啟恢復待送任務）。

**修改既有檔案**（最小侵入）：

- `api/app.py`：註冊 `crm_router`；lifespan 啟動 crm delivery dispatcher。
- `bootstrap`（container 組裝）：加入 CRMClient 與 dispatcher。
- `LiveCaptureService.start`／對應 API：同意閘門檢查點（未同意 → `ConfirmationRequiredError`）。
- `web/static/js/app.js`：側欄加入口（僅導覽，不動現有頁面）。
- `locales/index.json` + 新增 `zh-TW.json`；`check_ui_i18n.py` 的語言清單。

**明確不重寫**：錄音管線、轉錄引擎、分離引擎、摘要引擎、佇列、webhook 系統、MCP、RAG、Live Assistant。

## 6. 資料庫現況與 migration 建議

- 現況：自製 `MigrationRunner` + 編號 SQL（`001_initial` … `013_library`），`schema_migrations(version, filename, applied_at)` 追蹤。既有 25 張表，含 `meetings`、`recordings`、`transcriptions`、`transcript_segments`、`summaries`、`summary_templates`、`jobs`、`settings`(KV)、`webhook_endpoints`、`webhook_events`、`webhook_deliveries`、`webhook_insights`、`live_assistant_*`、`meeting_tags` 等。
- 建議：**新增 `014_crm_mvp.sql`（僅新增表，不修改既有表與歷史 migration）**：
  - `crm_meetings` — `id`、`meeting_id`（FK→meetings，1:1）、`crm_meeting_code`、`crm_meeting_id`、`title_snapshot`、`agenda_snapshot_json`（含載入時間、topic ID、原始排序）、`consent_given_at`、`consent_text_version`、`state`（draft/ready/recording/paused/ending/ended/error）、`created_at/updated_at`。
  - `crm_sections` — `id`、`crm_meeting_id`、`crm_topic_id`（NULL=臨時議題）、`original_name`、`display_name`、`order_index`、`status`（pending/active/processing/awaiting_review/confirmed/delivery_pending/delivered/delivery_failed）、`started_at/ended_at`、`transcription_id`/segment 範圍對應（Phase 1 設計定案）。
  - `crm_summaries` — `id`、`section_id`、`version`、`kind`（ai_original/edited/confirmed_submission）、`content_json`、`content_hash`、`confirmed_at`、`created_at`。
  - `crm_deliveries` — `delivery_id`（UUID，重試沿用）、`crm_meeting_id`、`section_id`（可 NULL=會談結束事件）、`delivery_type`（summary/meeting_ended）、`payload_snapshot_json`、`payload_version`、`idempotency_key`、`status`、`attempt_count`、`next_retry_at`、`last_http_status`、`error_category`（去敏感分類）、`created_at/updated_at/delivered_at`。
  - `crm_events` — meeting-ended 事件：`event_id`、`crm_meeting_id`、`schema_version`、`payload_snapshot_json`、`delivery_status`、`created_at/delivered_at`。
- 一致性要求（規格 10.3）：摘要「確認」與 `crm_deliveries` 建立必須同一 transaction；網路呼叫一律在 transaction 之外。

## 7. 既有 Webhook 能力是否可重用

**結論：模式與部分程式碼可重用，但不建議把 CRM 送出硬塞進 CloudEvents 格式。**

- 可直接沿用：outbox 表結構與 `claim_due/finish_delivery/retry_delivery/recover_delivering` 流程、指數退避常數與 `Retry-After` 處理（`application/webhooks.py`）、keyring secret 儲存、deliveries 查詢/手動重試 API 與 UI 模式、HMAC 簽章（若 CRM 需要反向驗證）、at-least-once 語意與 `X-Meet2Notes-Delivery` 冪等標頭經驗。
- 不直接沿用的事件格式：CRM 契約是請求/回應式（查議程、送摘要、送結束事件、健康檢查），payload 由 CRM 定義；CloudEvents structured mode（`application/cloudevents+json`）是給通用 webhook 訂閱者的。
- 關聯設計：CRM 送出成功後可另發一個既有 webhook 事件（如新增 `crm.delivery.completed`）讓現有訂閱者感知，屬加分項非必要。

## 8. Windows 音訊擷取現況（啟動/停止方式）

- 啟動：`POST /api/live-capture/...`（`LiveCaptureStartRequest`：來源選擇）→ `LiveCaptureService.start` → 建立 Meeting（DRAFT）+ 即時 transcription → `backend.start(session_id, source_id, destination, additional_source_id=...)`：Windows 上由 `CombinedCaptureBackend(WindowsWasapiCaptureBackend())` 同時開麥克風與 WASAPI loopback，背景 `AudioMixer` 執行緒混音；asyncio task 以 3 秒窗（1 秒重叠）呼叫轉錄引擎，暫存 segment（`is_final=0`）去重後入庫。
- 進行中：`drain_frames()` 取樣；`pause/resume/discard` 支援；`probe_level()`/`_pcm_level` 提供音量偵測（可做「無輸入/零音量」提示）。
- 停止：`LiveCaptureService.stop` → `backend.stop()` → `CapturedAudio`（WAV 存至 `meetings/<uuid>/`）→ 註冊 import job（FFmpeg 正規化）→ `finalize_realtime` 排入最終轉錄 job（保留即時 segments）→ webhook/live assistant 通知；串流與資源釋放。
- 單一活動 session 限制（`LiveCaptureService` 一次一場）符合規格「同一時間最多一個 active section」的錄音側約束。
- 同意撤回：現無此概念；需在 `LiveCaptureService` 增加「立即停止 + 依政策處理暫存」掛點（Phase 3）。

## 9. 驗證前端（handy-integration-verification-tool）分析

位置：`E:\Default\Desktop\handy-integration-verification-tool`（Next.js 16 + React 19，MIT；綁定 `127.0.0.1:8765`；無資料庫，事件存記憶體 ring buffer 100 筆）。該 repo 有使用者未提交的修改（`package.json`、`src/app/layout.tsx`、`tools/local_bridge/README.md`），**未經確認不得覆蓋**。

### 9.1 現有契約

| 方法 | 路徑 | 內容 |
|---|---|---|
| GET | `/api/health` | 活性檢查（`database: "not_required"`） |
| POST | `/api/handy/heartbeat` | `{app_version?, recording, status?, client?}`；Handy 每 5 秒 |
| POST | `/api/transcripts` | `{session_id, segment_id, text, final, timestamp, source?}`；ASR partial/final，422 驗證失敗 |
| GET | `/api/status` | dashboard 每 1.5 秒輪詢；三狀態獨立推導（後端/heartbeat/文字） |

配套：`tools/local_bridge/handy_bridge_client.py`（Python 模擬器，輸出標 `[SIMULATOR]`）、`handy_integration_snippet.rs`（真實 Handy fork 的 Rust/Tauri 整合片段：背景 thread、2 秒 timeout、失敗只記 log）、`test_backend.mjs`（煙霧測試：合法/非法 payload、UTF-8 繁中、partial→final 合併、完整模擬流程）。

### 9.2 與本規格 CRM 契約的落差

| 規格需要（§4.1 CRMClient） | 驗證前端現況 | 落差 |
|---|---|---|
| `health_check` | `GET /api/health` | ✅ 可直接重用 |
| `get_meeting_agenda(code)` | 無 | ❌ 需新增 mock 端點（含固定 fixture 議程、可設定錯誤注入） |
| `submit_summary(payload, idempotency_key)` | 無 | ❌ 需新增（應驗證冪等鍵、可注入 400/401/404/409/429/5xx/timeout） |
| `submit_meeting_ended(payload, idempotency_key)` | 無 | ❌ 需新增 |
| 重複送出防護驗證 | 無 | ❌ mock 需記錄收到的 idempotency key 供查詢 |

另注意：該工具的資料流方向是「桌面端 → 接收端」（Handy 推逐字稿進來），本規格 CRM adapter 是「Meet2Notes → CRM」（推摘要出去）；兩者角色不同但互補——它本身就是一個可驗證「用戶端以正確契約送資料」的現成驗收台。

### 9.3 建議採用方式（分層）

1. **自動化測試（pytest）**：不依賴驗證前端執行；在 `tests/` 內以 httpx MockTransport／fake `CRMClient` 完成規格 13.1–13.4，保證 CI 可離線重現。
2. **手動/E2E 驗收**：**沿用驗證前端**——擴充 `src/app/api/crm/*` mock CRM 端點（agenda fixture、summary/ended 送出、idempotency 記錄、dashboard 上的錯誤注入開關與送出紀錄檢視），沿用其既有 dashboard、驗證與測試慣例。此擴充屬於對使用者 repo 的修改，**待使用者確認後進行**（Phase 2 一併處理）。
3. **契約文件**：擴充後的端點即為「暫定 CRM 契約 fixture」，正式 CRM 就緒後僅替換 `adapters/crm/http_client.py` 的設定值，不改動應用層。

### 9.4 埠衝突（必須處理）

Meet2Notes 預設 `M2N_PORT=8765`（`.env.example`、`infrastructure/config.py`），驗證前端預設埠亦為 `8765`。同一台 Windows 同時執行兩者會綁定失敗。建議：驗證前端以 `npx next start -H 127.0.0.1 -p 8766` 執行（或另定 CRM mock 埠），Meet2Notes 維持預設；設定檔中 CRM base URL 預設指向 `http://127.0.0.1:8766`（僅開發環境預設，正式環境必須明確設定且強制 HTTPS）。

### 9.5 待確認的架構問題

驗證前端目前服務的對象是 **Handy**（Rust/Tauri 桌面轉錄 app，Breeze-ASR-25），與 Meet2Notes 是兩個不同的應用程式。需要使用者確認整體架構（見 §13 Q1）：摘要送 CRM 的是 Meet2Notes（本規格範圍）、Handy、或兩者各有工作流。

## 10. 尚未提供的 CRM 規格（需要使用者提供）

在真實 CRM API 文件到手前，mock 以外的**一切端點/欄位/驗證都不得猜測**（規格 4.6）。需要：

1. 會談代碼格式（字元集、長度、校驗）與查詢議程端點（method、路徑、回應 schema、topic ID 穩定性、排序欄位）。
2. 摘要送出端點：payload 欄位定義（discussion/key_points/decisions/action_items/open_questions 的對應）、成功回應範例、是否支援 `Idempotency-Key` header 或其他去重機制。
3. 會談結束端點/事件格式：欄位、`ended_at` 時區要求、completed/pending/partial_failure/failed 語意對應。
4. 驗證方式（API key / OAuth / 其他）、token 有效期與刷新。
5. Rate limit（RPS/瞬間上限）與建議 retry 政策。
6. 是否支援接收逐字稿（影響「同時送出逐字稿」選項是否開放）。
7. 臨時議題（無 CRM topic ID）的協議欄位格式；若不接受，錯誤語意。
8. 各環境（開發/測試/正式）base URL；正式是否強制 mTLS/允許清單。

## 11. 向後相容性風險

| 風險 | 說明 | 緩解 |
|---|---|---|
| `MeetingStatus` 列舉擴充 | 現有 `draft|importing|ready|failed` 語意是匯入生命週期；UI/JS 與 `FinalProcessingPipeline` 依賴它 | **不擴充**；CRM 工作流狀態放 `crm_meetings.state`，現有列舉不動 |
| 同意閘門侵入 `LiveCaptureService.start` | 一般（非 CRM）錄音不應被強制要求同意 | 同意檢查僅在「CRM 會談」路徑生效（以 `crm_meeting_id` 綁定判斷） |
| 新增 `/api/crm/*` 與 lifespan dispatcher | 影響面小，但 dispatcher 錯誤不得拖垮啟動 | dispatcher 以現有 webhook_service 同模式啟動並隔離例外 |
| i18n 新增 zh-TW | `scripts/check_ui_i18n.py` 強制目錄齊一；新增語言 = 全目錄翻譯 | Phase 9 一次補齊 zh-TW 完整目錄；期中開發以 en 鍵值顯示不落地半套目錄 |
| SQLite migration | 新表 + FK 對既有 `meetings`；升級失敗時的復原 | 純新增、可重跑驗證；補 `tests/integration/test_migrations.py` 對應案例 |
| 預設設定值 | CRM base URL 預設值若指向本機 mock，誤開正式會送錯地方 | 正式環境未明確設定時，CRM 功能整體停用並顯示未設定狀態（fail-closed） |

## 12. 開發環境現況與基準測試結果

- 作業系統：Windows 10 x64（10.0.19045）；本機 Python 僅 **3.14.6**（`py -0` 無 3.11–3.13）。`install.ps1` 依序探測 `py -3.12/-3.13/-3.11`，在此機器上不會命中；`pyproject.toml` 要求 `>=3.11`。本次以 Python 3.14 + 最小依賴建立 `.venv` 執行基準（結果見下）；完整 `install.ps1 -Dev -Models none`（含 torch、llama-cpp-python 等重量級 extras）尚未執行，屬已知待辦。
- 測試不需要重量級引擎：所有 adapter 對 `faster_whisper`/`llama_cpp`/`sherpa_onnx`/`PyAudioWPatch` 等皆為執行期 `importlib` 延遲載入；`tests/` 自身僅直接 import `fastapi`、`httpx`、`numpy`、stdlib。
- 基準測試指令與結果（2026-10-24，Python 3.14.6，`.venv` 核心依賴 + `[dev]` extras）：

```text
$ ./.venv/Scripts/ruff.exe check .
All checks passed!                                → PASS（exit 0）

$ ./.venv/Scripts/mypy.exe src
Success: no issues found in 115 source files      → PASS（exit 0）

$ ./.venv/Scripts/python.exe -m pytest -q
290 passed, 336 warnings in 86.78s                → PASS（exit 0）
```

- 警告內容：`pytest-asyncio` 於 Python 3.14 上的 `asyncio.get_event_loop_policy` DeprecationWarning（Python 3.16 將移除），屬上游相依警告，非專案程式碼問題；日後以 3.12/3.13 建置可消除。
- 尚未執行：完整 `install.ps1 -Dev -Models none`（torch／llama-cpp-python／faster-whisper 等重量級 extras）與 `tests/web/*.test.cjs`（需 Node 執行）；本機音訊裝置相關測試待 Phase 3 於真機人工驗證。

## 13. 待確認事項（Blocking questions）

> **2026-10-24 決議**：使用者已回覆全部六項——(1) 另建 Meet2Notes 專用驗證前端，上傳內容含逐字稿與摘要（JSON：文字、講者、時間戳記）；(2) 不修改原始驗證前端，於工作區複製副本實作 mock CRM；(3) 本階段僅預留接口、對上 mock CRM 驗證即停；(4) mock CRM 用埠 8766；(5) 接受 Python 3.14，必要時降版於虛擬環境；(6) 一切 git commit 僅留在本地。實作結果見 `docs/CRM_INTEGRATION.md`。

1. **整合架構**：最終把摘要送進 CRM 的是 Meet2Notes（本規格）還是 Handy（驗證前端目前契約的對象），或兩者並行（不同工作流）？這決定 Phase 1 狀態機要不要接 Handy 的 session_id/transcript 事件。
2. **驗證前端擴充授權**：同意我依 §9.3-2 擴充 `E:\Default\Desktop\handy-integration-verification-tool`（新增 `/api/crm/*` mock 端點 + dashboard 錯誤注入）嗎？該 repo 有未提交變更，我會先 branch 再動工，不覆蓋既有修改。
3. **CRM 契約**：§10 清單何時能提供？在那之前自動化測試全部以 mock/fake 進行，不標記真實 CRM 驗收。
4. **埠配置**：同意 mock CRM 用 8766（或你指定埠）、Meet2Notes 維持 8765？
5. **Python 版本**：本機僅 3.14.6；是否需要我引導安裝 3.12/3.13（`install.ps1` 預期版本），或接受 3.14 作為開發驗證環境？
6. **分支與提交**：已依規格建立功能分支；`docs/CRM_MVP_DISCOVERY.md` 是否由我 commit 到該分支（不 push）？

## 14. 下一步（Phase 1 預覽）

使用者確認 §13 後，Phase 1 產出：狀態機定義（`crm_meetings.state` × `crm_sections.status` 轉換表 + 防護規則）、節 ↔ 逐字稿範圍對應設計、摘要版本模型、送出任務狀態機與冪等鍵規則，並以失敗路徑測試先行（規格 3.4）。

---

*本報告僅記錄已驗證之事實；所有「建議」均為最小改動方案，未經使用者確認前不實作。*
