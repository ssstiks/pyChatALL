#!/usr/bin/env python3
"""
Управление состоянием: сессии агентов, общий контекст диалога,
долговременная память, активный агент, модели, rate-limit Claude.
"""

import json
import os
import re
import time
import threading

from config import (
    _lock,
    ACTIVE_FILE, ARCHIVE_DIR,
    CLAUDE_SESSION, CLAUDE_CTX_FILE, CLAUDE_MODEL_FILE, CLAUDE_RATE_FILE,
    GEMINI_SESSION, GEMINI_CTX_FILE, GEMINI_MODEL_FILE,
    QWEN_SESSION, QWEN_CTX_FILE, QWEN_MODEL_FILE,
    OPENROUTER_MODEL_FILE, OPENROUTER_KEY_FILE,
    SHARED_CTX_FILE, MEMORY_FILE,
    DISCUSS_FILE, DISCUSS_AWAIT_FILE,
    STATE_DIR, CTX_LIMITS,
    MODEL_FILES, DEFAULT_MODELS, KNOWN_MODELS, AGENT_CLI_CMDS, AGENT_NAMES,
    SHARED_CTX_MSGS, SHARED_CTX_CHARS,
    DB_PATH,
)
from logger import log_info, log_warn, log_error
from memory_manager import get_memory_manager
from db_manager import Database

# Initialize database instance
db = Database(DB_PATH)


# ── МОДЕЛИ ───────────────────────────────────────────────────
def get_model(agent: str) -> str:
    """Get selected model for agent from database."""
    try:
        m = db.get_model(agent)
        return m if m else DEFAULT_MODELS.get(agent, "")
    except Exception:
        return DEFAULT_MODELS.get(agent, "")


def set_model(agent: str, model: str) -> None:
    """Set selected model for agent in database."""
    try:
        db.set_model(agent, model)
    except Exception as e:
        log_error(f"Failed to set model for {agent}: {e}")


def agent_label(agent: str) -> str:
    """Возвращает строку типа 'Claude (claude-sonnet-4-6)'."""
    m = get_model(agent)
    name = AGENT_NAMES.get(agent, agent)
    return f"{name} ({m})" if m else name


def cmd_model(agent: str, arg: str) -> str:
    """
    /model           — показать текущую модель
    /model list      — список известных моделей
    /model <name>    — установить модель
    """
    arg = arg.strip()
    current = get_model(agent)
    name = AGENT_NAMES.get(agent, agent)

    if not arg:
        return f"🤖 {name}: текущая модель — `{current or '(авто)'}`"

    if arg == "list":
        models = KNOWN_MODELS.get(agent, [])
        if not models:
            return f"ℹ️ Список моделей для {name} не задан."
        lines = [f"📋 Модели {name}:"]
        for m in models:
            mark = "✓ " if m == current else "  "
            lines.append(f"  {mark}{m}")
        return "\n".join(lines)

    set_model(agent, arg)
    return f"✅ {name}: модель установлена — `{arg}`"


# ── СЕССИИ CLI-АГЕНТОВ ───────────────────────────────────────
def _load_session(path: str) -> str | None:
    """Load session from database by extracting agent name from path."""
    try:
        # Extract agent name from path (e.g., "...claude_session.txt" -> "claude")
        agent = _extract_agent_from_path(path)
        if agent:
            return db.get_session(agent)
        return None
    except Exception:
        return None


def _save_session(path: str, sid: str) -> None:
    """Save session to database by extracting agent name from path."""
    try:
        # Extract agent name from path (e.g., "...claude_session.txt" -> "claude")
        agent = _extract_agent_from_path(path)
        if agent:
            db.save_session(agent, sid)
    except Exception as e:
        log_error(f"Failed to save session: {e}")


def _extract_agent_from_path(path: str) -> str | None:
    """Extract agent name from session/context file paths."""
    if "claude" in path:
        return "claude"
    elif "gemini" in path:
        return "gemini"
    elif "qwen" in path:
        return "qwen"
    elif "openrouter" in path:
        return "openrouter"
    return None


