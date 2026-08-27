import pytest
from pathlib import Path
from tmux_agents import config


def _write(tmp_path, body: str, *, name="p.toml") -> Path:
    """Write a snippet (just the body — no `[section]` header) and return the path."""
    p = tmp_path / name
    p.write_text(body)
    return p


# Default templates assembled by config.load() for container projects.
DEFAULT_CONTAINER_EXEC = (
    "docker exec -it -e TERM -e COLORTERM -e TMUX_PANE -e TMUX_AGENTS_AGENT=1 "
    "-u vscode {container} "
    "bash -lc 'export SSH_AUTH_SOCK=/tmp/tmux-agents-ssh.sock && "
    "cd {workdir} && exec claude{resume_args}'"
)
DEFAULT_CONTAINER_EXEC_NO_SSH = (
    "docker exec -it -e TERM -e COLORTERM -e TMUX_PANE -e TMUX_AGENTS_AGENT=1 "
    "-u vscode {container} "
    "bash -lc 'cd {workdir} && exec claude{resume_args}'"
)
DEFAULT_UP_CMD = "cd {repo} && devcontainer up --workspace-folder ."


# ---------------------------------------------------------------------------
# Fixture-based smoke tests (use the bundled projects_example.toml)
# ---------------------------------------------------------------------------


def test_load_returns_all_projects(fixtures_dir):
    projects = config.load(fixtures_dir / "projects_example.toml")
    assert set(projects) == {"api", "scripts"}


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        config.load(tmp_path / "nope.toml")


def test_project_container_fields(fixtures_dir):
    p = config.load(fixtures_dir / "projects_example.toml")["api"]
    assert p.repo == Path("/Users/remi/dev/api")
    assert p.container == "api-devcontainer"
    assert p.container_workdir == "/work"
    assert p.up_cmd is not None
    assert p.is_container


def test_project_host_only(fixtures_dir):
    p = config.load(fixtures_dir / "projects_example.toml")["scripts"]
    assert p.container is None
    assert p.up_cmd is None
    assert not p.is_container


def test_substitute_container(fixtures_dir):
    p = config.load(fixtures_dir / "projects_example.toml")["api"]
    assert p.workdir_for(None) == "/work"
    assert p.workdir_for("feat-x") == "/work/.worktrees/feat-x"
    cmd = p.substitute(p.exec_cmd, branch="feat-x")
    assert (
        cmd
        == "docker exec -it api-devcontainer bash -lc 'cd /work/.worktrees/feat-x && claude'"
    )


def test_substitute_host_only(fixtures_dir):
    p = config.load(fixtures_dir / "projects_example.toml")["scripts"]
    assert p.workdir_for(None) == "/Users/remi/dev/scripts"
    assert p.workdir_for("hotfix") == "/Users/remi/dev/scripts/.worktrees/hotfix"
    cmd = p.substitute(p.exec_cmd, branch="hotfix")
    assert cmd == "cd /Users/remi/dev/scripts/.worktrees/hotfix && claude"


def test_substitute_up_cmd(fixtures_dir):
    p = config.load(fixtures_dir / "projects_example.toml")["api"]
    assert (
        p.substitute(p.up_cmd, branch=None)
        == "cd /Users/remi/dev/api && devcontainer up --workspace-folder ."
    )


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,match",
    [
        # Missing required `repo`:
        ('[foo]\nexec_cmd = "claude"\n', "repo"),
        # Both container and devcontainer set:
        (
            '[a]\nrepo = "/x"\nexec_cmd = "c"\ncontainer = "n"\ndevcontainer = true\n',
            "either 'container' or 'devcontainer",
        ),
        # `user` field with an explicit exec_cmd is a contradiction:
        (
            '[a]\nrepo = "/x"\ndevcontainer = true\nuser = "node"\nexec_cmd = "custom"\n',
            "user",
        ),
    ],
    ids=["missing_repo", "container_and_devcontainer", "user_with_custom_exec_cmd"],
)
def test_load_raises_config_error(tmp_path, body, match):
    p = _write(tmp_path, body)
    with pytest.raises(config.ConfigError, match=match):
        config.load(p)


# ---------------------------------------------------------------------------
# Default exec_cmd / up_cmd assembly per project shape
# ---------------------------------------------------------------------------

