from pathlib import Path
from tmux_agents import paths


def test_state_dir_honors_env_and_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX_AGENTS_STATE_DIR", str(tmp_path / "custom"))
    assert paths.state_dir() == tmp_path / "custom"
    monkeypatch.delenv("TMUX_AGENTS_STATE_DIR", raising=False)
    assert paths.state_dir() == Path("/tmp/tmux-agents")


def test_config_dir_honors_env_and_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX_AGENTS_CONFIG_DIR", str(tmp_path / "cfg"))
    assert paths.projects_toml() == tmp_path / "cfg" / "projects.toml"
    monkeypatch.delenv("TMUX_AGENTS_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", "/Users/test")
    assert paths.projects_toml() == Path(
        "/Users/test/.config/tmux-agents/projects.toml"
    )


def test_state_dir_paths(tmp_state_dir):
    assert paths.layout_file() == tmp_state_dir / "layout"
    assert paths.folds_file() == tmp_state_dir / "overview-folds.json"


def test_config_dir_paths(tmp_config_dir):
    assert paths.agents_conf() == tmp_config_dir / "agents.conf"
    assert paths.theme_toml() == tmp_config_dir / "theme.toml"
    assert paths.windows_dir() == tmp_config_dir / "windows"
    assert paths.windows_previous_dir() == tmp_config_dir / "windows.previous"
    assert paths.window_mapping_file("@5") == tmp_config_dir / "windows" / "@5.json"


def test_worktree_paths(tmp_path):
    wt = tmp_path / "w"
    assert (
        paths.worktree_state_file(wt, "23")
        == wt / ".local" / ".tmux-agents" / "state-23.json"
    )
    assert (
        paths.worktree_pending_dir(wt, "23")
        == wt / ".local" / ".tmux-agents" / "pending-23"
    )
    assert (
        paths.worktree_session_id_file(wt, "23")
        == wt / ".local" / ".tmux-agents" / "session-23.id"
    )


def test_spawn_log_path(tmp_state_dir):
    from tmux_agents import paths

    assert paths.spawn_log("@7") == tmp_state_dir / "spawn-@7.log"


def test_autouse_isolation_redirects_both_dirs_away_from_real_paths():
    """Belt-and-braces: every test (this one included) must see config/state
    pointed at a per-test tmp path, never the real `~/.config/tmux-agents/`
    or `/tmp/tmux-agents/`. A regression here means a misconfigured test
    could wipe or contaminate the developer's live setup."""
    assert paths.config_dir() != Path.home() / ".config/tmux-agents"
    assert paths.state_dir() != Path("/tmp/tmux-agents")
