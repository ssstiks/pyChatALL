#!/usr/bin/env python3
"""
Export / backup utility for pyChatALL bot.

Modes:
  state     — bot state only (~1 MB, sendable via Telegram)
  metadata  — state + project .tg_team folders (logs, plans, session IDs, no source code)
  rsync     — ready-to-run rsync commands for a full VPS-to-VPS copy

Archive layout (state and metadata modes):
  ARCHIVE.tar.gz
  ├── state/              → copy to STATE_DIR (~/.local/share/pyChatALL/)
  │   ├── pychatall.db
  │   ├── shared_context.json
  │   ├── global_memory.json
  │   ├── memory.md
  │   ├── openrouter_key.txt
  │   ├── work_dir.txt    (tells bot where workspace is)
  │   ├── *_model.txt
  │   ├── *_session.txt
  │   └── ...
  ├── workspace/          → copy to WORK_DIR  (metadata mode only)
  │   ├── projects/
  │   │   ├── <project1>/.tg_team/   (plans, logs — no source code)
  │   │   └── <project2>/.tg_team/
  │   └── .tg_team/       (global team state)
  └── RESTORE.md          (this guide)
"""

import io
import os
import tarfile
import time

from config import STATE_DIR, WORK_DIR


# Files in STATE_DIR to include.  Excludes large caches and temporary files.
_STATE_INCLUDE = {
    "active_agent.txt",
    "claude_session.txt",   "claude_ctx_chars.txt",    "claude_model.txt",
    "gemini_session.txt",   "gemini_ctx_chars.txt",    "gemini_model.txt",
    "qwen_session.txt",     "qwen_ctx_chars.txt",       "qwen_model.txt",
    "ollama_session.txt",   "ollama_ctx_chars.txt",     "ollama_model.txt",
    "openrouter_model.txt", "openrouter_key.txt",
    "shared_context.json",  "global_memory.json",       "memory.md",
    "discuss_agents.json",  "setup_done.txt",           "work_dir.txt",
    "pychatall.db",
}

BOT_DIR = os.path.dirname(os.path.abspath(__file__))


# ── RESTORE GUIDE ────────────────────────────────────────────────────────────

def _tree_state() -> str:
    """List actually present state files for the tree."""
    lines = []
    for fname in sorted(_STATE_INCLUDE):
        fpath = os.path.join(STATE_DIR, fname)
        if os.path.isfile(fpath):
            size = os.path.getsize(fpath)
            size_s = f"{size // 1024} КБ" if size >= 1024 else f"{size} Б"
            lines.append(f"│   ├── {fname:<36} ({size_s})")
    return "\n".join(lines)


def _tree_workspace() -> str:
    """List project .tg_team dirs for the tree."""
    lines = []
    projects_dir = os.path.join(WORK_DIR, "projects")
    if os.path.isdir(projects_dir):
        for proj in sorted(os.listdir(projects_dir)):
            pt = os.path.join(projects_dir, proj, ".tg_team")
            mark = "✓" if os.path.isdir(pt) else "·"
            lines.append(f"│   │   ├── {proj}/  ({mark} .tg_team)")
    team_dir = os.path.join(WORK_DIR, ".tg_team")
    if os.path.isdir(team_dir):
        lines.append("│   └── .tg_team/")
    return "\n".join(lines)


def _restore_guide(mode: str, archive_name: str) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    bot_dir = BOT_DIR
    state_dir = STATE_DIR
    work_dir = WORK_DIR

    tree_state = _tree_state()

    header = f"""\
# pyChatALL — Архив состояния бота
Дата:        {ts}
Режим:       {mode}
Бот:         {bot_dir}
Workspace:   {work_dir}
Состояние:   {state_dir}

## Что внутри архива

{archive_name}"""

    if mode == "state":
        tree = f"""\
├── state/
{tree_state}
│
└── RESTORE.md"""
    else:  # metadata
        tree_ws = _tree_workspace()
        tree = f"""\
├── state/
{tree_state}
│
├── workspace/
│   ├── projects/
{tree_ws}
│
└── RESTORE.md"""

    restore_state = f"""\
## Куда распаковать

### 1. Состояние бота  →  {state_dir}/

```bash
mkdir -p "{state_dir}"
tar xzf "{archive_name}" --strip-components=1 -C "{state_dir}" state/
```

> После этого файл `work_dir.txt` укажет боту где искать workspace.
> Если workspace на новой машине в другом месте — исправь его:
> ```bash
> echo "/новый/путь/к/workspace" > "{state_dir}/work_dir.txt"
> ```"""

    if mode == "metadata":
        restore_ws = f"""

### 2. Метаданные проектов  →  {work_dir}/

```bash
mkdir -p "{work_dir}"
tar xzf "{archive_name}" --strip-components=1 -C "{work_dir}" workspace/
```

> Исходный код проектов НЕ включён (слишком большой).
> Для полного копирования используй rsync (см. /export → rsync команды)."""
    else:
        restore_ws = ""

    restore_bot = f"""

### {3 if mode == "metadata" else 2}. Бот (исходники)

```bash
# Скопируй папку бота на новую машину:
scp -r "{bot_dir}/" user@vps:/opt/pychatall/

# Или через git, если проект под контролем версий.
```"""

    run = f"""

### Запуск

```bash
cd /opt/pychatall
python3 tg_agent.py
```

Или через nohup:
```bash
nohup python3 tg_agent.py >> /tmp/tg_agent.log 2>&1 &
```"""

    return header + "\n" + tree + "\n\n" + restore_state + restore_ws + restore_bot + run + "\n"


