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
)
from logger import log_info, log_warn, log_error


# ── МОДЕЛИ ───────────────────────────────────────────────────
def get_model(agent: str) -> str:
    path = MODEL_FILES.get(agent)
    if not path:
        return DEFAULT_MODELS.get(agent, "")
    try:
        with open(path) as f:
            m = f.read().strip()
            return m if m else DEFAULT_MODELS.get(agent, "")
    except FileNotFoundError:
        return DEFAULT_MODELS.get(agent, "")


def set_model(agent: str, model: str) -> None:
    path = MODEL_FILES.get(agent)
    if path:
        with open(path, "w") as f:
            f.write(model)


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
    try:
        with open(path) as f:
            s = f.read().strip()
            return s if s else None
    except Exception:
        return None


def _save_session(path: str, sid: str) -> None:
    with open(path, "w") as f:
        f.write(sid)


def _get_ctx(path: str) -> int:
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def _add_ctx(path: str, chars: int) -> int:
    total = _get_ctx(path) + chars
    with open(path, "w") as f:
        f.write(str(total))
    return total


def _reset_session(session_file: str, ctx_file: str) -> None:
    for p in (session_file, ctx_file):
        if os.path.exists(p):
            os.remove(p)


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
    try:
        with open(SHARED_CTX_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def shared_ctx_save(log_list: list) -> None:
    with open(SHARED_CTX_FILE, "w") as f:
        json.dump(log_list, f, ensure_ascii=False)


def shared_ctx_add(role: str, content: str, agent: str = "") -> None:
    with _lock:
        log_list = shared_ctx_load()
        log_list.append({
            "role": role, "agent": agent, "content": content,
            "ts": time.strftime("%H:%M"),
        })
        while len(log_list) > 200:
            log_list.pop(0)
        shared_ctx_save(log_list)


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
    """Читает файл памяти, возвращает строку или ''."""
    try:
        return open(MEMORY_FILE).read().strip()
    except FileNotFoundError:
        return ""


def memory_add(fact: str) -> None:
    """Добавляет факт в память."""
    fact = fact.strip()
    if not fact:
        return
    with _lock:
        existing = memory_load()
        ts = time.strftime("%Y-%m-%d")
        line = f"- [{ts}] {fact}"
        new_content = (existing + "\n" + line) if existing else line
        with open(MEMORY_FILE, "w") as f:
            f.write(new_content)


def memory_clear() -> None:
    """Очищает память."""
    try:
        os.remove(MEMORY_FILE)
    except FileNotFoundError:
        pass


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
