#!/usr/bin/env python3
"""
Telegram UI: отправка сообщений, редактирование, клавиатуры, меню,
загрузка/отправка файлов.
"""

import glob as glob_module
import hashlib
import mimetypes
import os
import time

import requests

from config import (
    API, BOT_TOKEN, ALLOWED_CHAT, TG_MAX_LEN,
    WORK_DIR, WORKSPACE_DL_DIR,
    AGENT_NAMES, KNOWN_MODELS, AGENT_CLI_CMDS, AGENT_INSTALL_INFO,
)
from context import (
    get_active, get_model, agent_label,
    discuss_get_agents, discuss_set_agents, DISCUSS_ALL_AGENTS,
)
from logger import log, log_info, log_warn, log_error


# ── БАЗОВЫЕ ОТПРАВКИ ─────────────────────────────────────────
def _split_text(text: str, max_len: int = TG_MAX_LEN) -> list[str]:
    if len(text) <= max_len:
        return [text]
    parts = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, max_len)
        if cut < max_len // 2:
            cut = max_len
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts


def _tg_send_one(text: str, reply_markup: dict | None,
                 parse_mode: str | None) -> tuple[dict | None, str | None]:
    payload: dict = {"chat_id": ALLOWED_CHAT, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    r = requests.post(f"{API}/sendMessage", json=payload, timeout=10)
    result = r.json()
    if not result.get("ok"):
        return None, result.get("description", "")
    return result.get("result"), None


def tg_send(text: str, reply_markup: dict | None = None) -> dict | None:
    """Отправляет сообщение (разбивает на части если > 4096). Возвращает последний message."""
    try:
        parts = _split_text(text)
        last_msg = None
        for i, part in enumerate(parts):
            markup = reply_markup if i == len(parts) - 1 else None
            msg, err = _tg_send_one(part, markup, "Markdown")
            if msg is None:
                if err and ("can't parse" in err.lower() or "parse" in err.lower()):
                    msg, err2 = _tg_send_one(part, markup, None)
                    if msg is None:
                        log_warn(f"tg_send plain failed: {err2}")
                else:
                    log_warn(f"tg_send md failed: {err}")
            last_msg = msg or last_msg
        return last_msg
    except Exception as e:
        log_error("tg_send exception", e)
        return None


def tg_edit(message_id: int, text: str, reply_markup: dict | None = None) -> None:
    """Редактирует сообщение; при длинном тексте или ошибке — отправляет новое."""
    try:
        parts = _split_text(text)
        if len(parts) > 1:
            tg_send(text, reply_markup)
            return
        payload: dict = {"chat_id": ALLOWED_CHAT, "message_id": message_id,
                         "text": text, "parse_mode": "Markdown"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        r = requests.post(f"{API}/editMessageText", json=payload, timeout=10)
        result = r.json()
        if not result.get("ok"):
            code = result.get("error_code", "?")
            desc = result.get("description", "")
            if "parse" in desc.lower():
                payload.pop("parse_mode", None)
                r2 = requests.post(f"{API}/editMessageText", json=payload, timeout=10)
                result2 = r2.json()
                if not result2.get("ok"):
                    log_warn(f"tg_edit plain [{result2.get('error_code')}]: "
                             f"{result2.get('description')} — sending new")
                    tg_send(text, reply_markup)
            else:
                log_warn(f"tg_edit [{code}]: {desc} — sending new message")
                tg_send(text, reply_markup)
    except Exception as e:
        log_error("tg_edit exception", e)
        tg_send(text, reply_markup)


def tg_answer_cb(callback_id: str, text: str = "") -> None:
    """Подтверждает нажатие инлайн-кнопки."""
    try:
        requests.post(f"{API}/answerCallbackQuery",
                      json={"callback_query_id": callback_id, "text": text[:200]},
                      timeout=5)
    except Exception:
        pass


def tg_typing() -> None:
    try:
        requests.post(f"{API}/sendChatAction",
                      json={"chat_id": ALLOWED_CHAT, "action": "typing"},
                      timeout=5)
    except Exception:
        pass


# ── ПОСТОЯННАЯ КЛАВИАТУРА ─────────────────────────────────────
_KB_AGENT_LABELS = {
    "claude":      "🔵 Claude",
    "gemini":      "🟢 Gemini",
    "qwen":        "🟡 Qwen",
    "openrouter":  "🌐 OpenRouter",
    "ollama":      "🦙 Ollama",
}
_KB_TEXT_TO_AGENT: dict[str, str] = {}
for _ag, _lbl in _KB_AGENT_LABELS.items():
    _KB_TEXT_TO_AGENT[_lbl] = _ag
    _KB_TEXT_TO_AGENT[f"▶ {_lbl}"] = _ag


def _build_reply_keyboard(active: str) -> dict:
    import rate_tracker
    limit_info = rate_tracker.get_display(active)
    placeholder = f"Активный: {_KB_AGENT_LABELS[active]}"
    if limit_info:
        placeholder += f" | {limit_info}"

    def agent_btn(ag: str) -> str:
        lbl = _KB_AGENT_LABELS[ag]
        return f"▶ {lbl}" if ag == active else lbl

    return {
        "keyboard": [
            [agent_btn("claude"), agent_btn("gemini"), agent_btn("qwen")],
            [agent_btn("openrouter"), agent_btn("ollama")],
            ["📋 /menu", "🔀 /all", "💬 /discuss", "📁 /files"],
            ["📊 /ctx", "📊 /limits", "🔧 /setup", "🧠 /memory"],
        ],
        "resize_keyboard":   True,
        "is_persistent":     True,
        "input_field_placeholder": placeholder,
    }


def tg_set_keyboard(active: str | None = None, notify: str | None = None) -> None:
    if active is None:
        active = get_active()
    markup = _build_reply_keyboard(active)
    text = notify or f"⌨️ {_KB_AGENT_LABELS[active]}"
    try:
        requests.post(f"{API}/sendMessage", json={
            "chat_id":    ALLOWED_CHAT,
            "text":       text,
            "reply_markup": markup,
        }, timeout=10)
    except Exception as e:
        log_error("tg_set_keyboard", e)


# ── ФАЙЛЫ ─────────────────────────────────────────────────────
_SENDABLE_EXTS = {
    ".apk", ".aab", ".ipa", ".exe", ".zip", ".tar", ".gz", ".tar.gz",
    ".pdf", ".docx", ".xlsx", ".csv", ".json", ".txt", ".log",
    ".py", ".go", ".js", ".ts", ".java", ".kt", ".swift",
    ".mp4", ".mp3", ".ogg", ".png", ".jpg", ".gif",
}
_send_file_map: dict[str, str] = {}


def tg_send_file(path: str, caption: str = "") -> bool:
    """Отправляет файл в Telegram. Возвращает True при успехе."""
    try:
        if not os.path.exists(path):
            tg_send(f"⚠️ Файл не найден: {path}")
            return False
        size = os.path.getsize(path)
        if size > 50 * 1024 * 1024:
            tg_send(f"⚠️ Файл слишком большой: {size // 1024 // 1024}МБ (лимит Telegram 50МБ)")
            return False
        log_info(f"Отправка файла: {path} ({size // 1024}кб)")
        with open(path, "rb") as f:
            data = {"chat_id": ALLOWED_CHAT}
            if caption:
                data["caption"] = caption[:1024]
            r = requests.post(
                f"{API}/sendDocument",
                data=data,
                files={"document": (os.path.basename(path), f)},
                timeout=120,
            )
        result = r.json()
        if not result.get("ok"):
            log_warn(f"tg_send_file failed: {result.get('description')}")
            tg_send(f"❌ Ошибка отправки: {result.get('description')}")
            return False
        return True
    except Exception as e:
        log_error("tg_send_file", e)
        return False


def _file_send_cb(path: str) -> str:
    key = hashlib.md5(path.encode()).hexdigest()[:10]
    _send_file_map[key] = path
    return f"sendfile:{key}"


def _detect_files_in_text(text: str) -> list[str]:
    import re
    found = []
    for match in re.finditer(r'[`\'"]?([/\w.\-]+\.\w+)[`\'"]?', text):
        raw = match.group(1)
        ext = os.path.splitext(raw)[1].lower()
        if ext not in _SENDABLE_EXTS:
            continue
        for candidate in [raw, os.path.join(WORK_DIR, raw)]:
            if os.path.isfile(candidate) and candidate not in found:
                found.append(candidate)
                break
    return found[:5]


def _files_keyboard(paths: list[str]) -> dict | None:
    if not paths:
        return None
    rows = []
    for p in paths:
        name = os.path.basename(p)
        size = os.path.getsize(p) // 1024
        rows.append([(f"📎 {name} ({size}кб)", _file_send_cb(p))])
    return kb(rows)


def cmd_files(search_dir: str = WORK_DIR) -> None:
    """Показывает последние файлы проекта с кнопками отправки."""
    matches = []
    for ext in _SENDABLE_EXTS:
        matches += glob_module.glob(f"{search_dir}/**/*{ext}", recursive=True)
        matches += glob_module.glob(f"{search_dir}/*{ext}")
    seen: set = set()
    unique = []
    for p in sorted(set(matches), key=os.path.getmtime, reverse=True):
        if p not in seen and ".tg_team" not in p and ".tg_downloads" not in p:
            seen.add(p)
            unique.append(p)
    if not unique:
        tg_send(f"📁 Файлов не найдено в:\n{search_dir}")
        return
    shown = unique[:12]
    proj_label = os.path.basename(search_dir.rstrip("/"))
    lines = [f"📁 Файлы: {proj_label} (свежие первые)", ""]
    for p in shown:
        rel = os.path.relpath(p, search_dir)
        size = os.path.getsize(p) // 1024
        lines.append(f"  {rel} ({size}кб)")
    rows = []
    for p in shown:
        name = os.path.basename(p)
        size = os.path.getsize(p) // 1024
        rows.append([(f"📎 {name} ({size}кб)", _file_send_cb(p))])
    tg_send("\n".join(lines), kb(rows))


def download_tg_file(file_id: str, hint_name: str = "") -> str | None:
    """Скачивает файл из Telegram. Возвращает путь внутри WORKSPACE_DL_DIR."""
    try:
        r = requests.get(f"{API}/getFile", params={"file_id": file_id}, timeout=10)
        file_path = r.json()["result"]["file_path"]
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        data = requests.get(url, timeout=30).content

        fname = hint_name or os.path.basename(file_path)
        ts = int(time.time())
        local_path = f"{WORKSPACE_DL_DIR}/{ts}_{fname}"
        with open(local_path, "wb") as f:
            f.write(data)
        log(f"Downloaded: {local_path} ({len(data) // 1024}kb)")
        return local_path
    except Exception as e:
        log(f"DOWNLOAD ERROR: {e}")
        return None


def file_hint(local_path: str) -> str:
    mime = mimetypes.guess_type(local_path)[0] or ""
    if "image" in mime:
        return f"[Изображение: {local_path}]"
    elif "text" in mime or local_path.endswith((".txt", ".py", ".kt", ".java", ".json", ".md")):
        return f"[Текстовый файл: {local_path}]"
    else:
        return f"[Файл: {local_path}]"


# ── ИНЛАЙН-МЕНЮ ──────────────────────────────────────────────
def kb(buttons: list[list[tuple[str, str]]]) -> dict:
    """Строит InlineKeyboardMarkup из [[('Текст','callback_data'), ...], ...]."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in buttons
        ]
    }


def send_agent_menu() -> None:
    """Меню выбора агента с текущей моделью и лимитами."""
    import rate_tracker
    active = get_active()
    m_active = get_model(active) if active != "openrouter" else "API"
    lines = [f"🤖 Активный: {AGENT_NAMES[active]} ({m_active})", ""]
    for ag in ("claude", "gemini", "qwen", "openrouter"):
        mark = "▶" if ag == active else " "
        m = get_model(ag) if ag != "openrouter" else "API"
        limit_str = rate_tracker.get_display(ag)
        limit_info = f" — {limit_str}" if limit_str else ""
        lines.append(f"  {mark} {AGENT_NAMES[ag]} ({m}){limit_info}")

    buttons = [
        [("🔵 Claude", "agent:claude"),    ("🟢 Gemini",     "agent:gemini")],
        [("🟡 Qwen",   "agent:qwen"),      ("🌐 OpenRouter", "agent:openrouter")],
        [("📋 Команды агента", "cmd:cmds"), ("🔧 Модели",    "cmd:models")],
        [("💬 Обсуждение",   "cmd:discuss"), ("🗑 Сброс",    "cmd:reset_menu")],
        [("📊 Контекст", "cmd:ctx"),        ("🧠 Память",    "cmd:memory")],
        [("👥 Команда агентов", "cmd:team"), ("🔧 Установка", "setup_check")],
        [("❓ Помощь",        "cmd:help")],
    ]
    tg_send("\n".join(lines), kb(buttons))


def send_commands_panel(agent: str | None = None, msg_id: int | None = None) -> None:
    """Панель команд для выбранного агента."""
    if agent is None:
        agent = get_active()
    cmds = AGENT_CLI_CMDS.get(agent, [])
    name = agent_label(agent)

    lines = [f"📋 Команды {name}", ""]
    for cmd, emoji, desc in cmds:
        lines.append(f"  {emoji} {cmd} — {desc}")
    if not cmds:
        lines.append("  (нет команд для этого агента)")

    rows = []
    for i in range(0, len(cmds), 2):
        row = []
        for cmd, emoji, desc in cmds[i:i + 2]:
            row.append((f"{emoji} {cmd}", f"cli_cmd:{agent}:{cmd}"))
        rows.append(row)

    if agent in ("gemini", "openrouter"):
        rows.append([("🗜 Сжать контекст", f"compress:{agent}")])
    rows.append([("🧠 Память", "cmd:memory")])
    rows.append([("← Агенты", "cmd:agent_menu")])

    text = "\n".join(lines)
    markup = kb(rows)
    if msg_id:
        tg_edit(msg_id, text, markup)
    else:
        tg_send(text, markup)


def send_model_menu(agent: str, message_id: int | None = None) -> None:
    """Меню выбора модели для агента."""
    if agent == "openrouter":
        send_or_model_menu(message_id)
        return
    current = get_model(agent)
    known = KNOWN_MODELS.get(agent, [])
    text = f"🔧 {AGENT_NAMES[agent]} — выбери модель\nТекущая: {current}"

    rows = []
    for i in range(0, len(known), 2):
        row = []
        for m in known[i:i + 2]:
            label = f"✓ {m}" if m == current else m
            row.append((label, f"model:{agent}:{m}"))
        rows.append(row)
    rows.append([("« Назад", "cmd:agent_menu")])

    markup = kb(rows)
    if message_id:
        tg_edit(message_id, text, markup)
    else:
        tg_send(text, markup)


def send_reset_menu(message_id: int | None = None) -> None:
    """Меню сброса сессий."""
    text = "🗑 Сброс сессии — выбери агента:"
    buttons = [
        [("Claude",  "reset:claude"),  ("Gemini", "reset:gemini")],
        [("Qwen",    "reset:qwen"),    ("GPT",    "reset:openrouter")],
        [("🔴 Все",  "reset:all"),     ("« Назад", "cmd:agent_menu")],
    ]
    markup = kb(buttons)
    if message_id:
        tg_edit(message_id, text, markup)
    else:
        tg_send(text, markup)


def send_models_menu(message_id: int | None = None) -> None:
    """Меню выбора агента для смены модели."""
    active = get_active()
    if active == "openrouter":
        send_or_model_menu(message_id)
        return
    text = "🔧 Выбери агента для смены модели:"
    buttons = [
        [("Claude",      "models:claude"),     ("Gemini",       "models:gemini")],
        [("Qwen",        "models:qwen"),        ("🌐 OpenRouter", "models:openrouter")],
        [("« Назад",     "cmd:agent_menu")],
    ]
    markup = kb(buttons)
    if message_id:
        tg_edit(message_id, text, markup)
    else:
        tg_send(text, markup)


def send_setup_menu(msg_id: int | None = None) -> None:
    """Панель проверки и установки агентов."""
    from agents import check_agents, get_openrouter_key  # ленивый импорт

    agents = check_agents()
    or_key = bool(get_openrouter_key())

    lines = ["🔧 Установка агентов", ""]
    rows: list = []

    icons = {"claude": "🔵", "gemini": "🟢", "qwen": "🟡"}
    for ag, info in agents.items():
        icon = icons[ag]
        if info["ok"]:
            lines.append(f"  ✅ {icon} {AGENT_NAMES[ag]} — {info['version']}")
            lines.append(f"     {info['path']}")
        else:
            lines.append(f"  ❌ {icon} {AGENT_NAMES[ag]} — не установлен")
            lines.append(f"     {AGENT_INSTALL_INFO[ag]['cmd']}")
            rows.append([(f"📦 Установить {AGENT_NAMES[ag]}", f"setup_install:{ag}")])

    lines.append("")
    lines.append(f"  {'✅' if or_key else '❌'} 🌐 OpenRouter — "
                 f"{'ключ задан' if or_key else 'нет ключа (/or /key sk-or-...)'}")

    if not rows:
        lines.append("")
        lines.append("Все агенты установлены ✅")

    rows.append([("🔄 Обновить статус", "setup_check"),
                 ("← Меню",            "cmd:agent_menu")])

    text = "\n".join(lines)
    markup = kb(rows)
    if msg_id:
        tg_edit(msg_id, text, markup)
    else:
        tg_send(text, markup)


def send_discuss_menu(msg_id: int | None = None) -> None:
    """Меню настройки режима обсуждения."""
    participants = discuss_get_agents()
    icons = {"claude": "🔵", "gemini": "🟢", "qwen": "🟡", "openrouter": "🌐"}

    lines = ["💬 Режим обсуждения", "",
             "Агенты отвечают последовательно, читая ответы предыдущих.",
             "", "Участники:"]
    for ag in DISCUSS_ALL_AGENTS:
        mark = "✅" if ag in participants else "❌"
        lines.append(f"  {mark} {icons[ag]} {AGENT_NAMES[ag]}")

    rows: list = []
    row: list = []
    for ag in DISCUSS_ALL_AGENTS:
        mark = "✅" if ag in participants else "❌"
        row.append((f"{mark} {AGENT_NAMES[ag]}", f"discuss_toggle:{ag}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([("▶️ Начать обсуждение", "discuss_start")])
    rows.append([("← Агенты", "cmd:agent_menu")])

    text = "\n".join(lines)
    markup = kb(rows)
    if msg_id:
        tg_edit(msg_id, text, markup)
    else:
        tg_send(text, markup)


# ── OPENROUTER МЕНЮ ───────────────────────────────────────────
def send_or_model_menu(msg_id: int | None = None) -> None:
    from agents import get_openrouter_key, _or_cb  # ленивый импорт

    key = get_openrouter_key()
    current = get_model("openrouter") or "не задана"

    key_status = f"🔑 Ключ: ...{key[-6:]}" if key else "🔑 Ключ: НЕ ЗАДАН"
    text = (
        f"🌐 OpenRouter\n"
        f"{key_status}\n"
        f"Модель: {current}\n\n"
        f"Поиск модели по слову:\n"
        f"  /or /model search <слово>\n"
        f"Установить ключ:\n"
        f"  /or /key sk-or-v1-..."
    )

    categories = [
        ("🤖 GPT",        "or_search:openai:0"),
        ("🧠 Claude",     "or_search:claude:0"),
        ("💎 Gemini",     "or_search:google:0"),
        ("🦙 Llama",      "or_search:llama:0"),
        ("🌪 Mistral",    "or_search:mistral:0"),
        ("🔥 DeepSeek",   "or_search:deepseek:0"),
        ("⚡ Qwen",       "or_search:qwen:0"),
        ("🆓 Бесплатные", "or_search:free:0"),
    ]
    rows = []
    for i in range(0, len(categories), 2):
        rows.append(list(categories[i:i + 2]))

    if key:
        rows.append([("🗑 Удалить ключ", "or_key_del"), ("← Назад", "cmd:agent_menu")])
    else:
        rows.append([("← Назад", "cmd:agent_menu")])

    markup = kb(rows)
    if msg_id:
        tg_edit(msg_id, text, markup)
    else:
        tg_send(text, markup)


def send_or_model_search(query: str, page: int = 0, msg_id: int | None = None) -> None:
    from agents import or_search_models, _or_cb, _or_model_label  # ленивый импорт

    PAGE = 6
    results = or_search_models(query)
    current = get_model("openrouter") or ""

    if not results:
        text = f"🔍 «{query}» — ничего не найдено\nПопробуй другой запрос"
        markup = kb([[("← Назад", "or_menu")]])
    else:
        total_pages = (len(results) + PAGE - 1) // PAGE
        page = max(0, min(page, total_pages - 1))
        chunk = results[page * PAGE:(page + 1) * PAGE]

        text = (f"🔍 OpenRouter: «{query}»\n"
                f"Найдено: {len(results)}  |  Стр. {page + 1}/{total_pages}\n"
                f"Текущая: {current or '—'}")

        rows = []
        for m in chunk:
            mid = m["id"]
            label = _or_model_label(m, current)
            rows.append([(label, _or_cb(mid))])

        nav = []
        if page > 0:
            nav.append(("◀ Пред.", f"or_search:{query}:{page - 1}"))
        if page < total_pages - 1:
            nav.append(("След. ▶", f"or_search:{query}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([("← Назад", "or_menu")])
        markup = kb(rows)

    if msg_id:
        tg_edit(msg_id, text, markup)
    else:
        tg_send(text, markup)
