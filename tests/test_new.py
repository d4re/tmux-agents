import shlex
from types import SimpleNamespace
from tmux_agents.commands import new
from tmux_agents import tmux, container, pickers, worktree, ssh_forward, phase
from tmux_agents import windows as windows_mod


def test_spawn_worker_uses_tmux_server_not_popen(monkeypatch):
    """The provisioning worker must be launched via `tmux run-shell -b` (the
    long-lived server), NOT subprocess.Popen from inside the popup. A Popen'd
    worker is killed when the display-popup closes, leaving the window stuck in
    S with no progress. Regression guard for that bug."""
    calls = []
    monkeypatch.setattr(new.tmux, "run_shell_bg", lambda command: calls.append(command))

    def _boom_popen(*a, **k):
        raise AssertionError(
            "worker spawned via subprocess.Popen; must use tmux.run_shell_bg"
        )

    monkeypatch.setattr(new.subprocess, "Popen", _boom_popen)

    new._spawn_worker("@9", "42", "myproj", "feat/slash-ok")

    assert len(calls) == 1
    assert shlex.split(calls[0]) == [
        "agent-new",
        "--provision",
        "--window-id",
        "@9",
        "--pane-id",
        "42",
        "--project",
        "myproj",
        "--branch",
        "feat/slash-ok",
    ]


FIXTURE_TOML = """
[api]
repo = "{repo}"
container = "api-devcontainer"
container_workdir = "/work"
up_cmd = "echo up"
exec_cmd = "docker exec -it {{container}} bash -lc 'cd {{workdir}} && claude'"

[scripts]
repo = "{repo2}"
exec_cmd = "cd {{workdir}} && claude"
"""


def _write_config(tmp_config_dir, tmp_path, *, repo_must_exist=True):
    repo = tmp_path / "api"
    repo2 = tmp_path / "scripts"
    if repo_must_exist:
        repo.mkdir()
        repo2.mkdir()
    (tmp_config_dir / "projects.toml").write_text(
        FIXTURE_TOML.format(repo=str(repo), repo2=str(repo2))
    )
    return repo, repo2


def _provision_env(
    monkeypatch, tmp_config_dir, tmp_path, *, warn=False, fail_container=False
):
    """Stub container/worktree/tmux for a --provision worker run. Returns a
    SimpleNamespace with capture lists: respawns, static_texts, holds, states.

    Patches os.fork to return 0 (child path) and _detach_stdio to a no-op so
    tests drive _provision directly via new.main(["--provision", ...]) without
    actually forking or closing file descriptors."""
    import os as _os
    from tmux_agents import container, worktree, provisioning, ssh_forward, startup
    from tmux_agents import windows as windows_mod

    repo, _ = _write_config(tmp_config_dir, tmp_path)
    cap = SimpleNamespace(respawns=[], static_texts=[], holds=[], states=[])
    # Simulate being in the child: fork() returns 0, setsid and detach are no-ops.
    monkeypatch.setattr(_os, "fork", lambda: 0)
    monkeypatch.setattr(_os, "setsid", lambda: None)
    monkeypatch.setattr(startup, "_detach_stdio", lambda: None)
    # Seed the mapping the interactive part would have written.
    windows_mod.write_mapping(
        windows_mod.WindowMapping(
            window_id="@5",
            project="api",
            branch=None,
            host_worktree=repo,
            pane_id="23",
            phase_hint="starting",
        )
    )
    monkeypatch.setattr(container, "current_name", lambda proj: None)
    if fail_container:

        def boom(proj, *, up_cmd):
            raise container.ContainerError("docker down")

        monkeypatch.setattr(container, "ensure_up", boom)
    else:
        monkeypatch.setattr(
            container, "ensure_up", lambda proj, up_cmd: "api-devcontainer"
        )
    monkeypatch.setattr(
        ssh_forward, "maybe_spawn_pump", lambda c, u: ssh_forward.PumpResult("ready")
    )
    monkeypatch.setattr(worktree, "resolve", lambda *a, **k: repo)
    monkeypatch.setattr(worktree, "check_freshness", lambda *a, **k: None)

    def fake_provision(*a, **k):
        if warn:
            raise RuntimeError("settings write failed")
        return True

    monkeypatch.setattr(provisioning, "provision_settings", fake_provision)
    monkeypatch.setattr(
        startup, "_respawn_with_retry", lambda pid, cmd: cap.respawns.append((pid, cmd))
    )
    monkeypatch.setattr(
        startup,
        "show_static_text",
        lambda pid, text: cap.static_texts.append((pid, text)),
    )
    monkeypatch.setattr(
        startup,
        "hold_pane_then_exec",
        lambda pid, log, cmd: cap.holds.append((pid, cmd)),
    )
    monkeypatch.setattr(
        startup,
        "_write_pane_state",
        lambda wt, pid, *, phase_value: cap.states.append(phase_value),
    )
    return cap


def test_provision_success_respawns_into_claude(monkeypatch, tmp_config_dir, tmp_path):
    cap = _provision_env(monkeypatch, tmp_config_dir, tmp_path)
    rc = new.main(
        ["--provision", "--window-id", "@5", "--pane-id", "23", "--project", "api"]
    )
    assert rc == 0
    assert len(cap.respawns) == 1
    pid, cmd = cap.respawns[0]
    assert pid == "%23"
    assert "claude" in cmd
    assert cap.holds == []


def test_provision_warning_holds_pane(monkeypatch, tmp_config_dir, tmp_path):
    cap = _provision_env(monkeypatch, tmp_config_dir, tmp_path, warn=True)
    rc = new.main(
        ["--provision", "--window-id", "@5", "--pane-id", "23", "--project", "api"]
    )
    assert rc == 0
    assert len(cap.holds) == 1  # held instead of direct respawn
    assert cap.respawns == []
    assert phase.WAITING in cap.states  # window shows W while held


