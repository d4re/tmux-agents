import pytest

from tmux_agents import overview, state, tmux
from tmux_agents.overview import Cursor


def _windows():
    return [
        tmux.Window(id="@1", index=1, name="api:feat-x"),
        tmux.Window(id="@2", index=2, name="api:bugfix-nav"),
        tmux.Window(id="@3", index=3, name="web:refactor"),
        tmux.Window(id="@4", index=4, name="infra:deploy-v2"),
    ]


def _states(tmp_state_dir):
    (tmp_state_dir / "@1.state").write_text(state.RUNNING)
    (tmp_state_dir / "@2.state").write_text(state.WAITING)
    (tmp_state_dir / "@3.state").write_text(state.IDLE)
    (tmp_state_dir / "@4.state").write_text(state.ERRORED)


def _three_repo_rows(tmp_state_dir, monkeypatch, folds=None):
    monkeypatch.setattr(tmux, "list_windows", lambda s: _windows())
    _states(tmp_state_dir)
    return overview.build_rows(folds or {})


# ---------- cursor model ----------

def test_first_cursor_is_first_header(tmp_state_dir, monkeypatch):
    rows = _three_repo_rows(tmp_state_dir, monkeypatch)
    assert overview.first_cursor(rows) == Cursor("header", "api")


def test_first_cursor_empty_rows_is_none(tmp_state_dir, monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: [])
    rows = overview.build_rows({})
    assert overview.first_cursor(rows) is None


def test_auto_track_cursor_skips_active_inside_folded_repo(tmp_state_dir, monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@1", index=1, name="api:feat-x", active=True),
        tmux.Window(id="@2", index=2, name="web:hotfix", active=False),
    ])
    (tmp_state_dir / "@1.state").write_text(state.RUNNING)
    (tmp_state_dir / "@2.state").write_text(state.IDLE)
    rows = overview.build_rows({"api": True})
    cursor, last_active = overview.auto_track_cursor(rows, None, None)
    assert cursor == Cursor("header", "api")
    assert last_active is None


def _rows_with_active(tmp_state_dir, monkeypatch, active_id):
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@1", index=1, name="api:feat-x", active=(active_id == "@1")),
        tmux.Window(id="@2", index=2, name="api:bug",    active=(active_id == "@2")),
        tmux.Window(id="@3", index=3, name="web:hotfix", active=(active_id == "@3")),
    ])
    _states(tmp_state_dir)
    return overview.build_rows({})


def test_auto_track_cursor_initial_state_lands_on_active(tmp_state_dir, monkeypatch):
    rows = _rows_with_active(tmp_state_dir, monkeypatch, "@2")
    cursor, last_active = overview.auto_track_cursor(rows, None, None)
    assert cursor == Cursor("agent", "@2")
    assert last_active == Cursor("agent", "@2")


def test_auto_track_cursor_advances_when_active_changes_and_cursor_was_tracking(tmp_state_dir, monkeypatch):
    rows = _rows_with_active(tmp_state_dir, monkeypatch, "@1")
    rows = _rows_with_active(tmp_state_dir, monkeypatch, "@3")
    cursor, last_active = overview.auto_track_cursor(rows, Cursor("agent", "@1"), Cursor("agent", "@1"))
    assert cursor == Cursor("agent", "@3")
    assert last_active == Cursor("agent", "@3")


def test_auto_track_cursor_keeps_user_moved_cursor_when_active_changes(tmp_state_dir, monkeypatch):
    rows = _rows_with_active(tmp_state_dir, monkeypatch, "@3")
    cursor, last_active = overview.auto_track_cursor(rows, Cursor("agent", "@2"), Cursor("agent", "@1"))
    assert cursor == Cursor("agent", "@2")
    assert last_active == Cursor("agent", "@3")


