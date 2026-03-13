#!/usr/bin/env python3
"""
Режим командной работы агентов.

Каждый проект изолирован в своей папке: WORK_DIR/projects/<slug>/
Файлы агентов, логи, план — всё внутри папки проекта.
Переключение между проектами через меню "📂 Проекты".

Пайплайн:
  Planner -> Coder -> Debugger -> если ISSUES -> Coder fix -> Debugger -> ...
  При APPROVED — запись в project_log.md, авто-отправка APK.
"""

import os
import re
import json
import threading
import time

import tg_agent as _bot

# ── ДИРЕКТОРИИ ──────────────────────────────────────────────────
PROJECTS_DIR    = os.path.join(_bot.WORK_DIR, "projects")   # все проекты здесь
TEAM_DIR        = os.path.join(_bot.WORK_DIR, ".tg_team")   # глобальный стейт
STATE_FILE      = os.path.join(TEAM_DIR, "state.json")      # глобальное состояние

TASK_AWAIT_FILE         = os.path.join(_bot.STATE_DIR, "team_task_await.txt")
PROJECT_AWAIT_FILE      = os.path.join(_bot.STATE_DIR, "team_project_await.txt")
CODE_REVIEW_AWAIT_FILE  = os.path.join(_bot.STATE_DIR, "team_code_review_await.txt")
PROJECT_LOG_MAX_CHARS = 4000

AGENTS = ("claude", "gemini", "qwen")
DEFAULT_MAX_ROUNDS = 3

_team_lock   = threading.Lock()
_pause_event = threading.Event()
_pipeline_thread: threading.Thread | None = None


# ── ХЕЛПЕРЫ ПУТЕЙ ──────────────────────────────────────────────
def _slug(name: str) -> str:
    """Конвертирует имя проекта в безопасный слаг."""
    s = name.strip().lower()
    s = re.sub(r"[^\w\-]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:40] or "project"


def _list_projects() -> list[str]:
    """Возвращает список существующих проектов (слаги)."""
    try:
        os.makedirs(PROJECTS_DIR, exist_ok=True)
        return sorted(
            d for d in os.listdir(PROJECTS_DIR)
            if os.path.isdir(os.path.join(PROJECTS_DIR, d))
        )
    except Exception:
        return []


def _cur_project() -> str:
    """Текущий проект из глобального состояния."""
    return _load_state().get("project", "")


def _project_dir(proj: str = "") -> str:
    """Директория проекта (создаётся если нет)."""
    p = proj or _cur_project()
    if not p:
        return _bot.WORK_DIR
    return os.path.join(PROJECTS_DIR, p)


def _p_team_dir(proj: str = "") -> str:
    """Служебная папка .tg_team внутри проекта."""
    return os.path.join(_project_dir(proj), ".tg_team")


def _p_file(name: str, proj: str = "") -> str:
    """Полный путь к файлу в .tg_team проекта."""
    return os.path.join(_p_team_dir(proj), name)


def _p_sessions(proj: str = "") -> dict[str, str]:
    """Пути к файлам сессий агентов в проекте."""
    td = _p_team_dir(proj)
    return {a: os.path.join(td, f"{a}_team.sid") for a in AGENTS}


def _project_info(proj: str) -> str:
    """Краткая информация о проекте: последняя задача из лога."""
    try:
        log = open(_p_file("project_log.md", proj)).read()
        # Берём последний заголовок ## [дата] ...
        lines = [l for l in log.splitlines() if l.startswith("## [")]
        if lines:
            last = lines[-1]
            # Убираем дату, оставляем задачу
            return re.sub(r"^##\s*\[\S+\s+\S+\]\s*", "", last)[:40]
    except Exception:
        pass
    return ""


# ── СОСТОЯНИЕ ──────────────────────────────────────────────────
DEFAULT_STATE: dict = {
    "phase":       "IDLE",   # IDLE PLANNING CODING DEBUGGING FIXING PAUSED DONE STOPPED
    "task":        "",
    "task_en":     "",       # English translation of task (for agents)
    "project":     "",       # текущий проект (слаг)
    "roles":       {"planner": "claude", "coder": "gemini", "debugger": "qwen"},
    "fix_round":   0,
    "max_rounds":  DEFAULT_MAX_ROUNDS,   # 0 = без ограничения
    "build_apk":   False,    # собирать APK после APPROVED
    "build_cmd":   "",       # команда сборки (пусто = авто-detect)
    "run_tests":   False,    # запускать тесты перед дебаггером
    "test_cmd":    "",       # команда тестов (пусто = авто-detect)
    "auto_commit": False,    # авто-коммит после APPROVED
    "preset":      "",       # активный пресет ролей
    "started_at":  "",
    "updated_at":  "",
}

ROLE_PRESETS = {
    "android": {"planner": "claude", "coder": "gemini", "debugger": "qwen",   "label": "📱 Android"},
    "web":     {"planner": "claude", "coder": "gemini", "debugger": "claude", "label": "🌐 Web"},
    "python":  {"planner": "gemini", "coder": "claude", "debugger": "gemini", "label": "🐍 Python"},
    "solo":    {"planner": "gemini", "coder": "gemini", "debugger": "gemini", "label": "🤖 Solo Gemini"},
}


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return {**DEFAULT_STATE, **json.load(f)}
    except Exception:
        return dict(DEFAULT_STATE)


def _save_state(s: dict):
    os.makedirs(TEAM_DIR, exist_ok=True)
    s["updated_at"] = time.strftime("%H:%M:%S")
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def _set_phase(phase: str, extra: dict | None = None):
    with _team_lock:
        s = _load_state()
        s["phase"] = phase
        if extra:
            s.update(extra)
        _save_state(s)


def _is_stopped() -> bool:
    return _load_state()["phase"] in ("STOPPED", "IDLE")


# ── ВЫЗОВ АГЕНТОВ ──────────────────────────────────────────────
_BINS = {"claude": _bot.CLAUDE_BIN, "gemini": _bot.GEMINI_BIN, "qwen": _bot.QWEN_BIN}


def _parse_cli_output(raw: str) -> str:
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("type") == "result":
                    return item.get("result") or raw
            for item in reversed(data):
                if isinstance(item, dict) and item.get("type") == "assistant":
                    for block in item.get("message", {}).get("content", []):
                        if isinstance(block, dict) and block.get("type") == "text":
                            return block["text"]
        elif isinstance(data, dict):
            return data.get("result") or data.get("response") or data.get("text") or raw
    except Exception:
        pass
    return raw


def _is_mostly_cyrillic(text: str) -> bool:
    """Check if text contains significant Cyrillic (Russian)."""
    if not text:
        return False
    cyrillic = sum(1 for c in text if '\u0400' <= c <= '\u04ff')
    return cyrillic > len(text) * 0.15


def _translate_to_en(text: str) -> str:
    """Translate Russian text to English using Gemini flash-lite (cheap/fast)."""
    if not text or not _is_mostly_cyrillic(text):
        return text
    import subprocess
    binary = _bot.GEMINI_BIN
    prompt = f"Translate to English. Output ONLY the translation, no explanations, no quotes:\n{text}"
    cmd = [binary, "--output-format", "json", "--yolo",
           "--model", "gemini-2.5-flash-lite", "--prompt", prompt]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=30, cwd=_bot.WORK_DIR)
        translated = _parse_cli_output(result.stdout.strip())
        return translated.strip() or text
    except Exception:
        return text  # fallback if translation fails


