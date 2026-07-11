from pathlib import Path

from tmux_agents import container, paths, tmux, windows as windows_mod
from tmux_agents.commands import vscode

CODE_BIN = "/usr/local/bin/code"


def _write_projects(tmp_config_dir: Path, body: str) -> None:
    (tmp_config_dir / "projects.toml").write_text(body)


def _write_mapping(window_id: str, **kwargs) -> None:
    paths.windows_dir().mkdir(parents=True, exist_ok=True)
    m = windows_mod.WindowMapping(
        window_id=window_id,
        project=kwargs["project"],
        branch=kwargs.get("branch"),
        host_worktree=kwargs["host_worktree"],
        pane_id=kwargs.get("pane_id", "%1"),
    )
    windows_mod.write_mapping(m)


def _stub_subprocess(monkeypatch, *, which_returns: str | None = CODE_BIN):
    called: list[list[str]] = []

    def fake_run(argv, check=True, **kw):
        called.append(argv)
        import subprocess

        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(vscode.subprocess, "run", fake_run)
    monkeypatch.setattr(vscode.shutil, "which", lambda _: which_returns)
    monkeypatch.setattr(tmux, "display_message", lambda *_: None)
    return called


def test_host_project_opens_worktree_path(monkeypatch, tmp_config_dir, tmp_path):
    repo = tmp_path / "api"
    repo.mkdir()
    worktree = repo / ".worktrees" / "feat-x"
    worktree.mkdir(parents=True)
    _write_projects(tmp_config_dir, f'[api]\nrepo = "{repo}"\n')
    _write_mapping("@1", project="api", branch="feat-x", host_worktree=worktree)

    called = _stub_subprocess(monkeypatch)
    rc = vscode.main(["--window-id", "@1"])

    assert rc == 0
    assert called == [[CODE_BIN, str(worktree)]]


def test_container_project_builds_attached_container_uri(
    monkeypatch, tmp_config_dir, tmp_path
):
    repo = tmp_path / "svc"
    repo.mkdir()
    worktree = repo / ".worktrees" / "bug"
    worktree.mkdir(parents=True)
    _write_projects(
        tmp_config_dir,
        f'[svc]\nrepo = "{repo}"\ncontainer = "svc-dev"\nup_cmd = "true"\n',
    )
    _write_mapping("@2", project="svc", branch="bug", host_worktree=worktree)
    monkeypatch.setattr(container, "current_name", lambda _: "svc-dev")

    called = _stub_subprocess(monkeypatch)
    rc = vscode.main(["--window-id", "@2"])

    assert rc == 0
    expected_hex = b"svc-dev".hex()
    expected_uri = (
        f"vscode-remote://attached-container+{expected_hex}/work/.worktrees/bug"
    )
    assert called == [[CODE_BIN, "--folder-uri", expected_uri]]


def test_devcontainer_project_uses_workspaces_path(
    monkeypatch, tmp_config_dir, tmp_path
):
    repo = tmp_path / "web"
    repo.mkdir()
    worktree = repo / ".worktrees" / "x"
    worktree.mkdir(parents=True)
    _write_projects(
        tmp_config_dir,
        f'[web]\nrepo = "{repo}"\ndevcontainer = true\n',
    )
    _write_mapping("@3", project="web", branch="x", host_worktree=worktree)
    monkeypatch.setattr(container, "current_name", lambda _: "vsc-web-abc123")

    called = _stub_subprocess(monkeypatch)
    rc = vscode.main(["--window-id", "@3"])

    assert rc == 0
    expected_hex = b"vsc-web-abc123".hex()
    expected_uri = (
        f"vscode-remote://attached-container+{expected_hex}/workspaces/web/.worktrees/x"
    )
    assert called == [[CODE_BIN, "--folder-uri", expected_uri]]


def test_container_project_with_no_running_container_fails(
    monkeypatch, tmp_config_dir, tmp_path
):
    repo = tmp_path / "svc"
    repo.mkdir()
    worktree = repo / ".worktrees" / "x"
    worktree.mkdir(parents=True)
    _write_projects(
        tmp_config_dir,
        f'[svc]\nrepo = "{repo}"\ncontainer = "svc-dev"\nup_cmd = "true"\n',
    )
    _write_mapping("@4", project="svc", branch="x", host_worktree=worktree)
    monkeypatch.setattr(container, "current_name", lambda _: None)

    called = _stub_subprocess(monkeypatch)
    rc = vscode.main(["--window-id", "@4"])

    assert rc == 1
    assert called == []


def test_missing_code_cli_and_no_fallback_fails(monkeypatch, tmp_config_dir, tmp_path):
    repo = tmp_path / "api"
    repo.mkdir()
    worktree = repo / ".worktrees" / "x"
    worktree.mkdir(parents=True)
    _write_projects(tmp_config_dir, f'[api]\nrepo = "{repo}"\n')
    _write_mapping("@5", project="api", branch="x", host_worktree=worktree)

    monkeypatch.setattr(vscode.shutil, "which", lambda _: None)
    monkeypatch.setattr(vscode.os.path, "isfile", lambda _: False)
    messages: list[str] = []
    monkeypatch.setattr(tmux, "display_message", lambda m: messages.append(m))

    rc = vscode.main(["--window-id", "@5"])
    assert rc == 1
    assert any("code_path" in m for m in messages)


def test_code_path_override_used_when_not_on_path(
    monkeypatch, tmp_config_dir, tmp_path
):
    repo = tmp_path / "api"
    repo.mkdir()
    worktree = repo / ".worktrees" / "x"
    worktree.mkdir(parents=True)
    # Real on-disk override target so os.path.isfile + os.access pass.
    override = tmp_path / "vscode-override" / "code"
    override.parent.mkdir()
    override.write_text("#!/bin/sh\nexit 0\n")
    override.chmod(0o755)
    _write_projects(
        tmp_config_dir,
        f'code_path = "{override}"\n[api]\nrepo = "{repo}"\n',
    )
    _write_mapping("@7", project="api", branch="x", host_worktree=worktree)

    called = _stub_subprocess(monkeypatch, which_returns=None)
    rc = vscode.main(["--window-id", "@7"])
    assert rc == 0
    assert called == [[str(override), str(worktree)]]


def test_unknown_window_fails(monkeypatch, tmp_config_dir, capsys):
    _write_projects(tmp_config_dir, "")
    monkeypatch.setattr(vscode.shutil, "which", lambda _: "/usr/local/bin/code")
    monkeypatch.setattr(tmux, "display_message", lambda *_: None)
    rc = vscode.main(["--window-id", "@99"])
    assert rc == 1


def test_host_branchless_uses_repo_root(monkeypatch, tmp_config_dir, tmp_path):
    repo = tmp_path / "api"
    repo.mkdir()
    _write_projects(tmp_config_dir, f'[api]\nrepo = "{repo}"\n')
    _write_mapping("@6", project="api", branch=None, host_worktree=repo)

    called = _stub_subprocess(monkeypatch)
    rc = vscode.main(["--window-id", "@6"])
    assert rc == 0
    assert called == [[CODE_BIN, str(repo)]]