def test_auto_track_cursor_resumes_tracking_when_cursor_lands_on_active(tmp_state_dir, monkeypatch):
    rows = _rows_with_active(tmp_state_dir, monkeypatch, "@2")
    cursor, last_active = overview.auto_track_cursor(rows, Cursor("agent", "@2"), Cursor("agent", "@2"))
    assert cursor == Cursor("agent", "@2")
    assert last_active == Cursor("agent", "@2")
    cursor, last_active = overview.auto_track_cursor(rows, cursor, last_active)
    assert cursor == Cursor("agent", "@2")


def test_auto_track_cursor_snaps_to_first_when_no_active(tmp_state_dir, monkeypatch):
    rows = _three_repo_rows(tmp_state_dir, monkeypatch)
    cursor, last_active = overview.auto_track_cursor(rows, None, None)
    assert cursor == Cursor("header", "api")
    assert last_active is None


def test_auto_track_cursor_clamps_user_moved_cursor_when_window_disappears(tmp_state_dir, monkeypatch):
    rows = _rows_with_active(tmp_state_dir, monkeypatch, "@1")
    cursor, last_active = overview.auto_track_cursor(rows, Cursor("agent", "@99"), Cursor("agent", "@2"))
    assert cursor == Cursor("agent", "@1")
    assert last_active == Cursor("agent", "@1")


def test_move_cursor_walks_visible_rows(tmp_state_dir, monkeypatch):
    rows = _three_repo_rows(tmp_state_dir, monkeypatch)
    cur = overview.first_cursor(rows)
    cur = overview.move_cursor(rows, cur, +1)
    assert cur == Cursor("agent", "@1")
    cur = overview.move_cursor(rows, cur, +1)
    assert cur == Cursor("agent", "@2")
    cur = overview.move_cursor(rows, cur, +1)
    assert cur == Cursor("header", "web")


def test_move_cursor_clamps_at_ends(tmp_state_dir, monkeypatch):
    rows = _three_repo_rows(tmp_state_dir, monkeypatch)
    cur = overview.first_cursor(rows)
    assert overview.move_cursor(rows, cur, -1) == cur
    last = Cursor("agent", "@4")
    assert overview.move_cursor(rows, last, +1) == last


def test_move_cursor_skips_folded_repo(tmp_state_dir, monkeypatch):
    rows = _three_repo_rows(tmp_state_dir, monkeypatch, folds={"api": True})
    cur = Cursor("header", "api")
    assert overview.move_cursor(rows, cur, +1) == Cursor("header", "web")


def test_pin_cursor_keeps_existing_target(tmp_state_dir, monkeypatch):
    rows = _three_repo_rows(tmp_state_dir, monkeypatch)
    assert overview.pin_cursor(rows, Cursor("agent", "@2")) == Cursor("agent", "@2")
    assert overview.pin_cursor(rows, Cursor("header", "web")) == Cursor("header", "web")


def test_pin_cursor_snaps_to_first_remaining_agent_when_target_gone(tmp_state_dir, monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@1", index=1, name="api:feat-x"),
        tmux.Window(id="@2", index=2, name="api:bugfix-nav"),
    ])
    _states(tmp_state_dir)
    rows = overview.build_rows({})
    assert overview.pin_cursor(rows, Cursor("agent", "@99")) == Cursor("agent", "@1")


def test_pin_cursor_snaps_to_first_agent_when_repo_emptied(tmp_state_dir, monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@3", index=3, name="web:refactor"),
    ])
    (tmp_state_dir / "@3.state").write_text(state.IDLE)
    rows = overview.build_rows({})
    assert overview.pin_cursor(rows, Cursor("agent", "@1")) == Cursor("agent", "@3")


def test_pin_cursor_returns_none_when_rows_empty():
    assert overview.pin_cursor([], Cursor("agent", "@1")) is None


def test_activate_target_agent_calls_select_window(monkeypatch):
    calls = []
    monkeypatch.setattr(tmux, "select_window", lambda t: calls.append(t))
    folds: dict[str, bool] = {}
    out = overview.activate_target(Cursor("agent", "@5"), folds)
    assert calls == ["@5"]
    assert out == Cursor("agent", "@5")
    assert folds == {}


