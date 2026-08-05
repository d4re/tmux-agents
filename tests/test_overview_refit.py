"""Content-fit auto-resize of the overview pane.

`desired_pane_height` is the one sizing rule (content + footer, capped at a
quarter of the window, never below 2). `refit_self_pane` is the TUI-side
trigger: on a row-count change it publishes the desired content height as the
`@overview_rows` pane option (read by the window-resized hook's
overview-refit script) and resizes its own pane.
"""

import subprocess

from tmux_agents import overview, tmux
from tmux_agents.overview import Row, TuiState


# ---------- desired_pane_height ----------


def test_desired_height_fits_content_below_cap():
    # 5 rows + footer = 6, cap is 60//4 = 15
    assert overview.desired_pane_height(5, 60) == 6


def test_desired_height_caps_at_quarter_of_window():
    assert overview.desired_pane_height(30, 60) == 15


def test_desired_height_floors_at_two_for_tiny_windows():
    # 4-row window: quarter would be 1, which shows nothing but the footer
    assert overview.desired_pane_height(10, 4) == 2


def test_desired_height_floors_at_two_for_empty_rows():
    assert overview.desired_pane_height(0, 60) == 2


# ---------- refit_self_pane ----------


def _rows(n: int) -> list[Row]:
    return [Row(kind="agent", repo="api") for _ in range(n)]


def _state(n_rows: int) -> TuiState:
    return TuiState(
        folds={},
        rows=_rows(n_rows),
        cursor=None,
        last_active=None,
        self_pane_id="%9",
    )


def _capture_tmux(monkeypatch, *, window_height=60):
    calls = {"options": [], "resizes": []}
    monkeypatch.setattr(
        tmux,
        "set_pane_option",
        lambda pid, name, val: calls["options"].append((pid, name, val)),
    )
    monkeypatch.setattr(
        tmux,
        "resize_pane",
        lambda pid, *, height: calls["resizes"].append((pid, height)),
    )
    monkeypatch.setattr(tmux, "pane_window_height", lambda pid: window_height)
    return calls


def test_refit_publishes_rows_and_resizes(monkeypatch):
    calls = _capture_tmux(monkeypatch, window_height=60)
    s = _state(5)
    overview.refit_self_pane(s)
    assert calls["options"] == [("%9", "@overview_rows", "6")]
    assert calls["resizes"] == [("%9", 6)]


def test_refit_noops_while_row_count_unchanged(monkeypatch):
    calls = _capture_tmux(monkeypatch)
    s = _state(5)
    overview.refit_self_pane(s)
    overview.refit_self_pane(s)
    assert len(calls["options"]) == 1
    assert len(calls["resizes"]) == 1


def test_refit_fires_again_when_rows_change(monkeypatch):
    calls = _capture_tmux(monkeypatch, window_height=60)
    s = _state(5)
    overview.refit_self_pane(s)
    s.rows = _rows(8)
    overview.refit_self_pane(s)
    assert calls["options"][-1] == ("%9", "@overview_rows", "9")
    assert calls["resizes"][-1] == ("%9", 9)


def test_refit_caps_resize_at_quarter_of_window(monkeypatch):
    calls = _capture_tmux(monkeypatch, window_height=40)
    s = _state(30)
    overview.refit_self_pane(s)
    # published content height is uncapped (the hook re-caps per window
    # height), the actual resize is capped
    assert calls["options"] == [("%9", "@overview_rows", "31")]
    assert calls["resizes"] == [("%9", 10)]


def test_refit_swallows_tmux_error_and_retries_next_call(monkeypatch):
    """A transient tmux failure must neither kill the TUI loop nor be cached
    as 'already published' — the next call retries."""
    calls = _capture_tmux(monkeypatch)
    attempts = []

    def flaky(pid, name, val):
        attempts.append((pid, name, val))
        if len(attempts) == 1:
            raise subprocess.CalledProcessError(1, ["tmux"])

    monkeypatch.setattr(tmux, "set_pane_option", flaky)
    s = _state(5)
    overview.refit_self_pane(s)  # must not raise
    overview.refit_self_pane(s)
    assert len(attempts) == 2
    assert len(calls["resizes"]) == 1
