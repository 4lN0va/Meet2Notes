# Handover — Meet2Notes CRM 整合專案交接文件

- 日期：2026-10-24
- 語言：台灣繁體中文（zh-TW）
- 狀態：MVP 驗證階段完成（介面預留 + 本機 mock CRM 驗證 + 助理編輯逐字稿），未接真實 CRM

---

## 1. 專案概述

將 Meet2Notes（本機優先的會議轉錄工作站）擴充為「CRM 導向的會議紀錄工作流程」：

- 主持人錄音/匯入影片 → 本機 GPU 轉錄（faster-whisper）→ 講者分離（sherpa-onnx）
- 摘要與會議助理問答走使用者自架的 llama.cpp server（Qwen3.8-27B，本機 8080 埠）
- 逐字稿/摘要可送出到 mock CRM（驗證契約與冪等），真實 CRM 尚未就緒
- 會議助理具三段權限（僅回答 / 編輯逐字稿 / 完全權限），可直接修改逐字稿

## 2. Repos 與來源

| 目錄 | 來源 | 分支 | 狀態 |
|---|---|---|---|
| `D:\CRM_Project\Meet2Notes` | clone 自 `https://github.com/estebanstifli/Meet2Notes.git`（第三方，MIT，v0.9.0 @ e05c0f0） | `feature/crm-meeting-workflow` | 領先 origin/main 15+ commits，工作樹乾淨 |
| `D:\CRM_Project\crm-integration-verification-tool` | 複製自使用者的 `handy-integration-verification-tool`（GitHub: `4lN0va/handy-integration-verification-tool`）並擴充 mock CRM | `main`（全新 git init，1 commit） | 尚無 remote |
| `E:\Default\Desktop\handy-integration-verification-tool` | 使用者原始工具（未動） | — | 有未提交修改，保持原狀 |

上傳前已掃描：無密鑰/Token 入檔、`data/`、`models/`、`.venv`、`.env` 均被 .gitignore 排除；本機 dummy key 只存在 Windows Credential Manager，不在任何追蹤檔案中。

## 3. 環境需求與本機快照

| 項目 | 值 |
|---|---|
| OS | Windows 10 x64 |
| GPU | NVIDIA RTX 5060 Ti 16GB（CUDA 轉錄 + WSL 內 llama.cpp） |
| Python | 3.14.6（venv 於 `Meet2Notes\.venv`；專案宣稱 ≥3.11，3.14 實測全綠） |
| LLM | WSL Ubuntu 內 llama.cpp server：`http://127.0.0.1:8080/v1`，模型 `Qwen3.8-27B-GSQ-RCO-IQ3_XXS.gguf`（launcher：`/home/nova/llama-launcher.sh`） |

### 環境地雷（重要）

1. **PyAV 必須 <19**：av 19 移除了 faster-whisper 使用的 `metadata_errors` 參數。目前 pin 在 `av 18.1.0`。若 `pip install -U` 又升到 19，轉錄引擎會噴 `open() got an unexpected keyword argument`。
2. **CUDA DLL**：GPU 轉錄需要 cuBLAS/cuDNN，已用 pip 裝 `nvidia-cublas-cu12` + `nvidia-cudnn-cu12`（DLL 在 venv 內）。啟動 Meet2Notes **必須把這兩個 bin 目錄加進 PATH**（見下方啟動指令），否則噴 `cublas64_12.dll is not found` 且即時轉錄管線死亡。
3. **llama.cpp server 不支援 embeddings**（未加 `--embeddings`），所以 RAG 採本機 BGE-M3 設定檔但維持停用。
4. Python 3.14 的 Proactor SSE 斷線噪音已由 `logging_config.py` 的 `ProactorNoiseFilter` 過濾。

## 4. 啟動程序（依序）

```bash
# 1) WSL 內啟動 llama.cpp server（模型載入約 1 分鐘）
wsl -d Ubuntu -- /home/nova/llama.cpp/build/bin/llama-server \
  -m /home/nova/models/Qwen3.8-27B-GSQ-RCO-IQ3_XXS.gguf -c 32768 -ngl 99 -fa on \
  --jinja --reasoning off --reasoning-budget 0 --cache-type-k q8_0 --cache-type-v q8_0 \
  --cache-prompt --parallel 1 --host 127.0.0.1 --port 8080
#（或直接跑 /home/nova/llama-launcher.sh）

# 2) mock CRM（可省略，僅驗證 CRM 鏈路時需要）
cd D:\CRM_Project\crm-integration-verification-tool
npx next start -H 127.0.0.1 -p 8766        # dashboard: http://127.0.0.1:8766/crm

# 3) Meet2Notes（務必帶 CUDA DLL PATH）
cd D:\CRM_Project\Meet2Notes
PATH=".../.venv/Lib/site-packages/nvidia/cublas/bin:.../.venv/Lib/site-packages/nvidia/cudnn/bin:$PATH" \
M2N_OPEN_BROWSER=false M2N_CRM_BASE_URL="http://127.0.0.1:8766" \
./.venv/Scripts/python.exe -m local_meeting_ai
# UI: http://127.0.0.1:8765
```

