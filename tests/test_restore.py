"""Restore worker tests. The worker is split into a pure planner
(`plan_entries` / `group_entries_by_project`) and an execution function
(`execute_plan`); these tests cover the planner."""

import dataclasses
import json
import logging
from pathlib import Path

import pytest

from tmux_agents import codex_hooks, paths, startup, windows


@pytest.fixture(autouse=True)
def _stub_codex_hooks(monkeypatch):
    """Default-stub codex_hooks so existing restore tests (which don't
    exercise codex-hook provisioning) don't perform real filesystem I/O,
    even against the conftest-isolated TMUX_AGENTS_CODEX_HOME. Tests that
    specifically exercise the ensure-provisioned call override this via
    their own `monkeypatch.setattr` later in the test body — that call
    wins because it runs after this fixture's setup."""
    monkeypatch.setattr(codex_hooks, "ensure_host", lambda: True)
    monkeypatch.setattr(codex_hooks, "ensure_container", lambda name, user: True)


def _load_test_projects(projects_file):
    from tmux_agents import config

    return config.load(projects_file)


def _write_snapshot(
    window_id: str,
    *,
    project: str,
    branch: str | None,
    host_worktree: Path,
    pane_id: str = "23",
    session_id: str | None = None,
    window_index: int | None = None,
) -> None:
    paths.windows_previous_dir().mkdir(parents=True, exist_ok=True)
    f = paths.windows_previous_dir() / f"{window_id}.json"
    payload = {
        "project": project,
        "branch": branch,
        "host_worktree": str(host_worktree),
        "pane_id": pane_id,
    }
    if session_id is not None:
        payload["claude_session_id"] = session_id
    if window_index is not None:
        payload["window_index"] = window_index
    f.write_text(json.dumps(payload))


@pytest.fixture
def projects_file(tmp_path, tmp_config_dir):
    p = tmp_config_dir / "projects.toml"
    p.write_text(
        "[scripts]\n"
        f'repo = "{tmp_path}/scripts"\n'
        'exec_cmd = "cd {workdir} && claude{resume_args}"\n'
        "\n"
        "[api]\n"
        f'repo = "{tmp_path}/api"\n'
        "devcontainer = true\n"
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "api").mkdir()
    return p


def test_plan_returns_entries_sorted_by_window_index(
    tmp_config_dir, tmp_path, projects_file
):
    from tmux_agents.commands import restore

    wt_a = tmp_path / "scripts"
    wt_b = tmp_path / "api"
    _write_snapshot(
        "@5", project="scripts", branch=None, host_worktree=wt_a, window_index=2
    )
    _write_snapshot("@3", project="api", branch="x", host_worktree=wt_b, window_index=1)
    plan = restore.plan_entries(
        live_panes={}, projects=_load_test_projects(projects_file)
    )
    assert [e.window_id for e in plan] == ["@3", "@5"]


def test_plan_drops_entries_for_unknown_project(
    tmp_config_dir, tmp_path, projects_file
):
    from tmux_agents.commands import restore

    wt = tmp_path / "ghost"
    _write_snapshot(
        "@1", project="ghost", branch=None, host_worktree=wt, window_index=1
    )
    plan = restore.plan_entries(
        live_panes={}, projects=_load_test_projects(projects_file)
    )
    assert plan == []


def test_plan_drops_entries_with_missing_worktree(
    tmp_config_dir, tmp_path, projects_file
):
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts" / ".worktrees" / "deleted"  # never created
    _write_snapshot(
        "@1", project="scripts", branch="deleted", host_worktree=wt, window_index=1
    )
    plan = restore.plan_entries(
        live_panes={}, projects=_load_test_projects(projects_file)
    )
    assert plan == []


def test_plan_revives_entry_when_window_alive_but_pane_gone(
    tmp_config_dir,
    tmp_path,
    projects_file,
):
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts"
    _write_snapshot(
        "@1",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id="42",
        window_index=1,
        session_id="sid-1",
    )
    plan = restore.plan_entries(
        live_panes={"@1": {"%99"}},
        projects=_load_test_projects(projects_file),
    )
    assert len(plan) == 1
    assert plan[0].kind == "revive"


def test_plan_skips_entry_when_pane_still_alive(
    tmp_config_dir,
    tmp_path,
    projects_file,
):
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts"
    _write_snapshot(
        "@1",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id="42",
        window_index=1,
    )
    plan = restore.plan_entries(
        live_panes={"@1": {"%42"}},
        projects=_load_test_projects(projects_file),
    )
    assert plan == []


def test_plan_keeps_entry_without_session_id(tmp_config_dir, tmp_path, projects_file):
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts"
    _write_snapshot(
        "@1",
        project="scripts",
        branch=None,
        host_worktree=wt,
        session_id=None,
        window_index=1,
    )
    plan = restore.plan_entries(
        live_panes={}, projects=_load_test_projects(projects_file)
    )
    assert len(plan) == 1
    assert plan[0].claude_session_id is None


def test_group_by_project_collapses_branches(tmp_config_dir, tmp_path, projects_file):
    from tmux_agents.commands import restore

    wt_main = tmp_path / "api"
    wt_branch = tmp_path / "api" / ".worktrees" / "feat"
    wt_branch.mkdir(parents=True)
    _write_snapshot(
        "@1", project="api", branch=None, host_worktree=wt_main, window_index=1
    )
    _write_snapshot(
        "@2", project="api", branch="feat", host_worktree=wt_branch, window_index=2
    )
    plan = restore.plan_entries(
        live_panes={}, projects=_load_test_projects(projects_file)
    )
    groups = restore.group_entries_by_project(plan)
    assert list(groups.keys()) == ["api"]
    assert {e.window_id for e in groups["api"]} == {"@1", "@2"}


def test_plan_falls_back_to_windows_dir_when_previous_missing(
    tmp_config_dir, tmp_path, projects_file
):
    """Manual `agent-restore` invocation with no snapshot move: read from
    the live windows/ dir."""
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts"
    paths.windows_dir().mkdir(parents=True, exist_ok=True)
    paths.window_mapping_file("@1").write_text(
        json.dumps(
            {
                "project": "scripts",
                "branch": None,
                "host_worktree": str(wt),
                "pane_id": "23",
                "window_index": 1,
            }
        )
    )
    plan = restore.plan_entries(
        live_panes={}, projects=_load_test_projects(projects_file)
    )
    assert len(plan) == 1
    assert plan[0].window_id == "@1"


def test_pre_create_writes_window_mapping_with_session_id(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    from tmux_agents import tmux, overview
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts"
    _write_snapshot(
        "@1",
        project="scripts",
        branch=None,
        host_worktree=wt,
        session_id="01234567-89ab-cdef-0123-456789abcdef",
        window_index=1,
    )

    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    new_window_calls = []

    def fake_new_window(session, *, name, command):
        new_window_calls.append((session, name, command))
        return f"@new-{len(new_window_calls)}"

    monkeypatch.setattr(tmux, "new_window", fake_new_window)
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%99")
    monkeypatch.setattr(tmux, "respawn_pane", lambda pane, *, command: None)
    monkeypatch.setattr(overview, "attach_overview_pane", lambda wid: None)

    # Skip provisioning: it touches files we don't care about here.
    from tmux_agents import provisioning

    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **kw: True)

    plan = restore.plan_entries(
        live_panes={}, projects=_load_test_projects(projects_file)
    )
    placeholders = restore.pre_create_windows(plan, live_panes={})
    assert len(new_window_calls) == 1
    assert new_window_calls[0][1] == "scripts"  # window name
    assert "sleep 3600" in new_window_calls[0][2]  # holding command body
    # Mapping was written under the new tmux-assigned window_id with session_id preserved.
    assert "@new-1" in placeholders
    p = placeholders["@new-1"]
    assert p.entry.claude_session_id == "01234567-89ab-cdef-0123-456789abcdef"
    assert p.pane_id == "%99"
    m = windows.read_mapping("@new-1")
    assert m.claude_session_id == "01234567-89ab-cdef-0123-456789abcdef"


def test_pre_create_fresh_scrubs_stale_files_for_assigned_pane(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    """A recycled pane id (`%99`) may still carry stale state/session/pending
    files from a previous, unrelated agent. `_pre_create_fresh` must scrub
    them for the id tmux is about to assign, before any state write or
    launch — otherwise a stale `session-99.id` would mispin a later codex
    resume onto the wrong session."""
    from tmux_agents import tmux, overview
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts"
    wt.mkdir(exist_ok=True)
    _write_snapshot(
        "@1", project="scripts", branch=None, host_worktree=wt, window_index=1
    )

    # Stale debris for the pane id tmux will hand back ("99").
    pane_dir = wt / ".local" / ".tmux-agents"
    pane_dir.mkdir(parents=True, exist_ok=True)
    (pane_dir / "state-99.json").write_text('{"phase": "running"}')
    (pane_dir / "session-99.id").write_text("ffffffff-ffff-ffff-ffff-ffffffffffff")
    (pane_dir / "pending-99").mkdir()
    (pane_dir / "pending-99" / "subagent__stale").write_text("")

    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    monkeypatch.setattr(tmux, "new_window", lambda s, *, name, command: "@new-1")
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%99")
    monkeypatch.setattr(tmux, "respawn_pane", lambda pane, *, command: None)
    monkeypatch.setattr(overview, "attach_overview_pane", lambda wid: None)
    from tmux_agents import provisioning

    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **kw: True)

    plan = restore.plan_entries(
        live_panes={}, projects=_load_test_projects(projects_file)
    )
    restore.pre_create_windows(plan, live_panes={})

    # The stale session-pin file is gone — this is what matters most: a
    # surviving pin would mispoint a later codex `resume` at a dead session.
    assert not (pane_dir / "session-99.id").exists()
    assert not (pane_dir / "pending-99").exists()
    # A fresh "starting" state file was written afterwards (not the stale one).
    s = json.loads((pane_dir / "state-99.json").read_text())
    assert s["phase"] == "starting"


