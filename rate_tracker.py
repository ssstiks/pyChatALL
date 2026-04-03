#!/usr/bin/env python3
"""
rate_tracker.py — Rate Limit Tracker for Claude, Gemini and Qwen.

Claude:
  1. Пассивный парсинг CLI (Source of Truth): "10 messages remaining until 3:00 PM"
  2. stats-cache.json (~/.claude/stats-cache.json) — реальные данные Claude Code CLI
     (все сессии, не только через бота). Показывает messageCount за 5ч и неделю.
  3. Ручной ввод (/usage, /limit) — процент остатка введённый вручную.

Gemini:
  Квота Code Assist API: 1500 RPD (сброс в 10:00 МСК / 07:00 UTC).
  Каждый «промпт» порождает ~10-20 внутренних API-запросов (сканирование файлов).
  Реально: ~70-100 «тяжёлых» промптов в день.
  RPM: до 20 в режиме CLI.

Qwen:
  1000 RPD (сброс в 03:00 МСК / 00:00 UTC).
  Практически без ограничений по RPM при ручном использовании.
"""

# ── Gemini квота ──────────────────────────────────────────────
_GEMINI_RPD         = 1500   # суточный лимит запросов Code Assist API
_GEMINI_AVG_MUL     = 15     # среднее кол-во API-вызовов на 1 промпт
_GEMINI_PROMPT_WARN = 80     # предупреждение начиная с этого кол-ва промптов
_GEMINI_PROMPT_CRIT = 95     # критический уровень (больше не рекомендуется)
_GEMINI_RPM_LIMIT   = 18     # RPM порог: при приближении — предупреждение
_MSK_OFFSET         = 3 * 3600   # UTC+3
_GEMINI_RESET_HOUR  = 10         # 10:00 МСК

# ── Claude Pro лимиты (приблизительные, зависят от модели) ───
_CLAUDE_LIMIT_5H    = 45     # ~45 сообщений за 5-часовое окно (Claude Pro Sonnet)
_CLAUDE_LIMIT_WEEK  = 400    # ~400 сообщений в неделю (Claude Pro)

# ── Qwen квота ────────────────────────────────────────────────
_QWEN_RPD           = 1000   # суточный лимит
_QWEN_PROMPT_WARN   = 800
_QWEN_PROMPT_CRIT   = 950
_QWEN_RESET_HOUR    = 3      # 03:00 МСК (00:00 UTC)

import json
import os
import time
import threading
import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

_lock = threading.Lock()
_logger = logging.getLogger("rate_tracker")

# Путь к файлу статистики Claude Code CLI (все сессии, не только через бот)
_CLAUDE_STATS_CACHE = os.path.expanduser("~/.claude/stats-cache.json")

# In-memory state
_state: dict[str, dict] = {}

def _load_from_db():
    try:
        from config import DB_PATH
        from db_manager import Database
        db = Database(DB_PATH)
        raw = db.get_setting("rate_tracker_state_safe")
        if raw:
            global _state
            _state = json.loads(raw)
    except Exception as e:
        _logger.warning("rate_tracker: failed to load state from DB: %s", e)

def _save_to_db():
    try:
        from config import DB_PATH
        from db_manager import Database
        db = Database(DB_PATH)
        db.set_setting("rate_tracker_state_safe", json.dumps(_state, ensure_ascii=False))
    except Exception as e:
        _logger.warning("rate_tracker: failed to save state to DB: %s", e)

_load_from_db()


# ── CLAUDE /config PTY парсинг ────────────────────────────────

