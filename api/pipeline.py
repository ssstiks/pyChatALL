"""Pipeline Manager for multi-agent orchestration (Planner -> Coder -> Debugger)."""
import asyncio
import logging
import mimetypes
import os
import threading
from typing import Optional, Dict, Any, List

from agents import ask_claude, ask_gemini, ask_qwen
from context import get_active, global_ctx_for_prompt, _load_session
from logger import log_info, log_error
import config

log = logging.getLogger(__name__)


class PipelineManager:
    """Manages orchestration of multi-agent pipeline."""

    def __init__(self, work_dir: str = None):
        """Initialize pipeline manager."""
        self.work_dir = work_dir or os.getcwd()
        self.active_agent = get_active()
        self.current_status = "idle"
        self.last_response = None

    def set_active_agent(self, agent: str) -> None:
        """Set the active agent for pipeline."""
        valid_agents = ["claude", "gemini", "qwen"]
        if agent not in valid_agents:
            raise ValueError(f"Invalid agent: {agent}. Must be one of {valid_agents}")
        self.active_agent = agent
        log_info(f"Active agent set to: {agent}")

    async def process_message(self,
                            text: str,
                            file_path: Optional[str] = None,
                            stage: str = "all") -> Dict[str, Any]:
        """
        Process message through pipeline stages.

        Args:
            text: Input text prompt
            file_path: Optional path to attached file
            stage: Pipeline stage - "planner", "coder", "debugger", or "all"

        Returns:
            Dictionary with response, status, and metadata
        """
        if not text.strip():
            return {"status": "error", "message": "Empty input"}

        log_info(f"Pipeline: Processing with agent={self.active_agent}, stage={stage}")

        try:
            self.current_status = "processing"

            # Check if agent has active session (Claude-specific optimization)
            skip_recent = False
            if self.active_agent.lower() == "claude":
                try:
                    session_sid = _load_session(config.CLAUDE_SESSION)
                    skip_recent = bool(session_sid)
                except Exception:
                    skip_recent = False

            # Get global context for prompt (aligned with agents.py logic)
            context = global_ctx_for_prompt(skip_recent=skip_recent)

            # Build full prompt with context and file info
            full_prompt = self._build_prompt(text, context, file_path, stage)

            # Route to appropriate agent
            response = await self._route_to_agent(full_prompt)

            self.last_response = response
            self.current_status = "idle"

            return {
                "status": "success",
                "agent": self.active_agent,
                "response": response,
                "stage": stage,
            }

        except Exception as e:
            log_error(f"Pipeline error: {e}", e)
            self.current_status = "error"
            return {"status": "error", "message": str(e)}

    def _build_prompt(self, text: str, context: str, file_path: Optional[str], stage: str) -> str:
        """
        Build full prompt with context and stage-specific instructions.
        Aligned with agents.py _run_cli() prompt building logic.
        """
        parts = []

        # Add context if available
        if context:
            parts.append(f"[Контекст диалога:\n{context}\n]")

        # Handle file attachment (similar to agents.py logic)
        if file_path and os.path.exists(file_path):
            try:
                rel_path = os.path.relpath(file_path, config.WORK_DIR)
            except ValueError:
                rel_path = file_path

            mime = mimetypes.guess_type(file_path)[0] or ""
            if "image" in mime:
                parts.append(
                    f"[Изображение: {rel_path}]\n"
                    f"Используй инструмент read_file или Read чтобы просмотреть изображение "
                    f"и ответить на вопрос пользователя."
                )
            else:
                parts.append(
                    f"[Файл: {rel_path}]\n"
                    f"Прочитай содержимое файла и ответь на вопрос пользователя."
                )

        # Add stage markers if specified
        if stage != "all":
            text = f"[{stage.upper()} STAGE]\n{text}"

        # Add main prompt
        parts.append(f"Вопрос: {text}" if (context or file_path) else text)

        full_prompt = "\n\n".join(parts)
        return full_prompt

    async def _route_to_agent(self, prompt: str) -> str:
        """Route prompt to active agent and get response."""
        agent = self.active_agent.lower()

        try:
            if agent == "claude":
                return await self._call_agent_async(ask_claude, prompt)
            elif agent == "gemini":
                return await self._call_agent_async(ask_gemini, prompt)
            elif agent == "qwen":
                return await self._call_agent_async(ask_qwen, prompt)
            else:
                raise ValueError(f"Unknown agent: {agent}")
        except Exception as e:
            log_error(f"Agent routing error: {e}", e)
            raise

    async def _call_agent_async(self, agent_func, prompt: str) -> str:
        """Call agent function in thread pool to avoid blocking with timeout protection."""
        loop = asyncio.get_event_loop()
        timeout = config._AGENT_TIMEOUT.get(self.active_agent, 300)

        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, agent_func, prompt),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            raise TimeoutError(f"Agent {self.active_agent} exceeded timeout of {timeout} seconds")

    def get_status(self) -> Dict[str, Any]:
        """Get current pipeline status."""
        return {
            "status": self.current_status,
            "agent": self.active_agent,
            "last_response": self.last_response[:200] if self.last_response else None,
        }

    def cancel(self) -> None:
        """Cancel current operation."""
        from agents import cancel_active_proc
        try:
            cancel_active_proc()
            self.current_status = "cancelled"
            log_info("Pipeline operation cancelled")
        except Exception as e:
            log_error(f"Error cancelling pipeline: {e}", e)
