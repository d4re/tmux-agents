"""`agent-rename` entry point. Replaces the `:branch` half of
`<repo>:<branch>` window names; preserves the repo prefix.

Pinning: an explicit rename (no `--from-hook`) sets `@pinned 1` on the
window. Pinning is also set by `agent-new` and `agent-restore` when a
branch is supplied at creation. `--from-hook` (wired to tmux's
`pane-title-changed`) skips pinned windows so manually-named windows
keep their label; it also skips unknown windows, the ctrl window, and
empty titles."""

import argparse
import logging
from tmux_agents import logging_setup, tmux

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    logging_setup.setup_logging()
    parser = argparse.ArgumentParser(prog="agent-rename")
    parser.add_argument("--window-id", required=True)
    parser.add_argument(
        "new_name",
        nargs="?",
        default="",
        help="new branch name (will be prefixed with <repo>:)",
    )
    parser.add_argument(
        "--from-hook",
        action="store_true",
        help="hook mode: skip silently if window is missing/ctrl/pinned, or new_name is empty",
    )
    args = parser.parse_args(argv)

    win = next(
        (w for w in tmux.list_windows(tmux.SESSION) if w.id == args.window_id), None
    )
    if win is None:
        if args.from_hook:
            return 0
        logging_setup.cli_error(logger, f"no window with id {args.window_id}")
        return 2

    new_name = args.new_name.strip()
    if args.from_hook:
        if win.name == tmux.CONTROL_WINDOW or not new_name:
            return 0
        if tmux.is_window_pinned(win.id):
            return 0
    elif not new_name:
        logging_setup.cli_error(logger, "new branch name is required")
        return 2

    repo = win.name.split(":", 1)[0]
    tmux.rename_window(win.id, f"{repo}:{new_name}")
    if not args.from_hook:
        tmux.set_window_option(win.id, "@pinned", "1")
    return 0