def fetch_claude_usage_from_cli() -> dict:
    """
    Запускает `claude` в PTY, открывает /config → Usage вкладку,
    парсит '37% used' для сессии и недели.
    Возвращает {"session_used": N, "week_used": N, "ok": True}
    или {"ok": False, "error": "..."}.
    Блокирующий вызов, занимает ~8-10 секунд.
    """
    try:
        import pexpect, re as _re
        from config import CLAUDE_BIN

        def _clean(t):
            return _re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]|\x1b[=>]|\r|\x1b\][^\x07]*\x07', '', t)

        child = pexpect.spawn(
            f"{CLAUDE_BIN} --dangerously-skip-permissions",
            encoding="utf-8", timeout=40, echo=False,
            dimensions=(50, 200)
        )
        try:
            child.expect(r"❯", timeout=30)
            import time as _time; _time.sleep(2)
        except Exception:
            pass

        child.send("/config\r")
        try:
            child.expect("compact", timeout=10)
            import time as _time; _time.sleep(0.5)
        except Exception:
            pass

        # ↑ к табам → → Usage (один раз вправо от Config)
        child.send("\x1b[A")
        import time as _time; _time.sleep(0.5)
        child.send("\x1b[C")
        _time.sleep(5)  # ждём загрузки Usage данных

        output = ""
        for _ in range(50):
            try:
                output += child.read_nonblocking(size=8192, timeout=0.3)
            except Exception:
                break

        child.send("\x1b")
        child.close(force=True)

        text = _clean(output)
        # "37%used" или "37% used"
        s_m = _re.search(r'Current session[^%]*?(\d{1,3})\s*%\s*used', text, _re.I)
        w_m = _re.search(r'Current week[^%]*?(\d{1,3})\s*%\s*used', text, _re.I)

        if s_m and w_m:
            s_used = int(s_m.group(1))
            w_used = int(w_m.group(1))
            return {"session_used": s_used, "week_used": w_used, "ok": True}
        return {"ok": False, "error": f"Pattern not found in: {text[200:400]!r}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── CLAUDE stats-cache.json ────────────────────────────────────

def get_claude_real_usage() -> dict:
    """
    Читает ~/.claude/stats-cache.json и возвращает реальное кол-во сообщений
    Claude Code CLI за последние ~5ч (сегодня) и 7 дней.

    Возвращает: {"today": N, "week": N, "ok": True/False}
    """
    try:
        with open(_CLAUDE_STATS_CACHE, "r", encoding="utf-8") as f:
            data = json.load(f)
        daily = data.get("dailyActivity", [])
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")

        today_count = 0
        week_count = 0
        for entry in daily:
            d = entry.get("date", "")
            n = entry.get("messageCount", 0)
            if d == today_str:
                today_count = n
            if d >= week_ago:
                week_count += n
        return {"today": today_count, "week": week_count, "ok": True}
    except Exception as e:
        _logger.debug("stats-cache.json read failed: %s", e)
        return {"today": 0, "week": 0, "ok": False}


# ── ПАССИВНЫЙ ПАРСИНГ ──────────────────────────────────────────

def parse_cli_warning(agent: str, text: str):
    """
    Парсит вывод CLI на наличие предупреждений о лимитах.
    Пример: "10 messages remaining until 3:00 PM"
    """
    if "claude" not in agent: return
    
    # Regex для "X messages remaining until Y:ZZ"
    m = re.search(r'(\d+)\s+messages?\s+remaining\s+until\s+([\d:]+\s*[APM]*)', text, re.I)
    if m:
        rem = int(m.group(1))
        reset = m.group(2)
        with _lock:
            _state[agent] = {
                "cli_exact": {"remaining": rem, "reset": reset, "ts": int(time.time())}
            }
            _save_to_db()
        return True
    return False

# ── ЭВРИСТИКА ────────────────────────────────────────────────

def log_request(agent: str):
    """Логирует запрос в БД для эвристического счета."""
    try:
        from config import DB_PATH
        from db_manager import Database
        db = Database(DB_PATH)
        with db.get_connection() as conn:
            conn.execute("INSERT INTO usage_log (agent) VALUES (?)", (agent,))
    except Exception as e:
        _logger.error(f"Failed to log request: {e}")

def get_safe_estimate(agent: str) -> dict:
    """Возвращает эвристическую оценку лимитов (5ч и неделя)."""
    try:
        from config import DB_PATH
        from db_manager import Database
        db = Database(DB_PATH)
        
        now = datetime.now()
        five_h = (now - timedelta(hours=5)).strftime('%Y-%m-%d %H:%M:%S')
        one_w = (now - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
        
        with db.get_connection() as conn:
            cursor = conn.cursor()
            # 5-часовое окно
            cursor.execute("SELECT COUNT(*) FROM usage_log WHERE agent = ? AND timestamp > ?", (agent, five_h))
            count_5h = cursor.fetchone()[0]
            # Недельное окно
            cursor.execute("SELECT COUNT(*) FROM usage_log WHERE agent = ? AND timestamp > ?", (agent, one_w))
            count_week = cursor.fetchone()[0]
        
        lim_5h = _CLAUDE_LIMIT_5H
        lim_week = _CLAUDE_LIMIT_WEEK

        pct_5h = max(0, int((1 - count_5h/lim_5h) * 100))
        pct_week = max(0, int((1 - count_week/lim_week) * 100))
        
        # Берем минимальный процент как Safe Estimate
        final_pct = min(pct_5h, pct_week)
        
        return {
            "pct": final_pct,
            "5h_count": count_5h, "5h_limit": lim_5h,
            "week_count": count_week, "week_limit": lim_week
        }
    except Exception as e:
        _logger.warning("get_safe_estimate failed: %s", e)
        return {"pct": 100, "5h_count": 0, "5h_limit": _CLAUDE_LIMIT_5H, "week_count": 0, "week_limit": _CLAUDE_LIMIT_WEEK}

def get_all_status() -> str:
    """Полный статус для /limits (Markdown-safe)."""
    lines = ["📊 *Claude Pro Rate Limits*\n"]

    for agent in ["claude"]:
        with _lock:
            s = _state.get(agent, {})

        cli = s.get("cli_exact")
        manual = s.get("manual")

        if cli and time.time() - cli["ts"] < 3600:
            age = int(time.time() - cli["ts"])
            lines.append("✅ *Точный остаток (из CLI):*")
            lines.append(f"  • Осталось: *{cli['remaining']}* сообщений")
            lines.append(f"  • Сброс: {cli['reset']}")
            lines.append(f"  _{age}с назад_")
        elif manual and "session_rem" in manual:
            s_rem = manual["session_rem"]
            w_rem = manual["week_rem"]
            age_h = (time.time() - manual.get("ts", 0)) / 3600
            si = "🔴" if s_rem < 10 else ("🟡" if s_rem < 40 else "🟢")
            wi = "🔴" if w_rem < 10 else ("🟡" if w_rem < 40 else "🟢")
            lines.append("🔵 *Лимиты Claude Pro (из /config):*")
            lines.append(f"  {si} Сессия (~5ч): *{s_rem}%* осталось")
            lines.append(f"  {wi} Неделя:       *{w_rem}%* осталось")
            lines.append(f"  _Обновлено {age_h:.1f}ч назад_")
        else:
            usage = get_claude_real_usage()
            if usage["ok"]:
                lines.append("📊 *Активность Claude Code (все сессии):*")
                lines.append(f"  • Сегодня: *{usage['today']}* событий")
                lines.append(f"  • За 7 дней: *{usage['week']}* событий")
                lines.append("  _Считаются все события (user+assistant+tools) — не то же самое, что лимит Pro_")
            else:
                lines.append("❓ _stats-cache.json не найден_")
            lines.append("  Реальный % лимита → `/config` в Claude Code, затем:")
            lines.append("  `/usage 45 62` _(session% used, week% used)_")

    # OpenRouter — из headers (если есть свежие)
    with _lock:
        or_s = _state.get("openrouter", {})
    auto = or_s.get("auto", {})
    auto_ts = or_s.get("auto_ts", 0)
    if auto and time.time() - auto_ts < 120:
        lines.append("\n🌐 *OpenRouter (из заголовков):*")
        for dim, v in auto.items():
            pct = int(v["remaining"] / v["limit"] * 100)
            icon = "🟢" if pct >= 40 else ("🟡" if pct >= 10 else "🔴")
            lines.append(f"  • {icon} {dim.upper()}: {pct}% ({v['remaining']}/{v['limit']})")

    lines.append(get_gemini_status())
    lines.append(get_qwen_status())
    lines.append("\n/limit claude 85 5h — ввести вручную")
    lines.append("/limit reset claude — сбросить")
    return "\n".join(lines)


# ── РУЧНОЙ ВВОД (Claude weekly/5h с сайта) ──────────────────────────────────

def set_manual(agent: str, session_rem: int, week_rem: int) -> None:
    """Store % remaining for session (5h) and week, from /config → Usage."""
    session_rem = max(0, min(100, session_rem))
    week_rem    = max(0, min(100, week_rem))
    with _lock:
        entry = _state.setdefault(agent, {})
        entry["manual"] = {
            "session_rem": session_rem,
            "week_rem":    week_rem,
            "ts":          int(time.time()),
        }
        _save_to_db()


def reset(agent: str) -> None:
    """Clear all stored data for an agent."""
    with _lock:
        _state.pop(agent, None)
        _save_to_db()


# ── ЗАГОЛОВКИ OPENROUTER ─────────────────────────────────────────────────────

def update_from_headers(agent: str, headers) -> None:
    """Parse x-ratelimit-* headers from OpenRouter HTTP response."""
    _OR_MAP = {
        "rpm": ("x-ratelimit-remaining-requests", "x-ratelimit-limit-requests"),
        "tpm": ("x-ratelimit-remaining-tokens",   "x-ratelimit-limit-tokens"),
    }
    dims: dict = {}
    for dim, (rem_key, lim_key) in _OR_MAP.items():
        try:
            r = headers.get(rem_key) or headers.get(rem_key.lower())
            l = headers.get(lim_key) or headers.get(lim_key.lower())
            if r is not None and l is not None:
                ri, li = int(r), int(l)
                if li > 0:
                    dims[dim] = {"remaining": ri, "limit": li}
        except (ValueError, TypeError):
            pass
    if not dims:
        return
    with _lock:
        entry = _state.setdefault(agent, {})
        entry["auto"] = dims
        entry["auto_ts"] = int(time.time())
        _save_to_db()


# ── ОТОБРАЖЕНИЕ В agent_label() ──────────────────────────────────────────────

def get_display(agent: str) -> str:
    """
    Returns compact indicator for agent_label():
      '🟢 23 msg'   — exact CLI data
      '🔵 85% 5h'   — manual entry
      '🟡 ~47%'     — heuristic estimate
      '🟢 85% RPM'  — OpenRouter headers
      ''             — no data
    """
    with _lock:
        s = dict(_state.get(agent, {}))

    # 1. CLI exact (freshest, highest priority)
    cli = s.get("cli_exact")
    if cli and time.time() - cli["ts"] < 3600:
        rem = cli["remaining"]
        icon = "🔴" if rem <= 5 else ("🟡" if rem <= 15 else "🟢")
        return f"{icon} {rem} msg"

    # 2. Manual entry (both session and week)
    manual = s.get("manual")
    if manual and "session_rem" in manual:
        s_rem = manual["session_rem"]
        w_rem = manual["week_rem"]
        si = "🔴" if s_rem < 10 else ("🟡" if s_rem < 40 else "🟢")
        wi = "🔴" if w_rem < 10 else ("🟡" if w_rem < 40 else "🟢")
        return f"{si} {s_rem}%·5ч  {wi} {w_rem}%·нед"

    # 3. OpenRouter headers (fresh < 2min)
    auto = s.get("auto", {})
    auto_ts = s.get("auto_ts", 0)
    if auto and time.time() - auto_ts < 120:
        bottleneck = min(
            (int(v["remaining"] / v["limit"] * 100), k)
            for k, v in auto.items()
        )
        pct, dim = bottleneck
        icon = "🔴" if pct < 10 else ("🟡" if pct < 40 else "🟢")
        return f"{icon} {pct}% {dim.upper()}"

    # 4. Claude: real usage from ~/.claude/stats-cache.json
    #    messageCount = all events (user+assistant+tools), NOT comparable to Pro limits.
    #    Shown as raw info only — real % comes from /config → /usage command.
    if agent == "claude":
        usage = get_claude_real_usage()
        if usage["ok"]:
            return f"📊 {usage['today']}сег  {usage['week']}нед"

    # 5. Gemini RPD tracking — always show (even 0 prompts) so agent_label()
    #    never falls back to the misleading ctx_pct() indicator
    if agent == "gemini":
        count = get_gemini_prompts_today()
        icon = "🔴" if count >= _GEMINI_PROMPT_CRIT else ("🟡" if count >= _GEMINI_PROMPT_WARN else "🟢")
        hrs = _gemini_hours_until_reset()
        return f"{icon} {count}💬 {hrs}ч↺"

    # 6. Qwen RPD tracking — always show
    if agent == "qwen":
        count = get_qwen_prompts_today()
        icon = "🔴" if count >= _QWEN_PROMPT_CRIT else ("🟡" if count >= _QWEN_PROMPT_WARN else "🟢")
        hrs = _qwen_hours_until_reset()
        return f"{icon} {count}💬 {hrs}ч↺"

    return ""


# ── GEMINI RPD/RPM TRACKING ───────────────────────────────────

def _gemini_day_start() -> float:
    """Unix timestamp of the last 10:00 MSK reset."""
    now_utc = time.time()
    now_msk_secs = now_utc + _MSK_OFFSET
    # Seconds since midnight UTC (used to find local MSK midnight)
    msk_midnight_utc = now_utc - (now_msk_secs % 86400)
    reset_utc = msk_midnight_utc + _GEMINI_RESET_HOUR * 3600
    if now_utc < reset_utc:
        # Still before 10:00 MSK today — yesterday's reset is the current window
        reset_utc -= 86400
    return reset_utc


def _gemini_hours_until_reset() -> str:
    """Returns hours remaining until next 10:00 MSK reset, e.g. '3.5'."""
    next_reset = _gemini_day_start() + 86400
    secs = next_reset - time.time()
    if secs <= 0:
        return "0"
    h = secs / 3600
    return f"{h:.0f}" if h >= 1 else f"{secs/60:.0f}м"


def get_gemini_prompts_today() -> int:
    """Count Gemini prompts sent since the last 10:00 MSK reset."""
    try:
        from config import DB_PATH
        from db_manager import Database
        db = Database(DB_PATH)
        day_start = _gemini_day_start()
        dt = datetime.fromtimestamp(day_start).strftime('%Y-%m-%d %H:%M:%S')
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM usage_log WHERE agent = 'gemini' AND timestamp > ?",
                (dt,)
            )
            return cursor.fetchone()[0]
    except Exception:
        return 0


def get_gemini_rpm() -> int:
    """Count Gemini prompts in the last 60 seconds."""
    try:
        from config import DB_PATH
        from db_manager import Database
        db = Database(DB_PATH)
        dt = datetime.fromtimestamp(time.time() - 60).strftime('%Y-%m-%d %H:%M:%S')
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM usage_log WHERE agent = 'gemini' AND timestamp > ?",
                (dt,)
            )
            return cursor.fetchone()[0]
    except Exception:
        return 0


