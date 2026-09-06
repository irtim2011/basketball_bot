#!/usr/bin/env bash
# Update an existing user-owned systemd deployment without sudo.
set -Eeuo pipefail
umask 077
app_root="$HOME/.local/share/training-bot"
unit="training-bot-$(id -un).service"
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
previous="$(readlink -f "$app_root/current")"
export ENV_FILE="$HOME/.config/training-bot/bot.env"
export DB_PATH="$app_root/data/training_bot.db"
[[ -x "$previous/venv/bin/python" ]]
[[ "$(systemctl show "$unit" -p User --value)" == "$(id -un)" ]]
[[ "$(systemctl show "$unit" -p Restart --value)" == on-failure ]]
exec 9>"$app_root/install.lock"
flock -n 9 || { echo 'Другая установка уже выполняется'; exit 1; }
release="$(mktemp -d "$app_root/releases/release-XXXXXXXX")"
cp "$source_dir/"*.py "$source_dir/"requirements.txt "$source_dir/"manage.sh "$source_dir/"deploy_existing.sh "$release/"
ln -s "$previous/venv" "$release/venv"
"$release/venv/bin/python" "$release/check_config.py"
(cd "$release" && GOOGLE_SYNC_DISABLED=1 "$release/venv/bin/python" -m unittest discover -p 'test_*.py' -q)
"$release/venv/bin/python" "$release/check_google_sheet.py"
backup="$(mktemp -d "$app_root/backups/deploy-2.4.6-XXXXXXXX")"
"$release/venv/bin/python" "$release/backup.py" "$DB_PATH" "$backup"
(cd "$release" && "$release/venv/bin/python" - "$backup" <<'PY'
import sys, shutil
from pathlib import Path
import google_sheet
shutil.move(google_sheet.export_workbook_xlsx(), Path(sys.argv[1])/'workbook.xlsx')
PY
)
printf '%s\n' "$previous" > "$backup/previous-release.txt"
printf '%s\n' "$previous" > "$app_root/previous"
old_pid="$(systemctl show "$unit" -p MainPID --value)"
[[ "$old_pid" =~ ^[1-9][0-9]*$ ]]
kill -0 "$old_pid"
rollback() {
    trap - ERR
    ln -sfn "$previous" "$app_root/current"
    current_pid="$(systemctl show "$unit" -p MainPID --value)"
    if [[ "$current_pid" =~ ^[1-9][0-9]*$ ]]; then kill -KILL "$current_pid" || true; fi
    echo "Откат к $previous. Резервная копия: $backup" >&2
    exit 1
}
trap rollback ERR
ln -sfn "$release" "$app_root/current"
kill -KILL "$old_pid"
new_pid=0
for _ in {1..30}; do
    sleep 1
    new_pid="$(systemctl show "$unit" -p MainPID --value)"
    if [[ "$new_pid" =~ ^[1-9][0-9]*$ && "$new_pid" != "$old_pid" ]] && kill -0 "$new_pid" 2>/dev/null; then break; fi
done
[[ "$new_pid" =~ ^[1-9][0-9]*$ && "$new_pid" != "$old_pid" ]]
sleep 8
systemctl is-active --quiet "$unit"
[[ "$(systemctl show "$unit" -p MainPID --value)" == "$new_pid" ]]
[[ "$(cat "$release/version.py")" == "VERSION = '2.4.6'" ]]
trap - ERR
printf 'release=%s\nold_pid=%s\nnew_pid=%s\nbackup=%s\n' "$release" "$old_pid" "$new_pid" "$backup"
