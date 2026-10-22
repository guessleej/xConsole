# 資安政策與弱點通報（Security Policy）

## 通報管道

發現 xConsole 的安全弱點，請寄 **security@cloudinfo.com.tw**。請附：受影響的版本或 commit、受影響的功能、重現步驟、影響評估。
請勿在公開 issue 揭露弱點細節；請勿對他人正在使用的系統進行破壞性測試或存取不屬於自己的資料。

## 我們的承諾

| 階段 | 時限 |
|---|---|
| 收到通報回覆確認 | 3 個工作天內 |
| 初步評估與嚴重度分級（CVSS） | 7 天內 |
| 修補：Critical | 7 天內 |
| 修補：High | 30 天內 |
| 修補：Medium | 90 天內 |
| 修補：Low | 下一個例行版本 |
| 修補完成通知通報者 | 修補上線後 3 個工作天內 |

涉及機關資通安全事件者，我們配合客戶資安窗口於知悉後 1 小時內提供通報所需資訊。

## 支援版本

只有 `main` 分支的最新版本接受安全修補。已部署的機器請重新執行 `deploy_agent.sh` 與 `deploy_console.sh` 升級。

## 安全開發與供應鏈

xConsole 依云碩 SSDLC 標準開發，對應 NIST SSDF（SP 800-218）、OWASP ASVS L2 與資通系統防護基準中級。每次 PR 與每週排程自動執行：全部測試（含 `test/security/` 安全測試）、SAST（CodeQL，Python 與 JavaScript）、機密掃描（gitleaks，含完整 git 歷史）、依賴弱點（pip-audit）、CycloneDX 格式 SBOM（原始碼與執行環境各一份）與 grype 弱點掃描、DAST（OWASP ZAP baseline）。設計文件見 `docs/SECURITY_PLAN.md` 與 `docs/THREAT_MODEL.md`，交付證據見 `手冊/資安證據/`。

## 致謝

對負責任揭露的通報者，經同意後列名於發行說明。
