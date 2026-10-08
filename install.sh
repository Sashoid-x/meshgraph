#!/usr/bin/env bash
#
# meshgraph — интерактивная установка одной строкой:
#
#   curl -fsSL https://raw.githubusercontent.com/Sashoid-x/meshgraph/main/install.sh | bash
#
# Скрипт спрашивает каталог, порт, интерфейс слушания, адрес MQTT-брокера,
# пароль для настроек и автозапуск (Enter — значение по умолчанию).  Ответы
# читаются из /dev/tty, поэтому поток stdin при «curl | bash» не ломается;
# без терминала (cron, CI) ставятся значения по умолчанию.
#
# Всё можно задать переменными окружения — вопрос пропускается:
#   MESHGRAPH_HOME         каталог установки     (~/nodegraph)
#   MESHGRAPH_PORT         порт веб-интерфейса   (5010)
#   MESHGRAPH_HOST         интерфейс слушания    (0.0.0.0)
#   MESHGRAPH_BROKER       адрес MQTT-брокера    (пусто — в настройках)
#   MESHGRAPH_PASSWORD     пароль для настроек   (пусто — без пароля;
#                          заданный в config.yaml не сбрасывается)
#   MESHGRAPH_SYSTEMD      автозапуск д/н        (д)
#   MESHGRAPH_NONINTERACTIVE=1   не спрашивать ничего, только дефолты
#   MESHGRAPH_REPO / MESHGRAPH_BRANCH / MESHGRAPH_ARCHIVE / MESHGRAPH_UNIT
#                          адрес репозитория, ветка, свой tar.gz, имя юнита
#   MESHGRAPH_TTY          файл с ответами вместо /dev/tty (для отладки)
set -euo pipefail

REPO_URL="${MESHGRAPH_REPO:-https://github.com/Sashoid-x/meshgraph}"
BRANCH="${MESHGRAPH_BRANCH:-main}"
UNIT="${MESHGRAPH_UNIT:-meshgraph}"
ARCHIVE="${MESHGRAPH_ARCHIVE:-${REPO_URL}/archive/refs/heads/${BRANCH}.tar.gz}"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# --- интерактивность --------------------------------------------------------
# stdin при «curl | bash» — сам скрипт, поэтому читаем ответы из /dev/tty.
TTY_FD=""
if [ -n "${MESHGRAPH_TTY:-}" ]; then
  exec 3<>"$MESHGRAPH_TTY" && TTY_FD=3 || TTY_FD=""
elif { exec 3<>/dev/tty; } 2>/dev/null; then
  TTY_FD=3
fi

ask() {
  # ask ИМЯ_ПЕРЕМЕННОЙ ИМЯ_ENV "вопрос" "дефолт"
  local out="$1" envname="$2" q="$3" def="$4" envval="" ans=""
  envval="${!envname:-}"
  if [ -n "$envval" ]; then
    printf -v "$out" '%s' "$envval"
    return
  fi
  if [ -n "$TTY_FD" ] && [ -z "${MESHGRAPH_NONINTERACTIVE:-}" ]; then
    printf '%s [%s]: ' "$q" "$def"
    IFS= read -r ans <&3 || ans=""
    printf -v "$out" '%s' "${ans:-$def}"
  else
    printf -v "$out" '%s' "$def"
  fi
}

cfg_get() { # значение ключа из существующего config.yaml (без кавычек)
  local v
  v="$(sed -n "s/^$1:[[:space:]]*//p" "$DEST/config.yaml" 2>/dev/null | head -1)"
  v="${v#\'}"; v="${v%\'}"; v="${v#\"}"; v="${v%\"}"
  printf '%s' "$v"
}

command -v curl >/dev/null 2>&1 || die "нужен curl (sudo apt install curl)"

# --- 0. Вопросы -------------------------------------------------------------
say "Установка meshgraph (Enter — по умолчанию)"
ask DEST MESHGRAPH_HOME "Каталог установки" "$HOME/nodegraph"
case "$DEST" in
  "~")   DEST="$HOME" ;;
  "~/"*) DEST="$HOME/${DEST#\~/}" ;;
