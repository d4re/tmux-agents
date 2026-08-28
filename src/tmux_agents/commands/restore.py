"""`agent-restore` worker.

Two-phase: plan_entries / group_entries_by_project (pure, testable) and
execute_plan (creates windows, runs up_cmds, respawns panes).
"""

from __future__ import annotations
import argparse
import dataclasses
import io
import logging
import os
import shutil
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

from tmux_agents import (
    agent_kind,
    codex_hooks,
    config,
    container,
    exec_cmd,
    gh_auth,
    locks,
    logging_setup,
    overview,
    paths,
    phase,
    progress,
    provisioning,
    sandbox,
    ssh_forward,
    startup,
    tmux,
    windows,
)
from tmux_agents.commands import state_tick

logger = logging.getLogger(__name__)

EntryKind = Literal["skip", "revive", "fresh", "reactivate"]
SlotAction = Literal["none", "reactivate", "revive"]


@dataclass(frozen=True)
class Entry:
    window_id: str
    project: str
    branch: str | None
    host_worktree: Path
    pane_id: str
    claude_session_id: str | None
    window_index: int
    kind: EntryKind = "fresh"
    # The plan model (spec Section 4 "Restore"): one window action that owns
    # slot 0 (kind, above) plus one independent action per EXISTING
    # secondary slot (secondary_action, below). `slots` is the source of
    # truth going forward; `pane_id`/`claude_session_id` stay as real fields
    # (not properties) purely so every pre-existing call site/test that
    # constructs or reads them keeps working unchanged — mirrors
    # `WindowMapping`'s own legacy-synthesis pattern.
    slots: list[windows.AgentSlot] = dataclasses.field(default_factory=list)
    secondary_action: SlotAction = "none"

    def __post_init__(self):
        if not self.slots:
            object.__setattr__(
                self,
                "slots",
                [
                    windows.AgentSlot(
                        kind=agent_kind.CLAUDE,
                        pane_id=self.pane_id or None,
                        session_id=self.claude_session_id,
                    )
                ],
            )


def _pane_phase(entry: Entry) -> str | None:
    """Read the per-pane phase Claude hooks / restore write for this entry's
    pane, or None if there's no state file yet."""
    d = paths.read_json_or(
        paths.worktree_state_file(entry.host_worktree, entry.pane_id), None
    )
    if not isinstance(d, dict):
        return None
    return d.get("phase")


def _slot_phase(entry: Entry, slot: windows.AgentSlot) -> str | None:
    """Same as `_pane_phase` but for an arbitrary slot, not just slot 0."""
    if slot.pane_id is None:
        return None
    d = paths.read_json_or(
        paths.worktree_state_file(entry.host_worktree, slot.pane_id), None
    )
    if not isinstance(d, dict):
        return None
    return d.get("phase")


def classify_entry(entry: Entry, live_panes: dict[str, set[str]]) -> EntryKind:
    """Classify a snapshot entry against the current tmux pane map.

    skip       — window alive, recorded pane present, and the pane is a healthy
                 agent (not an errored restore placeholder).
    reactivate — window alive, recorded pane present, but its phase is
                 `errored` (a failed restore left the placeholder pane alive
                 showing an error). Retry re-runs the container + respawns
                 Claude in the same pane.
    revive     — window alive but the recorded pane id is gone.
    fresh      — window is not present at all.
    """
    panes = live_panes.get(entry.window_id)
    if panes is None:
        return "fresh"
    # Stored pane_id is stripped (no '%'); tmux's pane_id includes it.
    if f"%{entry.pane_id}" in panes:
        if _pane_phase(entry) == phase.ERRORED:
            return "reactivate"
        return "skip"
    return "revive"


def classify_secondary(entry: Entry, live_panes: dict[str, set[str]]) -> SlotAction:
    """Independent action for entry's EXISTING secondary slot, if any.

    A mapping without a secondary slot is a normal single-agent window and
    is never "repaired" into a dual one — hence the `s is None` short
    circuit below, evaluated before anything else.

    none       — pane alive and healthy.
    reactivate — pane alive but phase=errored (a failed earlier attempt left
                 the placeholder pane alive).
    revive     — slot dead (`pane_id: null`) or its recorded pane is gone.
    """
    s = entry.slots[1] if len(entry.slots) > 1 else None
    if s is None:
        return "none"
    panes = live_panes.get(entry.window_id) or set()
    if s.pane_id and f"%{s.pane_id}" in panes:
        return "reactivate" if _slot_phase(entry, s) == phase.ERRORED else "none"
    return "revive"


def _read_disk_session_id(worktree: Path, pane_id: str) -> str | None:
    """Delegates to the state tick's on-disk session-id reader (same
    UUID-shape sanity check as the hook's sed validator) — see
    `state_tick._read_session_id`."""
    return state_tick._read_session_id(worktree, pane_id)


