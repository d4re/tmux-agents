"""sandbox.py unit tests: every test monkeypatches subprocess.run — no real sbx."""

import subprocess
from types import SimpleNamespace

import pytest

from tmux_agents import sandbox


def _result(rc=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


@pytest.fixture
def runs(monkeypatch):
    """Capture sbx invocations; each test seeds `queue` with results."""
    calls: list[dict] = []
    queue: list = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        result = queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    return SimpleNamespace(calls=calls, queue=queue)


def test_exec_capture_success(runs):
    runs.queue.append(_result(stdout="/home/agent"))
    out = sandbox.exec_capture("acg", 'printf %s "$HOME"')
    assert out == "/home/agent"
    argv = runs.calls[0]["argv"]
    assert argv[:2] == ["sbx", "exec"]
    assert argv[-4:] == ["acg", "sh", "-c", 'printf %s "$HOME"']


def test_exec_capture_stdin_closed_without_input(runs):
    """An interactive sbx prompt in a background worker is an invisible
    hang — stdin must be closed unless data is being piped in."""
    runs.queue.append(_result())
    sandbox.exec_capture("acg", "true")
    assert runs.calls[0].get("stdin") == subprocess.DEVNULL
    assert "-i" not in runs.calls[0]["argv"]


def test_exec_capture_passes_stdin_data(runs):
    runs.queue.append(_result())
    sandbox.exec_capture("acg", "cat > /tmp/x", stdin="hello")
    assert runs.calls[0].get("input") == "hello"
    assert "stdin" not in runs.calls[0]
    assert "-i" in runs.calls[0]["argv"]


def test_exec_capture_has_timeout(runs):
    runs.queue.append(_result())
    sandbox.exec_capture("acg", "true")
    assert runs.calls[0]["timeout"] == sandbox.EXEC_TIMEOUT


def test_missing_binary_raises_install_hint(runs):
    runs.queue.append(FileNotFoundError("sbx"))
    with pytest.raises(sandbox.SandboxError, match="not installed"):
        sandbox.exec_capture("acg", "true")


def test_timeout_raises_bounded_error(runs):
    runs.queue.append(subprocess.TimeoutExpired(cmd="sbx", timeout=60))
    with pytest.raises(sandbox.SandboxError, match="timed out"):
        sandbox.exec_capture("acg", "true")


def test_login_expired_classified(runs):
    """Login expiry can surface on ANY call (sessions lapse ~2-weekly),
    so classification is shared, not create-only."""
    runs.queue.append(_result(rc=1, stderr="Error: not logged in"))
    with pytest.raises(sandbox.SandboxError, match="sbx login"):
        sandbox.exec_capture("acg", "true")


def test_daemon_down_classified(runs):
    runs.queue.append(_result(rc=1, stderr="cannot connect to the sandboxes daemon"))
    with pytest.raises(sandbox.SandboxError, match="daemon"):
        sandbox.exec_capture("acg", "true")


def test_generic_failure_passes_stderr_through(runs):
    runs.queue.append(_result(rc=1, stderr="weird sbx explosion"))
    with pytest.raises(sandbox.SandboxError, match="weird sbx explosion"):
        sandbox.exec_capture("acg", "true")


def test_deliver_is_mktemp_plus_atomic_rename(runs):
    runs.queue.append(_result())
    sandbox.deliver("acg", "/home/agent/.codex/hooks.json", '{"a": 1}', mode="644")
    argv = runs.calls[0]["argv"]
    script = argv[argv.index("-c") + 1]
    assert "mktemp" in script
    assert "mv " in script
    assert "chmod 644" in script
    assert runs.calls[0]["input"] == '{"a": 1}'


def test_deliver_without_mode_skips_chmod(runs):
    runs.queue.append(_result())
    sandbox.deliver("acg", "/home/agent/x", "data")
    script = runs.calls[0]["argv"][runs.calls[0]["argv"].index("-c") + 1]
    assert "chmod" not in script


# ---------------------------------------------------------------------------
# Lifecycle: daemon, presence, ensure_up, remove
# ---------------------------------------------------------------------------


def test_daemon_running_needs_rc0_and_running_status(runs):
    """`sbx daemon status` semantics when stopped are unverified upstream —
    require BOTH rc 0 and a 'Status: running' line so either failure shape
    (nonzero rc, or rc 0 + 'Status: stopped') reads as down."""
    runs.queue.append(_result(rc=0, stdout="Status: running\nSocket: /x\n"))
    assert sandbox.daemon_running() is True
    assert runs.calls[0]["argv"][:3] == ["sbx", "daemon", "status"]
    runs.queue.append(_result(rc=0, stdout="Status: stopped\n"))
    assert sandbox.daemon_running() is False
    runs.queue.append(_result(rc=1, stderr="daemon not running"))
    assert sandbox.daemon_running() is False


def test_daemon_running_accepts_decorated_status_line(runs):
    """sbx 0.48 pads the value and prefixes a bullet glyph; a running daemon
    must not read as down (that made ensure_daemon poll a live daemon until
    its start timeout)."""
    out = "  Status:  \u25cf running\n  Socket:  /x/sandboxd.sock\n"
    runs.queue.append(_result(rc=0, stdout=out))
    assert sandbox.daemon_running() is True
    runs.queue.append(_result(rc=0, stdout="  Status:  \u25cf stopped\n"))
    assert sandbox.daemon_running() is False
    runs.queue.append(_result(rc=0, stdout="  Status:  \u25cf not running\n"))
    assert sandbox.daemon_running() is False


def test_ensure_daemon_skips_start_when_running(runs, tmp_state_dir):
    runs.queue.append(_result(rc=0, stdout="Status: running"))
    sandbox.ensure_daemon()
    assert len(runs.calls) == 1


def test_ensure_daemon_starts_and_polls(runs, tmp_state_dir, monkeypatch):
    monkeypatch.setattr(sandbox.time, "sleep", lambda s: None)
    runs.queue.extend(
        [
            _result(rc=1, stderr="daemon not running"),  # status: down
            _result(rc=0),  # daemon start -d
            _result(rc=1, stderr="daemon not running"),  # poll 1: not yet
            _result(rc=0, stdout="Status: running"),  # poll 2: ready
        ]
    )
    sandbox.ensure_daemon()
    assert runs.calls[1]["argv"] == ["sbx", "daemon", "start", "-d"]


def test_ensure_daemon_raises_when_never_ready(runs, tmp_state_dir, monkeypatch):
    monkeypatch.setattr(sandbox.time, "sleep", lambda s: None)
    clock = iter(range(10_000))
    monkeypatch.setattr(sandbox.time, "monotonic", lambda: next(clock))
    runs.queue.append(_result(rc=1, stderr="daemon not running"))
    runs.queue.append(_result(rc=0))  # start succeeds…
    for _ in range(200):
        runs.queue.append(_result(rc=1, stderr="daemon not running"))  # …never ready
    with pytest.raises(sandbox.SandboxError, match="daemon"):
        sandbox.ensure_daemon()


def test_is_present_parses_ls_quiet(runs):
    """No `sbx inspect` exists (v0.38.0) — presence = name in `ls -q` lines."""
    runs.queue.append(_result(stdout="aiop-compliance-gateway\nacg\n"))
    assert sandbox.is_present("acg") is True
    assert runs.calls[0]["argv"] == ["sbx", "ls", "-q"]
    runs.queue.append(_result(stdout="other\n"))
    assert sandbox.is_present("acg") is False


def _proj(tmp_path, **kw):
    from tmux_agents.config import Project

    defaults = {"name": "acg", "repo": tmp_path, "exec_cmd": "x", "sandbox": True}
    defaults.update(kw)
    return Project(**defaults)


def test_ensure_up_present_returns_false(runs, tmp_state_dir, tmp_path):
    runs.queue.append(_result(stdout="acg\n"))  # is_present → yes
    assert sandbox.ensure_up(_proj(tmp_path)) is False
    assert len(runs.calls) == 1


def test_ensure_up_creates_with_full_argv(runs, tmp_state_dir, tmp_path):
    mount = tmp_path / "kube"
    mount.mkdir()
    proj = _proj(
        tmp_path,
        sbx_template="tpl:1",
        sbx_kits=("https://github.com/rmabon/dotfiles",),
        sbx_mounts=(f"{mount}:ro",),
        sbx_memory="8g",
    )
    runs.queue.extend(
        [
            _result(stdout=""),  # is_present → no
            _result(rc=0),  # create
            _result(stdout="acg\n"),  # re-inspect → present
        ]
    )
    assert sandbox.ensure_up(proj) is True
    create = runs.calls[1]["argv"]
    pairs = [create[i : i + 2] for i in range(len(create))]
    assert create[:4] == ["sbx", "create", "--name", "acg"]
    assert ["-t", "tpl:1"] in pairs
    assert ["--kit", "https://github.com/rmabon/dotfiles"] in pairs
    assert ["-m", "8g"] in pairs
    # Agent positional is always `claude` (dual-agent architecture: codex
    # logs in INSIDE the claude sandbox), then repo, then extra mounts.
    assert create[create.index("claude") :] == ["claude", str(tmp_path), f"{mount}:ro"]
    assert runs.calls[1]["timeout"] == sandbox.CREATE_TIMEOUT


def test_ensure_up_raises_when_create_leaves_nothing(runs, tmp_state_dir, tmp_path):
    runs.queue.extend(
        [
            _result(stdout=""),
            _result(rc=0),
            _result(stdout=""),  # re-inspect: still absent
        ]
    )
    with pytest.raises(sandbox.SandboxError, match="not present"):
        sandbox.ensure_up(_proj(tmp_path))


def test_remove_uses_force(runs):
    """Bare `sbx rm` prompts for confirmation — a hang in a worker."""
    runs.queue.append(_result(rc=0))
    sandbox.remove("acg")
    assert runs.calls[0]["argv"] == ["sbx", "rm", "--force", "acg"]


# ---------------------------------------------------------------------------
# State export/import (agent-rebuild's state-preserving recreate)
# ---------------------------------------------------------------------------


def test_export_state_tar_scope(runs):
    """Export carries only what can't be recreated: ~/.claude minus the
    shared skills mount and kit-regenerated settings.json; codex sessions,
    history, and auth.json (a one-time transfer whose source dies with the
    sandbox keeps the token lineage single). codex config.toml stays OUT —
    kits regenerate it, and restoring an old copy would clobber a new
    template's config."""
    import base64 as b64

    runs.queue.append(_result(stdout=b64.b64encode(b"TARBYTES").decode()))
    blob = sandbox.export_state("acg")
    assert blob == b"TARBYTES"
    script = runs.calls[0]["argv"][runs.calls[0]["argv"].index("-c") + 1]
    assert ".claude" in script
    assert "--exclude='.claude/skills'" in script
    assert "--exclude='.claude/settings.json'" in script
    assert ".codex/auth.json" in script
    assert ".codex/sessions" in script
    assert "config.toml" not in script
    # Failure semantics: sh has no pipefail, so base64 must be GATED on
    # tar's own exit status via a temp-file stage — a `tar | base64` pipe
    # would return base64's rc 0 over a truncated archive, and rebuild
    # would then delete the sandbox believing the state was saved.
    assert 'tar -cf "$t"' in script
    assert '&& base64 < "$t"' in script
    assert "| base64" not in script


def test_import_state_roundtrips_base64(runs):
    import base64 as b64

    runs.queue.append(_result())
    sandbox.import_state("acg", b"TARBYTES")
    assert runs.calls[0]["input"] == b64.b64encode(b"TARBYTES").decode()
    script = runs.calls[0]["argv"][runs.calls[0]["argv"].index("-c") + 1]
    assert "base64 -d" in script
    assert "tar -xf -" in script


def test_recreate_is_one_locked_rm_create(runs, tmp_state_dir, tmp_path):
    """rm + create run as one critical section under the per-name lock, so
    a concurrent ensure_up can't slip a create between them (rebuild would
    then import state into the other caller's sandbox)."""
    runs.queue.extend(
        [
            _result(stdout="acg\n"),  # is_present → yes
            _result(rc=0),  # rm --force
            _result(rc=0),  # create
            _result(stdout="acg\n"),  # re-inspect → present
        ]
    )
    sandbox.recreate(_proj(tmp_path))
    verbs = [c["argv"][1] for c in runs.calls]
    assert verbs == ["ls", "rm", "create", "ls"]


def test_recreate_absent_sandbox_just_creates(runs, tmp_state_dir, tmp_path):
    runs.queue.extend(
        [
            _result(stdout=""),  # is_present → no
            _result(rc=0),  # create
            _result(stdout="acg\n"),  # re-inspect
        ]
    )
    sandbox.recreate(_proj(tmp_path))
    verbs = [c["argv"][1] for c in runs.calls]
    assert verbs == ["ls", "create", "ls"]


# ===== network_allowed (policy preflight) =====


def test_network_allowed_true_from_json(runs):
    runs.queue.append(_result(stdout='{"allowed": true, "target": "h:443"}'))
    assert sandbox.network_allowed("acg", "update.code.visualstudio.com") is True
    argv = runs.calls[0]["argv"]
    assert argv[:5] == ["sbx", "policy", "check", "network", "--json"]
    assert argv[-3:] == ["--sandbox", "acg", "update.code.visualstudio.com"]
    assert runs.calls[0]["timeout"] == sandbox.PROBE_TIMEOUT


def test_network_allowed_false_from_json_with_nonzero_rc(runs):
    # `sbx policy check` exits 1 on a deny and still prints JSON.
    runs.queue.append(
        _result(rc=1, stdout='{"allowed": false, "deny_kind": "implicit"}')
    )
    assert sandbox.network_allowed("acg", "blocked.example") is False


def test_network_allowed_unknown_on_non_json(runs):
    # Unknown sandbox: rc 0 + 'ERROR: ... not found' on stderr, no JSON.
    runs.queue.append(_result(rc=0, stdout="", stderr="ERROR: sandbox not found"))
    assert sandbox.network_allowed("acg", "h") is None


def test_network_allowed_unknown_when_sbx_missing_or_slow(runs):
    runs.queue.append(FileNotFoundError())
    assert sandbox.network_allowed("acg", "h") is None
    runs.queue.append(subprocess.TimeoutExpired("sbx", 15))
    assert sandbox.network_allowed("acg", "h") is None


# --- reap_pane_agents: guest-side cleanup for agent-kill --------------------
#
# `sbx exec` does not forward the host-side hangup into the guest, so killing
# a pane leaves its `claude` running in the VM (pinning deleted worktrees as
# ghost dirs). The reap script is plain sh over /proc; these run it for real
# against a fake proc root whose pid dirs point at real local processes.


def _spawn_sleeper(*, ignore_term: bool = False) -> int:
    """Start a sleeper that is NOT our child (sh backgrounds it and exits),
    so launchd/init reaps it on death — a zombie child of pytest would
    still answer `kill -0` and stall the reap script's grace loop."""
    import shlex
    import sys

    code = (
        "import signal, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
        + "print('ready', flush=True)\ntime.sleep(60)\n"
    )
    sh = subprocess.Popen(
        ["sh", "-c", f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} & echo $!"],
        stdout=subprocess.PIPE,
        text=True,
    )
    pid = int(sh.stdout.readline())
    assert sh.stdout.readline().strip() == "ready"
    sh.wait()
    sh.stdout.close()
    return pid


def _alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_dead(pid: int, timeout: float = 5) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _cleanup(*pids: int) -> None:
    import os
    import signal

    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _fake_proc(root, pid: int, env: dict[str, str]) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "environ").write_bytes(b"".join(f"{k}={v}\0".encode() for k, v in env.items()))


