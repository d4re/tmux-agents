"""Thin orchestrator for `agent-kill`: window picker, optional
`git worktree remove`. Interactively, the prune/force questions are asked
in the popup, then the window is killed and the removal itself runs in a
detached worker (`--finish-removal`, tracked in `tmux_agents.removals`)."""

from __future__ import annotations

import argparse
import logging
import os
import shlex
import sys

from tmux_agents import (
    config,
    container,
    logging_setup,
    paths,
    pickers,
    removals,
    sandbox,
    startup,
    tmux,
    worktree,
)
from tmux_agents import windows as windows_mod

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-kill")
    p.add_argument("window", nargs="?", default=None, help="window index (e.g. '2')")
    p.add_argument(
        "--window-id", default=None, help="tmux window id (e.g. '@5'); skips the picker"
    )
    p.add_argument("--prune-worktree", action="store_true")
    p.add_argument(
        "--finish-removal",
        metavar="KEY",
        default=None,
        help=argparse.SUPPRESS,  # internal: background worktree-removal worker
    )
    p.add_argument(
        "--show-removal",
        metavar="KEY",
        default=None,
        help=argparse.SUPPRESS,  # internal: overview details popup
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="pass --force to git worktree remove; requires --prune-worktree",
    )
    return p


def _format_line(win: tmux.Window) -> str:
    return f"{win.index}\t{win.name}\t{win.state_code or '?'}"


def _window_from_line(line: str, windows: list[tmux.Window]) -> tmux.Window:
    idx = int(line.split("\t", 1)[0])
    return next(w for w in windows if w.index == idx)


def _container_kwargs(proj: config.Project, branch: str) -> dict | None:
    """Container kwargs for worktree.remove; None on container failure (error printed)."""
    if not proj.is_container:
        return {}
    print(f"checking container for {proj.name}…", flush=True)
    try:
        name = container.ensure_up(
            proj,
            up_cmd=proj.substitute(proj.up_cmd, branch=branch) if proj.up_cmd else None,
        )
    except container.ContainerError as e:
        logging_setup.cli_error(logger, str(e))
        return None
    return {
        "container": name,
        "container_workdir": proj.workdir_for(None),
        "container_user": proj.user,
    }


def _resolve_prune_target(win: tmux.Window):
    """Return (proj, branch, container_kwargs) for `win`, None to skip silently
    (no branch or unknown project), or False on container failure (error printed)."""
    project, _, branch = win.name.partition(":")
    if not branch:
        return None
    proj = config.safe_load(paths.projects_toml()).get(project)
    if proj is None:
        return None
    kw = _container_kwargs(proj, branch)
    if kw is None:
        return False
    return proj, branch, kw


def _prune(win: tmux.Window, *, force: bool) -> int:
    target = _resolve_prune_target(win)
    if target is None:
        return 0
    if target is False:
        return 4
    proj, branch, kw = target
    try:
        worktree.remove(proj.repo, branch, force=force, **kw)
    except worktree.DirtyWorktreeError as e:
        logging_setup.cli_error(logger, f"worktree has uncommitted changes: {e}")
        print(
            "hint: commit or stash the changes, or re-run with --force", file=sys.stderr
        )
        return 3
    except worktree.WorktreeError as e:
        logging_setup.cli_error(logger, str(e))
        return 3
    return 0


def _plan_removal(win: tmux.Window) -> removals.Removal | None | bool:
    """Ask the prune questions up front, while the popup is still open.

    Returns the removal to queue in the background, None to kill without
    removing anything, or False to keep the window (container failure, or
    the user declined to force-remove uncommitted changes)."""
    target = _resolve_prune_target(win)
    if target is None:
        return None
    if target is False:
        return False
    proj, branch, kw = target

    def plan(*, force: bool = False, leftover: bool = False) -> removals.Removal:
        return removals.Removal(
            key=removals.new_key(win.id),
            name=win.name,
            project=proj.name,
            branch=branch,
            repo=str(proj.repo),
            force=force,
            leftover=leftover,
            container=kw.get("container"),
            container_workdir=kw.get("container_workdir"),
            container_user=kw.get("container_user"),
        )

    if worktree.is_husk(proj.repo, branch):
        # Husk dir: exists on disk but git doesn't know it (no .git entry).
        # Without this branch the kill wedged forever — git refuses the
        # remove, and the window (plus its restore mapping) survived.
        if pickers.prompt_yes_no(
            "remove the leftover folder? > ",
            default=True,
            header=(
                f".worktrees/{branch} is not a git worktree\n"
                "(no .git — leftover files only; either way the agent is killed)"
            ),
        ):
            return plan(leftover=True)
        return None

    # The fzf prompts leave the popup blank — say what we're waiting on.
    print(f"checking {branch} for uncommitted changes…", flush=True)
    try:
        dirty = worktree.is_dirty(proj.repo, branch, **kw)
    except worktree.WorktreeError as e:
        # Can't tell up front; let the background removal surface the error.
        logger.warning("%s: dirty check failed: %s", win.id, e)
        dirty = False
    if not dirty:
        return plan()
    if not pickers.prompt_yes_no(
        f"{win.name}: worktree has uncommitted changes; force remove? > ",
        default=False,
    ):
        return False
    return plan(force=True)


def _queue_removal(r: removals.Removal) -> None:
    """Record the removal and start its detached worker. Must go through
    `run-shell -b`: tmux tears down the popup's process tree on close, so a
    Popen'd child would die with it (see agent-new's `_spawn_worker`)."""
    removals.save(r)
    try:
        tmux.run_shell_bg(shlex.join(["agent-kill", "--finish-removal", r.key]))
    except Exception as e:  # noqa: BLE001 — surface on the overview row instead
        logger.error("could not start removal worker for %s: %s", r.name, e)
        r.status, r.error = removals.FAILED, f"could not start removal worker: {e}"
        removals.save(r)


