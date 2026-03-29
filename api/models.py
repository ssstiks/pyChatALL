"""Pydantic models for API endpoints."""
from typing import List, Optional
from pydantic import BaseModel


class ChatMessage(BaseModel):
    """Chat message model."""
    text: str
    timestamp: Optional[float] = None
    agent: Optional[str] = None
    status: Optional[str] = None


class LessonRecord(BaseModel):
    """Lesson/memory record from knowledge base."""
    id: Optional[int] = None
    timestamp: float
    error_summary: str
    fix_steps: str
    category: Optional[str] = None


class AgentStatus(BaseModel):
    """Agent status model for real-time monitoring."""
    agent_name: str
    status: str  # "idle", "processing", "error", "success"
    elapsed_seconds: float
    progress_message: Optional[str] = None


class FileNode(BaseModel):
    """File tree node for project file listing."""
    name: str
    path: str
    is_dir: bool
    size: Optional[int] = None
    children: Optional[List["FileNode"]] = None


FileNode.model_rebuild()


class ProjectFilesResponse(BaseModel):
    """Response model for project files endpoint."""
    project: str
    files: List[str]
    total_files: int


class MemoryResponse(BaseModel):
    """Response model for memory/lessons endpoint."""
    lessons: List[LessonRecord]
    total_count: int


class HealthCheckResponse(BaseModel):
    """Health check response."""
    status: str
    version: str
    backend: str = "FastAPI"
