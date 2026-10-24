# CRM 整合說明（MVP 驗證階段）

狀態：**接口預留 + 本機 mock CRM 驗證完成**。真實 CRM 尚未就緒；本文件描述
Meet2Notes 對外 CRM 接口、本機 mock CRM（驗證替身）的契約，以及重現驗證的步驟。

## 1. 架構

```
Meet2Notes（FastAPI，預設 127.0.0.1:8765）
   │  CRMClient（domain/protocols.py 埠；adapters/crm/http_client.py 實作）
   │  GET  {base}/api/crm/meetings/{code}/agenda   議程查詢
   │  POST {base}/api/crm/summaries                摘要送出（Idempotency-Key）
   │  POST {base}/api/crm/transcripts              逐字稿送出（選配，預設關閉）
   │  POST {base}/api/crm/meetings/{code}/ended    會談結束事件
   ▼
Mock CRM（crm-integration-verification-tool，Next.js，127.0.0.1:8766）
   │  冪等鍵記錄（重複→duplicate、同 key 異內容→409）
   │  錯誤注入（429/500/401/逾時）
   ▼
/crm dashboard — 檢視收到的摘要/逐字稿/結束事件與錯誤注入控制
```

## 2. 設定（環境變數，`M2N_` 前綴）

| 變數 | 預設 | 說明 |
|---|---|---|
| `M2N_CRM_BASE_URL` | 未設定（**CRM 功能停用**） | 未設定時所有 `/api/crm/*` 回 503；僅允許 HTTPS 或 loopback HTTP |
| `M2N_CRM_AGENDA_PATH` | `/api/crm/meetings/{meeting_code}/agenda` | `{meeting_code}` 會代入 |
| `M2N_CRM_SUMMARY_PATH` | `/api/crm/summaries` | |
| `M2N_CRM_TRANSCRIPTS_PATH` | `/api/crm/transcripts` | |
| `M2N_CRM_MEETING_ENDED_PATH` | `/api/crm/meetings/{meeting_code}/ended` | |
| `M2N_CRM_HEALTH_PATH` | `/api/health` | |
| `M2N_CRM_TIMEOUT_SECONDS` | `10` | 1–120 秒 |
| `M2N_CRM_TOKEN` | 未設定 | 有值時送 `Authorization: Bearer …`；`repr` 已遮蔽 |

安全設計：會談代碼必須符合 `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` 才會進 URL；
非 2xx 回應只取 200 字元摘要，不回顯 headers；token 不會出現在日誌或 API 回應。

## 3. Meet2Notes 端 API（本機 → CRM）

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/api/crm/status` | 是否已設定 CRM（`configured`、`base_url`） |
| GET | `/api/crm/health` | 代理 CRM 健康檢查 |
| GET | `/api/crm/meetings/{code}/agenda` | 取得議程（正常化為 topics 列表，保留 CRM 原始排序） |
| POST | `/api/crm/meetings/{id}/deliver` | 送出最新已完成摘要；body `{"include_transcript": false, "meeting_code": "…"}`。**逐字稿預設不送**，`include_transcript: true` 才會送出逐字稿段落 |
| POST | `/api/crm/meetings/{id}/ended` | 送出會談結束事件（segments 數、是否有摘要） |

冪等鍵規則：`uuid5(meeting_uuid + 類別 + payload SHA-256)`（ended 事件不含
`ended_at`）。**同內容重送 → 同一把 key**（CRM 端應視為重複，不建新資料）；
內容改版（如人工編輯摘要）→ 新 key。逾時（HTTP 504）代表「送達狀態不明」，
不得據此重送新資料。

錯誤語意（Meet2Notes 對 CRM 失敗的回應）：

| 狀況 | Meet2Notes 回應 |
|---|---|
| CRM 連不上 | 503 `CrmConnectionError` |
| CRM 逾時 | 504 `CrmTimeoutError`（`retryable: true`） |
| CRM 回 4xx/5xx | 502 `CrmHTTPError` + `upstream_status` + `retryable`（408/425/429/5xx 為 true） |
| CRM 回應不符契約 | 502 `CrmContractError` |

## 4. Mock CRM（驗證替身）

位置：repo 旁的 `crm-integration-verification-tool/`（自
handy-integration-verification-tool 複製，原始工具未更動）。

```powershell
# 啟動（第一次先 npm install；改程式後先 npm run build）
cd <path-to>/crm-integration-verification-tool
npx next start -H 127.0.0.1 -p 8766
# 開啟 dashboard：http://127.0.0.1:8766/crm
```

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/api/health` | 活性檢查 |
| GET | `/api/crm/meetings/{code}/agenda` | 固定 fixture 議程（4 個議題） |
| POST | `/api/crm/summaries` | 摘要收受（驗 structured 五鍵） |
| POST | `/api/crm/transcripts` | 逐字稿收受（text / speaker / start_ms / end_ms / is_final） |
| POST | `/api/crm/meetings/{code}/ended` | 會談結束收受 |
| GET | `/api/crm/events` | 收送紀錄（dashboard 與測試用） |
| GET/POST | `/api/crm/control` | 錯誤注入設定與重置 |

