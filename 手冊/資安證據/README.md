# xConsole 資安交付證據包

> 版本：v1.0　日期：2026/10/22　防護等級：中（依 `docs/SECURITY_PLAN.md`）
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
| 9 | 滲透測試（自測） | `滲透測試自測報告.md`、`掃描報告/安全測試-test_security.txt`、`掃描報告/API串接整合測試.txt` |
| 10 | 機密防護稽核 | `掃描報告/機密稽核-audit-secrets.txt` |
| 11 | 服務使用報告樣本 | `服務使用報告樣本-20261022.pdf` |

HTTPS 與憑證的交付檢查見 `掃描報告/HTTPS檢查-check-https.txt`；服務使用報告的四項檢查見 `掃描報告/使用報告檢查.txt`。

本次（2026/10/22）掃描結果：pip-audit、grype（兩份 SBOM）、trivy、gitleaks 皆 0 項；安全測試 52 項、API 串接整合測試 23 項全數通過；機密稽核 0 項高風險；HTTPS 檢查缺 0。