def _get_ctx(path: str) -> int:
    """Get context character count from database by extracting agent name from path."""
    try:
        agent = _extract_agent_from_path(path)
        if agent:
            return db.get_context_usage(agent)
        return 0
    except Exception:
        return 0


def _add_ctx(path: str, chars: int) -> int:
    """Add context characters to database and return total."""
    try:
        agent = _extract_agent_from_path(path)
        if agent:
            total = db.get_context_usage(agent) + chars
            db.update_context_usage(agent, total)
            return total
        return 0
    except Exception as e:
        log_error(f"Failed to add context: {e}")
        return 0


def _reset_session(session_file: str, ctx_file: str) -> None:
    """Reset session and context for an agent."""
    try:
        agent = _extract_agent_from_path(session_file)
        if agent:
            # Save empty session and zero context to database
            db.save_session(agent, "")
            db.update_context_usage(agent, 0)
            # Also archive the old session
            db.archive_session(agent)
    except Exception as e:
        log_error(f"Failed to reset session: {e}")


# ── АКТИВНЫЙ АГЕНТ ───────────────────────────────────────────
def get_active() -> str:
    try:
        with open(ACTIVE_FILE) as f:
            a = f.read().strip()
            if a in ("claude", "openrouter", "gemini", "qwen"):
                return a
    except Exception:
        pass
    return "claude"


def set_active(agent: str) -> None:
    prev = get_active()
    with open(ACTIVE_FILE, "w") as f:
        f.write(agent)
    if agent != prev:
        # Ленивый импорт для избежания кругового импорта (context ← ui ← context)
        import ui
        threading.Thread(
            target=ui.tg_set_keyboard,
            args=(agent, f"▶ Активный: {ui._KB_AGENT_LABELS[agent]}"),
            daemon=True,
        ).start()


# ── ОБЩИЙ КОНТЕКСТ ───────────────────────────────────────────
def shared_ctx_load() -> list:
    """Load shared context messages from database."""
    try:
        # Get all messages from database (order by timestamp)
        messages = db.get_recent_messages(limit=1000)
        # Add timestamp field in HH:MM format for backward compatibility
        result = []
        for msg in messages:
            msg_copy = dict(msg)
            msg_copy['ts'] = time.strftime("%H:%M")  # Use current time format for backward compatibility
            result.append(msg_copy)
        return result
    except Exception:
        return []


def shared_ctx_save(log_list: list) -> None:
    """Save shared context messages to database.
    This function clears old messages and saves new ones.
    """
    try:
        # Since we're replacing the entire context, we could clear and re-add,
        # but for now we'll just ensure the messages are in the database
        # For efficiency, this is called after adding individual messages
        pass
    except Exception:
        pass


def shared_ctx_add(role: str, content: str, agent: str = "") -> None:
    """Add a message to the shared context database.

    Automatically maintains a limit of ~200 messages.
    """
    with _lock:
        try:
            db.add_message(role=role, content=content, agent=agent)
        except Exception as e:
            log_error(f"Failed to add message to database: {e}")


