#!/usr/bin/env bash
# xConsole 單機安裝（Linux／macOS）：主控台＋節點代理裝在這一台，裝完開瀏覽器就能用。
# 在解開的安裝包資料夾裡，用一般帳號（不要用 root 或 sudo）執行：
#
#   bash install/install.sh              安裝或更新（資料與設定都保留）
#   bash install/install.sh --uninstall  停掉並移除服務（~/.xconsole 的資料、模型與憑證保留）
#   bash install/install.sh --password-file <檔>   自動化安裝：檔案第一行帳號、第二行密碼（權限要 600，用完請刪）
#
# 大量部署（只裝節點代理、加入既有的主控台；Intune、Jamf、Ansible、映像派送都走這條），要用 root：
#   sudo bash install/install.sh --console <主控台:埠> --fingerprint <指紋> --join-code-file <回應檔> [--data-dir <資料夾>]
#   加入碼只收回應檔（擁有者 root、權限 600，讀完就刪）或標準輸入（--join-code-file -），不收指令列與環境變數。
#   Linux 建專用帳號 xconsole，開機由 systemd 帶起它的服務（linger）；macOS 建隱藏帳號 _xconsole，裝成 LaunchDaemon。
#   沒有人登入也會跑。指紋與指令在主控台「加一台機器 → 大量部署」抄；主控台換成正式憑證後可以不給指紋。
#   sudo bash install/install.sh --uninstall    移除大量部署裝的代理服務（資料保留）
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

PWFILE="" CONSOLE="" FP="" CODEFILE="" DATA="" UNINST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --password-file) PWFILE="${2:?--password-file 後面要接檔名}"; shift 2 ;;
    --console) CONSOLE="${2:?--console 後面要接主控台位址，例 192.0.2.10:9400}"; shift 2 ;;
    --fingerprint) FP="${2:?--fingerprint 後面要接主控台憑證的指紋}"; shift 2 ;;
    --join-code-file) CODEFILE="${2:?--join-code-file 後面要接回應檔，或 - 代表標準輸入}"; shift 2 ;;
    --data-dir) DATA="${2:?--data-dir 後面要接資料夾}"; shift 2 ;;
    --uninstall) UNINST=1; shift ;;
    --join-code|--join-code=*|--code|--code=*)
      # 放在指令列會留在行程清單、shell 歷史與派送紀錄，看得到的人在有效期間內就能拿去加入別的機器
      die "加入碼不能放在指令列。請改成 --join-code-file <回應檔>（擁有者 root、權限 600，讀完會刪掉），或 --join-code-file - 從標準輸入給" ;;
    *) die "不認得的參數：$1" ;;
  esac
done
[ -z "$CONSOLE" ] && { [ -n "$FP" ] || [ -n "$CODEFILE" ] || [ -n "$DATA" ]; } && die "--fingerprint、--join-code-file、--data-dir 要搭配 --console（大量部署）使用"

