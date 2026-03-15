#!/usr/bin/env python3
"""
Мультиагентный Telegram поллер с роутингом, персистентными сессиями,
общим контекстом диалога, поддержкой файлов/фото и pass-through команд CLI.

Команды агентов:
  /claude [текст|команда]  — Claude Code CLI
  /gemini [текст|команда]  — Gemini CLI
  /qwen   [текст|команда]  — Qwen Code CLI
  /gpt    [текст]          — OpenAI API
  <текст>                  — активный агент

Управление:
  /reset [агент|all]  — сбросить сессию
  /ctx                — размер контекста
  /sessions           — архив сессий
  /help               — помощь

Запуск: nohup python3 tg_agent.py >> /tmp/tg_agent.log 2>&1 &
"""

# ── Импорты из модулей проекта ───────────────────────────────
# Все символы доступны через этот модуль (backward-compat для team_mode.py).
import atexit
import glob as glob_mod
import json
import os
import queue
import subprocess
import threading
import time

import requests

from config import (
    API, BOT_TOKEN, ALLOWED_CHAT, TG_MAX_LEN,
    WORK_DIR, STATE_DIR, ARCHIVE_DIR,
    AGENT_NAMES, AGENT_INSTALL_INFO,
    CLAUDE_BIN, GEMINI_BIN, QWEN_BIN,
    CLAUDE_SESSION, CLAUDE_CTX_FILE,
    GEMINI_SESSION, GEMINI_CTX_FILE,
    QWEN_SESSION, QWEN_CTX_FILE,
    OPENROUTER_KEY_FILE,
    _AGENT_TIMEOUT, _last_request as _last_request_cfg,
    CTX_LIMITS, KNOWN_MODELS, DEFAULT_MODELS, AGENT_CLI_CMDS,
    _lock, ensure_dirs,
)
from logger import (
    _setup_logging, _thread_excepthook,
    log, log_debug, log_info, log_warn, log_error,
)
from context import (
    get_model, set_model, agent_label, cmd_model,
    get_active, set_active,
    shared_ctx_load, shared_ctx_save, shared_ctx_add,
    shared_ctx_for_prompt, shared_ctx_for_api,
    _load_session, _save_session, _get_ctx, _add_ctx, _reset_session,
    claude_rate_set, claude_rate_until, claude_rate_msg, _detect_rate_limit,
    memory_load, memory_add, memory_clear,
    discuss_get_agents, discuss_set_agents,
    discuss_await_set, discuss_await_clear, discuss_await_get,
    DISCUSS_ALL_AGENTS,
)
from agents import (
    _parse_cli_output, _is_gemini_capacity_error, _gemini_fallback_retry,
    _run_subprocess, _run_cli, _run_passthrough,
    _find_binary, check_agents, _get_effective_bin, run_startup_check,
    cancel_active_proc,
    ask_claude, ask_gemini, ask_qwen,
    compress_gemini, compress_openrouter,
    get_openrouter_key, set_openrouter_key, ask_openrouter,
    _or_fetch_models, or_search_models, _or_cb, _or_model_label, _or_id_map,
    OR_MODELS_TTL,
)
from ui import (
    _split_text, _tg_send_one, tg_send, tg_edit, tg_answer_cb, tg_typing,
    _KB_AGENT_LABELS, _KB_TEXT_TO_AGENT,
    _build_reply_keyboard, tg_set_keyboard,
    _SENDABLE_EXTS, _send_file_map,
    tg_send_file, _file_send_cb, _detect_files_in_text, _files_keyboard, cmd_files,
    download_tg_file, file_hint,
    kb, send_agent_menu, send_commands_panel, send_model_menu,
    send_reset_menu, send_models_menu,
    send_setup_menu, send_discuss_menu,
    send_or_model_menu, send_or_model_search,
)

import team_mode

_request_queue: "queue.Queue[tuple[str, str | None]]" = queue.Queue()
_cancel_event = threading.Event()
_worker_busy = threading.Event()

# Mutable _last_request (из config — изменяем здесь)
_last_request: dict = {}

AGENT_FN = {
    "claude":      ask_claude,
    "openrouter":  ask_openrouter,
    "gemini":      ask_gemini,
    "qwen":        ask_qwen,
}

PREFIX_MAP = {
    "/claude":     "claude",
    "/openrouter": "openrouter",
    "/or":         "openrouter",
    "/gemini":     "gemini",
    "/qwen":       "qwen",
}


# ── PID-LOCK (защита от дублей) ───────────────────────────────
from config import PID_FILE


def _check_single_instance() -> None:
    """Завершает старый процесс если уже запущен."""
    if os.path.exists(PID_FILE):
        try:
            old_pid = int(open(PID_FILE).read().strip())
            if old_pid != os.getpid():
                try:
                    os.kill(old_pid, 0)
                    log_warn(f"Завершаю старый процесс PID={old_pid}")
                    os.kill(old_pid, 15)
                    time.sleep(2)
                except ProcessLookupError:
                    pass
        except (ValueError, OSError):
            pass
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(PID_FILE) and os.remove(PID_FILE))


# ── СЛУЖЕБНЫЕ КОМАНДЫ ────────────────────────────────────────
def cmd_ctx() -> str:
    log_list = shared_ctx_load()
    shared_total = sum(len(m["content"]) for m in log_list)
    lines = [
        "📊 Контекст:",
        f"  Общий лог: {len(log_list)} сообщ. / {shared_total // 1000}k симв",
        f"  (Передаётся последних {20} сообщений)",
        "",
    ]
    for agent_key, agent_label_str, sf, cf in [
        ("claude", "Claude", CLAUDE_SESSION, CLAUDE_CTX_FILE),
        ("gemini", "Gemini", GEMINI_SESSION, GEMINI_CTX_FILE),
        ("qwen",   "Qwen  ", QWEN_SESSION,   QWEN_CTX_FILE),
    ]:
        sid = _load_session(sf) or "нет"
        ctx = _get_ctx(cf)
        ctx_warn, ctx_archive = CTX_LIMITS[agent_key]
        lines.append(
            f"  {agent_label_str}: ~{ctx // 1000}k/{ctx_archive // 1000}k симв"
            f"  (⚠️{ctx_warn // 1000}k) | sid: {sid[:12] if sid != 'нет' else 'нет'}"
        )
    or_model = get_model("openrouter") or "не задана"
    lines += [
        f"  OR    : {or_model} (API)",
        "",
        f"  Активный: {AGENT_NAMES[get_active()]}",
    ]
    return "\n".join(lines)


