"""`agent-vscode` entry point.

Container / devcontainer projects use VS Code's `attached-container`
URI scheme so we reattach to whichever container is already running
(resolved via `container.current_name`) instead of going through the
full `devcontainer up` path — symmetric across `container = "name"`
and `devcontainer = true` projects, no rebuild, no second container.
"""

from __future__ import annotations
import argparse
import logging
import os
import shutil
import subprocess

from tmux_agents import config, container, logging_setup, paths, tmux
from tmux_agents import windows as windows_mod

logger = logging.getLogger(__name__)


def _resolve_code_cli() -> tuple[str | None, str]:
    """Return `(resolved, candidate)`. `resolved` is the `code` binary to
    exec (PATH lookup first, then the configured fallback) or None if
    neither works. `candidate` is always the configured fallback path so
    the caller can name it in error messages without re-reading the file."""
    candidate = config.read_code_path(paths.projects_toml())
    found = shutil.which("code")
    if found:
        return found, candidate
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate, candidate
    return None, candidate


def _attached_container_uri(container_name: str, workdir: str) -> str:
    hex_name = container_name.encode("utf-8").hex()
    return f"vscode-remote://attached-container+{hex_name}{workdir}"


def _fail(msg: str) -> int:
    logging_setup.cli_error(logger, msg)
    tmux.display_message(f"agent-vscode: {msg}")
    return 1


def main(argv: list[str] | None = None) -> int:
    logging_setup.setup_logging()
    parser = argparse.ArgumentParser(prog="agent-vscode")
    parser.add_argument("--window-id", required=True)
    args = parser.parse_args(argv)

    code, candidate = _resolve_code_cli()
    if code is None:
        return _fail(
            f"`code` CLI not found on PATH and no executable at {candidate} "
            "(set `code_path` in projects.toml)"
        )

    mapping = windows_mod.read_mapping(args.window_id)
    if mapping is None:
        return _fail(f"no window mapping for {args.window_id}")
    proj = config.safe_load(paths.projects_toml()).get(mapping.project)
    if proj is None:
        return _fail(f"project {mapping.project!r} not in projects.toml")

    if proj.is_container:
        name = container.current_name(proj)
        if not name:
            return _fail(f"no running container for {mapping.project!r}")
        uri = _attached_container_uri(name, proj.workdir_for(mapping.branch))
        cmd = [code, "--folder-uri", uri]
    else:
        cmd = [code, str(mapping.host_worktree)]

    logger.info("launching: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        return _fail(f"`code` exited {e.returncode}")
    return 0
