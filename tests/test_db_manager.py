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
