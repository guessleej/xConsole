# xConsole 模型主控台

云碩科技**統一記憶體 AI 機**的模型主控台。一句話：**哪台機器上開了哪個模型、還剩多少記憶體、能不能再開，不用再自己記。**

## 定位

專門給這三個系列：

| 系列 | 代表機 | 記憶體架構 |
|---|---|---|
| **AMD Ryzen AI 395 系列** | 兩台 AMD AI 395 Strix Halo（HP Z2 Mini G1a） | 96 GiB 給 GPU，系統只剩 31 GiB（切兩塊） |
| **MIC-743／Jetson 系列** | AGX Thor（`.20`）、AGX Orin、Orin NX | 統一記憶體單一池 |
| **DGX Spark 系列** | GB10（`.24`／`.25`／`.32`） | 統一記憶體單一池 |

這三個系列的共同點是**統一記憶體**——也正是傳統「看顯存」的儀表板會盯錯數字的地方。
`nvidia-smi` 在 GB10 與 Thor 上回的 `memory.total` 是 `[N/A]`；Strix Halo 則是顯存看起來很鬆、
真正會把整台拖死的是那 31 GiB 的系統記憶體。**xConsole 就是為了算對這件事而存在的。**

**一台也管，十台也管。** 單機模式把代理與主控台裝在同一台，客戶拿到機器開瀏覽器就能用，
不需要另外準備管理機；之後要串成多台，代理與欄位登錄完全不用動。
機群裡的傳統多卡伺服器（MI50 農場、P100、Ada）也能納管，走 `discrete` 架構，但不是本產品的主場。

### 版本

| 版本 | 名稱 | 說明 |
|---|---|---|
| 個人版 | **xConsole Personal** | 桌面安裝檔、管一台。就是本 repo 的開源核心，不另開 repo |
| 企業版 | **xConsole Enterprise** | 閉源模組（私有 repo），內含教育包（電腦教室）與企業包（單一登入、核准模型清單、分批推送等）；主控台偵測到並驗過簽章才載入 |

學校電腦教室與企業多工作站的規劃見 [`docs/WORKSTATION_FLEET.md`](docs/WORKSTATION_FLEET.md)。

- 線上位置：`https://192.168.11.27:9400`（自簽憑證，瀏覽器會先警告一次）
- 登入：帳號 `admin`，密碼依交接另行提供（不寫進版控）；登入後可在「設定 → 帳號與安全」自行變更
- 目前納管：兩台 AMD AI 395 Strix Halo（HP Z2 Mini G1a）、一台 Jetson AGX Thor（與 MIC-743 同一顆 SoC）

---

## 一、它解決什麼

這套東西的價值不在監控——監控你已經有了（amd-cluster-monitor、gpufleet）。價值在三件監控做不到的事：

1. **准入控制**：載模型之前先算「載下去之後最壞會剩多少」，不夠就擋住。
   這擋的是兩台 AMD AI 395 Strix Halo 被 OOM 拖死整台、需要走到機器前面物理重開的那種事故。
2. **情境切換**：同一顆模型、同一台機器，寫程式後端要長脈絡、抓料要吞吐。
   一鍵切換整組參數，參數留在平台上而不是留在腦袋裡。
3. **角色欄位**：按「主腦／副腦／聽覺／說話／記憶／判斷／雙手／眼睛」排版，
   空的欄位自己會喊話，一眼看出這台還缺什麼。

## 二、三種記憶體架構分開算（最關鍵的設計）

機群裡有三種完全不同的記憶體模型，**套同一條公式會盯錯數字**：

| 架構 | 機器 | 怎麼回事 | 底線預設 |
|---|---|---|---|
| `unified_split` | 兩台 AMD AI 395 Strix Halo | BIOS 切 96 GiB 給 GPU，**系統只剩 31 GiB**。會把整台拖死的是系統這塊，不是顯存 | 系統 RAM 15 GiB |
| `unified_single` | DGX Spark、Jetson | 統一記憶體一個池，GPU 與系統搶同一塊，不能分開算成「各自都夠」 | 12 GiB |
| `discrete` | MI50 農場、P100、Ada、5090 | 每張卡自己的顯存，另外一塊系統 RAM | 顯存 1 GiB／RAM 8 GiB |

底線可在各機 `~/.xconsole/registry.json` 用 `ram_floor_gib`／`vram_floor_gib` 覆寫。

### 多卡（discrete、兩張以上）

卡數不用設定，每次量測現場數：NVIDIA 是 `nvidia-smi` 每張卡一列（卡號＝它的 index），AMD 是 `/sys/class/drm/cardN`（卡號＝N）。
單卡的機器下面這些都不出現，行為跟以前一樣。

| 項目 | 做法 |
|---|---|
| 畫面 | 機器頁的顯存先畫全部卡的合計，再每張卡一條，標出綁在那張卡上的欄位；欄位卡上有「卡 N」標籤 |
| 綁卡 | 換模型視窗多一組「用哪幾張卡」，沒勾＝全部卡（llama.cpp 照卡的大小按層拆）。綁定存在欄位的 `gpu_ids`，換模型沒帶 `gpus` 就維持原本的綁定 |
| 建議 | 照「可用量由大到小」挑最少張數：單張放得下就綁最空的那張（少了跨卡傳輸，其他卡能跑第二個模型），放不下才多張一起扛 |
| 准入 | 逐張卡算：模型照卡的大小攤到它用的每張卡，任何一張跌破底線就擋，理由點名是哪張卡。改綁卡時，舊模型釋放的量還給它原本用的卡 |
| 啟動 | NVIDIA 設 `CUDA_DEVICE_ORDER=PCI_BUS_ID` 加 `CUDA_VISIBLE_DEVICES`（不設排序的話 CUDA 把最快的卡排第一，跟 `nvidia-smi` 對不上）；AMD 經 KFD 拓撲把 drm 卡號換成 HIP 序號（.70 實測 card1～8 ＝ HIP 0～7，card0 是主機板的顯示晶片），對不上就拒絕啟動 |

目前只有 llama.cpp 欄位能在網頁上綁卡；登錄檔裡手動寫了 `gpu_ids` 的其他服務，`gpu_ids` 只用來算帳，不會替它設環境變數。

### KV 的大小因模型而異，差好幾個數量級

`Qwen3.8-Flash-Next` 是 GDN／線性混合注意力，int8 下只要 **1,056 B/token**，
128K 全開也才 **0.13 GiB**。實測佐證：`.27` 把 `--max-context` 由 32,768 改成 131,072，
載入後剩餘顯存由 33,026／33,416 MiB 變成 33,094 MiB，**上下文變四倍而顯存沒變**。

傳統全注意力模型可以到幾十萬 B/token，同樣 128K 要吃掉幾十 GiB。
所以欄位可以用 `kv_bytes_per_cell` 指定自己的值，沒指定才用引擎預設表。
**不要假設所有模型的 KV 成本一樣。**

## 三、架構

```
瀏覽器 ──HTTPS──> xConsole 主控台（.27:9400）──HTTPS＋Bearer（驗證對方憑證）──> 各機節點代理（:9401）
                                                              └─> 同機 node_agent（:9300）取硬體指標
```

- **節點代理** `agent/xconsole_agent.py`：每台一支，只用 Python 標準函式庫。
  硬體指標優先取用同機已在跑的 `amd-node-agent`（不重複造探針），它沒有才自己量
  （nvidia-smi → amdgpu sysfs → /proc）。代理自己負責：欄位與模型狀態、准入試算、
  啟停、情境套用、埠口掃描、記錄。
- **主控台** `console/xconsole_server.py`：彙整各機、對人提供畫面與登入。
- **前端** `console/static/`：原生 HTML/CSS/JS，不需要建置工具。
  照 `console-ui-architecture` 的頂部雙列殼層與 `xcloud-web-style` 的品牌視覺。

### 為什麼前端沒用 React／Vite

`console-ui-architecture` 的 React＋Vite＋Tailwind 骨架是為「多單元平台」設計的
（xStudio 那種規模）。這裡只有四頁、一份密集儀表板，加一套建置鏈會讓它變成
「要先 npm build 才能改一行字」。所以殼層、token、互動鐵則、動態層次四件套全部照規範做，
但用原生三件組實作，單檔 scp 就能更新。**如果之後長到多單元，再換 React 骨架。**

## 四、怎麼用

### 介面與動線（2026-11 照 LocModel 的使用者流程改版）

頂部導覽照使用者的先後排，真本是 [`docs/WORKFLOW.md`](docs/WORKFLOW.md)：

**概覽｜模型｜執行｜對話｜Agent｜畫圖｜影片｜設定**