# (body, expected_exec_cmd, expected_up_cmd_or_None)
DEFAULT_CASES = [
    # Host-only: gets the bare claude template, no up_cmd.
    (
        '[scripts]\nrepo = "/x/scripts"\n',
        "cd {workdir} && TMUX_AGENTS_AGENT=1 exec claude{resume_args}",
        None,
    ),
    # devcontainer=true: full template + default up.
    (
        '[webapp]\nrepo = "/Users/me/dev/webapp"\ndevcontainer = true\n',
        DEFAULT_CONTAINER_EXEC,
        DEFAULT_UP_CMD,
    ),
    # Explicit container + workdir: same default template applies, plus default up.
    (
        '[a]\nrepo = "/x"\ncontainer = "foo"\ncontainer_workdir = "/w"\n',
        DEFAULT_CONTAINER_EXEC,
        DEFAULT_UP_CMD,
    ),
    # forward_ssh_agent = false: drops the SSH_AUTH_SOCK export from the default.
    (
        '[a]\nrepo = "/x"\ndevcontainer = true\nforward_ssh_agent = false\n',
        DEFAULT_CONTAINER_EXEC_NO_SSH,
        DEFAULT_UP_CMD,
    ),
]


@pytest.mark.parametrize(
    "body,exec_cmd,up_cmd",
    DEFAULT_CASES,
    ids=["host_only", "devcontainer", "explicit_container", "no_ssh_forward"],
)
def test_default_exec_and_up_cmd(tmp_path, body, exec_cmd, up_cmd):
    proj = next(iter(config.load(_write(tmp_path, body)).values()))
    assert proj.exec_cmd == exec_cmd
    assert proj.up_cmd == up_cmd


# Explicit user/exec_cmd overrides bypass the default-template logic.
@pytest.mark.parametrize(
    "body,assertion",
    [
        # Explicit exec_cmd wins over default.
        (
            '[a]\nrepo = "/x"\ndevcontainer = true\nexec_cmd = "docker exec -it -u node {container} zsh"\n',
            lambda p: p.exec_cmd == "docker exec -it -u node {container} zsh",
        ),
        # Explicit up_cmd wins over default.
        (
            '[a]\nrepo = "/x"\ndevcontainer = true\nup_cmd = "echo custom"\n',
            lambda p: p.up_cmd == "echo custom",
        ),
        # forward_ssh_agent=true with a custom exec_cmd: template is left alone.
        (
            '[a]\nrepo = "/x"\ndevcontainer = true\nforward_ssh_agent = true\n'
            'exec_cmd = "docker exec -it {container} zsh"\n',
            lambda p: (
                p.exec_cmd == "docker exec -it {container} zsh"
                and p.forward_ssh_agent is True
            ),
        ),
    ],
    ids=["explicit_exec_cmd", "explicit_up_cmd", "custom_exec_cmd_unaltered"],
)
def test_explicit_overrides_default(tmp_path, body, assertion):
    proj = next(iter(config.load(_write(tmp_path, body)).values()))
    assert assertion(proj)


# ---------------------------------------------------------------------------
# `user` field: rewrites -u <name> in the default template
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,expected_user,expected_flag",
    [
        # devcontainer + user override:
        ('[a]\nrepo = "/x"\ndevcontainer = true\nuser = "node"\n', "node", "-u node"),
        # devcontainer + default:
        ('[a]\nrepo = "/x"\ndevcontainer = true\n', None, "-u vscode"),
        # explicit container + user override:
        (
            '[a]\nrepo = "/x"\ncontainer = "foo"\ncontainer_workdir = "/w"\nuser = "node"\n',
            "node",
            "-u node",
        ),
    ],
)
def test_user_field_in_default_template(tmp_path, body, expected_user, expected_flag):
    proj = next(iter(config.load(_write(tmp_path, body)).values()))
    assert proj.user == expected_user
    assert expected_flag in proj.exec_cmd
    if expected_user == "node":
        assert "-u vscode" not in proj.exec_cmd


# ---------------------------------------------------------------------------
# forward_ssh_agent flag — defaults true everywhere, can be turned off.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,expected",
    [
        ('[a]\nrepo = "/x"\ndevcontainer = true\n', True),  # devcontainer default
        (
            '[a]\nrepo = "/x"\ncontainer = "foo"\ncontainer_workdir = "/w"\n',
            True,
        ),  # explicit container default
        (
            '[scripts]\nrepo = "/x"\nexec_cmd = "claude"\n',
            True,
        ),  # host-only default (inert)
        (
            '[a]\nrepo = "/x"\ndevcontainer = true\nforward_ssh_agent = false\n',
            False,
        ),  # explicit off
    ],
)
def test_forward_ssh_agent_flag(tmp_path, body, expected):
    proj = next(iter(config.load(_write(tmp_path, body)).values()))
    assert proj.forward_ssh_agent is expected


