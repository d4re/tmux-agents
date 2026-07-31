"""Host-side state poll: for each tmux window, read its window->worktree
mapping, read the per-worktree state JSON Claude hooks wrote, emit a
one-letter display code, and print the status-line summary.

`agent-state` is wired into tmux's status-right (`#(agent-state)`), so
its stdout becomes the visible summary chunk. The `set-option`
subprocesses for per-window `@state_fg` are batched (`source-file -`)
and skipped entirely when the window/letter set hasn't changed since
the last tick — most ticks are no-ops on that path.
"""

from __future__ import annotations
import argparse
import dataclasses
import logging
import subprocess
import time
from pathlib import Path
from tmux_agents import (
    tmux,
    paths,
    phase,
    windows,
    state,
    theme,
    overview,
    registry,
    logging_setup,
)

logger = logging.getLogger(__name__)

# How long a window must stay gone before its mapping is deleted. Only needs to
# outlast a tmux shutdown (sub-second) with room for a stalled tick; kept short
# enough that a killed window's files don't linger noticeably.
_ORPHAN_GRACE_SECONDS = 90.0


def _read_session_id(worktree: Path, pane_id: str) -> str | None:
    f = paths.worktree_session_id_file(worktree, pane_id)
    try:
        sid = f.read_text().strip()
    except OSError:
        logger.debug("session id file unreadable for pane=%s", pane_id)
        return None
    # UUID-shape sanity check (matches the hook's sed validator).
    if len(sid) != 36 or any(c not in "0123456789abcdefABCDEF-" for c in sid):
        return None
    return sid


def _read_phase(state_file: Path) -> str:
    # The hook also writes `updated_at`; ignored today (reserved for the
    # "waiting duration in overview" BACKLOG item).
    j = paths.read_json_or(state_file, None)
    if not isinstance(j, dict):
        return phase.IDLE
    return j.get("phase", phase.IDLE)


def _prune_windows_and_worktree_files(live_ids: set[str], now: float) -> None:
    """Two-phase GC of mappings for windows that are no longer live.

    First tick a window is missing, its mapping is *tombstoned*
    (`orphaned_at`); only after `_ORPHAN_GRACE_SECONDS` of continued absence
    is it deleted along with the per-worktree state/pending files.

    The delay is what makes session restore survive shutdown. tmux tears an
    exiting session down window-by-window while the server (and therefore the
    status-line tick) is still running, so an eager prune sees every agent
    window vanish and deletes the entire snapshot moments before the server
    exits — leaving `agents` nothing to offer a restore for on the next
    start. Ticks stop when the server dies, so a tombstone written during
    shutdown is never followed by the delete."""
    d = paths.windows_dir()
    if not d.exists():
        return
    for f in d.glob("*.json"):
        if f.stem in live_ids:
            continue
        try:
            mapping = windows.read_mapping(f.stem)
        except KeyError:
            logger.debug(
                "malformed mapping file for window %s, skipping worktree cleanup",
                f.stem,
            )
            mapping = None
        if mapping is None:
            # Unreadable/malformed: nothing restorable in it, drop it now.
            f.unlink(missing_ok=True)
            continue
        if mapping.orphaned_at is None:
            windows.write_mapping(dataclasses.replace(mapping, orphaned_at=now))
            logger.info(
                "window %s gone; tombstoned, deleting in %.0fs",
                f.stem,
                _ORPHAN_GRACE_SECONDS,
            )
            continue
        if now - mapping.orphaned_at < _ORPHAN_GRACE_SECONDS:
            continue
        logger.info("window %s gone for %.0fs; pruning", f.stem, _ORPHAN_GRACE_SECONDS)
        windows.forget(f.stem)


