import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tmux_agents import tmux


def _stub_run(monkeypatch, stdout="", returncode=0):
    calls = []
    def fake_run(cmd, capture_output=False, text=False, check=False, input=None):
        calls.append(cmd)
        return MagicMock(stdout=stdout, returncode=returncode, stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


# ----- fire-and-forget commands: call → exact argv sent -----
# (callable, expected argv after the "tmux -L agents" prefix)
_PREFIX = ["tmux", "-L", "agents"]

@pytest.mark.parametrize("action,expected_tail", [
    (lambda: tmux.rename_window("@1", "new-name"),
     ["rename-window", "-t", "@1", "new-name"]),
    (lambda: tmux.select_window("@5"),
     ["select-window", "-t", "@5"]),
    (lambda: tmux.set_window_option("@1", "@state_fg", "green"),
     ["set-option", "-wt", "@1", "@state_fg", "green"]),
    (lambda: tmux.set_pane_option("%5", "@role", "overview"),
     ["set-option", "-pt", "%5", "@role", "overview"]),
    (lambda: tmux.select_pane("%7"),
     ["select-pane", "-t", "%7"]),
    (lambda: tmux.respawn_pane("%23", command="echo hi"),
     ["respawn-pane", "-k", "-t", "%23", "echo hi"]),
])
def test_fire_and_forget_argv(monkeypatch, action, expected_tail):
    calls = _stub_run(monkeypatch)
    action()
    assert calls == [_PREFIX + expected_tail]


# ----- stdout parsers: stdout → return value -----
@pytest.mark.parametrize("action,stdout,expected", [
    (lambda: tmux.current_pane_id(),       "%42\n", "%42"),
    (lambda: tmux.active_pane_id("@5"),    "%23\n", "%23"),
    (lambda: tmux.is_window_pinned("@1"),  "1\n",   True),
    (lambda: tmux.is_window_pinned("@1"),  "",      False),
])
def test_stdout_parsers(monkeypatch, action, stdout, expected):
    _stub_run(monkeypatch, stdout=stdout)
    assert action() == expected


def test_is_window_pinned_argv(monkeypatch):
    """Uses -q so unset @pinned returns empty stdout without erroring."""
    calls = _stub_run(monkeypatch, stdout="")
    tmux.is_window_pinned("@7")
    assert calls == [_PREFIX + ["show-options", "-wvqt", "@7", "@pinned"]]


# ----- exit-code-only predicates -----
@pytest.mark.parametrize("returncode,expected", [(0, True), (1, False)])
def test_session_exists(monkeypatch, returncode, expected):
    _stub_run(monkeypatch, returncode=returncode)
    assert tmux.session_exists("agents") is expected


# ----- list_windows: parses tab-separated rows -----
def test_list_windows_parses_output(monkeypatch):
    # 5th column is @state_code (the per-window display letter; "" until the
    # first tick sets it — exercised by @2's trailing empty field).
    _stub_run(monkeypatch, stdout="@1\t1\tapi:feat-x\t0\tR\n@2\t2\tweb:refactor\t1\t\n")
    wins = tmux.list_windows("agents")
    assert wins == [
        tmux.Window(id="@1", index=1, name="api:feat-x", active=False, state_code="R"),
        tmux.Window(id="@2", index=2, name="web:refactor", active=True, state_code=""),
    ]


def test_list_windows_raises_on_nonzero_exit(monkeypatch):
    """A failed `tmux list-windows` must raise, not silently return [].
    Returning [] would let state_tick's prune wipe every mapping file."""
    def fake_run(cmd, capture_output=False, text=False, check=False, input=None):
        if check:
            raise subprocess.CalledProcessError(1, cmd, "", "no server running")
        return MagicMock(stdout="", returncode=1, stderr="no server running")
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        tmux.list_windows("agents")


# ----- new_window: target-selection flags depend on `after_target` -----
def test_new_window_default_appends_to_session(monkeypatch):
    calls = _stub_run(monkeypatch, stdout="@5\n")
    wid = tmux.new_window("agents", name="api", command="docker exec -it x claude")
    assert wid == "@5"
    assert calls[0][:9] == _PREFIX + ["new-window", "-P", "-F", "#{window_id}", "-t", "agents:"]


def test_new_window_after_target_uses_a_flag(monkeypatch):
    """`after_target=<wid>` swaps the session target for `-a -t <wid>`,
    asking tmux to insert immediately after that window."""
    calls = _stub_run(monkeypatch, stdout="@9\n")
    wid = tmux.new_window("agents", name="api:feat-x", command="echo hi",
                          after_target="@5")
    assert wid == "@9"
    args = calls[0]
    assert "-a" in args
    t_idx = args.index("-t")
    assert args[t_idx + 1] == "@5"
    assert "agents:" not in args  # no fallback session target


def test_split_window_horizontal_75_25(monkeypatch):
    calls = _stub_run(monkeypatch, stdout="%7\n")
    pid = tmux.split_window("@5", percent=25, command="agent-overview")
    assert pid == "%7"
    assert "-v" in calls[0]
    assert "-l" in calls[0] and "25%" in calls[0]
    assert "-d" in calls[0]  # new pane must not steal focus from the agent pane


def test_window_pane_map_includes_only_live_panes(monkeypatch):
    calls = _stub_run(monkeypatch, stdout=(
        "@1\t%5\t0\n"   # live
        "@1\t%6\t1\n"   # dead -> excluded
        "@2\t%9\t0\n"   # live
        "@3\t%10\t1\n"  # only pane is dead -> @3 has empty set
    ))
    assert tmux.window_pane_map("agents") == {
        "@1": {"%5"}, "@2": {"%9"}, "@3": set(),
    }
    # Format string carries pane_id, not pane_active/pane_dead-active distinction.
    assert any("#{pane_id}" in a for a in calls[0])


def test_window_pane_map_raises_on_nonzero_exit(monkeypatch):
    def fake_run(cmd, capture_output=False, text=False, check=False, input=None):
        if check:
            raise subprocess.CalledProcessError(1, cmd, "", "no server running")
        return MagicMock(stdout="", returncode=1, stderr="no server running")
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        tmux.window_pane_map("agents")


def test_start_server_detached_with_session_command(monkeypatch):
    calls = _stub_run(monkeypatch)
    tmux.start_server_detached_with_session(
        conf=Path("/tmp/agents.conf"),
        session="agents",
        window_name="ctrl",
    )
    assert calls == [_PREFIX + [
        "-f", "/tmp/agents.conf",
        "new-session", "-d", "-s", "agents", "-n", "ctrl",
    ]]


def test_split_window_before_targets_pane_id(monkeypatch):
    calls = _stub_run(monkeypatch, stdout="%12\n")
    pid = tmux.split_window("%9", percent=75, command="tail -F log", before=True)
    assert pid == "%12"
    args = calls[0]
    assert "-b" in args
    assert "-v" in args
    assert "-l" in args and "75%" in args
    assert "-t" in args and "%9" in args  # pane id, not window id
