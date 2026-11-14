# 只給 .50 自建 runner 用（security.yml 的整合測試以 `.` 載入）：讓測試接得上 xcloud 帳號的 systemd --user。
# runner 是系統服務，環境裡沒有 XDG_RUNTIME_DIR；家目錄是 runner 自己的（/mnt/disk16/ci-runners/xConsole/rN/home），
# 測試的 ~/.xconsole 等資料都留在那裡，不碰 .50 真正的家目錄。
# 唯一的例外是 systemd 使用者單元：使用者管理員只讀真正家目錄的 ~/.config/systemd/user，
# 所以把 runner 家目錄的同一路徑連過去。測試的單元名稱是隨機的，結束時自己刪掉。
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
_real_home="$(getent passwd "$(id -un)" | cut -d: -f6)"
if [ "$HOME" != "$_real_home" ] && [ ! -e "$HOME/.config/systemd/user" ]; then
  mkdir -p "$_real_home/.config/systemd/user" "$HOME/.config/systemd"
  ln -s "$_real_home/.config/systemd/user" "$HOME/.config/systemd/user"
fi
unset _real_home
