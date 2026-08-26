import subprocess
from unittest.mock import MagicMock

from tmux_agents import gh_auth

# Direct imports: the conftest `_no_real_gh_sync` autouse fixture replaces the
# `gh_auth.maybe_sync_gh_auth` module attribute, but these bindings keep the
# real function under test. Prereq monkeypatches on gh_auth.* still apply —
# the function resolves its collaborators through module globals at call time.
from tmux_agents.gh_auth import SyncResult, maybe_sync_gh_auth


# ---------------------------------------------------------------------------
# host_gh_token
# ---------------------------------------------------------------------------


def test_host_gh_token_returns_stripped_token(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="gho_abc123\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert gh_auth.host_gh_token() == "gho_abc123"
    assert calls == [["gh", "auth", "token", "--hostname", "github.com"]]


def test_host_gh_token_none_when_not_logged_in(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: MagicMock(returncode=1, stdout="")
    )
    assert gh_auth.host_gh_token() is None


def test_host_gh_token_none_when_gh_missing(monkeypatch):
    def fake_run(cmd, **kw):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert gh_auth.host_gh_token() is None


def test_host_gh_token_none_on_timeout(monkeypatch):
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert gh_auth.host_gh_token() is None


# ---------------------------------------------------------------------------
# has_gh_in_container
# ---------------------------------------------------------------------------


def test_has_gh_in_container_true(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="gh version 2.60.0\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert gh_auth.has_gh_in_container("api", "vscode") is True
    assert calls == [["docker", "exec", "-u", "vscode", "api", "gh", "--version"]]


def test_has_gh_in_container_false_on_nonzero(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: MagicMock(returncode=1, stdout="")
    )
    assert gh_auth.has_gh_in_container("api") is False


def test_has_gh_in_container_false_on_timeout(monkeypatch):
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert gh_auth.has_gh_in_container("api") is False


# ---------------------------------------------------------------------------
# _login — the token travels via stdin, never argv
# ---------------------------------------------------------------------------


def test_login_pipes_token_via_stdin_not_argv(monkeypatch):
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["kwargs"] = kw
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert gh_auth._login("api", "node", "gho_secret") is True
    cmd = captured["cmd"]
    assert cmd == [
        "docker",
        "exec",
        "-i",
        "-u",
        "node",
        "api",
        "gh",
        "auth",
        "login",
        "--with-token",
        "--hostname",
        "github.com",
    ]
    assert "gho_secret" not in " ".join(cmd)
    assert captured["kwargs"]["input"] == "gho_secret"


def test_login_false_on_nonzero(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: MagicMock(returncode=1, stdout="", stderr="bad token"),
    )
    assert gh_auth._login("api", "vscode", "gho_x") is False


def test_login_false_on_timeout(monkeypatch):
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert gh_auth._login("api", "vscode", "gho_x") is False


# ---------------------------------------------------------------------------
# maybe_sync_gh_auth — outcome mapping
# ---------------------------------------------------------------------------


def test_maybe_sync_disabled_when_no_host_gh(monkeypatch):
    monkeypatch.setattr(gh_auth, "host_gh_installed", lambda: False)
    assert maybe_sync_gh_auth("api") == SyncResult("disabled_no_host_gh")


def test_maybe_sync_disabled_when_not_logged_in(monkeypatch):
    monkeypatch.setattr(gh_auth, "host_gh_installed", lambda: True)
    monkeypatch.setattr(gh_auth, "host_gh_token", lambda: None)
    assert maybe_sync_gh_auth("api") == SyncResult("disabled_not_logged_in")


def test_maybe_sync_disabled_when_no_container_gh(monkeypatch):
    monkeypatch.setattr(gh_auth, "host_gh_installed", lambda: True)
    monkeypatch.setattr(gh_auth, "host_gh_token", lambda: "gho_x")
    monkeypatch.setattr(gh_auth, "has_gh_in_container", lambda c, u: False)
    assert maybe_sync_gh_auth("api") == SyncResult("disabled_no_container_gh")


def test_maybe_sync_disabled_on_empty_container_name(monkeypatch):
    assert maybe_sync_gh_auth("") == SyncResult("disabled_no_container_gh")


def test_maybe_sync_synced(monkeypatch):
    logins = []
    monkeypatch.setattr(gh_auth, "host_gh_installed", lambda: True)
    monkeypatch.setattr(gh_auth, "host_gh_token", lambda: "gho_x")
    monkeypatch.setattr(gh_auth, "has_gh_in_container", lambda c, u: True)
    monkeypatch.setattr(
        gh_auth, "_login", lambda c, u, t: logins.append((c, u, t)) or True
    )
    assert maybe_sync_gh_auth("api", "node") == SyncResult("synced")
    assert logins == [("api", "node", "gho_x")]


def test_maybe_sync_failed_when_login_fails(monkeypatch):
    monkeypatch.setattr(gh_auth, "host_gh_installed", lambda: True)
    monkeypatch.setattr(gh_auth, "host_gh_token", lambda: "gho_x")
    monkeypatch.setattr(gh_auth, "has_gh_in_container", lambda c, u: True)
    monkeypatch.setattr(gh_auth, "_login", lambda c, u, t: False)
    assert maybe_sync_gh_auth("api") == SyncResult("failed")


# ---------------------------------------------------------------------------
# SyncResult.render — stage-method mapping (mirrors test_pumpresult_render)
# ---------------------------------------------------------------------------


def test_render_disabled_no_host_gh_calls_warn():
    st = MagicMock()
    SyncResult("disabled_no_host_gh").render(st)
    st.warn.assert_called_once_with("gh not installed on host (auth sharing disabled)")


def test_render_disabled_not_logged_in_calls_warn():
    st = MagicMock()
    SyncResult("disabled_not_logged_in").render(st)
    st.warn.assert_called_once_with("gh not logged in on host (auth sharing disabled)")


def test_render_disabled_no_container_gh_calls_warn():
    st = MagicMock()
    SyncResult("disabled_no_container_gh").render(st)
    st.warn.assert_called_once_with("gh missing in container (auth sharing disabled)")


def test_render_synced_calls_info():
    st = MagicMock()
    SyncResult("synced").render(st)
    st.info.assert_called_once_with("token synced")


def test_render_failed_calls_warn():
    st = MagicMock()
    SyncResult("failed").render(st)
    st.warn.assert_called_once_with("token sync failed (see log)")