def get_gemini_status() -> str:
    """Full Gemini quota status for /limit command."""
    count = get_gemini_prompts_today()
    rpm = get_gemini_rpm()
    est_api = count * _GEMINI_AVG_MUL
    remaining_prompts = max(0, _GEMINI_PROMPT_CRIT - count)
    hrs = _gemini_hours_until_reset()

    icon = "🔴" if count >= _GEMINI_PROMPT_CRIT else ("🟡" if count >= _GEMINI_PROMPT_WARN else "🟢")
    lines = [
        f"\n🟢 *Gemini Code Assist — дневной лимит*",
        f"  {icon} Промптов сегодня: *{count}* / ~{_GEMINI_PROMPT_CRIT}",
        f"  Примерно API-запросов: ~{est_api} / {_GEMINI_RPD}",
        f"  RPM сейчас: {rpm} / {_GEMINI_RPM_LIMIT}",
        f"  Осталось промптов: ~{remaining_prompts}",
        f"  Сброс через: {hrs}",
        f"  _(сброс в 10:00 МСК каждый день)_",
    ]
    return "\n".join(lines)


# ── QWEN RPD TRACKING ─────────────────────────────────────────

def _qwen_day_start() -> float:
    """Unix timestamp of the last 03:00 MSK reset (= 00:00 UTC)."""
    now_utc = time.time()
    now_msk_secs = now_utc + _MSK_OFFSET
    msk_midnight_utc = now_utc - (now_msk_secs % 86400)
    reset_utc = msk_midnight_utc + _QWEN_RESET_HOUR * 3600
    if now_utc < reset_utc:
        reset_utc -= 86400
    return reset_utc


