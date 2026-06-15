"""Configurable application logging.

Call ``configure_logging(config)`` once at startup, then use
``logging.getLogger(__name__)`` in every module. Reconfiguring is safe:
handlers installed here are tagged and replaced, so repeated calls (tests,
notebooks, re-entrant startup) never stack duplicate output.

Stdlib only. No third-party dependencies.

Example
-------
    from logging_setup import configure_logging
    import logging

    configure_logging({
        "level": "DEBUG",
        "stdout": True,
        "file": "logs/app.log",
        "json": False,
        "quiet_loggers": {"urllib3": "WARNING", "asyncio": "WARNING"},
    })

    log = logging.getLogger(__name__)
    log.info("started", extra={"request_id": "abc123"})
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any
from collections.abc import Mapping
from contextlib import contextmanager

__all__ = [
    "LogConfig",
    "configure_logging",
    "JsonFormatter",
    "enable_debug_file",
    "disable_debug_file",
    "debug_file",
]

_DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DEFAULT_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"

# Marks handlers this module owns so reconfiguration can replace them without
# touching handlers the caller attached by hand.
_MANAGED = "_configure_logging_managed"

# Marks the on-the-fly debug handler so it can be found and removed on disable.
_DEBUG = "_configure_logging_debug"

# Attributes present on a bare LogRecord plus the two the formatter adds.
# Anything outside this set on a record is a caller-supplied `extra`.
_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per record. Caller `extra` fields are merged in."""

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.message,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        return json.dumps(payload, default=str, ensure_ascii=False)


class _MaxLevelFilter(logging.Filter):
    """Pass records strictly below `max_level`. Keeps stdout/stderr disjoint."""

    def __init__(self, max_level: int) -> None:
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self.max_level


@dataclass
class LogConfig:
    """Typed view of the config hash. Construct directly or via `from_dict`.

    level:            root level. str ("INFO") or int (logging.INFO).
    stdout:           emit to the console.
    stderr_threshold: records at/above this level go to stderr instead of
                      stdout; below it go to stdout. None sends everything to
                      stdout. Only meaningful when stdout is True.
    file:             path for a rotating file handler. None disables file logging.
    max_bytes:        rotate the file once it exceeds this size.
    backup_count:     number of rotated files to keep.
    json:             use JsonFormatter instead of the text format.
    fmt / datefmt:    text formatter strings (ignored when json is True).
    quiet_loggers:    {logger_name: level} to tame noisy dependencies.
    capture_warnings: route the warnings module through logging.
    """

    level: str | int = "INFO"
    stdout: bool = True
    stderr_threshold: str | int | None = "WARNING"
    file: str | Path | None = None
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5
    json: bool = False
    fmt: str = _DEFAULT_FORMAT
    datefmt: str = _DEFAULT_DATEFMT
    quiet_loggers: Mapping[str, str | int] = field(default_factory=dict)
    capture_warnings: bool = True

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LogConfig":
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(
                f"Unknown logging config keys: {sorted(unknown)}. "
                f"Valid keys: {sorted(known)}"
            )
        return cls(**dict(data))


def _resolve_level(level: str | int) -> int:
    if isinstance(level, bool):  # bool is an int subclass; reject it explicitly.
        raise ValueError(f"Invalid log level: {level!r}")
    if isinstance(level, int):
        return level
    if isinstance(level, str):
        resolved = logging.getLevelNamesMapping().get(level.upper())
        if resolved is not None:
            return resolved
    raise ValueError(f"Invalid log level: {level!r}")


def _clear_managed_handlers(logger: logging.Logger) -> None:
    for handler in [h for h in logger.handlers if getattr(h, _MANAGED, False)]:
        logger.removeHandler(handler)
        handler.close()


def _tag(handler: logging.Handler) -> logging.Handler:
    setattr(handler, _MANAGED, True)
    return handler


