#!/usr/bin/env bash
set -Eeuo pipefail
app_root="$HOME/.local/share/training-bot"
unit="training-bot-$(id -un).service"
case "${1:-status}" in
  status) sudo systemctl status "$unit" --no-pager ;;
  logs) sudo journalctl -u "$unit" -n 80 --no-pager ;;
  diagnose)
    ENV_FILE="$HOME/.config/training-bot/bot.env" "$app_root/current/venv/bin/python" "$app_root/current/process_guard.py"
    sudo systemctl status "$unit" --no-pager || true ;;
  restart) sudo systemctl restart "$unit" ;;
  stop) sudo systemctl stop "$unit" ;;
  configure)
    ENV_FILE="$HOME/.config/training-bot/bot.env" "$app_root/current/venv/bin/python" "$app_root/current/configure.py"
    sudo systemctl restart "$unit" ;;
  google-status)
    ENV_FILE="$HOME/.config/training-bot/bot.env" "$app_root/current/venv/bin/python" "$app_root/current/check_google_sheet.py" ;;
  google-connect)
    [[ -f "${2:-}" ]] || { echo "Использование: training-bot google-connect /путь/к/google-service-account.json"; exit 1; }
    mkdir -p "$HOME/.config/training-bot"
    install -m 600 "$2" "$HOME/.config/training-bot/google-service-account.json"
    ENV_FILE="$HOME/.config/training-bot/bot.env" "$app_root/current/venv/bin/python" "$app_root/current/check_google_sheet.py"
    sudo systemctl restart "$unit" ;;
  backup)
    "$app_root/current/venv/bin/python" "$app_root/current/backup.py" "$app_root/data/training_bot.db" "$app_root/backups" ;;
  rollback)
    previous="$(cat "$app_root/previous")"
    case "$previous" in "$app_root/releases/"*) ;; *) echo "Некорректный путь версии"; exit 1;; esac
    [[ -d "$previous" ]] || exit 1
    current="$(readlink -f "$app_root/current")"
    sudo systemctl stop "$unit"
    ln -sfn "$previous" "$app_root/current"
    sudo systemctl start "$unit"
    printf '%s\n' "$current" > "$app_root/previous"
    echo "Версия кода восстановлена. База ответов сохранена." ;;
  update)
    [[ -f "${2:-}" ]] || { echo "Использование: training-bot update /путь/training-bot-install.sh"; exit 1; }
    bash "$2" ;;
  *) echo "Команды: status, logs, diagnose, configure, google-status, google-connect ПУТЬ, restart, stop, backup, rollback, update ПУТЬ"; exit 1 ;;
esac
