"""Tests for exec_cmd.build's kind-aware resume-arg injection."""

from __future__ import annotations
from pathlib import Path

from tmux_agents import exec_cmd
from tmux_agents.config import Project


def _proj(**kw):
    base = dict(
        name="p",
        repo=Path("/r"),
        exec_cmd="cd {workdir} && TMUX_AGENTS_AGENT=1 exec claude{resume_args}",
        codex_exec_cmd="cd {workdir} && TMUX_AGENTS_AGENT=1 exec codex{resume_args}",
    )
    base.update(kw)
    return Project(**base)


def test_build_codex_resume_subcommand():
    cmd = exec_cmd.build(
        _proj(), branch=None, session_id="c-1", container_name=None, kind="codex"
    )
    assert cmd.endswith("exec codex resume c-1")


def test_build_claude_resume_flag_default_kind():
    cmd = exec_cmd.build(_proj(), branch=None, session_id="s-1", container_name=None)
    assert cmd.endswith("exec claude --resume s-1")


def test_build_no_session_no_resume():
    cmd = exec_cmd.build(
        _proj(), branch=None, session_id=None, container_name=None, kind="codex"
    )
    assert cmd.endswith("exec codex")
