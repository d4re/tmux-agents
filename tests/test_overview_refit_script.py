"""config/overview-refit — the shell half of overview auto-resize, invoked by
the window-resized hook in agents.conf. Runs the real script against a stub
`tmux` on PATH that serves canned `list-panes` rows and records `resize-pane`
calls, then asserts the min(content, quarter-of-window) rule matches
`overview.desired_pane_height`."""

import os
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "config" / "overview-refit"

STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$TMUX_STUB_LOG"
case "$3" in
  list-panes) cat "$TMUX_STUB_PANES" ;;
esac
"""


def _run_script(tmp_path, panes: str) -> list[str]:
    """Run overview-refit with a stub tmux; return the resize-pane arg lines."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "tmux"
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "tmux.log"
    log.write_text("")
    panes_file = tmp_path / "panes.txt"
    panes_file.write_text(panes)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TMUX_STUB_LOG": str(log),
        "TMUX_STUB_PANES": str(panes_file),
    }
    proc = subprocess.run(
        [str(SCRIPT), "/tmp/fake-socket", "@7"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return [line for line in log.read_text().splitlines() if "resize-pane" in line]


def test_script_is_executable():
    assert SCRIPT.stat().st_mode & stat.S_IEXEC


def test_resizes_overview_pane_to_published_content_height(tmp_path):
    resizes = _run_script(tmp_path, "%1:60::\n%2:60:overview:6\n")
    assert resizes == ["-S /tmp/fake-socket resize-pane -t %2 -y 6"]


def test_caps_at_quarter_of_window_height(tmp_path):
    resizes = _run_script(tmp_path, "%2:60:overview:31\n")
    assert resizes == ["-S /tmp/fake-socket resize-pane -t %2 -y 15"]


def test_falls_back_to_quarter_when_rows_not_published_yet(tmp_path):
    resizes = _run_script(tmp_path, "%2:60:overview:\n")
    assert resizes == ["-S /tmp/fake-socket resize-pane -t %2 -y 15"]


def test_floors_at_two_on_tiny_windows(tmp_path):
    resizes = _run_script(tmp_path, "%2:7:overview:6\n")
    assert resizes == ["-S /tmp/fake-socket resize-pane -t %2 -y 2"]


def test_ignores_windows_without_overview_pane(tmp_path):
    resizes = _run_script(tmp_path, "%1:60::\n%2:60::\n")
    assert resizes == []


def test_agents_conf_wires_the_hook_to_the_script():
    """Drift guard: the hook and the script ship as a pair, and the hook must
    pass `#{q:window_id}` — `#{hook_window_id}` expands to empty for
    window-resized (verified on tmux 3.6a), silently re-targeting the
    current window instead of the resized one."""
    conf = (REPO / "agents.conf").read_text()
    hook_lines = [
        line
        for line in conf.splitlines()
        if line.startswith("set-hook -g window-resized")
    ]
    assert len(hook_lines) == 1
    assert "overview-refit" in hook_lines[0]
    assert "#{q:window_id}" in hook_lines[0]
    assert "hook_window_id" not in hook_lines[0]