def test_pre_create_revive_scrubs_stale_files_for_assigned_pane(
    monkeypatch,
    tmp_config_dir,
    tmp_path,
    projects_file,
):
    """Same aliasing guard as the fresh case, for the revive path: the split
    lands on a pane id tmux may have recycled from an unrelated dead agent."""
    from tmux_agents.commands import restore
    from tmux_agents import tmux

    wt = tmp_path / "scripts"
    wt.mkdir(exist_ok=True)

    # Stale debris for the pane id split_window will hand back ("99") — NOT
    # the same as the entry's own old pane ("42"), which `_clean_old_pane_files`
    # already covers.
    pane_dir = wt / ".local" / ".tmux-agents"
    pane_dir.mkdir(parents=True, exist_ok=True)
    (pane_dir / "state-99.json").write_text('{"phase": "running"}')
    (pane_dir / "session-99.id").write_text("ffffffff-ffff-ffff-ffff-ffffffffffff")
    (pane_dir / "pending-99").mkdir()
    (pane_dir / "pending-99" / "subagent__stale").write_text("")

    e = restore.Entry(
        window_id="@7",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id="42",
        claude_session_id=None,
        window_index=1,
        kind="revive",
    )

    monkeypatch.setattr(tmux, "overview_pane_ids", lambda wid: ["%50"])
    monkeypatch.setattr(
        tmux,
        "split_window",
        lambda target, *, percent, command, before=False, horizontal=False: "%99",
    )
    monkeypatch.setattr(tmux, "respawn_pane", lambda *a, **kw: None)
    monkeypatch.setattr(tmux, "set_window_option", lambda *a, **kw: None)
    monkeypatch.setattr(tmux, "select_pane", lambda p: None)

    placeholders = restore.pre_create_windows([e], live_panes={"@7": {"%50"}})

    assert "@7" in placeholders
    assert not (pane_dir / "session-99.id").exists()
    assert not (pane_dir / "pending-99").exists()
    s = json.loads((pane_dir / "state-99.json").read_text())
    assert s["phase"] == "starting"


def test_pre_create_writes_starting_phase_state(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    from tmux_agents import tmux, overview
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts"
    _write_snapshot(
        "@1", project="scripts", branch=None, host_worktree=wt, window_index=1
    )

    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    monkeypatch.setattr(tmux, "new_window", lambda s, *, name, command: "@new-1")
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%99")
    monkeypatch.setattr(tmux, "respawn_pane", lambda pane, *, command: None)
    monkeypatch.setattr(overview, "attach_overview_pane", lambda wid: None)
    from tmux_agents import provisioning

    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **kw: True)

    plan = restore.plan_entries(
        live_panes={}, projects=_load_test_projects(projects_file)
    )
    restore.pre_create_windows(plan, live_panes={})

    state_file = paths.worktree_state_file(wt, "99")
    s = json.loads(state_file.read_text())
    assert s["phase"] == "starting"


def test_pre_create_window_name_includes_branch(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    from tmux_agents import tmux, overview
    from tmux_agents.commands import restore

    wt = tmp_path / "api" / ".worktrees" / "feat"
    wt.mkdir(parents=True)
    _write_snapshot(
        "@1", project="api", branch="feat", host_worktree=wt, window_index=1
    )

    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    captured = {}

    def fake_new_window(session, *, name, command):
        captured["name"] = name
        return "@new-1"

    monkeypatch.setattr(tmux, "new_window", fake_new_window)
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%99")
    monkeypatch.setattr(tmux, "respawn_pane", lambda pane, *, command: None)
    monkeypatch.setattr(overview, "attach_overview_pane", lambda wid: None)
    from tmux_agents import provisioning

    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **kw: True)

    plan = restore.plan_entries(
        live_panes={}, projects=_load_test_projects(projects_file)
    )
    restore.pre_create_windows(plan, live_panes={})
    assert captured["name"] == "api:feat"


def test_execute_calls_up_cmds_in_parallel_then_respawns(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    """Two projects, three entries: api has two branches, scripts has one.
    api's up_cmd runs once; respawn-pane fires for all three."""
    from tmux_agents import tmux, container
    from tmux_agents.commands import restore

    # Build snapshot.
    wt_main = tmp_path / "api"
    wt_branch = tmp_path / "api" / ".worktrees" / "feat"
    wt_branch.mkdir(parents=True)
    wt_scripts = tmp_path / "scripts"
    _write_snapshot(
        "@1",
        project="api",
        branch=None,
        host_worktree=wt_main,
        window_index=1,
        session_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    _write_snapshot(
        "@2", project="api", branch="feat", host_worktree=wt_branch, window_index=2
    )
    _write_snapshot(
        "@3", project="scripts", branch=None, host_worktree=wt_scripts, window_index=3
    )

    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    monkeypatch.setattr(tmux, "new_window", lambda s, *, name, command: f"@new-{name}")
    monkeypatch.setattr(tmux, "set_window_option", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: f"%{wid[5:]}")
    from tmux_agents import provisioning

    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **kw: True)

    up_calls: list[str] = []

    def fake_ensure_up(proj, *, up_cmd):
        up_calls.append(proj.name)
        return f"{proj.name}-container"

    monkeypatch.setattr(container, "ensure_up", fake_ensure_up)

    respawn_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tmux,
        "respawn_pane",
        lambda pane_id, *, command: respawn_calls.append((pane_id, command)),
    )

    projs = _load_test_projects(projects_file)
    plan = restore.plan_entries(live_panes={}, projects=projs)
    placeholders = restore.pre_create_windows(plan, live_panes={})
    restore.execute_plan(plan, placeholders, projs)

    # api.up_cmd ran once even though two entries belong to it.
    assert up_calls.count("api") == 1
    # scripts has no container, so no up call.
    assert "scripts" not in up_calls
    # pre_create_windows adds one tail-F respawn per entry (3 entries);
    # execute_plan adds one final respawn per entry (3 entries) = 6 total.
    assert len(respawn_calls) == 6
    # Filter to the final claude respawns (not the tail-F placeholders).
    claude_respawns = [(pid, c) for pid, c in respawn_calls if "tail -F" not in c]
    assert len(claude_respawns) == 3
    # The api entry with a session id must have --resume in its command.
    api_first = next(c for pid, c in claude_respawns if "claude --resume" in c)
    assert "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" in api_first


def test_execute_spawns_ssh_pump_for_container_projects(
    monkeypatch,
    tmp_config_dir,
    tmp_state_dir,
    tmp_path,
    projects_file,
):
    """After a project's container is up, restore must spawn the SSH pump
    (once per project, like `agent-new` does) — otherwise restored panes
    inherit SSH_AUTH_SOCK pointing at an unpublished UDS and git-over-SSH
    fails until the user happens to run `agent-new` for the same project."""
    from tmux_agents import tmux, container, ssh_forward
    from tmux_agents.ssh_forward import PumpResult
    from tmux_agents.commands import restore

    wt_main = tmp_path / "api"
    wt_branch = tmp_path / "api" / ".worktrees" / "feat"
    wt_branch.mkdir(parents=True)
    wt_scripts = tmp_path / "scripts"
    _write_snapshot(
        "@1", project="api", branch=None, host_worktree=wt_main, window_index=1
    )
    _write_snapshot(
        "@2", project="api", branch="feat", host_worktree=wt_branch, window_index=2
    )
    _write_snapshot(
        "@3", project="scripts", branch=None, host_worktree=wt_scripts, window_index=3
    )

    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    monkeypatch.setattr(tmux, "new_window", lambda s, *, name, command: f"@new-{name}")
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: f"%{wid[5:]}")
    monkeypatch.setattr(tmux, "respawn_pane", lambda pane_id, *, command: None)
    from tmux_agents import provisioning

    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **kw: True)
    monkeypatch.setattr(container, "current_name", lambda proj: None)
    monkeypatch.setattr(
        container, "ensure_up", lambda proj, *, up_cmd: f"{proj.name}-container"
    )

    pump_calls: list[tuple[str, str]] = []

    def fake_pump(c, u):
        pump_calls.append((c, u))
        return PumpResult("ready")

    monkeypatch.setattr(ssh_forward, "maybe_spawn_pump", fake_pump)

    projs = _load_test_projects(projects_file)
    plan = restore.plan_entries(live_panes={}, projects=projs)
    placeholders = restore.pre_create_windows(plan, live_panes={})
    restore.execute_plan(plan, placeholders, projs)

    # One spawn for api (despite two entries); none for scripts (host-only).
    assert pump_calls == [("api-container", "vscode")]


def test_execute_skips_ssh_pump_when_forward_ssh_agent_false(
    monkeypatch,
    tmp_config_dir,
    tmp_state_dir,
    tmp_path,
):
    from tmux_agents import tmux, container, ssh_forward, config
    from tmux_agents.commands import restore

    repo = tmp_path / "api"
    repo.mkdir()
    (tmp_config_dir / "projects.toml").write_text(
        f'[api]\nrepo = "{repo}"\ncontainer = "api-devcontainer"\n'
        f'container_workdir = "/work"\nup_cmd = "echo up"\n'
        f"forward_ssh_agent = false\n"
    )
    _write_snapshot(
        "@1", project="api", branch=None, host_worktree=repo, window_index=1
    )

    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    monkeypatch.setattr(tmux, "new_window", lambda s, *, name, command: "@new-1")
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%99")
    monkeypatch.setattr(tmux, "respawn_pane", lambda pane_id, *, command: None)
    from tmux_agents import provisioning

    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **kw: True)
    monkeypatch.setattr(
        container, "ensure_up", lambda proj, *, up_cmd: "api-devcontainer"
    )

    pump_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ssh_forward, "maybe_spawn_pump", lambda c, u: pump_calls.append((c, u))
    )

    projs = config.load(tmp_config_dir / "projects.toml")
    plan = restore.plan_entries(live_panes={}, projects=projs)
    placeholders = restore.pre_create_windows(plan, live_panes={})
    restore.execute_plan(plan, placeholders, projs)

    assert pump_calls == []


