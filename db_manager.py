"""
SQLite database manager for pyChatALL state persistence.

Provides a unified interface for managing all application state:
- Sessions (per agent)
- Context usage tracking
- Model selection
- User memory (profile, project state, context)
- Shared context messages
- API keys
- Settings
"""

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
        """Context manager for database connections.

        journal_mode=WAL is set once in initialize() and persists on the DB file.
        Only per-session PRAGMAs are set here on every connection.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except sqlite3.DatabaseError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        """Create all required tables if they don't exist"""
        with self.get_connection() as conn:
            # WAL mode persists on the DB file — set once here, not per-connection
            conn.execute("PRAGMA journal_mode=WAL")
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
                    user_id TEXT DEFAULT 'default',
                    key_name TEXT NOT NULL,
                    key_value TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, key_name)
                )
            ''')

            # Settings and preferences
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT DEFAULT 'default',
                    user_key TEXT NOT NULL,
                    user_value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, user_key)
                )
            ''')

            # Lessons learned — self-learning knowledge base
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS knowledge_base (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_name TEXT NOT NULL DEFAULT 'general',
                    error_summary TEXT NOT NULL,
                    fix_steps TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Usage log for heuristic rate limiting
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS usage_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                try:
                    user_profile = json.loads(row[0])
                except (json.JSONDecodeError, ValueError):
                    user_profile = {}

                try:
                    project_state = json.loads(row[1])
                except (json.JSONDecodeError, ValueError):
                    project_state = {}

                return {
                    'user_profile': user_profile,
                    'project_state': project_state,
                    'short_term_context': row[2] or ''
                }
            return {'user_profile': {}, 'project_state': {}, 'short_term_context': ''}

    def save_memory(self, memory_data, user_id='default'):
        """Save user memory"""
        if not isinstance(memory_data, dict):
            raise TypeError("memory_data must be a dictionary")

        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                user_profile_json = json.dumps(memory_data.get('user_profile', {}))
                project_state_json = json.dumps(memory_data.get('project_state', {}))
            except (TypeError, ValueError) as e:
                raise ValueError(f"memory_data contains non-JSON-serializable objects: {e}")

            cursor.execute(
                '''INSERT OR REPLACE INTO memory (user_id, user_profile_json, project_state_json, short_term_context)
                   VALUES (?, ?, ?, ?)''',
                (
                    user_id,
                    user_profile_json,
                    project_state_json,
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
        """Get last N messages for context in chronological order."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''SELECT role, agent, content, timestamp FROM (
                       SELECT id, role, agent, content, timestamp FROM messages
                       WHERE user_id = ?
                       ORDER BY id DESC LIMIT ?
                   ) ORDER BY id ASC''',
                (user_id, limit)
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_api_key(self, key_name, user_id='default'):
        """Get API key by name"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT key_value FROM api_keys WHERE user_id = ? AND key_name = ?', (user_id, key_name))
            row = cursor.fetchone()
            return row[0] if row else None

    def set_api_key(self, key_name, key_value, user_id='default'):
        """Save or update API key"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT OR REPLACE INTO api_keys (user_id, key_name, key_value)
                   VALUES (?, ?, ?)''',
                (user_id, key_name, key_value)
            )

    def get_setting(self, key, user_id='default'):
        """Get setting value"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT user_value FROM settings WHERE user_id = ? AND user_key = ?', (user_id, key))
            row = cursor.fetchone()
            return row[0] if row else None

    def set_setting(self, key, value, user_id='default'):
        """Save setting"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT OR REPLACE INTO settings (user_id, user_key, user_value)
                   VALUES (?, ?, ?)''',
                (user_id, key, str(value))
            )

    # ── Knowledge Base (Lessons Learned) ─────────────────────────────────────

    def add_lesson(self, project_name: str, error_summary: str, fix_steps: str) -> None:
        """Store a new lesson in the knowledge base."""
        with self.get_connection() as conn:
            conn.execute(
                '''INSERT INTO knowledge_base (project_name, error_summary, fix_steps)
                   VALUES (?, ?, ?)''',
                (project_name.strip().lower(), error_summary[:300], fix_steps[:500]),
            )

    def get_lessons(self, project_name: str | None = None, limit: int = 5) -> list[dict]:
        """Retrieve lessons, optionally filtered by project name."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if project_name:
                cursor.execute(
                    '''SELECT project_name, error_summary, fix_steps, timestamp
                       FROM knowledge_base
                       WHERE project_name = ?
                       ORDER BY timestamp DESC LIMIT ?''',
                    (project_name.strip().lower(), limit),
                )
            else:
                cursor.execute(
                    '''SELECT project_name, error_summary, fix_steps, timestamp
                       FROM knowledge_base
                       ORDER BY timestamp DESC LIMIT ?''',
                    (limit,),
                )
            return [dict(row) for row in cursor.fetchall()]

    def lesson_exists(self, error_summary: str) -> bool:
        """Check if a lesson with the same summary already exists (dedup)."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT 1 FROM knowledge_base WHERE error_summary = ? LIMIT 1',
                (error_summary[:300],),
            )
            return cursor.fetchone() is not None