def harvest_session_ids(entries: list[Entry]) -> list[Entry]:
    """Merge `session-<pane>.id` from disk into EVERY slot of EVERY entry.

    Must run as one pass over ALL entries, completing BEFORE any pane is
    created or scrubbed: on a fresh server, a later entry's newly assigned
    pane id can equal an earlier entry's OLD (recycled) pane id, so
    processing entries one at a time would let an early entry's pre-launch
    scrub destroy a later entry's still-unread `session-<id>.id` file."""
    out: list[Entry] = []
    for e in entries:
        slots = []
        for s in e.slots:
            pane = s.pane_id or s.last_pane_id
            sid = _read_disk_session_id(e.host_worktree, pane) if pane else None
            slots.append(dataclasses.replace(s, session_id=sid or s.session_id))
        out.append(dataclasses.replace(e, slots=slots))
    return out


def _snapshot_dir() -> Path:
    """windows.previous/ if the launcher staged it, else windows/ for manual reruns."""
    prev = paths.windows_previous_dir()
    if prev.exists() and any(prev.iterdir()):
        return prev
    return paths.windows_dir()


def plan_entries(*, live_panes: dict[str, set[str]], projects: dict) -> list[Entry]:
    """Read the snapshot, harvest on-disk session ids (barrier, before any
    classification or pane work), classify each entry, drop `skip`+`none`.
    Sort by window_index."""
    snap = _snapshot_dir()
    if not snap.exists():
        return []
    raw: list[Entry] = []
    for f in snap.glob("*.json"):
        d = paths.read_json_or(f, None)
        if not isinstance(d, dict):
            continue
        project = d.get("project")
        host_worktree = Path(d.get("host_worktree", ""))
        if project not in projects or not host_worktree.exists():
            continue
        mapping = windows.WindowMapping.from_dict(f.stem, d)
        e = Entry(
            window_id=f.stem,
            project=project,
            branch=mapping.branch,
            host_worktree=host_worktree,
            pane_id=mapping.default_slot.pane_id or "",
            claude_session_id=mapping.default_slot.session_id,
            window_index=int(d.get("window_index", 0)),
            kind="fresh",  # overwritten below
            slots=mapping.agents,
        )
        raw.append(e)

    # Harvest barrier: must complete over every surviving entry BEFORE any
    # classification (which only reads pane liveness, not session ids) or
    # pane work below.
    raw = harvest_session_ids(raw)

    entries: list[Entry] = []
    for e in raw:
        kind = classify_entry(e, live_panes)
        secondary_action = classify_secondary(e, live_panes)
        if kind == "skip" and secondary_action == "none":
            continue
        entries.append(
            dataclasses.replace(e, kind=kind, secondary_action=secondary_action)
        )
    entries.sort(key=lambda e: e.window_index)
    return entries


def group_entries_by_project(plan: list[Entry]) -> "OrderedDict[str, list[Entry]]":
    """Group by project, preserving plan order (first-occurrence wins)."""
    groups: OrderedDict[str, list[Entry]] = OrderedDict()
    for e in plan:
        groups.setdefault(e.project, []).append(e)
    return groups


@dataclass(frozen=True)
class Placeholder:
    entry: Entry
    new_window_id: str
    pane_id: str | None  # tmux pane id (incl. %) for slot 0; None when the
    # window action is `skip` and only the secondary needed pre-creation.
    secondary_pane_id: str | None = None  # tmux pane id (incl. %) for slot 1


def _mark_pane_failed(e: "Entry", pane_id_full: str, reason: str) -> None:
    """Show the failure in `pane_id_full` and flip its per-pane state to
    errored. The per-window spawn log is unlinked by `_activate_project`'s
    finally. Parameterized on a raw pane id (not a whole Placeholder) so a
    primary-slot failure and a secondary-slot failure can be isolated from
    each other — one slot erroring never touches its sibling's pane."""
    label = windows.window_name(e.project, e.branch)
    new_args = e.project + (f" {e.branch}" if e.branch else "")
    body = (
        f"\n  agent-restore failed for {label}\n  reason: {reason}\n\n"
        "  Fix the underlying issue (e.g. start Docker) and re-run:\n"
        "    agent-restore\n\n"
        f"  Or remove this window with {tmux.prefix_label()} k and re-spawn manually:\n"
        f"    agent-new {new_args}\n\n"
    )
    startup.show_static_text(pane_id_full, body)
    startup._write_pane_state(
        e.host_worktree, pane_id_full.lstrip("%"), phase_value=phase.ERRORED
    )


def _clean_old_pane_files(worktree: Path, old_pane_id: str) -> None:
    """Best-effort unlink of stale per-pane state/session files + pending markers."""
    import shutil

    for f in (
        paths.worktree_state_file(worktree, old_pane_id),
        paths.worktree_session_id_file(worktree, old_pane_id),
    ):
        f.unlink(missing_ok=True)
    shutil.rmtree(paths.worktree_pending_dir(worktree, old_pane_id), ignore_errors=True)


def _slots_with_default(e: Entry, new_pane_stripped: str) -> list[windows.AgentSlot]:
    """`e.slots` with slot 0's pane id replaced, preserving every other
    slot verbatim (kind + harvested session id) — used when rewriting the
    mapping for the window action so a secondary slot's identity survives
    a primary revive/fresh even before its own pane is touched."""
    return [dataclasses.replace(e.slots[0], pane_id=new_pane_stripped)] + list(
        e.slots[1:]
    )


