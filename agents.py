#!/usr/bin/env python3
"""
AI-бэкенды: Claude, Gemini, Qwen, OpenRouter.
Subprocess-обёртки, парсинг JSON, fallback-логика Gemini, поиск бинарников.
"""

import asyncio
import json
import mimetypes
import os
import shutil
import signal
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
    OLLAMA_BASE_URL, OLLAMA_DEFAULT_MODEL,
    OLLAMA_SESSION, OLLAMA_CTX_FILE,
)
from context import (
    get_model, set_model, agent_label,
    _load_session, _save_session, _get_ctx, _add_ctx, _reset_session,
    shared_ctx_for_prompt, shared_ctx_for_api, shared_ctx_add,
    global_ctx_for_prompt,
    claude_rate_set, claude_rate_msg, _detect_rate_limit,
    memory_load,
)
from logger import log_debug, log_info, log_warn, log_error
import router

OR_MODELS_TTL = 3600
_or_id_map: dict[str, str] = {}

# ── DYNAMIC MODEL DISCOVERY ───────────────────────────────────
_CLI_MODELS_CACHE: dict[str, tuple[list[str], float]] = {}
_CLI_MODELS_TTL = 300  # 5 min

_NVM_BIN = "/home/stx/.nvm/versions/node/v22.20.0/lib/node_modules"
_GEMINI_MODELS_JS = (
    f"{_NVM_BIN}/@google/gemini-cli/node_modules"
    "/@google/gemini-cli-core/dist/src/config/models.js"
)
_QWEN_CLI_JS = f"{_NVM_BIN}/@qwen-code/qwen-code/cli.js"


def get_cli_models(agent: str) -> list[str]:
    """Return available models for a CLI agent, with a 5-min cache.
    Reads from the installed npm package source; falls back to KNOWN_MODELS."""
    import re
    from config import KNOWN_MODELS

    now = time.time()
    cached = _CLI_MODELS_CACHE.get(agent)
    if cached and now - cached[1] < _CLI_MODELS_TTL:
        return cached[0]

    models: list[str] = []
    try:
        if agent == "gemini":
            with open(_GEMINI_MODELS_JS) as f:
                src = f.read()
            # Extract VALID_GEMINI_MODELS set members + auto aliases
            # Grab all export const X = 'value' lines that look like model IDs
            models = re.findall(r"export const \w+ = '((?:gemini|auto-gemini)-[^']+)'", src)
            # Filter out internal/embedding models, keep unique order
            skip = {"gemini-embedding", "customtools"}
            models = list(dict.fromkeys(
                m for m in models if not any(s in m for s in skip)
            ))

        elif agent == "qwen":
            with open(_QWEN_CLI_JS) as f:
                src = f.read()
            # Extract model IDs used by the Qwen CLI picker
            models = list(dict.fromkeys(
                re.findall(r'\b((?:coder|vision)-model)\b', src)
            ))

        elif agent == "claude":
            # Claude Code bundles many historical model IDs in its binary.
            # KNOWN_MODELS is the reliable source for current Claude models.
            pass  # falls through to KNOWN_MODELS fallback below

    except Exception as e:
        log_warn(f"[models] discovery failed for {agent}: {e}")

    if not models:
        models = list(KNOWN_MODELS.get(agent, []))

    _CLI_MODELS_CACHE[agent] = (models, now)
    return models


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


