"""`agent-overview` entry point: curses TUI for the split-layout overview
pane. The row model, summary renderer, and event handlers live in
`tmux_agents.overview`."""

import curses
from tmux_agents import logging_setup, overview, tmux


def _run_tui_loop(stdscr) -> None:
    curses.curs_set(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    overview.setup_curses_colors()
    stdscr.timeout(2000)
    state = overview.make_initial_state(tmux.current_pane_id())

    while True:
        state.row_at_y, state.text_w_at_y = overview.render_curses(
            stdscr, state.rows, state.cursor
        )
        stdscr.refresh()

        ch = stdscr.getch()
        if ch == -1:
            overview.handle_tick(state)
            continue
        if ch == curses.KEY_RESIZE:
            continue
        if ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
            except curses.error:
                continue
            overview.handle_mouse(state, mx, my, bstate)
        else:
            overview.handle_key(state, ch)


def main(argv: list[str] | None = None) -> int:
    logging_setup.setup_logging()
    curses.wrapper(_run_tui_loop)
    return 0