def cmd_sessions() -> str:
    files = sorted(glob_mod.glob(f"{ARCHIVE_DIR}/*"))
    if not files:
        return "📂 Архив пуст."
    lines = ["📂 Архив:"]
    for fp in files[-10:]:
        lines.append(f"  {os.path.basename(fp)}  ({os.path.getsize(fp) // 1024}kb)")
    return "\n".join(lines)


def cmd_reset(arg: str) -> str:
    agent = arg.strip().lower() if arg.strip() else get_active()
    if agent == "all":
        _reset_session(CLAUDE_SESSION, CLAUDE_CTX_FILE)
        _reset_session(GEMINI_SESSION, GEMINI_CTX_FILE)
        _reset_session(QWEN_SESSION, QWEN_CTX_FILE)
        ts = time.strftime("%Y%m%d_%H%M%S")
        ll = shared_ctx_load()
        if ll:
            with open(f"{ARCHIVE_DIR}/shared_{ts}.json", "w") as f:
                json.dump(ll, f, ensure_ascii=False)
        if os.path.exists(f"{STATE_DIR}/shared_context.json"):
            os.remove(f"{STATE_DIR}/shared_context.json")
        return "✅ Все сессии сброшены."
    if agent not in AGENT_NAMES:
        return f"⚠️ Неизвестно: {arg}. Используй: claude/gemini/qwen/openrouter/all"
    sf_map = {
        "claude":     (CLAUDE_SESSION, CLAUDE_CTX_FILE),
        "gemini":     (GEMINI_SESSION, GEMINI_CTX_FILE),
        "qwen":       (QWEN_SESSION,   QWEN_CTX_FILE),
        "openrouter": (None, None),
    }
    sf, cf = sf_map[agent]
    if sf:
        _reset_session(sf, cf)
    return f"✅ Сессия {AGENT_NAMES[agent]} сброшена."


# ── /all — параллельный запрос всем агентам ──────────────────
def cmd_all(prompt: str, file_path: str | None = None) -> None:
    shared_ctx_add("user", prompt + " [/all]")
    tg_send("🔀 Отправляю всем агентам...")

    agents = [
        ("claude",     ask_claude),
        ("gemini",     ask_gemini),
        ("qwen",       ask_qwen),
        ("openrouter", ask_openrouter),
    ]

    def run_one(ag: str, fn):
        try:
            reply = fn(prompt, file_path)
        except Exception as e:
            reply = f"❌ Ошибка: {e}"
        header = f"[{agent_label(ag)}]\n"
        full = header + reply
        if len(full) <= TG_MAX_LEN:
            tg_send(full)
        else:
            tg_send(header.rstrip())
            tg_send(reply)
        shared_ctx_add("assistant", reply, AGENT_NAMES[ag])

    for ag, fn in agents:
        threading.Thread(target=run_one, args=(ag, fn), daemon=True).start()


# ── РЕЖИМ ОБСУЖДЕНИЯ — runner ─────────────────────────────────
def run_discussion(question: str, file_path: str | None = None) -> None:
    """Агенты отвечают последовательно, читая ответы предыдущих."""
    participants = discuss_get_agents()
    if len(participants) < 2:
        tg_send("⚠️ Нужно минимум 2 агента. Настрой в /menu → 💬 Обсуждение")
        return

    icons = {"claude": "🔵", "gemini": "🟢", "qwen": "🟡", "openrouter": "🌐"}
    names = AGENT_NAMES

    names_str = " → ".join(f"{icons[a]} {names[a]}" for a in participants)
    tg_send(f"💬 Обсуждение начато\n{names_str}\n\nВопрос: {question}")
    shared_ctx_add("user", f"[Обсуждение] {question}")

    answers: list[tuple[str, str]] = []

    for i, ag in enumerate(participants):
        label = f"{icons[ag]} {names[ag]}"
        is_last = (i == len(participants) - 1)

        parts = [f"Вопрос для обсуждения: {question}"]
        if file_path:
            parts.append(f"[Прикреплённый файл: {os.path.basename(file_path)}]")
        if answers:
            parts.append("\nМнения других участников обсуждения:")
            for prev_ag, prev_ans in answers:
                prev_label = f"{icons[prev_ag]} {names[prev_ag]}"
                parts.append(f"\n--- {prev_label} ---\n{prev_ans}")
            if is_last:
                parts.append(
                    "\nТы последний участник. Проанализируй все мнения выше и дай финальный синтез — "
                    "лучшее решение с учётом всех предложений."
                )
            else:
                parts.append(
                    "\nДай своё мнение по вопросу, учитывая сказанное выше. "
                    "Можешь соглашаться, дополнять или предлагать альтернативу."
                )
        else:
            parts.append("\nДай своё мнение первым — без оглядки на других.")

        prompt = "\n".join(parts)

        ph = tg_send(f"⏳ {label} думает...")
        ph_id = ph["message_id"] if ph else None

        result_box: list[str] = []
        done_evt = threading.Event()

        def _worker(fn=AGENT_FN[ag], p=prompt, fp=file_path):
            result_box.append(fn(p, fp))
            done_evt.set()

        threading.Thread(target=_worker, daemon=True).start()
        t_start = time.time()
        while not done_evt.wait(timeout=5):
            elapsed = int(time.time() - t_start)
            if ph_id:
                tg_edit(ph_id, f"⏳ {label} думает... {elapsed}с")
            tg_typing()

        reply = result_box[0] if result_box else "❌ Нет ответа"
        answers.append((ag, reply))
        shared_ctx_add("assistant", reply, names[ag])

        step = f"[{i + 1}/{len(participants)}] {label}"
        full = f"{step}\n\n{reply}"
        if ph_id:
            if len(full) <= TG_MAX_LEN:
                tg_edit(ph_id, full)
            else:
                tg_edit(ph_id, f"{step} — ответ ниже:")
                tg_send(reply)
        else:
            tg_send(full)


