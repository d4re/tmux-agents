from pathlib import Path
from tmux_agents import windows, paths, tmux as tmux_mod
from tmux_agents.windows import AgentSlot, WindowMapping


def test_write_and_read_mapping(tmp_config_dir, tmp_path):
    wt = tmp_path / "repo" / ".worktrees" / "feat-x"
    m = windows.WindowMapping(
        window_id="@5",
        project="api",
        branch="feat-x",
        host_worktree=wt,
        pane_id="23",
    )
    windows.write_mapping(m)
    assert paths.window_mapping_file("@5").exists()
    back = windows.read_mapping("@5")
    assert back == m


def test_read_mapping_missing_returns_none(tmp_config_dir):
    assert windows.read_mapping("@999") is None


def test_write_mapping_creates_dir(tmp_config_dir, tmp_path):
    # windows_dir must not already exist
    assert not paths.windows_dir().exists()
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@9",
            project="p",
            branch=None,
            host_worktree=tmp_path,
            pane_id="0",
        )
    )
    assert paths.windows_dir().is_dir()


def test_read_mapping_tolerates_branch_none(tmp_config_dir, tmp_path):
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@7",
            project="scripts",
            branch=None,
            host_worktree=tmp_path,
            pane_id="3",
        )
    )
    m = windows.read_mapping("@7")
    assert m.branch is None
    assert m.pane_id == "3"


def test_mapping_with_session_id_and_index_roundtrips(tmp_config_dir, tmp_path):
    m = windows.WindowMapping(
        window_id="@5",
        project="api",
        branch="feat-x",
        host_worktree=tmp_path,
        pane_id="23",
        claude_session_id="01HK1234567890abcdef1234567890ab",
        window_index=3,
    )
    windows.write_mapping(m)
    back = windows.read_mapping("@5")
    assert back == m


def test_mapping_without_session_id_or_index_defaults_to_none(tmp_config_dir, tmp_path):
    # Old callers that don't pass the new fields still work.
    m = windows.WindowMapping(
        window_id="@5",
        project="api",
        branch=None,
        host_worktree=tmp_path,
        pane_id="23",
    )
    windows.write_mapping(m)
    back = windows.read_mapping("@5")
    assert back.claude_session_id is None
    assert back.window_index is None


def test_live_branches_for_filters_by_project_and_liveness(
    tmp_config_dir,
    monkeypatch,
):
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@1",
            project="api",
            branch="feat/foo",
            host_worktree=tmp_config_dir / "wt1",
            pane_id="1",
        )
    )
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@2",
            project="api",
            branch="fix-bar",
            host_worktree=tmp_config_dir / "wt2",
            pane_id="2",
        )
    )
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@3",
            project="scripts",
            branch="feat/foo",
            host_worktree=tmp_config_dir / "wt3",
            pane_id="3",
        )
    )
    # @1 and @3 are live; @2 is a stale mapping (no tmux window for it).
    monkeypatch.setattr(
        tmux_mod,
        "list_windows",
        lambda s: [
            tmux_mod.Window(id="@1", index=1, name="api:feat/foo"),
            tmux_mod.Window(id="@3", index=2, name="scripts:feat/foo"),
        ],
    )

    assert windows.live_branches_for("api") == {"feat/foo"}
    assert windows.live_branches_for("scripts") == {"feat/foo"}
    assert windows.live_branches_for("nonexistent") == set()


def test_live_branches_for_ignores_none_branch(tmp_config_dir, monkeypatch):
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@1",
            project="api",
            branch=None,
            host_worktree=tmp_config_dir / "wt1",
            pane_id="1",
        )
    )
    monkeypatch.setattr(
        tmux_mod,
        "list_windows",
        lambda s: [
            tmux_mod.Window(id="@1", index=1, name="api"),
        ],
    )
    assert windows.live_branches_for("api") == set()


def test_live_branches_for_handles_missing_windows_dir(
    tmp_config_dir,
    monkeypatch,
):
    monkeypatch.setattr(tmux_mod, "list_windows", lambda s: [])
    assert windows.live_branches_for("api") == set()