def _parse_stream_json_output(raw: str, session_file: str) -> str:
    """Parse Claude --output-format stream-json (newline-delimited JSON events).

    Looks for the 'result' event which contains the complete final text and
    session_id.  Falls back to concatenating all content_block_delta texts.
    """
    if not raw:
        return ""
    result_text = ""
    delta_text = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        t = ev.get("type", "")
        # Save session from any event that carries it
        sid = ev.get("session_id")
        if sid:
            _save_session(session_file, sid)
        if t == "result":
            if ev.get("is_error"):
                err = (ev.get("error", {}).get("message")
                       or ev.get("result") or "Ошибка CLI")
                return f"⚠️ {err}"
            result_text = ev.get("result", "")
        elif t == "content_block_delta":
            txt = ev.get("delta", {}).get("text", "")
            if txt:
                delta_text.append(txt)
    # Prefer the complete 'result' field; fall back to assembled deltas
    return result_text or "".join(delta_text) or raw


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
                            timeout: int, env: dict,
                            cwd: str | None = None) -> str:
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

        # Gemini CLI retries 429 internally with exponential backoff —
        # this can take 60-120s. Give enough time for that to complete.
        fallback_timeout = min(180, timeout)
        _cwd = cwd or WORK_DIR
        stdout, stderr, rc, timed_out = _run_subprocess(cmd, fallback_timeout, _cwd, env)

        if rc < 0:
            # SIGKILL = user cancelled — stop immediately
            log_error(f"Gemini fallback {fallback} killed (rc={rc})")
            break

        if timed_out:
            # This fallback timed out — try next model rather than giving up
            log_warn(f"Gemini fallback {fallback} timeout={fallback_timeout}s — trying next")
            continue

        # Проверяем только stderr при ненулевом rc — иначе текст ответа
        # (stdout) может содержать те же фразы и вызвать ложное срабатывание.
        if rc != 0 and _is_gemini_capacity_error(stderr):
            log_warn(f"Gemini fallback {fallback} also 429/unavailable: {stderr.strip()[:120]}")
            continue

        reply = _parse_cli_output(stdout.strip(), session_file) or ""
        if not reply and rc != 0:
            log_warn(f"Gemini fallback {fallback} empty reply rc={rc} stderr={stderr.strip()[:120]}")
            continue

        set_model("gemini", fallback)
        tg_send(f"✅ Переключился на Gemini [{fallback}]")
        log_info(f"Gemini fallback success: {fallback}, reply={len(reply)}ch")
        return reply or stderr.strip()[:500] or "⚠️ Пустой ответ"

    return (
        f"❌ Gemini: квота исчерпана на всех моделях (HTTP 429).\n"
        f"Попробованы: {', '.join(tried)}\n\n"
        f"Причина: лимит запросов Code Assist API (`cloudcode-pa.googleapis.com`).\n"
        f"Квота привязана к Google-аккаунту, не к тарифу.\n\n"
        f"Что делать:\n"
        f"• Подожди 1-2 часа — квота сбрасывается по RPM/RPD\n"
        f"• Используй /claude, /qwen или /openrouter\n"
        f"• Для неограниченного доступа — API-ключ Gemini через OpenRouter"
    )


# ── SUBPROCESS HELPER ────────────────────────────────────────
_active_proc: "subprocess.Popen | None" = None


def _kill_proc_tree(proc: "subprocess.Popen") -> None:
    """Kill the process and all its children (entire process group)."""
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        # Process already gone or getpgid failed — fall back to direct kill
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def cancel_active_proc() -> None:
    """Kill the currently running subprocess and its children. Thread-safe under CPython GIL."""
    global _active_proc
    if _active_proc is not None and _active_proc.poll() is None:
        _kill_proc_tree(_active_proc)
    _active_proc = None


def _run_subprocess(cmd: list, timeout: int, cwd: str, env: dict,
                    stderr_watcher=None,
                    stdout_cb=None,
                    ) -> tuple[str, str, int, bool]:
    """Запускает процесс; при таймауте убивает его и ждёт завершения.
    Возвращает (stdout, stderr, returncode, timed_out).

    stderr_watcher: optional callable(line: str) — fires on each stderr line
                    (background thread, for early error detection).
    stdout_cb:      optional callable(line: str) — fires on each stdout line
                    (background thread, for streaming responses).
                    When set, stdout is collected line-by-line instead of
                    via proc.stdout.read().
    """
    global _active_proc
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL,
                            text=True, cwd=cwd, env=env, start_new_session=True)
    _active_proc = proc

    if stderr_watcher or stdout_cb:
        stderr_lines: list[str] = []
        stdout_lines: list[str] = []

        def _read_stderr():
            for line in proc.stderr:
                stderr_lines.append(line)
                if stderr_watcher:
                    try:
                        stderr_watcher(line)
                    except Exception:
                        pass

        t_err = threading.Thread(target=_read_stderr, daemon=True)
        t_err.start()

        # Always read stdout in a thread — never block the main thread with
        # proc.stdout.read() because that has no timeout and will deadlock
        # when the process hangs (e.g. Gemini waiting on stdin/network).
        def _read_stdout():
            for line in proc.stdout:
                stdout_lines.append(line)
                if stdout_cb:
                    try:
                        stdout_cb(line)
                    except Exception:
                        pass
        t_out = threading.Thread(target=_read_stdout, daemon=True)
        t_out.start()

        try:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_proc_tree(proc)
                t_out.join(timeout=2)
                t_err.join(timeout=2)
                return "".join(stdout_lines), "".join(stderr_lines), -1, True
            t_out.join(timeout=2)
            t_err.join(timeout=2)
            return "".join(stdout_lines), "".join(stderr_lines), proc.returncode, False
        except Exception:
            _kill_proc_tree(proc)
            return "", "".join(stderr_lines), -1, True
        finally:
            _active_proc = None
    else:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return stdout, stderr, proc.returncode, False
        except subprocess.TimeoutExpired:
            _kill_proc_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            return stdout, stderr, -1, True
        finally:
            _active_proc = None