def test_activate_target_header_toggles_fold_and_persists(monkeypatch, tmp_state_dir):
    monkeypatch.setattr(tmux, "select_window", lambda t: pytest.fail("should not select"))
    folds: dict[str, bool] = {}
    overview.activate_target(Cursor("header", "api"), folds)
    assert folds == {"api": True}
    assert overview.load_folds() == {"api": True}
    overview.activate_target(Cursor("header", "api"), folds)
    assert folds == {"api": False}
    assert overview.load_folds() == {"api": False}


# ---------- curses rendering ----------

class _FakeStdscr:
    def __init__(self, height=20, width=80):
        self._h = height
        self._w = width
        self.writes: list[tuple[int, int, str, int]] = []

    def getmaxyx(self):
        return (self._h, self._w)

    def addnstr(self, y, x, text, n, attr=0):
        if y < 0 or y >= self._h:
            return
        clipped = text[: max(0, min(n, self._w - x))]
        self.writes.append((y, x, clipped, attr))

    def erase(self):
        self.writes.clear()


def test_render_curses_row_at_y_maps_visible_rows(tmp_state_dir, monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: _windows())
    _states(tmp_state_dir)
    rows = overview.build_rows({})
    scr = _FakeStdscr(height=20, width=80)
    row_at_y, text_w_at_y = overview.render_curses(scr, rows, cursor=Cursor("header", "api"))
    assert row_at_y[0] == Cursor("header", "api")
    assert row_at_y[1] == Cursor("agent", "@1")
    assert all(isinstance(w, int) and w > 0 for w in text_w_at_y[: len(rows)])


def test_render_curses_blank_rows_below_data_are_none(tmp_state_dir, monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: _windows())
    _states(tmp_state_dir)
    rows = overview.build_rows({})
    scr = _FakeStdscr(height=20, width=80)
    row_at_y, _ = overview.render_curses(scr, rows, cursor=None)
    assert row_at_y[len(rows)] is None
    assert row_at_y[19] is None


def test_render_curses_footer_hint_present_at_bottom_right(tmp_state_dir, monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: _windows())
    _states(tmp_state_dir)
    rows = overview.build_rows({})
    scr = _FakeStdscr(height=10, width=80)
    overview.render_curses(scr, rows, cursor=None)
    last_row_writes = [w for w in scr.writes if w[0] == 9]
    assert any("Ctrl-Space" in w[2] and "N new" in w[2] and "R restore" in w[2]
               for w in last_row_writes)


def test_render_curses_footer_hint_truncated_when_narrow(tmp_state_dir, monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: _windows())
    _states(tmp_state_dir)
    rows = overview.build_rows({})
    scr = _FakeStdscr(height=10, width=20)
    overview.render_curses(scr, rows, cursor=None)
    last_row_writes = [w for w in scr.writes if w[0] == 9]
    text = "".join(w[2] for w in last_row_writes)
    assert "Ctrl-Space" in text
    assert "N new" not in text


def test_render_curses_footer_hint_omitted_when_too_narrow(tmp_state_dir, monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: _windows())
    _states(tmp_state_dir)
    rows = overview.build_rows({})
    scr = _FakeStdscr(height=10, width=6)
    overview.render_curses(scr, rows, cursor=None)
    last_row_writes = [w for w in scr.writes if w[0] == 9]
    text = "".join(w[2] for w in last_row_writes)
    assert "N" not in text and "K" not in text and "R" not in text


def test_render_curses_cursor_row_marked_with_arrow(tmp_state_dir, monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: _windows())
    _states(tmp_state_dir)
    rows = overview.build_rows({})
    scr = _FakeStdscr(height=20, width=80)
    overview.render_curses(scr, rows, cursor=Cursor("agent", "@2"))
    cursor_writes = [w for w in scr.writes if w[0] == 2]
    assert any(w[2].startswith("> ") for w in cursor_writes)
    other_writes = [w for w in scr.writes if w[0] == 1]
    assert any(w[2].startswith("  ") for w in other_writes)


