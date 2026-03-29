# SQLite Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace 15 scattered JSON/TXT state files with a robust SQLite database, enabling reliable multi-user support and future scaling.

**Architecture:**
- Create centralized `db_manager.py` module that provides CRUD operations for all state management
- Implement one-time migration script `migrate_json_to_sqlite.py` to convert existing state files to SQLite
- Refactor 4 existing modules to use database layer instead of direct file I/O
- Maintain backward compatibility by running migration automatically on startup if JSON files exist

**Tech Stack:** SQLite3 (Python stdlib), JSON parsing, file archival

**Database Schema:**
```
sessions (agent, user_id, session_id, created_at, archived_at, last_used)
context_usage (user_id, agent, accumulated_chars, updated_at)
models (user_id, agent, model_name, updated_at)
memory (user_id, user_profile_json, project_state_json, short_term_context, updated_at)
messages (user_id, role, agent, content, timestamp)
api_keys (key_name, key_value, created_at, updated_at)
settings (user_key, user_value, updated_at)
```

---

## File Structure

### New Files
- **`db_manager.py`** — Core database layer (200 lines)
  - Initialize database with all tables
  - CRUD operations for sessions, context, models, memory, messages, API keys, settings
  - Transaction management for data consistency
  - Backward compatibility check for JSON files

- **`migrate_json_to_sqlite.py`** — One-time migration (150 lines)
  - Read 15 existing state files
  - Parse JSON/TXT content
  - Insert into appropriate database tables
  - Archive old files to `state_backup_<timestamp>/`
  - Run automatically on startup if JSON files detected

- **`tests/test_db_manager.py`** — Unit tests (100 lines)
  - Test table creation
  - Test CRUD operations for each entity type
  - Test transaction rollback on errors
  - Test auto-initialization

### Modified Files
- **`config.py`** — Add DB_PATH constant (+5 lines)
- **`context.py`** — Replace file I/O with db_manager (~20 lines changed)
- **`agents.py`** — Replace session file operations (~15 lines changed)
- **`tg_agent.py`** — Replace state initialization/reads (~10 lines changed)

---

## Tasks

### Task 1: Create Database Manager Module

**Files:**
- Create: `db_manager.py`
- Modify: `config.py`

- [ ] **Step 1: Add DB_PATH constant to config.py**

Open `config.py` and add this at the end:
```python
# Database path - persistent storage location
import os
DB_PATH = os.path.expanduser("~/.local/share/pyChatALL/pychatall.db")
```

Run: `python3 -c "from config import DB_PATH; print(DB_PATH)"`
Expected: `/home/stx/.local/share/pyChatALL/pychatall.db`

- [ ] **Step 2: Write failing test for database initialization**

Create `tests/test_db_manager.py`:
```python
import os
import sqlite3
import tempfile
import pytest
from db_manager import Database

@pytest.fixture
def temp_db():
    """Create temporary database for testing"""
    with tempfile.NamedTemporaryFile(delete=False, suffix='.db') as f:
        db_path = f.name
    yield db_path
    # Cleanup
    if os.path.exists(db_path):
        os.remove(db_path)

def test_database_initialization(temp_db):
    """Test that database initializes with all required tables"""
    db = Database(temp_db)
    db.initialize()

    # Verify all tables exist
    cursor = sqlite3.connect(temp_db).cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = set(row[0] for row in cursor.fetchall())

    required_tables = {'sessions', 'context_usage', 'models', 'memory', 'messages', 'api_keys', 'settings'}
    assert required_tables.issubset(tables), f"Missing tables: {required_tables - tables}"
```

Run: `pytest tests/test_db_manager.py::test_database_initialization -v`
Expected: FAIL - "No module named 'db_manager'"

- [ ] **Step 3: Implement Database class with table initialization**