def _run_reap_script(script: str) -> str:
    return subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, timeout=30, check=True
    ).stdout


def test_reap_script_kills_only_tagged_agents_of_the_given_panes(tmp_path):
    target = _spawn_sleeper()
    other_pane = _spawn_sleeper()  # %4 — must not match %40 by prefix
    untagged = _spawn_sleeper()  # right pane, but not an agent-launched process
    try:
        _fake_proc(tmp_path, target, {"TMUX_AGENTS_AGENT": "1", "TMUX_PANE": "%40"})
        _fake_proc(tmp_path, other_pane, {"TMUX_AGENTS_AGENT": "1", "TMUX_PANE": "%4"})
        _fake_proc(tmp_path, untagged, {"TMUX_PANE": "%40"})
        import time

        start = time.monotonic()
        out = _run_reap_script(
            sandbox._reap_script(["40"], proc_root=str(tmp_path), grace_s=10)
        )
        # Well inside the grace window: the target died of SIGTERM, not of
        # the SIGKILL fallback.
        assert time.monotonic() - start < 3
        assert out.strip() == "1"
        assert _wait_dead(target)
        assert _alive(other_pane)
        assert _alive(untagged)
    finally:
        _cleanup(target, other_pane, untagged)


def test_reap_script_sigkills_agents_that_ignore_sigterm(tmp_path):
    stubborn = _spawn_sleeper(ignore_term=True)
    try:
        _fake_proc(tmp_path, stubborn, {"TMUX_AGENTS_AGENT": "1", "TMUX_PANE": "%7"})
        out = _run_reap_script(
            sandbox._reap_script(["7"], proc_root=str(tmp_path), grace_s=0.3)
        )
        assert out.strip() == "1"
        assert _wait_dead(stubborn)  # SIGTERM is ignored: only the KILL ends it
    finally:
        _cleanup(stubborn)


