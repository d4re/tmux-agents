import os
from tmux_agents import registry, state


def _pending(tmp_path):
    d = tmp_path / ".local" / ".tmux-agents" / "pending-23"
    d.mkdir(parents=True)
    return d


def _mark(d, name, content="", *, mtime=None):
    f = d / name
    f.write_text(content)
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return f


def test_missing_dir_is_zero(tmp_path):
    c = registry.scan(tmp_path, "23", now=1000.0)
    assert c == registry.Counts(background=0, sleeping=0)


def test_live_wakeup_counts_as_sleeping(tmp_path):
    d = _pending(tmp_path)
    # scheduledFor is epoch MILLISECONDS; fire 100s in the future.
    _mark(d, "wakeup", str(int((1000.0 + 100) * 1000)))
    c = registry.scan(tmp_path, "23", now=1000.0)
    assert c == registry.Counts(background=0, sleeping=1)


def test_expired_wakeup_is_removed(tmp_path):
    d = _pending(tmp_path)
    f = _mark(d, "wakeup", str(int((1000.0 - 100) * 1000)))  # fired in the past
    c = registry.scan(tmp_path, "23", now=1000.0)
    assert c.sleeping == 0
    assert not f.exists()


def test_cron_oneshot_uses_next_fire(tmp_path):
    d = _pending(tmp_path)
    # created at epoch 0 (1970-01-01 local). Daily 00:00 cron => next fire is
    # the next local midnight, comfortably in the future relative to now=100.
    _mark(d, "cron-oneshot__abc", "0 0 * * *", mtime=0.0)
    c = registry.scan(tmp_path, "23", now=100.0)
    assert c.sleeping == 1


def test_cron_recur_lives_until_7d_backstop(tmp_path):
    d = _pending(tmp_path)
    created = 1000.0
    _mark(d, "cron-recur__abc", "0 0 * * *", mtime=created)
    assert registry.scan(tmp_path, "23", now=created + 3600).sleeping == 1
    assert registry.scan(tmp_path, "23", now=created + 8 * 24 * 3600).sleeping == 0


def test_bg_shell_counts_as_background_then_times_out(tmp_path):
    d = _pending(tmp_path)
    created = 1000.0
    _mark(d, "bg-shell__sh1", mtime=created)
    assert registry.scan(tmp_path, "23", now=created + 60).background == 1
    assert registry.scan(tmp_path, "23", now=created + registry.BG_SHELL_TTL + 1).background == 0


def test_subagent_counts_as_background(tmp_path):
    d = _pending(tmp_path)
    _mark(d, "subagent__a1", mtime=1000.0)
    assert registry.scan(tmp_path, "23", now=1000.0 + 60).background == 1


def test_unknown_kind_is_dropped(tmp_path):
    d = _pending(tmp_path)
    f = _mark(d, "bogus__x", mtime=1000.0)
    c = registry.scan(tmp_path, "23", now=1000.0)
    assert c == registry.Counts(0, 0)
    assert not f.exists()


def test_mixed_counts(tmp_path):
    d = _pending(tmp_path)
    _mark(d, "wakeup", str(int((1000.0 + 100) * 1000)))
    _mark(d, "subagent__a1", mtime=1000.0)
    _mark(d, "bg-shell__sh1", mtime=1000.0)
    c = registry.scan(tmp_path, "23", now=1000.0)
    assert c.background == 2 and c.sleeping == 1


def test_class_map_matches_state_letters():
    assert registry._CLASS[registry.WAKEUP] == state.SLEEPING
    assert registry._CLASS[registry.SUBAGENT] == state.BACKGROUND
