"""Overview pane: row model, fold persistence, status-line summary, and the
curses TUI shown in the split-layout bottom pane (`agent-overview`).

Imported from `state_tick` for the summary chunk and from `commands/overview`
for the TUI loop. Layout / restore / new use `attach_overview_pane` to wire
the pane into a window."""
from __future__ import annotations
import curses
import logging
import subprocess
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Literal, NamedTuple
from tmux_agents import paths, state, theme, tmux

logger = logging.getLogger(__name__)

LABEL = {
    state.RUNNING:    "running",
    state.WAITING:    "waiting",
    state.IDLE:       "idle",
    state.BACKGROUND: "background",
    state.SLEEPING:   "sleeping",
    state.ERRORED:    "errored",
    state.STARTING:   "starting",
}

TMUX_RESET = "#[default]"


# ===== Row model =====

@dataclass
class Row:
    kind: Literal["header", "agent"]
    repo: str
    count: int = 0           # header-only
    folded: bool = False     # header-only
    win: tmux.Window | None = None  # agent-only
    code: str = ""                  # agent-only
    overlay_count: int = 0          # agent-only (background/sleeping item count)


def build_rows(folds: dict[str, bool], *, windows: list[tmux.Window] | None = None) -> list[Row]:
    if windows is None:
        windows = tmux.list_windows(tmux.SESSION)
    rows: list[Row] = []
    for repo, wins in group_by_repo(windows).items():
        folded = bool(folds.get(repo, False))
        rows.append(Row(kind="header", repo=repo, count=len(wins), folded=folded))
        if folded:
            continue
        for w in wins:
            code, overlay_count = _parse_state_code(w.state_code)
            rows.append(Row(kind="agent", repo=repo, win=w, code=code, overlay_count=overlay_count))
    return rows


def group_by_repo(windows: list[tmux.Window]) -> dict[str, list[tmux.Window]]:
    groups: OrderedDict[str, list[tmux.Window]] = OrderedDict()
    for w in windows:
        if w.name == tmux.CONTROL_WINDOW:
            continue
        groups.setdefault(_window_repo(w), []).append(w)
    return groups


def _window_repo(win: tmux.Window) -> str:
    return win.name.split(":", 1)[0]


def format_header(row: Row) -> str:
    glyph = "▸" if row.folded else "▾"
    plural = "s" if row.count != 1 else ""
    return f"{glyph} {row.repo}  ({row.count} agent{plural})"


def format_line_plain(win: tmux.Window, code: str, count: int, *,
                      mark_active: bool = True) -> str:
    """No-escape row text. The curses TUI paints color via curses attribs and
    passes `mark_active=False` because it handles the cursor marker itself."""
    indent = "> " if (win.active and mark_active) else "  "
    return f"{indent}{win.index}:{win.name}    {_row_label(code, count)}"


def _parse_state_code(raw: str) -> tuple[str, int]:
    """Parse a `@state_code` window-option value (e.g. 'B2', 'Z3', 'R', or '')
    into (letter, overlay_count). Empty/unknown → idle."""
    raw = (raw or "").strip()
    if not raw or raw[0] not in LABEL:
        return state.IDLE, 0
    try:
        count = int(raw[1:]) if raw[1:] else 0
    except ValueError:
        logger.debug("overview: non-integer overlay in state code %r", raw)
        count = 0
    return raw[0], count


def _row_label(code: str, count: int) -> str:
    label = LABEL[code]
    if code in (state.BACKGROUND, state.SLEEPING) and count > 0:
        return f"{label}·{count}"
    return label


# ===== Status-line summary (hot path, called from state_tick) =====

def empty_counts() -> dict[str, int]:
    return {c: 0 for c in LABEL}


def render_summary(*, counts: dict[str, int] | None = None) -> str:
    """One-line status-bar chunk in tmux format-string markup. Pass `counts`
    to skip re-reading state files (state_tick already has them in memory)."""
    if counts is None:
        counts = _summary_counts()
    p = theme.get_palette()

    def tok(code: str, label: str, sep: str) -> str:
        return f"#[fg={p.fg[code]}]{label}{TMUX_RESET}{sep}{counts[code]}"

    return _summary(tok, counts)


def _summary_counts() -> dict[str, int]:
    counts = empty_counts()
    for w in tmux.list_windows(tmux.SESSION):
        if w.name != tmux.CONTROL_WINDOW:
            counts[_parse_state_code(w.state_code)[0]] += 1
    return counts


def _summary(tok, counts: dict[str, int]) -> str:
    summary = "  ".join(tok(c, c, " ") for c in (
        state.RUNNING, state.WAITING, state.BACKGROUND, state.SLEEPING,
        state.IDLE, state.ERRORED,
    ))
    # STARTING uses a different label/sep ("starting: N") and is hidden when zero.
    if counts[state.STARTING] > 0:
        summary += "  " + tok(state.STARTING, LABEL[state.STARTING], ": ")
    return summary


