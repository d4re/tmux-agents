"""Background worktree removals: the record store, the worker body, the
up-front git checks, and how the overview shows/acts on them."""

import curses
import os
import subprocess

import pytest

from tmux_agents import overview, removals, state, tmux, worktree
from tmux_agents.overview import Cursor


def _rec(key="k1", *, project="api", status=removals.RUNNING, **kw):
    r = removals.Removal(
        key=key,
        name=f"{project}:feat-x",
        project=project,
        branch="feat-x",
        repo="/repo",
        status=status,
        **kw,
    )
    removals.save(r)
    return r


# ---------- record store ----------


def test_running_record_with_live_worker_stays_running(tmp_state_dir):
    _rec(pid=os.getpid())
    [r] = removals.list_all()
    assert r.status == removals.RUNNING


def test_running_record_with_dead_worker_reads_as_failed(tmp_state_dir, monkeypatch):
    _rec(pid=999_999)
    monkeypatch.setattr(removals, "_pid_alive", lambda pid: False)
    [r] = removals.list_all()
    assert r.status == removals.FAILED
    assert "exited unexpectedly" in r.error


def test_worker_that_never_started_reads_as_failed(tmp_state_dir):
    _rec(started_at=0.0)
    [r] = removals.list_all()
    assert r.status == removals.FAILED
    assert "never started" in r.error


def test_fresh_record_without_pid_is_still_running(tmp_state_dir):
    _rec()
    [r] = removals.list_all()
    assert r.status == removals.RUNNING


def test_malformed_record_is_skipped(tmp_state_dir):
    d = tmp_state_dir / "removals"
    d.mkdir()
    (d / "bad.json").write_text("{not json")
    (d / "odd.json").write_text("[1, 2]")
    assert removals.list_all() == []


# ---------- worker ----------


def test_run_success_drops_record(tmp_state_dir, monkeypatch):
    _rec(force=True, container="c", container_workdir="/w", container_user="u")
    calls = []
    monkeypatch.setattr(worktree, "remove", lambda *a, **k: calls.append((a, k)))
    assert removals.run("k1") == 0
    assert removals.get("k1") is None
    (_, kw) = calls[0]
    assert kw == {
        "force": True,
        "container": "c",
        "container_workdir": "/w",
        "container_user": "u",
    }


def test_run_failure_keeps_record_with_error(tmp_state_dir, monkeypatch):
    _rec()

    def boom(*a, **k):
        raise worktree.WorktreeError("fatal: nope")

    monkeypatch.setattr(worktree, "remove", boom)
    assert removals.run("k1") == 1
    r = removals.get("k1")
    assert (r.status, r.error, r.pid) == (removals.FAILED, "fatal: nope", os.getpid())


def test_run_leftover_deletes_husk_folder(tmp_state_dir, tmp_path):
    husk = tmp_path / ".worktrees" / "feat-x"
    (husk / ".claude").mkdir(parents=True)
    removals.save(
        removals.Removal(
            key="k1",
            name="api:feat-x",
            project="api",
            branch="feat-x",
            repo=str(tmp_path),
            leftover=True,
        )
    )
    assert removals.run("k1") == 0
    assert not husk.exists()


# ---------- up-front git checks (real git) ----------


@pytest.fixture
def repo_with_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args, cwd=repo):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git(
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
    )
    git("worktree", "add", "-q", "-b", "feat-x", str(repo / ".worktrees" / "feat-x"))
    return repo


def test_is_dirty_clean_then_untracked(repo_with_worktree):
    assert worktree.is_dirty(repo_with_worktree, "feat-x") is False
    (repo_with_worktree / ".worktrees" / "feat-x" / "new.txt").write_text("x")
    assert worktree.is_dirty(repo_with_worktree, "feat-x") is True


def test_is_dirty_missing_worktree_is_clean(tmp_path):
    assert worktree.is_dirty(tmp_path, "nope") is False


def test_is_husk(repo_with_worktree):
    assert worktree.is_husk(repo_with_worktree, "feat-x") is False
    husk = repo_with_worktree / ".worktrees" / "husk"
    husk.mkdir()
    assert worktree.is_husk(repo_with_worktree, "husk") is True
    assert worktree.is_husk(repo_with_worktree, "absent") is False


# ---------- overview ----------


@pytest.fixture(autouse=True)
def _fixed_prefix(monkeypatch):
    monkeypatch.setattr(tmux, "prefix_label", lambda: "Ctrl-Space")


