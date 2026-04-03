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
| **Граф знаний (LightRAG)** | Семантический поиск по истории разговоров — релевантный контекст в каждом запросе |
| **Точный мониторинг квот** | `/stats` — остаток лимитов для каждого агента: Claude сообщения, Gemini/Qwen RPD |
| **Быстрый фоллбэк Gemini** | При 429-ошибке — мгновенная смена модели без ожидания таймаута |
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

# Вариант 1: через переменную окружения
export TG_ALLOWED_CHAT=123456789

# Вариант 2: через файл (start.sh подхватит автоматически)
echo '123456789' > ~/.local/share/pyChatALL/allowed_chat.txt
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

### 4. (Опционально) LightRAG — граф знаний

LightRAG добавляет семантическую память: факты из разговоров сохраняются в граф-векторное хранилище и автоматически подставляются в контекст при следующих запросах.

```bash
pip install lightrag-hku sentence-transformers

# Нужна Ollama с любой моделью для построения графа
ollama pull minimax-m2.7:cloud   # или любая другая
```

> Без LightRAG бот работает в штатном режиме — модуль подключается как опциональный компонент.

### 5. Запусти бота

```bash
./start.sh          # запуск в фоне
./start.sh stop     # остановить
./start.sh status   # проверить PID
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

### Мониторинг квот
```
/stats                — лимиты текущего агента
                        Claude: остаток сообщений из CLI + эвристика
                        Gemini: промпты сегодня / RPM / сброс в 10:00 МСК
                        Qwen:   промпты сегодня / сброс в 03:00 МСК
                        OpenRouter: RPM/TPM из заголовков ответов
                        Ollama: список установленных моделей

/limit claude 85 5h   — ввести вручную % остатка (с claude.ai/usage)
/limit reset claude   — сбросить
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
       ├── router.py          выбор модели (Haiku / Sonnet)
       ├── context.py         +LightRAG-контекст, +память, +последние 6 сообщений
       │
       ▼
 agents.py
  ├── Claude CLI subprocess
  ├── Gemini CLI subprocess   (изолированный CWD, без прокси, авто-фоллбэк на 429)
  ├── Qwen CLI subprocess
  ├── OpenRouter HTTP API
  └── Ollama HTTP API
       │
       ▼
  Ответ в Telegram (стриминг)
       │
       ▼
 memory_manager.py  ── Shadow Librarian: извлекает факты → global_memory.json
                    └── lightrag_manager.py: вставляет резюме в граф знаний
```

### LightRAG — как работает граф знаний

```
Каждое сообщение
       │
       ▼
 memory_manager.py  извлекает короткое резюме разговора
       │
       ▼
 lightrag_manager.rag_insert_background()
       │  (фоновый поток, не блокирует бота)
       ▼
 LightRAG (NetworkX граф + NanoVectorDB)
   хранится в ~/.local/share/pyChatALL/lightrag/

При следующем запросе:
 context.py → rag_query(первые 400 символов промпта)
            → блок [RELEVANT_MEMORY: ...] добавляется в контекст
```

**Компоненты:**
- Embeddings: `paraphrase-multilingual-MiniLM-L12-v2` (384-dim, поддерживает русский)
- LLM для построения графа: любая модель Ollama (задаётся в `lightrag_manager.py`)
- Один выделенный asyncio-loop в daemon-потоке — все LightRAG-операции идут через него

### Мониторинг квот — как считаются лимиты

| Агент | Источник данных | Метод |
|-------|----------------|-------|
| Claude | CLI-вывод `"X messages remaining"` | Парсинг в реальном времени |
| Claude | SQLite `usage_log` | Эвристика: 45 сообщений / 5ч, 400 / неделю |
| Gemini | SQLite `usage_log` | RPD-счётчик, сброс в 10:00 МСК |
| Qwen | SQLite `usage_log` | RPD-счётчик, сброс в 03:00 МСК |
| OpenRouter | HTTP-заголовки | `x-ratelimit-remaining-*` |
| Ollama | Ollama API | `/api/tags` — список моделей |

Запросы логируются только при успешном завершении (`rc == 0`). Фоллбэк-ответы Gemini тоже учитываются.

### Структура файлов

```
tg_agent.py         Точка входа, polling, команды, очередь
agents.py           CLI subprocess + HTTP клиенты, изоляция CWD
router.py           Классификатор сложности (Haiku vs Sonnet)
context.py          Сессии, общий контекст, LightRAG-запросы, модели
team_mode.py        Командный конвейер Planner→Coder→Debugger
config.py           Все константы, пути, таймауты, лимиты
rate_tracker.py     Мониторинг квот: Claude эвристика, Gemini/Qwen RPD
lightrag_manager.py Граф знаний: вставка и поиск через LightRAG
db_manager.py       SQLite: сессии, модели, память, настройки, usage_log
memory_manager.py   Shadow Librarian — извлечение фактов + фид в LightRAG
ui.py               Telegram UI: клавиатуры, меню, файлы
voice.py            Транскрипция голоса (Whisper)
monitor.py          Консольный дашборд
```

### Хранилище: `~/.local/share/pyChatALL/`

```
token.txt              токен бота (не коммитится)
allowed_chat.txt       Telegram ID владельца (альтернатива TG_ALLOWED_CHAT)
pychatall.db           SQLite база данных (сессии, usage_log, настройки)
shared_context.json    последние 6 сообщений (общий контекст)
global_memory.json     долгосрочная память (Shadow Librarian)
lightrag/              граф знаний LightRAG (создаётся автоматически)
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
