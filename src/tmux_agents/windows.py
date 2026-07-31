"""Window->worktree mapping files used by the host-side state tick.

Each tmux window created by `agent-new` has a JSON file at
~/.config/tmux-agents/windows/<window_id>.json that records the project,
branch, host-side worktree path, and pane id. The tick reads these to
locate per-worktree state JSON files written by Claude hooks.
"""

from __future__ import annotations
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from tmux_agents import paths

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WindowMapping:
    window_id: str
    project: str
    branch: str | None
    host_worktree: Path
    pane_id: str
    claude_session_id: str | None = None
    window_index: int | None = None
    phase_hint: str | None = None
    # Epoch seconds when the tick first saw this window gone. Tombstone for the
    # deferred GC in state_tick — see `forget` and `_prune_windows_and_worktree_files`.
    orphaned_at: float | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "project": self.project,
            "branch": self.branch,
            "host_worktree": str(self.host_worktree),
            "pane_id": self.pane_id,
        }
        if self.claude_session_id is not None:
            d["claude_session_id"] = self.claude_session_id
        if self.window_index is not None:
            d["window_index"] = self.window_index
        if self.phase_hint is not None:
            d["phase_hint"] = self.phase_hint
        if self.orphaned_at is not None:
            d["orphaned_at"] = self.orphaned_at
        return d

    @classmethod
    def from_dict(cls, window_id: str, d: dict) -> "WindowMapping":
        return cls(
            window_id=window_id,
            project=d["project"],
            branch=d.get("branch"),
            host_worktree=Path(d["host_worktree"]),
            pane_id=d["pane_id"],
            claude_session_id=d.get("claude_session_id"),
            window_index=d.get("window_index"),
            phase_hint=d.get("phase_hint"),
            orphaned_at=d.get("orphaned_at"),
        )


def window_name(project: str, branch: str | None) -> str:
    """tmux window name for an agent: 'project:branch' or just 'project'."""
    return f"{project}:{branch}" if branch else project


def write_mapping(m: WindowMapping) -> None:
    paths.atomic_write_json(paths.window_mapping_file(m.window_id), m.to_dict())


def read_mapping(window_id: str) -> WindowMapping | None:
    d = paths.read_json_or(paths.window_mapping_file(window_id), None)
    if d is None:
        return None
    return WindowMapping.from_dict(window_id, d)


def forget(window_id: str) -> None:
    """Delete a window's mapping plus the per-pane files it points at.

    This is what removes an agent from the restore snapshot, so it must only
    run when the window is *deliberately* gone — `agent-kill`, or the tick's
    deferred GC once the tombstone grace period has elapsed. Calling it the
    moment a window disappears would wipe the snapshot during tmux shutdown,
    when every pane dies a few ticks before the server does."""
    try:
        m = read_mapping(window_id)
    except KeyError:
        logger.debug("malformed mapping for %s, skipping worktree cleanup", window_id)
        m = None
    if m is not None:
        for f in (
            paths.worktree_state_file(m.host_worktree, m.pane_id),
            paths.worktree_session_id_file(m.host_worktree, m.pane_id),
        ):
            f.unlink(missing_ok=True)
        shutil.rmtree(
            paths.worktree_pending_dir(m.host_worktree, m.pane_id), ignore_errors=True
        )
    paths.window_mapping_file(window_id).unlink(missing_ok=True)


def live_branches_for(project: str) -> set[str]:
    """Set of branch names that have a live agent window for `project`.

    Reads every `<config_dir>/windows/<window_id>.json` mapping, intersects
    with the set of window ids reported by `tmux.list_windows`, and returns
    branches whose mapping matches `project`. Mappings with `branch=None`
    are excluded — there is no branch to compare against in the picker."""
    from tmux_agents import (
        tmux,
    )  # local: keeps windows.py importable without tmux side effects

    try:
        live_ids = {w.id for w in tmux.list_windows(tmux.SESSION)}
    except Exception:
        logger.warning("live_branches_for: tmux.list_windows failed", exc_info=True)
        live_ids = set()
    branches: set[str] = set()
    wd = paths.windows_dir()
    if not wd.exists():
        return branches
    for entry in wd.iterdir():
        if entry.suffix != ".json":
            continue
        window_id = entry.stem
        if window_id not in live_ids:
            continue
        m = read_mapping(window_id)
        if m is None or m.project != project or m.branch is None:
            continue
        branches.add(m.branch)
    return branches
