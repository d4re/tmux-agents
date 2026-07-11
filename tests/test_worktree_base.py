"""Tests for worktree._resolve_base — base-branch resolution."""
from unittest.mock import MagicMock
import subprocess

import pytest

from tmux_agents import worktree


def _route(monkeypatch, routes, *, default=(1, "", "")):
    """Stub subprocess.run with token-based routing.

    `routes` maps a single token (e.g. 'symbolic-ref', 'set-head', 'fetch',
    'rev-parse', 'config') to (returncode, stdout, stderr). The first token
    that appears anywhere in argv wins. argv that matches no token gets
    `default` (returncode=1, empty out/err).

    Returns the calls list (mutated as calls happen)."""
    calls = []
    def fake_run(cmd, capture_output=False, text=False, check=False, input=None):
        calls.append(cmd)
        for token, (rc, out, err) in routes.items():
            if token in cmd:
                return MagicMock(returncode=rc, stdout=out, stderr=err)
        rc, out, err = default
        return MagicMock(returncode=rc, stdout=out, stderr=err)
    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


# ----- success paths: (routes, override, expected_commit_ish, expected_warning_substr) -----
SUCCESS_CASES = [
    # 1. override + fetch ok → origin/<override>
    ({"remote": (0, "git@github.com:foo/bar.git\n", ""),
      "fetch":  (0, "", "")},
     "develop", "origin/develop", None),

    # 2. override + fetch fail + cached origin/<override> → cached
    ({"remote":    (0, "git@github.com:foo/bar.git\n", ""),
      "fetch":     (1, "", "Could not resolve host"),
      "rev-parse": (0, "deadbeef\n", "")},
     "develop", "origin/develop", "fetch failed"),

    # 4. override + no origin + local exists → local <override>
    ({"remote":    (1, "", "fatal: No such remote 'origin'"),
      "rev-parse": (0, "deadbeef\n", "")},
     "develop", "develop", None),

    # 6. no override + local symref set → origin/<head>
    ({"symbolic-ref": (0, "refs/remotes/origin/main\n", ""),
      "fetch":        (0, "", "")},
     None, "origin/main", None),

    # 11. auto-detect + fetch fail + cached → origin/<head> + warning
    ({"symbolic-ref": (0, "refs/remotes/origin/main\n", ""),
      "fetch":        (1, "", "Could not resolve host"),
      "rev-parse":    (0, "deadbeef\n", "")},
     None, "origin/main", "fetch failed"),
]


@pytest.mark.parametrize("routes,override,expected_commit_ish,warning_substr", SUCCESS_CASES)
def test_resolve_base_success(tmp_path, monkeypatch, routes, override,
                              expected_commit_ish, warning_substr):
    _route(monkeypatch, routes)
    commit_ish, warnings = worktree._resolve_base(
        tmp_path, override, container=None, container_workdir=None, container_user="vscode")
    assert commit_ish == expected_commit_ish
    if warning_substr is None:
        assert warnings == []
    else:
        assert any(warning_substr in w for w in warnings)


# ----- failure paths: (routes, override, expected_exception_match) -----
FAILURE_CASES = [
    # 3. override + fetch fail + no cache → raises
    ({"remote":    (0, "git@github.com:foo/bar.git\n", ""),
      "fetch":     (1, "", "Could not resolve host"),
      "rev-parse": (1, "", "unknown revision")},
     "develop", "develop"),

    # 5. override + no origin + no local → raises
    ({"remote":    (1, "", "fatal: No such remote 'origin'"),
      "rev-parse": (1, "", "unknown revision")},
     "develop", "develop"),

    # 12. auto-detect + fetch fail + no cache → raises
    ({"symbolic-ref": (0, "refs/remotes/origin/main\n", ""),
      "fetch":        (1, "", "Could not resolve host"),
      "rev-parse":    (1, "", "unknown revision")},
     None, "main"),

    # 13. non-offline fetch failure with cache → hard fail (cache reserved for offline)
    ({"remote":    (0, "git@github.com:foo/bar.git\n", ""),
      "fetch":     (1, "", "fatal: Authentication failed for 'https://github.com/foo/bar.git/'"),
      "rev-parse": (0, "deadbeef\n", "")},
     "develop", "Authentication failed"),

    # 14. non-offline fetch failure with no cache → hard fail with original stderr
    ({"symbolic-ref": (0, "refs/remotes/origin/main\n", ""),
      "fetch":        (1, "", "fatal: couldn't find remote ref refs/heads/main"),
      "rev-parse":    (1, "", "unknown revision")},
     None, "couldn't find remote ref"),
]


