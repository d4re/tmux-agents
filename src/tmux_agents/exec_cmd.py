"""Build the container/host exec command that launches an agent in a pane,
injecting the kind's resume snippet via the `{resume_args}` placeholder.

Shared by `agent-restore` and `agent-rebuild` so resume semantics stay
identical. Kept free of tmux/window knowledge — callers pass the pieces in.
"""

from __future__ import annotations

import logging
import re

from tmux_agents import agent_kind
from tmux_agents.config import Project

logger = logging.getLogger(__name__)

# `[VAR=val ...] [exec ]<exe>{resume_args}` — the launch the fallback wraps.
_LAUNCH_RE = r"((?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*)(exec\s+)?{exe}\{{resume_args\}}"


def _with_fresh_fallback(template: str, exe: str) -> str:
    """Rewrite the launch so a failed resume starts a fresh session.

    A resume id can outlive its transcript (the agent exited before the
    first message, so nothing was saved): the resume then exits non-zero
    and the pane dies straight back to X. `<exe> --resume <id> || exec
    <exe>` keeps the pane alive; the fresh session's SessionStart hook
    records its new id, which the tick merges over the stale one.
    """
    pattern = _LAUNCH_RE.format(exe=re.escape(exe))
    return re.sub(
        pattern,
        lambda m: f"{m[1]}{exe}{{resume_args}} || {m[1]}{m[2] or ''}{exe}",
        template,
        count=1,
    )


def build(
    proj: Project,
    *,
    branch: str | None,
    session_id: str | None,
    container_name: str | None,
    kind: str = agent_kind.CLAUDE,
    label: str = "",
) -> str:
    """Substitute the kind's exec template, injecting resume args
    (claude: ` --resume <id>`; codex: ` resume <id>`) when present.
    `label` (e.g. a window id) is only used to attribute the
    missing-placeholder warning."""
    template = proj.exec_cmd_for(kind)
    resume_args = agent_kind.resume_args(kind, session_id)
    if resume_args and "{resume_args}" not in template:
        logger.warning(
            "%s: project %r custom %s exec command lacks {resume_args}; "
            "no auto-resume.",
            label or proj.name,
            proj.name,
            kind,
        )
    if resume_args:
        template = _with_fresh_fallback(template, agent_kind.executable(kind))
    return proj.substitute(
        template,
        branch=branch,
        container_name=container_name,
        resume_args=resume_args,
    )
