"""Per-pane registry of self-expiring background/scheduled markers.

Claude hooks (running inside the container) drop one marker file per
pending/running thing under <worktree>/.local/.tmux-agents/pending-<pane>/,
named '<kind>__<id>' (or just 'wakeup' for the singleton). File content is the
kind's schedule signal (epoch-ms fire time for wakeup, cron expression for
crons, empty otherwise); the file's mtime is its creation time.

The host tick calls `scan()` each cycle: it computes each marker's effective
expiry, unlinks the dead ones, and returns live background/sleeping counts.
This is where all TTL policy + cron-expr parsing lives (host side, local TZ) —
the shell only writes and removes files.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from croniter import croniter

from tmux_agents import paths, state

logger = logging.getLogger(__name__)

# Marker kinds (the '<kind>' half of the filename).
WAKEUP = "wakeup"
CRON_ONESHOT = "cron-oneshot"
CRON_RECUR = "cron-recur"
SUBAGENT = "subagent"
BG_SHELL = "bg-shell"

# Which display class each kind contributes to.
_CLASS = {
    WAKEUP: state.SLEEPING,
    CRON_ONESHOT: state.SLEEPING,
    CRON_RECUR: state.SLEEPING,
    SUBAGENT: state.BACKGROUND,
    BG_SHELL: state.BACKGROUND,
}

# TTL knobs (seconds). These are *backstops* — every kind also has a precise
# removal path (see hooks/write-state.sh): wakeups/crons self-expire by their
# real fire time, and both background subagents and background Bash are removed
# on the UserPromptSubmit <task-notification> that signals their completion. The
# TTL only covers session-ends-before-completion and payload-format drift.
# Tunable; see BACKLOG.md for config-ification.
WAKEUP_GRACE = 30  # past the exact scheduledFor fire time
CRON_ONESHOT_GRACE = 60  # past the computed next-fire time
CRON_RECUR_TTL = 7 * 24 * 3600  # Claude's documented hard auto-expiry
SUBAGENT_TTL = (
    30 * 60
)  # backstop; precise removal via the UserPromptSubmit task-notification
BG_SHELL_TTL = (
    30 * 60
)  # backstop; precise removal via the UserPromptSubmit task-notification


@dataclass(frozen=True)
class Counts:
    background: int
    sleeping: int

    def for_letter(self, letter: str) -> int:
        """Overlay count to render alongside a derived display letter — the
        background count under `B`, the sleeping count under `Z`, else 0. Keeps
        the letter->which-count mapping here, where the B/Z classes are defined."""
        if letter == state.BACKGROUND:
            return self.background
        if letter == state.SLEEPING:
            return self.sleeping
        return 0


def scan(worktree: Path, pane_id: str, *, now: float | None = None) -> Counts:
    """Count live (non-expired) B/Z markers for a pane, unlinking dead ones."""
    if now is None:
        now = time.time()
    d = paths.worktree_pending_dir(worktree, pane_id)
    try:
        entries = list(d.iterdir())
    except (FileNotFoundError, NotADirectoryError, OSError):
        return Counts(0, 0)

    background = sleeping = 0
    for f in entries:
        if not f.is_file():
            continue
        kind, _ident = _parse_name(f.name)
        cls = _CLASS.get(kind)
        if cls is None:
            logger.debug("registry: dropping unknown marker %s", f.name)
            f.unlink(missing_ok=True)
            continue
        try:
            created_at = f.stat().st_mtime
            signal = f.read_text().strip()
        except OSError:
            logger.debug("registry: marker unreadable %s", f.name)
            continue
        if now >= _expiry(kind, signal, created_at):
            f.unlink(missing_ok=True)
            continue
        if cls == state.BACKGROUND:
            background += 1
        else:
            sleeping += 1
    return Counts(background=background, sleeping=sleeping)


def _parse_name(name: str) -> tuple[str, str]:
    """'cron-oneshot__abc' -> ('cron-oneshot', 'abc'); 'wakeup' -> ('wakeup', '')."""
    kind, _, ident = name.partition("__")
    return kind, ident


def _expiry(kind: str, signal: str, created_at: float) -> float:
    """Epoch-seconds after which a marker is considered dead."""
    if kind == WAKEUP:
        # signal = absolute fire time in epoch MILLISECONDS (scheduledFor).
        try:
            return float(signal) / 1000.0 + WAKEUP_GRACE
        except (TypeError, ValueError):
            logger.debug("registry: bad wakeup signal %r", signal)
            return created_at + WAKEUP_GRACE
    if kind == CRON_ONESHOT:
        nxt = _next_cron_fire(signal, created_at)
        base = nxt if nxt is not None else created_at
        return base + CRON_ONESHOT_GRACE
    if kind == CRON_RECUR:
        return created_at + CRON_RECUR_TTL
    if kind == SUBAGENT:
        return created_at + SUBAGENT_TTL
    if kind == BG_SHELL:
        return created_at + BG_SHELL_TTL
    return created_at  # unknown — already filtered, but expire defensively


def _next_cron_fire(cron_expr: str, after: float) -> float | None:
    """Next fire of a 5-field cron after `after` (epoch s), in local TZ.
    Returns None on an unparseable expression."""
    if not cron_expr:
        return None
    try:
        return croniter(cron_expr, datetime.fromtimestamp(after)).get_next(float)
    except (ValueError, KeyError, AttributeError):
        logger.debug("registry: unparseable cron %r", cron_expr)
        return None