- **目前這台機器**：頂列右側切換，概覽、模型、執行、Agent、畫圖、影片都只看這一台；只有一台時只顯示名稱。多台時「概覽」第二列可切到「全部機器」。
- **概覽**：這台的三步（檢測硬體 → 安裝推理引擎 → 下載並啟動一個模型）、執行中狀態列（API 網址可複製、去對話、停止）、「流暢執行 NB 以內」與前三名推薦。
- **模型**：推薦模型｜已安裝｜畫圖與影片模型｜搜尋 Hugging Face｜存放區，右上「匯入本機 GGUF」。每顆都標等級（流暢、勉強、要用硬碟、跑不動；頻寬沒填時標「放得下」）、預估 tok/s 與三色分層條（深色＝放顯示卡、淺色＝放系統記憶體、灰色＝放硬碟）。
- **執行**：選一顆已安裝的模型 → 自動分層規劃 → 上下文（自動／8K～128K）、思考模式、區網存取 → 預估速度與第一次載入時間 → 「啟動模型」→ 載入進度（準備、讀取模型、分配快取、預熱、完成）→ 執行中狀態列。**使用者不必先懂欄位**：這台還沒有語言模型欄位時，按啟動會自動建一格「主模型」。第二列另有「角色欄位」（一台跑好幾個模型時用）與「API 串接」。
- **對話**：本機模型、第三方 API、主控台助理三種來源；程式碼區塊可複製、下載，HTML 可「執行預覽」（沙盒網址，拿不到登入）。對話紀錄只存在這個瀏覽器分頁。
- **Agent**：選工作資料夾、交代一件事；每條指令要人按允許。每台預設關閉，在「設定 › 機器 › 這台的設定」打開。
- **畫圖、影片**：各一頁，表單只列那一類的模型；模型或引擎沒準備好時帶到「模型 › 畫圖與影片模型」。
- **設定**：機器｜推理引擎｜第三方 API｜HTTPS 與憑證｜使用報告｜記錄｜帳號與安全｜品牌與外觀。

### 桌面版：點兩下就裝好（一般使用者用這個）

照 LocModel 的形式做成桌面應用程式：不用系統管理員、不用先裝 Python、不用開命令列，也不用處理憑證警告。

| 系統 | 安裝檔 | 裝在哪 |
|---|---|---|
| Windows 10／11 | `xConsole_<版本>_x64-setup.exe` | 使用者自己的 `%LOCALAPPDATA%\xConsole`；開始功能表與桌面捷徑，「設定 › 應用程式」可以解除安裝 |
| macOS（Apple Silicon） | `xConsole_<版本>_aarch64.dmg` | 拖進「應用程式」 |
| Linux | `.AppImage`（點兩下就跑）或 `.deb` | — |

- 安裝檔內含獨立的 Python 3.12 與主控台、節點代理；打開應用程式就帶起它們，關掉視窗會縮到系統匣（模型照常服務），系統匣選「結束 xConsole」才停。可以在系統匣勾「開機自動啟動」。
- 這台自己的視窗不用登入。別台電腦要連進來，到「設定 › 帳號與安全」設一組密碼、打開「區網存取」，走 HTTPS＋密碼。
- 給一般人用的預設（2026-11 在 HP 筆電照一般使用者走一遍後定的）：第一次打開直接套用偵測到的記憶體架構；新模型只給這台自己用（不會跳防火牆詢問）、不先思考；設定頁只留帳號、推理引擎、更新、第三方 API、記錄。
- 記憶體：Windows／macOS 不夠時作業系統會改用硬碟分頁（變慢、不會當機），所以底線只是「建議留」，剩不到 1.5 GiB 才擋；沒指定上下文時放不下會自動縮到 16K、8K 再試。Linux 統一記憶體機（DGX Spark、Jetson、AI 395）照舊是硬底線，因為吃爆會整台卡死。
- 模型頁可以直接貼 Hugging Face 網址或「作者/模型」；原始權重（沒有 GGUF）的 repo 會列出社群轉好的 GGUF 版本讓人挑。
- 資料（設定、模型、引擎、對話）放在 `%LOCALAPPDATA%\tw.xcloudinfo.xconsole`（macOS：`~/Library/Application Support/tw.xcloudinfo.xconsole`），解除安裝不會刪。
- 更新：「設定 › 版本與更新」下載新版安裝檔、核對 GitHub 公布的 sha256 再打開它。
- 安裝檔由 CI（`.github/workflows/desktop.yml`）在三個平台各自建；Windows 那台會實際靜默安裝、啟動、確認主控台與代理起得來、結束後不留行程、再解除安裝。打 `v` 標籤時附到同一個正式版。原始碼在 `desktop/`（Tauri 2），內含的 Python 由 `desktop/scripts/prepare_payload.py` 準備（核對 sha256）。
- 安裝檔目前沒有程式碼簽章：Windows 第一次開會出現 SmartScreen「已保護您的電腦」，按「其他資訊 › 仍要執行」；macOS 要在「系統設定 › 隱私權與安全性」按「仍要打開」。要拿掉這一步得買程式碼簽章憑證（Windows）與 Apple 開發者帳號（macOS）。

### 伺服器版安裝：Ubuntu、macOS、Windows 都可以（多台機群、開機就跑、沒人登入也服務）

客戶拿到的安裝包（這個 repo 的壓縮檔）解開後，在那台機器上執行一行，主控台＋節點代理就裝成開機自啟：

| 系統 | 指令 | 裝成什麼 |
|---|---|---|
| Ubuntu（含 DGX Spark、Jetson） | `bash install/install.sh` | systemd 使用者服務，開 linger（沒人登入也會跑） |
| macOS（Apple Silicon） | `bash install/install.sh` | launchd 的 LaunchAgent（登入後自動啟動） |
| Windows 10／11／Server | 「Windows PowerShell（系統管理員）」執行 `powershell -ExecutionPolicy Bypass -File install\install.ps1` | 工作排程器，開機自啟（SYSTEM 帳號，資料放 `C:\ProgramData\xConsole`、權限只給 SYSTEM 與系統管理員），並開防火牆 9400／9401／9402（工作站連回）／8090～8289 |

需要 Python 3.9 以上（macOS 沒有的話先 `xcode-select --install`；Windows 沒有會問要不要用 winget 裝）。
安裝時會問管理者帳號與密碼；自動化安裝用 `--password-file <檔>`（Windows `-PasswordFile`），檔案第一行帳號、第二行密碼。
移除：`--uninstall`（Windows `-Uninstall`），資料保留。

要把別台機器加進來，在網頁「加一台機器」產生指令：Linux 與 macOS 是同一行 `curl … | bash`，Windows 是一行
`powershell -EncodedCommand …`（畫面上可以展開原文）。兩種都釘住主控台的憑證，不是關掉驗證。

**三個系統哪裡不一樣：**

| | Linux | macOS | Windows |
|---|---|---|---|
| 模型服務誰看管 | systemd（`xc-<欄位>-llama.service`） | 代理自己（`Supervisor`） | 代理自己（`Supervisor`） |
| 推理引擎 | 在這台編譯（CUDA／ROCm 跟著驅動；Intel 顯示卡、或 AMD 沒有 ROCm 時用 Vulkan） | 下載官方預編譯版（Metal） | 下載官方預編譯版（CUDA 12.4／13.4、Vulkan 或 CPU，依顯示卡與驅動自動選） |
| 不能連外時 | 三個系統都一樣：在別台下載官方安裝包，從「安裝推理引擎」視窗手動匯入（核對 sha256） | | |
| 分辨 SSD 與機械硬碟 | `/sys/block/*/queue/rotational` | `diskutil info` 的 SolidState | `Get-PhysicalDisk` 的 MediaType |
| Strata 引擎 | 有（AI 395） | 沒有 | 沒有 |
| 記憶體架構 | 三種都有 | 統一記憶體（GPU 上限照 Metal，預設 2/3～3/4） | AI 395＝切兩塊；獨立顯示卡＝獨立顯存 |
| 登記既有服務成欄位 | 可以 | 不行（只有 xConsole 自己建的） | 不行 |

代理自己看管的行為照 systemd 那份服務檔：異常結束 10 秒後重開、一分鐘內掛五次就停在「失敗」、「啟用」＝代理啟動時帶起來；
狀態存在 `~/.xconsole/units.json`，代理重開時照 PID 與程式名稱認回還在跑的模型，不會再帶起第二份去搶同一個埠。
預編譯版下載後核對 GitHub 公布的 sha256，對不上就不裝；解壓縮時任何檔案會落到資料夾外面就整個拒絕。

### 單機模式（內部用：從這台 Mac 經 SSH 部署）

```bash
bash deploy_standalone.sh 20      # 代理＋主控台都裝在 .20，它只管自己
```

### 加一台機器、設定機器與欄位（全部在網頁上）

管理者不用 SSH、不用改 JSON。設定 → 機器按「加一台機器」：

