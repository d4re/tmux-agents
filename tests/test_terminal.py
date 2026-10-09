from pathlib import Path

from tmux_agents import container as container_mod, paths, tmux, windows as windows_mod
from tmux_agents.commands import terminal
from tmux_agents.ssh_forward import UDS_PATH


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


def _stub_exec(monkeypatch, *, session_exists=False):
    """Capture the hidden-session creation and the final exec without touching
    tmux (the real calls would hit the live `-L agents` server) or exec-ing."""
    execs: list[tuple[str, list[str]]] = []
    created: list[tuple[str, list[str], str | None]] = []
    messages: list[str] = []
    monkeypatch.setattr(terminal.os, "execvp", lambda f, a: execs.append((f, a)))
    monkeypatch.setattr(tmux, "term_session_exists", lambda wid: session_exists)
    monkeypatch.setattr(
        tmux,
        "create_term_session",
        lambda wid, *, argv, cwd=None: created.append((wid, argv, cwd)),
    )
    monkeypatch.setattr(tmux, "display_message", lambda m: messages.append(m))
    return execs, created, messages


def _attach(window_id: str) -> tuple[str, list[str]]:
    n = window_id.lstrip("@")
    return ("tmux", ["tmux", "-L", "agents", "attach-session", "-t", f"=_term-{n}"])


def test_host_project_starts_shell_session_in_worktree_and_attaches(
    monkeypatch, tmp_config_dir, tmp_path
):
    repo = tmp_path / "api"
    repo.mkdir()
    worktree = repo / ".worktrees" / "feat-x"
    worktree.mkdir(parents=True)
    _write_projects(tmp_config_dir, f'[api]\nrepo = "{repo}"\n')
    _write_mapping("@1", project="api", branch="feat-x", host_worktree=worktree)

    monkeypatch.setenv("SHELL", "/bin/zsh")
    execs, created, _ = _stub_exec(monkeypatch)

    rc = terminal.main(["--window-id", "@1"])

    assert rc == 0
    assert created == [("@1", ["/bin/zsh", "-il"], str(worktree))]
    assert execs == [_attach("@1")]


def test_host_falls_back_to_bash_when_shell_unset(
    monkeypatch, tmp_config_dir, tmp_path
):
    repo = tmp_path / "api"
    repo.mkdir()
    worktree = repo / ".worktrees" / "x"
    worktree.mkdir(parents=True)
    _write_projects(tmp_config_dir, f'[api]\nrepo = "{repo}"\n')
    _write_mapping("@2", project="api", branch="x", host_worktree=worktree)

    monkeypatch.delenv("SHELL", raising=False)
    execs, created, _ = _stub_exec(monkeypatch)

    rc = terminal.main(["--window-id", "@2"])

    assert rc == 0
    assert created == [("@2", ["/bin/bash", "-il"], str(worktree))]
    assert execs == [_attach("@2")]