# ===== Fold persistence =====

def load_folds() -> dict[str, bool]:
    raw = paths.read_json_or(paths.folds_file(), {})
    if not isinstance(raw, dict):
        return {}
    return {k: bool(v) for k, v in raw.items() if isinstance(k, str)}


def save_folds(folds: dict[str, bool]) -> None:
    # No locking: split-layout panes can race on simultaneous toggles
    # (last-writer-wins, one click lost). Vanishingly rare; accepted for v1.
    paths.atomic_write_json(paths.folds_file(), folds, sort_keys=True)


def load_folds_with_gc(*, windows: list[tmux.Window] | None = None) -> dict[str, bool]:
    """Load folds and drop entries for repos not in the current window list."""
    folds = load_folds()
    if not folds:
        return folds
    if windows is None:
        windows = tmux.list_windows(tmux.SESSION)
    live_repos = {_window_repo(w) for w in windows if w.name != tmux.CONTROL_WINDOW}
    pruned = {k: v for k, v in folds.items() if k in live_repos}
    if pruned != folds:
        save_folds(pruned)
    return pruned


# ===== Curses TUI: cursor model =====

class Cursor(NamedTuple):
    """Identifies a row by its content (window_id for agents, repo name for
    headers) so it survives row reorderings, additions, and removals."""
    kind: Literal["agent", "header"]
    key: str


def _row_key(r: Row) -> Cursor:
    return Cursor(r.kind, r.win.id if r.kind == "agent" else r.repo)


def first_cursor(rows: list[Row]) -> Cursor | None:
    if not rows:
        return None
    return _row_key(rows[0])


def _active_row(rows: list[Row]) -> Cursor | None:
    for r in rows:
        if r.kind == "agent" and r.win is not None and r.win.active:
            return _row_key(r)
    return None


def auto_track_cursor(
    rows: list[Row],
    cursor: Cursor | None,
    last_active: Cursor | None,
) -> tuple[Cursor | None, Cursor | None]:
    """Tick-time cursor update: follow the active window unless the user
    moved it elsewhere.

    Detection: if the cursor matches `last_active` (or is None), the cursor
    was auto-tracking; advance it to the current active row. Otherwise the
    user moved it; keep it where it is (clamped to existing rows).

    Returns `(new_cursor, current_active)` — the caller stores
    `current_active` as the next tick's `last_active`. After activating an
    agent (Enter or click), the caller should also assign
    `last_active = cursor` to immediately re-engage tracking without a
    one-tick visual flicker.
    """
    current_active = _active_row(rows)
    if cursor is None or cursor == last_active:
        new_cursor = current_active or first_cursor(rows)
    else:
        new_cursor = pin_cursor(rows, cursor)
    return new_cursor, current_active


def pin_cursor(rows: list[Row], cursor: Cursor | None) -> Cursor | None:
    if not rows:
        return None
    if cursor is None:
        return first_cursor(rows)
    if cursor in (_row_key(r) for r in rows):
        return cursor
    # Cursor is gone (window killed, repo emptied/folded). Snap to the first
    # surviving agent if any; otherwise the first header.
    for r in rows:
        if r.kind == "agent":
            return _row_key(r)
    return first_cursor(rows)


def move_cursor(rows: list[Row], cursor: Cursor | None, delta: int) -> Cursor | None:
    if not rows or cursor is None:
        return cursor
    keys = [_row_key(r) for r in rows]
    if cursor not in keys:
        return first_cursor(rows)
    idx = keys.index(cursor)
    new_idx = max(0, min(len(keys) - 1, idx + delta))
    return keys[new_idx]


def activate_target(target: Cursor, folds: dict[str, bool]) -> Cursor:
    kind, key = target
    if kind == "agent":
        tmux.select_window(key)
    else:
        folds[key] = not folds.get(key, False)
        save_folds(folds)
    return target


# ===== Curses TUI: rendering =====

# Action keys are reached through the tmux prefix (Ctrl-Space). Footers spell
# the full chord so users who don't yet know the prefix can discover it; the
# bare keys also work while the overview pane itself is focused.
_PREFIX = "Ctrl-Space"
_FOOTER_FULL = f"↑↓ select  ↵ open  {_PREFIX}: N new  K kill  R restore  E rename"
_FOOTER_SHORT = f"{_PREFIX} N/K/R/E"


def _errored_count(rows: list[Row]) -> int:
    return sum(1 for r in rows if r.kind == "agent" and r.code == state.ERRORED)


def _restore_alert(n: int) -> str:
    plural = "" if n == 1 else "s"
    return f"⚠ {n} agent{plural} down — press {_PREFIX} R to restore"