Create `db_manager.py`:
```python
import sqlite3
import json
import os
from datetime import datetime
from contextlib import contextmanager

class Database:
    """SQLite database manager for pyChatALL state persistence"""

    def __init__(self, db_path):
        self.db_path = db_path
        # Ensure parent directory exists
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

    @contextmanager
    def get_connection(self):
        """Context manager for database connections"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        """Create all required tables if they don't exist"""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Sessions table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT DEFAULT 'default',
                    agent TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    archived_at TIMESTAMP,
                    last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, agent)
                )
            ''')

            # Context usage tracking
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS context_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT DEFAULT 'default',
                    agent TEXT NOT NULL,
                    accumulated_chars INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, agent)
                )
            ''')

            # Model selection state
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS models (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT DEFAULT 'default',
                    agent TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, agent)
                )
            ''')

            # Memory (user profile, project state, context)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT DEFAULT 'default' UNIQUE,
                    user_profile_json TEXT DEFAULT '{}',
                    project_state_json TEXT DEFAULT '{}',
                    short_term_context TEXT DEFAULT '',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Shared context messages
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT DEFAULT 'default',
                    role TEXT NOT NULL,
                    agent TEXT,
                    content TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # API keys storage
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_name TEXT NOT NULL UNIQUE,
                    key_value TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Settings and preferences
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_key TEXT NOT NULL UNIQUE,
                    user_value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

    def get_session(self, agent, user_id='default'):
        """Get active session ID for agent"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT session_id FROM sessions WHERE user_id = ? AND agent = ?',
                (user_id, agent)
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def save_session(self, agent, session_id, user_id='default'):
        """Save or update session ID for agent"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT OR REPLACE INTO sessions (user_id, agent, session_id, last_used)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)''',
                (user_id, agent, session_id)
            )

    def archive_session(self, agent, user_id='default'):
        """Mark session as archived"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE sessions SET archived_at = CURRENT_TIMESTAMP WHERE user_id = ? AND agent = ?',
                (user_id, agent)
            )

    def get_context_usage(self, agent, user_id='default'):
        """Get accumulated character count for agent"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT accumulated_chars FROM context_usage WHERE user_id = ? AND agent = ?',
                (user_id, agent)
            )
            row = cursor.fetchone()
            return row[0] if row else 0

    def update_context_usage(self, agent, char_count, user_id='default'):
        """Update context character usage"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT OR REPLACE INTO context_usage (user_id, agent, accumulated_chars)
                   VALUES (?, ?, ?)''',
                (user_id, agent, char_count)
            )

    def get_model(self, agent, user_id='default'):
        """Get selected model for agent"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT model_name FROM models WHERE user_id = ? AND agent = ?',
                (user_id, agent)
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def set_model(self, agent, model_name, user_id='default'):
        """Save selected model for agent"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT OR REPLACE INTO models (user_id, agent, model_name)
                   VALUES (?, ?, ?)''',
                (user_id, agent, model_name)
            )

    def get_memory(self, user_id='default'):
        """Get user memory (profile, project state, context)"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT user_profile_json, project_state_json, short_term_context FROM memory WHERE user_id = ?',
                (user_id,)
            )
            row = cursor.fetchone()
            if row:
                return {
                    'user_profile': json.loads(row[0]),
                    'project_state': json.loads(row[1]),
                    'short_term_context': row[2]
                }
            return {'user_profile': {}, 'project_state': {}, 'short_term_context': ''}

    def save_memory(self, memory_data, user_id='default'):
        """Save user memory"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT OR REPLACE INTO memory (user_id, user_profile_json, project_state_json, short_term_context)
                   VALUES (?, ?, ?, ?)''',
                (
                    user_id,
                    json.dumps(memory_data.get('user_profile', {})),
                    json.dumps(memory_data.get('project_state', {})),
                    memory_data.get('short_term_context', '')
                )
            )

    def add_message(self, role, content, agent=None, user_id='default'):
        """Add message to shared context"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO messages (user_id, role, agent, content) VALUES (?, ?, ?, ?)',
                (user_id, role, agent, content)
            )

    def get_recent_messages(self, limit=6, user_id='default'):
        """Get last N messages for context"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''SELECT role, agent, content FROM messages
                   WHERE user_id = ?
                   ORDER BY timestamp DESC LIMIT ?''',
                (user_id, limit)
            )
            return [dict(row) for row in cursor.fetchall()[::-1]]

    def get_api_key(self, key_name):
        """Get API key by name"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT key_value FROM api_keys WHERE key_name = ?', (key_name,))
            row = cursor.fetchone()
            return row[0] if row else None

    def set_api_key(self, key_name, key_value):
        """Save or update API key"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT OR REPLACE INTO api_keys (key_name, key_value)
                   VALUES (?, ?)''',
                (key_name, key_value)
            )

    def get_setting(self, key):
        """Get setting value"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT user_value FROM settings WHERE user_key = ?', (key,))
            row = cursor.fetchone()
            return row[0] if row else None

    def set_setting(self, key, value):
        """Save setting"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT OR REPLACE INTO settings (user_key, user_value)
                   VALUES (?, ?)''',
                (key, str(value))
            )
```