| 步驟 | 在哪裡 | 做什麼 |
|---|---|---|
| 1. 產生安裝指令 | 設定 → 機器 →「加一台機器」 | 填名稱、群組、主控台位址（那台連得到的 IP:埠），按「產生安裝指令」。加入碼 30 分鐘、只能用一次 |
| 2. 在那台貼上執行 | 那台機器的終端機（一般帳號，不要 root） | `curl --pinnedpubkey … \| bash`：釘住主控台憑證的公鑰下載代理、建使用者服務並開機自啟、開 linger、把代理的權杖與憑證送回主控台 |
| 3. 自動出現 | 同一個視窗 | 主控台用那張憑證與權杖實際連一次，成功就由主控台連這台（伺服器）。連不進去（工作站常見：防火牆擋連入、IP 會換）就改由那台主動連回主控台的通道埠 9402：安裝腳本寫下通道位址與通道憑證，代理每 25 秒以長輪詢拿指令，機器清單的位址欄顯示「由這台連回」。這種機器不必開 9401 埠。通道用自己的憑證，換管理網頁的憑證不會讓工作站斷線；換通道憑證、對外網址（帶回家的筆電用）與代理伺服器都在「設定 → HTTPS 與憑證 → 工作站連回通道」 |
| 4. 這台的設定 | 設定 › 機器，或執行 › 角色欄位右上「這台的設定」 | 記憶體架構（附偵測建議）、兩種底線、存放區（列出各顆碟、標出機械硬碟）、llama-server、Agent 開關、備註；名稱、群組、位址也在這裡改 |
| 5. 新增欄位（選用） | 執行 › 角色欄位右上 | 一台跑好幾個模型時才需要：空欄位，或登記這台已經在跑的 systemd 使用者服務。只跑一顆的話，在執行頁按「啟動模型」會自動建一格 |
| 6. 放入模型 | 模型 → 執行 | 下載或匯入，再到執行頁選好按「啟動模型」 |

新機器的機器頁上方會有這幾步的順序軸，全部完成就收起來。移除機器只是不再納管，不動那台機器上的東西。
代理重裝或換 IP 時，產碼時選「這是哪一台重新安裝」才會更新原本那筆；沒選一律新增，不會自動覆寫（防搶機器）。

**情境**也在網頁上管理（機器頁「管理情境」）：只開放上下文長度、並行數、自動縮輸出上限三項，其他引擎參數一律不開放（可能帶檔案路徑）。

**編譯 llama.cpp**（這台的設定／順序軸）：檢查 git、cmake、編譯器，缺的列出 `sudo apt install …` 請 IT 裝；自動選 CUDA／ROCm／CPU（CUDA 不認得新卡時退到它支援的最高架構）；下載官方最新正式版，編譯前核對 commit；依可用系統記憶體決定並行數，不夠就不編；背景編譯、看得到進度、可取消。.103 實測 b11870 CUDA 版約 6 分鐘。

**安全邊界**：
- **欄位只收白名單**：只收角色、等級、能力、埠、預估記憶體、關鍵、備註；記錄檔與設定檔的路徑不能從網頁改（改得到就能經「記錄」讀任意檔案）。
- **存放區**：不准系統目錄、隱藏資料夾、家目錄本身。
- **llama-server**：檔名必須是 `llama-server`，而且 `--version` 要跑得起來。

`deploy_agent.sh`、`deploy_console.sh`、`registries/*.json` 仍保留給首次安裝與災難救援。

### 換模型的方法（任何模型都照這四步）

xConsole 管兩種引擎：**Strata 只跑 Qwen3.8-Flash-Next 這一家**（原版、Coder、Swift、Unsloth 各量化），
**其他模型（Gemma、gpt-oss、Llama…）一律走 llama.cpp**。兩種在畫面上是同一個「換模型」，使用者不用知道差別。

| 步驟 | 做法 |
|---|---|
| 1. 放哪裡 | GGUF 一律放存放區 `~/models/gguf/<資料夾>/<模型>.gguf`（「這台的設定」可以改到別顆碟）。一顆模型一個資料夾；分片放同一個資料夾；看圖模型把 `mmproj*.gguf` 放同資料夾。同一個資料夾放多個量化，各自算一顆 |
| 2. 怎麼放進來 | 頂部導覽的「模型」（頂列右側選好機器）→「搜尋 Hugging Face」→ 用關鍵字找或貼名稱（例 `unsloth/gemma-4-E4B-it-GGUF`）→ 選量化 → 下載。代理在背景下載、可取消、中斷了再按一次會接續；下載前先檢查磁碟。已經有檔案就按右上「匯入本機 GGUF」，或照「手動放入」的目錄結構與指令，自己把檔案複製進存放區，重新整理就會掃到 |
| 3. 怎麼選 | 模型清單上按「執行」帶到執行頁、選好那顆按「啟動模型」；一台有好幾格時，在執行頁上方選放哪一格，或到「角色欄位」用欄位卡的「換模型」。清單同時列 Strata 與 GGUF；GGUF 的記憶體需求直接讀檔頭算（權重＋上下文的 KV，KV 照每層 KV 頭數算，滑動視窗層只算視窗內），放不下的標出來，套用前還會再做一次准入試算 |
| 4. 怎麼切 | 同一個欄位、同一個埠。跨引擎時舊引擎停掉並取消開機自啟、新引擎接手同一個埠並設成開機自啟，IDE 的網址不用改；金鑰、輸出上限、自己取的模型名稱跟著欄位走。用新設定實際打 `/v1/models`，打不通就整組換回原本的引擎與模型 |

換完會自動帶到「API 串接」頁，提示 IDE 要改成哪個模型名稱，按「實測一次」確認。

模型頁會把中國來源的模型（Qwen、DeepSeek、GLM、Kimi…）標成「政府案不可用」：內部測試可以用，交付政府案的機器不能裝。

**llama.cpp 欄位怎麼跑起來：** 代理為欄位寫一份 `~/.xconsole/engines/<欄位>-llama.json`（0600）與 systemd 服務
`xc-<欄位>-llama.service`，服務的進入點是 `xconsole_agent.py --launch-llama <設定檔>`，讀設定後 exec 成 llama-server。
llama-server 自動挑家目錄下版本最新、有 GPU 後端的那支（`machine.llama_server` 可指定）。固定帶 `--jinja`、`--no-mmap`
（統一記憶體機開 mmap 會多一份頁快取，系統記憶體那塊先爆）；金鑰用 `--api-key-file` 交給它，不放在指令列。
機器需要額外參數或環境變數就寫在登錄檔 `machine.llama_args`、`machine.llama_env`。

**Strata 家族的模型**照舊由 Strata 的 `setup.sh --setup --family <名稱> --model <量化>` 安裝，會產生
`<model_root>/packs/<名稱>/`（含 tokenizer）、`<model_root>/models/<名稱>/*.gguf` 與 `strata-<名稱>.json`，三樣齊了才列入清單。
換 Strata 模型換的是**一整份設定**，不是 `--pack`／`--native` 兩個路徑：以 `strata-<名稱>.json` 為底，
只覆寫跟機器有關的 `exe`、`lib_dirs`、`port`、`parallel`、`host`，以及跟著欄位走的 `api_key`、`max_tokens`。

安全上，換模型與刪除只接受掃描清單內的模型，下載只接受 Hugging Face 檔案清單內的檔名；Hugging Face 權杖只在下載期間放在記憶體，不寫任何紀錄。

### 推薦模型（依這台的記憶體與頻寬，先算好再下載）

參考 LocModel 的做法：先讀懂這台，再告訴管理者「哪些模型能跑、放在哪、多快、該下載哪個量化」，點一下下載，再到執行頁啟動。

| 在哪 | 看到什麼 |
|---|---|
| 概覽 | 從零到跑起來的三步：檢測硬體（含確認記憶體架構）→ 安裝推理引擎（一鍵編譯 llama.cpp，進度直接顯示在這一步）→ 下載並啟動一個模型；全部完成就收起來。下面是這台的硬體卡與前三名推薦 |
| 模型 › 推薦模型 | 硬體卡（「流暢執行 XB 以內的模型」＋分層長條＋晶片、處理器、記憶體、頻寬、存放區）與完整推薦清單；可依用途篩選，預設藏起放不下的。每列可換量化，等級、放在哪、速度跟著變 |
| 模型 › 推薦模型 › 管理推薦清單 | 增改刪、調順序、從 Hugging Face 重新讀取。中國來源的模型照樣可以加，畫面上標「中國來源・政府案不可用」 |
| 這台的設定 | 記憶體頻寬、系統記憶體頻寬（推薦估速度用；空白＝規格書標稱值） |

**數字怎麼來（全部在 `console/recommend.py`，畫面不自己算）：**
- 推薦清單只存人寫的欄位（名稱、用途、說明、標籤、來源）；各量化大小、架構、KV、MoE 比例、授權與發布月份，
  都由代理用 HTTP Range 只抓 Hugging Face 的檔頭讀出來（`hf_profile`，一顆約 5 秒，不下載權重）。
  隨附的預設清單由 `scripts/build_catalog.py` 走同一段程式產生，目前 14 顆，全部非中國來源。
