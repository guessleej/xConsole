# xConsole　OWASP Top 10:2025 對照表

> 版本：v1.0　日期：2026/10/22　由 `ASVS檢核表.md` 抽出，兩份內容一致；有出入以 ASVS 檢核表為準。

| # | 類別 | 已實作的控制 | 證據 | 剩餘風險 |
|---|---|---|---|---|
| A01 | 存取控制失效 | 所有 `/api/*` 預設要登入；機器與欄位 id 只能指到登錄檔內的項目；換模型只接受掃描清單；代理沒有改登錄檔的入口 | 安全測試第 1、4 節（23 支 API 未登入 401、IDOR 等價 404） | 只有單一管理者角色，未分權 |
| A02 | 安全設定錯誤 | CSP、X-Frame-Options、COOP、Permissions-Policy、nosniff；不公布版本號；錯誤不回堆疊；沒有預設帳密 | 安全測試第 7 節；CI ZAP baseline | `style-src 'unsafe-inline'` |
| A03 | 軟體供應鏈失效 | 依賴全部釘版本；pip-audit 與 grype 擋 high 以上；Dependabot 每週；Actions 釘 commit SHA；SBOM 兩份 | `sbom-*.cdx.json`、`掃描報告/pip-audit.txt`、`grype-*.txt` | 無映像簽章（不以容器交付） |
| A04 | 加密失效 | TLS 1.2+；AES-256-GCM 加密落地；PBKDF2-SHA256 200,000 輪；票證只存雜湊 | 安全測試第 8、10 節；`check-https.sh` 缺 0 | 模型 API 為 http（G1，2026/12/31） |
| A05 | 注入 | SQLite 參數化；命令參數白名單＋`shlex.quote`；前端只用 textContent；CodeQL | CI CodeQL（Python、JavaScript） | — |
| A06 | 不安全設計 | 威脅模型 STRIDE；記憶體准入控制；改設定後實際驗證、打不通自動還原 | `docs/THREAT_MODEL.md`；整合測試「上下文調到起不來 → 自動換回」 | — |
| A07 | 認證失效 | 伺服器端票證、登出與換密碼即作廢；登入節流；cookie 三旗標 | 安全測試第 2、6、9 節 | 無 MFA（G3，2027/03/31） |
| A08 | 軟體與資料完整性失效 | 分支保護；部署只從 repo 取檔；不使用 pickle 或 eval | gitleaks、CodeQL | — |
| A09 | 日誌與告警失效 | 全部管理操作寫稽核、集中遮罩、保存 400 天；使用報告揭露被擋的登入 | 安全測試第 10 節；`服務使用報告樣本-20261022.pdf` | 無即時告警推播、無獨立日誌主機 |
| A10 | 例外處理不當 | 統一錯誤處理；超大與畸形請求立即拒絕；找不到身分一律拒絕（fail-closed）；沒有代理憑證就不連 | 安全測試「負數 Content-Length 不會卡住」「沒信任代理憑證就不連」 | — |