@pytest.mark.parametrize("routes,override,match", FAILURE_CASES)
def test_resolve_base_failure(tmp_path, monkeypatch, routes, override, match):
    _route(monkeypatch, routes)
    with pytest.raises(worktree.WorktreeError, match=match):
        worktree._resolve_base(tmp_path, override, container=None, container_workdir=None, container_user="vscode")


# ----- 9. No override + nothing usable → None + warning (special: empty routes) -----
def test_resolve_base_auto_nothing_usable(tmp_path, monkeypatch):
    _route(monkeypatch, {})  # default returns rc=1 for everything
    commit_ish, warnings = worktree._resolve_base(
        tmp_path, None, container=None, container_workdir=None, container_user="vscode")
    assert commit_ish is None
    assert any("no usable base" in w.lower() for w in warnings)


# ----- 7. No override + symref missing + set-head succeeds → origin/<head> -----
# Sequence-based: order matters and includes a `set-head` call we verify.
def test_resolve_base_auto_set_head_succeeds(tmp_path, monkeypatch):
    seq = iter([
        MagicMock(returncode=1, stdout="", stderr=""),                              # symbolic-ref #1
        MagicMock(returncode=0, stdout="git@github.com:foo/bar.git\n", stderr=""),  # remote get-url
        MagicMock(returncode=0, stdout="", stderr=""),                              # remote set-head
        MagicMock(returncode=0, stdout="refs/remotes/origin/main\n", stderr=""),    # symbolic-ref #2
        MagicMock(returncode=0, stdout="", stderr=""),                              # fetch
    ])
    calls = []
    def fake_run(cmd, capture_output=False, text=False, check=False, input=None):
        calls.append(cmd)
        return next(seq)
    monkeypatch.setattr(subprocess, "run", fake_run)
    commit_ish, warnings = worktree._resolve_base(
        tmp_path, None, container=None, container_workdir=None, container_user="vscode")
    assert commit_ish == "origin/main"
    assert warnings == []
    assert any("set-head" in c for c in calls)


# ----- 8. No override + symref missing + set-head fails + init.defaultBranch → local -----
def test_resolve_base_auto_init_default_branch_local(tmp_path, monkeypatch):
    seq = iter([
        MagicMock(returncode=1, stdout="", stderr=""),                              # symbolic-ref
        MagicMock(returncode=0, stdout="git@github.com:foo/bar.git\n", stderr=""),  # remote get-url (origin exists)
        MagicMock(returncode=1, stdout="", stderr="Could not resolve host"),        # remote set-head fails
        MagicMock(returncode=0, stdout="main\n", stderr=""),                        # config init.defaultBranch
        MagicMock(returncode=0, stdout="deadbeef\n", stderr=""),                    # rev-parse refs/heads/main
    ])
    def fake_run(cmd, capture_output=False, text=False, check=False, input=None):
        return next(seq)
    monkeypatch.setattr(subprocess, "run", fake_run)
    commit_ish, warnings = worktree._resolve_base(
        tmp_path, None, container=None, container_workdir=None, container_user="vscode")
    assert commit_ish == "main"
    assert warnings == []


# ----- 10. Container project: every git invocation goes through `docker exec` -----
def test_resolve_base_container_uses_docker_exec(tmp_path, monkeypatch):
    calls = _route(monkeypatch, {
        "remote": (0, "git@github.com:foo/bar.git\n", ""),
        "fetch":  (0, "", ""),
    })
    commit_ish, _warnings = worktree._resolve_base(
        tmp_path, "develop",
        container="api-devcontainer", container_workdir="/work", container_user="vscode",
    )
    assert commit_ish == "origin/develop"
    for cmd in calls:
        assert cmd[:2] == ["docker", "exec"]
        assert any(a.startswith("SSH_AUTH_SOCK=") for a in cmd)
        assert any(a.startswith("GIT_SSH_COMMAND=") for a in cmd)
        assert "api-devcontainer" in cmd
        i = cmd.index("api-devcontainer")
        assert cmd[i:i + 4] == ["api-devcontainer", "git", "-C", "/work"]
