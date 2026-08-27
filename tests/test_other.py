from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from tmux_agents import (
    tmux,
    windows as windows_mod,
)
from tmux_agents.commands import other as other_mod
from tmux_agents.windows import AgentSlot, WindowMapping

WINDOW = "@1"


def _write_projects(tmp_config_dir: Path, body: str) -> None:
    (tmp_config_dir / "projects.toml").write_text(body)


def _mapping(worktree: Path, *, agents=None, project="proj", branch="feat-x"):
    default_pane = agents[0].pane_id if agents else "1"
    return WindowMapping(
        window_id=WINDOW,
        project=project,
        branch=branch,
        host_worktree=worktree,
        pane_id=default_pane or "",
        agents=agents or [AgentSlot(kind="claude", pane_id="1")],
    )


def _write_mapping(m: WindowMapping) -> None:
    windows_mod.write_mapping(m)


def _stub(monkeypatch):
    """Monkeypatch the whole tmux/codex_hooks surface `agent-other` touches,
    recording every call so tests can assert on them (including order via
    `calls.order`)."""
    calls = SimpleNamespace(
        messages=[],
        selected=[],
        splits=[],
        respawned=[],
        killed=[],
        scrubbed=[],
        ensure_host=[],
        ensure_container=[],
        order=[],
    )
    monkeypatch.setattr(tmux, "display_message", lambda m: calls.messages.append(m))
    monkeypatch.setattr(tmux, "select_pane", lambda p: calls.selected.append(p))

    def fake_split(
        target, *, percent, command, before=False, horizontal=False, full_size=False
    ):
        calls.splits.append((target, percent, command, horizontal))
        calls.order.append(("split", target))
        return "%99"

    monkeypatch.setattr(tmux, "split_window", fake_split)

    def fake_respawn(pane_id, *, command):
        calls.respawned.append((pane_id, command))
        calls.order.append(("respawn", pane_id, command))

    monkeypatch.setattr(tmux, "respawn_pane", fake_respawn)
    monkeypatch.setattr(tmux, "kill_pane", lambda p: calls.killed.append(p))

    def fake_scrub(worktree, pane_id):
        calls.scrubbed.append((worktree, pane_id))

    monkeypatch.setattr(other_mod.startup, "scrub_pane_files", fake_scrub)
    monkeypatch.setattr(
        other_mod.codex_hooks, "ensure_host", lambda: calls.ensure_host.append(True)
    )
    monkeypatch.setattr(
        other_mod.codex_hooks,
        "ensure_container",
        lambda name, user: calls.ensure_container.append((name, user)),
    )
    monkeypatch.setattr(other_mod.shutil, "which", lambda exe: "/usr/bin/" + exe)
    return calls


def _live(monkeypatch, pane_ids):
    monkeypatch.setattr(tmux, "window_pane_map", lambda s: {WINDOW: set(pane_ids)})


# ===== Branch 1: no mapping =====


def test_no_mapping_displays_message_and_returns_zero(monkeypatch, tmp_config_dir):
    calls = _stub(monkeypatch)

    rc = other_mod.main(["--window-id", "@nope"])

    assert rc == 0
    assert calls.messages == ["agent-other: no agent window"]
    assert calls.splits == []


# ===== Branch 2: default slot dead =====


def test_default_dead_displays_restore_message_and_returns_zero(
    monkeypatch, tmp_config_dir, tmp_path
):
    worktree = tmp_path / "wt"
    _write_mapping(_mapping(worktree))
    calls = _stub(monkeypatch)
    _live(monkeypatch, [])  # default pane %1 is not live

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 0
    assert calls.messages == ["default agent down — Ctrl-Space R to restore"]
    assert calls.splits == []


# ===== Branch 3: secondary live -> focus jump =====


def test_focus_jump_active_default_selects_secondary(
    monkeypatch, tmp_config_dir, tmp_path
):
    worktree = tmp_path / "wt"
    agents = [
        AgentSlot(kind="claude", pane_id="1"),
        AgentSlot(kind="codex", pane_id="2"),
    ]
    _write_mapping(_mapping(worktree, agents=agents))
    calls = _stub(monkeypatch)
    _live(monkeypatch, ["%1", "%2"])
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%1")

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 0
    assert calls.selected == ["%2"]
    assert calls.splits == []


