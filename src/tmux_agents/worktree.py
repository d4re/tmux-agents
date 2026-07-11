"""`git worktree add/remove`. For container projects, runs git via
`docker exec` so the worktree's internal `.git` pointers resolve inside
the container instead of pointing at host paths the container can't
reach."""

from __future__ import annotations
import logging
import subprocess
from pathlib import Path

from tmux_agents.ssh_forward import UDS_PATH as _SSH_UDS_PATH

logger = logging.getLogger(__name__)


class WorktreeError(RuntimeError):
    pass


class DirtyWorktreeError(WorktreeError):
    """Worktree has uncommitted or untracked files; caller may retry with force."""


_DIRTY_MARKER = "contains modified or untracked files"

# Substrings in `git fetch` stderr that indicate network unreachability —
# the only failure mode where falling back to a cached `origin/<base>` is
# safe. Auth errors, permission changes, and "remote ref not found" should
# surface as hard failures instead of silently branching from stale cache.
_OFFLINE_FETCH_HINTS = (
    "could not resolve host",
    "could not connect to server",
    "couldn't connect to server",
    "connection refused",
    "connection timed out",
    "operation timed out",
    "network is unreachable",
    "temporary failure in name resolution",
    "no route to host",
)


def _looks_like_offline_fetch_failure(stderr: str) -> bool:
    s = stderr.lower()
    return any(hint in s for hint in _OFFLINE_FETCH_HINTS)


