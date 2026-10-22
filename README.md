# xConsole 模型主控台

云碩科技**統一記憶體 AI 機**的模型主控台。一句話：**哪台機器上開了哪個模型、還剩多少記憶體、能不能再開，不用再自己記。**

## 定位

專門給這三個系列：

| 系列 | 代表機 | 記憶體架構 |
|---|---|---|
| **AMD Ryzen AI 395 系列** | HP Z2 Mini G1a（`.26`／`.27`） | 96 GiB 給 GPU，系統只剩 31 GiB（切兩塊） |
| **MIC-743／Jetson 系列** | AGX Thor（`.20`）、AGX Orin、Orin NX | 統一記憶體單一池 |
| **DGX Spark 系列** | GB10（`.24`／`.25`／`.32`） | 統一記憶體單一池 |

這三個系列的共同點是**統一記憶體**——也正是傳統「看顯存」的儀表板會盯錯數字的地方。
`nvidia-smi` 在 GB10 與 Thor 上回的 `memory.total` 是 `[N/A]`；Strix Halo 則是顯存看起來很鬆、
真正會把整台拖死的是那 31 GiB 的系統記憶體。**xConsole 就是為了算對這件事而存在的。**

**一台也管，十台也管。** 單機模式把代理與主控台裝在同一台，客戶拿到機器開瀏覽器就能用，
不需要另外準備管理機；之後要串成多台，代理與欄位登錄完全不用動。
機群裡的傳統多卡伺服器（MI50 農場、P100、Ada）也能納管，走 `discrete` 架構，但不是本產品的主場。

- 線上位置：`https://192.168.11.27:9400`（自簽憑證，瀏覽器會先警告一次）
- 初始密碼：見同目錄 `初始密碼.txt`，**登入後請盡快換掉**
- 目前納管：`.26`、`.27`（AI 395）、`.20`（Thor／MIC-743 同一顆 SoC）

---

## 一、它解決什麼

這套東西的價值不在監控——監控你已經有了（amd-cluster-monitor、gpufleet）。價值在三件監控做不到的事：

1. **准入控制**：載模型之前先算「載下去之後最壞會剩多少」，不夠就擋住。
   這擋的是 `.26`／`.27` 被 OOM 拖死整台、需要走到機器前面物理重開的那種事故。
2. **情境切換**：同一顆模型、同一台機器，寫程式後端要長脈絡、抓料要吞吐。
   一鍵切換整組參數，參數留在平台上而不是留在腦袋裡。
3. **角色欄位**：按「主腦／副腦／聽覺／說話／記憶／判斷／雙手／眼睛」排版，
   空的欄位自己會喊話，一眼看出這台還缺什麼。

## 二、三種記憶體架構分開算（最關鍵的設計）

機群裡有三種完全不同的記憶體模型，**套同一條公式會盯錯數字**：

| 架構 | 機器 | 怎麼回事 | 底線預設 |
|---|---|---|---|
| `unified_split` | `.26` `.27` Strix Halo | BIOS 切 96 GiB 給 GPU，**系統只剩 31 GiB**。會把整台拖死的是系統這塊，不是顯存 | 系統 RAM 15 GiB |
| `unified_single` | DGX Spark、Jetson | 統一記憶體一個池，GPU 與系統搶同一塊，不能分開算成「各自都夠」 | 12 GiB |
| `discrete` | MI50 農場、P100、Ada、5090 | 每張卡自己的顯存，另外一塊系統 RAM | 顯存 1 GiB／RAM 8 GiB |

底線可在各機 `~/.xconsole/registry.json` 用 `ram_floor_gib`／`vram_floor_gib` 覆寫。

### KV 的大小因模型而異，差好幾個數量級

`Qwen3.8-Flash-Next` 是 GDN／線性混合注意力，int8 下只要 **1,056 B/token**，
128K 全開也才 **0.13 GiB**。實測佐證：`.27` 把 `--max-context` 由 32,768 改成 131,072，
載入後剩餘顯存由 33,026／33,416 MiB 變成 33,094 MiB，**上下文變四倍而顯存沒變**。

傳統全注意力模型可以到幾十萬 B/token，同樣 128K 要吃掉幾十 GiB。
所以欄位可以用 `kv_bytes_per_cell` 指定自己的值，沒指定才用引擎預設表。
**不要假設所有模型的 KV 成本一樣。**

## 三、架構