def test_render_curses_active_row_gets_bold_inactive_does_not(tmp_state_dir, monkeypatch):
    import curses
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@1", index=1, name="api:feat-x", active=False),
        tmux.Window(id="@2", index=2, name="api:bug", active=True),
    ])
    (tmp_state_dir / "@1.state").write_text(state.RUNNING)
    (tmp_state_dir / "@2.state").write_text(state.WAITING)
    rows = overview.build_rows({})
    scr = _FakeStdscr(height=20, width=80)
    overview.render_curses(scr, rows, cursor=None)
    active_writes = [w for w in scr.writes if w[0] == 2]
    assert any(w[3] & curses.A_BOLD for w in active_writes)
    inactive_writes = [w for w in scr.writes if w[0] == 1]
    assert all(not (w[3] & curses.A_BOLD) for w in inactive_writes)


def _windows_with_errored(n_errored):
    """n_errored agent windows in state X, plus one running window."""
    wins = [tmux.Window(id=f"@{i}", index=i, name=f"api:dead-{i}",
                        state_code=state.ERRORED) for i in range(1, n_errored + 1)]
    wins.append(tmux.Window(id="@9", index=9, name="web:alive",
                            state_code=state.RUNNING))
    return wins


def test_render_curses_footer_shows_restore_alert_when_errored(tmp_state_dir, monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: _windows_with_errored(1))
    rows = overview.build_rows({})
    scr = _FakeStdscr(height=10, width=80)
    overview.render_curses(scr, rows, cursor=None)
    text = "".join(w[2] for w in scr.writes if w[0] == 9)
    assert "1 agent down" in text
    assert "Ctrl-Space R to restore" in text  # full chord, not just the bare key
    assert "N new" not in text  # alert replaces the nav footer


def test_render_curses_restore_alert_is_right_aligned(tmp_state_dir, monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: _windows_with_errored(1))
    rows = overview.build_rows({})
    width = 80
    scr = _FakeStdscr(height=10, width=width)
    overview.render_curses(scr, rows, cursor=None)
    alert = overview._restore_alert(1)
    alert_writes = [w for w in scr.writes if w[0] == 9 and "down" in w[2]]
    assert alert_writes
    expected_x = max(0, width - len(alert) - 1)
    assert expected_x > 0
    assert all(w[1] == expected_x for w in alert_writes)


def test_render_curses_restore_alert_pluralizes(tmp_state_dir, monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: _windows_with_errored(3))
    rows = overview.build_rows({})
    scr = _FakeStdscr(height=10, width=80)
    overview.render_curses(scr, rows, cursor=None)
    text = "".join(w[2] for w in scr.writes if w[0] == 9)
    assert "3 agents down" in text


def test_render_curses_restore_alert_uses_errored_color_and_bold(tmp_state_dir, monkeypatch):
    import curses
    monkeypatch.setattr(tmux, "list_windows", lambda s: _windows_with_errored(1))
    rows = overview.build_rows({})
    scr = _FakeStdscr(height=10, width=80)
    overview.render_curses(scr, rows, cursor=None)
    alert_writes = [w for w in scr.writes if w[0] == 9 and "down" in w[2]]
    assert alert_writes and all(w[3] & curses.A_BOLD for w in alert_writes)


# ---------- pane attachment ----------

def test_attach_overview_pane_splits_and_tags(monkeypatch):
    splits: list[tuple[str, int, str]] = []
    options: list[tuple[str, str, str]] = []
    monkeypatch.setattr(tmux, "overview_pane_ids", lambda wid: [])
    monkeypatch.setattr(tmux, "split_window",
                        lambda wid, *, percent, command:
                            splits.append((wid, percent, command)) or "%42")
    monkeypatch.setattr(tmux, "set_pane_option",
                        lambda pid, name, val: options.append((pid, name, val)))
    overview.attach_overview_pane("@7")
    assert splits == [("@7", 25, "agent-overview")]
    assert options == [("%42", "@role", "overview")]