# State-color pair allocations. Inactive rows use the state fg on the
# default bg; active rows invert (selected_fg on state bg) for the
# bg-block effect that matches the plain-text and status-line renderings.
_STATE_CODES = (state.RUNNING, state.WAITING, state.BACKGROUND,
                state.SLEEPING, state.IDLE, state.ERRORED)
_PAIR_INACTIVE = {code: i + 1 for i, code in enumerate(_STATE_CODES)}
_PAIR_ACTIVE = {code: i + 1 + len(_STATE_CODES) for i, code in enumerate(_STATE_CODES)}


def _footer_for_width(width: int) -> str:
    if width >= len(_FOOTER_FULL) + 2:
        return _FOOTER_FULL
    if width >= len(_FOOTER_SHORT) + 2:
        return _FOOTER_SHORT
    return ""


def _hex_to_xterm256(hex_color: str) -> int:
    """Map '#RRGGBB' to the closest xterm-256 color cube index."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    def to6(v: int) -> int:
        if v < 48:
            return 0
        if v < 115:
            return 1
        return (v - 35) // 40
    return 16 + 36 * to6(r) + 6 * to6(g) + to6(b)


def setup_curses_colors() -> None:
    """Initialize curses color pairs from the theme palette. Safe to call
    repeatedly; no-op in tests (curses isn't initscr'd)."""
    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    p = theme.get_palette()
    for code in _STATE_CODES:
        fg = _hex_to_xterm256(p.fg[code])
        sel = _hex_to_xterm256(p.selected_fg[code])
        try:
            curses.init_pair(_PAIR_INACTIVE[code], fg, -1)
            curses.init_pair(_PAIR_ACTIVE[code], sel, fg)
        except curses.error:
            pass  # too many pairs/colors; degrade gracefully


def _row_text(r: Row) -> str:
    if r.kind == "header":
        return format_header(r)
    assert r.win is not None
    return format_line_plain(r.win, r.code, r.overlay_count, mark_active=False)


def _decorate_cursor(text: str) -> str:
    """Mark the keyboard cursor row with a '> ' prefix in place of the indent."""
    return "> " + (text[2:] if text.startswith("  ") else text)


def _color_pair(n: int) -> int:
    # curses.color_pair raises before initscr(); guard so tests can drive us.
    try:
        return curses.color_pair(n)
    except curses.error:
        return 0


def _agent_attr(r: Row) -> int:
    code = r.code if r.code in _PAIR_INACTIVE else state.IDLE
    if r.win is not None and r.win.active:
        return _color_pair(_PAIR_ACTIVE[code]) | curses.A_BOLD
    return _color_pair(_PAIR_INACTIVE[code])


def render_curses(stdscr, rows: list[Row], cursor: Cursor | None):
    """Render rows + footer; return (row_at_y, text_w_at_y) parallel arrays
    the click handler uses for hit-testing (y-indexed)."""
    height, width = stdscr.getmaxyx()
    stdscr.erase()
    row_at_y: list[Cursor | None] = [None] * height
    text_w_at_y: list[int] = [0] * height
    data_rows = max(0, height - 1)  # last row reserved for footer

    for y in range(min(len(rows), data_rows)):
        r = rows[y]
        text = _row_text(r)
        is_cursor = cursor is not None and _row_key(r) == cursor
        if is_cursor:
            text = _decorate_cursor(text)
        attr = _agent_attr(r) if r.kind == "agent" else 0
        stdscr.addnstr(y, 0, text, max(0, width - 1), attr)
        row_at_y[y] = _row_key(r)
        text_w_at_y[y] = len(text)

    fy = height - 1
    n_err = _errored_count(rows)
    if n_err > 0:
        # When an agent pane is dead, the recovery hint replaces the dim nav
        # footer — it's the one thing worth showing. Right-aligned like the nav
        # footer, in the errored color + bold so it grabs attention.
        alert = _restore_alert(n_err)
        attr = _color_pair(_PAIR_INACTIVE[state.ERRORED]) | curses.A_BOLD
        fx = max(0, width - len(alert) - 1)
        stdscr.addnstr(fy, fx, alert, max(0, width - fx), attr)
    else:
        footer = _footer_for_width(width)
        if footer:
            fx = max(0, width - len(footer) - 1)
            stdscr.addnstr(fy, fx, footer, max(0, width - fx), curses.A_DIM)
    return row_at_y, text_w_at_y


# ===== Subprocess actions (popups) =====

def _popen(argv: list[str]) -> None:
    """Wrapper to make subprocess.Popen monkeypatchable in tests."""
    subprocess.Popen(argv)


def spawn_new_agent() -> None:
    _popen([
        "tmux", "-L", "agents",
        "display-popup", "-E", "-w", "60%", "-h", "60%",
        "agent-new",
    ])


def restore_dead() -> None:
    """Revive every dead agent pane via the existing restore worker. Idempotent:
    windows with a live pane classify as `skip`, so this is a no-op when nothing
    is down. `--background` keeps the keypress from blocking while the worker
    brings containers/Claude back up."""
    _popen(["agent-restore", "--background"])


def _agent_window_id(cursor: Cursor | None) -> str | None:
    return cursor.key if cursor is not None and cursor.kind == "agent" else None


def kill_at(cursor: Cursor | None) -> None:
    wid = _agent_window_id(cursor)
    if wid is None:
        return
    _popen(["tmux", "-L", "agents", "display-popup", "-E", "-w", "50%", "-h", "20%",
            f"agent-kill --window-id {wid}"])


def rename_at(cursor: Cursor | None) -> None:
    wid = _agent_window_id(cursor)
    if wid is None:
        return
    _popen(["tmux", "-L", "agents", "command-prompt", "-p", "new branch name:",
            f"run-shell 'agent-rename --window-id {wid} %%'"])


# ===== Pane wiring =====

def attach_overview_pane(window_id: str) -> None:
    """Bottom 25% overview pane, tagged @role=overview so tmux's
    MouseDown1Pane binding routes clicks back into the curses TUI.

    Idempotent: skips windows that already have an overview pane. Without this
    guard, a layout toggle (or restore) re-attaching to a window whose agent
    pane has already died adds a *second* overview pane, wedging the window
    into a two-overview state that revive cannot trivially recover."""
    if tmux.overview_pane_ids(window_id):
        return
    pane_id = tmux.split_window(window_id, percent=25, command="agent-overview")
    tmux.set_pane_option(pane_id, "@role", "overview")


# ===== Curses TUI: event loop state + handlers =====

@dataclass
class TuiState:
    folds: dict[str, bool]
    rows: list[Row]
    cursor: Cursor | None
    last_active: Cursor | None
    self_pane_id: str
    row_at_y: list[Cursor | None] = field(default_factory=list)
    text_w_at_y: list[int] = field(default_factory=list)


def make_initial_state(self_pane_id: str) -> TuiState:
    """First-tick state: GC folds on disk, seed cursor on the active row."""
    windows = tmux.list_windows(tmux.SESSION)
    folds = load_folds_with_gc(windows=windows)
    rows = build_rows(folds, windows=windows)
    cursor, last_active = auto_track_cursor(rows, None, None)
    return TuiState(folds=folds, rows=rows, cursor=cursor,
                    last_active=last_active, self_pane_id=self_pane_id)


def handle_tick(state: TuiState) -> None:
    """Periodic refresh: re-read fold state (other panes may have toggled),
    rebuild rows, and advance the cursor with the active window if the user
    hadn't moved it elsewhere."""
    state.folds = load_folds()
    state.rows = build_rows(state.folds)
    state.cursor, state.last_active = auto_track_cursor(
        state.rows, state.cursor, state.last_active)


