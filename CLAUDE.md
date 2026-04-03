# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**pyChatALL** is a multi-agent AI orchestrator running as a personal Telegram bot. It routes requests to Claude, Gemini, Qwen, OpenRouter, and Ollama, with persistent sessions, shared context, rate-limit tracking, and an orchestrated Team Mode (Planner → Coder → Debugger pipeline).

## Commands

### Run the bot
```bash
./start.sh              # start/restart (handles PID lock, proxy detection)
./start.sh stop         # stop
./start.sh status       # check process status
./start.sh logs         # tail -f /tmp/tg_agent.log
python3 monitor.py      # real-time console dashboard
```

### Direct invocation
```bash
export TG_BOT_TOKEN=<token>
python3 tg_agent.py
```

### Dependencies
```bash
pip install -r requirements.txt
```

### Tests
```bash
pytest
pytest tests/test_router.py          # single test file
pytest -k "test_classify"            # single test by name
pytest --asyncio-mode=auto           # async tests
```

### Optional REST API
```bash
uvicorn main_api:app --reload --port 8000
```

## Architecture

### Request lifecycle
1. `tg_agent.py` polls Telegram (main thread)
2. Messages enqueued via `_enqueue_request()` → async worker processes serially
3. `router.py` classifies prompt → picks Haiku vs Sonnet for Claude
4. `context.py` injects shared context (last 6 messages) into prompt
5. `agents.py` dispatches to CLI subprocess (claude/gemini/qwen) or HTTP (openrouter/ollama)
6. Response streamed back via `_StreamEditor` (throttled to 1.5s min edit interval)
7. `memory_manager.py` background task updates global memory JSON

### Key modules

| File | Role |
|------|------|
| `tg_agent.py` | Entry point, Telegram polling, command handlers, concurrency |
| `async_core.py` | Event loop singleton + thread-safe bridge (polling thread → asyncio loop) |
| `agents.py` | Subprocess wrappers for CLI agents + HTTP clients for API agents |
| `config.py` | All constants, binary discovery, context limits, timeouts |
| `context.py` | Session IDs, shared context, global memory, model selection state |
| `rate_tracker.py` | Per-agent quota tracking (passive CLI parse + heuristic counting) |
| `team_mode.py` | Planner→Coder→Debugger orchestration pipeline |
| `ui.py` | Telegram message sending, keyboards, file upload/download |
| `db_manager.py` | SQLite persistence (sessions, models, memory, settings) |
| `router.py` | Complexity classifier (Haiku vs Sonnet selection) |
| `memory_manager.py` | Shadow Librarian — extracts facts from conversations into JSON |
| `logger.py` | Structured logging setup, thread excepthook, log helpers |
| `voice.py` | Whisper-based voice message transcription (OGG → text) |
| `translator.py` | Optional text translation helper |
| `export_manager.py` | State/metadata backup and rsync helpers |
| `api/` | FastAPI REST layer (routes, auth, pipeline) mirroring the bot's functionality |

### State persistence

All runtime state lives in `~/.local/share/pyChatALL/` (`STATE_DIR`):
- `pychatall.db` — SQLite (sessions, models, memory, settings, rate logs)
- `shared_context.json` — last 6 messages injected into every prompt
- `global_memory.json` / `memory.md` — user profile and project knowledge
- `{agent}_session.txt` — CLI session IDs for resuming conversations
- `{agent}_ctx_chars.txt` — accumulated context size for archive triggering
- `claude_rate_until.txt` — Unix timestamp blocking Claude until rate limit clears
- `archive/` — rotated old sessions
- `downloads/` — files received from Telegram

### Agent backends

**CLI agents** (Claude, Gemini, Qwen): spawned as subprocesses, session IDs persisted for `--resume`. Gemini has a 5-model fallback chain. All use `_run_subprocess()` with SIGKILL on timeout.

**HTTP agents** (OpenRouter, Ollama): `requests` POST to `/v1/chat/completions`. Retry on 502/503/504. No-retry on 403/429.

### Concurrency model
- Main thread: blocking Telegram polling
- Worker thread: single asyncio event loop, serially processes `asyncio.Queue`
- Background threads: agent execution, Team Mode pipeline, memory updates
- `threading.Lock` guards file state mutations
- `_cancel_event` (threading.Event) triggers SIGKILL on active subprocess groups
- `async_core.set_loop()` / `get_loop()` is the only safe bridge between the polling thread and the asyncio worker

### Team Mode
Projects stored at `WORK_DIR/projects/{slug}/.tg_team/` with `state.json`, `plan.md`, `coder_output.md`, `debug_round_N.md`, `project_log.md`. Max rounds configurable (3/5/10/∞). Auto-build triggers on Debugger approval.

## Configuration

`config.py` is the single source of truth. Key values:
- `ALLOWED_CHAT` — single authorized Telegram user ID
- `CTX_LIMITS` — `(warn_chars, archive_chars)` per agent
- `_AGENT_TIMEOUT` — subprocess timeout in seconds per agent
- `DEFAULT_MODELS` — default model names per agent
- Binary paths auto-detected from `$PATH`, `~/.local/bin`, nvm

Set `TG_BOT_TOKEN` as environment variable before starting.

## Migration utilities

- `migrate_json_to_sqlite.py` — one-time migration of JSON flat-files → SQLite (`pychatall.db`)
- `migrate_memory.py` — migrates the old `memory.json` format to the current schema

## Voice support

Optional: requires `openai-whisper`, `ffmpeg`, and `gtts` (see `requirements.txt`). Voice messages arrive as `*_voice.ogg` files; `voice.py` transcribes them before routing to the active agent.
