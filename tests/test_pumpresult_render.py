from unittest.mock import MagicMock
from tmux_agents.ssh_forward import PumpResult


def test_render_disabled_no_sock_calls_warn():
    st = MagicMock()
    PumpResult("disabled_no_sock").render(st)
    st.warn.assert_called_once_with("SSH_AUTH_SOCK not set on host (forwarding disabled)")


def test_render_already_healthy_calls_skip():
    st = MagicMock()
    PumpResult("already_healthy").render(st)
    st.skip.assert_called_once_with("already healthy")


def test_render_ready_no_kill_calls_info_starting():
    st = MagicMock()
    PumpResult("ready").render(st)
    st.info.assert_called_once_with("starting…")


def test_render_ready_with_kill_mentions_count():
    st = MagicMock()
    PumpResult("ready", killed_stale=3).render(st)
    st.info.assert_called_once_with("killed 3 stale pump(s); respawned")


def test_render_timed_out_calls_warn():
    st = MagicMock()
    PumpResult("timed_out").render(st)
    st.warn.assert_called_once_with("not ready within budget (forwarding may be flaky)")


def test_render_disabled_no_python_calls_warn():
    st = MagicMock()
    PumpResult("disabled_no_python").render(st)
    st.warn.assert_called_once_with("python3 missing in container (forwarding disabled)")
