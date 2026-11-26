"""Background worktree removals queued by an interactive `agent-kill`.

The popup asks its questions (prune? force?), kills the window, and hands
the slow `git worktree remove` to a detached worker. Each pending removal
is one JSON record under `paths.removals_dir()`; the overview TUI lists
them as "removing worktree…" rows. Success deletes the record; failure
flips it to `failed` with the error, and the row stays until dismissed.

A `running` record whose worker is gone (crashed, machine slept through a
kill -9, …) is reported as failed on read, so a row can never spin forever.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from tmux_agents import paths, worktree

logger = logging.getLogger(__name__)

RUNNING = "running"
FAILED = "failed"

# A record that never got a worker pid within this window is treated as a
# worker that failed to launch.
_START_GRACE_S = 60.0


@dataclass
class Removal:
    key: str
    name: str  # window name at kill time, e.g. "api:feat-x"
    project: str
    branch: str
    repo: str
    force: bool = False
    leftover: bool = False  # husk dir: rmtree instead of `git worktree remove`
    container: str | None = None
    container_workdir: str | None = None
    container_user: str | None = None
    status: str = RUNNING
    error: str = ""
    pid: int | None = None
    started_at: float = field(default_factory=time.time)

    @property
    def path(self) -> Path:
        return Path(self.repo) / ".worktrees" / self.branch

    @classmethod
    def from_dict(cls, d: dict) -> Removal:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


def _file(key: str) -> Path:
    return paths.removals_dir() / f"{key}.json"


def new_key(window_id: str) -> str:
    return f"{window_id.lstrip('@')}-{time.time_ns()}"


def save(r: Removal) -> None:
    paths.atomic_write_json(_file(r.key), asdict(r))


def get(key: str) -> Removal | None:
    raw = paths.read_json_or(_file(key), None)
    if not isinstance(raw, dict):
        return None
    try:
        return Removal.from_dict(raw)
    except TypeError:
        logger.warning("removal record %s is malformed", key)
        return None


def dismiss(key: str) -> None:
    _file(key).unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _effective(r: Removal, now: float) -> Removal:
    """Display view: a running record with no live worker reads as failed."""
    if r.status != RUNNING:
        return r
    if r.pid is not None and not _pid_alive(r.pid):
        r.status, r.error = FAILED, "removal worker exited unexpectedly"
    elif r.pid is None and now - r.started_at > _START_GRACE_S:
        r.status, r.error = FAILED, "removal worker never started"
    return r


def list_all() -> list[Removal]:
    """Every pending/failed removal, oldest first."""
    d = paths.removals_dir()
    try:
        files = sorted(d.glob("*.json"))
    except OSError:
        return []
    now = time.time()
    out = [r for f in files if (r := get(f.stem)) is not None]
    return sorted((_effective(r, now) for r in out), key=lambda r: r.started_at)


def run(key: str) -> int:
    """Worker body: remove the worktree, then drop (success) or fail the record."""
    r = get(key)
    if r is None:
        logger.warning("removal %s: no record", key)
        return 2
    r.pid = os.getpid()
    save(r)
    logger.info("removal %s: removing %s (force=%s)", key, r.path, r.force)
    try:
        if r.leftover:
            worktree.remove_leftover(Path(r.repo), r.branch)
        else:
            worktree.remove(
                Path(r.repo),
                r.branch,
                force=r.force,
                container=r.container,
                container_workdir=r.container_workdir,
                container_user=r.container_user,
            )
    except Exception as e:  # noqa: BLE001 — any failure must land on the row
        logger.error("removal %s: %s", key, e)
        r.status, r.error = FAILED, str(e) or e.__class__.__name__
        save(r)
        return 1
    dismiss(key)
    logger.info("removal %s: done", key)
    return 0