def test_mapping_round_trips_phase_hint(tmp_config_dir):
    from tmux_agents import windows

    m = windows.WindowMapping(
        window_id="@7",
        project="api",
        branch="feat-x",
        host_worktree=Path("/repo"),
        pane_id="23",
        phase_hint="starting",
    )
    windows.write_mapping(m)
    got = windows.read_mapping("@7")
    assert got is not None
    assert got.phase_hint == "starting"


def test_mapping_omits_phase_hint_when_unset(tmp_config_dir):
    from tmux_agents import windows

    m = windows.WindowMapping(
        window_id="@8",
        project="api",
        branch=None,
        host_worktree=Path("/repo"),
        pane_id="23",
    )
    assert "phase_hint" not in m.to_dict()
    windows.write_mapping(m)
    assert windows.read_mapping("@8").phase_hint is None


def test_from_dict_synthesizes_claude_slot_from_legacy():
    m = WindowMapping.from_dict(
        "@1",
        {
            "project": "p",
            "branch": None,
            "host_worktree": "/r",
            "pane_id": "12",
            "claude_session_id": "s-1",
        },
    )
    assert m.agents == [AgentSlot(kind="claude", pane_id="12", session_id="s-1")]
    assert m.schema == 2


def test_roundtrip_two_slots():
    m = WindowMapping(
        window_id="@1",
        project="p",
        branch="b",
        host_worktree=Path("/r"),
        pane_id="12",
        agents=[
            AgentSlot(kind="claude", pane_id="12", session_id="s-1"),
            AgentSlot(kind="codex", pane_id=None, session_id="c-1", last_pane_id="15"),
        ],
    )
    d = m.to_dict()
    assert d["schema"] == 2
    m2 = WindowMapping.from_dict("@1", d)
    assert m2.agents == m.agents


def test_to_dict_mirrors_slot0_for_downgrade():
    m = WindowMapping(
        window_id="@1",
        project="p",
        branch=None,
        host_worktree=Path("/r"),
        pane_id="12",
        agents=[AgentSlot(kind="claude", pane_id="12", session_id="s-1")],
    )
    d = m.to_dict()
    assert d["pane_id"] == "12"
    assert d["claude_session_id"] == "s-1"


def test_to_dict_never_mirrors_codex_session_as_claude():
    m = WindowMapping(
        window_id="@1",
        project="p",
        branch=None,
        host_worktree=Path("/r"),
        pane_id="12",
        agents=[AgentSlot(kind="codex", pane_id="12", session_id="c-1")],
    )
    d = m.to_dict()
    assert d["pane_id"] == "12"
    assert "claude_session_id" not in d


def test_slot_accessors():
    a = AgentSlot(kind="claude", pane_id="12")
    b = AgentSlot(kind="codex", pane_id="15")
    m = WindowMapping(
        window_id="@1",
        project="p",
        branch=None,
        host_worktree=Path("/r"),
        pane_id="12",
        agents=[a, b],
    )
    assert m.default_slot == a
    assert m.secondary_slot == b
    m1 = WindowMapping(
        window_id="@1",
        project="p",
        branch=None,
        host_worktree=Path("/r"),
        pane_id="12",
        agents=[a],
    )
    assert m1.secondary_slot is None


def test_forget_scrubs_every_slot_and_pending_pointer(tmp_path, tmp_state_dir):
    wt = tmp_path / "repo"
    d = wt / ".local" / ".tmux-agents"
    d.mkdir(parents=True)
    for pane in ("12", "15", "20"):
        (d / f"state-{pane}.json").write_text("{}")
        (d / f"session-{pane}.id").write_text("x")
    windows.write_mapping(
        WindowMapping(
            window_id="@1",
            project="p",
            branch=None,
            host_worktree=wt,
            pane_id="12",
            agents=[
                AgentSlot(kind="claude", pane_id="12"),
                AgentSlot(kind="codex", pane_id="15", last_pane_id="20"),
            ],
        )
    )
    windows.forget("@1")
    for pane in ("12", "15", "20"):
        assert not (d / f"state-{pane}.json").exists()
        assert not (d / f"session-{pane}.id").exists()
    assert windows.read_mapping("@1") is None
