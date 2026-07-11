from pathlib import Path
from tmux_agents.commands import kill
from tmux_agents import container, pickers, tmux, windows, worktree


def _write_mapping(window_id: str, project: str, branch: str | None, host_worktree: Path):
    host_worktree.mkdir(parents=True, exist_ok=True)
    windows.write_mapping(windows.WindowMapping(
        window_id=window_id, project=project, branch=branch,
        host_worktree=host_worktree, pane_id="23",
    ))


def _stub_state(tmp_state_dir, window_id, code):
    (tmp_state_dir / f"{window_id}.state").write_text(code)


def test_kill_by_number_closes_window(monkeypatch):
    killed = []
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@1", index=1, name="api:feat-x"),
        tmux.Window(id="@2", index=2, name="web:refactor"),
    ])
    import subprocess
    from unittest.mock import MagicMock
    def fake_run(cmd, **kw):
        killed.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert kill.main(["2"]) == 0
    assert any("kill-window" in c and "@2" in c for c in killed)


def test_kill_prune_worktree(kill_env, monkeypatch):
    import subprocess
    from unittest.mock import MagicMock
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""))
    removed = []
    monkeypatch.setattr(
        worktree, "remove",
        lambda repo, branch, *, force=False: removed.append((repo, branch, force)),
    )
    kill.main(["1", "--prune-worktree"])
    assert removed == [(kill_env.repo, "feat-x", False)]


def test_kill_prune_container_project_ensures_up_and_passes_container(
    monkeypatch, tmp_config_dir, tmp_path,
):
    repo = tmp_path / "api"
    repo.mkdir()
    (tmp_config_dir / "projects.toml").write_text(
        f'[api]\nrepo = "{repo}"\ncontainer = "api-devcontainer"\n'
        f'container_workdir = "/work"\nup_cmd = "echo up"\n'
        f'exec_cmd = "claude"\n'
    )
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@1", index=1, name="api:feat-x"),
    ])
    ensured = []
    monkeypatch.setattr(
        container, "ensure_up",
        lambda proj, up_cmd: ensured.append((proj.name, up_cmd)) or "api-devcontainer",
    )
    remove_calls = []
    def fake_remove(repo_arg, branch, *, force=False, container=None, container_workdir=None, container_user=None):
        remove_calls.append((repo_arg, branch, force, container, container_workdir))
    monkeypatch.setattr(worktree, "remove", fake_remove)
    monkeypatch.setattr(tmux, "kill_window", lambda t: None)
    kill.main(["1", "--prune-worktree"])
    assert ensured == [("api", "echo up")]
    assert remove_calls == [(repo, "feat-x", False, "api-devcontainer", "/work")]


def test_kill_prune_container_down_returns_error(
    monkeypatch, tmp_config_dir, tmp_path, capsys,
):
    repo = tmp_path / "api"
    repo.mkdir()
    (tmp_config_dir / "projects.toml").write_text(
        f'[api]\nrepo = "{repo}"\ncontainer = "api-devcontainer"\n'
        f'container_workdir = "/work"\nexec_cmd = "claude"\n'
    )
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@1", index=1, name="api:feat-x"),
    ])
    def boom(*a, **k):
        raise container.ContainerError("no container running and no up_cmd")
    monkeypatch.setattr(container, "ensure_up", boom)
    removed = []
    monkeypatch.setattr(
        worktree, "remove",
        lambda *a, **k: removed.append(1),
    )
    killed = []
    monkeypatch.setattr(tmux, "kill_window", lambda t: killed.append(t))
    rc = kill.main(["1", "--prune-worktree"])
    assert rc == 4
    assert removed == []
    assert killed == []
    assert "no container running" in capsys.readouterr().err


def test_kill_unknown_number_errors(monkeypatch, capsys):
    monkeypatch.setattr(tmux, "list_windows", lambda s: [])
    rc = kill.main(["7"])
    assert rc != 0
    assert "no window" in capsys.readouterr().err.lower()


def test_kill_force_requires_prune(kill_env, capsys):
    rc = kill.main(["1", "--force"])
    assert rc == 2
    assert "--force requires --prune-worktree" in capsys.readouterr().err
    assert kill_env.killed == []