def _call_agent(agent: str, prompt: str, timeout: int = 300) -> str:
    import subprocess
    binary    = _BINS[agent]
    sessions  = _p_sessions()
    sid_file  = sessions[agent]
    is_claude = "claude" in binary

    session_id = None
    try:
        with open(sid_file) as f:
            session_id = f.read().strip() or None
    except Exception:
        pass

    model = _bot.get_model(agent)
    cmd = [binary]
    if is_claude:
        cmd += ["--print", "--dangerously-skip-permissions"]
    cmd += ["--output-format", "json"]
    if not is_claude:
        cmd += ["--yolo"]
    if model:
        cmd += ["--model", model]
    if session_id:
        cmd += ["--resume", session_id]
    if is_claude:
        cmd.append(prompt)
    else:
        cmd += ["--prompt", prompt]

    env = os.environ.copy()
    for var in ("CLAUDECODE", "GEMINICODE", "QWENCODE"):
        env.pop(var, None)

    # Агент работает в директории своего проекта
    cwd = _project_dir()
    os.makedirs(cwd, exist_ok=True)
    os.makedirs(_p_team_dir(), exist_ok=True)

    try:
        stdout, stderr, rc, timed_out = _bot._run_subprocess(cmd, timeout, cwd, env)
        if timed_out:
            return f"⏱ {agent} timeout ({timeout}s) — процесс завершён"

        raw   = stdout.strip()
        reply = _parse_cli_output(raw)

        # Сохраняем session_id для продолжения контекста
        try:
            data  = json.loads(raw)
            items = data if isinstance(data, list) else [data]
            for item in items:
                sid = (item.get("session_id") or item.get("sessionId")) if isinstance(item, dict) else None
                if sid:
                    with open(sid_file, "w") as f:
                        f.write(sid)
                    break
        except Exception:
            pass

        return reply or stderr.strip()[:500] or "⚠️ Пустой ответ"
    except Exception as e:
        return f"❌ {agent} ошибка: {e}"


def _team_log(msg: str):
    ts   = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    os.makedirs(_p_team_dir(), exist_ok=True)
    try:
        with open(_p_file("team_log.md"), "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    _bot.tg_send(line)


# ── ЖУРНАЛ ПРОЕКТА ─────────────────────────────────────────────
def _get_changed_files() -> list[str]:
    """Список файлов изменённых в git (исключая .tg_team)."""
    import subprocess
    try:
        r = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, cwd=_project_dir()
        )
        files = [l for l in r.stdout.splitlines() if ".tg_team" not in l]
        if not files:
            r2 = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                capture_output=True, text=True, cwd=_project_dir()
            )
            files = [l for l in r2.stdout.splitlines() if ".tg_team" not in l]
        return files[:20]
    except Exception:
        return []


def _append_project_log(task: str, rounds: int, verdict: str):
    """Записывает итог задачи в журнал проекта."""
    os.makedirs(_p_team_dir(), exist_ok=True)
    ts           = time.strftime("%Y-%m-%d %H:%M")
    verdict_icon = "✅" if verdict == "APPROVED" else "⚠️"
    task_log     = _translate_to_en(task)

    coder_summary = ""
    try:
        txt = open(_p_file("coder_output.md")).read().strip()
        coder_summary = txt[:400].split("\n\n\n")[0].strip()
    except Exception:
        pass

    changed   = _get_changed_files()
    files_str = ", ".join(changed) if changed else "нет изменений"

    entry = (
        f"\n## [{ts}] {verdict_icon} {task_log}\n"
        f"Rounds: {rounds} | Verdict: {verdict}\n"
        f"Files: {files_str}\n"
    )
    if coder_summary:
        entry += f"Итог: {coder_summary[:300]}\n"
    entry += "---"

    with open(_p_file("project_log.md"), "a") as f:
        f.write(entry + "\n")


def _project_context() -> str:
    """Читает журнал проекта для инжекции в промпты агентов."""
    try:
        content = open(_p_file("project_log.md")).read().strip()
        if not content:
            return ""
        if len(content) > PROJECT_LOG_MAX_CHARS:
            content = "...(старые задачи обрезаны)\n" + content[-PROJECT_LOG_MAX_CHARS:]
        return (
            "\n\n[PROJECT HISTORY — completed tasks. "
            "Do NOT redo what is already done, build on existing code:\n"
            f"{content}\n]"
        )
    except FileNotFoundError:
        return ""


# ── PROMPTS ─────────────────────────────────────────────────────
def _planner_prompt(task: str) -> str:
    ctx      = _project_context()
    proj_dir = _project_dir()
    return f"""You are PLANNER in an AI agent team. Working dir: {proj_dir}{ctx}

TASK: {task}

Create implementation plan. Check project history above — do not redo completed work.
Save to .tg_team/plan.md:

# Plan: {task}

## Goal
One sentence.

## Existing code (do not touch)
List files that must not be changed.

## Files to create/modify
- path/to/file — reason

## Steps
1. Step
2. ...

## Acceptance criteria
- [ ] Criterion 1
- [ ] Criterion 2

IMPORTANT: Plan only, no implementation. Save file."""


def _coder_prompt() -> str:
    ctx      = _project_context()
    proj_dir = _project_dir()
    return f"""You are CODER. Working dir: {proj_dir}{ctx}

Read .tg_team/plan.md and implement everything described.
Use existing code from history above — don't duplicate, extend.

After implementation write report to .tg_team/coder_output.md:
- Each created/modified file (one line each)
- Deviations from plan"""


def _fixer_prompt(fix_round: int, prev_round: int) -> str:
    ctx = _project_context()
    return f"""You are CODER. Fix round {fix_round}.{ctx}

Read .tg_team/debug_round_{prev_round}.md — it contains issues found.
Fix ALL issues listed. Do not touch what works.

Update .tg_team/coder_output.md — add section "Fix Round {fix_round}"."""


def _debugger_prompt(review_round: int, max_r: int, test_output: str = "") -> str:
    ctx        = _project_context()
    limit_note = f"(max {max_r} rounds)" if max_r > 0 else "(no limit)"
    test_section = ""
    if test_output:
        test_section = f"\n\nAUTO-TEST OUTPUT:\n```\n{test_output}\n```\nConsider test results in your verdict."
    return f"""You are DEBUGGER. Review round {review_round} {limit_note}.{ctx}

Read:
- .tg_team/plan.md — requirements and acceptance criteria
- .tg_team/coder_output.md — what was done

Check project files. Evaluate:
1. Does implementation match acceptance criteria?
2. Syntax errors or obvious bugs?
3. Missing files or incomplete fragments?{test_section}

Write report to .tg_team/debug_round_{review_round}.md

At the end of the report, on a separate line, exactly one of:
VERDICT: APPROVED
VERDICT: ISSUES FOUND

If ISSUES FOUND — list each issue with file and line."""


