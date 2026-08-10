import dataclasses
import multiprocessing as mp
from pathlib import Path

from tmux_agents import locks, paths, windows
from tmux_agents.windows import WindowMapping


def _mk(window_id="@1", **kw):
    base = dict(
        window_id=window_id,
        project="p",
        branch=None,
        host_worktree=Path("/r"),
        pane_id="12",
    )
    base.update(kw)
    return WindowMapping(**base)


def _set_window_index(m, value):
    return dataclasses.replace(m, window_index=value)


def test_update_mapping_read_modify_write(tmp_config_dir):
    windows.write_mapping(_mk())
    out = windows.update_mapping("@1", lambda m: _set_window_index(m, 7))
    assert out.window_index == 7
    assert windows.read_mapping("@1").window_index == 7


def test_update_mapping_none_aborts_write(tmp_config_dir):
    windows.write_mapping(_mk(window_index=1))
    assert windows.update_mapping("@1", lambda m: None) is None
    assert windows.read_mapping("@1").window_index == 1


def test_update_mapping_missing_file_creates_when_fn_returns(tmp_config_dir):
    out = windows.update_mapping("@9", lambda m: _mk("@9") if m is None else None)
    assert out is not None
    assert windows.read_mapping("@9") is not None


def _bump_window_index(m):
    return dataclasses.replace(m, window_index=(m.window_index or 0) + 1)


def _bump(_, iterations):
    for _ in range(iterations):
        windows.update_mapping("@1", _bump_window_index)


def test_update_mapping_serializes_two_writers(tmp_config_dir):
    # Deterministic serialization check: two forked processes each do N
    # read-modify-write increments through the flock-guarded update_mapping.
    # If the lock were removed, concurrent read-modify-write races would
    # lose increments and the final count would land below writers*N.
    windows.write_mapping(_mk(window_index=0))
    iterations = 25
    ctx = mp.get_context("fork")
    ps = [ctx.Process(target=_bump, args=(i, iterations)) for i in range(2)]
    [p.start() for p in ps]
    [p.join() for p in ps]
    assert windows.read_mapping("@1").window_index == 2 * iterations


def test_delete_mapping(tmp_config_dir):
    windows.write_mapping(_mk())
    windows.delete_mapping("@1")
    assert windows.read_mapping("@1") is None


def test_cleanup_lock_path(tmp_path):
    p = paths.worktree_cleanup_lock(tmp_path)
    assert p == tmp_path / ".local" / ".tmux-agents" / ".cleanup.lock"


def test_no_deadlock_when_nesting_in_declared_order(tmp_config_dir, tmp_path):
    # Lock-inversion guard: cleanup lock outer, mapping ops inner — the only
    # legal composition. Completing without hanging is the assertion.
    windows.write_mapping(_mk())
    with locks.locked(paths.worktree_cleanup_lock(tmp_path)):
        windows.update_mapping("@1", lambda m: _set_window_index(m, 3))
    assert windows.read_mapping("@1").window_index == 3
