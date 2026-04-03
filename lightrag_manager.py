"""
lightrag_manager.py — Knowledge-graph memory layer for pyChatALL.

Stores conversation context in a LightRAG graph+vector store.
Replaces flat global_memory.json dump with semantic retrieval.

Architecture:
  - ONE persistent asyncio event loop in a daemon thread handles all LightRAG ops.
  - LightRAG internal worker queues are created in that loop at init time and reused.
  - All public API calls submit coroutines to that loop via run_coroutine_threadsafe().

Insert: background thread after each Shadow Librarian update (non-blocking).
Query:  called from global_ctx_for_prompt() with a hard 10s timeout.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading

log = logging.getLogger(__name__)

LIGHTRAG_DIR  = os.path.expanduser("~/.local/share/pyChatALL/lightrag")
EMBED_MODEL   = "paraphrase-multilingual-MiniLM-L12-v2"
OLLAMA_MODEL  = "minimax-m2.7:cloud"
OLLAMA_URL    = "http://localhost:11434"

_QUERY_TIMEOUT  = 10   # seconds
_INSERT_TIMEOUT = 120  # seconds

# ── Dedicated event loop ─────────────────────────────────────────────────────
_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    """Return (or create) the single dedicated LightRAG event loop."""
    global _loop
    if _loop is not None:
        return _loop
    with _loop_lock:
        if _loop is None:
            loop = asyncio.new_event_loop()

            def _run():
                asyncio.set_event_loop(loop)
                loop.run_forever()

            t = threading.Thread(target=_run, daemon=True, name="lightrag-loop")
            t.start()
            _loop = loop
    return _loop


def _submit(coro, timeout: float):
    """Submit coroutine to the dedicated loop and wait for result."""
    loop = _get_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        fut.cancel()
        log.warning("LightRAG operation timed out after %.0fs", timeout)
        return None
    except Exception as e:
        log.warning("LightRAG error: %s", e)
        return None


# ── RAG instance ─────────────────────────────────────────────────────────────
_rag      = None
_rag_lock = threading.Lock()
_rag_ready = False
_rag_ok    = False


def _build_embed_func():
    from sentence_transformers import SentenceTransformer
    from lightrag.utils import EmbeddingFunc

    _model = SentenceTransformer(EMBED_MODEL)

    async def _embed(texts: list[str]):
        # Returns numpy array — LightRAG expects .size attribute (not a plain list)
        return _model.encode(texts, normalize_embeddings=True)

    return EmbeddingFunc(embedding_dim=384, max_token_size=512, func=_embed)


def _init_rag_sync() -> bool:
    """Blocking init. Must be called from a background thread."""
    global _rag, _rag_ready, _rag_ok
    try:
        from lightrag import LightRAG
        from lightrag.llm.ollama import ollama_model_complete

        os.makedirs(LIGHTRAG_DIR, exist_ok=True)

        # Create LightRAG instance — worker queues will bind to _get_loop()
        # when initialize_storages() is called in that loop below.
        rag = LightRAG(
            working_dir=LIGHTRAG_DIR,
            llm_model_func=ollama_model_complete,
            llm_model_name=OLLAMA_MODEL,
            llm_model_kwargs={"host": OLLAMA_URL},
            embedding_func=_build_embed_func(),
            top_k=20,
            chunk_top_k=10,
            max_total_tokens=8000,
            summary_context_size=6000,
            chunk_token_size=800,
            chunk_overlap_token_size=80,
            auto_manage_storages_states=True,
        )

        # Initialize storages IN the dedicated loop
        _submit(rag.initialize_storages(), timeout=30)

        with _rag_lock:
            _rag = rag
            _rag_ready = True
            _rag_ok = True
        log.info("LightRAG ready at %s", LIGHTRAG_DIR)
        return True
    except Exception as e:
        log.warning("LightRAG init failed: %s", e)
        with _rag_lock:
            _rag_ready = True
            _rag_ok = False
        return False


def init_background() -> None:
    """Start LightRAG init in a daemon thread. Call once at bot startup."""
    threading.Thread(
        target=_init_rag_sync, daemon=True, name="lightrag-init"
    ).start()


# ── Public API ───────────────────────────────────────────────────────────────

def rag_insert(text: str) -> None:
    """Insert text into LightRAG (blocking). Call from a background thread."""
    if not text.strip():
        return
    # Wait for init (up to 90s)
    import time
    t0 = time.monotonic()
    while not _rag_ready:
        if time.monotonic() - t0 > 90:
            log.debug("LightRAG not ready after 90s, skipping insert")
            return
        time.sleep(0.5)
    if not _rag_ok or _rag is None:
        return
    try:
        _submit(_rag.ainsert(text), timeout=_INSERT_TIMEOUT)
        log.debug("LightRAG inserted %d chars", len(text))
    except Exception as e:
        log.warning("LightRAG insert error: %s", e)


def rag_insert_background(text: str) -> None:
    """Non-blocking insert — fire and forget."""
    threading.Thread(
        target=rag_insert, args=(text,), daemon=True, name="lightrag-insert"
    ).start()


def rag_query(query: str, mode: str = "mix") -> str:
    """
    Retrieve relevant context for a query string.
    Uses only_need_context=True — returns raw chunks, no LLM generation.
    Returns empty string if LightRAG unavailable or times out.
    """
    if not query.strip() or not _rag_ready or not _rag_ok or _rag is None:
        return ""
    try:
        from lightrag import QueryParam
        result = _submit(
            _rag.aquery(
                query,
                param=QueryParam(
                    mode=mode,
                    only_need_context=True,
                    top_k=10,
                    chunk_top_k=5,
                    max_total_tokens=2000,
                ),
            ),
            timeout=_QUERY_TIMEOUT,
        )
        text = (result or "").strip()
        # Suppress empty-graph noise
        if not text or text.upper().startswith("FAILURE") or len(text) < 20:
            return ""
        return text[:2000]
    except Exception as e:
        log.warning("LightRAG query error: %s", e)
        return ""
