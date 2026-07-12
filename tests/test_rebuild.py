from pathlib import Path
from types import SimpleNamespace

import pytest

from tmux_agents import phase, pickers, ssh_forward, startup, tmux, container
from tmux_agents import windows as windows_mod
from tmux_agents.commands import rebuild
from tmux_agents.config import Project


def _proj(
    name="webapp",
    *,
    devcontainer=False,
    container=None,
    up_cmd=None,
    up_cmd_explicit=None,
    user=None,
    forward_ssh_agent=True,
    repo="/Users/me/dev/webapp",
):
    # Mirror config.load: an up_cmd passed here is treated as explicit unless
    # the caller says otherwise (matches "user configured it in projects.toml").
    if up_cmd_explicit is None:
        up_cmd_explicit = up_cmd is not None
    return Project(
        name=name,
        repo=Path(repo),
        exec_cmd="claude{resume_args}",
        container=container,
        devcontainer=devcontainer,
        up_cmd=up_cmd,
        up_cmd_explicit=up_cmd_explicit,
        user=user,
        forward_ssh_agent=forward_ssh_agent,
    )


def _affected(
    project="webapp",
    branch="feat-x",
    pane_id="23",
    letter="I",
    session_id=None,
    host_worktree="/wt",
):
    m = windows_mod.WindowMapping(
        window_id="@7",
        project=project,
        branch=branch,
        host_worktree=Path(host_worktree),
        pane_id=pane_id,
        claude_session_id=session_id,
    )
    return rebuild.Affected(
        mapping=m, window_name=f"{project}:{branch}", state_letter=letter
    )


# ---- eligibility ----


@pytest.mark.parametrize(
    "proj,expected",
    [
        (_proj(devcontainer=True), True),
        (_proj(container="named", up_cmd="echo up"), True),  # explicit recipe
        (_proj(container="named", up_cmd=None), False),
        # A named container that only inherited the auto-default up_cmd is NOT
        # rebuildable (pre-existing-container case).
        (
            _proj(container="named", up_cmd="devcontainer up", up_cmd_explicit=False),
            False,
        ),
        (_proj(), False),  # host-only
    ],
)
def test_eligible(proj, expected):
    assert rebuild._eligible(proj) is expected


def test_eligible_via_config_load_excludes_default_up_cmd(tmp_config_dir):
    """End-to-end through config.load: a pre-existing named container with no
    up_cmd in projects.toml inherits the default but stays ineligible."""
    from tmux_agents import config, paths

    (tmp_config_dir / "projects.toml").write_text(
        '[named]\nrepo = "/r/named"\ncontainer = "named-workspace"\n'
        '[dc]\nrepo = "/r/dc"\ndevcontainer = true\n'
    )
    projects = config.load(paths.projects_toml())
    assert rebuild._eligible(projects["named"]) is False
    assert rebuild._eligible(projects["dc"]) is True


# ---- affected-window gathering ----


def test_gather_affected_groups_and_skips(monkeypatch, tmp_config_dir):
    for wid, project, branch in [
        ("@1", "webapp", "a"),
        ("@2", "webapp", "b"),
        ("@3", "api", "c"),
    ]:
        windows_mod.write_mapping(
            windows_mod.WindowMapping(
                window_id=wid,
                project=project,
                branch=branch,
                host_worktree=Path("/wt"),
                pane_id="9",
            )
        )
    wins = [
        tmux.Window(id="@0", index=0, name=tmux.CONTROL_WINDOW),  # skipped: ctrl
        tmux.Window(id="@1", index=1, name="webapp:a", state_code="R"),
        tmux.Window(id="@2", index=2, name="webapp:b", state_code="B2"),
        tmux.Window(id="@3", index=3, name="api:c", state_code=""),
        tmux.Window(id="@9", index=9, name="ghost:x"),  # skipped: no mapping
    ]
    by = rebuild._gather_affected(wins)
    assert set(by) == {"webapp", "api"}
    assert {a.state_letter for a in by["webapp"]} == {"R", "B"}
    assert by["api"][0].state_letter == "I"  # empty state_code → idle letter


def test_picker_line_tally_and_empty():
    assert rebuild._picker_line("api", []).endswith("no agents")
    line = rebuild._picker_line(
        "webapp", [_affected(letter="R"), _affected(letter="I")]
    )
    assert "2 agents" in line and "1R 1I" in line


# ---- confirmation default is tiered on busy state ----


@pytest.mark.parametrize(
    "letters,expected_default",
    [
        (["I", "Z"], True),  # all safe → default yes
        ([], True),  # none → default yes
        (["I", "R"], False),  # a running agent → default no
        (["W"], False),
        (["B"], False),
    ],
)
def test_confirm_default_tier(monkeypatch, letters, expected_default):
    seen = {}
    monkeypatch.setattr(
        pickers,
        "prompt_yes_no",
        lambda prompt, *, default: seen.setdefault("default", default) or True,
    )
    affected = [_affected(letter=letter) for letter in letters]
    rebuild._confirm("webapp", affected, assume_yes=False)
    assert seen["default"] is expected_default


def test_confirm_assume_yes_skips_prompt(monkeypatch):
    monkeypatch.setattr(
        pickers,
        "prompt_yes_no",
        lambda *a, **k: pytest.fail("prompt should be skipped"),
    )
    assert rebuild._confirm("webapp", [_affected(letter="R")], assume_yes=True) is True


# ---- worker: rebuild + respawn ----


