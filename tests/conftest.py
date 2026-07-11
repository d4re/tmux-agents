import tempfile
from pathlib import Path
from types import SimpleNamespace
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _auto_isolate_paths(tmp_path, monkeypatch):
    """Default both config and state dirs to per-test tmp paths so no test can
    accidentally read or write the real `~/.config/tmux-agents/` or
    `/tmp/tmux-agents/`. Tests that need explicit dirs override via
    `tmp_config_dir` / `tmp_state_dir` (those still take precedence)."""
    monkeypatch.setenv("TMUX_AGENTS_CONFIG_DIR", str(tmp_path / "_default_config"))
    monkeypatch.setenv("TMUX_AGENTS_STATE_DIR", str(tmp_path / "_default_state"))


@pytest.fixture
def fixtures_dir():
    return FIXTURES

@pytest.fixture()
def tmp_sock_dir():
    """Temp dir under /tmp to stay within AF_UNIX's 104-byte path limit on macOS."""
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        yield Path(d)

@pytest.fixture
def tmp_state_dir(tmp_path, monkeypatch):
    d = tmp_path / "tmux-agents"
    d.mkdir()
    monkeypatch.setenv("TMUX_AGENTS_STATE_DIR", str(d))
    return d

@pytest.fixture
def tmp_config_dir(tmp_path, monkeypatch):
    d = tmp_path / "config"
    d.mkdir()
    monkeypatch.setenv("TMUX_AGENTS_CONFIG_DIR", str(d))
    return d

@pytest.fixture(autouse=True)
def _reset_theme_cache():
    from tmux_agents import theme
    theme.reset_cache()
    yield
    theme.reset_cache()


def _reset_root_logger():
    import logging
    root = logging.getLogger("tmux_agents")
    for h in list(root.handlers):
        h.close()
        root.removeHandler(h)
    root.setLevel(logging.NOTSET)
    root.propagate = True


# setup_logging() sets propagate=False on the 'tmux_agents' logger so the
# stdlib lastResort handler doesn't echo warnings to stderr (which would
# duplicate cli_error's explicit stderr write). Tests that trigger
# setup_logging() (e.g. by calling an entry-point main()) and also need
# caplog must use the `tmux_agents_caplog` fixture below — it attaches
# caplog.handler directly to the tmux_agents logger so records bypass the
# blocked propagation path.
@pytest.fixture(autouse=True)
def _reset_logging():
    _reset_root_logger()
    yield
    _reset_root_logger()


@pytest.fixture
def agent_new_env(monkeypatch, tmp_state_dir):
    """Default-stub the tmux + container plumbing exercised by `agent-new`.

    Returns a SimpleNamespace with capture lists:
      - made:     [(name, command), …] for tmux.new_window
      - splits:   [(window_id, percent, command), …] for tmux.split_window
      - selected: [window_id, …] for tmux.select_window
      - ensured:  [(project_name, up_cmd), …] for container.ensure_up

    Tests can override any single attr by calling monkeypatch.setattr after
    the fixture runs — the last assignment wins.
    """
    from tmux_agents import tmux, container, ssh_forward
    from tmux_agents.ssh_forward import PumpResult

    captured = SimpleNamespace(made=[], splits=[], selected=[], ensured=[], spawned=[], respawned=[])

    monkeypatch.setattr(tmux, "session_exists", lambda s: True)
    monkeypatch.setattr(container, "current_name", lambda proj: None)
    monkeypatch.setattr(
        container, "ensure_up",
        lambda proj, up_cmd: captured.ensured.append((proj.name, up_cmd)) or "api-devcontainer",
    )
    monkeypatch.setattr(
        ssh_forward, "maybe_spawn_pump",
        lambda c, u: PumpResult("ready"),
    )
    monkeypatch.setattr(
        tmux, "new_window",
        lambda s, *, name, command, **_: captured.made.append((name, command)) or "@5",
    )
    monkeypatch.setattr(tmux, "active_pane_id", lambda wid: "%23")
    monkeypatch.setattr(
        tmux, "respawn_pane",
        lambda pane_id, *, command: captured.respawned.append((pane_id, command)),
    )
    monkeypatch.setattr(tmux, "overview_pane_ids", lambda wid: [])
    monkeypatch.setattr(
        tmux, "split_window",
        lambda w, *, percent, command: captured.splits.append((w, percent, command)) or "%6",
    )
    monkeypatch.setattr(tmux, "set_pane_option", lambda *args: None)
    monkeypatch.setattr(tmux, "set_window_option", lambda *args: None)
    monkeypatch.setattr(tmux, "select_window", lambda t: captured.selected.append(t))
    # The provisioning worker is launched via `tmux run-shell -b` (parented by
    # the long-lived server) so it survives the popup closing. Capture that
    # command, shlex-split into an argv list so tests assert on tokens.
    import shlex
    monkeypatch.setattr(
        tmux, "run_shell_bg",
        lambda command: captured.spawned.append(shlex.split(command)),
    )
    (tmp_state_dir / "layout").write_text("split")
    return captured


@pytest.fixture
def kill_env(monkeypatch, tmp_config_dir, tmp_path, tmp_state_dir):
    """Default test bed for `agent-kill`: one api host-only project with a
    single live window @1 (`api:feat-x`).

    Returns a SimpleNamespace with:
      - repo:    Path to the project repo (already created)
      - killed:  capture list for tmux.kill_window
    """
    from tmux_agents import tmux

    repo = tmp_path / "api"
    repo.mkdir()
    (tmp_config_dir / "projects.toml").write_text(
        f'[api]\nrepo = "{repo}"\nexec_cmd = "claude"\n'
    )
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@1", index=1, name="api:feat-x"),
    ])
    killed: list[str] = []
    monkeypatch.setattr(tmux, "kill_window", lambda t: killed.append(t))
    return SimpleNamespace(repo=repo, killed=killed)


@pytest.fixture
def tmux_agents_caplog(caplog):
    """caplog variant that captures records emitted under tmux_agents.* even
    after setup_logging() has set propagate=False on the root logger."""
    import logging
    root = logging.getLogger("tmux_agents")
    root.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        root.removeHandler(caplog.handler)
