# 使用者動線與介面編排

參考 LocModel 的工作方式，把 xConsole 改成「這台機器從零到能用」的單一路徑，機群管理退到設定。
本檔是動線的真本：加功能前先找它在哪一步，再動手。

## 一、角色與動線

**一般使用者（拿到一台裝好 xConsole 的機器）**

1. 概覽：看這台跑得動多大的模型 → 照三步走（檢測硬體、裝推理引擎、下載一顆模型）。
2. 模型：在「推薦模型」挑一顆按下載；要別的模型就到「搜尋 Hugging Face」，每個量化都標好跑不跑得動；
   自己已經有檔案就按右上「匯入本機 GGUF」。
3. 執行：選一顆已安裝的模型 → 看分層規劃（放顯示卡／系統記憶體／硬碟各多少）→ 設上下文、思考模式、區網存取
   → 看預估速度與第一次載入時間 → 按「啟動模型」→ 看載入進度 → 下方狀態列出現 API 網址可以複製。
   記憶體放不下時自動往後排到硬碟，不必另外勾選；按鈕會標「會用到硬碟，很慢」，存放區在機械硬碟上會提醒改放 SSD。
4. 對話：直接跟剛啟動的模型聊；按「附件」可以附 PDF、Word、Excel、PowerPoint、純文字與程式碼（主控台抽出文字附進對話），
   看得懂圖的模型也能附圖。模型寫出 HTML 可以直接執行預覽、下載、複製。也可以切到管理者設好的第三方 API。
5. Agent：選一個工作資料夾、交代一件事，Agent 自己規劃、建檔、寫程式、跑指令；每條指令要人按允許。
6. 畫圖、影片：寫一句描述就產出；模型不在就帶去下載。畫圖可選方形、直式、橫式畫幅、一次 1～4 張，
   也可以放一張參考圖在上面重繪；影片可以放第一格畫面，接著往下演。

**管理者（多一台、多台）**

7. 設定：機器（加一台、這台的設定、移除）、推理引擎、第三方 API、HTTPS 與憑證、服務使用報告、記錄、帳號、品牌。
   推理引擎下載可以暫停、斷線後接著下載；不能連外的機器改成手動匯入官方安裝包（送齊後核對 sha256）。

## 二、頂部導覽（禁左側欄）

第一列：品牌｜概覽｜模型｜執行｜對話｜Agent｜畫圖｜影片｜設定｜（右側）目前這台機器 ▾、即時、重新整理、深淺色、登出。

- 「目前這台機器」是全站共用的選擇：概覽、模型、執行、Agent、畫圖、影片都只看這一台。只有一台時不顯示下拉，只顯示名稱。
- 第二列只在有分頁的頁面出現：
  - 模型：推薦模型｜已安裝｜畫圖與影片模型｜搜尋 Hugging Face｜存放區（右側按鈕：匯入本機 GGUF）
  - 執行：執行模型｜角色欄位（進階）｜API 串接
  - 設定：機器｜推理引擎｜第三方 API｜HTTPS 與憑證｜服務使用報告｜記錄｜帳號｜品牌
- 概覽有「全部機器」切換：多台時第二列出現「這台｜全部機器」。

## 三、每一頁的骨架

| 頁 | 上方 | 主體 | 下方 |
|---|---|---|---|
| 概覽 | 三步設定順序軸（真實狀態算出） | 左：「流暢執行 NB 以內的模型」＋三色記憶體條；右：硬體規格 | 為這台推薦（前 3 顆，下載／執行） |
| 模型 | 分頁 | 清單：名稱、標籤、量化選擇、等級＋預估 tok/s＋三色分層條、動作 | — |
| 執行 | 模型下拉＋管理模型 | 左：自動分層規劃（顯示卡／系統記憶體／硬碟）；右：上下文、思考、區網、預估、啟動 | 載入進度卡；執行中狀態列（生成速度、顯存、記憶體、API 網址＋複製） |
| 對話 | 模型選擇（本機／第三方）、新對話、對話紀錄、狀態（tok/s、思考、本機執行或外部） | 訊息 | 輸入框（附件、對話設定），Enter 送出、IME 防誤送 |
| Agent | 模型、工作資料夾 | 對話與步驟 | 右側面板：計畫、改動的檔案、工作資料夾 |
| 畫圖／影片 | 停止引擎、打開作品、下載模型 | 左：表單；右：作品牆 | — |

三色分層條的顏色語意固定：深色＝放顯示卡（最快）、淺色＝放系統記憶體、灰色＝放硬碟（最慢）。

## 四、等級