def shared_ctx_for_prompt() -> str:
    log_list = shared_ctx_load()
    recent = log_list[-(SHARED_CTX_MSGS):]
    lines = []
    for m in recent:
        ts = m.get("ts", "")
        if m["role"] == "user":
            lines.append(f"[{ts}] Пользователь: {m['content']}")
        else:
            lines.append(f"[{ts}] {m.get('agent', '')}: {m['content']}")
    text = "\n".join(lines)
    if len(text) > SHARED_CTX_CHARS:
        text = "...(обрезано)\n" + text[-SHARED_CTX_CHARS:]

    mem = memory_load()
    if mem:
        text = (f"[Долговременная память пользователя:\n{mem}\n]\n\n" + text
                if text else f"[Долговременная память:\n{mem}\n]")

    rate_until = claude_rate_until()
    if rate_until is not None:
        import datetime
        left = int(rate_until - time.time())
        h, m2 = divmod(left // 60, 60)
        at = datetime.datetime.fromtimestamp(rate_until).strftime("%H:%M")
        rate_note = (
            f"\n[СИСТЕМНАЯ ЗАМЕТКА: Claude в данный момент недоступен из-за лимита запросов. "
            f"Будет доступен через {h}ч {m2}м (примерно в {at}). "
            f"Если пользователь спрашивает о Claude, сообщи об этом.]"
        )
        text = text + rate_note if text else rate_note
    return text


def global_ctx_for_prompt(skip_recent: bool = False) -> str:
    """
    Token-efficient context injection:
      [MEMORY: {JSON}]          ← English summary (~200-400 tokens)
      [Recent context (RU): ...]  ← Last 3 raw RU messages (skipped when skip_recent=True)

    skip_recent=True: use for Claude with active session (CLI already has session history,
    injecting recent messages would duplicate context and waste tokens).

    Replaces verbose shared_ctx_for_prompt() for CLI agents (Claude/Gemini/Qwen).
    OpenRouter API path continues to use shared_ctx_for_api() — out of scope.
    """
    parts: list[str] = []

    # 1. English memory JSON block (always included)
    mm = get_memory_manager()
    parts.append(mm.to_prompt_block())

    # 2. Last 3 raw Russian messages — skip for Claude with active session
    if not skip_recent:
        try:
            if os.path.exists(SHARED_CTX_FILE):
                with open(SHARED_CTX_FILE, encoding="utf-8") as f:
                    msgs = json.load(f)
                if isinstance(msgs, list) and msgs:
                    recent = msgs[-3:]
                    lines = []
                    for m in recent:
                        role = m.get("role", "?")
                        agent = m.get("agent", "")
                        content = str(m.get("content", ""))[:500]  # hard cap per message
                        label = f"{agent}({role})" if agent else role
                        lines.append(f"[{label}]: {content}")
                    if lines:
                        parts.append("[Recent context (RU):\n" + "\n".join(lines) + "]")
        except (json.JSONDecodeError, OSError):
            pass

    return "\n\n".join(parts)


def shared_ctx_for_api() -> list:
    log_list = shared_ctx_load()
    recent = log_list[-(SHARED_CTX_MSGS):]
    messages: list = []
    total = 0
    mem = memory_load()
    if mem:
        messages.append({"role": "system",
                         "content": f"Долговременная память пользователя:\n{mem}"})
    for m in recent:
        c = m["content"]
        total += len(c)
        if total > SHARED_CTX_CHARS:
            break
        messages.append({
            "role": "user" if m["role"] == "user" else "assistant",
            "content": c,
        })
    return messages


# ── RATE LIMIT CLAUDE ─────────────────────────────────────────
def claude_rate_set(seconds: int) -> None:
    """Записывает время снятия лимита (unix timestamp)."""
    until = time.time() + seconds
    with open(CLAUDE_RATE_FILE, "w") as f:
        f.write(str(until))
    log_warn(f"Claude rate limit: доступен через {seconds // 3600}ч {(seconds % 3600) // 60}м")


def claude_rate_until() -> float | None:
    """Возвращает timestamp снятия лимита или None если лимита нет."""
    try:
        until = float(open(CLAUDE_RATE_FILE).read().strip())
        if until > time.time():
            return until
        os.remove(CLAUDE_RATE_FILE)
    except (FileNotFoundError, ValueError):
        pass
    return None


def claude_rate_msg() -> str:
    """Человекочитаемое сообщение о лимите с оставшимся временем."""
    until = claude_rate_until()
    if until is None:
        return ""
    import datetime
    left = int(until - time.time())
    h, m = divmod(left // 60, 60)
    s = left % 60
    if h > 0:
        eta = f"{h}ч {m}м"
    elif m > 0:
        eta = f"{m}м {s}с"
    else:
        eta = f"{s}с"
    at = datetime.datetime.fromtimestamp(until).strftime("%H:%M")
    return (f"⏳ Claude достиг лимита запросов.\n"
            f"Доступен через: **{eta}** (примерно в {at})")


def _detect_rate_limit(text: str) -> int | None:
    """
    Ищет признаки rate limit в тексте ответа/ошибки Claude.
    Возвращает количество секунд до снятия лимита или None.
    """
    text_low = text.lower()
    # Narrow keywords to avoid false positives even on stderr.
    # "quota" and "overloaded" are too generic; real Claude rate-limit errors
    # always contain "rate_limit", "rate limit", or explicit HTTP 429.
    rate_keywords = ["rate_limit_error", "rate limit", "too many requests", "429"]
    if not any(kw in text_low for kw in rate_keywords):
        return None

    m = re.search(r'"retry[_-]after"\s*:\s*(\d+)', text)
    if m:
        return int(m.group(1))

    m = re.search(r'retry.after[:\s]+(\d+)', text_low)
    if m:
        return int(m.group(1))

    m = re.search(r'(\d+)\s*(hour|час|minute|минут|second|секунд)', text_low)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if "hour" in unit or "час" in unit:
            return n * 3600
        elif "minute" in unit or "минут" in unit:
            return n * 60
        else:
            return n

    # No specific retry-after found — use a conservative 30-minute default.
    # Anthropic always includes retry-after on real rate limits, so this
    # path should rarely fire; 30m is short enough to auto-recover quickly.
    return 30 * 60


# ── ДОЛГОВРЕМЕННАЯ ПАМЯТЬ ─────────────────────────────────────
def memory_load() -> str:
    """Load user memory from database.

    Returns the short_term_context field as a string, or '' if not found.
    For backward compatibility, we return the text content rather than the full dict.
    """
    try:
        mem_data = db.get_memory()
        return mem_data.get('short_term_context', '')
    except Exception:
        return ""


def memory_add(fact: str) -> None:
    """Add a fact to user memory.

    This appends to the short_term_context field in the database.
    """
    fact = fact.strip()
    if not fact:
        return
    with _lock:
        try:
            existing = memory_load()
            ts = time.strftime("%Y-%m-%d")
            line = f"- [{ts}] {fact}"
            new_content = (existing + "\n" + line) if existing else line

            # Save back to database
            mem_data = db.get_memory()
            mem_data['short_term_context'] = new_content
            db.save_memory(mem_data)
        except Exception as e:
            log_error(f"Failed to add memory fact: {e}")


def memory_clear() -> None:
    """Clear user memory.

    Clears the short_term_context field in the database.
    """
    try:
        mem_data = db.get_memory()
        mem_data['short_term_context'] = ''
        db.save_memory(mem_data)
    except Exception as e:
        log_error(f"Failed to clear memory: {e}")


# ── РЕЖИМ ОБСУЖДЕНИЯ — состояние ─────────────────────────────
DISCUSS_ALL_AGENTS = ["claude", "gemini", "qwen", "openrouter"]


def discuss_get_agents() -> list[str]:
    """Возвращает список агентов-участников обсуждения (минимум 2)."""
    try:
        with open(DISCUSS_FILE) as f:
            agents = json.load(f)
            filtered = [a for a in agents if a in DISCUSS_ALL_AGENTS]
            return filtered if len(filtered) >= 2 else DISCUSS_ALL_AGENTS[:3]
    except Exception:
        return ["claude", "gemini", "qwen"]


def discuss_set_agents(agents: list[str]) -> None:
    with open(DISCUSS_FILE, "w") as f:
        json.dump(agents, f)


def discuss_await_set() -> None:
    with open(DISCUSS_AWAIT_FILE, "w") as f:
        f.write("1")


def discuss_await_clear() -> None:
    try:
        os.remove(DISCUSS_AWAIT_FILE)
    except FileNotFoundError:
        pass


def discuss_await_get() -> bool:
    return os.path.exists(DISCUSS_AWAIT_FILE)
