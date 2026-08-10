"""`agent-other` entry point: start, revive, or switch focus to the
*secondary* agent — the kind other than the window's default slot's kind
(claude<->codex) — in the active agent window.

One smart action per invocation (spec:
docs/superpowers/specs/2026-07-17-codex-support-design.md Section 4):

1. No window mapping (includes the ctrl window, which has none) -> notice.
2. Default slot's pane is gone -> notice pointing at Ctrl-Space R restore.
3. Secondary slot live -> focus-jump between the two agent panes.
4. Secondary absent or dead -> start it: ensure-provisioned + executable
   pre-flight + split + scrub + respawn + publish, all under the
   per-worktree cleanup lock so a concurrent `agent-other` cannot double-spawn.

Publication is always the LAST step of the start branch, so any exception
up to and including the publish itself just kills the pane it created and
scrubs its files — the mapping is never left pointing at a broken pane.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import shlex
import shutil
import subprocess

from tmux_agents import (
    agent_kind,
    codex_hooks,
    config,
    container,
    exec_cmd,
    locks,
    logging_setup,
    paths,
    startup,
    tmux,
)
from tmux_agents import windows as windows_mod
from tmux_agents.config import Project
from tmux_agents.windows import AgentSlot, WindowMapping

logger = logging.getLogger(__name__)


def _notice(msg: str) -> int:
    """Flash `msg` verbatim and return 0 — used for the no-op branches
    (no mapping, default down, provisioning/pre-flight abort) where nothing
    was created and there is nothing to roll back."""
    tmux.display_message(msg)
    return 0


def _fail(msg: str) -> int:
    logging_setup.cli_error(logger, msg)
    tmux.display_message(f"agent-other: {msg}")
    return 1


def _focus_jump(window_id: str, mapping: WindowMapping) -> None:
    """Select the OTHER agent pane relative to whichever pane is currently
    active. If the active pane is neither agent pane, default to the
    secondary (matches "both live -> jump focus between the two")."""
    default_pane = f"%{mapping.default_slot.pane_id}"
    secondary = mapping.secondary_slot
    if secondary is None or secondary.pane_id is None:
        return
    secondary_pane = f"%{secondary.pane_id}"
    active = tmux.active_pane_id(window_id)
    target = default_pane if active == secondary_pane else secondary_pane
    tmux.select_pane(target)


def _container_has_exe(container_name: str, user: str, exe: str) -> bool:
    try:
        container.exec_capture(container_name, user, f"command -v {shlex.quote(exe)}")
        return True
    except subprocess.CalledProcessError:
        return False


def _rollback(pane_id: str, worktree, pane_id_stripped: str) -> None:
    try:
        tmux.kill_pane(pane_id)
    except Exception:
        logger.warning(
            "agent-other: rollback kill_pane failed for %s", pane_id, exc_info=True
        )
    startup.scrub_pane_files(worktree, pane_id_stripped)


def _start(mapping: WindowMapping, proj: Project, window_id: str) -> int:
    """Ensure-provisioned + executable pre-flight run BEFORE the cleanup
    lock is acquired: both are idempotent, read-only-with-respect-to-
    worktree-state checks (docker execs, a `command -v`/`which` probe) —
    holding a per-worktree lock across them would only stretch a
    start-vs-start race's serialized window for no benefit, and would nest
    `codex-hooks.lock` inside `worktree_cleanup_lock` (the one nesting the
    lock-discipline doc rules out). The lock's hold time covers only
    re-read -> split -> scrub -> respawn -> publish."""
    worktree = mapping.host_worktree
    other_kind = agent_kind.other(mapping.default_slot.kind)
    user = proj.user or "vscode"

    container_name = None
    if proj.is_container:
        container_name = container.current_name(proj)
        if not container_name:
            return _notice(f"agent-other: no running container for {proj.name!r}")

    if other_kind == agent_kind.CODEX:
        try:
            if proj.is_container:
                codex_hooks.ensure_container(container_name, user)
            else:
                codex_hooks.ensure_host()
        except Exception as ex:
            logger.warning(
                "agent-other: codex hook provisioning failed for %s",
                window_id,
                exc_info=True,
            )
            return _notice(f"agent-other: codex hook provisioning failed: {ex}")

    explicit = (
        proj.codex_exec_cmd_explicit
        if other_kind == agent_kind.CODEX
        else proj.exec_cmd_explicit
    )
    if not explicit:
        exe = agent_kind.executable(other_kind)
        found = (
            _container_has_exe(container_name, user, exe)
            if proj.is_container
            else shutil.which(exe) is not None
        )
        if not found:
            where = "container" if proj.is_container else "PATH"
            return _notice(f"agent-other: `{exe}` not found on {where}")

    with locks.locked(paths.worktree_cleanup_lock(worktree)):
        # Re-read the mapping inside the cleanup lock and re-check
        # eligibility: the loser of a start-vs-start race sees the winner's
        # freshly-published secondary and falls through to focus-jump
        # instead of splitting a second pane.
        fresh = windows_mod.read_mapping(window_id)
        if fresh is None:
            return _notice("agent-other: no agent window")
        secondary = fresh.secondary_slot
        if secondary is not None and secondary.pane_id is not None:
            _focus_jump(window_id, fresh)
            return 0

        default_pane = f"%{fresh.default_slot.pane_id}"
        new_pane = tmux.split_window(
            default_pane,
            percent=50,
            command=startup.placeholder_command(window_id),
            horizontal=True,
        )
        new_pane_stripped = new_pane.lstrip("%")
        try:
            startup.scrub_pane_files(worktree, new_pane_stripped)
            session_id = secondary.session_id if secondary is not None else None
            cmd = exec_cmd.build(
                proj,
                branch=fresh.branch,
                session_id=session_id,
                container_name=container_name,
                kind=other_kind,
                label=window_id,
            )
            tmux.respawn_pane(new_pane, command=cmd)

            def publish(m: WindowMapping | None) -> WindowMapping | None:
                if m is None:
                    return None
                agents = list(m.agents)
                if len(agents) > 1:
                    agents[1] = dataclasses.replace(
                        agents[1], pane_id=new_pane_stripped
                    )
                else:
                    agents.append(AgentSlot(kind=other_kind, pane_id=new_pane_stripped))
                return dataclasses.replace(m, agents=agents)

            updated = windows_mod.update_mapping(window_id, publish)
            if updated is None:
                raise RuntimeError(
                    f"mapping for {window_id} disappeared before publish"
                )
        except Exception:
            logger.warning(
                "agent-other: start failed for %s, rolling back pane %s",
                window_id,
                new_pane,
                exc_info=True,
            )
            _rollback(new_pane, worktree, new_pane_stripped)
            raise
    return 0


def main(argv: list[str] | None = None) -> int:
    logging_setup.setup_logging()
    parser = argparse.ArgumentParser(prog="agent-other")
    parser.add_argument("--window-id", required=True)
    args = parser.parse_args(argv)
    window_id = args.window_id

    mapping = windows_mod.read_mapping(window_id)
    if mapping is None:
        return _notice("agent-other: no agent window")

    try:
        live = tmux.window_pane_map(tmux.SESSION).get(window_id, set())
    except subprocess.CalledProcessError:
        return _notice("agent-other: tmux unavailable")

    default_id = mapping.default_slot.pane_id
    if default_id is None or f"%{default_id}" not in live:
        return _notice("default agent down — Ctrl-Space R to restore")

    secondary = mapping.secondary_slot
    if secondary is not None and secondary.pane_id is not None:
        try:
            _focus_jump(window_id, mapping)
        except tmux.TmuxError as ex:
            # A pane dying between the liveness check above and the
            # select-pane call (e.g. the user just killed it) must surface
            # as a friendly notice, not an unhandled traceback.
            return _notice(f"agent-other: focus-jump failed: {ex}")
        return 0

    proj = config.safe_load(paths.projects_toml(), on_error=logger.warning).get(
        mapping.project
    )
    if proj is None:
        return _fail(f"project {mapping.project!r} not in projects.toml")

    try:
        return _start(mapping, proj, window_id)
    except Exception as ex:
        logger.warning("agent-other: start failed for %s", window_id, exc_info=True)
        return _fail(f"start failed: {ex}")