def test_kill_dirty_worktree_without_force_preserves_window(kill_env, monkeypatch, capsys):
    def boom(repo_arg, branch, *, force=False):
        raise worktree.DirtyWorktreeError(
            "fatal: 'api/.worktrees/feat-x' contains modified or untracked files"
        )
    monkeypatch.setattr(worktree, "remove", boom)
    rc = kill.main(["1", "--prune-worktree"])
    assert rc == 3
    assert kill_env.killed == []
    err = capsys.readouterr().err
    assert "uncommitted changes" in err
    assert "re-run with --force" in err


def test_kill_dirty_worktree_with_force_succeeds(kill_env, monkeypatch):
    remove_calls = []
    def fake_remove(repo_arg, branch, *, force=False):
        remove_calls.append((repo_arg, branch, force))
    monkeypatch.setattr(worktree, "remove", fake_remove)
    rc = kill.main(["1", "--prune-worktree", "--force"])
    assert rc == 0
    assert remove_calls == [(kill_env.repo, "feat-x", True)]
    assert kill_env.killed == ["@1"]


def test_kill_generic_worktree_error_preserves_window(kill_env, monkeypatch, capsys):
    def boom(repo_arg, branch, *, force=False):
        raise worktree.WorktreeError("fatal: could not lock index")
    monkeypatch.setattr(worktree, "remove", boom)
    rc = kill.main(["1", "--prune-worktree"])
    assert rc == 3
    assert kill_env.killed == []
    err = capsys.readouterr().err
    assert "could not lock index" in err
    assert "--force" not in err


def test_kill_interactive_picks_window_and_kills_without_prune(
    monkeypatch, tmp_state_dir,
):
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@9", index=1, name="ctrl"),  # ctrl may be at any index
        tmux.Window(id="@0", index=0, name="orphan"),
        tmux.Window(id="@1", index=2, name="api:feat-x"),
        tmux.Window(id="@2", index=3, name="web:refactor"),
    ])
    _stub_state(tmp_state_dir, "@0", "I")
    _stub_state(tmp_state_dir, "@1", "W")
    _stub_state(tmp_state_dir, "@2", "R")
    picked_items = {}
    def fake_pick(items, *, prompt, **_):
        picked_items["items"] = list(items)
        return picked_items["items"][-1]  # pick "3\tweb:refactor\tR"
    monkeypatch.setattr(pickers, "pick_one", fake_pick)
    monkeypatch.setattr(pickers, "prompt_yes_no", lambda prompt, *, default: False)
    killed = []
    monkeypatch.setattr(tmux, "kill_window", lambda t: killed.append(t))
    rc = kill.main([])
    assert rc == 0
    # ctrl (by name) filtered out regardless of its index
    assert all("ctrl" not in line for line in picked_items["items"])
    # non-ctrl window at index 0 is still included
    assert any(line.startswith("0\t") for line in picked_items["items"])
    assert killed == ["@2"]


def test_kill_interactive_prune_yes_clean_kills(kill_env, monkeypatch, tmp_state_dir):
    _write_mapping("@1", "api", "feat-x", kill_env.repo / ".worktrees" / "feat-x")
    _stub_state(tmp_state_dir, "@1", "I")
    monkeypatch.setattr(pickers, "pick_one", lambda items, *, prompt, **_: items[0])
    yes_no_calls = []
    def fake_yn(prompt, *, default):
        yes_no_calls.append((prompt, default))
        return True
    monkeypatch.setattr(pickers, "prompt_yes_no", fake_yn)
    remove_calls = []
    monkeypatch.setattr(
        worktree, "remove",
        lambda r, b, *, force=False: remove_calls.append((r, b, force)),
    )
    rc = kill.main([])
    assert rc == 0
    assert yes_no_calls == [("prune worktree for api:feat-x? > ", True)]
    assert remove_calls == [(kill_env.repo, "feat-x", False)]
    assert kill_env.killed == ["@1"]


