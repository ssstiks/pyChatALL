#!/usr/bin/env python3
"""
Настройка логирования для tg_agent.
"""

import logging
import logging.handlers
import traceback

from config import LOG_FILE, LOG_FILE_ERR

_logger: logging.Logger | None = None


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("tg_agent")
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    eh = logging.handlers.RotatingFileHandler(
        LOG_FILE_ERR, maxBytes=1 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    eh.setLevel(logging.WARNING)
    eh.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(eh)
    logger.propagate = False
    return logger


def _get_logger() -> logging.Logger:
    global _logger
    if _logger is None:
        _logger = _setup_logging()
    return _logger


def log(msg: str, level: str = "info") -> None:
    """Обратная совместимость: log('msg') → INFO, log('msg','error') → ERROR."""
    lvl = getattr(logging, level.upper(), logging.INFO)
    _get_logger().log(lvl, msg)


def log_debug(msg: str) -> None:
    _get_logger().debug(msg)


def log_info(msg: str) -> None:
    _get_logger().info(msg)


def log_warn(msg: str) -> None:
    _get_logger().warning(msg)


def log_error(msg: str, exc: Exception | None = None) -> None:
    if exc:
        _get_logger().error(f"{msg}: {exc}\n{traceback.format_exc()}")
    else:
        _get_logger().error(msg)


def _thread_excepthook(args: object) -> None:
    """Перехватывает необработанные исключения в потоках."""
    log_error(
        f"Unhandled exception in thread '{args.thread.name}'",  # type: ignore[attr-defined]
        args.exc_value,  # type: ignore[attr-defined]
    )
