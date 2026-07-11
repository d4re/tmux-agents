"""Integration tests for the `_provision` worker's stage progress output.

These tests verify that the correct stage lines appear in the spawn-log file
(which the placeholder pane tail -F's), and that the final disposition (respawn,
hold, or error) is correct.  All tests use the _provision_env helper + the
`tmp_state_dir` fixture so TMUX_AGENTS_STATE_DIR is isolated.
"""
import os

from tmux_agents import container, provisioning, ssh_forward, worktree
from tmux_agents import startup, phase
from tmux_agents import windows as windows_mod
from tmux_agents.commands import new
from tmux_agents.ssh_forward import PumpResult
from types import SimpleNamespace


def _write_projects(tmp_config_dir, repo):
    (tmp_config_dir / "projects.toml").write_text(
        f'[backend]\nrepo = "{repo}"\n'
        f'container = "backend-c"\n'
        f'up_cmd = "true"\n'
        f'exec_cmd = "true"\n'
    )


def _provision_env_prog(monkeypatch, tmp_config_dir, tmp_state_dir, repo,
                        *, fail_container=False, fail_worktree=False,
                        warn_hooks=False, warn_worktree=False, pump_result="ready"):
    """Provision env for progress-integration tests.  Returns a cap namespace."""
    cap = SimpleNamespace(respawns=[], static_texts=[], holds=[], states=[])
    monkeypatch.setattr(os, "fork", lambda: 0)
    monkeypatch.setattr(os, "setsid", lambda: None)
    monkeypatch.setattr(startup, "_detach_stdio", lambda: None)

    monkeypatch.setattr(container, "current_name", lambda proj: None)
    if fail_container:
        def boom(proj, *, up_cmd):
            raise container.ContainerError("docker daemon not running")
        monkeypatch.setattr(container, "ensure_up", boom)
    else:
        monkeypatch.setattr(container, "ensure_up", lambda proj, up_cmd: "backend-c")

    def fake_pump(c, u):
        if pump_result == "already_healthy":
            return PumpResult("already_healthy")
        if pump_result == "timed_out":
            return PumpResult("timed_out")
        return PumpResult("ready")
    monkeypatch.setattr(ssh_forward, "maybe_spawn_pump", fake_pump)

    if fail_worktree:
        def boom_wt(r, b, **k):
            raise worktree.WorktreeError("fetch origin main failed: dns")
        monkeypatch.setattr(worktree, "resolve", boom_wt)
    elif warn_worktree:
        def warn_wt(r, b, *, base_override=None, container=None,
                    container_workdir=None, container_user=None, reporter_stage=None):
            if reporter_stage is not None:
                reporter_stage.warn("fetch failed; using cached origin/main")
            target = r / ".worktrees" / b
            target.mkdir(parents=True, exist_ok=True)
            return target
        monkeypatch.setattr(worktree, "resolve", warn_wt)
    else:
        def ok_wt(r, b, **k):
            return r / ".worktrees" / b
        monkeypatch.setattr(worktree, "resolve", ok_wt)

    if warn_hooks:
        def boom_prov(*a, **k):
            raise OSError("permission denied")
        monkeypatch.setattr(provisioning, "provision_settings", boom_prov)
    else:
        monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **k: True)

    monkeypatch.setattr(startup, "_respawn_with_retry",
                        lambda pid, cmd: cap.respawns.append((pid, cmd)))
    monkeypatch.setattr(startup, "show_static_text",
                        lambda pid, text: cap.static_texts.append((pid, text)))
    monkeypatch.setattr(startup, "hold_pane_then_exec",
                        lambda pid, log, cmd: cap.holds.append((pid, cmd)))
    monkeypatch.setattr(startup, "_write_pane_state",
                        lambda wt, pid, *, phase_value: cap.states.append(phase_value))
    return cap


def _read_log(tmp_state_dir, window_id="@5"):
    """Return the content of the spawn log for window_id."""
    log = tmp_state_dir / f"spawn-{window_id}.log"
    try:
        return log.read_text()
    except FileNotFoundError:
        return ""


