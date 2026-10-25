# xConsole 資安交付證據包

> 版本：v1.1　日期：2026/10/25（v1.0 為 2026/10/22）　防護等級：中（依 `docs/SECURITY_PLAN.md`）
> 這個資料夾收的是可以被當場點開核對的證據；每份文件裡的路徑都相對於 repo 根目錄。

| # | 項目 | 位置 |
|---|---|---|
| 1 | 安全計畫（等級、資料分類、角色、信任邊界、事件應變） | `docs/SECURITY_PLAN.md` |
| 2 | 威脅模型（STRIDE，含缺口清單） | `docs/THREAT_MODEL.md` |
| 3 | NIST SSDF 對照表 | `SSDF對照表.md` |
| 4 | OWASP ASVS L2 檢核表 | `ASVS檢核表.md` |
| 5 | OWASP Top 10:2025 對照 | `OWASP-Top10對照.md` |
| 6 | SBOM（CycloneDX JSON）：原始碼、正式機實際執行環境 | `sbom-source.cdx.json`、`sbom-runtime.cdx.json` |
| 7 | 掃描報告：依賴（pip-audit）、SBOM 弱點（grype）、檔案系統（trivy）、機密（gitleaks 全部歷史與工作區）；CodeQL 與 ZAP 見 GitHub Actions | `掃描報告/` |
| 8 | 弱點通報與修補時限 | `SECURITY.md`（repo 根目錄） |
| 9 | 滲透測試（自測＋獨立審查） | `滲透測試自測報告.md`；原始輸出：`掃描報告/安全測試-test_security.txt`、`掃描報告/API串接整合測試.txt`、`掃描報告/機器管理整合測試.txt`、`掃描報告/模型庫與llama引擎整合測試.txt`、`掃描報告/准入與代理單元測試.txt` |
| 10 | 機密防護稽核 | `掃描報告/機密稽核-audit-secrets.txt` |
| 11 | 服務使用報告樣本 | `服務使用報告樣本-20261022.pdf` |

HTTPS 與憑證的交付檢查見 `掃描報告/HTTPS檢查-check-https.txt`；服務使用報告的四項檢查見 `掃描報告/使用報告檢查.txt`。

2026/10/25 更新：新增「網頁上加一台機器、機器設定、欄位、情境、編譯 llama.cpp」之後，做了一輪獨立資安審查（三次複查），找到的 13 項（高 1、中 6、低 6，同類的合併計算）都已修正並寫成測試，見 `滲透測試自測報告.md` 第二輪。九支自動化測試共 335 項於 2026/10/25 在 .27 全數通過（原始輸出在 `掃描報告/`）；機密稽核 0 項高風險；HTTPS 檢查缺 0。

依賴與 SBOM 的掃描（pip-audit、grype、trivy、gitleaks）沿用 2026/10/22 的檔案：之後沒有新增或升級任何依賴套件，每次 PR 也都由 GitHub Actions 重跑同一組掃描。
