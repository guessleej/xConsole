# xConsole　NIST SSDF（SP 800-218 v1.1）對照表

> 版本：v1.0　日期：2026/10/22　依據：`docs/SECURITY_PLAN.md` v1.0
> 狀態只有三種：符合／部分（寫差多少、何時補）／不適用（寫理由）。證據欄的路徑都相對於 repo 根目錄，可直接點開核對。

## PO：準備組織

| 條號 | 實務 | 專案證據 | 狀態 |
|---|---|---|---|
| PO.1.1 | 定義內部安全需求 | `docs/SECURITY_PLAN.md` 第 1、4 節 | 符合 |
| PO.1.2 | 對第三方元件的安全需求 | `requirements.txt` 釘版本；`.github/dependabot.yml`；CI 第 3 道 pip-audit、第 5 道 grype（high 以上擋） | 符合 |
| PO.1.3 | 將安全需求傳達給開發人員與供應商 | 公司 SSDLC 標準（ssdlc-compliance、secret-handling、https-certificate 三份規範）；本 repo `SECURITY.md` | 符合 |
| PO.2.1 | 定義 SSDLC 角色與責任 | `docs/SECURITY_PLAN.md` 第 3 節 | 符合 |
| PO.2.2 | 提供角色訓練 | 公司規範內的踩坑清單；本 repo `README.md` 最後一節 | 部分：單人專案，無正式訓練紀錄；第二位開發者加入時補 |
| PO.2.3 | 管理層承諾 | 公司政策將 SSDLC、機密防護、HTTPS 列為同級強制項 | 符合 |
| PO.3.1 | 選定工具鏈 | `.github/workflows/security.yml`（CodeQL、gitleaks、pip-audit、syft、grype、ZAP） | 符合 |
| PO.3.2 | 遵循工具鏈安全使用建議 | workflow `permissions: contents: read`；所有 action 釘 commit SHA；gitleaks 下載後核對官方 checksum | 符合 |
| PO.3.3 | 工具產生證據 | GitHub Actions artifacts（SBOM、gitleaks、ZAP 報告保留 90 天）；本資料夾 `掃描報告/` | 符合 |
| PO.4.1 | 定義安全檢查準則 | `security.yml`：high 以上依賴弱點擋、gitleaks 任一命中擋、ZAP 有警告擋 | 符合 |
| PO.4.2 | 蒐集並保護檢查資訊 | 本資料夾隨版控保存；CI artifacts 保留 90 天 | 符合 |
| PO.5.1 | 分離並保護開發環境 | 分支保護（PR＋CI 全綠＋禁 force push）；正式環境機密不進版控（`.gitignore`） | 符合 |
| PO.5.2 | 強化開發端點 | `docs/SECURITY_PLAN.md` 附錄 | 部分：GitHub MFA 待帳號擁有者確認 |

## PS：保護軟體

| 條號 | 實務 | 專案證據 | 狀態 |
|---|---|---|---|
| PS.1.1 | 保護程式碼不被未授權存取與竄改 | GitHub 分支保護；gitleaks 掃完整歷史 | 符合 |
| PS.2.1 | 提供驗證軟體完整性的機制 | git commit SHA 與 tag；SBOM 列出每個依賴的版本與雜湊 | 部分：不以容器交付，未做 cosign；改以 git tag 驗證（見 SECURITY_PLAN 4.3） |
| PS.3.1 | 保存每個版本的來源與資訊 | git 歷史；部署腳本只從 repo 取檔 | 符合 |
| PS.3.2 | 提供 SBOM | `手冊/資安證據/sbom-source.cdx.json`、`sbom-runtime.cdx.json`（正式機 .27 實際執行環境）；CI 每次重產 | 符合 |

## PW：產出安全軟體

