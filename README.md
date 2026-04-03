# pyChatALL

Personal Telegram bot that routes messages to multiple AI agents: **Claude, Gemini, Qwen, OpenRouter, Ollama**. Switch agents on the fly, run them in parallel, or chain them in a Planner→Coder→Debugger pipeline.

---

## Features

- **5 AI backends** — Claude CLI, Gemini CLI, Qwen CLI, OpenRouter API, Ollama (local)
- **Smart routing** — auto-selects Haiku vs Sonnet based on prompt complexity
- **Persistent sessions** — CLI agents resume conversations with `--resume <session_id>`
- **Shared context** — last 6 messages injected into every prompt across all agents
- **Team Mode** — Planner → Coder → Debugger pipeline with auto-build and auto-commit
- **Rate tracking** — Gemini (1500 RPD) and Qwen (1000 RPD) quota monitoring
- **Agent isolation** — each agent runs in its own workspace, never scans the bot's files
- **Emergency cancel** — `/cancel` kills any stuck process and drains the queue

---

## Requirements

- Python 3.10+
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram user ID (from [@userinfobot](https://t.me/userinfobot))
- At least one AI CLI installed: `claude`, `gemini`, or `qwen`

---

## Installation

```bash
git clone https://github.com/ssstiks/pyChatALL.git
cd pyChatALL
pip install requests
```

### Configure

```bash
# Save your bot token
mkdir -p ~/.local/share/pyChatALL
echo 'YOUR_BOT_TOKEN' > ~/.local/share/pyChatALL/token.txt
chmod 600 ~/.local/share/pyChatALL/token.txt

# Set your Telegram user ID (only this user can talk to the bot)
export TG_ALLOWED_CHAT=123456789
```

Or use environment variables only:
```bash
export TG_BOT_TOKEN=your_token
export TG_ALLOWED_CHAT=123456789
```

### Install AI agents (install what you need)

```bash
# Claude Code CLI
npm install -g @anthropic-ai/claude-code && claude login

# Gemini CLI
npm install -g @google/gemini-cli && gemini

# Qwen Code CLI (requires Node.js >= 20)
npm install -g @qwen-code/qwen-code && qwen

# Ollama (local models, no API key needed)
curl -fsSL https://ollama.com/install.sh | sh && ollama pull llama3.2
```

---

## Running

```bash
./start.sh              # start (auto-detects proxy, loads token)
./start.sh stop         # stop
./start.sh status       # check if running
./start.sh logs         # tail live logs
python3 monitor.py      # real-time console dashboard
```

### As a systemd service

```bash
sudo tee /etc/systemd/system/pychatall.service << EOF
[Unit]
Description=pyChatALL Telegram Bot
After=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
Environment="TG_BOT_TOKEN=your_token"
Environment="TG_ALLOWED_CHAT=123456789"
ExecStart=$(which python3) $(pwd)/tg_agent.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now pychatall
```

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/claude` `/gemini` `/qwen` `/openrouter` `/ollama` | Switch active agent |
| `/all <question>` | Ask all agents in parallel |
| `/discuss <topic>` | Sequential discussion between agents |
| `/cancel` | Kill stuck process, drain queue |
| `/retry` | Repeat last request |
| `/reset [agent\|all]` | Reset session |
| `/ctx` | Show context usage |
| `/model [list\|name]` | View or change model |
| `/remember <fact>` | Save to long-term memory |
| `/memory` | Show saved memory |
| `/files` | File browser |
| `/git` | Git panel (status/diff/commit) |
| `/team /new <name>` | Create Team Mode project |
| `/team /start <task>` | Run Planner→Coder→Debugger pipeline |
| `/setup` | Agent installation panel |
| `/menu` | Main menu |

---

## Architecture

```
tg_agent.py       Telegram polling, command handlers, async queue
agents.py         Subprocess wrappers (CLI agents) + HTTP clients (API agents)
router.py         Complexity classifier — Haiku vs Sonnet selection
context.py        Session IDs, shared context, global memory
team_mode.py      Planner→Coder→Debugger pipeline
config.py         Constants, binary discovery, timeouts, limits
rate_tracker.py   Quota tracking: Gemini (1500 RPD), Qwen (1000 RPD)
db_manager.py     SQLite persistence
ui.py             Telegram UI: keyboards, menus, file upload
voice.py          Whisper voice transcription (OGG → text)
monitor.py        Real-time console dashboard
```

### State: `~/.local/share/pyChatALL/`

| Path | Contents |
|------|----------|
| `token.txt` | Bot token (gitignored) |
| `pychatall.db` | SQLite: sessions, models, memory, settings |
| `shared_context.json` | Last 6 messages injected into every prompt |
| `global_memory.json` | Long-term facts |
| `workspaces/{agent}/` | Isolated CWD per agent — no bot files visible |

### Agent isolation

Each agent runs in its own empty workspace directory (`workspaces/claude/`, `workspaces/gemini/`, etc.). The agent never sees the bot's source code, preventing unnecessary file scanning and API quota waste. When a Team Mode project is active, the agent uses the project directory instead.

---

## License

MIT
