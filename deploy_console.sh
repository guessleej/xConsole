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
for f in xconsole_server.py; do
  PUT="$HERE/console/$f" DST="/home/xcloud/xconsole/$f" bash "$XS" "$HOST_ALIAS" --scp
done
for f in index.html style.css app.js; do
  PUT="$HERE/console/static/$f" DST="/home/xcloud/xconsole/static/$f" bash "$XS" "$HOST_ALIAS" --scp
done

echo "=== 組 fleet.json（含各機代理權杖，0600）==="
NODE_JSON=""
# 下標一律加引號：bash 會把看起來像數字的裸下標當算術運算，
# 一個 [26.2] 就會讓 declare 從那行起整個失敗（後面的鍵全部沒建起來，群組變「未分類」）。
declare -A NODE_GROUPS=(
  ["26"]="AMD Strix Halo" ["27"]="AMD Strix Halo"
  ["24"]="DGX Spark" ["25"]="DGX Spark" ["32"]="DGX Spark"
  ["20"]="Jetson / MIC-743" ["21"]="Jetson / MIC-743" ["23"]="Jetson / MIC-743" ["248"]="Jetson / MIC-743"
  ["28"]="NVIDIA 伺服器" ["30"]="NVIDIA 伺服器" ["29"]="NVIDIA 伺服器" ["50"]="NVIDIA 伺服器"
  ["70"]="GPU 農場" ["103"]="GPU 農場"
)
for a in "${NODES[@]}"; do
  TOK=$(bash "$XS" "$a" 'cat $HOME/.xconsole/token 2>/dev/null' 2>/dev/null | tr -d '\r\n ')
  if [ -z "$TOK" ]; then echo "  [略過] .$a 還沒部署節點代理"; continue; fi
  IP=$(python3 -c "import json,sys;print(json.load(open('$HERE/registries/$a.json'))['machine'].get('ip',''))" 2>/dev/null)
  if [ -z "$IP" ]; then
    IP=$(bash "$XS" "$a" 'hostname -I 2>/dev/null | awk "{print \$1}"' 2>/dev/null | tr -d '\r\n ')
  fi
  NAME=".$a"
  GRP="${NODE_GROUPS[$a]:-未分類}"
  [ -n "$NODE_JSON" ] && NODE_JSON="$NODE_JSON,"
  NODE_JSON="$NODE_JSON{\"id\":\"$a\",\"name\":\"$NAME\",\"host\":\"$IP\",\"group\":\"$GRP\",\"agent_port\":9401,\"token\":\"$TOK\"}"
  echo "  [納管] .$a  $IP  $GRP"
done

bash "$XS" "$HOST_ALIAS" "mkdir -p -m 700 \$HOME/.xconsole
python3 - <<'PY'
import json, os, secrets
p = os.path.expanduser('~/.xconsole/fleet.json')
cfg = {}
if os.path.exists(p):
    cfg = json.load(open(p))
cfg['nodes'] = json.loads('''[$NODE_JSON]''')
cfg.setdefault('session_secret', secrets.token_urlsafe(32))
cfg.setdefault('auth', {})
fd = os.open(p + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as fh:
    json.dump(cfg, fh, ensure_ascii=False, indent=1)
os.replace(p + '.tmp', p)
print('fleet.json 已寫入，納管', len(cfg['nodes']), '台')
PY"

echo "=== 建立服務並啟動 ==="
bash "$XS" "$HOST_ALIAS" 'set -e
cat > $HOME/.config/systemd/user/xconsole.service <<UNIT
[Unit]
Description=xConsole 模型主控台
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/xconsole
ExecStart=/usr/bin/python3 %h/xconsole/xconsole_server.py
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
curl -sk -m 5 https://127.0.0.1:9400/healthz; echo'