def test_execute_failure_shows_error_in_pane_and_marks_state_errored(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file, caplog
):
    """When ensure_up fails, the placeholder pane is replaced with an error
    display and the per-pane state JSON is set to phase=errored (so the
    overview transitions S -> X)."""
    from tmux_agents import tmux, container, overview
    from tmux_agents.commands import restore

    wt = tmp_path / "api"
    _write_snapshot("@1", project="api", branch=None, host_worktree=wt, window_index=1)

    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    monkeypatch.setattr(tmux, "new_window", lambda s, *, name, command: "@new-1")
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%99")
    monkeypatch.setattr(overview, "attach_overview_pane", lambda wid: None)
    from tmux_agents import provisioning

    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **kw: True)

    def fake_ensure_up(proj, *, up_cmd):
        raise container.ContainerError("docker daemon not running")

    monkeypatch.setattr(container, "ensure_up", fake_ensure_up)

    respawn_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tmux,
        "respawn_pane",
        lambda pane_id, *, command: respawn_calls.append((pane_id, command)),
    )

    projs = _load_test_projects(projects_file)
    plan = restore.plan_entries(live_panes={}, projects=projs)
    placeholders = restore.pre_create_windows(plan, live_panes={})
    with caplog.at_level(logging.ERROR, logger="tmux_agents.commands.restore"):
        restore.execute_plan(plan, placeholders, projs)

    # pre_create added one tail-F respawn; the error path adds one more.
    assert len(respawn_calls) == 2
    # The error-display respawn replaced the placeholder (not the tail-F one).
    error_calls = [(pid, c) for pid, c in respawn_calls if "agent-restore failed" in c]
    assert len(error_calls) == 1
    pane_id, cmd = error_calls[0]
    assert pane_id == "%99"
    assert "agent-restore failed" in cmd
    assert "docker daemon not running" in cmd

    # phase=errored was written for that pane's worktree.
    state_file = paths.worktree_state_file(wt, "99")
    s = json.loads(state_file.read_text())
    assert s["phase"] == "errored"

    # Failure was logged via the unified logger (_fail emits logger.error).
    assert any(
        "docker daemon not running" in r.message and r.levelno == logging.ERROR
        for r in caplog.records
    )


def test_execute_finalize_removes_windows_previous(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    from tmux_agents import tmux
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts"
    _write_snapshot(
        "@1", project="scripts", branch=None, host_worktree=wt, window_index=1
    )
    assert paths.windows_previous_dir().exists()

    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    monkeypatch.setattr(tmux, "new_window", lambda s, *, name, command: "@new-1")
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%99")
    from tmux_agents import provisioning

    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **kw: True)
    monkeypatch.setattr(tmux, "respawn_pane", lambda pane_id, *, command: None)

    projs = _load_test_projects(projects_file)
    plan = restore.plan_entries(live_panes={}, projects=projs)
    placeholders = restore.pre_create_windows(plan, live_panes={})
    restore.execute_plan(plan, placeholders, projs)

    # windows.previous/ is gone after finalize.
    assert not paths.windows_previous_dir().exists()


def test_pre_create_attaches_overview_pane_when_split_layout(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    """When layout=split, the overview pane is attached to each restored window."""
    from tmux_agents import tmux, overview, paths
    from tmux_agents.commands import restore

    # Force split layout
    paths.layout_file().parent.mkdir(parents=True, exist_ok=True)
    paths.layout_file().write_text("split")

    wt = tmp_path / "scripts"
    _write_snapshot(
        "@1", project="scripts", branch=None, host_worktree=wt, window_index=1
    )

    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    monkeypatch.setattr(tmux, "new_window", lambda s, *, name, command: "@new-1")
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%99")
    monkeypatch.setattr(tmux, "respawn_pane", lambda pane, *, command: None)
    from tmux_agents import provisioning

    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **kw: True)

    attach_calls = []
    monkeypatch.setattr(
        overview, "attach_overview_pane", lambda wid: attach_calls.append(wid)
    )

    plan = restore.plan_entries(
        live_panes={}, projects=_load_test_projects(projects_file)
    )
    restore.pre_create_windows(plan, live_panes={})
    assert attach_calls == ["@new-1"]


def test_pre_create_skips_overview_pane_when_compact_layout(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    from tmux_agents import tmux, overview, paths
    from tmux_agents.commands import restore

    paths.layout_file().parent.mkdir(parents=True, exist_ok=True)
    paths.layout_file().write_text("compact")

    wt = tmp_path / "scripts"
    _write_snapshot(
        "@1", project="scripts", branch=None, host_worktree=wt, window_index=1
    )

    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    monkeypatch.setattr(tmux, "new_window", lambda s, *, name, command: "@new-1")
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%99")
    monkeypatch.setattr(tmux, "respawn_pane", lambda pane, *, command: None)
    from tmux_agents import provisioning

    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **kw: True)

    attach_calls = []
    monkeypatch.setattr(
        overview, "attach_overview_pane", lambda wid: attach_calls.append(wid)
    )

    plan = restore.plan_entries(
        live_panes={}, projects=_load_test_projects(projects_file)
    )
    restore.pre_create_windows(plan, live_panes={})
    assert attach_calls == []


def test_safe_load_projects_returns_empty_and_logs_on_malformed_toml(
    tmp_config_dir, tmp_state_dir, caplog
):
    """A broken projects.toml must not crash the worker; it should log and
    return an empty dict so per-entry validation drops the entries cleanly
    rather than the whole worker exiting silently with no diagnostic."""
    from tmux_agents import config

    paths.projects_toml().write_text(":::not::valid::toml:::")

    with caplog.at_level(logging.ERROR, logger="tmux_agents.commands.restore"):
        result = config.safe_load(
            paths.projects_toml(),
            on_error=lambda msg: logging.getLogger(
                "tmux_agents.commands.restore"
            ).error(msg),
        )
    assert result == {}
    assert any("projects.toml load failed" in r.message for r in caplog.records)
    # The legacy restore.log file must not be created.
    assert not (tmp_state_dir / "restore.log").exists()


def test_safe_load_projects_returns_empty_when_missing(tmp_config_dir, tmp_state_dir):
    """Missing projects.toml is the documented FileNotFoundError path —
    no log entry, just an empty dict."""
    from tmux_agents import config

    assert not paths.projects_toml().exists()
    assert (
        config.safe_load(
            paths.projects_toml(),
            on_error=lambda msg: logging.getLogger(
                "tmux_agents.commands.restore"
            ).error(msg),
        )
        == {}
    )
    # No restore.log written (FileNotFoundError is the silent path).
    assert not (tmp_state_dir / "restore.log").exists()


