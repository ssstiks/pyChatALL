#!/usr/bin/env python3
"""
FastAPI backend server for pyChatALL mobile application.

Entry point for the multi-agent orchestration system with support for:
- FastAPI REST API with JWT/API_KEY authentication
- Multi-agent pipeline (Claude, Gemini, Qwen)
- File upload and management
- Voice transcription (Whisper)
- Real-time agent status monitoring
"""

import os
import sys
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn
import time

# Ensure existing modules are importable
sys.path.insert(0, os.path.dirname(__file__))

import config
from logger import log_info, log_error, _setup_logging
from db_manager import Database
from api.routes import router as api_router

# Configure logging
_setup_logging()
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context for startup and shutdown events."""
    log_info("🚀 FastAPI server starting...")
    # Startup
    yield
    # Shutdown
    log_info("🛑 FastAPI server shutting down...")


# Create FastAPI application
app = FastAPI(
    title="pyChatALL Mobile API",
    description="Multi-agent orchestration backend for pyChatALL",
    version="1.0.0",
    lifespan=lifespan
)

# Configure CORS for mobile clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:*",
        "http://192.168.*",
        "http://10.*",
        "https://*",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Initialize database
try:
    db = Database(config.DB_PATH)
    log_info("Database initialized successfully")
except Exception as e:
    log_error(f"Database initialization failed: {e}", e)
    raise


# Request logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    api_key = request.headers.get("X-API-KEY", "—")
    auth = request.headers.get("Authorization", "—")
    body = b""
    try:
        body = await request.body()
    except Exception:
        pass

    print(f"\n{'='*60}")
    print(f"📱 REQUEST  {request.method} {request.url}")
    print(f"   IP:      {request.client.host}:{request.client.port}")
    print(f"   X-API-KEY: {api_key}")
    print(f"   Authorization: {auth}")
    if body:
        print(f"   Body: {body[:500].decode('utf-8', errors='replace')}")

    # Restore body for downstream
    async def receive():
        return {"type": "http.request", "body": body}
    request._receive = receive

    response = await call_next(request)
    elapsed = round((time.time() - start) * 1000)
    print(f"   → {response.status_code} ({elapsed}ms)")
    print(f"{'='*60}\n")
    return response


# Include API routes
app.include_router(api_router, prefix="/api/v1", tags=["API v1"])


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    """Custom HTTP exception handler."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": "error", "message": exc.detail}
    )


@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    """General exception handler."""
    log_error(f"Unhandled exception: {exc}", exc)
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": "Internal server error"}
    )


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": "pyChatALL Mobile API",
        "version": "1.0.0",
        "docs": "/docs",
        "endpoints": "/api/v1"
    }


@app.get("/api/v1/info")
async def api_info():
    """API information endpoint."""
    return {
        "name": "pyChatALL Mobile API",
        "version": "1.0.0",
        "agents": ["claude", "gemini", "qwen"],
        "database": config.DB_PATH,
        "work_dir": config.WORK_DIR,
        "features": [
            "chat",
            "memory",
            "file_management",
            "voice_transcription",
            "agent_monitoring"
        ]
    }


def main():
    """Main entry point."""
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", 8000))
    reload = os.getenv("API_RELOAD", "false").lower() == "true"

    log_info(f"Starting FastAPI server: {host}:{port}")
    log_info(f"Database: {config.DB_PATH}")
    log_info(f"Work directory: {config.WORK_DIR}")

    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=reload,
        log_level="info"
    )


if __name__ == "__main__":
    main()
