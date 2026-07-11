from tmux_agents import ssh_forward


def test_pumpresult_outcome_disabled_no_sock(monkeypatch):
    monkeypatch.setattr(ssh_forward, "host_ssh_auth_sock", lambda: None)
    r = ssh_forward.maybe_spawn_pump("container-x", "vscode")
    assert r.outcome == "disabled_no_sock"
    assert r.killed_stale == 0


def test_pumpresult_outcome_disabled_no_python(monkeypatch):
    monkeypatch.setattr(ssh_forward, "host_ssh_auth_sock", lambda: "/tmp/sock")
    monkeypatch.setattr(ssh_forward, "has_python3_in_container", lambda *a, **k: False)
    r = ssh_forward.maybe_spawn_pump("container-x", "vscode")
    assert r.outcome == "disabled_no_python"


def test_pumpresult_outcome_already_healthy(monkeypatch):
    monkeypatch.setattr(ssh_forward, "host_ssh_auth_sock", lambda: "/tmp/sock")
    monkeypatch.setattr(ssh_forward, "has_python3_in_container", lambda *a, **k: True)
    monkeypatch.setattr(ssh_forward, "pump_pids_for", lambda c: [123])
    monkeypatch.setattr(ssh_forward, "is_pump_responsive", lambda *a, **k: True)
    r = ssh_forward.maybe_spawn_pump("container-x", "vscode")
    assert r.outcome == "already_healthy"


def test_pumpresult_outcome_ready_after_kill(monkeypatch):
    monkeypatch.setattr(ssh_forward, "host_ssh_auth_sock", lambda: "/tmp/sock")
    monkeypatch.setattr(ssh_forward, "has_python3_in_container", lambda *a, **k: True)
    monkeypatch.setattr(ssh_forward, "pump_pids_for", lambda c: [123])
    monkeypatch.setattr(ssh_forward, "is_pump_responsive", lambda *a, **k: False)
    monkeypatch.setattr(ssh_forward, "kill_stale_pumps", lambda c: 2)
    monkeypatch.setattr(ssh_forward, "spawn_pump", lambda c, u: None)
    monkeypatch.setattr(ssh_forward, "wait_until_pump_ready", lambda *a, **k: True)
    r = ssh_forward.maybe_spawn_pump("container-x", "vscode")
    assert r.outcome == "ready"
    assert r.killed_stale == 2


def test_pumpresult_outcome_ready_fresh_spawn(monkeypatch):
    monkeypatch.setattr(ssh_forward, "host_ssh_auth_sock", lambda: "/tmp/sock")
    monkeypatch.setattr(ssh_forward, "has_python3_in_container", lambda *a, **k: True)
    monkeypatch.setattr(ssh_forward, "pump_pids_for", lambda c: [])
    monkeypatch.setattr(ssh_forward, "spawn_pump", lambda c, u: None)
    monkeypatch.setattr(ssh_forward, "wait_until_pump_ready", lambda *a, **k: True)
    r = ssh_forward.maybe_spawn_pump("container-x", "vscode")
    assert r.outcome == "ready"
    assert r.killed_stale == 0


def test_pumpresult_outcome_timed_out(monkeypatch):
    monkeypatch.setattr(ssh_forward, "host_ssh_auth_sock", lambda: "/tmp/sock")
    monkeypatch.setattr(ssh_forward, "has_python3_in_container", lambda *a, **k: True)
    monkeypatch.setattr(ssh_forward, "pump_pids_for", lambda c: [])
    monkeypatch.setattr(ssh_forward, "spawn_pump", lambda c, u: None)
    monkeypatch.setattr(ssh_forward, "wait_until_pump_ready", lambda *a, **k: False)
    r = ssh_forward.maybe_spawn_pump("container-x", "vscode")
    assert r.outcome == "timed_out"
