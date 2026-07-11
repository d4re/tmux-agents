import io
import pytest
from tmux_agents.progress import Reporter
from tmux_agents import state, theme


def _make(out: io.StringIO, *clock_ticks: float) -> Reporter:
    ticks = iter(clock_ticks)
    return Reporter(out=out, color=False, clock=lambda: next(ticks))


def test_banner_writes_title():
    out = io.StringIO()
    r = Reporter(out=out, color=False)
    r.banner("Spawning agent: backend / feat/foo")
    assert "Spawning agent: backend / feat/foo" in out.getvalue()


def test_stage_clean_exit_under_1s_no_timing():
    out = io.StringIO()
    r = _make(out, 100.0, 100.4)
    with r.stage("worktree"):
        pass
    output = out.getvalue()
    assert "✓ worktree" in output
    assert "s)" not in output


def test_stage_clean_exit_seconds_format():
    out = io.StringIO()
    r = _make(out, 100.0, 102.5)
    with r.stage("worktree"):
        pass
    assert "✓ worktree (2.5s)" in out.getvalue()


def test_stage_clean_exit_minutes_format():
    out = io.StringIO()
    r = _make(out, 100.0, 175.3)
    with r.stage("container"):
        pass
    assert "✓ container (1m 15s)" in out.getvalue()


def test_stage_info_then_clean_exit():
    out = io.StringIO()
    r = _make(out, 0.0, 1.0)
    with r.stage("worktree") as st:
        st.info("fetching origin/main")
    output = out.getvalue()
    assert "▸ worktree — fetching origin/main" in output
    assert "✓ worktree (1.0s)" in output


def test_stage_skip_suppresses_check():
    out = io.StringIO()
    r = _make(out, 0.0, 0.1)
    with r.stage("container") as st:
        st.skip("already running")
    output = out.getvalue()
    assert "▸ container — already running" in output
    assert "✓ container" not in output
    assert r.had_warning is False


def test_stage_warn_flips_flag_and_suppresses_check():
    out = io.StringIO()
    r = _make(out, 0.0, 0.5)
    with r.stage("hooks") as st:
        st.warn("permission denied")
    output = out.getvalue()
    assert "! hooks — permission denied" in output
    assert "✓ hooks" not in output
    assert r.had_warning is True


def test_stage_exception_emits_cross_and_reraises():
    out = io.StringIO()
    r = _make(out, 0.0, 0.3)
    with pytest.raises(RuntimeError, match="boom"):
        with r.stage("container"):
            raise RuntimeError("boom")
    assert "✗ container — RuntimeError: boom" in out.getvalue()


def test_stage_warn_includes_timing():
    out = io.StringIO()
    r = _make(out, 100.0, 102.5)
    with r.stage("ssh pump") as st:
        st.warn("not ready within budget")
    assert "! ssh pump — not ready within budget (2.5s)" in out.getvalue()


def test_stage_exception_includes_timing():
    out = io.StringIO()
    r = _make(out, 100.0, 101.2)
    with pytest.raises(RuntimeError):
        with r.stage("container"):
            raise RuntimeError("boom")
    assert "✗ container — RuntimeError: boom (1.2s)" in out.getvalue()


def test_keyboard_interrupt_treated_like_any_exception():
    out = io.StringIO()
    r = _make(out, 0.0, 0.3)
    with pytest.raises(KeyboardInterrupt):
        with r.stage("container"):
            raise KeyboardInterrupt()
    assert "✗ container — KeyboardInterrupt" in out.getvalue()


def test_info_after_skip_is_allowed_skip_still_dominates():
    # Defensive: skip after info shouldn't double-print on exit.
    out = io.StringIO()
    r = _make(out, 0.0, 0.4)
    with r.stage("container") as st:
        st.info("building…")
        st.skip("already running")
    output = out.getvalue()
    assert "▸ container — building…" in output
    assert "▸ container — already running" in output
    assert "✓ container" not in output


def test_stage_clean_exit_minute_boundary_no_rollover():
    """119.5s must display as (2m 0s), not (1m 60s)."""
    out = io.StringIO()
    r = _make(out, 100.0, 219.5)
    with r.stage("container"):
        pass
    output = out.getvalue()
    assert "✓ container (2m 0s)" in output
    assert "60s" not in output  # no "1m 60s" leak


def test_stage_clean_exit_just_under_60s_rounds_to_minute():
    """59.95s rounds up to 60s — must display as (1m 0s), not (60.0s)."""
    out = io.StringIO()
    r = _make(out, 100.0, 159.95)
    with r.stage("container"):
        pass
    output = out.getvalue()
    assert "✓ container (1m 0s)" in output
    assert "60.0s" not in output


def test_stage_clean_exit_at_60s_exactly():
    """Exactly 60.0s displays as 1m 0s."""
    out = io.StringIO()
    r = _make(out, 100.0, 160.0)
    with r.stage("container"):
        pass
    assert "✓ container (1m 0s)" in out.getvalue()


def test_color_emission_covers_all_four_symbols():
    """Each of the four symbols emits the matching palette ANSI fg + reset."""
    out = io.StringIO()
    # Six ticks: clean-exit enter+exit, info+skip (skip has its own enter+exit),
    # warn enter+exit, exception enter+exit.
    ticks = iter([0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    r = Reporter(out=out, color=True, clock=lambda: next(ticks))
    pal = theme.get_palette()

    with r.stage("worktree") as st:
        st.info("fetching origin/main")   # ▸ → S

    # clean exit prints "✓ worktree" → R
    # (skip path would suppress the ✓; we want both ▸ and ✓ in output)

    with r.stage("hooks") as st:
        st.warn("denied")                  # ! → W

    with pytest.raises(RuntimeError):
        with r.stage("c"):
            raise RuntimeError()           # ✗ → X

    text = out.getvalue()
    assert pal.ansi_fg[state.STARTING] in text  # ▸ is grey
    assert pal.ansi_fg[state.RUNNING] in text   # ✓ is green
    assert pal.ansi_fg[state.WAITING] in text   # ! is yellow
    assert pal.ansi_fg[state.ERRORED] in text   # ✗ is red
    assert "\x1b[0m" in text                    # reset after symbol


def test_color_off_emits_no_ansi():
    out = io.StringIO()
    r = Reporter(out=out, color=False, clock=lambda: 0.0)
    r.banner("hi")
    with r.stage("c") as st:
        st.info("x")
    assert "\x1b[" not in out.getvalue()
