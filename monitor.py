#!/usr/bin/env python3
"""
Мониторинг tg_agent — живой вывод ошибок и статуса агентов.
Запуск: python3 tools/monitor.py
"""

import os
import re
import time
import subprocess
from collections import defaultdict

LOG_FILE  = "/tmp/tg_agent.log"
ERR_FILE  = "/tmp/tg_agent_errors.log"
PID_FILE  = "/tmp/tg_agent.pid"
STATE_DIR = "/tmp/tg_agent"

# ANSI цвета
R  = "\033[91m"   # red
Y  = "\033[93m"   # yellow
G  = "\033[92m"   # green
B  = "\033[94m"   # blue
M  = "\033[95m"   # magenta
C  = "\033[96m"   # cyan
DIM = "\033[2m"
BOLD = "\033[1m"
RST = "\033[0m"

# Паттерны для раскраски
PATTERNS = [
    (re.compile(r'(ERROR|CRITICAL|Exception|Traceback|error|crash)', re.I), R),
    (re.compile(r'(WARN|WARNING|timeout|Timeout|timed_out|fail)', re.I),    Y),
    (re.compile(r'(→ Gemini|← Gemini|gemini)', re.I),                       M),
    (re.compile(r'(→ Claude|← Claude|claude)', re.I),                       B),
    (re.compile(r'(→ Qwen|← Qwen|qwen)', re.I),                             C),
    (re.compile(r'(INFO)',),                                                  G),
    (re.compile(r'(DEBUG)',),                                                 DIM),
]

# Статистика за сессию
stats = defaultdict(lambda: {"ok": 0, "err": 0, "timeout": 0})


def colorize(line: str) -> str:
    for pat, color in PATTERNS:
        if pat.search(line):
            return color + line + RST
    return DIM + line + RST


def is_agent_alive() -> tuple[bool, int]:
    try:
        pid = int(open(PID_FILE).read().strip())
        os.kill(pid, 0)
        return True, pid
    except Exception:
        # fallback: поищем по имени
        r = subprocess.run(["pgrep", "-f", "tg_agent.py"], capture_output=True, text=True)
        if r.stdout.strip():
            pid = int(r.stdout.strip().split()[0])
            return True, pid
        return False, 0


def get_active_agent() -> str:
    try:
        return open(f"{STATE_DIR}/active_agent.txt").read().strip()
    except Exception:
        return "?"


def get_session(agent: str) -> str:
    f = f"{STATE_DIR}/{agent}_session.txt"
    try:
        v = open(f).read().strip()
        return v[:20] if v else "нет"
    except Exception:
        return "нет"


def print_status():
    alive, pid = is_agent_alive()
    active = get_active_agent()
    status = f"{G}●  ALIVE  pid={pid}{RST}" if alive else f"{R}✖  DEAD{RST}"
    print(f"\r{BOLD}[tg_agent]{RST} {status}  |  активный: {M}{active}{RST}  "
          f"|  Gemini: {stats['gemini']['ok']}ok {stats['gemini']['err']}err {stats['gemini']['timeout']}to  "
          f"|  Claude: {stats['claude']['ok']}ok {stats['claude']['err']}err", end="", flush=True)


def update_stats(line: str):
    for agent in ("gemini", "claude", "qwen"):
        if f"← {agent}" in line.lower():
            if any(x in line for x in ("ERROR", "error", "⚠️", "fail")):
                stats[agent]["err"] += 1
            else:
                stats[agent]["ok"] += 1
        elif f"timed_out=True" in line and agent in line.lower():
            stats[agent]["timeout"] += 1
        elif "timeout" in line.lower() and agent in line.lower():
            stats[agent]["timeout"] += 1


def tail_log(path: str):
    """Открывает файл и следит за новыми строками (как tail -f)."""
    try:
        f = open(path, "r", errors="replace")
        f.seek(0, 2)  # прыгаем в конец
    except FileNotFoundError:
        return None
    return f


def main():
    print(f"{BOLD}{'='*70}{RST}")
    print(f"{BOLD}  tg_agent monitor  —  {LOG_FILE}{RST}")
    print(f"{BOLD}{'='*70}{RST}")
    print(f"{DIM}Ctrl+C для выхода{RST}\n")

    log_f = tail_log(LOG_FILE)
    err_f = tail_log(ERR_FILE)

    last_status = 0
    last_alive  = True

    try:
        while True:
            now = time.time()

            # Читаем новые строки из основного лога
            if log_f:
                for line in log_f:
                    line = line.rstrip()
                    if not line:
                        continue
                    update_stats(line)
                    # Пишем строку (перебиваем строку статуса)
                    print(f"\r{' '*120}\r{colorize(line)}")
                    last_status = 0  # сбросим таймер статуса чтобы сразу обновить

            # Читаем ошибки из error-лога
            if err_f:
                for line in err_f:
                    line = line.rstrip()
                    if not line:
                        continue
                    print(f"\r{' '*120}\r{R}[ERR] {line}{RST}")

            # Статус раз в 3 секунды
            if now - last_status >= 3:
                alive, _ = is_agent_alive()
                if not alive and last_alive:
                    print(f"\n{R}{BOLD}!!! tg_agent УПАЛ !!!{RST}")
                last_alive = alive
                print_status()
                last_status = now

            # Если лог-файл пересоздан (ротация)
            try:
                if log_f and os.stat(LOG_FILE).st_ino != os.fstat(log_f.fileno()).st_ino:
                    log_f.close()
                    log_f = tail_log(LOG_FILE)
            except Exception:
                pass

            time.sleep(0.2)

    except KeyboardInterrupt:
        print(f"\n\n{Y}Мониторинг завершён.{RST}")
        print(f"\n{BOLD}Итого за сессию:{RST}")
        for agent, s in stats.items():
            print(f"  {agent:10s}  ok={s['ok']}  err={s['err']}  timeout={s['timeout']}")


if __name__ == "__main__":
    main()
