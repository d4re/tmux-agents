"""`agent-layout` entry point. Toggles the persistent split/compact
mode and rebuilds existing agent windows accordingly."""

from __future__ import annotations
import argparse
from tmux_agents import overview, paths, tmux


def _write(value: str) -> None:
    f = paths.layout_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-layout")
    parser.parse_args(argv)

    current = paths.read_layout()
    target = "compact" if current == "split" else "split"

    for win in tmux.list_windows(tmux.SESSION):
        if win.name == tmux.CONTROL_WINDOW:
            continue
        if target == "compact":
            for pane in tmux.list_panes(win.id):
                if pane.index != 0:
                    tmux.kill_pane(pane.id)
        else:
            overview.attach_overview_pane(win.id)

    _write(target)
    return 0
