# -*- coding: utf-8 -*-
"""xConsole 技術長版的各頁內容。版型 helper 在 build_techdeck.py 的 Deck。

口徑：
  測試數字——2026年10月22日在 .27 實跑的通過項計數（agent/test_admit.py 15、console/test_tls.py 36、
  console/test_usage_report.py 26、console/test_e2e_tls.py 21、console/test_ui_guard.py 5、
  test/security/test_security.py 52、test/test_api_integration.py 23、test/test_llama_engine.py 36，共214；
  test/smoke_real_llama.py 12）。
  常數——程式本身：SESSION_HOURS=12、LOGIN_WINDOW=900、LOGIN_MAX_PER_IP=5、LOGIN_MAX_GLOBAL=50、PBKDF2 200,000、
  SAMPLE_EVERY=300、prune 400天、MAX_BODY 6 MB、憑證歷史10份、檢查票證10分鐘、到期提醒30／14／7／1天。
  缺口——docs/THREAT_MODEL.md 第6節 G1～G4。
"""
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN


def build_pages(d, G):
    T = d.tx
    RED, RED_D, RTINT, CHAR, SLATE, GREY, LGREY, WHITE = (G["RED"], G["RED_D"], G["RTINT"], G["CHAR"], G["SLATE"],
                                                         G["GREY"], G["LGREY"], G["WHITE"])
    STATUS, text_in, FIG, OLDFIG, DATE = G["STATUS"], G["text_in"], G["FIG"], G["OLDFIG"], G["DATE"]

    def page(title, sub=None):
        s = d.add(); d.title(s, title, sub)
        return s

    # 1 封面
    s = d.add("頁首")
    d.rect(s, 4.48, 4.85, 0.18, 1.65, WHITE)
    T(s, 4.62, 4.42, 8.2, 0.36, "技術簡報 Technical Overview", size=13, color=RED, bold=True)
    T(s, 4.62, 4.80, 8.2, 1.10, "xConsole\n模型主控台", size=32, color=CHAR, bold=True, sp=1.0)
    T(s, 4.62, 6.00, 7.9, 0.62, "統一記憶體AI機的模型主控台：准入演算法、雙引擎換模型、安全架構、維運與已知限制", size=13, color=SLATE, sp=1.15)
    T(s, 4.62, 6.78, 8.2, 0.3, f"云碩科技股份有限公司　{DATE}　main分支（2026年10月22日）", size=12, color=GREY)
    d.notes(s, "開場說明這份簡報的立場：不是銷售簡報，是讓技術長判斷主控台在技術上站不站得住、怎麼接進現有環境、風險在哪。每個模組都標出做到哪裡。",
            "版本以GitHub公開倉庫guessleej/xConsole的main分支為準，2026年10月22日合併PR #5。")

    # 2 讀法
    s = page("這份簡報的讀法：每個模組都標出做到哪裡", "狀態標記全本一致；數字一律對得到測試輸出、程式常數與威脅模型")
    cards = [("done", "程式完成並有自動測試，且已在云碩自有機群（兩台AMD AI 395、一台Jetson AGX Thor）的正式環境運作。"),
             ("verify", "程式與自動測試完成，還沒在真實硬體上跑過。目前屬於這一類的是DGX Spark的實機納管。"),
             ("plan", "列在威脅模型缺口清單，尚未實作，集中放在最後的已知限制與路線圖頁，不寫成功能。")]
    for i, (k, txt) in enumerate(cards):
        cx = 0.68 + i * 3.52
        d.rr(s, cx, 1.55, 3.34, 1.95, LGREY, rad=0.06, name="card")
        label, fill, fg = STATUS[k]
        d.rr(s, cx + 0.22, 1.74, text_in(label, 13) + 0.36, 0.36, fill, rad=0.5, name="inner-status")
        T(s, cx + 0.22, 1.74, text_in(label, 13) + 0.36, 0.36, label, size=13, color=fg, bold=True, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        T(s, cx + 0.22, 2.24, 2.90, 1.20, txt, size=13, color=SLATE, sp=1.18)
    d.heading(s, 0.68, 3.72, 6, "數字口徑")
    T(s, 0.68, 4.08, 5.6, 1.9,
      "測試數字是2026年10月22日在.27實際執行的通過項計數，共214項，另有12項真機冒煙測試。"
      "記憶體與速度數字取自自有機群的量測紀錄，不是規格值；每個數字的出處寫在備忘稿。", size=13, color=SLATE, sp=1.2)
    d.heading(s, 6.75, 3.72, 6, "內容順序")
    agenda = ["1　風險與架構", "2　記憶體准入", "3　換模型與雙引擎", "4　模型庫與API串接",
              "5　安全架構", "6　憑證、稽核、報告", "7　證據與規格", "8　限制、路線圖、導入"]
    for i, a in enumerate(agenda):
        T(s, 6.75 + (i // 4) * 2.95, 4.10 + (i % 4) * 0.44, 2.9, 0.4, a, size=12.5, color=SLATE)
    d.goldline(s, "做到的寫做到，沒做到的寫出缺口與期限。")
    d.notes(s, "先把三種狀態講清楚，後面每頁就不用再解釋。DGX Spark標在待實機驗證，不講成已上線。",
            "214＝15＋36＋26＋21＋5＋52＋23＋36，各支測試的通過行數；冒煙測試12項另計，工具呼叫一項列為參考不計入。")

    # 3 為什麼需要
    s = page("為什麼需要：統一記憶體機的失效方式跟一般伺服器不同", "云碩自有機群的實際紀錄")
    y = d.paras(s, 0.68, 1.50, 6.3, [
        ("記憶體是共用的", "AMD AI 395把96 GiB切給GPU，作業系統只剩31 GiB；Jetson與DGX Spark則是GPU和系統搶同一池。"
         "傳統監控只看顯存，在這些機器上會盯錯數字。"),
        ("失效不會報錯", "記憶體耗盡時引擎多半不會回錯誤，而是系統開始大量使用置換空間，整台越來越慢，最後連SSH都進不去，只能到現場重開。"),
        ("用量會在載入後才長大", "模型剛載入時用量不高，處理長提示時上下文快取才撐開；只看當下用量放行，稍後就會出事。"),
    ], size=13.5, sp=1.2, gap=0.16)
    d.panel(s, 7.25, 1.50, 3.83, "自有機群紀錄", [
        "2026年10月2日：AI 395系統記憶體可用1 GiB／31 GiB，置換空間用掉25 GiB，負載7.7，每段處理67秒。",
        "更早以前：記憶體耗盡導致整台卡死，不只一次需到機房重開。",
        "2026年10月20日：裝上xConsole後再載一顆64 GiB模型，試算剩3.2 GiB，低於15 GiB底線而擋下。"], h_min=4.3)
    d.goldline(s, "要防的是「載下去之後最壞剩多少」，不是「現在用了多少」。")
    d.notes(s, "用自己的紀錄說明風險，技術長會比較相信踩過的數字。",
            "10月2日數字出自機群維運紀錄（free -g、uptime）；10月20日出自xConsole准入試算回應。")

    # 4 全景
    s = page("系統全景：誰在用、能做什麼、跑在哪裡")
    d.status_inline(s, [("done", "AI 395與Jetson已在正式環境"), ("verify", "DGX Spark待實機")])
    d.figure(s, FIG / "fig-overview.png")
    d.notes(s, "一頁講完，後面每頁是其中一格的展開。主控台是唯一的網頁入口；同仁不登入主控台，拿網址與金鑰直接打模型。",
            "能力卡的8種範例：VS Code的GitHub Copilot、其他IDE通用欄位、OpenCode、Continue、Cline／Roo Code、Claude Code、curl、Python。")

    # 5 元件
    s = page("元件與執行方式：三個程序，全部以一般使用者執行", "不需要root、不需要容器；依賴只有cryptography一套")
    d.table(s, 0.68, 1.50, [2.0, 4.3, 2.2, 1.9],
            ["元件", "做什麼", "執行方式", "資源上限"],
            [["主控台", "網頁介面與API、准入彙整、憑證管理、稽核與取樣、使用報告", "systemd使用者服務，埠9400", "記憶體512 MiB、CPU 40%"],
             ["節點代理", "每台一個：讀硬體、准入試算、啟停服務、換模型、模型庫、下載", "systemd使用者服務，埠9401", "記憶體256 MiB、CPU 25%"],
             ["推論引擎", "Strata（Qwen3.8家族）或llama.cpp（其他GGUF），由代理產生設定與服務", "systemd使用者服務，埠依欄位", "由准入試算把關"],
             ["資料", "SQLite（稽核、取樣、登入票證）、設定檔、憑證與主金鑰", "主控台的~/.xconsole，目錄0700、檔案0600", "取樣與稽核保存400天"]],
            size=12.5)
    y = d.paras(s, 0.68, 4.55, 10.4, [
        ("技術選擇", "主控台與代理只用Python標準函式庫的HTTP伺服器，加上cryptography處理憑證與加密；前端是無框架的原生JavaScript，"
         "沒有建置步驟。依賴少，SBOM只有四個套件，弱點面小。"),
    ], size=13, sp=1.18)
    d.goldline(s, "依賴越少，要追的弱點越少。")
    d.notes(s, "強調不需要root與容器，對既有機器的侵入性低。",
            "資源上限出自deploy_console.sh與deploy_agent.sh的systemd設定；SBOM見手冊/資安證據/sbom-runtime.cdx.json（cffi、cryptography、pip、pycparser）。")

    # 6 部署型態
    s = page("部署型態：單機附在AI機上，多機選一台當主控台")
    d.status_inline(s, [("done", "單機與多機都在用"), ("done", "三台機器正式納管")])
    d.figure(s, FIG / "fig-deploy.png")
    d.notes(s, "賣一台時主控台直接附在那台；之後機器變多，各機的欄位登錄與代理不用重做。",
            "目前多機模式：主控台在.27，納管.26、.27、.20。單機模式腳本deploy_standalone.sh。")

    # 7 三種記憶體架構
    s = page("三種記憶體架構分開計算")
    d.status_inline(s, [("done", "unified_split、unified_single、discrete"), ("verify", "DGX Spark實機")])
    d.figure(s, FIG / "fig-memory.png")
    d.notes(s, "這是整套系統最核心的設計。AI 395要分兩池看，系統那池才是會拖死整台的；Jetson與DGX Spark要合併看。",
            "Jetson與GB10的nvidia-smi在memory.total回[N/A]，所以單一池機器改讀/proc/meminfo。底線為預設值，可在各機登錄檔覆寫。")

    # 8 准入演算法
    s = page("准入演算法：每次啟動、換模型、調大上下文前都會跑")
    d.status(s, 1.45, [("done", "agent/test_admit.py 15項：三種架構、KV兩種量級、底線邊界、滑動視窗乘並行")])
    y = d.paras(s, 0.68, 1.95, 6.4, [
        ("輸入", "各池可用量（由硬體量測）、欄位的預估需求（權重＋常駐量）、最壞情況的上下文快取＝每token位元組×上下文×並行數。"),
        ("算回自己", "重啟正在跑的欄位時，先把它目前占的量加回可用量；跨引擎切換時，舊引擎是另一個服務，另以extra_free算回。"),
        ("判斷", "任何一池在載入後低於底線就拒絕；距底線4 GiB內放行但提出警告。"),
        ("輸出", "allow、reasons（白話說明哪一池差多少）、warnings、kv_note；管理者可選擇強制執行，系統留下稽核紀錄。"),
    ], size=13, sp=1.16, gap=0.12)
    d.table(s, 7.35, 1.95, [1.75, 1.95], ["架構", "預設底線"],
            [["unified_split", "系統15 GiB、顯存2 GiB"], ["unified_single", "單一池12 GiB"], ["discrete", "系統8 GiB、顯存1 GiB"]],
            size=12.5)
    d.goldline(s, "擋下來時說得出差多少，管理者才知道要停哪個、調哪裡。")
    d.notes(s, "強調「算回自己」這一條是踩過的：沒算回時，重啟一個本來跑得好好的模型會被自己擋住。",
            "常數出自agent/xconsole_agent.py的DEFAULT_FLOORS與admit()；警告門檻為底線＋4 GiB。")

    # 9 KV 估算
    s = page("上下文快取怎麼估：兩種引擎、差好幾個數量級")
    d.status(s, 1.45, [("done", "Strata以實測校正；llama.cpp由GGUF檔頭計算，真機冒煙測試驗證過讀取")])
    y = d.paras(s, 0.68, 1.95, 10.4, [
        ("Strata（Qwen3.8-Flash-Next）", "GDN線性混合注意力，int8下每token只要1,056位元組。實測把上下文由32,768調到131,072，"
         "載入後剩餘顯存由33,026～33,416 MiB變為33,094 MiB，幾乎不變。"),
        ("llama.cpp（GGUF）", "直接讀檔頭的層數、每層KV頭數與頭維度：每token位元組＝Σ每層KV頭數×(K＋V)×2；"
         "混合架構的SSM層KV頭數為0自然不計；滑動視窗層只計min(上下文, 視窗)，兩部分都乘並行數。"),
        ("實例", "SmolLM2-135M：30層、每層3個KV頭、頭維度64，算得每token 23,040位元組，與手算一致。"),
        ("校正", "模型載入成功後，代理自動量一次實際用量存起來；下次換回這顆，估算優先用實測值。"),
    ], size=13, sp=1.16, gap=0.12)
    d.goldline(s, "同樣128K上下文，線性注意力0.13 GiB，全注意力可能幾十GiB，不能套同一個數字。")
    d.notes(s, "技術長常問的問題：你們怎麼知道一顆模型要多少記憶體。答案是兩種來源：檔頭算＋載入後實測校正。",
            "Strata數字出自2026年10月19日.27的載入紀錄；SmolLM2數字出自smoke_real_llama.py與本機解析（30×3×128×2）。")

    # 10 角色欄位與服務狀態
    s = page("角色欄位與服務狀態：不把狀態壓成開或關")
    d.status(s, 1.45, [("done", "Jetson AGX Thor與兩台AI 395的角色欄位在正式環境運作")])
    y = d.paras(s, 0.68, 1.95, 5.7, [
        ("欄位登錄", "每台的~/.xconsole/registry.json：角色、systemd服務名、引擎種類、預估需求、能力標籤、是否為關鍵服務。登錄檔只能經SSH寫入，沒有API可改。"),
        ("狀態", "直接取systemd的真實狀態：active、activating、deactivating、failed；設了Restart=的服務退出時會停在activating，要再看SubState是否為auto-restart。"),
        ("速度與用量", "常駐記憶體取服務所有程序的RSS；速度取自引擎每次請求留下的紀錄，累積成區間，不是規格值。"),
    ], size=13, sp=1.16, gap=0.12)
    d.shot(s, "shot-node20-slots.png", "正式環境：Jetson AGX Thor的角色欄位", 6.55, 1.95, 4.55, 3.95)
    d.goldline(s, "卡在啟動中的服務，壓成「關」或「開」都會誤導判斷。")
    d.notes(s, "說明為什麼登錄檔不開放API寫入：若能改登錄檔，拿到代理權杖的人就能把記錄檔指到私鑰再讀出來。",
            "auto-restart的處理是2026年10月22日測試抓到後加上的（壞模型原本會讓還原白等到逾時）。")

    # 11 換模型（Strata）
    s = page("換模型的流程：換一整份設定，打得通才算成功")
    d.status(s, 1.45, [("done", "失敗自動還原：整合測試與正式環境都驗證過")])
    d.table(s, 0.68, 1.95, [0.6, 2.4, 7.4], ["步", "動作", "細節"],
            [["1", "只接受掃描清單", "Strata要pack（含tokenizer）、權重與strata-<名稱>.json三樣齊；GGUF要分片齊全、檔頭讀得出"],
             ["2", "備份與組設定", "以該模型自己的設定為底，只沿用跟機器有關的項目，以及跟著欄位走的金鑰、輸出上限、自訂名稱"],
             ["3", "准入試算", "用要換過去那顆的大小算，不沿用舊模型的估值"],
             ["4", "重啟", "reset-failed後restart；跨引擎時先停用舊服務並取消開機自啟，新服務設成開機自啟"],
             ["5", "驗證", "等埠回得出/v1/models且模型名稱在清單內（設了金鑰時401也算服務活著）"],
             ["6", "失敗還原", "寫回原設定、還原開機自啟狀態、重啟原服務並再驗一次；回報原因"]],
            size=12.5)
    d.goldline(s, "判斷成功看的是模型真的回得出清單，不是指令有沒有回傳成功。")
    d.notes(s, "強調第5步：Type=simple的服務只要被接受就回0，載入失敗是幾秒後才發生，只看指令回傳值會誤以為成功。",
            "流程出自agent/xconsole_agent.py的set_model、set_llama_model、back_to_strata；逾時：Strata 240秒、llama.cpp 900秒。")

    # 12 llama.cpp 引擎
    s = page("llama.cpp引擎：代理替欄位產生設定與服務")
    d.status(s, 1.45, [("done", "test_llama_engine.py 36項＋真機冒煙測試12項（真的llama-server）")])
    y = d.paras(s, 0.68, 1.95, 6.0, [
        ("設定與服務", "每個欄位一份~/.xconsole/engines/<欄位>-llama.json（0600）與xc-<欄位>-llama.service；"
         "服務的進入點是xconsole_agent.py --launch-llama，讀設定後exec成llama-server，不多一層程序。"),
        ("選哪支llama-server", "登錄檔machine.llama_server優先；否則挑家目錄下版本號最大、有GPU後端（HIP、CUDA、Vulkan）的那支。"),
        ("機器專屬參數", "需要額外參數或環境變數時寫在machine.llama_args與machine.llama_env，不改程式。"),
    ], size=13, sp=1.16, gap=0.12)
    d.panel(s, 6.95, 1.95, 4.13, "固定帶上的參數", [
        "-c 上下文×並行、-np 並行、-ngl 999",
        "--alias 欄位的模型名稱、--jinja",
        "--no-mmap：統一記憶體機開mmap會多吃一份頁快取",
        "--api-key-file：金鑰用檔案交給引擎，不放在指令列",
        "-n 單次輸出上限（有設才帶）"], h_min=3.6)
    d.goldline(s, "金鑰在檔案裡，ps看不到。")
    d.notes(s, "說明為什麼要用代理產生服務而不是讓客戶自己寫systemd：參數與金鑰處理容易出錯，而且換模型時要能整組還原。",
            "冒煙測試在.27以隔離HOME與隔離欄位進行，用100 MB的SmolLM2，跑完刪除，不碰正式欄位。")

    # 13 跨引擎切換
    s = page("跨引擎切換：同一個欄位、同一個埠，IDE網址不用改")
    d.status(s, 1.45, [("done", "Strata→GGUF、GGUF→GGUF、GGUF→Strata、壞模型還原都有測試")])
    y = d.paras(s, 0.68, 1.95, 10.4, [
        ("切過去", "Strata→llama.cpp：停掉Strata服務並取消開機自啟，llama.cpp服務接手同一個埠並設成開機自啟；Strata的服務與設定記在欄位的engines裡，之後切得回來。"),
        ("跟著欄位走", "API金鑰、單次輸出上限、使用者自訂的模型名稱都隨欄位帶過去；llama.cpp那邊清掉的輸出上限，切回Strata時也一併清掉。"),
        ("能力標籤", "「看圖」依新模型是否帶mmproj重算，不沿用上一顆。"),
        ("失敗", "任何一步失敗就整組還原：引擎、服務的開機自啟與開關狀態、設定檔、金鑰檔都回到原樣。"),
        ("限制", "96 GiB的AI 395裝不下兩顆大模型，跨引擎時無法先在別的埠驗證再切，必須先停舊的釋放記憶體。"),
    ], size=13, sp=1.16, gap=0.12)
    d.goldline(s, "開機自啟一起換，不然重開機後兩個引擎會搶同一個埠。")
    d.notes(s, "這頁回答「換到Gemma 4怎麼辦」：Strata只跑Qwen3.8一家，其他模型走llama.cpp，在同一個欄位切換。",
            "審查意見修正（PR #5）：同一服務換模型時不再重複算回記憶體；失敗時照原狀態還原開機自啟。")

    # 14 模型庫
    s = page("模型庫：存放規則、下載與刪除")
    d.status(s, 1.45, [("done", "假Hugging Face整合測試＋真Hugging Face冒煙測試（105 MB約6秒下載完）")])
    y = d.table(s, 0.68, 1.95, [2.0, 8.4], ["項目", "做法"],
                [["存放規則", "<存放區>/<資料夾>/<模型>.gguf；預設~/models/gguf，可由machine.gguf_root改到別顆碟；分片齊才列入，mmproj放同資料夾"],
                 ["多個量化", "同一資料夾的多個量化各算一顆；分片不齊或檔頭讀不到的，清單會寫出原因"],
                 ["下載", "列出repo的GGUF並依量化分組；推測解碼用的輔助檔標出並排到最後；一次只下載一個"],
                 ["續傳與核對", "8 MB分段寫入.part，中斷後以Range接續，大小核對才改名；資料夾名稱為「作者--模型」"],
                 ["權杖與磁碟", "Hugging Face權杖只在下載期間放在記憶體，不寫任何檔案；下載前檢查剩餘空間並保留10 GiB"],
                 ["刪除", "只刪存放區第一層資料夾內的GGUF；有欄位正在使用的不准刪；Strata家族不從這裡刪"],
                 ["中國來源", "架構或名稱含qwen、deepseek、glm、kimi、minimax、hunyuan、internlm、baichuan、doubao、ernie等即標示"]],
                size=12.5)
    d.goldline(s, "放錯位置的模型，清單會寫出缺什麼，不會默默消失。")
    d.notes(s, "說明我們替客戶擋掉的常見錯誤：分片不齊、檔頭讀不到、輔助檔誤當成量化。",
            "資料夾帶作者名稱是PR #5審查意見修正，避免不同作者的同名repo互相覆蓋。")

    # 15 API 串接
    s = page("API串接：設定、驗證、範例與實測")
    d.status(s, 1.45, [("done", "test_api_integration.py 23項；正式環境實測對話約1.5秒、工具呼叫約2.3秒")])
    d.table(s, 0.68, 1.95, [2.0, 8.4], ["項目", "做法"],
            [["可改的設定", "對外網址（不重啟）；模型名稱、上下文、單次輸出上限、金鑰（重啟後驗證，失敗換回）"],
             ["輸入驗證", "名稱限英數與._-:/且1～80字；網址限http(s)；上下文2,048～1,048,576；輸出上限要小於上下文"],
             ["金鑰", "256位元隨機值，只在產生的回應裡出現一次；套用後實際驗證「沒帶金鑰打對話端點回401」"],
             ["範例", "依目前設定產生8種：VS Code Copilot、其他IDE、OpenCode、Continue、Cline、Claude Code、curl、Python"],
             ["本機實測", "模型清單、沒帶金鑰被擋、一般對話、工具呼叫（給一個查時間的假工具，看模型有沒有呼叫）"],
             ["經網路實測", "從主控台打對外網址，不跟轉址；有金鑰時改打需要金鑰的對話端點驗證"]],
            size=12.5)
    d.goldline(s, "llama.cpp的模型清單本來就公開，驗金鑰一定要打對話端點。")
    d.notes(s, "說明實測為什麼分本機與經網路兩段：本機通只證明服務活著，經網路通才證明同仁的電腦連得到。",
            "實測秒數出自2026年10月22日正式環境「實測一次」的結果（.27主腦）。")

    # 16 情境與記錄
    s = page("情境切換與記錄檢視")
    d.status(s, 1.45, [("done", "寫程式後端、抓料清洗兩個情境在AI 395上使用")])
    y = d.paras(s, 0.68, 1.95, 10.4, [
        ("情境", "同一顆模型用途不同時參數不同：寫程式的用戶端一輪送四萬token以上，要長上下文；抓料清洗每段只有幾千token，把並行讓出來換吞吐。"
         "情境寫在登錄檔profiles，切換時先備份設定、寫入參數、重啟，失敗還原上一版。情境只套用在Strata欄位，llama.cpp欄位會略過並說明。"),
        ("記錄", "選一台機器、一個服務，直接看systemd記錄與引擎自己的記錄檔；區塊固定高度在區塊內捲動，不讓頁面無限延長。"),
    ], size=13, sp=1.2, gap=0.16)
    d.table(s, 0.68, 4.25, [2.4, 2.2, 1.6, 4.2], ["情境", "上下文", "並行數", "用途"],
            [["寫程式後端", "131,072", "1", "GitHub Copilot等一次送大量程式碼脈絡"],
             ["抓料清洗", "32,768", "2", "網頁抓取後的資料清洗，每段提示較短"]], size=12.5)
    d.goldline(s, "情境是一組參數，切換是一個按鈕，失敗是自動還原。")
    d.notes(s, "這頁通常不用多講，技術長關心的是失敗會不會留下改壞的狀態：不會。",
            "情境內容出自各機registry.json的profiles；長上下文實測：68,666 token提示加輸出上限16,384，70.6秒正常作答（2026年10月19日）。")

    # 17 安全架構
    s = page("安全架構：逐層的做法", "以資通系統防護基準中級、OWASP ASVS L2為基準")
    d.table(s, 0.68, 1.50, [1.8, 6.4, 2.2], ["層面", "做法", "測試"],
            [["傳輸", "只提供HTTPS（TLS 1.2以上），http打到同埠回308；主控台連代理也走HTTPS並驗證對方憑證", "安全測試第8節"],
             ["認證", "PBKDF2-SHA256 200,000輪加鹽；同一來源15分鐘錯5次、全部來源錯50次即擋", "安全測試第9節"],
             ["會話", "伺服器端票證、資料庫只存SHA-256；HttpOnly、Secure、SameSite=Strict；12小時", "安全測試第2節"],
             ["授權", "所有/api預設要登入；機器與欄位id只能指到登錄檔內的項目", "安全測試第1、4節"],
             ["輸入", "POST只收JSON（擋跨站表單）；6 MB上限，超過立即413；路徑一律限定在目錄內", "安全測試第3、5節"],
             ["輸出", "統一錯誤處理不回堆疊；CSP無unsafe-inline，另送COOP、COEP、CORP", "安全測試第7節"],
             ["機密", "代理權杖AES-256-GCM加密存放；模型金鑰只顯示一次；稽核寫入前遮罩", "安全測試第10節"]],
            size=12.5)
    d.goldline(s, "每一層都有對應的測試，CI每次修改都重跑。")
    d.notes(s, "這頁是安全的總表，細節在後兩頁。",
            "安全測試＝test/security/test_security.py，共52項；完整對照見手冊/資安證據/ASVS檢核表.md。")

    # 18 認證與會話
    s = page("認證與會話：被偷的票證、猜密碼、跨站請求")
    d.status(s, 1.45, [("done", "安全測試在修正前抓到keep-alive身分沿用問題")])
    y = d.paras(s, 0.68, 1.95, 10.4, [
        ("伺服器端票證", "以前是無狀態簽章票證，登出只清瀏覽器cookie，被偷走的票證12小時內照樣能用；現在票證記在伺服器端，登出、換密碼即作廢，資料庫只存雜湊。"),
        ("節流", "同一來源15分鐘內錯5次就回429，被擋期間連正確密碼也不放行，讓暴力猜測無從判斷是否猜中；不鎖帳號，避免內網任何人都能把管理者鎖在門外。"),
        ("跨站請求", "SameSite=Strict，另外POST一律只收application/json；跨站的表單只能送urlencoded、multipart與text/plain，送不進來。"),
        ("keep-alive", "同一條連線上的多個請求共用同一個處理物件，身分快取曾被下一個請求沿用：登出後在同一條連線上仍是登入狀態。現在每個請求開始時清掉，並有測試守著。"),
    ], size=13, sp=1.16, gap=0.12)
    d.goldline(s, "這個漏洞是自己寫的攻擊式測試抓到的，修正前先確認測試會失敗。")
    d.notes(s, "把keep-alive的事照實講，技術長通常會因為我們主動揭露而更信任測試。",
            "見手冊/資安證據/滲透測試自測報告.md的F1（高）與F6（中），皆已修正。")

    # 19 主控台與節點代理的信任
    s = page("主控台與節點代理之間的信任")
    y0 = d.status(s, 1.45, [("done", "沒有對方憑證就不連，不退回不驗證或明文"), ("plan", "節點端權杖改存雜湊（G2）")])
    y = d.paras(s, 0.68, y0 + 0.12, 10.4, [
        ("連線", "各機代理第一次啟動自產憑證；部署腳本經SSH取回當作信任依據存在主控台。代理送出的憑證跟當初信任的不同就拒絕連線；代理重裝後重跑部署腳本或在網頁匯入新憑證。"),
        ("權杖", "每台一把隨機Bearer權杖，只走Authorization標頭；網址帶權杖一律401。主控台端以AES-256-GCM加密存在fleet.json，啟動時自動把既有明文遷移成密文。"),
        ("代理的入口", "代理只接受登錄檔內的欄位與掃描清單內的模型；沒有改登錄檔的API，避免拿到權杖的人把記錄檔指到私鑰再讀出。"),
        ("缺口", "節點端的權杖檔目前以0600明文存放，能讀這個檔的人已擁有執行模型服務的帳號；計畫改為節點只存雜湊。"),
    ], size=13, sp=1.16, gap=0.12)
    d.goldline(s, "信任來自SSH取回的憑證，不是第一次連上就相信。")
    d.notes(s, "這頁回答「主控台被冒充怎麼辦、代理被冒充怎麼辦」。",
            "G2期限2026年12月31日（docs/THREAT_MODEL.md第6節）。")

    # 20 HTTPS 與憑證
    s = page("HTTPS與憑證：熱換、檢查、還原")
    d.status(s, 1.45, [("done", "test_tls.py 36項、test_e2e_tls.py 21項；HTTPS交付檢查缺0")])
    y = d.paras(s, 0.68, 1.95, 6.3, [
        ("熱換", "不在監聽socket上先包TLS，改在處理執行緒裡才握手，每條連線當下讀目前的憑證物件；換憑證就是換掉那個物件，不重開服務。同時先看第一個位元組，不是TLS就308轉https。"),
        ("套用與還原", "套用後實際連自己一次核對送出的憑證指紋，不符就換回；保留最近10張可一鍵還原。"),
        ("CSR", "私鑰在伺服器上產生，以AES-256-GCM加密保存；廠商只寄回憑證時自動對上那把私鑰。"),
        ("HSTS", "只有正式憑證套用後才能開；剩30、14、7、1天各提醒一次。"),
    ], size=12.5, sp=1.14, gap=0.10)
    d.panel(s, 7.25, 1.95, 3.83, "接受的格式", [
        "PFX／P12（含Windows匯出的RC2-40與3DES舊式加密）",
        "PEM、CER（DER也行）、CRT，私鑰與中繼憑證順序不拘",
        "P7B（搭配當初的私鑰或系統內產生的CSR）",
        "匯入前檢查：成對、效期、主機名稱涵蓋、憑證鏈、金鑰強度、自簽"], h_min=3.9)
    d.goldline(s, "換憑證是管理者在網頁上做的事，不必叫工程師進主機。")
    d.notes(s, "技術長常問熱換怎麼做到的：TLS握手延到處理執行緒，這也讓同一個埠能同時把http轉https。",
            "預設「使用者會打的網址」只取對外網卡的IP，避免把Docker的172.17.0.1列入涵蓋檢查讓正式憑證永遠過不了。")

    # 21 稽核與資料
    s = page("操作紀錄、取樣與資料保存")
    d.status(s, 1.45, [("done", "稽核不含密碼、金鑰與錯誤的金鑰（安全測試與整合測試皆驗證）")])
    y = d.table(s, 0.68, 1.95, [2.0, 5.2, 1.2, 2.0], ["資料", "內容", "保存", "保護"],
            [["稽核紀錄", "時間、帳號、來源IP、動作、目標、結果，與遮罩後的細節", "400天", "寫入前經redact()"],
             ["機器取樣", "每5分鐘：各池用量、GPU功耗與使用率、欄位狀態、請求計數", "400天", "不含個資"],
             ["登入票證", "票證的SHA-256、帳號、來源、到期時間", "12小時", "只存雜湊"],
             ["設定與機密", "fleet.json、TLS私鑰、CSR私鑰、主金鑰", "持續", "0700／0600，機密加密"]],
            size=12.5)
    y = d.paras(s, 0.68, y + 0.18, 10.4, [
        ("遮罩規則", "鍵名像機密的一律換成「已遮罩」（password、token、key、pem、cookie、authorization…），含PRIVATE KEY或enc:v1:的字串整段遮掉，長字串截斷；所有寫稽核的地方都經過同一支函式。"),
    ], size=13, sp=1.16)
    d.goldline(s, "稽核最常見的洞是把整包請求寫進去，我們只寫遮罩後的必要欄位。")
    d.notes(s, "保存400天符合防護基準中級建議的6個月以上。",
            "常數出自console/store.py（redact、prune keep_days=400）與xconsole_server.py（SAMPLE_EVERY=300）。")

    # 22 使用報告
    s = page("服務使用報告：數字由程式算，估算值帶著假設")
    d.status(s, 1.45, [("done", "test_usage_report.py 26項；2026年10月22日產出第一份")])
    d.table(s, 0.68, 1.95, [2.4, 4.3, 3.7], ["區塊", "回答的問題", "怎麼算"],
            [["誰在用", "多少次登入、多少次操作", "稽核紀錄"],
             ["用了多少電", "GPU晶片本期耗電度數", "取樣功耗×間隔；超過15分鐘的空窗不計"],
             ["地端處理了多少", "請求數、匯入與生成token、快取命中", "代理累計計數器取差值；變小視為重啟"],
             ["設備有沒有用到", "在線比例、池平均與峰值、低於底線次數", "每5分鐘取樣"],
             ["連線與操作安不安全", "憑證狀態、代理信任、准入擋下與強制略過", "憑證管理與稽核"],
             ["系統健不健康", "待處理事項清單", "以上全部"]], size=12.5)
    d.goldline(s, "電價與雲端單價沒設，就只列度數與token量，不自己補數字換算金額。")
    d.notes(s, "第一份報告列出三項待處理：.26系統記憶體曾低於底線、主控台仍用暫用自簽、.26的抓料瀏覽器一直停在啟動中。報告會揭露系統自己的問題。",
            "產生器scripts/report/build_usage_report.py：抓端點→畫圖→python-docx→轉PDF回填目錄頁碼→驗證。")

    # 23 品質與資安證據
    s = page("品質與資安證據")
    d.status(s, 1.45, [("done", "GitHub Actions七個作業全綠才能合併；CodeQL高風險有規則擋合併")])
    d.table(s, 0.68, 1.95, [4.6, 1.2, 4.6], ["測試", "通過", "涵蓋"],
            [["agent/test_admit.py", "15", "准入公式、KV、底線邊界、模型名稱"],
             ["console/test_tls.py＋test_e2e_tls.py", "57", "憑證匯入、熱套用、還原、轉址、經網頁走完整套"],
             ["console/test_usage_report.py", "26", "報告六塊、計數器、空窗、遮罩"],
             ["test/security/test_security.py", "52", "從攻擊者角度：票證、IDOR等價、SSRF、穿越、節流、標頭、TLS"],
             ["test/test_api_integration.py", "23", "API設定、金鑰、自動還原、不跟轉址"],
             ["test/test_llama_engine.py", "36", "模型庫、檔頭KV、跨引擎切換與還原、下載、刪除"],
             ["console/test_ui_guard.py", "5", "彈窗不准點背景關閉、Enter擋輸入法"]], size=12.5)
    d.goldline(s, "214項自動化測試加12項真機冒煙測試，掃描與SBOM弱點皆為0。")
    d.notes(s, "說明CI的七個作業：測試、CodeQL（Python與JavaScript）、gitleaks（完整歷史）、pip-audit、SBOM＋grype、ZAP baseline。",
            "掃描結果見手冊/資安證據/掃描報告/：pip-audit、grype、trivy、gitleaks皆0項（2026年10月22日）。")

    # 24 規格總表
    s = page("技術規格總表")
    d.table(s, 0.68, 1.50, [2.6, 7.8], ["項目", "規格"],
            [["執行環境", "Python 3.10以上（正式環境3.12）、systemd使用者服務；主控台以venv執行，依賴釘版本"],
             ["依賴", "cryptography 50.0.2（含cffi 2.1.1、pycparser 3.11）；報表產生器另需matplotlib、python-docx"],
             ["埠", "主控台9400（HTTPS，同埠http轉址）；節點代理9401（HTTPS）；模型依欄位"],
             ["推論引擎", "Strata 0.1.45（Qwen3.8-Flash-Next家族）；llama.cpp（自動挑本機最新、有GPU後端的建置）"],
             ["已驗證的機器", "AMD Ryzen AI 395（HP Z2 Mini G1a，ROCm 7.1.1）×2、Jetson AGX Thor（JetPack 7.2）×1"],
             ["瀏覽器", "以Chromium系瀏覽器（Chrome、Edge）實測；版面在1440、1280與375寬都量過"],
             ["原始碼", "GitHub公開倉庫guessleej/xConsole，main分支受保護"]], size=12.5)
    d.goldline(s, "版本與機型都寫實際用的，沒驗過的不列。")
    d.notes(s, "DGX Spark不在已驗證清單，准入公式已過自我檢查但未接實機。",
            "版本出自requirements.txt、各機實際環境（2026年10月）與CLAUDE.md機群紀錄。")

    # 25 已知限制
    s = page("已知限制：照威脅模型缺口清單照實寫")
    d.table(s, 0.68, 1.50, [3.0, 4.6, 2.8], ["項目", "目前狀況", "影響與因應"],
            [[("模型API走http（G1）", None, True), "推論引擎本身不提供TLS，金鑰與提示在內網明文傳輸", "畫面明示；計畫由代理加一層HTTPS"],
             [("節點端權杖明文（G2）", None, True), "~/.xconsole/token以0600存放", "計畫改為只存雜湊"],
             [("管理者無兩步驟驗證（G3）", None, True), "單一管理者帳號，以節流與換密碼作廢補強", "政府案可依要求補上"],
             [("第三方滲透測試（G4）", None, True), "目前是開發團隊自測加自動化掃描", "依合約安排"],
             ["只有一種角色", "沒有唯讀或分權帳號", "主控台只給管理者用"],
             ["單一GPU的顯存歸屬", "無法逐程序區分，依各欄位預估比例分攤", "多模型並存時為估計值"],
             ["工具呼叫看模型", "llama.cpp的工具呼叫取決於模型的對話範本", "用「實測一次」確認"],
             ["DGX Spark", "公式已自我檢查，未接實機", "待實機驗證"]], size=12.5)
    d.goldline(s, "技術長最需要的是誠實的限制清單。")
    d.notes(s, "逐條講，不要跳過。G1是最常被問的，說清楚期限與做法。",
            "G1～G4出自docs/THREAT_MODEL.md第6節，期限：G1、G2為2026年12月31日，G3為2027年3月31日，G4依合約。")

    # 26 路線圖
    s = page("路線圖：已排定的補強", "以下皆為規劃中，尚未實作")
    rows = [("2026年12月31日", "模型API改走HTTPS", "節點代理加TLS反向代理，沿用代理憑證與網頁換憑證流程，IDE改打https"),
            ("2026年12月31日", "節點端權杖只存雜湊", "部署腳本產生權杖後只交給主控台，節點只留雜湊比對"),
            ("2026年12月31日", "設定與資料加密備份", "~/.xconsole以公鑰加密納入機群備份"),
            ("2027年3月31日", "管理者兩步驟驗證", "TOTP；交付政府案前完成或以客戶網段限制替代"),
            ("待排", "DGX Spark實機驗證", "接上實機確認單一池計算與nvidia-smi的回報")]
    y = 1.55
    for date, name, desc in rows:
        d.rr(s, 0.68, y, 10.4, 0.80, LGREY, rad=0.08, name="card")
        label, fill, fg = STATUS["plan"]
        T(s, 0.95, y, 1.9, 0.80, date, size=13, color=RED_D, bold=True, anchor=MSO_ANCHOR.MIDDLE)
        T(s, 2.95, y, 2.6, 0.80, name, size=13.5, color=CHAR, bold=True, anchor=MSO_ANCHOR.MIDDLE)
        T(s, 5.55, y, 5.35, 0.80, desc, size=12.5, color=SLATE, anchor=MSO_ANCHOR.MIDDLE, sp=1.1)
        y += 0.90
    d.goldline(s, "有日期的才叫路線圖。")
    d.notes(s, "強調這些都是規劃中，不要講成已完成。",
            "期限出自docs/THREAT_MODEL.md第6節與手冊/資安證據/ASVS檢核表.md的待補彙整。")

    # 27 導入需求
    s = page("導入需求：貴單位要準備什麼")
    d.table(s, 0.68, 1.50, [2.6, 7.8], ["項目", "需求"],
            [["機器", "要納管的AI機（AMD AI 395、Jetson／MIC-743、DGX Spark或多卡伺服器），Ubuntu（已驗證24.04）"],
             ["帳號", "每台一個一般使用者帳號，可用systemd使用者服務；部署時需要SSH連線"],
             ["網路", "瀏覽器能連主控台9400；主控台能連各機9401；同仁的電腦能連模型的埠"],
             ["憑證", "正式憑證或內部CA；沒有時先用暫用自簽，之後在網頁上換"],
             ["模型", "要提供給同仁的模型清單；政府、學校、醫院案只安裝非中國來源的模型"],
             ["人", "主控台管理者一位；資安聯絡窗口一位"]], size=12.5)
    d.goldline(s, "不需要另外買管理機，主控台可以裝在其中一台AI機上。")
    d.notes(s, "這頁可以直接拿去跟客戶的機房與網路負責人對。",
            "埠號出自deploy_console.sh與deploy_agent.sh；Ubuntu版本依目前已驗證的三台機器。")

    # 28 導入步驟
    s = page("導入步驟：四個指令，每一步都有驗證")
    d.table(s, 0.68, 1.50, [0.6, 2.8, 4.4, 2.6], ["步", "做什麼", "怎麼做", "驗證"],
            [["1", "裝節點代理", "每台執行deploy_agent.sh（第一次加--seed寫入欄位登錄）", "代理的健康檢查以HTTPS回應"],
             ["2", "裝主控台", "deploy_console.sh：建venv、組納管清單、取回各機代理憑證", "http打主控台回308"],
             ["3", "設帳密", "xconsole_server.py --set-credentials，帳密由標準輸入讀，不進指令列與環境變數", "網頁登入"],
             ["4", "換憑證與交付", "網頁上傳正式憑證；跑安全、HTTPS、機密與使用報告四項檢查", "四項檢查皆通過"]],
            size=12.5)
    d.goldline(s, "單機模式用deploy_standalone.sh一行完成。")
    d.notes(s, "說明帳密不走環境變數與指令列的理由：那兩個地方會被ps、/proc/<pid>/environ與shell歷史留下。",
            "四項檢查：check-compliance.sh、check-https.sh、audit-secrets.sh、check-usage-report.sh，2026年10月22日皆通過。")

    # 29 Thank You
    s = d.add("頁首")
    d.rect(s, 4.48, 4.85, 0.18, 1.65, WHITE)
    T(s, 4.62, 4.70, 8.0, 0.6, "Thank You", size=30, color=CHAR, bold=True)
    T(s, 4.62, 5.42, 8.2, 1.1, [("xConsole 模型主控台　技術簡報\n", SLATE, False),
                                ("云碩科技股份有限公司　xCloudinfo Corp. Limited\n", CHAR, True),
                                ("資安聯絡窗口　security@cloudinfo.com.tw", GREY, False)], size=12.5, sp=1.25)
    d.notes(s, "收尾提出下一步：一次實機展示，當場多載一顆模型看它被擋、換一次模型看它驗證與還原；並約定會後提供的文件（威脅模型、ASVS與SSDF對照表、SBOM、自測報告）。",
            "資安聯絡信箱出自SECURITY.md與SECURITY_PLAN第3節。價格不在本簡報，另行提供。")
