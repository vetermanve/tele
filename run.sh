#!/usr/bin/env bash
# Запуск мониторинга P2P-цен wallet.tg одной командой.
# Готовит виртуальное окружение, ставит зависимости при изменении
# requirements.txt и стартует monitor.py.
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
PY="$VENV/bin/python"
STAMP="$VENV/.deps-hash"

# 1. Виртуальное окружение
if [ ! -x "$PY" ]; then
    echo ">> создаю виртуальное окружение $VENV"
    python3 -m venv "$VENV"
fi

# 2. Зависимости — переустанавливаем только при изменении requirements.txt
req_hash="$(shasum -a 256 requirements.txt | awk '{print $1}')"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$req_hash" ]; then
    echo ">> ставлю зависимости из requirements.txt"
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet -r requirements.txt
    echo "$req_hash" > "$STAMP"
fi

# 3. Конфигурация
if [ ! -f .env ]; then
    echo "!! нет .env — скопируй .env.example в .env и впиши WALLET_TG_API_KEY:"
    echo "     cp .env.example .env"
    exit 1
fi

# 4. Запуск
exec "$PY" monitor.py