# ---------------------------------------------------------------------------
# share_gh_auth flag — defaults true everywhere, can be turned off.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,expected",
    [
        ('[a]\nrepo = "/x"\ndevcontainer = true\n', True),  # devcontainer default
        (
            '[a]\nrepo = "/x"\ncontainer = "foo"\ncontainer_workdir = "/w"\n',
            True,
        ),  # explicit container default
        (
            '[scripts]\nrepo = "/x"\nexec_cmd = "claude"\n',
            True,
        ),  # host-only default (inert)
        (
            '[a]\nrepo = "/x"\ndevcontainer = true\nshare_gh_auth = false\n',
            False,
        ),  # explicit off
    ],
)
def test_share_gh_auth_flag(tmp_path, body, expected):
    proj = next(iter(config.load(_write(tmp_path, body)).values()))
    assert proj.share_gh_auth is expected


# ---------------------------------------------------------------------------
# devcontainer workdir resolution
# ---------------------------------------------------------------------------


def test_devcontainer_workdir_defaults_to_workspaces_basename(tmp_path):
    p = _write(
        tmp_path,
        "[webapp-gateway]\n"
        'repo = "/Users/me/dev/webapp-gateway-service"\n'
        "devcontainer = true\n"
        "exec_cmd = \"docker exec -it {container} bash -lc 'cd {workdir} && claude'\"\n",
    )
    proj = config.load(p)["webapp-gateway"]
    assert proj.workdir_for(None) == "/workspaces/webapp-gateway-service"
    assert (
        proj.workdir_for("feat-x")
        == "/workspaces/webapp-gateway-service/.worktrees/feat-x"
    )


def test_devcontainer_explicit_workdir_overrides_default(tmp_path):
    p = _write(
        tmp_path,
        '[a]\nrepo = "/Users/me/dev/a"\ndevcontainer = true\n'
        'container_workdir = "/custom/path"\n'
        'exec_cmd = "c"\n',
    )
    assert config.load(p)["a"].workdir_for(None) == "/custom/path"


def test_devcontainer_field_marks_is_container(tmp_path):
    p = _write(
        tmp_path,
        '[a]\nrepo = "/Users/me/dev/a"\ndevcontainer = true\n'
        'exec_cmd = "docker exec -it {container} bash"\n',
    )
    proj = config.load(p)["a"]
    assert proj.devcontainer is True
    assert proj.container is None
    assert proj.is_container is True


# ---------------------------------------------------------------------------
# substitute(): {workdir}, {container}, {resume_args} placeholders
# ---------------------------------------------------------------------------


def test_substitute_container_name_override(tmp_path):
    p = _write(
        tmp_path,
        '[a]\nrepo = "/Users/me/dev/a"\ndevcontainer = true\n'
        "exec_cmd = \"docker exec -it {container} bash -lc 'cd {workdir} && claude'\"\n",
    )
    proj = config.load(p)["a"]
    cmd = proj.substitute(proj.exec_cmd, branch=None, container_name="brave_benz")
    assert cmd == "docker exec -it brave_benz bash -lc 'cd /workspaces/a && claude'"


# (body, resume_args, must_contain, must_not_contain)
RESUME_ARGS_CASES = [
    # Default container template + resume.
    (
        '[api]\nrepo = "/x/api"\ndevcontainer = true\n',
        " --resume X",
        "exec claude --resume X",
        None,
    ),
    # Default container template + empty resume.
    ('[api]\nrepo = "/x/api"\ndevcontainer = true\n', "", "exec claude'", "--resume"),
    # Default host-only template + resume.
    ('[scripts]\nrepo = "/x/scripts"\n', " --resume X", "exec claude --resume X", None),
    # Default host-only template + empty resume.
    ('[scripts]\nrepo = "/x/scripts"\n', "", "exec claude", "--resume"),
    # User-defined template using {resume_args}.
    (
        '[scripts]\nrepo = "/x/scripts"\n'
        'exec_cmd = "cd {workdir} && claude{resume_args}"\n',
        " --resume Y",
        "claude --resume Y",
        None,
    ),
]