def test_attach_overview_pane_skips_when_overview_already_present(monkeypatch):
    """Idempotent: a window that already has an @role=overview pane must not
    get a second one. A layout toggle re-attaching overview to an agent-dead
    window is what wedged windows into the unrevivable two-overview state."""
    monkeypatch.setattr(tmux, "overview_pane_ids", lambda wid: ["%5"])
    monkeypatch.setattr(tmux, "split_window",
                        lambda *a, **k: pytest.fail("should not split"))
    monkeypatch.setattr(tmux, "set_pane_option",
                        lambda *a, **k: pytest.fail("should not tag"))
    overview.attach_overview_pane("@7")  # no-op, no exception


# ---------- TUI event handlers ----------

def _two_agent_state(monkeypatch, tmp_state_dir, *, active_id="@2"):
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@1", index=1, name="api:a", active=(active_id == "@1")),
        tmux.Window(id="@2", index=2, name="api:b", active=(active_id == "@2")),
    ])
    (tmp_state_dir / "@1.state").write_text(state.RUNNING)
    (tmp_state_dir / "@2.state").write_text(state.WAITING)
    return overview.make_initial_state("%99")


def test_make_initial_state_seeds_cursor_on_active(monkeypatch, tmp_state_dir):
    s = _two_agent_state(monkeypatch, tmp_state_dir, active_id="@2")
    assert s.cursor == Cursor("agent", "@2")
    assert s.last_active == Cursor("agent", "@2")
    assert s.self_pane_id == "%99"
    assert s.row_at_y == [] and s.text_w_at_y == []  # populated only after render


def test_handle_key_arrow_down_moves_cursor(monkeypatch, tmp_state_dir):
    import curses
    s = _two_agent_state(monkeypatch, tmp_state_dir, active_id="@2")
    s.cursor = Cursor("header", "api")
    overview.handle_key(s, curses.KEY_DOWN)
    assert s.cursor == Cursor("agent", "@1")


def test_handle_key_enter_on_agent_activates_and_resumes_tracking(monkeypatch, tmp_state_dir):
    s = _two_agent_state(monkeypatch, tmp_state_dir, active_id="@1")
    s.cursor = Cursor("agent", "@2")  # user moved off active
    selects: list[str] = []
    monkeypatch.setattr(tmux, "select_window", lambda t: selects.append(t))
    overview.handle_key(s, 10)  # newline = Enter
    assert selects == ["@2"]
    assert s.cursor == Cursor("agent", "@2")
    assert s.last_active == Cursor("agent", "@2")  # tracking re-engaged


def test_handle_key_enter_on_header_toggles_fold_does_not_touch_last_active(monkeypatch, tmp_state_dir):
    import curses
    s = _two_agent_state(monkeypatch, tmp_state_dir, active_id="@1")
    prior_last_active = s.last_active
    s.cursor = Cursor("header", "api")
    monkeypatch.setattr(tmux, "select_window",
                        lambda t: pytest.fail("header Enter should not switch"))
    overview.handle_key(s, curses.KEY_ENTER)
    assert s.folds == {"api": True}
    assert s.last_active == prior_last_active


def test_handle_key_n_spawns_new_agent_popup(monkeypatch, tmp_state_dir):
    s = _two_agent_state(monkeypatch, tmp_state_dir)
    captured: list[list[str]] = []
    monkeypatch.setattr(overview, "_popen", lambda argv: captured.append(argv))
    overview.handle_key(s, ord('N'))
    assert captured and captured[0][-1] == "agent-new"


def test_handle_key_r_restores_dead(monkeypatch, tmp_state_dir):
    s = _two_agent_state(monkeypatch, tmp_state_dir)
    captured: list[list[str]] = []
    monkeypatch.setattr(overview, "_popen", lambda argv: captured.append(argv))
    overview.handle_key(s, ord('R'))
    assert captured == [["agent-restore", "--background"]]


def test_handle_key_e_renames_current_agent(monkeypatch, tmp_state_dir):
    s = _two_agent_state(monkeypatch, tmp_state_dir, active_id="@2")
    s.cursor = Cursor("agent", "@2")
    captured: list[list[str]] = []
    monkeypatch.setattr(overview, "_popen", lambda argv: captured.append(argv))
    overview.handle_key(s, ord('E'))
    assert captured and captured[0][:2] == ["tmux", "-L"]
    assert "agent-rename --window-id @2 %%" in captured[0][-1]