def test_pre_create_windows_does_not_provision(monkeypatch, tmp_state_dir, tmp_path):
    """Provisioning moves out of pre_create_windows into execute_plan."""
    from tmux_agents.commands import restore
    from tmux_agents import provisioning, tmux

    called = []
    monkeypatch.setattr(
        provisioning, "provision_settings", lambda *a, **k: called.append(a)
    )
    monkeypatch.setattr(tmux, "new_window", lambda *a, **k: "@11")
    monkeypatch.setattr(tmux, "set_window_option", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%99")
    respawn_calls = []
    monkeypatch.setattr(
        tmux,
        "respawn_pane",
        lambda pane, *, command: respawn_calls.append((pane, command)),
    )
    from tmux_agents import startup

    monkeypatch.setattr(startup, "_write_pane_state", lambda *a, **k: None)
    monkeypatch.setattr(restore.overview, "attach_overview_pane", lambda *a, **k: None)
    monkeypatch.setattr(restore.windows, "write_mapping", lambda m: None)
    monkeypatch.setattr(restore.paths, "read_layout", lambda: "compact")

    e = restore.Entry(
        window_id="@old",
        project="p",
        branch="b",
        host_worktree=tmp_path,
        pane_id="%2",
        claude_session_id=None,
        window_index=0,
    )
    restore.pre_create_windows([e], live_panes={})

    assert called == []
    # respawn-pane was called with the tail -F command keyed by the NEW window_id
    assert len(respawn_calls) == 1
    pane, command = respawn_calls[0]
    assert pane == "%99"
    assert command == startup.placeholder_command("@11")


def test_pre_create_revive_splits_surviving_pane_and_cleans_old_files(
    monkeypatch,
    tmp_config_dir,
    tmp_path,
    projects_file,
):
    """Revive path: split the surviving overview pane above at 75%, rewrite
    the mapping with the new pane id, and unlink the old per-pane files."""
    from tmux_agents.commands import restore
    from tmux_agents import paths, tmux

    wt = tmp_path / "scripts"
    wt.mkdir(exist_ok=True)

    # Old per-pane debris that should be cleaned up.
    old_pane = "42"
    pane_dir = wt / ".local" / ".tmux-agents"
    pane_dir.mkdir(parents=True)
    (pane_dir / f"state-{old_pane}.json").write_text("{}")
    (pane_dir / f"pending-{old_pane}").mkdir()
    (pane_dir / f"pending-{old_pane}" / "subagent__a1").write_text("")
    (pane_dir / f"session-{old_pane}.id").write_text(
        "00000000-0000-0000-0000-000000000000"
    )

    e = restore.Entry(
        window_id="@7",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id=old_pane,
        claude_session_id=None,
        window_index=1,
        kind="revive",
    )

    # Mock all tmux calls used by pre_create_windows on the revive branch.
    # The lone survivor is tagged overview, so this preserves the classic
    # single-agent revive geometry (75%/vertical).
    monkeypatch.setattr(tmux, "overview_pane_ids", lambda wid: ["%50"])
    split_calls = []

    def fake_split(target, *, percent, command, before=False, horizontal=False):
        split_calls.append(
            {
                "target": target,
                "percent": percent,
                "command": command,
                "before": before,
                "horizontal": horizontal,
            }
        )
        return "%99"

    monkeypatch.setattr(tmux, "split_window", fake_split)
    monkeypatch.setattr(tmux, "respawn_pane", lambda *a, **kw: None)
    monkeypatch.setattr(tmux, "set_window_option", lambda *a, **kw: None)
    selected: list[str] = []
    monkeypatch.setattr(tmux, "select_pane", lambda p: selected.append(p))

    placeholders = restore.pre_create_windows([e], live_panes={"@7": {"%50"}})

    assert "@7" in placeholders
    ph = placeholders["@7"]
    assert ph.pane_id == "%99"
    assert ph.new_window_id == "@7"  # window id is unchanged on revive

    m = paths.read_json_or(paths.window_mapping_file("@7"), None)
    assert m["pane_id"] == "99"

    # Old per-pane files gone, new state file present.
    assert not (pane_dir / f"state-{old_pane}.json").exists()
    assert not (pane_dir / f"pending-{old_pane}").exists()
    assert not (pane_dir / f"session-{old_pane}.id").exists()
    assert (pane_dir / "state-99.json").exists()

    assert split_calls == [
        {
            "target": "%50",
            "percent": 75,
            "command": startup.placeholder_command("@7"),
            "before": True,
            "horizontal": False,
        }
    ]
    # Focus must move to the new agent pane (split_window's `-d` would
    # otherwise leave focus on the surviving overview pane).
    assert selected == ["%99"]


def test_classify_skip_when_pane_alive():
    from tmux_agents.commands import restore

    wt = Path("/tmp/unused-by-classifier")
    e = restore.Entry(
        window_id="@3",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id="42",
        claude_session_id=None,
        window_index=1,
        kind="fresh",
    )
    assert restore.classify_entry(e, live_panes={"@3": {"%42"}}) == "skip"


def test_classify_revive_when_window_live_but_pane_gone():
    from tmux_agents.commands import restore

    wt = Path("/tmp/unused-by-classifier")
    e = restore.Entry(
        window_id="@3",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id="42",
        claude_session_id=None,
        window_index=1,
        kind="fresh",
    )
    assert restore.classify_entry(e, live_panes={"@3": {"%99"}}) == "revive"


def test_classify_fresh_when_window_missing():
    from tmux_agents.commands import restore

    wt = Path("/tmp/unused-by-classifier")
    e = restore.Entry(
        window_id="@3",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id="42",
        claude_session_id=None,
        window_index=1,
        kind="fresh",
    )
    assert restore.classify_entry(e, live_panes={}) == "fresh"


def test_classify_reactivate_when_pane_alive_but_errored(tmp_path):
    """A failed restore leaves the placeholder pane alive but with
    phase=errored. A retry must reactivate it (re-run container + respawn
    Claude in place), not skip it as if it were a healthy agent."""
    from tmux_agents.commands import restore
    from tmux_agents import startup, phase

    wt = tmp_path / "scripts"
    wt.mkdir()
    startup._write_pane_state(wt, "42", phase_value=phase.ERRORED)
    e = restore.Entry(
        window_id="@3",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id="42",
        claude_session_id=None,
        window_index=1,
        kind="fresh",
    )
    assert restore.classify_entry(e, live_panes={"@3": {"%42"}}) == "reactivate"


def test_classify_skip_when_pane_alive_and_not_errored(tmp_path):
    """A live, non-errored pane is a healthy agent — still skip."""
    from tmux_agents.commands import restore
    from tmux_agents import startup, phase

    wt = tmp_path / "scripts"
    wt.mkdir()
    startup._write_pane_state(wt, "42", phase_value=phase.RUNNING)
    e = restore.Entry(
        window_id="@3",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id="42",
        claude_session_id=None,
        window_index=1,
        kind="fresh",
    )
    assert restore.classify_entry(e, live_panes={"@3": {"%42"}}) == "skip"


def test_classify_revive_when_all_panes_dead():
    """Window is in live_panes with an empty pane set — all panes dead.
    The classifier returns 'revive'; the no-survivor guard in
    _pre_create_revive then catches it without creating a Placeholder."""
    from tmux_agents.commands import restore

    e = restore.Entry(
        window_id="@3",
        project="scripts",
        branch=None,
        host_worktree=Path("/tmp/unused-by-classifier"),
        pane_id="42",
        claude_session_id=None,
        window_index=1,
        kind="fresh",
    )
    assert restore.classify_entry(e, live_panes={"@3": set()}) == "revive"


def test_pre_create_reactivate_reuses_existing_pane(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    """Reactivate reuses the errored placeholder's window+pane in place: no
    new window is created, the existing pane is respawned into the tail-log
    placeholder, its state is reset to starting, and a Placeholder pointing at
    the same pane is returned so execute_plan can respawn Claude into it."""
    from tmux_agents.commands import restore
    from tmux_agents import tmux, startup, phase

    wt = tmp_path / "scripts"
    wt.mkdir()
    e = restore.Entry(
        window_id="@5",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id="42",
        claude_session_id="sid-1",
        window_index=1,
        kind="reactivate",
    )

    new_window_calls: list[int] = []
    monkeypatch.setattr(
        tmux,
        "new_window",
        lambda *a, **k: (new_window_calls.append(1), "@nope")[1],
    )
    respawns: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tmux, "respawn_pane", lambda pane, *, command: respawns.append((pane, command))
    )
    monkeypatch.setattr(restore.paths, "read_layout", lambda: "compact")

    placeholders = restore.pre_create_windows([e], live_panes={"@5": {"%42"}})

    assert new_window_calls == []  # reused the existing window
    assert "@5" in placeholders
    ph = placeholders["@5"]
    assert ph.pane_id == "%42"
    assert ph.new_window_id == "@5"
    assert ph.entry.claude_session_id == "sid-1"
    # Existing pane respawned into the tail-log placeholder (shows retry progress).
    assert respawns == [("%42", startup.placeholder_command("@5"))]
    # State reset from errored back to starting.
    s = json.loads(paths.worktree_state_file(wt, "42").read_text())
    assert s["phase"] == phase.STARTING


def test_retry_reactivates_errored_placeholder_from_windows_dir(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    """End-to-end retry: after a failed restore, windows/ holds a mapping for
    a live-but-errored placeholder pane and windows.previous/ is gone. A
    second agent-restore must plan it as `reactivate` and respawn Claude into
    the SAME pane — the bug was it planned nothing (skip) and did nothing."""
    from tmux_agents.commands import restore
    from tmux_agents import tmux, startup, phase, windows

    wt = tmp_path / "scripts"  # host-only project (no container)
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@new-1",
            project="scripts",
            branch=None,
            host_worktree=wt,
            pane_id="42",
            claude_session_id="sid-1",
        )
    )
    startup._write_pane_state(wt, "42", phase_value=phase.ERRORED)
    assert not paths.windows_previous_dir().exists()  # first run deleted it

    live_panes = {"@new-1": {"%42"}}
    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    monkeypatch.setattr(restore.paths, "read_layout", lambda: "compact")
    respawns: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tmux, "respawn_pane", lambda pane, *, command: respawns.append((pane, command))
    )
    from tmux_agents import provisioning

    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **kw: True)

    projs = _load_test_projects(projects_file)
    plan = restore.plan_entries(live_panes=live_panes, projects=projs)
    assert len(plan) == 1
    assert plan[0].kind == "reactivate"

    placeholders = restore.pre_create_windows(plan, live_panes=live_panes)
    restore.execute_plan(plan, placeholders, projs)

    # Claude was respawned into the SAME pane %42, not a new window.
    claude_respawns = [(p, c) for p, c in respawns if "claude" in c]
    assert claude_respawns
    assert all(p == "%42" for p, c in claude_respawns)


def test_pre_create_revive_reaps_duplicate_overview_panes(
    monkeypatch,
    tmp_config_dir,
    tmp_path,
    projects_file,
):
    """Degenerate window with two overview panes and no agent pane (a layout
    toggle re-attached an overview to an already-agent-dead window). Revive
    keeps one overview pane, kills the extras, and splits a fresh agent pane
    above the survivor."""
    from tmux_agents.commands import restore
    from tmux_agents import tmux

    wt = tmp_path / "scripts"
    wt.mkdir(exist_ok=True)

    e = restore.Entry(
        window_id="@7",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id="42",
        claude_session_id=None,
        window_index=1,
        kind="revive",
    )

    monkeypatch.setattr(tmux, "overview_pane_ids", lambda wid: ["%50", "%51"])
    killed: list[str] = []
    monkeypatch.setattr(tmux, "kill_pane", lambda p: killed.append(p))
    split_calls: list[dict] = []

    def fake_split(target, *, percent, command, before=False, horizontal=False):
        split_calls.append({"target": target, "before": before})
        return "%99"

    monkeypatch.setattr(tmux, "split_window", fake_split)
    monkeypatch.setattr(tmux, "select_pane", lambda p: None)

    placeholders = restore.pre_create_windows(
        [e],
        live_panes={"@7": {"%50", "%51"}},
    )

    assert "@7" in placeholders
    assert placeholders["@7"].pane_id == "%99"
    assert killed == ["%51"]  # keep the first overview pane, reap the rest
    assert split_calls == [{"target": "%50", "before": True}]