def _git_run(
    args: list[str],
    *,
    repo: Path,
    container: str | None,
    container_workdir: str | None,
    container_user: str,
) -> subprocess.CompletedProcess[str]:
    """Run a git command on host or via `docker exec`. Never raises on
    non-zero exit; caller inspects the returned CompletedProcess.

    Container invocations forward SSH_AUTH_SOCK to the same path the
    pump publishes (see ssh_forward.UDS_PATH) so SSH-form remotes
    authenticate via the host agent. Harmless for HTTPS remotes — git
    only consults the socket when ssh is invoked.

    GIT_SSH_COMMAND adds StrictHostKeyChecking=accept-new so a fresh
    container (no `~/.ssh/known_hosts`) accepts github.com on first
    contact rather than aborting with 'Host key verification failed'.
    Mirrors VS Code Dev Containers' default posture."""
    if container and container_workdir:
        cmd = [
            "docker",
            "exec",
            "-e",
            f"SSH_AUTH_SOCK={_SSH_UDS_PATH}",
            "-e",
            "GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=accept-new",
            "-u",
            container_user,
            container,
            "git",
            "-C",
            container_workdir,
            *args,
        ]
    else:
        cmd = ["git", "-C", str(repo), *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def _git_cmd(
    repo: Path,
    target: Path,
    branch: str,
    op: str,
    container: str | None,
    container_workdir: str | None,
    container_user: str,
) -> list[str]:
    # When run inside a container, `git worktree add/remove` bakes the absolute
    # paths it sees into .git pointers. Running it on the host would store host
    # paths the container can't reach; running it via `docker exec` stores
    # container paths so git resolves correctly inside the container — which is
    # where work actually happens.
    if container and container_workdir:
        container_target = f"{container_workdir}/.worktrees/{branch}"
        return [
            "docker",
            "exec",
            "-u",
            container_user,
            container,
            "git",
            "-C",
            container_workdir,
            "worktree",
            op,
            container_target,
        ]
    return ["git", "-C", str(repo), "worktree", op, str(target)]


def _resolve_base(
    repo: Path,
    base_override: str | None,
    *,
    container: str | None,
    container_workdir: str | None,
    container_user: str,
    reporter_stage=None,
) -> tuple[str | None, list[str]]:
    """Resolve the commit-ish for `git worktree add -B <branch> <target> <commit-ish>`.

    Returns (commit_ish, warnings):
      - commit_ish: str or None (None ⇒ branch from HEAD).
      - warnings: list[str] of stderr-bound messages (already prefixed-free).
    """

    def run(*args):
        return _git_run(
            list(args),
            repo=repo,
            container=container,
            container_workdir=container_workdir,
            container_user=container_user,
        )

    def origin_exists():
        return run("remote", "get-url", "origin").returncode == 0

    def local_branch_exists(name):
        return (
            run("rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode
            == 0
        )

    def cached_origin_ref_exists(name):
        return (
            run(
                "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{name}"
            ).returncode
            == 0
        )

    warnings: list[str] = []

    # Step 1: determine name + use_origin + local_confirmed
    # local_confirmed=True means we already verified refs/heads/<name> exists.
    name: str | None = None
    use_origin = False
    local_confirmed = False

    if base_override:
        name = base_override
        use_origin = origin_exists()
    else:
        r = run("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        if r.returncode == 0:
            name = r.stdout.strip().removeprefix("refs/remotes/origin/")
            use_origin = True
        elif origin_exists():
            sh = run("remote", "set-head", "origin", "-a")
            if sh.returncode == 0:
                r2 = run("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
                if r2.returncode == 0:
                    name = r2.stdout.strip().removeprefix("refs/remotes/origin/")
                    use_origin = True
        if name is None:
            cfg = run("config", "--get", "init.defaultBranch")
            if cfg.returncode == 0:
                candidate = cfg.stdout.strip()
                if candidate and local_branch_exists(candidate):
                    name = candidate
                    use_origin = False
                    local_confirmed = True

    if name is None:
        msg = "no usable base branch detected; branching from HEAD"
        warnings.append(msg)
        if reporter_stage is not None:
            reporter_stage.warn(msg)
        return None, warnings

    # Step 2: resolve commit-ish
    if use_origin:
        if reporter_stage is not None:
            reporter_stage.info(f"fetching origin/{name}")
        f = run("fetch", "origin", name)
        if f.returncode == 0:
            return f"origin/{name}", warnings
        stderr = f.stderr.strip()
        if _looks_like_offline_fetch_failure(f.stderr):
            if cached_origin_ref_exists(name):
                msg = f"fetch failed; using cached origin/{name}"
                warnings.append(msg)
                if reporter_stage is not None:
                    reporter_stage.warn(msg)
                return f"origin/{name}", warnings
            raise WorktreeError(
                f"fetch origin {name} failed and no cached origin/{name} exists "
                f"(stderr: {stderr})"
            )
        raise WorktreeError(f"fetch origin {name} failed: {stderr}")
    else:
        if local_confirmed or local_branch_exists(name):
            return name, warnings
        raise WorktreeError(f"base branch {name!r} does not exist locally")


def resolve(
    repo: Path,
    branch: str | None,
    *,
    base_override: str | None = None,
    container: str | None = None,
    container_workdir: str | None = None,
    container_user: str | None = None,
    reporter_stage=None,
) -> Path:
    if branch is None:
        return repo
    target = repo / ".worktrees" / branch
    if target.exists():
        if reporter_stage is not None:
            reporter_stage.skip(f"reusing .worktrees/{branch}")
        return target

    user = container_user or "vscode"
    commit_ish, warnings = _resolve_base(
        repo,
        base_override,
        container=container,
        container_workdir=container_workdir,
        container_user=user,
        reporter_stage=reporter_stage,
    )
    # If no stage was passed, preserve the existing logger.warning path:
    if reporter_stage is None:
        for w in warnings:
            logger.warning("%s", w)
    # else: warnings were already surfaced via reporter_stage.warn

    if reporter_stage is not None:
        reporter_stage.info(f"creating .worktrees/{branch}")

    cmd = _git_cmd(repo, target, branch, "add", container, container_workdir, user)
    cmd += ["-B", branch]
    if commit_ish is not None:
        cmd.append(commit_ish)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise WorktreeError(
            r.stderr.strip() or r.stdout.strip() or "git worktree add failed"
        )
    return target


def remove(
    repo: Path,
    branch: str,
    *,
    force: bool = False,
    container: str | None = None,
    container_workdir: str | None = None,
    container_user: str | None = None,
) -> None:
    target = repo / ".worktrees" / branch
    if not target.exists():
        return
    cmd = _git_cmd(
        repo,
        target,
        branch,
        "remove",
        container,
        container_workdir,
        container_user or "vscode",
    )
    if force:
        cmd.append("--force")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        stderr = r.stderr.strip()
        if _DIRTY_MARKER in stderr:
            raise DirtyWorktreeError(stderr)
        raise WorktreeError(stderr or "git worktree remove failed")


def list_existing(repo: Path) -> list[str]:
    """Return names of worktrees under ``<repo>/.worktrees/``, sorted.

    A worktree is any directory containing a ``.git`` entry; the name is
    its path relative to ``.worktrees/`` (so ``feat/foo`` stays nested).

    Why a filesystem walk instead of ``git worktree list --porcelain``:
    container projects run ``git worktree add`` via ``docker exec``, which
    bakes container paths (e.g. ``/workspace/...``) into ``.git/worktrees/``.
    The host's ``git worktree list`` then returns those container paths as
    ``prunable``, and a host-side prefix check matches none of them. The
    devcontainer mount maps the same files at ``<host_repo>/.worktrees/``,
    so a direct walk works for every project type without a docker probe.
    """
    base = repo / ".worktrees"
    if not base.is_dir():
        return []
    branches: list[str] = []

    def walk(directory: Path) -> None:
        try:
            entries = list(directory.iterdir())
        except OSError as e:
            logger.warning("list_existing: cannot read %s: %s", directory, e)
            return
        if directory != base and (directory / ".git").exists():
            branches.append(str(directory.relative_to(base)))
            return  # don't recurse into a worktree's internals
        for entry in entries:
            if entry.is_dir():
                walk(entry)

    walk(base)
    return sorted(branches)
