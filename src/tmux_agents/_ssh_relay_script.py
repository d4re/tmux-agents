"""In-container SSH agent relay.

Delivered into the container as a plain file (alongside `_ssh_framing.py`) by
the host pump and run as `python3 <dir>/_ssh_relay_script.py`; also importable
by tests as `tmux_agents._ssh_relay_script`.
"""

# Framing names come from the installed package when imported normally (tests),
# or from the sibling `_ssh_framing.py` on sys.path[0] when run as a delivered
# file inside the container (where the `tmux_agents` package isn't installed).
try:
    from tmux_agents._ssh_framing import (  # noqa: F401
        SENTINEL,
        encode_frame,
        encode_sentinel,
        read_frame,
        FrameError,
        splice,
        EXIT_DUPLICATE,
    )
except ModuleNotFoundError:
    from _ssh_framing import (  # noqa: F401
        SENTINEL,
        encode_frame,
        encode_sentinel,
        read_frame,
        FrameError,
        splice,
        EXIT_DUPLICATE,
    )

import errno
import os
import socket
import sys

UDS_PATH = "/tmp/tmux-agents-ssh.sock"


def try_connect_existing(path: str) -> bool:
    """Return True iff something is currently accepting on `path`."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
        return True
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    except OSError as e:
        if e.errno == errno.ENOTSOCK:
            return False
        raise
    finally:
        s.close()


def bind_listening_socket(path: str) -> socket.socket:
    """Bind+listen at `path`, mode 0600, after unlinking any stale leftover."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # fchmod on the socket fd is a silent no-op for the bound path on Linux
    # (it changes the sockfs inode, not the filesystem entry), so the only
    # race-free way to get 0600 is restricting the umask at bind time.
    old_umask = os.umask(0o177)
    try:
        s.bind(path)
    finally:
        os.umask(old_umask)
    s.listen(1)
    return s


def main() -> int:
    if try_connect_existing(UDS_PATH):
        # Another relay is already serving this container.
        print(f"relay: another relay is serving {UDS_PATH}; exiting.", file=sys.stderr)
        return EXIT_DUPLICATE
    listener = bind_listening_socket(UDS_PATH)
    try:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError as e:
                print(f"relay: accept failed: {e}", file=sys.stderr)
                return 1
            try:
                splice(conn, sys.stdin.buffer, sys.stdout.buffer)
            finally:
                conn.close()
    finally:
        listener.close()
        try:
            os.unlink(UDS_PATH)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    sys.exit(main())
