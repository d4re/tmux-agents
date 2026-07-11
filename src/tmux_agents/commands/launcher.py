"""`agents` entry point. Detects fresh tmux server with a stale snapshot
and orchestrates the restore handoff:

  fresh server + snapshot + user consents
    → move windows/ → windows.previous/
    → start tmux server detached
    → spawn `agent-restore --background`
    → execvp into `tmux attach`

Otherwise falls back to the legacy `tmux new-session -A` path."""
from __future__ import annotations
import logging
import os
import select
import shutil
import subprocess
import sys
from pathlib import Path

from tmux_agents import paths, tmux, logging_setup

logger = logging.getLogger(__name__)


_PROMPT_TIMEOUT_SECONDS = 5.0


def _snapshot_size() -> int:
    d = paths.windows_dir()
    if not d.exists():
        return 0
    return sum(1 for _ in d.glob("*.json"))


def _prompt_restore(count: int) -> bool:
    """Return True if the user consents to restoring `count` agents.
    Y/y/Enter/timeout/non-tty → True. Anything else → False."""
    msg = f"Restore {count} previous agents? [Y/n] ({int(_PROMPT_TIMEOUT_SECONDS)}s, default Y): "
    if not sys.stdin.isatty():
        sys.stderr.write(msg + "[non-interactive: Y]\n")
        return True
    sys.stderr.write(msg)
    sys.stderr.flush()
    rlist, _, _ = select.select([sys.stdin], [], [], _PROMPT_TIMEOUT_SECONDS)
    if not rlist:
        sys.stderr.write("\n[timed out: Y]\n")
        return True
    line = sys.stdin.readline().strip().lower()
    if line in {"", "y", "yes"}:
        return True
    return False


def _move_snapshot_aside() -> None:
    src = paths.windows_dir()
    dst = paths.windows_previous_dir()
    shutil.rmtree(dst, ignore_errors=True)  # clear stale leftover
    os.rename(src, dst)


def _spawn_restore_worker() -> None:
    subprocess.Popen(
        ["agent-restore", "--background"], start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def main() -> int:
    logging_setup.setup_logging()
    conf = paths.agents_conf()
    if not conf.exists():
        logging_setup.cli_error(logger, f"{conf} does not exist. Run install.sh first.")
        return 1

    if tmux.session_exists(tmux.SESSION):
        os.execvp("tmux", tmux.attach_argv())
        return 0  # unreachable

    snapshot_count = _snapshot_size()
    if snapshot_count == 0 or not _prompt_restore(snapshot_count):
        if snapshot_count > 0:
            shutil.rmtree(paths.windows_dir(), ignore_errors=True)
        os.execvp("tmux", tmux.legacy_new_session_argv(conf))
        return 0  # unreachable

    _move_snapshot_aside()
    tmux.start_server_detached_with_session(
        conf=conf, session=tmux.SESSION, window_name=tmux.CONTROL_WINDOW,
    )
    _spawn_restore_worker()
    os.execvp("tmux", tmux.attach_argv())
    return 0  # unreachable