_NON_RETRYABLE_MARKERS = (
    "403", "forbidden", "request not allowed",       # auth / permission
    "429", "rate limit", "quota exceeded", "too many requests",  # rate limit
)

_TRANSIENT_PATTERNS = (
    "500", "502", "503", "504",
    "overloaded", "temporarily unavailable",
    "service unavailable", "internal server error",
)


def _is_transient_error(stdout: str, stderr: str, rc: int, timed_out: bool) -> bool:
    """Returns True if the subprocess failure is transient and worth retrying once.

    Non-retryable check runs first: any auth or rate-limit marker → False.
    Then: 5xx pattern in combined output → True.
    Then: empty stdout (subprocess crash with no output) → True.
    rc <= 0 or timed_out=True are always False.
    """
    if timed_out or rc <= 0:
        return False
    combined = (stdout + stderr).lower()
    if any(m in combined for m in _NON_RETRYABLE_MARKERS):
        return False
    if any(p in combined for p in _TRANSIENT_PATTERNS):
        return True
    if not stdout.strip():
        return True
    return False

# ── УНИВЕРСАЛЬНЫЙ ВЫЗОВ CLI ──────────────────────────────────
def _run_cli(binary: str, session_file: str, ctx_file: str,
             agent_name: str, prompt: str,
             file_path: str | None = None,
             extra_flags: list | None = None,
             model_override: str | None = None,
             stream_cb=None,
             cwd: str | None = None) -> str:
    """
    Запускает CLI-агент (claude/gemini/qwen) с промптом, сессией и файлом.
    Для Claude добавляет --dangerously-skip-permissions.
    """
    from ui import tg_send  # ленивый импорт

    is_claude = "claude" in binary

    # Авто-перевод для Claude: RU -> EN для экономии лимитов
    original_prompt = prompt
    if is_claude:
        from translator import translate_to_en
        prompt = translate_to_en(prompt)

    # skip_recent=True when Claude has active session (CLI already has history)
    sid = _load_session(session_file) if is_claude else None
    ctx_text = global_ctx_for_prompt(skip_recent=bool(sid))

    parts = []
    if ctx_text:
        parts.append(ctx_text)
    if file_path and os.path.exists(file_path):
        try:
            rel_path = os.path.relpath(file_path, WORK_DIR)
        except ValueError:
            rel_path = file_path
        mime = mimetypes.guess_type(file_path)[0] or ""
        if "image" in mime:
            parts.append(f"[img: {rel_path}]")
        else:
            parts.append(f"[file: {rel_path}]")
    parts.append(prompt)
    full_prompt = "\n\n".join(parts)

    agent_key = ("claude" if "claude" in binary else
                 "gemini" if "gemini" in binary else "qwen")
    effective_model = model_override if model_override is not None else get_model(agent_key)

    cmd = [binary]
    if is_claude:
        cmd += ["--print", "--dangerously-skip-permissions"]
        # stream-json requires --verbose when used with --print
        _fmt = "stream-json" if stream_cb else "json"
        cmd += ["--output-format", _fmt]
        if stream_cb:
            cmd += ["--verbose"]
    else:
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

    # Gemini connects directly to Google APIs — proxy (set by start.sh for
    # Telegram polling) breaks the connection and causes 12-second timeouts.
    if agent_key == "gemini":
        for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                          "ALL_PROXY", "all_proxy"):
            env.pop(proxy_var, None)

    _cwd = cwd if cwd and os.path.isdir(cwd) else WORK_DIR

    t_start = time.time()
    timeout_secs = _AGENT_TIMEOUT.get(agent_key, 300)
    log_info(f"→ {agent_name} [{effective_model}] prompt={len(full_prompt)}ch timeout={timeout_secs}s"
             + (f" file={os.path.basename(file_path)}" if file_path else ""))
    log_debug(f"  prompt preview: {full_prompt[:200]!r}")

    try:
        # For Claude streaming: parse content_block_delta events line-by-line.
        _stdout_line_cb = None
        if is_claude and stream_cb:
            def _stdout_line_cb(line: str) -> None:
                line = line.strip()
                if not line:
                    return
                try:
                    ev = json.loads(line)
                    if ev.get("type") == "content_block_delta":
                        text = ev.get("delta", {}).get("text", "")
                        if text:
                            stream_cb(text)
                except (json.JSONDecodeError, KeyError):
                    pass

        # For Gemini: watch stderr live so we can notify the user immediately
        # if Google returns 429 — the CLI will keep retrying internally for
        # minutes, but the user can switch agents right away.
        if agent_key == "gemini":
            _quota_notified = threading.Event()

            def _gemini_stderr_watcher(line: str) -> None:
                if _quota_notified.is_set():
                    return
                if "429" in line or "exhausted" in line.lower() or "rateLimitExceeded" in line:
                    _quota_notified.set()
                    from ui import tg_send as _tg
                    _tg(
                        "⚠️ Gemini: Google вернул 429 (квота).\n"
                        "CLI автоматически повторяет запрос — подожди, или переключись прямо сейчас:\n"
                        "/claude  /qwen  /openrouter"
                    )
                    log_warn("Gemini 429 detected early via stderr watcher")

            stdout, stderr, rc, timed_out = _run_subprocess(
                cmd, timeout_secs, _cwd, env, stderr_watcher=_gemini_stderr_watcher
            )
        else:
            stdout, stderr, rc, timed_out = _run_subprocess(
                cmd, timeout_secs, _cwd, env,
                stdout_cb=_stdout_line_cb,
            )

        if _is_transient_error(stdout, stderr, rc, timed_out):
            log_warn(f"{agent_name}: transient error (rc={rc}), retrying in 2s…")
            time.sleep(2)
            stdout, stderr, rc, timed_out = _run_subprocess(cmd, timeout_secs, _cwd, env)

        elapsed = time.time() - t_start

        if timed_out:
            log_error(f"{agent_name} TIMEOUT after {elapsed:.0f}s (process killed)")
            mins = timeout_secs // 60
            return (f"⏱ {agent_name} не ответил за {mins} мин (процесс завершён)\n\n"
                    f"Попробуй переформулировать запрос или нажми /retry")

        raw = stdout.strip()
        if rc != 0 and not raw:
            log_warn(f"  {agent_name} exit={rc} stderr: {stderr.strip()[:300]}")

        # --- SAFE RATE LIMIT TRACKING ---
        import rate_tracker
        if is_claude and rc == 0:
            rate_tracker.log_request(agent_key) # Локальный счетчик (эвристика)
        if agent_key == "gemini":
            rate_tracker.log_request("gemini")  # Трекинг Gemini RPD
        if agent_key == "qwen":
            rate_tracker.log_request("qwen")    # Трекинг Qwen RPD

            # Пассивный парсинг системных сообщений из stdout/stderr
            combined_output = stdout + stderr
            try:
                rate_tracker.parse_cli_warning(agent_key, combined_output)
            except Exception as e:
                # Отправляем ошибку парсинга в базу знаний (Knowledge Base)
                from db_manager import Database
                from config import DB_PATH
                db = Database(DB_PATH)
                db.add_lesson("claude_cli_parsing", f"Regex failed on output", f"Error: {e}\nOutput: {combined_output[:200]}")
                log_error("Rate limit parsing failed", e)
        # --------------------------------

        # Only check for rate limits on error responses (rc != 0) and only in
        # stderr — scanning raw stdout causes false positives when Claude's
        # reply happens to mention "quota", "overloaded", etc. in normal text.
        if is_claude and rc != 0:
            secs = _detect_rate_limit(stderr)
            if secs:
                claude_rate_set(secs)
                msg = claude_rate_msg()
                log_warn(f"Claude rate limit detected, retry_after={secs}s")
                return msg

        # Gemini: ошибка модели → авто-переключение на fallback.
        # Проверяем ТОЛЬКО stderr при rc > 0: stdout содержит текст ответа,
        # который может случайно содержать те же ключевые слова.
        # rc < 0 означает SIGKILL (отмена пользователем) — fallback не нужен.
        if agent_key == "gemini" and rc > 0 and _is_gemini_capacity_error(stderr):
            return _gemini_fallback_retry(
                binary, session_file, ctx_file, full_prompt, file_path, timeout_secs, env,
                cwd=_cwd,
            )

        if is_claude and stream_cb:
            reply = _parse_stream_json_output(raw, session_file) or stderr.strip()[:1000] or "⚠️ Пустой ответ"
        else:
            reply = _parse_cli_output(raw, session_file) or stderr.strip()[:1000] or "⚠️ Пустой ответ"

        # Обратный перевод для Claude: EN -> RU
        if is_claude and reply and not reply.startswith("⚠️"):
            from translator import translate_to_ru
            reply = translate_to_ru(reply)

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
_bin_cache: dict[str, str] = {}  # agent → resolved binary path (in-process cache)


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
    # Check Ollama availability (local HTTP service, not a CLI binary)
    ollama_models = get_ollama_models()
    if ollama_models:
        result["ollama"] = {
            "ok": True,
            "path": OLLAMA_BASE_URL,
            "version": f"{len(ollama_models)} model(s): {', '.join(ollama_models[:3])}",
        }
    else:
        result["ollama"] = {"ok": False, "path": None, "version": None}
    return result