def _pre_create_revive(
    e: Entry, live_panes: dict[str, set[str]]
) -> "Placeholder | None":
    """Split a new agent pane above the surviving overview pane.

    Normal case: exactly one pane survives (the overview) — split above it
    at 75%/vertical (single-agent geometry). Compact-layout dual-agent case:
    exactly one pane survives and it's not an overview but the entry's live
    secondary slot (dead default + healthy secondary, no overview pane in
    compact layout) — split the new default placeholder off it at
    50%/horizontal (dual-agent geometry), landing the default on the left;
    the secondary is never killed or respawned. Split-layout dual-agent
    case: an overview pane AND the entry's live mapped secondary both
    survive — anchor on the SECONDARY (same 50%/horizontal geometry as the
    compact case) rather than off the overview, leaving the overview pane
    completely untouched. Degenerate case: more than one pane survives, no
    live secondary among them, e.g. a duplicate overview pane left by a
    layout toggle on an already-agent-dead window — keep one overview pane
    as the split target and reap the extras so the window ends up with
    exactly overview + new agent. Returns None (logs) when nothing
    survives, when a lone survivor is neither an overview pane nor the
    mapped secondary (nothing usable to split from), or when several panes
    survive but none is tagged overview (can't place agent)."""
    survivors = live_panes.get(e.window_id, set())
    if not survivors:
        logger.warning("%s: cannot revive — no surviving pane", e.window_id)
        return None
    secondary_pane_id = e.slots[1].pane_id if len(e.slots) > 1 else None
    secondary_full = f"%{secondary_pane_id}" if secondary_pane_id else None
    if len(survivors) == 1:
        target = next(iter(survivors))
        overview_panes = tmux.overview_pane_ids(e.window_id)
        if target in overview_panes:
            percent, horizontal = 75, False
        elif secondary_full is not None and target == secondary_full:
            percent, horizontal = 50, True
        else:
            logger.warning(
                "%s: cannot revive — lone survivor %s is neither an overview "
                "pane nor the mapped secondary — nothing usable to split from",
                e.window_id,
                target,
            )
            return None
    else:
        overview_panes = tmux.overview_pane_ids(e.window_id)
        if not overview_panes:
            logger.warning(
                "%s: cannot revive — %d panes survive, none tagged overview (%s)",
                e.window_id,
                len(survivors),
                survivors,
            )
            return None
        if secondary_full is not None and secondary_full in survivors:
            # A live secondary survives alongside the overview pane — anchor
            # on the secondary (dual-agent geometry) instead of splitting
            # off the overview; the overview pane is never touched.
            target = secondary_full
            percent, horizontal = 50, True
        else:
            target = overview_panes[0]
            for extra in overview_panes[1:]:
                logger.info(
                    "%s: reaping duplicate overview pane %s", e.window_id, extra
                )
                tmux.kill_pane(extra)
            percent, horizontal = 75, False
    new_full_pane_id = tmux.split_window(
        target,
        percent=percent,
        command=startup.placeholder_command(e.window_id),
        before=True,
        horizontal=horizontal,
    )
    # split_window uses `-d` to keep focus on the original pane — correct for
    # fresh windows (original = agent), wrong for revive (original = overview).
    tmux.select_pane(new_full_pane_id)
    new_pane_stripped = new_full_pane_id.lstrip("%")
    # Scrub the ASSIGNED pane id before any state write/launch — required
    # before landing an agent on a (possibly recycled) pane id, same
    # aliasing guard `_pre_create_secondary_split` applies for slot 1.
    with locks.locked(paths.worktree_cleanup_lock(e.host_worktree)):
        startup.scrub_pane_files(e.host_worktree, new_pane_stripped)
    _clean_old_pane_files(e.host_worktree, e.pane_id)

    def _revived_mapping(
        m: "windows.WindowMapping | None",
    ) -> "windows.WindowMapping":
        # update_mapping (not a bare write_mapping) so `fn` reads fresh at
        # write time — a secondary slot published (e.g. by agent-other)
        # between the snapshot `e` was built from and now survives the
        # rewrite, instead of being clobbered by `e.slots`' stale copy.
        fresh_agents = list(m.agents) if m is not None else list(e.slots)
        new_slot0 = dataclasses.replace(e.slots[0], pane_id=new_pane_stripped)
        agents = [new_slot0, *fresh_agents[1:]] if fresh_agents else [new_slot0]
        return windows.WindowMapping(
            window_id=e.window_id,
            project=e.project,
            branch=e.branch,
            host_worktree=e.host_worktree,
            pane_id=new_pane_stripped,
            claude_session_id=e.claude_session_id,
            agents=agents,
        )

    windows.update_mapping(e.window_id, _revived_mapping)
    startup._write_pane_state(
        e.host_worktree, new_pane_stripped, phase_value=phase.STARTING
    )
    logger.info(
        "%s: revived -> pane=%s (split %s)", e.window_id, new_full_pane_id, target
    )
    return Placeholder(e, e.window_id, new_full_pane_id)


