"""Tests for MultiReporter broadcast and log cleanup in agent-restore."""

import re
import pytest
from pathlib import Path

from tmux_agents import config, container, paths, provisioning, ssh_forward, tmux
from tmux_agents.commands import restore
from tmux_agents.ssh_forward import PumpResult


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences so assertions work regardless of color."""
    return re.sub(r"\x1b\[[^m]*m", "", text)


def _entry(
    window_id: str, project: str, host_worktree: Path, branch: str = "feat/x"
) -> restore.Entry:
    return restore.Entry(
        window_id=window_id,
        project=project,
        branch=branch,
        host_worktree=host_worktree,
        pane_id="%1",
        claude_session_id=None,
        window_index=0,
    )


def _project(name: str = "backend") -> config.Project:
    return config.Project(
        name=name,
        repo=Path("/tmp/repo"),
        exec_cmd="true",
        container="backend-c",
        up_cmd="true",
        forward_ssh_agent=True,
        user="vscode",
    )


def test_execute_plan_broadcasts_to_all_member_logs(
    monkeypatch, tmp_state_dir, tmp_path
):
    wt1 = tmp_path / "wt1"
    wt1.mkdir()
    wt2 = tmp_path / "wt2"
    wt2.mkdir()
    e1 = _entry("@1", "backend", wt1)
    e2 = _entry("@2", "backend", wt2)
    plan = [e1, e2]
    placeholders = {
        "@1": restore.Placeholder(e1, "@1", "%1"),
        "@2": restore.Placeholder(e2, "@2", "%2"),
    }

    monkeypatch.setattr(container, "current_name", lambda proj: None)
    monkeypatch.setattr(container, "ensure_up", lambda proj, up_cmd: "backend-c")
    monkeypatch.setattr(
        ssh_forward, "maybe_spawn_pump", lambda c, u: PumpResult("ready")
    )
    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "respawn_pane", lambda *a, **k: None)

    # Capture log contents before they get unlinked.
    captured: dict[str, str] = {}
    real_unlink = Path.unlink

    def capture_then_unlink(self, *args, **kwargs):
        try:
            captured[self.name] = self.read_text()
        except FileNotFoundError:
            pass
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", capture_then_unlink)

    restore.execute_plan(plan, placeholders, {"backend": _project()})

    log1 = _strip_ansi(captured.get("spawn-@1.log", ""))
    log2 = _strip_ansi(captured.get("spawn-@2.log", ""))
    for log in (log1, log2):
        assert "Restoring agent: backend / feat/x" in log
        assert "▸ container — building (this may take minutes)…" in log
        assert "✓ container" in log
        assert "✓ ssh pump" in log
        assert "✓ hooks" in log


def test_execute_plan_deletes_logs_on_success(monkeypatch, tmp_state_dir, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    e = _entry("@1", "backend", wt)
    placeholders = {"@1": restore.Placeholder(e, "@1", "%1")}

    monkeypatch.setattr(container, "current_name", lambda proj: "backend-c")
    monkeypatch.setattr(
        ssh_forward, "maybe_spawn_pump", lambda c, u: PumpResult("already_healthy")
    )
    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "respawn_pane", lambda *a, **k: None)

    restore.execute_plan([e], placeholders, {"backend": _project()})
    assert not paths.spawn_log("@1").exists()


def test_container_failure_fails_entries_and_deletes_logs(
    monkeypatch, tmp_state_dir, tmp_path
):
    wt = tmp_path / "wt"
    wt.mkdir()
    e = _entry("@1", "backend", wt)
    placeholders = {"@1": restore.Placeholder(e, "@1", "%1")}

    monkeypatch.setattr(container, "current_name", lambda proj: None)

    def boom(proj, up_cmd):
        raise container.ContainerError("daemon not running")

    monkeypatch.setattr(container, "ensure_up", boom)
    monkeypatch.setattr(provisioning, "provision_settings", lambda *a, **k: None)

    failed = []
    monkeypatch.setattr(
        restore,
        "_mark_entry_failed",
        lambda e, ph, reason: failed.append((e.window_id, reason)),
    )

    restore.execute_plan([e], placeholders, {"backend": _project()})
    assert any("daemon not running" in r for _, r in failed)
    assert not paths.spawn_log("@1").exists()


def test_open_failure_mid_loop_cleans_up_already_opened_files(
    monkeypatch, tmp_state_dir, tmp_path
):
    """If open() raises on the 2nd entry, the 1st entry's file is still closed
    and its log file is still deleted."""
    wt1 = tmp_path / "wt1"
    wt1.mkdir()
    wt2 = tmp_path / "wt2"
    wt2.mkdir()
    e1 = _entry("@1", "backend", wt1)
    e2 = _entry("@2", "backend", wt2)
    placeholders = {
        "@1": restore.Placeholder(e1, "@1", "%1"),
        "@2": restore.Placeholder(e2, "@2", "%2"),
    }

    real_open = open
    call_count = {"n": 0}

    def flaky_open(path, *args, **kwargs):
        # The first open() inside _activate_project's log-open loop wraps wt1's log.
        # The second open() wraps wt2's log — that's the one we make fail.
        if call_count["n"] == 0:
            call_count["n"] += 1
            return real_open(path, *args, **kwargs)
        if "spawn-@2.log" in str(path):
            raise OSError("disk full")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky_open)

    # The exception will propagate out of _activate_project → execute_plan's
    # ThreadPoolExecutor → f.result(). We expect it to raise.
    with pytest.raises(OSError, match="disk full"):
        restore.execute_plan([e1, e2], placeholders, {"backend": _project()})

    # Even though we crashed, the wt1 log should have been cleaned up.
    assert not paths.spawn_log("@1").exists()


def test_hooks_failure_per_entry_does_not_block_other_entries(
    monkeypatch, tmp_state_dir, tmp_path
):
    wt1 = tmp_path / "wt1"
    wt1.mkdir()
    wt2 = tmp_path / "wt2"
    wt2.mkdir()
    e1 = _entry("@1", "backend", wt1)
    e2 = _entry("@2", "backend", wt2)
    placeholders = {
        "@1": restore.Placeholder(e1, "@1", "%1"),
        "@2": restore.Placeholder(e2, "@2", "%2"),
    }
    monkeypatch.setattr(container, "current_name", lambda proj: "backend-c")
    monkeypatch.setattr(
        ssh_forward, "maybe_spawn_pump", lambda c, u: PumpResult("already_healthy")
    )

    def fail_only_wt1(worktree, *, template_path):
        if worktree == wt1:
            raise OSError("permission denied")

    monkeypatch.setattr(provisioning, "provision_settings", fail_only_wt1)

    respawn_calls = []
    monkeypatch.setattr(
        tmux, "respawn_pane", lambda pane, *, command: respawn_calls.append(pane)
    )

    restore.execute_plan([e1, e2], placeholders, {"backend": _project()})

    # Both panes should still be respawned (one with hooks warning, one clean).
    assert "%1" in respawn_calls
    assert "%2" in respawn_calls