def _qwen_hours_until_reset() -> str:
    next_reset = _qwen_day_start() + 86400
    secs = next_reset - time.time()
    if secs <= 0:
        return "0"
    h = secs / 3600
    return f"{h:.0f}" if h >= 1 else f"{secs/60:.0f}м"


def get_qwen_prompts_today() -> int:
    try:
        from config import DB_PATH
        from db_manager import Database
        db = Database(DB_PATH)
        day_start = _qwen_day_start()
        dt = datetime.fromtimestamp(day_start).strftime('%Y-%m-%d %H:%M:%S')
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM usage_log WHERE agent = 'qwen' AND timestamp > ?",
                (dt,)
            )
            return cursor.fetchone()[0]
    except Exception:
        return 0


def fetch_qwen_context_from_cli() -> dict:
    """
    Запускает `qwen --yolo` в PTY, выполняет /context,
    парсит использование контекстного окна.
    Возвращает {"used_k": float, "total_k": float, "pct": float, "items": [...], "ok": True}
    или {"ok": False, "error": "..."}.
    """
    try:
        import pexpect as _px, re as _re

        def _clean(t):
            return _re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]|\x1b[=>]|\r|\x1b\][^\x07]*\x07', '', t)

        child = _px.spawn(
            "qwen --yolo",
            encoding="utf-8", timeout=30, echo=False, dimensions=(50, 200)
        )
        try:
            child.expect("YOLO", timeout=20)
            import time as _t; _t.sleep(2)
        except Exception:
            pass

        child.send("\x1b")
        import time as _t; _t.sleep(0.3)
        child.send("/context")
        _t.sleep(0.3)
        child.send("\r\n")   # отдельно, чтобы не смешать с автодополнением
        _t.sleep(4)

        out = ""
        for _ in range(30):
            try: out += child.read_nonblocking(size=8192, timeout=0.3)
            except: break
        child.close(force=True)

        text = _clean(out)
        # "Контекстное окно: 1000.0k токенов"
        total_m = _re.search(r'Контекстное окно[:\s]+([0-9.]+)k', text, _re.I)
        # "Системная подсказка 5.6k токенов (0.6%)" or "Файлы памяти 169 токенов (0.0%)"
        items_raw = _re.findall(r'([^\n│█]+?)\s+([0-9.]+)(k?)\s+токенов?\s+\(([0-9.]+)%\)', text)

        if not total_m:
            return {"ok": False, "error": "pattern not found"}

        total_k = float(total_m.group(1))
        parsed_items = []
        used_k = 0.0
        for name, num, k_suffix, pct_s in items_raw:
            val = float(num)
            val_k = val if k_suffix == "k" else val / 1000.0
            used_k += val_k
            parsed_items.append({"name": name.strip(), "tokens_k": round(val_k, 3), "pct": float(pct_s)})

        pct = round(used_k / total_k * 100, 1) if total_k else 0.0
        return {"used_k": round(used_k, 2), "total_k": total_k, "pct": pct, "items": parsed_items, "ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_qwen_status() -> str:
    count = get_qwen_prompts_today()
    remaining = max(0, _QWEN_PROMPT_CRIT - count)
    hrs = _qwen_hours_until_reset()
    icon = "🔴" if count >= _QWEN_PROMPT_CRIT else ("🟡" if count >= _QWEN_PROMPT_WARN else "🟢")
    lines = [
        f"\n🟡 *Qwen — дневной лимит*",
        f"  {icon} Промптов сегодня: *{count}* / {_QWEN_PROMPT_CRIT}",
        f"  Осталось: ~{remaining}",
        f"  Сброс через: {hrs}",
        f"  _(сброс в 03:00 МСК / 00:00 UTC)_",
    ]
    return "\n".join(lines)


def get_agent_stats(agent: str) -> str:
    """
    Per-agent stats string for the /stats command.
    Returns a human-readable Markdown-safe status block for the given agent.
    """
    if agent == "claude":
        return get_all_status()   # full Claude rate-limit + OR + Gemini + Qwen summary

    if agent == "gemini":
        return get_gemini_status()

    if agent == "qwen":
        return get_qwen_status()

    if agent == "openrouter":
        with _lock:
            or_s = dict(_state.get("openrouter", {}))
        auto = or_s.get("auto", {})
        auto_ts = or_s.get("auto_ts", 0)
        if auto and time.time() - auto_ts < 300:
            lines = ["🌐 *OpenRouter (из заголовков):*"]
            for dim, v in auto.items():
                pct = int(v["remaining"] / v["limit"] * 100) if v["limit"] else 0
                icon = "🟢" if pct >= 40 else ("🟡" if pct >= 10 else "🔴")
                lines.append(f"  • {icon} {dim.upper()}: {pct}% ({v['remaining']}/{v['limit']})")
            age = int(time.time() - auto_ts)
            lines.append(f"  _{age}с назад_")
            return "\n".join(lines)
        return "🌐 *OpenRouter:* лимиты неизвестны\n_(обновляются после первого ответа)_"

    if agent == "ollama":
        try:
            import requests
            resp = requests.get("http://localhost:11434/api/tags", timeout=3)
            models = [m["name"] for m in resp.json().get("models", [])]
            if models:
                return "🦙 *Ollama — установленные модели:*\n" + "\n".join(f"  • {m}" for m in models)
            return "🦙 *Ollama:* нет установленных моделей"
        except Exception:
            return "🦙 *Ollama:* недоступна (`ollama serve` не запущен?)"

    return f"❓ Статистика для *{agent}* недоступна"