- **每個 token 讀多少權重**：讀 GGUF 的張量清單，相鄰 offset 相減得每個張量的大小（不必認得每種量化格式）；
  MoE 的專家張量只算被選中的比例，`token_embd`（有獨立輸出層時）與 Gemma E 系列的 `per_layer_token_embd` 是查表、不算。
  實測：gpt-oss-20b 約 20%、Gemma 4 26B-A4B 約 21%、Gemma 4 E4B 約 61%、稠密 31B 100%。
- **速度** ≈ 記憶體頻寬 × 60% ÷ 每個 token 讀的位元組，再加每層 0.1 ms 固定開銷（稀疏 MoE 光算頻寬會高估）。
  多張卡依層切、輪流算，照單張卡的頻寬算。畫面上寫明是估算、以 API 串接的實測為準。頻寬認不出就不給速度，不硬掰。
- **等級**：放得下且 ≥ 10 tok/s 是「流暢」；放得下但太慢、或要把專家搬到系統記憶體是「能跑」；其餘「放不下」。
  用整台容量（扣底線）算，不是現在剩多少；真正換上時准入照當下剩餘量再擋一次。
- **挑量化**：4～6 位元裡流暢的挑最高、同位元偏好 K 系列；repo 沒有 4～6 位元才挑 8 位元；都不流暢就退到 4 位元最小的。
- **自動分層（只有獨立顯示卡）**：MoE 放不下顯示卡時，從第一層起把專家搬到系統記憶體（llama.cpp `--n-cpu-moe`），
  搬到放得下為止。推薦畫面與實際換模型用同一條公式（`test_recommend.py` 對照兩邊算出的層數）。
  統一記憶體機只有一個池，搬了沒用，不做。

### 畫圖與影片（stable-diffusion.cpp）

參考 LocModel：下載與管理放在**模型 › 畫圖與影片模型**，產生放在頂層的**畫圖**與**影片**兩頁（表單只列那一類的模型）。
分頁上方有自己的三步順序軸：

| 步驟 | 在哪 | 做什麼 |
|---|---|---|
| 1. 安裝畫圖引擎 | 畫圖與影片 › 第一步「一鍵安裝」 | 跟安裝 llama.cpp 同一套：Linux 依顯示卡選 CUDA／ROCm，下載 stable-diffusion.cpp 最新正式版（`master-<編號>-<commit>`）連同子模組，在這台編出 `sd-cli`；macOS（Apple Silicon）與 Windows x64 下載官方預編譯版（CPU／CUDA 12／Vulkan），核對官方公布的 sha256。進度與記錄看得到、可取消。兩種引擎共用一把鎖，同時只裝一個 |
| 2. 下載一顆模型 | 畫圖與影片 › 推薦模型 | 每顆＝主體＋文字編碼器＋VAE（影片再加音訊 VAE、嵌入連接器），一鍵全部下載；別顆已經有的共用檔（例 FLUX 系列同一份 t5xxl）不重複下載。依這台記憶體建議量化，標「流暢／能跑／放不下」與放在哪 |
| 3. 產生 | 畫圖與影片 › 產生圖片與影片 | 左邊填提示詞、畫幅（方形、直式 3:4／9:16、橫式 4:3／16:9）、張數（畫圖一次 1～4 張，sd-cli 的 `-b`）、步數、種子；可以放一張參考圖（畫圖＝在上面重繪，`-i`＋`--strength`；影片＝當第一格畫面接著往下演，LTX 的 I2V），只收 PNG／JPEG／WebP、4 MB 內。右邊看進度與結果，多張的點縮圖切換；最近 30 件在區塊內捲動，可下載、帶入同樣設定（含參考圖）、刪除 |

| 在哪 | 看到什麼 |
|---|---|
| 推薦模型 › 管理清單 | 增改刪、調順序、從 Hugging Face 重新讀取。每顆寫明每個元件是哪個 repo 的哪個檔，或寫「檔名開頭」讓機器挑量化（其他元件跟著主體的位元數） |
| 已下載 | 每顆的大小、能不能用（缺哪個檔）、實測，刪除時只刪這顆獨用的檔 |
| 從 Hugging Face 下載 › 手動組一顆（內部測試） | 不在清單上的（含中國來源，例 Qwen-Image、Wan）只能在這裡組，一定要勾「只做內部測試」，畫面上一直標著 |
| 這台的設定 | 指定用哪支 `sd-cli`（預設挑版本最新的）；畫圖模型放在存放區同一層的 `sd/` 資料夾 |

**數字怎麼來（`console/recommend.py` 與代理 `sd_admit`，畫面不自己算）：**
- **要多少記憶體** ＝ 所有權重 ＋ 1 GiB ＋ 每百萬像素 2 GiB（影片每一格 0.3 GiB）。產過一次後改用實測的最高用量（取比估算大的那個）。
- **等級**：整台容量放得下全部是「流暢」；獨立顯示卡放不下全部、但最大一個檔加計算緩衝放得下，權重放系統記憶體輪流搬進卡算，是「能跑」；
  其餘「放不下」。`sd-cli` 只用一張卡，獨立顯示卡機取最大那張。
- **挑量化**：流暢的裡面取 4～8 位元最高（擴散模型低於 4 位元畫質掉很多，Q8 與原始幾乎一樣）；都不流暢取能跑的最小一個。
- **速度不估算**：擴散模型吃算力不吃頻寬，用頻寬推會錯。產過一次之後顯示這台的實測（尺寸、步數、秒數、最高用量）。
- **准入（每次產之前）**：照三種記憶體架構、用「當下」剩多少試算，不夠就擋；放行時把可用額度交給 `sd-cli --max-vram`，它自己分配也不會越線。
  產的時候**看門狗**每秒看一次系統記憶體，跌破底線就中止並講原因（統一記憶體吃爆會把整台拖死）。
- **中國來源**：清單不收，而且**文字編碼器與 VAE 也算**——FLUX.2 klein 用 Qwen3、Krea2 用 Wan 的 VAE，主體再乾淨也不能列（政府案判準是「會不會在客戶機器上執行」）。

隨附清單（`scripts/build_sd_catalog.py` 產生，`console/sd_catalog_default.json`）：FLUX.1 schnell、Chroma1 HD、Stable Diffusion 3.5 Medium、
Stable Diffusion 1.5、LTX-2.3 影片（蒸餾版，文字編碼器是 Google Gemma 3 12B）。

實測（.27 AI 395，網頁編譯的 sd-cli master-1030／ROCm）：FLUX.1 schnell Q8_0 1024×1024 四步 53 秒，試算 20.1 GiB、實測最高 20.9 GiB；
LTX-2.3 蒸餾 Q2_K 512×320、17 格、八步 48 秒（webm，含聲音），試算 19.9 GiB、實測最高 19.3 GiB。

### 網頁對話、助理、執行設定、匯入與搜尋（對齊 LocModel 的其餘功能）

