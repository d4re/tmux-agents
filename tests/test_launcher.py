from pathlib import Path


from tmux_agents.commands import launcher


def _write_conf(tmp_path: Path) -> Path:
    conf = tmp_path / "agents.conf"
    conf.write_text("# stub\n")
    return conf


def test_main_attaches_to_existing_session_without_restore(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX_AGENTS_CONFIG_DIR", str(tmp_path))
    _write_conf(tmp_path)

    from tmux_agents import tmux
    monkeypatch.setattr(tmux, "session_exists", lambda s: True)

    captured = {}
    def fake_execvp(prog, argv):
        captured["argv"] = argv
    monkeypatch.setattr("os.execvp", fake_execvp)

    rc = launcher.main()
    assert rc == 0
    assert "attach" in captured["argv"]
    # No new-session in the exec; we attached to an existing one.
    assert "new-session" not in captured["argv"]


def test_main_no_snapshot_uses_legacy_new_session_path(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX_AGENTS_CONFIG_DIR", str(tmp_path))
    _write_conf(tmp_path)

    from tmux_agents import tmux
    monkeypatch.setattr(tmux, "session_exists", lambda s: False)

    captured = {}
    def fake_execvp(prog, argv):
        captured["argv"] = argv
    monkeypatch.setattr("os.execvp", fake_execvp)

    rc = launcher.main()
    assert rc == 0
    assert "new-session" in captured["argv"]
    assert "-A" in captured["argv"]
    assert "-f" in captured["argv"]
    assert str(tmp_path / "agents.conf") in captured["argv"]


def test_main_with_snapshot_and_consent_spawns_worker_then_attaches(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX_AGENTS_CONFIG_DIR", str(tmp_path))
    _write_conf(tmp_path)
    # Populate a stub snapshot (one entry).
    from tmux_agents import paths
    paths.windows_dir().mkdir(parents=True, exist_ok=True)
    paths.window_mapping_file("@1").write_text(
        '{"project":"p","branch":null,"host_worktree":"/tmp/x","pane_id":"23"}')

    from tmux_agents import tmux
    monkeypatch.setattr(tmux, "session_exists", lambda s: False)

    started = {}
    def fake_start(*, conf, session, window_name):
        started["conf"] = str(conf)
        started["session"] = session
    monkeypatch.setattr(tmux, "start_server_detached_with_session", fake_start)

    popen_calls = []
    class DummyPopen:
        def __init__(self, *args, **kwargs):
            popen_calls.append((args, kwargs))
    monkeypatch.setattr("subprocess.Popen", DummyPopen)

    # Force the prompt to consent without waiting on real stdin.
    monkeypatch.setattr(launcher, "_prompt_restore", lambda count: True)

    captured_argv = {}
    def fake_execvp(prog, argv):
        captured_argv["argv"] = argv
    monkeypatch.setattr("os.execvp", fake_execvp)

    rc = launcher.main()
    assert rc == 0
    # Snapshot moved aside.
    assert paths.windows_previous_dir().exists()
    assert not paths.windows_dir().exists() or not list(paths.windows_dir().iterdir())
    # Server started detached.
    assert started["session"] == "agents"
    # Worker spawned.
    assert any("agent-restore" in str(a) for a, _ in popen_calls)
    # Final exec was an attach.
    assert "attach" in captured_argv["argv"]


def test_main_with_snapshot_and_decline_clears_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX_AGENTS_CONFIG_DIR", str(tmp_path))
    _write_conf(tmp_path)
    from tmux_agents import paths
    paths.windows_dir().mkdir(parents=True, exist_ok=True)
    paths.window_mapping_file("@1").write_text(
        '{"project":"p","branch":null,"host_worktree":"/tmp/x","pane_id":"23"}')

    from tmux_agents import tmux
    monkeypatch.setattr(tmux, "session_exists", lambda s: False)

    popen_calls = []
    class DummyPopen:
        def __init__(self, *args, **kwargs):
            popen_calls.append((args, kwargs))
    monkeypatch.setattr("subprocess.Popen", DummyPopen)

    monkeypatch.setattr(launcher, "_prompt_restore", lambda count: False)

    captured = {}
    monkeypatch.setattr("os.execvp", lambda p, a: captured.setdefault("argv", a))

    rc = launcher.main()
    assert rc == 0
    # Snapshot removed; no worker spawned.
    assert not paths.windows_dir().exists() or not list(paths.windows_dir().iterdir())
    assert not paths.windows_previous_dir().exists()
    assert popen_calls == []
    # Falls through to the standard new-session path.
    assert "new-session" in captured["argv"]


def test_main_errors_when_conf_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TMUX_AGENTS_CONFIG_DIR", str(tmp_path))
    rc = launcher.main()
    assert rc == 1
    assert "agents.conf" in capsys.readouterr().err
