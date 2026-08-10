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
    locks,
    startup,
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


def _mark_secondary_dead(mapping: "windows.WindowMapping", slot) -> None:
    """One guarded compare-and-set transaction: only mutate the slot if it
    still holds the observed (kind, pane_id) — guards against a revival that
    respawned the pane between the tick's read and this call. Cleanup lock
    first (files are deleted below), mapping lock second, inside
    `update_mapping`. Session id is merged from disk into the slot BEFORE
    nulling `pane_id`, so a captured `SessionStart` id isn't lost.

    The tick's pane-liveness snapshot (`panes = tmux.window_pane_map(...)`)
    predates this call — a revive that publishes a fresh pane between that
    snapshot and this lock acquisition would otherwise still look dead here.
    Re-verify with a fresh, single-pane tmux query right after taking the
    lock; a live pane aborts before the CAS runs at all (belt-and-braces
    with the CAS itself, which also catches a revival that lands after this
    check but before `update_mapping`'s own read)."""
    observed = (slot.kind, slot.pane_id)
    with locks.locked(paths.worktree_cleanup_lock(mapping.host_worktree)):
        if tmux.pane_alive(mapping.window_id, f"%{observed[1]}"):
            return

        def fn(m):
            if m is None:
                return None
            new_agents = []
            hit = False
            for s in m.agents:
                if (s.kind, s.pane_id) == observed:
                    sid = _read_session_id(m.host_worktree, s.pane_id) or s.session_id
                    s = dataclasses.replace(
                        s, pane_id=None, session_id=sid, last_pane_id=s.pane_id
                    )
                    hit = True
                new_agents.append(s)
            return dataclasses.replace(m, agents=new_agents) if hit else None

        updated = windows.update_mapping(mapping.window_id, fn)
        if updated is not None:
            startup.scrub_pane_files(mapping.host_worktree, observed[1])


def _read_agent_mappings(
    wins: list["tmux.Window"],
) -> dict[str, "windows.WindowMapping"]:
    """Read every non-control window's mapping once. Shared by the tick's
    single upfront read (in `main`) and `_sweep_cleanup_pointers`'s fresh
    re-read under the cleanup lock."""
    out: dict[str, windows.WindowMapping] = {}
    for win in wins:
        if win.name == tmux.CONTROL_WINDOW:
            continue
        m = windows.read_mapping(win.id)
        if m is not None:
            out[win.id] = m
    return out


def _worktree_live_pane_index(
    mappings: dict[str, "windows.WindowMapping"],
) -> dict[Path, set[str]]:
    """{host_worktree: {pane ids claimed by any slot of any window's
    mapping}} — the cross-window alias index, since re-running agent-new on
    an open branch can map the same worktree into a second window."""
    idx: dict[Path, set[str]] = {}
    for m in mappings.values():
        ids = idx.setdefault(m.host_worktree, set())
        for slot in m.agents:
            if slot.pane_id:
                ids.add(slot.pane_id)
    return idx


