#!/usr/bin/env python3
"""
Глобальные константы и конфигурация tg_agent.
Не импортирует ничего из других модулей проекта.
"""

import os
import shutil
import threading
import pathlib

# ════════════════════════════════════════
#  TELEGRAM + АГЕНТЫ
# ════════════════════════════════════════
BOT_TOKEN    = os.getenv("TG_BOT_TOKEN", "")
ALLOWED_CHAT = int(os.getenv("TG_ALLOWED_CHAT", "0"))  # set your Telegram user ID

OPENROUTER_API_KEY = ""  # fallback если нет файла; лучше задать через бот

# ── Автоопределение бинарников агентов ───────────────────────
_HOME = os.path.expanduser("~")


def _nvm_bin() -> str:
    """Возвращает путь к bin последней установленной версии Node.js через nvm."""
    nvm_dir = os.path.join(_HOME, ".nvm", "versions", "node")
    if os.path.isdir(nvm_dir):
        versions = sorted(
            (v for v in os.listdir(nvm_dir) if os.path.isdir(os.path.join(nvm_dir, v))),
            reverse=True,
        )
        if versions:
            return os.path.join(nvm_dir, versions[0], "bin")
    return ""


_NVM = _nvm_bin()

CLAUDE_BIN = (
    shutil.which("claude")
    or os.path.join(_HOME, ".local", "bin", "claude")
)
GEMINI_BIN = (
    shutil.which("gemini")
    or (os.path.join(_NVM, "gemini") if _NVM else "gemini")
)
QWEN_BIN = (
    shutil.which("qwen")
    or (os.path.join(_NVM, "qwen") if _NVM else "qwen")
)
# WORK_DIR: directory the bot treats as its workspace.
# Defaults to a saved path (work_dir.txt), then falls back to the bot's own
# directory so the bot is self-contained wherever it is deployed.
_WORK_DIR_FILE = str(pathlib.Path.home() / ".local" / "share" / "pyChatALL" / "work_dir.txt")
def _load_work_dir() -> str:
    try:
        with open(_WORK_DIR_FILE) as _f:
            p = _f.read().strip()
        if p and os.path.isdir(p):
            return p
    except FileNotFoundError:
        pass
    return os.path.dirname(os.path.abspath(__file__))
WORK_DIR = _load_work_dir()

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
OLLAMA_SESSION   = f"{STATE_DIR}/ollama_session.txt"
OLLAMA_CTX_FILE  = f"{STATE_DIR}/ollama_ctx_chars.txt"
OLLAMA_MODEL_FILE = f"{STATE_DIR}/ollama_model.txt"
SHARED_CTX_FILE  = f"{STATE_DIR}/shared_context.json"
ARCHIVE_DIR      = f"{STATE_DIR}/archive"
DOWNLOAD_DIR     = f"{STATE_DIR}/downloads"
WORKSPACE_DL_DIR = f"{WORK_DIR}/.tg_downloads"

# ── Изолированные рабочие папки агентов ──────────────────────
# Каждый агент работает в своей папке — не видит файлы бота и
# других проектов, не тратит API-квоту на сканирование лишнего.
WORKSPACES_DIR    = f"{STATE_DIR}/workspaces"
CLAUDE_WORKSPACE  = f"{WORKSPACES_DIR}/claude"
GEMINI_WORKSPACE  = f"{WORKSPACES_DIR}/gemini"
QWEN_WORKSPACE    = f"{WORKSPACES_DIR}/qwen"
PROJECTS_WORKSPACE = f"{WORKSPACES_DIR}/projects"
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
    "ollama":      OLLAMA_MODEL_FILE,
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
    "ollama":      ( 60_000,  200_000),
}

# ── Таймауты агентов (секунды) ────────────────────────────────
_AGENT_TIMEOUT: dict[str, int] = {
    "claude":     800,
    "gemini":     600,
    "qwen":       300,
    "openrouter": 120,
    "ollama":      120,
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

# ── Ollama (local) ────────────────────────────────────────────
OLLAMA_BASE_URL      = "http://localhost:11434"
OLLAMA_DEFAULT_MODEL = "llama3.2"

# Named constant — immune to KNOWN_MODELS list reordering
SONNET_MODEL = KNOWN_MODELS["claude"][0]   # "claude-sonnet-4-6"

# ── CLI-команды каждого агента ────────────────────────────────
AGENT_CLI_CMDS = {
    "claude": [
        ("/cost",     "💰", "Стоимость токенов"),
        ("/review",   "🔍", "Code review (PR)"),
        ("/init",     "⚡", "Инициализация CLAUDE.md"),
    ],
    "gemini": [],
    "qwen": [
        ("/summary",  "🗜", "Резюме контекста"),
        ("/compress", "🗜", "Сжать контекст"),
        ("/bug",      "🐛", "Найти баги"),
        ("/init",     "⚡", "Инициализация проекта"),
    ],
    "openrouter": [],
    "ollama":      [],
}

# ── Установка агентов ─────────────────────────────────────────
_AGENT_SEARCH_PATHS = [
    os.path.join(_HOME, ".local", "bin"),
    *([_NVM] if _NVM else []),
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
    "ollama":      "Ollama",
}


# ── Database path - persistent SQLite storage location ──────────────
import os
DB_PATH = os.path.expanduser("~/.local/share/pyChatALL/pychatall.db")


def ensure_dirs() -> None:
    """Создаёт необходимые директории если их нет."""
    import os
    for d in (STATE_DIR, ARCHIVE_DIR, DOWNLOAD_DIR, WORKSPACE_DL_DIR,
              CLAUDE_WORKSPACE, GEMINI_WORKSPACE, QWEN_WORKSPACE, PROJECTS_WORKSPACE):
        os.makedirs(d, exist_ok=True)


# ============================================================================
# Auto-initialization: Create directories on import (lightweight, always safe)
# Database initialization is deferred to ensure_db() called from main entry point
# ============================================================================

# Create required directories (no I/O beyond mkdir — safe at import time)
for directory in [
    os.path.dirname(DB_PATH),
    ARCHIVE_DIR,
    DOWNLOAD_DIR,
]:
    os.makedirs(directory, exist_ok=True)


def ensure_db() -> None:
    """Initialize SQLite database and run one-time migration if needed.

    Must be called explicitly from the main entry point (tg_agent.py),
    NOT at import time — avoids blocking startup and circular-import issues.
    """
    import logging
    _log = logging.getLogger(__name__)

    if not os.path.exists(DB_PATH):
        _log.info("[CONFIG] Creating new SQLite database at %s", DB_PATH)
        try:
            from db_manager import Database
            db = Database(DB_PATH)
            db.initialize()
            _log.info("[CONFIG] Database initialized successfully")

            _log.info("[CONFIG] Checking for legacy state files...")
            from migrate_json_to_sqlite import migrate_json_to_sqlite
            if migrate_json_to_sqlite():
                _log.info("[CONFIG] Legacy migration complete")
            else:
                _log.info("[CONFIG] No legacy files to migrate")
        except Exception as e:
            _log.warning("[CONFIG] Could not auto-initialize database: %s", e)
    else:
        ensure_dirs()