def _run_provision(monkeypatch, tmp_config_dir, tmp_state_dir, repo,
                   window_id="@5", branch="feat/x", project="backend",
                   **env_kwargs):
    """Seed mapping, run _provision via main(), return (rc, log_text, cap)."""
    cap = _provision_env_prog(monkeypatch, tmp_config_dir, tmp_state_dir, repo, **env_kwargs)
    windows_mod.write_mapping(windows_mod.WindowMapping(
        window_id=window_id, project=project, branch=branch,
        host_worktree=repo, pane_id="23", phase_hint="starting"))
    argv = ["--provision", "--window-id", window_id, "--pane-id", "23",
            "--project", project]
    if branch:
        argv += ["--branch", branch]
    rc = new.main(argv)
    log = _read_log(tmp_state_dir, window_id)
    return rc, log, cap


def test_warm_start_emits_skip_lines(monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path):
    repo = tmp_path / "backend"
    (repo / ".worktrees" / "feat" / "x").mkdir(parents=True)
    _write_projects(tmp_config_dir, repo)
    # Container already running → current_name returns a name → skip.
    monkeypatch.setattr(os, "fork", lambda: 0)
    monkeypatch.setattr(os, "setsid", lambda: None)
    monkeypatch.setattr(startup, "_detach_stdio", lambda: None)
    monkeypatch.setattr(container, "current_name", lambda proj: "backend-c")
    monkeypatch.setattr(ssh_forward, "maybe_spawn_pump",
                        lambda c, u: PumpResult("already_healthy"))
    monkeypatch.setattr(worktree, "resolve",
                        lambda r, b, **k: r / ".worktrees" / b)
    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **k: True)
    cap = SimpleNamespace(respawns=[], static_texts=[], holds=[], states=[])
    monkeypatch.setattr(startup, "_respawn_with_retry",
                        lambda pid, cmd: cap.respawns.append((pid, cmd)))
    monkeypatch.setattr(startup, "show_static_text", lambda *a: None)
    monkeypatch.setattr(startup, "hold_pane_then_exec", lambda *a: None)
    monkeypatch.setattr(startup, "_write_pane_state", lambda *a, **k: None)

    windows_mod.write_mapping(windows_mod.WindowMapping(
        window_id="@5", project="backend", branch="feat/x",
        host_worktree=repo, pane_id="23", phase_hint="starting"))
    rc = new.main(["--provision", "--window-id", "@5", "--pane-id", "23",
                   "--project", "backend", "--branch", "feat/x"])
    log = _read_log(tmp_state_dir)
    assert rc == 0
    assert "Spawning agent: backend / feat/x" in log
    assert "container — already running" in log
    assert "ssh pump" in log
    assert cap.respawns  # success path


def test_cold_start_emits_building_then_check(monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path):
    repo = tmp_path / "backend"
    repo.mkdir()
    _write_projects(tmp_config_dir, repo)
    rc, log, cap = _run_provision(monkeypatch, tmp_config_dir, tmp_state_dir, repo)
    assert rc == 0
    assert "container — building" in log
    assert "container" in log
    assert cap.respawns


def test_hooks_warning_holds_pane(monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path):
    """Hooks provisioning failure → stage warn → hold_pane_then_exec (not key gate to stdin)."""
    repo = tmp_path / "backend"
    (repo / ".worktrees" / "feat" / "x").mkdir(parents=True)
    _write_projects(tmp_config_dir, repo)
    # Container already running to skip container stage issues.
    monkeypatch.setattr(container, "current_name", lambda proj: "backend-c")
    rc, log, cap = _run_provision(monkeypatch, tmp_config_dir, tmp_state_dir, repo,
                                  warn_hooks=True, pump_result="already_healthy")
    assert rc == 0
    assert "hooks" in log
    assert "could not provision" in log
    # Warning path → hold, not direct respawn.
    assert len(cap.holds) == 1
    assert cap.respawns == []
    assert phase.WAITING in cap.states


def test_fatal_container_error_returns_4(monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path):
    """ContainerError → _fatal → show_static_text + rc=4."""
    repo = tmp_path / "backend"
    repo.mkdir()
    _write_projects(tmp_config_dir, repo)
    rc, log, cap = _run_provision(monkeypatch, tmp_config_dir, tmp_state_dir, repo,
                                  fail_container=True)
    assert rc == 4
    assert len(cap.static_texts) == 1
    _, text = cap.static_texts[0]
    assert "container start failed" in text or "docker daemon not running" in text
    assert cap.holds == []
    # Mapping gets phase_hint=errored.
    assert windows_mod.read_mapping("@5").phase_hint == "errored"


