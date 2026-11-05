#!/usr/bin/env bash
# VPS Buyer — установка одной командой (Ubuntu/Debian):
#   bash <(curl -fsSL https://raw.githubusercontent.com/k9032431-cmd/Vps-Buyer/HEAD/install.sh)
# Повторный запуск — обновление. Удаление: тот же скрипт с аргументом "uninstall".
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/k9032431-cmd/Vps-Buyer.git}"
BRANCH="${BRANCH:-}"                      # пусто = ветка по умолчанию
APP_DIR="${APP_DIR:-/opt/vps-buyer}"
SERVICE="vps-buyer"

green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
red()   { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }
ask()   { local v; read -r -p "$1" v </dev/tty; printf '%s' "$v"; }

[ "$(id -u)" -eq 0 ] || { red "Запустите от root (sudo -i)."; exit 1; }

if [ "${1:-}" = "uninstall" ]; then
    systemctl disable --now "$SERVICE" 2>/dev/null || true
    rm -f "/etc/systemd/system/$SERVICE.service"
    systemctl daemon-reload
    rm -rf "$APP_DIR"
    green "VPS Buyer удалён."
    exit 0
fi

green "==> Устанавливаю пакеты"
if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq git python3 python3-venv python3-pip >/dev/null
else
    command -v git >/dev/null && command -v python3 >/dev/null \
        || { red "Нужны git и python3 (поддерживаются Ubuntu/Debian)."; exit 1; }
fi

green "==> Загружаю бота в $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only -q
else
    git clone -q ${BRANCH:+-b "$BRANCH"} "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"

green "==> Ставлю зависимости Python"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
    green "==> Настройка"
    BOT_TOKEN="${BOT_TOKEN:-}"
    ADMIN_IDS="${ADMIN_IDS:-}"
    while [ -z "$BOT_TOKEN" ]; do BOT_TOKEN="$(ask 'Токен бота от @BotFather: ')"; done
    while [ -z "$ADMIN_IDS" ]; do ADMIN_IDS="$(ask 'Ваш Telegram ID (узнать: @userinfobot), можно несколько через запятую: ')"; done
    KEY="$(.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
    sed -e "s|^BOT_TOKEN=.*|BOT_TOKEN=$BOT_TOKEN|" \
        -e "s|^ADMIN_IDS=.*|ADMIN_IDS=$ADMIN_IDS|" \
        -e "s|^ENCRYPTION_KEY=.*|ENCRYPTION_KEY=$KEY|" \
        .env.example > .env
    chmod 600 .env
else
    green "==> .env уже есть — настройки сохранены"
fi

green "==> Создаю службу $SERVICE"
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=VPS Buyer Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m bot.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q "$SERVICE"
systemctl restart "$SERVICE"

sleep 3
if systemctl is-active -q "$SERVICE"; then
    green "✅ Бот запущен! Напишите ему /start в Telegram."
else
    red "❌ Бот не запустился. Последние строки лога:"
    journalctl -u "$SERVICE" -n 20 --no-pager || true
    exit 1
fi

cat <<EOF

Полезные команды:
  journalctl -u $SERVICE -f        — логи
  systemctl restart $SERVICE       — перезапуск
  nano $APP_DIR/.env               — настройки
  bash <(curl -fsSL https://raw.githubusercontent.com/k9032431-cmd/Vps-Buyer/HEAD/install.sh)            — обновить
  bash <(curl -fsSL https://raw.githubusercontent.com/k9032431-cmd/Vps-Buyer/HEAD/install.sh) uninstall  — удалить
EOF
