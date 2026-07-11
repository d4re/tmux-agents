import pytest

from tmux_agents import phase, state


# (phase, b_count, z_count, pane_alive, expected_letter, comment)
# Priority chain: X > W > R > B > Z > I > S.
CASES = [
    # Dead pane → X regardless of phase or items.
    (phase.RUNNING, 1, 1, False, state.ERRORED, "dead pane overrides running"),
    (phase.IDLE, 0, 0, False, state.ERRORED, "dead pane overrides idle"),
    (phase.STARTING, 0, 0, False, state.ERRORED, "dead pane overrides starting"),
    # Explicit errored phase always wins.
    (phase.ERRORED, 0, 0, True, state.ERRORED, "explicit errored"),
    (phase.ERRORED, 1, 1, True, state.ERRORED, "explicit errored beats overlays"),
    # Waiting beats running and overlays.
    (phase.WAITING, 1, 1, True, state.WAITING, "waiting beats overlays"),
    # Running beats overlays (the agent is actively working).
    (phase.RUNNING, 0, 0, True, state.RUNNING, "running, no items"),
    (phase.RUNNING, 1, 1, True, state.RUNNING, "running beats overlays"),
    # Background beats sleeping when idle.
    (phase.IDLE, 1, 0, True, state.BACKGROUND, "idle + bg item = background"),
    (phase.IDLE, 1, 2, True, state.BACKGROUND, "background beats sleeping"),
    (phase.STARTING, 1, 0, True, state.BACKGROUND, "starting + bg item = background"),
    # Sleeping when idle/starting with only Z items.
    (phase.IDLE, 0, 2, True, state.SLEEPING, "idle + sleeping item = sleeping"),
    (phase.STARTING, 0, 1, True, state.SLEEPING, "starting + sleeping item = sleeping"),
    # Fall through to phase letters.
    (phase.IDLE, 0, 0, True, state.IDLE, "idle, no items"),
    (phase.STARTING, 0, 0, True, state.STARTING, "starting, no items"),
    ("bogus", 0, 0, True, state.IDLE, "unknown phase defaults to idle"),
]


@pytest.mark.parametrize(
    "phase_val,b_count,z_count,pane_alive,expected,comment",
    CASES,
    ids=[c[-1] for c in CASES],
)
def test_derive_letter(phase_val, b_count, z_count, pane_alive, expected, comment):
    assert (
        phase.derive_letter(
            phase_val, b_count=b_count, z_count=z_count, pane_alive=pane_alive
        )
        == expected
    )
