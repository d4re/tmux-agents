"""Build the container/host exec command that launches Claude in a pane,
injecting ` --resume <session_id>` via the `{resume_args}` placeholder.

Shared by `agent-restore` and `agent-rebuild` so resume semantics stay
identical. Kept free of tmux/window knowledge — callers pass the pieces in.
"""

from __future__ import annotations
import logging
import shlex

from tmux_agents.config import Project

logger = logging.getLogger(__name__)


def build(
    proj: Project,
    *,
    branch: str | None,
    claude_session_id: str | None,
    container_name: str | None,
    label: str = "",
) -> str:
    """Substitute `proj.exec_cmd`, injecting ` --resume <id>` via
    `{resume_args}` when a session id is present. `label` (e.g. a window id)
    is only used to attribute the missing-placeholder warning."""
    resume_args = ""
    if claude_session_id:
        resume_args = f" --resume {shlex.quote(claude_session_id)}"
        if "{resume_args}" not in proj.exec_cmd:
            logger.warning(
                "%s: project %r has a custom exec_cmd without {resume_args} placeholder; "
                "Claude will not auto-resume. Add {resume_args} after `claude` in "
                "projects.toml to enable resume.",
                label or proj.name,
                proj.name,
            )
    return proj.substitute(
        proj.exec_cmd,
        branch=branch,
        container_name=container_name,
        resume_args=resume_args,
    )