def configure_logging(
    config: Mapping[str, Any] | LogConfig | None = None,
    *,
    logger: logging.Logger | None = None,
) -> logging.Logger:
    """Configure and return a logger (the root logger by default).

    Accepts a plain config hash, a LogConfig, or None for defaults. Safe to
    call more than once: previously installed handlers are replaced, not stacked.
    """
    if config is None:
        config = LogConfig()
    elif isinstance(config, LogConfig):
        pass
    elif isinstance(config, Mapping):
        config = LogConfig.from_dict(config)
    else:
        raise TypeError(
            f"config must be a Mapping, LogConfig, or None, got {type(config).__name__}"
        )

    target = logger if logger is not None else logging.getLogger()
    level = _resolve_level(config.level)
    target.setLevel(level)

    _clear_managed_handlers(target)

    formatter: logging.Formatter = (
        JsonFormatter(datefmt=config.datefmt)
        if config.json
        else logging.Formatter(config.fmt, config.datefmt)
    )

    if config.stdout:
        stdout_handler = _tag(logging.StreamHandler(sys.stdout))
        stdout_handler.setFormatter(formatter)
        stdout_handler.setLevel(level)  # floor: stays put if the logger drops to DEBUG

        if config.stderr_threshold is not None:
            threshold = _resolve_level(config.stderr_threshold)
            stdout_handler.addFilter(_MaxLevelFilter(threshold))

            stderr_handler = _tag(logging.StreamHandler(sys.stderr))
            stderr_handler.setFormatter(formatter)
            stderr_handler.setLevel(threshold)
            target.addHandler(stderr_handler)

        target.addHandler(stdout_handler)

    if config.file:
        path = Path(config.file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = _tag(
            logging.handlers.RotatingFileHandler(
                path,
                maxBytes=config.max_bytes,
                backupCount=config.backup_count,
                encoding="utf-8",
            )
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)  # floor: keeps the main log clean in debug mode
        target.addHandler(file_handler)

    for name, lvl in config.quiet_loggers.items():
        logging.getLogger(name).setLevel(_resolve_level(lvl))

    if config.capture_warnings:
        logging.captureWarnings(True)

    return target


def enable_debug_file(
    path: str | Path,
    *,
    logger: logging.Logger | None = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 3,
    json: bool = False,
) -> logging.Handler:
    """Turn on a verbose DEBUG-level side log without touching the main outputs.

    Drops the logger to DEBUG so debug records start flowing, then attaches a
    rotating file handler at DEBUG to `path`. The handlers installed by
    configure_logging keep their own levels, so the standard log stays clean.

    Idempotent: calling it again swaps the target file rather than stacking
    handlers. Pair with disable_debug_file to restore the previous level.
    """
    target = logger if logger is not None else logging.getLogger()

    # Remember the level to restore. Guard so repeat calls keep the true floor.
    if not hasattr(target, _DEBUG + "_level"):
        setattr(target, _DEBUG + "_level", target.level)
    target.setLevel(logging.DEBUG)

    _remove_debug_handlers(target)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        JsonFormatter(datefmt=_DEFAULT_DATEFMT)
        if json
        else logging.Formatter(_DEFAULT_FORMAT, _DEFAULT_DATEFMT)
    )
    setattr(handler, _MANAGED, True)
    setattr(handler, _DEBUG, True)
    target.addHandler(handler)
    return handler


def disable_debug_file(*, logger: logging.Logger | None = None) -> None:
    """Remove the debug side log and restore the prior logger level."""
    target = logger if logger is not None else logging.getLogger()
    _remove_debug_handlers(target)
    saved = getattr(target, _DEBUG + "_level", None)
    if saved is not None:
        target.setLevel(saved)
        delattr(target, _DEBUG + "_level")


@contextmanager
def debug_file(path: str | Path, *, logger: logging.Logger | None = None, **kwargs):
    """Scoped debug logging: enable for the block, restore on exit (even on error)."""
    handler = enable_debug_file(path, logger=logger, **kwargs)
    try:
        yield handler
    finally:
        disable_debug_file(logger=logger)


def _remove_debug_handlers(logger: logging.Logger) -> None:
    for handler in [h for h in logger.handlers if getattr(h, _DEBUG, False)]:
        logger.removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    configure_logging({"level": "DEBUG", "stdout": True})
    log = logging.getLogger("demo")
    log.debug("debug to stdout")
    log.info("info to stdout", extra={"request_id": "abc123"})
    log.warning("warning to stderr")
    try:
        1 / 0
    except ZeroDivisionError:
        log.exception("error to stderr with traceback")