def main(argv: list[str] | None = None) -> int:
    logging_setup.setup_logging()
    argparse.ArgumentParser(description="Run one state-polling tick").parse_args(argv)

    if not tmux.session_exists(tmux.SESSION):
        return 0

    palette = theme.get_palette()
    now = time.time()
    try:
        panes = tmux.window_pane_map(tmux.SESSION)
    except subprocess.CalledProcessError as e:
        # Transient tmux hiccup. Bail: an empty pane map would mark every
        # window X for one tick (mapping.pane_id wouldn't match any set).
        logger.warning(
            "window_pane_map failed: rc=%s stderr=%r stdout=%r",
            e.returncode,
            e.stderr,
            e.stdout,
        )
        return 0
    try:
        wins = tmux.list_windows(tmux.SESSION)
    except subprocess.CalledProcessError as e:
        # Transient tmux hiccup. Bail without writing or pruning — a partial
        # window list here would let the prune below wipe live mappings.
        logger.warning(
            "list_windows failed: rc=%s stderr=%r stdout=%r",
            e.returncode,
            e.stderr,
            e.stdout,
        )
        return 0
    live_ids = {w.id for w in wins}
    # If the windows_dir on disk has more mappings than live_ids has agent
    # windows, the GC below is about to tombstone (and eventually delete)
    # some — log a heads-up so we can correlate next time it fires
    # unexpectedly.
    try:
        existing = sorted(p.stem for p in paths.windows_dir().glob("*.json"))
    except OSError:
        logger.debug("windows dir unreadable, skipping prune-warning check")
        existing = []
    suspicious = [s for s in existing if s not in live_ids]
    if suspicious:
        logger.warning(
            "tick.start live_ids=%s existing=%s orphaned=%s",
            sorted(live_ids),
            existing,
            suspicious,
        )

    counts = overview.empty_counts()
    option_cmds: list[str] = []
    fingerprint_parts: list[str] = []

    for win in wins:
        if win.name == tmux.CONTROL_WINDOW:
            continue
        mapping = windows.read_mapping(win.id)
        if mapping is None:
            # A live window with no mapping shouldn't happen — surface as X
            # so the breakage is visible instead of letting a stale letter
            # persist on disk.
            letter = state.ERRORED
            overlay = 0
        else:
            sf = paths.worktree_state_file(mapping.host_worktree, mapping.pane_id)
            # A present state file (written by Claude's hooks) always wins. Only
            # when it's absent — e.g. during agent-new's pre-worktree startup —
            # do we fall back to the host-side phase_hint on the mapping.
            ph = _read_phase(sf) if sf.exists() else (mapping.phase_hint or phase.IDLE)
            counts_bz = registry.scan(mapping.host_worktree, mapping.pane_id, now=now)
            letter = phase.derive_letter(
                ph,
                b_count=counts_bz.background,
                z_count=counts_bz.sleeping,
                pane_alive=f"%{mapping.pane_id}" in panes.get(win.id, set()),
            )
            overlay = counts_bz.for_letter(letter)
        code = f"{letter}{overlay}" if overlay else letter
        counts[letter] += 1
        option_cmds.append(f'set-option -wt {win.id} @state_code "{code}"')
        option_cmds.append(f'set-option -wt {win.id} @state_fg "{palette.fg[letter]}"')
        option_cmds.append(
            f'set-option -wt {win.id} @state_selected_fg "{palette.selected_fg[letter]}"'
        )
        # `code` (not just letter) is in the fingerprint so a B2->B3 overlay
        # change still re-publishes @state_code (the option write is gated on it).
        fingerprint_parts.append(f"{win.id}:{win.name}:{win.index}:{code}")

        if mapping is None:
            continue
        # Merge captured session id and window_index into the mapping idempotently.
        sid = _read_session_id(mapping.host_worktree, mapping.pane_id)
        updates: dict = {}
        if sid is not None and sid != mapping.claude_session_id:
            updates["claude_session_id"] = sid
        if win.index != mapping.window_index:
            updates["window_index"] = win.index
        if mapping.orphaned_at is not None:
            # Window id reused by a new server before the GC fired; the
            # tombstone belongs to the previous occupant.
            updates["orphaned_at"] = None
        if updates:
            windows.write_mapping(dataclasses.replace(mapping, **updates))

    fingerprint = "|".join(sorted(fingerprint_parts))
    cache = paths.tick_cache()
    if paths.read_json_or(cache, None) != fingerprint:
        tmux.apply_commands(option_cmds)
        paths.atomic_write_json(cache, fingerprint)

    # No host-side .state files to clean up — the derived letter now lives in
    # the @state_code window option, which dies with its window.
    _prune_windows_and_worktree_files(live_ids, now)

    print(overview.render_summary(counts=counts), end="")
    return 0
