"""Process-wide logging setup. One RotatingFileHandler on the
`tmux_agents` logger writes to paths.state_dir()/tmux-agents.log.
Idempotent — re-entry is a no-op. CLI error helper writes to stderr
AND logs at ERROR via the caller's module logger."""
from __future__ import annotations
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

from tmux_agents import paths

_LOGGER_NAME = "tmux_agents"
_LOG_FILENAME = "tmux-agents.log"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3
_DATE_FMT = "%Y-%m-%dT%H:%M:%SZ"
_LINE_FMT = "%(asctime)s %(levelname)-7s pid=%(process)d %(name)s: %(message)s"


def log_file_path() -> Path:
    """Resolved path to the unified log file. Single source of truth for
    callers that need to point external processes at it (e.g. the SSH pump
    via TMUX_AGENTS_LOG_FILE)."""
    return paths.state_dir() / _LOG_FILENAME


def _resolve_level() -> tuple[int, str | None]:
    raw = os.environ.get("TMUX_AGENTS_LOG_LEVEL", "INFO").strip()
    name = raw.upper() if raw else "INFO"
    level = logging.getLevelName(name)
    if isinstance(level, int):
        return level, None
    return logging.INFO, f"unrecognized TMUX_AGENTS_LOG_LEVEL={raw!r}; using INFO"


def setup_logging(*, _max_bytes_override: int | None = None) -> None:
    root = logging.getLogger(_LOGGER_NAME)
    if root.handlers:
        return
    level, warn_msg = _resolve_level()
    path = log_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=_max_bytes_override if _max_bytes_override is not None else _MAX_BYTES,
        backupCount=_BACKUP_COUNT,
    )
    formatter = logging.Formatter(_LINE_FMT, datefmt=_DATE_FMT)
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    if warn_msg:
        root.warning(warn_msg)


def cli_error(logger: logging.Logger, msg: str, *, exc_info: bool = False) -> None:
    print(f"error: {msg}", file=sys.stderr)
    logger.error(msg, exc_info=exc_info)