def _stub_worker_io(monkeypatch):
    respawns: list[tuple[str, str]] = []
    states: list[tuple[str, str]] = []
    monkeypatch.setattr(
        startup, "_respawn_with_retry", lambda pane, cmd: respawns.append((pane, cmd))
    )
    monkeypatch.setattr(
        startup,
        "_write_pane_state",
        lambda wt, pid, *, phase_value: states.append((pid, phase_value)),
    )
    monkeypatch.setattr(
        ssh_forward, "maybe_spawn_pump", lambda c, u: ssh_forward.PumpResult("ready")
    )
    return SimpleNamespace(respawns=respawns, states=states)


def test_worker_rebuilds_then_resumes_each_pane(monkeypatch, tmp_state_dir):
    io = _stub_worker_io(monkeypatch)
    rebuilt = []
    monkeypatch.setattr(
        container,
        "rebuild",
        lambda proj, *, up_cmd, no_cache: rebuilt.append((up_cmd, no_cache)) or "cid",
    )
    proj = _proj(
        devcontainer=True, up_cmd="cd /r && devcontainer up --workspace-folder ."
    )
    affected = [
        _affected(pane_id="23", session_id="sess-1"),
        _affected(pane_id="24", session_id=None),
    ]

    rc = rebuild._run_worker(proj, affected, no_cache=True)

    assert rc == 0
    assert rebuilt == [("cd /r && devcontainer up --workspace-folder .", True)]
    # Final respawns target the %-prefixed pane ids and inject --resume when present.
    final = io.respawns[-2:]
    assert ("%23", "claude --resume sess-1") in final
    assert ("%24", "claude") in final
    # Every pane ends in STARTING.
    assert io.states[-2:] == [("23", phase.STARTING), ("24", phase.STARTING)]


def test_worker_container_failure_marks_panes_errored(monkeypatch, tmp_state_dir):
    _stub_worker_io(monkeypatch)
    monkeypatch.setattr(
        container,
        "rebuild",
        lambda *a, **k: (_ for _ in ()).throw(container.ContainerError("docker down")),
    )
    failed = []
    monkeypatch.setattr(
        startup, "show_static_text", lambda pane, body: failed.append(pane)
    )
    proj = _proj(devcontainer=True, up_cmd="devcontainer up")
    affected = [_affected(pane_id="23"), _affected(pane_id="24")]

    rc = rebuild._run_worker(proj, affected, no_cache=False)

    assert rc == 1
    assert failed == ["%23", "%24"]


def test_worker_isolates_a_failing_respawn(monkeypatch, tmp_state_dir):
    _stub_worker_io(monkeypatch)
    monkeypatch.setattr(container, "rebuild", lambda *a, **k: "cid")

    def flaky(pane, cmd):
        # Placeholder respawns (first pass) succeed; the second final respawn blows up.
        if cmd.startswith("claude") and pane == "%24":
            raise RuntimeError("pane gone")

    monkeypatch.setattr(startup, "_respawn_with_retry", flaky)
    proj = _proj(devcontainer=True, up_cmd="devcontainer up")
    affected = [_affected(pane_id="23", session_id="s"), _affected(pane_id="24")]

    assert rebuild._run_worker(proj, affected, no_cache=False) == 1  # one failure


# ---- CLI dispatch ----


def _write_projects(tmp_config_dir, body):
    (tmp_config_dir / "projects.toml").write_text(body)


def test_main_interactive_fires_worker(monkeypatch, tmp_config_dir, tmp_state_dir):
    _write_projects(
        tmp_config_dir, '[webapp]\nrepo = "/r/webapp"\ndevcontainer = true\n'
    )
    monkeypatch.setattr(tmux, "list_windows", lambda s: [])
    spawned = []
    monkeypatch.setattr(tmux, "run_shell_bg", lambda command: spawned.append(command))
    rc = rebuild.main(["webapp", "--yes", "--no-cache"])
    assert rc == 0
    assert spawned == ["agent-rebuild --worker --project webapp --no-cache"]


def test_main_rejects_ineligible_project(
    monkeypatch, tmp_config_dir, tmp_state_dir, capsys
):
    _write_projects(
        tmp_config_dir,
        '[webapp]\nrepo = "/r/webapp"\ndevcontainer = true\n[host]\nrepo = "/r/host"\n',
    )
    monkeypatch.setattr(tmux, "list_windows", lambda s: [])
    monkeypatch.setattr(
        tmux, "run_shell_bg", lambda command: pytest.fail("must not fire worker")
    )
    rc = rebuild.main(["host", "--yes"])
    assert rc == 2
    assert "cannot be rebuilt" in capsys.readouterr().err


def test_main_worker_branch_invokes_run_worker(
    monkeypatch, tmp_config_dir, tmp_state_dir
):
    _write_projects(
        tmp_config_dir, '[webapp]\nrepo = "/r/webapp"\ndevcontainer = true\n'
    )
    monkeypatch.setattr(tmux, "list_windows", lambda s: [])
    seen = {}
    monkeypatch.setattr(
        rebuild,
        "_run_worker",
        lambda proj, affected, *, no_cache: (
            seen.update(proj=proj.name, n=len(affected), no_cache=no_cache) or 0
        ),
    )
    rc = rebuild.main(["--worker", "--project", "webapp", "--no-cache"])
    assert rc == 0
    assert seen == {"proj": "webapp", "n": 0, "no_cache": True}
