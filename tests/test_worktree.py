import io
from pathlib import Path
from unittest.mock import MagicMock
import subprocess
import pytest
from tmux_agents import worktree
from tmux_agents.progress import Reporter

# Capture the real _resolve_base before the autouse fixture replaces it.
_REAL_RESOLVE_BASE = worktree._resolve_base


@pytest.fixture(autouse=True)
def _stub_resolve_base(monkeypatch):
    """Existing tests assert legacy argv shapes. Short-circuit base
    resolution so they keep doing that; new behavior is covered in
    tests/test_worktree_base.py."""
    monkeypatch.setattr(worktree, "_resolve_base", lambda *a, **kw: (None, []))


def _stub_run(monkeypatch, responses):
    """responses: list of (returncode, stdout) tuples, consumed in order."""
    it = iter(responses)
    calls = []

    def fake_run(cmd, capture_output=False, text=False, check=False, input=None):
        calls.append(cmd)
        rc, out = next(it)
        return MagicMock(returncode=rc, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_resolve_no_branch_returns_repo(tmp_path):
    assert worktree.resolve(tmp_path, None) == tmp_path


def test_resolve_existing_worktree_reuses(tmp_path):
    wt = tmp_path / ".worktrees" / "feat-x"
    wt.mkdir(parents=True)
    assert worktree.resolve(tmp_path, "feat-x") == wt


def test_resolve_missing_worktree_creates(tmp_path, monkeypatch):
    calls = _stub_run(monkeypatch, [(0, "")])
    result = worktree.resolve(tmp_path, "feat-x")
    assert result == tmp_path / ".worktrees" / "feat-x"
    assert calls == [
        [
            "git",
            "-C",
            str(tmp_path),
            "worktree",
            "add",
            str(tmp_path / ".worktrees" / "feat-x"),
            "-B",
            "feat-x",
        ]
    ]


def test_resolve_in_container_uses_docker_exec(tmp_path, monkeypatch):
    calls = _stub_run(monkeypatch, [(0, "")])
    result = worktree.resolve(
        tmp_path,
        "feat-x",
        container="api-devcontainer",
        container_workdir="/work",
    )
    assert result == tmp_path / ".worktrees" / "feat-x"
    assert calls == [
        [
            "docker",
            "exec",
            "-u",
            "vscode",
            "api-devcontainer",
            "git",
            "-C",
            "/work",
            "worktree",
            "add",
            "/work/.worktrees/feat-x",
            "-B",
            "feat-x",
        ]
    ]


def test_resolve_git_failure_raises(tmp_path, monkeypatch):
    _stub_run(
        monkeypatch,
        [(128, "fatal: branch 'feat-x' is already checked out at /other/path")],
    )
    with pytest.raises(worktree.WorktreeError, match="already checked out"):
        worktree.resolve(tmp_path, "feat-x")


def test_remove_worktree(tmp_path, monkeypatch):
    wt = tmp_path / ".worktrees" / "feat-x"
    wt.mkdir(parents=True)
    calls = _stub_run(monkeypatch, [(0, "")])
    worktree.remove(tmp_path, "feat-x")
    assert calls == [["git", "-C", str(tmp_path), "worktree", "remove", str(wt)]]


def test_remove_in_container_uses_docker_exec(tmp_path, monkeypatch):
    wt = tmp_path / ".worktrees" / "feat-x"
    wt.mkdir(parents=True)
    calls = _stub_run(monkeypatch, [(0, "")])
    worktree.remove(
        tmp_path,
        "feat-x",
        force=True,
        container="api-devcontainer",
        container_workdir="/work",
    )
    assert calls == [
        [
            "docker",
            "exec",
            "-u",
            "vscode",
            "api-devcontainer",
            "git",
            "-C",
            "/work",
            "worktree",
            "remove",
            "/work/.worktrees/feat-x",
            "--force",
        ]
    ]


def test_remove_when_absent_is_noop(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: called.append(a) or MagicMock(returncode=0, stdout=""),
    )
    worktree.remove(tmp_path, "nonexistent")
    assert called == []


def test_remove_passes_force_flag(tmp_path, monkeypatch):
    wt = tmp_path / ".worktrees" / "feat-x"
    wt.mkdir(parents=True)
    calls = _stub_run(monkeypatch, [(0, "")])
    worktree.remove(tmp_path, "feat-x", force=True)
    assert calls == [
        ["git", "-C", str(tmp_path), "worktree", "remove", str(wt), "--force"]
    ]


def test_remove_dirty_raises_dirty_error(tmp_path, monkeypatch):
    wt = tmp_path / ".worktrees" / "feat-x"
    wt.mkdir(parents=True)

    def fake_run(cmd, capture_output=False, text=False, check=False, input=None):
        return MagicMock(
            returncode=1,
            stdout="",
            stderr="fatal: '.worktrees/feat-x' contains modified or untracked files, use --force to delete it",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(worktree.DirtyWorktreeError, match="modified or untracked"):
        worktree.remove(tmp_path, "feat-x")


def test_remove_generic_failure_raises_plain_worktree_error(tmp_path, monkeypatch):
    wt = tmp_path / ".worktrees" / "feat-x"
    wt.mkdir(parents=True)

    def fake_run(cmd, capture_output=False, text=False, check=False, input=None):
        return MagicMock(
            returncode=128,
            stdout="",
            stderr="fatal: could not lock index",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(worktree.WorktreeError) as exc:
        worktree.remove(tmp_path, "feat-x")
    assert "lock index" in str(exc.value)
    assert not isinstance(exc.value, worktree.DirtyWorktreeError)


def test_resolve_appends_commit_ish_when_base_resolves(tmp_path, monkeypatch):
    # Override the autouse stub for this one test.
    monkeypatch.setattr(worktree, "_resolve_base", lambda *a, **kw: ("origin/main", []))
    calls = _stub_run(monkeypatch, [(0, "")])
    worktree.resolve(tmp_path, "feat-x")
    assert calls == [
        [
            "git",
            "-C",
            str(tmp_path),
            "worktree",
            "add",
            str(tmp_path / ".worktrees" / "feat-x"),
            "-B",
            "feat-x",
            "origin/main",
        ]
    ]


def _make_worktree(base: Path, name: str) -> None:
    """Create a fake worktree dir at ``base/name`` with a ``.git`` file."""
    wt = base / name
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: /tmp/fake/.git/worktrees/{wt.name}\n")


def test_list_existing_returns_top_level_and_nested_branches(tmp_path):
    repo = tmp_path / "myrepo"
    base = repo / ".worktrees"
    _make_worktree(base, "fix-bar")
    _make_worktree(base, "feat/foo")
    assert worktree.list_existing(repo) == ["feat/foo", "fix-bar"]


def test_list_existing_skips_dirs_without_git_marker(tmp_path):
    repo = tmp_path / "myrepo"
    base = repo / ".worktrees"
    _make_worktree(base, "real-branch")
    # An empty/non-worktree directory should not be reported.
    (base / "leftover").mkdir(parents=True)
    # A nested non-worktree dir below a worktree must not pollute results
    # (we should not recurse into a worktree).
    (base / "real-branch" / "subdir").mkdir()
    assert worktree.list_existing(repo) == ["real-branch"]


def test_list_existing_does_not_recurse_into_worktree(tmp_path):
    """Branch named ``feat`` must not be confused with intermediate
    namespace dir that contains nested branches: once we find ``.git`` we
    stop descending."""
    repo = tmp_path / "myrepo"
    base = repo / ".worktrees"
    _make_worktree(base, "feat")  # branch literally named "feat"
    # Even if some sub-path inside the worktree had a .git, we would have
    # already returned "feat" and stopped recursion; verify by sanity.
    assert worktree.list_existing(repo) == ["feat"]


def test_list_existing_handles_missing_worktrees_dir(tmp_path):
    repo = tmp_path / "no-worktrees-yet"
    repo.mkdir()
    assert worktree.list_existing(repo) == []


def test_list_existing_ignores_non_directory_entries(tmp_path):
    repo = tmp_path / "myrepo"
    base = repo / ".worktrees"
    base.mkdir(parents=True)
    (base / ".DS_Store").write_text("")  # mac noise
    (base / "stray.zip").write_bytes(b"\x00")
    _make_worktree(base, "kept")
    assert worktree.list_existing(repo) == ["kept"]


def test_resolve_calls_stage_info_for_substeps(monkeypatch, tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()

    class FakeProc:
        def __init__(self, code=0, stdout="", stderr=""):
            self.returncode = code
            self.stdout = stdout
            self.stderr = stderr

    def fake_git_run(args, **kw):
        # symbolic-ref --quiet refs/remotes/origin/HEAD returns origin/main
        if args[:2] == ["symbolic-ref", "--quiet"]:
            return FakeProc(0, "refs/remotes/origin/main\n")
        if args[:2] == ["fetch", "origin"]:
            return FakeProc(0)
        return FakeProc(0)

    # Restore the real _resolve_base (autouse fixture replaced it with a stub).
    monkeypatch.setattr(worktree, "_resolve_base", _REAL_RESOLVE_BASE)
    monkeypatch.setattr(worktree, "_git_run", fake_git_run)
    monkeypatch.setattr(worktree.subprocess, "run", lambda *a, **k: FakeProc(0))

    out = io.StringIO()
    r = Reporter(out=out, color=False, clock=lambda: 0.0)
    with r.stage("worktree") as st:
        worktree.resolve(repo, "feat/x", reporter_stage=st)

    text = out.getvalue()
    assert "▸ worktree — fetching origin/main" in text
    assert "▸ worktree — creating .worktrees/feat/x" in text


def test_resolve_offline_fetch_emits_warn_through_stage(monkeypatch, tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()

    class FakeProc:
        def __init__(self, code=0, stdout="", stderr=""):
            self.returncode = code
            self.stdout = stdout
            self.stderr = stderr

    def fake_git_run(args, **kw):
        if args[:2] == ["symbolic-ref", "--quiet"]:
            return FakeProc(0, "refs/remotes/origin/main\n")
        if args[:2] == ["fetch", "origin"]:
            return FakeProc(1, stderr="fatal: could not resolve host")
        if args[:2] == ["rev-parse", "--verify"]:
            return FakeProc(0)  # cached origin/main exists
        return FakeProc(0)

    # Restore the real _resolve_base (autouse fixture replaced it with a stub).
    monkeypatch.setattr(worktree, "_resolve_base", _REAL_RESOLVE_BASE)
    monkeypatch.setattr(worktree, "_git_run", fake_git_run)
    monkeypatch.setattr(worktree.subprocess, "run", lambda *a, **k: FakeProc(0))

    out = io.StringIO()
    r = Reporter(out=out, color=False, clock=lambda: 0.0)
    with r.stage("worktree") as st:
        worktree.resolve(repo, "feat/x", reporter_stage=st)

    assert "! worktree — fetch failed; using cached origin/main" in out.getvalue()
    assert r.had_warning is True


def test_resolve_skips_when_worktree_exists(monkeypatch, tmp_path):
    repo = tmp_path / "r"
    (repo / ".worktrees" / "feat/x").mkdir(parents=True)

    out = io.StringIO()
    r = Reporter(out=out, color=False, clock=lambda: 0.0)
    with r.stage("worktree") as st:
        result = worktree.resolve(repo, "feat/x", reporter_stage=st)

    assert result == repo / ".worktrees" / "feat/x"
    output = out.getvalue()
    assert "▸ worktree — reusing .worktrees/feat/x" in output
    assert "✓ worktree" not in output  # skip suppresses ✓
