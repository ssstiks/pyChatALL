"""
async_core.py — Asyncio infrastructure for pyChatALL.

Single responsibility: provide the event loop reference and
thread-safe helpers for submitting work from the polling thread.
"""

import asyncio
import threading
from typing import Coroutine, Any

# Set once in main(), read by polling thread helpers
_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called once from main() before the polling thread starts."""
    global _loop
    with _loop_lock:
        _loop = loop


def get_loop() -> asyncio.AbstractEventLoop:
    """Return the running event loop. Raises if not yet set."""
    with _loop_lock:
        if _loop is None:
            raise RuntimeError("Event loop not initialised — call set_loop() first")
        return _loop


def submit(coro: Coroutine) -> "asyncio.Future[Any]":
    """
    Thread-safe: schedule a coroutine on the event loop from any thread.
    Returns a concurrent.futures.Future that resolves when coro completes.
    """
    return asyncio.run_coroutine_threadsafe(coro, get_loop())


def call_soon(fn, *args) -> None:
    """Thread-safe: schedule a zero-arg callback on the event loop."""
    get_loop().call_soon_threadsafe(fn, *args)