def test_focus_jump_active_secondary_selects_default(
    monkeypatch, tmp_config_dir, tmp_path
):
    worktree = tmp_path / "wt"
    agents = [
        AgentSlot(kind="claude", pane_id="1"),
        AgentSlot(kind="codex", pane_id="2"),
    ]
    _write_mapping(_mapping(worktree, agents=agents))
    calls = _stub(monkeypatch)
    _live(monkeypatch, ["%1", "%2"])
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%2")

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 0
    assert calls.selected == ["%1"]
    assert calls.splits == []


def test_focus_jump_active_neither_selects_secondary(
    monkeypatch, tmp_config_dir, tmp_path
):
    worktree = tmp_path / "wt"
    agents = [
        AgentSlot(kind="claude", pane_id="1"),
        AgentSlot(kind="codex", pane_id="2"),
    ]
    _write_mapping(_mapping(worktree, agents=agents))
    calls = _stub(monkeypatch)
    _live(monkeypatch, ["%1", "%2"])
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%3")  # e.g. overview pane

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 0
    assert calls.selected == ["%2"]


def test_focus_jump_pane_died_mid_jump_shows_notice_not_traceback(
    monkeypatch, tmp_config_dir, tmp_path
):
    """A TmuxError from `select_pane` (e.g. the target pane died between the
    liveness check and the jump) must surface as a friendly `_notice`, not
    propagate as an unhandled traceback."""
    worktree = tmp_path / "wt"
    agents = [
        AgentSlot(kind="claude", pane_id="1"),
        AgentSlot(kind="codex", pane_id="2"),
    ]
    _write_mapping(_mapping(worktree, agents=agents))
    calls = _stub(monkeypatch)
    _live(monkeypatch, ["%1", "%2"])
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%1")

    def boom(pane_id):
        raise tmux.TmuxError(
            1,
            ["tmux", "-L", "agents", "select-pane"],
            output="",
            stderr="can't find pane: %2",
        )

    monkeypatch.setattr(tmux, "select_pane", boom)

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 0
    assert any("focus-jump failed" in m for m in calls.messages)


# ===== Branch 4: start (fresh append) =====


def test_start_fresh_appends_codex_slot_after_respawn(
    monkeypatch, tmp_config_dir, tmp_path
):
    repo = tmp_path / "proj"
    repo.mkdir()
    worktree = repo / ".worktrees" / "feat-x"
    worktree.mkdir(parents=True)
    _write_projects(tmp_config_dir, f'[proj]\nrepo = "{repo}"\n')
    _write_mapping(_mapping(worktree))
    calls = _stub(monkeypatch)
    _live(monkeypatch, ["%1"])

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 0
    assert len(calls.splits) == 1
    split_target, percent, _placeholder_cmd, horizontal = calls.splits[0]
    assert split_target == "%1"
    assert percent == 50
    assert horizontal is True

    assert len(calls.respawned) == 1
    pane_id, cmd = calls.respawned[0]
    assert pane_id == "%99"
    assert "exec codex" in cmd
    assert " resume" not in cmd  # fresh start, no retained session

    updated = windows_mod.read_mapping(WINDOW)
    assert len(updated.agents) == 2
    assert updated.agents[1].kind == "codex"
    assert updated.agents[1].pane_id == "99"

    # scrub happened for the newly split pane before respawn.
    assert (worktree, "99") in calls.scrubbed


def test_publish_happens_after_respawn_call_order(
    monkeypatch, tmp_config_dir, tmp_path
):
    repo = tmp_path / "proj"
    repo.mkdir()
    worktree = repo / ".worktrees" / "feat-x"
    worktree.mkdir(parents=True)
    _write_projects(tmp_config_dir, f'[proj]\nrepo = "{repo}"\n')
    _write_mapping(_mapping(worktree))
    _stub(monkeypatch)
    _live(monkeypatch, ["%1"])

    order = []
    real_update_mapping = windows_mod.update_mapping

    def spy_update_mapping(window_id, fn):
        order.append("publish")
        return real_update_mapping(window_id, fn)

    monkeypatch.setattr(windows_mod, "update_mapping", spy_update_mapping)

    real_respawn = tmux.respawn_pane

    def spy_respawn(pane_id, *, command):
        order.append("respawn")
        return real_respawn(pane_id, command=command)

    monkeypatch.setattr(tmux, "respawn_pane", spy_respawn)

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 0
    assert order == ["respawn", "publish"]


