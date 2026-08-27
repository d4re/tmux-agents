from pathlib import Path
from types import SimpleNamespace

import pytest

from tmux_agents import (
    codex_hooks,
    gh_auth,
    paths,
    phase,
    pickers,
    ssh_forward,
    startup,
    tmux,
    container,
)
from tmux_agents import windows as windows_mod
from tmux_agents.commands import rebuild
from tmux_agents.config import Project


@pytest.fixture(autouse=True)
def _stub_codex_hooks(monkeypatch):
    """Default-stub codex_hooks so existing rebuild tests (which don't
    exercise codex-hook provisioning) don't perform real filesystem I/O,
    even against the conftest-isolated TMUX_AGENTS_CODEX_HOME. Tests that
    specifically exercise the ensure-provisioned call override this via
    their own `monkeypatch.setattr` later in the test body."""
    monkeypatch.setattr(codex_hooks, "ensure_host", lambda: True)
    monkeypatch.setattr(codex_hooks, "ensure_container", lambda name, user: True)


def _proj(
    name="webapp",
    *,
    devcontainer=False,
    container=None,
    up_cmd=None,
    up_cmd_explicit=None,
    user=None,
    forward_ssh_agent=True,
    share_gh_auth=True,
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
        share_gh_auth=share_gh_auth,
    )


def _affected(
    project="webapp",
    branch="feat-x",
    pane_id="23",
    letter="I",
    session_id=None,
    host_worktree="/wt",
    busy=None,
):
    m = windows_mod.WindowMapping(
        window_id="@7",
        project=project,
        branch=branch,
        host_worktree=Path(host_worktree),
        pane_id=pane_id,
        claude_session_id=session_id,
    )
    if busy is None:
        busy = letter in rebuild.BUSY_LETTERS
    return rebuild.Affected(
        mapping=m, window_name=f"{project}:{branch}", state_letter=letter, busy=busy
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


def test_gather_affected_busy_true_when_any_slot_letter_is_busy(monkeypatch):
    """A dual-slot window's busy flag must be true when ANY parsed slot
    letter is in BUSY_LETTERS — not just the combined (highest-priority)
    display letter. `I|R` (idle default + running secondary) must count as
    busy even though it's a mundane case where the combined letter (R)
    already happens to be busy too."""
    windows_mod.write_mapping(
        windows_mod.WindowMapping(
            window_id="@1",
            project="webapp",
            branch="a",
            host_worktree=Path("/wt"),
            pane_id="9",
        )
    )
    wins = [
        tmux.Window(id="@1", index=1, name="webapp:a", state_code="I|R"),
    ]
    by = rebuild._gather_affected(wins)
    assert by["webapp"][0].busy is True


def test_gather_affected_busy_true_even_when_combined_letter_is_not_busy(
    monkeypatch,
):
    """Regression guard: `combined_letter` picks the highest-*priority*
    letter (X > W > R > B > Z > I > S), not the "most urgent for the
    rebuild-busy check" letter. A window with an errored default slot (X)
    and a running secondary slot (R) combines to "X" for display — X is
    NOT in BUSY_LETTERS — but the window still has a genuinely busy live
    agent and must be flagged busy."""
    windows_mod.write_mapping(
        windows_mod.WindowMapping(
            window_id="@1",
            project="webapp",
            branch="a",
            host_worktree=Path("/wt"),
            pane_id="9",
        )
    )
    wins = [
        tmux.Window(id="@1", index=1, name="webapp:a", state_code="X|R"),
    ]
    by = rebuild._gather_affected(wins)
    assert by["webapp"][0].state_letter == "X"
    assert by["webapp"][0].busy is True


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
    gh_syncs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gh_auth,
        "maybe_sync_gh_auth",
        lambda c, u: gh_syncs.append((c, u)) or gh_auth.SyncResult("synced"),
    )
    return SimpleNamespace(respawns=respawns, states=states, gh_syncs=gh_syncs)


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


def test_worker_syncs_gh_auth_after_rebuild(monkeypatch, tmp_state_dir):
    io = _stub_worker_io(monkeypatch)
    monkeypatch.setattr(container, "rebuild", lambda proj, *, up_cmd, no_cache: "cid")
    proj = _proj(devcontainer=True, up_cmd="devcontainer up")
    rc = rebuild._run_worker(proj, [_affected(pane_id="23")], no_cache=False)
    assert rc == 0
    assert io.gh_syncs == [("cid", "vscode")]


def test_worker_skips_gh_auth_when_share_gh_auth_false(monkeypatch, tmp_state_dir):
    io = _stub_worker_io(monkeypatch)
    monkeypatch.setattr(container, "rebuild", lambda proj, *, up_cmd, no_cache: "cid")
    proj = _proj(devcontainer=True, up_cmd="devcontainer up", share_gh_auth=False)
    rc = rebuild._run_worker(proj, [_affected(pane_id="23")], no_cache=False)
    assert rc == 0
    assert io.gh_syncs == []


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