def _code_review_prompt(target: str) -> str:
    proj_dir = _project_dir()
    return f"""You are a CODE REVIEWER. Working dir: {proj_dir}

Review the following code/files: {target}

Provide structured review:
## Summary
Brief overall assessment.
## Issues Found
- [ ] Issue 1 (file:line)
## Security Concerns
- List any security issues
## Suggestions
- Improvement suggestions
## Verdict
APPROVED or NEEDS WORK"""


# ── ПАЙПЛАЙН ───────────────────────────────────────────────────
def _read_verdict(review_round: int) -> str:
    path = _p_file(f"debug_round_{review_round}.md")
    try:
        with open(path) as f:
            lines = f.read().splitlines()
        for line in reversed(lines):
            s = line.strip().upper()
            if "VERDICT: APPROVED" in s:
                return "APPROVED"
            if "VERDICT: ISSUES FOUND" in s:
                return "ISSUES FOUND"
    except Exception:
        pass
    return "ISSUES FOUND"


def _clear_team_sessions():
    for sid_file in _p_sessions().values():
        if os.path.exists(sid_file):
            os.remove(sid_file)


def _detect_build_cmd(proj_dir: str) -> str | None:
    """Определяет команду сборки по файлам проекта."""
    if os.path.exists(os.path.join(proj_dir, "gradlew")):
        return "./gradlew assembleDebug"
    if os.path.exists(os.path.join(proj_dir, "build.gradle")):
        return "gradle assembleDebug"
    if os.path.exists(os.path.join(proj_dir, "pom.xml")):
        return "mvn package"
    if os.path.exists(os.path.join(proj_dir, "Makefile")):
        return "make"
    if os.path.exists(os.path.join(proj_dir, "package.json")):
        return "npm run build"
    return None


def _run_build() -> bool:
    """Запускает сборку проекта. Возвращает True при успехе."""
    import subprocess
    s        = _load_state()
    proj_dir = _project_dir()
    cmd_str  = s.get("build_cmd") or _detect_build_cmd(proj_dir)

    if not cmd_str:
        _bot.tg_send("⚠️ Не удалось определить команду сборки. Укажи вручную в настройках проекта.")
        return False

    _bot.tg_send(f"🔨 Запускаю сборку: `{cmd_str}`")
    _team_log(f"BUILD: {cmd_str}")

    try:
        result = subprocess.run(
            cmd_str, shell=True, cwd=proj_dir,
            capture_output=True, text=True, timeout=600
        )
        if result.returncode == 0:
            _bot.tg_send("✅ Сборка успешна!")
            _team_log("BUILD: SUCCESS")
            _auto_send_build_artifacts()
            return True
        else:
            err = (result.stderr or result.stdout)[-2000:]
            _bot.tg_send(f"❌ Сборка упала (код {result.returncode}):\n\n{err}")
            _team_log(f"BUILD: FAILED rc={result.returncode}")
            return False
    except subprocess.TimeoutExpired:
        _bot.tg_send("⏱ Сборка превысила 10 мин — остановлена.")
        _team_log("BUILD: TIMEOUT")
        return False
    except Exception as e:
        _bot.tg_send(f"❌ Ошибка сборки: {e}")
        return False


def _send_pause_buttons(fix_round: int):
    s = _load_state()
    max_r = s["max_rounds"]
    _bot.tg_send(
        f"⏸ Пауза после {fix_round} раунд(а)\n\n"
        f"Задача: {s['task']}\n"
        f"Лимит раундов: {'∞' if max_r == 0 else max_r}\n\n"
        f"Дебаггер нашёл проблемы. Что делать?",
        _bot.kb([
            [("▶️ +3 раунда", "team_continue:3"),
             ("▶️ +5 раундов", "team_continue:5")],
            [("▶️ +∞ (до APPROVED)", "team_continue:0")],
            [("🔍 Изучить результат", "team_review"),
             ("🛑 Остановить", "team_stop")],
        ])
    )


def _detect_test_cmd(proj_dir: str) -> str | None:
    """Определяет команду запуска тестов по файлам проекта."""
    if os.path.exists(os.path.join(proj_dir, "gradlew")):
        return "./gradlew test"
    if os.path.exists(os.path.join(proj_dir, "pytest.ini")) or \
       os.path.exists(os.path.join(proj_dir, "setup.py")) or \
       os.path.exists(os.path.join(proj_dir, "pyproject.toml")):
        return "pytest"
    pkg_json = os.path.join(proj_dir, "package.json")
    if os.path.exists(pkg_json):
        try:
            import json as _json
            data = _json.load(open(pkg_json))
            if "test" in data.get("scripts", {}):
                return "npm test"
        except Exception:
            pass
    if os.path.exists(os.path.join(proj_dir, "Makefile")):
        return "make test"
    return None


def _run_tests() -> str:
    """Запускает тесты проекта и возвращает вывод (последние 2000 символов)."""
    import subprocess
    s        = _load_state()
    proj_dir = _project_dir()
    cmd_str  = s.get("test_cmd") or _detect_test_cmd(proj_dir)

    if not cmd_str:
        return "(no test command detected)"

    try:
        result = subprocess.run(
            cmd_str, shell=True, cwd=proj_dir,
            capture_output=True, text=True, timeout=300
        )
        output = (result.stdout + result.stderr).strip()
        return output[-2000:] if len(output) > 2000 else output
    except subprocess.TimeoutExpired:
        return "(test timed out after 5 min)"
    except Exception as e:
        return f"(test error: {e})"