def _pre_create_reactivate(e: Entry) -> "Placeholder | None":
    """Reuse the errored placeholder's existing window + pane in place.

    A failed restore left this pane alive showing an error message and its
    per-pane state at `errored`. Respawn it back into the tail-log placeholder
    (so the retry's progress is visible), reset its state to `starting`, and
    return a Placeholder pointing at the same pane so `execute_plan` can
    respawn Claude into it once the container is up. No new window is created,
    so retries never accumulate duplicate windows."""
    full_pane_id = f"%{e.pane_id}"
    startup._respawn_with_retry(full_pane_id, startup.placeholder_command(e.window_id))
    startup._write_pane_state(e.host_worktree, e.pane_id, phase_value=phase.STARTING)
    logger.info("%s: reactivating errored pane=%s", e.window_id, full_pane_id)
    return Placeholder(e, e.window_id, full_pane_id)


def _pre_create_fresh(e: Entry, layout: str) -> "Placeholder":
    """Existing single-window-creation path, extracted unchanged so the
    dual/secondary helpers below can layer on top of it uniformly with the
    revive/reactivate branches."""
    new_wid = tmux.new_window(
        tmux.SESSION,
        name=windows.window_name(e.project, e.branch),
        command="sh -c 'while :; do sleep 3600; done'",
    )
    if e.branch:
        tmux.set_window_option(new_wid, "@pinned", "1")
    full_pane_id = tmux.active_pane_id(new_wid)
    pane_stripped = full_pane_id.lstrip("%")
    # Scrub the ASSIGNED pane id before any state write/launch — required
    # before landing an agent on a (possibly recycled) pane id, same
    # aliasing guard `_pre_create_secondary_split` applies for slot 1.
    with locks.locked(paths.worktree_cleanup_lock(e.host_worktree)):
        startup.scrub_pane_files(e.host_worktree, pane_stripped)
    startup._respawn_with_retry(full_pane_id, startup.placeholder_command(new_wid))
    if layout == "split":
        try:
            overview.attach_overview_pane(new_wid)
        except Exception:
            logger.warning(
                "%s: overview-pane attach failed", e.window_id, exc_info=True
            )
    windows.write_mapping(
        windows.WindowMapping(
            window_id=new_wid,
            project=e.project,
            branch=e.branch,
            host_worktree=e.host_worktree,
            pane_id=pane_stripped,
            claude_session_id=e.claude_session_id,
            agents=_slots_with_default(e, pane_stripped),
        )
    )
    startup._write_pane_state(
        e.host_worktree, pane_stripped, phase_value=phase.STARTING
    )
    logger.info("%s: pre-created -> %s pane=%s", e.window_id, new_wid, full_pane_id)
    return Placeholder(e, new_wid, full_pane_id)


def _pre_create_primary(
    e: Entry, live_panes: dict[str, set[str]], layout: str
) -> "Placeholder | None":
    """Dispatch slot-0's window action. `skip` has no primary work — callers
    branch on that before reaching here (see `_pre_create_entry`)."""
    if e.kind == "revive":
        return _pre_create_revive(e, live_panes)
    if e.kind == "reactivate":
        return _pre_create_reactivate(e)
    return _pre_create_fresh(e, layout)


def _secondary_fallback_slot(e: Entry) -> windows.AgentSlot:
    """Slot-1 metadata to fall back to when the mapping doesn't have one yet
    (shouldn't normally happen — `secondary_action` is only non-`none` when
    `e.slots` already has a slot 1 — but keeps the publish helper total)."""
    if len(e.slots) > 1:
        return e.slots[1]
    return windows.AgentSlot(kind=agent_kind.other(e.slots[0].kind), pane_id=None)


def _secondary_slot_for_activation(e: Entry, window_id: str) -> windows.AgentSlot:
    """The slot-1 identity to build the secondary's respawn command from.

    Prefers the mapping `_pre_create_secondary_split` just published on
    disk: when publication went through the compatible dead-state
    progression (see `_secondary_cas_compatible`), that fresh slot can
    carry a NEWER `session_id` than the plan's — `_mark_secondary_dead`
    merges the on-disk session id into the slot before nulling `pane_id`,
    and `publish()`'s `dataclasses.replace(agents[1], pane_id=...)`
    preserves whatever session id was on the fresh slot at CAS time, not
    the planned one. `window_id` must be the LIVE tmux window id
    (`Placeholder.new_window_id`), not `e.window_id` — those differ for
    the fresh/revive primary kinds, where the secondary is published under
    the newly created window. Falls back to the planned slot if the
    mapping or slot 1 is unexpectedly missing."""
    m = windows.read_mapping(window_id)
    if m is not None and len(m.agents) > 1:
        return m.agents[1]
    return _secondary_fallback_slot(e)


