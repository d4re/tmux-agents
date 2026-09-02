"""Docker Sandboxes (sbx) probes and primitives: `exec_capture`, atomic
`deliver`, daemon/lifecycle management, and state export/import. Sole module
that shells out to `sbx` (the exec templates in config.py and
agent-terminal's execvp aside). Unlike `container.ensure_up`, creation is
race-free: a per-name lock around inspect -> create -> re-inspect.

Every call has a bounded timeout and runs with stdin closed unless stdin
data is passed — `agent-new` runs in a background worker where an
interactive sbx prompt is an invisible hang. Three user-fixable failures
get first-class remediation text (install, login, daemon); everything else
passes sbx's own stderr through."""

from __future__ import annotations

import base64
import json
import logging
import shlex
import subprocess
import time
from pathlib import PurePosixPath

from tmux_agents import locks, paths
from tmux_agents.config import Project

logger = logging.getLogger(__name__)

INSTALL_HINT = (
    "sbx is not installed — unpack DockerSandboxes-darwin.tar.gz from "
    "github.com/docker/sbx-releases under ~/.local/share/docker-sandboxes/<ver>/ "
    "and symlink bin/sbx into ~/.local/bin (keep libexec/ next to the binary)"
)
LOGIN_HINT = "sbx session expired or missing — run: sbx login"
DAEMON_HINT = "sbx daemon is not running — run: sbx daemon start -d --policy balanced"

PROBE_TIMEOUT = 15
EXEC_TIMEOUT = 60
DAEMON_START_TIMEOUT = 60
CREATE_TIMEOUT = 600


class SandboxError(RuntimeError):
    """sbx failure; str() carries the remediation when the cause is known."""


def _classify(stderr: str) -> str | None:
    """Map stderr to a remediation hint for the user-fixable failures.
    Login expiry can surface on ANY call (sessions lapse ~2-weekly), so
    this runs for every nonzero exit, not just create. Substrings are
    deliberately broad — an unmatched failure still surfaces stderr
    verbatim, so a missed classification degrades gracefully."""
    s = stderr.lower()
    if (
        "not logged in" in s
        or "unauthorized" in s
        or "sbx login" in s
        or "sign in" in s
    ):
        return LOGIN_HINT
    if "daemon" in s and ("not running" in s or "connect" in s or "refused" in s):
        return DAEMON_HINT
    return None


def _run(
    argv: list[str], *, timeout: int, stdin_data: str | None = None
) -> subprocess.CompletedProcess:
    kwargs: dict = {"capture_output": True, "text": True, "timeout": timeout}
    if stdin_data is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin_data
    try:
        return subprocess.run(["sbx", *argv], **kwargs)
    except FileNotFoundError as ex:
        raise SandboxError(INSTALL_HINT) from ex
    except subprocess.TimeoutExpired as ex:
        raise SandboxError(f"sbx {argv[0]} timed out after {timeout}s") from ex


def _check(r: subprocess.CompletedProcess, context: str) -> subprocess.CompletedProcess:
    if r.returncode == 0:
        return r
    hint = _classify(r.stderr or "")
    if hint:
        raise SandboxError(hint)
    raise SandboxError(
        f"sbx {context} failed (exit {r.returncode}): {(r.stderr or '').strip()}"
    )


def exec_capture(
    name: str, script: str, *, stdin: str | None = None, timeout: int = EXEC_TIMEOUT
) -> str:
    """Run `sh -c <script>` inside sandbox `name`, returning stdout.
    Auto-starts a stopped (not deleted) sandbox; the daemon must run."""
    argv = ["exec"]
    if stdin is not None:
        argv.append("-i")
    argv += [name, "sh", "-c", script]
    r = _run(argv, timeout=timeout, stdin_data=stdin)
    return _check(r, f"exec in {name!r}").stdout


def deliver(
    name: str, path: PurePosixPath | str, content: str, *, mode: str | None = None
) -> None:
    """Write `content` to `path` inside the sandbox via unique mktemp +
    atomic rename — same guarantees as codex_hooks' container delivery; a
    bare `cat > path` is not acceptable for hook provisioning."""
    p = PurePosixPath(path)
    directory = shlex.quote(str(p.parent))
    chmod = f' && chmod {mode} "$t"' if mode else ""
    script = (
        f"mkdir -p {directory} && "
        f"t=$(mktemp {directory}/.tmux-agents.XXXXXX) && "
        f'cat > "$t"{chmod} && mv "$t" {shlex.quote(str(p))}'
    )
    exec_capture(name, script, stdin=content)