def test_pre_create_revive_anchors_on_live_secondary_when_overview_also_survives(
    monkeypatch,
    tmp_config_dir,
    tmp_path,
    projects_file,
):
    """Split layout, dual mapping: dead default, live healthy secondary,
    AND the overview pane also survives (unlike the compact-layout case
    where there's no overview at all). The new default placeholder must
    anchor on the SECONDARY (50/50 horizontal, before=True — same
    dual-agent geometry as the compact case), not split off the overview —
    and the overview pane must never be split or killed."""
    from tmux_agents.commands import restore
    from tmux_agents import tmux, windows

    wt = tmp_path / "scripts"
    wt.mkdir(exist_ok=True)

    e = restore.Entry(
        window_id="@7",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id="10",  # dead default's recorded pane
        claude_session_id=None,
        window_index=1,
        kind="revive",
        secondary_action="none",  # secondary is healthy — never repaired
        slots=[
            windows.AgentSlot(kind="claude", pane_id="10"),
            windows.AgentSlot(kind="codex", pane_id="20"),
        ],
    )

    monkeypatch.setattr(tmux, "overview_pane_ids", lambda wid: ["%50"])
    killed: list[str] = []
    monkeypatch.setattr(tmux, "kill_pane", lambda p: killed.append(p))
    respawned: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tmux, "respawn_pane", lambda p, *, command: respawned.append((p, command))
    )
    split_calls: list[dict] = []

    def fake_split(target, *, percent, command, before=False, horizontal=False):
        split_calls.append(
            {
                "target": target,
                "percent": percent,
                "before": before,
                "horizontal": horizontal,
            }
        )
        return "%99"

    monkeypatch.setattr(tmux, "split_window", fake_split)
    monkeypatch.setattr(tmux, "select_pane", lambda p: None)

    # Survivors: the overview pane AND the live secondary — no default pane.
    placeholders = restore.pre_create_windows([e], live_panes={"@7": {"%50", "%20"}})

    assert "@7" in placeholders
    ph = placeholders["@7"]
    assert ph.pane_id == "%99"
    assert ph.secondary_pane_id is None  # secondary untouched at pre-create

    assert split_calls == [
        {"target": "%20", "percent": 50, "before": True, "horizontal": True}
    ]
    # The overview pane is never split off or killed; the secondary is
    # never killed or respawned.
    assert killed == []
    assert respawned == []

    m = windows.read_mapping("@7")
    assert m.agents[0].pane_id == "99"  # new default's pane
    assert m.agents[1].pane_id == "20"  # secondary untouched


def test_pre_create_revive_bails_when_survivors_have_no_overview_pane(
    monkeypatch,
    tmp_config_dir,
    tmp_path,
    projects_file,
):
    """More than one survivor but none tagged @role=overview — we can't tell
    where to put the agent, so revive bails without splitting or killing."""
    from tmux_agents.commands import restore
    from tmux_agents import tmux

    wt = tmp_path / "scripts"
    wt.mkdir(exist_ok=True)

    e = restore.Entry(
        window_id="@7",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id="42",
        claude_session_id=None,
        window_index=1,
        kind="revive",
    )

    monkeypatch.setattr(tmux, "overview_pane_ids", lambda wid: [])
    killed: list[str] = []
    monkeypatch.setattr(tmux, "kill_pane", lambda p: killed.append(p))
    split_calls: list[str] = []
    monkeypatch.setattr(
        tmux,
        "split_window",
        lambda target, *, percent, command, before=False: (
            split_calls.append(target) or "%99"
        ),
    )

    placeholders = restore.pre_create_windows(
        [e],
        live_panes={"@7": {"%50", "%51"}},
    )

    assert placeholders == {}
    assert split_calls == []
    assert killed == []


def test_pre_create_revive_skips_when_no_panes_survive(
    monkeypatch,
    tmp_config_dir,
    tmp_path,
    projects_file,
):
    """All panes dead — _pre_create_revive returns None without calling
    tmux.split_window."""
    from tmux_agents.commands import restore
    from tmux_agents import tmux

    wt = tmp_path / "scripts"
    wt.mkdir(exist_ok=True)

    e = restore.Entry(
        window_id="@7",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id="42",
        claude_session_id=None,
        window_index=1,
        kind="revive",
    )

    split_calls: list[dict] = []
    monkeypatch.setattr(
        tmux,
        "split_window",
        lambda target, *, percent, command, before=False: (
            split_calls.append(target) or "%99"
        ),
    )

    placeholders = restore.pre_create_windows([e], live_panes={"@7": set()})

    assert placeholders == {}
    assert split_calls == []


def _tmux_error(stderr):
    from tmux_agents import tmux

    return tmux.TmuxError(
        1, ["tmux", "-L", "agents", "respawn-pane"], output="", stderr=stderr
    )


def _fork_failure():
    return _tmux_error("respawn pane failed: fork failed: Device not configured")


def test_pre_create_salvages_transient_fork_failure(
    monkeypatch,
    tmp_config_dir,
    tmp_state_dir,
    tmp_path,
    projects_file,
):
    """The #7 scenario: the placeholder respawn hits a transient fork
    failure once, then succeeds — the entry must be restored, not skipped."""
    from tmux_agents import tmux, overview, provisioning, startup
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts"
    _write_snapshot(
        "@1", project="scripts", branch=None, host_worktree=wt, window_index=1
    )

    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    monkeypatch.setattr(tmux, "new_window", lambda s, *, name, command: "@new-1")
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%99")
    calls = []

    def flaky_respawn(pane, *, command):
        calls.append(pane)
        if len(calls) == 1:
            raise _fork_failure()

    monkeypatch.setattr(tmux, "respawn_pane", flaky_respawn)
    monkeypatch.setattr(startup.time, "sleep", lambda s: None)
    monkeypatch.setattr(overview, "attach_overview_pane", lambda wid: None)
    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **kw: True)

    plan = restore.plan_entries(
        live_panes={}, projects=_load_test_projects(projects_file)
    )
    placeholders = restore.pre_create_windows(plan, live_panes={})

    assert "@new-1" in placeholders  # salvaged, not skipped
    assert len(calls) == 2  # failed once, retried, succeeded


# ===== Task 16: harvest barrier, classify_secondary, per-slot actions =====


def _write_dual_snapshot(
    window_id: str,
    *,
    project: str,
    branch: str | None,
    host_worktree: Path,
    agents: list[dict],
    window_index: int | None = None,
    snapshot_dir=None,
) -> None:
    d = snapshot_dir or paths.windows_previous_dir()
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{window_id}.json"
    payload = {
        "project": project,
        "branch": branch,
        "host_worktree": str(host_worktree),
        "pane_id": agents[0].get("pane_id") or "",
        "agents": agents,
    }
    if window_index is not None:
        payload["window_index"] = window_index
    f.write_text(json.dumps(payload))


def test_harvest_session_ids_merges_disk_session_id_across_entries(tmp_path):
    """Slot A's newly assigned pane id equals slot B's OLD pane id; only
    disk holds B's latest session id (written after the snapshot, before the
    server died). The barrier must merge B's disk id into B's slot, keyed by
    B's own recorded pane id — not accidentally cross-wired with A."""
    from tmux_agents.commands import restore
    from tmux_agents import paths

    wt = tmp_path / "scripts"
    wt.mkdir()
    paths.worktree_session_id_file(wt, "15").parent.mkdir(parents=True, exist_ok=True)
    paths.worktree_session_id_file(wt, "15").write_text(
        "11111111-1111-1111-1111-111111111111"
    )

    entry_a = restore.Entry(
        window_id="@a",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id="99",
        claude_session_id=None,
        window_index=1,
        kind="fresh",
    )
    entry_b = restore.Entry(
        window_id="@b",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id="15",
        claude_session_id=None,
        window_index=2,
        kind="fresh",
    )

    out = restore.harvest_session_ids([entry_a, entry_b])
    by_id = {e.window_id: e for e in out}
    assert by_id["@a"].slots[0].session_id is None
    assert by_id["@b"].slots[0].session_id == "11111111-1111-1111-1111-111111111111"
    # windows.AgentSlot.pane_id is untouched by the harvest.
    assert by_id["@b"].slots[0].pane_id == "15"


def test_harvest_session_ids_prefers_disk_over_stale_snapshot_value():
    """When disk holds nothing readable, the snapshot's own value survives."""
    from tmux_agents.commands import restore

    e = restore.Entry(
        window_id="@a",
        project="scripts",
        branch=None,
        host_worktree=Path("/tmp/does-not-exist-for-harvest-test"),
        pane_id="42",
        claude_session_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        window_index=1,
        kind="fresh",
    )
    out = restore.harvest_session_ids([e])
    assert out[0].slots[0].session_id == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _dual_entry(
    wt: Path, *, default_pane="10", secondary_pane=None, secondary_kind="codex"
):
    from tmux_agents.commands import restore
    from tmux_agents import windows

    return restore.Entry(
        window_id="@1",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id=default_pane,
        claude_session_id=None,
        window_index=1,
        kind="skip",
        slots=[
            windows.AgentSlot(kind="claude", pane_id=default_pane),
            windows.AgentSlot(kind=secondary_kind, pane_id=secondary_pane),
        ],
    )


def test_classify_secondary_none_when_slot_absent():
    from tmux_agents.commands import restore

    e = restore.Entry(
        window_id="@1",
        project="scripts",
        branch=None,
        host_worktree=Path("/tmp/unused"),
        pane_id="10",
        claude_session_id=None,
        window_index=1,
    )
    assert len(e.slots) == 1  # single-agent window: never repaired into dual
    assert restore.classify_secondary(e, live_panes={"@1": {"%10"}}) == "none"


def test_classify_secondary_none_when_alive_and_healthy(tmp_path):
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts"
    wt.mkdir()
    e = _dual_entry(wt, secondary_pane="20")
    assert restore.classify_secondary(e, live_panes={"@1": {"%10", "%20"}}) == "none"


def test_classify_secondary_reactivate_when_alive_but_errored(tmp_path):
    from tmux_agents.commands import restore
    from tmux_agents import startup, phase

    wt = tmp_path / "scripts"
    wt.mkdir()
    startup._write_pane_state(wt, "20", phase_value=phase.ERRORED)
    e = _dual_entry(wt, secondary_pane="20")
    assert (
        restore.classify_secondary(e, live_panes={"@1": {"%10", "%20"}}) == "reactivate"
    )


def test_classify_secondary_revive_when_pane_dead(tmp_path):
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts"
    wt.mkdir()
    e = _dual_entry(wt, secondary_pane="20")
    assert restore.classify_secondary(e, live_panes={"@1": {"%10"}}) == "revive"


def test_classify_secondary_revive_when_pane_id_none(tmp_path):
    """Secondary slot exists but has never been started (pane_id: null)."""
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts"
    wt.mkdir()
    e = _dual_entry(wt, secondary_pane=None)
    assert restore.classify_secondary(e, live_panes={"@1": {"%10"}}) == "revive"


