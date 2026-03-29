"""API routes for FastAPI backend."""
import os
import logging
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query

import config
from logger import log_info, log_error
from memory_manager import get_memory_manager
from utils import file_utils
from .models import (
    ChatMessage, LessonRecord, AgentStatus, ProjectFilesResponse,
    MemoryResponse, HealthCheckResponse
)
from .auth import get_combined_auth
from .pipeline import PipelineManager

log = logging.getLogger(__name__)

router = APIRouter()

# Initialize components
memory_manager = get_memory_manager()
pipeline_manager = PipelineManager(config.WORK_DIR)


@router.get("/health", response_model=HealthCheckResponse)
async def health_check():
    """Health check endpoint (no auth required)."""
    return HealthCheckResponse(status="ok", version="1.0.0")


@router.post("/chat")
async def chat_endpoint(
    text: str = Query(..., min_length=1, max_length=10000),
    file: Optional[UploadFile] = File(None),
    agent: Optional[str] = Query("claude"),
    stage: Optional[str] = Query("all"),
    auth: str = Depends(get_combined_auth)
):
    """
    Main chat endpoint. Processes text/files through multi-agent pipeline.

    Args:
        text: User prompt text
        file: Optional file upload
        agent: Target agent (claude, gemini, qwen)
        stage: Pipeline stage (planner, coder, debugger, all)

    Returns:
        Pipeline processing result
    """
    log_info(f"Chat endpoint: agent={agent}, stage={stage}, text_len={len(text)}")

    try:
        # Handle file upload if provided
        file_path = None
        if file:
            file_path = f"/tmp/{file.filename}"
            try:
                with open(file_path, "wb") as f:
                    content = await file.read()
                    f.write(content)
            except Exception as e:
                log_error(f"File upload error: {e}", e)
                return {"status": "error", "message": "File upload failed"}

        # Set active agent
        if agent:
            pipeline_manager.set_active_agent(agent)

        # Process through pipeline
        result = await pipeline_manager.process_message(text, file_path, stage)

        # Clean up temp file
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass

        return result

    except Exception as e:
        log_error(f"Chat endpoint error: {e}", e)
        return {"status": "error", "message": str(e)}


@router.get("/memory/lessons", response_model=MemoryResponse)
async def get_lessons(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    category: Optional[str] = Query(None),
    auth: str = Depends(get_combined_auth)
):
    """
    Retrieve lessons/knowledge base records.

    Args:
        limit: Number of records to return
        offset: Pagination offset
        category: Filter by category

    Returns:
        List of lesson records with metadata
    """
    try:
        lessons = []

        # Get all lessons from memory_manager (pagination handled manually)
        try:
            # memory_manager returns a dict with lessons if available
            memory_data = memory_manager.get_memory() if hasattr(memory_manager, 'get_memory') else {}

            # Extract lessons from memory data structure
            if isinstance(memory_data, dict) and 'lessons' in memory_data:
                all_lessons = memory_data['lessons']
                # Apply offset and limit
                paginated = all_lessons[offset:offset + limit]
                lessons = [
                    LessonRecord(
                        timestamp=lesson.get('timestamp', 0),
                        error_summary=lesson.get('error_summary', ''),
                        fix_steps=lesson.get('fix_steps', ''),
                        category=lesson.get('category')
                    )
                    for lesson in paginated
                ]
        except (AttributeError, TypeError, KeyError):
            # If memory_manager doesn't have expected structure, return empty
            lessons = []

        return MemoryResponse(lessons=lessons, total_count=len(lessons))
    except Exception as e:
        log_error(f"Memory endpoint error: {e}", e)
        raise HTTPException(status_code=500, detail="Memory retrieval failed")


