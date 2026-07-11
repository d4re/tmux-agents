"""Filesystem locations for config, state, per-worktree, and per-window
data. Env-overridable (`TMUX_AGENTS_CONFIG_DIR` / `TMUX_AGENTS_STATE_DIR`)
so tests redirect — every path used elsewhere should come from here."""

from __future__ import annotations
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def state_dir() -> Path:
    env = os.environ.get("TMUX_AGENTS_STATE_DIR")
    return Path(env) if env else Path("/tmp/tmux-agents")


def config_dir() -> Path:
    env = os.environ.get("TMUX_AGENTS_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".config" / "tmux-agents"


def projects_toml() -> Path:
    return config_dir() / "projects.toml"


def agents_conf() -> Path:
    return config_dir() / "agents.conf"


def layout_file() -> Path:
    return state_dir() / "layout"


def read_layout() -> str:
    """Persisted layout mode ('split' or 'compact'); 'split' if absent or invalid."""
    try:
        v = layout_file().read_text().strip()
    except (FileNotFoundError, OSError):
        logger.debug("layout file missing/unreadable, defaulting to split")
        return "split"
    return v if v in {"split", "compact"} else "split"


def theme_toml() -> Path:
    return config_dir() / "theme.toml"


def windows_dir() -> Path:
    return config_dir() / "windows"


def window_mapping_file(window_id: str) -> Path:
    return windows_dir() / f"{window_id}.json"


def worktree_state_file(worktree: Path, pane_id: str) -> Path:
    return worktree / ".local" / ".tmux-agents" / f"state-{pane_id}.json"


def worktree_pending_dir(worktree: Path, pane_id: str) -> Path:
    """Directory of self-expiring background/scheduled marker files for a pane.
    Markers are named '<kind>__<id>' (or 'wakeup' for the singleton); the host
    tick scans them via tmux_agents.registry."""
    return worktree / ".local" / ".tmux-agents" / f"pending-{pane_id}"


def folds_file() -> Path:
    return state_dir() / "overview-folds.json"


def windows_previous_dir() -> Path:
    # Snapshot staging during restore: launcher populates, agent-restore drains.
    return config_dir() / "windows.previous"


def worktree_session_id_file(worktree: Path, pane_id: str) -> Path:
    # Stripped pane id (no `%`), matching state-<pane>.json keying.
    return worktree / ".local" / ".tmux-agents" / f"session-{pane_id}.id"


def spawn_log(window_id: str) -> Path:
    """Per-window startup log. The placeholder pane `tail -F`s this file while
    `agent-new --provision` / `agent-restore` write stage progress to it."""
    return state_dir() / f"spawn-{window_id}.log"


def tick_cache() -> Path:
    """Per-tick fingerprint of the window/letter set. Used to skip
    redundant tmux set-option subprocess calls when nothing changed."""
    return state_dir() / "tick.cache"


def read_json_or(path: Path, default: Any) -> Any:
    """Load JSON from `path`; return `default` if missing or unparseable."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        logger.debug("json missing/unparseable at %s", path)
        return default


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    indent: int | None = None,
    sort_keys: bool = False,
) -> None:
    """Atomically write `data` as JSON to `path` via a sibling `.tmp` rename.
    Creates the parent directory. Trailing newline only when indented."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps(data, indent=indent, sort_keys=sort_keys)
    if indent is not None:
        text += "\n"
    tmp.write_text(text)
    tmp.replace(path)