def _secondary_cas_compatible(
    current: "tuple[str, str | None] | None",
    planned: "tuple[str, str | None] | None",
) -> bool:
    """True when the fresh slot-1 identity exactly matches what the split
    was planned against, OR represents a compatible DEAD-STATE PROGRESSION
    of it: same kind, `pane_id` now null. That covers everything
    `state_tick._mark_secondary_dead` can produce (fresh `last_pane_id` set
    to the planned pane id) as well as states reached by other lock-held
    activity — e.g. a completed cleanup sweep clearing `last_pane_id`
    entirely, or a different winner appearing and then also dying — none of
    which are a competing publish. We don't gate on `last_pane_id` matching
    the planned pane id: `pane_id is None` under the cleanup lock already
    means no LIVE winner exists to overwrite, which is the only thing this
    CAS needs to protect against. Activation then builds the respawn
    command from the fresh slot's own (possibly newer) `session_id`, which
    is correct in both cases — the acknowledged death of the planned pane,
    and the appeared-and-died-winner case. Any other divergence (different
    non-null pane id, or a changed kind) means a winner published a LIVE
    secondary meanwhile, and the CAS must reject."""
    if current == planned:
        return True
    if planned is None or current is None:
        return False
    planned_kind, _planned_pane_id = planned
    current_kind, current_pane_id = current
    return current_kind == planned_kind and current_pane_id is None


def _pre_create_secondary_split(e: Entry, window_id: str, anchor: str) -> "str | None":
    """Split `anchor` 50/50 horizontal (Task 6's `agent-other` params) for
    the secondary placeholder. Split, scrub, AND publish all happen under
    the SAME worktree-cleanup-lock hold (mirrors `agent-other`'s
    publish-last discipline) — a mapping read+written mid-way through would
    let a concurrent `agent-other` publish a live secondary that this call
    then clobbers.

    Publication is a compare-and-set against the PLANNED slot-1 identity
    captured at planning time — `(kind, pane_id)` where `pane_id` is the
    OLD pane id for a recorded-but-gone slot (`classify_secondary` said
    `revive` precisely BECAUSE that recorded pane is no longer alive) or
    `None` for a never-started slot. This is deliberately NOT "any non-null
    `pane_id` rejects": the normal fresh-server dual-snapshot revive case
    has slot 1 carrying that old non-null pane id, and this split must
    still be allowed to claim it. Publication also proceeds against ANY
    COMPATIBLE DEAD-STATE PROGRESSION of that same identity: same kind,
    `pane_id` now null, regardless of `last_pane_id`. The common case is
    the tick's `_mark_secondary_dead` transitioning the planned slot to
    `(same kind, pane_id=None, last_pane_id=<planned pane_id>)` between
    planning and this CAS (it independently discovered the same pane is
    gone) — not a competing publish, just the same fact observed twice.
    But `last_pane_id` is deliberately NOT required to match: a completed
    cleanup sweep can clear it entirely, or a different winner can appear
    and then also die, before this CAS runs — both leave `pane_id=None`
    with no live winner to protect, so `_secondary_cas_compatible` accepts
    them too. Only a DIFFERENT non-null pane id or a changed kind means a
    winner published a live secondary meanwhile (`agent-other`, or a
    concurrent restore) — the CAS is a no-op, the just-created placeholder
    pane is killed (its files were already scrubbed pre-publish, so
    nothing to clean up there), and the slot is left alone for the winner.
    Returns the new full pane id, or
    `None` when the CAS lost — callers already treat a `None`
    `secondary_pane_id` as "nothing to respawn for this slot"."""
    planned = (e.slots[1].kind, e.slots[1].pane_id) if len(e.slots) > 1 else None
    with locks.locked(paths.worktree_cleanup_lock(e.host_worktree)):
        new_pane = tmux.split_window(
            anchor,
            percent=50,
            command=startup.placeholder_command(window_id),
            horizontal=True,
        )
        new_pane_stripped = new_pane.lstrip("%")
        # Scrub the ASSIGNED pane id before any state write/launch —
        # required before landing an agent on a (possibly recycled) pane id
        # (Section 3's aliasing guard), regardless of how the CAS below
        # turns out.
        startup.scrub_pane_files(e.host_worktree, new_pane_stripped)

        def publish(
            m: "windows.WindowMapping | None",
        ) -> "windows.WindowMapping | None":
            if m is None:
                return None
            agents = list(m.agents)
            current_slot = agents[1] if len(agents) > 1 else None
            current = (
                (current_slot.kind, current_slot.pane_id) if current_slot else None
            )
            if not _secondary_cas_compatible(current, planned):
                # Slot 1's identity has drifted from what we planned this
                # split against — someone else (agent-other, or a
                # concurrent restore) already published a live secondary
                # (or otherwise changed its kind/pane) — back off rather
                # than overwrite the winner's pane.
                return None
            fallback = _secondary_fallback_slot(e)
            if len(agents) > 1:
                agents[1] = dataclasses.replace(agents[1], pane_id=new_pane_stripped)
            else:
                agents.append(dataclasses.replace(fallback, pane_id=new_pane_stripped))
            return dataclasses.replace(m, agents=agents)

        updated = windows.update_mapping(window_id, publish)
        if updated is None:
            logger.info(
                "%s: secondary slot changed since planning (or mapping gone) "
                "— killing our placeholder pane=%s instead of publishing",
                window_id,
                new_pane,
            )
            try:
                tmux.kill_pane(new_pane)
            except Exception:
                logger.warning(
                    "%s: failed to kill losing secondary placeholder %s",
                    window_id,
                    new_pane,
                    exc_info=True,
                )
            return None
    startup._write_pane_state(
        e.host_worktree, new_pane_stripped, phase_value=phase.STARTING
    )
    logger.info(
        "%s: secondary pre-created -> pane=%s (split %s)", window_id, new_pane, anchor
    )
    return new_pane


