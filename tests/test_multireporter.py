import io
import pytest
from tmux_agents.progress import Reporter, MultiReporter


def _make_pair():
    out_a, out_b = io.StringIO(), io.StringIO()
    ticks_a = iter([0.0, 1.5, 0.0, 1.5])
    ticks_b = iter([0.0, 1.5, 0.0, 1.5])
    ra = Reporter(out=out_a, color=False, clock=lambda: next(ticks_a))
    rb = Reporter(out=out_b, color=False, clock=lambda: next(ticks_b))
    return out_a, out_b, MultiReporter([ra, rb])


def test_banner_fanned_out_to_all_members():
    out_a, out_b, m = _make_pair()
    m.banner("Restoring agent: backend / feat/foo")
    assert "Restoring agent: backend / feat/foo" in out_a.getvalue()
    assert "Restoring agent: backend / feat/foo" in out_b.getvalue()


def test_stage_info_check_warn_fanned_out():
    out_a, out_b, m = _make_pair()
    with m.stage("container") as st:
        st.info("building…")
    assert "▸ container — building…" in out_a.getvalue()
    assert "▸ container — building…" in out_b.getvalue()
    assert "✓ container (1.5s)" in out_a.getvalue()
    assert "✓ container (1.5s)" in out_b.getvalue()


def test_warn_aggregates_had_warning_across_members():
    out_a, out_b, m = _make_pair()
    with m.stage("hooks") as st:
        st.warn("denied")
    assert "! hooks — denied (1.5s)" in out_a.getvalue()
    assert "! hooks — denied (1.5s)" in out_b.getvalue()
    assert m.had_warning is True


def test_exception_propagates_once_with_fail_written_to_all():
    out_a, out_b, m = _make_pair()
    with pytest.raises(RuntimeError, match="boom"):
        with m.stage("container"):
            raise RuntimeError("boom")
    assert out_a.getvalue().count("✗ container") == 1
    assert out_b.getvalue().count("✗ container") == 1


def test_empty_member_list_is_a_noop():
    m = MultiReporter([])
    m.banner("x")
    with m.stage("y") as st:
        st.info("z")
    assert m.had_warning is False