`smooth` 流暢（≥ 10 tok/s 且全部放快速層）、`ok` 能跑（放得下但慢，或部分專家在系統記憶體）、
`disk` 要用硬碟（放不進記憶體，權重按需從硬碟讀，通常不到 1 tok/s；**AI 395 這類 BIOS 切兩塊的機器一律不允許**）、
`no` 放不下、`unknown` 待更新。

## 五、API 合約（前後端以此為準）

所有路徑都經主控台 `/api/...`，登入後才能用；寫入動作記稽核。

1. **Hugging Face 量化清單附等級**：`POST /api/node/{n}/library/hf` 回應的每個 `choices[i]` 多一個
   `plan: {level, tok_s, place, need_gib, split: {gpu, ram, disk}, guess}`；整體多 `pick`（建議的 stem）。
   `guess: true` 表示拿不到這個 repo 的模型結構，用稠密模型假設估的。
2. **已安裝清單附等級**：模型庫取得的每個 GGUF 多 `plan`（同上格式）。
3. **載入進度**：`GET /api/node/{n}/slot/{s}/progress` → `{state: idle|loading|ready|failed, stage: prepare|read|alloc|warmup|done,
   pct, read_gib, total_gib, elapsed_sec, eta_sec, message}`。換模型的請求進行中，前端每秒輪詢一次。
4. **執行預覽**：`POST /api/preview {html}` → `{url: "/preview/<id>"}`；`GET /preview/<id>` 回原文並帶
   `Content-Security-Policy: sandbox allow-scripts ...`（不透明來源，拿不到主控台的 cookie）、`X-Frame-Options: SAMEORIGIN`。
   只有建立者看得到，30 分鐘過期，單筆上限 2 MB。
5. **用硬碟執行**：換模型時帶 `allow_disk: true`（前端要管理者勾選確認才帶）。`unified_split` 一律回 400。
   執行設定（`apiinfo` 的 `run`）多 `split: {gpu, ram, disk}` 與 `disk_allowed`。
6. **第三方 API**：`GET /api/providers`（不回金鑰，只回 `key_set`）、`POST /api/providers/save`、
   `POST /api/providers/{id}/delete`、`POST /api/providers/{id}/models`、`POST /api/providers/{id}/chat`（串流格式同本機對話）。
   金鑰 AES-256-GCM（`enc:v1:`）落地；中國來源的服務標 `cn_origin`。
7. **Agent**：每台機器預設關閉，要在「這台的設定」打開。工作資料夾只能在 `~/xconsole-agent/` 底下。
   `GET /api/node/{n}/agent/folders`、`POST /api/node/{n}/agent/folders/create`、
   `POST /api/agent/tasks {node, folder, model, prompt}`、`GET /api/agent/tasks`、`GET /api/agent/tasks/{id}`、
   `POST /api/agent/tasks/{id}/approve {allow, always}`、`POST /api/agent/tasks/{id}/stop`。
   任務狀態 `running|waiting_approval|done|failed|stopped`；回 `plan`、`files`、`log`、`pending`、`tree`。
8. **推理引擎下載與手動匯入**：預編譯版下載中按「暫停下載」→ 狀態 `paused`；再按「繼續下載」用 HTTP Range 接著下，
   整個檔核對官方 sha256 才安裝。手動匯入：`POST /api/node/{n}/(llama|sd)/import?name&offset&total`（本文是檔案位元組、每塊最多 64 MB、
   帶 `X-XConsole-Upload: 1`）、`POST .../import/status {name}`、`.../import/cancel {name}`、`.../import/install {name, sha256?}`；
   安裝進度與編譯共用 `.../build/progress`（`mode: "import"`）。
9. **對話附件**：`POST /api/extract {name, b64}` → `{text, chars, truncated, pages}`；上限 15 MB、抽出最多 20 萬字。
10. **畫圖參數**：`POST /api/node/{n}/sd/generate` 多 `count`（1～4，只限畫圖）、`init_image`（data URL，PNG／JPEG／WebP，4 MB 內）、
    `strength`（0.05～1，只限畫圖）。一次多張的產出用 `GET .../sd/jobs/{id}/output/{i}` 取第 i 張，參考圖用 `.../init`。
11. **機器偵測**：Linux 的 Intel 顯示卡（sysfs vendor 0x8086）用 Vulkan 編譯（缺 `libvulkan-dev glslc` 時講清楚要裝什麼）；
    存放區是不是機械硬碟：Linux 讀 `queue/rotational`、macOS 讀 `diskutil` 的 SolidState、Windows 讀 `Get-PhysicalDisk` 的 MediaType。
