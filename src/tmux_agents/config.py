"""projects.toml loader. Resolves the three project modes (named
`container` / `devcontainer = true` / host-only) and fills in defaults
for `exec_cmd`, `up_cmd`, and `container_workdir`."""

from __future__ import annotations
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass


_DEFAULT_USER = "vscode"
_CONTAINER_DEFAULT_UP_CMD = "cd {repo} && devcontainer up --workspace-folder ."
_HOST_ONLY_DEFAULT_EXEC_CMD = "cd {workdir} && exec claude{resume_args}"
# macOS Application bundle binary used as the fallback for `agent-vscode`
# when neither `shutil.which("code")` nor a `code_path` override resolves.
DEFAULT_CODE_PATH = (
    "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
)
_CONTAINER_EXEC_CMD_PREFIX = (
    "docker exec -it -e TERM -e COLORTERM -e TMUX_PANE -u {user} {container} bash -lc"
)
_CONTAINER_EXEC_CMD_BODY_WITH_FORWARD = (
    "'export SSH_AUTH_SOCK=/tmp/tmux-agents-ssh.sock && "
    "cd {workdir} && exec claude{resume_args}'"
)
_CONTAINER_EXEC_CMD_BODY_NO_FORWARD = "'cd {workdir} && exec claude{resume_args}'"


@dataclass(frozen=True)
class Project:
    name: str
    repo: Path
    exec_cmd: str
    container: str | None = None
    container_workdir: str | None = None
    up_cmd: str | None = None
    devcontainer: bool = False
    user: str | None = None
    forward_ssh_agent: bool = True
    base_branch: str | None = None
    # True iff `up_cmd` came from projects.toml rather than the auto-default.
    # `agent-rebuild` uses this to tell a real recipe from the devcontainer
    # default that container projects otherwise inherit.
    up_cmd_explicit: bool = False

    @property
    def is_container(self) -> bool:
        return self.container is not None or self.devcontainer

    def workdir_for(self, branch: str | None) -> str:
        if self.is_container:
            base = self.container_workdir or self._default_container_workdir()
        else:
            base = str(self.repo)
        if branch:
            return f"{base}/.worktrees/{branch}"
        return base

    def _default_container_workdir(self) -> str:
        if self.devcontainer:
            return f"/workspaces/{self.repo.name}"
        return "/work"

    def substitute(
        self,
        template: str,
        *,
        branch: str | None,
        container_name: str | None = None,
        resume_args: str = "",
    ) -> str:
        return template.format(
            repo=str(self.repo),
            container=container_name or self.container or "",
            workdir=self.workdir_for(branch),
            resume_args=resume_args,
        )


def safe_load(path: Path, *, on_error=None) -> dict[str, Project]:
    """Load projects.toml; return {} on missing/malformed. `on_error` is
    called with the exception message when a non-FileNotFoundError occurs
    (e.g. to log it); the worker uses this to surface load failures."""
    try:
        return load(path)
    except FileNotFoundError:
        return {}
    except Exception as ex:
        if on_error is not None:
            on_error(f"projects.toml load failed: {type(ex).__name__}: {ex}")
        return {}


def load(path: Path) -> dict[str, Project]:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    projects: dict[str, Project] = {}
    for name, entry in raw.items():
        # Top-level scalars (e.g. `code_path = "..."`) are tmux-agents-wide
        # settings, not projects. Read them via dedicated accessors below.
        if not isinstance(entry, dict):
            continue
        if "repo" not in entry:
            raise ConfigError(f"project {name!r} is missing required field 'repo'")
        devcontainer = bool(entry.get("devcontainer", False))
        container = entry.get("container")
        if container is not None and devcontainer:
            raise ConfigError(
                f"project {name!r}: set either 'container' or 'devcontainer = true', not both"
            )
        is_container = devcontainer or container is not None
        user = entry.get("user")
        exec_cmd = entry.get("exec_cmd")
        if user is not None and exec_cmd is not None:
            raise ConfigError(
                f"project {name!r}: 'user' is for the default exec_cmd; "
                "set one or the other, not both"
            )
        forward_ssh_agent = bool(entry.get("forward_ssh_agent", True))
        if exec_cmd is None:
            exec_cmd = _default_exec_cmd(is_container, forward_ssh_agent, user)
        up_cmd = entry.get("up_cmd")
        up_cmd_explicit = up_cmd is not None
        if up_cmd is None and is_container:
            up_cmd = _CONTAINER_DEFAULT_UP_CMD
        projects[name] = Project(
            name=name,
            repo=Path(entry["repo"]),
            exec_cmd=exec_cmd,
            container=container,
            container_workdir=entry.get("container_workdir"),
            up_cmd=up_cmd,
            devcontainer=devcontainer,
            user=user,
            forward_ssh_agent=forward_ssh_agent,
            base_branch=entry.get("base_branch"),
            up_cmd_explicit=up_cmd_explicit,
        )
    return projects


def read_code_path(path: Path) -> str:
    """Top-level `code_path` from projects.toml, falling back to
    `DEFAULT_CODE_PATH` when the file or key is absent. Returns a string
    unconditionally — the caller decides whether the path is usable."""
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        return DEFAULT_CODE_PATH
    value = raw.get("code_path")
    if isinstance(value, str) and value:
        return value
    return DEFAULT_CODE_PATH


def _default_exec_cmd(
    is_container: bool, forward_ssh_agent: bool, user: str | None
) -> str:
    if not is_container:
        return _HOST_ONLY_DEFAULT_EXEC_CMD
    body = (
        _CONTAINER_EXEC_CMD_BODY_WITH_FORWARD
        if forward_ssh_agent
        else _CONTAINER_EXEC_CMD_BODY_NO_FORWARD
    )
    return (_CONTAINER_EXEC_CMD_PREFIX + " " + body).replace(
        "{user}", user or _DEFAULT_USER
    )
