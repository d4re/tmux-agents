import pytest

from tmux_agents import overview
from tmux_agents.overview import Cursor


_POPUP_PREFIX = ["tmux", "-L", "agents", "display-popup", "-E"]

# (action, expected_argv) — action is a thunk that calls overview.*
ACTIONS = [
    (
        "spawn_new_agent",
        lambda: overview.spawn_new_agent(),
        _POPUP_PREFIX + ["-w", "60%", "-h", "60%", "agent-new"],
    ),
    (
        "kill_at_agent",
        lambda: overview.kill_at(Cursor("agent", "@5")),
        _POPUP_PREFIX + ["-w", "50%", "-h", "20%", "agent-kill --window-id @5"],
    ),
    (
        "rename_at_agent",
        lambda: overview.rename_at(Cursor("agent", "@5")),
        [
            "tmux",
            "-L",
            "agents",
            "command-prompt",
            "-p",
            "new branch name:",
            "run-shell 'agent-rename --window-id @5 %%'",
        ],
    ),
    (
        "restore_dead",
        lambda: overview.restore_dead(),
        ["agent-restore", "--background"],
    ),
]


@pytest.mark.parametrize("name,action,expected", ACTIONS, ids=[a[0] for a in ACTIONS])
def test_action_invokes_tmux_argv(monkeypatch, name, action, expected):
    captured = []
    monkeypatch.setattr(overview, "_popen", lambda argv: captured.append(argv))
    action()
    assert captured == [expected]


@pytest.mark.parametrize(
    "action",
    [
        lambda: overview.kill_at(Cursor("header", "api")),
        lambda: overview.rename_at(Cursor("header", "api")),
    ],
    ids=["kill_at_header", "rename_at_header"],
)
def test_action_on_header_is_noop(monkeypatch, action):
    captured = []
    monkeypatch.setattr(overview, "_popen", lambda argv: captured.append(argv))
    action()
    assert captured == []
