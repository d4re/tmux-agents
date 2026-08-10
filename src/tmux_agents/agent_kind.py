"""The two supported agent kinds and their per-kind knowledge: executable
name and resume-argument spelling. Nothing else may hardcode 'claude'."""

from __future__ import annotations
import shlex

CLAUDE = "claude"
CODEX = "codex"
KINDS = (CLAUDE, CODEX)

# Exported (=1) only by agent exec templates; codex-hook.sh requires it so a
# manual codex run inside agent-terminal can't corrupt a pane's state/pin.
AGENT_MARKER = "TMUX_AGENTS_AGENT"


def _check(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown agent kind {kind!r}; expected one of {KINDS}")


def executable(kind: str) -> str:
    _check(kind)
    return kind  # both CLIs are named after their kind


def resume_args(kind: str, session_id: str | None) -> str:
    """Leading-space resume snippet for {resume_args}, or '' without an id.
    claude resumes via a flag, codex via a subcommand."""
    _check(kind)
    if not session_id:
        return ""
    q = shlex.quote(session_id)
    return f" --resume {q}" if kind == CLAUDE else f" resume {q}"


def other(kind: str) -> str:
    _check(kind)
    return CODEX if kind == CLAUDE else CLAUDE
