"""Docker probes: `is_running`, `current_name` (by `container=` name OR
`devcontainer.local_folder` label), and `ensure_up`. Sole module that
shells out to `docker` (the SSH pump aside)."""

import logging
import subprocess

from tmux_agents.config import Project

logger = logging.getLogger(__name__)


class ContainerError(RuntimeError):
    pass


def is_running(name: str) -> bool:
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return False
    return r.stdout.strip() == "true"


def current_name(proj: Project) -> str | None:
    if proj.container:
        return proj.container if is_running(proj.container) else None
    if proj.devcontainer:
        return _resolve_by_label(f"devcontainer.local_folder={proj.repo}")
    return None


def _resolve_by_label(label: str) -> str | None:
    r = subprocess.run(
        ["docker", "ps", "--filter", f"label={label}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return None
    names = [n for n in r.stdout.splitlines() if n]
    return names[0] if names else None


def ensure_up(proj: Project, *, up_cmd: str | None) -> str:
    name = current_name(proj)
    if name:
        return name
    if not up_cmd:
        raise ContainerError(
            f"no container for {proj.name!r} is running and no up_cmd configured"
        )
    logger.info("starting container for %r; cmd=%s", proj.name, up_cmd)
    r = subprocess.run(up_cmd, shell=True)
    if r.returncode != 0:
        raise ContainerError(f"up_cmd failed for {proj.name!r} (exit {r.returncode})")
    name = current_name(proj)
    if not name:
        raise ContainerError(f"up_cmd ran but no container for {proj.name!r} is up")
    return name
