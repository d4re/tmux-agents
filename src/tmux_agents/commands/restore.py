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
import shlex
import shutil
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

from tmux_agents import (
    config,
    container,
    logging_setup,
    overview,
    paths,
    phase,
    progress,
    provisioning,
    ssh_forward,
    startup,
    tmux,
    windows,
)

logger = logging.getLogger(__name__)

EntryKind = Literal["skip", "revive", "fresh"]


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


def classify_entry(entry: Entry, live_panes: dict[str, set[str]]) -> EntryKind:
    """Classify a snapshot entry against the current tmux pane map.

    skip   — window alive AND the recorded pane id is still present.
    revive — window alive but the recorded pane id is gone.
    fresh  — window is not present at all.
    """
    panes = live_panes.get(entry.window_id)
    if panes is None:
        return "fresh"
    # Stored pane_id is stripped (no '%'); tmux's pane_id includes it.
    if f"%{entry.pane_id}" in panes:
        return "skip"
    return "revive"


def _snapshot_dir() -> Path:
    """windows.previous/ if the launcher staged it, else windows/ for manual reruns."""
    prev = paths.windows_previous_dir()
    if prev.exists() and any(prev.iterdir()):
        return prev
    return paths.windows_dir()


def plan_entries(*, live_panes: dict[str, set[str]], projects: dict) -> list[Entry]:
    """Read the snapshot, classify each entry; drop `skip`. Sort by window_index."""
    snap = _snapshot_dir()
    if not snap.exists():
        return []
    entries: list[Entry] = []
    for f in snap.glob("*.json"):
        d = paths.read_json_or(f, None)
        if not isinstance(d, dict):
            continue
        project = d.get("project")
        host_worktree = Path(d.get("host_worktree", ""))
        if project not in projects or not host_worktree.exists():
            continue
        e = Entry(
            window_id=f.stem,
            project=project,
            branch=d.get("branch"),
            host_worktree=host_worktree,
            pane_id=d.get("pane_id", ""),
            claude_session_id=d.get("claude_session_id"),
            window_index=int(d.get("window_index", 0)),
            kind="fresh",  # overwritten below
        )
        kind = classify_entry(e, live_panes)
        if kind == "skip":
            continue
        entries.append(dataclasses.replace(e, kind=kind))
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
    pane_id: str  # tmux pane id including the % prefix


