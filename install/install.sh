#!/usr/bin/env bash
# xConsole 單機安裝（Linux／macOS）：主控台＋節點代理裝在這一台，裝完開瀏覽器就能用。
# 在解開的安裝包資料夾裡，用一般帳號（不要用 root 或 sudo）執行：
#
#   bash install/install.sh              安裝或更新（資料與設定都保留）
#   bash install/install.sh --uninstall  停掉並移除服務（~/.xconsole 的資料、模型與憑證保留）
#   bash install/install.sh --password-file <檔>   自動化安裝：檔案第一行帳號、第二行密碼（權限要 600，用完請刪）
#
# Linux 建 systemd 使用者服務並開 linger（沒登入也會跑）；macOS 建 launchd 的 LaunchAgent（登入後自動啟動）。
# 要把別台機器加進來，裝好後在網頁「加一台機器」產生指令，到那台貼上就好，不用再跑這支。
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
OSN=$(uname -s)
DST="$HOME/xconsole"
ST="$HOME/.xconsole"
LA="tw.xcloudinfo.xconsole-agent"
LC="tw.xcloudinfo.xconsole"
# launchd：有人在桌面登入就放 gui 網域；只有 SSH、沒有桌面登入的 Mac（含 CI）沒有 gui 網域，改放 user 網域
LDOM="gui/$(id -u)"
[ "$OSN" = Darwin ] && { launchctl print "$LDOM" >/dev/null 2>&1 || LDOM="user/$(id -u)"; }

say() { printf '\n== %s ==\n' "$1"; }
die() { printf '\n安裝停止：%s\n' "$1" >&2; exit 1; }

svc_stop() {   # 名稱（不含副檔名）
  if [ "$OSN" = Darwin ]; then
    launchctl bootout "$LDOM/tw.xcloudinfo.$1" 2>/dev/null || true
  else
    systemctl --user disable --now "$1.service" 2>/dev/null || true
  fi
}

PWFILE=""
[ "${1:-}" = "--password-file" ] && PWFILE="${2:?--password-file 後面要接檔名}"

if [ "${1:-}" = "--uninstall" ]; then
  svc_stop xconsole; svc_stop xconsole-agent
  if [ "$OSN" = Darwin ]; then rm -f "$HOME/Library/LaunchAgents/$LC.plist" "$HOME/Library/LaunchAgents/$LA.plist"
  else rm -f "$HOME/.config/systemd/user/xconsole.service" "$HOME/.config/systemd/user/xconsole-agent.service"; systemctl --user daemon-reload; fi
  echo "已移除主控台與節點代理的服務。資料保留在 ${ST}，要整個刪掉請自行刪除該資料夾與 ${DST}。"
  echo "注意：代理帶起來的模型服務（llama-server）在 Linux 是另外的 systemd 服務 xc-*-llama，請先在網頁上停掉。"
  exit 0
fi

say "1/6 檢查這台"
[ "$(id -u)" = 0 ] && die "請用一般帳號執行，不要用 root 或 sudo"
[ -f "$SRC/console/xconsole_server.py" ] && [ -f "$SRC/agent/xconsole_agent.py" ] || die "請在解開的 xConsole 安裝包資料夾裡執行（找不到 console/ 與 agent/）"
case "$OSN" in
  Darwin)
    python3 -c 'import ssl' 2>/dev/null || die "這台沒有 python3。請先執行 xcode-select --install 安裝命令列工具，再跑一次"
    [ "$(uname -m)" = arm64 ] || echo "提醒：這是 Intel Mac，模型只能用 CPU 跑，速度會很慢" ;;
  Linux)
    for c in python3 curl; do command -v $c >/dev/null || die "這台沒有 ${c}（Ubuntu：sudo apt install ${c}）"; done
    python3 -c 'import venv, ensurepip' 2>/dev/null || die "Python 缺少 venv（Ubuntu：sudo apt install python3-venv）"
    systemctl --user show-environment >/dev/null 2>&1 || die "這個帳號沒有 systemd 使用者服務，請用一般登入的帳號（SSH 或桌面登入）執行" ;;
  *) die "這支只給 Linux 與 macOS；Windows 請用 install\\install.ps1" ;;
esac
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "Python 版本要 3.9 以上"

say "2/6 複製程式到 $DST"
mkdir -p -m 700 "$ST" "$DST/static" "$DST/agent"
for f in xconsole_server.py tls.py store.py report.py recommend.py catalog_default.json; do cp "$SRC/console/$f" "$DST/$f"; done
cp "$SRC/console/static/"* "$DST/static/"
cp "$SRC/agent/xconsole_agent.py" "$DST/agent/xconsole_agent.py"       # 「加一台機器」要發給別台的那一份
cp "$SRC/agent/xconsole_agent.py" "$HOME/xconsole_agent.py"
cp "$SRC/requirements.txt" "$DST/requirements.txt"

say "3/6 準備 Python 環境（主控台需要 cryptography）"
[ -x "$DST/.venv/bin/python" ] || python3 -m venv "$DST/.venv"
"$DST/.venv/bin/python" -m pip install -q --disable-pip-version-check -r "$DST/requirements.txt" \
  || die "裝不了相依套件；這台要能連 pypi.org，或請 IT 先放行"