def test_provision_container_failure_shows_error_pane(
    monkeypatch, tmp_config_dir, tmp_path
):
    from tmux_agents import windows as windows_mod

    cap = _provision_env(monkeypatch, tmp_config_dir, tmp_path, fail_container=True)
    rc = new.main(
        ["--provision", "--window-id", "@5", "--pane-id", "23", "--project", "api"]
    )
    assert rc != 0
    assert len(cap.static_texts) == 1
    _, text = cap.static_texts[0]
    assert "docker down" in text
    # Fatal-before-worktree marks the mapping errored (worktree doesn't exist).
    assert windows_mod.read_mapping("@5").phase_hint == "errored"


def test_new_container_no_branch(monkeypatch, tmp_config_dir, tmp_path):
    """Container project, no branch: _provision calls ensure_up and respawns with docker exec cmd."""
    cap = _provision_env(monkeypatch, tmp_config_dir, tmp_path)
    ensured = []
    monkeypatch.setattr(
        __import__("tmux_agents.container", fromlist=["ensure_up"]),
        "ensure_up",
        lambda proj, up_cmd: ensured.append((proj.name, up_cmd)) or "api-devcontainer",
    )
    rc = new.main(
        ["--provision", "--window-id", "@5", "--pane-id", "23", "--project", "api"]
    )
    assert rc == 0
    assert ensured == [("api", "echo up")]
    assert len(cap.respawns) == 1
    _, cmd = cap.respawns[0]
    assert "api-devcontainer" in cmd
    assert "claude" in cmd


def test_new_no_branch_checks_base_freshness(monkeypatch, tmp_config_dir, tmp_path):
    """No-branch mode runs Claude in the repo as-is, so _provision must run a
    freshness check against origin/<default> to warn on a stale checkout."""
    _provision_env(monkeypatch, tmp_config_dir, tmp_path)
    calls = []
    monkeypatch.setattr(
        worktree, "check_freshness", lambda *a, **k: calls.append((a, k))
    )
    rc = new.main(
        ["--provision", "--window-id", "@5", "--pane-id", "23", "--project", "api"]
    )
    assert rc == 0
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["base_override"] is None  # api has no base_branch override
    assert kwargs["container"] == "api-devcontainer"
    assert kwargs["container_workdir"] == "/work"


def test_new_container_with_branch_creates_worktree(
    monkeypatch, tmp_config_dir, tmp_path
):
    """worktree.resolve is called with the right container name and workdir."""
    cap = _provision_env(monkeypatch, tmp_config_dir, tmp_path)
    # _provision_env already created repo via _write_config; grab the path.
    repo = tmp_path / "api"
    # Seed mapping with branch (overwrite the one seeded by _provision_env).
    windows_mod.write_mapping(
        windows_mod.WindowMapping(
            window_id="@5",
            project="api",
            branch="feat-x",
            host_worktree=repo,
            pane_id="23",
            phase_hint="starting",
        )
    )
    wt_calls = []

    def fake_resolve(
        repo_arg,
        branch,
        *,
        base_override=None,
        container=None,
        container_workdir=None,
        container_user=None,
        reporter_stage=None,
    ):
        wt_calls.append((repo_arg, branch, container, container_workdir))
        return repo_arg / ".worktrees" / branch

    monkeypatch.setattr(worktree, "resolve", fake_resolve)
    rc = new.main(
        [
            "--provision",
            "--window-id",
            "@5",
            "--pane-id",
            "23",
            "--project",
            "api",
            "--branch",
            "feat-x",
        ]
    )
    assert rc == 0
    assert wt_calls == [(repo, "feat-x", "api-devcontainer", "/work")]
    assert len(cap.respawns) == 1
    _, cmd = cap.respawns[0]
    assert "/work/.worktrees/feat-x" in cmd


def test_new_host_only_skips_container_up(monkeypatch, tmp_config_dir, tmp_path):
    """Host-only project: no container.ensure_up; respawn cmd is `cd … && claude`."""
    import os as _os
    from tmux_agents import container as container_mod, worktree as wt_mod
    from tmux_agents import provisioning as prov_mod, startup as startup_mod
    from tmux_agents import windows as windows_mod

    _, repo2 = _write_config(tmp_config_dir, tmp_path)
    cap = SimpleNamespace(respawns=[], ensured=[])
    monkeypatch.setattr(_os, "fork", lambda: 0)
    monkeypatch.setattr(_os, "setsid", lambda: None)
    monkeypatch.setattr(startup_mod, "_detach_stdio", lambda: None)
    # Seed mapping for scripts project.
    windows_mod.write_mapping(
        windows_mod.WindowMapping(
            window_id="@5",
            project="scripts",
            branch=None,
            host_worktree=repo2,
            pane_id="23",
            phase_hint="starting",
        )
    )
    monkeypatch.setattr(
        container_mod,
        "ensure_up",
        lambda proj, up_cmd: cap.ensured.append((proj.name, up_cmd)) or "c",
    )
    monkeypatch.setattr(wt_mod, "resolve", lambda *a, **k: repo2)
    monkeypatch.setattr(prov_mod, "provision_settings", lambda *a, **k: True)
    monkeypatch.setattr(
        startup_mod,
        "_respawn_with_retry",
        lambda pid, cmd: cap.respawns.append((pid, cmd)),
    )
    monkeypatch.setattr(startup_mod, "show_static_text", lambda *a: None)
    monkeypatch.setattr(startup_mod, "hold_pane_then_exec", lambda *a: None)
    monkeypatch.setattr(startup_mod, "_write_pane_state", lambda *a, **k: None)

    rc = new.main(
        ["--provision", "--window-id", "@5", "--pane-id", "23", "--project", "scripts"]
    )
    assert rc == 0
    assert cap.ensured == []
    assert len(cap.respawns) == 1
    _, cmd = cap.respawns[0]
    assert f"cd {repo2}" in cmd
    assert "claude" in cmd


def test_new_compact_layout_skips_split(
    agent_new_env, tmp_config_dir, tmp_path, tmp_state_dir
):
    _write_config(tmp_config_dir, tmp_path)
    (tmp_state_dir / "layout").write_text("compact")
    new.main(["api"])
    assert agent_new_env.splits == []