def _mark_entry_failed(e: "Entry", ph: "Placeholder", reason: str) -> None:
    """Show the failure in the placeholder pane and flip its state to errored.
    The per-window spawn log is unlinked by `_activate_project`'s finally."""
    label = windows.window_name(e.project, e.branch)
    new_args = e.project + (f" {e.branch}" if e.branch else "")
    body = (
        f"\n  agent-restore failed for {label}\n  reason: {reason}\n\n"
        "  Fix the underlying issue (e.g. start Docker) and re-run:\n"
        "    agent-restore\n\n"
        "  Or remove this window with Ctrl-Space K and re-spawn manually:\n"
        f"    agent-new {new_args}\n\n"
    )
    startup.show_static_text(ph.pane_id, body)
    startup._write_pane_state(
        e.host_worktree, ph.pane_id.lstrip("%"), phase_value=phase.ERRORED
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


def _pre_create_revive(
    e: Entry, live_panes: dict[str, set[str]]
) -> "Placeholder | None":
    """Split a new agent pane above the surviving overview pane.

    Normal case: exactly one pane survives (the overview) — split above it.
    Degenerate case: more than one survives, e.g. a duplicate overview pane
    left by a layout toggle on an already-agent-dead window. Keep one overview
    pane as the split target and reap the extras so the window ends up with
    exactly overview + new agent. Returns None (logs) when nothing survives, or
    when several panes survive but none is tagged overview (can't place agent)."""
    survivors = live_panes.get(e.window_id, set())
    if not survivors:
        logger.warning("%s: cannot revive — no surviving pane", e.window_id)
        return None
    if len(survivors) == 1:
        target = next(iter(survivors))
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
        target = overview_panes[0]
        for extra in overview_panes[1:]:
            logger.info("%s: reaping duplicate overview pane %s", e.window_id, extra)
            tmux.kill_pane(extra)
    new_full_pane_id = tmux.split_window(
        target,
        percent=75,
        command=startup.placeholder_command(e.window_id),
        before=True,
    )
    # split_window uses `-d` to keep focus on the original pane — correct for
    # fresh windows (original = agent), wrong for revive (original = overview).
    tmux.select_pane(new_full_pane_id)
    new_pane_stripped = new_full_pane_id.lstrip("%")
    _clean_old_pane_files(e.host_worktree, e.pane_id)
    windows.write_mapping(
        windows.WindowMapping(
            window_id=e.window_id,
            project=e.project,
            branch=e.branch,
            host_worktree=e.host_worktree,
            pane_id=new_pane_stripped,
            claude_session_id=e.claude_session_id,
        )
    )
    startup._write_pane_state(
        e.host_worktree, new_pane_stripped, phase_value=phase.STARTING
    )
    logger.info(
        "%s: revived -> pane=%s (split %s)", e.window_id, new_full_pane_id, target
    )
    return Placeholder(e, e.window_id, new_full_pane_id)


def pre_create_windows(
    plan: list[Entry], live_panes: dict[str, set[str]]
) -> dict[str, Placeholder]:
    """Create placeholder panes for each plan entry, branching on kind."""
    placeholders: dict[str, Placeholder] = {}
    layout = paths.read_layout()
    for e in plan:
        try:
            if e.kind == "revive":
                ph = _pre_create_revive(e, live_panes)
                if ph is not None:
                    placeholders[ph.new_window_id] = ph
                continue
            # fresh: existing path
            new_wid = tmux.new_window(
                tmux.SESSION,
                name=windows.window_name(e.project, e.branch),
                command="sh -c 'while :; do sleep 3600; done'",
            )
            if e.branch:
                tmux.set_window_option(new_wid, "@pinned", "1")
            full_pane_id = tmux.active_pane_id(new_wid)
            startup._respawn_with_retry(
                full_pane_id, startup.placeholder_command(new_wid)
            )
            pane_stripped = full_pane_id.lstrip("%")
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
                )
            )
            startup._write_pane_state(
                e.host_worktree, pane_stripped, phase_value=phase.STARTING
            )
            placeholders[new_wid] = Placeholder(e, new_wid, full_pane_id)
            logger.info(
                "%s: pre-created -> %s pane=%s", e.window_id, new_wid, full_pane_id
            )
        except Exception:
            logger.error("%s: pre-create failed", e.window_id, exc_info=True)
    return placeholders


def _build_exec_cmd(proj, e: Entry, container_name: str | None) -> str:
    """Substitute exec_cmd, injecting ` --resume <id>` via {resume_args}."""
    resume_args = ""
    if e.claude_session_id:
        resume_args = f" --resume {shlex.quote(e.claude_session_id)}"
        if "{resume_args}" not in proj.exec_cmd:
            logger.warning(
                "%s: project %r has a custom exec_cmd without {resume_args} placeholder; "
                "Claude will not auto-resume. Add {resume_args} after `claude` in "
                "projects.toml to enable resume.",
                e.window_id,
                proj.name,
            )
    return proj.substitute(
        proj.exec_cmd,
        branch=e.branch,
        container_name=container_name,
        resume_args=resume_args,
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
        try:
            if proj.is_container:
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
        except container.ContainerError as ce:
            for e in entries:
                _fail(e, f"container start failed: {ce}")
            return  # finally block runs, cleaning up logs

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
                try:
                    cmd = _build_exec_cmd(proj, e, container_name)
                    startup._respawn_with_retry(ph.pane_id, cmd)
                    logger.info(
                        "%s: respawned pane=%s cmd_preview=%r",
                        e.window_id,
                        ph.pane_id,
                        cmd[:80],
                    )
                except Exception as ex:
                    _fail(e, f"respawn-pane failed: {type(ex).__name__}: {ex}")
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

    def _fail(e: Entry, msg: str) -> None:
        logger.error("%s: %s", e.window_id, msg)
        ph = by_entry_window.get(e.window_id)
        if ph is not None:
            _mark_entry_failed(e, ph, msg)

    # Up to 4 concurrent projects to keep docker honest under load.
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures: list[Future] = [
            ex.submit(
                _activate_project, name, list(entries), projects, by_entry_window, _fail
            )
            for name, entries in group_entries_by_project(plan).items()
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
