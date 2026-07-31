from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

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


def _json_default(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def _make_jsonl_file_writer(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a", encoding="utf-8")

    def _write_jsonl(logger, method_name, event_dict):
        handle.write(
            json.dumps(event_dict, ensure_ascii=False, sort_keys=True, default=_json_default) + "\n"
        )
        handle.flush()
        return event_dict

    return _write_jsonl


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    json_logs = os.getenv("LOG_JSON", "true").lower() in {"1", "true", "yes", "y"}
    log_file = os.getenv("LOG_FILE", "logs/translator.jsonl").strip()
    if log_file:
        is_lambda = bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME") or os.getenv("LAMBDA_TASK_ROOT"))
        if is_lambda and not os.path.isabs(log_file):
            log_file = str(Path("/tmp") / log_file)

    log_output = sys.stdout
    file_writer = None
    if log_file:
        file_writer = _make_jsonl_file_writer(Path(log_file))

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
            *([file_writer] if file_writer else []),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=log_output),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(level=level, stream=sys.stdout, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _CONFIGURED = True
