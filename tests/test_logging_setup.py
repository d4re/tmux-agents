import logging

import pytest

from tmux_agents import logging_setup


@pytest.mark.parametrize("env_value,expected", [
    ("DEBUG",   logging.DEBUG),
    ("warning", logging.WARNING),     # case-insensitive
    ("TRACE",   logging.INFO),        # unrecognized → INFO fallback
])
def test_setup_logging_respects_env_level(tmp_state_dir, monkeypatch, env_value, expected):
    monkeypatch.setenv("TMUX_AGENTS_LOG_LEVEL", env_value)
    logging_setup.setup_logging()
    assert logging.getLogger("tmux_agents").level == expected


def test_state_tick_main_sets_up_logging_only_once(tmp_state_dir, monkeypatch):
    """Each tmux status interval calls `agent-state` in a fresh process,
    but inside a test process we must verify the in-process guard works."""
    from tmux_agents.commands import state_tick
    monkeypatch.setattr("tmux_agents.tmux.session_exists", lambda *_: False)
    state_tick.main([])
    state_tick.main([])
    handlers = logging.getLogger("tmux_agents").handlers
    assert len(handlers) == 1
