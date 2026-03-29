"""File utilities for secure upload and tree traversal."""
import os
import logging
import zipfile
import shutil
from typing import List, Dict, Optional
from pathlib import Path

log = logging.getLogger(__name__)

# Maximum file size allowed (100 MB)
MAX_UPLOAD_SIZE = 100 * 1024 * 1024

# Allowed upload extensions
ALLOWED_UPLOAD_EXTS = {
    ".py", ".txt", ".json", ".md", ".log",
    ".zip", ".tar", ".gz", ".sql", ".db",
    ".go", ".js", ".ts", ".java", ".cpp", ".c",
    ".yaml", ".yml", ".toml", ".ini", ".conf",
    ".sh", ".bash", ".zsh", ".fish",
}


def is_safe_filename(filename: str) -> bool:
    """Check if filename is safe for upload."""
    # Reject absolute paths and path traversal attempts
    if filename.startswith("/") or ".." in filename:
        return False
    # Check for null bytes
    if "\x00" in filename:
        return False
    return True


def validate_upload_file(file_path: str) -> tuple[bool, str]:
    """
    Validate uploaded file.

    Args:
        file_path: Path to uploaded file

    Returns:
        Tuple (is_valid, reason)
    """
    if not os.path.exists(file_path):
        return False, "File not found"

    # Check file size
    file_size = os.path.getsize(file_path)
    if file_size > MAX_UPLOAD_SIZE:
        return False, f"File too large ({file_size} > {MAX_UPLOAD_SIZE})"

    # Check extension
    _, ext = os.path.splitext(file_path.lower())
    if ext and ext not in ALLOWED_UPLOAD_EXTS:
        return False, f"File type not allowed: {ext}"

    return True, "Valid"


def extract_zip(zip_path: str, extract_to: str) -> tuple[bool, str]:
    """
    Safely extract ZIP archive.

    Args:
        zip_path: Path to ZIP file
        extract_to: Destination directory

    Returns:
        Tuple (success, message)
    """
    try:
        os.makedirs(extract_to, exist_ok=True)

        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            # Validate all paths for traversal attacks
            for name in zip_ref.namelist():
                if name.startswith("/") or ".." in name:
                    return False, "Invalid path in archive"

            zip_ref.extractall(extract_to)

        return True, f"Extracted to {extract_to}"
    except Exception as e:
        log.error(f"ZIP extraction error: {e}")
        return False, str(e)


def build_file_tree(root_path: str, max_depth: int = 5, max_files: int = 1000) -> Dict:
    """
    Build recursive file tree structure.

    Args:
        root_path: Root directory path
        max_depth: Maximum recursion depth
        max_files: Maximum files to include

    Returns:
        Tree structure with files and directories
    """
    if not os.path.isdir(root_path):
        return {}

    tree = {
        "name": os.path.basename(root_path) or root_path,
        "path": root_path,
        "type": "directory",
        "children": []
    }

    file_count = [0]  # Use list to allow modification in nested function

    def traverse(current_path: str, current_depth: int) -> List[Dict]:
        if current_depth > max_depth or file_count[0] >= max_files:
            return []

        items = []
        try:
            entries = sorted(os.listdir(current_path))
        except PermissionError:
            return items

        for entry in entries:
            if file_count[0] >= max_files:
                break

            # Skip hidden files and common unneeded directories
            if entry.startswith(".") or entry in ("__pycache__", ".git", "node_modules"):
                continue

            full_path = os.path.join(current_path, entry)

            try:
                if os.path.isdir(full_path):
                    file_count[0] += 1
                    item = {
                        "name": entry,
                        "path": full_path,
                        "type": "directory",
                        "children": traverse(full_path, current_depth + 1) if current_depth < max_depth else []
                    }
                    items.append(item)
                else:
                    file_count[0] += 1
                    size = os.path.getsize(full_path)
                    item = {
                        "name": entry,
                        "path": full_path,
                        "type": "file",
                        "size": size
                    }
                    items.append(item)
            except (OSError, PermissionError):
                continue

        return items

    tree["children"] = traverse(root_path, 0)
    return tree


def get_file_tree_flat(root_path: str, max_files: int = 1000) -> List[str]:
    """
    Get flat list of files in directory tree.

    Args:
        root_path: Root directory path
        max_files: Maximum files to include

    Returns:
        List of relative file paths
    """
    files = []
    file_count = [0]

    for root, dirs, filenames in os.walk(root_path):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", ".git", "node_modules")]

        for filename in filenames:
            if file_count[0] >= max_files:
                return files

            if not filename.startswith("."):
                rel_path = os.path.relpath(os.path.join(root, filename), root_path)
                files.append(rel_path)
                file_count[0] += 1

    return sorted(files)


def cleanup_old_uploads(upload_dir: str, days: int = 7) -> int:
    """
    Clean up old uploaded files.

    Args:
        upload_dir: Upload directory path
        days: Files older than N days are deleted

    Returns:
        Number of files deleted
    """
    import time
    from pathlib import Path

    if not os.path.isdir(upload_dir):
        return 0

    cutoff_time = time.time() - (days * 24 * 3600)
    deleted = 0

    try:
        for file_path in Path(upload_dir).rglob("*"):
            if file_path.is_file():
                if os.path.getmtime(file_path) < cutoff_time:
                    try:
                        os.remove(file_path)
                        deleted += 1
                    except Exception as e:
                        log.error(f"Failed to delete {file_path}: {e}")
    except Exception as e:
        log.error(f"Cleanup error: {e}")

    return deleted


def read_file_safe(file_path: str, max_size: int = 1000000) -> Optional[str]:
    """
    Safely read file with size limit.

    Args:
        file_path: Path to file
        max_size: Maximum bytes to read

    Returns:
        File content or None if failed
    """
    try:
        if not os.path.exists(file_path):
            return None

        if os.path.getsize(file_path) > max_size:
            log.warning(f"File too large: {file_path}")
            return None

        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except Exception as e:
        log.error(f"File read error: {e}")
        return None
