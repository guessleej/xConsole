// 合約測試用的假模組畫面：在設定頁加一個分頁
window.XC && window.XC.registerPage({ key: "fake", label: "測試模組", render(wrap) { wrap.appendChild(window.XC.card("測試模組", "合約測試用")); } });