# ── ВЕБ-ПОИСК ────────────────────────────────────────────────
def cmd_web_search(query: str) -> None:
    """Search web via DuckDuckGo (free, no API key) and send results to active agent."""
    import urllib.request
    import urllib.parse

    tg_send(f"🔍 Ищу: {query}...")
    try:
        url = ("https://api.duckduckgo.com/?q=" + urllib.parse.quote(query)
               + "&format=json&no_html=1&skip_disambig=1")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        results = []
        if data.get("AbstractText"):
            results.append(f"📖 {data['AbstractText'][:500]}")
        if data.get("RelatedTopics"):
            for t in data["RelatedTopics"][:5]:
                if isinstance(t, dict) and t.get("Text"):
                    results.append(f"• {t['Text'][:150]}")

        if results:
            search_result = f"🔍 Результаты поиска: {query}\n\n" + "\n".join(results)
            tg_send(search_result)
            agent = get_active()
            prompt = f"Based on these search results, answer in Russian:\n{search_result}\n\nQuestion: {query}"
            threading.Thread(target=lambda: _web_search_reply(agent, prompt), daemon=True).start()
            return
    except Exception as e:
        log_warn(f"Web search error: {e}")

    agent = get_active()
    prompt = f"Search the internet for information about: {query}\nAnswer in Russian with current information."
    threading.Thread(target=lambda: _web_search_reply(agent, prompt), daemon=True).start()


def _web_search_reply(agent: str, prompt: str) -> None:
    try:
        reply = AGENT_FN[agent](prompt, None)
        lbl = agent_label(agent)
        tg_send(f"[{lbl}]\n{reply}")
        shared_ctx_add("assistant", reply, AGENT_NAMES[agent])
    except Exception as e:
        tg_send(f"❌ Ошибка агента при поиске: {e}")


# ── GIT ПАНЕЛЬ ────────────────────────────────────────────────
def cmd_git(subcmd: str = "") -> None:
    proj = team_mode._cur_project()
    cwd = team_mode._project_dir(proj) if proj else WORK_DIR

    if not subcmd:
        try:
            r = subprocess.run(["git", "status", "--short"], cwd=cwd,
                               capture_output=True, text=True, timeout=10)
            status = r.stdout.strip() or "чисто"
            r2 = subprocess.run(["git", "log", "--oneline", "-5"], cwd=cwd,
                                capture_output=True, text=True, timeout=10)
            log_lines = r2.stdout.strip() or "нет коммитов"
        except Exception:
            status = "git недоступен"
            log_lines = ""

        text = f"🔀 Git — {os.path.basename(cwd)}\n\nСтатус:\n{status}\n\nПоследние коммиты:\n{log_lines}"
        markup = kb([
            [("📊 Status", "git:status"), ("📝 Diff", "git:diff")],
            [("📋 Log",    "git:log"),    ("➕ Add + Commit", "git:add_commit")],
            [("⬆️ Push",   "git:push"),   ("⬇️ Pull", "git:pull")],
            [("← Назад",  "cmd:agent_menu")],
        ])
        tg_send(text, markup)
        return

    cmds = {
        "status": ["git", "status"],
        "diff":   ["git", "diff", "--stat"],
        "log":    ["git", "log", "--oneline", "-15"],
        "pull":   ["git", "pull"],
        "push":   ["git", "push"],
    }

    if subcmd in cmds:
        try:
            r = subprocess.run(cmds[subcmd], cwd=cwd, capture_output=True,
                               text=True, timeout=30)
            out = (r.stdout + r.stderr).strip()[:3000] or "(нет вывода)"
            tg_send(f"🔀 git {subcmd}:\n\n{out}")
        except Exception as e:
            tg_send(f"❌ git {subcmd}: {e}")
    elif subcmd == "add_commit":
        tg_send("📝 Введи сообщение для коммита:", kb([[("❌ Отмена", "git:cancel")]]))
        with open(f"{STATE_DIR}/git_commit_await.txt", "w") as f:
            f.write(cwd)


# ── SEND TO AGENT (для /retry) ────────────────────────────────
def send_to_agent(agent: str, prompt: str, file_path: str | None = None) -> None:
    """Отправляет промпт конкретному агенту и публикует ответ в чат."""
    fn = AGENT_FN.get(agent)
    if not fn:
        tg_send(f"⚠️ Неизвестный агент: {agent}")
        return
    try:
        reply = fn(prompt, file_path)
    except Exception as e:
        reply = f"❌ Ошибка: {e}"
    lbl = agent_label(agent)
    shared_ctx_add("assistant", reply, AGENT_NAMES[agent])
    found_files = _detect_files_in_text(reply)
    file_markup = _files_keyboard(found_files) if found_files else None
    full = f"[{lbl}]\n{reply}"
    if len(full) <= TG_MAX_LEN:
        tg_send(full, file_markup)
    else:
        tg_send(f"[{lbl}]")
        tg_send(reply, file_markup)


# ── ОЧЕРЕДЬ ЗАПРОСОВ ──────────────────────────────────────────
def _queue_worker() -> None:
    """Persistent daemon thread. Processes one request at a time."""
    while True:
        try:
            item = _request_queue.get(timeout=1)
        except queue.Empty:
            continue
        try:
            # Discard items dequeued during a cancel window.
            # _cancel_event is cleared HERE on the discard path only.
            # On the non-cancel path, route_and_reply clears it at the
            # commit point (after placeholder is sent).
            if _cancel_event.is_set():
                _cancel_event.clear()
                continue
            _worker_busy.set()
            text, file_path = item
            try:
                route_and_reply(text, file_path)
            finally:
                _worker_busy.clear()
        finally:
            _request_queue.task_done()


