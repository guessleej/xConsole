# xConsole Sales Kit

給業務與客戶決策者看的簡報，以及給客戶技術長看的技術簡報。兩份共用同一組 Visio 風藍色圖與真實系統截圖。

| 檔案 | 給誰 | 頁數 |
|---|---|---|
| `xConsole-SalesKit-通用.pptx`／`.pdf` | 不分產業的決策者 | 23 |
| `xConsole-SalesKit-企業.pptx`／`.pdf` | 一般企業（工程團隊的寫程式模型） | 23 |
| `xConsole-SalesKit-政府.pptx`／`.pdf` | 政府機關（模型來源、操作紀錄、合規） | 23 |
| `xConsole-SalesKit-學校.pptx`／`.pdf` | 學校（研究室與 AI 教室） | 23 |
| `xConsole-SalesKit-醫院.pptx`／`.pdf` | 醫院（金鑰與存取紀錄、資料不出院） | 23 |
| `技術長版/xConsole-技術簡報.pptx`／`.pdf` | 客戶的技術長、資訊長、架構師 | 29 |

每一頁的 PowerPoint 備忘稿都有講稿：Sales Kit 是四項業務講稿（解什麼問題、話術金句、可反問客戶、常見疑問應對），
技術長版是講法導引與口徑註記（數字出處、哪些話不能說過頭）。放映時內容依序進場，圖按一下出現一層。

## 重建

在 repo 根目錄執行（兩行各自獨立，可以只跑其中一行）：

```bash
(cd tools/saleskit && /opt/homebrew/bin/python3.14 build_saleskit.py --industry all)
(cd tools/techdeck && /opt/homebrew/bin/python3.14 build_techdeck.py)
```

前置條件：python-pptx、LibreOffice（`soffice`）、rsvg-convert，以及云碩內部的三組簡報素材——
公司簡報模板與動畫模組（`~/.claude/skills/xcloud-deck-build/assets/`）、圖示庫（`~/.claude/skills/arch-diagram-style/`）、
Visio 風圖產生器（`~/.claude/skills/xcloud-visio-diagram/`）。這三組含公司識別與授權圖示，不放進本公開 repo，
所以 repo 外的人無法重建簡報，只能使用已產出的 pptx 與 pdf；缺任何一組時，建置腳本會直接列出缺哪一個路徑。

文案在 `tools/saleskit/content.py`（產業差異）與 `pages.py`（共用頁）、技術長版在 `tools/techdeck/pages_tech.py`；
圖在 `diagrams.py`、`diagrams_tech.py`。建置時會先跑自動檢查（超出頁面、色條、壓到 logo、字級、圈碼與 emoji、
中英數間空白、文字溢出、每頁動畫），沒過就不產檔；`QA/` 是轉 PDF 後的全頁總覽圖。

截圖原檔在 `圖/截圖/原始/`，是 2026 年 10 月 22 日在正式環境以元素座標截取的真實畫面。
數字一律是實測值：214 項自動化測試、52 項攻擊式安全測試、12 項真機冒煙測試為同日在 .27 實跑的通過項計數。