def _reactivate_secondary_in_place(e: Entry, window_id: str) -> str:
    """Mirrors `_pre_create_reactivate` for slot 1: the secondary pane is
    alive but errored, so respawn it back into the placeholder in place —
    no split, no new pane."""
    secondary = e.slots[1]
    full_pane = f"%{secondary.pane_id}"
    startup._respawn_with_retry(full_pane, startup.placeholder_command(window_id))
    startup._write_pane_state(
        e.host_worktree, secondary.pane_id, phase_value=phase.STARTING
    )
    logger.info("%s: reactivating errored secondary pane=%s", window_id, full_pane)
    return full_pane


def _pre_create_entry(
    e: Entry, live_panes: dict[str, set[str]], layout: str
) -> "Placeholder | None":
    """One entry's full pre-creation: the window action (slot 0) plus the
    independent secondary action, if any.

    `kind == "skip"` means slot 0 is healthy and untouched — the drop rule
    guarantees `secondary_action != "none"` reaches this branch, so only
    the secondary needs work, split off the LIVE default pane (repair)."""
    if e.kind == "skip":
        if e.secondary_action == "revive":
            anchor = f"%{e.slots[0].pane_id}"
            new_pane = _pre_create_secondary_split(e, e.window_id, anchor)
            return Placeholder(e, e.window_id, None, secondary_pane_id=new_pane)
        if e.secondary_action == "reactivate":
            new_pane = _reactivate_secondary_in_place(e, e.window_id)
            return Placeholder(e, e.window_id, None, secondary_pane_id=new_pane)
        return None  # unreachable: plan_entries drops skip+none

    ph = _pre_create_primary(e, live_panes, layout)
    if ph is None:
        return None
    if e.secondary_action == "revive":
        new_pane = _pre_create_secondary_split(e, ph.new_window_id, ph.pane_id)
        return dataclasses.replace(ph, secondary_pane_id=new_pane)
    if e.secondary_action == "reactivate":
        new_pane = _reactivate_secondary_in_place(e, ph.new_window_id)
        return dataclasses.replace(ph, secondary_pane_id=new_pane)
    return ph


def pre_create_windows(
    plan: list[Entry], live_panes: dict[str, set[str]]
) -> dict[str, Placeholder]:
    """Create placeholder panes for each plan entry, branching on kind."""
    placeholders: dict[str, Placeholder] = {}
    layout = paths.read_layout()
    for e in plan:
        try:
            ph = _pre_create_entry(e, live_panes, layout)
            if ph is not None:
                placeholders[ph.new_window_id] = ph
        except Exception:
            logger.error("%s: pre-create failed", e.window_id, exc_info=True)
    return placeholders


def _build_slot_cmd(
    proj, e: Entry, slot: windows.AgentSlot, container_name: str | None
) -> str:
    """Substitute the slot's kind's exec template, injecting its own resume
    args (` --resume <id>` for claude, ` resume <id>` for codex)."""
    return exec_cmd.build(
        proj,
        branch=e.branch,
        session_id=slot.session_id,
        container_name=container_name,
        kind=slot.kind,
        label=e.window_id,
    )


