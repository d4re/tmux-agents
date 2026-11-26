"""Docker Sandboxes (sbx) probes and primitives: `exec_capture`, atomic
`deliver`, daemon/lifecycle management, and sandbox composition. Sole module
that shells out to `sbx` (the exec templates in config.py and
agent-terminal's execvp aside). Unlike `container.ensure_up`, creation is
race-free: a per-name lock around inspect -> create -> re-inspect.

Every call has a bounded timeout and runs with stdin closed unless stdin
data is passed — `agent-new` runs in a background worker where an
interactive sbx prompt is an invisible hang. Three user-fixable failures
get first-class remediation text (install, login, daemon); everything else
passes sbx's own stderr through."""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import time
from importlib import resources
from pathlib import Path, PurePosixPath

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
        return subprocess.run(["sbx", *argv], **kwargs, check=False)
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


def status(name: str) -> str | None:
    """Sandbox `name`'s status from `sbx ls --json` ("running", "stopped",
    …), or None when no such sandbox exists. Accepts both shapes: a bare
    list (0.49+) and the older `{"sandboxes": [...]}` wrapper; anything
    else raises SandboxError (the only error callers catch)."""
    r = _check(_run(["ls", "--json"], timeout=PROBE_TIMEOUT), "ls")
    try:
        data = json.loads(r.stdout or "")
    except ValueError as ex:
        raise SandboxError(f"sbx ls --json returned non-JSON: {ex}") from ex
    if isinstance(data, dict):
        data = data.get("sandboxes")
    if not isinstance(data, list):
        raise SandboxError(f"sbx ls --json: unexpected shape {type(data).__name__}")
    for sb in data:
        if isinstance(sb, dict) and sb.get("name") == name:
            return sb.get("status")
    return None


def path_visible(name: str, path: str) -> bool | None:
    """Does sandbox `name` see `path` as a directory? None when the probe
    can't decide, so callers treat None as "don't veto". Exists because
    the workspace virtiofs share caches a negative lookup indefinitely:
    if anything in the guest looked a path up before the host created it,
    the guest keeps getting ENOENT for that name (sbx v0.42.0 — survives
    guest drop_caches and host rename; only a sandbox restart clears it)."""
    q = shlex.quote(str(path))
    try:
        out = exec_capture(
            name,
            f"if [ -d {q} ]; then echo yes; else echo no; fi",
            timeout=PROBE_TIMEOUT,
        )
    except SandboxError as ex:
        logger.debug("visibility probe of %s in %r skipped: %s", path, name, ex)
        return None
    return {"yes": True, "no": False}.get(out.strip())


# Find guest processes launched for the given panes and terminate them.
# Every agent exec template passes `-e TMUX_PANE -e TMUX_AGENTS_AGENT=1`, and
# children inherit both, so the match is exact per pane (the space-padded
# list keeps %4 from matching %40) and never touches the user's own shells.
# TERM first so claude can end its session cleanly; KILL what's left.
_REAP_SCRIPT = """
panes=' @PANES@ '
pids=
for d in @PROC@/[0-9]*; do
  pid=${d##*/}
  [ "$pid" = "$$" ] && continue
  e=$(tr '\\000' '\\n' < "$d/environ" 2>/dev/null) || continue
  printf '%s\\n' "$e" | grep -qx 'TMUX_AGENTS_AGENT=1' || continue
  p=$(printf '%s\\n' "$e" | sed -n 's/^TMUX_PANE=//p')
  [ -n "$p" ] || continue
  case "$panes" in *" $p "*) pids="$pids $pid" ;; esac
done
set -- $pids
echo $#
[ $# -eq 0 ] && exit 0
kill -TERM "$@" 2>/dev/null
i=0
while [ $i -lt @TICKS@ ]; do
  alive=
  for pid in "$@"; do kill -0 "$pid" 2>/dev/null && alive="$alive $pid"; done
  [ -z "$alive" ] && exit 0
  sleep 0.1
  i=$((i + 1))
done
kill -KILL $alive 2>/dev/null
exit 0
"""


def _reap_script(
    pane_ids: list[str], *, proc_root: str = "/proc", grace_s: float = 3
) -> str:
    return (
        _REAP_SCRIPT.replace("@PANES@", " ".join(f"%{p}" for p in pane_ids))
        .replace("@PROC@", shlex.quote(proc_root))
        .replace("@TICKS@", str(max(1, round(grace_s * 10))))
    )