def _sweep_cleanup_pointers(window_id: str, mapping: "windows.WindowMapping") -> None:
    """Retry pending per-pane deletions for every slot carrying a
    `last_pane_id` (live or dead).

    `mapping` (the tick's upfront snapshot) is only a fast-path pre-check —
    skip taking the lock at all when nothing is pending. Once the cleanup
    lock is held, the mapping and the same-worktree mapped-pane index are
    re-derived fresh: a spawn that scrubs+publishes a recycled id between
    the tick's upfront snapshot and this lock acquisition must not be
    mistaken for a still-dead pane.

    Two different resolutions for a colliding `last_pane_id`, per slot:
    - **Mapped elsewhere** (`lp` claimed by any slot of the same worktree,
      live or dead — a second window can map the same worktree): the
      on-disk files now belong to THAT slot. Clear only our pointer via
      compare-and-set and delete nothing — no ownership dispute, nothing to
      scrub.
    - **Unmapped but tmux-alive**: genuinely still ambiguous (the mapping
      publish that would explain the liveness hasn't landed yet) — skip
      entirely this tick, retry later. Checked against the fresh
      SESSION-WIDE pane set (flattened `tmux.window_pane_map`), not just
      `window_id`'s own panes: pane ids are server-global, so a live
      unmapped pane sitting in a *different* window (e.g. a mid-agent-other
      crash artifact) must still defer this slot.

    Otherwise, scrub and verify via `startup.pane_files_absent` before
    clearing the pointer: a `scrub_pane_files` call that silently left a
    survivor (e.g. `rmtree` swallowing a permission error) must not lose
    its retry by having the pointer cleared anyway."""
    if not any(s.last_pane_id for s in mapping.agents):
        return
    with locks.locked(paths.worktree_cleanup_lock(mapping.host_worktree)):
        fresh_mapping = windows.read_mapping(window_id)
        if fresh_mapping is None:
            return
        pending = [s for s in fresh_mapping.agents if s.last_pane_id]
        if not pending:
            return
        try:
            fresh_wins = tmux.list_windows(tmux.SESSION)
            fresh_panes = tmux.window_pane_map(tmux.SESSION)
        except subprocess.CalledProcessError:
            logger.warning(
                "sweep: fresh tmux query failed for %s, deferring",
                window_id,
                exc_info=True,
            )
            return
        fresh_worktree_ids = _worktree_live_pane_index(
            _read_agent_mappings(fresh_wins)
        ).get(fresh_mapping.host_worktree, set())
        # Session-wide (not window-scoped) liveness: pane ids are
        # server-global, so a live unmapped pane parked in a different
        # window must still defer this pointer's cleanup.
        session_live_ids = {
            p.lstrip("%") for pane_set in fresh_panes.values() for p in pane_set
        }
        for slot in pending:
            lp = slot.last_pane_id

            def clear(m, *, _kind=slot.kind, _lp=lp):
                if m is None:
                    return None
                out, hit = [], False
                for s in m.agents:
                    if s.kind == _kind and s.last_pane_id == _lp:
                        s = dataclasses.replace(s, last_pane_id=None)
                        hit = True
                    out.append(s)
                return dataclasses.replace(m, agents=out) if hit else None

            if lp in fresh_worktree_ids:
                # Ownership of the on-disk files already transferred to
                # another slot of this worktree — clear OUR pointer, touch
                # no files.
                windows.update_mapping(window_id, clear)
                continue
            if lp in session_live_ids:
                continue  # unmapped but tmux-alive somewhere — genuinely defer
            startup.scrub_pane_files(fresh_mapping.host_worktree, lp)
            if not startup.pane_files_absent(fresh_mapping.host_worktree, lp):
                logger.warning(
                    "sweep: %s pane=%s files survived scrub, leaving pointer for retry",
                    window_id,
                    lp,
                )
                continue
            windows.update_mapping(window_id, clear)