@pytest.mark.parametrize(
    "body,resume_args,must_contain,must_not_contain", RESUME_ARGS_CASES
)
def test_substitute_resume_args(
    tmp_path, body, resume_args, must_contain, must_not_contain
):
    proj = next(iter(config.load(_write(tmp_path, body)).values()))
    cmd = proj.substitute(
        proj.exec_cmd,
        branch=None,
        container_name="anycontainer",
        resume_args=resume_args,
    )
    assert must_contain in cmd
    if must_not_contain is not None:
        assert must_not_contain not in cmd


# ---------------------------------------------------------------------------
# base_branch field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,expected",
    [
        ('[svc]\nrepo = "/x/svc"\nbase_branch = "develop"\n', "develop"),
        ('[svc]\nrepo = "/x/svc"\n', None),
    ],
)
def test_base_branch_field(tmp_path, body, expected):
    proj = config.load(_write(tmp_path, body))["svc"]
    assert proj.base_branch == expected


# ---------------------------------------------------------------------------
# Top-level non-project settings (e.g. code_path)
# ---------------------------------------------------------------------------


def test_loader_skips_top_level_scalars(tmp_path):
    body = 'code_path = "/Applications/VS Code/code"\n[svc]\nrepo = "/x/svc"\n'
    projects = config.load(_write(tmp_path, body))
    assert set(projects) == {"svc"}


def test_read_code_path_returns_configured_value(tmp_path):
    body = 'code_path = "/opt/code"\n[svc]\nrepo = "/x/svc"\n'
    assert config.read_code_path(_write(tmp_path, body)) == "/opt/code"


def test_read_code_path_defaults_when_key_missing(tmp_path):
    body = '[svc]\nrepo = "/x/svc"\n'
    assert config.read_code_path(_write(tmp_path, body)) == config.DEFAULT_CODE_PATH


def test_read_code_path_defaults_when_file_missing(tmp_path):
    assert config.read_code_path(tmp_path / "nope.toml") == config.DEFAULT_CODE_PATH


# ---------------------------------------------------------------------------
# agent kind: default_agent / per-project agent / codex_exec_cmd
# ---------------------------------------------------------------------------


def _load_one(tmp_path, body: str, name: str = "proj"):
    p = tmp_path / "projects.toml"
    p.write_text(body)
    return config.load(p)[name]


def test_agent_defaults_to_claude(tmp_path):
    proj = _load_one(tmp_path, '[proj]\nrepo = "/r"\n')
    assert proj.agent == "claude"


def test_global_default_agent(tmp_path):
    proj = _load_one(tmp_path, 'default_agent = "codex"\n[proj]\nrepo = "/r"\n')
    assert proj.agent == "codex"


def test_project_agent_overrides_global(tmp_path):
    proj = _load_one(
        tmp_path, 'default_agent = "codex"\n[proj]\nrepo = "/r"\nagent = "claude"\n'
    )
    assert proj.agent == "claude"


def test_invalid_agent_value_raises(tmp_path):
    p = tmp_path / "projects.toml"
    p.write_text('[proj]\nrepo = "/r"\nagent = "gemini"\n')
    with pytest.raises(config.ConfigError):
        config.load(p)


def test_invalid_default_agent_raises(tmp_path):
    p = tmp_path / "projects.toml"
    p.write_text('default_agent = "gemini"\n[proj]\nrepo = "/r"\n')
    with pytest.raises(config.ConfigError):
        config.load(p)


def test_default_codex_exec_cmd_host_only(tmp_path):
    proj = _load_one(tmp_path, '[proj]\nrepo = "/r"\n')
    assert proj.codex_exec_cmd == (
        "cd {workdir} && TMUX_AGENTS_AGENT=1 exec codex{resume_args}"
    )


def test_default_exec_cmds_carry_launch_marker(tmp_path):
    host = _load_one(tmp_path, '[proj]\nrepo = "/r"\n')
    cont = _load_one(tmp_path, '[proj]\nrepo = "/r"\ndevcontainer = true\n')
    assert "TMUX_AGENTS_AGENT=1" in host.exec_cmd
    assert "-e TMUX_AGENTS_AGENT=1" in cont.exec_cmd
    assert "-e TMUX_AGENTS_AGENT=1" in cont.codex_exec_cmd


def test_custom_codex_exec_cmd_preserved(tmp_path):
    proj = _load_one(tmp_path, '[proj]\nrepo = "/r"\ncodex_exec_cmd = "my-codex"\n')
    assert proj.codex_exec_cmd == "my-codex"