def reap_pane_agents(name: str, pane_ids: list[str]) -> int:
    """Terminate the agent processes inside sandbox `name` that were
    launched for `pane_ids` (stripped ids). Returns how many were found.

    Killing a tmux pane only kills the host-side `sbx exec` client — the
    guest process survives, keeps running in a possibly-deleted worktree,
    and pins it so the guest still lists it after the host removed it.
    A sandbox that isn't running has nothing to reap and is NOT booted
    (`sbx exec` would auto-start it). Raises SandboxError on sbx failure."""
    if not pane_ids or status(name) != "running":
        return 0
    out = exec_capture(name, _reap_script(pane_ids))
    try:
        return int(out.strip() or 0)
    except ValueError:
        return 0


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


# Tolerant of decoration between label and value: 0.48 prints
# "Status:  ● running" (padding + a bullet glyph), older builds "Status: running".
_DAEMON_RUNNING_RE = re.compile(
    r"^\s*status:[^a-z\n]*running\b", re.IGNORECASE | re.MULTILINE
)


def daemon_running() -> bool:
    """`sbx daemon status` semantics when stopped are unverified upstream —
    require BOTH rc 0 and a 'Status: running' line so either failure shape
    (nonzero rc, or rc 0 + 'Status: stopped') reads as down."""
    r = _run(["daemon", "status"], timeout=PROBE_TIMEOUT)
    return r.returncode == 0 and bool(_DAEMON_RUNNING_RE.search(r.stdout or ""))


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


# Both agents come from Docker's published mixins, pinned by tag so a
# rebuild never silently changes agent versions. Bump deliberately (tags:
# `hub.docker.com/r/sbx/{claude,codex}-mixin/tags`). Each mixin carries its
# agent's proxy-managed credential (anthropic / openai OAuth bound on the
# HOST via `sbx secret set`), so a fresh sandbox needs no in-VM login.
CLAUDE_MIXIN = "sbx/claude-mixin:2.1.332"
CODEX_MIXIN = "sbx/codex-mixin:0.171.0"


def state_kit() -> Path:
    """tmux-agents' own v3 mixin (package data): persistent volumes for the
    agent state that must survive rm + create under the same name. sbx keys
    volume storage on (sandbox name, path), not kit identity, so this path
    moving across reinstalls doesn't orphan data."""
    return Path(str(resources.files("tmux_agents") / "sbx_kits" / "state"))


def create_argv(proj: Project) -> list[str]:
    """`sbx create` argv (minus the binary): workload + both agent mixins +
    the state kit + the project's own kits, then repo + extra workspaces."""
    argv = ["create", "--name", proj.sandbox_name]
    if proj.sbx_memory:
        argv += ["-m", proj.sbx_memory]
    for kit in (CLAUDE_MIXIN, CODEX_MIXIN, str(state_kit()), *proj.sbx_kits):
        argv += ["--kit", kit]
    argv += [proj.sbx_workload, str(proj.repo), *proj.sbx_mounts]
    return argv


def _create(proj: Project) -> None:
    """Run `sbx create` for the project. Caller MUST hold the project's
    `sbx_create_lock`."""
    name = proj.sandbox_name
    argv = create_argv(proj)
    logger.info("creating sandbox %r: sbx %s", name, " ".join(argv))
    _check(_run(argv, timeout=CREATE_TIMEOUT), f"create {name!r}")
    if not is_present(name):
        raise SandboxError(f"sbx create ran but sandbox {name!r} is not present")


def ensure_up(proj: Project) -> bool:
    """Create the project's sandbox if it doesn't exist. Returns True iff
    it was created. A fresh VM keeps sessions (state-kit volumes reattach
    by name) and logins (proxy-managed), but loses everything else in the
    home dir — callers re-provision codex hooks + gh auth. Per-name lock
    around inspect -> create -> re-inspect, so the check-then-create race
    container.ensure_up has is not copied here."""
    with locks.locked(paths.sbx_create_lock(proj.sandbox_name)):
        if is_present(proj.sandbox_name):
            return False
        _create(proj)
        return True


def recreate(proj: Project) -> None:
    """rm + create as ONE critical section under the per-name lock —
    rebuild's recreate must not let a concurrent `ensure_up` (agent-new,
    restore) slip its own create between the removal and rebuild's create
    (the second create would then fail on the taken name, failing the
    rebuild after the old sandbox is already gone). Agent state needs no carrying: `sbx rm` retains the
    state-kit volumes and the same-name create reattaches them."""
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