| 功能 | 在哪 | 做法 |
|---|---|---|
| 網頁對話 | 對話 | 選一個正在跑的本機模型或第三方 API 直接聊，一個字一個字串流出來；看圖模型可以附圖。按「附件」可以附 PDF、Word、Excel、PowerPoint、純文字與程式碼（一則最多 5 個、每個 15 MB）：主控台在獨立行程裡抽出文字（限時 30 秒、Linux 限 2 GB 記憶體、擋壓縮炸彈），放在問題前面標明是附件；舊版 .doc／.xls／.ppt 與掃描型 PDF 會講清楚怎麼處理。主控台經代理轉給模型服務，**金鑰留在代理那邊不給瀏覽器**。速度顯示 llama-server 自己量的 `predicted_per_second`（實測）。對話只留在這個分頁，不存伺服器、不寫稽核（稽核只記用了哪個欄位、幾則訊息） |
| 助理 | 對話 › 模型選單的「主控台助理」 | 模型用四個**唯讀**工具查這個主控台管的機器（列機器、這台狀態、推薦模型、已下載的模型），只能查不能動作。最多來回 6 次；同一個工具同樣參數查第二次直接回「已經查過」；最後一輪不給工具逼它作答；小模型產出不合工具格式（llama.cpp 回 500）時改成不帶工具重答 |
| 執行設定 | 執行頁（也可從欄位卡的「執行設定」） | 上下文 8K／16K／32K／64K／128K 一鍵切換，每個選項先用准入公式試算（`admit(cfg_override=…)`），放不下的反灰並說明原因；思考模式（新版 llama.cpp 用 `--reasoning on/off`，舊版關掉用 `--reasoning-budget 0`，看 `--help` 決定）；區網存取（`--host 0.0.0.0` 或 `127.0.0.1`）。顯示預估速度（跟推薦同一條公式）與載入時間——實際載入過就顯示實測秒數，沒有才依磁碟速度估 |
| 推理引擎下載與離線安裝 | 安裝推理引擎視窗 | 下載官方預編譯版時可以「暫停下載」，斷線、暫停或代理重啟後按「繼續下載」用 HTTP Range 接著下，整個檔核對官方 sha256 才裝，裝好後下載檔清掉。不能連外：展開「改成手動匯入安裝包」，選官方的 .zip／.tar.gz（可貼官方 sha256），分塊送、可以續傳；送齊後核對、解壓縮（落到資料夾外就整個拒絕）、找出包在子資料夾的 llama-server／sd-cli、試跑，裝進 `~/llama.cpp-import-<sha 前八碼>/` |
| 用硬碟執行 | 執行頁 | 記憶體放不下時自動往後排（顯示卡→系統記憶體→硬碟），不必另外勾選；按鈕標「會用到硬碟，很慢」。存放區在機械硬碟上會提醒改放 SSD（機械硬碟慢約十倍）。AI 395 這類 BIOS 切兩塊的機器照樣不給 |
| 匯入 GGUF | 模型 › 右上「匯入本機 GGUF」 | 瀏覽器分塊送（每塊 32 MB）、可以續傳（代理回報已收到多少）；最後一塊到齊才核對 GGUF 檔頭、改成正式檔名。上傳的本文不是 JSON，另外擋跨站：要登入、要 `application/octet-stream`、要自訂標頭 `X-XConsole-Upload` |
| 搜尋 Hugging Face | 模型 › 搜尋 Hugging Face | 關鍵字找 GGUF（照下載數排），點一下帶進名稱並列出量化；每個量化標這台跑不跑得動、預估 tok/s 與三色分層條，並標「建議」；中國來源標出來 |

### Agent、第三方 API、執行預覽、用硬碟執行、載入進度（API）

前後端的合約真本在 `docs/WORKFLOW.md` 第四、五節；下面是實際的路由與回應。全部要登入，寫入動作都記稽核。

| 路由 | 回應／說明 |
|---|---|
| `POST /api/node/{n}/library/hf` `{repo, token?, match?}` | 代理讀遠端 GGUF 檔頭（`hf_profile`）取結構；每個 `choices[i]` 多 `plan: {level, tok_s, place, need_gib, split:{gpu,ram,disk}, guess, cpu_moe, kv_gib, ctx, bits, load_est_sec}`，整體多 `pick`、`guess`、`capacity`、`mmproj_pick`。讀不到檔頭退回只有檔案清單，用稠密假設估、`guess: true`、另附 `profile_error`。同一個 repo 10 分鐘內不重抓檔頭 |
| `GET /api/node/{n}/library` | 每顆 `gguf[i]` 多 `plan`（同上格式，另加 `load_sec`＝代理記的實測載入秒數，沒有是 `null`）；整體多 `capacity` |
| `GET /api/node/{n}/slot/{s}/progress` | `{state: idle\|loading\|ready\|failed, stage: prepare\|read\|alloc\|warmup\|done, pct, read_gib, total_gib, elapsed_sec, eta_sec, message}`；換模型的請求進行中每秒問一次。載入結束後結果保留 10 分鐘 |
| `POST /api/node/{n}/slot/{s}/model` | 原本的參數之外多收 `allow_disk`（只有 `true` 才算）、`thinking`（auto／on／off）、`lan`（true／false），同一次啟動就套用；回應多 `disk_mode`、`ngl`、`split`、`thinking`、`lan`。AI 395 這類 BIOS 切兩塊的機器帶 `allow_disk` 一律 400（`disk_denied: true`） |
| `GET /api/node/{n}/apiinfo/{s}` | `run` 多 `split`、`mode`（normal／cpu_moe／disk）、`disk_mode`（現在是不是用硬碟跑）、`disk_allowed`、`disk_needed`、`disk_note` |
| `POST /api/preview` `{html}` → `{url, id, expires_at}` | `GET /preview/<id>` 回原文，標頭 `Content-Security-Policy: sandbox allow-scripts allow-forms allow-modals allow-popups; …`、`X-Frame-Options: SAMEORIGIN`、`Cross-Origin-Embedder-Policy: credentialless`。只有建立者那一個登入看得到，30 分鐘、單筆 2 MB、最多 50 筆 |
| `GET /api/providers` | `{items: [{id, name, base_url, default_model, key_set, ca_set, cn_origin, created, updated}], presets: [{key, name, base_url, default_model, note}]}`，永不回金鑰 |
| `POST /api/providers/save` | `{id?, name, base_url, api_key?, default_model?, ca_pem?, clear_key?, clear_ca?}`；編輯時 `api_key` 空白＝保留、沒帶 `default_model`＝保留 → `{ok, provider}` |
| `POST /api/providers/{id}/delete`、`/models`、`/chat` | `/models` → `{ok, ms, models: [{id, cn_origin}]}`；`/chat` 參數與串流格式同本機對話（`data: …` 逐行原樣轉送、`data: [DONE]`），不串流回 JSON |
| `GET /api/node/{n}/agent/folders` | `{enabled, root, os, folders: [{name, files, updated}]}`；這台沒開 Agent 模式回 403 `{disabled: true}` |
| `POST /api/node/{n}/agent/folders/create` `{name}`、`/agent/fs/list` `{folder, path?}`、`/agent/fs/read` `{folder, path}` | 建資料夾、列檔（`files: [{path, dir, size, mtime, link}]`）、讀檔（唯讀，給右側面板）；寫檔與執行指令只能經任務 |
| `POST /api/agent/tasks` `{node, folder, model, prompt}` | `model` 是 `{kind: "local", slot, node?}` 或 `{kind: "provider", id, model?}`（也收字串 `local:<欄位>`、`provider:<id>[:<模型>]`）；同時只能一個進行中（409） |
| `GET /api/agent/tasks`、`GET /api/agent/tasks/{id}` | 清單不含 log；單筆 `{id, state, plan, files:[{path, action: created\|modified}], log:[{type: text\|tool\|result\|error, ts, …}], pending:{id, command, timeout}, tree, steps, max_steps, summary, error, always}` |
| `POST /api/agent/tasks/{id}/approve` `{allow, always}`、`/stop` | 沒有在等核准時 409 |
| 這台的設定 `POST /api/node/{n}/machine` | 多收 `agent_enabled`（布林；沒帶就不動），`GET` 的 `settings.agent_enabled` 與 `agent: {enabled, root}` |

等級多一級 `disk`（要用硬碟）：放不進快速層、但 KV、計算緩衝與放 GPU 的層放得下，而且不是 BIOS 切兩塊的機器。
速度照 `recommend.DISK_GBS`（硬碟讀取頻寬，MoE 只算被選中的專家）估，`split.disk` 是溢出到硬碟的量。建議下載（`pick`）不會挑 disk 的量化。
代理端換模型時先照一般放法試算，放不下且帶了 `allow_disk` 才改用檔案映射：不加 `--no-mmap`，獨立顯卡 `-ngl` 照放得下的層數，
Apple 用 999，DGX Spark／Jetson 這類非 Apple 的統一記憶體一律 0（CUDA 會把卸載的層複製成不可換頁的記憶體）；准入只算不是檔案映射的部分，底線照守。

Agent 的工作資料夾固定在每台的 `~/xconsole-agent/`，路徑 realpath 後檢查（符號連結指出去也擋），讀 1 MB、寫 2 MB；
指令逾時預設 120 秒、上限 600 秒，輸出各截 32 KB，環境變數拿掉代理權杖與 `*_KEY`／`*_TOKEN`／`*_SECRET`；
每個任務最多 40 步、30 分鐘；`run_command` 一定先等人核准，每條實際執行的指令寫稽核（前 300 字、結束碼）。

### 加一個情境

在該機 registry 的 `profiles` 加一筆，`changes` 寫要改哪個欄位的哪些參數。
套用時會先備份設定檔，重啟失敗自動還原回上一版並重啟。

### 帳號與密碼

登入改為「帳號＋密碼」，帳號預設 `admin`。平常在網頁「設定 → 帳號與安全」變更；
變更後所有已登入的裝置會被登出。忘記密碼時在主控台機器上重設（從標準輸入讀，不進指令列也不走環境變數）：

```bash
# 在 .27 上執行，會互動詢問帳號與密碼
cd ~/xconsole && python3 xconsole_server.py --set-credentials
```

### 客戶品牌與頁尾（賣給客戶時換成客戶的名稱與 Logo）

「設定 → 5 品牌與外觀」可以改**單位名稱**（例：中原大學）、**系統名稱**（例：AI 模型主控台）並上傳 **Logo**。
套用後頁首、登入頁、瀏覽器分頁標題與小圖示一起換，不用重開服務。

