"""projects.toml loader. Resolves the three project modes (named
`container` / `devcontainer = true` / host-only) and fills in defaults
for `exec_cmd`, `up_cmd`, and `container_workdir`."""

from __future__ import annotations
import tomllib
from dataclasses import dataclass
from pathlib import Path

from tmux_agents import agent_kind


class ConfigError(ValueError):
    pass


# The three project backends. `sandbox = true` is the external spelling, but
# internally this is a backend, not a boolean: every Docker-vs-host
# assumption behind `is_container` is wrong for sandboxes in a different
# way, so consumers dispatch on `Project.backend` (see docs/SANDBOX-MODE.md).
BACKEND_HOST = "host"
BACKEND_CONTAINER = "container"
BACKEND_SANDBOX = "sandbox"


_DEFAULT_USER = "vscode"
_CONTAINER_DEFAULT_UP_CMD = "cd {repo} && devcontainer up --workspace-folder ."
# macOS Application bundle binary used as the fallback for `agent-vscode`
# when neither `shutil.which("code")` nor a `code_path` override resolves.
DEFAULT_CODE_PATH = (
    "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
)
_CONTAINER_EXEC_CMD_PREFIX = (
    "docker exec -it -e TERM -e COLORTERM -e TMUX_PANE -e TMUX_AGENTS_AGENT=1 "
    "-u {user} {container} bash -lc"
)
_HOST_ONLY_EXEC_CMD_TMPL = (
    "cd {{workdir}} && TMUX_AGENTS_AGENT=1 exec {exe}{{resume_args}}"
)
_CONTAINER_BODY_WITH_FORWARD_TMPL = (
    "'export SSH_AUTH_SOCK=/tmp/tmux-agents-ssh.sock && "
    "cd {{workdir}} && exec {exe}{{resume_args}}'"
)
_CONTAINER_BODY_NO_FORWARD_TMPL = "'cd {{workdir}} && exec {exe}{{resume_args}}'"
# `sbx exec` auto-starts a stopped sandbox and forwards TMUX_PANE, so the
# state pipeline works unchanged; only the daemon must already run. No SSH
# pump/env: sbx forwards the host agent natively.
_SANDBOX_EXEC_CMD_TMPL = (
    "sbx exec -it -e TERM -e COLORTERM -e TMUX_PANE -e TMUX_AGENTS_AGENT=1 "
    "{{sandbox}} bash -lc 'cd {{workdir}} && exec {exe}{{resume_args}}'"
)


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
    share_gh_auth: bool = True
    # True iff `forward_ssh_agent` was set in projects.toml (vs the default).
    # Sandbox mode uses it to warn only on an explicit key: sbx forwards the
    # host agent natively and the Docker SSH pump must never spawn.
    forward_ssh_agent_explicit: bool = False
    base_branch: str | None = None
    # True iff `up_cmd` came from projects.toml rather than the auto-default.
    # `agent-rebuild` uses this to tell a real recipe from the devcontainer
    # default that container projects otherwise inherit.
    up_cmd_explicit: bool = False
    agent: str = agent_kind.CLAUDE
    codex_exec_cmd: str = ""
    # True iff exec_cmd/codex_exec_cmd came from projects.toml rather than the
    # auto-default template. `agent-other`'s executable pre-flight
    # (`command -v <exe>`) only proves anything against the fixed default
    # template; a custom command's binary name is unknowable, so the
    # pre-flight is skipped when this is True.
    exec_cmd_explicit: bool = False
    codex_exec_cmd_explicit: bool = False
    sandbox: bool = False
    sbx_template: str | None = None
    sbx_kits: tuple[str, ...] = ()
    # Canonical `path` / `path:ro` strings, `~` expanded, ready for
    # `sbx create` argv (extra host workspaces, absolute paths preserved
    # inside the VM — create-time only, changing them needs agent-rebuild).
    sbx_mounts: tuple[str, ...] = ()
    sbx_memory: str | None = None

    def exec_cmd_for(self, kind: str) -> str:
        return self.exec_cmd if kind == agent_kind.CLAUDE else self.codex_exec_cmd

    @property
    def backend(self) -> str:
        if self.sandbox:
            return BACKEND_SANDBOX
        if self.container is not None or self.devcontainer:
            return BACKEND_CONTAINER
        return BACKEND_HOST

    @property
    def is_container(self) -> bool:
        return self.backend == BACKEND_CONTAINER

    @property
    def sandbox_name(self) -> str:
        # Sandbox name = project name (host-global; a collision override key
        # is deliberately deferred as YAGNI — see docs/SANDBOX-MODE.md).
        return self.name

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
            sandbox=self.sandbox_name if self.sandbox else "",
        )


_SBX_KEYS = ("sbx_template", "sbx_kits", "sbx_mounts", "sbx_memory")