# ── РОУТЕР ────────────────────────────────────────────────────
def route_and_reply(text: str, file_path: str | None = None) -> None:
    global _last_request
    tg_typing()
    text = text.strip()
    log_info(f"MSG: {text[:120]!r}" + (f" + file:{os.path.basename(file_path)}" if file_path else ""))

    # Режим ожидания сообщения git commit
    git_await_file = f"{STATE_DIR}/git_commit_await.txt"
    if os.path.exists(git_await_file) and text and not text.startswith("/"):
        try:
            cwd = open(git_await_file).read().strip()
            os.remove(git_await_file)
        except Exception:
            cwd = WORK_DIR

        def _do_commit(msg=text, d=cwd):
            try:
                subprocess.run(["git", "add", "-A"], cwd=d, timeout=10)
                r = subprocess.run(["git", "commit", "-m", msg], cwd=d,
                                   capture_output=True, text=True, timeout=30)
                out = (r.stdout + r.stderr).strip()
                tg_send(f"✅ Git commit:\n{out[:500]}")
            except Exception as e:
                tg_send(f"❌ {e}")

        threading.Thread(target=_do_commit, daemon=True).start()
        return

    # Режим ожидания code review
    if team_mode.code_review_await_get() and text and not text.startswith("/"):
        team_mode.code_review_await_clear()
        threading.Thread(target=team_mode.run_code_review, args=(text,), daemon=True).start()
        return

    # Режим ожидания команды сборки
    if team_mode.build_cmd_await_get() and text and not text.startswith("/"):
        team_mode.build_cmd_await_clear()
        cmd_str = "" if text.strip().lower() in ("авто", "auto") else text.strip()
        with team_mode._team_lock:
            s = team_mode._load_state()
            s["build_cmd"] = cmd_str
            s["build_apk"] = True
            team_mode._save_state(s)
        label = "авто-определение" if not cmd_str else cmd_str
        tg_send(f"✅ Команда сборки: {label}\n🔨 Сборка APK включена.")
        return

    # Режим ожидания названия нового проекта
    if team_mode.project_await_get() and text and not text.startswith("/"):
        team_mode.project_await_clear()
        threading.Thread(target=team_mode.create_project_and_await_task,
                         args=(text,), daemon=True).start()
        return

    # Режим ожидания задачи для командного режима
    if team_mode.task_await_get() and text and not text.startswith("/"):
        team_mode.task_await_clear()
        threading.Thread(target=team_mode.start_task, args=(text,), daemon=True).start()
        return

    # Режим ожидания вопроса для обсуждения
    if discuss_await_get() and text and not text.startswith("/"):
        discuss_await_clear()
        threading.Thread(target=run_discussion, args=(text, file_path), daemon=True).start()
        return

    # Кнопки постоянной клавиатуры — агент
    if text in _KB_TEXT_TO_AGENT:
        ag = _KB_TEXT_TO_AGENT[text]
        set_active(ag)
        tg_send(f"▶ Активный агент: {agent_label(ag)}", _build_reply_keyboard(ag))
        return

    # Кнопки постоянной клавиатуры — команды с эмодзи-префиксом
    _KB_CMD_MAP = {
        "📋 /menu": "/menu", "🔀 /all": "/all",
        "💬 /discuss": "/discuss", "📁 /files": "/files",
        "📊 /ctx": "/ctx", "🔧 /setup": "/setup",
        "🧠 /memory": "/memory", "❓ /help": "/help",
    }
    if text in _KB_CMD_MAP:
        text = _KB_CMD_MAP[text]

    # Служебные команды
    if text in ("/menu", "/start"):
        send_agent_menu()
        return

    if text == "/setup":
        threading.Thread(target=send_setup_menu, daemon=True).start()
        return

    if text == "/retry":
        req = _last_request.copy()
        if req.get("prompt"):
            ag = req["agent"]
            tg_send(f"🔄 Повторяю запрос к {agent_label(ag)}...")
            threading.Thread(
                target=send_to_agent,
                args=(ag, req["prompt"], req.get("file_path")),
                daemon=True,
            ).start()
        else:
            tg_send("⚠️ Нет сохранённого запроса для повтора.")
        return

    if text.startswith("/timeout"):
        parts = text.split(maxsplit=2)
        if len(parts) == 1:
            lines = ["⏱ Таймауты агентов:"]
            for ag, secs in _AGENT_TIMEOUT.items():
                lines.append(f"  {ag}: {secs}с ({secs // 60} мин)")
            lines.append("\nИзменить: /timeout gemini 900")
            tg_send("\n".join(lines))
        elif len(parts) == 3:
            target, val = parts[1].lower(), parts[2]
            try:
                secs = int(val)
                if target == "all":
                    for k in _AGENT_TIMEOUT:
                        _AGENT_TIMEOUT[k] = secs
                    tg_send(f"✅ Таймаут всех агентов: {secs}с ({secs // 60} мин)")
                elif target in _AGENT_TIMEOUT:
                    _AGENT_TIMEOUT[target] = secs
                    tg_send(f"✅ Таймаут {target}: {secs}с ({secs // 60} мин)")
                else:
                    tg_send(f"❌ Неизвестный агент: {target}")
            except ValueError:
                tg_send("❌ Укажи число секунд: /timeout gemini 900")
        else:
            tg_send("Использование: /timeout [агент] [секунды]\nПример: /timeout gemini 900")
        return

    if text.startswith("/team"):
        threading.Thread(target=team_mode.handle_command, args=(text,), daemon=True).start()
        return

    if text.startswith("/search ") or text.startswith("/s "):
        query = text.split(maxsplit=1)[1].strip() if " " in text else ""
        if query:
            threading.Thread(target=cmd_web_search, args=(query,), daemon=True).start()
        else:
            tg_send("Использование: /search запрос")
        return

    if text == "/git" or text.startswith("/git "):
        sub = text[5:].strip() if text.startswith("/git ") else ""
        threading.Thread(target=cmd_git, args=(sub,), daemon=True).start()
        return

    if text == "/help":
        tg_send(
            "Агенты (общий контекст):\n"
            f"/claude      — {agent_label('claude')}\n"
            f"/gemini      — {agent_label('gemini')}\n"
            f"/qwen        — {agent_label('qwen')}\n"
            f"/openrouter  — OpenRouter API (/or — сокращение)\n\n"
            "Модели:\n"
            "  /claude /model list           — список\n"
            "  /claude /model opus           — сменить\n"
            "  /openrouter /model search gpt — поиск по каталогу\n"
            "  (аналогично /gemini, /qwen)\n\n"
            "Pass-through CLI:\n"
            "  /claude /compact\n"
            "  /gemini /compress\n"
            "  /qwen /summary\n\n"
            "Управление:\n"
            "/reset [агент|all] — сброс сессии\n"
            "/ctx               — контекст\n"
            "/sessions          — архив\n"
            "/all <вопрос>      — спросить всех агентов сразу\n\n"
            "Файлы:\n"
            "/files             — список файлов проекта (кнопки отправки)\n"
            "/send <путь>       — отправить файл в Telegram\n\n"
            "Память:\n"
            "/remember <факт>   — сохранить факт (виден всем агентам)\n"
            "/memory            — показать память\n"
            "/forget            — очистить память\n\n"
            "Веб и Git:\n"
            "/search <запрос>   — веб-поиск (DuckDuckGo)\n"
            "/s <запрос>        — сокращение /search\n"
            "/git               — Git панель\n\n"
            "При зависании агента:\n"
            "/retry             — повторить последний запрос\n"
            "/timeout gemini 900 — изменить таймаут (сек)\n"
            "/timeout           — показать все таймауты\n\n"
            "Файлы/фото: просто отправь."
        )
        return

    if text == "/ctx":
        tg_send(cmd_ctx())
        return
    if text == "/sessions":
        tg_send(cmd_sessions())
        return
    if text.startswith("/reset"):
        parts = text.split(maxsplit=1)
        tg_send(cmd_reset(parts[1] if len(parts) > 1 else ""))
        return

    if text.startswith("/all ") or text == "/all":
        prompt_all = text[4:].strip()
        if not prompt_all and file_path:
            prompt_all = "Обработай этот файл."
        if prompt_all or file_path:
            threading.Thread(target=cmd_all, args=(prompt_all, file_path), daemon=True).start()
        else:
            tg_send("Использование: /all <вопрос>")
        return

    if text.startswith("/discuss ") or text == "/discuss":
        q = text[len("/discuss"):].strip()
        if not q and file_path:
            q = "Обработай и обсуди этот файл."
        if q or file_path:
            threading.Thread(target=run_discussion, args=(q, file_path), daemon=True).start()
        else:
            send_discuss_menu()
        return

    if text.startswith("/remember "):
        fact = text[len("/remember "):].strip()
        if fact:
            memory_add(fact)
            tg_send(f"🧠 Запомнено: {fact}")
        else:
            tg_send("Использование: /remember <факт>")
        return

    if text.startswith("/send "):
        path = text[6:].strip().strip('"\'`')
        if not os.path.isabs(path):
            path = os.path.join(WORK_DIR, path)
        threading.Thread(target=tg_send_file, args=(path,), daemon=True).start()
        return

    if text == "/files" or text.startswith("/files "):
        search = text[7:].strip()
        if not search:
            proj = team_mode._cur_project()
            search = team_mode._project_dir(proj) if proj else WORK_DIR
        elif not os.path.isabs(search):
            search = os.path.join(WORK_DIR, search)
        threading.Thread(target=cmd_files, args=(search,), daemon=True).start()
        return

    if text == "/memory":
        mem = memory_load()
        tg_send(f"🧠 Память:\n\n{mem}\n\n/forget — очистить"
                if mem else "🧠 Память пуста. /remember <факт>")
        return

    if text == "/forget":
        memory_clear()
        tg_send("🧠 Память очищена.")
        return

    # Определяем агента и промпт
    agent = None
    prompt = None

    for prefix, ag in PREFIX_MAP.items():
        if text == prefix or text.startswith(prefix + " "):
            agent = ag
            rest = text[len(prefix):].strip()
            prompt = rest if rest else None
            break

    if agent:
        set_active(agent)

        if agent == "openrouter" and prompt:
            if prompt.startswith("/key"):
                key_val = prompt[len("/key"):].strip()
                if key_val:
                    set_openrouter_key(key_val)
                    tg_send(f"✅ OpenRouter ключ сохранён: ...{key_val[-6:]}")
                else:
                    tg_send("Укажи ключ: /or /key sk-or-v1-...")
                return
            if prompt.startswith("/search"):
                query = prompt[len("/search"):].strip() or "openai"
                threading.Thread(target=send_or_model_search, args=(query,), daemon=True).start()
                return

        if prompt and prompt.startswith("/model"):
            model_arg = prompt[len("/model"):].strip()
            if agent == "openrouter" and model_arg.startswith("search"):
                query = model_arg[len("search"):].strip() or "openai"
                threading.Thread(target=send_or_model_search, args=(query,), daemon=True).start()
            else:
                tg_send(cmd_model(agent, model_arg))
            return

        if prompt is None and file_path is None:
            tg_send(f"🔄 Активный агент: {agent_label(agent)}")
            return
        if prompt is None:
            prompt = "Обработай этот файл."
    else:
        agent = get_active()
        prompt = text

    if not prompt and file_path:
        prompt = "Опиши / обработай этот файл."

    _last_request = {"agent": agent, "prompt": prompt, "file_path": file_path}

    log_entry = prompt
    if file_path:
        log_entry += f" {file_hint(file_path)}"
    shared_ctx_add("user", log_entry)

    lbl = agent_label(agent)
    timeout_secs = _AGENT_TIMEOUT.get(agent, 300)
    timeout_mins = timeout_secs // 60
    cancel_markup = kb([[("🛑 Отмена", "cancel_current")]])
    ph = tg_send(f"⏳ {lbl} думает... (макс {timeout_mins} мин)", cancel_markup)
    ph_id = ph["message_id"] if ph else None

    # Commit point: cancel may have fired between dequeue and placeholder send.
    # Edit the new placeholder and return if cancel is set.
    # The cancel handler may concurrently edit the same placeholder — this is a
    # benign double-edit: tg_edit handles 400 "message not modified" gracefully.
    if _cancel_event.is_set():
        if ph_id:
            tg_edit(ph_id, "❌ Запрос отменён")
        _cancel_event.clear()
        return
    _cancel_event.clear()  # Committed to this request — clear any stale cancel state

    result_box: list[str] = []

    def _worker():
        result_box.append(AGENT_FN[agent](prompt, file_path))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t_start = time.time()

    while t.is_alive():
        time.sleep(5)
        if _cancel_event.is_set():
            break
        if not t.is_alive():
            break
        elapsed = int(time.time() - t_start)
        if ph_id:
            tg_edit(ph_id, f"⏳ {lbl} думает... {elapsed}с / {timeout_secs}с", cancel_markup)
        tg_typing()

    t.join()
    if _cancel_event.is_set():
        _cancel_event.clear()
        return  # Cancel handler already edited placeholder to "❌ Запрос отменён"
    reply = result_box[0] if result_box else "❌ Нет ответа"

    shared_ctx_add("assistant", reply, AGENT_NAMES[agent])
    log(f"{AGENT_NAMES[agent]}: {reply[:80]}...")

    found_files = _detect_files_in_text(reply)
    file_markup = _files_keyboard(found_files) if found_files else None

    is_timeout = "не ответил за" in reply or "думает >" in reply
    if is_timeout:
        retry_markup = kb([[("🔄 Повторить запрос", "retry_last")]])
        if ph_id:
            tg_edit(ph_id, f"[{lbl}]\n{reply}", retry_markup)
        else:
            tg_send(f"[{lbl}]\n{reply}", retry_markup)
        return

    header = f"[{lbl}]\n"
    full = header + reply
    if ph_id:
        if len(full) <= TG_MAX_LEN:
            tg_edit(ph_id, full, file_markup)
        else:
            tg_edit(ph_id, f"[{lbl}] — ответ ниже:")
            tg_send(reply, file_markup)
    else:
        tg_send(full, file_markup)

    for fp in found_files:
        ext = os.path.splitext(fp)[1].lower()
        if ext in (".apk", ".aab", ".ipa", ".exe"):
            tg_send_file(fp, caption=f"📦 {os.path.basename(fp)}")