def test_kill_interactive_dirty_force_yes(kill_env, monkeypatch, tmp_state_dir):
    _write_mapping("@1", "api", "feat-x", kill_env.repo / ".worktrees" / "feat-x")
    _stub_state(tmp_state_dir, "@1", "I")
    monkeypatch.setattr(pickers, "pick_one", lambda items, *, prompt, **_: items[0])
    yn_prompts = []
    yes_no_results = iter([True, True])
    def fake_yn(prompt, *, default):
        yn_prompts.append(prompt)
        return next(yes_no_results)
    monkeypatch.setattr(pickers, "prompt_yes_no", fake_yn)
    calls = []
    def fake_remove(r, b, *, force=False):
        calls.append(force)
        if not force:
            raise worktree.DirtyWorktreeError("contains modified or untracked files")
    monkeypatch.setattr(worktree, "remove", fake_remove)
    rc = kill.main([])
    assert rc == 0
    assert calls == [False, True]
    assert kill_env.killed == ["@1"]
    assert all("api:feat-x" in p for p in yn_prompts)


def test_kill_interactive_dirty_force_no_preserves_window(kill_env, monkeypatch, tmp_state_dir):
    _write_mapping("@1", "api", "feat-x", kill_env.repo / ".worktrees" / "feat-x")
    _stub_state(tmp_state_dir, "@1", "I")
    monkeypatch.setattr(pickers, "pick_one", lambda items, *, prompt, **_: items[0])
    yes_no_prompts = iter([True, False])
    monkeypatch.setattr(
        pickers, "prompt_yes_no",
        lambda prompt, *, default: next(yes_no_prompts),
    )
    calls = []
    def fake_remove(r, b, *, force=False):
        calls.append(force)
        if not force:
            raise worktree.DirtyWorktreeError("contains modified or untracked files")
    monkeypatch.setattr(worktree, "remove", fake_remove)
    rc = kill.main([])
    assert rc == 0
    assert calls == [False]
    assert kill_env.killed == []


def test_kill_interactive_cancel_at_picker_is_noop(kill_env, monkeypatch, tmp_state_dir):
    _stub_state(tmp_state_dir, "@1", "I")
    monkeypatch.setattr(pickers, "pick_one", lambda items, *, prompt, **_: None)
    rc = kill.main([])
    assert rc == 0
    assert kill_env.killed == []


def test_kill_interactive_cancel_at_prune_prompt_is_noop(kill_env, monkeypatch, tmp_state_dir):
    _write_mapping("@1", "api", "feat-x", kill_env.repo / ".worktrees" / "feat-x")
    _stub_state(tmp_state_dir, "@1", "I")
    monkeypatch.setattr(pickers, "pick_one", lambda items, *, prompt, **_: items[0])
    def cancel(prompt, *, default):
        raise pickers.Cancelled
    monkeypatch.setattr(pickers, "prompt_yes_no", cancel)
    rc = kill.main([])
    assert rc == 0
    assert kill_env.killed == []


def test_kill_interactive_branchless_window_skips_prune_prompt(
    monkeypatch, tmp_config_dir, tmp_path, tmp_state_dir,
):
    """Branchless agent windows get auto-renamed `<repo>:<pane title>` by the
    `pane-title-changed` hook, so the name has `:` even though there's no
    worktree. The windows mapping (branch=None) is what we actually trust."""
    repo = tmp_path / "tmux"
    repo.mkdir()
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@1", index=1, name="tmux:editing-readme"),
    ])
    _write_mapping("@1", "tmux", None, repo)
    _stub_state(tmp_state_dir, "@1", "I")
    monkeypatch.setattr(pickers, "pick_one", lambda items, *, prompt, **_: items[0])
    yn_called = []
    monkeypatch.setattr(
        pickers, "prompt_yes_no",
        lambda prompt, *, default: yn_called.append(1) or False,
    )
    killed = []
    monkeypatch.setattr(tmux, "kill_window", lambda t: killed.append(t))
    rc = kill.main([])
    assert rc == 0
    assert yn_called == []
    assert killed == ["@1"]


def test_kill_interactive_missing_mapping_skips_prune_prompt(monkeypatch, tmp_state_dir):
    """Manually-created windows have no mapping; treat as no-worktree."""
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@1", index=1, name="scratch:notes"),
    ])
    _stub_state(tmp_state_dir, "@1", "I")
    monkeypatch.setattr(pickers, "pick_one", lambda items, *, prompt, **_: items[0])
    yn_called = []
    monkeypatch.setattr(
        pickers, "prompt_yes_no",
        lambda prompt, *, default: yn_called.append(1) or False,
    )
    killed = []
    monkeypatch.setattr(tmux, "kill_window", lambda t: killed.append(t))
    rc = kill.main([])
    assert rc == 0
    assert yn_called == []
    assert killed == ["@1"]