Run: `pytest tests/test_db_manager.py::test_database_initialization -v`
Expected: PASS

- [ ] **Step 4: Commit database manager**

```bash
cd /home/stx/Applications/progect/pyChatALL
git add db_manager.py config.py tests/test_db_manager.py
git commit -m "feat: add SQLite database manager with CRUD operations"
```

---

### Task 2: Create Migration Script

**Files:**
- Create: `migrate_json_to_sqlite.py`
- Test: Verify migration with test files

- [ ] **Step 1: Write test for JSON file detection**

Add to `tests/test_db_manager.py`:
```python
from migrate_json_to_sqlite import detect_json_state_files

def test_detect_json_state_files(temp_db):
    """Test detection of existing JSON state files"""
    # Create a temporary STATE_DIR with sample files
    import tempfile
    with tempfile.TemporaryDirectory() as temp_state:
        # Create sample state files
        open(os.path.join(temp_state, 'claude_session.txt'), 'w').close()
        open(os.path.join(temp_state, 'shared_context.json'), 'w').close()

        # Test detection
        files = detect_json_state_files(temp_state)
        assert 'claude_session.txt' in files
        assert 'shared_context.json' in files
```

Run: `pytest tests/test_db_manager.py::test_detect_json_state_files -v`
Expected: FAIL - "cannot import name 'detect_json_state_files'"

- [ ] **Step 2: Implement migration script**

Create `migrate_json_to_sqlite.py`:
```python
import os
import json
import shutil
from datetime import datetime
from config import STATE_DIR, DB_PATH
from db_manager import Database

def detect_json_state_files(state_dir):
    """Detect existing JSON/TXT state files"""
    if not os.path.exists(state_dir):
        return []

    files = os.listdir(state_dir)
    state_files = [f for f in files if not f.startswith('.') and f not in ['archive', 'downloads']]
    return state_files

def migrate_json_to_sqlite():
    """Migrate all JSON/TXT state files to SQLite database"""
    # Check if JSON files exist
    existing_files = detect_json_state_files(STATE_DIR)
    if not existing_files:
        print("No legacy state files found. Starting fresh SQLite database.")
        return True

    print(f"Found {len(existing_files)} legacy state files. Starting migration...")

    # Initialize database
    db = Database(DB_PATH)
    db.initialize()

    # Create backup directory
    backup_dir = os.path.join(STATE_DIR, f'state_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}')
    os.makedirs(backup_dir, exist_ok=True)

    try:
        # Migrate sessions
        for agent in ['claude', 'gemini', 'qwen', 'openrouter']:
            session_file = os.path.join(STATE_DIR, f'{agent}_session.txt')
            if os.path.exists(session_file):
                with open(session_file, 'r') as f:
                    session_id = f.read().strip()
                if session_id:
                    db.save_session(agent, session_id)
                shutil.copy2(session_file, backup_dir)

        # Migrate context usage
        for agent in ['claude', 'gemini', 'qwen', 'openrouter']:
            ctx_file = os.path.join(STATE_DIR, f'{agent}_ctx_chars.txt')
            if os.path.exists(ctx_file):
                with open(ctx_file, 'r') as f:
                    chars = int(f.read().strip() or '0')
                db.update_context_usage(agent, chars)
                shutil.copy2(ctx_file, backup_dir)

        # Migrate model selections
        for agent in ['claude', 'gemini', 'qwen', 'openrouter']:
            model_file = os.path.join(STATE_DIR, f'{agent}_model.txt')
            if os.path.exists(model_file):
                with open(model_file, 'r') as f:
                    model_name = f.read().strip()
                if model_name:
                    db.set_model(agent, model_name)
                shutil.copy2(model_file, backup_dir)

        # Migrate shared context
        shared_ctx_file = os.path.join(STATE_DIR, 'shared_context.json')
        if os.path.exists(shared_ctx_file):
            with open(shared_ctx_file, 'r') as f:
                shared_ctx = json.load(f)
            if isinstance(shared_ctx, list):
                for msg in shared_ctx:
                    db.add_message(
                        role=msg.get('role', 'user'),
                        content=msg.get('content', ''),
                        agent=msg.get('agent')
                    )
            shutil.copy2(shared_ctx_file, backup_dir)

        # Migrate memory
        memory_file = os.path.join(STATE_DIR, 'global_memory.json')
        if os.path.exists(memory_file):
            with open(memory_file, 'r') as f:
                memory_data = json.load(f)
            db.save_memory(memory_data)
            shutil.copy2(memory_file, backup_dir)

        # Migrate active agent setting
        active_agent_file = os.path.join(STATE_DIR, 'active_agent.txt')
        if os.path.exists(active_agent_file):
            with open(active_agent_file, 'r') as f:
                active_agent = f.read().strip()
            if active_agent:
                db.set_setting('active_agent', active_agent)
            shutil.copy2(active_agent_file, backup_dir)

        # Migrate rate limit info
        rate_limit_file = os.path.join(STATE_DIR, 'claude_rate_until.txt')
        if os.path.exists(rate_limit_file):
            with open(rate_limit_file, 'r') as f:
                rate_until = f.read().strip()
            if rate_until:
                db.set_setting('claude_rate_until', rate_until)
            shutil.copy2(rate_limit_file, backup_dir)

        print(f"✓ Migration complete. Backup created in {backup_dir}")
        print(f"✓ {len(existing_files)} files migrated to SQLite")
        return True

    except Exception as e:
        print(f"✗ Migration failed: {e}")
        print(f"  Backup available in {backup_dir}")
        return False

if __name__ == '__main__':
    if migrate_json_to_sqlite():
        print("SQLite migration successful!")
    else:
        print("SQLite migration failed!")
```