def _refresh_after_activation(state: TuiState) -> None:
    """Rebuild rows after activate_target ran. If the activation was on an
    agent (window switch), re-engage tracking so the cursor follows the
    new active window without a one-tick lag."""
    state.rows = build_rows(state.folds)
    state.cursor = pin_cursor(state.rows, state.cursor)
    if state.cursor is not None and state.cursor.kind == "agent":
        state.last_active = state.cursor


def handle_key(state: TuiState, ch: int) -> None:
    """Apply a non-mouse keypress to the state. KEY_RESIZE is a no-op the
    caller filters out (next iteration redraws). Unknown keys are silently
    ignored."""
    if ch == curses.KEY_UP:
        state.cursor = move_cursor(state.rows, state.cursor, -1)
    elif ch == curses.KEY_DOWN:
        state.cursor = move_cursor(state.rows, state.cursor, +1)
    elif ch in (curses.KEY_ENTER, 10, 13):
        if state.cursor is not None:
            state.cursor = activate_target(state.cursor, state.folds)
            _refresh_after_activation(state)
    elif ch == ord('N'):
        spawn_new_agent()
    elif ch == ord('K'):
        kill_at(state.cursor)
    elif ch == ord('R'):
        restore_dead()
    elif ch == ord('E'):
        rename_at(state.cursor)


def handle_mouse(state: TuiState, mx: int, my: int, bstate: int) -> None:
    """Apply a mouse event. tmux's mouse forwarding can land as either
    BUTTON1_PRESSED or BUTTON1_CLICKED depending on the curses-negotiated
    mouse mode; accept either and ignore everything else."""
    if not (bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED)):
        return
    target = state.row_at_y[my] if 0 <= my < len(state.row_at_y) else None
    if target is None or mx >= state.text_w_at_y[my]:
        tmux.select_pane(state.self_pane_id)
        return
    state.cursor = target
    state.cursor = activate_target(state.cursor, state.folds)
    _refresh_after_activation(state)
