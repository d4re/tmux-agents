"""`agent-rebuild`: force-recreate a project's shared container and resume
its agents.

Two halves, like `agent-new`:

- interactive `main` (runs in the `display-popup`): pick the project, warn,
  confirm, then fire the worker via `tmux.run_shell_bg` and return so the
  popup closes.
- detached `main --worker` (parented to the tmux server): the slow work —
  container rebuild, SSH-pump respawn, per-pane resume. Must run off the
  server because the SSH pump is a `Popen(start_new_session=True)` that tmux
  kills when the popup closes, and the container build takes minutes.
"""

from __future__ import annotations
import argparse
import io
import logging
import shlex
import sys
import time
from dataclasses import dataclass

from tmux_agents import (
    config,
    container,
    exec_cmd,
    logging_setup,
    overview,
    paths,
    phase,
    pickers,
    progress,
    ssh_forward,
    startup,
    tmux,
)
from tmux_agents import windows as windows_mod

logger = logging.getLogger(__name__)

# Agents in these states are actively working; their presence flips the
# confirmation default to No.
BUSY_LETTERS = frozenset({"R", "W", "B"})
# Tally display order for the picker.
_TALLY_ORDER = ("R", "W", "B", "Z", "I", "S", "X")


@dataclass(frozen=True)
class Affected:
    """A live agent window that shares the target project's container."""

    mapping: windows_mod.WindowMapping
    window_name: str
    state_letter: str


def _eligible(proj: config.Project) -> bool:
    """A project can be rebuilt iff it has a container recipe: a devcontainer,
    or a named container with an *explicitly configured* `up_cmd` (the
    auto-defaulted devcontainer up_cmd doesn't count — a pre-existing named
    container has no way to recreate itself)."""
    if proj.devcontainer:
        return True
    return proj.container is not None and proj.up_cmd_explicit


def _ineligible_reason(proj: config.Project) -> str:
    if not proj.is_container:
        return "host-only project (no container)"
    return "pre-existing named container with no up_cmd to recreate it"


def _gather_affected(windows: list[tmux.Window]) -> dict[str, list[Affected]]:
    """Group live agent windows by project, carrying each window's state
    letter. Skips the ctrl window and any window without a mapping."""
    out: dict[str, list[Affected]] = {}
    for w in windows:
        if w.name == tmux.CONTROL_WINDOW:
            continue
        m = windows_mod.read_mapping(w.id)
        if m is None:
            continue
        letter, _ = overview._parse_state_code(w.state_code)
        out.setdefault(m.project, []).append(
            Affected(mapping=m, window_name=w.name, state_letter=letter)
        )
    return out


def _tally(affected: list[Affected]) -> str:
    counts: dict[str, int] = {}
    for a in affected:
        counts[a.state_letter] = counts.get(a.state_letter, 0) + 1
    return " ".join(
        f"{counts[letter]}{letter}" for letter in _TALLY_ORDER if letter in counts
    )


def _picker_line(name: str, affected: list[Affected]) -> str:
    if not affected:
        return f"{name}\t—  no agents"
    noun = "agent" if len(affected) == 1 else "agents"
    return f"{name}\t{len(affected)} {noun}  ·  {_tally(affected)}"


def _pick_project(
    eligible: dict[str, config.Project], by_project: dict[str, list[Affected]]
) -> str | None:
    lines = [_picker_line(name, by_project.get(name, [])) for name in sorted(eligible)]
    pick = pickers.pick_one(lines, prompt="rebuild project> ")
    if pick is None:
        return None
    return pick.split("\t", 1)[0]


def _print_warning(
    project: str, affected: list[Affected], busy: list[Affected]
) -> None:
    n = len(affected)
    if not affected:
        print(f"Rebuilding {project}: no agents are currently in its container.")
        return
    noun = "agent" if n == 1 else "agents"
    if busy:
        print(
            f"⚠  Rebuilding {project} recreates the shared container and kills all "
            f"{n} {noun} in it. {len(busy)} actively working:"
        )
    else:
        print(
            f"Rebuilding {project} recreates the shared container and kills all "
            f"{n} {noun} in it (all idle/sleeping):"
        )
    for a in affected:
        marker = "   ← busy" if a.state_letter in BUSY_LETTERS else ""
        label = a.mapping.branch or a.window_name
        print(f"     {a.state_letter}  {label}{marker}")
    print(
        "Agents will be auto-resumed after the rebuild. "
        "(Sleeping agents lose any pending scheduled wakeup until they resume.)"
    )