# ── ОБРАБОТКА ОБНОВЛЕНИЙ ─────────────────────────────────────
def handle_callback(cb: dict) -> None:
    """Обрабатывает нажатие инлайн-кнопок."""
    if (cb.get("from", {}).get("id") != ALLOWED_CHAT and
            cb.get("message", {}).get("chat", {}).get("id") != ALLOWED_CHAT):
        return

    data = cb.get("data", "")
    cb_id = cb["id"]
    msg_id = cb.get("message", {}).get("message_id")

    if data == "cancel_current":
        tg_answer_cb(cb_id, "❌ Отменяю...")
        cancel_active_proc()
        _cancel_event.set()
        while not _request_queue.empty():
            try:
                _request_queue.get_nowait()
                _request_queue.task_done()
            except queue.Empty:
                break
        if msg_id:
            tg_edit(msg_id, "❌ Запрос отменён")
        return

    elif data == "retry_last":
        tg_answer_cb(cb_id, "🔄 Повторяю запрос...")
        req = _last_request.copy()
        if req.get("prompt"):
            threading.Thread(
                target=send_to_agent,
                args=(req["agent"], req["prompt"], req.get("file_path")),
                daemon=True,
            ).start()
        else:
            tg_send("⚠️ Нет сохранённого запроса для повтора.")

    elif data.startswith("agent:"):
        agent = data.split(":", 1)[1]
        if agent in AGENT_NAMES:
            set_active(agent)
            tg_answer_cb(cb_id, f"Агент: {agent_label(agent)}")
            send_agent_menu()

    elif data.startswith("model:"):
        _, agent, model = data.split(":", 2)
        set_model(agent, model)
        sf_map = {
            "claude": (CLAUDE_SESSION, CLAUDE_CTX_FILE),
            "gemini": (GEMINI_SESSION, GEMINI_CTX_FILE),
            "qwen":   (QWEN_SESSION,   QWEN_CTX_FILE),
        }
        sf, cf = sf_map.get(agent, (None, None))
        if sf:
            _reset_session(sf, cf)
        tg_answer_cb(cb_id, f"✅ {AGENT_NAMES[agent]}: {model}")
        send_model_menu(agent, msg_id)

    elif data.startswith("models:"):
        agent = data.split(":", 1)[1]
        tg_answer_cb(cb_id)
        send_model_menu(agent, msg_id)

    elif data.startswith("reset:"):
        target = data.split(":", 1)[1]
        tg_answer_cb(cb_id, cmd_reset(target))
        send_agent_menu()

    elif data.startswith("team_") or data == "team_noop":
        team_mode.handle_team_callback(cb_id, msg_id, data)

    elif data.startswith("git:"):
        sub = data[4:]
        tg_answer_cb(cb_id)
        if sub == "cancel":
            try:
                os.remove(f"{STATE_DIR}/git_commit_await.txt")
            except Exception:
                pass
            tg_send("Отменено.")
        else:
            threading.Thread(target=cmd_git, args=(sub,), daemon=True).start()

    elif data.startswith("cli_cmd:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            _, ag, cli_cmd = parts
            tg_answer_cb(cb_id, f"▶ {cli_cmd}")
            bin_map = {"claude": CLAUDE_BIN, "gemini": GEMINI_BIN, "qwen": QWEN_BIN}
            sf_map = {"claude": CLAUDE_SESSION, "gemini": GEMINI_SESSION, "qwen": QWEN_SESSION}
            binary = bin_map.get(ag)
            sf = sf_map.get(ag)
            back_btn = kb([[("← Назад к командам", f"cmd:cmds:{ag}")]])
            if binary and sf:
                if msg_id:
                    tg_edit(msg_id, f"⏳ {agent_label(ag)}: выполняю {cli_cmd}...", back_btn)

                def _exec_passthrough(ag=ag, cli_cmd=cli_cmd, binary=binary, sf=sf, mid=msg_id):
                    reply = _run_passthrough(binary, sf, AGENT_NAMES[ag], cli_cmd)
                    text = f"[{agent_label(ag)}] {cli_cmd}\n\n{reply}"
                    if mid:
                        if len(text) <= TG_MAX_LEN:
                            tg_edit(mid, text, back_btn)
                        else:
                            tg_edit(mid, f"[{agent_label(ag)}] {cli_cmd} — ответ ниже:", back_btn)
                            tg_send(reply)
                    else:
                        tg_send(text)

                threading.Thread(target=_exec_passthrough, daemon=True).start()
            else:
                tg_send(f"⚠️ CLI-команды для {ag} не поддерживаются")

    elif data.startswith("cmd:cmds"):
        parts = data.split(":", 2)
        ag = parts[2] if len(parts) == 3 else None
        tg_answer_cb(cb_id)
        send_commands_panel(ag, msg_id=msg_id)

    elif data.startswith("or_search:"):
        parts = data.split(":", 2)
        query = parts[1] if len(parts) > 1 else "openai"
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        tg_answer_cb(cb_id, f"🔍 {query}…")
        threading.Thread(target=send_or_model_search, args=(query, page, msg_id), daemon=True).start()

    elif data.startswith("or_model:"):
        model_id = data[len("or_model:"):]
        if model_id.startswith("~"):
            model_id = _or_id_map.get(model_id[1:], model_id[1:])
        set_model("openrouter", model_id)
        tg_answer_cb(cb_id, f"✅ {model_id.split('/')[-1]}")
        send_or_model_menu(msg_id)

    elif data == "or_menu":
        tg_answer_cb(cb_id)
        send_or_model_menu(msg_id)

    elif data == "or_key_del":
        tg_answer_cb(cb_id, "Ключ удалён")
        try:
            os.remove(OPENROUTER_KEY_FILE)
        except FileNotFoundError:
            pass
        send_or_model_menu(msg_id)

    elif data == "cmd:ctx":
        tg_answer_cb(cb_id)
        tg_send(cmd_ctx())

    elif data == "cmd:agent_menu":
        tg_answer_cb(cb_id)
        send_agent_menu()

    elif data == "cmd:models":
        tg_answer_cb(cb_id)
        send_models_menu(msg_id)

    elif data == "cmd:reset_menu":
        tg_answer_cb(cb_id)
        send_reset_menu(msg_id)

    elif data == "cmd:team":
        tg_answer_cb(cb_id)
        team_mode.send_team_menu(msg_id)

    elif data == "cmd:discuss":
        tg_answer_cb(cb_id)
        send_discuss_menu(msg_id)

    elif data == "setup_check":
        tg_answer_cb(cb_id, "🔄 Проверяю...")
        threading.Thread(target=send_setup_menu, args=(msg_id,), daemon=True).start()

    elif data.startswith("setup_install:"):
        ag = data.split(":", 1)[1]
        info = AGENT_INSTALL_INFO.get(ag)
        if not info:
            tg_answer_cb(cb_id, "⚠️ Неизвестный агент")
        else:
            tg_answer_cb(cb_id, f"📦 Устанавливаю {AGENT_NAMES[ag]}...")

            def _do_install(ag=ag, info=info, mid=msg_id):
                back = kb([[("← Назад к установке", "setup_check")]])
                tg_send(f"📦 Устанавливаю {AGENT_NAMES[ag]}...\n`{info['cmd']}`")
                try:
                    env = os.environ.copy()
                    proc = subprocess.Popen(
                        info["cmd"].split(), stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, env=env,
                    )
                    output_lines = []
                    for line in proc.stdout:
                        output_lines.append(line.rstrip())
                        if len(output_lines) % 10 == 0:
                            tg_send("⏳ " + "\n".join(output_lines[-5:]))
                    proc.wait(timeout=120)
                    rc = proc.returncode
                    tail = "\n".join(output_lines[-15:])
                    if rc == 0:
                        tg_send(
                            f"✅ {AGENT_NAMES[ag]} установлен!\n\n"
                            f"ℹ️ {info['note']}\n\n"
                            f"Последние строки:\n{tail}",
                            back,
                        )
                    else:
                        tg_send(f"❌ Ошибка (код {rc}):\n{tail}", back)
                except Exception as e:
                    tg_send(f"❌ Ошибка установки: {e}", back)
                send_setup_menu()

            threading.Thread(target=_do_install, daemon=True).start()

    elif data == "discuss_start":
        discuss_await_set()
        tg_answer_cb(cb_id, "✏️ Напиши вопрос")
        participants = discuss_get_agents()
        icons = {"claude": "🔵", "gemini": "🟢", "qwen": "🟡", "openrouter": "🌐"}
        names_str = " → ".join(f"{icons[a]} {AGENT_NAMES[a]}" for a in participants)
        tg_edit(msg_id,
                f"💬 Режим обсуждения активен\n\n{names_str}\n\n✏️ Напиши вопрос для обсуждения:",
                kb([[("❌ Отмена", "discuss_cancel")]]))

    elif data == "discuss_cancel":
        discuss_await_clear()
        tg_answer_cb(cb_id, "Отменено")
        send_discuss_menu(msg_id)

    elif data.startswith("discuss_toggle:"):
        ag = data.split(":", 1)[1]
        participants = discuss_get_agents()
        if ag in participants:
            if len(participants) > 2:
                participants.remove(ag)
        else:
            participants.append(ag)
            participants = [a for a in DISCUSS_ALL_AGENTS if a in participants]
        discuss_set_agents(participants)
        tg_answer_cb(cb_id)
        send_discuss_menu(msg_id)

    elif data.startswith("compress:"):
        agent = data.split(":", 1)[1]
        tg_answer_cb(cb_id, "⏳ Сжимаю...")

        def _do_compress(ag=agent):
            if ag == "gemini":
                result = compress_gemini()
            elif ag == "openrouter":
                result = compress_openrouter()
            else:
                result = f"⚠️ Сжатие для {ag} не реализовано здесь."
            tg_send(result)

        threading.Thread(target=_do_compress, daemon=True).start()

    elif data.startswith("sendfile:"):
        key = data[len("sendfile:"):]
        path = _send_file_map.get(key)
        if path:
            tg_answer_cb(cb_id, "📎 Отправляю...")
            threading.Thread(target=tg_send_file, args=(path,), daemon=True).start()
        else:
            tg_answer_cb(cb_id, "⚠️ Путь не найден (перезапусти /files)")

    elif data == "cmd:memory":
        tg_answer_cb(cb_id)
        mem = memory_load()
        if mem:
            tg_send(f"🧠 Память:\n\n{mem}\n\n/forget — очистить")
        else:
            tg_send("🧠 Память пуста.\n\nДобавить: /remember <факт>")

    elif data == "cmd:help":
        tg_answer_cb(cb_id)
        tg_send(
            "Команды текстом:\n"
            "/menu — открыть меню\n"
            "/claude /gemini /qwen /openrouter — переключить агента\n"
            "/claude /compact — CLI команда\n"
            "/reset [all] — сброс сессии\n"
            "/ctx — контекст\n"
            "/sessions — архив\n"
            "/all <вопрос> — спросить всех\n"
            "/remember <факт> — сохранить в память\n"
            "/memory — показать память\n"
            "/forget — очистить память\n\n"
            "Файлы/фото: просто отправь.\n"
            "Текст без префикса → активному агенту."
        )


def process_update(upd: dict) -> None:
    cb = upd.get("callback_query")
    if cb:
        threading.Thread(target=handle_callback, args=(cb,), daemon=True).start()
        return

    msg = upd.get("message") or upd.get("channel_post")
    if not msg:
        return
    if msg.get("chat", {}).get("id") != ALLOWED_CHAT:
        return

    text = msg.get("text", "").strip()
    caption = msg.get("caption", "").strip()
    photo = msg.get("photo")
    document = msg.get("document")
    voice = msg.get("voice")
    audio = msg.get("audio")
    video = msg.get("video")
    sticker = msg.get("sticker")

    prompt_text = text or caption
    file_path = None

    if photo:
        best = photo[-1]
        file_path = download_tg_file(best["file_id"], "photo.jpg")
        if not prompt_text:
            prompt_text = "Опиши это изображение."
    elif document:
        fname = document.get("file_name", "document")
        file_path = download_tg_file(document["file_id"], fname)
        if not prompt_text:
            prompt_text = f"Обработай файл {fname}."
    elif voice:
        file_path = download_tg_file(voice["file_id"], "voice.ogg")
        if not prompt_text:
            prompt_text = "Это голосовое сообщение (ogg). Укажи что можешь с ним сделать."
    elif audio:
        fname = audio.get("file_name", "audio.mp3")
        file_path = download_tg_file(audio["file_id"], fname)
        if not prompt_text:
            prompt_text = f"Аудиофайл: {fname}"
    elif video:
        file_path = download_tg_file(video["file_id"], "video.mp4")
        if not prompt_text:
            prompt_text = "Видеофайл получен."
    elif sticker:
        file_path = download_tg_file(sticker["file_id"], "sticker.webp")
        if not prompt_text:
            prompt_text = "Это стикер (webp изображение)."

    if not prompt_text and not file_path:
        return

    qsize_before = _request_queue.qsize()
    _request_queue.put((prompt_text or "", file_path))
    if _worker_busy.is_set() or qsize_before > 0:
        pos = qsize_before + 1
        tg_send(f"📋 В очереди (позиция {pos})")


# ── MAIN ─────────────────────────────────────────────────────
def main() -> None:
    ensure_dirs()
    _setup_logging()
    _check_single_instance()
    threading.excepthook = _thread_excepthook

    log_info(f"=== tg_agent запущен === PID={os.getpid()}")
    log_info(f"Active agent: {get_active()} | Work dir: {WORK_DIR}")

    offset = None
    tg_send("🤖 Мультиагент запущен! Используй /menu для управления.")
    tg_set_keyboard()
    threading.Thread(target=run_startup_check, daemon=True).start()
    threading.Thread(target=_queue_worker, daemon=True, name="queue-worker").start()
    send_agent_menu()

    poll_errors = 0
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": [
                "message", "channel_post", "callback_query"
            ]}
            if offset:
                params["offset"] = offset
            r = requests.get(f"{API}/getUpdates", params=params, timeout=40)
            data = r.json()
            if not data.get("ok"):
                poll_errors += 1
                log_warn(f"getUpdates error #{poll_errors}: {data.get('description')}")
                time.sleep(min(5 * poll_errors, 30))
                continue
            poll_errors = 0
            updates = data.get("result", [])
            if updates:
                log_debug(f"Poll: {len(updates)} update(s)")
            for upd in updates:
                offset = upd["update_id"] + 1
                process_update(upd)
        except requests.exceptions.RequestException as e:
            poll_errors += 1
            log_warn(f"Network error #{poll_errors}: {e}")
            time.sleep(min(5 * poll_errors, 30))
        except Exception as e:
            log_error("Main loop exception", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
