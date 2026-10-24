# Handover — Meet2Notes CRM 整合專案交接文件

- 日期：2026-10-24
- 語言：台灣繁體中文（zh-TW）
- 狀態：MVP 驗證階段完成（介面預留 + 本機 mock CRM 驗證 + 助理編輯逐字稿），未接真實 CRM
- 授權：沿用上游 MIT

---

## 1. 專案概述

將 Meet2Notes（本機優先的會議轉錄工作站）擴充為「CRM 導向的會議紀錄工作流程」：

- 錄音或匯入影片 → 本機 GPU 轉錄（faster-whisper）→ 講者分離（sherpa-onnx）
- 摘要與會議助理問答走 OpenAI 相容端點（示範環境：本機 llama.cpp server 跑 Qwen3.8-27B）
- 逐字稿/摘要可送出到 mock CRM（驗證契約與冪等），真實 CRM 尚未就緒
- 會議助理具三段權限（僅回答 / 編輯逐字稿 / 完全權限），可直接修改逐字稿

## 2. Repos 與來源

| Repo | 來源 | 分支 | 狀態 |
|---|---|---|---|
| `Meet2Notes`（本倉庫） | fork/clone 自 [`estebanstifli/Meet2Notes`](https://github.com/estebanstifli/Meet2Notes)（MIT，v0.9.0 @ e05c0f0） | `feature/crm-meeting-workflow` | 領先上游 main 16+ commits |
| `crm-integration-verification-tool` | 參考 handy-integration-verification-tool 擴充的 mock CRM 驗證工具（含 Handy bridge 原有功能） | `main` | 獨立 repo |

上傳前已掃描：無密鑰/Token 入檔；`data/`、`models/`、`.venv`、`.env` 均被 .gitignore 排除；示範用的本機 dummy key 只存在作業系統憑證庫（Windows Credential Manager），不在任何追蹤檔案中。

## 3. 環境需求

| 項目 | 需求 | 示範環境 |
|---|---|---|
| OS | Windows 10/11 x64 | Windows 10 x64 |
| GPU | NVIDIA（可選，CUDA 加速轉錄） | RTX 5060 Ti 16GB |
| Python | ≥3.11（3.14 實測全綠） | 3.14.6 |
| FFmpeg | 需要（音訊正規化/匯入探測） | 8.x |
| LLM | 任何 OpenAI 相容端點（`/v1`） | llama.cpp server · Qwen3.8-27B GGUF |

### 環境注意事項

1. **PyAV 必須 <19**：av 19 移除了 faster-whisper 使用的 `metadata_errors` 參數，pin 在 `av 18.1.0`（`pip install "av>=18,<19"`）。
2. **CUDA DLL**：GPU 轉錄需 cuBLAS/cuDNN，可用 pip 安裝 `nvidia-cublas-cu12` + `nvidia-cudnn-cu12`；啟動時要把兩個 bin 目錄加入 PATH（見 §4）。
3. **embeddings**：若 LLM 端點不支援 `/v1/embeddings`，RAG 請維持停用或改用本機 BGE-M3（CPU）。
4. Windows + Python 3.14 的 Proactor SSE 斷線噪音已由 `ProactorNoiseFilter` 過濾。

## 4. 啟動程序（依序）

```bash
# 1) 啟動 OpenAI 相容 LLM 端點（示範：llama.cpp server；模型載入約 1 分鐘）
llama-server -m <你的模型.gguf> -c 32768 -ngl 99 -fa on --jinja \
  --host 127.0.0.1 --port 8080   # 依你的環境調整

# 2) mock CRM（可省略；驗證 CRM 鏈路時需要）
cd <path-to>/crm-integration-verification-tool
npx next start -H 127.0.0.1 -p 8766        # dashboard: http://127.0.0.1:8766/crm

# 3) Meet2Notes（GPU 轉錄需帶 CUDA DLL PATH）
cd <path-to>/Meet2Notes
export PATH="<repo>/.venv/Lib/site-packages/nvidia/cublas/bin:<repo>/.venv/Lib/site-packages/nvidia/cudnn/bin:$PATH"
export M2N_CRM_BASE_URL="http://127.0.0.1:8766"    # 可省略；未設時 /api/crm/* 一律 503
.venv/Scripts/python -m local_meeting_ai           # UI: http://127.0.0.1:8765
```

模型（首次於 UI 設定頁下載，約 509MB+）：Faster Whisper small（`balanced` 設定檔）、sherpa-onnx 講者分離（3D-Speaker + pyannote segmentation 3.0，CPU 執行）。摘要 LLM 不需下載（走遠端/本機端點）。

## 5. 範例驗證流程（以立法院 iVOD 公開影片為例）

以 [Dataly iVOD 171395](https://dataly.openfun.app/collection/item/ivod/171395/ai-transcript)（邱慧洳委員質詢片段，3 分 14 秒，政府公開資料）作為端到端範例。該站同時提供官方 whisperx 逐字稿可比對。

```bash
# 1) 取得影片（ly.govapi.tw 公開 API → HLS，需帶 Referer）
curl -s https://ly.govapi.tw/v1/ivod/171395 -o ivod.json      # 內含 video_url 與官方逐字稿
ffmpeg -user_agent "Mozilla/5.0" -headers "Referer: https://ivod.ly.gov.tw/" \
  -i "<video_url>" -c copy -bsf:a aac_adtstoasc ivod_171395.mp4

# 2) Meet2Notes UI：新會議 → 匯入 ivod_171395.mp4 → 轉錄（+講者分離）
#    預期：66 個段落、1 位講者、繁中內容與官方 whisperx 逐字稿逐句一致

# 3) 影片同步檢視：媒體列自動切為影片播放器，播放時右側逐字稿高亮跟隨（可切換雙欄/單欄）

# 4) 會議助理修错字（權限選「可編輯逐字稿」後直接下指令）：
#    「把『九年一毛』改成『九牛一毛』」
#    「把『獻上最高的制服』改成『獻上最高的祝福』」
#    → 助理套用批次取代，右側逐字稿即時重繪（ASR 同音錯字示範：九年一毛/制服/吝色/宰名）

# 5) AI 摘要：AI 摘要分頁 → 產生摘要（走你的 LLM 端點，輸出 Summary/Highlights/Decisions 格式）

# 6) CRM 驗證（需 mock CRM 啟動 + M2N_CRM_BASE_URL）：
#    curl -X POST http://127.0.0.1:8765/api/crm/meetings/<id>/deliver -H "Content-Type: application/json" \
#      -d '{"include_transcript": true, "meeting_code": "MTG-DEMO"}'
#    → mock dashboard（/crm）顯示收到的摘要與逐字稿（含講者、毫秒時間戳）；重送同內容會標記 duplicate（冪等）
```

種子資料替代方案（不想抓影片時）：`.venv/Scripts/python tools/crm_seed_demo.py` 產生示範會議（資料目錄可用 `M2N_DEMO_DATA_DIR` 覆寫）。

## 6. 已交付功能（對應 commits）

| 功能 | Commits |
|---|---|
| zh-TW 介面層（270+ key、1269+ literal、標點全形、補上游缺口 key） | `4d4d6f7` `1c10504` |
| CRM adapter（CRMClient 埠、httpx client、CrmService、`/api/crm` 路由、冪等鍵、fail-closed） | `911ce49` |
| 影片/音訊匯入 + 播放與逐字稿同步高亮、跟隨捲動、雙欄排版 | `247e3df` `2f47789` |
| 會議助理三段權限 + ACTIONS 協定 + 工具迴圈 | `6915d79` |
| 助理對話 localStorage 持久化 + 清除按鈕 | `71f3b5f` |
| 助理修改後逐字稿自動重繪；API no-store；Proactor 噪音過濾 | `92f0898` `adb7479` |
| 助理「建議式/假宣稱」自動糾正 + 確定性替換後備 | `ae4dd93` `0195ff2` `5243f8d` |
| 會議範圍上下文修復（RAG 停用時仍載入逐字稿） | `2198c76` |
| 開源清理（路徑一般化、種子工具可組態） | 最新 commit |

文件：`docs/CRM_MVP_DISCOVERY.md`（Phase 0 調查＋決議）、`docs/CRM_INTEGRATION.md`（CRM 契約與重現步驟）。

## 7. 目前運行設定（示範環境）

- **摘要 + Live Assistant**：provider=litellm（profile `litellm-custom`）、base_url 指向本機 llama.cpp `/v1`。OpenAI 相容端點即使免鑰也需要 API key 欄位 → keyring 存 dummy 值（僅存在 OS 憑證庫）。
- **助理權限**：`assistant_permission`（read / edit_transcript / full），UI 於會議助理面板下拉切換，全域生效。
- **RAG**：停用。開啟：設定 → RAG → 「啟用歷史 RAG」→ 重建索引（首次下載 BGE-M3 約 2.3GB，CPU 執行不吃 VRAM）。
- **CRM**：`M2N_CRM_BASE_URL` 未設時 `/api/crm/*` 一律 503（fail-closed）；HTTP 僅允許 loopback，外部一律 HTTPS。

## 8. 測試與品質關卡現況

- `pytest`：**342 passed**（含助理工具 15、CRM 37）
- `ruff check` 通過；`mypy --strict src` 121 檔零錯誤
- `scripts/check_ui_i18n.py`：3 locales, 285 keys 通過
- mock CRM 煙霧測試：`node tools/local_bridge/test_crm_backend.mjs` → 14 passed
- 端到端實測：iVOD 171395 匯入→轉錄（與官方逐字稿一致）→講者分離→LLM 摘要→助理批次改錯字→CRM deliver（冪等/錯誤注入）

## 9. 已知限制與下一步建議

1. CRM 送出為同步單次請求，**無持久化重試佇列**（斷線重送、重啟恢復）→ 依 `docs/CRM_MVP_DISCOVERY.md` §6 的 `crm_deliveries` outbox 設計實作。
2. 錄音同意閘門、議題分節、摘要版本管理尚未實作（規格 Phase 1+）。
3. 助理權限為全域；可考慮每會議記憶或「編輯前確認」模式。
4. 確定性替換後備僅支援引號配對（`「X」改成「Y」`）；可再加「AI 一鍵校稿（diff 勾選）」。
5. RAG 若要啟用：BGE-M3 下載，或 LLM 端點啟用 embeddings。

## 10. GitHub 上傳

本倉庫為上游 `estebanstifli/Meet2Notes` 的衍生（fork），同步上游與回饋 PR 皆便：

```bash
git remote add myfork https://github.com/<你的帳號>/Meet2Notes.git
git push -u myfork feature/crm-meeting-workflow
```

mock CRM 驗證工具建議另建獨立 repo（private 或 public 皆可）：

```bash
cd <path-to>/crm-integration-verification-tool
gh repo create crm-integration-verification-tool --source . --push
```