def test_worker_respawns_every_live_slot_kind_matched(monkeypatch, tmp_state_dir):
    """A dual-slot window (a claude default + a codex secondary side-by-side
    in the same window) must have BOTH panes re-execed after the container
    rebuild, each built from its own slot's kind template — not just the
    window's default slot."""
    io = _stub_worker_io(monkeypatch)
    monkeypatch.setattr(container, "rebuild", lambda proj, *, up_cmd, no_cache: "cid")
    proj = Project(
        name="webapp",
        repo=Path("/Users/me/dev/webapp"),
        exec_cmd="claude{resume_args}",
        codex_exec_cmd="codex{resume_args}",
        devcontainer=True,
        up_cmd="devcontainer up",
        up_cmd_explicit=True,
    )
    m = windows_mod.WindowMapping(
        window_id="@7",
        project="webapp",
        branch="feat-x",
        host_worktree=Path("/wt"),
        pane_id="23",
        agents=[
            windows_mod.AgentSlot(kind="claude", pane_id="23", session_id="s1"),
            windows_mod.AgentSlot(kind="codex", pane_id="24", session_id="s2"),
        ],
    )
    affected = [
        rebuild.Affected(
            mapping=m, window_name="webapp:feat-x", state_letter="I", busy=False
        )
    ]

    rc = rebuild._run_worker(proj, affected, no_cache=False)

    assert rc == 0
    final = io.respawns[-2:]
    assert ("%23", "claude --resume s1") in final
    assert ("%24", "codex resume s2") in final
    assert io.states[-2:] == [("23", phase.STARTING), ("24", phase.STARTING)]


def test_worker_skips_dead_secondary_slot(monkeypatch, tmp_state_dir):
    """A secondary slot with `pane_id=None` (dead/never-started) must be
    skipped entirely — no respawn attempted for a nonexistent pane."""
    io = _stub_worker_io(monkeypatch)
    monkeypatch.setattr(container, "rebuild", lambda proj, *, up_cmd, no_cache: "cid")
    proj = _proj(devcontainer=True, up_cmd="devcontainer up")
    m = windows_mod.WindowMapping(
        window_id="@7",
        project="webapp",
        branch="feat-x",
        host_worktree=Path("/wt"),
        pane_id="23",
        agents=[
            windows_mod.AgentSlot(kind="claude", pane_id="23"),
            windows_mod.AgentSlot(kind="codex", pane_id=None, session_id="s2"),
        ],
    )
    affected = [
        rebuild.Affected(
            mapping=m, window_name="webapp:feat-x", state_letter="I", busy=False
        )
    ]

    rc = rebuild._run_worker(proj, affected, no_cache=False)

    assert rc == 0
    panes_touched = {pane for pane, _ in io.respawns}
    assert panes_touched == {"%23"}


# ---- Task 16 fix: ensure-provisioned codex hooks after rebuild ----


def test_worker_ensures_codex_hooks_after_rebuild(monkeypatch, tmp_state_dir):
    """Section 2's cheap idempotent codex ensure-provisioned check must run
    once after the container rebuild, before re-exec'ing panes —
    `ensure_container(name, user)` for a container project (the only kind
    `agent-rebuild` supports)."""
    _stub_worker_io(monkeypatch)
    monkeypatch.setattr(
        container, "rebuild", lambda proj, *, up_cmd, no_cache: "cid-123"
    )
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        codex_hooks,
        "ensure_container",
        lambda name, user: calls.append((name, user)) or True,
    )
    monkeypatch.setattr(
        codex_hooks,
        "ensure_host",
        lambda: pytest.fail("must not be called for a container project"),
    )
    proj = _proj(devcontainer=True, up_cmd="devcontainer up")
    affected = [_affected(pane_id="23")]

    rc = rebuild._run_worker(proj, affected, no_cache=False)

    assert rc == 0
    assert calls == [("cid-123", "vscode")]


def test_worker_codex_hooks_failure_is_nonfatal(monkeypatch, tmp_state_dir):
    """A codex-hook ensure-provisioned failure during rebuild must not block
    resuming the project's agents — mirroring `agent-new`'s non-fatal
    `codex hooks` stage."""
    io = _stub_worker_io(monkeypatch)
    monkeypatch.setattr(container, "rebuild", lambda proj, *, up_cmd, no_cache: "cid")
    monkeypatch.setattr(
        codex_hooks,
        "ensure_container",
        lambda *a: (_ for _ in ()).throw(RuntimeError("codex boom")),
    )
    proj = _proj(devcontainer=True, up_cmd="devcontainer up")
    affected = [_affected(pane_id="23", session_id="sess-1")]

    rc = rebuild._run_worker(proj, affected, no_cache=False)

    assert rc == 0
    final = io.respawns[-1:]
    assert ("%23", "claude --resume sess-1") in final


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
    # Simulate being in the forked child: fork() returns 0, setsid/detach no-op.
    monkeypatch.setattr(rebuild.os, "fork", lambda: 0)
    monkeypatch.setattr(rebuild.os, "setsid", lambda: None)
    monkeypatch.setattr(startup, "_detach_stdio", lambda: None)
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