def _confirm(project: str, affected: list[Affected], *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    busy = [a for a in affected if a.state_letter in BUSY_LETTERS]
    _print_warning(project, affected, busy)
    try:
        return pickers.prompt_yes_no(f"rebuild {project}? ", default=not busy)
    except (pickers.Cancelled, KeyboardInterrupt):
        return False


# ===== worker half =====


def _fail_pane(a: Affected, reason: str) -> None:
    """Show the rebuild failure in the agent's pane and flip it to errored."""
    body = (
        f"\n  agent-rebuild failed for {a.window_name}\n  reason: {reason}\n\n"
        "  Fix the underlying issue (e.g. start Docker), then re-run:\n"
        f"    agent-rebuild {a.mapping.project}\n\n"
    )
    startup.show_static_text(f"%{a.mapping.pane_id}", body)
    startup._write_pane_state(
        a.mapping.host_worktree, a.mapping.pane_id, phase_value=phase.ERRORED
    )


def _run_worker(
    proj: config.Project, affected: list[Affected], *, no_cache: bool
) -> int:
    """Detached: show progress in each pane, rebuild the container, respawn
    the SSH pump, and re-exec each pane into Claude. Per-pane failures are
    isolated; a container-rebuild failure marks every pane errored."""
    # Show live build output where each agent used to be.
    for a in affected:
        startup._respawn_with_retry(
            f"%{a.mapping.pane_id}",
            startup.placeholder_command(a.mapping.window_id),
        )
        startup._write_pane_state(
            a.mapping.host_worktree, a.mapping.pane_id, phase_value=phase.STARTING
        )

    files: dict[str, io.TextIOWrapper] = {}
    reporters: list[progress.Reporter] = []
    try:
        for a in affected:
            log_path = paths.spawn_log(a.mapping.window_id)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            f = open(log_path, "w", buffering=1)
            files[a.mapping.window_id] = f
            r = progress.Reporter(out=f, color=True, clock=time.monotonic)
            r.banner(f"Rebuilding container: {proj.name}")
            reporters.append(r)
        multi = progress.MultiReporter(reporters)

        try:
            with multi.stage("rebuild") as st:
                st.info("recreating container (this may take minutes)…")
                up_cmd = (
                    proj.substitute(proj.up_cmd, branch=None) if proj.up_cmd else None
                )
                container_name = container.rebuild(
                    proj, up_cmd=up_cmd, no_cache=no_cache
                )
            if proj.forward_ssh_agent:
                with multi.stage("ssh pump") as st:
                    ssh_forward.maybe_spawn_pump(
                        container_name,
                        proj.user or "vscode",
                    ).render(st)
        except container.ContainerError as ce:
            logger.error("rebuild failed for %r: %s", proj.name, ce)
            for a in affected:
                _fail_pane(a, f"container rebuild failed: {ce}")
            return 1
    finally:
        for f in files.values():
            try:
                f.close()
            except Exception:
                pass
        for a in affected:
            paths.spawn_log(a.mapping.window_id).unlink(missing_ok=True)

    # Container is up; re-exec each pane into Claude, isolating failures.
    failures = 0
    for a in affected:
        m = a.mapping
        try:
            cmd = exec_cmd.build(
                proj,
                branch=m.branch,
                claude_session_id=m.claude_session_id,
                container_name=container_name,
                label=m.window_id,
            )
            startup._respawn_with_retry(f"%{m.pane_id}", cmd)
            startup._write_pane_state(
                m.host_worktree, m.pane_id, phase_value=phase.STARTING
            )
            logger.info("%s: respawned pane=%%%s", m.window_id, m.pane_id)
        except Exception as ex:
            failures += 1
            logger.error("%s: respawn failed: %s", m.window_id, ex, exc_info=True)
    logger.info(
        "rebuilt %r; respawned %d/%d agent(s)",
        proj.name,
        len(affected) - failures,
        len(affected),
    )
    return 0 if failures == 0 else 1


# ===== CLI =====


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-rebuild")
    p.add_argument("project", nargs="?", default=None, help="project name")
    p.add_argument(
        "--project",
        dest="project_opt",
        default=None,
        help="project name (explicit; skips the picker)",
    )
    p.add_argument(
        "--no-cache", action="store_true", help="full from-scratch image rebuild"
    )
    p.add_argument("--yes", "-y", action="store_true", help="skip confirmation")
    p.add_argument(
        "--worker",
        action="store_true",
        help="internal: run the detached rebuild worker",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    logging_setup.setup_logging()
    args = _parser().parse_args(argv)
    projects = config.safe_load(
        paths.projects_toml(), on_error=lambda msg: logger.error(msg)
    )
    project = args.project_opt or args.project

    if args.worker:
        proj = projects.get(project)
        if proj is None:
            logger.error("worker: unknown project %r", project)
            return 2
        affected = _gather_affected(tmux.list_windows(tmux.SESSION)).get(project, [])
        return _run_worker(proj, affected, no_cache=args.no_cache)

    eligible = {n: p for n, p in projects.items() if _eligible(p)}
    if not eligible:
        logging_setup.cli_error(
            logger,
            "no projects with a rebuild recipe "
            "(need devcontainer = true, or a named container with an up_cmd)",
        )
        return 2

    by_project = _gather_affected(tmux.list_windows(tmux.SESSION))

    if project is None:
        try:
            project = _pick_project(eligible, by_project)
        except (KeyboardInterrupt, pickers.Cancelled):
            return 0
        if project is None:
            return 0

    proj = projects.get(project)
    if proj is None:
        logging_setup.cli_error(logger, f"unknown project {project!r}")
        return 2
    if not _eligible(proj):
        logging_setup.cli_error(
            logger, f"{project!r} cannot be rebuilt: {_ineligible_reason(proj)}"
        )
        return 2

    affected = by_project.get(project, [])
    if not _confirm(project, affected, assume_yes=args.yes):
        print("aborted", file=sys.stderr)
        return 0

    worker_argv = ["agent-rebuild", "--worker", "--project", project]
    if args.no_cache:
        worker_argv.append("--no-cache")
    tmux.run_shell_bg(shlex.join(worker_argv))
    print(
        f"rebuilding {project} in the background — watch its agent panes for progress"
    )
    return 0