def _activate_project(
    project_name: str,
    entries: list[Entry],
    projects: dict,
    by_entry_window: "dict[str, Placeholder]",
    _fail,
) -> None:
    """Bring up the project container (if any) and respawn each entry's pane.

    Opens one log file + Reporter per entry. Project-shared stages
    (container, ssh pump) are broadcast via MultiReporter; per-entry
    stages (hooks) go to that entry's log only. Logs are deleted after
    each activation attempt (success or failure) in the finally block.
    """
    logger.info("activating project %r with %d entries", project_name, len(entries))
    proj = projects.get(project_name)
    if proj is None:
        for e in entries:
            _fail(e, f"project {project_name!r} not in projects.toml")
        # Defensive: clean up any stray logs (shouldn't exist yet).
        for e in entries:
            try:
                paths.spawn_log(e.window_id).unlink()
            except FileNotFoundError:
                pass
        return

    # Open per-entry log files + Reporters.
    files: dict[str, io.TextIOWrapper] = {}
    reporters: dict[str, progress.Reporter] = {}
    try:
        for e in entries:
            log_path = paths.spawn_log(e.window_id)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            f = open(log_path, "w", buffering=1)
            files[e.window_id] = f
            reporters[e.window_id] = progress.Reporter(
                out=f, color=True, clock=time.monotonic
            )

        # Banner is per-entry so each window's log shows its own branch.
        for e in entries:
            branch_label = e.branch or "(no branch)"
            reporters[e.window_id].banner(
                f"Restoring agent: {proj.name} / {branch_label}",
            )
        multi = progress.MultiReporter(list(reporters.values()))

        container_name: str | None = None
        sandbox_created = False
        try:
            if proj.backend == config.BACKEND_SANDBOX:
                with multi.stage("sandbox") as st:
                    # Handles a sandbox that is GONE, not merely stopped —
                    # `sbx exec` auto-start does not cover deletion.
                    sandbox_created = sandbox.ensure_up(proj)
                    if sandbox_created:
                        st.info(
                            "recreated (was deleted) — fresh VM: claude /login "
                            "and codex login required; resume ids cleared"
                        )
                    else:
                        st.skip("already present")
                if proj.share_gh_auth:
                    with multi.stage("gh auth") as st:
                        gh_auth.maybe_sync_gh_auth_sandbox(proj.sandbox_name).render(st)
            elif proj.is_container:
                with multi.stage("container") as st:
                    existing = container.current_name(proj)
                    if existing:
                        st.skip("already running")
                        container_name = existing
                    else:
                        st.info("building (this may take minutes)…")
                        up_cmd = (
                            proj.substitute(proj.up_cmd, branch=None)
                            if proj.up_cmd
                            else None
                        )
                        container_name = container.ensure_up(proj, up_cmd=up_cmd)
                if proj.forward_ssh_agent:
                    with multi.stage("ssh pump") as st:
                        ssh_forward.maybe_spawn_pump(
                            container_name,
                            proj.user or "vscode",
                        ).render(st)
                if proj.share_gh_auth:
                    with multi.stage("gh auth") as st:
                        gh_auth.maybe_sync_gh_auth(
                            container_name,
                            proj.user or "vscode",
                        ).render(st)
        except (container.ContainerError, sandbox.SandboxError) as ce:
            kind = "sandbox" if proj.backend == config.BACKEND_SANDBOX else "container"
            for e in entries:
                _fail(e, f"{kind} start failed: {ce}")
            return  # finally block runs, cleaning up logs

        # Once per project group, after the container is up (for container
        # projects) and before any respawn: cheap idempotent codex hook
        # provisioning check, mirroring agent-new's Task 15 "codex hooks"
        # stage. Non-fatal — a failure here must never block Claude/Codex
        # respawn for this project's entries.
        with multi.stage("codex hooks") as st:
            try:
                if proj.backend == config.BACKEND_SANDBOX:
                    codex_hooks.ensure_sandbox(proj.sandbox_name)
                elif proj.is_container:
                    codex_hooks.ensure_container(container_name, proj.user or "vscode")
                else:
                    codex_hooks.ensure_host()
            except Exception as ex:
                st.warn(f"could not provision codex hooks: {type(ex).__name__}: {ex}")
                logger.warning(
                    "%s: codex hook provisioning failed (non-fatal)",
                    project_name,
                    exc_info=True,
                )

        def _slot_for_cmd(slot: windows.AgentSlot) -> windows.AgentSlot:
            # A recreated sandbox lost its session files with the VM;
            # passing the stale id would make `claude --resume` error out.
            if sandbox_created and slot.session_id:
                return dataclasses.replace(slot, session_id=None)
            return slot

        def _hold_codex_login(pane_full: str) -> None:
            # A fresh sandbox has no codex login; launching codex into an
            # auth error loop helps nobody — hold the pane on the runbook
            # (the spec's errored-placeholder-with-hint for recreation).
            startup.show_static_text(
                pane_full, sandbox.CODEX_LOGIN_RUNBOOK.format(name=proj.sandbox_name)
            )
            startup._write_pane_state(
                e.host_worktree, pane_full.lstrip("%"), phase_value=phase.ERRORED
            )

        # Per-entry: hooks + respawn-pane.
        template_path = resources.files("tmux_agents.hooks") / "agents.json"
        with resources.as_file(template_path) as template_file:
            for e in entries:
                ph = by_entry_window.get(e.window_id)
                if ph is None:
                    logger.warning(
                        "%s: no placeholder pane (pre_create skipped this entry)",
                        e.window_id,
                    )
                    continue
                r = reporters[e.window_id]
                with r.stage("hooks") as st:
                    try:
                        provisioning.provision_settings(
                            e.host_worktree, template_path=template_file
                        )
                    except Exception as ex:
                        st.warn(
                            f"could not provision .claude/settings.local.json: "
                            f"{type(ex).__name__}: {ex}"
                        )
                        logger.warning(
                            "%s: provisioning failed (non-fatal)",
                            e.window_id,
                            exc_info=True,
                        )
                # Per-slot: a primary respawn failure never blocks (or is
                # masked by) the secondary's, and vice versa — each is
                # isolated exactly like today's per-entry failures.
                if sandbox_created:
                    # Persist the clearing, not just the respawn arguments:
                    # a stale id left in the mapping (e.g. on a slot held
                    # below) would be merged forward by the tick and
                    # resurface as `--resume <stale>` on a later restore.
                    windows.update_mapping(
                        ph.new_window_id,
                        lambda m: (
                            dataclasses.replace(
                                m,
                                agents=[
                                    dataclasses.replace(s, session_id=None)
                                    for s in m.agents
                                ],
                            )
                            if m is not None
                            else None
                        ),
                    )
                if ph.pane_id is not None:
                    try:
                        if sandbox_created and e.slots[0].kind == agent_kind.CODEX:
                            _hold_codex_login(ph.pane_id)
                        else:
                            cmd = _build_slot_cmd(
                                proj, e, _slot_for_cmd(e.slots[0]), container_name
                            )
                            startup._respawn_with_retry(ph.pane_id, cmd)
                            logger.info(
                                "%s: respawned pane=%s cmd_preview=%r",
                                e.window_id,
                                ph.pane_id,
                                cmd[:80],
                            )
                    except Exception as ex:
                        msg = f"respawn-pane failed: {type(ex).__name__}: {ex}"
                        logger.error("%s: %s", e.window_id, msg)
                        _mark_pane_failed(e, ph.pane_id, msg)
                if ph.secondary_pane_id is not None:
                    secondary = _secondary_slot_for_activation(e, ph.new_window_id)
                    try:
                        if sandbox_created and secondary.kind == agent_kind.CODEX:
                            _hold_codex_login(ph.secondary_pane_id)
                        else:
                            cmd2 = _build_slot_cmd(
                                proj, e, _slot_for_cmd(secondary), container_name
                            )
                            startup._respawn_with_retry(ph.secondary_pane_id, cmd2)
                            logger.info(
                                "%s: respawned secondary pane=%s cmd_preview=%r",
                                e.window_id,
                                ph.secondary_pane_id,
                                cmd2[:80],
                            )
                    except Exception as ex:
                        msg = f"respawn-pane failed: {type(ex).__name__}: {ex}"
                        logger.error("%s: %s", e.window_id, msg)
                        _mark_pane_failed(e, ph.secondary_pane_id, msg)
    finally:
        for e in entries:
            f = files.get(e.window_id)
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass
            try:
                paths.spawn_log(e.window_id).unlink()
            except FileNotFoundError:
                pass


