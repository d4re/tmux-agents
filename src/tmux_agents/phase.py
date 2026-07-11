"""Hook-written phase vocabulary and display-letter derivation.

`state.py` keeps the single-letter display codes the overview renders. This
module bridges the JSON `phase` field Claude hooks write into those codes,
overlaying the background/sleeping item counts computed by `registry.scan`,
using the priority rule X > W > R > B > Z > I > S.
"""

from __future__ import annotations
from tmux_agents import state

RUNNING = "running"
WAITING = "waiting"
IDLE = "idle"
STARTING = "starting"
ERRORED = "errored"


def derive_letter(phase: str, *, b_count: int, z_count: int, pane_alive: bool) -> str:
    """Priority: X > W > R > B > Z > I > S. Pane death and explicit `errored`
    phase both produce X. `b_count` (background items) and `z_count` (sleeping
    items) come from tmux_agents.registry.scan; they overlay an otherwise
    idle/starting agent."""
    if not pane_alive:
        return state.ERRORED
    if phase == ERRORED:
        return state.ERRORED
    if phase == WAITING:
        return state.WAITING
    if phase == RUNNING:
        return state.RUNNING
    if b_count > 0:
        return state.BACKGROUND
    if z_count > 0:
        return state.SLEEPING
    if phase == STARTING:
        return state.STARTING
    # phase == IDLE (or unknown).
    return state.IDLE
