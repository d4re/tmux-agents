import shutil
import subprocess
import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)

SESSION = "agents-smoke"


@pytest.fixture
def ephemeral_session(tmp_sock_dir, monkeypatch):
    # Dedicated tmux socket inside a tmp dir so this smoke test never touches the
    # user's live `-L agents` server (a kill-server here stays contained) and the
    # socket file is cleaned up with the tmp dir — no leftover sockets. conftest's
    # autouse `_auto_isolate_paths` already isolates the config/state dirs; the
    # socket is the one thing tmux.py hardcodes (`-L agents`), so override it too.
    tmuxbin = ["tmux", "-S", str(tmp_sock_dir / "sock")]
    monkeypatch.setattr("tmux_agents.tmux._TMUX", tmuxbin)
    monkeypatch.setattr("tmux_agents.tmux.SESSION", SESSION)

    def _tmux(*args):
        return subprocess.run(
            [*tmuxbin, *args], capture_output=True, text=True, check=False
        )

    _tmux("new-session", "-d", "-s", SESSION, "-n", "ctrl")
    _tmux(
        "new-window",
        "-t",
        SESSION,
        "-n",
        "api:feat-x",
        "-d",
        "bash -c 'while true; do sleep 60; done'",
    )
    try:
        yield
    finally:
        _tmux("kill-server")  # dedicated socket → safe, full teardown


def test_state_tick_runs_and_emits_summary(ephemeral_session, capsys):
    """End-to-end: state_tick against a real tmux session writes per-window
    state files, sets per-window options, and prints the status-line summary
    chunk on stdout (the same chunk that fills `#(agent-state)`).

    The TUI itself (`agent-overview`) isn't smoke-tested — `curses.wrapper`
    requires a real terminal and exits non-zero under pytest.
    """
    from tmux_agents.commands import state_tick

    assert state_tick.main([]) == 0
    out = capsys.readouterr().out
    assert any(c in out for c in "RWILX")  # at least one state code rendered