PY="$DST/.venv/bin/python"

say "4/6 管理者帳號"
if "$PY" -c "import json,os,sys; c=json.load(open(os.path.expanduser('~/.xconsole/fleet.json'))); sys.exit(0 if c.get('auth',{}).get('hash') else 1)" 2>/dev/null; then
  echo "已經設過帳號，保留原本的（要改請到網頁「設定 → 帳號」）"
else
  if [ -n "$PWFILE" ]; then
    # 密碼走檔案不走參數或環境變數：參數會出現在 ps，環境變數會出現在 /proc/<pid>/environ
    [ "$(stat -c %a "$PWFILE" 2>/dev/null || stat -f %Lp "$PWFILE")" = 600 ] || die "$PWFILE 的權限要是 600"
    (cd "$DST" && "$PY" xconsole_server.py --set-credentials < "$PWFILE")
  else
    echo "設定主控台的登入帳號與密碼（密碼至少 8 碼，不會顯示在畫面上）"
    (cd "$DST" && "$PY" xconsole_server.py --set-credentials)
  fi
fi

say "5/6 建立服務並啟動"
if [ "$OSN" = Darwin ]; then
  mkdir -p "$HOME/Library/LaunchAgents"
  plist() {   # 標籤 工作目錄 程式 記錄檔
    cat > "$HOME/Library/LaunchAgents/$1.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$1</string>
  <key>WorkingDirectory</key><string>$2</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$3</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Standard</string>
  <key>StandardOutPath</key><string>$4</string>
  <key>StandardErrorPath</key><string>$4</string>
</dict></plist>
PLIST
    launchctl bootout "$LDOM/$1" 2>/dev/null || true
    launchctl bootstrap "$LDOM" "$HOME/Library/LaunchAgents/$1.plist"
    launchctl kickstart -k "$LDOM/$1"
  }
  plist "$LA" "$HOME" "$HOME/xconsole_agent.py" "$ST/agent.log"
  AGENT_LOG="tail -n 50 $ST/agent.log；launchd 的狀態：launchctl print $LDOM/$LA"
else
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/xconsole-agent.service" <<UNIT
[Unit]
Description=xConsole 節點代理（模型欄位、准入控制、啟停）
After=network.target

[Service]
Type=simple
ExecStart=$PY %h/xconsole_agent.py
Restart=always
RestartSec=5
MemoryMax=256M
CPUQuota=25%

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable xconsole-agent.service >/dev/null
  systemctl --user restart xconsole-agent.service
  AGENT_LOG="journalctl --user -u xconsole-agent -n 50"
fi
UP=no
for i in $(seq 1 30); do
  if [ -s "$ST/token" ] && [ -s "$ST/agent-cert.pem" ] \
     && curl -fsS -m 3 --cacert "$ST/agent-cert.pem" https://127.0.0.1:9401/healthz >/dev/null 2>&1; then UP=yes; break; fi
  sleep 1
done
if [ "$UP" != yes ]; then
  [ "$OSN" = Darwin ] && launchctl print "$LDOM/$LA" 2>&1 | grep -E "state|last exit|pid|program|domain" | head -8
  die "節點代理沒有起來，請看記錄：${AGENT_LOG}"
fi
"$PY" "$SRC/install/setup_local.py" --home "$HOME"

if [ "$OSN" = Darwin ]; then
  plist "$LC" "$DST" "$DST/xconsole_server.py" "$ST/console.log"
  CONSOLE_LOG="tail -n 50 $ST/console.log"
else
  cat > "$HOME/.config/systemd/user/xconsole.service" <<UNIT
[Unit]
Description=xConsole 模型主控台
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/xconsole
ExecStart=$PY %h/xconsole/xconsole_server.py
Restart=always
RestartSec=5
MemoryMax=512M
CPUQuota=40%

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable xconsole.service >/dev/null
  systemctl --user restart xconsole.service
  CONSOLE_LOG="journalctl --user -u xconsole -n 50"
  loginctl enable-linger "$USER" 2>/dev/null || echo "提醒：這個帳號登出後服務會停。請有 sudo 權限的人執行一次：sudo loginctl enable-linger $USER"
fi
# 存活檢查打同埠的 http://：回 308（轉到 https）就代表主控台活著，不必關掉憑證驗證
UP=no
for i in $(seq 1 30); do
  [ "$(curl -s -m 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:9400/healthz)" = 308 ] && { UP=yes; break; }
  sleep 1
done
[ "$UP" = yes ] || die "主控台沒有起來，請看記錄：$CONSOLE_LOG"

say "6/6 完成"
IP=$("$PY" -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('1.1.1.1', 80)); print(s.getsockname()[0])" 2>/dev/null || echo 127.0.0.1)
echo "開瀏覽器到：https://$IP:9400"
echo "第一次會看到憑證警告（系統先用暫用自簽），登入後到「設定 → HTTPS 與憑證」換成正式憑證。"
echo "接著照總覽頁的三步：確認記憶體架構 → 一鍵安裝推理引擎 → 從推薦模型挑一顆下載。"
[ "$OSN" = Darwin ] && echo "macOS 第一次會問是否允許 python3 接受傳入連線，請按「允許」，別台電腦才連得到。"
exit 0
