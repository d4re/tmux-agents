"""Sole module that shells out to `tmux -L agents`. Add new tmux
invocations here, not inline in callers."""

from __future__ import annotations
import subprocess
from dataclasses import dataclass
from pathlib import Path

SESSION = "agents"
CONTROL_WINDOW = "ctrl"

_TMUX = ["tmux", "-L", "agents"]


def legacy_new_session_argv(conf: "Path") -> list[str]:
    """`tmux -L agents -f <conf> new-session -A` — launcher's no-snapshot path."""
    return [
        *_TMUX,
        "-f",
        str(conf),
        "new-session",
        "-A",
        "-s",
        SESSION,
        "-n",
        CONTROL_WINDOW,
    ]


def attach_argv() -> list[str]:
    return [*_TMUX, "attach", "-t", SESSION]


@dataclass(frozen=True)
class Window:
    id: str
    index: int
    name: str
    active: bool = False
    state_code: str = (
        ""  # @state_code option: display letter (+overlay), "" until first tick
    )


@dataclass(frozen=True)
class Pane:
    id: str
    index: int


class TmuxError(subprocess.CalledProcessError):
    """CalledProcessError whose str() includes tmux's captured stderr.

    Subclasses CalledProcessError so existing `except CalledProcessError`
    handlers catch it unchanged. The base class's __str__ reports only the
    exit code — which is why a failed `respawn-pane` logged a bare
    'returned non-zero exit status 1' with no reason. Appending stderr turns
    that into tmux's actual complaint (e.g. "can't find pane: %5")."""

    def __str__(self) -> str:
        base = super().__str__()
        err = (self.stderr or "").strip()
        return f"{base} -- tmux stderr: {err}" if err else base