def _get_effective_bin(agent: str) -> str:
    """Возвращает путь к бинарнику агента. Lazy: ищет при первом вызове, кэширует в памяти."""
    if agent in _bin_cache:
        cached = _bin_cache[agent]
        if os.path.isfile(cached):
            return cached
        # Cached path no longer valid — re-discover
        del _bin_cache[agent]

    # 1. Custom path saved by user/setup
    path_file = f"{STATE_DIR}/{agent}_bin_path.txt"
    try:
        p = open(path_file).read().strip()
        if p and os.path.isfile(p):
            _bin_cache[agent] = p
            return p
    except Exception:
        pass

    # 2. Search PATH and known locations
    bin_names = {"claude": "claude", "gemini": "gemini", "qwen": "qwen"}
    found = _find_binary(bin_names.get(agent, agent))
    if found:
        _bin_cache[agent] = found
        return found

    # 3. Hardcoded default
    defaults = {"claude": CLAUDE_BIN, "gemini": GEMINI_BIN, "qwen": QWEN_BIN}
    return defaults.get(agent, agent)


def run_startup_check() -> None:
    """Проверяет агентов при старте (без subprocess — только существование файла)."""
    from ui import tg_send, kb  # ленивый импорт

    is_first = not os.path.exists(SETUP_DONE_FILE)

    # Fast presence check: no subprocess, just file existence + cache warm-up
    icons = {"claude": "🔵", "gemini": "🟢", "qwen": "🟡"}
    status = {}
    for ag in icons:
        path = _get_effective_bin(ag)  # warms _bin_cache as a side effect
        ok = os.path.isfile(path)
        status[ag] = {"ok": ok, "path": path if ok else None}

    missing = [ag for ag, s in status.items() if not s["ok"]]

    if is_first or missing:
        lines = ["🤖 Статус агентов при запуске:", ""]
        for ag, info in status.items():
            mark = "✅" if info["ok"] else "❌"
            lines.append(f"  {mark} {icons[ag]} {AGENT_NAMES[ag]}")
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