esac
mkdir -p "$DEST"
DEST="$(cd "$DEST" && pwd)"

DEF_PORT="$(cfg_get web_port)";   DEF_PORT="${DEF_PORT:-5010}"
DEF_HOST="$(cfg_get web_host)";   DEF_HOST="${DEF_HOST:-0.0.0.0}"
DEF_BROKER="$(cfg_get mqtt_broker_address)"
ask PORT   MESHGRAPH_PORT   "Порт веб-интерфейса"                       "$DEF_PORT"
ask HOST   MESHGRAPH_HOST   "Интерфейс (0.0.0.0 — вся сеть, 127.0.0.1 — локально)" "$DEF_HOST"
ask BROKER MESHGRAPH_BROKER "Адрес MQTT-брокера (Enter — позже, в диалоге «Настройки»)" "$DEF_BROKER"
ask SYSTEMD MESHGRAPH_SYSTEMD "Ставить автозапуск в systemd (д/н)"      "д"

# Пароль на настройки спрашивается до скачивания — при опечатке не жалко
# перезапустить. Пустой ответ оставляет настройки открытыми (задаётся потом
# диалогом «Настройки → Безопасность»); уже заданный в config.yaml пароль
# установщик не трогает, если его явно не передали через MESHGRAPH_PASSWORD.
PASSWORD="${MESHGRAPH_PASSWORD:-}"
if [ -z "$PASSWORD" ]; then
  if [ -n "$(cfg_get settings_password_hash)" ]; then
    say "Пароль на настроек уже задан — оставляю как есть"
  elif [ -n "$TTY_FD" ] && [ -z "${MESHGRAPH_NONINTERACTIVE:-}" ]; then
    printf 'Пароль для настроек (Enter — без пароля, вводится в «Настройки → Безопасность»): '
    IFS= read -r -s PASSWORD <&3 || PASSWORD=""
    printf '\n'
    if [ -n "$PASSWORD" ]; then
      printf 'Повторите пароль: '
      IFS= read -r -s PASSWORD_AGAIN <&3 || PASSWORD_AGAIN=""
      printf '\n'
      [ "$PASSWORD" = "$PASSWORD_AGAIN" ] || die "пароли не совпали — запустите установщик заново"
    fi
  fi
fi
if [ -n "$PASSWORD" ] && [ "${#PASSWORD}" -lt 4 ]; then
  die "пароль должен быть не короче 4 символов"
fi

case "$PORT" in
  ''|*[!0-9]*) die "порт должен быть числом 1–65535, получено: $PORT" ;;
esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "порт вне диапазона 1–65535: $PORT"
[ -n "$HOST" ] || die "интерфейс слушания не может быть пустым"
case "$SYSTEMD" in
  ""|[Дд]|[Дд]а|[Yy]|[Yy]es|[Yy]ES|1)                 USE_SYSTEMD=1 ;;
  [Нн]|[Нн]ет|[Nn]|[Nn]o|[Nn]O|0)                     USE_SYSTEMD=0 ;;
  *) die "ожидалось д или н, получено: $SYSTEMD" ;;
esac
say "Итого: $DEST | порт $PORT | $HOST | брокер: ${BROKER:-— в настройках} | пароль на настройки: $([ -n "$PASSWORD" ] && printf 'задан' || printf 'нет') | systemd: $([ "$USE_SYSTEMD" = 1 ] && echo да || echo нет)"
if [ "$HOST" != "127.0.0.1" ] && [ -z "$PASSWORD" ] && [ -z "$(cfg_get settings_password_hash)" ]; then
  printf '\033[1;33m⚠\033[0m Пароль не задан, а интерфейс слушает %s — настройки открыты всей сети.\n' "$HOST"
  printf '    Задайте его после установки: «Настройки» → «Безопасность».\n'
fi

# --- 1. Код -----------------------------------------------------------------
say "Код: $DEST"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "$ARCHIVE" -o "$TMP/src.tar.gz" || die "не скачался архив: $ARCHIVE"
tar -xzf "$TMP/src.tar.gz" --strip-components=1 -C "$DEST"

# --- 2. uv ------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  say "Ставлю uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$PATH"
fi
command -v uv >/dev/null 2>&1 || \
  die "uv не найден — добавьте ~/.local/bin в PATH и повторите запуск"

