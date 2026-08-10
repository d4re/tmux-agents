import dataclasses
import json
import os
import time
from pathlib import Path
from tmux_agents.commands import state_tick
from tmux_agents import tmux, state, paths, windows, overview


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
    # Fresh single-pane re-verify query used by `_mark_secondary_dead` /
    # `_sweep_cleanup_pointers` under the cleanup lock — kept consistent
    # with the `panes` snapshot above so these tests don't shell out to a
    # real tmux server.
    monkeypatch.setattr(
        tmux,
        "pane_alive",
        lambda window_id, pane_id: pane_id in panes.get(window_id, set()),
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


def _dead_window_fixture(tmp_path: Path) -> Path:
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_mapping("@99", wt))
    (wt / ".local" / ".tmux-agents").mkdir(parents=True)
    (wt / ".local" / ".tmux-agents" / "state-23.json").write_text('{"phase":"running"}')
    (wt / ".local" / ".tmux-agents" / "pending-23").mkdir(parents=True)
    (wt / ".local" / ".tmux-agents" / "pending-23" / "subagent__a1").write_text("")
    return wt


def test_tick_tombstones_dead_window_before_pruning_it(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    """First tick after a window disappears must only tombstone. tmux closes
    every window a moment before the server exits, so an eager prune here
    destroys the restore snapshot on shutdown."""
    wt = _dead_window_fixture(tmp_path)
    _configure_live(monkeypatch, [])
    state_tick.main([])
    m = windows.read_mapping("@99")
    assert m is not None and m.orphaned_at is not None
    assert (wt / ".local" / ".tmux-agents" / "state-23.json").exists()
    assert (wt / ".local" / ".tmux-agents" / "pending-23").exists()


def test_tick_prunes_mapping_and_worktree_files_after_grace_period(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    wt = _dead_window_fixture(tmp_path)
    _configure_live(monkeypatch, [])
    state_tick.main([])
    stale = time.time() - state_tick._ORPHAN_GRACE_SECONDS - 1
    m = windows.read_mapping("@99")
    assert m is not None
    windows.write_mapping(dataclasses.replace(m, orphaned_at=stale))
    state_tick.main([])
    assert not paths.window_mapping_file("@99").exists()
    assert not (wt / ".local" / ".tmux-agents" / "state-23.json").exists()
    assert not (wt / ".local" / ".tmux-agents" / "pending-23").exists()


def test_tick_clears_tombstone_when_window_id_is_live_again(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    """A new server can hand out an id the previous one used; the stale
    tombstone must not survive to prune the new window's mapping."""
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(
        dataclasses.replace(_mapping("@1", wt), orphaned_at=time.time() - 1000)
    )
    _write_state_json(wt, "23", "running")
    _configure_live(monkeypatch, [tmux.Window(id="@1", index=1, name="p")])
    state_tick.main([])
    m = windows.read_mapping("@1")
    assert m is not None and m.orphaned_at is None


def test_tick_prune_skips_files_for_pane_id_aliased_by_a_live_mapping(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    """Dead window @99 recorded pane_id 23 for its (now-dead) window, but a
    LIVE window @1 of the SAME worktree currently maps pane 23 too (a fresh
    server recycled the id). Pruning @99 must not delete pane 23's files —
    they belong to @1's live agent — even though @99's own mapping file is
    still removed."""
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(
        dataclasses.replace(
            _mapping("@99", wt, pane="23"), orphaned_at=time.time() - 1000
        )
    )
    windows.write_mapping(_mapping("@1", wt, pane="23"))
    (wt / ".local" / ".tmux-agents").mkdir(parents=True)
    (wt / ".local" / ".tmux-agents" / "state-23.json").write_text('{"phase":"running"}')
    _configure_live(
        monkeypatch,
        [tmux.Window(id="@1", index=1, name="p")],
        panes={"@1": {"%23"}},
    )
    state_tick.main([])
    assert not paths.window_mapping_file("@99").exists()  # dead mapping removed
    assert paths.window_mapping_file("@1").exists()  # live mapping untouched
    # The live agent's files survive pruning.
    assert (wt / ".local" / ".tmux-agents" / "state-23.json").exists()


# ===== Codex-review fixes: prune revalidation + per-worktree batching =====


def test_prune_skips_delete_when_window_reappears_under_lock(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    """Finding 2: the candidate mapping must be revalidated under the
    worktree cleanup lock, not just read pre-lock. Here the tick's upfront
    `live_ids` snapshot said @99 was dead, but by the time the per-worktree
    lock's FRESH live-window query runs, @99 has reappeared (a fast
    respawn/replacement) — the mapping and its worktree files must survive."""
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(
        dataclasses.replace(
            _mapping("@99", wt, pane="23"), orphaned_at=time.time() - 1000
        )
    )
    (wt / ".local" / ".tmux-agents").mkdir(parents=True)
    (wt / ".local" / ".tmux-agents" / "state-23.json").write_text('{"phase":"running"}')

    # Fresh, under-lock query reports @99 alive again.
    monkeypatch.setattr(
        tmux, "list_windows", lambda s: [tmux.Window(id="@99", index=1, name="p")]
    )
    monkeypatch.setattr(tmux, "window_pane_map", lambda s: {"@99": {"%23"}})

    # Upfront snapshot (as the tick would have captured before @99 came
    # back) says @99 is dead.
    state_tick._prune_windows_and_worktree_files(set(), now=time.time())

    assert paths.window_mapping_file("@99").exists()
    assert (wt / ".local" / ".tmux-agents" / "state-23.json").exists()


def test_prune_skips_delete_when_mapping_replaced_under_lock(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    """Finding 2, the mapping-identity half: the window id is still absent
    from the fresh live-window set, but the on-disk mapping for it has been
    REPLACED (different content) since the pre-lock read — a concurrent
    publish for the same window id. The stale candidate must not be
    deleted out from under the replacement."""
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_mapping("@99", wt, pane="23"))
    (wt / ".local" / ".tmux-agents").mkdir(parents=True)
    (wt / ".local" / ".tmux-agents" / "state-23.json").write_text('{"phase":"running"}')

    monkeypatch.setattr(tmux, "list_windows", lambda s: [])
    monkeypatch.setattr(tmux, "window_pane_map", lambda s: {})

    real_read_mapping = windows.read_mapping
    calls = {"n": 0}

    def racy_read_mapping(window_id):
        # First read (pre-lock, for grouping) returns the original mapping.
        # Simulate a concurrent publish landing before the fresh re-read
        # under the lock.
        calls["n"] += 1
        if calls["n"] == 2:
            windows.write_mapping(_mapping("@99", wt, pane="55"))
        return real_read_mapping(window_id)

    monkeypatch.setattr(windows, "read_mapping", racy_read_mapping)

    state_tick._prune_windows_and_worktree_files(set(), now=time.time())

    m = windows.read_mapping("@99")
    assert m is not None
    assert m.pane_id == "55"  # replacement survived, untouched by the prune


def test_prune_batches_tmux_queries_per_worktree(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    """Finding 3: multiple dead candidates for the SAME worktree must share
    one lock hold and one pair of fresh tmux queries, not one pair per
    candidate."""
    wt = tmp_path / "repo"
    wt.mkdir()
    stale = time.time() - 1000
    windows.write_mapping(
        dataclasses.replace(_mapping("@98", wt, pane="23"), orphaned_at=stale)
    )
    windows.write_mapping(
        dataclasses.replace(_mapping("@99", wt, pane="24"), orphaned_at=stale)
    )
    (wt / ".local" / ".tmux-agents").mkdir(parents=True)
    (wt / ".local" / ".tmux-agents" / "state-23.json").write_text('{"phase":"running"}')
    (wt / ".local" / ".tmux-agents" / "state-24.json").write_text('{"phase":"running"}')

    calls = {"list_windows": 0, "window_pane_map": 0}

    def fake_list_windows(s):
        calls["list_windows"] += 1
        return []

    def fake_window_pane_map(s):
        calls["window_pane_map"] += 1
        return {}

    monkeypatch.setattr(tmux, "list_windows", fake_list_windows)
    monkeypatch.setattr(tmux, "window_pane_map", fake_window_pane_map)

    state_tick._prune_windows_and_worktree_files(set(), now=time.time())

    assert calls == {"list_windows": 1, "window_pane_map": 1}
    assert not paths.window_mapping_file("@98").exists()
    assert not paths.window_mapping_file("@99").exists()
    assert not (wt / ".local" / ".tmux-agents" / "state-23.json").exists()
    assert not (wt / ".local" / ".tmux-agents" / "state-24.json").exists()


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


def _dual_mapping(wid: str, wt: Path, *, pane_a: str = "23", pane_b: str = "24"):
    return windows.WindowMapping(
        window_id=wid,
        project="p",
        branch=None,
        host_worktree=wt,
        pane_id=pane_a,
        agents=[
            windows.AgentSlot(kind="claude", pane_id=pane_a),
            windows.AgentSlot(kind="codex", pane_id=pane_b),
        ],
    )


def test_tick_dual_slot_publishes_joined_state_code(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_dual_mapping("@1", wt))
    _write_state_json(wt, "23", "running")
    _write_state_json(wt, "24", "idle")
    batches = _configure_live(
        monkeypatch,
        [tmux.Window(id="@1", index=1, name="p")],
        panes={"@1": {"%23", "%24"}},
    )
    state_tick.main([])
    assert _state_code(batches, "@1") == "R|I"


def test_tick_dual_slot_combined_color_is_highest_priority(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    """@state_fg/@state_selected_fg reflect the combined (highest-priority)
    letter across live slots, not just the default slot's letter."""
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_dual_mapping("@1", wt))
    _write_state_json(wt, "23", "idle")
    _write_state_json(wt, "24", "waiting")  # secondary waiting beats default idle
    batches = _configure_live(
        monkeypatch,
        [tmux.Window(id="@1", index=1, name="p")],
        panes={"@1": {"%23", "%24"}},
    )
    state_tick.main([])
    cmds = batches[0]
    palette = __import__("tmux_agents.theme", fromlist=["get_palette"]).get_palette()
    assert f'set-option -wt @1 @state_fg "{palette.fg[state.WAITING]}"' in cmds
    assert _state_code(batches, "@1") == "I|W"


def test_tick_secondary_slot_no_state_file_derives_starting(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_dual_mapping("@1", wt))
    _write_state_json(wt, "23", "running")
    # No state-24.json written for the secondary slot.
    batches = _configure_live(
        monkeypatch,
        [tmux.Window(id="@1", index=1, name="p")],
        panes={"@1": {"%23", "%24"}},
    )
    state_tick.main([])
    assert _state_code(batches, "@1") == f"R|{state.STARTING}"


def test_tick_secondary_pane_missing_marks_dead_and_scrubs(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_dual_mapping("@1", wt))
    _write_state_json(wt, "23", "running")
    _write_state_json(wt, "24", "idle")
    sid_file = wt / ".local" / ".tmux-agents" / "session-24.id"
    sid_file.parent.mkdir(parents=True, exist_ok=True)
    sid_file.write_text("11111111-1111-1111-1111-111111111111\n")
    # Secondary pane %24 is not among the live panes for the window.
    batches = _configure_live(
        monkeypatch,
        [tmux.Window(id="@1", index=1, name="p")],
        panes={"@1": {"%23"}},
    )
    state_tick.main([])
    # Only the surviving default slot renders — no '|' for the dead secondary.
    assert _state_code(batches, "@1") == state.RUNNING

    m2 = windows.read_mapping("@1")
    dead = m2.agents[1]
    assert dead.pane_id is None
    assert dead.session_id == "11111111-1111-1111-1111-111111111111"
    assert dead.last_pane_id == "24"
    assert not (wt / ".local" / ".tmux-agents" / "state-24.json").exists()
    assert not sid_file.exists()


def test_mark_secondary_dead_is_cas_revival_wins(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    """If the secondary is revived with a new pane id between the tick's read
    and the dead-marking transaction, the stale transaction must be a no-op."""
    wt = tmp_path / "repo"
    wt.mkdir()
    m = _dual_mapping("@1", wt)
    windows.write_mapping(m)
    observed_slot = m.agents[1]  # snapshot: kind="codex", pane_id="24"
    # The old observed pane (%24) is genuinely gone by the time the fresh
    # re-verify runs — this test is specifically about the CAS safety net,
    # not the fresh-liveness pre-check (covered separately).
    monkeypatch.setattr(tmux, "pane_alive", lambda window_id, pane_id: False)

    # Simulate a concurrent revival with a fresh pane id.
    windows.update_mapping(
        "@1",
        lambda cur: dataclasses.replace(
            cur,
            agents=[
                cur.agents[0],
                dataclasses.replace(cur.agents[1], pane_id="99"),
            ],
        ),
    )

    state_tick._mark_secondary_dead(m, observed_slot)

    m2 = windows.read_mapping("@1")
    assert m2.agents[1].pane_id == "99"  # stale CAS did not clobber the revival


def test_mark_secondary_dead_skips_when_fresh_query_reports_pane_alive(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    """The tick's pane-liveness snapshot predates the mapping read: a slot
    that was just revived (published between the snapshot and the cleanup
    lock) must not be killed. A fresh, single-pane tmux query under the lock
    catches this even when the CAS alone wouldn't (e.g. the revival hasn't
    reached `update_mapping`'s own read yet, but the pane already exists)."""
    wt = tmp_path / "repo"
    wt.mkdir()
    m = _dual_mapping("@1", wt)
    windows.write_mapping(m)
    observed_slot = m.agents[1]  # kind="codex", pane_id="24"

    monkeypatch.setattr(tmux, "pane_alive", lambda window_id, pane_id: True)
    scrub_calls = []
    monkeypatch.setattr(
        __import__("tmux_agents.startup", fromlist=["scrub_pane_files"]),
        "scrub_pane_files",
        lambda *a: scrub_calls.append(a),
    )

    state_tick._mark_secondary_dead(m, observed_slot)

    m2 = windows.read_mapping("@1")
    assert m2.agents[1].pane_id == "24"  # unchanged — no CAS ran
    assert scrub_calls == []


def test_tick_sweep_deletes_and_clears_pointer_when_pane_truly_gone(
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
        agents=[
            windows.AgentSlot(kind="claude", pane_id="23"),
            windows.AgentSlot(kind="codex", pane_id=None, last_pane_id="24"),
        ],
    )
    windows.write_mapping(m)
    _write_state_json(wt, "23", "running")
    _write_state_json(wt, "24", "idle")  # leftover file from the dead pane
    _configure_live(
        monkeypatch,
        [tmux.Window(id="@1", index=1, name="p")],
        panes={"@1": {"%23"}},  # %24 is nowhere alive
    )
    state_tick.main([])
    assert not (wt / ".local" / ".tmux-agents" / "state-24.json").exists()
    m2 = windows.read_mapping("@1")
    assert m2.agents[1].last_pane_id is None


def test_tick_sweep_retains_pointer_when_scrub_leaves_a_survivor(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    """If `scrub_pane_files` silently leaves a survivor (e.g. `rmtree`
    swallowing a permission error), `startup.pane_files_absent` catches it
    and the pointer must NOT be cleared — otherwise the survivor is never
    retried."""
    wt = tmp_path / "repo"
    wt.mkdir()
    m = windows.WindowMapping(
        window_id="@1",
        project="p",
        branch=None,
        host_worktree=wt,
        pane_id="23",
        agents=[
            windows.AgentSlot(kind="claude", pane_id="23"),
            windows.AgentSlot(kind="codex", pane_id=None, last_pane_id="24"),
        ],
    )
    windows.write_mapping(m)
    _write_state_json(wt, "23", "running")
    _write_state_json(wt, "24", "idle")
    _configure_live(
        monkeypatch,
        [tmux.Window(id="@1", index=1, name="p")],
        panes={"@1": {"%23"}},  # %24 is nowhere alive
    )
    monkeypatch.setattr(state_tick.startup, "pane_files_absent", lambda wt, pid: False)

    state_tick.main([])

    m2 = windows.read_mapping("@1")
    assert m2.agents[1].last_pane_id == "24"  # pointer retained for retry


def test_tick_sweep_skips_when_last_pane_id_aliases_live_mapped_pane(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    """A dead secondary's last_pane_id equal to a currently-live mapped pane
    (fresh-server id reuse): the files already belong to that mapped slot,
    so the sweep must not touch them — but OUR pointer is cleared via CAS
    since there's nothing left for it to track."""
    wt = tmp_path / "repo"
    wt.mkdir()
    m = windows.WindowMapping(
        window_id="@1",
        project="p",
        branch=None,
        host_worktree=wt,
        pane_id="23",
        agents=[
            windows.AgentSlot(kind="claude", pane_id="23"),
            windows.AgentSlot(kind="codex", pane_id=None, last_pane_id="23"),
        ],
    )
    windows.write_mapping(m)
    _write_state_json(wt, "23", "running")
    _configure_live(
        monkeypatch,
        [tmux.Window(id="@1", index=1, name="p")],
        panes={"@1": {"%23"}},
    )
    state_tick.main([])
    assert (wt / ".local" / ".tmux-agents" / "state-23.json").exists()
    m2 = windows.read_mapping("@1")
    assert m2.agents[1].last_pane_id is None


def test_tick_sweep_skips_when_last_pane_id_aliases_live_pane_in_other_window(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    """Same worktree mapped into two windows (e.g. agent-new re-run on an
    open branch): window A's dead secondary points at a pane id that is now
    live as window B's slot. The alias guard must be worktree-scoped, not
    window-scoped — sweeping window A must not delete window B's files just
    because A's own mapping doesn't mention pane 15. Ownership of those
    files has transferred to window B's slot, so A's now-meaningless
    pointer is cleared via CAS."""
    wt = tmp_path / "repo"
    wt.mkdir()
    mapping_a = windows.WindowMapping(
        window_id="@1",
        project="p",
        branch=None,
        host_worktree=wt,
        pane_id="10",
        agents=[
            windows.AgentSlot(kind="claude", pane_id="10"),
            windows.AgentSlot(kind="codex", pane_id=None, last_pane_id="15"),
        ],
    )
    windows.write_mapping(mapping_a)
    mapping_b = windows.WindowMapping(
        window_id="@2",
        project="p",
        branch=None,
        host_worktree=wt,
        pane_id="15",
    )
    windows.write_mapping(mapping_b)
    _write_state_json(wt, "10", "running")
    _write_state_json(wt, "15", "running")  # window B's live pane state
    _configure_live(
        monkeypatch,
        [
            tmux.Window(id="@1", index=1, name="p"),
            tmux.Window(id="@2", index=2, name="p2"),
        ],
        panes={"@1": {"%10"}, "@2": {"%15"}},
    )
    state_tick.main([])
    # Window B's state file for the live-aliased pane must survive.
    assert (wt / ".local" / ".tmux-agents" / "state-15.json").exists()
    m2 = windows.read_mapping("@1")
    assert m2.agents[1].last_pane_id is None


# ===== Codex-review fix: sweep's tmux-alive deferral is session-wide =====


def test_tick_sweep_defers_when_last_pane_id_alive_in_different_window(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    """Finding 4: pane ids are server-global. A live, unmapped pane sitting
    in a DIFFERENT window (e.g. a mid-agent-other crash artifact) must
    still defer this pointer's cleanup — checking only the pointer owner's
    own window (`tmux.pane_alive(window_id, ...)`) would miss it and scrub
    + drop the pointer for a pane that is, in fact, still alive."""
    wt = tmp_path / "repo"
    wt.mkdir()
    m = windows.WindowMapping(
        window_id="@1",
        project="p",
        branch=None,
        host_worktree=wt,
        pane_id="23",
        agents=[
            windows.AgentSlot(kind="claude", pane_id="23"),
            windows.AgentSlot(kind="codex", pane_id=None, last_pane_id="24"),
        ],
    )
    windows.write_mapping(m)
    _write_state_json(wt, "23", "running")
    _write_state_json(wt, "24", "idle")  # would be deleted if swept

    win1 = tmux.Window(id="@1", index=1, name="p")
    win2 = tmux.Window(id="@2", index=2, name="other")
    monkeypatch.setattr(tmux, "list_windows", lambda s: [win1, win2])
    # %24 is alive, but parked under window @2 — not @1, and unmapped by
    # any window's mapping.
    monkeypatch.setattr(
        tmux, "window_pane_map", lambda s: {"@1": {"%23"}, "@2": {"%24"}}
    )
    monkeypatch.setattr(tmux, "pane_alive", lambda window_id, pane_id: False)

    state_tick._sweep_cleanup_pointers("@1", m)

    assert (wt / ".local" / ".tmux-agents" / "state-24.json").exists()
    m2 = windows.read_mapping("@1")
    assert m2.agents[1].last_pane_id == "24"  # pointer retained — deferred


def test_sweep_rederives_fresh_collision_inputs_under_lock(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    """Called directly (not through a full `main()` tick, whose end-of-tick
    prune would confound the result): the `mapping` argument mirrors the
    tick's stale, pre-lock snapshot, in which pane 24 is aliased by nothing
    — a naive implementation would scrub it. Simulate the interleave by
    having a second window (@2) already publish a mapping for the SAME
    worktree claiming pane 24 by the time `tmux.list_windows` is queried —
    representing a spawn that landed between the tick's own upfront
    snapshot and this call. `_sweep_cleanup_pointers` must re-derive its
    collision inputs fresh under the lock and see that alias: files
    survive, and the now-meaningless pointer is cleared via CAS (ownership
    transferred to window @2's slot)."""
    wt = tmp_path / "repo"
    wt.mkdir()
    m = windows.WindowMapping(
        window_id="@1",
        project="p",
        branch=None,
        host_worktree=wt,
        pane_id="23",
        agents=[
            windows.AgentSlot(kind="claude", pane_id="23"),
            windows.AgentSlot(kind="codex", pane_id=None, last_pane_id="24"),
        ],
    )
    windows.write_mapping(m)
    _write_state_json(wt, "23", "running")
    _write_state_json(wt, "24", "idle")  # would be deleted if swept

    win1 = tmux.Window(id="@1", index=1, name="p")
    # The alias-causing window did not exist when the caller's stale
    # `mapping` snapshot was taken, but is live and mapped by the time the
    # sweep's fresh re-read runs.
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@2", project="p", branch=None, host_worktree=wt, pane_id="24"
        )
    )
    monkeypatch.setattr(
        tmux,
        "list_windows",
        lambda s: [win1, tmux.Window(id="@2", index=2, name="p2")],
    )
    monkeypatch.setattr(
        tmux,
        "window_pane_map",
        lambda s: {"@1": {"%23"}, "@2": {"%24"}},
    )
    monkeypatch.setattr(tmux, "pane_alive", lambda window_id, pane_id: False)

    state_tick._sweep_cleanup_pointers("@1", m)

    # The file survived: the fresh re-read caught the alias that the stale
    # pre-lock snapshot would have missed. The pointer is cleared anyway —
    # it no longer tracks anything this slot owns.
    assert (wt / ".local" / ".tmux-agents" / "state-24.json").exists()
    m2 = windows.read_mapping("@1")
    assert m2.agents[1].last_pane_id is None


def test_tick_merge_ids_fast_path_skips_update_mapping_call(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path
):
    """merge_ids's pre-check should skip the `windows.update_mapping` call
    entirely when neither the session id nor window_index actually changed —
    not just skip the disk write inside it."""
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

    calls: list = []
    orig_update_mapping = windows.update_mapping

    def counting_update_mapping(*args, **kwargs):
        calls.append(args[0])
        return orig_update_mapping(*args, **kwargs)

    monkeypatch.setattr(windows, "update_mapping", counting_update_mapping)

    state_tick.main([])

    assert calls == []


def test_tick_summary_counts_each_live_slot(
    monkeypatch, tmp_config_dir, tmp_state_dir, tmp_path, capsys
):
    wt = tmp_path / "repo"
    wt.mkdir()
    windows.write_mapping(_dual_mapping("@1", wt))
    _write_state_json(wt, "23", "running")
    _write_state_json(wt, "24", "waiting")
    _configure_live(
        monkeypatch,
        [tmux.Window(id="@1", index=1, name="p")],
        panes={"@1": {"%23", "%24"}},
    )
    state_tick.main([])
    out = capsys.readouterr().out
    expected_counts = overview.empty_counts()
    expected_counts[state.RUNNING] = 1
    expected_counts[state.WAITING] = 1
    assert out == overview.render_summary(counts=expected_counts)


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
