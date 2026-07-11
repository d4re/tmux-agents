"""Shared fzf idioms for interactive commands.

Keep this module free of any tmux or project knowledge — it only wraps the
fzf-backed primitives (pick, yes/no, free text, pick-or-create) that
agent-new and agent-kill consume.
"""

from __future__ import annotations
import logging
import subprocess
from collections.abc import Callable, Iterable
from tmux_agents import logging_setup

logger = logging.getLogger(__name__)

NO_BRANCH_SENTINEL = "[no branch — use repo root]"


class Cancelled(Exception):
    """User dismissed a prompt (Esc). Ctrl-C propagates as KeyboardInterrupt."""


def pick_one(
    items: Iterable[str],
    *,
    prompt: str,
    start_index: int | None = None,
) -> str | None:
    """Fuzzy-pick one of `items`. Returns the chosen string, or None on Esc.
    First item is pre-highlighted unless `start_index` (1-based) is given."""
    from iterfzf import iterfzf

    extra: tuple[str, ...] = ("--layout=reverse-list",)
    if start_index is not None:
        extra += (f"--bind=load:pos({start_index})",)
    return iterfzf(list(items), prompt=prompt, __extra__=extra)


def prompt_yes_no(prompt: str, *, default: bool) -> bool:
    """Two-item fzf picker for yes/no. `default=True` pre-highlights 'yes';
    `default=False` pre-highlights 'no'. Raises `Cancelled` on Esc."""
    items = ["yes", "no"] if default else ["no", "yes"]
    choice = pick_one(items, prompt=prompt)
    if choice is None:
        raise Cancelled
    return choice == "yes"


def pick_or_create(
    candidates: list[str],
    *,
    prompt: str,
    validator: Callable[[str], bool] | None = None,
) -> str | None:
    """fzf with `--print-query`: user can pick a candidate or type new.

    Returns:
        - The selected candidate string when fzf matched a candidate.
        - The typed string when no candidate matched and the user hit Enter.
          If `validator` is given, a non-empty unmatched query that fails
          validation triggers an error line on stderr and a reprompt.
        - `None` when the user submits empty input AND no candidate is
          available to default-highlight.

    Raises `Cancelled` on Esc.

    fzf output contract with `--print-query`:
        - Query line (always; may be empty).
        - Followed by the matched line(s) when the query matched a candidate
          and the user hit Enter (rc=0). When the user typed a non-matching
          query and hit Enter, fzf returns rc=1 and prints only the query.
    """
    from iterfzf import BUNDLED_EXECUTABLE

    while True:
        r = subprocess.run(
            [str(BUNDLED_EXECUTABLE), "--print-query", f"--prompt={prompt}"],
            input=("\n".join(candidates) + "\n") if candidates else "",
            capture_output=True,
            text=True,
        )
        if r.returncode not in (0, 1):
            raise Cancelled
        lines = r.stdout.splitlines()
        query = lines[0].strip() if lines else ""
        matched = lines[1] if len(lines) >= 2 else None
        if matched is not None:
            return matched
        if not query:
            return None
        if validator is None or validator(query):
            return query
        logging_setup.cli_error(logger, f"invalid input {query!r}")
