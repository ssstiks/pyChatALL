import os
import sys
import sqlite3
import tempfile
import pytest

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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


def test_save_and_load_memory(temp_db):
    """Test memory save and load with JSON handling"""
    db = Database(temp_db)
    db.initialize()

    # Save valid memory
    memory = {
        'user_profile': {'name': 'Alice', 'role': 'developer'},
        'project_state': {'current_project': 'pyChatALL'},
        'short_term_context': 'Working on database migration'
    }
    db.save_memory(memory)

    # Load and verify
    loaded = db.get_memory()
    assert loaded['user_profile']['name'] == 'Alice'
    assert loaded['project_state']['current_project'] == 'pyChatALL'
    assert loaded['short_term_context'] == 'Working on database migration'


def test_save_memory_with_invalid_data(temp_db):
    """Test that save_memory rejects non-serializable data"""
    db = Database(temp_db)
    db.initialize()

    # Try to save non-serializable object
    invalid_memory = {
        'user_profile': {'callback': lambda x: x},  # Functions can't be JSON serialized
        'project_state': {}
    }

    with pytest.raises(ValueError):
        db.save_memory(invalid_memory)


def test_get_memory_with_corrupted_json(temp_db):
    """Test that get_memory handles corrupted JSON gracefully"""
    db = Database(temp_db)
    db.initialize()

    # Manually insert corrupted JSON into database
    with sqlite3.connect(temp_db) as conn:
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO memory (user_id, user_profile_json, project_state_json) VALUES (?, ?, ?)',
            ('default', '{invalid json}', '{"valid": "json"}')
        )
        conn.commit()

    # Should return defaults instead of crashing
    result = db.get_memory('default')
    assert result['user_profile'] == {}  # Should default to empty dict
    assert result['project_state'] == {'valid': 'json'}  # Should load valid JSON


def test_api_keys_and_settings_with_user_id(temp_db):
    """Test API keys and settings with user_id support"""
    db = Database(temp_db)
    db.initialize()

    # Test API keys
    db.set_api_key('openrouter', 'key123')
    assert db.get_api_key('openrouter') == 'key123'

    db.set_api_key('openrouter', 'key456', user_id='user2')
    assert db.get_api_key('openrouter', user_id='user2') == 'key456'
    assert db.get_api_key('openrouter') == 'key123'  # Default user unaffected

    # Test settings
    db.set_setting('active_agent', 'claude')
    assert db.get_setting('active_agent') == 'claude'

    db.set_setting('active_agent', 'gemini', user_id='user2')
    assert db.get_setting('active_agent', user_id='user2') == 'gemini'
    assert db.get_setting('active_agent') == 'claude'  # Default user unaffected


def test_detect_json_state_files():
    """Test detection of existing JSON state files"""
    import tempfile
    from migrate_json_to_sqlite import detect_json_state_files

    with tempfile.TemporaryDirectory() as temp_state:
        # Create sample state files
        open(os.path.join(temp_state, 'claude_session.txt'), 'w').close()
        open(os.path.join(temp_state, 'shared_context.json'), 'w').close()
        os.makedirs(os.path.join(temp_state, 'archive'), exist_ok=True)  # Should be ignored

        # Test detection
        files = detect_json_state_files(temp_state)
        assert 'claude_session.txt' in files
        assert 'shared_context.json' in files
        assert 'archive' not in files  # Should skip directories like archive
