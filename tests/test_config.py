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
    "docker exec -it -e TERM -e COLORTERM -e TMUX_PANE -u vscode {container} "
    "bash -lc 'export SSH_AUTH_SOCK=/tmp/tmux-agents-ssh.sock && "
    "cd {workdir} && exec claude{resume_args}'"
)
DEFAULT_CONTAINER_EXEC_NO_SSH = (
    "docker exec -it -e TERM -e COLORTERM -e TMUX_PANE -u vscode {container} "
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
    assert cmd == "docker exec -it api-devcontainer bash -lc 'cd /work/.worktrees/feat-x && claude'"


def test_substitute_host_only(fixtures_dir):
    p = config.load(fixtures_dir / "projects_example.toml")["scripts"]
    assert p.workdir_for(None) == "/Users/remi/dev/scripts"
    assert p.workdir_for("hotfix") == "/Users/remi/dev/scripts/.worktrees/hotfix"
    cmd = p.substitute(p.exec_cmd, branch="hotfix")
    assert cmd == "cd /Users/remi/dev/scripts/.worktrees/hotfix && claude"


def test_substitute_up_cmd(fixtures_dir):
    p = config.load(fixtures_dir / "projects_example.toml")["api"]
    assert p.substitute(p.up_cmd, branch=None) == "cd /Users/remi/dev/api && devcontainer up --workspace-folder ."


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body,match", [
    # Missing required `repo`:
    ('[foo]\nexec_cmd = "claude"\n', "repo"),
    # Both container and devcontainer set:
    ('[a]\nrepo = "/x"\nexec_cmd = "c"\ncontainer = "n"\ndevcontainer = true\n',
     "either 'container' or 'devcontainer"),
    # `user` field with an explicit exec_cmd is a contradiction:
    ('[a]\nrepo = "/x"\ndevcontainer = true\nuser = "node"\nexec_cmd = "custom"\n',
     "user"),
], ids=["missing_repo", "container_and_devcontainer", "user_with_custom_exec_cmd"])
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
        "cd {workdir} && exec claude{resume_args}",
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


@pytest.mark.parametrize("body,exec_cmd,up_cmd", DEFAULT_CASES,
                         ids=["host_only", "devcontainer", "explicit_container", "no_ssh_forward"])
def test_default_exec_and_up_cmd(tmp_path, body, exec_cmd, up_cmd):
    proj = next(iter(config.load(_write(tmp_path, body)).values()))
    assert proj.exec_cmd == exec_cmd
    assert proj.up_cmd == up_cmd


# Explicit user/exec_cmd overrides bypass the default-template logic.
@pytest.mark.parametrize("body,assertion", [
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
        lambda p: p.exec_cmd == "docker exec -it {container} zsh" and p.forward_ssh_agent is True,
    ),
], ids=["explicit_exec_cmd", "explicit_up_cmd", "custom_exec_cmd_unaltered"])
def test_explicit_overrides_default(tmp_path, body, assertion):
    proj = next(iter(config.load(_write(tmp_path, body)).values()))
    assert assertion(proj)


# ---------------------------------------------------------------------------
# `user` field: rewrites -u <name> in the default template
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body,expected_user,expected_flag", [
    # devcontainer + user override:
    ('[a]\nrepo = "/x"\ndevcontainer = true\nuser = "node"\n', "node", "-u node"),
    # devcontainer + default:
    ('[a]\nrepo = "/x"\ndevcontainer = true\n', None, "-u vscode"),
    # explicit container + user override:
    ('[a]\nrepo = "/x"\ncontainer = "foo"\ncontainer_workdir = "/w"\nuser = "node"\n',
     "node", "-u node"),
])
def test_user_field_in_default_template(tmp_path, body, expected_user, expected_flag):
    proj = next(iter(config.load(_write(tmp_path, body)).values()))
    assert proj.user == expected_user
    assert expected_flag in proj.exec_cmd
    if expected_user == "node":
        assert "-u vscode" not in proj.exec_cmd