| 條號 | 實務 | 專案證據 | 狀態 |
|---|---|---|---|
| PW.1.1 | 以威脅模型評估設計 | `docs/THREAT_MODEL.md` | 符合 |
| PW.1.2 | 追蹤安全需求與設計決策 | SECURITY_PLAN、THREAT_MODEL 隨版控；程式內註解寫明踩過的坑 | 符合 |
| PW.1.3 | 重用既有安全元件 | 集中遮罩 `console/store.py` 的 `redact()`；集中加密 `console/tls.py` 的 `seal()`／`unseal()`；統一錯誤處理 `Handler.handle_one_request` | 符合 |
| PW.2.1 | 設計審查 | 威脅模型 v1.0 審查；安全測試在 2026/10/22 抓到 keep-alive 身分沿用問題並修正 | 符合 |
| PW.4.1 | 取得並維護安全的第三方元件 | 執行期只依賴 cryptography（含 cffi、pycparser）；Dependabot 每週 | 符合 |
| PW.4.2 | 自建元件也套同樣標準 | 主控台與節點代理同一條 CI | 符合 |
| PW.4.4 | 驗證第三方元件符合需求 | `掃描報告/pip-audit.txt`、`grype-*.txt`、`trivy-fs.txt`（皆 0 項弱點） | 符合 |
| PW.5.1 | 遵循安全編碼實務 | `手冊/資安證據/ASVS檢核表.md` | 符合 |
| PW.6.1 | 設定建置工具的安全功能 | Python 不需編譯；部署以 venv＋釘版本依賴 | 不適用：無編譯步驟 |
| PW.6.2 | 確實啟用上述功能 | 同上 | 不適用 |
| PW.7.1 | 決定程式碼審查／分析的範圍 | CodeQL 全 repo（Python、JavaScript），security-extended 查詢集 | 符合 |
| PW.7.2 | 執行審查與分析並修正 | CI Code scanning 結果 | 符合 |
| PW.8.1 | 決定測試範圍 | 准入、憑證、使用報告、端對端、前端守衛、安全測試、API 串接整合測試，七組 | 符合 |
| PW.8.2 | 執行測試並修正 | `掃描報告/安全測試-test_security.txt`（52 項）、`API串接整合測試.txt`（23 項） | 符合 |
| PW.9.1 | 定義安全預設設定 | 第一次啟動即 HTTPS；所有 API 預設要登入；HSTS 只有正式憑證才能開 | 符合 |
| PW.9.2 | 以安全預設交付並說明如何調整 | `手冊/操作手冊/xConsole主要功能說明.md` 第二章、第五章 | 符合 |

## RV：回應弱點

| 條號 | 實務 | 專案證據 | 狀態 |
|---|---|---|---|
| RV.1.1 | 持續蒐集弱點資訊 | Dependabot 每週；CI 每週一排程重掃 | 符合 |
| RV.1.2 | 審查並確認弱點 | `SECURITY.md`：3 個工作天內回覆 | 符合 |
| RV.1.3 | 弱點揭露政策 | `SECURITY.md` | 符合 |
| RV.2.1 | 分析並排定優先 | `SECURITY.md` 修補時限：Critical 7 天、High 30 天、Medium 90 天 | 符合 |
| RV.2.2 | 修補並發布 | PR → CI 全綠 → `deploy_agent.sh`、`deploy_console.sh` | 符合 |
| RV.3.1 | 根因分析 | THREAT_MODEL 變更紀錄；程式內「實際踩到」註解 | 部分：尚未建立 `docs/incidents/`，第一件 critical／high 發生時建立 |
| RV.3.2 | 找同類問題 | 修 keep-alive 問題時一併檢查代理端（代理無身分快取） | 符合 |
| RV.3.3 | 回饋到流程 | 對齊問題寫回公司 UI 規範；健康檢查 401 問題寫進程式註解 | 符合 |
| RV.3.4 | 檢討 SSDLC 流程 | `docs/SECURITY_PLAN.md` 第 6 節年度複查（每年 10 月） | 符合 |
