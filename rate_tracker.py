#!/usr/bin/env python3
"""
rate_tracker.py — 100% Safe Rate Limit Tracker for Claude.

Два источника данных:
1. Пассивный парсинг CLI (Source of Truth): "10 messages remaining until 3:00 PM"
2. Локальная эвристика (Fallback): Подсчет запросов в БД за 5-часовое окно (лимит ~45).
"""

import json
import time
import threading
import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

_lock = threading.Lock()
_logger = logging.getLogger("rate_tracker")

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
    except: pass

def _save_to_db():
    try:
        from config import DB_PATH
        from db_manager import Database
        db = Database(DB_PATH)
        db.set_setting("rate_tracker_state_safe", json.dumps(_state, ensure_ascii=False))
    except: pass

_load_from_db()

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
        
        lim_5h = 45
        lim_week = 400
        
        pct_5h = max(0, int((1 - count_5h/lim_5h) * 100))
        pct_week = max(0, int((1 - count_week/lim_week) * 100))
        
        # Берем минимальный процент как Safe Estimate
        final_pct = min(pct_5h, pct_week)
        
        return {
            "pct": final_pct,
            "5h_count": count_5h, "5h_limit": lim_5h,
            "week_count": count_week, "week_limit": lim_week
        }
    except:
        return {"pct": 100, "5h_count": 0, "5h_limit": 45, "week_count": 0, "week_limit": 400}

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
        elif manual:
            pct = manual["pct"]
            lbl = manual["label"]
            age_h = (time.time() - manual.get("ts", 0)) / 3600
            icon = "🟢" if pct >= 40 else ("🟡" if pct >= 10 else "🔴")
            lines.append("🔵 *Ручной ввод:*")
            lines.append(f"  • {icon} {pct}% ({lbl})")
            lines.append(f"  _Введено {age_h:.1f}ч назад_")
        else:
            est = get_safe_estimate(agent)
            icon = "🟢" if est["pct"] >= 40 else ("🟡" if est["pct"] >= 10 else "🔴")
            lines.append("⚠️ *Безопасная оценка (эвристика):*")
            lines.append(f"  • 5ч: {est['5h_count']}/{est['5h_limit']}")
            lines.append(f"  • Неделя: {est['week_count']}/{est['week_limit']}")
            lines.append(f"  • {icon} ~{est['pct']}% (минимум из двух)")

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

    lines.append("\n/limit claude 85 5h — ввести вручную")
    lines.append("/limit reset claude — сбросить")
    return "\n".join(lines)


# ── РУЧНОЙ ВВОД (Claude weekly/5h с сайта) ──────────────────────────────────

def set_manual(agent: str, pct: int, label: str = "manual") -> None:
    """Store manually entered % remaining (from claude.ai/usage)."""
    pct = max(0, min(100, pct))
    with _lock:
        entry = _state.setdefault(agent, {})
        entry["manual"] = {"pct": pct, "label": label, "ts": int(time.time())}
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

    # 2. Manual entry
    manual = s.get("manual")
    if manual:
        pct = manual["pct"]
        lbl = manual["label"]
        icon = "🔴" if pct < 10 else ("🟡" if pct < 40 else "🔵")
        return f"{icon} {pct}% {lbl}"

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

    # 4. Heuristic estimate (Claude only)
    if agent == "claude":
        est = get_safe_estimate(agent)
        pct = est["pct"]
        if est["5h_count"] > 0 or est["week_count"] > 0:
            icon = "🔴" if pct < 10 else ("🟡" if pct < 40 else "🟢")
            return f"{icon} ~{pct}%"

    return ""
