#!/usr/bin/env python3
"""
Migration script to convert legacy JSON/TXT state files to SQLite database.

This is a one-time migration that runs automatically if legacy state files exist.
Creates timestamped backups before migration.
"""

import os
import json
import shutil
from datetime import datetime
from config import STATE_DIR, DB_PATH
from db_manager import Database

# Supported agents for migration
SUPPORTED_AGENTS = ['claude', 'gemini', 'qwen', 'openrouter']


def detect_json_state_files(state_dir):
    """Detect existing JSON/TXT state files"""
    if not os.path.exists(state_dir):
        return []

    files = os.listdir(state_dir)
    # Exclude directories and hidden files
    state_files = [f for f in files if not f.startswith('.') and not os.path.isdir(os.path.join(state_dir, f))]
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

    # Create backup directory with validation
    backup_dir = os.path.join(STATE_DIR, f'state_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}')
    try:
        os.makedirs(backup_dir, exist_ok=True)
        # Verify directory is writable
        test_file = os.path.join(backup_dir, '.writable_test')
        with open(test_file, 'w') as f:
            f.write('test')
        os.remove(test_file)
    except Exception as e:
        print(f"✗ Cannot create backup directory {backup_dir}: {e}")
        return False

    try:
        # Migrate sessions
        for agent in SUPPORTED_AGENTS:
            session_file = os.path.join(STATE_DIR, f'{agent}_session.txt')
            if os.path.exists(session_file):
                with open(session_file, 'r') as f:
                    session_id = f.read().strip()
                if session_id:
                    db.save_session(agent, session_id)
                shutil.copy2(session_file, backup_dir)

        # Migrate context usage
        for agent in SUPPORTED_AGENTS:
            ctx_file = os.path.join(STATE_DIR, f'{agent}_ctx_chars.txt')
            if os.path.exists(ctx_file):
                try:
                    with open(ctx_file, 'r') as f:
                        content = f.read().strip()
                        chars = int(content) if content else 0
                    db.update_context_usage(agent, chars)
                    shutil.copy2(ctx_file, backup_dir)
                except ValueError as e:
                    print(f"⚠ Warning: {agent}_ctx_chars.txt has invalid integer, skipping: {e}")
                except Exception as e:
                    print(f"⚠ Warning: Failed to migrate {agent}_ctx_chars.txt: {e}")

        # Migrate model selections
        for agent in SUPPORTED_AGENTS:
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
            try:
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
            except json.JSONDecodeError as e:
                print(f"⚠ Warning: shared_context.json is corrupted, skipping: {e}")
            except Exception as e:
                print(f"⚠ Warning: Failed to migrate shared context: {e}")

        # Migrate memory
        memory_file = os.path.join(STATE_DIR, 'global_memory.json')
        if os.path.exists(memory_file):
            try:
                with open(memory_file, 'r') as f:
                    memory_data = json.load(f)
                db.save_memory(memory_data)
                shutil.copy2(memory_file, backup_dir)
            except json.JSONDecodeError as e:
                print(f"⚠ Warning: global_memory.json is corrupted, skipping: {e}")
            except Exception as e:
                print(f"⚠ Warning: Failed to migrate memory: {e}")

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

    except (IOError, json.JSONDecodeError, ValueError, TypeError) as e:
        print(f"✗ Migration failed: {e}")
        print(f"  Backup available in {backup_dir}")
        return False
    except Exception as e:
        print(f"✗ Unexpected error during migration: {e}")
        print(f"  Backup available in {backup_dir}")
        raise


if __name__ == '__main__':
    if migrate_json_to_sqlite():
        print("SQLite migration successful!")
    else:
        print("SQLite migration failed!")
