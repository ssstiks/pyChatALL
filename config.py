#!/usr/bin/env python3
"""
Глобальные константы и конфигурация tg_agent.
Не импортирует ничего из других модулей проекта.
"""

import threading
import pathlib

# ════════════════════════════════════════
#  TELEGRAM + АГЕНТЫ
# ════════════════════════════════════════
BOT_TOKEN    = "YOUR_BOT_TOKEN_HERE"
ALLOWED_CHAT = YOUR_TELEGRAM_ID

OPENROUTER_API_KEY = ""  # fallback если нет файла; лучше задать через бот

CLAUDE_BIN  = "/home/stx/.local/bin/claude"
GEMINI_BIN  = "/home/stx/.nvm/versions/node/v22.20.0/bin/gemini"
QWEN_BIN    = "/home/stx/.nvm/versions/node/v22.20.0/bin/qwen"
WORK_DIR    = "/home/stx/Applications/progect/shadowchat"

# ── Файлы состояния ───────────────────────────────────────────
# Использование постоянного хранилища в home (сохранится при перезагрузке)
STATE_DIR        = str(pathlib.Path.home() / ".local" / "share" / "pyChatALL")
ACTIVE_FILE      = f"{STATE_DIR}/active_agent.txt"
CLAUDE_SESSION   = f"{STATE_DIR}/claude_session.txt"
CLAUDE_CTX_FILE  = f"{STATE_DIR}/claude_ctx_chars.txt"
CLAUDE_MODEL_FILE = f"{STATE_DIR}/claude_model.txt"
GEMINI_SESSION   = f"{STATE_DIR}/gemini_session.txt"
GEMINI_CTX_FILE  = f"{STATE_DIR}/gemini_ctx_chars.txt"
GEMINI_MODEL_FILE = f"{STATE_DIR}/gemini_model.txt"
QWEN_SESSION     = f"{STATE_DIR}/qwen_session.txt"
QWEN_CTX_FILE    = f"{STATE_DIR}/qwen_ctx_chars.txt"
QWEN_MODEL_FILE  = f"{STATE_DIR}/qwen_model.txt"
OPENROUTER_MODEL_FILE   = f"{STATE_DIR}/openrouter_model.txt"
OPENROUTER_KEY_FILE     = f"{STATE_DIR}/openrouter_key.txt"
OPENROUTER_MODELS_CACHE = f"{STATE_DIR}/openrouter_models.json"
SHARED_CTX_FILE  = f"{STATE_DIR}/shared_context.json"
ARCHIVE_DIR      = f"{STATE_DIR}/archive"
DOWNLOAD_DIR     = f"{STATE_DIR}/downloads"
WORKSPACE_DL_DIR = f"{WORK_DIR}/.tg_downloads"
CLAUDE_RATE_FILE = f"{STATE_DIR}/claude_rate_until.txt"
MEMORY_FILE      = f"{STATE_DIR}/memory.md"
DISCUSS_FILE     = f"{STATE_DIR}/discuss_agents.json"
DISCUSS_AWAIT_FILE = f"{STATE_DIR}/discuss_await.txt"
SETUP_DONE_FILE  = f"{STATE_DIR}/setup_done.txt"
GLOBAL_MEMORY_FILE = f"{STATE_DIR}/global_memory.json"
ALL_AGENTS_FILE  = f"{STATE_DIR}/all_agents.txt"

MODEL_FILES = {
    "claude":      CLAUDE_MODEL_FILE,
    "gemini":      GEMINI_MODEL_FILE,
    "qwen":        QWEN_MODEL_FILE,
    "openrouter":  OPENROUTER_MODEL_FILE,
}

LOG_FILE     = "/tmp/tg_agent.log"
LOG_FILE_ERR = "/tmp/tg_agent_errors.log"
PID_FILE     = "/tmp/tg_agent.pid"

# ── Лимиты контекста (символы, ~4 симв/токен) ────────────────
CTX_LIMITS = {
    #                warn      archive
    "claude":      ( 80_000,  200_000),
    "gemini":      (500_000, 1_500_000),
    "qwen":        (100_000,  300_000),
    "openrouter":  ( 60_000,  200_000),
}

# ── Таймауты агентов (секунды) ────────────────────────────────
_AGENT_TIMEOUT: dict[str, int] = {
    "claude":     800,
    "gemini":     600,
    "qwen":       300,
    "openrouter": 120,
}

# ── Fallback-модели Gemini ────────────────────────────────────
GEMINI_FALLBACK_MODELS = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "auto-gemini-3",
    "gemini-2.5-pro",
    "gemini-3-flash-preview",
]

SHARED_CTX_MSGS  = 6
SHARED_CTX_CHARS = 3_000

