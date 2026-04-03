# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Project Overview

**pyChatALL** is a personal Telegram bot that orchestrates multiple AI agents: Claude CLI, Gemini CLI, Qwen CLI, OpenRouter API, and Ollama. It handles persistent sessions, shared context, rate-limit tracking, and a Team Mode pipeline (Planner → Coder → Debugger).

## Commands

```bash
./start.sh              # start/restart (auto-detects proxy, loads token from ~/.local/share/pyChatALL/token.txt)
./start.sh stop         # stop
./start.sh status       # check process status
./start.sh logs         # tail -f /tmp/tg_agent.log
python3 monitor.py      # real-time console dashboard
pytest                  # run tests
```

## Architecture

### Request lifecycle
1. `tg_agent.py` polls Telegram (main thread)
2. Messages enqueued → `asyncio.Queue` worker processes serially
3. `router.py` classifies prompt → picks Haiku vs Sonnet for Claude
4. `context.py` injects shared context (last 6 messages) into prompt
5. `agents.py` dispatches to CLI subprocess or HTTP API
6. Response streamed back via `_StreamEditor` (throttled edits)
7. `memory_manager.py` updates global memory JSON in background

### Key modules

| File | Role |
|------|------|
| `tg_agent.py` | Entry point, Telegram polling, command handlers, queue |
| `agents.py` | CLI subprocess wrappers + HTTP clients; agent workspace CWD |
| `config.py` | All constants, binary discovery, context limits, timeouts |
| `context.py` | Session IDs, shared context, memory, model selection |
| `rate_tracker.py` | Quota tracking: Gemini 1500 RPD (reset 10:00 MSK), Qwen 1000 RPD (reset 03:00 MSK) |
| `team_mode.py` | Planner→Coder→Debugger orchestration |
| `ui.py` | Telegram message sending, keyboards, file upload |
| `db_manager.py` | SQLite: sessions, models, memory, settings, usage_log |
| `router.py` | Complexity classifier (Haiku vs Sonnet) |
| `memory_manager.py` | Shadow Librarian — extracts facts into global_memory.json |
| `logger.py` | Structured logging |
| `voice.py` | Whisper voice transcription (OGG → text) |
| `monitor.py` | Real-time console dashboard |

### State directory: `~/.local/share/pyChatALL/`

- `pychatall.db` — SQLite (sessions, models, memory, settings, usage_log)
- `token.txt` — bot token (never commit, gitignored)
- `shared_context.json` — last 6 messages injected into every prompt
- `global_memory.json` — long-term user/project facts
- `workspaces/claude/`, `workspaces/gemini/`, `workspaces/qwen/` — isolated CWD per agent
- `archive/` — rotated old sessions
- `downloads/` — files from Telegram

### Agent isolation (important)

Each agent subprocess runs in its own empty workspace directory, not in the bot's source tree. This prevents Gemini/Qwen from wasting API quota scanning the bot's files.

- No active project → `workspaces/{agent}/` (empty sandbox)
- Team Mode project active → `WORK_DIR/projects/{slug}/` (project files only)

Set in `agents.py`: `_get_agent_workspace(agent_key)`.

### Subprocess handling

`_run_subprocess()` in `agents.py`:
- Always reads stdout in a background thread (never blocks with `proc.stdout.read()`)
- `stdin=subprocess.DEVNULL` — prevents agents from waiting on stdin
- `start_new_session=True` — SIGKILL kills the whole process group
- Gemini env: `HTTP_PROXY`/`HTTPS_PROXY` stripped (Google APIs don't need proxy)

### CLI agent commands

```python
# Claude: --print --dangerously-skip-permissions --output-format stream-json --verbose --model <m> <prompt>
# Gemini: --yolo --model <m> --prompt <prompt>
# Qwen:   --yolo --model <m> --prompt <prompt>
```

`--output-format` is Claude-only. Do NOT add it to Gemini or Qwen commands.

### Concurrency model
- Main thread: blocking Telegram polling
- Worker: single asyncio event loop, processes queue serially
- Background threads: agent execution, Team Mode pipeline, memory updates
- `threading.Lock` guards file state mutations
- `_cancel_event` (asyncio.Event) → SIGKILL on active subprocess

## Configuration

`config.py` is the single source of truth. Key values:
- `BOT_TOKEN` — from `TG_BOT_TOKEN` env var
- `ALLOWED_CHAT` — from `TG_ALLOWED_CHAT` env var (your Telegram user ID)
- `CTX_LIMITS` — `(warn_chars, archive_chars)` per agent
- `_AGENT_TIMEOUT` — subprocess timeout per agent (seconds)
- `WORKSPACES_DIR` — `~/.local/share/pyChatALL/workspaces/`
- Binary paths auto-detected: `shutil.which()` → `~/.local/bin` → nvm

## Known issues / gotchas

- **Gemini proxy**: `HTTP_PROXY` from `start.sh` must be stripped for Gemini subprocess or it times out in ~12s. Already handled in `agents.py`.
- **--output-format**: Claude-only flag. Adding it to Gemini causes it to hang indefinitely.
- **stdout deadlock**: Never call `proc.stdout.read()` without a timeout — use a thread instead. Already fixed in `_run_subprocess`.
- **Shared context size**: trim `shared_context.json` if it grows large — only last 6 messages are injected anyway.
- **usage_log table**: must exist in SQLite for rate tracking. Created by `db.initialize()`.
