"""SSH agent forwarding: probes, pump spawn, pump lifecycle.

Public API used by `agent-new` and `agent-restore`:
    has_python3_in_container(container) -> bool
    host_ssh_auth_sock() -> str | None
    spawn_pump(container) -> subprocess.Popen
    maybe_spawn_pump(container, user) -> PumpResult  (idempotent wrapper)

The pump runs detached as `python -m tmux_agents._ssh_pump_script <container>
<user>`; it delivers + supervises the in-container relay. Nothing is inlined.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Literal

from tmux_agents import logging_setup

logger = logging.getLogger(__name__)

UDS_PATH = "/tmp/tmux-agents-ssh.sock"
_PUMP_MODULE = "tmux_agents._ssh_pump_script"


def has_python3_in_container(container: str, user: str = "vscode") -> bool:
    r = subprocess.run(
        ["docker", "exec", "-u", user, container, "python3", "--version"],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def host_ssh_auth_sock() -> str | None:
    return os.environ.get("SSH_AUTH_SOCK")


def is_pump_responsive(
    container: str, user: str = "vscode", *, timeout: float = 1.5
) -> bool:
    """Probe the in-container UDS by running `ssh-add -l` with a timeout.
    Any return code is fine — what we're guarding against is the pump
    accepting connections but not actually round-tripping bytes (the
    observed broken-but-listening failure mode), which presents as a
    hang. Timeout → unhealthy → caller should respawn."""
    try:
        subprocess.run(
            [
                "docker",
                "exec",
                "-u",
                user,
                "-e",
                f"SSH_AUTH_SOCK={UDS_PATH}",
                container,
                "ssh-add",
                "-l",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return True
    except subprocess.TimeoutExpired:
        return False
    except FileNotFoundError:
        return False


def wait_until_pump_ready(
    container: str,
    user: str = "vscode",
    *,
    deadline_secs: float = 5.0,
    poll_interval: float = 0.15,
) -> bool:
    """Block until the in-container ssh agent socket actually answers.

    Stronger check than `is_pump_responsive`: `ssh-add -l` rc 0 (has
    identities) or 1 (agent reachable, no identities) means the relay
    has bound the UDS and the pump is shuttling bytes. rc 2 (or no
    binary / timeout) means not ready yet — keep polling. Returns False
    on deadline so the caller can warn and proceed (downstream ops will
    just fall back to no-agent SSH auth)."""
    end = time.monotonic() + deadline_secs
    while True:
        try:
            r = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-u",
                    user,
                    "-e",
                    f"SSH_AUTH_SOCK={UDS_PATH}",
                    container,
                    "ssh-add",
                    "-l",
                ],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            if r.returncode in (0, 1):
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        if time.monotonic() >= end:
            return False
        time.sleep(poll_interval)


def pump_pids_for(container: str) -> list[int]:
    """Pump pids whose argv ends with `<container> <user>`. We post-filter via
    `ps -o args=` per pid because pgrep matches the pump module path, not the
    container."""
    r = subprocess.run(
        ["pgrep", "-f", _PUMP_MODULE],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return []
    pids: list[int] = []
    for line in r.stdout.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        ps = subprocess.run(
            ["ps", "-o", "args=", "-p", str(pid)],
            capture_output=True,
            text=True,
        )
        # Pump argv: <python> -m tmux_agents._ssh_pump_script <container> <user>.
        # rsplit picks off the last 2 tokens regardless of interpreter path.
        toks = ps.stdout.strip().rsplit(None, 2)
        if len(toks) >= 2 and toks[-2] == container:
            pids.append(pid)
    return pids


def kill_stale_pumps(container: str) -> int:
    """SIGTERM all pumps for `container`, then SIGKILL any that didn't exit
    within ~0.5s. Returns the count terminated on the first pass."""
    pids = pump_pids_for(container)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if pids:
        time.sleep(0.5)
    for pid in pump_pids_for(container):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return len(pids)


def spawn_pump(container: str, user: str = "vscode") -> subprocess.Popen:
    """Detached host pump for `container`, run as a module so the in-container
    relay it spawns runs as `user` (the UDS bind owner must match the agent's
    user — the agent runs `-u {user}` per the exec_cmd template; mode 0600
    requires they match).

    Uses `sys.executable` (the installed tool's interpreter) so
    `-m tmux_agents._ssh_pump_script` resolves the package. Returns the Popen
    handle (caller may discard; the pump reparents to launchd via
    start_new_session).
    """
    env = dict(os.environ)
    env["TMUX_AGENTS_LOG_FILE"] = str(logging_setup.log_file_path())
    return subprocess.Popen(
        [sys.executable, "-m", _PUMP_MODULE, container, user],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )


@dataclass(frozen=True)
class PumpResult:
    outcome: Literal[
        "disabled_no_sock",
        "disabled_no_python",
        "already_healthy",
        "ready",
        "timed_out",
    ]
    killed_stale: int = 0

    def render(self, stage) -> None:
        """Map this result onto a progress.Stage's methods. `stage` is duck-typed
        so we don't need a runtime import from `progress`."""
        if self.outcome == "disabled_no_sock":
            stage.warn("SSH_AUTH_SOCK not set on host (forwarding disabled)")
        elif self.outcome == "disabled_no_python":
            stage.warn("python3 missing in container (forwarding disabled)")
        elif self.outcome == "already_healthy":
            stage.skip("already healthy")
        elif self.outcome == "ready":
            if self.killed_stale > 0:
                stage.info(f"killed {self.killed_stale} stale pump(s); respawned")
            else:
                stage.info("starting…")
        elif self.outcome == "timed_out":
            stage.warn("not ready within budget (forwarding may be flaky)")


def maybe_spawn_pump(container_name: str, user: str = "vscode") -> PumpResult:
    """Idempotent pump spawn for a container. Returns a PumpResult describing
    the outcome — callers map this onto progress.Stage methods. Called by
    `agent-new` and `agent-restore` after the container is up. Probes host
    SSH_AUTH_SOCK and in-container python3; replaces broken-but-listening
    pumps; blocks on `wait_until_pump_ready` so downstream docker-exec'd git
    ops see a bound UDS. Prereq failures return a disabled outcome — downstream
    SSH falls back to no-agent auth. logger.warning calls remain for the
    unified log."""
    if not container_name:
        return PumpResult("disabled_no_sock")  # treat empty container name as disabled
    if host_ssh_auth_sock() is None:
        logger.warning("SSH_AUTH_SOCK not set on host; SSH agent forwarding disabled")
        return PumpResult("disabled_no_sock")
    if not has_python3_in_container(container_name, user):
        logger.warning(
            "python3 not found in %s; SSH agent forwarding disabled", container_name
        )
        return PumpResult("disabled_no_python")
    existing = pump_pids_for(container_name)
    if existing and is_pump_responsive(container_name, user):
        return PumpResult("already_healthy")
    killed = 0
    if existing:
        killed = kill_stale_pumps(container_name)
        if killed > 0:
            logger.info(
                "killed %d stale ssh pump(s) for %s; respawning", killed, container_name
            )
    spawn_pump(container_name, user)
    if not wait_until_pump_ready(container_name, user):
        logger.warning(
            "ssh pump for %s not ready within budget; continuing", container_name
        )
        return PumpResult("timed_out", killed_stale=killed)
    return PumpResult("ready", killed_stale=killed)