def execute_plan(
    plan: list[Entry], placeholders: dict[str, Placeholder], projects: dict
) -> None:
    """Bring containers up in parallel; sequentially activate each
    project's entries once its container is ready. Failures are logged
    and isolated; the placeholder pane is left in place for failed
    entries (the next state tick will mark it errored once it dies)."""
    by_entry_window = {ph.entry.window_id: ph for ph in placeholders.values()}

    groups = group_entries_by_project(plan)
    if any(
        name in projects and projects[name].backend == config.BACKEND_SANDBOX
        for name in groups
    ):
        try:
            # Once, before the parallel wave — the daemon does not auto-start
            # at boot, and N groups racing a start would just serialize on
            # the daemon lock anyway.
            sandbox.ensure_daemon()
        except sandbox.SandboxError:
            # Each sandbox group will fail individually with the actionable
            # hint from its own ensure_up call.
            logger.error("sbx daemon unavailable before restore wave", exc_info=True)

    def _fail(e: Entry, msg: str) -> None:
        """Whole-project/whole-entry failure (project missing, container
        bring-up failed): mark every placeholder pane this entry has, since
        neither slot can proceed without the container/project."""
        logger.error("%s: %s", e.window_id, msg)
        ph = by_entry_window.get(e.window_id)
        if ph is None:
            return
        if ph.pane_id is not None:
            _mark_pane_failed(e, ph.pane_id, msg)
        if ph.secondary_pane_id is not None:
            _mark_pane_failed(e, ph.secondary_pane_id, msg)

    # Up to 4 concurrent projects to keep docker honest under load.
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures: list[Future] = [
            ex.submit(
                _activate_project, name, list(entries), projects, by_entry_window, _fail
            )
            for name, entries in groups.items()
        ]
        for f in futures:
            f.result()  # propagate unexpected exceptions for visibility

    shutil.rmtree(paths.windows_previous_dir(), ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-restore")
    parser.add_argument(
        "--background",
        action="store_true",
        help="fork-and-detach so the launcher can exec into tmux attach",
    )
    if parser.parse_args(argv).background:
        if os.fork() > 0:
            return 0
        os.setsid()
        startup._detach_stdio()
    logging_setup.setup_logging()
    projects = config.safe_load(
        paths.projects_toml(),
        on_error=lambda msg: logger.error(msg),
    )
    live_panes = tmux.window_pane_map(tmux.SESSION)
    plan = plan_entries(live_panes=live_panes, projects=projects)
    logger.info(
        "plan: %d entries; summary=%s",
        len(plan),
        [
            (e.window_id, e.project, e.branch, bool(e.claude_session_id), e.kind)
            for e in plan
        ],
    )
    placeholders = pre_create_windows(plan, live_panes)
    logger.info("pre_create: %d placeholders created", len(placeholders))
    execute_plan(plan, placeholders, projects)
    logger.info("execute_plan: done")
    return 0
