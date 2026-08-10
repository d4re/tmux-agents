"""Shared spawn/restore primitives used by both `agent-new` (async startup)
and `agent-restore`. These are the pieces common to placing a placeholder
pane, respawning it, writing per-pane state, and showing a static message —
the orchestration around them lives in commands/new.py and commands/restore.py.
"""

from __future__ import annotations
import logging
import os
import shlex
import shutil
import time
from pathlib import Path

from tmux_agents import paths, tmux

logger = logging.getLogger(__name__)

# Restoring/spawning several agents at once briefly stresses fork(); macOS can
# transiently refuse a pane spawn with "fork failed: Device not configured".
# The condition clears in well under a second, so a short bounded retry
# salvages the pane. Non-fork failures are real and re-raised immediately.
_FORK_RETRY_ATTEMPTS = 3
_FORK_RETRY_BACKOFF_S = 0.25


def placeholder_command(window_id: str) -> str:
    """Pre-create the log so tail never prints its "cannot open …: No such
    file or directory" / "has appeared" noise into the pane — the worker that
    fills the log in runs detached and may open it well after the pane is up.
    `tail -F` (not `-f`) still matters: it survives the worker re-creating
    the file."""
    log = paths.spawn_log(window_id)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.touch()
    return f"tail -F {log}"


def _respawn_with_retry(pane_id: str, command: str) -> None:
    for attempt in range(1, _FORK_RETRY_ATTEMPTS + 1):
        try:
            tmux.respawn_pane(pane_id, command=command)
            return
        except tmux.TmuxError as ex:
            transient = "fork failed" in (ex.stderr or "")
            if not transient or attempt == _FORK_RETRY_ATTEMPTS:
                raise
            logger.warning(
                "respawn pane=%s transient fork failure (attempt %d/%d), retrying: %s",
                pane_id,
                attempt,
                _FORK_RETRY_ATTEMPTS,
                ex,
            )
            time.sleep(_FORK_RETRY_BACKOFF_S)


def _detach_stdio(_os=os) -> None:
    """Redirect stdin/stdout/stderr to /dev/null so a backgrounded worker
    holds no inherited fds. Logging still works — setup_logging writes to a
    file, not stdout."""
    devnull = _os.open(_os.devnull, _os.O_RDWR)
    for fd in (0, 1, 2):
        _os.dup2(devnull, fd)
    if devnull > 2:
        _os.close(devnull)


def _write_pane_state(
    worktree: Path, pane_id_stripped: str, *, phase_value: str
) -> None:
    f = paths.worktree_state_file(worktree, pane_id_stripped)
    try:
        paths.atomic_write_json(f, {"phase": phase_value, "updated_at": ""})
    except OSError as ex:
        logger.warning(
            "pane-state write failed for %s pane=%s phase=%r: %s",
            worktree,
            pane_id_stripped,
            phase_value,
            ex,
        )


def show_static_text(pane_id: str, text: str) -> None:
    """Respawn `pane_id` into a static heredoc that prints `text` then
    idle-sleeps, so the message stays readable. Callers build their own
    message body (failure notice, etc.). Heredoc avoids escaping every shell
    metacharacter in `text`."""
    cmd = (
        'sh -c \'cat <<"EOF"'
        + text
        + 'EOF\nexec sh -c "while :; do sleep 3600; done"\''
    )
    try:
        tmux.respawn_pane(pane_id, command=cmd)
    except Exception:
        logger.warning(
            "show_static_text: respawn failed for %s", pane_id, exc_info=True
        )


def hold_pane_then_exec(pane_id: str, log_path: Path, exec_cmd: str) -> None:
    """Respawn `pane_id` to show the startup log plus a warning prompt, wait
    for the user to press Enter, then exec the real command. Per-window and
    non-modal — other windows are unaffected. Used when startup finished but
    emitted a non-fatal warning that would otherwise be wiped by respawning
    straight into Claude."""
    # `read` waits for Enter, then `exec_cmd` runs exactly as tmux would run it
    # on the no-warning path (`respawn-pane` hands the string to `sh -c`).
    # Do NOT prefix it with `exec`: every exec_cmd starts with `cd <workdir> &&`
    # (see config._HOST_ONLY_DEFAULT_EXEC_CMD), and `exec cd …` looks for an
    # external `cd` binary, fails 127, and kills the pane the instant the user
    # presses Enter. The `exec claude` inside exec_cmd already replaces the shell.
    inner = (
        f'cat "{log_path}"; '
        'printf "\\n\\n  startup finished with warnings — press Enter to launch Claude "; '
        "read _; "
        f"{exec_cmd}"
    )
    _respawn_with_retry(pane_id, f"sh -c {shlex.quote(inner)}")


def scrub_pane_files(worktree: Path, pane_id: str) -> None:
    """Delete a pane's state/session/pending files under the RESOLVED
    worktree. Callers hold the per-worktree cleanup lock. Required before
    launching an agent into a (possibly recycled) pane id — see the spec's
    aliasing guard."""
    paths.worktree_state_file(worktree, pane_id).unlink(missing_ok=True)
    paths.worktree_session_id_file(worktree, pane_id).unlink(missing_ok=True)
    shutil.rmtree(paths.worktree_pending_dir(worktree, pane_id), ignore_errors=True)


def pane_files_absent(worktree: Path, pane_id: str) -> bool:
    """True iff none of a pane's state/session/pending artifacts remain on
    disk under `worktree`. Used to verify a `scrub_pane_files` call actually
    completed before a caller clears a cleanup pointer that depends on
    it — `shutil.rmtree(ignore_errors=True)` can silently leave a survivor
    (e.g. a permission error), and that must not be mistaken for success."""
    return (
        not paths.worktree_state_file(worktree, pane_id).exists()
        and not paths.worktree_session_id_file(worktree, pane_id).exists()
        and not paths.worktree_pending_dir(worktree, pane_id).exists()
    )