def _mapping_needs_merge(mapping: "windows.WindowMapping", win: "tmux.Window") -> bool:
    """Cheap pre-check mirroring `merge_ids`'s change test, using the mapping
    snapshot the tick already has in hand — avoids taking the mapping lock
    (and its fresh re-read) on the common tick where nothing changed."""
    if win.index != mapping.window_index:
        return True
    if mapping.orphaned_at is not None:
        # A live window with a tombstone means the id came back (new server
        # reuse); the stale timestamp must be cleared even when nothing else
        # changed, or a later disappearance skips the 90s grace entirely.
        return True
    for s in mapping.agents:
        if not s.pane_id:
            continue
        sid = _read_session_id(mapping.host_worktree, s.pane_id)
        if sid and sid != s.session_id:
            return True
    return False


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
    is it deleted along with the per-worktree state/pending files. The delay
    is what makes session restore survive shutdown: tmux tears an exiting
    session down window-by-window while the server (and therefore the
    status-line tick) is still running, so an eager prune would delete the
    entire snapshot moments before the server exits. Ticks stop when the
    server dies, so a tombstone written during shutdown is never followed by
    the delete.

    Grace-elapsed candidates are grouped by `host_worktree` from a plain
    pre-lock read — that read is only used for grouping + as the "identity
    we're about to delete", it is NOT trusted for the actual delete
    decision. Malformed/missing mappings are dropped immediately (nothing
    restorable to defer for).

    Each worktree's group is then processed under ONE hold of that
    worktree's cleanup lock: the fresh tmux queries (`list_windows`,
    `window_pane_map`) and the derived protected-pane-id set are computed
    ONCE per worktree, not once per candidate. Protected = pane ids still
    claimed by a live window's mapping of the SAME worktree (reuses the
    sweep's own `_worktree_live_pane_index` helper), union every
    currently-alive tmux pane id session-wide — a pane id can't be alive
    twice at once, so any literally-alive pane id must be the very agent
    the stale mapping recorded.

    Per candidate, still under that same lock hold: re-check the window id
    against the FRESH live-window set (a spawn — `agent-new`, `agent-other`,
    restore, rebuild — can recreate the window between the tick's start and
    this prune running), and re-read the mapping fresh and compare it to the
    pre-lock snapshot — if either the window reappeared or the mapping was
    replaced since the pre-lock read, skip this candidate entirely rather
    than delete out from under whoever just published it. Only then scrub
    (state + session + pending, for both the slot's current pane id and any
    still-pending `last_pane_id`) and `windows.delete_mapping` — both BEFORE
    releasing the lock. `delete_mapping` takes the per-window mapping lock,
    always the second lock after the cleanup lock (see `locks.py`), so this
    nesting is safe."""
    d = paths.windows_dir()
    if not d.exists():
        return

    candidates: dict[Path, list[tuple[str, "windows.WindowMapping"]]] = {}
    for f in d.glob("*.json"):
        window_id = f.stem
        if window_id in live_ids:
            continue
        try:
            mapping = windows.read_mapping(window_id)
        except KeyError:
            logger.debug(
                "malformed mapping file for window %s, skipping worktree cleanup",
                window_id,
            )
            windows.delete_mapping(window_id)
            continue
        if mapping is None:
            # Unreadable/malformed: nothing restorable in it, drop it now.
            windows.delete_mapping(window_id)
            continue
        if mapping.orphaned_at is None:
            windows.write_mapping(dataclasses.replace(mapping, orphaned_at=now))
            logger.info(
                "window %s gone; tombstoned, deleting in %.0fs",
                window_id,
                _ORPHAN_GRACE_SECONDS,
            )
            continue
        if now - mapping.orphaned_at < _ORPHAN_GRACE_SECONDS:
            continue
        logger.info(
            "window %s gone for %.0fs; pruning", window_id, _ORPHAN_GRACE_SECONDS
        )
        candidates.setdefault(mapping.host_worktree, []).append((window_id, mapping))

    for host_worktree, entries in candidates.items():
        with locks.locked(paths.worktree_cleanup_lock(host_worktree)):
            try:
                fresh_wins = tmux.list_windows(tmux.SESSION)
                fresh_panes = tmux.window_pane_map(tmux.SESSION)
            except subprocess.CalledProcessError:
                logger.warning(
                    "prune: fresh tmux query failed for worktree %s, deferring "
                    "cleanup of %d candidate(s)",
                    host_worktree,
                    len(entries),
                    exc_info=True,
                )
                continue
            fresh_live_ids = {w.id for w in fresh_wins}
            protected = _worktree_live_pane_index(_read_agent_mappings(fresh_wins)).get(
                host_worktree, set()
            )
            for pane_set in fresh_panes.values():
                protected |= {p.lstrip("%") for p in pane_set}

            for window_id, stale_mapping in entries:
                if window_id in fresh_live_ids:
                    continue  # reappeared since the upfront snapshot
                fresh_mapping = windows.read_mapping(window_id)
                if fresh_mapping is None:
                    continue  # already deleted by someone else
                if fresh_mapping != stale_mapping:
                    continue  # replaced mid-prune — not the candidate we planned to drop
                for slot in fresh_mapping.agents:
                    for pid in (slot.pane_id, slot.last_pane_id):
                        if pid and pid not in protected:
                            startup.scrub_pane_files(host_worktree, pid)
                windows.delete_mapping(window_id)


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

    # One read of every window's mapping, done once for the whole tick: reused
    # below instead of re-reading per window. `_sweep_cleanup_pointers`
    # re-derives its OWN fresh worktree-scoped index under the cleanup lock
    # (this snapshot would otherwise be stale by the time it takes the lock).
    mappings = _read_agent_mappings(wins)

    for win in wins:
        if win.name == tmux.CONTROL_WINDOW:
            continue
        mapping = mappings.get(win.id)
        if mapping is None:
            # A live window with no mapping shouldn't happen — surface as X
            # so the breakage is visible instead of letting a stale letter
            # persist on disk.
            code = state.ERRORED
            combined = state.ERRORED
            counts[state.ERRORED] += 1
        else:
            slot_codes: list[str] = []
            letters: list[str] = []
            live_panes = panes.get(win.id, set())
            for i, slot in enumerate(mapping.agents):
                if slot.pane_id is None:
                    continue  # dead secondary: renders nothing, counts nothing
                alive = f"%{slot.pane_id}" in live_panes
                if not alive and i > 0:
                    _mark_secondary_dead(mapping, slot)
                    continue
                sf = paths.worktree_state_file(mapping.host_worktree, slot.pane_id)
                # A present state file (written by Claude's hooks) always wins.
                # Only when it's absent do we fall back: slot 0 uses the
                # host-side phase_hint (e.g. during agent-new's pre-worktree
                # startup); a live secondary with no state file yet is starting.
                ph = (
                    _read_phase(sf)
                    if sf.exists()
                    else (
                        (mapping.phase_hint or phase.IDLE) if i == 0 else phase.STARTING
                    )
                )
                counts_bz = registry.scan(mapping.host_worktree, slot.pane_id, now=now)
                letter = phase.derive_letter(
                    ph,
                    b_count=counts_bz.background,
                    z_count=counts_bz.sleeping,
                    pane_alive=alive,
                )
                overlay = counts_bz.for_letter(letter)
                slot_codes.append(f"{letter}{overlay}" if overlay else letter)
                letters.append(letter)
                counts[letter] += 1
            _sweep_cleanup_pointers(win.id, mapping)
            code = "|".join(slot_codes) if slot_codes else state.ERRORED
            combined = phase.combined_letter(letters)
        option_cmds.append(f'set-option -wt {win.id} @state_code "{code}"')
        option_cmds.append(
            f'set-option -wt {win.id} @state_fg "{palette.fg[combined]}"'
        )
        option_cmds.append(
            f'set-option -wt {win.id} @state_selected_fg "{palette.selected_fg[combined]}"'
        )
        # `code` (not just the combined letter) is in the fingerprint so a
        # B2->B3 overlay change, or a slot joining/leaving, still re-publishes
        # @state_code (the option write is gated on it).
        fingerprint_parts.append(f"{win.id}:{win.name}:{win.index}:{code}")

        if mapping is None:
            continue

        def merge_ids(m, *, _win=win):
            if m is None:
                return None
            out, changed = [], False
            for s in m.agents:
                sid = (
                    _read_session_id(m.host_worktree, s.pane_id) if s.pane_id else None
                )
                if sid and sid != s.session_id:
                    s = dataclasses.replace(s, session_id=sid)
                    changed = True
                out.append(s)
            if _win.index != m.window_index:
                changed = True
            if m.orphaned_at is not None:
                # Window id reused by a new server before the GC fired; the
                # tombstone belongs to the previous occupant.
                changed = True
            return (
                dataclasses.replace(
                    m, agents=out, window_index=_win.index, orphaned_at=None
                )
                if changed
                else None
            )

        if _mapping_needs_merge(mapping, win):
            windows.update_mapping(win.id, merge_ids)

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