Run: `pytest tests/test_db_manager.py::test_detect_json_state_files -v`
Expected: PASS

- [ ] **Step 3: Commit migration script**

```bash
git add migrate_json_to_sqlite.py
git commit -m "feat: add one-time JSON to SQLite migration script"
```

---

### Task 3: Update config.py

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Add initialization logic to config.py**

Open `config.py` and add this at the end:
```python
# Auto-create required directories
import os

for directory in [os.path.dirname(DB_PATH), os.path.join(STATE_DIR, 'archive'), os.path.join(STATE_DIR, 'downloads')]:
    os.makedirs(directory, exist_ok=True)

# Auto-initialize SQLite if needed
if not os.path.exists(DB_PATH):
    from db_manager import Database
    db = Database(DB_PATH)
    db.initialize()
    print(f"[CONFIG] Created new SQLite database at {DB_PATH}")

    # Run one-time migration if JSON files exist
    from migrate_json_to_sqlite import migrate_json_to_sqlite
    migrate_json_to_sqlite()
```

Run: `python3 -c "import config; print('Config initialized')"`
Expected: Output shows database creation and any migrations

- [ ] **Step 2: Commit config changes**

```bash
git add config.py
git commit -m "feat: auto-initialize SQLite database and run migrations on startup"
```

---

### Task 4: Update context.py

**Files:**
- Modify: `context.py`

- [ ] **Step 1: Replace file I/O with db_manager in context.py**

Find these sections in `context.py` and replace them:

**Original (file-based):**
```python
def save_shared_context(messages):
    with open(os.path.join(STATE_DIR, 'shared_context.json'), 'w') as f:
        json.dump(messages, f)

def load_shared_context():
    ctx_file = os.path.join(STATE_DIR, 'shared_context.json')
    if os.path.exists(ctx_file):
        with open(ctx_file, 'r') as f:
            return json.load(f)
    return []
```

**Replace with:**
```python
from db_manager import Database
from config import DB_PATH

db = Database(DB_PATH)

def save_shared_context(messages):
    # Clear old messages and save new ones
    for msg in messages:
        db.add_message(
            role=msg.get('role', 'user'),
            content=msg.get('content', ''),
            agent=msg.get('agent')
        )

def load_shared_context():
    return db.get_recent_messages(limit=6)
```

- [ ] **Step 2: Replace memory file operations**

Find memory load/save functions and replace:

**Original:**
```python
def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_memory(data):
    with open(MEMORY_FILE, 'w') as f:
        json.dump(data, f)
```

**Replace with:**
```python
def load_memory():
    return db.get_memory()

def save_memory(data):
    db.save_memory(data)
```

Run: `python3 -c "from context import load_shared_context, load_memory; print('Context module loads')" 2>&1 | head -20`
Expected: "Context module loads" or specific import errors (will fix next)