- Logo 只收 PNG、JPEG、WebP，1 MB 以內，寬高 16～4096 像素；看檔頭判斷格式，不看副檔名。**不收 SVG**（可以夾帶程式碼）。
- Logo 一律放在白底上顯示，深色模式下深色字的校徽也看得清楚；瀏覽器分頁小圖示用單位名稱第一個字產生（橫式 Logo 縮成正方形會擠成一團）。
- 品牌資料與 Logo 不用登入就讀得到（登入頁要用），改動要登入，每次變更都寫進稽核紀錄（不記圖檔內容）。
- 存在 `~/.xconsole/fleet.json` 的 `branding` 與 `~/.xconsole/branding/logo.*`（0600）。
- 每一頁最下方的「Powered by xCloudinfo 云碩科技」寫死在 `index.html`，**不開放關掉**；`test_branding.py` 守著登入頁與主畫面各一個。
- 品牌資料回來之前先把品牌區藏住，避免畫面先閃一下預設的 xConsole 才換成客戶名稱。

### HTTPS 與憑證（管理者在網頁上自己換）

「設定 → HTTPS 與憑證」照 5 步設定順序軸操作，每一步的狀態由系統實際資料算出：

| 步驟 | 做什麼 |
|---|---|
| 1. 確認使用者會打的網址 | 預設只取對外那張網卡的 IP；有網域名稱就填網域 |
| 2. 取得憑證 | 已有憑證就直接上傳；沒有就在頁面上產 CSR 交給憑證廠商（私鑰加密存在伺服器，不會離開） |
| 3. 上傳並檢查 | 拖放任何格式：PFX／P12（含 Windows 匯出的 RC2-40＋3DES 舊式加密）、PEM、DER 的 CER／CRT、KEY、P7B，順序不拘。檢查結果分「不能套用／要注意／可以套用」三段，全部講人話 |
| 4. 套用 | **不重開服務**：新連線立刻用新憑證，已連上的不中斷；系統實際連自己一次核對指紋，不對就自動換回 |
| 5. 開啟 HSTS | 只有正式憑證才能開；暫用自簽時開關是灰的 |

另外有「還原上一張」（保留最近十張）與「依目前網址重產暫用自簽」。剩 30／14／7／1 天會記一次到期提醒，
剩 7 天內每一頁頂端都會出現提醒列；健康檢查 `GET /api/health` 回 `tls.daysLeft`。

用 `http://` 打到 9400 埠會收到 308 轉到 `https://` 同埠同路徑（用 308 不用 301，POST 轉址後還是 POST）。

### 服務使用報告

網頁「設定 → 使用報告」可直接看近 7／30／90 天的六塊數字，也能下載 JSON。
要產出給主管看的 Word 月報（含 PDF），在有原始碼的電腦上執行：

```bash
# 密碼檔要自己建，權限必須是 600（產生器會檢查，太寬就拒絕執行）
/usr/bin/python3 scripts/report/build_usage_report.py --days 30 --password-file ~/.xconsole/report.pw
```

連線時用 `deploy_console.sh` 帶回本機的主控台憑證驗證；主控台換成正式憑證後改用系統的憑證庫。**不提供略過驗證的選項。**
產出放在 `dist-report/`（不進版控）。電價與雲端參考單價在網頁上填；沒填就只報用電度數與 token 量、不換算金額，並在報告裡寫明。

需要 `matplotlib` 與 `python-docx`，以及 `soffice`、`pdftotext`。macOS 上 Homebrew 的 Python 3.15 裝不上 python-docx 的相依套件，請用系統的 `/usr/bin/python3`。

## 五、安全

- 主控台只提供 HTTPS；用 `http://` 打到同埠會被 308 轉走。只開 TLS 1.2／1.3。
- **主控台連各機代理也走 HTTPS**。各機代理第一次啟動自產憑證，`deploy_console.sh` 經 SSH 取回當作信任依據；
  代理送出的憑證跟當初信任的不同就拒絕連線，不會退回明文或關掉驗證。代理重裝後重跑 `deploy_console.sh`，或在「設定 → 機器」匯入新憑證。
- 各機代理權杖存在該機 `~/.xconsole/token`（0600）；主控台端以 AES-256-GCM 加密存在 `fleet.json`（啟動時自動把明文遷移成密文）。瀏覽器拿不到任何代理權杖。
- 登入票證記在伺服器端（資料庫只存雜湊），登出、換密碼即作廢；同一來源 15 分鐘錯 5 次即擋（429）。POST 只收 `application/json`，跨站表單送不出來。
- 登入密碼以 PBKDF2-SHA256（200,000 輪）加鹽雜湊後存檔，不存明文；帳號與密碼都算一遍才比對，錯誤時固定延遲一秒。
- 待配對 CSR 的私鑰以 AES-256-GCM 加密落地（`enc:v1:` 前綴），主金鑰檔 0600。
- 所有操作寫入稽核紀錄（誰、何時、從哪裡、做了什麼、結果）。寫入前一律經過集中的 `redact()`：密碼、私鑰、權杖、PFX 一律遮罩，憑證只記指紋。
- 回應帶內容安全政策、禁止被嵌入框架、禁止 MIME 類型猜測等標頭；HSTS 由後台開關控制。
- 含輸入欄位的彈窗不准點背景關閉；所有 Enter 送出的欄位都擋中文輸入法組字（`test_ui_guard.py` 守著）。
- 代理與主控台都設有記憶體與 CPU 用量上限。

## 六、自我檢查