def test_new_unknown_project_errors(monkeypatch, tmp_config_dir, tmp_path, capsys):
    _write_config(tmp_config_dir, tmp_path)
    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    rc = new.main(["nope"])
    assert rc != 0
    assert "unknown project" in capsys.readouterr().err


def test_new_no_project_invokes_picker(
    agent_new_env, monkeypatch, tmp_config_dir, tmp_path
):
    _write_config(tmp_config_dir, tmp_path)
    picked = []

    def fake_pick(items, *, prompt):
        picked.append(list(items))
        return "scripts"

    monkeypatch.setattr(pickers, "pick_one", fake_pick)
    monkeypatch.setattr(
        pickers, "pick_or_create", lambda candidates, *, prompt, validator=None: None
    )
    monkeypatch.setattr(worktree, "list_existing", lambda r: [])
    monkeypatch.setattr(windows_mod, "live_branches_for", lambda p: set())
    rc = new.main([])
    assert rc == 0
    assert picked == [["api", "scripts"]]
    assert [m[0] for m in agent_new_env.made] == ["scripts"]


def test_new_picker_prompts_for_branch(
    agent_new_env, monkeypatch, tmp_config_dir, tmp_path
):
    """Interactive path: picker picks a branch, interactive part creates window with right name."""
    _write_config(tmp_config_dir, tmp_path)
    monkeypatch.setattr(pickers, "pick_one", lambda items, *, prompt: "api")
    monkeypatch.setattr(
        pickers,
        "pick_or_create",
        lambda candidates, *, prompt, validator=None: "feat-x",
    )
    monkeypatch.setattr(worktree, "list_existing", lambda r: [])
    monkeypatch.setattr(windows_mod, "live_branches_for", lambda p: set())
    rc = new.main([])
    assert rc == 0
    # Interactive part creates the window with the right name.
    assert agent_new_env.made[0][0] == "api:feat-x"
    # Worker argv carries branch.
    argv = agent_new_env.spawned[0]
    assert argv[argv.index("--branch") + 1] == "feat-x"


def test_new_rejects_invalid_branch_cli(monkeypatch, tmp_config_dir, tmp_path, capsys):
    _write_config(tmp_config_dir, tmp_path)
    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    made = []
    monkeypatch.setattr(tmux, "new_window", lambda *a, **k: made.append(1) or "@5")
    rc = new.main(["api", "bad name"])
    assert rc == 2
    assert "invalid branch name" in capsys.readouterr().err
    assert made == []


def test_new_picker_cancelled_is_noop(
    agent_new_env, monkeypatch, tmp_config_dir, tmp_path
):
    _write_config(tmp_config_dir, tmp_path)
    monkeypatch.setattr(pickers, "pick_one", lambda items, *, prompt: None)
    rc = new.main([])
    assert rc == 0
    assert agent_new_env.made == []


def test_new_branch_prompt_cancelled_is_noop(
    agent_new_env, monkeypatch, tmp_config_dir, tmp_path
):
    _write_config(tmp_config_dir, tmp_path)
    monkeypatch.setattr(pickers, "pick_one", lambda items, *, prompt: "api")

    def boom(candidates, *, prompt, validator=None):
        raise pickers.Cancelled

    monkeypatch.setattr(pickers, "pick_or_create", boom)
    monkeypatch.setattr(worktree, "list_existing", lambda r: [])
    monkeypatch.setattr(windows_mod, "live_branches_for", lambda p: set())
    rc = new.main([])
    assert rc == 0
    assert agent_new_env.made == []