def _run(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    proc = subprocess.run([*_TMUX, *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise TmuxError(
            proc.returncode, proc.args, output=proc.stdout, stderr=proc.stderr
        )
    return proc


def session_exists(name: str) -> bool:
    return _run(["has-session", "-t", name]).returncode == 0


def new_session(name: str, *, window_name: str) -> None:
    _run(["new-session", "-d", "-s", name, "-n", window_name], check=True)


def list_windows(session: str) -> list[Window]:
    # check=True so a transient tmux failure raises instead of silently
    # returning [] — the latter would let the state-tick prune wipe every
    # mapping file in one go.
    out = _run(
        [
            "list-windows",
            "-t",
            session,
            "-F",
            "#{window_id}\t#{window_index}\t#{window_name}\t#{window_active}\t#{@state_code}",
        ],
        check=True,
    ).stdout
    windows: list[Window] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        wid, idx, name, active, code = line.split("\t", 4)
        windows.append(
            Window(
                id=wid, index=int(idx), name=name, active=active == "1", state_code=code
            )
        )
    return windows


def list_panes(window_id: str) -> list[Pane]:
    out = _run(
        ["list-panes", "-t", window_id, "-F", "#{pane_id}\t#{pane_index}"]
    ).stdout
    panes: list[Pane] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        pid, idx = line.split("\t", 1)
        panes.append(Pane(id=pid, index=int(idx)))
    return panes


def overview_pane_ids(window_id: str) -> list[str]:
    """Pane ids in `window_id` tagged `@role overview`, in pane-index order.
    Empty when the window has no overview pane (e.g. compact layout)."""
    out = _run(["list-panes", "-t", window_id, "-F", "#{pane_id}\t#{@role}"]).stdout
    ids: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1] == "overview":
            ids.append(parts[0])
    return ids


def rename_window(target: str, name: str) -> None:
    _run(["rename-window", "-t", target, name], check=True)


def new_window(
    session: str, *, name: str, command: str, after_target: str | None = None
) -> str:
    # `after_target` (e.g. `@7`) places the new window immediately after the
    # given window via `new-window -a -t <target>`. Combined with
    # `renumber-windows on` this keeps same-project windows contiguous in
    # the tab strip (see commands/new.py).
    args = ["new-window", "-P", "-F", "#{window_id}"]
    if after_target is not None:
        args += ["-a", "-t", after_target]
    else:
        args += ["-t", f"{session}:"]
    args += ["-n", name, "-d", command]
    return _run(args, check=True).stdout.strip()


def kill_window(target: str) -> None:
    _run(["kill-window", "-t", target], check=True)


def split_window(
    target: str, *, percent: int, command: str, before: bool = False
) -> str:
    """Split `target` (window id or pane id) and return the new pane id.

    `before=True` adds `-b` so the new pane lands above (`-v` direction).
    `-d` keeps focus on the original pane.

    Percentage goes through `-l <n>%`, not `-p <n>`: tmux 3.4 (current
    Ubuntu/Debian stable) still parses `-p` but fails every split with
    `size missing`; `-l <n>%` is the documented percentage syntax and
    works on 3.4 and later alike."""
    args = [
        "split-window",
        "-v",
        "-d",
        "-l",
        f"{percent}%",
        "-P",
        "-F",
        "#{pane_id}",
        "-t",
        target,
    ]
    if before:
        args.insert(1, "-b")
    args.append(command)
    return _run(args, check=True).stdout.strip()


def kill_pane(target: str) -> None:
    _run(["kill-pane", "-t", target], check=True)


def select_window(target: str) -> None:
    _run(["select-window", "-t", target], check=True)


def select_pane(pane_id: str) -> None:
    _run(["select-pane", "-t", pane_id], check=True)


def current_pane_id() -> str:
    """pane_id of the pane this process is running in (always set under tmux)."""
    return _run(["display-message", "-p", "#{pane_id}"], check=True).stdout.strip()


def set_window_option(window_id: str, name: str, value: str) -> None:
    _run(["set-option", "-wt", window_id, name, value], check=True)


def is_window_pinned(window_id: str) -> bool:
    """True when `@pinned 1` is set on the window — see `agent-rename`.

    `-q` suppresses the missing-option error; unset returns empty stdout."""
    r = _run(["show-options", "-wvqt", window_id, "@pinned"])
    return r.stdout.strip() == "1"


def run_shell_bg(command: str) -> None:
    """Run `command` detached on the tmux server via `run-shell -b`.

    Parented by the long-lived tmux server, not the caller's process tree, so
    it survives a `display-popup` that closes immediately after spawning it.
    `subprocess.Popen(..., start_new_session=True)` from inside a popup does
    NOT survive: tmux tears down the popup's process tree on close and the
    worker dies before it does any work."""
    _run(["run-shell", "-b", command], check=True)


def apply_commands(lines: list[str]) -> None:
    """Apply many tmux commands in a single subprocess via `source-file -`.
    Cheaper than one set-option call per item on the hot path."""
    if not lines:
        return
    subprocess.run(
        [*_TMUX, "source-file", "-"],
        input="\n".join(lines) + "\n",
        text=True,
        check=True,
    )


def window_pane_map(session: str) -> dict[str, set[str]]:
    """For each window in `session`, the set of LIVE pane ids (pane_dead=0).
    A window present with an empty set means all its panes are dead.

    Raises CalledProcessError on tmux failure (caller distinguishes
    transient failure from a legitimately empty session)."""
    r = _run(
        [
            "list-panes",
            "-s",
            "-t",
            session,
            "-F",
            "#{window_id}\t#{pane_id}\t#{pane_dead}",
        ],
        check=True,
    )
    result: dict[str, set[str]] = {}
    for line in r.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        wid, pid, dead = parts
        bucket = result.setdefault(wid, set())
        if dead == "0":
            bucket.add(pid)
    return result


def active_pane_id(window_id: str) -> str:
    return _run(["display-message", "-p", "-t", window_id, "#{pane_id}"]).stdout.strip()


def set_pane_option(pane_id: str, name: str, value: str) -> None:
    _run(["set-option", "-pt", pane_id, name, value], check=True)


def display_message(text: str) -> None:
    """Flash `text` in tmux's status line via `display-message`."""
    _run(["display-message", text])


def respawn_pane(pane_id: str, *, command: str) -> None:
    # -k kills the existing process; pane_id and TMUX_PANE env survive.
    _run(["respawn-pane", "-k", "-t", pane_id, command], check=True)


def start_server_detached_with_session(
    *,
    conf: "Path",
    session: str,
    window_name: str,
) -> None:
    """Spawn the tmux server detached so the launcher can run the restore
    worker before any client attaches (status line stays quiet until then)."""
    subprocess.run(
        [
            *_TMUX,
            "-f",
            str(conf),
            "new-session",
            "-d",
            "-s",
            session,
            "-n",
            window_name,
        ],
        check=True,
    )