# ── GEMINI CWD STRATEGY ──────────────────────────────────────
# Gemini CLI scans all files in its working directory on each new session.
# Running in a large workspace (thousands of files) = 10-20 hidden API
# requests per prompt, burning through the 1500 RPD quota fast.
#
# Strategy:
#   lite mode ON  → CWD = STATE_DIR  (bot configs only, no source code)
#   active project → CWD = project dir (just that one project, not the whole workspace)
#   fallback       → CWD = WORK_DIR  (original behaviour)

_gemini_lite_mode: bool = False   # toggled via /gemini_lite or Gemini panel button


def get_gemini_lite() -> bool:
    return _gemini_lite_mode


def set_gemini_lite(val: bool) -> None:
    global _gemini_lite_mode
    _gemini_lite_mode = val


def _get_agent_workspace(agent_key: str) -> str:
    """Return the isolated workspace dir for the given agent.

    Each agent runs in its own empty folder — it never sees the bot's source
    code or other project files, so it won't waste API quota scanning them.
    If a Team Mode project is active, use the project's own workspace subdir
    (inside PROJECTS_WORKSPACE) so the agent sees only that project's files.
    Sessions and memory are stored in STATE_DIR (unaffected by CWD).
    """
    from config import CLAUDE_WORKSPACE, GEMINI_WORKSPACE, QWEN_WORKSPACE, PROJECTS_WORKSPACE

    # Active Team Mode project → actual project directory (agent needs the code)
    try:
        import team_mode as _tm
        proj = _tm._cur_project()
        if proj:
            proj_dir = _tm._project_dir(proj)
            if os.path.isdir(proj_dir):
                return proj_dir   # scan only this project, not the whole workspace
    except Exception:
        pass

    # No project → agent's own sandbox (empty, no source files to scan)
    ws_map = {"claude": CLAUDE_WORKSPACE, "gemini": GEMINI_WORKSPACE, "qwen": QWEN_WORKSPACE}
    ws = ws_map.get(agent_key)
    if ws:
        os.makedirs(ws, exist_ok=True)
        return ws

    from config import STATE_DIR
    return STATE_DIR


