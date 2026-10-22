#!/usr/bin/env bash
# 把 xConsole 節點代理部署到一台機器。
#   bash deploy_agent.sh <別名>            只裝代理，保留既有登錄檔
#   bash deploy_agent.sh <別名> --seed     同時把 registries/<別名>.json 當成登錄檔寫進去
# 登錄檔只在 --seed 時覆寫，免得把機器上累積的實測值洗掉。
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
XS=/Users/jeff/.claude/skills/connect-xcloud-servers/scripts/xssh.sh
ALIAS="${1:?用法: bash deploy_agent.sh <別名> [--seed]}"
SEED="${2:-}"

echo "=== [$ALIAS] 傳送代理程式 ==="
PUT="$HERE/agent/xconsole_agent.py" DST=/home/xcloud/xconsole_agent.py bash "$XS" "$ALIAS" --scp || {
  # 帳號不是 xcloud 的機器，家目錄不一樣
  HOMEDIR=$(bash "$XS" "$ALIAS" 'echo $HOME')
  PUT="$HERE/agent/xconsole_agent.py" DST="$HOMEDIR/xconsole_agent.py" bash "$XS" "$ALIAS" --scp
}

if [ "$SEED" = "--seed" ]; then
  REG="$HERE/registries/$ALIAS.json"
  [ -f "$REG" ] || { echo "沒有 $REG"; exit 1; }
  echo "=== [$ALIAS] 寫入欄位登錄 ==="
  bash "$XS" "$ALIAS" 'mkdir -p -m 700 $HOME/.xconsole'
  PUT="$REG" DST='$HOME/.xconsole/registry.json' bash "$XS" "$ALIAS" --scp 2>/dev/null \
    || { HOMEDIR=$(bash "$XS" "$ALIAS" 'echo $HOME'); PUT="$REG" DST="$HOMEDIR/.xconsole/registry.json" bash "$XS" "$ALIAS" --scp; }
fi

echo "=== [$ALIAS] 建立服務並啟動 ==="
bash "$XS" "$ALIAS" 'set -e
mkdir -p -m 700 $HOME/.xconsole $HOME/.config/systemd/user
cat > $HOME/.config/systemd/user/xconsole-agent.service <<UNIT
[Unit]
Description=xConsole 節點代理（模型欄位、准入控制、啟停）
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 %h/xconsole_agent.py
Restart=always
RestartSec=5
# 這支會啟停別的服務，自己不要吃太多資源
MemoryMax=256M
CPUQuota=25%

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable xconsole-agent.service
systemctl --user restart xconsole-agent.service   # 已在跑時 enable --now 不會換成新程式碼
sleep 2
systemctl --user is-active xconsole-agent.service
echo "權杖已產生於 $HOME/.xconsole/token（0600，不印出內容）"
curl -s -m 5 http://127.0.0.1:9401/healthz; echo'