- [ ] **Step 3: Commit context.py changes**

```bash
git add context.py
git commit -m "refactor: replace file-based context storage with SQLite db_manager"
```

---

### Task 5: Update agents.py

**Files:**
- Modify: `agents.py`

- [ ] **Step 1: Replace session file operations**

Find session save/load functions in `agents.py`:

**Original:**
```python
def save_session_id(agent, session_id):
    with open(os.path.join(STATE_DIR, f'{agent}_session.txt'), 'w') as f:
        f.write(session_id)

def load_session_id(agent):
    session_file = os.path.join(STATE_DIR, f'{agent}_session.txt')
    if os.path.exists(session_file):
        with open(session_file, 'r') as f:
            return f.read().strip()
    return None
```

**Replace with:**
```python
from db_manager import Database
from config import DB_PATH

db = Database(DB_PATH)

def save_session_id(agent, session_id):
    db.save_session(agent, session_id)

def load_session_id(agent):
    return db.get_session(agent)
```

- [ ] **Step 2: Replace context usage operations**

Find context char tracking:

**Original:**
```python
def update_ctx_chars(agent, char_count):
    with open(os.path.join(STATE_DIR, f'{agent}_ctx_chars.txt'), 'w') as f:
        f.write(str(char_count))

def get_ctx_chars(agent):
    chars_file = os.path.join(STATE_DIR, f'{agent}_ctx_chars.txt')
    if os.path.exists(chars_file):
        with open(chars_file, 'r') as f:
            return int(f.read().strip() or '0')
    return 0
```

**Replace with:**
```python
def update_ctx_chars(agent, char_count):
    db.update_context_usage(agent, char_count)

def get_ctx_chars(agent):
    return db.get_context_usage(agent)
```

- [ ] **Step 3: Replace model selection operations**

Find model file operations:

**Original:**
```python
def set_model(agent, model_name):
    with open(os.path.join(STATE_DIR, f'{agent}_model.txt'), 'w') as f:
        f.write(model_name)

def get_model(agent):
    model_file = os.path.join(STATE_DIR, f'{agent}_model.txt')
    if os.path.exists(model_file):
        with open(model_file, 'r') as f:
            return f.read().strip()
    return None
```

**Replace with:**
```python
def set_model(agent, model_name):
    db.set_model(agent, model_name)

def get_model(agent):
    return db.get_model(agent)
```

- [ ] **Step 4: Commit agents.py changes**

```bash
git add agents.py
git commit -m "refactor: replace file-based session storage with SQLite db_manager"
```

---

### Task 6: Update tg_agent.py

**Files:**
- Modify: `tg_agent.py`

- [ ] **Step 1: Replace active agent setting operations**

Find where active_agent is read/written:

**Original:**
```python
def get_active_agent():
    active_file = os.path.join(STATE_DIR, 'active_agent.txt')
    if os.path.exists(active_file):
        with open(active_file, 'r') as f:
            return f.read().strip()
    return 'claude'

def set_active_agent(agent):
    with open(os.path.join(STATE_DIR, 'active_agent.txt'), 'w') as f:
        f.write(agent)
```

**Replace with:**
```python
from db_manager import Database
from config import DB_PATH

db = Database(DB_PATH)

def get_active_agent():
    return db.get_setting('active_agent') or 'claude'

def set_active_agent(agent):
    db.set_setting('active_agent', agent)
```

- [ ] **Step 2: Replace rate limit operations**

Find rate limit tracking:

**Original:**
```python
def is_claude_rate_limited():
    rate_file = os.path.join(STATE_DIR, 'claude_rate_until.txt')
    if os.path.exists(rate_file):
        with open(rate_file, 'r') as f:
            until_ts = float(f.read().strip() or 0)
        return time.time() < until_ts
    return False

def set_claude_rate_limit(until_ts):
    with open(os.path.join(STATE_DIR, 'claude_rate_until.txt'), 'w') as f:
        f.write(str(until_ts))
```

**Replace with:**
```python
import time

def is_claude_rate_limited():
    until_ts = db.get_setting('claude_rate_until')
    if until_ts:
        return time.time() < float(until_ts)
    return False

def set_claude_rate_limit(until_ts):
    db.set_setting('claude_rate_until', until_ts)
```

- [ ] **Step 3: Commit tg_agent.py changes**

```bash
git add tg_agent.py
git commit -m "refactor: replace file-based settings with SQLite db_manager"
```

