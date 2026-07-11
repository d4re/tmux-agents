from tmux_agents import overview, state, tmux


def _windows():
    return [
        tmux.Window(id="@1", index=1, name="api:feat-x", state_code=state.RUNNING),
        tmux.Window(id="@2", index=2, name="api:bugfix-nav", state_code=state.WAITING),
        tmux.Window(id="@3", index=3, name="web:refactor", state_code=state.IDLE),
        tmux.Window(id="@4", index=4, name="infra:deploy-v2", state_code=state.ERRORED),
    ]


def _render_plain(rows):
    """Plain-text dump of rows mirroring how the TUI lays them out."""
    return "\n".join(
        overview.format_header(r) if r.kind == "header"
        else overview.format_line_plain(r.win, r.code, r.overlay_count)
        for r in rows
    )


def test_group_by_repo():
    groups = overview.group_by_repo(_windows())
    assert list(groups.keys()) == ["api", "web", "infra"]
    assert [w.name for w in groups["api"]] == ["api:feat-x", "api:bugfix-nav"]


def test_rows_render_groups_and_labels(monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: _windows())
    out = _render_plain(overview.build_rows({}))
    assert "api" in out
    assert "1:api:feat-x" in out and "running" in out
    assert "2:api:bugfix-nav" in out and "waiting" in out
    assert "3:web:refactor" in out and "idle" in out
    assert "4:infra:deploy-v2" in out and "errored" in out
    monkeypatch.setattr(tmux, "list_windows",
                        lambda s: _windows() + [tmux.Window(id="@0", index=0, name="ctrl")])
    assert "ctrl" not in _render_plain(overview.build_rows({}))


def test_summary_counts(monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: _windows())
    counts = overview._summary_counts()
    assert counts[state.RUNNING] == 1
    assert counts[state.WAITING] == 1
    assert counts[state.IDLE] == 1
    assert counts[state.ERRORED] == 1


def test_render_summary_emits_tmux_format_with_hex_colors(monkeypatch, tmp_config_dir):
    monkeypatch.setattr(tmux, "list_windows", lambda s: _windows())
    out = overview.render_summary()
    assert "#[fg=#87af5f]R#[default]" in out
    assert "#[fg=#ffd75f]W#[default]" in out
    assert "\x1b[" not in out  # no ANSI; tmux format only


def _active_windows():
    return [
        tmux.Window(id="@1", index=1, name="api:feat-x", active=False, state_code=state.RUNNING),
        tmux.Window(id="@2", index=2, name="api:bugfix-nav", active=True, state_code=state.WAITING),
        tmux.Window(id="@3", index=3, name="web:refactor", active=False, state_code=state.IDLE),
    ]


def test_active_row_marked_with_arrow_prefix(monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: _active_windows())
    out = _render_plain(overview.build_rows({}))
    lines = out.splitlines()
    active_line = next(ln for ln in lines if "2:api:bugfix-nav" in ln)
    inactive_line = next(ln for ln in lines if "1:api:feat-x" in ln)
    assert active_line.startswith("> ")
    assert inactive_line.startswith("  ")
    assert "\x1b[" not in out


def test_unknown_state_code_rendered_as_idle(monkeypatch):
    monkeypatch.setattr(tmux, "list_windows",
                        lambda s: [tmux.Window(id="@1", index=1, name="api:x", state_code="Q")])
    out = _render_plain(overview.build_rows({}))
    assert "idle" in out  # unknown treated as idle (blue) to fail safe


def test_sleeping_count_renders_with_dot_suffix(monkeypatch):
    monkeypatch.setattr(tmux, "list_windows",
                        lambda s: [tmux.Window(id="@1", index=1, name="api:feat-x",
                                               state_code=f"{state.SLEEPING}2")])
    out = _render_plain(overview.build_rows({}))
    assert "sleeping·2" in out


def test_background_count_renders_with_dot_suffix(monkeypatch):
    monkeypatch.setattr(tmux, "list_windows",
                        lambda s: [tmux.Window(id="@1", index=1, name="api:feat-x",
                                               state_code=f"{state.BACKGROUND}3")])
    out = _render_plain(overview.build_rows({}))
    assert "background·3" in out


def test_summary_counts_aggregate_sleeping_regardless_of_suffix(monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@1", index=1, name="a", state_code=f"{state.SLEEPING}3"),
        tmux.Window(id="@2", index=2, name="b", state_code=state.SLEEPING),
    ])
    counts = overview._summary_counts()
    assert counts[state.SLEEPING] == 2


def test_build_rows_emits_header_then_agents(monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: _windows())
    rows = overview.build_rows({})
    assert rows[0].kind == "header" and rows[0].repo == "api" and rows[0].count == 2
    assert rows[1].kind == "agent" and rows[1].win.id == "@1"
    assert rows[2].kind == "agent" and rows[2].win.id == "@2"
    assert rows[3].kind == "header" and rows[3].repo == "web"
    assert rows[4].kind == "agent" and rows[4].win.id == "@3"
    assert rows[5].kind == "header" and rows[5].repo == "infra"
    assert rows[6].kind == "agent" and rows[6].win.id == "@4"


def test_build_rows_skips_ctrl(monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s:
        _windows() + [tmux.Window(id="@0", index=0, name="ctrl")])
    repos = [r.repo for r in overview.build_rows({}) if r.kind == "header"]
    assert "ctrl" not in repos


def test_build_rows_folds_omit_agents(monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: _windows())
    rows = overview.build_rows({"api": True})
    api_header = next(r for r in rows if r.kind == "header" and r.repo == "api")
    assert api_header.folded is True
    assert api_header.count == 2  # count still reflects total agents
    api_agents = [r for r in rows if r.kind == "agent" and r.repo == "api"]
    assert api_agents == []
    web_agents = [r for r in rows if r.kind == "agent" and r.repo == "web"]
    assert len(web_agents) == 1  # other repos unchanged


def test_build_rows_carries_state_code_and_overlay_count(monkeypatch):
    monkeypatch.setattr(tmux, "list_windows",
                        lambda s: [tmux.Window(id="@1", index=1, name="api:x",
                                               state_code=f"{state.SLEEPING}3")])
    rows = overview.build_rows({})
    agent = next(r for r in rows if r.kind == "agent")
    assert agent.code == state.SLEEPING
    assert agent.overlay_count == 3


def test_folded_header_uses_right_arrow_glyph(monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: _windows())
    rows = overview.build_rows({"api": True})
    out = _render_plain(rows)
    assert "▸ api" in out
    assert "▾ api" not in out
    assert "1:api:feat-x" not in out  # folded agents not rendered


def test_summary_appends_starting_count_when_present(monkeypatch, tmp_config_dir):
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@1", index=1, name="api", state_code="R"),
        tmux.Window(id="@2", index=2, name="api:feat", state_code="S"),
        tmux.Window(id="@3", index=3, name="api:other", state_code="S"),
    ])
    out = overview.render_summary()
    assert "starting" in out  # label appears
    assert ": 2" in out       # "starting: 2" — sep is ": " not " "
