import os
import subprocess
import sys
from unittest.mock import MagicMock


from tmux_agents import ssh_forward


def test_has_python3_in_container_true(monkeypatch):
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="/usr/bin/python3\n")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ssh_forward.has_python3_in_container("api", "vscode") is True
    assert calls == [["docker", "exec", "-u", "vscode", "api", "python3", "--version"]]


def test_has_python3_in_container_false(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: MagicMock(returncode=1, stdout=""),
    )
    assert ssh_forward.has_python3_in_container("api") is False


def test_host_ssh_auth_sock_returns_env(monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/private/tmp/agent.sock")
    assert ssh_forward.host_ssh_auth_sock() == "/private/tmp/agent.sock"


def test_host_ssh_auth_sock_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    assert ssh_forward.host_ssh_auth_sock() is None


def test_spawn_pump_invokes_popen_with_module_and_devnull(monkeypatch, tmp_state_dir):
    captured = {}
    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
        def poll(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    handle = ssh_forward.spawn_pump("api-devcontainer", "vscode")
    assert isinstance(handle, FakePopen)
    cmd = captured["cmd"]
    # <python> -m tmux_agents._ssh_pump_script <container> <user>
    assert cmd[0] == sys.executable
    assert cmd[1] == "-m"
    assert cmd[2] == "tmux_agents._ssh_pump_script"
    assert cmd[3] == "api-devcontainer"
    assert cmd[4] == "vscode"
    assert captured["kwargs"]["start_new_session"] is True
    # Per-container log file is retired; pump stderr/stdout go to DEVNULL.
    assert captured["kwargs"]["stdout"] == subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] == subprocess.DEVNULL


def test_spawn_pump_defaults_user_to_vscode(monkeypatch, tmp_state_dir):
    captured = {}
    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
        def poll(self):
            return None
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    ssh_forward.spawn_pump("foo")
    assert captured["cmd"][4] == "vscode"


def test_is_pump_responsive_true_when_ssh_add_returns(monkeypatch):
    """Any return code (0=keys, 1=no identities, 2=agent unreachable) means
    the call completed — that's what 'responsive' tests."""
    def fake_run(cmd, **kw):
        assert "ssh-add" in cmd
        assert "brave_benz" in cmd
        return MagicMock(returncode=1, stdout="The agent has no identities.\n", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ssh_forward.is_pump_responsive("brave_benz", "vscode") is True


def test_is_pump_responsive_false_on_timeout(monkeypatch):
    """The actual failure mode: ssh-add hangs forever because the relay accepts
    but the host pump can't round-trip. Must return False so we respawn."""
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ssh_forward.is_pump_responsive("brave_benz", "vscode") is False


def test_is_pump_responsive_false_when_docker_missing(monkeypatch):
    def fake_run(cmd, **kw):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ssh_forward.is_pump_responsive("brave_benz", "vscode") is False


def test_wait_until_pump_ready_returns_true_once_agent_reachable(monkeypatch):
    """ssh-add -l rc 0/1 means the UDS is bound and the relay is shuttling
    bytes — the relay is up after N polls, which the helper should detect."""
    calls = {"n": 0}
    def fake_run(cmd, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            return MagicMock(returncode=2, stdout="", stderr="Could not open\n")
        return MagicMock(returncode=1, stdout="The agent has no identities.\n", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    assert ssh_forward.wait_until_pump_ready("brave_benz", "vscode") is True
    assert calls["n"] == 3


def test_wait_until_pump_ready_returns_false_after_deadline(monkeypatch):
    """If the relay never comes up, give up after the deadline so the caller
    can warn and proceed (SSH falls back to no-agent auth)."""
    def fake_run(cmd, **kw):
        return MagicMock(returncode=2, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    assert ssh_forward.wait_until_pump_ready(
        "brave_benz", "vscode", deadline_secs=0.0,
    ) is False


def test_pump_pids_for_filters_by_container_argv(monkeypatch):
    """pgrep finds all pumps; filter to the ones whose argv ends with
    `<container> <user>` so unrelated pumps for other containers survive."""
    def fake_run(cmd, **kw):
        if cmd[:2] == ["pgrep", "-f"]:
            return MagicMock(returncode=0, stdout="111\n222\n333\n", stderr="")
        if cmd[:2] == ["ps", "-o"]:
            pid = cmd[-1]
            argv_by_pid = {
                "111": "/usr/bin/python3 -m tmux_agents._ssh_pump_script brave_benz vscode\n",
                "222": "/usr/bin/python3 -m tmux_agents._ssh_pump_script other_container vscode\n",
                "333": "/usr/bin/python3 -m tmux_agents._ssh_pump_script brave_benz vscode\n",
            }
            return MagicMock(returncode=0, stdout=argv_by_pid[pid], stderr="")
        raise AssertionError(f"unexpected cmd: {cmd}")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ssh_forward.pump_pids_for("brave_benz") == [111, 333]


def test_pump_pids_for_returns_empty_when_no_pumps(monkeypatch):
    def fake_run(cmd, **kw):
        # pgrep exits 1 when no matches.
        return MagicMock(returncode=1, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ssh_forward.pump_pids_for("brave_benz") == []


def test_kill_stale_pumps_sigterms_each_pid(monkeypatch):
    pids = [111, 222]
    monkeypatch.setattr(ssh_forward, "pump_pids_for", lambda c: list(pids) if pids else [])
    killed: list[tuple[int, int]] = []
    def fake_kill(pid, sig):
        killed.append((pid, sig))
        if sig == 15:  # SIGTERM
            pids.remove(pid)  # drain so SIGKILL pass finds no stragglers
    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr("time.sleep", lambda s: None)
    n = ssh_forward.kill_stale_pumps("brave_benz")
    assert n == 2
    sigs = [s for _, s in killed]
    assert 15 in sigs  # SIGTERM was sent
