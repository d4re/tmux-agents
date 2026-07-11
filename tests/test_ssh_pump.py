import os
import socket
import subprocess
import threading
from unittest.mock import MagicMock

from tmux_agents import _ssh_framing as f
from tmux_agents import _ssh_pump_script as pump


def test_supervise_exits_when_ssh_auth_sock_unset(monkeypatch):
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    spawn_calls = []
    rc = pump.supervise(
        "c",
        "vscode",
        run_one=lambda c, u: spawn_calls.append((c, u)) or 0,
        container_alive=lambda c: True,
        sleep=lambda s: None,
    )
    assert rc == 0
    assert spawn_calls == []  # never even tried to spawn


def test_supervise_exits_when_relay_signals_duplicate(monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/x")
    rc = pump.supervise(
        "c",
        "vscode",
        run_one=lambda c, u: f.EXIT_DUPLICATE,
        container_alive=lambda c: True,
        sleep=lambda s: None,
    )
    assert rc == 0


def test_supervise_exits_when_container_gone(monkeypatch):
    """Relay died and the container is no longer running — give up cleanly,
    don't busy-loop trying to docker exec into a dead container."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/x")
    rc = pump.supervise(
        "c",
        "vscode",
        run_one=lambda c, u: 0,
        container_alive=lambda c: False,
        sleep=lambda s: None,
    )
    assert rc == 0


def test_supervise_retries_on_transient_failure_then_exits_when_container_dies(
    monkeypatch,
):
    """Three transient failures (container alive), then container goes away."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/x")
    run_calls = 0
    alive_seq = [True, True, False]
    sleeps: list[float] = []

    def run_one(c, u):
        nonlocal run_calls
        run_calls += 1
        return 0  # treat as transient

    def container_alive(c):
        return alive_seq.pop(0)

    rc = pump.supervise(
        "c",
        "vscode",
        run_one=run_one,
        container_alive=container_alive,
        sleep=sleeps.append,
    )
    assert rc == 0
    assert run_calls == 3  # alive,alive,alive then 4th check returns False
    assert sleeps == [1, 2]  # backoff doubled between retries; 3rd round bails


def test_supervise_caps_backoff(monkeypatch):
    """Backoff caps at 30s — long-running outage shouldn't sleep an hour."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/x")
    sleeps: list[float] = []
    iterations = {"n": 0}

    def container_alive(c):
        iterations["n"] += 1
        return iterations["n"] < 10  # keep alive for 9 iterations

    pump.supervise(
        "c",
        "vscode",
        run_one=lambda c, u: 0,
        container_alive=container_alive,
        sleep=sleeps.append,
    )
    assert sleeps[-1] == 30
    assert max(sleeps) == 30


def _pipe_pair():
    r, w = os.pipe()
    return os.fdopen(r, "rb", buffering=0), os.fdopen(w, "wb", buffering=0)


def test_run_pump_loop_round_trips_frames_to_host_uds(tmp_sock_dir, monkeypatch):
    # Fake host SSH_AUTH_SOCK: a UDS we listen on in this test.
    auth_sock_path = tmp_sock_dir / "auth.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(auth_sock_path))
    listener.listen(1)
    monkeypatch.setenv("SSH_AUTH_SOCK", str(auth_sock_path))

    # Simulated docker-exec stdio:
    #   stdout pipe = "what the in-container relay writes" (framed bytes coming TO pump)
    #   stdin pipe  = "what the pump writes to the relay" (framed bytes going FROM pump)
    relay_to_pump_r, relay_to_pump_w = _pipe_pair()  # we write, pump reads as stdout
    pump_to_relay_r, pump_to_relay_w = (
        _pipe_pair()
    )  # pump writes, we read as stdin sink

    # Server thread: accept the pump's connection to auth.sock, echo bytes.
    received = []

    def echo_server():
        conn, _ = listener.accept()
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                received.append(data)
                conn.sendall(b"ack:" + data)
        finally:
            conn.close()

    threading.Thread(target=echo_server, daemon=True).start()

    # Run the pump loop in a thread.
    t = threading.Thread(
        target=pump.run_pump_loop,
        args=(relay_to_pump_r, pump_to_relay_w),
        daemon=True,
    )
    t.start()

    # Simulate "in-container UDS connection opens, sends data": send a frame.
    relay_to_pump_w.write(f.encode_frame(b"req1"))
    relay_to_pump_w.flush()

    # Read back the echoed reply (framed) on the pump-to-relay pipe.
    head = pump_to_relay_r.read(4)
    length = int.from_bytes(head, "big")
    assert length > 0
    reply = pump_to_relay_r.read(length)
    assert reply == b"ack:req1"
    assert received == [b"req1"]

    # Send sentinel to indicate "in-container UDS closed". Pump should close
    # its host UDS connection and loop back to wait for a new op.
    relay_to_pump_w.write(f.encode_sentinel())
    relay_to_pump_w.flush()

    # Then close pipe to terminate the pump.
    relay_to_pump_w.close()
    t.join(timeout=2)
    assert not t.is_alive()
    listener.close()
    pump_to_relay_w.close()
    relay_to_pump_r.close()


def test_deliver_relay_pipes_both_source_files(monkeypatch):
    """Both framing + relay source are delivered into the container via
    `docker exec ... cat >`, read verbatim from package data (no splicing)."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw.get("input")))
        return MagicMock(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    pump._deliver_relay("api", "vscode")
    assert len(calls) == 2
    for cmd, inp in calls:
        assert cmd[:6] == ["docker", "exec", "-i", "-u", "vscode", "api"]
        assert cmd[6:8] == ["sh", "-c"]
        assert "mkdir -p /tmp/tmux-agents-relay" in cmd[8]
        assert inp  # verbatim source piped on stdin
    targets = " ".join(c[8] for c, _ in calls)
    assert "/tmp/tmux-agents-relay/_ssh_framing.py" in targets
    assert "/tmp/tmux-agents-relay/_ssh_relay_script.py" in targets


def test_run_one_relay_runs_delivered_file(monkeypatch):
    """_run_one_relay delivers, then runs the relay FILE (not a `-c` blob)."""
    monkeypatch.setattr(pump, "_deliver_relay", lambda c, u: None)
    monkeypatch.setattr(pump, "run_pump_loop", lambda out, inp: None)
    captured = {}

    class FakeProc:
        def __init__(self):
            self.stdout = object()
            self.stdin = MagicMock()

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    rc = pump._run_one_relay("api", "vscode")
    assert rc == 0
    assert captured["cmd"] == [
        "docker",
        "exec",
        "-i",
        "-u",
        "vscode",
        "api",
        "python3",
        "/tmp/tmux-agents-relay/_ssh_relay_script.py",
    ]


def test_run_one_relay_returns_1_when_delivery_fails(monkeypatch):
    """Delivery failure (e.g. container mid-restart) is retryable, not fatal."""

    def boom(c, u):
        raise subprocess.CalledProcessError(1, ["docker", "exec"])

    monkeypatch.setattr(pump, "_deliver_relay", boom)

    def no_popen(*a, **k):
        raise AssertionError("relay should not run when delivery fails")

    monkeypatch.setattr(subprocess, "Popen", no_popen)
    assert pump._run_one_relay("api", "vscode") == 1