def network_allowed(name: str, host: str) -> bool | None:
    """Would sandbox `name`'s egress policy let it reach `host` (port 443)?
    Read-only `sbx policy check network --json`. True/False from the JSON
    `allowed` field; None when the probe can't decide (sbx missing, timeout,
    unknown sandbox — which exits 0 with an `ERROR:` line and no JSON), so
    callers treat None as "don't veto". Note the deny case exits 1 *and*
    prints JSON, so the return code is not the signal."""
    try:
        r = _run(
            ["policy", "check", "network", "--json", "--sandbox", name, host],
            timeout=PROBE_TIMEOUT,
        )
    except SandboxError as ex:
        logger.debug("policy check %s for %r skipped: %s", host, name, ex)
        return None
    try:
        data = json.loads(r.stdout or "")
    except ValueError:
        logger.debug(
            "policy check %s for %r: no JSON (rc %s): %s",
            host,
            name,
            r.returncode,
            (r.stderr or "").strip(),
        )
        return None
    allowed = data.get("allowed") if isinstance(data, dict) else None
    return allowed if isinstance(allowed, bool) else None


def daemon_running() -> bool:
    """`sbx daemon status` semantics when stopped are unverified upstream —
    require BOTH rc 0 and a 'Status: running' line so either failure shape
    (nonzero rc, or rc 0 + 'Status: stopped') reads as down."""
    r = _run(["daemon", "status"], timeout=PROBE_TIMEOUT)
    return r.returncode == 0 and "status: running" in (r.stdout or "").lower()


def ensure_daemon() -> None:
    """Global daemon-start lock, then status -> detached start -> bounded
    readiness poll. The daemon does not auto-start at boot; restore
    parallelizes project groups, so this must be idempotent + serialized."""
    with locks.locked(paths.sbx_daemon_lock()):
        if daemon_running():
            return
        _check(
            _run(["daemon", "start", "-d"], timeout=DAEMON_START_TIMEOUT),
            "daemon start",
        )
        deadline = time.monotonic() + DAEMON_START_TIMEOUT
        while time.monotonic() < deadline:
            if daemon_running():
                return
            time.sleep(1)
    raise SandboxError(DAEMON_HINT + " (start ran but the daemon never became ready)")


def is_present(name: str) -> bool:
    """A stopped sandbox counts as present — `sbx exec` auto-starts it.
    Only a DELETED sandbox is absent (restore recreates those). There is
    no `sbx inspect` (v0.38.0); presence = name in `sbx ls -q` lines."""
    r = _check(_run(["ls", "-q"], timeout=PROBE_TIMEOUT), "ls")
    return name in (r.stdout or "").splitlines()


def _create(proj: Project) -> None:
    """Run `sbx create` for the project. Caller MUST hold the project's
    `sbx_create_lock`."""
    name = proj.sandbox_name
    argv = ["create", "--name", name]
    if proj.sbx_template:
        argv += ["-t", proj.sbx_template]
    for kit in proj.sbx_kits:
        argv += ["--kit", kit]
    if proj.sbx_memory:
        argv += ["-m", proj.sbx_memory]
    # Agent positional is always `claude` — the dual-agent architecture
    # runs codex INSIDE the claude sandbox (see docs/SANDBOX-MODE.md).
    argv += ["claude", str(proj.repo), *proj.sbx_mounts]
    logger.info("creating sandbox %r: sbx %s", name, " ".join(argv))
    _check(_run(argv, timeout=CREATE_TIMEOUT), f"create {name!r}")
    if not is_present(name):
        raise SandboxError(f"sbx create ran but sandbox {name!r} is not present")


def ensure_up(proj: Project) -> bool:
    """Create the project's sandbox if it doesn't exist. Returns True iff
    it was created — a fresh VM means logins and session files are gone,
    which callers must react to (clear stale --resume ids; codex needs
    re-login). Per-name lock around inspect -> create -> re-inspect, so the
    check-then-create race container.ensure_up has is not copied here."""
    with locks.locked(paths.sbx_create_lock(proj.sandbox_name)):
        if is_present(proj.sandbox_name):
            return False
        _create(proj)
        return True


