#!/usr/bin/env python3
"""
AI-бэкенды: Claude, Gemini, Qwen, OpenRouter.
Subprocess-обёртки, парсинг JSON, fallback-логика Gemini, поиск бинарников.
"""

import json
import mimetypes
import os
import shutil
import subprocess
import time
import threading

import requests

from config import (
    AGENT_NAMES, AGENT_INSTALL_INFO, _AGENT_SEARCH_PATHS,
    CLAUDE_BIN, GEMINI_BIN, QWEN_BIN,
    CLAUDE_SESSION, CLAUDE_CTX_FILE,
    GEMINI_SESSION, GEMINI_CTX_FILE,
    QWEN_SESSION, QWEN_CTX_FILE,
    OPENROUTER_API_KEY, OPENROUTER_KEY_FILE, OPENROUTER_MODELS_CACHE,
    GEMINI_FALLBACK_MODELS, CTX_LIMITS, _AGENT_TIMEOUT,
    STATE_DIR, WORK_DIR, ARCHIVE_DIR, SETUP_DONE_FILE,
)
from context import (
    get_model, set_model, agent_label,
    _load_session, _save_session, _get_ctx, _add_ctx, _reset_session,
    shared_ctx_for_prompt, shared_ctx_for_api, shared_ctx_add,
    claude_rate_set, claude_rate_msg, _detect_rate_limit,
    memory_load,
)
from logger import log_debug, log_info, log_warn, log_error
import router

OR_MODELS_TTL = 3600
_or_id_map: dict[str, str] = {}


# ── ПАРСИНГ JSON ОТВЕТА (Claude/Gemini/Qwen формат) ─────────
def _parse_cli_output(raw: str, session_file: str) -> str:
    """Извлекает текст ответа и сессию из JSON вывода CLI."""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict) or item.get("type") != "result":
                    continue
                sid = item.get("session_id")
                if sid:
                    _save_session(session_file, sid)
                if item.get("is_error"):
                    err_msg = (item.get("error", {}).get("message")
                               or item.get("result")
                               or "Ошибка CLI")
                    return f"⚠️ {err_msg}"
                result = item.get("result")
                if result:
                    return result
            # result не нашли — ищем assistant message
            for item in reversed(data):
                if isinstance(item, dict) and item.get("type") == "assistant":
                    content = item.get("message", {}).get("content", [])
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            sid = item.get("session_id")
                            if sid:
                                _save_session(session_file, sid)
                            return block["text"]
        elif isinstance(data, dict):
            sid = data.get("session_id") or data.get("sessionId")
            if sid:
                _save_session(session_file, sid)
            return (data.get("result") or data.get("response")
                    or data.get("text") or raw)
    except Exception:
        pass
    return raw


# ── GEMINI FALLBACK ──────────────────────────────────────────
def _is_gemini_capacity_error(text: str) -> bool:
    """Проверяет, является ли stderr-вывод ошибкой перегрузки/недоступности модели.

    ВАЖНО: вызывать только на stderr при rc != 0, никогда на stdout —
    иначе нормальные ответы, содержащие слова "not available" и т.п.,
    вызывают ложные срабатывания и цикличное переключение моделей.
    """
    markers = [
        "MODELCAPACITYEXHAUSTED",
        "RESOURCE_EXHAUSTED",
        "ResourceExhausted",
        "rateLimitExceeded",
        "No capacity available",
        "MODEL_NOT_FOUND",
        "ModelNotFoundError",
        "model is not available",
        "model was not found",
        "model is not supported",
    ]
    tl = text.lower()
    return any(m.lower() in tl for m in markers)


