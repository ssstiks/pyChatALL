<div align="center">

# 🤖 pyChatALL

**Один Telegram-бот — пять AI-агентов**

Переключайся между Claude, Gemini, Qwen, OpenRouter и Ollama прямо в чате.
Спрашивай всех сразу, запускай командный конвейер, управляй проектами.

[![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-blue?logo=telegram)](https://t.me/BotFather)

</div>

---

## ✨ Возможности

| | Описание |
|-|----------|
| **5 AI-агентов** | Claude, Gemini, Qwen — через CLI; OpenRouter, Ollama — через API |
| **Умный роутинг** | Автоматически выбирает Haiku или Sonnet по сложности запроса |
| **Параллельный опрос** | `/all вопрос` — получи ответы от всех агентов сразу |
| **Командный режим** | Planner → Coder → Debugger с авто-коммитом и авто-сборкой |
| **Голосовые сообщения** | Транскрипция через Whisper прямо в боте |
| **Долгосрочная память** | Бот помнит факты между сессиями (`/remember`) |
| **Мониторинг квот** | Gemini 1500 RPD, Qwen 1000 RPD — предупреждает до исчерпания |
| **Экстренная отмена** | `/cancel` — убивает зависший процесс, очищает очередь |
| **Изоляция агентов** | Каждый агент в своей папке, не сканирует лишние файлы |

---

## 🚀 Установка

### 1. Клонируй репозиторий

```bash
git clone https://github.com/ssstiks/pyChatALL.git
cd pyChatALL
pip install requests
```

### 2. Настрой токен и доступ

Получи токен у [@BotFather](https://t.me/BotFather), свой ID — у [@userinfobot](https://t.me/userinfobot).

```bash
mkdir -p ~/.local/share/pyChatALL
echo 'ВАШ_ТОКЕН' > ~/.local/share/pyChatALL/token.txt
chmod 600 ~/.local/share/pyChatALL/token.txt

export TG_ALLOWED_CHAT=123456789   # ваш Telegram ID
```

### 3. Установи нужные AI-агенты

```bash
# Claude (аккаунт Anthropic)
npm install -g @anthropic-ai/claude-code && claude login

# Gemini (аккаунт Google)
npm install -g @google/gemini-cli && gemini

# Qwen (Node.js >= 20, аккаунт Alibaba)
npm install -g @qwen-code/qwen-code && qwen

# Ollama — локальные модели, без API-ключа
curl -fsSL https://ollama.com/install.sh | sh && ollama pull llama3.2
```

### 4. Запусти бота

```bash
./start.sh          # запуск в фоне
./start.sh stop     # остановить
./start.sh logs     # логи в реальном времени
python3 monitor.py  # консольный дашборд
```

---

## 💬 Команды

### Выбор агента
```
/claude   /gemini   /qwen   /openrouter   /ollama
```

### Работа с AI
```
/all <вопрос>         — спросить всех агентов параллельно
/discuss <тема>       — обсуждение: каждый читает ответы предыдущих
/retry                — повторить последний запрос
/cancel               — остановить зависший процесс
```

### Сессия и модели
```
/reset [агент|all]    — сбросить сессию
/ctx                  — использование контекста
/model [list|имя]     — сменить модель
/timeout [агент] [с]  — изменить таймаут
```

### Память и файлы
```
/remember <факт>      — запомнить навсегда
/memory               — показать память
/files                — браузер файлов проекта
/git                  — git панель (статус / diff / коммит)
/search <запрос>      — веб-поиск
```

### Team Mode — командный конвейер
```
/team /new <имя>      — создать проект
/team /start <задача> — запустить Planner→Coder→Debugger
/team /status         — статус текущего проекта
/team /list           — список всех проектов
```

---

## 🏗️ Архитектура

```
Сообщение в Telegram
       │
       ▼
 tg_agent.py ──► asyncio.Queue (один запрос за раз)
       │
       ├── router.py     выбор модели (Haiku / Sonnet)
       ├── context.py    +память, +последние 6 сообщений
       │
       ▼
 agents.py
  ├── Claude CLI subprocess
  ├── Gemini CLI subprocess   (изолированный CWD, без прокси)
  ├── Qwen CLI subprocess
  ├── OpenRouter HTTP API
  └── Ollama HTTP API
       │
       ▼
  Ответ в Telegram (стриминг)
```

### Структура файлов

```
tg_agent.py      Точка входа, polling, команды, очередь
agents.py        CLI subprocess + HTTP клиенты, изоляция CWD
router.py        Классификатор сложности (Haiku vs Sonnet)
context.py       Сессии, общий контекст, память, модели
team_mode.py     Командный конвейер Planner→Coder→Debugger
config.py        Все константы, пути, таймауты, лимиты
rate_tracker.py  Мониторинг квот Gemini и Qwen
db_manager.py    SQLite: сессии, модели, память, настройки
ui.py            Telegram UI: клавиатуры, меню, файлы
voice.py         Транскрипция голоса (Whisper)
monitor.py       Консольный дашборд
```

### Хранилище: `~/.local/share/pyChatALL/`

```
token.txt              токен бота (не коммитится)
pychatall.db           SQLite база данных
shared_context.json    последние 6 сообщений (общий контекст)
global_memory.json     долгосрочная память
workspaces/claude/     изолированная папка Claude
workspaces/gemini/     изолированная папка Gemini
workspaces/qwen/       изолированная папка Qwen
```

---

## ⚙️ Запуск как системная служба

```bash
sudo systemctl enable --now pychatall
# см. .env.example для настройки переменных окружения
```

Полный пример unit-файла — в разделе [Installation](#-установка).

---

## 📄 Лицензия

MIT