模型（已下載於 `Meet2Notes\models\`，約 509MB+）：Faster Whisper small（balanced 設定檔）、sherpa-onnx 讓者分離（3D-Speaker + pyannote segmentation 3.0，CPU 執行）。摘要 LLM 不需下載（走 8080）。

## 5. 已交付功能（對應 commits）

| 功能 | Commits |
|---|---|
| zh-TW 介面層（270+ key、1269+ literal、標點全形、補上游缺口 key） | `4d4d6f7` `1c10504` |
| CRM adapter（CRMClient 埠、httpx client、CrmService、/api/crm 路由、冪等鍵、fail-closed） | `911ce49` |
| 影片/音訊匯入 + 播放與逐字稿同步高亮、跟隨捲動、雙欄排版 | `247e3df` `2f47789` |
| 會議助理三段權限 + ACTIONS 協定 + 工具迴圈 | `6915d79` |
| 助理對話 localStorage 持久化 + 清除按鈕 | `71f3b5f` |
| 助理修改後逐字稿自動重繪；API no-store；Proactor 噪音過濾 | `92f0898` `adb7479` |
| 助理「建議式/假宣稱」自動糾正 + 確定性替換後備 | `ae4dd93` `0195ff2` `5243f8d` |
| 會議範圍上下文修復（RAG 停用時仍載入逐字稿） | `2198c76` |

文件：`docs/CRM_MVP_DISCOVERY.md`（Phase 0 調查＋決議）、`docs/CRM_INTEGRATION.md`（契約與重現步驟）、`tools/crm_seed_demo.py`（E2E 種子）。

## 6. 目前運行設定

- **摘要 + Live Assistant**：provider=litellm（profile `litellm-custom`）、model `openai//home/nova/models/Qwen3.8-27B-GSQ-RCO-IQ3_XXS.gguf`、base_url `http://127.0.0.1:8080/v1`。本機端點仍需 API key 欄位 → keyring 已存 dummy 值（`local-no-key-required`，僅存在 OS 憑證庫）。
- **助理權限**：`assistant_permission` 設定（read / edit_transcript / full），UI 於會議助理面板下拉切換，全域生效。
- **RAG**：停用（embedding 供應商不支援）。開啟方式：設定 → RAG → 「啟用歷史 RAG」→ 重建索引（首次下載 BGE-M3 約 2.3GB，CPU 執行不吃 VRAM）。
- **CRM**：`M2N_CRM_BASE_URL` 未設時 `/api/crm/*` 一律 503（fail-closed）；HTTP 僅允許 loopback。

## 7. 測試與品質關卡現況

- `pytest`：**342 passed**（含助理工具 15、CRM 37）
- `ruff check`：通過；`mypy --strict src`：121 檔零錯誤
- `scripts/check_ui_i18n.py`：3 locales, 285 keys 通過
- mock CRM 煙霧測試：`node tools/local_bridge/test_crm_backend.mjs` → 14 passed
- 已實測 E2E：iVOD 171395 影片匯入→轉錄（66 段，與官方 whisperx 逐字稿一致）→講者分離→Qwen 摘要→助理批次改錯字（宰名→載明等）→CRM deliver（冪等/錯誤注入）

## 8. 已知限制與下一步建議

1. CRM 送出為同步單次請求，**無持久化重試佇列**（斷線重送、重啟恢復）→ 依 `CRM_MVP_DISCOVERY.md` §6 的 `crm_deliveries` outbox 設計實作。
2. 錄音同意閘門、議題分節、摘要版本管理尚未實作（規格 Phase 1+）。
3. 助理權限為全域；可考慮每會議記憶或「編輯前確認」模式。
4. 確定性替換後備僅支援引號配對（`「X」改成「Y」`）；可再加「AI 一鍵校稿（diff 勾選）」。
5. RAG 若要啟用：BGE-M3 下載或 llama-server 加 `--embeddings`。
6. `git push` 前注意：上游 `estebanstifli/Meet2Notes` 無寫入權，需 fork 或自建 remote（見 §9）。

## 9. GitHub 上傳計畫（建議）

**Meet2Notes → 建議用 GitHub Fork**（理由：完整保留上游歷史與血緣、方便日後同步上游更新、可回饋 PR）：

```bash
# 1) 在 GitHub 上 fork estebanstifli/Meot2Notes（網頁或 gh CLI）
gh repo fork estebanstifli/Meet2Notes --clone=false
# 2) 推分支
cd D:\CRM_Project\Meet2Notes
git remote add myfork https://github.com/<你的帳號>/Meet2Notes.git
git push -u myfork feature/crm-meeting-workflow
```

**驗證工具 → 建議另開新 repo**（歷史與原始 `4lN0va/handy-integration-verification-tool` 無關聯，推成分支會產生孤兒歷史；且原始 repo 有未提交修改）：

```bash
gh repo create crm-integration-verification-tool --private --source . --push
```

上傳後建議：repo 設 private（含內部文件與環境路徑），日後要開源再清理。
