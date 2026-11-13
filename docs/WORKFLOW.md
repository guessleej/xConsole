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
   MoE 模型用硬碟執行時依張量種類分層：注意力、共用權重與 KV 放顯示卡，專家層留在記憶體映射，放不下的專家用到才從 SSD 讀。
   「高階：啟動引數」列出完整的 llama-server 指令，可以加自訂引數（跟著模型存；自己寫分層參數時自動分層讓位）。
   速度先照記憶體頻寬估，對話過之後改顯示實測，並用實測校正這台其他模型的估算。
4. 對話：直接跟剛啟動的模型聊；按「附件」可以附 PDF、Word、Excel、PowerPoint、純文字與程式碼（主控台抽出文字附進對話），
   看得懂圖的模型也能附圖。模型寫出 HTML 可以直接執行預覽、下載、複製。也可以切到管理者設好的第三方 API。
   對話紀錄存在主控台（左邊可搜尋、改名、刪除，換瀏覽器也在）；每則回答可以複製、重新生成、調整回答（更簡潔、更詳細、
   更正式、更口語、換個說法、改用英文）、編輯、刪除，重新生成與調整回答會留下各版本可切換。「對話設定」放系統提示詞與取樣參數。
   說「畫一張…」「生成一段…的影片」就直接產出，附圖說「把…改成…」就改圖（也可以按輸入列的「畫圖」「影片」）；
   「工具」打開時模型可以查這台的狀態與推薦。對話太長時，較早的部分自動整理成摘要再送，畫面上標「已壓縮前 N 則」。
5. Agent：選一個工作資料夾（新建在 ~/xconsole-agent/，或「連結這台的資料夾」選既有的專案），交代一件事，
   Agent 自己規劃、建檔、寫程式、跑指令；每條指令要人按允許。
   對話頁的「操作電腦」：看得懂圖的模型看這台的螢幕、控制滑鼠鍵盤，每一步預設先問（管理者要先在這台的設定打開）。
6. 畫圖、影片：寫一句描述就產出；模型不在就帶去下載。畫圖可選方形、直式、橫式畫幅、一次 1～4 張，
   也可以放一張參考圖在上面重繪；影片可以放第一格畫面，接著往下演。

**管理者（多一台、多台）**

7. 設定：機器（加一台、這台的設定、移除）、推理引擎、第三方 API、HTTPS 與憑證、服務使用報告、記錄、帳號、版本與更新、品牌。
   介面語言（繁體中文、English、日本語、한국어、跟隨系統）在頂列與登入畫面切換，記在這個瀏覽器。
   推理引擎下載可以暫停、斷線後接著下載；不能連外的機器改成手動匯入官方安裝包（送齊後核對 sha256）。

## 二、頂部導覽（禁左側欄）

第一列：品牌｜概覽｜模型｜執行｜對話｜Agent｜畫圖｜影片｜設定｜（右側）目前這台機器 ▾、即時、重新整理、語言、深淺色、登出。

- 「目前這台機器」是全站共用的選擇：概覽、模型、執行、Agent、畫圖、影片都只看這一台。只有一台時頂列不顯示機器切換（名稱看概覽大標題，標題旁可直接改名稱），連不上時頂列顯示「連不上」。
- 第二列只在有分頁的頁面出現：
  - 模型：推薦模型｜已安裝｜畫圖與影片模型｜搜尋 Hugging Face｜存放區（右側按鈕：匯入本機 GGUF）
  - 執行：執行模型｜角色欄位（進階）｜API 串接
  - 設定：機器｜推理引擎｜第三方 API｜HTTPS 與憑證｜服務使用報告｜記錄｜帳號｜版本與更新｜品牌
- 概覽有「全部機器」切換：多台時第二列出現「這台｜全部機器」。

## 三、每一頁的骨架

| 頁 | 上方 | 主體 | 下方 |
|---|---|---|---|
| 概覽 | 三步設定順序軸（真實狀態算出） | 左：「流暢執行 NB 以內的模型」＋三色記憶體條；右：硬體規格 | 為這台推薦（前 3 顆，下載／執行） |
| 模型 | 分頁 | 清單：名稱、標籤、量化選擇、等級＋預估 tok/s＋三色分層條、動作 | — |
| 執行 | 模型下拉＋管理模型 | 左：自動分層規劃（顯示卡／系統記憶體／硬碟）；右：上下文、思考、區網、預估、啟動 | 載入進度卡；執行中狀態列（生成速度、顯存、記憶體、API 網址＋複製） |
| 對話 | 左：新對話、搜尋、對話清單（改名、刪除；窄螢幕收成抽屜）；右上：模型選擇、狀態（tok/s、上下文、已自訂、已壓縮）、對話設定、匯出 | 訊息（每則下方：複製、重新生成、調整回答、編輯、刪除、版本切換） | 輸入框（附件、工具、畫圖、影片），Enter 送出、IME 防誤送 |
| Agent | 模型、工作資料夾 | 對話與步驟 | 右側面板：計畫、改動的檔案、工作資料夾 |
| 畫圖／影片 | 停止引擎、打開作品、下載模型 | 左：表單；右：作品牆 | — |

