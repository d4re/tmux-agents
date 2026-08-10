import pytest
from tmux_agents import agent_kind


def test_kind_constants():
    assert agent_kind.KINDS == ("claude", "codex")
    assert agent_kind.CLAUDE == "claude"
    assert agent_kind.CODEX == "codex"


def test_executable():
    assert agent_kind.executable("claude") == "claude"
    assert agent_kind.executable("codex") == "codex"


def test_resume_args_claude_flag_style():
    assert agent_kind.resume_args("claude", "abc-123") == " --resume abc-123"


def test_resume_args_codex_subcommand_style():
    assert agent_kind.resume_args("codex", "abc-123") == " resume abc-123"


def test_resume_args_empty_for_no_session():
    assert agent_kind.resume_args("claude", None) == ""
    assert agent_kind.resume_args("codex", "") == ""


def test_resume_args_quotes_session_id():
    assert agent_kind.resume_args("claude", "a b") == " --resume 'a b'"


def test_other():
    assert agent_kind.other("claude") == "codex"
    assert agent_kind.other("codex") == "claude"


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        agent_kind.executable("gemini")
