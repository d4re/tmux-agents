"""Shared spawn/restore primitives used by both `agent-new` (async startup)
and `agent-restore`. These are the pieces common to placing a placeholder
pane, respawning it, writing per-pane state, and showing a static message —
the orchestration around them lives in commands/new.py and commands/restore.py.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import shlex
import shutil
import time
from pathlib import Path

from tmux_agents import locks, paths, tmux, windows

logger = logging.getLogger(__name__)

# Restoring/spawning several agents at once briefly stresses fork(); macOS can
# transiently refuse a pane spawn with "fork failed: Device not configured".
# The condition clears in well under a second, so a short bounded retry
# salvages the pane. Non-fork failures are real and re-raised immediately.
#
# The retry must NOT be another `respawn-pane` on the same pane. tmux <= 3.6a
# frees the pane's input context before forking and leaves it NULL when the
# fork fails; the next respawn of that pane calls input_free(NULL) and
# segfaults the whole server (upstream fixed it after 3.6a). Such a "poisoned"
# pane (pane_pid == -1) can only be killed, so `respawn` replaces it with a
# fresh split instead — a failed split removes its pane cleanly, so retrying
# the split is safe.
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


def _is_fork_failure(ex: tmux.TmuxError) -> bool:
    return "fork failed" in (ex.stderr or "")


def respawn(pane_id: str, command: str) -> str:
    """Respawn `pane_id` into `command` and return the id (incl. `%`) of the
    pane now running it. That is `pane_id` itself unless its spawn failed
    with a fork failure (now or earlier) — then the pane is poisoned and is
    replaced by a new pane (see `_FORK_RETRY_ATTEMPTS`), so callers must use
    the returned id for anything that follows."""
    info = tmux.pane_spawn_info(pane_id)
    if info is not None and info.pid == -1:
        logger.warning(
            "respawn pane=%s is poisoned by an earlier fork failure", pane_id
        )
        return _replace_poisoned_pane(pane_id, info, command)
    try:
        tmux.respawn_pane(pane_id, command=command)
        return pane_id
    except tmux.TmuxError as ex:
        if not _is_fork_failure(ex) or info is None:
            raise
        logger.warning(
            "respawn pane=%s fork failure, replacing the pane: %s", pane_id, ex
        )
    return _replace_poisoned_pane(pane_id, info, command)


def _replace_poisoned_pane(pane_id: str, info: tmux.PaneSpawnInfo, command: str) -> str:
    """Split a fresh pane off the poisoned `pane_id` running `command`, move
    the window mapping's slot (and the old pane's per-pane files) onto it,
    then kill the poisoned pane — whose space the new pane inherits. The
    mapping moves BEFORE the kill so the tick never sees the slot's pane
    vanish. Caller must not hold the worktree cleanup lock."""
    for attempt in range(1, _FORK_RETRY_ATTEMPTS + 1):
        try:
            new_pane = tmux.split_window(
                pane_id, percent=50, command=command, cwd=info.start_path
            )
            break
        except tmux.TmuxError as ex:
            if not _is_fork_failure(ex) or attempt == _FORK_RETRY_ATTEMPTS:
                raise
            logger.warning(
                "replace pane=%s fork failure (attempt %d/%d), retrying: %s",
                pane_id,
                attempt,
                _FORK_RETRY_ATTEMPTS,
                ex,
            )
            time.sleep(_FORK_RETRY_BACKOFF_S)
    _move_mapping_slot(info.window_id, pane_id.lstrip("%"), new_pane.lstrip("%"))
    tmux.kill_pane(pane_id)
    logger.info("replaced poisoned pane=%s with pane=%s", pane_id, new_pane)
    return new_pane


def _move_mapping_slot(window_id: str, old: str, new: str) -> None:
    """Point the mapping slot that owned stripped pane id `old` at `new`. A
    secondary slot the tick already death-marked (pane_id None, last_pane_id
    `old`) is revived onto `new` too. Per-pane files follow the slot: the
    new id is scrubbed (recycled-id aliasing guard), then the old pane's
    state + session-id files are renamed over, so a captured resume id
    survives. No mapping (e.g. agent-new before it writes one) → no-op."""
    mapping = windows.read_mapping(window_id)
    if mapping is None:
        return
    worktree = mapping.host_worktree
    with locks.locked(paths.worktree_cleanup_lock(worktree)):

        def fn(m):
            if m is None:
                return None
            agents = []
            hit = False
            for s in m.agents:
                if s.pane_id == old or (s.pane_id is None and s.last_pane_id == old):
                    s = dataclasses.replace(s, pane_id=new, last_pane_id=None)
                    hit = True
                agents.append(s)
            if not hit:
                return None
            pane = new if m.pane_id == old else m.pane_id
            return dataclasses.replace(m, pane_id=pane, agents=agents)

        if windows.update_mapping(window_id, fn) is None:
            return
        scrub_pane_files(worktree, new)
        for path_for in (paths.worktree_state_file, paths.worktree_session_id_file):
            src = path_for(worktree, old)
            if src.exists():
                src.replace(path_for(worktree, new))
        shutil.rmtree(paths.worktree_pending_dir(worktree, old), ignore_errors=True)


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


def show_static_text(pane_id: str, text: str) -> str:
    """Respawn `pane_id` into a static heredoc that prints `text` then
    idle-sleeps, so the message stays readable. Callers build their own
    message body (failure notice, etc.). Heredoc avoids escaping every shell
    metacharacter in `text`. Returns the pane now showing it (see `respawn`),
    or `pane_id` unchanged if that failed."""
    cmd = (
        'sh -c \'cat <<"EOF"'
        + text
        + 'EOF\nexec sh -c "while :; do sleep 3600; done"\''
    )
    try:
        return respawn(pane_id, cmd)
    except Exception:
        logger.warning(
            "show_static_text: respawn failed for %s", pane_id, exc_info=True
        )
        return pane_id


def hold_pane_then_exec(pane_id: str, log_path: Path, exec_cmd: str) -> str:
    """Respawn `pane_id` to show the startup log plus a warning prompt, wait
    for the user to press Enter, then exec the real command. Per-window and
    non-modal — other windows are unaffected. Used when startup finished but
    emitted a non-fatal warning that would otherwise be wiped by respawning
    straight into Claude. Returns the pane id it ended up in (see `respawn`)."""
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
    return respawn(pane_id, f"sh -c {shlex.quote(inner)}")


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
