from pathlib import Path

from tmux_agents import container, paths, sandbox, tmux, windows as windows_mod
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


# ===== Sandbox backend =====


def _sandbox_setup(tmp_config_dir, tmp_path):
    repo = tmp_path / "svc"
    repo.mkdir()
    worktree = repo / ".worktrees" / "feat-x"
    worktree.mkdir(parents=True)
    _write_projects(tmp_config_dir, f'[svc]\nrepo = "{repo}"\nsandbox = true\n')
    _write_mapping("@1", project="svc", branch="feat-x", host_worktree=worktree)
    return worktree


def test_sandbox_project_uses_remote_ssh(monkeypatch, tmp_config_dir, tmp_path):
    worktree = _sandbox_setup(tmp_config_dir, tmp_path)
    called = _stub_subprocess(monkeypatch)
    monkeypatch.setattr(vscode, "_ssh_host_configured", lambda host: True)
    monkeypatch.setattr(sandbox, "network_allowed", lambda name, host: True)

    rc = vscode.main(["--window-id", "@1"])

    assert rc == 0
    assert called == [[CODE_BIN, "--remote", "ssh-remote+svc.sbx", str(worktree)]]


def test_sandbox_ssh_unconfigured_prints_fix(
    monkeypatch, tmp_config_dir, tmp_path, capsys
):
    _sandbox_setup(tmp_config_dir, tmp_path)
    called = _stub_subprocess(monkeypatch)
    monkeypatch.setattr(vscode, "_ssh_host_configured", lambda host: False)
    monkeypatch.setattr(
        sandbox,
        "network_allowed",
        lambda *_: (_ for _ in ()).throw(AssertionError("ssh check comes first")),
    )

    rc = vscode.main(["--window-id", "@1"])

    assert rc == 1
    assert called == []
    assert "sbx setup ssh" in capsys.readouterr().err


def test_sandbox_blocked_vscode_egress_prints_allow_fix(
    monkeypatch, tmp_config_dir, tmp_path, capsys
):
    """Remote-SSH scp's only the CLI; the CLI downloads the server from
    inside the VM. Under deny-all that 403s and the window opens empty —
    fail fast with the exact `sbx policy allow` instead."""
    _sandbox_setup(tmp_config_dir, tmp_path)
    called = _stub_subprocess(monkeypatch)
    monkeypatch.setattr(vscode, "_ssh_host_configured", lambda host: True)
    probed: list[tuple[str, str]] = []

    def fake_allowed(name, host):
        probed.append((name, host))
        return host != "update.code.visualstudio.com"

    monkeypatch.setattr(sandbox, "network_allowed", fake_allowed)

    rc = vscode.main(["--window-id", "@1"])

    assert rc == 1
    assert called == []
    assert [n for n, _ in probed] == ["svc"] * len(vscode.VSCODE_SERVER_HOSTS)
    err = capsys.readouterr().err
    assert "update.code.visualstudio.com" in err
    assert (
        'sbx policy allow network --sandbox svc "update.code.visualstudio.com"' in err
    )
    assert "--local" in err


def test_sandbox_egress_probe_unknown_does_not_block(
    monkeypatch, tmp_config_dir, tmp_path
):
    """A probe that can't decide (sbx missing, daemon down, sandbox not yet
    created) must not veto the attach — SSH itself will report the real error."""
    worktree = _sandbox_setup(tmp_config_dir, tmp_path)
    called = _stub_subprocess(monkeypatch)
    monkeypatch.setattr(vscode, "_ssh_host_configured", lambda host: True)
    monkeypatch.setattr(sandbox, "network_allowed", lambda name, host: None)

    rc = vscode.main(["--window-id", "@1"])

    assert rc == 0
    assert called == [[CODE_BIN, "--remote", "ssh-remote+svc.sbx", str(worktree)]]


def test_sandbox_local_flag_opens_host_folder(monkeypatch, tmp_config_dir, tmp_path):
    """Passthrough means host paths are the same files — useful when the
    experimental SSH support is down."""
    worktree = _sandbox_setup(tmp_config_dir, tmp_path)
    called = _stub_subprocess(monkeypatch)
    monkeypatch.setattr(
        vscode,
        "_ssh_host_configured",
        lambda host: (_ for _ in ()).throw(AssertionError("--local must skip SSH")),
    )
    monkeypatch.setattr(
        sandbox,
        "network_allowed",
        lambda *_: (_ for _ in ()).throw(AssertionError("--local must skip egress")),
    )

    rc = vscode.main(["--window-id", "@1", "--local"])

    assert rc == 0
    assert called == [[CODE_BIN, str(worktree)]]


def test_ssh_host_configured_parses_ssh_g(monkeypatch):
    import subprocess as _subprocess

    def run_hit(argv, **kw):
        assert argv[:2] == ["ssh", "-G"]
        return _subprocess.CompletedProcess(
            argv, 0, "user remi\nhostname 127.0.0.1\nproxycommand something\n", ""
        )

    monkeypatch.setattr(vscode.subprocess, "run", run_hit)
    assert vscode._ssh_host_configured("acg.sbx") is True

    def run_miss(argv, **kw):
        return _subprocess.CompletedProcess(
            argv, 0, "user remi\nhostname acg.sbx\n", ""
        )

    monkeypatch.setattr(vscode.subprocess, "run", run_miss)
    assert vscode._ssh_host_configured("acg.sbx") is False
