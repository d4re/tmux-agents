"""`agent-terminal` entry point.

Pops a shell rooted at the active agent's worktree — host projects do
`chdir` + `exec $SHELL -il`; container/devcontainer projects exec into
`docker exec -it -u <user> -w <workdir> <container> bash -il` with the
same env forwarding (TERM, COLORTERM, TMUX_PANE, optional
SSH_AUTH_SOCK) Claude uses inside the agent pane. Bound to `Ctrl-Space T`
via `display-popup -E` so the popup closes when the shell exits.
"""

from __future__ import annotations

import argparse
import logging
import os

from tmux_agents import config, container, logging_setup, paths, tmux
from tmux_agents import windows as windows_mod
from tmux_agents.ssh_forward import UDS_PATH as _SSH_UDS_PATH

logger = logging.getLogger(__name__)


def _fail(msg: str) -> int:
    logging_setup.cli_error(logger, msg)
    tmux.display_message(f"agent-terminal: {msg}")
    return 1


def _exec_host(mapping: windows_mod.WindowMapping) -> int:
    os.chdir(mapping.host_worktree)
    shell = os.environ.get("SHELL", "/bin/bash")
    # -il = interactive + login. Plain `-l` can exit without a prompt under
    # setups (e.g. zsh4humans) that key init off explicit interactivity.
    os.execvp(shell, [shell, "-il"])
    return 0  # unreachable in production


def _exec_container(proj: config.Project, mapping: windows_mod.WindowMapping) -> int:
    name = container.current_name(proj)
    if not name:
        return _fail(f"no running container for {mapping.project!r}")
    workdir = proj.workdir_for(mapping.branch)
    argv = ["docker", "exec", "-it", "-e", "TERM", "-e", "COLORTERM", "-e", "TMUX_PANE"]
    if proj.forward_ssh_agent:
        argv += ["-e", f"SSH_AUTH_SOCK={_SSH_UDS_PATH}"]
    argv += ["-u", proj.user or "vscode", "-w", workdir, name, "bash", "-il"]
    os.execvp("docker", argv)
    return 0  # unreachable in production


def main(argv: list[str] | None = None) -> int:
    logging_setup.setup_logging()
    parser = argparse.ArgumentParser(prog="agent-terminal")
    parser.add_argument("--window-id", required=True)
    args = parser.parse_args(argv)

    mapping = windows_mod.read_mapping(args.window_id)
    if mapping is None:
        return _fail(f"no window mapping for {args.window_id}")
    proj = config.safe_load(paths.projects_toml()).get(mapping.project)
    if proj is None:
        return _fail(f"project {mapping.project!r} not in projects.toml")

    if proj.is_container:
        return _exec_container(proj, mapping)
    return _exec_host(mapping)