def test_handle_mouse_blank_space_focuses_self(monkeypatch, tmp_state_dir):
    import curses
    s = _two_agent_state(monkeypatch, tmp_state_dir)
    s.row_at_y = [Cursor("header", "api"), Cursor("agent", "@1"), Cursor("agent", "@2")] + [None] * 17
    s.text_w_at_y = [20, 30, 30] + [0] * 17
    selects: list[str] = []
    monkeypatch.setattr(tmux, "select_pane", lambda p: selects.append(p))
    overview.handle_mouse(s, mx=50, my=1, bstate=curses.BUTTON1_PRESSED)  # past text width
    assert selects == ["%99"]


def test_handle_mouse_click_on_agent_switches_window_and_resumes_tracking(monkeypatch, tmp_state_dir):
    import curses
    s = _two_agent_state(monkeypatch, tmp_state_dir, active_id="@1")
    # Hit-test arrays as the renderer would have populated them.
    s.row_at_y = [Cursor("header", "api"), Cursor("agent", "@1"), Cursor("agent", "@2")] + [None] * 17
    s.text_w_at_y = [20, 30, 30] + [0] * 17
    selects: list[str] = []
    monkeypatch.setattr(tmux, "select_window", lambda t: selects.append(t))
    overview.handle_mouse(s, mx=10, my=2, bstate=curses.BUTTON1_PRESSED)
    assert selects == ["@2"]
    assert s.cursor == Cursor("agent", "@2")
    assert s.last_active == Cursor("agent", "@2")  # tracking re-engaged


def test_handle_mouse_ignores_non_button1_events(monkeypatch, tmp_state_dir):
    import curses
    s = _two_agent_state(monkeypatch, tmp_state_dir)
    s.row_at_y = [Cursor("header", "api"), Cursor("agent", "@1"), Cursor("agent", "@2")] + [None] * 17
    s.text_w_at_y = [20, 30, 30] + [0] * 17
    monkeypatch.setattr(tmux, "select_window",
                        lambda t: pytest.fail("non-BUTTON1 should be ignored"))
    monkeypatch.setattr(tmux, "select_pane",
                        lambda p: pytest.fail("non-BUTTON1 should be ignored"))
    overview.handle_mouse(s, mx=10, my=2, bstate=curses.BUTTON3_PRESSED)


def test_handle_mouse_accepts_button1_clicked_too(monkeypatch, tmp_state_dir):
    """Curses can deliver a click as either BUTTON1_PRESSED or BUTTON1_CLICKED
    depending on the negotiated mouse mode; both must dispatch."""
    import curses
    s = _two_agent_state(monkeypatch, tmp_state_dir, active_id="@1")
    s.row_at_y = [Cursor("header", "api"), Cursor("agent", "@1"), Cursor("agent", "@2")] + [None] * 17
    s.text_w_at_y = [20, 30, 30] + [0] * 17
    selects: list[str] = []
    monkeypatch.setattr(tmux, "select_window", lambda t: selects.append(t))
    overview.handle_mouse(s, mx=10, my=2, bstate=curses.BUTTON1_CLICKED)
    assert selects == ["@2"]


def test_handle_tick_advances_cursor_when_active_changes(monkeypatch, tmp_state_dir):
    s = _two_agent_state(monkeypatch, tmp_state_dir, active_id="@1")
    # Cursor is on @1 (active) and tracking. Now active flips to @2.
    monkeypatch.setattr(tmux, "list_windows", lambda s2: [
        tmux.Window(id="@1", index=1, name="api:a", active=False),
        tmux.Window(id="@2", index=2, name="api:b", active=True),
    ])
    overview.handle_tick(s)
    assert s.cursor == Cursor("agent", "@2")
    assert s.last_active == Cursor("agent", "@2")
