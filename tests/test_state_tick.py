import json
import os
import time
from pathlib import Path
from tmux_agents.commands import state_tick
from tmux_agents import tmux, state, paths, windows


def _mapping(wid: str, wt: Path, pane: str = "23"):
    return windows.WindowMapping(
        window_id=wid,
        project="p",
        branch=None,
        host_worktree=wt,
        pane_id=pane,
    )


def _write_state_json(wt: Path, pane: str, phase: str):
    d = wt / ".local" / ".tmux-agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"state-{pane}.json").write_text(
        json.dumps(
            {
                "phase": phase,
                "updated_at": "2026-01-01T00:00:00Z",
            }
        )
    )


def _write_marker(wt: Path, pane: str, name: str, content: str = "", *, mtime=None):
    d = wt / ".local" / ".tmux-agents" / f"pending-{pane}"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text(content)
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return f


def _configure_live(monkeypatch, wins, *, panes=None):
    """Stub the tmux surface for one tick and capture the batched set-option
    commands. Returns the list of captured batches so tests can assert the
    published `@state_code` (the derived letter now lives in that window option,
    not a `.state` file)."""
    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    monkeypatch.setattr(tmux, "list_windows", lambda s: wins if s == "agents" else [])
    if panes is None:
        # Default: every live window has one live pane "%23" — matches the
        # fixed pane id used by _mapping(...) throughout this test module.
        panes = {w.id: {"%23"} for w in wins}
    monkeypatch.setattr(
        tmux, "window_pane_map", lambda s: dict(panes) if s == "agents" else {}
    )
    batches: list[list[str]] = []
    monkeypatch.setattr(
        tmux, "apply_commands", lambda lines: batches.append(list(lines))
    )
    return batches


def _state_code(batches, wid):
    """The @state_code value published for `wid`, or None if it was never set."""
    prefix = f'set-option -wt {wid} @state_code "'
    for batch in batches:
        for cmd in batch:
            if cmd.startswith(prefix):
                return cmd[len(prefix) :].rstrip('"')
    return None


def test_tick_running(monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path):
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_mapping("@1", wt))
    _write_state_json(wt, "23", "running")
    batches = _configure_live(monkeypatch, [tmux.Window(id="@1", index=1, name="p")])
    state_tick.main([])
    assert _state_code(batches, "@1") == state.RUNNING


def test_tick_idle_no_items(monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path):
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_mapping("@1", wt))
    _write_state_json(wt, "23", "idle")
    batches = _configure_live(monkeypatch, [tmux.Window(id="@1", index=1, name="p")])
    state_tick.main([])
    assert _state_code(batches, "@1") == state.IDLE


def test_tick_idle_with_bg_marker_is_background(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_mapping("@1", wt))
    _write_state_json(wt, "23", "idle")
    _write_marker(wt, "23", "subagent__a1", mtime=time.time())
    batches = _configure_live(monkeypatch, [tmux.Window(id="@1", index=1, name="p")])
    state_tick.main([])
    assert _state_code(batches, "@1") == f"{state.BACKGROUND}1"


def test_tick_idle_with_sleeping_marker_is_sleeping(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_mapping("@1", wt))
    _write_state_json(wt, "23", "idle")
    # wakeup fires 100s out; scheduledFor is epoch ms.
    _write_marker(wt, "23", "wakeup", str(int((time.time() + 100) * 1000)))
    batches = _configure_live(monkeypatch, [tmux.Window(id="@1", index=1, name="p")])
    state_tick.main([])
    assert _state_code(batches, "@1") == f"{state.SLEEPING}1"


def test_tick_waiting(monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path):
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_mapping("@1", wt))
    _write_state_json(wt, "23", "waiting")
    batches = _configure_live(monkeypatch, [tmux.Window(id="@1", index=1, name="p")])
    state_tick.main([])
    assert _state_code(batches, "@1") == state.WAITING


def test_tick_pane_dead_overrides_to_errored(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_mapping("@1", wt))
    _write_state_json(wt, "23", "running")
    batches = _configure_live(
        monkeypatch,
        [tmux.Window(id="@1", index=1, name="p")],
        panes={"@1": set()},  # window present, all panes dead
    )
    state_tick.main([])
    assert _state_code(batches, "@1") == state.ERRORED


def test_tick_missing_state_file_is_idle(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_mapping("@1", wt))
    batches = _configure_live(monkeypatch, [tmux.Window(id="@1", index=1, name="p")])
    state_tick.main([])
    assert _state_code(batches, "@1") == state.IDLE