def _wins():
    return [tmux.Window(id="@1", index=1, name="api:other", state_code=state.IDLE)]


def test_build_rows_lists_removals_under_their_repo(tmp_state_dir):
    _rec("k1", project="api")
    _rec("k2", project="web", status=removals.FAILED, error="boom")
    rows = overview.build_rows({}, windows=_wins())
    assert [(r.kind, r.repo) for r in rows] == [
        ("header", "api"),
        ("agent", "api"),
        ("removal", "api"),
        ("header", "web"),  # repo with no live agents still gets a header
        ("removal", "web"),
    ]
    assert rows[3].count == 0


def test_folded_repo_hides_its_removals(tmp_state_dir):
    _rec("k1", project="api")
    rows = overview.build_rows({"api": True}, windows=_wins())
    assert [r.kind for r in rows] == ["header"]


def test_removal_rows_render_with_status_text(tmp_state_dir):
    _rec("k1")
    _rec("k2", status=removals.FAILED, error="boom")
    rows = overview.build_rows({}, windows=[])
    texts = [overview._row_text(r) for r in rows if r.kind == "removal"]
    assert "removing worktree…" in texts[0]
    assert "removal failed" in texts[1]


def test_render_curses_maps_removal_rows(tmp_state_dir):
    from tests.test_overview_tui import _FakeStdscr

    _rec("k1")
    rows = overview.build_rows({}, windows=[])
    scr = _FakeStdscr(height=10, width=80)
    row_at_y, _ = overview.render_curses(scr, rows, cursor=Cursor("removal", "k1"))
    assert row_at_y[:2] == [Cursor("header", "api"), Cursor("removal", "k1")]
    assert any("> ✕ api:feat-x" in w[2] for w in scr.writes)


def test_enter_on_removal_opens_details_popup(tmp_state_dir, monkeypatch):
    popped = []
    monkeypatch.setattr(overview, "_popen", popped.append)
    overview.activate_target(Cursor("removal", "k1"), {})
    assert popped[0][-1] == "agent-kill --show-removal k1"


def test_k_dismisses_failed_removal(tmp_state_dir, monkeypatch):
    _rec(status=removals.FAILED, error="boom")
    monkeypatch.setattr(overview, "_popen", lambda a: pytest.fail("no popup"))
    overview.kill_at(Cursor("removal", "k1"))
    assert removals.list_all() == []


def test_k_does_not_dismiss_running_removal(tmp_state_dir, monkeypatch):
    _rec(pid=os.getpid())
    monkeypatch.setattr(overview, "_popen", lambda a: pytest.fail("no popup"))
    overview.kill_at(Cursor("removal", "k1"))
    assert len(removals.list_all()) == 1


def _state_with_rows(monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: _wins())
    s = overview.make_initial_state("%99")
    s.row_at_y = [Cursor("header", "api"), Cursor("agent", "@1")] + [None] * 8
    s.text_w_at_y = [20, 30] + [0] * 8
    return s


@pytest.mark.parametrize("bstate", [curses.BUTTON3_PRESSED, curses.BUTTON3_CLICKED])
def test_right_click_on_agent_opens_kill_popup(tmp_state_dir, monkeypatch, bstate):
    s = _state_with_rows(monkeypatch)
    popped = []
    monkeypatch.setattr(overview, "_popen", popped.append)
    monkeypatch.setattr(tmux, "select_window", lambda t: pytest.fail("no switch"))
    overview.handle_mouse(s, mx=5, my=1, bstate=bstate)
    assert popped[0][-1] == "agent-kill --window-id @1"
    assert s.cursor == Cursor("agent", "@1")


def test_right_click_on_blank_space_does_nothing(tmp_state_dir, monkeypatch):
    s = _state_with_rows(monkeypatch)
    monkeypatch.setattr(overview, "_popen", lambda a: pytest.fail("no popup"))
    monkeypatch.setattr(tmux, "select_pane", lambda p: pytest.fail("no focus"))
    overview.handle_mouse(s, mx=5, my=5, bstate=curses.BUTTON3_PRESSED)


def test_right_click_on_header_does_nothing(tmp_state_dir, monkeypatch):
    s = _state_with_rows(monkeypatch)
    monkeypatch.setattr(overview, "_popen", lambda a: pytest.fail("no popup"))
    overview.handle_mouse(s, mx=5, my=0, bstate=curses.BUTTON3_PRESSED)
