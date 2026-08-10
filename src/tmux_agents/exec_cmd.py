"""Build the container/host exec command that launches an agent in a pane,
injecting the kind's resume snippet via the `{resume_args}` placeholder.

Shared by `agent-restore` and `agent-rebuild` so resume semantics stay
identical. Kept free of tmux/window knowledge — callers pass the pieces in.
"""

from __future__ import annotations
import logging

from tmux_agents import agent_kind
from tmux_agents.config import Project

logger = logging.getLogger(__name__)


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
    return proj.substitute(
        template,
        branch=branch,
        container_name=container_name,
        resume_args=resume_args,
    )