def test_kill_interactive_empty_window_list_exits_cleanly(
    monkeypatch, tmp_state_dir, capsys,
):
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@9", index=1, name="ctrl"),
    ])
    called = []
    monkeypatch.setattr(
        pickers, "pick_one",
        lambda items, *, prompt, **_: called.append(1) or None,
    )
    killed = []
    monkeypatch.setattr(tmux, "kill_window", lambda t: killed.append(t))
    rc = kill.main([])
    assert rc == 0
    assert called == []
    assert killed == []
    assert "no agent windows" in capsys.readouterr().err.lower()


def test_kill_interactive_missing_state_file_shows_question_mark(
    monkeypatch, tmp_state_dir,
):
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@1", index=1, name="api:feat-x"),
    ])
    captured = {}
    def fake_pick(items, *, prompt, **_):
        captured["items"] = list(items)
        return None
    monkeypatch.setattr(pickers, "pick_one", fake_pick)
    monkeypatch.setattr(tmux, "kill_window", lambda t: None)
    kill.main([])
    assert captured["items"] == ["1\tapi:feat-x\t?"]


def test_kill_with_window_id_skips_picker_and_kills_directly(monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@5", index=2, name="api:feat-x"),
        tmux.Window(id="@9", index=3, name="web:hotfix"),
    ])
    killed = []
    monkeypatch.setattr(tmux, "kill_window", lambda t: killed.append(t))

    # Pick-one should never run with --window-id (no fzf path).
    def _no_picker(*a, **k):
        raise AssertionError("picker should not run with --window-id")
    monkeypatch.setattr("tmux_agents.pickers.pick_one", _no_picker)
    # No mapping for @5, so the prune-prompt is skipped and kill proceeds.
    def _no_prompt(*a, **k):
        raise AssertionError("prune prompt should not run without a worktree mapping")
    monkeypatch.setattr("tmux_agents.pickers.prompt_yes_no", _no_prompt)

    rc = kill.main(["--window-id", "@5"])
    assert rc == 0
    assert killed == ["@5"]


def test_kill_interactive_prune_prints_progress(kill_env, monkeypatch, tmp_state_dir, capsys):
    """The popup is blank after the fzf prune prompt; the slow worktree
    removal must announce itself so the wait doesn't look like a hang."""
    _write_mapping("@1", "api", "feat-x", kill_env.repo / ".worktrees" / "feat-x")
    _stub_state(tmp_state_dir, "@1", "I")
    monkeypatch.setattr(pickers, "pick_one", lambda items, *, prompt, **_: items[0])
    monkeypatch.setattr(pickers, "prompt_yes_no", lambda prompt, *, default: True)
    monkeypatch.setattr(worktree, "remove", lambda r, b, *, force=False: None)
    rc = kill.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "removing worktree feat-x" in out
    assert "worktree removed" in out


def test_kill_prune_container_prints_container_check(
    monkeypatch, tmp_config_dir, tmp_path, capsys,
):
    repo = tmp_path / "api"
    repo.mkdir()
    (tmp_config_dir / "projects.toml").write_text(
        f'[api]\nrepo = "{repo}"\ncontainer = "api-devcontainer"\n'
        f'container_workdir = "/work"\nup_cmd = "echo up"\n'
        f'exec_cmd = "claude"\n'
    )
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@1", index=1, name="api:feat-x"),
    ])
    monkeypatch.setattr(container, "ensure_up", lambda proj, up_cmd: "api-devcontainer")
    monkeypatch.setattr(worktree, "remove", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "kill_window", lambda t: None)
    kill.main(["1", "--prune-worktree"])
    assert "checking container for api" in capsys.readouterr().out


def test_kill_with_unknown_window_id_returns_error(monkeypatch):
    monkeypatch.setattr(tmux, "list_windows", lambda s: [])
    rc = kill.main(["--window-id", "@99"])
    assert rc == 2
