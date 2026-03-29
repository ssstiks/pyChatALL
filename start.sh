#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  start.sh — Запуск / перезапуск / статус pyChatALL бота
#
#  Использование:
#    ./start.sh          — запустить (если уже запущен — перезапустить)
#    ./start.sh stop     — остановить
#    ./start.sh status   — показать статус
#    ./start.sh logs     — показать хвост лога
# ─────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_SCRIPT="$SCRIPT_DIR/tg_agent.py"
PID_FILE="/tmp/tg_agent.pid"
LOG_FILE="/tmp/tg_agent.log"
PYTHON="python3"

# ── Helpers ───────────────────────────────────────────────────
_pid() {
    [[ -f "$PID_FILE" ]] && cat "$PID_FILE" 2>/dev/null || echo ""
}

_running() {
    local pid
    pid=$(_pid)
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

_stop() {
    local pid
    pid=$(_pid)
    if [[ -z "$pid" ]]; then
        echo "Бот не запущен (PID-файл пуст)."
        return 0
    fi
    if kill -0 "$pid" 2>/dev/null; then
        echo "Останавливаю PID=$pid..."
        kill -15 "$pid"
        local i=0
        while kill -0 "$pid" 2>/dev/null && (( i < 10 )); do
            sleep 1; (( i++ ))
        done
        if kill -0 "$pid" 2>/dev/null; then
            echo "Процесс не завершился — SIGKILL."
            kill -9 "$pid" 2>/dev/null || true
        fi
    fi
    rm -f "$PID_FILE"
    echo "Остановлен."
}

# ── Commands ──────────────────────────────────────────────────
case "${1:-start}" in

    stop)
        _stop
        ;;

    status)
        if _running; then
            echo "✅ Бот запущен   PID=$(_pid)"
            echo "   Лог: $LOG_FILE"
        else
            echo "❌ Бот не запущен"
        fi
        ;;

    logs)
        echo "=== $LOG_FILE (последние 50 строк) ==="
        tail -n 50 "$LOG_FILE" 2>/dev/null || echo "(лог пуст или не найден)"
        ;;

    start|restart|"")
        if _running; then
            echo "Бот уже запущен (PID=$(_pid)). Перезапускаю..."
            _stop
            sleep 1
        fi

        cd "$SCRIPT_DIR"

        # Проверяем наличие зависимостей
        if ! $PYTHON -c "import requests" 2>/dev/null; then
            echo "⚠️  Пакет 'requests' не найден. Установить? [y/N]"
            read -r ans
            if [[ "$ans" =~ ^[Yy]$ ]]; then
                pip install -r requirements.txt
            fi
        fi

        echo "Запускаю бота..."
        nohup $PYTHON "$BOT_SCRIPT" >> "$LOG_FILE" 2>&1 &
        BGPID=$!
        echo $BGPID > "$PID_FILE"

        # Ждём 2 секунды и проверяем что процесс жив
        sleep 2
        if kill -0 "$BGPID" 2>/dev/null; then
            echo "✅ Запущен   PID=$BGPID"
            echo "   Лог: $LOG_FILE"
            echo "   tail -f $LOG_FILE"
        else
            echo "❌ Процесс упал сразу после запуска. Последние строки лога:"
            tail -n 20 "$LOG_FILE"
            exit 1
        fi
        ;;

    *)
        echo "Использование: $0 [start|stop|restart|status|logs]"
        exit 1
        ;;
esac
