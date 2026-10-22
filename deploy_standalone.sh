#!/usr/bin/env bash
# 單機模式：把節點代理與主控台都裝在同一台，這台只管自己。
# 賣一台 AI 395／MIC-743／DGX Spark 給客戶時用這個——客戶拿到機器開瀏覽器就能用，
# 不需要另外準備一台管理機。之後要擴成多台，再用 deploy_console.sh 把它們串起來即可，
# 代理與登錄檔完全不用動。
#
#   bash deploy_standalone.sh <別名> [registries/<別名>.json 存在就會一起寫入]
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ALIAS="${1:?用法: bash deploy_standalone.sh <別名>}"

SEED=""
[ -f "$HERE/registries/$ALIAS.json" ] && SEED="--seed"
[ -z "$SEED" ] && echo "（沒有 registries/$ALIAS.json，代理會先建一份空的欄位登錄，之後再補）"

bash "$HERE/deploy_agent.sh" "$ALIAS" $SEED || exit 1
bash "$HERE/deploy_console.sh" "$ALIAS" "$ALIAS" || exit 1

XS=/Users/jeff/.claude/skills/connect-xcloud-servers/scripts/xssh.sh
IP=$(bash "$XS" "$ALIAS" 'hostname -I 2>/dev/null | awk "{print \$1}"' 2>/dev/null | tr -d '\r\n ')

echo
echo "完成。單機主控台：https://$IP:9400"
echo "還沒設密碼的話，在該機跑："
echo "    cd ~/xconsole && python3 xconsole_server.py --set-password"