def test_new_picker_ctrlc_is_noop(agent_new_env, monkeypatch, tmp_config_dir, tmp_path):
    _write_config(tmp_config_dir, tmp_path)

    def boom(items, *, prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr(pickers, "pick_one", boom)
    rc = new.main([])
    assert rc == 0
    assert agent_new_env.made == []


def test_new_devcontainer_resolved_name_flows_to_exec_cmd(
    monkeypatch, tmp_config_dir, tmp_path
):
    """Devcontainer: ensure_up returns a dynamic name that flows into exec_cmd."""
    import os as _os
    from tmux_agents import container as container_mod, worktree as wt_mod
    from tmux_agents import provisioning as prov_mod, startup as startup_mod
    from tmux_agents import windows as windows_mod

    repo = tmp_path / "webapp-gateway-service"
    repo.mkdir()
    (tmp_config_dir / "projects.toml").write_text(
        f'[webapp]\nrepo = "{repo}"\ndevcontainer = true\n'
        f'up_cmd = "echo up"\n'
        f"exec_cmd = \"docker exec -it {{container}} bash -lc 'cd {{workdir}} && claude'\"\n"
    )
    cap = SimpleNamespace(respawns=[])
    monkeypatch.setattr(_os, "fork", lambda: 0)
    monkeypatch.setattr(_os, "setsid", lambda: None)
    monkeypatch.setattr(startup_mod, "_detach_stdio", lambda: None)
    windows_mod.write_mapping(
        windows_mod.WindowMapping(
            window_id="@5",
            project="webapp",
            branch=None,
            host_worktree=repo,
            pane_id="23",
            phase_hint="starting",
        )
    )
    monkeypatch.setattr(container_mod, "current_name", lambda proj: None)
    monkeypatch.setattr(container_mod, "ensure_up", lambda proj, up_cmd: "brave_benz")
    monkeypatch.setattr(
        ssh_forward, "maybe_spawn_pump", lambda c, u: ssh_forward.PumpResult("ready")
    )
    monkeypatch.setattr(wt_mod, "resolve", lambda *a, **k: repo)
    monkeypatch.setattr(prov_mod, "provision_settings", lambda *a, **k: True)
    monkeypatch.setattr(
        startup_mod,
        "_respawn_with_retry",
        lambda pid, cmd: cap.respawns.append((pid, cmd)),
    )
    monkeypatch.setattr(startup_mod, "show_static_text", lambda *a: None)
    monkeypatch.setattr(startup_mod, "hold_pane_then_exec", lambda *a: None)
    monkeypatch.setattr(startup_mod, "_write_pane_state", lambda *a, **k: None)

    rc = new.main(
        ["--provision", "--window-id", "@5", "--pane-id", "23", "--project", "webapp"]
    )
    assert rc == 0
    assert len(cap.respawns) == 1
    _, cmd = cap.respawns[0]
    assert (
        cmd
        == "docker exec -it brave_benz bash -lc 'cd /workspaces/webapp-gateway-service && claude'"
    )


def test_new_creates_session_if_missing(
    monkeypatch, tmp_config_dir, tmp_path, tmp_state_dir
):
    _write_config(tmp_config_dir, tmp_path)
    import subprocess
    from types import SimpleNamespace
    from tmux_agents.ssh_forward import PumpResult

    called = []

    def fake_run(cmd, **kw):
        called.append(cmd)
        from unittest.mock import MagicMock

        return MagicMock(returncode=0, stdout="@0\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    from tmux_agents.commands import new as _new_mod

    monkeypatch.setattr(
        _new_mod.subprocess, "Popen", lambda argv, **kw: SimpleNamespace(pid=0)
    )
    monkeypatch.setattr(tmux, "session_exists", lambda s: False)
    monkeypatch.setattr(container, "current_name", lambda proj: None)
    monkeypatch.setattr(container, "ensure_up", lambda *a, **k: "api-devcontainer")
    monkeypatch.setattr(
        ssh_forward, "maybe_spawn_pump", lambda c, u: PumpResult("ready")
    )
    monkeypatch.setattr(tmux, "new_window", lambda s, *, name, command, **_: "@5")
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%23")
    monkeypatch.setattr(tmux, "respawn_pane", lambda pane_id, *, command: None)
    monkeypatch.setattr(tmux, "split_window", lambda *a, **k: "%6")
    monkeypatch.setattr(tmux, "set_pane_option", lambda *args: None)
    monkeypatch.setattr(tmux, "select_window", lambda t: None)
    (tmp_state_dir / "layout").write_text("split")
    new.main(["api"])
    assert any("new-session" in c for c in called), f"no new-session call in {called}"


def test_new_writes_window_mapping(agent_new_env, tmp_config_dir, tmp_path):
    _, repo2 = _write_config(tmp_config_dir, tmp_path)
    new.main(["scripts"])
    m = windows_mod.read_mapping("@5")
    assert m.project == "scripts"
    assert m.branch is None
    assert m.pane_id == "23"
    assert m.host_worktree == repo2
    assert m.phase_hint == phase.STARTING


def test_new_provisions_settings_local_json(monkeypatch, tmp_config_dir, tmp_path):
    """_provision writes .claude/settings.local.json to the worktree."""
    import os as _os
    import json
    from tmux_agents import worktree as wt_mod
    from tmux_agents import startup as startup_mod
    from tmux_agents import windows as windows_mod

    _, repo2 = _write_config(tmp_config_dir, tmp_path)
    monkeypatch.setattr(_os, "fork", lambda: 0)
    monkeypatch.setattr(_os, "setsid", lambda: None)
    monkeypatch.setattr(startup_mod, "_detach_stdio", lambda: None)
    windows_mod.write_mapping(
        windows_mod.WindowMapping(
            window_id="@5",
            project="scripts",
            branch=None,
            host_worktree=repo2,
            pane_id="23",
            phase_hint="starting",
        )
    )
    monkeypatch.setattr(wt_mod, "resolve", lambda *a, **k: repo2)
    monkeypatch.setattr(startup_mod, "_respawn_with_retry", lambda *a: None)
    monkeypatch.setattr(startup_mod, "show_static_text", lambda *a: None)
    monkeypatch.setattr(startup_mod, "hold_pane_then_exec", lambda *a: None)
    monkeypatch.setattr(startup_mod, "_write_pane_state", lambda *a, **k: None)

    rc = new.main(
        ["--provision", "--window-id", "@5", "--pane-id", "23", "--project", "scripts"]
    )
    assert rc == 0
    target = repo2 / ".claude" / "settings.local.json"
    assert target.exists()
    got = json.loads(target.read_text())
    assert got["tui"] == "fullscreen"
    from importlib.metadata import version as _pkg_version

    assert got["_tmux_agents_version"] == _pkg_version("tmux-agents")


def test_new_provisioning_is_idempotent(monkeypatch, tmp_config_dir, tmp_path):
    """Running _provision twice on the same worktree must not rewrite the settings file."""
    import os as _os
    from tmux_agents import worktree as wt_mod
    from tmux_agents import startup as startup_mod
    from tmux_agents import windows as windows_mod

    _, repo2 = _write_config(tmp_config_dir, tmp_path)
    monkeypatch.setattr(_os, "fork", lambda: 0)
    monkeypatch.setattr(_os, "setsid", lambda: None)
    monkeypatch.setattr(startup_mod, "_detach_stdio", lambda: None)
    monkeypatch.setattr(wt_mod, "resolve", lambda *a, **k: repo2)
    monkeypatch.setattr(startup_mod, "_respawn_with_retry", lambda *a: None)
    monkeypatch.setattr(startup_mod, "show_static_text", lambda *a: None)
    monkeypatch.setattr(startup_mod, "hold_pane_then_exec", lambda *a: None)
    monkeypatch.setattr(startup_mod, "_write_pane_state", lambda *a, **k: None)

    windows_mod.write_mapping(
        windows_mod.WindowMapping(
            window_id="@5",
            project="scripts",
            branch=None,
            host_worktree=repo2,
            pane_id="23",
            phase_hint="starting",
        )
    )
    new.main(
        ["--provision", "--window-id", "@5", "--pane-id", "23", "--project", "scripts"]
    )
    first = (repo2 / ".claude" / "settings.local.json").read_text()
    # Re-seed the mapping for the second run (it gets cleared by _provision).
    windows_mod.write_mapping(
        windows_mod.WindowMapping(
            window_id="@5",
            project="scripts",
            branch=None,
            host_worktree=repo2,
            pane_id="23",
            phase_hint="starting",
        )
    )
    new.main(
        ["--provision", "--window-id", "@5", "--pane-id", "23", "--project", "scripts"]
    )
    second = (repo2 / ".claude" / "settings.local.json").read_text()
    assert first == second


def _stub_ssh_pump_healthy_path(
    monkeypatch, *, has_python3=True, auth_sock="/tmp/agent.sock"
):
    """Default ssh-pump bits: python3 present, auth sock set, no existing pumps."""
    monkeypatch.setattr(
        ssh_forward, "has_python3_in_container", lambda c, u: has_python3
    )
    monkeypatch.setattr(ssh_forward, "host_ssh_auth_sock", lambda: auth_sock)
    monkeypatch.setattr(ssh_forward, "pump_pids_for", lambda c: [])
    monkeypatch.setattr(ssh_forward, "is_pump_responsive", lambda c, u: False)
    monkeypatch.setattr(ssh_forward, "kill_stale_pumps", lambda c: 0)
    monkeypatch.setattr(ssh_forward, "wait_until_pump_ready", lambda c, u, **kw: True)


def test_new_spawns_ssh_pump_for_container_project(
    monkeypatch, tmp_config_dir, tmp_path
):
    """_provision calls maybe_spawn_pump with (container_name, user) for container projects."""
    _provision_env(monkeypatch, tmp_config_dir, tmp_path)
    pump_calls = []
    monkeypatch.setattr(
        ssh_forward,
        "maybe_spawn_pump",
        lambda c, u: pump_calls.append((c, u)) or ssh_forward.PumpResult("ready"),
    )
    rc = new.main(
        ["--provision", "--window-id", "@5", "--pane-id", "23", "--project", "api"]
    )
    assert rc == 0
    assert pump_calls == [("api-devcontainer", "vscode")]


def test_new_skips_pump_spawn_when_responsive_pump_already_running(
    monkeypatch,
    tmp_config_dir,
    tmp_path,
):
    """Idempotency: re-running _provision on a project with a healthy pump
    must not spawn a second pump (the bug that produced the zombie pile-up)."""
    _provision_env(monkeypatch, tmp_config_dir, tmp_path)
    spawned, killed = [], []
    _stub_ssh_pump_healthy_path(monkeypatch)
    monkeypatch.setattr(ssh_forward, "pump_pids_for", lambda c: [4242])
    monkeypatch.setattr(ssh_forward, "is_pump_responsive", lambda c, u: True)
    monkeypatch.setattr(
        ssh_forward, "kill_stale_pumps", lambda c: killed.append(c) or 0
    )
    monkeypatch.setattr(ssh_forward, "spawn_pump", lambda c, u: spawned.append((c, u)))
    # Override the _provision_env stub with the real maybe_spawn_pump so the
    # low-level stubs above take effect.
    monkeypatch.setattr(ssh_forward, "maybe_spawn_pump", ssh_forward.maybe_spawn_pump)
    rc = new.main(
        ["--provision", "--window-id", "@5", "--pane-id", "23", "--project", "api"]
    )
    assert rc == 0
    assert spawned == []
    assert killed == []


def test_new_kills_stale_pump_then_respawns_when_unresponsive(
    monkeypatch,
    tmp_config_dir,
    tmp_path,
):
    """The broken-but-listening case: pump pid is alive but ssh-add hangs.
    Must kill the stale pump first, then spawn a fresh one.
    Verified at the maybe_spawn_pump level: a PumpResult with killed_stale > 0
    is returned, confirming that stale pumps were detected and replaced."""
    _provision_env(monkeypatch, tmp_config_dir, tmp_path)
    results = []
    monkeypatch.setattr(
        ssh_forward,
        "maybe_spawn_pump",
        lambda c, u: (
            results.append(ssh_forward.PumpResult("ready", killed_stale=2))
            or ssh_forward.PumpResult("ready", killed_stale=2)
        ),
    )
    rc = new.main(
        ["--provision", "--window-id", "@5", "--pane-id", "23", "--project", "api"]
    )
    assert rc == 0
    assert results and results[0].killed_stale == 2


def test_new_skips_ssh_pump_when_forward_ssh_agent_false(
    monkeypatch, tmp_config_dir, tmp_path
):
    """forward_ssh_agent=false: maybe_spawn_pump is not called at all."""
    import os as _os
    from tmux_agents import container as container_mod, worktree as wt_mod
    from tmux_agents import provisioning as prov_mod, startup as startup_mod
    from tmux_agents import windows as windows_mod

    repo = tmp_path / "api"
    repo.mkdir()
    (tmp_config_dir / "projects.toml").write_text(
        f'[api]\nrepo = "{repo}"\ncontainer = "api-devcontainer"\n'
        f'container_workdir = "/work"\nup_cmd = "echo up"\n'
        f"forward_ssh_agent = false\n"
    )
    monkeypatch.setattr(_os, "fork", lambda: 0)
    monkeypatch.setattr(_os, "setsid", lambda: None)
    monkeypatch.setattr(startup_mod, "_detach_stdio", lambda: None)
    windows_mod.write_mapping(
        windows_mod.WindowMapping(
            window_id="@5",
            project="api",
            branch=None,
            host_worktree=repo,
            pane_id="23",
            phase_hint="starting",
        )
    )
    monkeypatch.setattr(container_mod, "current_name", lambda proj: None)
    monkeypatch.setattr(
        container_mod, "ensure_up", lambda proj, up_cmd: "api-devcontainer"
    )
    pump_calls = []
    monkeypatch.setattr(
        ssh_forward,
        "maybe_spawn_pump",
        lambda c, u: pump_calls.append((c, u)) or ssh_forward.PumpResult("ready"),
    )
    monkeypatch.setattr(wt_mod, "resolve", lambda *a, **k: repo)
    monkeypatch.setattr(prov_mod, "provision_settings", lambda *a, **k: True)
    monkeypatch.setattr(startup_mod, "_respawn_with_retry", lambda *a: None)
    monkeypatch.setattr(startup_mod, "show_static_text", lambda *a: None)
    monkeypatch.setattr(startup_mod, "hold_pane_then_exec", lambda *a: None)
    monkeypatch.setattr(startup_mod, "_write_pane_state", lambda *a, **k: None)
    rc = new.main(
        ["--provision", "--window-id", "@5", "--pane-id", "23", "--project", "api"]
    )
    assert rc == 0
    assert pump_calls == []


def test_new_skips_ssh_pump_for_host_only(monkeypatch, tmp_config_dir, tmp_path):
    """Host-only project: maybe_spawn_pump is not called (no container)."""
    import os as _os
    from tmux_agents import worktree as wt_mod, provisioning as prov_mod
    from tmux_agents import startup as startup_mod
    from tmux_agents import windows as windows_mod

    _, repo2 = _write_config(tmp_config_dir, tmp_path)
    monkeypatch.setattr(_os, "fork", lambda: 0)
    monkeypatch.setattr(_os, "setsid", lambda: None)
    monkeypatch.setattr(startup_mod, "_detach_stdio", lambda: None)
    windows_mod.write_mapping(
        windows_mod.WindowMapping(
            window_id="@5",
            project="scripts",
            branch=None,
            host_worktree=repo2,
            pane_id="23",
            phase_hint="starting",
        )
    )
    pump_calls = []
    monkeypatch.setattr(
        ssh_forward,
        "maybe_spawn_pump",
        lambda c, u: pump_calls.append((c, u)) or ssh_forward.PumpResult("ready"),
    )
    monkeypatch.setattr(wt_mod, "resolve", lambda *a, **k: repo2)
    monkeypatch.setattr(prov_mod, "provision_settings", lambda *a, **k: True)
    monkeypatch.setattr(startup_mod, "_respawn_with_retry", lambda *a: None)
    monkeypatch.setattr(startup_mod, "show_static_text", lambda *a: None)
    monkeypatch.setattr(startup_mod, "hold_pane_then_exec", lambda *a: None)
    monkeypatch.setattr(startup_mod, "_write_pane_state", lambda *a, **k: None)
    rc = new.main(
        ["--provision", "--window-id", "@5", "--pane-id", "23", "--project", "scripts"]
    )
    assert rc == 0
    assert pump_calls == []


def test_new_skips_ssh_pump_when_python3_missing_with_warning(
    monkeypatch,
    tmp_config_dir,
    tmp_path,
    tmux_agents_caplog,
):
    """python3 missing → PumpResult("disabled_no_python") → stage warn → hold pane."""
    import logging
    from tmux_agents.ssh_forward import PumpResult

    _provision_env(monkeypatch, tmp_config_dir, tmp_path, warn=False)
    # Override stub to emit a log warning (mimicking the real behavior).
    monkeypatch.setattr(
        ssh_forward,
        "maybe_spawn_pump",
        lambda c, u: (
            ssh_forward.logger.warning(
                "python3 not found in %s; SSH agent forwarding disabled", c
            )
            or PumpResult("disabled_no_python")
        ),
    )
    with tmux_agents_caplog.at_level(logging.WARNING, logger="tmux_agents.ssh_forward"):
        new.main(
            ["--provision", "--window-id", "@5", "--pane-id", "23", "--project", "api"]
        )
    assert any("python3 not found" in r.message for r in tmux_agents_caplog.records)


def test_new_skips_ssh_pump_when_host_auth_sock_unset_with_warning(
    monkeypatch,
    tmp_config_dir,
    tmp_path,
    tmux_agents_caplog,
):
    """No SSH_AUTH_SOCK → PumpResult("disabled_no_sock") → warning logged."""
    import logging
    from tmux_agents.ssh_forward import PumpResult

    _provision_env(monkeypatch, tmp_config_dir, tmp_path, warn=False)
    monkeypatch.setattr(
        ssh_forward,
        "maybe_spawn_pump",
        lambda c, u: (
            ssh_forward.logger.warning(
                "SSH_AUTH_SOCK not set on host; SSH agent forwarding disabled"
            )
            or PumpResult("disabled_no_sock")
        ),
    )
    with tmux_agents_caplog.at_level(logging.WARNING, logger="tmux_agents.ssh_forward"):
        new.main(
            ["--provision", "--window-id", "@5", "--pane-id", "23", "--project", "api"]
        )
    assert any("SSH_AUTH_SOCK not set" in r.message for r in tmux_agents_caplog.records)


def test_new_passes_empty_resume_args_to_substitute(
    monkeypatch, tmp_path, tmp_config_dir
):
    """_provision must substitute resume_args="" so {resume_args} resolves to nothing."""
    import os as _os
    from tmux_agents import worktree as wt_mod, provisioning as prov_mod
    from tmux_agents import startup as startup_mod
    from tmux_agents import windows as windows_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_config_dir / "projects.toml").write_text(
        "[scripts]\n"
        f'repo = "{repo}"\n'
        'exec_cmd = "cd {workdir} && claude{resume_args}"\n'
    )
    monkeypatch.setattr(_os, "fork", lambda: 0)
    monkeypatch.setattr(_os, "setsid", lambda: None)
    monkeypatch.setattr(startup_mod, "_detach_stdio", lambda: None)
    windows_mod.write_mapping(
        windows_mod.WindowMapping(
            window_id="@5",
            project="scripts",
            branch=None,
            host_worktree=repo,
            pane_id="23",
            phase_hint="starting",
        )
    )
    monkeypatch.setattr(wt_mod, "resolve", lambda *a, **k: repo)
    monkeypatch.setattr(prov_mod, "provision_settings", lambda *a, **k: True)
    captured = {}
    monkeypatch.setattr(
        startup_mod,
        "_respawn_with_retry",
        lambda pid, cmd: captured.__setitem__("cmd", cmd),
    )
    monkeypatch.setattr(startup_mod, "show_static_text", lambda *a: None)
    monkeypatch.setattr(startup_mod, "hold_pane_then_exec", lambda *a: None)
    monkeypatch.setattr(startup_mod, "_write_pane_state", lambda *a, **k: None)

    rc = new.main(
        ["--provision", "--window-id", "@5", "--pane-id", "23", "--project", "scripts"]
    )
    assert rc == 0
    assert "cmd" in captured
    assert captured["cmd"].endswith("claude")
    assert "--resume" not in captured["cmd"]


def test_new_spawns_ssh_pump_with_project_user(monkeypatch, tmp_config_dir, tmp_path):
    """project.user is passed as the `user` argument to maybe_spawn_pump."""
    import os as _os
    from tmux_agents import container as container_mod, worktree as wt_mod
    from tmux_agents import provisioning as prov_mod, startup as startup_mod
    from tmux_agents import windows as windows_mod

    repo = tmp_path / "api"
    repo.mkdir()
    (tmp_config_dir / "projects.toml").write_text(
        f'[api]\nrepo = "{repo}"\ncontainer = "api-devcontainer"\n'
        f'container_workdir = "/work"\nup_cmd = "echo up"\n'
        f'user = "node"\n'
    )
    monkeypatch.setattr(_os, "fork", lambda: 0)
    monkeypatch.setattr(_os, "setsid", lambda: None)
    monkeypatch.setattr(startup_mod, "_detach_stdio", lambda: None)
    windows_mod.write_mapping(
        windows_mod.WindowMapping(
            window_id="@5",
            project="api",
            branch=None,
            host_worktree=repo,
            pane_id="23",
            phase_hint="starting",
        )
    )
    monkeypatch.setattr(container_mod, "current_name", lambda proj: None)
    monkeypatch.setattr(
        container_mod, "ensure_up", lambda proj, up_cmd: "api-devcontainer"
    )
    pump_calls = []
    monkeypatch.setattr(
        ssh_forward,
        "maybe_spawn_pump",
        lambda c, u: pump_calls.append((c, u)) or ssh_forward.PumpResult("ready"),
    )
    monkeypatch.setattr(wt_mod, "resolve", lambda *a, **k: repo)
    monkeypatch.setattr(prov_mod, "provision_settings", lambda *a, **k: True)
    monkeypatch.setattr(startup_mod, "_respawn_with_retry", lambda *a: None)
    monkeypatch.setattr(startup_mod, "show_static_text", lambda *a: None)
    monkeypatch.setattr(startup_mod, "hold_pane_then_exec", lambda *a: None)
    monkeypatch.setattr(startup_mod, "_write_pane_state", lambda *a, **k: None)
    rc = new.main(
        ["--provision", "--window-id", "@5", "--pane-id", "23", "--project", "api"]
    )
    assert rc == 0
    assert pump_calls == [("api-devcontainer", "node")]


def test_interactive_branch_picker_sentinel_means_no_branch(
    agent_new_env,
    monkeypatch,
    tmp_config_dir,
    tmp_path,
):
    """Sentinel choice → no branch passed to worker; interactive window name has no colon."""
    _write_config(tmp_config_dir, tmp_path)
    monkeypatch.setattr(worktree, "list_existing", lambda r: ["feat/foo", "fix-bar"])
    monkeypatch.setattr(windows_mod, "live_branches_for", lambda p: set())
    monkeypatch.setattr(pickers, "pick_one", lambda items, *, prompt: "api")
    captured = {}

    def fake_pick_or_create(candidates, *, prompt, validator=None):
        captured["candidates"] = candidates
        return pickers.NO_BRANCH_SENTINEL

    monkeypatch.setattr(pickers, "pick_or_create", fake_pick_or_create)

    rc = new.main([])
    assert rc == 0
    assert captured["candidates"] == [
        pickers.NO_BRANCH_SENTINEL,
        "feat/foo",
        "fix-bar",
    ]
    # Window name has no colon (no branch).
    assert agent_new_env.made[0][0] == "api"
    # Worker argv has no --branch.
    argv = agent_new_env.spawned[0]
    assert "--branch" not in argv


def test_interactive_branch_picker_open_marker_stripped(
    agent_new_env,
    monkeypatch,
    tmp_config_dir,
    tmp_path,
):
    repo, _ = _write_config(tmp_config_dir, tmp_path)
    # 'feat/foo' is open; 'fix-bar' is not.
    monkeypatch.setattr(worktree, "list_existing", lambda r: ["feat/foo", "fix-bar"])
    monkeypatch.setattr(windows_mod, "live_branches_for", lambda p: {"feat/foo"})
    # Pretend the worktree dir already exists so worktree.resolve doesn't shell out.
    (repo / ".worktrees" / "feat" / "foo").mkdir(parents=True)
    monkeypatch.setattr(pickers, "pick_one", lambda items, *, prompt: "api")
    captured = {}

    def fake_pick_or_create(candidates, *, prompt, validator=None):
        captured["candidates"] = candidates
        return "feat/foo  (open)"

    monkeypatch.setattr(pickers, "pick_or_create", fake_pick_or_create)

    rc = new.main([])
    assert rc == 0
    assert captured["candidates"] == [
        pickers.NO_BRANCH_SENTINEL,
        "feat/foo  (open)",
        "fix-bar",
    ]
    assert agent_new_env.made[0][0] == "api:feat/foo"


def test_interactive_branch_picker_typed_new_branch(
    agent_new_env,
    monkeypatch,
    tmp_config_dir,
    tmp_path,
):
    _write_config(tmp_config_dir, tmp_path)
    monkeypatch.setattr(worktree, "list_existing", lambda r: [])
    monkeypatch.setattr(windows_mod, "live_branches_for", lambda p: set())
    monkeypatch.setattr(worktree, "resolve", lambda *a, **k: tmp_path / "wt")
    monkeypatch.setattr(pickers, "pick_one", lambda items, *, prompt: "api")
    monkeypatch.setattr(
        pickers,
        "pick_or_create",
        lambda candidates, *, prompt, validator=None: "brand-new",
    )

    rc = new.main([])
    assert rc == 0
    assert agent_new_env.made[0][0] == "api:brand-new"


def test_new_inserts_after_last_project_sibling(
    agent_new_env,
    monkeypatch,
    tmp_config_dir,
    tmp_path,
):
    """When a live window already belongs to the same project,
    `agent-new` passes its window_id as `after_target` so tmux inserts
    the new window adjacent to siblings (keeps numbering grouped)."""
    _write_config(tmp_config_dir, tmp_path)
    windows_mod.write_mapping(
        windows_mod.WindowMapping(
            window_id="@4",
            project="api",
            branch=None,
            host_worktree=tmp_path,
            pane_id="23",
        )
    )
    windows_mod.write_mapping(
        windows_mod.WindowMapping(
            window_id="@6",
            project="scripts",
            branch=None,
            host_worktree=tmp_path,
            pane_id="24",
        )
    )
    monkeypatch.setattr(
        tmux,
        "list_windows",
        lambda s: [
            tmux.Window(id="@0", index=0, name="ctrl"),
            tmux.Window(id="@4", index=1, name="api"),
            tmux.Window(id="@6", index=2, name="scripts"),
        ],
    )
    captured = {}

    def fake_new(session, *, name, command, after_target=None):
        captured["after_target"] = after_target
        return "@9"

    monkeypatch.setattr(tmux, "new_window", fake_new)

    rc = new.main(["api"])
    assert rc == 0
    assert captured["after_target"] == "@4"


def test_new_interactive_creates_placeholder_and_spawns_worker(
    agent_new_env, tmp_config_dir, tmp_path
):
    from tmux_agents import startup
    from tmux_agents import windows as windows_mod

    _write_config(tmp_config_dir, tmp_path)
    rc = new.main(["api"])
    assert rc == 0
    # new_window receives the sleep bootstrap, NOT the placeholder.
    name, command = agent_new_env.made[0]
    assert name == "api"
    assert "sleep" in command
    # placeholder is passed via respawn_pane, not new_window.
    assert len(agent_new_env.respawned) == 1
    assert agent_new_env.respawned[0][1] == startup.placeholder_command("@5")
    # No container work in the interactive part.
    assert agent_new_env.ensured == []
    # Mapping written with phase_hint=starting.
    m = windows_mod.read_mapping("@5")
    assert m is not None and m.phase_hint == "starting"
    assert m.pane_id == "23"
    # Exactly one detached worker spawned, with the right argv.
    assert len(agent_new_env.spawned) == 1
    argv = agent_new_env.spawned[0]
    assert argv[:2] == ["agent-new", "--provision"]
    assert "--window-id" in argv and argv[argv.index("--window-id") + 1] == "@5"
    assert "--project" in argv and argv[argv.index("--project") + 1] == "api"
    assert "--pane-id" in argv and argv[argv.index("--pane-id") + 1] == "23"
    assert "--branch" not in argv


def test_new_interactive_passes_branch_to_worker(
    agent_new_env, monkeypatch, tmp_config_dir, tmp_path
):
    _write_config(tmp_config_dir, tmp_path)
    new.main(["api", "feat-x"])
    argv = agent_new_env.spawned[0]
    assert argv[argv.index("--project") + 1] == "api"
    assert argv[argv.index("--branch") + 1] == "feat-x"
    # Window name still reflects the branch.
    assert agent_new_env.made[0][0] == "api:feat-x"


def test_new_no_siblings_passes_after_target_none(
    agent_new_env,
    monkeypatch,
    tmp_config_dir,
    tmp_path,
):
    """No live window for this project → `after_target=None`, so
    `tmux.new_window` falls back to appending at the end of the session."""
    _write_config(tmp_config_dir, tmp_path)
    monkeypatch.setattr(
        tmux,
        "list_windows",
        lambda s: [
            tmux.Window(id="@0", index=0, name="ctrl"),
        ],
    )
    captured = {}

    def fake_new(session, *, name, command, after_target=None):
        captured["after_target"] = after_target
        return "@9"

    monkeypatch.setattr(tmux, "new_window", fake_new)

    rc = new.main(["api"])
    assert rc == 0
    assert captured["after_target"] is None


def test_provision_unexpected_exception_lands_on_X(
    monkeypatch, tmp_config_dir, tmp_path
):
    """An unexpected exception in the detached worker must not propagate silently;
    instead rc == 4, an error pane is shown, and the mapping phase_hint is 'errored'."""
    cap = _provision_env(monkeypatch, tmp_config_dir, tmp_path)
    # Seed mapping with a branch so worktree.resolve is called and can raise.
    from tmux_agents import windows as windows_mod

    repo = tmp_path / "api"
    windows_mod.write_mapping(
        windows_mod.WindowMapping(
            window_id="@5",
            project="api",
            branch="feat-x",
            host_worktree=repo,
            pane_id="23",
            phase_hint="starting",
        )
    )
    # Make worktree.resolve raise a generic RuntimeError (not WorktreeError).
    from tmux_agents import worktree as worktree_mod

    monkeypatch.setattr(
        worktree_mod,
        "resolve",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    rc = new.main(
        [
            "--provision",
            "--window-id",
            "@5",
            "--pane-id",
            "23",
            "--project",
            "api",
            "--branch",
            "feat-x",
        ]
    )
    assert rc == 4
    assert len(cap.static_texts) == 1
    _, text = cap.static_texts[0]
    assert "unexpected error" in text
    assert windows_mod.read_mapping("@5").phase_hint == "errored"


def test_new_interactive_clears_stale_pane_state(
    agent_new_env, tmp_config_dir, tmp_path
):
    """Interactive spawn must unlink stale per-pane state/session files so a
    recycled pane id doesn't show a stale letter during the S → starting window."""
    from tmux_agents import paths as paths_mod

    repo, _ = _write_config(tmp_config_dir, tmp_path)
    # Pre-create a stale state file for pane 23 (active_pane_id returns "%23").
    stale = paths_mod.worktree_state_file(repo, "23")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"phase":"running"}')
    rc = new.main(["api"])
    assert rc == 0
    assert not stale.exists(), (
        "stale state file should have been unlinked before mapping write"
    )
