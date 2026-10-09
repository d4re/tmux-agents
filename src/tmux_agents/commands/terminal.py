"""`agent-terminal` entry point.

Opens a persistent shell rooted at the active agent's worktree — host
projects run `$SHELL -il` in the worktree; container/devcontainer projects
`docker exec -it -u <user> -w <workdir> <container> bash -il` with the same
env forwarding (TERM, COLORTERM, TMUX_PANE, optional SSH_AUTH_SOCK) Claude
uses inside the agent pane. Bound to `prefix + t` via `display-popup -E`.

The shell runs in a hidden per-window tmux session (`tmux.create_term_session`)
and this command execs a nested `tmux attach` to it, so inside the popup tmux's
mouse copy and copy-mode scrollback work (a popup is not a pane), and hiding
the popup (`prefix t` again, or `prefix d`) only detaches — reopening returns
to the same shell. Exiting the shell destroys the session and closes the popup.
"""

from __future__ import annotations

import argparse
import logging
import os
import shlex

from tmux_agents import config, container, logging_setup, paths, tmux
from tmux_agents import windows as windows_mod
from tmux_agents.ssh_forward import UDS_PATH as _SSH_UDS_PATH

logger = logging.getLogger(__name__)


def _fail(msg: str) -> int:
    logging_setup.cli_error(logger, msg)
    tmux.display_message(f"agent-terminal: {msg}")
    return 1


def _host_shell() -> list[str]:
    shell = os.environ.get("SHELL", "/bin/bash")
    # -il = interactive + login. Plain `-l` can exit without a prompt under
    # setups (e.g. zsh4humans) that key init off explicit interactivity.
    return [shell, "-il"]


def _container_shell(
    proj: config.Project, mapping: windows_mod.WindowMapping
) -> list[str] | None:
    name = container.current_name(proj)
    if not name:
        return None
    workdir = proj.workdir_for(mapping.branch)
    argv = ["docker", "exec", "-it", "-e", "TERM", "-e", "COLORTERM", "-e", "TMUX_PANE"]
    if proj.forward_ssh_agent:
        argv += ["-e", f"SSH_AUTH_SOCK={_SSH_UDS_PATH}"]
    argv += ["-u", proj.user or "vscode", "-w", workdir, name, "bash", "-il"]
    return argv


def _sandbox_shell(
    proj: config.Project, mapping: windows_mod.WindowMapping
) -> list[str]:
    """Shell INSIDE the sandbox — a host shell for a sandbox project would
    be a silent isolation hole. `sbx exec` auto-starts a stopped sandbox;
    worktree paths are host-identical (passthrough), so cd works as-is."""
    workdir = proj.workdir_for(mapping.branch)
    return [
        "sbx",
        "exec",
        "-it",
        "-e",
        "TERM",
        "-e",
        "COLORTERM",
        "-e",
        "TMUX_PANE",
        proj.sandbox_name,
        "bash",
        "-lc",
        f"cd {shlex.quote(workdir)} && exec bash -il",
    ]


def _create_session(window_id: str) -> int:
    mapping = windows_mod.read_mapping(window_id)
    if mapping is None:
        return _fail(f"no window mapping for {window_id}")
    proj = config.safe_load(paths.projects_toml()).get(mapping.project)
    if proj is None:
        return _fail(f"project {mapping.project!r} not in projects.toml")

    cwd = None
    if proj.backend == config.BACKEND_SANDBOX:
        argv = _sandbox_shell(proj, mapping)
    elif proj.is_container:
        argv = _container_shell(proj, mapping)
        if argv is None:
            return _fail(f"no running container for {mapping.project!r}")
    else:
        argv, cwd = _host_shell(), str(mapping.host_worktree)
    tmux.create_term_session(window_id, argv=argv, cwd=cwd)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging_setup.setup_logging()
    parser = argparse.ArgumentParser(prog="agent-terminal")
    parser.add_argument("--window-id", required=True)
    args = parser.parse_args(argv)

    # An existing session is reattached as-is: that's the persistence, and it
    # skips the (docker) resolution so reopening the popup is instant.
    if not tmux.term_session_exists(args.window_id):
        rc = _create_session(args.window_id)
        if rc != 0:
            return rc
    attach = tmux.term_attach_argv(args.window_id)
    os.execvp(attach[0], attach)
    return 0  # unreachable in production
