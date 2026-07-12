import pytest
from tmux_agents import startup, tmux, paths


def _tmux_error(stderr):
    return tmux.TmuxError(
        1, ["tmux", "-L", "agents", "respawn-pane"], output="", stderr=stderr
    )


def _fork_failure():
    return _tmux_error("respawn pane failed: fork failed: Device not configured")


def test_placeholder_command_is_tail_F(tmp_state_dir):
    cmd = startup.placeholder_command("@9")
    assert cmd.startswith("tail -F ")
    assert str(paths.spawn_log("@9")) in cmd


def test_placeholder_command_pre_creates_log(tmp_state_dir):
    """The log must exist before the pane runs `tail -F`, or tail prints
    "cannot open …: No such file or directory" / "has appeared" into the pane
    while the detached worker is still starting up."""
    log = paths.spawn_log("@9")
    assert not log.exists()
    startup.placeholder_command("@9")
    assert log.exists()


def test_respawn_retries_transient_fork_failure_then_succeeds(monkeypatch):
    """A `fork failed` respawn (transient OS pressure during the restore
    burst) is retried; the helper returns once a later attempt succeeds."""
    attempts = []

    def fake_respawn(pane_id, *, command):
        attempts.append(pane_id)
        if len(attempts) < 3:
            raise _fork_failure()

    monkeypatch.setattr(tmux, "respawn_pane", fake_respawn)
    sleeps: list[float] = []
    monkeypatch.setattr(startup.time, "sleep", lambda s: sleeps.append(s))

    startup._respawn_with_retry("%13", "tail -F log")

    assert len(attempts) == 3
    assert len(sleeps) == 2  # slept between the two failures


def test_respawn_does_not_retry_non_fork_error(monkeypatch):
    """A respawn failure that is NOT a fork failure (e.g. can't find pane)
    is a real error — raise immediately without retrying or sleeping."""
    attempts = []

    def fake_respawn(pane_id, *, command):
        attempts.append(pane_id)
        raise _tmux_error("can't find pane: %13")

    monkeypatch.setattr(tmux, "respawn_pane", fake_respawn)
    monkeypatch.setattr(
        startup.time,
        "sleep",
        lambda s: (_ for _ in ()).throw(
            AssertionError("should not sleep on a non-transient error")
        ),
    )

    with pytest.raises(tmux.TmuxError):
        startup._respawn_with_retry("%13", "tail -F log")
    assert len(attempts) == 1


def test_respawn_gives_up_after_max_attempts(monkeypatch):
    """Persistent fork failures eventually re-raise so the caller can fall
    back to its skip-and-log handling."""
    attempts = []

    def fake_respawn(pane_id, *, command):
        attempts.append(pane_id)
        raise _fork_failure()

    monkeypatch.setattr(tmux, "respawn_pane", fake_respawn)
    monkeypatch.setattr(startup.time, "sleep", lambda s: None)

    with pytest.raises(tmux.TmuxError):
        startup._respawn_with_retry("%13", "tail -F log")
    assert len(attempts) == startup._FORK_RETRY_ATTEMPTS


def test_detach_stdio_redirects_std_fds_to_devnull():
    """--background must redirect fd 0/1/2 to /dev/null. Otherwise a child
    launched via tmux `run-shell` keeps run-shell's capture pipe as stdout and
    tmux paints the child's output (e.g. `devcontainer up` JSON) over the
    active pane."""

    class FakeOS:
        O_RDWR = 2
        devnull = "/dev/null"

        def __init__(self):
            self.dup2_calls: list[tuple[int, int]] = []
            self.closed: list[int] = []

        def open(self, path, flags):
            assert path == "/dev/null" and flags == self.O_RDWR
            return 7

        def dup2(self, src, dst):
            self.dup2_calls.append((src, dst))

        def close(self, fd):
            self.closed.append(fd)

    fake = FakeOS()
    startup._detach_stdio(_os=fake)
    assert fake.dup2_calls == [(7, 0), (7, 1), (7, 2)]
    assert fake.closed == [7]  # devnull fd > 2 closed after duping onto std fds


def test_show_static_text_respawns_heredoc(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tmux,
        "respawn_pane",
        lambda pane_id, *, command: calls.append((pane_id, command)),
    )
    startup.show_static_text("%5", "\n  hello world\n")
    assert len(calls) == 1
    pid, cmd = calls[0]
    assert pid == "%5"
    assert "hello world" in cmd
    assert cmd.startswith('sh -c \'cat <<"EOF"')


def test_hold_pane_then_exec_shows_log_and_execs(monkeypatch, tmp_state_dir):
    calls = []
    monkeypatch.setattr(
        tmux,
        "respawn_pane",
        lambda pane_id, *, command: calls.append((pane_id, command)),
    )
    log = paths.spawn_log("@1")
    startup.hold_pane_then_exec("%7", log, "claude --resume abc")
    assert len(calls) == 1
    pid, cmd = calls[0]
    assert pid == "%7"
    assert str(log) in cmd  # prints the startup log
    assert "press Enter" in cmd  # the hold prompt
    assert "read" in cmd  # waits for Enter
    assert "exec claude --resume abc" in cmd  # then launches Claude