def test_plan_keeps_healthy_default_with_dead_secondary(
    tmp_config_dir, tmp_path, projects_file
):
    """Drop rule: entry dropped only when window action is `skip` AND every
    secondary action is `none`. A healthy default (skip) with a dead
    secondary (revive) must stay planned as a secondary-only repair."""
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts"
    _write_dual_snapshot(
        "@1",
        project="scripts",
        branch=None,
        host_worktree=wt,
        agents=[
            {"kind": "claude", "pane_id": "10"},
            {"kind": "codex", "pane_id": "20"},
        ],
        window_index=1,
    )
    plan = restore.plan_entries(
        live_panes={"@1": {"%10"}},  # only the default survives
        projects=_load_test_projects(projects_file),
    )
    assert len(plan) == 1
    assert plan[0].kind == "skip"
    assert plan[0].secondary_action == "revive"


def test_plan_drops_entry_when_skip_and_secondary_none(
    tmp_config_dir, tmp_path, projects_file
):
    """Both slots healthy and alive -> dropped, exactly like a single-agent
    skip today."""
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts"
    _write_dual_snapshot(
        "@1",
        project="scripts",
        branch=None,
        host_worktree=wt,
        agents=[
            {"kind": "claude", "pane_id": "10"},
            {"kind": "codex", "pane_id": "20"},
        ],
        window_index=1,
    )
    plan = restore.plan_entries(
        live_panes={"@1": {"%10", "%20"}},
        projects=_load_test_projects(projects_file),
    )
    assert plan == []


def test_plan_revives_only_default_when_secondary_healthy(
    tmp_config_dir, tmp_path, projects_file
):
    """Dead default + healthy secondary revives only the default — the
    secondary must never be touched (secondary_action == 'none')."""
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts"
    _write_dual_snapshot(
        "@1",
        project="scripts",
        branch=None,
        host_worktree=wt,
        agents=[
            {"kind": "claude", "pane_id": "10"},
            {"kind": "codex", "pane_id": "20"},
        ],
        window_index=1,
    )
    plan = restore.plan_entries(
        live_panes={"@1": {"%20"}},  # only the secondary survives
        projects=_load_test_projects(projects_file),
    )
    assert len(plan) == 1
    assert plan[0].kind == "revive"
    assert plan[0].secondary_action == "none"


def test_pre_create_revive_splits_off_live_secondary_in_compact_layout(
    monkeypatch,
    tmp_config_dir,
    tmp_path,
    projects_file,
):
    """Compact layout (no overview panes at all), dual mapping: dead default
    + live healthy secondary. The lone survivor is the secondary agent
    pane, not an overview — pre-create must recognize that (via
    `tmux.overview_pane_ids` returning empty AND the survivor matching the
    mapped secondary's pane id) and split the new default placeholder off
    it 50/50 HORIZONTAL (dual-agent geometry, default lands on the left via
    `before=True`), never killing or respawning the healthy secondary.
    Regression guard: the old code assumed a lone survivor was always the
    overview and would wrongly reshape the secondary with a 75%/vertical
    split."""
    from tmux_agents.commands import restore
    from tmux_agents import tmux, windows

    wt = tmp_path / "scripts"
    wt.mkdir(exist_ok=True)

    e = restore.Entry(
        window_id="@7",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id="10",  # dead default's recorded pane
        claude_session_id=None,
        window_index=1,
        kind="revive",
        secondary_action="none",  # secondary is healthy — never repaired
        slots=[
            windows.AgentSlot(kind="claude", pane_id="10"),
            windows.AgentSlot(kind="codex", pane_id="20"),
        ],
    )

    monkeypatch.setattr(tmux, "overview_pane_ids", lambda wid: [])  # compact layout
    killed: list[str] = []
    monkeypatch.setattr(tmux, "kill_pane", lambda p: killed.append(p))
    respawned: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tmux, "respawn_pane", lambda p, *, command: respawned.append((p, command))
    )
    split_calls: list[dict] = []

    def fake_split(target, *, percent, command, before=False, horizontal=False):
        split_calls.append(
            {
                "target": target,
                "percent": percent,
                "before": before,
                "horizontal": horizontal,
            }
        )
        return "%99"

    monkeypatch.setattr(tmux, "split_window", fake_split)
    monkeypatch.setattr(tmux, "select_pane", lambda p: None)

    placeholders = restore.pre_create_windows([e], live_panes={"@7": {"%20"}})

    assert "@7" in placeholders
    ph = placeholders["@7"]
    assert ph.pane_id == "%99"
    assert ph.secondary_pane_id is None  # secondary untouched at pre-create

    assert split_calls == [
        {"target": "%20", "percent": 50, "before": True, "horizontal": True}
    ]
    # The healthy secondary is never killed or respawned.
    assert killed == []
    assert respawned == []

    m = windows.read_mapping("@7")
    assert m.agents[0].pane_id == "99"  # new default's pane
    assert m.agents[1].pane_id == "20"  # secondary untouched


def test_pre_create_revive_bails_when_lone_survivor_is_unmapped_pane(
    monkeypatch,
    tmp_config_dir,
    tmp_path,
    projects_file,
):
    """Lone survivor that is neither an overview pane nor the mapped
    secondary — nothing usable to split from. Must bail (like the
    no-overview-among-multiple-survivors case) rather than guessing."""
    from tmux_agents.commands import restore
    from tmux_agents import tmux, windows

    wt = tmp_path / "scripts"
    wt.mkdir(exist_ok=True)

    e = restore.Entry(
        window_id="@7",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id="10",
        claude_session_id=None,
        window_index=1,
        kind="revive",
        secondary_action="none",
        slots=[
            windows.AgentSlot(kind="claude", pane_id="10"),
            windows.AgentSlot(kind="codex", pane_id="20"),
        ],
    )

    monkeypatch.setattr(tmux, "overview_pane_ids", lambda wid: [])
    split_calls: list[str] = []
    monkeypatch.setattr(
        tmux,
        "split_window",
        lambda target, *, percent, command, before=False, horizontal=False: (
            split_calls.append(target) or "%99"
        ),
    )

    # Survivor %77 is neither the overview nor the mapped secondary (%20).
    placeholders = restore.pre_create_windows([e], live_panes={"@7": {"%77"}})

    assert placeholders == {}
    assert split_calls == []


def test_pre_create_dual_fresh_emits_horizontal_split(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    """Two-slot fresh entry: after the primary placeholder is created, the
    secondary is split off it 50/50 horizontal (`agent-other`'s params)."""
    from tmux_agents import tmux, overview, windows
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts"
    e = restore.Entry(
        window_id="@1",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id="",
        claude_session_id=None,
        window_index=1,
        kind="fresh",
        slots=[
            windows.AgentSlot(kind="claude", pane_id=None),
            windows.AgentSlot(
                kind="codex",
                pane_id=None,
                session_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
            ),
        ],
        secondary_action="revive",
    )

    monkeypatch.setattr(tmux, "new_window", lambda s, *, name, command: "@new-1")
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%50")
    monkeypatch.setattr(tmux, "respawn_pane", lambda *a, **k: None)
    monkeypatch.setattr(overview, "attach_overview_pane", lambda wid: None)

    split_calls = []

    def fake_split(
        target, *, percent, command, before=False, horizontal=False, full_size=False
    ):
        split_calls.append(
            {"target": target, "percent": percent, "horizontal": horizontal}
        )
        return "%51"

    monkeypatch.setattr(tmux, "split_window", fake_split)

    placeholders = restore.pre_create_windows([e], live_panes={})

    assert "@new-1" in placeholders
    ph = placeholders["@new-1"]
    assert ph.pane_id == "%50"
    assert ph.secondary_pane_id == "%51"
    assert split_calls == [{"target": "%50", "percent": 50, "horizontal": True}]

    m = windows.read_mapping("@new-1")
    assert [s.pane_id for s in m.agents] == ["50", "51"]
    assert m.agents[1].kind == "codex"
    # The secondary's harvested session id survives the round trip.
    assert m.agents[1].session_id == "cccccccc-cccc-cccc-cccc-cccccccccccc"


def test_pre_create_secondary_split_loses_race_to_live_secondary(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    """`agent-other` (or a concurrent restore) publishes a live secondary
    between planning and `_pre_create_secondary_split`'s locked region:
    the CAS must reject our publish, kill the placeholder we just split
    off, and leave slot 1 pointing at the winner's pane untouched."""
    from tmux_agents.commands import restore
    from tmux_agents import tmux, windows

    wt = tmp_path / "scripts"
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@1",
            project="scripts",
            branch=None,
            host_worktree=wt,
            pane_id="10",
            agents=[
                windows.AgentSlot(kind="claude", pane_id="10"),
                windows.AgentSlot(kind="codex", pane_id=None),
            ],
        )
    )
    e = _dual_entry(wt, default_pane="10", secondary_pane=None)
    e = dataclasses.replace(e, kind="skip", secondary_action="revive")

    monkeypatch.setattr(tmux, "split_window", lambda *a, **k: "%77")
    kill_calls = []
    monkeypatch.setattr(tmux, "kill_pane", lambda target: kill_calls.append(target))

    # Simulate the winner (agent-other) publishing a live secondary AFTER
    # our split_window call but BEFORE our publish CAS runs, by mutating
    # the on-disk mapping from inside update_mapping's own lock hold via a
    # wrapped fn.
    real_update_mapping = windows.update_mapping

    def racy_update_mapping(window_id, fn):
        winner = windows.read_mapping(window_id)
        agents = list(winner.agents)
        agents[1] = dataclasses.replace(agents[1], pane_id="99")
        windows.write_mapping(dataclasses.replace(winner, agents=agents))
        return real_update_mapping(window_id, fn)

    monkeypatch.setattr(windows, "update_mapping", racy_update_mapping)

    result = restore._pre_create_secondary_split(e, "@1", "%10")

    assert result is None
    assert kill_calls == ["%77"]
    m = windows.read_mapping("@1")
    assert m.agents[1].pane_id == "99"  # winner's pane untouched