| 測試 | 在哪跑 | 項數 | 測什麼 |
|---|---|---|---|
| `agent/test_admit.py` | 任何機器 | 15 | 三種記憶體架構的准入公式、KV 兩種量級、底線邊界 |
| `console/test_ui_guard.py` | 任何機器 | 14 | 掃所有前端 JS：彈窗不准點背景關閉、Enter 擋輸入法、自動更新不整頁重建、頂層導覽照動線排（概覽第一、模型→執行→對話、設定最後、最多八格）、樣式表大括號成對 |
| `agent/test_platform.py` | Linux／macOS／Windows | 32 | 硬體讀數、代理自己看管模型服務（帶起來、掛了重開、停掉、代理重開後認回、起不來停在失敗） |
| `test/e2e_install.py` | 裝好的機器 | 16 | 經網頁 API 走完：登入 → 確認架構 → 推薦 → 裝推理引擎 → 下載模型 → 換上 → 問一句話 → 停掉 |
| `console/test_sd.py` | 任何機器 | 66 | 畫圖與影片：挑檔與量化、元件跟著主體、三種記憶體架構的准入與 `--max-vram`、中國來源（含文字編碼器與 VAE）、`sd-cli` 參數、進度解析、共用檔刪除、看門狗中止、啟動失敗不卡住、macOS／Windows 預編譯版挑檔 |
| `agent/test_platform.py` 裡的匯入、搜尋、思考模式 | 三個系統 | 12 | 分塊、續傳位置、同名不覆蓋、不是 GGUF 刪掉、路徑穿越；搜尋字元；新舊版思考模式旗標 |
| `console/test_recommend.py` | 任何機器 | 77 | 推薦：容量、等級、速度、挑量化、與代理同一條公式（KV、自動分層層數、資料夾名）、GGUF 張量剖析、中國來源擋下、推薦清單增改刪 |
| `console/test_branding.py` | 任何 Linux | 49 | 客戶品牌：名稱驗證、Logo 只認 PNG／JPEG／WebP 檔頭（SVG、偽裝檔、超大檔被擋）、未登入、權限 0600、稽核不記圖檔、頁尾寫死 |
| `console/test_tls.py` | .27（要 openssl 3） | 36 | 用 openssl 現場產客戶會拿來的各種檔案逐一匯入、熱套用、還原、轉址、到期提醒 |
| `console/test_usage_report.py` | .27 | 26 | 報告六塊、計數器歸零、取樣空窗不計電、低於底線次數、稽核遮罩 |
| `console/test_e2e_tls.py` | .27 | 21 | 另起隔離主控台，經網頁 API 走完換憑證整套流程（不碰正式環境） |
| `agent/test_agent_mode.py` | Linux／macOS／Windows | 54 | Agent 模式代理端：預設關閉、路徑跳脫（含符號連結）、讀寫上限、工作目錄、逾時、輸出截斷、環境變數拿掉權杖與金鑰、HTTP 路由 |
| `agent/test_gaps.py` | Linux／macOS／Windows（裝執行檔那幾段不跑 Windows） | 58 | Linux 的 Intel 顯示卡偵測、記憶體架構、不綁卡、Vulkan 編譯；macOS／Windows 分辨 SSD；預編譯版接著下載（Range）、sha256、暫停；手動匯入（續傳、檔頭、sha256、子資料夾的執行檔、試跑）；參考圖檢查、`-i`／`--strength`／`-b` |
| `console/test_extract.py` | Linux／macOS／Windows | 29 | 對話附件抽文字：Word／Excel／PowerPoint／PDF、四種文字編碼、二進位與舊版 Office、壓縮炸彈、截斷；主控台端的獨立行程、逾時、被砍、上限 |
| `agent/test_load_disk.py` | 任何機器（實際換模型那段不跑 Windows） | 37 | 載入進度的階段與百分比、用硬碟執行的分層與 `-ngl`、准入的 KV 分池、假 llama-server 實際換模型（mmap、思考模式與區網同次套用與沿用、進行中問得到進度） |
| `console/test_providers.py` | 任何機器 | 40 | 第三方 API：金鑰加密落地（資料庫檔找不到明文）、GET 不回金鑰、編輯空白保留、cn_origin、http 只限私有位址、不跟轉址、錯誤遮罩、串流跟本機對話逐位元組相同 |
| `console/test_preview.py` | 任何機器 | 21 | 執行預覽：sandbox 標頭、別人的登入拿不到、過期、2 MB、50 筆、其他頁仍是 DENY |
| `console/test_agent_tasks.py` | 任何機器 | 31 | Agent 對話迴圈（假的 OpenAI 相容服務）：等待核准、拒絕、always、步數上限、不支援 tools、單一任務、停止、本機欄位當模型、稽核 |
| `console/test_e2e_features.py` | 任何機器（要 cryptography） | 18 | 另起隔離的代理與主控台走 HTTPS：HF 等級（假 Hugging Face）、已安裝等級、進度、預覽、第三方 API、打開 Agent 模式、任務核准到完成 |
| `console/test_agentlink.py` | 任何 Linux（要 cryptography） | 63 | 代理主動連回：指令配對、逾時回 504 並送取消、沒收到的重送、長的不擋短的、串流、缺片段；通道憑證（私鑰不落明文、輪替要在線的都收到才換、SNI 選憑證、退役 30 天後刪）；另起隔離主控台與代理端對端（通道埠與管理網頁埠分開、換管理網頁憑證不斷線、輪替與對外網址幾秒內送達）；同一來源 200 支已驗證的代理全部連得上、未登入仍在第 65 條被擋 |
| `console/test_groups.py` | 任何 Linux（要 cryptography） | 46 | 群組：設定檢查（不合法的講人話擋下、全部檢查過才動）、內容真的變了版本號才加一、改名撞名、「未分類」不能改名或刪除；另起隔離主控台端對端：舊版群組名稱補成群組物件、改名成員跟著改、刪除時成員移到未分類、機器改到新群組名稱時自動建群組；主控台把群組設定送給一支真的代理，幾秒內準備並套用、鎖定經主控台也改不了 |
| `console/test_joincodes.py` | 任何 Linux 或 macOS（要 cryptography） | 49 | 群組加入碼：名稱樣板展開與不合法寫法、網段解析與 IPv4 對映、可用次數與有效時間的範圍、多台必須用樣板、重新安裝只能一次；兩個同時加入各拿到不同名稱、名額滿了第三個擋下；重新載入後仍有效；用完、網段外、作廢都加入不了；過期 7 天的紀錄自動清掉；另起隔離主控台與一支真的代理端對端：主控台重啟後同一個碼照樣加入 A01～A03、用完擋下、清單不含公鑰、沒登入看不到也不能作廢、磁碟上只有雜湊 |
| `agent/test_group.py` | Linux／macOS／Windows | 32 | 群組設定對齊代理這一側：準備（下載、核對 SHA-256、只准用的模型）與套用分兩段、指定時間以主控台時鐘為準、下次開機才套用、鎖定時本機 API 回 423、被改掉的模型拉回、換群組重來（本機假的 Hugging Face） |
| `agent/test_link.py` | Linux／macOS／Windows | 34 | 代理主動連回代理這一側：連回設定、釘住通道憑證、內部位址連不到改走對外網址、經 HTTP 代理伺服器（含帳密）與壞掉時改直連、SNI 選通道憑證與收到新清單、一般／大檔分片／串流、同一個編號不重做、取消關掉本機連線 |
| `test/security/test_security.py` | 任何 Linux | 52 | 從攻擊者角度：未登入、票證、IDOR 等價、SSRF、路徑穿越、節流、標頭、TLS、機密不落地 |
| `test/test_api_integration.py` | 有 systemd --user | 23 | API 串接設定：改名稱／上下文／金鑰後重啟驗證、失敗自動還原、經網路實測不跟轉址 |
| `test/test_llama_engine.py` | 有 systemd --user | 36 | 模型庫與 llama.cpp：讀檔頭算 KV、跨引擎切換與還原、下載（假 Hugging Face）、刪除 |
| `test/smoke_real_llama.py` | 有 llama.cpp 的機器 | 12 | 真的 Hugging Face＋真的 llama-server 跑一次（100 MB 小模型，跑完刪掉） |

以上除了最後一支，每次 PR 都在 GitHub Actions 重跑（`.github/workflows/security.yml`），另有 CodeQL、gitleaks、pip-audit、SBOM＋grype、ZAP 五道掃描；`main` 有分支保護，全綠才能合。

交付前另外要跑：`check-https.sh`、`check-usage-report.sh`、`audit-secrets.sh`（三支都在對應的 skill 裡）。

## 七、已安裝的模型與實測

`.27` 上目前裝了兩顆，用 `setup.sh --setup --family <名稱> --model <量化> --no-start` 安裝：

| | unsloth-UD-IQ4_XS | coder-IQ1_M |
|---|---|---|
| 權重 | 87 GiB | 54 GiB |
| 匯入 68,622 token（冷啟動） | 1,011.6 tok/s | 1,017.5 tok/s |
| 生成 | 46.0 tok/s | 45.4 tok/s |
| 該次從硬碟讀取 | 70,563 MB（4,013 major faults） | **9.1 MB（0 次）** |
| 載入後剩餘顯存 | 33,094 MiB | **66,364 MiB** |
| 中文 | 正常 | **會出錯或鬼打牆**（官方 issue #438） |

**兩個與直覺相反的量測結果，寫下來免得下次又判斷錯：**

1. **長提示的瓶頸是算力，不是磁碟。** 先前看到「一次請求讀 70 GB 硬碟」曾判定磁碟是瓶頸；
   Coder 把讀取量降到 9.1 MB（專家全在記憶體、零 major fault），prefill 速度卻幾乎沒變
   （1,017 對 1,011 tok/s）。磁碟 I/O 有發生但被重疊掉了。**不要再為了 prefill 去調記憶體配置。**
2. **提示快取的效益遠大於換模型。** 同一份 68,622 token 的提示，命中快取時
   「68,615 重用 + 7 讀取，61 毫秒」，整筆由 68.2 秒降到 24.7 秒。
   寫程式的用戶端每輪送的是「同一份脈絡再長一點」，**快取命中率才是該顧的**。

**Coder 是 reasoning 模型**：預設把 token 全燒在 `reasoning_content`、`content` 回 `null`，
只讀 `content` 的用戶端（Copilot 這類）會拿到空的。設定檔加 `"reasoning_effort": "none"`
即可在伺服器端預設關掉，用戶端不必改。

## 八、踩過的坑（改之前先看）

