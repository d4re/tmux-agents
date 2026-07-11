"""Host-side SSH agent pump.

Importable by tests; also run detached on the host as
`python -m tmux_agents._ssh_pump_script <container> <user>` (see
`tmux_agents.ssh_forward.spawn_pump`). It supervises an in-container relay,
which it delivers as plain files (`_ssh_framing.py` + `_ssh_relay_script.py`)
via `docker exec` and runs with `python3 <dir>/_ssh_relay_script.py`. Nothing
is inlined — the relay source is shipped verbatim from package data.
"""

from tmux_agents._ssh_framing import (  # noqa: F401
    SENTINEL,
    encode_frame,
    encode_sentinel,
    read_frame,
    FrameError,
    splice,
    EXIT_DUPLICATE,
)

import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import time
from importlib import resources

_LOGGER_NAME = "tmux_agents.ssh.pump"
# Format + rotation are duplicated from logging_setup so the pump's lines match
# the unified log without importing the host logging stack (keeps the pump's
# startup cheap; it runs as its own detached process).
_LOG_LINE_FMT = (
    "%(asctime)s %(levelname)-7s pid=%(process)d %(name)s[%(component)s]: %(message)s"
)
_LOG_DATE_FMT = "%Y-%m-%dT%H:%M:%SZ"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3

# Where the relay + framing source are delivered inside the container. Distinct
# from the relay's UDS at /tmp/tmux-agents-ssh.sock.
_RELAY_DIR = "/tmp/tmux-agents-relay"
_RELAY_FILES = ("_ssh_framing.py", "_ssh_relay_script.py")
_RELAY_ENTRY = f"{_RELAY_DIR}/_ssh_relay_script.py"


def _init_logging(container_name: str) -> None:
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return
    path = os.environ.get("TMUX_AGENTS_LOG_FILE")
    if not path:
        return
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
    )
    formatter = logging.Formatter(_LOG_LINE_FMT, datefmt=_LOG_DATE_FMT)
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)

    class _ComponentFilter(logging.Filter):
        def filter(self, record):
            record.component = container_name
            return True

    logger.addFilter(_ComponentFilter())
    logger.addHandler(handler)
    raw = os.environ.get("TMUX_AGENTS_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    level = logging.getLevelName(raw)
    logger.setLevel(level if isinstance(level, int) else logging.INFO)
    logger.propagate = False


def run_pump_loop(framed_stdout, framed_stdin) -> None:
    """Pump loop: for each in-container op, open a fresh host UDS connection
    and splice it.

    `framed_stdout` is what the relay writes to us (we read framed bytes).
    `framed_stdin`  is what we write to the relay (framed bytes going down).

    Loops until `framed_stdout` EOFs or the host SSH_AUTH_SOCK is unset.
    """
    while True:
        # Wait for the first frame of a new in-container op.
        try:
            payload = read_frame(framed_stdout)
        except FrameError as e:
            print(f"pump: framing error: {e}", file=sys.stderr)
            return
        if payload is None:
            return  # docker-exec stdio EOF
        if payload == b"":
            # Spurious sentinel with no live op; ignore.
            continue

        host_sock_path = os.environ.get("SSH_AUTH_SOCK")
        if not host_sock_path:
            print("pump: SSH_AUTH_SOCK not set on host; aborting.", file=sys.stderr)
            return

        try:
            host = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            host.connect(host_sock_path)
        except OSError as e:
            print(f"pump: connect to {host_sock_path} failed: {e}", file=sys.stderr)
            framed_stdin.write(encode_sentinel())
            framed_stdin.flush()
            continue

        try:
            host.sendall(payload)
            splice(host, framed_stdout, framed_stdin)
        except OSError as e:
            print(f"pump: host sendall/splice error: {e}", file=sys.stderr)
            try:
                framed_stdin.write(encode_sentinel())
                framed_stdin.flush()
            except OSError:
                pass
        finally:
            try:
                host.close()
            except OSError:
                pass


def _container_running(container: str) -> bool:
    """True iff `docker inspect` reports the container as running. Used by
    supervise to stop retrying once the container is gone."""
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0 and r.stdout.strip() == "true"


def _deliver_relay(container: str, user: str) -> None:
    """Copy the framing + relay source into the container, owned by `user`.

    Re-run on every (re)spawn so a restarted container (empty /tmp) gets a fresh
    copy. The source is read verbatim from package data and piped in via
    `docker exec ... cat >` — no source splicing. The relay then imports framing
    as a sibling on sys.path[0] (see `_ssh_relay_script`'s import fallback)."""
    for name in _RELAY_FILES:
        src = resources.files("tmux_agents").joinpath(name).read_text()
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "-u",
                user,
                container,
                "sh",
                "-c",
                f"mkdir -p {_RELAY_DIR} && cat > {_RELAY_DIR}/{name}",
            ],
            input=src,
            text=True,
            check=True,
        )


def _run_one_relay(container: str, user: str) -> int:
    """Deliver the relay, spawn one docker-exec-relay session, and pump until it
    ends. Returns the relay's exit code (-1 if SIGKILLed; 1 if delivery failed
    so supervise retries with backoff)."""
    try:
        _deliver_relay(container, user)
    except (OSError, subprocess.SubprocessError) as e:
        logging.getLogger(_LOGGER_NAME).warning("relay delivery failed: %s", e)
        return 1
    proc = subprocess.Popen(
        ["docker", "exec", "-i", "-u", user, container, "python3", _RELAY_ENTRY],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        bufsize=0,
    )
    try:
        run_pump_loop(proc.stdout, proc.stdin)
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
    return rc


def supervise(
    container: str,
    user: str,
    *,
    run_one=_run_one_relay,
    container_alive=_container_running,
    sleep=time.sleep,
) -> int:
    """Re-spawn the relay on transient failures so the pump survives
    container restarts, sleep/wake, and framing glitches. Exits cleanly when:
    SSH_AUTH_SOCK is gone, the relay said another one is serving, or the
    container is no longer running."""
    logger = logging.getLogger(_LOGGER_NAME)
    backoff = 1
    while True:
        if not os.environ.get("SSH_AUTH_SOCK"):
            logger.warning("SSH_AUTH_SOCK not set; exiting")
            return 0
        rc = run_one(container, user)
        if rc == EXIT_DUPLICATE:
            logger.info("another pump owns the relay; exiting")
            return 0
        if not container_alive(container):
            logger.info("container no longer running; exiting")
            return 0
        logger.info("relay exited (rc=%s); retrying in %ds", rc, backoff)
        sleep(backoff)
        backoff = min(backoff * 2, 30)


def main() -> int:
    if len(sys.argv) < 2:
        print("pump: usage: <pump> <container> [<user>]", file=sys.stderr)
        return 2
    container = sys.argv[1]
    user = sys.argv[2] if len(sys.argv) >= 3 else "vscode"
    _init_logging(container)
    return supervise(container, user)


if __name__ == "__main__":
    sys.exit(main())