def test_main_worker_parent_detaches_without_running(
    monkeypatch, tmp_config_dir, tmp_state_dir
):
    """The fork parent must return 0 immediately, releasing run-shell's output
    pipe — otherwise tmux paints the worker's output (devcontainer up JSON)
    over the active pane in view mode until a key is pressed."""
    _write_projects(
        tmp_config_dir, '[webapp]\nrepo = "/r/webapp"\ndevcontainer = true\n'
    )
    monkeypatch.setattr(rebuild.os, "fork", lambda: 12345)  # parent path
    monkeypatch.setattr(
        rebuild,
        "_run_worker",
        lambda *a, **k: pytest.fail("parent must not run the worker"),
    )
    assert rebuild.main(["--worker", "--project", "webapp"]) == 0


# ---------------------------------------------------------------------------
# Sandbox backend: state-preserving recreate (docs/SANDBOX-MODE.md)
# ---------------------------------------------------------------------------


def _sandbox_proj(name="sbxproj", repo="/Users/me/dev/sbxproj"):
    return Project(
        name=name,
        repo=Path(repo),
        exec_cmd="claude{resume_args}",
        codex_exec_cmd="codex{resume_args}",
        sandbox=True,
    )


def _stub_sandbox(monkeypatch, *, present=True, created=True):
    """Record-order stub of the whole sandbox module surface the rebuild
    worker touches."""
    from tmux_agents import sandbox as sandbox_mod

    calls = SimpleNamespace(order=[])
    monkeypatch.setattr(sandbox_mod, "is_present", lambda n: present)
    monkeypatch.setattr(
        sandbox_mod, "export_state", lambda n: calls.order.append("export") or b"TAR"
    )
    monkeypatch.setattr(sandbox_mod, "ensure_daemon", lambda: None)
    monkeypatch.setattr(
        sandbox_mod, "recreate", lambda p: calls.order.append("recreate")
    )
    monkeypatch.setattr(
        sandbox_mod, "import_state", lambda n, b: calls.order.append("import")
    )
    monkeypatch.setattr(
        codex_hooks, "ensure_sandbox", lambda n: calls.order.append("hooks") or True
    )
    return calls


def test_sandbox_projects_are_rebuild_eligible():
    assert rebuild._eligible(_sandbox_proj()) is True


def test_sandbox_worker_export_recreate_import_resumes(monkeypatch, tmp_state_dir):
    """Happy path: export → rm → create → import → hooks; resume ids stay
    valid because the session files came along in the tar."""
    io = _stub_worker_io(monkeypatch)
    calls = _stub_sandbox(monkeypatch)
    proj = _sandbox_proj()
    affected = [_affected(project="sbxproj", pane_id="23", session_id="sess-1")]

    rc = rebuild._run_sandbox_worker(
        proj, affected, discard_state=False, no_cache=False
    )

    assert rc == 0
    assert calls.order == ["export", "recreate", "import", "hooks"]
    assert ("%23", "claude --resume sess-1") in io.respawns
    # Backup archive is deleted only after a successful import.
    assert not paths.sbx_rebuild_backup("sbxproj").exists()


def test_sandbox_worker_export_failure_aborts(monkeypatch, tmp_state_dir):
    """Never delete what couldn't be saved: export failure → no rm, panes
    show the failure + the --discard-state escape hatch."""
    from tmux_agents import sandbox as sandbox_mod

    _stub_worker_io(monkeypatch)
    _stub_sandbox(monkeypatch)

    def boom(n):
        raise sandbox_mod.SandboxError("tar exploded")

    monkeypatch.setattr(sandbox_mod, "export_state", boom)
    monkeypatch.setattr(
        sandbox_mod,
        "recreate",
        lambda p: pytest.fail("must not delete a sandbox whose state wasn't saved"),
    )
    texts = []
    monkeypatch.setattr(
        startup, "show_static_text", lambda pane, body: texts.append(body)
    )

    rc = rebuild._run_sandbox_worker(
        _sandbox_proj(),
        [_affected(project="sbxproj", pane_id="23")],
        discard_state=False,
        no_cache=False,
    )

    assert rc == 1
    assert any("--discard-state" in t for t in texts)


