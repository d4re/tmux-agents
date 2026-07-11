"""Thin orchestrator for `agent-kill`: window picker, optional
`git worktree remove` with interactive force-retry on dirty."""
from __future__ import annotations
import argparse
import logging
import sys
from tmux_agents import (
    config, container, logging_setup, paths, pickers, tmux, worktree,
)
from tmux_agents import windows as windows_mod

logger = logging.getLogger(__name__)

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-kill")
    p.add_argument("window", nargs="?", default=None, help="window index (e.g. '2')")
    p.add_argument("--window-id", default=None,
                   help="tmux window id (e.g. '@5'); skips the picker")
    p.add_argument("--prune-worktree", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="pass --force to git worktree remove; requires --prune-worktree")
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
    return {"container": name, "container_workdir": proj.workdir_for(None), "container_user": proj.user}

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
        print("hint: commit or stash the changes, or re-run with --force", file=sys.stderr)
        return 3
    except worktree.WorktreeError as e:
        logging_setup.cli_error(logger, str(e))
        return 3
    return 0

def _interactive_prune(win: tmux.Window) -> int:
    """Remove the worktree, prompting for force on dirty. 0 = caller should kill."""
    target = _resolve_prune_target(win)
    if target is None:
        return 0
    if target is False:
        return 1
    proj, branch, kw = target
    # The fzf prompts leave the popup blank, and removal through a container
    # bind mount can take many seconds — announce it so it doesn't look hung.
    print(f"removing worktree {branch}… (large worktrees can take a while)", flush=True)
    try:
        worktree.remove(proj.repo, branch, force=False, **kw)
        print("worktree removed", flush=True)
        return 0
    except worktree.DirtyWorktreeError:
        if not pickers.prompt_yes_no(
            f"{win.name}: worktree has uncommitted changes; force remove? > ", default=False,
        ):
            return 1
        print(f"force removing worktree {branch}…", flush=True)
        try:
            worktree.remove(proj.repo, branch, force=True, **kw)
            print("worktree removed", flush=True)
            return 0
        except worktree.WorktreeError as e:
            logging_setup.cli_error(logger, str(e))
            return 1
    except worktree.WorktreeError as e:
        logging_setup.cli_error(logger, str(e))
        return 1

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

def _kill_with_optional_prune(win: tmux.Window) -> int:
    """Prompt for worktree pruning (only when a worktree exists) then kill."""
    if _has_worktree(win):
        try:
            prune = pickers.prompt_yes_no(f"prune worktree for {win.name}? > ", default=True)
        except (KeyboardInterrupt, pickers.Cancelled):
            return 0
        if prune and _interactive_prune(win) != 0:
            return 0
    tmux.kill_window(win.id)
    return 0


def _interactive() -> int:
    windows = [w for w in tmux.list_windows(tmux.SESSION) if w.name != tmux.CONTROL_WINDOW]
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

    tmux.kill_window(win.id)
    return 0
