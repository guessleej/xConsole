# 只給 .50 自建 runner 用（security.yml 的整合測試以 `.` 載入）：讓測試接得上 xcloud 帳號的 systemd --user。
# runner 是系統服務，環境裡沒有 XDG_RUNTIME_DIR；家目錄是 runner 自己的（/mnt/disk16/ci-runners/xConsole/rN/home），
# 測試的資料都留在那裡，不碰 .50 真正的家目錄。例外有兩個：
# 1. systemd 使用者管理員只讀真正家目錄的 ~/.config/systemd/user，所以把 runner 家目錄的同一路徑連過去
#    （測試的單元名稱是隨機的，結束時自己刪掉）。
# 2. 經 systemd --user 帶起來的行程拿到的是真正的家目錄（代理的 llama 單元跑 xconsole_agent.py --launch-llama，
#    會在那裡建 ~/.xconsole）。這一步開始前沒有、結束時也不是 CI 實際安裝（.ci-owned）留下的，就在結束時刪掉。
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
_xc_real="$(getent passwd "$(id -un)" | cut -d: -f6)"
if [ "$HOME" != "$_xc_real" ]; then
  _xc_link="$HOME/.config/systemd/user"
  if [ -d "$_xc_link" ] && [ ! -L "$_xc_link" ]; then rm -f "$_xc_link"/xc-*.service; rmdir "$_xc_link"; fi
  if [ ! -e "$_xc_link" ]; then
    mkdir -p "$_xc_real/.config/systemd/user" "$HOME/.config/systemd"
    ln -s "$_xc_real/.config/systemd/user" "$_xc_link"
  fi
  if [ ! -e "$_xc_real/.xconsole" ]; then
    trap '[ -e "'"$_xc_real"'/.xconsole/.ci-owned" ] || rm -rf "'"$_xc_real"'/.xconsole"' EXIT
  fi
  unset _xc_link
fi
unset _xc_real
