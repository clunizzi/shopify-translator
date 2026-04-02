from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import structlog


_CONFIGURED = False


class _TeeWriter:
    def __init__(self, *streams) -> None:
        self._streams = streams

    def write(self, data: str) -> int:
        for stream in self._streams:
            stream.write(data)
            stream.flush()
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    json_logs = os.getenv("LOG_JSON", "true").lower() in {"1", "true", "yes", "y"}
    log_file = os.getenv("LOG_FILE", "logs/translator.jsonl").strip()

    log_output = sys.stdout
    file_handle = None
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handle = path.open("a", encoding="utf-8")
        if json_logs:
            log_output = _TeeWriter(sys.stdout, file_handle)

    shared_processors = [
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
    ]
    renderer = (
        structlog.processors.JSONRenderer(sort_keys=True)
        if json_logs
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            *shared_processors,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=log_output),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(level=level, stream=sys.stdout, force=True)
    _CONFIGURED = True
