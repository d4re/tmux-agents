"""Idempotent merge of src/tmux_agents/hooks/agents.json into a worktree's
.claude/settings.local.json, plus the helper script the hooks invoke.

We own three top-level keys: `_tmux_agents_version`, `tui`, and the
entries in `hooks` that correspond to events we ship. User-authored
hooks on the SAME event are preserved by appending our group alongside
theirs. Any other top-level key (env, permissions, model, mcpServers…)
is left untouched.

The hook commands themselves are tiny — they just dispatch to
`<worktree>/.local/.tmux-agents/write-state.sh` (also provisioned here
from package data). All shell logic lives in that script.

The version marker is the installed `tmux-agents` package version. Every
release bumps it, which forces re-provisioning on the next agent-new run
so newly-shipped hooks supersede stale ones in existing worktrees. The
script is rewritten unconditionally on every provision call (cheap,
idempotent, and ensures dev iteration on the script body propagates
even without a version bump).
"""
from __future__ import annotations
import json
from importlib import resources
from importlib.metadata import version as _pkg_version
from pathlib import Path

from tmux_agents import paths

_TMUX_AGENTS_VERSION = _pkg_version("tmux-agents")
_SCRIPT_NAME = "write-state.sh"


def _merge_hooks(existing: dict, template_hooks: dict, previously_versioned: bool) -> dict:
    """Append our hook groups per event.

    Versioned files (we wrote them previously): replace all groups for events
    we own, dropping no-longer-shipped event keys. Unversioned files: only drop
    groups that look like ours by signature, preserving genuine user hooks.
    """
    result = dict(existing) if existing else {}
    for event, our_groups in template_hooks.items():
        if previously_versioned:
            result[event] = list(our_groups)
        else:
            kept = [g for g in result.get(event, []) if not _looks_like_our_group(g)]
            result[event] = kept + list(our_groups)
    if previously_versioned:
        for event in list(result.keys()):
            if event not in template_hooks and not result[event]:
                del result[event]
    return result


_OUR_SIGNATURE = _SCRIPT_NAME


def _looks_like_our_group(group: dict) -> bool:
    for h in group.get("hooks", []):
        cmd = h.get("command", "")
        if _OUR_SIGNATURE in cmd or cmd == "printf '\\a'":
            return True
    return False


def _provision_script(worktree: Path) -> None:
    target = worktree / ".local" / ".tmux-agents" / _SCRIPT_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    src = resources.files("tmux_agents.hooks") / _SCRIPT_NAME
    target.write_text(src.read_text())
    target.chmod(0o755)


def provision_settings(worktree: Path, *, template_path: Path) -> bool:
    """Merge the template's tmux-agents keys into settings.local.json and
    rewrite the helper script. Returns True if settings.local.json changed."""
    _provision_script(worktree)
    template = json.loads(template_path.read_text())
    target = worktree / ".claude" / "settings.local.json"
    current: dict = paths.read_json_or(target, {})

    merged_hooks = _merge_hooks(
        current.get("hooks", {}), template["hooks"],
        previously_versioned="_tmux_agents_version" in current,
    )
    if (current.get("_tmux_agents_version") == _TMUX_AGENTS_VERSION
            and current.get("tui") == template["tui"]
            and current.get("hooks") == merged_hooks):
        return False

    paths.atomic_write_json(target, {
        **current,
        "_tmux_agents_version": _TMUX_AGENTS_VERSION,
        "tui": template["tui"],
        "hooks": merged_hooks,
    }, indent=2)
    return True