# ---- 大量部署：只裝節點代理，用專用帳號、開機就跑，裝好直接加入 --console 那台主控台
MU=xconsole; [ "$OSN" = Darwin ] && MU=_xconsole
mstop() {   # 停掉大量部署裝的代理服務（重裝與移除共用）
  if [ "$OSN" = Darwin ]; then
    launchctl bootout "system/$LA" 2>/dev/null || true
  elif id -u "$MU" >/dev/null 2>&1; then
    local u; u=$(id -u "$MU")
    [ -d "/run/user/$u" ] && runuser -u "$MU" -- env XDG_RUNTIME_DIR="/run/user/$u" systemctl --user disable --now xconsole-agent.service 2>/dev/null || true
  fi
}
# 專用帳號的家目錄；帳號還不存在就回空字串（不能讓 set -e／pipefail 在第一次安裝時直接中斷）
mhome() { if [ "$OSN" = Darwin ]; then dscl . -read "/Users/$MU" NFSHomeDirectory 2>/dev/null | awk '{print $2}' || true; else getent passwd "$MU" | cut -d: -f6 || true; fi; }
managed_uninstall() {
  mstop
  if [ "$OSN" = Darwin ]; then rm -f "/Library/LaunchDaemons/$LA.plist"
  else H=$(mhome); [ -n "$H" ] && rm -f "$H/.config/systemd/user/xconsole-agent.service"; loginctl disable-linger "$MU" 2>/dev/null || true; fi
  echo "已移除大量部署裝的節點代理服務。專用帳號 $MU 與資料（$(mhome)）保留；要整個刪掉請自行刪除帳號與資料夾。"
  echo "記得到主控台把這台從清單移除。"
  exit 0
}
perm_ok() {   # 回應檔：擁有者 root、群組與其他人沒有任何權限
  local o m
  if [ "$OSN" = Darwin ]; then o=$(stat -f %u "$1"); m=$(stat -f %Lp "$1"); else o=$(stat -c %u "$1"); m=$(stat -c %a "$1"); fi
  [ "$o" = 0 ] && [ $((8#$m & 8#077)) = 0 ]
}
managed() {
  [ "$(id -u)" = 0 ] || die "大量部署要用 root 執行（sudo bash install/install.sh --console …）：要建專用帳號與開機自啟"
  [ -f "$SRC/agent/xconsole_agent.py" ] && [ -f "$SRC/install/join.py" ] || die "請在解開的 xConsole 安裝包資料夾裡執行（找不到 agent/ 與 install/join.py）"
  [ -n "$CODEFILE" ] || die "缺加入碼：請給 --join-code-file <回應檔>，或 --join-code-file - 從標準輸入讀"
  local CODE=""
  if [ "$CODEFILE" = - ]; then
    IFS= read -r CODE || true
  else
    [ -f "$CODEFILE" ] || die "找不到回應檔 $CODEFILE"
    perm_ok "$CODEFILE" || { rm -f "$CODEFILE"; die "回應檔 $CODEFILE 的擁有者要是 root、權限 600（chown root $CODEFILE; chmod 600 $CODEFILE）；別人讀得到的加入碼可能已外流，已刪掉這個檔，請在主控台作廢它、重新產生"; }
    IFS= read -r CODE < "$CODEFILE" || true
    rm -f "$CODEFILE"
  fi
  [ -n "$CODE" ] || die "加入碼是空的"
  # 資料夾路徑要寫進 systemd 服務檔與 launchd 設定：空白會被 systemd 拆成別的參數，先擋掉，不要等建好帳號才失敗
  if [ -n "$DATA" ]; then
    case "$DATA" in /*) ;; *) die "--data-dir 要是絕對路徑" ;; esac
    printf '%s' "$DATA" | grep -q '^[A-Za-z0-9._/-]*$' || die "--data-dir 只能用英數字與 . _ - /（不能有空白或中文），例：/data/xconsole"
  fi

  say "1/5 檢查這台"
  python3 -c 'import ssl, venv' 2>/dev/null || die "這台沒有 python3（含 venv）。Ubuntu：apt install python3-venv；macOS：xcode-select --install"
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "Python 版本要 3.9 以上"
  if [ "$OSN" = Darwin ]; then
    for p in /Users/*/Library/LaunchAgents/$LA.plist; do
      [ -f "$p" ] && die "這台已經有人用一般帳號裝過代理（$p），兩份會搶同一個埠。請先用那個帳號執行 bash install/install.sh --uninstall"
    done
  else
    command -v runuser >/dev/null && command -v loginctl >/dev/null || die "這支需要 systemd（runuser、loginctl）"
  fi
  mstop
  (exec 3<>/dev/tcp/127.0.0.1/9401) 2>/dev/null && die "9401 埠已經有程式在用（可能是別的帳號裝過的代理），請先移除那一份"

  say "2/5 專用帳號 $MU"
  local H; H=$(mhome)
  if [ -n "$H" ]; then
    [ -z "$DATA" ] || [ "$DATA" = "$H" ] || die "這台的代理已經裝在 $H，要換位置請先 --uninstall 並搬走資料"
    DATA="$H"
  elif [ "$OSN" = Darwin ]; then
    DATA="${DATA:-/opt/xconsole}"
    local id="" i
    for i in $(seq 400 499); do
      dscl . -search /Users UniqueID "$i" | grep -q . || dscl . -search /Groups PrimaryGroupID "$i" | grep -q . || { id=$i; break; }
    done
    [ -n "$id" ] || die "找不到空的帳號代號（400～499 都被用掉了）"
    dscl . -create "/Groups/$MU" PrimaryGroupID "$id"
    dscl . -create "/Groups/$MU" Password '*'
    dscl . -create "/Users/$MU"
    dscl . -create "/Users/$MU" UniqueID "$id"
    dscl . -create "/Users/$MU" PrimaryGroupID "$id"
    dscl . -create "/Users/$MU" UserShell /usr/bin/false
    dscl . -create "/Users/$MU" NFSHomeDirectory "$DATA"
    dscl . -create "/Users/$MU" RealName "xConsole agent"
    dscl . -create "/Users/$MU" IsHidden 1
    dscl . -create "/Users/$MU" Password '*'
  else
    DATA="${DATA:-/var/lib/xconsole}"
    useradd --system --home-dir "$DATA" --shell "$(command -v nologin || echo /bin/false)" --user-group "$MU"
  fi
  printf '%s' "$DATA" | grep -q '^/[A-Za-z0-9._/-]*$' || die "這台的代理資料夾 $DATA 有空白或特殊字元，systemd 與 launchd 設定寫不進去；請先 --uninstall 再用 --data-dir 指定別的位置"
  if [ "$OSN" != Darwin ]; then
    # 顯示卡裝置（AMD 的 /dev/kfd、/dev/dri）要這兩個群組才開得了
    for g in video render; do getent group "$g" >/dev/null && usermod -aG "$g" "$MU"; done
  fi
  echo "帳號 $MU，資料放 $DATA"

  say "3/5 複製代理與 Python 環境（cryptography）"
  mkdir -p "$DATA/.xconsole"
  cp "$SRC/agent/xconsole_agent.py" "$DATA/xconsole_agent.py"
  cp "$SRC/install/join.py" "$DATA/join.py"
  [ -x "$DATA/.venv/bin/python" ] || python3 -m venv "$DATA/.venv"
  "$DATA/.venv/bin/python" -m pip install -q --disable-pip-version-check 'cryptography>=42' \
    || die "裝不了 cryptography；這台要能連 pypi.org，或請 IT 先放行"
  chown -R "$MU:$MU" "$DATA"
  chmod 700 "$DATA" "$DATA/.xconsole"
  local PYM="$DATA/.venv/bin/python"

  say "4/5 開機自啟並啟動"
  if [ "$OSN" = Darwin ]; then
    local PL="/Library/LaunchDaemons/$LA.plist"
    cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LA</string>
  <key>UserName</key><string>$MU</string>
  <key>GroupName</key><string>$MU</string>
  <key>WorkingDirectory</key><string>$DATA</string>
  <key>ProgramArguments</key><array><string>$PYM</string><string>$DATA/xconsole_agent.py</string><string>--home</string><string>$DATA</string></array>
  <key>EnvironmentVariables</key><dict><key>HOME</key><string>$DATA</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Standard</string>
  <key>StandardOutPath</key><string>$DATA/.xconsole/agent.log</string>
  <key>StandardErrorPath</key><string>$DATA/.xconsole/agent.log</string>
</dict></plist>
PLIST
    chown root:wheel "$PL"; chmod 644 "$PL"
    launchctl bootstrap system "$PL"
    launchctl kickstart -k "system/$LA"
    AGENT_LOG="tail -n 50 $DATA/.xconsole/agent.log；launchd 的狀態：sudo launchctl print system/$LA"
  else
    loginctl enable-linger "$MU"
    local u; u=$(id -u "$MU")
    for i in $(seq 1 30); do [ -S "/run/user/$u/systemd/private" ] && break; sleep 1; done
    [ -S "/run/user/$u/systemd/private" ] || die "專用帳號的 systemd 使用者服務沒有起來（loginctl enable-linger $MU 之後 /run/user/$u 還沒出現）"
    mkdir -p "$DATA/.config/systemd/user"
    cat > "$DATA/.config/systemd/user/xconsole-agent.service" <<UNIT
[Unit]
Description=xConsole 節點代理（模型欄位、准入控制、啟停）
After=network.target

[Service]
Type=simple
ExecStart=$PYM %h/xconsole_agent.py
Restart=always
RestartSec=5
MemoryMax=256M
CPUQuota=25%

[Install]
WantedBy=default.target
UNIT
    chown -R "$MU:$MU" "$DATA/.config"
    local asu=(runuser -u "$MU" -- env XDG_RUNTIME_DIR="/run/user/$u")
    "${asu[@]}" systemctl --user daemon-reload
    "${asu[@]}" systemctl --user enable xconsole-agent.service >/dev/null
    "${asu[@]}" systemctl --user restart xconsole-agent.service
    AGENT_LOG="sudo journalctl _UID=$u _SYSTEMD_USER_UNIT=xconsole-agent.service -n 50"
  fi
  local up=no
  for i in $(seq 1 40); do
    if [ -s "$DATA/.xconsole/token" ] && [ -s "$DATA/.xconsole/agent-cert.pem" ] \
       && curl -fsS -m 3 --cacert "$DATA/.xconsole/agent-cert.pem" https://127.0.0.1:9401/healthz >/dev/null 2>&1; then up=yes; break; fi
    sleep 1
  done
  [ "$up" = yes ] || die "節點代理沒有起來，請看記錄：$AGENT_LOG"

  say "5/5 加入主控台 $CONSOLE"
  local as=(sudo -u "$MU"); [ "$OSN" = Darwin ] || as=(runuser -u "$MU" --)
  # 加入碼經由管線給 join.py 的標準輸入：printf 是 shell 內建，不會出現在行程清單
  printf '%s\n' "$CODE" | "${as[@]}" "$PYM" "$DATA/join.py" --console "$CONSOLE" --fingerprint "$FP" --home "$DATA" \
    || die "加入主控台失敗（原因見上一行）。代理已經裝好在跑，在主控台重新產生加入碼後，用同一個指令再跑一次即可"
  echo "完成：這台會一直開機自動啟動，沒有人登入也會跑。回到主控台就看得到它。"
  exit 0
}
[ -n "$CONSOLE" ] && managed
[ -n "$UNINST" ] && [ "$(id -u)" = 0 ] && managed_uninstall

if [ -n "$UNINST" ]; then
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
for f in xconsole_server.py tls.py agentlink.py linkcert.py groups.py joincodes.py store.py report.py recommend.py extract.py chats.py computer.py updater.py catalog_default.json sd_catalog_default.json; do cp "$SRC/console/$f" "$DST/$f"; done
cp "$SRC/console/static/"* "$DST/static/"
cp "$SRC/agent/xconsole_agent.py" "$DST/agent/xconsole_agent.py"       # 「加一台機器」要發給別台的那一份
cp "$SRC/agent/xconsole_agent.py" "$HOME/xconsole_agent.py"
cp "$SRC/requirements.txt" "$DST/requirements.txt"
cp "$SRC/VERSION" "$DST/VERSION"

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
