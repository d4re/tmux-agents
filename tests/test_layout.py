import pytest
from tmux_agents.commands import layout
from tmux_agents import tmux, paths


def test_layout_split_to_compact_kills_bottom_panes(monkeypatch, tmp_state_dir):
    (tmp_state_dir / "layout").write_text("split")
    monkeypatch.setattr(
        tmux,
        "list_windows",
        lambda s: [
            tmux.Window(id="@1", index=1, name="api:x"),
        ],
    )
    monkeypatch.setattr(
        tmux,
        "list_panes",
        lambda w: [
            tmux.Pane(id="%1", index=0),
            tmux.Pane(id="%2", index=1),
        ],
    )
    killed_panes = []
    monkeypatch.setattr(tmux, "kill_pane", lambda pid: killed_panes.append(pid))
    layout.main([])
    assert paths.layout_file().read_text() == "compact"
    assert killed_panes == ["%2"]  # the non-zero pane index = overview pane


def test_attach_overview_pane_splits_and_tags(monkeypatch):
    from tmux_agents import overview

    monkeypatch.setattr(tmux, "overview_pane_ids", lambda wid: [])
    splits = []
    monkeypatch.setattr(
        tmux,
        "split_window",
        lambda w, *, percent, command: splits.append((w, percent, command)) or "%9",
    )
    tagged = []
    monkeypatch.setattr(tmux, "set_pane_option", lambda *args: tagged.append(args))
    overview.attach_overview_pane("@1")
    assert splits == [("@1", 25, "agent-overview")]
    assert tagged == [("%9", "@role", "overview")]


def test_attach_overview_pane_skips_window_with_existing_overview(monkeypatch):
    from tmux_agents import overview

    monkeypatch.setattr(tmux, "overview_pane_ids", lambda wid: ["%5"])
    monkeypatch.setattr(
        tmux,
        "split_window",
        lambda *a, **kw: pytest.fail("split_window must not be called"),
    )
    overview.attach_overview_pane("@1")


def test_layout_creates_file_if_missing(monkeypatch, tmp_state_dir):
    monkeypatch.setattr(tmux, "list_windows", lambda s: [])
    layout.main([])  # defaulting from missing file = "split" → toggles to compact
    assert paths.layout_file().read_text() == "compact"


def test_layout_split_calls_attach_overview_for_each_non_ctrl(
    monkeypatch, tmp_state_dir
):
    from tmux_agents import overview, paths, tmux
    from tmux_agents.commands import layout

    paths.layout_file().write_text("compact")  # toggle goes to 'split'

    monkeypatch.setattr(
        tmux,
        "list_windows",
        lambda s: [
            tmux.Window(id="@0", index=0, name="ctrl"),
            tmux.Window(id="@1", index=1, name="api:feat-x"),
            tmux.Window(id="@2", index=2, name="web:bug"),
        ],
    )
    monkeypatch.setattr(tmux, "list_panes", lambda wid: [])

    attached: list[str] = []
    monkeypatch.setattr(
        overview, "attach_overview_pane", lambda wid: attached.append(wid)
    )

    rc = layout.main([])
    assert rc == 0
    assert attached == ["@1", "@2"]
    assert paths.layout_file().read_text() == "split"