def _gemini_fallback_retry(binary: str, session_file: str, ctx_file: str,
                            full_prompt: str, file_path: str | None,
                            timeout: int, env: dict) -> str:
    """
    При ошибке модели Gemini перебирает fallback-модели по порядку.
    Пропускает модели с ошибкой доступа/перегрузки.
    """
    # Ленивый импорт для разрыва цикла agents → ui → agents
    from ui import tg_send

    current_model = get_model("gemini") or "unknown"
    tried = {current_model}

    for fallback in GEMINI_FALLBACK_MODELS:
        if fallback in tried:
            continue
        tried.add(fallback)

        tg_send(f"⚠️ Gemini [{current_model}] недоступен — пробую {fallback}...")
        log_warn(f"Gemini error for {current_model}, trying fallback: {fallback}")

        cmd = [binary, "--output-format", "json", "--yolo", "--model", fallback]
        sid = _load_session(session_file)
        if sid:
            cmd += ["--resume", sid]
        cmd += ["--prompt", full_prompt]

        stdout, stderr, rc, timed_out = _run_subprocess(cmd, timeout, WORK_DIR, env)

        if timed_out:
            log_error(f"Gemini fallback {fallback} TIMEOUT")
            continue

        # Проверяем только stderr при ненулевом rc — иначе текст ответа
        # (stdout) может содержать те же фразы и вызвать ложное срабатывание.
        if rc != 0 and _is_gemini_capacity_error(stderr):
            log_warn(f"Gemini fallback {fallback} also failed: capacity/not-found")
            continue

        reply = _parse_cli_output(stdout.strip(), session_file) or ""
        if not reply and rc != 0:
            log_warn(f"Gemini fallback {fallback} empty reply, rc={rc}")
            continue

        set_model("gemini", fallback)
        tg_send(f"✅ Переключился на Gemini [{fallback}]")
        log_info(f"Gemini fallback success: {fallback}, reply={len(reply)}ch")
        return reply or stderr.strip()[:500] or "⚠️ Пустой ответ"

    return (f"❌ Gemini недоступен — все модели перегружены или не найдены.\n"
            f"Попробованы: {', '.join(tried)}\n\n"
            f"Смени модель вручную: /gemini /model list")


