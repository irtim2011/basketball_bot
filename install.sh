#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
if [[ "$(id -u)" == 0 ]]; then
  echo "Запускайте от обычного пользователя, например dima, без sudo перед bash."
  exit 1
fi
command -v systemctl >/dev/null || { echo "Нужна Ubuntu с systemd"; exit 1; }
command -v python3 >/dev/null || { echo "Установите python3"; exit 1; }
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
app_root="$HOME/.local/share/training-bot"
env_file="$HOME/.config/training-bot/bot.env"
unit="training-bot-$(id -un).service"
[[ -s "$env_file" ]] || { echo "Нет настроек: $env_file"; exit 1; }
mkdir -p "$app_root/releases" "$app_root/data" "$app_root/backups" "$HOME/.local/bin"
chmod 700 "$app_root" "$app_root/data" "$app_root/backups"
chmod 600 "$env_file"
exec 9>"$app_root/install.lock"
flock -n 9 || { echo "Другая установка уже выполняется."; exit 1; }
release="$(mktemp -d "$app_root/releases/release-XXXXXXXX")"
echo "Подготовка новой версии..."
cp "$source_dir/"*.py "$source_dir/"*.txt "$source_dir/"*.sh "$release/"
if ! python3 -m venv "$release/venv" 2>/dev/null; then
  sudo apt-get update
  sudo apt-get install -y python3-venv
  python3 -m venv "$release/venv"
fi
"$release/venv/bin/python" -m pip install --disable-pip-version-check -q -r "$release/requirements.txt"
export ENV_FILE="$env_file"
export DB_PATH="$app_root/data/training_bot.db"
"$release/venv/bin/python" "$release/configure.py" --upgrade
"$release/venv/bin/python" "$release/check_config.py"
if [[ -f "$HOME/.config/training-bot/google-service-account.json" ]]; then
  "$release/venv/bin/python" "$release/check_google_sheet.py"
else
  echo "Google Таблица подготовлена. Для подключения: training-bot google-connect /путь/к/ключу.json"
fi
(cd "$release" && GOOGLE_SYNC_DISABLED=1 "$release/venv/bin/python" -m unittest discover -p 'test_*.py' -q)
sudo -v
previous="$(readlink -f "$app_root/current" || true)"
was_active=0
if systemctl is-active --quiet "$unit"; then was_active=1; fi
restore() {
  echo "Запуск не удался. Возвращаю прежнюю версию."
  if [[ -n "$previous" && -d "$previous" ]]; then
    ln -sfn "$previous" "$app_root/current"
    if [[ "$was_active" == 1 ]]; then sudo systemctl restart "$unit" || true; fi
  else
    sudo systemctl stop "$unit" || true
  fi
}
if [[ -n "$previous" && -d "$previous" ]]; then
  printf '%s\n' "$previous" > "$app_root/previous"
fi
sudo systemctl stop "$unit" 2>/dev/null || true
trap restore ERR
"$release/venv/bin/python" "$release/process_guard.py" --stop-old
if [[ -f "$DB_PATH" ]]; then
  "$release/venv/bin/python" "$release/backup.py" "$DB_PATH" "$app_root/backups"
fi
ln -sfn "$release" "$app_root/current"
sudo tee "/etc/systemd/system/$unit" >/dev/null <<EOF
[Unit]
Description=Telegram training attendance bot ($(id -un))
Wants=network-online.target
After=network-online.target
[Service]
Type=simple
User=$(id -un)
Group=$(id -gn)
WorkingDirectory=$app_root/current
Environment=ENV_FILE=$env_file
Environment=DB_PATH=$app_root/data/training_bot.db
Environment=PYTHONUNBUFFERED=1
ExecStart=$app_root/current/venv/bin/python $app_root/current/main.py
Restart=on-failure
RestartPreventExitStatus=73
RestartSec=5
TimeoutStopSec=25
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
[Install]
WantedBy=multi-user.target
EOF
cp "$release/manage.sh" "$HOME/.local/bin/training-bot"
chmod 700 "$HOME/.local/bin/training-bot"
sudo systemctl daemon-reload
sudo systemctl enable --now "$unit"
sleep 4
sudo systemctl is-active --quiet "$unit"
trap - ERR
echo
echo "Бот запущен. Откройте его в Telegram и нажмите /start."
echo "Статус: ~/.local/bin/training-bot status"
echo "Логи:   ~/.local/bin/training-bot logs"
echo "Диагностика: ~/.local/bin/training-bot diagnose"
echo "Следующее обновление: запустите новый training-bot-install.sh той же командой."