def test_sandbox_worker_import_failure_falls_back_fresh(monkeypatch, tmp_state_dir):
    """Import failure → fresh-but-working fallback: resume ids cleared,
    codex slot held on a login-required placeholder instead of launching
    codex into an auth error loop."""
    from tmux_agents import sandbox as sandbox_mod

    io = _stub_worker_io(monkeypatch)
    _stub_sandbox(monkeypatch)

    def boom(n, b):
        raise sandbox_mod.SandboxError("tar import exploded")

    monkeypatch.setattr(sandbox_mod, "import_state", boom)
    texts = []
    monkeypatch.setattr(
        startup, "show_static_text", lambda pane, body: texts.append((pane, body))
    )

    m = windows_mod.WindowMapping(
        window_id="@7",
        project="sbxproj",
        branch="feat-x",
        host_worktree=Path("/wt"),
        pane_id="23",
        claude_session_id="sess-1",
        agents=[
            windows_mod.AgentSlot(kind="claude", pane_id="23", session_id="sess-1"),
            windows_mod.AgentSlot(kind="codex", pane_id="30", session_id="c-sess"),
        ],
    )
    affected = [
        rebuild.Affected(
            mapping=m, window_name="sbxproj:feat-x", state_letter="I", busy=False
        )
    ]
    windows_mod.write_mapping(m)

    rc = rebuild._run_sandbox_worker(
        _sandbox_proj(), affected, discard_state=False, no_cache=False
    )

    assert rc == 0
    claude_respawns = [
        (p, c) for p, c in io.respawns if "claude" in c and "tail -F" not in c
    ]
    assert claude_respawns == [("%23", "claude")]  # no stale --resume
    assert any("codex login" in body for pane, body in texts if pane == "%30")
    assert ("30", phase.ERRORED) in io.states
    # The clearing must be PERSISTED: the held codex slot never respawns, so
    # a stale id left in the mapping would become `codex resume <stale>` on
    # the next restore (which only clears ids when IT recreated the sandbox).
    fresh = windows_mod.read_mapping("@7")
    assert all(s.session_id is None for s in fresh.agents)
    # The exported archive survives an import failure for manual recovery.
    assert paths.sbx_rebuild_backup("sbxproj").read_bytes() == b"TAR"


def test_sandbox_worker_discard_state_skips_export(monkeypatch, tmp_state_dir):
    from tmux_agents import sandbox as sandbox_mod

    _stub_worker_io(monkeypatch)
    calls = _stub_sandbox(monkeypatch)
    monkeypatch.setattr(
        sandbox_mod,
        "export_state",
        lambda n: pytest.fail("--discard-state must skip the export"),
    )

    rc = rebuild._run_sandbox_worker(
        _sandbox_proj(),
        [_affected(project="sbxproj", pane_id="23")],
        discard_state=True,
        no_cache=False,
    )

    assert rc == 0
    assert calls.order == ["recreate", "hooks"]


def test_main_worker_dispatches_sandbox_project(
    monkeypatch, tmp_config_dir, tmp_state_dir
):
    import os as _os

    repo = tmp_config_dir / "sbxrepo"
    repo.mkdir()
    _write_projects(tmp_config_dir, f'[sbxproj]\nrepo = "{repo}"\nsandbox = true\n')
    monkeypatch.setattr(_os, "fork", lambda: 0)
    monkeypatch.setattr(_os, "setsid", lambda: None)
    monkeypatch.setattr(startup, "_detach_stdio", lambda: None)
    monkeypatch.setattr(tmux, "list_windows", lambda s: [])
    seen = {}

    def fake_worker(proj, affected, *, discard_state, no_cache):
        seen.update(project=proj.name, discard_state=discard_state, no_cache=no_cache)
        return 0

    monkeypatch.setattr(rebuild, "_run_sandbox_worker", fake_worker)
    monkeypatch.setattr(
        rebuild,
        "_run_worker",
        lambda *a, **k: pytest.fail("container worker wrong for sandbox"),
    )

    rc = rebuild.main(["--worker", "--project", "sbxproj", "--discard-state"])

    assert rc == 0
    assert seen == {"project": "sbxproj", "discard_state": True, "no_cache": False}


def test_main_interactive_threads_discard_state(
    monkeypatch, tmp_config_dir, tmp_state_dir
):
    repo = tmp_config_dir / "sbxrepo"
    repo.mkdir()
    _write_projects(tmp_config_dir, f'[sbxproj]\nrepo = "{repo}"\nsandbox = true\n')
    monkeypatch.setattr(tmux, "list_windows", lambda s: [])
    fired = []
    monkeypatch.setattr(tmux, "run_shell_bg", lambda cmd: fired.append(cmd))

    rc = rebuild.main(["sbxproj", "--yes", "--discard-state"])

    assert rc == 0
    assert len(fired) == 1
    assert "--discard-state" in fired[0]
