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


def test_config_auto_initialization():
    """Test that importing config initializes database and directories"""
    import tempfile
    import importlib

    with tempfile.TemporaryDirectory() as temp_home:
        # Create a temporary config module that uses temp_home as STATE_DIR
        config_path = os.path.join(temp_home, 'config.py')

        # Write a test config file with minimal initialization
        with open(config_path, 'w') as f:
            f.write(f"""
import os

STATE_DIR = '{temp_home}'
DB_PATH = os.path.expanduser("{temp_home}/pychatall.db")

# Auto-create required directories
for directory in [os.path.dirname(DB_PATH), os.path.join(STATE_DIR, 'archive'), os.path.join(STATE_DIR, 'downloads')]:
    os.makedirs(directory, exist_ok=True)

# Verify directories exist
assert os.path.exists(os.path.dirname(DB_PATH)), f"DB directory not created: {{os.path.dirname(DB_PATH)}}"
assert os.path.exists(os.path.join(STATE_DIR, 'archive')), f"archive directory not created"
assert os.path.exists(os.path.join(STATE_DIR, 'downloads')), f"downloads directory not created"
""")

        # Import and verify
        sys.path.insert(0, temp_home)
        try:
            spec = importlib.util.spec_from_file_location("config_test", config_path)
            config_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(config_module)

            # Verify all directories were created
            assert os.path.exists(os.path.dirname(config_module.DB_PATH))
            assert os.path.exists(os.path.join(config_module.STATE_DIR, 'archive'))
            assert os.path.exists(os.path.join(config_module.STATE_DIR, 'downloads'))
        finally:
            sys.path.remove(temp_home)


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
    # Messages are returned in chronological (insertion) order
    assert messages[0]['content'] == 'Hello'
    assert messages[1]['content'] == 'Hi there'

    # Save memory
    memory = {'user_profile': {'name': 'Alex'}, 'project_state': {}}
    db.save_memory(memory)
    loaded = db.get_memory()
    assert loaded['user_profile']['name'] == 'Alex'


def test_multi_agent_isolation(temp_db):
    """Test that different agents have isolated state"""
    db = Database(temp_db)
    db.initialize()

    # Set different values for different agents
    db.save_session('claude', 'session_claude')
    db.save_session('gemini', 'session_gemini')

    db.update_context_usage('claude', 1000)
    db.update_context_usage('gemini', 2000)

    # Verify isolation
    assert db.get_session('claude') == 'session_claude'
    assert db.get_session('gemini') == 'session_gemini'
    assert db.get_context_usage('claude') == 1000
    assert db.get_context_usage('gemini') == 2000