| 症狀 | 真因 |
|---|---|
| 服務起不來，ExecStart 指向 `/Users/jeff/...` | 單引號內的 `$HOME` 在本機就展開了；systemd 的 ExecStart 也不吃 `$HOME`，要用 `%h` |
| 改了程式但行為沒變 | `systemctl enable --now` 對已在跑的服務不會重啟，要明確 `restart` |
| 群組全變「未分類」 | `GROUPS` 是 bash 唯讀內建變數，不能拿來當關聯陣列名 |
| 登入後整頁登入畫面還在，頁長多一屏 | `.login-page{display:grid}` 蓋掉 `[hidden]` 的 `display:none`，要補 `[hidden]{display:none!important}` |
| 整段內容停在 `opacity:0` 看不見 | 捲動漸入的備援寫成「IO 沒回呼才放行」——IO 一掛上就會對所有目標回呼，旗標馬上變 true，備援永遠不執行。**三秒後要無條件放行** |
| 顯存長條加起來超過整機 | 單 GPU 無法逐進程分 VRAM，每個欄位都記整機用量會重複計算。改成依各欄位預估需求加權分攤 |
| 溫度欄顯示 `temp1_input` | hwmon 的 label 檔不一定存在，退回用感測器名稱 |
| camofox 顯示「空欄位」 | 它真的卡在 `activating`（Type=notify 沒送就緒訊號）。狀態不能壓成開／關兩種 |
| `setup.sh` 編譯掛在 `iq_kernels.cu:1856` 的 `alignas` | 系統 ROCm 7.1.1 的 clang 20 編不過 0.1.45，要用 TheRock 的 clang 23（`~/rocm/install`）。跑的時候仍要用系統 ROCm 的函式庫 |
| `systemd-run` 跑 setup 被配成 CPU-only | 它的環境極簡，沒有 `ROCM_PATH`／`/opt/rocm/bin`，HIP 偵測失敗。要用 `--setenv` 補齊 |
| 量到的速度好得不像話 | 先看 `cached_tokens`。同一份提示第二次跑會命中快取（68,615 重用、61 毫秒），那不是冷啟動成績 |
| 客戶上傳正確的憑證也被擋下 | 預設網址抓了本機所有 IP（含 Docker 的 172.17.0.1），任何正式憑證都不可能涵蓋。預設只取對外網卡的 IP，`localhost`／`127.0.0.1` 不列入涵蓋檢查 |
| 過期的登入票證照樣能用 | `user or None if 沒過期 else None` 在 Python 會解析成 `user or (None if … else None)`。拆開寫 |
| 自動還原沒觸發，服務其實已經死了 | 只看了 `systemctl restart` 的回傳值。`Type=simple` 被接受就回 0，載模型失敗是幾秒後才退出；要等埠回得出 `/v1/models` 才算成功，還原前先 `reset-failed` |
| 換模型三秒就崩 | 換模型換的是一整份設定，不是 `--pack`／`--native` 兩個路徑。以 `setup.sh` 產的 `strata-<名稱>.json` 為底，只覆寫 exe／lib_dirs／port／parallel |
| 改了手機版 CSS 完全沒效果 | 手機覆寫寫在前面的媒體查詢，後面才定義的基本樣式同權重蓋掉了它。手機覆寫放檔案最後 |
| 手機版整頁被撐寬 | 憑證指紋這種冒號串沒有斷行點。數值加 `overflow-wrap: anywhere` |
| 自動更新時正在填的表單蒸發 | 畫面每 15 秒重畫。設定頁與記錄頁不參與自動重畫 |
| `declare: -A: invalid option` | macOS 內建 bash 是 3.2，不支援關聯陣列。用 `case` |
| 登出後在同一條連線上仍是登入狀態（反過來，登入後第一個請求被當成未登入） | `BaseHTTPRequestHandler` 在 keep-alive 連線上用同一個物件處理多個請求，身分快取會被下一個請求沿用。每個請求開始時清掉 |
| 壞掉的模型讓換模型等了十幾分鐘才還原 | 設了 `Restart=` 的服務一退出就進 `auto-restart`，狀態仍是 `activating`；只看 `failed` 會白等到逾時。要看 SubState |
| 開了金鑰之後每次重啟都被判成起不來 | 不帶金鑰打 `/v1/models` 回 401；健康檢查要把 401 視為服務活著 |
| llama.cpp 開了金鑰，「沒帶金鑰要被擋」卻驗不出來 | llama.cpp 的 `/v1/models` 本來就公開。要用 POST 對話端點測 |
| 下載完成了，模型庫卻沒有它 | 先掃存放區、後讀下載狀態，下載剛好在兩步之間完成。先讀狀態再掃 |
| Hugging Face 清單第一列是 0.1 GiB 的「Q8_0」 | 那是 `MTP/` 子資料夾裡推測解碼用的草稿頭，不是主模型。輔助檔要標出來、排到最後 |
| 推薦畫面把 Gemma 4 E4B 算成每個 token 讀 100% 權重 | E 系列的 `per_layer_token_embd` 占大半檔案，但它是查表，每個 token 只讀一列。查表的張量不算讀取量 |
| 確認記憶體架構之後，硬體卡還是舊的分層 | 推薦結果有 60 秒快取；改了這台的設定、下載完成、刪除模型都要清掉，否則畫面停在舊架構或「下載中 0%」 |
| 網頁編譯在 .27 設定階段就失敗：`HIPCXX=/usr/llvm/bin/clang` 找不到 | `hipcc` 在 `/usr/bin`，是指到 `/opt/rocm-7.1.1/bin/hipcc` 的符號連結；照 `/usr/bin` 推 ROCm 根目錄會推成 `/usr`。要先 `realpath` 追到底（llama.cpp 的編譯也一樣受影響） |
| AMD 上產圖在載入擴散模型時 `ROCm error: unspecified launch failure` | gfx1151＋ROCm 7.1.1 開 flash attention（HIP MMA）會崩。AMD 一律加 `--flash-attn off`，512×512 四步 12 秒正常出圖 |
| 畫圖與影片的樣式整段沒套上（最近產出變成白底按鈕） | 合併衝突時吃掉品牌區 `@media` 的結尾 `}`，瀏覽器把後面所有規則都當成那個區塊的一部分丟掉。`test_ui_guard.py` 現在會檢查 `style.css` 的大括號成對 |
| 正式機上網頁編譯與產圖被砍掉（測試時好好的） | 代理服務設了 `MemoryMax=256M`、`CPUQuota=25%`，代理直接開的子程序在同一個 cgroup 裡；用 nohup 起的測試代理沒有這個限制，所以測不出來。編譯與 sd-cli 改用 `systemd-run --user --scope` 開出去（`own_scope()`），PID 不變、看門狗照用 |
| 推薦清單裡的 FLUX VAE 要授權才能下載 | `black-forest-labs` 的 repo 要同意條款（gated）。改用 `diffusers/FLUX.1-vae`（diffusers 格式，sd-cli 讀得懂，實測畫面正常） |
| 新代理沒確認架構，AI 395 被當成獨立顯示卡推薦 | 登錄檔預設 `discrete`。代理把偵測建議放進 `chip.suggest`，沒確認又不一致時，總覽第一步與硬體卡都會要求確認 |
| macOS 上代理重開後認不回還在跑的模型 | 認行程用「帶起來當下記的程式名稱」：macOS 的 Python 啟動半秒後會從 python3.15 換成 python（Python.app）。名稱去掉結尾版本號，並在看管迴圈裡持續更新 |
| 代理重開後，被認回來的模型掛了不會重開 | 認回來的不是這次 Popen 的，拿不到結束碼；看管迴圈要另外逐一檢查 PID 還在不在 |
| macOS 推薦把 M5 Max 當成基本款 M5（153 GB/s） | 型號比對「M5」吃到了「M5 Max」。基本款名稱後面不能接 Pro／Max／Ultra；沒列在表上的高階型號回未知，請管理者填，不拿基本款的數字充數 |
| Windows 上 `print()` 什麼都沒留下 | 用 pythonw 在背景跑時 `sys.stdout` 是 None；代理與主控台啟動時改寫進 `.xconsole/agent.log`／`console.log` |
| 瀏覽器上傳被擋時看到的是「連線重置」不是錯誤訊息 | 擋下時沒把本文讀完就關連線，對方送到一半收到 RST。小的本文（64 KB 內）照樣讀掉再回錯誤，大的才直接關 |
| 助理一直重複呼叫同一個工具、不肯作答 | 3B 這種小模型會這樣。重複的呼叫直接回「已經查過」，最後一輪不給工具；llama.cpp 帶工具時遇到不合格式的輸出會整個 500，改成不帶工具重答 |
| `test_e2e_tls.py` 在 Python 3.13 以上一開始就 `CERTIFICATE_VERIFY_FAILED` | 3.13 起預設 `VERIFY_X509_STRICT`：測試自己簽的憑證要有 AKI／SKI，根 CA 要有 keyUsage。只改測試的憑證產生參數，主控台的驗證不放寬 |
| 稽核裡「金鑰換新／保留」整欄變成「[已遮罩]」 | `redact()` 看鍵名，含 `key` 的一律遮掉。稽核欄位不要用 `key_change` 這種名字（改叫 `credential`） |
| 一整間教室同時開機，有幾台連回主控台時被「連線重置」 | `ThreadingHTTPServer` 的接受佇列預設只有 5，同一瞬間進來的連線多出來就被系統重置。`tls.TLSServer` 改成 128 |
| 換了通道憑證，工作站要等 25 秒以上才收到 | 長輪詢掛著時不會回頭看設定有沒有變。輪詢要帶「提早回」的條件（`Link.poll(stop=…)`），換憑證或改通道設定時叫醒所有掛著的輪詢；代理回報收到的那一輪也要馬上檢查能不能換上，不能被「每 2 秒最多檢查一次」擋掉 |
| 主動連回的代理按「停止」後，本機那條連線關不掉 | 本機回應帶 `Connection: close` 時 `http.client` 會把 `conn.sock` 清成 `None`，取消時拿不到。要在送出請求前就把 socket 存起來 |
| 換模型後思考模式、只給本機連的設定不見了 | 以前換 GGUF 都重寫一份新設定（`host` 固定 0.0.0.0、沒有 `reasoning`）。現在沒帶就沿用欄位原本的設定 |