def test_reap_script_with_no_match_reports_zero(tmp_path):
    out = _run_reap_script(sandbox._reap_script(["40"], proc_root=str(tmp_path)))
    assert out.strip() == "0"


def _ls_json(status: str) -> str:
    return (
        '{"sandboxes": [{"name": "other", "status": "running"}, '
        f'{{"name": "proj", "status": "{status}"}}]}}'
    )


def test_reap_pane_agents_runs_script_in_running_sandbox(runs):
    runs.queue += [_result(stdout=_ls_json("running")), _result(stdout="2\n")]
    assert sandbox.reap_pane_agents("proj", ["40", "41"]) == 2
    exec_argv = runs.calls[1]["argv"]
    assert exec_argv[:3] == ["sbx", "exec", "proj"]
    assert "%40" in exec_argv[-1] and "%41" in exec_argv[-1]


def test_reap_pane_agents_never_boots_a_stopped_sandbox(runs):
    # `sbx exec` auto-starts a stopped VM — a stopped VM has no processes
    # to reap, so booting one from agent-kill would be pure cost.
    runs.queue.append(_result(stdout=_ls_json("stopped")))
    assert sandbox.reap_pane_agents("proj", ["40"]) == 0
    assert len(runs.calls) == 1


def test_status_reads_bare_list_json_from_sbx_049(runs):
    # sbx 0.49 dropped the {"sandboxes": [...]} wrapper: `ls --json` is a
    # bare list of sandbox objects.
    runs.queue.append(
        _result(
            stdout='[{"name": "other", "status": "running"}, '
            '{"name": "proj", "status": "stopped", "memory": "12g"}]'
        )
    )
    assert sandbox.status("proj") == "stopped"