def test_exec_cmd_for(tmp_path):
    proj = _load_one(tmp_path, '[proj]\nrepo = "/r"\n')
    assert proj.exec_cmd_for("claude") == proj.exec_cmd
    assert proj.exec_cmd_for("codex") == proj.codex_exec_cmd


def test_exec_cmd_explicit_flags_default_false(tmp_path):
    proj = _load_one(tmp_path, '[proj]\nrepo = "/r"\n')
    assert proj.exec_cmd_explicit is False
    assert proj.codex_exec_cmd_explicit is False


def test_exec_cmd_explicit_flag_true_when_set(tmp_path):
    proj = _load_one(tmp_path, '[proj]\nrepo = "/r"\nexec_cmd = "my-claude"\n')
    assert proj.exec_cmd_explicit is True
    assert proj.codex_exec_cmd_explicit is False


def test_codex_exec_cmd_explicit_flag_true_when_set(tmp_path):
    proj = _load_one(tmp_path, '[proj]\nrepo = "/r"\ncodex_exec_cmd = "my-codex"\n')
    assert proj.exec_cmd_explicit is False
    assert proj.codex_exec_cmd_explicit is True


# ---------------------------------------------------------------------------
# Sandbox backend (docs/SANDBOX-MODE.md)
# ---------------------------------------------------------------------------


def _sandbox_toml(tmp_path, extra: str = "") -> Path:
    repo = tmp_path / "sbxrepo"
    repo.mkdir(exist_ok=True)
    return _write(tmp_path, f'[sbxproj]\nrepo = "{repo}"\nsandbox = true\n{extra}')


def test_sandbox_project_backend(tmp_path):
    p = config.load(_sandbox_toml(tmp_path))["sbxproj"]
    assert p.sandbox is True
    assert p.backend == config.BACKEND_SANDBOX
    assert not p.is_container
    assert p.sandbox_name == "sbxproj"


def test_container_project_backend(fixtures_dir):
    p = config.load(fixtures_dir / "projects_example.toml")["api"]
    assert p.backend == config.BACKEND_CONTAINER
    assert p.is_container


def test_host_project_backend(fixtures_dir):
    p = config.load(fixtures_dir / "projects_example.toml")["scripts"]
    assert p.backend == config.BACKEND_HOST
    assert not p.is_container