# ── Telegram ──────────────────────────────────────────────────
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_MAX_LEN = 4096

# ── Глобальный лок для операций с файлами состояния ──────────
_lock = threading.Lock()

# ── Последний запрос (для /retry) ────────────────────────────
_last_request: dict = {}

# ── Известные модели ──────────────────────────────────────────
KNOWN_MODELS = {
    "claude": [
        "claude-sonnet-4-6",
        "claude-opus-4-6",
        "claude-haiku-4-5-20251001",
        "sonnet",
        "opus",
        "haiku",
    ],
    "gemini": [
        "auto-gemini-3",
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-3.1-pro-preview",
        "gemini-3-flash-preview",
        "gemini-2.5-pro",
    ],
    "qwen": [
        "coder-model",
        "vision-model",
    ],
}

DEFAULT_MODELS = {
    "claude": "claude-haiku-4-5-20251001",
    "gemini": "auto-gemini-3",
    "qwen":   "vision-model",
}

# Named constant — immune to KNOWN_MODELS list reordering
SONNET_MODEL = KNOWN_MODELS["claude"][0]   # "claude-sonnet-4-6"

# ── CLI-команды каждого агента ────────────────────────────────
AGENT_CLI_CMDS = {
    "claude": [
        ("/compact",  "🗜", "Сжать контекст"),
        ("/clear",    "🗑", "Очистить историю"),
        ("/doctor",   "🔍", "Диагностика"),
        ("/cost",     "💰", "Стоимость токенов"),
        ("/status",   "📡", "Статус сессии"),
        ("/config",   "⚙️", "Конфигурация"),
    ],
    "gemini": [],
    "qwen": [
        ("/summary",  "🗜", "Резюме контекста"),
        ("/compress", "🗜", "Сжать контекст"),
        ("/bug",      "🐛", "Найти баги"),
        ("/init",     "⚡", "Инициализация проекта"),
    ],
    "openrouter": [],
}

# ── Установка агентов ─────────────────────────────────────────
_AGENT_SEARCH_PATHS = [
    "/home/stx/.local/bin",
    "/home/stx/.nvm/versions/node/v22.20.0/bin",
    "/usr/local/bin",
    "/usr/bin",
]

AGENT_INSTALL_INFO = {
    "claude": {
        "bin":     "claude",
        "package": "@anthropic-ai/claude-code",
        "cmd":     "npm install -g @anthropic-ai/claude-code",
        "note":    "Требует Node.js 18+. После установки: claude login",
    },
    "gemini": {
        "bin":     "gemini",
        "package": "@google/gemini-cli",
        "cmd":     "npm install -g @google/gemini-cli",
        "note":    "После установки: gemini (войти через Google аккаунт)",
    },
    "qwen": {
        "bin":     "qwen",
        "package": "@qwen-code/qwen-code",
        "cmd":     "npm install -g @qwen-code/qwen-code",
        "note":    "После установки: qwen (войти через Alibaba аккаунт)",
    },
}

AGENT_NAMES = {
    "claude":      "Claude",
    "openrouter":  "OpenRouter",
    "gemini":      "Gemini",
    "qwen":        "Qwen",
}


# ── Database path - persistent SQLite storage location ──────────────
import os
DB_PATH = os.path.expanduser("~/.local/share/pyChatALL/pychatall.db")


def ensure_dirs() -> None:
    """Создаёт необходимые директории если их нет."""
    import os
    for d in (STATE_DIR, ARCHIVE_DIR, DOWNLOAD_DIR, WORKSPACE_DL_DIR):
        os.makedirs(d, exist_ok=True)


# ============================================================================
# Auto-initialization: Create directories and database on import
# ============================================================================

# Create required directories
for directory in [
    os.path.dirname(DB_PATH),
    ARCHIVE_DIR,
    DOWNLOAD_DIR,
]:
    os.makedirs(directory, exist_ok=True)

# Auto-initialize SQLite database if it doesn't exist
if not os.path.exists(DB_PATH):
    print(f"[CONFIG] Creating new SQLite database at {DB_PATH}")
    try:
        from db_manager import Database
        db = Database(DB_PATH)
        db.initialize()
        print(f"[CONFIG] Database initialized successfully")

        # Run one-time migration if JSON files exist
        print(f"[CONFIG] Checking for legacy state files...")
        from migrate_json_to_sqlite import migrate_json_to_sqlite
        if migrate_json_to_sqlite():
            print(f"[CONFIG] Initialization complete")
        else:
            print(f"[CONFIG] Initialization complete (no legacy files to migrate)")
    except Exception as e:
        print(f"[CONFIG] Warning: Could not auto-initialize database: {e}")
else:
    # Database exists, just ensure directories are in place
    ensure_dirs()