冪等行為：所有 POST 需要 `Idempotency-Key` header（缺 → 400）；同 key 同
payload → 200 `duplicate: true`（不重複入列）；同 key 異 payload → 409
`idempotency_conflict`。ended 事件的 hash 排除 `ended_at`（時間戳為資訊欄位）。

煙霧測試（14 項）：

```bash
node tools/local_bridge/test_crm_backend.mjs --base-url http://127.0.0.1:8766
```

## 5. 重現 E2E 驗證（Meet2Notes → mock CRM）

```powershell
# 1. 啟動 mock CRM（上節），確認 http://127.0.0.1:8766/crm 可開啟

# 2. 種子資料（一次性；meeting id 1 = 「CRM 整合驗證會談」）
cd <path-to>/Meet2Notes
.\.venv\Scripts\python.exe tools\crm_seed_demo.py

# 3. 啟動 Meet2Notes（CRM 指向 mock）
$env:M2N_DATA_DIR="<path-to>/.demo-data"
$env:M2N_OPEN_BROWSER="false"
$env:M2N_CRM_BASE_URL="http://127.0.0.1:8766"
.\.venv\Scripts\python.exe -m local_meeting_ai --no-browser

# 4. 驗證（另開終端）
curl http://127.0.0.1:8765/api/crm/status
curl http://127.0.0.1:8765/api/crm/meetings/MTG-2026-001/agenda
curl -X POST http://127.0.0.1:8765/api/crm/meetings/1/deliver `
  -H "Content-Type: application/json" `
  -d '{\"include_transcript\": true, \"meeting_code\": \"MTG-2026-001\"}'
curl -X POST http://127.0.0.1:8765/api/crm/meetings/1/ended `
  -H "Content-Type: application/json" -d '{\"meeting_code\": \"MTG-2026-001\"}'
# 5. 打開 http://127.0.0.1:8766/crm 檢視收到的內容；重複送出應標記 duplicate
```

## 6. 測試與已驗證項目

- 自動化：`pytest` 327 passed（含 CRM 37 項：client 錯誤分類/冪等/契約驗證、
  routes 注入 fake CRM、逐字稿預設不送、404/422 錯誤路徑）；`ruff check` 通過；
  `mypy --strict src`（120 檔）通過。
- E2E（真程序）：議程查詢、摘要+逐字稿送達（zh-TW 文字/講者/毫秒時間戳）、
  重送 duplicate、ended 重送 duplicate、429 注入 → 502 retryable、
  缺 ended_at 的會談 → 以當下時間送出。

## 7. 已知限制（本階段刻意不做）

- 無持久化送出佇列（斷線重試、重啟恢復）— 現有呼叫為同步單次，正式化時依
  discovery 報告 §6 的 `crm_deliveries` outbox 設計實作。
- 無錄音同意閘門、議題分節、人工確認 UI — 詳見 `docs/CRM_MVP_DISCOVERY.md`。
- Mock CRM 議程為固定 fixture；正式 CRM 契約確定後僅需調整 `M2N_CRM_*` 路徑
  與 `adapters/crm/http_client.py` 的回應解析。
- `M2N_CRM_TOKEN` 以環境變數傳遞僅供本機驗證；正式環境應改用 keyring
  （Windows Credential Manager），模式同 `webhook_secrets.py`。