# ── SUBPROCESS HELPER ────────────────────────────────────────
def _run_subprocess(cmd: list, timeout: int, cwd: str, env: dict
                    ) -> tuple[str, str, int, bool]:
    """Запускает процесс; при таймауте убивает его и ждёт завершения.
    Возвращает (stdout, stderr, returncode, timed_out)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=cwd, env=env)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return stdout, stderr, proc.returncode, False
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return "", "", -1, True


# ── УНИВЕРСАЛЬНЫЙ ВЫЗОВ CLI ──────────────────────────────────
def _run_cli(binary: str, session_file: str, ctx_file: str,
             agent_name: str, prompt: str,
             file_path: str | None = None,
             extra_flags: list | None = None,
             model_override: str | None = None) -> str:
    """
    Запускает CLI-агент (claude/gemini/qwen) с промптом, сессией и файлом.
    Для Claude добавляет --dangerously-skip-permissions.
    """
    from ui import tg_send  # ленивый импорт

    is_claude = "claude" in binary

    # Для Claude с активной сессией CLI уже несёт полную историю разговора —
    # повторная вставка shared_ctx дублирует токены. Вставляем только память.
    if is_claude and _load_session(session_file):
        mem = memory_load()
        ctx_text = f"[Долговременная память пользователя:\n{mem}\n]" if mem else ""
    else:
        ctx_text = shared_ctx_for_prompt()

    parts = []
    if ctx_text:
        parts.append(f"[Контекст диалога:\n{ctx_text}\n]")
    if file_path and os.path.exists(file_path):
        try:
            rel_path = os.path.relpath(file_path, WORK_DIR)
        except ValueError:
            rel_path = file_path
        mime = mimetypes.guess_type(file_path)[0] or ""
        if "image" in mime:
            parts.append(
                f"[Изображение: {rel_path}]\n"
                f"Используй инструмент read_file или Read чтобы просмотреть изображение "
                f"и ответить на вопрос пользователя."
            )
        else:
            parts.append(
                f"[Файл: {rel_path}]\n"
                f"Прочитай содержимое файла и ответь на вопрос пользователя."
            )
    parts.append(f"Вопрос: {prompt}" if ctx_text or file_path else prompt)
    full_prompt = "\n\n".join(parts)

    agent_key = ("claude" if "claude" in binary else
                 "gemini" if "gemini" in binary else "qwen")
    effective_model = model_override if model_override is not None else get_model(agent_key)

    cmd = [binary]
    if is_claude:
        cmd += ["--print", "--dangerously-skip-permissions"]
    cmd += ["--output-format", "json"]
    if not is_claude:
        cmd += ["--yolo"]
    if effective_model:
        cmd += ["--model", effective_model]
    if extra_flags:
        cmd += extra_flags
    sid = _load_session(session_file)
    if sid:
        cmd += ["--resume", sid]
    if is_claude:
        cmd += [full_prompt]
    else:
        cmd += ["--prompt", full_prompt]

    env = os.environ.copy()
    for var in ("CLAUDECODE", "GEMINICODE", "QWENCODE"):
        env.pop(var, None)

    t_start = time.time()
    timeout_secs = _AGENT_TIMEOUT.get(agent_key, 300)
    log_info(f"→ {agent_name} [{effective_model}] prompt={len(full_prompt)}ch timeout={timeout_secs}s"
             + (f" file={os.path.basename(file_path)}" if file_path else ""))
    log_debug(f"  prompt preview: {full_prompt[:200]!r}")

    try:
        stdout, stderr, rc, timed_out = _run_subprocess(cmd, timeout_secs, WORK_DIR, env)
        elapsed = time.time() - t_start

        if timed_out:
            log_error(f"{agent_name} TIMEOUT after {elapsed:.0f}s (process killed)")
            mins = timeout_secs // 60
            return (f"⏱ {agent_name} не ответил за {mins} мин (процесс завершён)\n\n"
                    f"Попробуй переформулировать запрос или нажми /retry")

        raw = stdout.strip()
        if rc != 0 and not raw:
            log_warn(f"  {agent_name} exit={rc} stderr: {stderr.strip()[:300]}")

        combined = raw + "\n" + stderr
        if is_claude:
            secs = _detect_rate_limit(combined)
            if secs:
                claude_rate_set(secs)
                msg = claude_rate_msg()
                log_warn(f"Claude rate limit detected, retry_after={secs}s")
                return msg

        # Gemini: ошибка модели → авто-переключение на fallback.
        # Проверяем ТОЛЬКО stderr при rc != 0: stdout содержит текст ответа,
        # который может случайно содержать те же ключевые слова.
        if agent_key == "gemini" and rc != 0 and _is_gemini_capacity_error(stderr):
            return _gemini_fallback_retry(
                binary, session_file, ctx_file, full_prompt, file_path, timeout_secs, env
            )

        reply = _parse_cli_output(raw, session_file) or stderr.strip()[:1000] or "⚠️ Пустой ответ"

        log_info(f"← {agent_name} {elapsed:.1f}s reply={len(reply)}ch: {reply[:120]!r}")

        total = _add_ctx(ctx_file, len(full_prompt) + len(reply))
        ctx_warn, ctx_archive = CTX_LIMITS.get(agent_key, (150_000, 400_000))
        if total >= ctx_archive:
            ts = time.strftime("%Y%m%d_%H%M%S")
            with open(f"{ARCHIVE_DIR}/{agent_name.lower()}_{ts}.txt", "w") as f:
                f.write(f"session={_load_session(session_file)}\ntotal={total}\n")
            _reset_session(session_file, ctx_file)
            log_warn(f"{agent_name} context archived at {total // 1000}k chars")
            tg_send(f"🗄 {agent_name}: контекст {total // 1000}k символов архивирован. Новая сессия.")
        elif total >= ctx_warn:
            log_warn(f"{agent_name} context warning: {total // 1000}k/{ctx_archive // 1000}k")
            tg_send(f"⚠️ {agent_name}: контекст {total // 1000}k/{ctx_archive // 1000}k симв.")

        return reply
    except Exception as e:
        log_error(f"{agent_name} exception", e)
        return f"❌ {agent_name} ошибка: {e}"


# ── PASS-THROUGH CLI-КОМАНДЫ ──────────────────────────────────
def _run_passthrough(binary: str, session_file: str, agent_name: str, cmd: str) -> str:
    """
    Отправляет CLI-команду напрямую, без инжекции общего контекста.
    """
    is_claude = "claude" in binary
    agent_key = ("claude" if "claude" in binary else
                 "gemini" if "gemini" in binary else "qwen")
    model = get_model(agent_key)

    command = [binary]
    if is_claude:
        command += ["--print", "--dangerously-skip-permissions"]
    command += ["--output-format", "json"]
    if not is_claude:
        command += ["--yolo"]
    if model:
        command += ["--model", model]
    sid = _load_session(session_file)
    if sid:
        command += ["--resume", sid]
    if is_claude:
        command += [cmd]
    else:
        command += ["--prompt", cmd]

    env = os.environ.copy()
    for var in ("CLAUDECODE", "GEMINICODE", "QWENCODE"):
        env.pop(var, None)

    t_start = time.time()
    log_info(f"→ {agent_name} [passthrough] {cmd!r}")
    try:
        stdout, stderr, rc, timed_out = _run_subprocess(command, 60, WORK_DIR, env)
        elapsed = time.time() - t_start
        if timed_out:
            log_error(f"{agent_name} passthrough TIMEOUT (process killed)")
            return f"⏱ {agent_name}: команда не ответила за 60с (процесс завершён)"
        raw = stdout.strip()
        reply = _parse_cli_output(raw, session_file) or stderr.strip()[:500] or "⚠️ Пустой ответ"
        log_info(f"← {agent_name} passthrough {elapsed:.1f}s: {reply[:120]!r}")
        return reply
    except Exception as e:
        log_error(f"{agent_name} passthrough exception", e)
        return f"❌ {agent_name}: {e}"


# ── ОПРЕДЕЛЕНИЕ ПУТИ К БИНАРНИКУ ─────────────────────────────
def _find_binary(name: str) -> str | None:
    """Ищет бинарник по имени в известных путях и PATH."""
    found = shutil.which(name)
    if found:
        return found
    for d in _AGENT_SEARCH_PATHS:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def check_agents() -> dict[str, dict]:
    """Проверяет наличие каждого CLI-агента. Возвращает {agent: {ok, path, version}}."""
    result = {}
    for ag, info in AGENT_INSTALL_INFO.items():
        path = _find_binary(info["bin"])
        if path:
            try:
                r = subprocess.run(
                    [path, "--version"], capture_output=True, text=True, timeout=5,
                    env={**os.environ, **{v: "" for v in ("CLAUDECODE", "GEMINICODE", "QWENCODE")}},
                )
                ver = (r.stdout.strip() or r.stderr.strip()).split("\n")[0][:60]
            except Exception:
                ver = "?"
            result[ag] = {"ok": True, "path": path, "version": ver}
        else:
            result[ag] = {"ok": False, "path": None, "version": None}
    return result


def _get_effective_bin(agent: str) -> str:
    """Возвращает актуальный путь к бинарнику агента."""
    path_file = f"{STATE_DIR}/{agent}_bin_path.txt"
    try:
        p = open(path_file).read().strip()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    defaults = {"claude": CLAUDE_BIN, "gemini": GEMINI_BIN, "qwen": QWEN_BIN}
    return defaults[agent]


def run_startup_check() -> None:
    """Проверяет агентов при старте, отправляет статус если что-то отсутствует."""
    from ui import tg_send, kb  # ленивый импорт

    is_first = not os.path.exists(SETUP_DONE_FILE)
    agents = check_agents()
    missing = [ag for ag, s in agents.items() if not s["ok"]]

    for ag, info in agents.items():
        if info["ok"] and info["path"]:
            path_file = f"{STATE_DIR}/{ag}_bin_path.txt"
            with open(path_file, "w") as f:
                f.write(info["path"])

    if is_first or missing:
        icons = {"claude": "🔵", "gemini": "🟢", "qwen": "🟡"}
        lines = ["🤖 Статус агентов при запуске:", ""]
        for ag, info in agents.items():
            icon = icons[ag]
            mark = "✅" if info["ok"] else "❌"
            ver = f" ({info['version']})" if info["version"] else ""
            lines.append(f"  {mark} {icon} {AGENT_NAMES[ag]}{ver}")
        or_key = bool(get_openrouter_key())
        lines.append(f"  {'✅' if or_key else '⚠️'} 🌐 OpenRouter — "
                     f"{'ключ есть' if or_key else 'ключ не задан'}")
        if missing:
            lines.append("")
            lines.append(f"⚠️ Не найдено: {', '.join(AGENT_NAMES[a] for a in missing)}")
            lines.append("Нажми 🔧 Установка для инструкций.")
        else:
            lines.append("")
            lines.append("Все CLI-агенты найдены ✅")

        tg_send("\n".join(lines),
                kb([[("🔧 Установка агентов", "setup_check")]]) if missing else None)

    with open(SETUP_DONE_FILE, "w") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S"))


# ── АГЕНТЫ ───────────────────────────────────────────────────
def ask_claude(prompt: str, file_path: str | None = None) -> str:
    bin_path = _get_effective_bin("claude")
    if not os.path.isfile(bin_path):
        return "❌ Claude CLI не установлен. Используй /setup для установки."
    msg = claude_rate_msg()
    if msg:
        return msg
    current_model = get_model("claude")
    effective_model = router.classify(prompt, file_path, current_model)
    return _run_cli(bin_path, CLAUDE_SESSION, CLAUDE_CTX_FILE,
                    "Claude", prompt, file_path,
                    model_override=effective_model)


def ask_gemini(prompt: str, file_path: str | None = None) -> str:
    bin_path = _get_effective_bin("gemini")
    if not os.path.isfile(bin_path):
        return "❌ Gemini CLI не установлен. Используй /setup для установки."
    return _run_cli(bin_path, GEMINI_SESSION, GEMINI_CTX_FILE,
                    "Gemini", prompt, file_path)


def ask_qwen(prompt: str, file_path: str | None = None) -> str:
    bin_path = _get_effective_bin("qwen")
    if not os.path.isfile(bin_path):
        return "❌ Qwen CLI не установлен. Используй /setup для установки."
    return _run_cli(bin_path, QWEN_SESSION, QWEN_CTX_FILE,
                    "Qwen", prompt, file_path)


# ── СЖАТИЕ КОНТЕКСТА ─────────────────────────────────────────
def compress_gemini() -> str:
    SUMMARY_PROMPT = (
        "Сделай краткое структурированное резюме ВСЕГО нашего диалога (не более 800 слов). "
        "Включи ключевые вопросы, решения и выводы. Только резюме, без вступлений."
    )
    log_info("Gemini: compressing context via summary prompt")
    summary = _run_cli(GEMINI_BIN, GEMINI_SESSION, GEMINI_CTX_FILE,
                       "Gemini", SUMMARY_PROMPT)
    _reset_session(GEMINI_SESSION, GEMINI_CTX_FILE)
    shared_ctx_add("assistant", f"[Резюме предыдущего диалога с Gemini]\n{summary}", "Gemini")
    return f"✅ Gemini: контекст сжат. Резюме ({len(summary)} симв.) сохранено в историю."


def compress_openrouter() -> str:
    from context import get_openrouter_key
    key = get_openrouter_key()
    if not key:
        return "⚠️ OpenRouter ключ не задан — нечего сжимать."
    model = get_model("openrouter") or "openai/gpt-4o-mini"
    messages = shared_ctx_for_api()
    messages.append({"role": "user", "content":
        "Сделай краткое структурированное резюме нашего диалога (не более 600 слов). "
        "Только резюме, без вступлений."})
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "HTTP-Referer": "https://github.com/tg-agent",
                     "X-Title": "TG-Agent"},
            json={"model": model, "messages": messages},
            timeout=60,
        )
        data = r.json()
        if "error" in data:
            return f"❌ OpenRouter: {data['error'].get('message', str(data['error']))}"
        summary = data["choices"][0]["message"]["content"]
        shared_ctx_add("assistant", f"[Резюме диалога OpenRouter]\n{summary}", "OpenRouter")
        return f"✅ OpenRouter: контекст сжат. Резюме ({len(summary)} симв.) сохранено в историю."
    except Exception as e:
        return f"❌ compress_openrouter: {e}"


# ── OPENROUTER API ────────────────────────────────────────────
def get_openrouter_key() -> str:
    """Читает API ключ из файла, затем из CONFIG."""
    try:
        key = open(OPENROUTER_KEY_FILE).read().strip()
        if key:
            return key
    except FileNotFoundError:
        pass
    return OPENROUTER_API_KEY


def set_openrouter_key(key: str) -> None:
    with open(OPENROUTER_KEY_FILE, "w") as f:
        f.write(key.strip())
    log_info("OpenRouter API key updated")


def ask_openrouter(prompt: str, file_path: str | None = None) -> str:
    key = get_openrouter_key()
    if not key:
        return (
            "⚠️ OpenRouter API ключ не задан.\n\n"
            "Чтобы добавить ключ:\n"
            "  /or /key sk-or-v1-...\n\n"
            "Ключи: https://openrouter.ai/keys"
        )
    model = get_model("openrouter") or "openai/gpt-4o-mini"
    messages = shared_ctx_for_api()
    if not messages or messages[-1]["content"] != prompt:
        messages.append({"role": "user", "content": prompt})
    try:
        log_info(f"→ OpenRouter [{model}] prompt={len(prompt)}ch")
        t = time.time()
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "HTTP-Referer": "https://github.com/tg-agent",
                "X-Title": "TG-Agent",
            },
            json={"model": model, "messages": messages},
            timeout=120,
        )
        data = r.json()
        elapsed = time.time() - t
        if "error" in data:
            err = data["error"]
            msg = err.get("message", str(err))
            log_warn(f"OpenRouter error: {msg}")
            return f"❌ OpenRouter: {msg}"
        reply = data["choices"][0]["message"]["content"]
        log_info(f"← OpenRouter {elapsed:.1f}s reply={len(reply)}ch")
        return reply
    except Exception as e:
        log_error("OpenRouter exception", e)
        return f"❌ OpenRouter ошибка: {e}"


# ── OPENROUTER: ПОИСК МОДЕЛЕЙ ─────────────────────────────────
def _or_fetch_models() -> list[dict]:
    """Загружает список моделей OpenRouter, кэширует на OR_MODELS_TTL секунд."""
    try:
        if os.path.exists(OPENROUTER_MODELS_CACHE):
            age = time.time() - os.path.getmtime(OPENROUTER_MODELS_CACHE)
            if age < OR_MODELS_TTL:
                with open(OPENROUTER_MODELS_CACHE) as f:
                    return json.load(f)
        key = get_openrouter_key()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        r = requests.get("https://openrouter.ai/api/v1/models", headers=headers, timeout=15)
        models = r.json().get("data", [])
        if models:
            with open(OPENROUTER_MODELS_CACHE, "w") as f:
                json.dump(models, f)
        return models
    except Exception as e:
        log_error("OR fetch models", e)
        return []


def or_search_models(query: str) -> list[dict]:
    """Ищет модели по подстроке в id или названии (case-insensitive)."""
    q = query.lower().strip()
    all_models = _or_fetch_models()
    free_only = q.endswith(":free") or q == "free"
    if free_only:
        q = ""

    results = []
    for m in all_models:
        mid = m.get("id", "").lower()
        name = m.get("name", "").lower()

        if free_only:
            has_free_suffix = ":free" in m.get("id", "")
            try:
                has_zero_price = float(m.get("pricing", {}).get("prompt", "1")) == 0
            except (ValueError, TypeError):
                has_zero_price = False
            if not has_free_suffix and not has_zero_price:
                continue

        if not q or q in mid or q in name:
            results.append(m)
    return results


def _or_cb(model_id: str) -> str:
    """
    Возвращает callback_data для модели.
    Если model ID слишком длинный для Telegram (лимит 64 байта),
    сохраняет его в _or_id_map и возвращает короткий хэш-ключ.
    """
    import hashlib
    cb = f"or_model:{model_id}"
    if len(cb.encode()) <= 64:
        return cb
    short = hashlib.md5(model_id.encode()).hexdigest()[:10]
    _or_id_map[short] = model_id
    return f"or_model:~{short}"


def _or_model_label(m: dict, current: str) -> str:
    """Формирует читаемый лейбл для кнопки модели."""
    mid = m.get("id", "")
    name = m.get("name", "") or mid.split("/")[-1]
    pricing = m.get("pricing", {})
    try:
        price = float(pricing.get("prompt", 0)) * 1_000_000
        price_str = f" ${price:.2f}" if price > 0 else " 🆓"
    except Exception:
        price_str = ""
    mark = "✓ " if mid == current else ""
    label = f"{mark}{name[:28]}{price_str}"
    return label
