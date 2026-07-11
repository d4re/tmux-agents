import subprocess
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from tmux_agents import container
from tmux_agents.config import Project

def _stub_run(monkeypatch, responses):
    it = iter(responses)
    calls = []
    def fake_run(cmd, capture_output=False, text=False, check=False, shell=False, input=None):
        calls.append((cmd, shell))
        rc, out = next(it)
        return MagicMock(returncode=rc, stdout=out, stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls

@pytest.mark.parametrize("response,expected", [
    ((0, "true\n"),  True),    # running
    ((0, "false\n"), False),   # exists but stopped
    ((1, ""),        False),   # missing
])
def test_is_running(monkeypatch, response, expected):
    _stub_run(monkeypatch, [response])
    assert container.is_running("api-devcontainer") is expected

def test_ensure_up_skips_when_running(monkeypatch):
    calls = _stub_run(monkeypatch, [(0, "true\n")])
    name = container.ensure_up(_proj(container="api-devcontainer"), up_cmd="echo no")
    assert name == "api-devcontainer"
    assert len(calls) == 1  # only the is_running check


def test_ensure_up_does_not_print_header(monkeypatch, capsys):
    _stub_run(monkeypatch, [(0, "false\n"), (0, ""), (0, "true\n")])
    container.ensure_up(_proj(container="api-devcontainer"), up_cmd="devcontainer up")
    err = capsys.readouterr().err
    assert "starting container" not in err


def test_ensure_up_silent_when_already_running(monkeypatch, capsys):
    _stub_run(monkeypatch, [(0, "true\n")])
    container.ensure_up(_proj(container="api-devcontainer"), up_cmd="echo no")
    assert "starting container" not in capsys.readouterr().err


def test_ensure_up_does_not_capture_up_cmd_output(monkeypatch):
    seen = []
    def fake_run(cmd, capture_output=False, text=False, check=False, shell=False, input=None):
        seen.append({"cmd": cmd, "capture_output": capture_output, "shell": shell})
        if isinstance(cmd, list):
            return MagicMock(returncode=0, stdout="false\n" if seen.__len__() == 1 else "true\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    container.ensure_up(_proj(container="api-devcontainer"), up_cmd="devcontainer up")
    up_call = next(c for c in seen if c["cmd"] == "devcontainer up" and c["shell"])
    assert up_call["capture_output"] is False

def test_ensure_up_runs_up_cmd_when_down(monkeypatch):
    calls = _stub_run(monkeypatch, [(0, "false\n"), (0, ""), (0, "true\n")])
    name = container.ensure_up(
        _proj(container="api-devcontainer"),
        up_cmd="devcontainer up --workspace-folder /x",
    )
    assert name == "api-devcontainer"
    assert calls[1] == ("devcontainer up --workspace-folder /x", True)

def test_ensure_up_no_cmd_raises(monkeypatch):
    _stub_run(monkeypatch, [(0, "false\n")])
    with pytest.raises(container.ContainerError, match="no container for"):
        container.ensure_up(_proj(container="missing"), up_cmd=None)

def test_ensure_up_cmd_failure_raises(monkeypatch):
    _stub_run(monkeypatch, [(0, "false\n"), (1, "error: cannot start")])
    with pytest.raises(container.ContainerError):
        container.ensure_up(_proj(container="api-devcontainer"), up_cmd="devcontainer up")

def test_ensure_up_label_resolves_after_up(monkeypatch):
    calls = _stub_run(monkeypatch, [(0, ""), (0, "ok"), (0, "brave_benz\n")])
    name = container.ensure_up(
        _proj(devcontainer=True, repo="/Users/me/dev/webapp"),
        up_cmd="devcontainer up --workspace-folder /Users/me/dev/webapp",
    )
    assert name == "brave_benz"
    assert calls[0][0][:2] == ["docker", "ps"]
    assert calls[1] == ("devcontainer up --workspace-folder /Users/me/dev/webapp", True)
    assert calls[2][0][:2] == ["docker", "ps"]

def test_ensure_up_label_still_missing_after_up_raises(monkeypatch):
    _stub_run(monkeypatch, [(0, ""), (0, "ok"), (0, "")])
    with pytest.raises(container.ContainerError, match="up_cmd ran but no container"):
        container.ensure_up(
            _proj(devcontainer=True),
            up_cmd="devcontainer up --workspace-folder /x",
        )

def _proj(*, container=None, devcontainer=False, repo="/Users/me/dev/webapp"):
    return Project(
        name="webapp",
        repo=Path(repo),
        exec_cmd="docker exec -it {container} bash",
        container=container,
        devcontainer=devcontainer,
    )

@pytest.mark.parametrize("proj_kwargs,response,expected", [
    # Literal container, running:
    ({"container": "api-devcontainer"},     (0, "true\n"),           "api-devcontainer"),
    # Literal container, stopped:
    ({"container": "api-devcontainer"},     (0, "false\n"),          None),
    # Devcontainer label match:
    ({"devcontainer": True},                (0, "brave_benz\n"),     "brave_benz"),
    # Devcontainer no match:
    ({"devcontainer": True},                (0, ""),                 None),
    # Devcontainer multiple matches → first:
    ({"devcontainer": True},                (0, "first\nsecond\n"),  "first"),
], ids=["literal_running", "literal_stopped", "label_match", "label_no_match", "label_multi_match"])
def test_current_name(monkeypatch, proj_kwargs, response, expected):
    _stub_run(monkeypatch, [response])
    assert container.current_name(_proj(**proj_kwargs)) == expected


def test_current_name_devcontainer_uses_local_folder_label(monkeypatch):
    """Pin the exact docker-ps argv used for label-based lookup."""
    calls = _stub_run(monkeypatch, [(0, "brave_benz\n")])
    container.current_name(_proj(devcontainer=True, repo="/Users/me/dev/webapp"))
    assert calls[0][0] == [
        "docker", "ps",
        "--filter", "label=devcontainer.local_folder=/Users/me/dev/webapp",
        "--format", "{{.Names}}",
    ]


def test_current_name_host_only(monkeypatch):
    _stub_run(monkeypatch, [])  # should not shell out
    assert container.current_name(_proj()) is None