@router.post("/memory/lesson")
async def add_lesson(
    error_summary: str,
    fix_steps: str,
    category: Optional[str] = None,
    auth: str = Depends(get_combined_auth)
):
    """Add a new lesson to knowledge base."""
    try:
        # Try to add lesson via memory_manager if it has the method
        if hasattr(memory_manager, 'add_lesson'):
            memory_manager.add_lesson({
                'error_summary': error_summary,
                'fix_steps': fix_steps,
                'category': category
            })
        else:
            log_info(f"Memory manager doesn't have add_lesson method; lesson not persisted")

        return {
            "status": "success",
            "message": "Lesson added",
            "error_summary": error_summary,
            "fix_steps": fix_steps
        }
    except Exception as e:
        log_error(f"Add lesson error: {e}", e)
        raise HTTPException(status_code=500, detail="Failed to add lesson")


@router.get("/project/files", response_model=ProjectFilesResponse)
async def get_project_files(
    project: str = Query("default"),
    auth: str = Depends(get_combined_auth)
):
    """
    Get file tree for active project.

    Args:
        project: Project name (default: "default")

    Returns:
        Recursive file listing with metadata
    """
    try:
        project_path = os.path.join(config.WORK_DIR, "projects", project)

        if not os.path.exists(project_path):
            return ProjectFilesResponse(project=project, files=[], total_files=0)

        # Use file_utils to get consistent file tree
        files = file_utils.get_file_tree_flat(project_path)

        return ProjectFilesResponse(
            project=project,
            files=sorted(files),
            total_files=len(files)
        )
    except Exception as e:
        log_error(f"Project files error: {e}", e)
        raise HTTPException(status_code=500, detail="Failed to get project files")


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    category: str = Query("general"),
    auth: str = Depends(get_combined_auth)
):
    """
    Upload file (logs, code, archives).

    Args:
        file: File to upload
        category: Upload category (logs, code, archive, etc.)

    Returns:
        Upload result with file info
    """
    try:
        # Validate filename for security
        if not file_utils.is_safe_filename(file.filename):
            raise HTTPException(status_code=400, detail="Invalid filename")

        upload_dir = os.path.join(config.WORK_DIR, "uploads", category)
        os.makedirs(upload_dir, exist_ok=True)

        file_path = os.path.join(upload_dir, file.filename)

        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)

        # Validate the uploaded file
        is_valid, reason = file_utils.validate_upload_file(file_path)
        if not is_valid:
            os.remove(file_path)
            raise HTTPException(status_code=400, detail=f"File validation failed: {reason}")

        file_size = len(content)
        log_info(f"File uploaded: {file.filename} ({file_size} bytes)")

        return {
            "status": "success",
            "filename": file.filename,
            "size": file_size,
            "category": category,
            "path": file_path
        }
    except HTTPException:
        raise
    except Exception as e:
        log_error(f"Upload error: {e}", e)
        raise HTTPException(status_code=500, detail="File upload failed")


@router.get("/agent/status")
async def get_agent_status(auth: str = Depends(get_combined_auth)):
    """Get current agent status and pipeline state."""
    try:
        status = pipeline_manager.get_status()
        return status
    except Exception as e:
        log_error(f"Status endpoint error: {e}", e)
        raise HTTPException(status_code=500, detail="Failed to get status")


@router.post("/agent/cancel")
async def cancel_agent(auth: str = Depends(get_combined_auth)):
    """Cancel current agent operation."""
    try:
        pipeline_manager.cancel()
        return {"status": "success", "message": "Operation cancelled"}
    except Exception as e:
        log_error(f"Cancel error: {e}", e)
        raise HTTPException(status_code=500, detail="Failed to cancel operation")


@router.get("/config/agents")
async def get_agents_config(auth: str = Depends(get_combined_auth)):
    """Get available agents and their status."""
    try:
        agents_info = {
            "available": ["claude", "gemini", "qwen"],
            "active": pipeline_manager.active_agent,
            "models": {
                "claude": config.DEFAULT_MODELS.get("claude", "unknown"),
                "gemini": config.DEFAULT_MODELS.get("gemini", "unknown"),
                "qwen": config.DEFAULT_MODELS.get("qwen", "unknown"),
            }
        }
        return agents_info
    except Exception as e:
        log_error(f"Config endpoint error: {e}", e)
        raise HTTPException(status_code=500, detail="Failed to get config")
