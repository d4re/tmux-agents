from tmux_agents.commands import rename
from tmux_agents import tmux


def _stub_windows(monkeypatch, wins, *, pinned: set[str] | None = None):
    """Stub list_windows + rename_window + the @pinned helpers.

    `pinned` is the set of window ids reported as pinned. Calls to
    `set_window_option(@id, "@pinned", "1")` add to this set, mirroring
    real tmux behavior."""
    pinned = set() if pinned is None else set(pinned)
    renamed: list[tuple[str, str]] = []
    options: list[tuple[str, str, str]] = []

    def _set_opt(window_id, name, value):
        options.append((window_id, name, value))
        if name == "@pinned" and value == "1":
            pinned.add(window_id)

    monkeypatch.setattr(tmux, "list_windows", lambda s: wins)
    monkeypatch.setattr(tmux, "rename_window", lambda t, n: renamed.append((t, n)))
    monkeypatch.setattr(tmux, "is_window_pinned", lambda wid: wid in pinned)
    monkeypatch.setattr(tmux, "set_window_option", _set_opt)
    return renamed, options, pinned


def test_rename_prefixes_repo(monkeypatch):
    renamed, opts, _ = _stub_windows(
        monkeypatch, [tmux.Window(id="@1", index=1, name="api")]
    )
    rename.main(["--window-id", "@1", "feat-x"])
    assert renamed == [("@1", "api:feat-x")]
    assert ("@1", "@pinned", "1") in opts


def test_rename_strips_existing_branch(monkeypatch):
    renamed, _, _ = _stub_windows(
        monkeypatch, [tmux.Window(id="@1", index=1, name="api:old")]
    )
    rename.main(["--window-id", "@1", "new-name"])
    assert renamed == [("@1", "api:new-name")]


def test_rename_unknown_window(monkeypatch, capsys):
    _stub_windows(monkeypatch, [])
    rc = rename.main(["--window-id", "@9", "x"])
    assert rc != 0
    assert "no window" in capsys.readouterr().err.lower()


# --- --from-hook mode ----------------------------------------------------


def test_hook_renames_repo_only_window(monkeypatch):
    renamed, opts, _ = _stub_windows(
        monkeypatch, [tmux.Window(id="@1", index=1, name="api")]
    )
    rename.main(["--window-id", "@1", "--from-hook", "reviewing auth flow"])
    assert renamed == [("@1", "api:reviewing auth flow")]
    # Hook fires must not pin — that would freeze the window on the first title.
    assert opts == []


def test_hook_updates_previously_auto_renamed_window(monkeypatch):
    # The window was already auto-renamed once (`api:old topic`); a colon
    # in the name MUST NOT count as pinning. The hook must keep tracking.
    renamed, _, _ = _stub_windows(
        monkeypatch,
        [tmux.Window(id="@1", index=1, name="api:old topic")],
    )
    rename.main(["--window-id", "@1", "--from-hook", "new topic"])
    assert renamed == [("@1", "api:new topic")]


def test_hook_skips_pinned_window(monkeypatch):
    renamed, _, _ = _stub_windows(
        monkeypatch,
        [tmux.Window(id="@1", index=1, name="api:feat-x")],
        pinned={"@1"},
    )
    rename.main(["--window-id", "@1", "--from-hook", "some topic"])
    assert renamed == []


def test_hook_skips_ctrl(monkeypatch):
    renamed, _, _ = _stub_windows(
        monkeypatch, [tmux.Window(id="@0", index=0, name="ctrl")]
    )
    rename.main(["--window-id", "@0", "--from-hook", "anything"])
    assert renamed == []


def test_hook_unknown_window_noop(monkeypatch):
    _stub_windows(monkeypatch, [])
    assert rename.main(["--window-id", "@99", "--from-hook", "x"]) == 0


def test_hook_skips_empty_title(monkeypatch):
    renamed, _, _ = _stub_windows(
        monkeypatch, [tmux.Window(id="@1", index=1, name="api")]
    )
    rename.main(["--window-id", "@1", "--from-hook", "  "])
    assert renamed == []


def test_hook_skips_missing_title_token(monkeypatch):
    # Real-world case: tmux's `#{q:pane_title}` on an empty title expands to
    # zero shell tokens, not an empty-string token — `agent-rename --window-id
    # @1 --from-hook --` with nothing after `--`. new_name must be optional.
    renamed, _, _ = _stub_windows(
        monkeypatch, [tmux.Window(id="@1", index=1, name="api")]
    )
    assert rename.main(["--window-id", "@1", "--from-hook", "--"]) == 0
    assert renamed == []


def test_explicit_rename_requires_name(monkeypatch, capsys):
    _stub_windows(monkeypatch, [tmux.Window(id="@1", index=1, name="api")])
    rc = rename.main(["--window-id", "@1"])
    assert rc != 0
    assert "name" in capsys.readouterr().err.lower()


def test_hook_handles_flag_like_title(monkeypatch):
    # Pane titles can contain flag-like tokens (e.g., "Pass -L agents flag");
    # the hook passes `--` before the title so argparse doesn't treat them
    # as options.
    renamed, _, _ = _stub_windows(
        monkeypatch, [tmux.Window(id="@5", index=5, name="tmux")]
    )
    rename.main(["--window-id", "@5", "--from-hook", "--", "Pass -L agents flag"])
    assert renamed == [("@5", "tmux:Pass -L agents flag")]