def recreate(proj: Project) -> None:
    """rm + create as ONE critical section under the per-name lock —
    rebuild's recreate must not let a concurrent `ensure_up` (agent-new,
    restore) slip a create between the removal and the new create, or the
    subsequent state import would land in the other caller's sandbox."""
    name = proj.sandbox_name
    with locks.locked(paths.sbx_create_lock(name)):
        if is_present(name):
            _rm(name)
        _create(proj)


def _rm(name: str) -> None:
    # --force skips the confirmation prompt (a hang in a worker) and
    # removes even an in-use sandbox (e.g. an open SSH connection).
    # CREATE_TIMEOUT: deleting a microVM with a large disk can be slow.
    _check(_run(["rm", "--force", name], timeout=CREATE_TIMEOUT), f"rm {name!r}")


def remove(name: str) -> None:
    _rm(name)


# What rebuild carries across a recreate. Principle: only what cannot be
# recreated — sessions, history, memory, the codex login. ~/.claude/skills
# is the SHARED sbx skills store mount and must never be tarred;
# ~/.claude/settings.json and codex config.toml are kit-regenerated (an old
# config.toml would clobber a new template/kit's config, so it stays out).
# codex auth.json IS carried: a one-time transfer whose source is destroyed
# with the sandbox keeps the token lineage single (codex rotates refresh
# tokens — two live copies invalidate each other, but this isn't a copy).
_EXPORT_CANDIDATES = ".claude .codex/auth.json .codex/sessions .codex/history.jsonl"
# Staged through a temp file, NOT `tar | base64` piped: sh has no pipefail,
# so a tar that dies partway (agent still writing ~/.claude, unreadable
# file) would exit 0 through base64 with a silently TRUNCATED archive — and
# rebuild would then delete the sandbox believing the state was saved. The
# `&&` chain makes tar's own exit status gate the base64. Any nonzero tar
# rc (including GNU tar's 1 = "file changed as we read it") aborts.
_EXPORT_SCRIPT = (
    'cd "$HOME" && t=$(mktemp) && { '
    f"for p in {_EXPORT_CANDIDATES}; do "
    '[ -e "$p" ] && printf "%s\\n" "$p"; done; } | '
    "tar -cf \"$t\" --exclude='.claude/skills' --exclude='.claude/settings.json' "
    '-T - && base64 < "$t" && rm -f "$t"'
)


def export_state(name: str) -> bytes:
    """Tar of the irreplaceable agent state inside sandbox `name`, base64'd
    through the text exec channel. Raises SandboxError on any failure —
    callers must NOT delete a sandbox whose state couldn't be saved.
    (If session dirs ever grow to hundreds of MB, `sbx cp` — copy-out
    escape fixed in 0.38 — is the escape hatch from in-memory base64.)"""
    out = exec_capture(name, _EXPORT_SCRIPT, timeout=CREATE_TIMEOUT)
    return base64.b64decode(out)


def import_state(name: str, blob: bytes) -> None:
    exec_capture(
        name,
        'cd "$HOME" && base64 -d | tar -xf -',
        stdin=base64.b64encode(blob).decode(),
        timeout=CREATE_TIMEOUT,
    )


# Shown in a held pane when a codex slot lands in a FRESH sandbox (created,
# recreated by restore, or rebuilt without state) — launching codex into an
# auth error loop helps nobody. Shared by agent-rebuild and agent-restore.
CODEX_LOGIN_RUNBOOK = """
  codex login required — this sandbox is fresh (no codex login yet).

  codex's login server binds to the VM's loopback, which sbx port
  publishing can't reach — bridge it with an SSH tunnel instead
  (prereq once per host: sbx setup ssh):

    host terminal (keep running):  ssh -N -L 1455:127.0.0.1:1455 {name}.sbx
    sandbox shell (Ctrl-Space t):  codex login
                                   # open the printed URL in the host browser
    after the browser flow:        ctrl-c the tunnel — done.

  Then re-run agent-restore (Ctrl-Space r) or restart this pane.
"""
