import json
import subprocess
from pathlib import Path

SCRIPT = Path("src/tmux_agents/hooks/codex-hook.sh")


def run_hook(action, payload, cwd, env_extra=None, pane="12"):
    env = {"PATH": "/usr/bin:/bin", "TMUX_PANE": f"%{pane}", "TMUX_AGENTS_AGENT": "1"}
    env.update(env_extra or {})
    return subprocess.run(
        ["sh", str(SCRIPT.resolve()), action],
        input=json.dumps(payload),
        text=True,
        cwd=cwd,
        env=env,
        capture_output=True,
    )


def setup_worktree(tmp_path):
    (tmp_path / ".local" / ".tmux-agents").mkdir(parents=True)
    return tmp_path / ".local" / ".tmux-agents"


def test_noop_without_marker(tmp_path):
    d = setup_worktree(tmp_path)
    r = run_hook(
        "running", {"session_id": "s1"}, tmp_path, env_extra={"TMUX_AGENTS_AGENT": ""}
    )
    assert r.returncode == 0
    assert not (d / "state-12.json").exists()


def test_noop_outside_worktree(tmp_path):
    r = run_hook("running", {"session_id": "s1"}, tmp_path)
    assert r.returncode == 0
    assert not (tmp_path / ".local").exists()


def test_init_startup_pins_and_writes_idle(tmp_path):
    d = setup_worktree(tmp_path)
    r = run_hook("init", {"session_id": "s1", "source": "startup"}, tmp_path)
    assert r.returncode == 0
    assert (d / "session-12.id").read_text().strip() == "s1"
    assert json.loads((d / "state-12.json").read_text())["phase"] == "idle"


def test_init_compact_pins_without_phase(tmp_path):
    d = setup_worktree(tmp_path)
    run_hook("init", {"session_id": "s1", "source": "compact"}, tmp_path)
    assert (d / "session-12.id").read_text().strip() == "s1"
    assert not (d / "state-12.json").exists()


def test_init_unknown_source_treated_as_compact(tmp_path):
    d = setup_worktree(tmp_path)
    run_hook("init", {"session_id": "s1", "source": "mystery"}, tmp_path)
    assert not (d / "state-12.json").exists()


def test_init_startup_does_not_hijack_existing_pin(tmp_path):
    d = setup_worktree(tmp_path)
    (d / "session-12.id").write_text("root\n")
    run_hook("init", {"session_id": "nested", "source": "startup"}, tmp_path)
    assert (d / "session-12.id").read_text().strip() == "root"
    assert not (d / "state-12.json").exists()


def test_init_new_repins(tmp_path):
    d = setup_worktree(tmp_path)
    (d / "session-12.id").write_text("old\n")
    run_hook("init", {"session_id": "fresh", "source": "new"}, tmp_path)
    assert (d / "session-12.id").read_text().strip() == "fresh"


def test_phase_mismatched_session_dropped(tmp_path):
    d = setup_worktree(tmp_path)
    (d / "session-12.id").write_text("root\n")
    run_hook("idle", {"session_id": "child"}, tmp_path)
    assert not (d / "state-12.json").exists()


def test_phase_matching_session_writes(tmp_path):
    d = setup_worktree(tmp_path)
    (d / "session-12.id").write_text("root\n")
    run_hook("running", {"session_id": "root"}, tmp_path)
    assert json.loads((d / "state-12.json").read_text())["phase"] == "running"


def test_phase_absent_pin_captures_and_writes(tmp_path):
    """Absent-pin capture is ENABLED (release-gate decision, observed in
    the field): the first attributable event adopts its session id as the
    pin and writes phase, so tracking starts mid-session after a
    trust-skipped SessionStart."""
    d = setup_worktree(tmp_path)
    r = run_hook("running", {"session_id": "s1"}, tmp_path)
    assert r.returncode == 0
    assert (d / "session-12.id").read_text().strip() == "s1"
    assert json.loads((d / "state-12.json").read_text())["phase"] == "running"


def test_phase_absent_pin_and_absent_sid_writes_nothing(tmp_path):
    """No pin AND no session id in the payload: unattributable — write
    nothing rather than track blind."""
    d = setup_worktree(tmp_path)
    r = run_hook("running", {}, tmp_path)
    assert r.returncode == 0
    assert not (d / "state-12.json").exists()
    assert not (d / "session-12.id").exists()


def test_waiting_absent_pin_captures_and_rings(tmp_path):
    d = setup_worktree(tmp_path)
    r = run_hook("waiting", {"session_id": "s1"}, tmp_path)
    assert "\a" in r.stdout
    assert json.loads((d / "state-12.json").read_text())["phase"] == "waiting"


def test_waiting_emits_bell(tmp_path):
    d = setup_worktree(tmp_path)
    (d / "session-12.id").write_text("s1\n")  # pre-pinned
    r = run_hook("waiting", {"session_id": "s1"}, tmp_path)
    assert "\a" in r.stdout


def test_script_compiles():
    subprocess.run(["sh", "-n", str(SCRIPT)], check=True)