def test_sandbox_must_be_bool(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = _write(tmp_path, f'[p]\nrepo = "{repo}"\nsandbox = "yes"\n')
    with pytest.raises(config.ConfigError, match="must be a boolean"):
        config.load(path)


@pytest.mark.parametrize(
    "key_line",
    [
        'container = "x"',
        "devcontainer = true",
        'user = "vscode"',
        'container_workdir = "/work"',
        'up_cmd = "echo up"',
    ],
)
def test_sandbox_mutually_exclusive_keys(tmp_path, key_line):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = _write(tmp_path, f'[p]\nrepo = "{repo}"\nsandbox = true\n{key_line}\n')
    with pytest.raises(config.ConfigError, match="mutually exclusive"):
        config.load(path)


def test_sandbox_false_is_plain_host(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    p = config.load(_write(tmp_path, f'[p]\nrepo = "{repo}"\nsandbox = false\n'))["p"]
    assert p.backend == config.BACKEND_HOST


def test_sandbox_workdir_is_host_path(tmp_path):
    p = config.load(_sandbox_toml(tmp_path))["sbxproj"]
    assert p.workdir_for(None) == str(p.repo)
    assert p.workdir_for("feat") == f"{p.repo}/.worktrees/feat"


def test_sbx_keys_parsed(tmp_path):
    mount = tmp_path / "kube"
    mount.mkdir()
    p = config.load(
        _sandbox_toml(
            tmp_path,
            extra=(
                'sbx_template = "acg-sbx-template:0.3"\n'
                'sbx_kits = ["https://github.com/rmabon/dotfiles"]\n'
                f'sbx_mounts = ["{mount}:ro"]\n'
                'sbx_memory = "8g"\n'
            ),
        )
    )["sbxproj"]
    assert p.sbx_template == "acg-sbx-template:0.3"
    assert p.sbx_kits == ("https://github.com/rmabon/dotfiles",)
    assert p.sbx_mounts == (f"{mount}:ro",)
    assert p.sbx_memory == "8g"


def test_sbx_defaults_when_absent(tmp_path):
    p = config.load(_sandbox_toml(tmp_path))["sbxproj"]
    assert p.sbx_template is None
    assert p.sbx_kits == ()
    assert p.sbx_mounts == ()
    assert p.sbx_memory is None


@pytest.mark.parametrize(
    "key_line",
    [
        'sbx_template = "t:1"',
        'sbx_kits = ["k"]',
        'sbx_mounts = ["/tmp"]',
        'sbx_memory = "8g"',
    ],
)
def test_sbx_keys_require_sandbox(tmp_path, key_line):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = _write(tmp_path, f'[p]\nrepo = "{repo}"\n{key_line}\n')
    with pytest.raises(config.ConfigError, match="requires 'sandbox = true'"):
        config.load(path)


@pytest.mark.parametrize(
    "key_line, match",
    [
        ("sbx_template = 3", "'sbx_template' must be a string"),
        ('sbx_kits = "k"', "'sbx_kits' must be a list of strings"),
        ("sbx_kits = [1]", "'sbx_kits' must be a list of strings"),
        ('sbx_mounts = "x"', "'sbx_mounts' must be a list of strings"),
        ("sbx_memory = 8", "'sbx_memory' must be a string"),
    ],
)
def test_sbx_key_strict_types(tmp_path, key_line, match):
    with pytest.raises(config.ConfigError, match=match):
        config.load(_sandbox_toml(tmp_path, extra=key_line + "\n"))


def test_sbx_mounts_tilde_expanded_and_ro_parsed(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".kube").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    p = config.load(_sandbox_toml(tmp_path, extra='sbx_mounts = ["~/.kube:ro"]\n'))[
        "sbxproj"
    ]
    assert p.sbx_mounts == (f"{fake_home / '.kube'}:ro",)


def test_sbx_mounts_reject_missing_path(tmp_path):
    with pytest.raises(config.ConfigError, match="does not exist"):
        config.load(_sandbox_toml(tmp_path, extra='sbx_mounts = ["/nope-zzz"]\n'))


def test_sbx_mounts_reject_relative_path(tmp_path):
    with pytest.raises(config.ConfigError, match="absolute"):
        config.load(_sandbox_toml(tmp_path, extra='sbx_mounts = ["relative/path"]\n'))


def test_sbx_mounts_reject_duplicates(tmp_path):
    d = tmp_path / "kube"
    d.mkdir()
    with pytest.raises(config.ConfigError, match="duplicate"):
        config.load(_sandbox_toml(tmp_path, extra=f'sbx_mounts = ["{d}", "{d}:ro"]\n'))


DEFAULT_SANDBOX_EXEC = (
    "sbx exec -it -e TERM -e COLORTERM -e TMUX_PANE -e TMUX_AGENTS_AGENT=1 "
    "{sandbox} bash -lc 'cd {workdir} && exec claude{resume_args}'"
)
DEFAULT_SANDBOX_CODEX_EXEC = DEFAULT_SANDBOX_EXEC.replace("exec claude", "exec codex")


def test_sandbox_default_exec_cmds_both_kinds(tmp_path):
    """The default must exist for BOTH kinds: with only exec_cmd overridden,
    the codex slot once fell back to the host-only default and ran codex on
    the host (hit live 2026-08-26)."""
    p = config.load(_sandbox_toml(tmp_path))["sbxproj"]
    assert p.exec_cmd == DEFAULT_SANDBOX_EXEC
    assert p.codex_exec_cmd == DEFAULT_SANDBOX_CODEX_EXEC


def test_sandbox_substitute_injects_sandbox_name(tmp_path):
    p = config.load(_sandbox_toml(tmp_path))["sbxproj"]
    cmd = p.substitute(p.exec_cmd, branch="feat", resume_args=" --resume abc")
    assert " sbxproj " in cmd
    assert f"cd {p.repo}/.worktrees/feat" in cmd
    assert "--resume abc" in cmd


def test_non_sandbox_substitute_sandbox_is_empty(fixtures_dir):
    p = config.load(fixtures_dir / "projects_example.toml")["scripts"]
    assert p.substitute("x{sandbox}y", branch=None) == "xy"


def test_forward_ssh_agent_explicit_flag(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    implicit = config.load(_write(tmp_path, f'[p]\nrepo = "{repo}"\n'))["p"]
    assert implicit.forward_ssh_agent_explicit is False
    explicit = config.load(
        _write(
            tmp_path, f'[q]\nrepo = "{repo}"\nforward_ssh_agent = true\n', name="q.toml"
        )
    )["q"]
    assert explicit.forward_ssh_agent_explicit is True