def test_tick_marks_window_without_mapping_as_errored(
    monkeypatch, tmp_config_dir, tmp_state_dir
):
    """A live window with no mapping shouldn't happen — publish X so the
    breakage is visible instead of leaving a stale letter in place."""
    batches = _configure_live(monkeypatch, [tmux.Window(id="@1", index=1, name="p")])
    state_tick.main([])
    assert _state_code(batches, "@1") == state.ERRORED


def test_tick_skips_control_window(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_mapping("@0", wt))
    batches = _configure_live(monkeypatch, [tmux.Window(id="@0", index=0, name="ctrl")])
    state_tick.main([])
    assert _state_code(batches, "@0") is None  # ctrl window gets no @state_code


def test_tick_prunes_mapping_and_worktree_files_for_dead_windows(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_mapping("@99", wt))
    (wt / ".local" / ".tmux-agents").mkdir(parents=True)
    (wt / ".local" / ".tmux-agents" / "state-23.json").write_text('{"phase":"running"}')
    (wt / ".local" / ".tmux-agents" / "pending-23").mkdir(parents=True)
    (wt / ".local" / ".tmux-agents" / "pending-23" / "subagent__a1").write_text("")
    _configure_live(monkeypatch, [])
    state_tick.main([])
    assert not paths.window_mapping_file("@99").exists()
    assert not (wt / ".local" / ".tmux-agents" / "state-23.json").exists()
    assert not (wt / ".local" / ".tmux-agents" / "pending-23").exists()


def test_tick_noop_when_session_missing(monkeypatch, tmp_config_dir, tmp_state_dir):
    monkeypatch.setattr(tmux, "session_exists", lambda s: False)
    called = []
    monkeypatch.setattr(tmux, "list_windows", lambda s: called.append(s) or [])
    state_tick.main([])
    assert called == []


def test_tick_preserves_mapping_when_list_windows_fails(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    """If `tmux list-windows` raises, the tick must bail without pruning
    mapping/worktree files. Otherwise a transient tmux failure wipes
    everything (the original symptom)."""
    import subprocess as sp

    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_mapping("@1", wt))
    _write_state_json(wt, "23", "running")
    monkeypatch.setattr(tmux, "session_exists", lambda s: True)

    def boom(_s):
        raise sp.CalledProcessError(1, ["tmux"], "", "")

    monkeypatch.setattr(tmux, "list_windows", boom)
    monkeypatch.setattr(tmux, "window_pane_map", lambda s: {})
    monkeypatch.setattr(tmux, "apply_commands", lambda lines: None)

    state_tick.main([])

    assert paths.window_mapping_file("@1").exists()
    assert (wt / ".local" / ".tmux-agents" / "state-23.json").exists()


def _capture_apply_commands(monkeypatch) -> list[list[str]]:
    """Replace tmux.apply_commands with a recorder; return the list of batches."""
    batches: list[list[str]] = []
    monkeypatch.setattr(
        tmux, "apply_commands", lambda lines: batches.append(list(lines))
    )
    return batches


def test_tick_sets_state_fg_hex_per_window(
    monkeypatch, tmp_state_dir, tmp_config_dir, tmp_path
):
    wt_a = tmp_path / "a"
    wt_a.mkdir()
    wt_b = tmp_path / "b"
    wt_b.mkdir()
    windows.write_mapping(_mapping("@1", wt_a, pane="23"))
    windows.write_mapping(_mapping("@2", wt_b, pane="24"))
    _write_state_json(wt_a, "23", "running")
    _write_state_json(wt_b, "24", "waiting")
    wins_ = [
        tmux.Window(id="@1", index=1, name="api:feat-x"),
        tmux.Window(id="@2", index=2, name="web:refactor"),
    ]
    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    monkeypatch.setattr(tmux, "list_windows", lambda s: wins_ if s == "agents" else [])
    monkeypatch.setattr(
        tmux,
        "window_pane_map",
        lambda s: {"@1": {"%23"}, "@2": {"%24"}} if s == "agents" else {},
    )
    batches = _capture_apply_commands(monkeypatch)

    state_tick.main([])

    assert len(batches) == 1
    cmds = batches[0]
    assert 'set-option -wt @1 @state_fg "#87af5f"' in cmds
    assert 'set-option -wt @2 @state_fg "#ffd75f"' in cmds
    # the derived letter is also published as @state_code
    assert 'set-option -wt @1 @state_code "R"' in cmds
    assert 'set-option -wt @2 @state_code "W"' in cmds


