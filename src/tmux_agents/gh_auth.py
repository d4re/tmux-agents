"""GitHub CLI auth sharing: one-shot host→container token sync.

Public API used by `agent-new`, `agent-restore`, and `agent-rebuild`:
    maybe_sync_gh_auth(container, user) -> SyncResult

The host token comes from `gh auth token` (keyring-backed on macOS — the
container can't read it, and a rebuilt container loses its own
`~/.config/gh/hosts.yml` login). The sync pipes the token via stdin to
`gh auth login --with-token` inside the container: never on argv, never in
tmux command strings, never on disk host-side. Always overwrites — a probe
can't detect a revoked token, and re-login is cheap.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

_HOSTNAME = "github.com"
# `gh auth login --with-token` validates against the GitHub API, so it gets a
# longer budget than the local probes.
_PROBE_TIMEOUT = 5.0
_LOGIN_TIMEOUT = 15.0


def host_gh_installed() -> bool:
    return shutil.which("gh") is not None


def host_gh_token() -> str | None:
    """The host's gh OAuth token (reads the keyring), or None when gh is
    missing, not logged in, or slow to answer."""
    try:
        r = subprocess.run(
            ["gh", "auth", "token", "--hostname", _HOSTNAME],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    token = r.stdout.strip()
    return token or None


def has_gh_in_container(container: str, user: str = "vscode") -> bool:
    try:
        r = subprocess.run(
            ["docker", "exec", "-u", user, container, "gh", "--version"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _login(container: str, user: str, token: str) -> bool:
    """Run `gh auth login --with-token` in the container, token on stdin."""
    try:
        r = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "-u",
                user,
                container,
                "gh",
                "auth",
                "login",
                "--with-token",
                "--hostname",
                _HOSTNAME,
            ],
            input=token,
            capture_output=True,
            text=True,
            timeout=_LOGIN_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as ex:
        logger.warning("gh auth sync for %s: %s", container, ex)
        return False
    if r.returncode != 0:
        logger.warning(
            "gh auth sync for %s failed (rc=%d): %s",
            container,
            r.returncode,
            r.stderr.strip(),
        )
        return False
    return True


@dataclass(frozen=True)
class SyncResult:
    outcome: Literal[
        "disabled_no_host_gh",
        "disabled_not_logged_in",
        "disabled_no_container_gh",
        "synced",
        "failed",
    ]

    def render(self, stage) -> None:
        """Map this result onto a progress.Stage's methods. `stage` is duck-typed
        so we don't need a runtime import from `progress`."""
        if self.outcome == "disabled_no_host_gh":
            stage.warn("gh not installed on host (auth sharing disabled)")
        elif self.outcome == "disabled_not_logged_in":
            stage.warn("gh not logged in on host (auth sharing disabled)")
        elif self.outcome == "disabled_no_container_gh":
            stage.warn("gh missing in container (auth sharing disabled)")
        elif self.outcome == "synced":
            stage.info("token synced")
        elif self.outcome == "failed":
            stage.warn("token sync failed (see log)")


def maybe_sync_gh_auth(container_name: str, user: str = "vscode") -> SyncResult:
    """Idempotent gh-token sync for a container. Returns a SyncResult that
    callers map onto progress.Stage methods. Every failure path is non-fatal:
    the agent still spawns and gh asks for a manual login exactly as before."""
    if not container_name:
        return SyncResult("disabled_no_container_gh")
    if not host_gh_installed():
        logger.warning("gh not installed on host; gh auth sharing disabled")
        return SyncResult("disabled_no_host_gh")
    token = host_gh_token()
    if token is None:
        logger.warning("gh not logged in on host; gh auth sharing disabled")
        return SyncResult("disabled_not_logged_in")
    if not has_gh_in_container(container_name, user):
        logger.warning("gh not found in %s; gh auth sharing disabled", container_name)
        return SyncResult("disabled_no_container_gh")
    if not _login(container_name, user, token):
        return SyncResult("failed")
    logger.info("gh auth token synced into %s", container_name)
    return SyncResult("synced")