def _get_gemini_cwd() -> str:
    """Backward-compat wrapper — kept for /gemini_lite toggle in tg_agent.py."""
    from config import STATE_DIR, GEMINI_WORKSPACE
    if _gemini_lite_mode:
        return GEMINI_WORKSPACE  # lite: isolated sandbox, no project files
    return _get_agent_workspace("gemini")


# ── АГЕНТЫ ───────────────────────────────────────────────────
def ask_claude(prompt: str, file_path: str | None = None, stream_cb=None) -> str:
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
                    model_override=effective_model,
                    stream_cb=stream_cb,
                    cwd=_get_agent_workspace("claude"))


def ask_gemini(prompt: str, file_path: str | None = None, stream_cb=None) -> str:
    bin_path = _get_effective_bin("gemini")
    if not os.path.isfile(bin_path):
        return "❌ Gemini CLI не установлен. Используй /setup для установки."

    # ── Pre-request quota check ───────────────────────────────
    try:
        import rate_tracker as _rt
        _count = _rt.get_gemini_prompts_today()
        _rpm   = _rt.get_gemini_rpm()
        _hrs   = _rt._gemini_hours_until_reset()

        if _count >= _rt._GEMINI_PROMPT_CRIT:
            return (
                f"🔴 Gemini: исчерпан дневной лимит (~{_rt._GEMINI_PROMPT_CRIT} промптов, "
                f"~{_rt._GEMINI_RPD} API-запросов).\n"
                f"Сброс через {_hrs} (в 10:00 МСК).\n\n"
                "Переключись: /claude, /qwen или /openrouter"
            )
        elif _count >= _rt._GEMINI_PROMPT_WARN:
            # Warn but proceed — send through a tg message via lazy import
            try:
                from ui import tg_send
                tg_send(
                    f"⚠️ Gemini: {_count} промптов сегодня — остаток ~"
                    f"{_rt._GEMINI_PROMPT_CRIT - _count}. Сброс через {_hrs}."
                )
            except Exception:
                pass

        if _rpm >= _rt._GEMINI_RPM_LIMIT:
            time.sleep(3)  # brief throttle to avoid RPM burst
    except Exception:
        pass
    # ─────────────────────────────────────────────────────────

    return _run_cli(bin_path, GEMINI_SESSION, GEMINI_CTX_FILE,
                    "Gemini", prompt, file_path,
                    cwd=_get_gemini_cwd())


def ask_qwen(prompt: str, file_path: str | None = None, stream_cb=None) -> str:
    bin_path = _get_effective_bin("qwen")
    if not os.path.isfile(bin_path):
        return "❌ Qwen CLI не установлен. Используй /setup для установки."
    return _run_cli(bin_path, QWEN_SESSION, QWEN_CTX_FILE,
                    "Qwen", prompt, file_path,
                    cwd=_get_agent_workspace("qwen"))


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


# ── STREAMING HELPER (OpenAI-compatible SSE) ─────────────────
def _parse_sse_stream(response, stream_cb) -> str:
    """Consume an OpenAI-compatible SSE stream.

    Calls stream_cb(chunk) for every non-empty text delta.
    Returns the full accumulated text, or "❌ ..." on API-level errors.
    """
    parts: list[str] = []
    for raw_line in response.iter_lines():
        if not raw_line:
            continue
        line = raw_line.decode() if isinstance(raw_line, bytes) else raw_line
        if line.startswith("data: "):
            line = line[6:]
        if line.strip() == "[DONE]":
            break
        try:
            data = json.loads(line)
            if "error" in data:
                err = data["error"]
                return f"❌ {err.get('message', str(err)) if isinstance(err, dict) else err}"
            delta = (data.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
            if delta:
                parts.append(delta)
                if stream_cb:
                    stream_cb(delta)
        except (json.JSONDecodeError, KeyError, IndexError):
            pass
    return "".join(parts)


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


def ask_openrouter(prompt: str, file_path: str | None = None, stream_cb=None) -> str:
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
    use_stream = stream_cb is not None
    try:
        log_info(f"→ OpenRouter [{model}] prompt={len(prompt)}ch stream={use_stream}")
        t = time.time()
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "HTTP-Referer": "https://github.com/tg-agent",
                "X-Title": "TG-Agent",
            },
            json={"model": model, "messages": messages, "stream": use_stream},
            timeout=120,
            stream=use_stream,
        )
        elapsed = time.time() - t
        if use_stream:
            reply = _parse_sse_stream(r, stream_cb)
            if reply.startswith("❌"):
                log_warn(f"OpenRouter stream error: {reply}")
                return f"OpenRouter: {reply}"
        else:
            data = r.json()
            # Capture rate-limit headers (non-blocking, best-effort)
            try:
                import rate_tracker
                rate_tracker.update_from_headers("openrouter", r.headers)
            except Exception:
                pass
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