# ===== Branch 4: revive dead secondary =====


def test_revive_dead_secondary_uses_resume_args(monkeypatch, tmp_config_dir, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    worktree = repo / ".worktrees" / "feat-x"
    worktree.mkdir(parents=True)
    _write_projects(tmp_config_dir, f'[proj]\nrepo = "{repo}"\n')
    agents = [
        AgentSlot(kind="claude", pane_id="1"),
        AgentSlot(kind="codex", pane_id=None, session_id="sess-abc123"),
    ]
    _write_mapping(_mapping(worktree, agents=agents))
    calls = _stub(monkeypatch)
    _live(monkeypatch, ["%1"])  # secondary pane is dead/absent from live set

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 0
    assert len(calls.respawned) == 1
    _, cmd = calls.respawned[0]
    assert "exec codex" in cmd
    assert "resume sess-abc123" in cmd

    updated = windows_mod.read_mapping(WINDOW)
    assert len(updated.agents) == 2
    assert updated.agents[1].pane_id == "99"
    assert updated.agents[1].session_id == "sess-abc123"


# ===== Rollback: respawn failure =====


def test_respawn_failure_kills_pane_scrubs_files_mapping_unchanged(
    monkeypatch, tmp_config_dir, tmp_path
):
    repo = tmp_path / "proj"
    repo.mkdir()
    worktree = repo / ".worktrees" / "feat-x"
    worktree.mkdir(parents=True)
    _write_projects(tmp_config_dir, f'[proj]\nrepo = "{repo}"\n')
    before = _mapping(worktree)
    _write_mapping(before)
    calls = _stub(monkeypatch)
    _live(monkeypatch, ["%1"])

    def failing_respawn(pane_id, *, command):
        raise RuntimeError("respawn boom")

    monkeypatch.setattr(tmux, "respawn_pane", failing_respawn)

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 1
    assert calls.killed == ["%99"]
    assert (worktree, "99") in calls.scrubbed

    after = windows_mod.read_mapping(WINDOW)
    assert after.to_dict() == before.to_dict()


# ===== Rollback: publication failure =====


def test_publication_failure_kills_pane_scrubs_files_mapping_unchanged(
    monkeypatch, tmp_config_dir, tmp_path
):
    repo = tmp_path / "proj"
    repo.mkdir()
    worktree = repo / ".worktrees" / "feat-x"
    worktree.mkdir(parents=True)
    _write_projects(tmp_config_dir, f'[proj]\nrepo = "{repo}"\n')
    before = _mapping(worktree)
    _write_mapping(before)
    calls = _stub(monkeypatch)
    _live(monkeypatch, ["%1"])

    def failing_update_mapping(window_id, fn):
        raise RuntimeError("publish boom")

    monkeypatch.setattr(windows_mod, "update_mapping", failing_update_mapping)

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 1
    assert calls.killed == ["%99"]
    assert (worktree, "99") in calls.scrubbed

    after = windows_mod.read_mapping(WINDOW)
    assert after.to_dict() == before.to_dict()


# ===== Start-vs-start race =====


def test_start_vs_start_loser_focus_jumps_no_second_split(
    monkeypatch, tmp_config_dir, tmp_path
):
    repo = tmp_path / "proj"
    repo.mkdir()
    worktree = repo / ".worktrees" / "feat-x"
    worktree.mkdir(parents=True)
    _write_projects(tmp_config_dir, f'[proj]\nrepo = "{repo}"\n')
    dead = _mapping(worktree)  # secondary absent
    winner = _mapping(
        worktree,
        agents=[
            AgentSlot(kind="claude", pane_id="1"),
            AgentSlot(kind="codex", pane_id="55"),
        ],
    )
    _write_mapping(dead)
    calls = _stub(monkeypatch)
    # The winner's freshly-created pane %55 is genuinely alive by the time
    # the loser re-checks under the lock.
    _live(monkeypatch, ["%1", "%55"])
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%1")

    n = {"count": 0}
    real_read_mapping = windows_mod.read_mapping

    def fake_read_mapping(window_id):
        n["count"] += 1
        if n["count"] == 1:
            return dead
        return winner

    monkeypatch.setattr(windows_mod, "read_mapping", fake_read_mapping)

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 0
    assert calls.splits == []
    assert calls.selected == ["%55"]

    monkeypatch.setattr(windows_mod, "read_mapping", real_read_mapping)


# ===== Lock discipline: provisioning/pre-flight run before the lock =====


def test_provisioning_and_preflight_run_before_cleanup_lock(
    monkeypatch, tmp_config_dir, tmp_path
):
    """`ensure_host`/`ensure_container` (docker execs) and the executable
    pre-flight are idempotent and touch no worktree state — they must run
    BEFORE the per-worktree cleanup lock is acquired, so the lock's hold
    time covers only re-read -> split -> scrub -> respawn -> publish (and so
    `codex-hooks.lock`, held inside `ensure_host`, never nests inside
    `worktree_cleanup_lock`)."""
    repo = tmp_path / "proj"
    repo.mkdir()
    worktree = repo / ".worktrees" / "feat-x"
    worktree.mkdir(parents=True)
    _write_projects(tmp_config_dir, f'[proj]\nrepo = "{repo}"\n')
    _write_mapping(_mapping(worktree))
    _stub(monkeypatch)
    _live(monkeypatch, ["%1"])

    order: list[str] = []
    monkeypatch.setattr(
        other_mod.codex_hooks, "ensure_host", lambda: order.append("ensure_host")
    )
    monkeypatch.setattr(
        other_mod.shutil,
        "which",
        lambda exe: order.append("which") or "/usr/bin/" + exe,
    )

    @contextmanager
    def fake_locked(path):
        order.append("lock_enter")
        try:
            yield
        finally:
            order.append("lock_exit")

    monkeypatch.setattr(other_mod.locks, "locked", fake_locked)

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 0
    assert "ensure_host" in order and "which" in order and "lock_enter" in order
    assert order.index("ensure_host") < order.index("lock_enter")
    assert order.index("which") < order.index("lock_enter")


# ===== Custom exec command skips which-preflight =====


def test_custom_codex_exec_cmd_skips_which_preflight(
    monkeypatch, tmp_config_dir, tmp_path
):
    repo = tmp_path / "proj"
    repo.mkdir()
    worktree = repo / ".worktrees" / "feat-x"
    worktree.mkdir(parents=True)
    _write_projects(
        tmp_config_dir,
        f'[proj]\nrepo = "{repo}"\ncodex_exec_cmd = "cd {{workdir}} && exec my-codex{{resume_args}}"\n',
    )
    _write_mapping(_mapping(worktree))
    calls = _stub(monkeypatch)
    _live(monkeypatch, ["%1"])

    def boom(exe):
        raise AssertionError("shutil.which should not be called for a custom template")

    monkeypatch.setattr(other_mod.shutil, "which", boom)

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 0
    assert len(calls.respawned) == 1
    _, cmd = calls.respawned[0]
    assert "exec my-codex" in cmd


def test_missing_executable_notice_no_split(monkeypatch, tmp_config_dir, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    worktree = repo / ".worktrees" / "feat-x"
    worktree.mkdir(parents=True)
    _write_projects(tmp_config_dir, f'[proj]\nrepo = "{repo}"\n')
    _write_mapping(_mapping(worktree))
    calls = _stub(monkeypatch)
    _live(monkeypatch, ["%1"])
    monkeypatch.setattr(other_mod.shutil, "which", lambda exe: None)

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 0
    assert calls.splits == []
    assert any("codex" in m for m in calls.messages)


def test_project_not_in_toml_returns_error(monkeypatch, tmp_config_dir, tmp_path):
    worktree = tmp_path / "wt"
    _write_projects(tmp_config_dir, "")
    _write_mapping(_mapping(worktree, project="ghost"))
    calls = _stub(monkeypatch)
    _live(monkeypatch, ["%1"])

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 1
    assert any("ghost" in m for m in calls.messages)


def test_container_project_uses_docker_exec_preflight_and_ensure_container(
    monkeypatch, tmp_config_dir, tmp_path
):
    repo = tmp_path / "svc"
    repo.mkdir()
    worktree = repo / ".worktrees" / "feat-x"
    worktree.mkdir(parents=True)
    _write_projects(
        tmp_config_dir,
        f'[svc]\nrepo = "{repo}"\ncontainer = "svc-dev"\nup_cmd = "true"\n',
    )
    _write_mapping(_mapping(worktree, project="svc"))
    calls = _stub(monkeypatch)
    _live(monkeypatch, ["%1"])
    monkeypatch.setattr(other_mod.container, "current_name", lambda proj: "svc-dev")
    monkeypatch.setattr(
        other_mod.container,
        "exec_capture",
        lambda name, user, script, **kw: "/usr/bin/codex\n",
    )

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 0
    assert calls.ensure_container == [("svc-dev", "vscode")]
    assert len(calls.respawned) == 1


def test_malformed_projects_toml_logs_warning_and_fails_gracefully(
    monkeypatch, tmp_config_dir, tmp_path
):
    """Malformed projects.toml should log a warning and exit gracefully."""
    worktree = tmp_path / "wt"
    # Write invalid TOML (missing closing bracket)
    _write_projects(tmp_config_dir, "[proj\nrepo = /invalid")
    _write_mapping(_mapping(worktree))
    calls = _stub(monkeypatch)
    _live(monkeypatch, ["%1"])

    # Track logger.warning calls
    warnings = []
    original_warning = other_mod.logger.warning

    def spy_warning(*args, **kwargs):
        warnings.append(args)
        return original_warning(*args, **kwargs)

    monkeypatch.setattr(other_mod.logger, "warning", spy_warning)

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 1
    assert any("proj" in m for m in calls.messages)
    assert any("projects.toml load failed" in str(w[0]) for w in warnings)


# ===== Stale secondary: recorded pane_id whose pane already exited =====


def test_stale_secondary_pane_routes_to_revive_not_focus_jump(
    monkeypatch, tmp_config_dir, tmp_path
):
    """A secondary that just exited keeps its pane_id until the next tick
    nulls it. agent-other must treat recorded-but-not-live as dead: revive
    (resuming the freshest disk session id) instead of erroring on a
    focus-jump to a nonexistent pane."""
    repo = tmp_path / "proj"
    repo.mkdir()
    worktree = repo / ".worktrees" / "feat-x"
    d = worktree / ".local" / ".tmux-agents"
    d.mkdir(parents=True)
    _write_projects(tmp_config_dir, f'[proj]\nrepo = "{repo}"\n')
    agents = [
        AgentSlot(kind="claude", pane_id="1"),
        AgentSlot(
            kind="codex",
            pane_id="15",
            session_id="aaaaaaaa-1111-2222-3333-444444444444",
        ),
    ]
    _write_mapping(_mapping(worktree, agents=agents))
    # Disk carries a NEWER id than the mapping (captured after the last merge).
    (d / "session-15.id").write_text("bbbbbbbb-1111-2222-3333-444444444444\n")
    (d / "state-15.json").write_text('{"phase":"idle"}')
    calls = _stub(monkeypatch)
    _live(monkeypatch, ["%1"])  # pane %15 is NOT alive

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 0
    assert calls.selected == []  # never tried to focus-jump
    assert len(calls.respawned) == 1
    _, cmd = calls.respawned[0]
    assert "resume bbbbbbbb-1111-2222-3333-444444444444" in cmd  # disk id wins

    updated = windows_mod.read_mapping(WINDOW)
    assert updated.agents[1].pane_id == "99"
    assert updated.agents[1].session_id == "bbbbbbbb-1111-2222-3333-444444444444"
    # The exited pane's files were scrubbed inline (no cleanup pointer existed).
    assert (worktree, "15") in calls.scrubbed


# ===== Sandbox backend =====


def _sandbox_setup(monkeypatch, tmp_config_dir, tmp_path):
    repo = tmp_path / "svc"
    repo.mkdir()
    worktree = repo / ".worktrees" / "feat-x"
    worktree.mkdir(parents=True)
    _write_projects(tmp_config_dir, f'[svc]\nrepo = "{repo}"\nsandbox = true\n')
    _write_mapping(_mapping(worktree, project="svc"))
    calls = _stub(monkeypatch)
    _live(monkeypatch, ["%1"])
    return calls


def test_sandbox_project_probes_in_sandbox_not_host(
    monkeypatch, tmp_config_dir, tmp_path
):
    """Executable preflight for a sandbox project runs `command -v` via
    sandbox.exec_capture — never shutil.which on the host, never docker."""
    import pytest as _pytest

    from tmux_agents import sandbox as sandbox_mod

    calls = _sandbox_setup(monkeypatch, tmp_config_dir, tmp_path)
    probes = []
    monkeypatch.setattr(
        sandbox_mod,
        "exec_capture",
        lambda name, script, **kw: probes.append((name, script)) or "found\n",
    )
    monkeypatch.setattr(
        other_mod.shutil,
        "which",
        lambda exe: _pytest.fail("host which() must not run for sandbox"),
    )
    ensured = []
    monkeypatch.setattr(
        other_mod.codex_hooks, "ensure_sandbox", lambda name: ensured.append(name)
    )

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 0
    assert ensured == ["svc"]
    assert calls.ensure_host == [] and calls.ensure_container == []
    assert probes and probes[0][0] == "svc" and "command -v codex" in probes[0][1]
    assert len(calls.respawned) == 1
    assert calls.respawned[0][1].startswith("sbx exec")


def test_sandbox_hook_provisioning_failure_is_notice(
    monkeypatch, tmp_config_dir, tmp_path
):
    from tmux_agents import sandbox as sandbox_mod

    calls = _sandbox_setup(monkeypatch, tmp_config_dir, tmp_path)

    def boom(name):
        raise sandbox_mod.SandboxError(sandbox_mod.DAEMON_HINT)

    monkeypatch.setattr(other_mod.codex_hooks, "ensure_sandbox", boom)

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 0  # notice, not crash — nothing was created
    assert calls.splits == []
    assert any("daemon" in m for m in calls.messages)


def test_sandbox_daemon_down_during_preflight_surfaces_hint(
    monkeypatch, tmp_config_dir, tmp_path
):
    """A hint-classified SandboxError (daemon/login) from the preflight must
    surface its remediation, not read as 'codex not found on sandbox'."""
    from tmux_agents import sandbox as sandbox_mod

    calls = _sandbox_setup(monkeypatch, tmp_config_dir, tmp_path)
    monkeypatch.setattr(other_mod.codex_hooks, "ensure_sandbox", lambda name: True)

    def boom(name, script, **kw):
        raise sandbox_mod.SandboxError(sandbox_mod.DAEMON_HINT)

    monkeypatch.setattr(sandbox_mod, "exec_capture", boom)

    rc = other_mod.main(["--window-id", WINDOW])

    assert calls.splits == []
    assert rc == 1
    assert any("daemon" in m for m in calls.messages)


def test_sandbox_missing_exe_is_notice(monkeypatch, tmp_config_dir, tmp_path):
    from tmux_agents import sandbox as sandbox_mod

    calls = _sandbox_setup(monkeypatch, tmp_config_dir, tmp_path)
    monkeypatch.setattr(other_mod.codex_hooks, "ensure_sandbox", lambda name: True)
    # The probe script always exits 0 and answers via stdout — "missing"
    # is the only shape that may read as not-found.
    monkeypatch.setattr(
        sandbox_mod, "exec_capture", lambda name, script, **kw: "missing\n"
    )

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 0
    assert calls.splits == []
    assert any("not found on sandbox" in m for m in calls.messages)


def test_sandbox_transport_failure_not_misread_as_missing_exe(
    monkeypatch, tmp_config_dir, tmp_path
):
    """A non-exit failure (sbx binary missing, timeout, deleted sandbox)
    must surface its own message, never the misleading 'not found'."""
    from tmux_agents import sandbox as sandbox_mod

    calls = _sandbox_setup(monkeypatch, tmp_config_dir, tmp_path)
    monkeypatch.setattr(other_mod.codex_hooks, "ensure_sandbox", lambda name: True)

    def boom(name, script, **kw):
        raise sandbox_mod.SandboxError(sandbox_mod.INSTALL_HINT)

    monkeypatch.setattr(sandbox_mod, "exec_capture", boom)

    rc = other_mod.main(["--window-id", WINDOW])

    assert rc == 1
    assert calls.splits == []
    assert not any("not found" in m for m in calls.messages)
    assert any("not installed" in m for m in calls.messages)