def test_container_with_forward_ssh_runs_docker_exec_with_socket(
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
    _write_mapping("@3", project="svc", branch="bug", host_worktree=worktree)
    monkeypatch.setattr(container_mod, "current_name", lambda _: "svc-dev")

    execs, created, _ = _stub_exec(monkeypatch)

    rc = terminal.main(["--window-id", "@3"])

    assert rc == 0
    assert execs == [_attach("@3")]
    assert created == [
        (
            "@3",
            [
                "docker",
                "exec",
                "-it",
                "-e",
                "TERM",
                "-e",
                "COLORTERM",
                "-e",
                "TMUX_PANE",
                "-e",
                f"SSH_AUTH_SOCK={UDS_PATH}",
                "-u",
                "vscode",
                "-w",
                "/work/.worktrees/bug",
                "svc-dev",
                "bash",
                "-il",
            ],
            None,  # no host cwd: the shell lives in the container
        )
    ]


def test_container_without_forward_ssh_omits_socket(
    monkeypatch, tmp_config_dir, tmp_path
):
    repo = tmp_path / "svc"
    repo.mkdir()
    worktree = repo / ".worktrees" / "x"
    worktree.mkdir(parents=True)
    _write_projects(
        tmp_config_dir,
        f'[svc]\nrepo = "{repo}"\ncontainer = "svc-dev"\nup_cmd = "true"\n'
        "forward_ssh_agent = false\n",
    )
    _write_mapping("@4", project="svc", branch="x", host_worktree=worktree)
    monkeypatch.setattr(container_mod, "current_name", lambda _: "svc-dev")

    _, created, _ = _stub_exec(monkeypatch)

    rc = terminal.main(["--window-id", "@4"])

    assert rc == 0
    _, argv, _ = created[0]
    assert argv[0] == "docker"
    assert f"SSH_AUTH_SOCK={UDS_PATH}" not in argv
    e_flag_indices = [i for i, a in enumerate(argv) if a == "-e"]
    e_values = [argv[i + 1] for i in e_flag_indices]
    assert e_values == ["TERM", "COLORTERM", "TMUX_PANE"]


def test_container_custom_user_overrides_default(monkeypatch, tmp_config_dir, tmp_path):
    repo = tmp_path / "svc"
    repo.mkdir()
    worktree = repo / ".worktrees" / "x"
    worktree.mkdir(parents=True)
    _write_projects(
        tmp_config_dir,
        f'[svc]\nrepo = "{repo}"\ncontainer = "svc-dev"\nup_cmd = "true"\n'
        'user = "node"\n',
    )
    _write_mapping("@5", project="svc", branch="x", host_worktree=worktree)
    monkeypatch.setattr(container_mod, "current_name", lambda _: "svc-dev")

    _, created, _ = _stub_exec(monkeypatch)

    rc = terminal.main(["--window-id", "@5"])

    assert rc == 0
    _, argv, _ = created[0]
    u_index = argv.index("-u")
    assert argv[u_index + 1] == "node"


def test_devcontainer_uses_workspaces_workdir(monkeypatch, tmp_config_dir, tmp_path):
    repo = tmp_path / "web"
    repo.mkdir()
    worktree = repo / ".worktrees" / "x"
    worktree.mkdir(parents=True)
    _write_projects(
        tmp_config_dir,
        f'[web]\nrepo = "{repo}"\ndevcontainer = true\n',
    )
    _write_mapping("@6", project="web", branch="x", host_worktree=worktree)
    monkeypatch.setattr(container_mod, "current_name", lambda _: "vsc-web-abc123")

    _, created, _ = _stub_exec(monkeypatch)

    rc = terminal.main(["--window-id", "@6"])

    assert rc == 0
    _, argv, _ = created[0]
    w_index = argv.index("-w")
    assert argv[w_index + 1] == "/workspaces/web/.worktrees/x"
    assert argv[-3:] == ["vsc-web-abc123", "bash", "-il"]


def test_missing_window_mapping_fails_with_message(monkeypatch, tmp_config_dir):
    _write_projects(tmp_config_dir, "")
    execs, created, messages = _stub_exec(monkeypatch)

    rc = terminal.main(["--window-id", "@99"])

    assert rc == 1
    assert created == []
    assert execs == []
    assert any("@99" in m for m in messages)


def test_project_not_in_toml_fails_with_message(monkeypatch, tmp_config_dir, tmp_path):
    _write_projects(tmp_config_dir, "")
    _write_mapping(
        "@8",
        project="ghost",
        branch="x",
        host_worktree=tmp_path / "nope",
    )
    execs, created, messages = _stub_exec(monkeypatch)

    rc = terminal.main(["--window-id", "@8"])

    assert rc == 1
    assert created == []
    assert execs == []
    assert any("ghost" in m for m in messages)


def test_container_not_running_fails_with_message(
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
    _write_mapping("@9", project="svc", branch="x", host_worktree=worktree)
    monkeypatch.setattr(container_mod, "current_name", lambda _: None)

    execs, created, messages = _stub_exec(monkeypatch)

    rc = terminal.main(["--window-id", "@9"])

    assert rc == 1
    assert created == []
    assert execs == []
    assert any("no running container" in m for m in messages)


def test_sandbox_project_runs_sbx_exec_not_host_shell(
    monkeypatch, tmp_config_dir, tmp_path
):
    """A host shell for a sandbox project would be a silent isolation hole."""
    repo = tmp_path / "svc"
    repo.mkdir()
    worktree = repo / ".worktrees" / "feat-x"
    worktree.mkdir(parents=True)
    _write_projects(tmp_config_dir, f'[svc]\nrepo = "{repo}"\nsandbox = true\n')
    _write_mapping("@1", project="svc", branch="feat-x", host_worktree=worktree)

    execs, created, _ = _stub_exec(monkeypatch)

    rc = terminal.main(["--window-id", "@1"])

    assert rc == 0
    assert execs == [_attach("@1")]
    ((_, argv, cwd),) = created
    assert cwd is None  # a host cwd would mean a host shell
    assert argv[:3] == ["sbx", "exec", "-it"]
    assert "-e" in argv and "TMUX_PANE" in argv
    assert "svc" in argv
    body = argv[-1]
    assert body.startswith(f"cd '{worktree}'") or body.startswith(f"cd {worktree}")
    assert body.endswith("exec bash -il")


def test_existing_session_reattaches_without_respawning_shell(
    monkeypatch, tmp_config_dir
):
    """The point of the hidden session: reopening the popup returns to the
    same shell (history, cwd, running command). No mapping/project/container
    resolution either — the shell is already there, so reopening is instant."""
    _write_projects(tmp_config_dir, "")
    execs, created, messages = _stub_exec(monkeypatch, session_exists=True)

    rc = terminal.main(["--window-id", "@7"])

    assert rc == 0
    assert created == []
    assert messages == []
    assert execs == [_attach("@7")]
