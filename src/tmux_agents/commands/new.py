"""Thin orchestrator for `agent-new`. See `docs/ARCHITECTURE.md` for
the spawn-time pipeline that ties together config, container, worktree,
ssh_forward, tmux, provisioning, and windows."""

from __future__ import annotations
import argparse
import dataclasses
import logging
import os
import shlex
import subprocess
import sys
import time
from importlib import resources
from tmux_agents import (
    config,
    container,
    logging_setup,
    overview,
    paths,
    phase,
    pickers,
    progress,
    provisioning,
    ssh_forward,
    startup,
    tmux,
    worktree,
)
from tmux_agents import windows as windows_mod

logger = logging.getLogger(__name__)


def _ensure_session() -> None:
    if not tmux.session_exists(tmux.SESSION):
        tmux.new_session(tmux.SESSION, window_name=tmux.CONTROL_WINDOW)


def _is_valid_branch(name: str) -> bool:
    return (
        subprocess.run(
            ["git", "check-ref-format", "--branch", name],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


_OPEN_SUFFIX = "  (open)"


def _decode_branch_choice(choice: str | None) -> str | None:
    """Map a `pick_or_create` return value to the branch arg.

    - The sentinel and `None` (empty input) both mean "no branch".
    - The `(open)` suffix is a display marker only; strip it.
    - Any other string is a real branch name (validated by the picker).
    """
    if choice is None or choice == pickers.NO_BRANCH_SENTINEL:
        return None
    if choice.endswith(_OPEN_SUFFIX):
        return choice[: -len(_OPEN_SUFFIX)]
    return choice


def _spawn_worker(
    window_id: str, pane_id: str, project: str, branch: str | None
) -> None:
    """Fire-and-forget the detached provisioning worker via the tmux server
    (`run-shell -b`) so it survives the popup closing.

    The interactive part runs inside a `display-popup` that closes the instant
    this returns. A `subprocess.Popen(..., start_new_session=True)` spawned from
    inside that popup does NOT survive — tmux tears down the popup's process
    tree on close and kills the worker before it does any work. Launching via
    the long-lived tmux server keeps the worker alive."""
    argv = [
        "agent-new",
        "--provision",
        "--window-id",
        window_id,
        "--pane-id",
        pane_id,
        "--project",
        project,
    ]
    if branch:
        argv += ["--branch", branch]
    tmux.run_shell_bg(shlex.join(argv))


def _last_sibling_window_id(project: str) -> str | None:
    """Return the window_id of the highest-indexed live window for `project`,
    or `None` if none exist. Used to insert the new window adjacent to its
    project siblings so tab-bar numbering stays grouped (`renumber-windows
    on` then collapses the indices). Project identity comes from the
    persisted window mapping, not the tmux window name — names can be
    overridden via `agent-rename` or raw `tmux rename-window`."""
    try:
        live = tmux.list_windows(tmux.SESSION)
    except Exception:
        logger.warning(
            "_last_sibling_window_id: tmux.list_windows failed",
            exc_info=True,
        )
        return None
    siblings: list[tmux.Window] = []
    for w in live:
        if w.name == tmux.CONTROL_WINDOW:
            continue
        m = windows_mod.read_mapping(w.id)
        if m is not None and m.project == project:
            siblings.append(w)
    if not siblings:
        return None
    return max(siblings, key=lambda w: w.index).id


def _provision(
    *, window_id: str, pane_id: str, project: str, branch: str | None
) -> int:
    """Detached worker: run the slow startup stages against the spawn log,
    then swap the placeholder pane into Claude. Failures/warnings are handled
    per the async-startup spec."""
    logging_setup.setup_logging()
    projects = config.safe_load(
        paths.projects_toml(), on_error=lambda msg: logger.error(msg)
    )
    proj = projects.get(project)
    full_pane = f"%{pane_id}"

    def _fatal(reason: str, *, worktree_path=None) -> int:
        label = windows_mod.window_name(project, branch)
        new_args = project + (f" {branch}" if branch else "")
        body = (
            f"\n  agent-new failed for {label}\n  reason: {reason}\n\n"
            "  Fix the underlying issue (e.g. start Docker), then re-run:\n"
            f"    agent-new {new_args}\n\n"
            "  Or remove this window with Ctrl-Space K.\n\n"
        )
        startup.show_static_text(full_pane, body)
        if worktree_path is not None:
            startup._write_pane_state(worktree_path, pane_id, phase_value=phase.ERRORED)
        else:
            # Worktree doesn't exist yet — flip the host-side hint to errored.
            m = windows_mod.read_mapping(window_id)
            if m is not None:
                windows_mod.write_mapping(
                    dataclasses.replace(m, phase_hint=phase.ERRORED)
                )
        logger.error("%s: %s", window_id, reason)
        return 4

    if proj is None:
        return _fatal(f"unknown project {project!r}")

    try:
        log_path = paths.spawn_log(window_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", buffering=1) as f:
            reporter = progress.Reporter(out=f, color=True, clock=time.monotonic)
            reporter.banner(f"Spawning agent: {proj.name} / {branch or '(no branch)'}")

            container_name: str | None = None
            container_workdir: str | None = None
            if proj.is_container:
                try:
                    with reporter.stage("container") as st:
                        existing = container.current_name(proj)
                        if existing:
                            st.skip("already running")
                            container_name = existing
                        else:
                            st.info("building (this may take minutes)…")
                            container_name = container.ensure_up(
                                proj,
                                up_cmd=proj.substitute(proj.up_cmd, branch=branch)
                                if proj.up_cmd
                                else None,
                            )
                except container.ContainerError as ce:
                    return _fatal(f"container start failed: {ce}")
                container_workdir = proj.workdir_for(None)
                if proj.forward_ssh_agent:
                    with reporter.stage("ssh pump") as st:
                        ssh_forward.maybe_spawn_pump(
                            container_name, proj.user or "vscode"
                        ).render(st)

            if branch is not None:
                try:
                    with reporter.stage("worktree") as st:
                        wt_path = worktree.resolve(
                            proj.repo,
                            branch,
                            base_override=proj.base_branch,
                            container=container_name,
                            container_workdir=container_workdir,
                            container_user=proj.user,
                            reporter_stage=st,
                        )
                except worktree.WorktreeError as we:
                    return _fatal(f"worktree resolve failed: {we}")
            else:
                wt_path = proj.repo
                with reporter.stage("base freshness") as st:
                    worktree.check_freshness(
                        proj.repo,
                        base_override=proj.base_branch,
                        container=container_name,
                        container_workdir=container_workdir,
                        container_user=proj.user or "vscode",
                        reporter_stage=st,
                    )

            # Worktree confirmed: rewrite the mapping with the real path, clear the hint.
            m = windows_mod.read_mapping(window_id)
            if m is not None:
                windows_mod.write_mapping(
                    dataclasses.replace(m, host_worktree=wt_path, phase_hint=None)
                )

            with reporter.stage("hooks") as st:
                try:
                    with resources.as_file(
                        resources.files("tmux_agents.hooks") / "agents.json"
                    ) as template_path:
                        provisioning.provision_settings(
                            wt_path, template_path=template_path
                        )
                except Exception as e:
                    st.warn(
                        f"could not provision .claude/settings.local.json: {type(e).__name__}: {e}"
                    )
                    logger.warning(
                        "%s: provisioning failed (non-fatal)", window_id, exc_info=True
                    )

            cmd = proj.substitute(
                proj.exec_cmd,
                branch=branch,
                container_name=container_name,
                resume_args="",
            )

        # Log file closed. Swap the pane into Claude (or hold on warning).
        if reporter.had_warning:
            startup._write_pane_state(wt_path, pane_id, phase_value=phase.WAITING)
            startup.hold_pane_then_exec(full_pane, log_path, cmd)
        else:
            startup._respawn_with_retry(full_pane, cmd)
        logger.info(
            "%s: provisioned, pane=%s warning=%s",
            window_id,
            full_pane,
            reporter.had_warning,
        )
        return 0
    except Exception as e:
        logger.error(
            "%s: unexpected error in provisioning worker", window_id, exc_info=True
        )
        return _fatal(f"unexpected error: {type(e).__name__}: {e}")


def main(argv: list[str] | None = None) -> int:
    logging_setup.setup_logging()
    parser = argparse.ArgumentParser(prog="agent-new")
    parser.add_argument("project", nargs="?", default=None)
    parser.add_argument("branch", nargs="?", default=None)
    parser.add_argument(
        "--provision",
        action="store_true",
        help="internal: run the detached provisioning worker",
    )
    parser.add_argument("--window-id")
    parser.add_argument("--pane-id")
    parser.add_argument("--project", dest="project_opt")
    parser.add_argument("--branch", dest="branch_opt")
    args = parser.parse_args(argv)

    if args.provision:
        if os.fork() > 0:
            return 0
        os.setsid()
        startup._detach_stdio()
        return _provision(
            window_id=args.window_id,
            pane_id=args.pane_id,
            project=args.project_opt,
            branch=args.branch_opt,
        )

    try:
        projects = config.load(paths.projects_toml())
    except FileNotFoundError:
        logging_setup.cli_error(logger, f"{paths.projects_toml()} not found")
        return 2

    project_name = args.project
    branch = args.branch
    if project_name is None:
        if not projects:
            logging_setup.cli_error(logger, "no projects defined in projects.toml")
            return 2
        try:
            project_name = pickers.pick_one(sorted(projects), prompt="project> ")
            if not project_name:
                return 0
            if branch is None:
                if project_name in projects:
                    repo = projects[project_name].repo
                    existing = worktree.list_existing(repo)
                    live = windows_mod.live_branches_for(project_name)
                else:
                    existing, live = [], set()
                candidates = [pickers.NO_BRANCH_SENTINEL] + [
                    f"{b}{_OPEN_SUFFIX}" if b in live else b for b in existing
                ]
                choice = pickers.pick_or_create(
                    candidates,
                    prompt="branch (Esc to cancel)> ",
                    validator=_is_valid_branch,
                )
                branch = _decode_branch_choice(choice)
        except (KeyboardInterrupt, pickers.Cancelled):
            print("cancelled", file=sys.stderr)
            logger.info("cancelled")
            return 0

    if project_name not in projects:
        logging_setup.cli_error(logger, f"unknown project {project_name!r}")
        return 2

    if branch is not None and not _is_valid_branch(branch):
        logging_setup.cli_error(logger, f"invalid branch name {branch!r}")
        return 2

    proj = projects[project_name]

    _ensure_session()

    # The window is created immediately with a placeholder pane tailing the
    # spawn log; the detached worker fills it in and respawns into Claude.
    window_id = tmux.new_window(
        tmux.SESSION,
        name=windows_mod.window_name(proj.name, branch),
        command="sh -c 'while :; do sleep 3600; done'",  # replaced immediately below
        after_target=_last_sibling_window_id(proj.name),
    )
    if branch:
        tmux.set_window_option(window_id, "@pinned", "1")
    full_pane_id = tmux.active_pane_id(window_id)
    startup._respawn_with_retry(full_pane_id, startup.placeholder_command(window_id))
    pane_id = full_pane_id.lstrip("%")
    # Best-effort: clear any stale per-pane state from a previous session so a
    # recycled pane id doesn't show a stale letter during startup.
    paths.worktree_state_file(proj.repo, pane_id).unlink(missing_ok=True)
    paths.worktree_session_id_file(proj.repo, pane_id).unlink(missing_ok=True)
    windows_mod.write_mapping(
        windows_mod.WindowMapping(
            window_id=window_id,
            project=proj.name,
            branch=branch,
            host_worktree=proj.repo,
            pane_id=pane_id,
            phase_hint=phase.STARTING,
        )
    )
    if paths.read_layout() == "split":
        try:
            overview.attach_overview_pane(window_id)
        except Exception:
            logger.warning("%s: overview-pane attach failed", window_id, exc_info=True)
    tmux.select_window(window_id)
    _spawn_worker(window_id, pane_id, proj.name, branch)
    return 0