三色分層條的顏色語意固定：深色＝放顯示卡（最快）、淺色＝放系統記憶體、灰色＝放硬碟（最慢）。

## 四、等級

`smooth` 流暢（≥ 10 tok/s 且全部放快速層）、`ok` 能跑（放得下但慢，或部分專家在系統記憶體）、
`disk` 要用硬碟（放不進記憶體，權重按需從硬碟讀，通常不到 1 tok/s；AI 395 這類 BIOS 切兩塊的機器也可以，系統那塊只當頁快取，跑的時候有看門狗）、
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
12. **對話紀錄**（每個人只看得到自己的；內容與附圖 AES-256-GCM 加密落地；每人最多 500 則，超過刪最舊的）：
    `GET /api/chats?q=` → `{items: [{id, title, target, n_msgs, created, updated, snippet?}], limit}`（`q` 搜標題與內容，命中內容時帶 `snippet`）；
    `GET /api/chats/{id}` → `{id, title, target, created, updated, msgs, settings, summary?}`；
    `POST /api/chats/save {id?, title, target, msgs, settings, summary?}` → `{id, updated, images: {訊息序號: "blob:<id>"}}`
    （訊息裡新附的圖用 data URL 送，存成獨立 blob 後換成 `blob:<id>`；本文上限 16 MB）；
    `GET /api/chats/blob/{id}`（附圖原檔）；`POST /api/chats/{id}/rename {title}`、`POST /api/chats/{id}/delete`、`POST /api/chats/delete-all`。
    訊息欄位：`role`、`content`、`reasoning`、`stat`、`error`、`image`、`docs`、`alts`＋`alt`（回答的各版本）、`gen`（對話裡畫圖、做影片：
    `{node, job, kind, prompt, asked, model, title, count, state, edit}`）。
14. **執行與設定**：`GET/POST /api/node/{n}/llama/args`（`{model, args}`，自訂啟動引數，白名單）；`run_options` 多 `cmd`、`extra_args`、
    `measured_tok_s`、`speed_factor`、`tiered`、`cpu_moe`；機器設定多 `gpu_override`（`null`／`"cpu"`／`{name, vendor, vram_gib}`）、
    `hf_mirror`、`github_proxy`、`desktop_enabled`；`POST /api/settings/hf {token}|{clear:true}`（加密保存，GET `/api/settings` 回 `hf_token_set`）。
15. **Agent 連結資料夾**：`GET /api/node/{n}/agent/browse?path=`（只列子資料夾，`ok`／`why`）、`POST .../agent/folders/link {path}`、
    `POST .../agent/folders/unlink {name}`；`/agent/folders` 的連結項目多 `linked`、`path`、`broken`。
16. **操作電腦**：`GET /api/node/{n}/desktop/status` → `{enabled, available, how, why, os}`；`POST /api/computer/tasks {node, model, goal}`、
    `GET /api/computer/tasks/{id}?after=<截圖編號>`、`POST .../decide {allow, always, pending}`、`POST .../stop`；
    `POST /api/node/{n}/desktop/open {path}`（只開模型存放區與 Agent 工作資料夾底下）。
17. **線上更新**：`GET /api/update` → `{current, job, backups, agent_sha256, nodes: [{id, name, online, sha256, current, old_agent}]}`；
    `POST /api/update/check`（問 GitHub 最新正式版）、`POST /api/update/apply`（網址與 sha256 由主控台自己問 GitHub，不收畫面送的）、
    `POST /api/update/upload?sha256=`（本文是 .tar.gz，`application/octet-stream`＋`X-XConsole-Upload: 1`，64 MB 內）、
    `POST /api/update/rollback`、`POST /api/update/agent/{node}`（把隨附的代理推過去）。代理端 `GET /self`、`POST /self/update {code, sha256}`。
    正式版由 `.github/workflows/release.yml` 在打 `v*` 標籤時建立（`VERSION` 要跟標籤一致）。
18. **介面語言**：`console/static/i18n.js` 依選的語言把畫面文字換成 `i18n-{en,ja,ko}.json`；字典由 `scripts/i18n_extract.py` 抽字串、
    `scripts/i18n_translations.json` 是翻譯真本、`scripts/i18n_build.py` 產生字典。模型回答、程式碼、路徑不翻。
13. **取樣參數**：本機對話（`/api/node/{n}/chat/{s}`）與助理（`/api/assistant` 的 `sampling`）收 `temperature` 0～2、`top_p` 0～1、
    `top_k` 0～1000 整數、`min_p` 0～1、`repeat_penalty` 0.5～2、`presence_penalty` -2～2、`max_tokens` 1～131072、`seed` -1～2^31-1（-1＝隨機，不送）；
    第三方 API 只轉送 `temperature`、`top_p`、`presence_penalty`、`max_tokens`、`seed`。助理另收 `system`（接在主控台的助理指示後面）。