def test_pre_create_secondary_split_publishes_against_planned_revive_identity(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    """Normal fresh-server dual-snapshot revive: `classify_secondary` said
    'revive' because slot 1's OLD recorded pane is no longer alive — but the
    fresh mapping's slot 1 still carries that OLD, non-null pane id (nobody
    has touched it since the snapshot). The CAS must compare against the
    PLANNED identity captured at planning time, not reject on a bare
    `pane_id is not None` check — that used to make this the common case
    fail. Before the fix, this test fails: the placeholder gets killed and
    slot 1 is never restored."""
    from tmux_agents.commands import restore
    from tmux_agents import tmux, windows

    wt = tmp_path / "scripts"
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@1",
            project="scripts",
            branch=None,
            host_worktree=wt,
            pane_id="10",
            agents=[
                windows.AgentSlot(kind="claude", pane_id="10"),
                # OLD, now-dead pane id — exactly what a fresh-server
                # dual-snapshot restore finds on disk.
                windows.AgentSlot(kind="codex", pane_id="20"),
            ],
        )
    )
    e = _dual_entry(wt, default_pane="10", secondary_pane="20")
    e = dataclasses.replace(e, kind="skip", secondary_action="revive")

    monkeypatch.setattr(tmux, "split_window", lambda *a, **k: "%77")
    kill_calls = []
    monkeypatch.setattr(tmux, "kill_pane", lambda target: kill_calls.append(target))

    result = restore._pre_create_secondary_split(e, "@1", "%10")

    assert result == "%77"
    assert kill_calls == []
    m = windows.read_mapping("@1")
    assert m.agents[1].pane_id == "77"
    assert m.agents[1].kind == "codex"


def test_pre_create_secondary_split_accepts_concurrent_dead_marking(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    """Between planning and the CAS, the tick's `_mark_secondary_dead` can
    legitimately transition the planned slot-1 identity `(codex, "20")` to
    `(codex, pane_id=None, last_pane_id="20")` — it independently noticed
    the same recorded pane is gone, merging a (possibly newer) session id
    from disk in the process. This is NOT a competing publish (no live
    pane exists), so the CAS must accept it as a compatible progression:
    publish our placeholder, and use the FRESH session id rather than the
    planned one."""
    from tmux_agents.commands import restore
    from tmux_agents import tmux, windows

    wt = tmp_path / "scripts"
    planned_session_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fresh_session_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@1",
            project="scripts",
            branch=None,
            host_worktree=wt,
            pane_id="10",
            agents=[
                windows.AgentSlot(kind="claude", pane_id="10"),
                windows.AgentSlot(
                    kind="codex", pane_id="20", session_id=planned_session_id
                ),
            ],
        )
    )
    e = _dual_entry(wt, default_pane="10", secondary_pane="20")
    e = dataclasses.replace(
        e,
        kind="skip",
        secondary_action="revive",
        slots=[
            windows.AgentSlot(kind="claude", pane_id="10"),
            windows.AgentSlot(
                kind="codex", pane_id="20", session_id=planned_session_id
            ),
        ],
    )

    monkeypatch.setattr(tmux, "split_window", lambda *a, **k: "%77")
    kill_calls = []
    monkeypatch.setattr(tmux, "kill_pane", lambda target: kill_calls.append(target))

    real_update_mapping = windows.update_mapping

    def racy_update_mapping(window_id, fn):
        # Simulate `state_tick._mark_secondary_dead` firing between
        # planning and this CAS: same kind, pane_id nulled, last_pane_id
        # pointing at the planned pane, and a fresher session id merged
        # in from disk.
        current = windows.read_mapping(window_id)
        agents = list(current.agents)
        agents[1] = dataclasses.replace(
            agents[1],
            pane_id=None,
            last_pane_id="20",
            session_id=fresh_session_id,
        )
        windows.write_mapping(dataclasses.replace(current, agents=agents))
        return real_update_mapping(window_id, fn)

    monkeypatch.setattr(windows, "update_mapping", racy_update_mapping)

    result = restore._pre_create_secondary_split(e, "@1", "%10")

    assert result == "%77"
    assert kill_calls == []
    m = windows.read_mapping("@1")
    assert m.agents[1].pane_id == "77"
    assert m.agents[1].kind == "codex"
    assert m.agents[1].session_id == fresh_session_id


def test_activate_uses_fresh_session_id_after_concurrent_dead_marking(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    """End-to-end continuation of the previous test: once the placeholder
    is published via the compatible dead-state progression, the secondary's
    respawn command built during activation must resume the FRESH session
    id (merged by the concurrent `_mark_secondary_dead`), not the stale one
    captured at planning time."""
    from tmux_agents.commands import restore
    from tmux_agents import tmux, windows, provisioning

    wt = tmp_path / "scripts"
    planned_session_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fresh_session_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@1",
            project="scripts",
            branch=None,
            host_worktree=wt,
            pane_id="10",
            agents=[
                windows.AgentSlot(kind="claude", pane_id="10"),
                windows.AgentSlot(
                    kind="codex", pane_id="20", session_id=planned_session_id
                ),
            ],
        )
    )
    e = _dual_entry(wt, default_pane="10", secondary_pane="20")
    e = dataclasses.replace(
        e,
        kind="skip",
        secondary_action="revive",
        slots=[
            windows.AgentSlot(kind="claude", pane_id="10"),
            windows.AgentSlot(
                kind="codex", pane_id="20", session_id=planned_session_id
            ),
        ],
    )

    monkeypatch.setattr(tmux, "split_window", lambda *a, **k: "%77")
    monkeypatch.setattr(tmux, "kill_pane", lambda target: None)

    real_update_mapping = windows.update_mapping

    def racy_update_mapping(window_id, fn):
        current = windows.read_mapping(window_id)
        agents = list(current.agents)
        agents[1] = dataclasses.replace(
            agents[1],
            pane_id=None,
            last_pane_id="20",
            session_id=fresh_session_id,
        )
        windows.write_mapping(dataclasses.replace(current, agents=agents))
        return real_update_mapping(window_id, fn)

    monkeypatch.setattr(windows, "update_mapping", racy_update_mapping)

    placeholders = restore.pre_create_windows([e], live_panes={"@1": {"%10"}})
    assert placeholders["@1"].secondary_pane_id == "%77"

    respawn_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tmux,
        "respawn_pane",
        lambda pane, *, command: respawn_calls.append((pane, command)),
    )
    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **kw: True)

    projs = _load_test_projects(projects_file)
    restore.execute_plan([e], placeholders, projs)

    secondary_calls = [c for c in respawn_calls if c[0] == "%77"]
    assert len(secondary_calls) == 1
    _, cmd = secondary_calls[0]
    assert fresh_session_id in cmd
    assert planned_session_id not in cmd


def test_pre_create_secondary_split_kills_placeholder_when_winner_changes_planned_pane(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    """Keep the winner case even when planning against a non-null (revive)
    identity: if the fresh mapping's slot 1 shows a DIFFERENT live pane id
    by publish time (a winner raced us), the CAS must still reject and kill
    our placeholder, leaving the winner's pane untouched."""
    from tmux_agents.commands import restore
    from tmux_agents import tmux, windows

    wt = tmp_path / "scripts"
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@1",
            project="scripts",
            branch=None,
            host_worktree=wt,
            pane_id="10",
            agents=[
                windows.AgentSlot(kind="claude", pane_id="10"),
                windows.AgentSlot(kind="codex", pane_id="20"),
            ],
        )
    )
    e = _dual_entry(wt, default_pane="10", secondary_pane="20")
    e = dataclasses.replace(e, kind="skip", secondary_action="revive")

    monkeypatch.setattr(tmux, "split_window", lambda *a, **k: "%77")
    kill_calls = []
    monkeypatch.setattr(tmux, "kill_pane", lambda target: kill_calls.append(target))

    real_update_mapping = windows.update_mapping

    def racy_update_mapping(window_id, fn):
        winner = windows.read_mapping(window_id)
        agents = list(winner.agents)
        agents[1] = dataclasses.replace(agents[1], pane_id="99")  # different live pane
        windows.write_mapping(dataclasses.replace(winner, agents=agents))
        return real_update_mapping(window_id, fn)

    monkeypatch.setattr(windows, "update_mapping", racy_update_mapping)

    result = restore._pre_create_secondary_split(e, "@1", "%10")

    assert result is None
    assert kill_calls == ["%77"]
    m = windows.read_mapping("@1")
    assert m.agents[1].pane_id == "99"  # winner's pane untouched


def test_pre_create_secondary_split_accepts_cleared_last_pane_id(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    """A completed cleanup sweep can clear `last_pane_id` back to `None`
    before this CAS runs, leaving the fresh slot as
    `(same kind, pane_id=None, last_pane_id=None)` — no `last_pane_id` left
    to match the planned pane id against. Since `pane_id is None` already
    means no LIVE winner exists to overwrite, the CAS must still accept
    this as a compatible dead-state progression rather than rejecting and
    killing the placeholder."""
    from tmux_agents.commands import restore
    from tmux_agents import tmux, windows

    wt = tmp_path / "scripts"
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@1",
            project="scripts",
            branch=None,
            host_worktree=wt,
            pane_id="10",
            agents=[
                windows.AgentSlot(kind="claude", pane_id="10"),
                windows.AgentSlot(kind="codex", pane_id="20"),
            ],
        )
    )
    e = _dual_entry(wt, default_pane="10", secondary_pane="20")
    e = dataclasses.replace(e, kind="skip", secondary_action="revive")

    monkeypatch.setattr(tmux, "split_window", lambda *a, **k: "%77")
    kill_calls = []
    monkeypatch.setattr(tmux, "kill_pane", lambda target: kill_calls.append(target))

    real_update_mapping = windows.update_mapping

    def racy_update_mapping(window_id, fn):
        # Simulate a completed cleanup sweep clearing last_pane_id entirely
        # (rather than state_tick._mark_secondary_dead setting it to the
        # planned pane id) between planning and this CAS.
        current = windows.read_mapping(window_id)
        agents = list(current.agents)
        agents[1] = dataclasses.replace(agents[1], pane_id=None, last_pane_id=None)
        windows.write_mapping(dataclasses.replace(current, agents=agents))
        return real_update_mapping(window_id, fn)

    monkeypatch.setattr(windows, "update_mapping", racy_update_mapping)

    result = restore._pre_create_secondary_split(e, "@1", "%10")

    assert result == "%77"
    assert kill_calls == []
    m = windows.read_mapping("@1")
    assert m.agents[1].pane_id == "77"
    assert m.agents[1].kind == "codex"