```
瀏覽器 ──HTTPS──> xConsole 主控台（.27:9400）──HTTP+Bearer──> 各機節點代理（:9401）
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

### 單機模式（賣一台機器給客戶時）

```bash
bash deploy_standalone.sh 20      # 代理＋主控台都裝在 .20，它只管自己
```

### 加一台機器（多機模式）

```bash
bash deploy_agent.sh <別名> --seed      # 先在 registries/<別名>.json 寫好欄位
bash deploy_console.sh 27 26 27 <別名>  # 重新組 fleet.json 把它串進來
```

`registries/<別名>.json` 要填的重點：

```jsonc
{
  "machine": {
    "memory_model": "unified_split",   // 三選一，決定用哪條准入公式
    "ram_floor_gib": 15                // 這台的系統記憶體底線
  },
  "slots": [{
    "id": "brain", "role": "主腦", "tier": "高",
    "unit": "strata.service",          // 對應的 systemd 服務
    "kind": "strata",                  // 會去讀設定檔解析 context／KV
    "config": "/home/.../strata-prod.json",
    "log": "/home/.../strata-prod.log",// 從這裡撈實測 tok/s
    "est_vram_gib": 64, "est_ram_gib": 14,   // 准入試算用
    "critical": true                   // 停它要二次確認
  }]
}
```

### 換模型（使用者自己在網頁上換）

欄位卡上的「換模型」按鈕會掃這台裝了哪些模型讓人挑，不需要 SSH、不需要改設定檔。
Strata 一顆模型是 **pack（含 tokenizer）＋ 權重檔** 兩件成套的，少一邊就起不來，
所以清單只列配得起來的組合；配不起來的（只有 pack、或缺 tokenizer）另外列出來並寫明缺什麼。

**換模型換的是「一整份設定」，不是兩個路徑。** 每顆模型的 `--ple-gguf`、`--expert-profile`、
`--expert-cache` 都不一樣（Coder 有自己的 `expert-profile-coder.bin`），只換 `--pack` 與 `--native`
會讓專家剖析檔還指著舊模型，**引擎三秒就崩**（實際踩到）。所以清單只提供有 `strata-<名稱>.json`
的模型，換的時候以那份為底，只覆寫「跟這台機器有關、跟模型無關」的項目：
`exe`（我們自編的 build-145）、`lib_dirs`（系統 ROCm；setup 會寫成 TheRock 的路徑，
那條編得動但跑不起來）、`port`、`parallel`。

套用順序：備份 → 套用該模型的設定 → **記憶體准入試算** → `reset-failed` → 重啟 →
**等到那個埠真的回得出模型清單才算成功**，否則還原並重啟。

⚠️ **不可以只看 `systemctl restart` 的回傳值**：`Type=simple` 的 unit 只要被接受就回 0，
載模型失敗是幾秒後才退出的——實際踩到過「API 回成功但服務其實已經死了」，自動還原沒觸發。
還原前也要 `reset-failed`，否則會撞上 systemd 的 `Start request repeated too quickly`。

准入試算用的是**要換過去那顆**的大小，不是欄位上為舊模型估的數字（踩過：Coder 比現用的小，
卻因為沿用舊估值被誤擋）。估算優先序：**實測 > 推自現用模型（按權重比例）> 保底估計**，
載入成功後會自動量一次真值存起來，下次就不用猜。

安全上，這支 API **只接受掃描掃得到的 pack／權重組合**，指到任意路徑會被拒絕
（實測 `{"pack":"/etc","native":"/etc/passwd"}` 回「這個組合不在本機掃到的模型清單內」）。

要讓它有東西可換，把新模型放成這個樣子即可，不必改任何程式：

```
<model_root>/packs/<名稱>/tokenizer     ← pack，含 tokenizer
<model_root>/models/<名稱>/*.gguf       ← 權重，可多分片
```

兩邊的 `<名稱>` 忽略大小寫與符號後要能對上（例 `unsloth-ud-iq4_xs` ↔ `unsloth-UD-IQ4_XS`）。

### 加一個情境

在該機 registry 的 `profiles` 加一筆，`changes` 寫要改哪個欄位的哪些參數。
套用時會先備份設定檔，重啟失敗自動還原回上一版並重啟。

### 改密碼

```bash
# 在 .27 上
cd ~/xconsole && python3 xconsole_server.py --set-password
```

### 換正式憑證

把 `cert.pem` 與 `key.pem` 放進 `.27` 的 `~/.xconsole/tls/`，再 `systemctl --user restart xconsole`。
第一次啟動沒有憑證時會自動產生暫用自簽，**不會退回只開 http**。

## 五、安全

- 各機代理權杖存在該機 `~/.xconsole/token`（0600），**只有主控台伺服器讀得到**；
  瀏覽器拿到的只是 HMAC 簽章的登入票證，`/api/nodes` 已過濾掉 token 欄位。
- 登入密碼以 PBKDF2-SHA256（20 萬輪）加鹽雜湊存檔，不存明文；錯誤時固定延遲 1 秒。
- 主控台只走 HTTPS，帶 CSP／nosniff／X-Frame-Options／Referrer-Policy。
- 代理與主控台都掛 `MemoryMax`／`CPUQuota`，自己不會變成壓垮機器的那一根稻草。

## 六、自我檢查

```bash
python3 agent/test_admit.py
```

13 項，涵蓋三種記憶體架構的放行／擋下、KV 兩種量級、底線邊界、重啟既有服務。
**這支壞掉代表主控台會放行一個會把機器弄死的載入**，改准入邏輯前後都要跑。

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