def _has_worktree(win: tmux.Window) -> bool:
    """True iff this window was spawned with a branch and its worktree dir exists.

    Window names are unreliable: `pane-title-changed` auto-rename turns
    branchless `<repo>` into `<repo>:<pane title>` (see agent-rename), so
    `":" in win.name` matches even when there's nothing to prune. The
    windows mapping records the original branch passed to `agent-new`.
    """
    m = windows_mod.read_mapping(win.id)
    if m is None or m.branch is None:
        return False
    return m.host_worktree.is_dir()


def _reap_guest_agents(win: tmux.Window) -> None:
    """Sandbox projects: terminate the window's agents inside the VM.

    Killing the tmux pane only kills the host-side `sbx exec` client; the
    guest `claude`/`codex` keeps running — invisible, still holding its
    (possibly just-removed) worktree, which the guest then keeps listing.
    Best-effort: a failure here must never block the kill itself."""
    m = windows_mod.read_mapping(win.id)
    if m is None:
        return
    proj = config.safe_load(paths.projects_toml()).get(m.project)
    if proj is None or proj.backend != config.BACKEND_SANDBOX:
        return
    pane_ids = sorted(
        {p for slot in m.agents for p in (slot.pane_id, slot.last_pane_id) if p}
    )
    try:
        n = sandbox.reap_pane_agents(proj.sandbox_name, pane_ids)
    except sandbox.SandboxError as e:
        logger.warning("%s: could not stop agents in sandbox: %s", win.id, e)
        return
    if n:
        logger.info(
            "%s: stopped %d agent process(es) in sandbox %s",
            win.id,
            n,
            proj.sandbox_name,
        )


def _kill_with_optional_prune(win: tmux.Window) -> int:
    """Prompt for worktree pruning (only when a worktree exists), kill the
    window, then remove the worktree in the background — the popup closes
    right away and the overview shows the removal's progress/failure."""
    planned = None
    if _has_worktree(win):
        try:
            if pickers.prompt_yes_no(
                f"prune worktree for {win.name}? > ", default=True
            ):
                planned = _plan_removal(win)
        except (KeyboardInterrupt, pickers.Cancelled):
            return 0
        if planned is False:
            return 0
    # Kill first so the agent can't keep writing into a folder being deleted.
    _reap_guest_agents(win)
    tmux.kill_window(win.id)
    tmux.kill_term_session(win.id)
    # Deliberate kill: drop the mapping now rather than leaving it to the
    # tick's grace-period GC, so the agent can't reappear in a restore prompt.
    windows_mod.forget(win.id)
    if isinstance(planned, removals.Removal):
        _queue_removal(planned)
    return 0


def _show_removal(key: str) -> int:
    """Details view for a failed background removal (overview ↵ on its row)."""
    r = removals.get(key)
    if r is None:
        print("this removal already finished or was dismissed")
    else:
        print(f"{r.name}: worktree removal {r.status}\n")
        print(f"  {r.path}\n")
        if r.error:
            print(r.error + "\n")
        print(
            "The agent itself is already stopped. Fix the cause and delete the\n"
            "worktree by hand (e.g. `git worktree remove --force <path>`), then\n"
            "press k on the row in the overview to dismiss it."
        )
    try:
        input("\npress Enter to close")
    except (EOFError, KeyboardInterrupt):
        pass
    return 0


def _interactive() -> int:
    windows = [
        w for w in tmux.list_windows(tmux.SESSION) if w.name != tmux.CONTROL_WINDOW
    ]
    if not windows:
        print("no agent windows to kill", file=sys.stderr)
        logger.info("no agent windows to kill")
        return 0
    lines = [_format_line(w) for w in windows]
    active = next((i for i, w in enumerate(windows, 1) if w.active), None)
    pick = pickers.pick_one(lines, prompt="kill window> ", start_index=active)
    if pick is None:
        return 0
    return _kill_with_optional_prune(_window_from_line(pick, windows))


def main(argv: list[str] | None = None) -> int:
    logging_setup.setup_logging()
    args = _parser().parse_args(argv)

    if args.finish_removal is not None:
        # Same detach dance as `agent-rebuild --worker`: run-shell -b keeps
        # the job's output pipe and tmux would paint any output over the
        # active pane. Fork, drop the pipe, let the parent exit 0.
        if os.fork() > 0:
            return 0
        os.setsid()
        startup._detach_stdio()
        return removals.run(args.finish_removal)

    if args.show_removal is not None:
        return _show_removal(args.show_removal)

    if args.force and not args.prune_worktree:
        logging_setup.cli_error(logger, "--force requires --prune-worktree")
        return 2

    if args.window_id is not None:
        windows = tmux.list_windows(tmux.SESSION)
        win = next((w for w in windows if w.id == args.window_id), None)
        if win is None:
            logging_setup.cli_error(logger, f"no window with id {args.window_id}")
            return 2
        return _kill_with_optional_prune(win)

    if args.window is None:
        try:
            return _interactive()
        except (KeyboardInterrupt, pickers.Cancelled):
            return 0

    try:
        idx = int(args.window)
    except ValueError:
        logging_setup.cli_error(logger, "window must be an integer")
        return 2

    windows = tmux.list_windows(tmux.SESSION)
    win = next((w for w in windows if w.index == idx), None)
    if win is None:
        logging_setup.cli_error(logger, f"no window with index {idx}")
        return 2

    if args.prune_worktree:
        rc = _prune(win, force=args.force)
        if rc != 0:
            return rc

    _reap_guest_agents(win)
    tmux.kill_window(win.id)
    tmux.kill_term_session(win.id)
    return 0
