"""fcntl file locks. Global order when both are needed: per-worktree
cleanup lock FIRST, per-window mapping lock SECOND — and the mapping lock
is only ever taken inside windows.update_mapping/delete_mapping, which
never acquire anything else while holding it."""

from __future__ import annotations
import fcntl
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def locked(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
