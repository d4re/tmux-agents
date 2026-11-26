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
import contextlib
import dataclasses
import logging
import os
import shlex
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass

from tmux_agents import (
    codex_hooks,
    config,
    container,
    exec_cmd,
    gh_auth,
    logging_setup,
    overview,
    paths,
    phase,
    pickers,
    progress,
    sandbox,
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
    state_letter: str  # combined (highest-priority) letter, for display only
    # True iff ANY live slot's letter is in BUSY_LETTERS. Not derivable from
    # `state_letter` alone: `combined_letter` picks the highest-*priority*
    # letter (X > W > R > B > Z > I > S), so an errored default slot (X)
    # alongside a running secondary (R) combines to "X" for display even
    # though the window has a genuinely busy agent in it.
    busy: bool = False


def _eligible(proj: config.Project) -> bool:
    """A project can be rebuilt iff it has a recreation recipe: a sandbox
    (the projects.toml sbx_* keys ARE the recipe), a devcontainer, or a
    named container with an *explicitly configured* `up_cmd` (the
    auto-defaulted devcontainer up_cmd doesn't count — a pre-existing named
    container has no way to recreate itself)."""
    if proj.backend == config.BACKEND_SANDBOX:
        return True
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
        codes = [s.code for s in overview.parse_state_code(w.state_code)]
        letter = phase.combined_letter(codes)
        busy = any(c in BUSY_LETTERS for c in codes)
        out.setdefault(m.project, []).append(
            Affected(mapping=m, window_name=w.name, state_letter=letter, busy=busy)
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
    project: str,
    affected: list[Affected],
    busy: list[Affected],
    *,
    is_sandbox: bool = False,
) -> None:
    n = len(affected)
    what = (
        "recreates the sandbox VM (sessions and logins persist)"
        if is_sandbox
        else "recreates the shared container"
    )
    if not affected:
        where = "sandbox" if is_sandbox else "container"
        print(f"Rebuilding {project}: no agents are currently in its {where}.")
        return
    noun = "agent" if n == 1 else "agents"
    if busy:
        print(
            f"⚠  Rebuilding {project} {what} and kills all "
            f"{n} {noun} in it. {len(busy)} actively working:"
        )
    else:
        print(
            f"Rebuilding {project} {what} and kills all "
            f"{n} {noun} in it (all idle/sleeping):"
        )
    for a in affected:
        marker = "   ← busy" if a.busy else ""
        label = a.mapping.branch or a.window_name
        print(f"     {a.state_letter}  {label}{marker}")
    print(
        "Agents will be auto-resumed after the rebuild. "
        "(Sleeping agents lose any pending scheduled wakeup until they resume.)"
    )


def _confirm(
    project: str,
    affected: list[Affected],
    *,
    assume_yes: bool,
    is_sandbox: bool = False,
) -> bool:
    if assume_yes:
        return True
    busy = [a for a in affected if a.busy]
    _print_warning(project, affected, busy, is_sandbox=is_sandbox)
    try:
        return pickers.prompt_yes_no(f"rebuild {project}? ", default=not busy)
    except (pickers.Cancelled, KeyboardInterrupt):
        return False


# ===== worker half =====


def _fail_pane(a: Affected, reason: str) -> None:
    """Show the rebuild failure in every one of the window's live agent
    panes (not just the default slot) and flip each to errored."""
    body = (
        f"\n  agent-rebuild failed for {a.window_name}\n  reason: {reason}\n\n"
        "  Fix the underlying issue (e.g. start Docker), then re-run:\n"
        f"    agent-rebuild {a.mapping.project}\n\n"
    )
    for slot in a.mapping.agents:
        if slot.pane_id is None:
            continue
        shown = startup.show_static_text(f"%{slot.pane_id}", body)
        startup._write_pane_state(
            a.mapping.host_worktree, shown.lstrip("%"), phase_value=phase.ERRORED
        )


def _live_slots(
    affected: list[Affected],
) -> list[tuple[Affected, windows_mod.AgentSlot]]:
    """Every (window, slot) pair across `affected` whose slot has a live
    pane — flattens each window's `mapping.agents` list, skipping dead
    (`pane_id is None`) secondary slots."""
    return [
        (a, slot)
        for a in affected
        for slot in a.mapping.agents
        if slot.pane_id is not None
    ]


def _show_placeholders(affected: list[Affected]) -> list[Affected]:
    """Show live rebuild output where each agent used to be. Returns
    `affected` with the mapping re-read for any window whose pane
    `startup.respawn` had to replace, so later stages target the new pane."""
    replaced: set[str] = set()
    for a, slot in _live_slots(affected):
        pane = startup.respawn(
            f"%{slot.pane_id}",
            startup.placeholder_command(a.mapping.window_id),
        )
        startup._write_pane_state(
            a.mapping.host_worktree, pane.lstrip("%"), phase_value=phase.STARTING
        )
        if pane != f"%{slot.pane_id}":
            replaced.add(a.mapping.window_id)
    return [
        dataclasses.replace(a, mapping=fresh)
        if a.mapping.window_id in replaced
        and (fresh := windows_mod.read_mapping(a.mapping.window_id)) is not None
        else a
        for a in affected
    ]


@contextlib.contextmanager
def _multi_reporter(
    affected: list[Affected], banner: str
) -> Iterator[progress.MultiReporter]:
    """One spawn log + Reporter per affected window. On exit the logs are
    closed, then removed (callbacks unwind LIFO)."""
    with contextlib.ExitStack() as logs:
        logs.callback(_remove_spawn_logs, affected)
        reporters: list[progress.Reporter] = []
        for a in affected:
            log_path = paths.spawn_log(a.mapping.window_id)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            f = logs.enter_context(open(log_path, "w", buffering=1))
            r = progress.Reporter(out=f, color=True, clock=time.monotonic)
            r.banner(banner)
            reporters.append(r)
        yield progress.MultiReporter(reporters)


def _remove_spawn_logs(affected: list[Affected]) -> None:
    for a in affected:
        paths.spawn_log(a.mapping.window_id).unlink(missing_ok=True)


def _run_worker(
    proj: config.Project, affected: list[Affected], *, no_cache: bool
) -> int:
    """Detached: show progress in each pane, rebuild the container, respawn
    the SSH pump, and re-exec every live agent slot's pane. Per-pane
    failures are isolated; a container-rebuild failure marks every pane
    errored."""
    affected = _show_placeholders(affected)
    live = _live_slots(affected)

    with _multi_reporter(affected, f"Rebuilding container: {proj.name}") as multi:
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
            if proj.share_gh_auth:
                with multi.stage("gh auth") as st:
                    gh_auth.maybe_sync_gh_auth(
                        container_name,
                        proj.user or "vscode",
                    ).render(st)
        except container.ContainerError as ce:
            logger.error("rebuild failed for %r: %s", proj.name, ce)
            for a in affected:
                _fail_pane(a, f"container rebuild failed: {ce}")
            return 1

        # After the container rebuild, before re-exec'ing panes: cheap
        # idempotent codex hook provisioning check, mirroring agent-new's
        # Task 15 "codex hooks" stage. Non-fatal — a failure here must
        # never block resuming the rebuilt project's agents.
        with multi.stage("codex hooks") as st:
            try:
                if proj.is_container:
                    codex_hooks.ensure_container(container_name, proj.user or "vscode")
                else:
                    codex_hooks.ensure_host()
            except Exception as ex:
                st.warn(f"could not provision codex hooks: {type(ex).__name__}: {ex}")
                logger.warning(
                    "%s: codex hook provisioning failed (non-fatal)",
                    proj.name,
                    exc_info=True,
                )

    # Container is up; re-exec every live slot's pane, isolating failures.
    failures = 0
    for a, slot in live:
        m = a.mapping
        try:
            cmd = exec_cmd.build(
                proj,
                branch=m.branch,
                session_id=slot.session_id,
                container_name=container_name,
                kind=slot.kind,
                label=m.window_id,
            )
            pane = startup.respawn(f"%{slot.pane_id}", cmd)
            startup._write_pane_state(
                m.host_worktree, pane.lstrip("%"), phase_value=phase.STARTING
            )
            logger.info("%s: respawned pane=%s", m.window_id, pane)
        except Exception:
            failures += 1
            logger.exception("%s: respawn failed", m.window_id)
    logger.info(
        "rebuilt %r; respawned %d/%d agent slot(s)",
        proj.name,
        len(live) - failures,
        len(live),
    )
    return 0 if failures == 0 else 1


def _run_sandbox_worker(
    proj: config.Project,
    affected: list[Affected],
    *,
    no_cache: bool,
) -> int:
    """Sandbox rebuild = rm + create under the same name (new workload /
    kits / mounts / memory take effect), re-provision codex hooks + gh
    auth, respawn every live slot with its resume id. Nothing to carry:
    the state kit's volumes are retained by `sbx rm` and reattached by the
    same-name create (sessions, memory, scratchpads, histories), and both
    logins are proxy-managed on the host."""
    if no_cache:
        logger.warning(
            "--no-cache is meaningless for sandbox project %r (kits are "
            "resolved by sbx) — proceeding without it",
            proj.name,
        )
    affected = _show_placeholders(affected)
    live = _live_slots(affected)

    name = proj.sandbox_name
    with _multi_reporter(affected, f"Rebuilding sandbox: {name}") as multi:
        try:
            with multi.stage("sbx daemon"):
                sandbox.ensure_daemon()
            with multi.stage("recreate sandbox") as st:
                st.info("recreating (this may take minutes)…")
                # rm + create as ONE critical section — a concurrent
                # ensure_up must not slip its own create in between.
                sandbox.recreate(proj)
        except sandbox.SandboxError as ex:
            logger.error("sandbox rebuild failed for %r: %s", proj.name, ex)
            for a in affected:
                _fail_pane(a, f"sandbox rebuild failed: {ex}")
            return 1

        # Rebuild deletes the installed codex hooks by construction —
        # re-provision unconditionally, non-fatal like everywhere else.
        with multi.stage("codex hooks") as st:
            try:
                codex_hooks.ensure_sandbox(name)
            except Exception as ex:
                st.warn(f"could not provision codex hooks: {type(ex).__name__}: {ex}")
                logger.warning(
                    "%s: codex hook provisioning failed (non-fatal)",
                    proj.name,
                    exc_info=True,
                )

        # The recreate wiped the sandbox's gh login (not on a state volume —
        # the host token is the source of truth).
        if proj.share_gh_auth:
            with multi.stage("gh auth") as st:
                gh_auth.maybe_sync_gh_auth_sandbox(name).render(st)

    # Sandbox is up; re-exec every live slot's pane, isolating failures.
    failures = 0
    for a, slot in live:
        m = a.mapping
        try:
            cmd = exec_cmd.build(
                proj,
                branch=m.branch,
                session_id=slot.session_id,
                container_name=None,
                kind=slot.kind,
                label=m.window_id,
            )
            pane = startup.respawn(f"%{slot.pane_id}", cmd)
            startup._write_pane_state(
                m.host_worktree, pane.lstrip("%"), phase_value=phase.STARTING
            )
            logger.info("%s: respawned pane=%s", m.window_id, pane)
        except Exception:
            failures += 1
            logger.exception("%s: respawn failed", m.window_id)
    logger.info(
        "rebuilt sandbox %r; respawned %d/%d agent slot(s)",
        proj.name,
        len(live) - failures,
        len(live),
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
        # Same detach dance as `agent-new --provision`: run-shell -b keeps the
        # job's stdout/stderr pipe, and tmux paints any output (devcontainer
        # up's JSON, a nonzero exit notice) over the active pane in view mode
        # until a key is pressed. Fork, drop the pipe, let the parent exit 0.
        if os.fork() > 0:
            return 0
        os.setsid()
        startup._detach_stdio()
        proj = projects.get(project)
        if proj is None:
            logger.error("worker: unknown project %r", project)
            return 2
        affected = _gather_affected(tmux.list_windows(tmux.SESSION)).get(project, [])
        if proj.backend == config.BACKEND_SANDBOX:
            return _run_sandbox_worker(proj, affected, no_cache=args.no_cache)
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
    if not _confirm(
        project,
        affected,
        assume_yes=args.yes,
        is_sandbox=proj.backend == config.BACKEND_SANDBOX,
    ):
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