def test_worktree_warning_path_triggers_hold(monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path):
    """Offline-fetch fallback (worktree.resolve warns) → hold_pane_then_exec."""
    repo = tmp_path / "backend"
    repo.mkdir()
    _write_projects(tmp_config_dir, repo)
    monkeypatch.setattr(container, "current_name", lambda proj: "backend-c")
    rc, log, cap = _run_provision(monkeypatch, tmp_config_dir, tmp_state_dir, repo,
                                  warn_worktree=True, pump_result="already_healthy")
    assert rc == 0
    assert "fetch failed; using cached origin/main" in log
    assert len(cap.holds) == 1
    assert phase.WAITING in cap.states


def test_host_only_project_skips_worktree_stage(monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path):
    """When branch is None (host-only path), no `worktree` line in the log."""
    repo = tmp_path / "tools"
    repo.mkdir()
    (tmp_config_dir / "projects.toml").write_text(
        f'[tools]\nrepo = "{repo}"\nexec_cmd = "true"\n'
    )
    monkeypatch.setattr(os, "fork", lambda: 0)
    monkeypatch.setattr(os, "setsid", lambda: None)
    monkeypatch.setattr(startup, "_detach_stdio", lambda: None)
    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **k: True)
    cap = SimpleNamespace(respawns=[], static_texts=[], holds=[], states=[])
    monkeypatch.setattr(startup, "_respawn_with_retry",
                        lambda pid, cmd: cap.respawns.append((pid, cmd)))
    monkeypatch.setattr(startup, "show_static_text", lambda *a: None)
    monkeypatch.setattr(startup, "hold_pane_then_exec", lambda *a: None)
    monkeypatch.setattr(startup, "_write_pane_state", lambda *a, **k: None)

    windows_mod.write_mapping(windows_mod.WindowMapping(
        window_id="@5", project="tools", branch=None,
        host_worktree=repo, pane_id="23", phase_hint="starting"))
    rc = new.main(["--provision", "--window-id", "@5", "--pane-id", "23",
                   "--project", "tools"])
    log = _read_log(tmp_state_dir)
    assert rc == 0
    assert "Spawning agent: tools" in log
    assert "worktree" not in log
    assert "hooks" in log
    assert cap.respawns


def test_fatal_worktree_error_returns_4(monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path):
    """WorktreeError → _fatal → show_static_text + rc=4."""
    repo = tmp_path / "backend"
    repo.mkdir()
    _write_projects(tmp_config_dir, repo)
    monkeypatch.setattr(container, "current_name", lambda proj: "backend-c")
    rc, log, cap = _run_provision(monkeypatch, tmp_config_dir, tmp_state_dir, repo,
                                  fail_worktree=True, pump_result="already_healthy")
    assert rc == 4
    assert len(cap.static_texts) == 1
    _, text = cap.static_texts[0]
    assert "worktree resolve failed" in text or "fetch origin main failed" in text
    assert cap.holds == []
    assert windows_mod.read_mapping("@5").phase_hint == "errored"


def test_ssh_pump_timeout_warns_and_triggers_hold(monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path):
    """PumpResult('timed_out') → stage warn → hold_pane_then_exec."""
    repo = tmp_path / "backend"
    (repo / ".worktrees" / "feat" / "x").mkdir(parents=True)
    _write_projects(tmp_config_dir, repo)
    monkeypatch.setattr(container, "current_name", lambda proj: "backend-c")
    rc, log, cap = _run_provision(monkeypatch, tmp_config_dir, tmp_state_dir, repo,
                                  pump_result="timed_out")
    assert rc == 0
    assert "ssh pump" in log
    assert "timed_out" in log or "not ready" in log
    assert len(cap.holds) == 1
    assert phase.WAITING in cap.states


def test_hooks_warning_also_logged(monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path,
                                   tmux_agents_caplog):
    """Hooks-provision failure appears in BOTH the spawn log AND the unified logger."""
    import logging
    repo = tmp_path / "backend"
    (repo / ".worktrees" / "feat" / "x").mkdir(parents=True)
    _write_projects(tmp_config_dir, repo)
    monkeypatch.setattr(container, "current_name", lambda proj: "backend-c")
    with tmux_agents_caplog.at_level(logging.WARNING, logger="tmux_agents"):
        rc, log, cap = _run_provision(monkeypatch, tmp_config_dir, tmp_state_dir, repo,
                                      warn_hooks=True, pump_result="already_healthy")
    assert rc == 0
    assert "could not provision" in log
    assert any(
        "provisioning failed" in rec.message
        for rec in tmux_agents_caplog.records
    )
