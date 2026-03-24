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