# ── OLLAMA (LOCAL) ────────────────────────────────────────────

def get_ollama_models() -> list[str]:
    """Fetch available models from local Ollama instance."""
    try:
        r = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5,
                         proxies={"http": None, "https": None})
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


_OLLAMA_MODEL_NOT_FOUND = "__OLLAMA_MODEL_NOT_FOUND__"
_ollama_active_pulls: dict[str, bool] = {}  # model -> True while pulling


def ollama_pull_model(model: str, send_fn, edit_fn) -> bool:
    """Pull an Ollama model in background. Returns False if already pulling.

    send_fn(text) -> message_id  — send a new TG message, return its id
    edit_fn(msg_id, text)        — edit existing TG message
    """
    import re as _re
    if _ollama_active_pulls.get(model):
        return False
    _ollama_active_pulls[model] = True

    def _pull():
        msg_id = send_fn(
            f"⏳ Начинаю загрузку `{model}`...\n"
            "Можно пока использовать другого агента (/gemini, /claude)."
        )
        try:
            # Build env: inherit everything + force SOCKS5 proxy for ollama
            pull_env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}
            # Detect proxy port 2080 (nekobox) and set SOCKS5 for ollama
            import socket as _sock
            try:
                s = _sock.create_connection(("127.0.0.1", 2080), timeout=0.5)
                s.close()
                proxy_url = "socks5://127.0.0.1:2080"
                pull_env["HTTPS_PROXY"] = proxy_url
                pull_env["HTTP_PROXY"]  = proxy_url
                pull_env["ALL_PROXY"]   = proxy_url
                log_info(f"[ollama pull] using proxy {proxy_url}")
            except OSError:
                pull_env.pop("HTTPS_PROXY", None)
                pull_env.pop("HTTP_PROXY",  None)
                pull_env.pop("ALL_PROXY",   None)
                log_info("[ollama pull] no proxy detected, direct connection")

            # Discard stdout/stderr — progress tracked via `ollama ps` polling
            proc = subprocess.Popen(
                ["ollama", "pull", model],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                env=pull_env,
            )

            last_status = ""
            while proc.poll() is None:
                time.sleep(4)
                try:
                    ps = subprocess.run(
                        ["ollama", "ps"],
                        capture_output=True, text=True, timeout=5,
                    )
                    # Find line with our model, e.g.: "qwen3:1.7b  ...  34% 456 MB/1.1 GB"
                    for ps_line in ps.stdout.splitlines():
                        m = _re.search(
                            r'(\d+)%\s+([\d.]+\s*\w+)\s*/\s*([\d.]+\s*\w+)',
                            ps_line,
                        )
                        if m and model.split(":")[0] in ps_line:
                            status = f"⏳ `{model}`: {m.group(1)}% ({m.group(2)} / {m.group(3)})"
                            if status != last_status:
                                last_status = status
                                if msg_id:
                                    edit_fn(msg_id, status)
                            break
                except Exception:
                    pass

            stderr_out = proc.stderr.read().decode("utf-8", errors="replace").strip()
            rc = proc.returncode

            # Verify via ollama list rather than trusting return code alone
            installed_after = get_ollama_models()
            model_base = model.split(":")[0]
            success = rc == 0 or any(model_base in m for m in installed_after)

            if success:
                _CLI_MODELS_CACHE.pop("ollama", None)
                done_text = (
                    f"✅ Модель `{model}` установлена!\n"
                    f"Доступна в /menu → ⚙️ Ollama → 📋 Установленные"
                )
            else:
                details = stderr_out or f"exit code {rc}"
                log_warn(f"[ollama pull] FAILED rc={rc}: {details}")
                done_text = (
                    f"❌ Не удалось установить `{model}`\n\n"
                    f"`{details[:400]}`\n\n"
                    f"Проверь имя на [ollama.com/library](https://ollama.com/library)"
                )
            edit_fn(msg_id, done_text) if msg_id else send_fn(done_text)
        except FileNotFoundError:
            txt = "❌ `ollama` не найден. Установи: https://ollama.com/download"
            edit_fn(msg_id, txt) if msg_id else send_fn(txt)
        except Exception as e:
            log_error("ollama_pull", e)
            txt = f"❌ Ошибка загрузки `{model}`: {e}"
            edit_fn(msg_id, txt) if msg_id else send_fn(txt)
        finally:
            _ollama_active_pulls.pop(model, None)

    threading.Thread(target=_pull, daemon=True, name=f"ollama-pull-{model}").start()
    return True