---

### Task 7: Run Integration Tests

**Files:**
- Test: All modified files

- [ ] **Step 1: Create integration test**

Add to `tests/test_db_manager.py`:
```python
def test_full_workflow(temp_db):
    """Test complete workflow: sessions → context → memory"""
    db = Database(temp_db)
    db.initialize()

    # Save and load session
    db.save_session('claude', 'sess_abc123')
    assert db.get_session('claude') == 'sess_abc123'

    # Track context
    db.update_context_usage('claude', 5000)
    assert db.get_context_usage('claude') == 5000

    # Set model
    db.set_model('claude', 'claude-3-sonnet')
    assert db.get_model('claude') == 'claude-3-sonnet'

    # Save messages
    db.add_message('user', 'Hello', agent='claude')
    db.add_message('assistant', 'Hi there', agent='claude')
    messages = db.get_recent_messages(limit=2)
    assert len(messages) == 2
    assert messages[0]['content'] == 'Hello'

    # Save memory
    memory = {'user_profile': {'name': 'Alex'}, 'project_state': {}}
    db.save_memory(memory)
    loaded = db.get_memory()
    assert loaded['user_profile']['name'] == 'Alex'
```

Run: `pytest tests/test_db_manager.py::test_full_workflow -v`
Expected: PASS

- [ ] **Step 2: Test all database operations**

Run: `pytest tests/test_db_manager.py -v`
Expected: All tests pass (at least 4 tests)

- [ ] **Step 3: Commit integration tests**

```bash
git add tests/test_db_manager.py
git commit -m "test: add comprehensive integration tests for database operations"
```

---

### Task 8: Manual Smoke Test

**Files:**
- Test: Full application startup

- [ ] **Step 1: Start the application**

```bash
cd /home/stx/Applications/progect/pyChatALL
python3 -c "
import config
from db_manager import Database
db = Database(config.DB_PATH)

# Verify database exists
import os
print(f'Database created: {os.path.exists(config.DB_PATH)}')

# Verify tables exist
import sqlite3
conn = sqlite3.connect(config.DB_PATH)
cursor = conn.cursor()
cursor.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")
tables = [row[0] for row in cursor.fetchall()]
print(f'Tables: {tables}')

# Test CRUD
db.save_session('claude', 'test_session_123')
session = db.get_session('claude')
print(f'Session saved and loaded: {session == \"test_session_123\"}')
"
```

Expected output:
```
Database created: True
Tables: ['sessions', 'context_usage', 'models', 'memory', 'messages', 'api_keys', 'settings']
Session saved and loaded: True
```

- [ ] **Step 2: Test migration (if JSON files present)**

```bash
# Only if legacy state files exist
python3 migrate_json_to_sqlite.py
```

Expected: "Migration complete" or "No legacy state files"

- [ ] **Step 3: Verify no old files are accessed**

```bash
# Search codebase for direct file I/O (should find none)
grep -r "STATE_DIR.*txt\|STATE_DIR.*json" --include="*.py" . | grep -v test | grep -v backup
```

Expected: No results (all file I/O goes through db_manager)

- [ ] **Step 4: Final commit and summary**

```bash
git log --oneline -10
```

Expected: Shows 7-8 commits related to SQLite migration

---

## Testing Checklist

- [ ] Unit tests pass: `pytest tests/test_db_manager.py -v`
- [ ] Database initialization works: `python3 -c "from config import DB_PATH; import os; print(os.path.exists(DB_PATH))"`
- [ ] No direct file I/O in modified modules
- [ ] Backward compatibility: JSON files migrated automatically
- [ ] All 7 database tables created
- [ ] CRUD operations work for all entity types

---

## Rollback Plan

If issues occur:
1. Database backup: `~/.local/share/pyChatALL/pychatall.db` (just delete to reset)
2. State file backup: `STATE_DIR/state_backup_<timestamp>/` contains original files
3. Restore from backup: Copy files from `state_backup_<timestamp>/` back to `STATE_DIR`
4. Revert commits: `git reset --hard HEAD~7` (if all tasks completed)

---

## Success Criteria

✅ All 7 tasks completed
✅ All tests passing
✅ No breaking changes to existing APIs
✅ Database auto-initializes on first run
✅ One-time migration automatic
✅ Performance improvement: no file I/O overhead
✅ Ready for multi-user expansion

