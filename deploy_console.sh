#!/usr/bin/env bash
# 把 xConsole 主控台部署到一台機器（預設 .27），並把各節點代理串起來。
#   bash deploy_console.sh [主控台所在機器別名] [要納管的機器別名...]
# 例：bash deploy_console.sh 27 26 27
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
XS=/Users/jeff/.claude/skills/connect-xcloud-servers/scripts/xssh.sh
HOST_ALIAS="${1:-27}"; shift || true
NODES=("$@")
[ ${#NODES[@]} -eq 0 ] && NODES=(26 27)

echo "=== 傳送主控台程式 ==="
bash "$XS" "$HOST_ALIAS" 'mkdir -p $HOME/xconsole/static'
for f in xconsole_server.py tls.py store.py report.py; do
  PUT="$HERE/console/$f" DST="/home/xcloud/xconsole/$f" bash "$XS" "$HOST_ALIAS" --scp
done
for f in index.html style.css app.js; do
  PUT="$HERE/console/static/$f" DST="/home/xcloud/xconsole/static/$f" bash "$XS" "$HOST_ALIAS" --scp
done
PUT="$HERE/requirements.txt" DST="/home/xcloud/xconsole/requirements.txt" bash "$XS" "$HOST_ALIAS" --scp

echo "=== 獨立的 Python 環境（依賴版本照 requirements.txt 釘死，不吃系統套件）==="
# 系統的 python3-cryptography 跟著發行版走（Ubuntu 24.04 是 41.0.7），版本不受我們控制，
# SBOM 與弱點掃描也對不上實際跑的版本
bash "$XS" "$HOST_ALIAS" 'set -e
[ -x $HOME/xconsole/.venv/bin/python ] || python3 -m venv $HOME/xconsole/.venv
$HOME/xconsole/.venv/bin/python -m pip install -q --disable-pip-version-check -r $HOME/xconsole/requirements.txt
$HOME/xconsole/.venv/bin/python -c "import cryptography; print(\"cryptography\", cryptography.__version__)"'

echo "=== 組 fleet.json（含各機代理權杖，0600）==="
NODE_JSON=""
# 群組對照用 case，不用 declare -A：macOS 內建的 bash 是 3.2，不支援關聯陣列
# （實際踩到：declare -A 報錯，但因為鍵剛好都是數字被當成一般陣列索引，群組碰巧還是對的）
group_of() {
  case "$1" in
    26|27)          echo "AMD AI 395 Strix Halo" ;;
    24|25|32)       echo "DGX Spark" ;;
    20|21|23|248)   echo "Jetson / MIC-743" ;;
    28|29|30|50)    echo "NVIDIA 伺服器" ;;
    70|103)         echo "GPU 農場" ;;
    *)              echo "未分類" ;;
  esac
}
for a in "${NODES[@]}"; do
  TOK=$(bash "$XS" "$a" 'cat $HOME/.xconsole/token 2>/dev/null' 2>/dev/null | tr -d '\r\n ')
  if [ -z "$TOK" ]; then echo "  [略過] .$a 還沒部署節點代理"; continue; fi
  IP=$(python3 -c "import json,sys;print(json.load(open('$HERE/registries/$a.json'))['machine'].get('ip',''))" 2>/dev/null)
  if [ -z "$IP" ]; then
    IP=$(bash "$XS" "$a" 'hostname -I 2>/dev/null | awk "{print \$1}"' 2>/dev/null | tr -d '\r\n ')
  fi
  NAME=".$a"
  GRP="$(group_of "$a")"
  [ -n "$NODE_JSON" ] && NODE_JSON="$NODE_JSON,"
  NODE_JSON="$NODE_JSON{\"id\":\"$a\",\"name\":\"$NAME\",\"host\":\"$IP\",\"group\":\"$GRP\",\"agent_port\":9401,\"token\":\"$TOK\"}"
  echo "  [納管] .$a  $IP  $GRP"
done

bash "$XS" "$HOST_ALIAS" "mkdir -p -m 700 \$HOME/.xconsole
python3 - <<'PY'
import json, os
p = os.path.expanduser('~/.xconsole/fleet.json')
cfg = {}
if os.path.exists(p):
    cfg = json.load(open(p))
cfg['nodes'] = json.loads('''[$NODE_JSON]''')
cfg.pop('session_secret', None)
cfg.setdefault('auth', {})
fd = os.open(p + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as fh:
    json.dump(cfg, fh, ensure_ascii=False, indent=1)
os.replace(p + '.tmp', p)
print('fleet.json 已寫入，納管', len(cfg['nodes']), '台')
PY"

echo "=== 取回各機代理憑證，當作主控台連線時的信任依據 ==="
# 經 SSH 取（SSH 本身已驗證主機），比「第一次連上就相信」可靠
bash "$XS" "$HOST_ALIAS" 'mkdir -p -m 700 $HOME/.xconsole/trust'
for a in "${NODES[@]}"; do
  PEMF=$(mktemp "${TMPDIR:-/tmp}/xc-trust.XXXXXX")
  bash "$XS" "$a" 'cat $HOME/.xconsole/agent-cert.pem 2>/dev/null' 2>/dev/null | grep -v '^Warning' > "$PEMF"
  if grep -q "BEGIN CERTIFICATE" "$PEMF"; then
    PUT="$PEMF" DST="/home/xcloud/.xconsole/trust/$a.pem" bash "$XS" "$HOST_ALIAS" --scp && echo "  [信任] .$a"
  else
    echo "  [略過] .$a 還沒有代理憑證（代理要先更新成 HTTPS 版）"
  fi
  rm -f "$PEMF"
done
bash "$XS" "$HOST_ALIAS" 'chmod 600 $HOME/.xconsole/trust/*.pem 2>/dev/null; true'

echo "=== 建立服務並啟動 ==="
bash "$XS" "$HOST_ALIAS" 'set -e
cat > $HOME/.config/systemd/user/xconsole.service <<UNIT
[Unit]
Description=xConsole 模型主控台
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/xconsole
ExecStart=%h/xconsole/.venv/bin/python %h/xconsole/xconsole_server.py
Restart=always
RestartSec=5
MemoryMax=512M
CPUQuota=40%

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable xconsole.service
systemctl --user restart xconsole.service
sleep 3
systemctl --user is-active xconsole.service
sleep 2
# 存活檢查打同埠的 http://：回 308 就代表服務活著。
# 不用 https://127.0.0.1 驗證：換成正式憑證後主機名稱對不上會誤報，而加 -k 又等於關掉驗證
code=$(curl -s -m 8 -o /dev/null -w "%{http_code}" http://127.0.0.1:9400/healthz)
[ "$code" = "308" ] && echo "主控台存活（http 正確轉到 https）" || echo "主控台沒有回應（得到 $code）"'

echo "=== 把主控台憑證帶回本機（報表產生器連線時用它驗證，不關驗證）==="
mkdir -p -m 700 "$HOME/.xconsole/console-ca"
CIP=$(python3 -c "import json;print(json.load(open('$HERE/registries/$HOST_ALIAS.json'))['machine'].get('ip',''))" 2>/dev/null)
bash "$XS" "$HOST_ALIAS" 'cat $HOME/.xconsole/tls/cert.pem' 2>/dev/null | grep -v '^Warning' > "$HOME/.xconsole/console-ca/$CIP.pem"
chmod 600 "$HOME/.xconsole/console-ca/$CIP.pem"
echo "  已存：$HOME/.xconsole/console-ca/$CIP.pem"