def _parse_sbx_mounts(name: str, raw: object) -> tuple[str, ...]:
    """Normalize `sbx_mounts` entries to canonical `path[:ro]` strings ready
    for `sbx create` argv: `~` expanded in Python (subprocess argv gets no
    shell expansion), paths resolved, duplicates and missing paths rejected."""
    if not isinstance(raw, list) or not all(isinstance(m, str) for m in raw):
        raise ConfigError(f"project {name!r}: 'sbx_mounts' must be a list of strings")
    seen: set[str] = set()
    out: list[str] = []
    for m in raw:
        ro = m.endswith(":ro")
        path_part = m[:-3] if ro else m
        p = Path(path_part).expanduser()
        if not p.is_absolute():
            raise ConfigError(
                f"project {name!r}: sbx_mount {m!r} must be an absolute or ~-based path"
            )
        p = p.resolve()
        if not p.exists():
            raise ConfigError(f"project {name!r}: sbx_mount {m!r} does not exist")
        if str(p) in seen:
            raise ConfigError(f"project {name!r}: duplicate sbx_mount {m!r}")
        seen.add(str(p))
        out.append(f"{p}:ro" if ro else str(p))
    return tuple(out)


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
    default_agent = raw.get("default_agent", agent_kind.CLAUDE)
    if default_agent not in agent_kind.KINDS:
        raise ConfigError(
            f"default_agent must be one of {agent_kind.KINDS}, got {default_agent!r}"
        )
    projects: dict[str, Project] = {}
    for name, entry in raw.items():
        # Top-level scalars (e.g. `code_path = "..."`) are tmux-agents-wide
        # settings, not projects. Read them via dedicated accessors below.
        if not isinstance(entry, dict):
            continue
        if "repo" not in entry:
            raise ConfigError(f"project {name!r} is missing required field 'repo'")
        sandbox = entry.get("sandbox", False)
        if not isinstance(sandbox, bool):
            raise ConfigError(f"project {name!r}: 'sandbox' must be a boolean")
        if sandbox:
            for key in (
                "container",
                "devcontainer",
                "user",
                "container_workdir",
                "up_cmd",
            ):
                if key in entry:
                    raise ConfigError(
                        f"project {name!r}: 'sandbox' is mutually exclusive with {key!r}"
                    )
        if not sandbox:
            for key in _SBX_KEYS:
                if key in entry:
                    raise ConfigError(
                        f"project {name!r}: {key!r} requires 'sandbox = true'"
                    )
        sbx_template = entry.get("sbx_template")
        if sbx_template is not None and not isinstance(sbx_template, str):
            raise ConfigError(f"project {name!r}: 'sbx_template' must be a string")
        sbx_kits_raw = entry.get("sbx_kits", [])
        if not isinstance(sbx_kits_raw, list) or not all(
            isinstance(k, str) for k in sbx_kits_raw
        ):
            raise ConfigError(f"project {name!r}: 'sbx_kits' must be a list of strings")
        sbx_memory = entry.get("sbx_memory")
        if sbx_memory is not None and not isinstance(sbx_memory, str):
            raise ConfigError(f"project {name!r}: 'sbx_memory' must be a string")
        sbx_mounts = _parse_sbx_mounts(name, entry.get("sbx_mounts", []))
        devcontainer = bool(entry.get("devcontainer", False))
        container = entry.get("container")
        if container is not None and devcontainer:
            raise ConfigError(
                f"project {name!r}: set either 'container' or 'devcontainer = true', not both"
            )
        backend = (
            BACKEND_SANDBOX
            if sandbox
            else BACKEND_CONTAINER
            if (devcontainer or container is not None)
            else BACKEND_HOST
        )
        is_container = backend == BACKEND_CONTAINER
        user = entry.get("user")
        exec_cmd = entry.get("exec_cmd")
        if user is not None and exec_cmd is not None:
            raise ConfigError(
                f"project {name!r}: 'user' is for the default exec_cmd; "
                "set one or the other, not both"
            )
        forward_ssh_agent = entry.get("forward_ssh_agent", True)
        if not isinstance(forward_ssh_agent, bool):
            raise ConfigError(
                f"project {name!r}: 'forward_ssh_agent' must be a boolean"
            )
        forward_ssh_agent_explicit = "forward_ssh_agent" in entry
        exec_cmd_explicit = exec_cmd is not None
        if exec_cmd is None:
            exec_cmd = _default_exec_cmd(
                backend, forward_ssh_agent, user, exe=agent_kind.CLAUDE
            )
        agent = entry.get("agent", default_agent)
        if agent not in agent_kind.KINDS:
            raise ConfigError(
                f"project {name!r}: agent must be one of {agent_kind.KINDS}, got {agent!r}"
            )
        codex_exec_cmd = entry.get("codex_exec_cmd")
        codex_exec_cmd_explicit = codex_exec_cmd is not None
        if codex_exec_cmd is None:
            codex_exec_cmd = _default_exec_cmd(
                backend, forward_ssh_agent, user, exe=agent_kind.CODEX
            )
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
            share_gh_auth=bool(entry.get("share_gh_auth", True)),
            forward_ssh_agent_explicit=forward_ssh_agent_explicit,
            base_branch=entry.get("base_branch"),
            up_cmd_explicit=up_cmd_explicit,
            agent=agent,
            codex_exec_cmd=codex_exec_cmd,
            exec_cmd_explicit=exec_cmd_explicit,
            codex_exec_cmd_explicit=codex_exec_cmd_explicit,
            sandbox=sandbox,
            sbx_template=sbx_template,
            sbx_kits=tuple(sbx_kits_raw),
            sbx_mounts=sbx_mounts,
            sbx_memory=sbx_memory,
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
    backend: str, forward_ssh_agent: bool, user: str | None, *, exe: str
) -> str:
    if backend == BACKEND_SANDBOX:
        return _SANDBOX_EXEC_CMD_TMPL.format(exe=exe)
    if backend == BACKEND_HOST:
        return _HOST_ONLY_EXEC_CMD_TMPL.format(exe=exe)
    body = (
        _CONTAINER_BODY_WITH_FORWARD_TMPL
        if forward_ssh_agent
        else _CONTAINER_BODY_NO_FORWARD_TMPL
    ).format(exe=exe)
    return (_CONTAINER_EXEC_CMD_PREFIX + " " + body).replace(
        "{user}", user or _DEFAULT_USER
    )