def test_pre_create_secondary_split_accepts_different_dead_lineage(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    """A different winner can appear (e.g. `agent-other` publishes a live
    secondary) and then itself die before this CAS runs, leaving the fresh
    slot as `(same kind, pane_id=None, last_pane_id="25")` where "25" is
    NOT the planned pane id ("20"). No live winner exists at CAS time, so
    the CAS must accept this dead lineage too — and activation must use
    the fresh slot's retained session id, not the planned one. Publication
    must also preserve the fresh slot's last_pane_id (25) so that dead
    pane's pending cleanup still happens."""
    from tmux_agents.commands import restore
    from tmux_agents import tmux, windows, provisioning

    wt = tmp_path / "scripts"
    planned_session_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    winner_session_id = "11111111-1111-1111-1111-111111111111"
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@1",
            project="scripts",
            branch=None,
            host_worktree=wt,
            pane_id="10",
            agents=[
                windows.AgentSlot(kind="claude", pane_id="10"),
                windows.AgentSlot(
                    kind="codex", pane_id="20", session_id=planned_session_id
                ),
            ],
        )
    )
    e = _dual_entry(wt, default_pane="10", secondary_pane="20")
    e = dataclasses.replace(
        e,
        kind="skip",
        secondary_action="revive",
        slots=[
            windows.AgentSlot(kind="claude", pane_id="10"),
            windows.AgentSlot(
                kind="codex", pane_id="20", session_id=planned_session_id
            ),
        ],
    )

    monkeypatch.setattr(tmux, "split_window", lambda *a, **k: "%77")
    monkeypatch.setattr(tmux, "kill_pane", lambda target: None)

    real_update_mapping = windows.update_mapping

    def racy_update_mapping(window_id, fn):
        # A different winner (pane "25") appeared and died before this CAS
        # runs, leaving a dead lineage that doesn't match the planned pane
        # id at all.
        current = windows.read_mapping(window_id)
        agents = list(current.agents)
        agents[1] = dataclasses.replace(
            agents[1],
            pane_id=None,
            last_pane_id="25",
            session_id=winner_session_id,
        )
        windows.write_mapping(dataclasses.replace(current, agents=agents))
        return real_update_mapping(window_id, fn)

    monkeypatch.setattr(windows, "update_mapping", racy_update_mapping)

    placeholders = restore.pre_create_windows([e], live_panes={"@1": {"%10"}})
    assert placeholders["@1"].secondary_pane_id == "%77"

    m = windows.read_mapping("@1")
    assert m.agents[1].pane_id == "77"
    assert m.agents[1].kind == "codex"
    # last_pane_id from the fresh (dead-winner) slot survives publication —
    # that pane's own pending cleanup still needs to happen.
    assert m.agents[1].last_pane_id == "25"
    assert m.agents[1].session_id == winner_session_id

    # Activation resumes the fresh (dead-winner) session id, not the
    # planned one.
    respawn_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tmux,
        "respawn_pane",
        lambda pane, *, command: respawn_calls.append((pane, command)),
    )
    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **kw: True)

    projs = _load_test_projects(projects_file)
    restore.execute_plan([e], placeholders, projs)

    secondary_calls = [c for c in respawn_calls if c[0] == "%77"]
    assert len(secondary_calls) == 1
    _, cmd = secondary_calls[0]
    assert winner_session_id in cmd
    assert planned_session_id not in cmd


def test_pre_create_repair_splits_off_live_default_pane(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    """skip + revive-secondary (repair): splits off the LIVE default pane,
    touching nothing about the healthy default itself."""
    from tmux_agents import tmux, windows
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts"
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@1",
            project="scripts",
            branch=None,
            host_worktree=wt,
            pane_id="10",
            agents=[
                windows.AgentSlot(kind="claude", pane_id="10"),
                windows.AgentSlot(kind="codex", pane_id=None),
            ],
        )
    )
    e = _dual_entry(wt, default_pane="10", secondary_pane=None)
    e = dataclasses.replace(e, kind="skip", secondary_action="revive")

    split_calls = []

    def fake_split(
        target, *, percent, command, before=False, horizontal=False, full_size=False
    ):
        split_calls.append({"target": target, "horizontal": horizontal})
        return "%77"

    monkeypatch.setattr(tmux, "split_window", fake_split)
    new_window_calls = []
    monkeypatch.setattr(
        tmux, "new_window", lambda *a, **k: (new_window_calls.append(1), "@nope")[1]
    )

    placeholders = restore.pre_create_windows([e], live_panes={"@1": {"%10"}})

    assert new_window_calls == []  # default untouched, no new window
    assert "@1" in placeholders
    ph = placeholders["@1"]
    assert ph.pane_id is None  # nothing pending for the healthy default
    assert ph.secondary_pane_id == "%77"
    assert split_calls == [{"target": "%10", "horizontal": True}]

    m = windows.read_mapping("@1")
    assert m.agents[0].pane_id == "10"  # untouched
    assert m.agents[1].pane_id == "77"
    assert m.agents[1].kind == "codex"


def test_activate_builds_resume_command_for_codex_secondary(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    """Activation: per slot, exec_cmd.build(kind=slot.kind, session_id=
    slot.session_id) — a codex secondary must get ` resume <id>`, not
    claude's ` --resume <id>`."""
    from tmux_agents import windows, tmux, provisioning
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts"
    e = restore.Entry(
        window_id="@1",
        project="scripts",
        branch=None,
        host_worktree=wt,
        pane_id="10",
        claude_session_id=None,
        window_index=1,
        kind="skip",
        secondary_action="revive",
        slots=[
            windows.AgentSlot(kind="claude", pane_id="10"),
            windows.AgentSlot(
                kind="codex",
                pane_id="20",
                session_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
            ),
        ],
    )
    ph = restore.Placeholder(
        entry=e, new_window_id="@1", pane_id=None, secondary_pane_id="%20"
    )

    respawn_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tmux,
        "respawn_pane",
        lambda pane, *, command: respawn_calls.append((pane, command)),
    )
    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **kw: True)

    projs = _load_test_projects(projects_file)
    restore.execute_plan([e], {"@1": ph}, projs)

    assert len(respawn_calls) == 1
    pane, cmd = respawn_calls[0]
    assert pane == "%20"
    assert "resume" in cmd
    assert "cccccccc-cccc-cccc-cccc-cccccccccccc" in cmd
    assert "--resume" not in cmd  # codex spells resume as a subcommand


# ===== Task 16 fix: ensure-provisioned codex hooks during restore =====


def test_execute_ensures_codex_hooks_once_per_project_group(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file
):
    """Section 2's cheap idempotent codex ensure-provisioned check must run
    once per activated project group: `ensure_host()` for the host-only
    project, `ensure_container(name, user)` for the container project,
    after its container is up."""
    from tmux_agents import tmux, container
    from tmux_agents.commands import restore

    wt_api = tmp_path / "api"
    wt_scripts = tmp_path / "scripts"
    _write_snapshot(
        "@1", project="api", branch=None, host_worktree=wt_api, window_index=1
    )
    _write_snapshot(
        "@2", project="scripts", branch=None, host_worktree=wt_scripts, window_index=2
    )

    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    monkeypatch.setattr(tmux, "new_window", lambda s, *, name, command: f"@new-{name}")
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: f"%{wid[5:]}")
    monkeypatch.setattr(tmux, "respawn_pane", lambda pane, *, command: None)
    from tmux_agents import provisioning

    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **kw: True)
    monkeypatch.setattr(container, "current_name", lambda proj: None)
    monkeypatch.setattr(container, "ensure_up", lambda proj, *, up_cmd: "api-container")

    host_calls: list[int] = []
    container_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        codex_hooks, "ensure_host", lambda: host_calls.append(1) or True
    )
    monkeypatch.setattr(
        codex_hooks,
        "ensure_container",
        lambda name, user: container_calls.append((name, user)) or True,
    )

    projs = _load_test_projects(projects_file)
    plan = restore.plan_entries(live_panes={}, projects=projs)
    placeholders = restore.pre_create_windows(plan, live_panes={})
    restore.execute_plan(plan, placeholders, projs)

    assert host_calls == [1]  # once for the "scripts" host-only group
    assert container_calls == [("api-container", "vscode")]  # once for "api"


def test_execute_codex_hooks_failure_is_nonfatal(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, projects_file, caplog
):
    """A codex-hook ensure-provisioned failure during restore must not block
    the project's respawns — mirroring `agent-new`'s non-fatal `codex
    hooks` stage."""
    from tmux_agents import tmux
    from tmux_agents.commands import restore

    wt = tmp_path / "scripts"
    _write_snapshot(
        "@1", project="scripts", branch=None, host_worktree=wt, window_index=1
    )

    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    monkeypatch.setattr(tmux, "new_window", lambda s, *, name, command: "@new-1")
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%99")
    respawn_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tmux,
        "respawn_pane",
        lambda pane, *, command: respawn_calls.append((pane, command)),
    )
    from tmux_agents import provisioning

    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **kw: True)
    monkeypatch.setattr(
        codex_hooks,
        "ensure_host",
        lambda: (_ for _ in ()).throw(RuntimeError("codex boom")),
    )

    projs = _load_test_projects(projects_file)
    plan = restore.plan_entries(live_panes={}, projects=projs)
    placeholders = restore.pre_create_windows(plan, live_panes={})
    with caplog.at_level(logging.WARNING, logger="tmux_agents.commands.restore"):
        restore.execute_plan(plan, placeholders, projs)

    # Final claude respawn still happened despite the codex-hook failure.
    claude_respawns = [(pid, c) for pid, c in respawn_calls if "claude" in c]
    assert len(claude_respawns) == 1
    assert claude_respawns[0][0] == "%99"
    assert any("codex hook" in r.message for r in caplog.records)
