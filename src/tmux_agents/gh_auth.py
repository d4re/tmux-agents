"""GitHub CLI auth sharing: one-shot host→container/sandbox token sync.

Public API used by `agent-new`, `agent-restore`, and `agent-rebuild`:
    maybe_sync_gh_auth(container, user) -> SyncResult       # docker exec
    maybe_sync_gh_auth_sandbox(sandbox_name) -> SyncResult  # sbx exec

The host token comes from `gh auth token` (keyring-backed on macOS — the
container can't read it, and a rebuilt container loses its own
`~/.config/gh/hosts.yml` login). The sync pipes the token via stdin to
`gh auth login --with-token` inside the container: never on argv, never in
tmux command strings, never on disk host-side. Always overwrites — a probe
can't detect a revoked token, and re-login is cheap.

The sandbox variant is the same ladder over `sandbox.exec_capture` (the
sole sbx caller); the stdin guarantee holds there too. No user parameter:
sbx exec always runs as the sandbox's default user. One extra rung at the
top: the sbx runtime injects its own proxy-managed GH_TOKEN into every
sandbox, and `gh auth login` hard-refuses to run while GH_TOKEN is set —
so when the probe sees it, the sync short-circuits to
"already_authenticated" instead of warning about a doomed login.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal

from tmux_agents import sandbox

logger = logging.getLogger(__name__)

_HOSTNAME = "github.com"
# `gh auth login --with-token` validates against the GitHub API, so it gets a
# longer budget than the local probes.
_PROBE_TIMEOUT = 5.0
_LOGIN_TIMEOUT = 15.0
# sbx exec crosses a VM boundary (and auto-starts a stopped sandbox), so the
# sandbox variants get roomier budgets than docker exec.
_SBX_PROBE_TIMEOUT = 15
_SBX_LOGIN_TIMEOUT = 30


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
        "already_authenticated",
        "synced",
        "failed",
    ]
    # Rendered in the missing-gh message only; host-side messages are
    # target-agnostic. "container" default keeps the docker path unchanged.
    where: str = "container"

    def render(self, stage) -> None:
        """Map this result onto a progress.Stage's methods. `stage` is duck-typed
        so we don't need a runtime import from `progress`."""
        if self.outcome == "disabled_no_host_gh":
            stage.warn("gh not installed on host (auth sharing disabled)")
        elif self.outcome == "disabled_not_logged_in":
            stage.warn("gh not logged in on host (auth sharing disabled)")
        elif self.outcome == "disabled_no_container_gh":
            stage.warn(f"gh missing in {self.where} (auth sharing disabled)")
        elif self.outcome == "already_authenticated":
            stage.info("gh already authenticated (runtime-injected token)")
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


def has_gh_in_sandbox(name: str) -> bool:
    try:
        sandbox.exec_capture(name, "gh --version", timeout=_SBX_PROBE_TIMEOUT)
    except sandbox.SandboxError:
        return False
    return True


def sandbox_has_injected_gh_token(name: str) -> bool:
    """True when the sbx runtime injected a GH_TOKEN into the sandbox env.

    The runtime provisions a proxy-managed GH_TOKEN (PID1 scope, so every
    exec sees it): gh is already authenticated, and `gh auth login` refuses
    to run at all while GH_TOKEN is set — so a sync attempt can only fail.
    A probe error means "unknown"; callers fall through to the normal sync
    ladder, which degrades exactly as before."""
    try:
        sandbox.exec_capture(name, 'test -n "$GH_TOKEN"', timeout=_SBX_PROBE_TIMEOUT)
    except sandbox.SandboxError:
        return False
    return True


def _login_sandbox(name: str, token: str) -> bool:
    """Run `gh auth login --with-token` in the sandbox, token on stdin."""
    try:
        sandbox.exec_capture(
            name,
            f"gh auth login --with-token --hostname {_HOSTNAME}",
            stdin=token,
            timeout=_SBX_LOGIN_TIMEOUT,
        )
    except sandbox.SandboxError as ex:
        logger.warning("gh auth sync for sandbox %s: %s", name, ex)
        return False
    return True


def maybe_sync_gh_auth_sandbox(sandbox_name: str) -> SyncResult:
    """Idempotent gh-token sync for a sandbox — `maybe_sync_gh_auth` with
    sbx exec as the transport, plus a runtime-token short-circuit (see the
    module docstring). Every failure path is non-fatal: the agent still
    spawns and gh asks for a manual login exactly as before."""
    if not sandbox_name:
        return SyncResult("disabled_no_container_gh", where="sandbox")
    if sandbox_has_injected_gh_token(sandbox_name):
        logger.info(
            "sandbox %s has a runtime-injected GH_TOKEN; skipping sync", sandbox_name
        )
        return SyncResult("already_authenticated", where="sandbox")
    if not host_gh_installed():
        logger.warning("gh not installed on host; gh auth sharing disabled")
        return SyncResult("disabled_no_host_gh", where="sandbox")
    token = host_gh_token()
    if token is None:
        logger.warning("gh not logged in on host; gh auth sharing disabled")
        return SyncResult("disabled_not_logged_in", where="sandbox")
    if not has_gh_in_sandbox(sandbox_name):
        logger.warning("gh not found in %s; gh auth sharing disabled", sandbox_name)
        return SyncResult("disabled_no_container_gh", where="sandbox")
    if not _login_sandbox(sandbox_name, token):
        return SyncResult("failed", where="sandbox")
    logger.info("gh auth token synced into sandbox %s", sandbox_name)
    return SyncResult("synced", where="sandbox")
