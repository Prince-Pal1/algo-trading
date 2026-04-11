"""Structured logging via structlog + orjson.

Usage:
    from src.utils.logger import get_logger
    log = get_logger("binance_ws")
    log.info("connected", symbol="BTCUSDT", latency_ms=12)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import orjson
import structlog

from src.utils.config import get_config

_configured = False


def _orjson_serializer(data: dict, **_kw) -> str:
    return orjson.dumps(data).decode()


def setup_logging() -> None:
    """Configure structlog once at startup. Safe to call multiple times."""
    global _configured
    if _configured:
        return

    cfg = get_config()
    log_cfg = cfg.logging.get("structlog", {})
    level_name = cfg.log_level
    level = getattr(logging, level_name.upper(), logging.INFO)
    use_json = log_cfg.get("format", "json") == "json"

    # Ensure log directory exists
    log_path = log_cfg.get("file_path", "data/logs/trading.log")
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)

    # Standard library logging config
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]

    if log_cfg.get("file_output", False):
        from logging.handlers import RotatingFileHandler

        max_bytes = log_cfg.get("max_file_size_mb", 50) * 1024 * 1024
        backup_count = log_cfg.get("backup_count", 5)
        fh = RotatingFileHandler(log_path, maxBytes=max_bytes, backupCount=backup_count)
        handlers.append(fh)

    logging.basicConfig(format="%(message)s", handlers=handlers, level=level)

    # Structlog processors
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if use_json:
        renderer = structlog.processors.JSONRenderer(serializer=_orjson_serializer)
    else:
        renderer = structlog.dev.ConsoleRenderer()  # type: ignore[assignment]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Formatter for stdlib handlers
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    for h in logging.root.handlers:
        h.setFormatter(formatter)

    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a named logger. Calls setup_logging() if needed."""
    setup_logging()
    return structlog.get_logger(name)
