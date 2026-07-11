import socket
import subprocess
import sys
from importlib import resources

from tmux_agents import _ssh_framing as f
from tmux_agents import _ssh_relay_script as relay


def test_main_returns_exit_duplicate_when_other_relay_serving(
    tmp_sock_dir, monkeypatch
):
    """Distinct exit code so the pump's supervise loop knows to defer
    instead of retrying — otherwise we'd busy-loop racing the live relay."""
    sock_path = tmp_sock_dir / "live.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(sock_path))
    s.listen(1)
    try:
        monkeypatch.setattr(relay, "UDS_PATH", str(sock_path))
        rc = relay.main()
        assert rc == f.EXIT_DUPLICATE
        assert f.EXIT_DUPLICATE != 0
    finally:
        s.close()
        sock_path.unlink(missing_ok=True)


def test_try_connect_existing_returns_false_when_path_missing(tmp_sock_dir):
    sock_path = tmp_sock_dir / "nope.sock"
    assert relay.try_connect_existing(str(sock_path)) is False


def test_try_connect_existing_returns_false_for_regular_file_at_path(tmp_sock_dir):
    sock_path = tmp_sock_dir / "stale.sock"
    sock_path.write_bytes(b"")  # not a real listening socket
    assert relay.try_connect_existing(str(sock_path)) is False


def test_try_connect_existing_returns_false_for_orphaned_socket_file(tmp_sock_dir):
    sock_path = tmp_sock_dir / "orphan.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(sock_path))
    s.listen(1)
    s.close()  # listener gone, but socket file remains on disk
    assert sock_path.exists()
    assert relay.try_connect_existing(str(sock_path)) is False
    sock_path.unlink(missing_ok=True)


def test_try_connect_existing_returns_true_when_listener_up(tmp_sock_dir):
    sock_path = tmp_sock_dir / "live.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(sock_path))
    s.listen(1)
    try:
        assert relay.try_connect_existing(str(sock_path)) is True
    finally:
        s.close()
        sock_path.unlink(missing_ok=True)


def test_bind_listening_socket_fresh_path(tmp_sock_dir):
    sock_path = tmp_sock_dir / "fresh.sock"
    s = relay.bind_listening_socket(str(sock_path))
    try:
        assert sock_path.exists()
        mode = sock_path.stat().st_mode & 0o777
        assert mode == 0o600
    finally:
        s.close()
        sock_path.unlink(missing_ok=True)


def test_bind_listening_socket_unlinks_stale(tmp_sock_dir):
    sock_path = tmp_sock_dir / "stale.sock"
    sock_path.write_bytes(b"")  # pretend stale leftover
    s = relay.bind_listening_socket(str(sock_path))
    try:
        assert sock_path.exists()
        # path now resolves to our new bound socket; mode is 0600
        assert sock_path.stat().st_mode & 0o777 == 0o600
    finally:
        s.close()
        sock_path.unlink(missing_ok=True)


def test_relay_imports_framing_as_sibling_without_package(tmp_path):
    """In-container layout: relay + framing delivered as sibling files, with no
    `tmux_agents` package present. `-E -S` strips env + site so the
    `from _ssh_framing import` fallback is exercised; importing the module must
    succeed. Replaces the old assembled-source compile guard with a check of the
    real delivered-file import path."""
    for name in ("_ssh_framing.py", "_ssh_relay_script.py"):
        (tmp_path / name).write_text(
            resources.files("tmux_agents").joinpath(name).read_text()
        )
    code = (
        f"import sys; sys.path.insert(0, {str(tmp_path)!r}); "
        "import _ssh_relay_script as m; print(m.UDS_PATH)"
    )
    r = subprocess.run(
        [sys.executable, "-E", "-S", "-c", code],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "/tmp/tmux-agents-ssh.sock" in r.stdout