def test_status_reads_wrapped_json_from_older_sbx(runs):
    runs.queue.append(_result(stdout=_ls_json("running")))
    assert sandbox.status("proj") == "running"


def test_status_absent_sandbox_is_none(runs):
    runs.queue.append(_result(stdout='[{"name": "other", "status": "running"}]'))
    assert sandbox.status("proj") is None


def test_status_unexpected_json_shape_raises_sandbox_error(runs):
    # Callers (agent-kill) catch SandboxError only — a format drift must
    # never surface as AttributeError/TypeError and abort the kill.
    runs.queue.append(_result(stdout='"surprise"'))
    with pytest.raises(sandbox.SandboxError):
        sandbox.status("proj")


def test_reap_pane_agents_without_pane_ids_does_nothing(runs):
    assert sandbox.reap_pane_agents("proj", []) == 0
    assert runs.calls == []


# --- path_visible: does the guest see a host path? ---------------------------


def test_path_visible_true_and_false(runs):
    runs.queue += [_result(stdout="yes\n"), _result(stdout="no\n")]
    assert sandbox.path_visible("proj", "/w/feat x") is True
    assert sandbox.path_visible("proj", "/w/feat x") is False
    assert "'/w/feat x'" in runs.calls[0]["argv"][-1]


def test_path_visible_unknown_when_probe_fails(runs):
    runs.queue.append(_result(rc=1, stderr="boom"))
    assert sandbox.path_visible("proj", "/w") is None