def ask_ollama(prompt: str, file_path: str | None = None, stream_cb=None) -> str:
    """Call local Ollama via its OpenAI-compatible endpoint."""
    model = get_model("ollama") or OLLAMA_DEFAULT_MODEL
    if not model:
        available = get_ollama_models()
        if available:
            model = available[0]
            set_model("ollama", model)
        else:
            return _OLLAMA_MODEL_NOT_FOUND

    # Build message list from shared conversation context (all agents).
    # This gives Ollama awareness of the full conversation history.
    messages = shared_ctx_for_api()
    if not messages or messages[-1].get("content") != prompt:
        messages.append({"role": "user", "content": prompt})

    # Prepend identity system prompt so Ollama doesn't adopt a previous
    # agent's persona (e.g. Claude saying "I am Claude Code" in prior turns).
    # Do NOT inject global_ctx_for_prompt() — that's Claude-specific memory.
    identity = (
        f"You are {model}, a local AI assistant running via Ollama. "
        "This conversation may include responses from other AI systems "
        "(Claude, Gemini, Qwen) — those are prior turns, not your own words. "
        "Answer as yourself."
    )
    messages.insert(0, {"role": "system", "content": identity})

    use_stream = stream_cb is not None
    try:
        log_info(f"→ Ollama [{model}] prompt={len(prompt)}ch stream={use_stream}")
        t = time.time()
        # proxies=None bypasses HTTP_PROXY — Ollama is local, no proxy needed
        r = requests.post(
            f"{OLLAMA_BASE_URL}/v1/chat/completions",
            json={"model": model, "messages": messages, "stream": use_stream},
            timeout=_AGENT_TIMEOUT.get("ollama", 120),
            proxies={"http": None, "https": None},
            stream=use_stream,
        )
        elapsed = time.time() - t
        if use_stream:
            reply = _parse_sse_stream(r, stream_cb)
            if reply.startswith("❌"):
                if "not found" in reply.lower():
                    return _OLLAMA_MODEL_NOT_FOUND
                return f"❌ Ollama: {reply[2:].strip()}"
        else:
            data = r.json()
            if "error" in data:
                err_msg = data["error"]
                if isinstance(err_msg, dict):
                    err_msg = err_msg.get("message", str(data["error"]))
                if "not found" in err_msg.lower():
                    return _OLLAMA_MODEL_NOT_FOUND
                return f"❌ Ollama: {err_msg}"
            reply = data["choices"][0]["message"]["content"]
        log_info(f"← Ollama {elapsed:.1f}s reply={len(reply)}ch")
        total = _add_ctx(OLLAMA_CTX_FILE, len(prompt) + len(reply))
        ctx_warn, ctx_archive = CTX_LIMITS.get("ollama", (60_000, 200_000))
        if total >= ctx_archive:
            _reset_session(OLLAMA_SESSION, OLLAMA_CTX_FILE)
            from ui import tg_send
            tg_send("🗄 Ollama: контекст архивирован. Новая сессия.")
        elif total >= ctx_warn:
            from ui import tg_send
            tg_send(f"⚠️ Ollama: контекст {total // 1000}k/{ctx_archive // 1000}k симв.")
        return reply
    except requests.exceptions.ConnectionError:
        return "❌ Ollama недоступна. Убедитесь что запущен: `ollama serve`"
    except Exception as e:
        log_error("Ollama exception", e)
        return f"❌ Ollama ошибка: {e}"


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


# ── Async wrappers (asyncio.to_thread, no new OS threads) ────────────────────

async def async_ask_claude(prompt: str, file_path: str | None = None, stream_cb=None) -> str:
    """Async wrapper — runs ask_claude in the default thread pool."""
    return await asyncio.to_thread(ask_claude, prompt, file_path, stream_cb)


async def async_ask_gemini(prompt: str, file_path: str | None = None, stream_cb=None) -> str:
    return await asyncio.to_thread(ask_gemini, prompt, file_path, stream_cb)


async def async_ask_qwen(prompt: str, file_path: str | None = None, stream_cb=None) -> str:
    return await asyncio.to_thread(ask_qwen, prompt, file_path, stream_cb)


async def async_ask_openrouter(prompt: str, file_path: str | None = None, stream_cb=None) -> str:
    return await asyncio.to_thread(ask_openrouter, prompt, file_path, stream_cb)


async def async_ask_ollama(prompt: str, file_path: str | None = None, stream_cb=None) -> str:
    return await asyncio.to_thread(ask_ollama, prompt, file_path, stream_cb)