def _auto_git_commit(task: str) -> bool:
    """Auto git commit after APPROVED."""
    import subprocess
    proj_dir = _project_dir()
    try:
        subprocess.run(["git", "add", "-A"], cwd=proj_dir, timeout=10)
        msg = f"feat: {task[:70]}\n\nAuto-commit after team APPROVED"
        result = subprocess.run(["git", "commit", "-m", msg],
                               cwd=proj_dir, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            _bot.tg_send(f"✅ Git commit создан:\n`{msg[:60]}...`")
            return True
        elif "nothing to commit" in result.stdout:
            _bot.tg_send("ℹ️ Git: нечего коммитить (нет изменений)")
            return False
        else:
            _bot.tg_send(f"⚠️ Git commit: {result.stderr[:300]}")
            return False
    except Exception as e:
        _bot.tg_send(f"❌ Git commit ошибка: {e}")
        return False


def _pipeline(task: str, roles: dict, start_round: int = 0):
    global _pipeline_thread
    try:
        s     = _load_state()
        max_r = s["max_rounds"]

        if start_round == 0:
            # Планирование
            _team_log(f"🗂 PLANNING — {roles['planner'].upper()} creating plan...")
            _set_phase("PLANNING")
            _call_agent(roles["planner"], _planner_prompt(task))
            if _is_stopped():
                return
            if not os.path.exists(_p_file("plan.md")):
                _team_log("❌ Planner did not create plan.md. Stopping.")
                _set_phase("IDLE")
                return
            _team_log("✅ Plan ready. Starting implementation...")

            # Реализация
            _team_log(f"💻 CODING — {roles['coder'].upper()} implementing...")
            _set_phase("CODING")
            _call_agent(roles["coder"], _coder_prompt())
            if _is_stopped():
                return

            # Авто-тесты (если включены, перед дебаггером)
            test_output = ""
            if s.get("run_tests"):
                _team_log("🧪 Running tests...")
                test_output = _run_tests()
                if test_output:
                    _bot.tg_send(f"🧪 Test output:\n```\n{test_output[:1000]}\n```")

        # Цикл проверки/исправления
        review_round = start_round
        test_output  = ""
        while True:
            if _is_stopped():
                return

            review_round += 1
            _set_phase("DEBUGGING", {"fix_round": review_round})
            _team_log(
                f"🔍 DEBUGGING — {roles['debugger'].upper()} "
                f"reviewing (round {review_round}" +
                (f"/{max_r}" if max_r > 0 else "") + ")..."
            )
            _call_agent(roles["debugger"], _debugger_prompt(review_round, max_r, test_output))
            verdict = _read_verdict(review_round)

            if verdict == "APPROVED":
                _team_log(f"✅ APPROVED after round {review_round}!\nTask done: {task}")
                _set_phase("DONE")
                _append_project_log(task, review_round, "APPROVED")
                proj = _cur_project()
                # Сборка если включена
                if s.get("build_apk"):
                    _run_build()
                else:
                    # Просто ищем уже готовые артефакты
                    _auto_send_build_artifacts()
                # Авто-коммит если включён
                if s.get("auto_commit"):
                    _auto_git_commit(task)
                _bot.tg_send(
                    f"🎉 Задача выполнена!\n\n"
                    f"📁 Проект: {proj}\n"
                    f"📋 Задача: {task}",
                    _bot.kb([[
                        ("🔍 Изучить результат", "team_review"),
                        ("📝 Следующая задача", "team_new_task"),
                        ("📖 История", "team_project_log"),
                    ]])
                )
                return

            # Лимит раундов
            if max_r > 0 and review_round >= max_r:
                _set_phase("PAUSED", {"fix_round": review_round})
                _team_log(f"⏸ Limit {max_r} round(s) reached. Waiting for user...")
                _send_pause_buttons(review_round)
                _pause_event.clear()
                _pause_event.wait()
                if _is_stopped():
                    return
                s     = _load_state()
                max_r = s["max_rounds"]

            if _is_stopped():
                return

            # Исправление
            _team_log(f"🔧 FIXING — {roles['coder'].upper()} fixing (round {review_round + 1})...")
            _set_phase("FIXING", {"fix_round": review_round + 1})
            _call_agent(roles["coder"], _fixer_prompt(review_round + 1, review_round))
            # Re-run tests before next debug round if enabled
            test_output = ""
            if _load_state().get("run_tests"):
                test_output = _run_tests()

    except Exception as e:
        _bot.log(f"TEAM PIPELINE ERROR: {e}")
        _team_log(f"❌ Pipeline error: {e}")
        _set_phase("IDLE")


# ── МЕНЮ ───────────────────────────────────────────────────────
PHASE_ICONS = {
    "IDLE": "💤", "PLANNING": "🗂", "CODING": "💻",
    "DEBUGGING": "🔍", "FIXING": "🔧", "PAUSED": "⏸",
    "DONE": "✅", "STOPPED": "🛑",
}


def send_team_menu(message_id: int | None = None):
    """Главное меню команды агентов."""
    s     = _load_state()
    phase = s["phase"]
    icon  = PHASE_ICONS.get(phase, "❓")
    roles = s["roles"]
    max_r = s["max_rounds"]
    proj  = s.get("project", "") or "—"
    round_str = f"{s['fix_round']}" + (f"/{max_r}" if max_r > 0 else "/∞")

    build_apk   = s.get("build_apk", False)
    build_cmd   = s.get("build_cmd", "") or _detect_build_cmd(_project_dir()) or "не определена"
    build_icon  = "🔨 ВКЛ" if build_apk else "🔨 ВЫКЛ"
    run_tests   = s.get("run_tests", False)
    auto_commit = s.get("auto_commit", False)
    preset      = s.get("preset", "") or "—"

    lines = [
        "👥 Команда агентов", "",
        f"📁 Проект: {proj}",
        f"{icon} Фаза: {phase}",
        f"📋 Задача: {s['task'] or '—'}",
        f"🔄 Раунд: {round_str}",
        "",
        f"  🗂 Planner:  {_bot.agent_label(roles['planner'])}",
        f"  💻 Coder:    {_bot.agent_label(roles['coder'])}",
        f"  🔍 Debugger: {_bot.agent_label(roles['debugger'])}",
        f"  ⏱ Лимит раундов: {'∞' if max_r == 0 else max_r}",
        f"  {build_icon} Сборка: {build_cmd}",
        f"  🧪 Тесты: {'ВКЛ' if run_tests else 'ВЫКЛ'} | 🔀 Авто-коммит: {'ВКЛ' if auto_commit else 'ВЫКЛ'}",
    ]

    is_active = phase in ("PLANNING", "CODING", "DEBUGGING", "FIXING")
    is_paused = phase == "PAUSED"

    rows = []
    if is_active:
        rows.append([("🛑 Остановить", "team_stop"),
                     ("📊 Статус", "team_status")])
    elif is_paused:
        rows.append([("▶️ +3 раунда", "team_continue:3"),
                     ("▶️ +5 раундов", "team_continue:5")])
        rows.append([("▶️ +∞", "team_continue:0"),
                     ("🛑 Стоп", "team_stop")])
    else:
        rows.append([("🚀 Новая задача", "team_new_task"),
                     ("🆕 Новый проект", "team_new_project")])

    rows.append([("🎭 Роли", "team_roles"),
                 ("🎯 Пресеты", "team_presets")])
    rows.append([("⏱ Раундов: " + ("∞" if max_r == 0 else str(max_r)), "team_rounds_menu")])
    # Кнопка сборки + тесты
    rows.append([(f"{'🔨✅' if build_apk else '🔨❌'} Сборка APK", "team_toggle_build"),
                 ("⚙️ Команда сборки", "team_set_build_cmd")])
    rows.append([(f"{'🧪✅' if run_tests else '🧪❌'} Тесты", "team_toggle_tests"),
                 (f"{'🔀✅' if auto_commit else '🔀❌'} Авто-коммит", "team_toggle_commit")])
    rows.append([("📂 Мои проекты", "team_project_list")])

    if phase not in ("IDLE",):
        rows.append([("🔍 Изучить результат", "team_review")])

    if phase in ("IDLE", "DONE", "STOPPED"):
        rows.append([("🔎 Code Review", "team_code_review")])

    rows.append([("📋 Лог", "team_log"),
                 ("📖 История", "team_project_log"),
                 ("← Назад", "cmd:agent_menu")])

    markup = _bot.kb(rows)
    text   = "\n".join(lines)
    if message_id:
        _bot.tg_edit(message_id, text, markup)
    else:
        _bot.tg_send(text, markup)


def send_project_list_menu(message_id: int | None = None):
    """Список всех проектов — переключение и создание нового."""
    projects = _list_projects()
    cur      = _cur_project()

    if not projects:
        text = "📂 Проектов пока нет\n\nСоздай первый проект!"
        rows = [
            [("🆕 Создать проект", "team_new_project")],
            [("← Назад", "team_menu")],
        ]
    else:
        lines = [f"📂 Проекты ({len(projects)})", "", "Выбери проект для переключения:"]
        for p in projects:
            info = _project_info(p)
            mark = "▶ " if p == cur else ""
            lines.append(f"  {mark}{p}" + (f"  — {info}" if info else ""))
        text = "\n".join(lines)

        rows = []
        for p in projects:
            mark  = "▶️ " if p == cur else ""
            label = f"{mark}{p}"
            rows.append([(label, f"team_switch_project:{p}")])

        rows.append([("🆕 Новый проект", "team_new_project"),
                     ("← Назад", "team_menu")])

    if message_id:
        _bot.tg_edit(message_id, text, _bot.kb(rows))
    else:
        _bot.tg_send(text, _bot.kb(rows))


def send_new_task_menu(message_id: int | None = None):
    """Меню перед запуском задачи: продолжить проект или новый."""
    s        = _load_state()
    proj     = s.get("project", "")
    proj_dir = _project_dir(proj) if proj else None

    if proj:
        text = (
            f"🚀 Новая задача\n\n"
            f"📁 Текущий проект: {proj}\n"
            f"📂 Папка: {proj_dir}\n\n"
            f"Продолжить в этом проекте или выбрать другой?"
        )
        rows = [
            [(f"▶️ Задачу в «{proj}»", "team_task_in_cur")],
            [("🆕 Новый проект", "team_new_project")],
            [("📂 Другой проект", "team_project_list"),
             ("← Назад", "team_menu")],
        ]
    else:
        text = (
            "🚀 Новая задача\n\n"
            "Нет активного проекта.\n"
            "Создай новый или выбери существующий."
        )
        rows = [
            [("🆕 Создать проект", "team_new_project")],
            [("📂 Мои проекты", "team_project_list"),
             ("← Назад", "team_menu")],
        ]

    if message_id:
        _bot.tg_edit(message_id, text, _bot.kb(rows))
    else:
        _bot.tg_send(text, _bot.kb(rows))


def send_rounds_menu(message_id: int | None = None):
    s       = _load_state()
    cur     = s["max_rounds"]
    options = [3, 5, 10, 0]
    labels  = {3: "3 раунда", 5: "5 раундов", 10: "10 раундов", 0: "∞ авто"}

    rows = []
    row  = []
    for n in options:
        mark = "✓ " if n == cur else ""
        row.append((f"{mark}{labels[n]}", f"team_set_rounds:{n}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("← Назад", "team_menu")])

    text = (
        f"⏱ Лимит раундов исправлений\n"
        f"Текущий: {'∞' if cur == 0 else cur}\n\n"
        f"При достижении лимита — пауза с выбором."
    )
    if message_id:
        _bot.tg_edit(message_id, text, _bot.kb(rows))
    else:
        _bot.tg_send(text, _bot.kb(rows))


def send_team_review(message_id: int | None = None):
    """Панель просмотра результатов работы команды."""
    s      = _load_state()
    phase  = s["phase"]
    icon   = PHASE_ICONS.get(phase, "❓")
    fix_r  = s["fix_round"]
    proj   = s.get("project", "") or "—"

    lines = [
        "🔍 Результаты команды", "",
        f"📁 Проект: {proj}",
        f"{icon} Фаза: {phase}",
        f"Задача: {s['task'] or '—'}",
        f"Раунд: {fix_r}",
        "",
        "Что посмотреть:",
    ]

    rows = [
        [("📋 План (plan.md)", "team_review:plan"),
         ("💻 Код (coder_output.md)", "team_review:coder")],
    ]
    debug_buttons = []
    for r in range(1, fix_r + 1):
        path = _p_file(f"debug_round_{r}.md")
        if os.path.exists(path):
            verdict = _read_verdict(r)
            v_icon  = "✅" if verdict == "APPROVED" else "❌"
            debug_buttons.append((f"{v_icon} Debug #{r}", f"team_review:debug:{r}"))
    for i in range(0, len(debug_buttons), 2):
        rows.append(debug_buttons[i:i+2])

    rows.append([("📁 Изменённые файлы", "team_review:files"),
                 ("📖 История проекта", "team_project_log")])

    is_active = phase in ("PLANNING", "CODING", "DEBUGGING", "FIXING")
    if phase == "PAUSED":
        rows.append([("▶️ Продолжить", "team_continue:3"),
                     ("🛑 Стоп", "team_stop")])
    elif not is_active:
        rows.append([("🚀 Новая задача", "team_new_task"),
                     ("🔨 Собрать APK", "team_build_now")])

    rows.append([("← Меню", "team_menu")])

    text = "\n".join(lines)
    if message_id:
        _bot.tg_edit(message_id, text, _bot.kb(rows))
    else:
        _bot.tg_send(text, _bot.kb(rows))


def _show_file(path: str, title: str, back_cb: str, msg_id: int | None):
    try:
        content = open(path).read()
    except FileNotFoundError:
        content = "(файл не найден)"
    back   = _bot.kb([[("← Назад к обзору", back_cb)]])
    header = f"📄 {title}\n\n"
    full   = header + content
    if msg_id:
        if len(full) <= _bot.TG_MAX_LEN:
            _bot.tg_edit(msg_id, full, back)
        else:
            _bot.tg_edit(msg_id, f"📄 {title} — содержимое ниже:", back)
            _bot.tg_send(content)
    else:
        _bot.tg_send(full)


# ── AWAIT STATE ─────────────────────────────────────────────────
BUILD_CMD_AWAIT_FILE = os.path.join(_bot.STATE_DIR, "team_build_cmd_await.txt")


def build_cmd_await_set():
    with open(BUILD_CMD_AWAIT_FILE, "w") as f:
        f.write("1")


def build_cmd_await_clear():
    try:
        os.remove(BUILD_CMD_AWAIT_FILE)
    except FileNotFoundError:
        pass


def build_cmd_await_get() -> bool:
    return os.path.exists(BUILD_CMD_AWAIT_FILE)


def task_await_set():
    with open(TASK_AWAIT_FILE, "w") as f:
        f.write("1")


def task_await_clear():
    try:
        os.remove(TASK_AWAIT_FILE)
    except FileNotFoundError:
        pass


def task_await_get() -> bool:
    return os.path.exists(TASK_AWAIT_FILE)


def project_await_set():
    with open(PROJECT_AWAIT_FILE, "w") as f:
        f.write("1")


def project_await_clear():
    try:
        os.remove(PROJECT_AWAIT_FILE)
    except FileNotFoundError:
        pass


def project_await_get() -> bool:
    return os.path.exists(PROJECT_AWAIT_FILE)


def code_review_await_set():
    with open(CODE_REVIEW_AWAIT_FILE, "w") as f:
        f.write("1")


def code_review_await_clear():
    try:
        os.remove(CODE_REVIEW_AWAIT_FILE)
    except FileNotFoundError:
        pass


def code_review_await_get() -> bool:
    return os.path.exists(CODE_REVIEW_AWAIT_FILE)


def run_code_review(target: str):
    """Запускает code review агентом-дебаггером."""
    s      = _load_state()
    roles  = s["roles"]
    agent  = roles.get("debugger", "claude")
    _bot.tg_send(f"🔎 Code Review — {agent.upper()} анализирует: {target[:80]}...")
    prompt = _code_review_prompt(target)
    reply  = _call_agent(agent, prompt, timeout=300)
    _bot.tg_send(f"🔎 Code Review результат:\n\n{reply}")


def send_presets_menu(message_id: int | None = None):
    """Показывает меню пресетов ролей."""
    s   = _load_state()
    cur = s.get("preset", "")
    lines = ["🎯 Пресеты ролей команды", ""]
    for key, p in ROLE_PRESETS.items():
        mark = "✓ " if key == cur else ""
        lines.append(f"  {mark}{p['label']}: {p['planner']} / {p['coder']} / {p['debugger']}")
    text = "\n".join(lines)
    rows = []
    for key, p in ROLE_PRESETS.items():
        mark = "✓ " if key == cur else ""
        rows.append([(f"{mark}{p['label']}", f"team_preset:{key}")])
    rows.append([("← Назад", "team_menu")])
    markup = _bot.kb(rows)
    if message_id:
        _bot.tg_edit(message_id, text, markup)
    else:
        _bot.tg_send(text, markup)


def create_project_and_await_task(name: str):
    """Создаёт проект, делает его текущим, ждёт ввода задачи."""
    slug     = _slug(name)
    proj_dir = os.path.join(PROJECTS_DIR, slug)
    os.makedirs(proj_dir, exist_ok=True)
    os.makedirs(os.path.join(proj_dir, ".tg_team"), exist_ok=True)

    with _team_lock:
        s = _load_state()
        s["project"]   = slug
        s["phase"]     = "IDLE"
        s["task"]      = ""
        s["fix_round"] = 0
        _save_state(s)

    # Новый проект — сброс сессий агентов
    _clear_team_sessions()

    task_await_set()
    _bot.tg_send(
        f"✅ Проект создан: {slug}\n"
        f"📂 Папка: {proj_dir}\n\n"
        f"✏️ Напиши задачу для этого проекта:"
    )


def switch_project_and_await_task(proj: str):
    """Переключается на существующий проект, ждёт ввода задачи."""
    with _team_lock:
        s = _load_state()
        old_proj       = s.get("project", "")
        s["project"]   = proj
        s["phase"]     = "IDLE"
        s["task"]      = ""
        s["fix_round"] = 0
        _save_state(s)

    # Сессии сбрасываем только если проект изменился
    if proj != old_proj:
        _clear_team_sessions()

    proj_dir = _project_dir(proj)
    # Показываем последнюю задачу из истории если есть
    last_task = _project_info(proj)
    hint = f"\nПоследняя задача: {last_task}" if last_task else ""

    task_await_set()
    _bot.tg_send(
        f"📁 Проект: {proj}\n"
        f"📂 Папка: {proj_dir}{hint}\n\n"
        f"✏️ Введи задачу:"
    )


# ── КОЛБЭКИ ────────────────────────────────────────────────────
def handle_team_callback(cb_id: str, msg_id: int, data: str):
    """Обрабатывает все team_* callback."""
    global _pipeline_thread

    if data == "team_menu":
        _bot.tg_answer_cb(cb_id)
        send_team_menu(msg_id)

    elif data == "team_new_task":
        _bot.tg_answer_cb(cb_id)
        s = _load_state()
        if s.get("project"):
            send_new_task_menu(msg_id)
        else:
            # Нет проекта — сначала создаём
            project_await_set()
            _bot.tg_edit(
                msg_id,
                "🆕 Новый проект\n\n✏️ Введи название проекта:",
                _bot.kb([[("❌ Отмена", "team_cancel_await")]])
            )

    elif data == "team_new_project":
        project_await_set()
        _bot.tg_answer_cb(cb_id, "✏️ Введи название")
        _bot.tg_edit(
            msg_id,
            "🆕 Новый проект\n\n✏️ Введи название проекта:",
            _bot.kb([[("❌ Отмена", "team_cancel_await")]])
        )

    elif data == "team_task_in_cur":
        # Запускаем задачу в текущем проекте
        _bot.tg_answer_cb(cb_id, "✏️ Напиши задачу")
        s     = _load_state()
        roles = s["roles"]
        proj  = s.get("project", "—")
        task_await_set()
        _bot.tg_edit(
            msg_id,
            f"📁 Проект: {proj}\n\n"
            f"Роли:\n"
            f"  🗂 Planner:  {_bot.agent_label(roles['planner'])}\n"
            f"  💻 Coder:    {_bot.agent_label(roles['coder'])}\n"
            f"  🔍 Debugger: {_bot.agent_label(roles['debugger'])}\n\n"
            f"✏️ Напиши описание задачи:",
            _bot.kb([[("❌ Отмена", "team_cancel_await")]])
        )

    elif data == "team_cancel_await":
        task_await_clear()
        project_await_clear()
        build_cmd_await_clear()
        code_review_await_clear()
        _bot.tg_answer_cb(cb_id, "Отменено")
        send_team_menu(msg_id)

    elif data == "team_toggle_build":
        with _team_lock:
            s = _load_state()
            s["build_apk"] = not s.get("build_apk", False)
            _save_state(s)
        state = "включена" if s["build_apk"] else "выключена"
        _bot.tg_answer_cb(cb_id, f"🔨 Сборка {state}")
        send_team_menu(msg_id)

    elif data == "team_set_build_cmd":
        build_cmd_await_set()
        _bot.tg_answer_cb(cb_id, "✏️ Введи команду сборки")
        proj_dir = _project_dir()
        detected = _detect_build_cmd(proj_dir) or "не определена"
        _bot.tg_edit(
            msg_id,
            f"⚙️ Команда сборки\n\n"
            f"📂 Проект: {proj_dir}\n"
            f"🔍 Авто-определено: {detected}\n\n"
            f"Введи команду (например: ./gradlew assembleDebug)\n"
            f"Или напиши 'авто' для авто-определения:",
            _bot.kb([[("❌ Отмена", "team_cancel_await")]])
        )

    elif data == "team_build_now":
        _bot.tg_answer_cb(cb_id, "🔨 Запускаю сборку...")
        threading.Thread(target=_run_build, daemon=True).start()

    elif data == "team_project_list":
        _bot.tg_answer_cb(cb_id)
        send_project_list_menu(msg_id)

    elif data.startswith("team_switch_project:"):
        proj = data.split(":", 1)[1]
        _bot.tg_answer_cb(cb_id, f"📁 {proj}")
        # Переключаем и ждём задачу
        switch_project_and_await_task(proj)
        send_team_menu(msg_id)

    elif data == "team_stop":
        _set_phase("STOPPED")
        _pause_event.set()
        _bot.tg_answer_cb(cb_id, "🛑 Остановлено")
        send_team_menu(msg_id)

    elif data == "team_status":
        _bot.tg_answer_cb(cb_id)
        send_team_menu(msg_id)

    elif data == "team_roles":
        _bot.tg_answer_cb(cb_id)
        send_role_menu(msg_id)

    elif data == "team_rounds_menu":
        _bot.tg_answer_cb(cb_id)
        send_rounds_menu(msg_id)

    elif data.startswith("team_set_rounds:"):
        n = int(data.split(":")[1])
        with _team_lock:
            s = _load_state()
            s["max_rounds"] = n
            _save_state(s)
        label = "∞" if n == 0 else str(n)
        _bot.tg_answer_cb(cb_id, f"✅ Лимит: {label}")
        send_rounds_menu(msg_id)

    elif data.startswith("team_continue:"):
        extra = int(data.split(":")[1])
        with _team_lock:
            s = _load_state()
            cur_round = s["fix_round"]
            s["max_rounds"] = (cur_round + extra) if extra > 0 else 0
            s["phase"] = "DEBUGGING"
            _save_state(s)
        label = f"+{extra} раунд(а)" if extra > 0 else "+∞"
        _bot.tg_answer_cb(cb_id, f"▶️ Продолжаем ({label})")
        _pause_event.set()
        send_team_menu(msg_id)

    elif data == "team_review":
        _bot.tg_answer_cb(cb_id)
        send_team_review(msg_id)

    elif data.startswith("team_review:"):
        _bot.tg_answer_cb(cb_id)
        what = data[len("team_review:"):]
        if what == "plan":
            _show_file(_p_file("plan.md"), "plan.md", "team_review", msg_id)
        elif what == "coder":
            _show_file(_p_file("coder_output.md"), "coder_output.md", "team_review", msg_id)
        elif what == "files":
            _show_changed_files(msg_id)
        elif what.startswith("debug:"):
            r    = int(what.split(":")[1])
            path = _p_file(f"debug_round_{r}.md")
            _show_file(path, f"debug_round_{r}.md", "team_review", msg_id)

    elif data == "team_project_log":
        _bot.tg_answer_cb(cb_id)
        try:
            content = open(_p_file("project_log.md")).read().strip()
            proj    = _cur_project() or "—"
            back    = _bot.kb([[("← Меню", "team_menu")]])
            header  = f"📖 История проекта «{proj}»:\n\n"
            text    = (header + content) if content else f"📖 История проекта «{proj}» пуста."
            if msg_id:
                if len(text) <= _bot.TG_MAX_LEN:
                    _bot.tg_edit(msg_id, text, back)
                else:
                    _bot.tg_edit(msg_id, f"📖 История — ниже:", back)
                    _bot.tg_send(content)
            else:
                _bot.tg_send(text)
        except FileNotFoundError:
            _bot.tg_send("📖 История пуста.")

    elif data == "team_log":
        _bot.tg_answer_cb(cb_id)
        try:
            lines = open(_p_file("team_log.md")).readlines()
            last  = "".join(lines[-25:])
            back  = _bot.kb([[("← Меню", "team_menu")]])
            text  = f"📋 Лог (последние 25):\n\n{last}"
            if msg_id:
                if len(text) <= _bot.TG_MAX_LEN:
                    _bot.tg_edit(msg_id, text, back)
                else:
                    _bot.tg_edit(msg_id, "📋 Лог — ниже:", back)
                    _bot.tg_send(last)
            else:
                _bot.tg_send(text)
        except Exception:
            _bot.tg_send("Лог пуст.")

    elif data.startswith("team_role:") or data == "team_noop":
        handle_role_callback(cb_id, msg_id, data)

    elif data == "team_presets":
        _bot.tg_answer_cb(cb_id)
        send_presets_menu(msg_id)

    elif data.startswith("team_preset:"):
        key = data.split(":", 1)[1]
        p   = ROLE_PRESETS.get(key)
        if p:
            with _team_lock:
                s = _load_state()
                s["roles"]["planner"]  = p["planner"]
                s["roles"]["coder"]    = p["coder"]
                s["roles"]["debugger"] = p["debugger"]
                s["preset"] = key
                _save_state(s)
            _bot.tg_answer_cb(cb_id, f"✅ Пресет: {p['label']}")
            send_presets_menu(msg_id)
        else:
            _bot.tg_answer_cb(cb_id, "⚠️ Неизвестный пресет")

    elif data == "team_toggle_tests":
        with _team_lock:
            s = _load_state()
            s["run_tests"] = not s.get("run_tests", False)
            _save_state(s)
        state = "включены" if s["run_tests"] else "выключены"
        _bot.tg_answer_cb(cb_id, f"🧪 Тесты {state}")
        send_team_menu(msg_id)

    elif data == "team_toggle_commit":
        with _team_lock:
            s = _load_state()
            s["auto_commit"] = not s.get("auto_commit", False)
            _save_state(s)
        state = "включён" if s["auto_commit"] else "выключён"
        _bot.tg_answer_cb(cb_id, f"🔀 Авто-коммит {state}")
        send_team_menu(msg_id)

    elif data == "team_code_review":
        code_review_await_set()
        _bot.tg_answer_cb(cb_id, "✏️ Укажи файлы для ревью")
        _bot.tg_edit(
            msg_id,
            "🔎 Code Review\n\n"
            "Напиши путь к файлу или текст кода для ревью.\n"
            "Пример: src/main.py\n"
            "Или просто опиши что нужно проверить:",
            _bot.kb([[("❌ Отмена", "team_cancel_await")]])
        )


# ── АВТО-ОТПРАВКА АРТЕФАКТОВ ────────────────────────────────────
def _auto_send_build_artifacts():
    """Ищет APK/AAB/IPA/EXE в папке проекта и отправляет в Telegram."""
    import glob as glob_mod
    build_exts = (".apk", ".aab", ".ipa", ".exe")
    proj_dir   = _project_dir()
    found      = []
    for ext in build_exts:
        found += glob_mod.glob(f"{proj_dir}/**/*{ext}", recursive=True)
        found += glob_mod.glob(f"{proj_dir}/*{ext}")
    if not found:
        return
    found = sorted(set(found), key=os.path.getmtime, reverse=True)
    sent  = 0
    for path in found[:3]:
        size_mb = os.path.getsize(path) / 1024 / 1024
        if size_mb > 50:
            _bot.tg_send(f"⚠️ {os.path.basename(path)} слишком большой ({size_mb:.1f}МБ)")
            continue
        _bot.tg_send(f"📦 Отправляю: {os.path.basename(path)} ({size_mb:.1f}МБ)...")
        _bot.tg_send_file(path, caption=f"📦 {os.path.basename(path)}")
        sent += 1
    if not sent and found:
        _bot.tg_send("📦 Найдены сборки: " + ", ".join(os.path.basename(p) for p in found[:3]))


def _show_changed_files(msg_id: int | None):
    """Показывает изменённые файлы проекта (git diff)."""
    import subprocess
    try:
        r = subprocess.run(
            ["git", "diff", "--name-status"],
            capture_output=True, text=True, cwd=_project_dir()
        )
        lines = [l for l in r.stdout.splitlines() if ".tg_team" not in l]
        if lines:
            text = "📁 Изменённые файлы:\n\n" + "\n".join(lines[:50])
        else:
            r2 = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                capture_output=True, text=True, cwd=_project_dir()
            )
            new_files = [l for l in r2.stdout.splitlines() if ".tg_team" not in l]
            text = ("📁 Новые файлы:\n\n" + "\n".join(new_files[:50])
                    if new_files else "Нет изменений в git.")
    except Exception:
        text = "⚠️ Git недоступен."

    back = _bot.kb([[("← Назад", "team_review")]])
    if msg_id:
        _bot.tg_edit(msg_id, text, back)
    else:
        _bot.tg_send(text)


# ── ЗАПУСК ЗАДАЧИ ──────────────────────────────────────────────
def start_task(task: str):
    """Запускает пайплайн для новой задачи в текущем проекте."""
    global _pipeline_thread

    s = _load_state()
    if s["phase"] not in ("IDLE", "DONE", "STOPPED"):
        _bot.tg_send(
            f"Команда уже работает: {s['phase']}\n"
            f"Используй меню → 🛑 Остановить"
        )
        return

    proj = s.get("project", "")
    if not proj:
        # Если проект не задан — создаём автоматически из задачи
        proj = _slug(task[:30])
        proj_dir = os.path.join(PROJECTS_DIR, proj)
        os.makedirs(proj_dir, exist_ok=True)
        os.makedirs(os.path.join(proj_dir, ".tg_team"), exist_ok=True)
        with _team_lock:
            s["project"] = proj
            _save_state(s)

    _clear_team_sessions()
    _pause_event.clear()

    # Translate task to English for agents
    task_en = _translate_to_en(task)

    with _team_lock:
        s = _load_state()
        s.update({
            "phase": "IDLE", "task": task, "task_en": task_en,
            "fix_round": 0, "started_at": time.strftime("%H:%M:%S"),
        })
        _save_state(s)

    if task_en != task:
        _bot.tg_send(f"🌐 Задача переведена для агентов:\n_{task_en}_")

    roles = s["roles"]
    max_r = s["max_rounds"]
    proj_dir = _project_dir(proj)

    _bot.tg_send(
        f"🚀 Запускаю команду!\n\n"
        f"📁 Проект: {proj}\n"
        f"📂 Папка: {proj_dir}\n"
        f"📋 Задача: {task}\n\n"
        f"  🗂 Planner:  {_bot.agent_label(roles['planner'])}\n"
        f"  💻 Coder:    {_bot.agent_label(roles['coder'])}\n"
        f"  🔍 Debugger: {_bot.agent_label(roles['debugger'])}\n"
        f"  ⏱ Лимит:    {'∞' if max_r == 0 else max_r} раундов\n\n"
        f"Жди обновлений...",
        _bot.kb([[("📊 Статус", "team_menu")]])
    )

    # Use English task for agents
    agent_task = task_en if task_en != task else task
    _pipeline_thread = threading.Thread(
        target=_pipeline, args=(agent_task, roles, 0), daemon=True
    )
    _pipeline_thread.start()


# ── ТЕКСТОВЫЕ КОМАНДЫ ──────────────────────────────────────────
def handle_command(text: str):
    parts = text.strip().split(maxsplit=2)
    sub   = parts[1].lower() if len(parts) > 1 else ""

    if sub == "start":
        task = parts[2] if len(parts) > 2 else ""
        if not task:
            _bot.tg_send("Укажи задачу: /team start <описание>\nИли используй /menu → 👥 Команда")
            return
        start_task(task)

    elif sub == "stop":
        _set_phase("STOPPED")
        _pause_event.set()
        _bot.tg_send("🛑 Остановлено.", _bot.kb([[("📊 Статус", "team_menu")]]))

    elif sub == "status":
        send_team_menu()

    elif sub == "log":
        try:
            lines = open(_p_file("team_log.md")).readlines()
            _bot.tg_send("📋 Лог:\n\n" + "".join(lines[-20:]))
        except Exception:
            _bot.tg_send("Лог пуст.")

    elif sub == "plan":
        _show_file(_p_file("plan.md"), "plan.md", "team_review", None)

    elif sub == "roles":
        send_role_menu()

    else:
        send_team_menu()


# ── РОЛИ ───────────────────────────────────────────────────────
def send_role_menu(message_id: int | None = None):
    s     = _load_state()
    roles = s["roles"]

    def row(role: str) -> list:
        return [
            (f"{'✓ ' if roles[role] == a else ''}{_bot.AGENT_NAMES[a]}",
             f"team_role:{role}:{a}")
            for a in AGENTS
        ]

    text = (
        f"🎭 Роли:\n\n"
        f"  🗂 Planner:  {_bot.agent_label(roles['planner'])}\n"
        f"  💻 Coder:    {_bot.agent_label(roles['coder'])}\n"
        f"  🔍 Debugger: {_bot.agent_label(roles['debugger'])}"
    )
    markup = _bot.kb([
        [("── 🗂 Planner ──", "team_noop")], row("planner"),
        [("── 💻 Coder ──", "team_noop")],   row("coder"),
        [("── 🔍 Debugger ──", "team_noop")], row("debugger"),
        [("✅ Готово", "team_menu")],
    ])
    if message_id:
        _bot.tg_edit(message_id, text, markup)
    else:
        _bot.tg_send(text, markup)


def handle_role_callback(cb_id: str, msg_id: int, data: str):
    if data == "team_noop":
        _bot.tg_answer_cb(cb_id)
        return
    parts = data.split(":", 2)
    if len(parts) != 3:
        _bot.tg_answer_cb(cb_id)
        return
    _, role, agent = parts
    if role == "done":
        _bot.tg_answer_cb(cb_id, "Роли сохранены!")
        send_team_menu(msg_id)
        return
    with _team_lock:
        s = _load_state()
        s["roles"][role] = agent
        _save_state(s)
    role_names = {"planner": "🗂 Planner", "coder": "💻 Coder", "debugger": "🔍 Debugger"}
    _bot.tg_answer_cb(cb_id, f"{role_names.get(role, role)} → {agent}")
    send_role_menu(msg_id)