# --- 3. Зависимости ---------------------------------------------------------
say "Зависимости (uv sync)"
cd "$DEST"
uv sync

# --- 4. Конфиг --------------------------------------------------------------
if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  ok "Создан config.yaml"
else
  say "config.yaml уже есть — обновляю только порт, интерфейс и брокер"
fi
BROKER_Q="${BROKER//\'/\'\'}"  # одинарные кавычки — экранирование YAML
sed -i \
  -e "s|^web_port:.*|web_port: $PORT|" \
  -e "s|^web_host:.*|web_host: $HOST|" \
  -e "s|^mqtt_broker_address:.*|mqtt_broker_address: '$BROKER_Q'|" \
  config.yaml
mkdir -p data

# --- 4a. Пароль на настройки -----------------------------------------------
# В config.yaml хранится только хэш PBKDF2 (см. meshgraph.config.hash_password);
# сам пароль никуда не пишется. Существующий ключ заменяется, отсутствующий —
# добавляется в конец файла.
if [ -n "$PASSWORD" ]; then
  say "Хэш пароля → config.yaml"
  HASH="$(PASSWORD_TO_HASH="$PASSWORD" "$DEST/.venv/bin/python" -c '
import os, sys
sys.path.insert(0, os.getcwd())
from meshgraph.config import hash_password
print(hash_password(os.environ["PASSWORD_TO_HASH"]))
')" || die "не удалось вычислить хэш пароля"
  if grep -q '^settings_password_hash:' config.yaml; then
    sed -i "s|^settings_password_hash:.*|settings_password_hash: '$HASH'|" config.yaml
  else
    printf "\n# Пароль на настройки: только хэш PBKDF2, ввод и смена — в разделе\n# «Безопасность» диалога настроек.\nsettings_password_hash: '%s'\n" \
      "$HASH" >> config.yaml
  fi
  ok "Пароль на настройки задан"
fi

# --- 5. systemd -------------------------------------------------------------
if [ "$USE_SYSTEMD" = 1 ]; then
  command -v systemctl >/dev/null 2>&1 || \
    die "systemd не найден — запустите без автозапуска: MESHGRAPH_SYSTEMD=н bash install.sh"
  RUNUSER="$(id -un)"
  SUDO=""
  if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 || die "нужен sudo, чтобы поставить юнит"
    SUDO="sudo"
  fi
  say "Юнит: /etc/systemd/system/$UNIT.service (пользователь $RUNUSER)"
  $SUDO tee "/etc/systemd/system/$UNIT.service" >/dev/null <<EOF
[Unit]
Description=meshgraph — граф сети Meshtastic
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUNUSER
WorkingDirectory=$DEST
ExecStart=$DEST/.venv/bin/meshgraph
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable "$UNIT" >/dev/null
  $SUDO systemctl restart "$UNIT"

  # --- 6. Проверка ----------------------------------------------------------
  for _ in $(seq 1 30); do
    curl -fsS -o /dev/null "http://127.0.0.1:$PORT/" 2>/dev/null && break
    sleep 1
  done
  curl -fsS -o /dev/null "http://127.0.0.1:$PORT/" || \
    die "страница не поднялась — journalctl -u $UNIT -n 50"
  HOSTIP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  ok "Готово: http://${HOSTIP:-127.0.0.1}:$PORT"
  printf '    Настройки брокера — кнопка «Настройки» на странице.\n'
  if [ -n "$PASSWORD" ]; then
    printf '    Настройки защищены паролем — он спросится при открытии диалога.\n'
  else
    printf '    Пароль на настройки не задан — задайте в «Настройки → Безопасность».\n'
  fi
  printf '    Лог: journalctl -u %s -f\n' "$UNIT"
else
  ok "Установлено без автозапуска. Запуск: cd $DEST && uv run meshgraph"
  if [ -n "$PASSWORD" ]; then
    printf '    Настройки защищены паролем — он спросится при открытии диалога.\n'
  else
    printf '    Пароль на настройки не задан — задайте в «Настройки → Безопасность».\n'
  fi
fi