# ── EXPORT FUNCTIONS ─────────────────────────────────────────────────────────

def create_state_export() -> tuple[str, int]:
    """Bundle bot state files only. Returns (path, size_bytes)."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    name = f"pychatall_state_{ts}.tar.gz"
    out  = f"/tmp/{name}"

    with tarfile.open(out, "w:gz") as tar:
        for fname in _STATE_INCLUDE:
            fpath = os.path.join(STATE_DIR, fname)
            if os.path.isfile(fpath):
                tar.add(fpath, arcname=f"state/{fname}")
        _add_text(tar, "RESTORE.md", _restore_guide("state", name))

    return out, os.path.getsize(out)


def create_metadata_export() -> tuple[str, int]:
    """Bundle state + project .tg_team folders (no source code). Returns (path, size_bytes)."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    name = f"pychatall_meta_{ts}.tar.gz"
    out  = f"/tmp/{name}"

    with tarfile.open(out, "w:gz") as tar:
        # State
        for fname in _STATE_INCLUDE:
            fpath = os.path.join(STATE_DIR, fname)
            if os.path.isfile(fpath):
                tar.add(fpath, arcname=f"state/{fname}")

        # Project .tg_team only
        projects_dir = os.path.join(WORK_DIR, "projects")
        if os.path.isdir(projects_dir):
            for proj in os.listdir(projects_dir):
                proj_team = os.path.join(projects_dir, proj, ".tg_team")
                if os.path.isdir(proj_team):
                    tar.add(proj_team, arcname=f"workspace/projects/{proj}/.tg_team")

        # Global .tg_team
        team_dir = os.path.join(WORK_DIR, ".tg_team")
        if os.path.isdir(team_dir):
            tar.add(team_dir, arcname="workspace/.tg_team")

        _add_text(tar, "RESTORE.md", _restore_guide("metadata", name))

    return out, os.path.getsize(out)


def rsync_commands() -> str:
    """Return ready-to-run rsync commands for a full VPS copy."""
    return (
        "📋 *Полное копирование на VPS через rsync*\n\n"
        "Замени `USER@VPS` на адрес сервера.\n\n"
        "```\n"
        "# 1. Состояние бота\n"
        f"rsync -avz --progress \\\n"
        f"  \"{STATE_DIR}/\" \\\n"
        f"  USER@VPS:\"~/.local/share/pyChatALL/\" \\\n"
        f"  --exclude='downloads/' \\\n"
        f"  --exclude='openrouter_models.json' \\\n"
        f"  --exclude='archive/'\n"
        "\n"
        "# 2. Проекты (весь исходный код)\n"
        f"rsync -avz --progress \\\n"
        f"  \"{os.path.join(WORK_DIR, 'projects')}/\" \\\n"
        f"  USER@VPS:\"/opt/workspace/projects/\"\n"
        "\n"
        "# 3. Исходники бота\n"
        f"rsync -avz --progress \\\n"
        f"  \"{BOT_DIR}/\" \\\n"
        f"  USER@VPS:\"/opt/pychatall/\"\n"
        "```\n\n"
        "После копирования укажи пути на VPS:\n"
        "```\n"
        "echo \"/opt/workspace\" > ~/.local/share/pyChatALL/work_dir.txt\n"
        "```"
    )


def settings_info() -> str:
    """Return current path configuration as a formatted string."""
    import shutil

    def _du(path: str) -> str:
        try:
            total = sum(
                os.path.getsize(os.path.join(dp, f))
                for dp, _, files in os.walk(path)
                for f in files
            )
            return f"{total // 1024} КБ" if total < 1_048_576 else f"{total // 1_048_576} МБ"
        except Exception:
            return "?"

    def _free(path: str) -> str:
        try:
            free = shutil.disk_usage(path).free
            return f"{free // (1024**3)} ГБ свободно"
        except Exception:
            return ""

    projects_dir = os.path.join(WORK_DIR, "projects")
    n_proj = len(os.listdir(projects_dir)) if os.path.isdir(projects_dir) else 0
    state_size = _du(STATE_DIR)
    free_bot   = _free(BOT_DIR)
    free_work  = _free(WORK_DIR)

    return (
        f"⚙️ *Настройки путей*\n\n"
        f"🤖 *Бот (исходники)*\n"
        f"`{BOT_DIR}`\n"
        f"_{free_bot}_\n\n"
        f"📂 *Workspace (проекты)*\n"
        f"`{WORK_DIR}`\n"
        f"_{n_proj} проект(ов) · {free_work}_\n\n"
        f"💾 *Состояние бота*\n"
        f"`{STATE_DIR}`\n"
        f"_{state_size}_"
    )


# ── HELPERS ──────────────────────────────────────────────────────────────────

def _add_text(tar: tarfile.TarFile, name: str, content: str) -> None:
    data = content.encode()
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))