def test_tick_sets_state_selected_fg_per_window(
    monkeypatch, tmp_state_dir, tmp_config_dir, tmp_path
):
    wt_a = tmp_path / "a"
    wt_a.mkdir()
    wt_b = tmp_path / "b"
    wt_b.mkdir()
    windows.write_mapping(_mapping("@1", wt_a, pane="23"))
    windows.write_mapping(_mapping("@2", wt_b, pane="24"))
    _write_state_json(wt_a, "23", "running")
    _write_state_json(wt_b, "24", "waiting")
    wins_ = [
        tmux.Window(id="@1", index=1, name="api:feat-x"),
        tmux.Window(id="@2", index=2, name="web:refactor"),
    ]
    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    monkeypatch.setattr(tmux, "list_windows", lambda s: wins_ if s == "agents" else [])
    monkeypatch.setattr(
        tmux,
        "window_pane_map",
        lambda s: {"@1": {"%23"}, "@2": {"%24"}} if s == "agents" else {},
    )
    batches = _capture_apply_commands(monkeypatch)

    state_tick.main([])

    cmds = batches[0]
    assert 'set-option -wt @1 @state_selected_fg "#000000"' in cmds
    assert 'set-option -wt @2 @state_selected_fg "#000000"' in cmds


def test_tick_does_not_set_state_fg_on_ctrl_window(
    monkeypatch, tmp_state_dir, tmp_config_dir
):
    wins_ = [tmux.Window(id="@0", index=0, name="ctrl")]
    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    monkeypatch.setattr(tmux, "list_windows", lambda s: wins_)
    monkeypatch.setattr(tmux, "window_pane_map", lambda s: {"@0": {"%99"}})
    batches = _capture_apply_commands(monkeypatch)

    state_tick.main([])

    # No agent windows -> empty batch (apply_commands no-ops on empty list, but
    # the tick still calls it once with []).
    assert all(not b for b in batches)


def test_tick_skips_apply_commands_when_unchanged(
    monkeypatch, tmp_state_dir, tmp_config_dir, tmp_path
):
    """Second tick with identical state should not re-emit set-option commands."""
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_mapping("@1", wt))
    _write_state_json(wt, "23", "running")
    _configure_live(monkeypatch, [tmux.Window(id="@1", index=1, name="p")])
    batches = _capture_apply_commands(monkeypatch)

    state_tick.main([])
    state_tick.main([])

    assert len(batches) == 1  # only the first tick applies commands
    assert any("@state_fg" in c for c in batches[0])


def test_tick_refreshes_overlay_count_change(
    monkeypatch, tmp_state_dir, tmp_config_dir, tmp_path
):
    """A B2 -> B3 overlay change (same letter) must re-publish @state_code:
    the fingerprint includes the count, so the gated option write still fires."""
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_mapping("@1", wt))
    _write_state_json(wt, "23", "idle")
    _write_marker(wt, "23", "subagent__a1", mtime=time.time())
    batches = _configure_live(monkeypatch, [tmux.Window(id="@1", index=1, name="p")])

    state_tick.main([])
    assert _state_code(batches, "@1") == f"{state.BACKGROUND}1"

    # second background item appears -> overlay 1 -> 2, letter stays B
    _write_marker(wt, "23", "subagent__a2", mtime=time.time())
    state_tick.main([])
    assert len(batches) == 2  # not skipped despite same letter
    assert _state_code([batches[1]], "@1") == f"{state.BACKGROUND}2"


def test_tick_merges_session_id_into_mapping(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_mapping("@1", wt))
    _write_state_json(wt, "23", "running")
    # SessionStart hook would have written this:
    sid_file = wt / ".local" / ".tmux-agents" / "session-23.id"
    sid_file.parent.mkdir(parents=True, exist_ok=True)
    sid_file.write_text("01234567-89ab-cdef-0123-456789abcdef\n")
    _configure_live(monkeypatch, [tmux.Window(id="@1", index=2, name="p")])
    state_tick.main([])
    m = windows.read_mapping("@1")
    assert m.claude_session_id == "01234567-89ab-cdef-0123-456789abcdef"


def test_tick_merges_window_index_into_mapping(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_mapping("@1", wt))
    _write_state_json(wt, "23", "running")
    _configure_live(monkeypatch, [tmux.Window(id="@1", index=4, name="p")])
    state_tick.main([])
    m = windows.read_mapping("@1")
    assert m.window_index == 4