# ---------------------------------------------------------------------------
# forward_ssh_agent flag — defaults true everywhere, can be turned off.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body,expected", [
    ('[a]\nrepo = "/x"\ndevcontainer = true\n', True),                              # devcontainer default
    ('[a]\nrepo = "/x"\ncontainer = "foo"\ncontainer_workdir = "/w"\n', True),      # explicit container default
    ('[scripts]\nrepo = "/x"\nexec_cmd = "claude"\n', True),                        # host-only default (inert)
    ('[a]\nrepo = "/x"\ndevcontainer = true\nforward_ssh_agent = false\n', False),  # explicit off
])
def test_forward_ssh_agent_flag(tmp_path, body, expected):
    proj = next(iter(config.load(_write(tmp_path, body)).values()))
    assert proj.forward_ssh_agent is expected


# ---------------------------------------------------------------------------
# devcontainer workdir resolution
# ---------------------------------------------------------------------------

def test_devcontainer_workdir_defaults_to_workspaces_basename(tmp_path):
    p = _write(tmp_path,
        '[webapp-gateway]\n'
        'repo = "/Users/me/dev/webapp-gateway-service"\n'
        'devcontainer = true\n'
        'exec_cmd = "docker exec -it {container} bash -lc \'cd {workdir} && claude\'"\n'
    )
    proj = config.load(p)["webapp-gateway"]
    assert proj.workdir_for(None) == "/workspaces/webapp-gateway-service"
    assert proj.workdir_for("feat-x") == "/workspaces/webapp-gateway-service/.worktrees/feat-x"


def test_devcontainer_explicit_workdir_overrides_default(tmp_path):
    p = _write(tmp_path,
        '[a]\nrepo = "/Users/me/dev/a"\ndevcontainer = true\n'
        'container_workdir = "/custom/path"\n'
        'exec_cmd = "c"\n'
    )
    assert config.load(p)["a"].workdir_for(None) == "/custom/path"


def test_devcontainer_field_marks_is_container(tmp_path):
    p = _write(tmp_path,
        '[a]\nrepo = "/Users/me/dev/a"\ndevcontainer = true\n'
        'exec_cmd = "docker exec -it {container} bash"\n'
    )
    proj = config.load(p)["a"]
    assert proj.devcontainer is True
    assert proj.container is None
    assert proj.is_container is True


# ---------------------------------------------------------------------------
# substitute(): {workdir}, {container}, {resume_args} placeholders
# ---------------------------------------------------------------------------

def test_substitute_container_name_override(tmp_path):
    p = _write(tmp_path,
        '[a]\nrepo = "/Users/me/dev/a"\ndevcontainer = true\n'
        'exec_cmd = "docker exec -it {container} bash -lc \'cd {workdir} && claude\'"\n'
    )
    proj = config.load(p)["a"]
    cmd = proj.substitute(proj.exec_cmd, branch=None, container_name="brave_benz")
    assert cmd == "docker exec -it brave_benz bash -lc 'cd /workspaces/a && claude'"


# (body, resume_args, must_contain, must_not_contain)
RESUME_ARGS_CASES = [
    # Default container template + resume.
    ('[api]\nrepo = "/x/api"\ndevcontainer = true\n',
     " --resume X",  "exec claude --resume X",  None),
    # Default container template + empty resume.
    ('[api]\nrepo = "/x/api"\ndevcontainer = true\n',
     "",             "exec claude'",            "--resume"),
    # Default host-only template + resume.
    ('[scripts]\nrepo = "/x/scripts"\n',
     " --resume X",  "exec claude --resume X",  None),
    # Default host-only template + empty resume.
    ('[scripts]\nrepo = "/x/scripts"\n',
     "",             "exec claude",             "--resume"),
    # User-defined template using {resume_args}.
    ('[scripts]\nrepo = "/x/scripts"\n'
     'exec_cmd = "cd {workdir} && claude{resume_args}"\n',
     " --resume Y",  "claude --resume Y",       None),
]


@pytest.mark.parametrize("body,resume_args,must_contain,must_not_contain", RESUME_ARGS_CASES)
def test_substitute_resume_args(tmp_path, body, resume_args, must_contain, must_not_contain):
    proj = next(iter(config.load(_write(tmp_path, body)).values()))
    cmd = proj.substitute(proj.exec_cmd, branch=None,
                          container_name="anycontainer", resume_args=resume_args)
    assert must_contain in cmd
    if must_not_contain is not None:
        assert must_not_contain not in cmd


# ---------------------------------------------------------------------------
# base_branch field
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body,expected", [
    ('[svc]\nrepo = "/x/svc"\nbase_branch = "develop"\n', "develop"),
    ('[svc]\nrepo = "/x/svc"\n',                          None),
])
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
