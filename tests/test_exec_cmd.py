"""Tests for exec_cmd.build's kind-aware resume-arg injection."""

from __future__ import annotations

from pathlib import Path

from tmux_agents import exec_cmd
from tmux_agents.config import Project


def _proj(**kw):
    base = {
        "name": "p",
        "repo": Path("/r"),
        "exec_cmd": "cd {workdir} && TMUX_AGENTS_AGENT=1 exec claude{resume_args}",
        "codex_exec_cmd": "cd {workdir} && TMUX_AGENTS_AGENT=1 exec codex{resume_args}",
    }
    base.update(kw)
    return Project(**base)


def test_build_codex_resume_subcommand():
    cmd = exec_cmd.build(
        _proj(), branch=None, session_id="c-1", container_name=None, kind="codex"
    )
    assert cmd.endswith(
        "TMUX_AGENTS_AGENT=1 codex resume c-1 || TMUX_AGENTS_AGENT=1 exec codex"
    )


def test_build_claude_resume_flag_default_kind():
    cmd = exec_cmd.build(_proj(), branch=None, session_id="s-1", container_name=None)
    assert cmd.endswith(
        "TMUX_AGENTS_AGENT=1 claude --resume s-1 || TMUX_AGENTS_AGENT=1 exec claude"
    )


def test_build_no_session_no_resume():
    cmd = exec_cmd.build(
        _proj(), branch=None, session_id=None, container_name=None, kind="codex"
    )
    assert cmd.endswith("exec codex")


def test_build_resume_falls_back_to_fresh_start_in_container_body():
    # A resume of an id with no transcript exits 1 ("No conversation
    # found"); the pane must start a fresh session instead of dying.
    p = _proj(
        exec_cmd="sbx exec -it {sandbox} bash -lc 'cd {workdir} && exec claude{resume_args}'"
    )
    cmd = exec_cmd.build(p, branch=None, session_id="s-1", container_name=None)
    assert cmd.endswith("&& claude --resume s-1 || exec claude'")


def test_build_resume_fallback_without_exec_in_template():
    p = _proj(exec_cmd="cd {workdir} && claude{resume_args}")
    cmd = exec_cmd.build(p, branch=None, session_id="s-1", container_name=None)
    assert cmd == "cd /r && claude --resume s-1 || claude"


def test_build_no_session_keeps_template_untouched():
    cmd = exec_cmd.build(_proj(), branch=None, session_id=None, container_name=None)
    assert cmd == "cd /r && TMUX_AGENTS_AGENT=1 exec claude"