def test_tick_does_not_rewrite_mapping_when_unchanged(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    """Idempotent: same id + index -> no file write, so mtime should be stable."""
    import time

    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@1",
            project="p",
            branch=None,
            host_worktree=wt,
            pane_id="23",
            claude_session_id="01234567-89ab-cdef-0123-456789abcdef",
            window_index=2,
        )
    )
    _write_state_json(wt, "23", "running")
    sid_file = wt / ".local" / ".tmux-agents" / "session-23.id"
    sid_file.parent.mkdir(parents=True, exist_ok=True)
    sid_file.write_text("01234567-89ab-cdef-0123-456789abcdef\n")
    _configure_live(monkeypatch, [tmux.Window(id="@1", index=2, name="p")])
    mapping_path = paths.window_mapping_file("@1")
    before_mtime_ns = mapping_path.stat().st_mtime_ns
    # Sleep a hair to make any rewrite visible.
    time.sleep(0.01)
    state_tick.main([])
    after_mtime_ns = mapping_path.stat().st_mtime_ns
    assert before_mtime_ns == after_mtime_ns


def test_tick_pane_id_missing_from_live_panes_is_errored(
    monkeypatch,
    tmp_config_dir,
    tmp_state_dir,
    tmp_path,
):
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_mapping("@1", wt, pane="23"))  # mapping pane id = "23"
    _write_state_json(wt, "23", "running")
    # Window alive with a different pane (e.g. just the overview pane survives).
    batches = _configure_live(
        monkeypatch,
        [tmux.Window(id="@1", index=1, name="p")],
        panes={"@1": {"%99"}},
    )
    state_tick.main([])
    assert _state_code(batches, "@1") == state.ERRORED


def test_tick_bails_on_window_pane_map_failure(
    monkeypatch,
    tmp_config_dir,
    tmp_state_dir,
    tmp_path,
):
    """If window_pane_map raises (transient tmux failure), the tick must
    return 0 without pruning — letting every window show X for one tick
    would be a worse outcome than leaving stale state."""
    import subprocess

    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_mapping("@1", wt))
    _write_state_json(wt, "23", "running")

    monkeypatch.setattr(tmux, "session_exists", lambda s: True)

    def boom(_session):
        raise subprocess.CalledProcessError(1, ["tmux", "list-panes"])

    monkeypatch.setattr(tmux, "window_pane_map", boom)
    monkeypatch.setattr(
        tmux, "list_windows", lambda s: [tmux.Window(id="@1", index=1, name="p")]
    )
    monkeypatch.setattr(tmux, "apply_commands", lambda lines: None)

    assert state_tick.main([]) == 0
    # Mapping + worktree state untouched by the bail.
    assert paths.window_mapping_file("@1").exists()
    assert (wt / ".local" / ".tmux-agents" / "state-23.json").exists()


def test_tick_uses_phase_hint_when_no_state_file(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    wt = tmp_path / "repo"
    wt.mkdir()
    m = windows.WindowMapping(
        window_id="@1",
        project="p",
        branch=None,
        host_worktree=wt,
        pane_id="23",
        phase_hint="starting",
    )
    windows.write_mapping(m)
    wins = [tmux.Window(id="@1", index=1, name="api")]
    batches = _configure_live(monkeypatch, wins)
    state_tick.main([])
    assert _state_code(batches, "@1") == state.STARTING


def test_tick_phase_hint_errored_shows_x(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@1",
            project="p",
            branch=None,
            host_worktree=wt,
            pane_id="23",
            phase_hint="errored",
        )
    )
    wins = [tmux.Window(id="@1", index=1, name="api")]
    batches = _configure_live(monkeypatch, wins)
    state_tick.main([])
    assert _state_code(batches, "@1") == state.ERRORED


def test_tick_state_file_wins_over_phase_hint(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    wt = tmp_path / "repo"
    wt.mkdir()
    _write_state_json(wt, "23", "running")
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@1",
            project="p",
            branch=None,
            host_worktree=wt,
            pane_id="23",
            phase_hint="starting",
        )
    )
    wins = [tmux.Window(id="@1", index=1, name="api")]
    batches = _configure_live(monkeypatch, wins)
    state_tick.main([])
    assert _state_code(batches, "@1") == state.RUNNING


def test_tick_no_file_no_hint_is_idle(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@1", project="p", branch=None, host_worktree=wt, pane_id="23"
        )
    )
    wins = [tmux.Window(id="@1", index=1, name="api")]
    batches = _configure_live(monkeypatch, wins)
    state_tick.main([])
    assert _state_code(batches, "@1") == state.IDLE
