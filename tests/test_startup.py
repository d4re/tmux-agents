import pytest

from tmux_agents import paths, startup, tmux, windows


def _tmux_error(stderr):
    return tmux.TmuxError(
        1, ["tmux", "-L", "agents", "respawn-pane"], output="", stderr=stderr
    )


def _fork_failure():
    return _tmux_error("respawn pane failed: fork failed: Device not configured")


def test_placeholder_command_is_tail_F(tmp_state_dir):
    cmd = startup.placeholder_command("@9")
    assert cmd.startswith("tail -F ")
    assert str(paths.spawn_log("@9")) in cmd


def test_placeholder_command_pre_creates_log(tmp_state_dir):
    """The log must exist before the pane runs `tail -F`, or tail prints
    "cannot open …: No such file or directory" / "has appeared" into the pane
    while the detached worker is still starting up."""
    log = paths.spawn_log("@9")
    assert not log.exists()
    startup.placeholder_command("@9")
    assert log.exists()


class _FakePanes:
    """Fake tmux pane surface for `startup.respawn`: per-pane pid (-1 =
    poisoned), scripted respawn/split outcomes, and a record of every call."""

    def __init__(self, monkeypatch, *, pids=None, respawn_errors=(), split_errors=()):
        self.pids = dict(pids or {})
        self.respawn_errors = list(respawn_errors)
        self.split_errors = list(split_errors)
        self.respawned: list[tuple[str, str]] = []
        self.splits: list[tuple[str, str, str | None]] = []
        self.killed: list[str] = []
        monkeypatch.setattr(tmux, "pane_spawn_info", self.info)
        monkeypatch.setattr(tmux, "respawn_pane", self.respawn_pane)
        monkeypatch.setattr(tmux, "split_window", self.split_window)
        monkeypatch.setattr(tmux, "kill_pane", self.killed.append)
        monkeypatch.setattr(startup.time, "sleep", lambda s: None)

    def info(self, pane_id):
        if pane_id not in self.pids:
            return None
        return tmux.PaneSpawnInfo("@4", self.pids[pane_id], "/start")

    def respawn_pane(self, pane_id, *, command):
        self.respawned.append((pane_id, command))
        if self.respawn_errors:
            raise self.respawn_errors.pop(0)

    def split_window(self, target, *, percent, command, cwd=None, **kw):
        self.splits.append((target, command, cwd))
        if self.split_errors:
            raise self.split_errors.pop(0)
        return "%50"


def test_respawn_healthy_pane_returns_same_id(monkeypatch):
    fake = _FakePanes(monkeypatch, pids={"%13": 77})
    assert startup.respawn("%13", "tail -F log") == "%13"
    assert fake.respawned == [("%13", "tail -F log")]
    assert fake.splits == [] and fake.killed == []


def test_respawn_does_not_retry_non_fork_error(monkeypatch):
    """A respawn failure that is NOT a fork failure (e.g. can't find pane)
    is a real error — raise immediately, no replacement."""
    fake = _FakePanes(
        monkeypatch,
        pids={"%13": 77},
        respawn_errors=[_tmux_error("can't find pane: %13")],
    )
    with pytest.raises(tmux.TmuxError):
        startup.respawn("%13", "tail -F log")
    assert len(fake.respawned) == 1
    assert fake.splits == []


def test_respawn_fork_failure_replaces_pane_never_respawns_it_again(monkeypatch):
    """The 2026-10-12 crash: tmux <= 3.6a leaves a pane whose respawn failed
    to fork with a NULL input context, and respawning it again segfaults the
    whole server. The fork failure must be salvaged by a replacement split
    (same command, same start dir) + killing the poisoned pane."""
    fake = _FakePanes(monkeypatch, pids={"%13": 77}, respawn_errors=[_fork_failure()])
    assert startup.respawn("%13", "tail -F log") == "%50"
    assert [p for p, _ in fake.respawned] == ["%13"]  # exactly once
    assert fake.splits == [("%13", "tail -F log", "/start")]
    assert fake.killed == ["%13"]


def test_respawn_poisoned_pane_is_replaced_without_respawning(monkeypatch):
    """A pane already poisoned by an earlier process (pane_pid == -1), e.g.
    an errored placeholder `agent-restore` reactivates, is never respawned."""
    fake = _FakePanes(monkeypatch, pids={"%13": -1})
    assert startup.respawn("%13", "tail -F log") == "%50"
    assert fake.respawned == []
    assert fake.killed == ["%13"]


def test_respawn_retries_split_on_fork_failure(monkeypatch):
    """A failed split removes its own pane cleanly, so retrying it is safe."""
    fake = _FakePanes(
        monkeypatch,
        pids={"%13": -1},
        split_errors=[_fork_failure(), _fork_failure()],
    )
    assert startup.respawn("%13", "tail -F log") == "%50"
    assert len(fake.splits) == 3
    assert fake.killed == ["%13"]


def test_respawn_gives_up_after_max_split_attempts(monkeypatch):
    """Persistent fork failures re-raise so the caller can fall back to its
    skip-and-log handling; the poisoned pane is left alone (killing it
    could close the window and orphan its mapping)."""
    fake = _FakePanes(
        monkeypatch,
        pids={"%13": -1},
        split_errors=[_fork_failure()] * startup._FORK_RETRY_ATTEMPTS,
    )
    with pytest.raises(tmux.TmuxError):
        startup.respawn("%13", "tail -F log")
    assert len(fake.splits) == startup._FORK_RETRY_ATTEMPTS
    assert fake.killed == []


def _write_dual_mapping(worktree, *, secondary):
    windows.write_mapping(
        windows.WindowMapping(
            window_id="@4",
            project="p",
            branch=None,
            host_worktree=worktree,
            pane_id="13",
            agents=[
                windows.AgentSlot(kind="claude", pane_id="13", session_id="s0"),
                secondary,
            ],
        )
    )


def test_replacement_moves_mapping_slot_and_pane_files(
    monkeypatch, tmp_state_dir, tmp_path
):
    """The mapping is authoritative for which pane a slot lives in (the tick
    reads `state-<pane>.json` by it), so the replacement must take the slot
    — and the old pane's state + captured session id — with it."""
    wt = tmp_path / "wt"
    _write_dual_mapping(wt, secondary=windows.AgentSlot(kind="codex", pane_id="14"))
    old_state = paths.worktree_state_file(wt, "13")
    old_state.parent.mkdir(parents=True, exist_ok=True)
    old_state.write_text('{"phase": "starting"}')
    paths.worktree_session_id_file(wt, "13").write_text("sid-13")
    stale_new = paths.worktree_pending_dir(wt, "50")
    stale_new.mkdir(parents=True)  # recycled id leftovers must be scrubbed
    _FakePanes(monkeypatch, pids={"%13": -1})

    assert startup.respawn("%13", "cmd") == "%50"

    m = windows.read_mapping("@4")
    assert m.pane_id == "50"
    assert [s.pane_id for s in m.agents] == ["50", "14"]
    assert m.default_slot.session_id == "s0"
    assert not old_state.exists()
    assert paths.worktree_state_file(wt, "50").read_text() == '{"phase": "starting"}'
    assert paths.worktree_session_id_file(wt, "50").read_text() == "sid-13"
    assert not stale_new.exists()


def test_replacement_revives_death_marked_secondary(
    monkeypatch, tmp_state_dir, tmp_path
):
    """The tick death-marks a secondary whose pane looks dead (a poisoned
    pane does): pane_id None, last_pane_id kept. The replacement must
    revive that slot onto the new pane rather than miss it."""
    wt = tmp_path / "wt"
    _write_dual_mapping(
        wt,
        secondary=windows.AgentSlot(
            kind="codex", pane_id=None, session_id="s1", last_pane_id="14"
        ),
    )
    _FakePanes(monkeypatch, pids={"%14": -1})

    assert startup.respawn("%14", "cmd") == "%50"

    sec = windows.read_mapping("@4").secondary_slot
    assert (sec.pane_id, sec.last_pane_id, sec.session_id) == ("50", None, "s1")


def test_show_static_text_on_poisoned_pane_replaces_it(monkeypatch):
    """The failure notice after a fork failure is the other crash path: it
    must go through the replacement too and report the pane it landed in."""
    fake = _FakePanes(monkeypatch, pids={"%5": -1})
    assert startup.show_static_text("%5", "\n  boom\n") == "%50"
    assert fake.respawned == []
    assert "boom" in fake.splits[0][1]


def test_detach_stdio_redirects_std_fds_to_devnull():
    """--background must redirect fd 0/1/2 to /dev/null. Otherwise a child
    launched via tmux `run-shell` keeps run-shell's capture pipe as stdout and
    tmux paints the child's output (e.g. `devcontainer up` JSON) over the
    active pane."""

    class FakeOS:
        O_RDWR = 2
        devnull = "/dev/null"

        def __init__(self):
            self.dup2_calls: list[tuple[int, int]] = []
            self.closed: list[int] = []

        def open(self, path, flags):
            assert path == "/dev/null" and flags == self.O_RDWR
            return 7

        def dup2(self, src, dst):
            self.dup2_calls.append((src, dst))

        def close(self, fd):
            self.closed.append(fd)

    fake = FakeOS()
    startup._detach_stdio(_os=fake)
    assert fake.dup2_calls == [(7, 0), (7, 1), (7, 2)]
    assert fake.closed == [7]  # devnull fd > 2 closed after duping onto std fds


def test_show_static_text_respawns_heredoc(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tmux,
        "respawn_pane",
        lambda pane_id, *, command: calls.append((pane_id, command)),
    )
    startup.show_static_text("%5", "\n  hello world\n")
    assert len(calls) == 1
    pid, cmd = calls[0]
    assert pid == "%5"
    assert "hello world" in cmd
    assert cmd.startswith('sh -c \'cat <<"EOF"')


def test_hold_pane_then_exec_shows_log_and_execs(monkeypatch, tmp_state_dir):
    calls = []
    monkeypatch.setattr(
        tmux,
        "respawn_pane",
        lambda pane_id, *, command: calls.append((pane_id, command)),
    )
    log = paths.spawn_log("@1")
    startup.hold_pane_then_exec("%7", log, "claude --resume abc")
    assert len(calls) == 1
    pid, cmd = calls[0]
    assert pid == "%7"
    assert str(log) in cmd  # prints the startup log
    assert "press Enter" in cmd  # the hold prompt
    assert "read" in cmd  # waits for Enter
    assert "claude --resume abc" in cmd  # then launches Claude


def test_hold_pane_then_exec_does_not_exec_prefix_a_cd_command(
    monkeypatch, tmp_state_dir
):
    """The real exec_cmd starts with `cd <workdir> &&`. Prefixing it with
    `exec` makes sh look for a `cd` binary, fail 127, and kill the pane the
    moment the user acknowledges the warning — Claude never starts."""
    calls = []
    monkeypatch.setattr(
        tmux,
        "respawn_pane",
        lambda pane_id, *, command: calls.append((pane_id, command)),
    )
    real_exec_cmd = "cd /repo/.worktrees/feat/x && exec claude"
    startup.hold_pane_then_exec("%7", paths.spawn_log("@1"), real_exec_cmd)
    _, cmd = calls[0]
    assert "exec cd" not in cmd
    assert cmd.rstrip("'").endswith(real_exec_cmd)


def test_scrub_pane_files_removes_all_three(tmp_path):
    d = tmp_path / ".local" / ".tmux-agents"
    d.mkdir(parents=True)
    (d / "state-12.json").write_text("{}")
    (d / "session-12.id").write_text("x")
    (d / "pending-12").mkdir()
    (d / "pending-12" / "wakeup").write_text("")
    startup.scrub_pane_files(tmp_path, "12")
    assert not (d / "state-12.json").exists()
    assert not (d / "session-12.id").exists()
    assert not (d / "pending-12").exists()


def test_scrub_pane_files_tolerates_absence(tmp_path):
    startup.scrub_pane_files(tmp_path, "99")  # no raise


def test_pane_files_absent_true_when_nothing_on_disk(tmp_path):
    assert startup.pane_files_absent(tmp_path, "12") is True


def test_pane_files_absent_false_when_state_file_survives(tmp_path):
    d = tmp_path / ".local" / ".tmux-agents"
    d.mkdir(parents=True)
    (d / "state-12.json").write_text("{}")
    assert startup.pane_files_absent(tmp_path, "12") is False


def test_pane_files_absent_false_when_pending_dir_survives(tmp_path):
    d = tmp_path / ".local" / ".tmux-agents" / "pending-12"
    d.mkdir(parents=True)
    assert startup.pane_files_absent(tmp_path, "12") is False


def test_pane_files_absent_true_after_scrub(tmp_path):
    d = tmp_path / ".local" / ".tmux-agents"
    d.mkdir(parents=True)
    (d / "state-12.json").write_text("{}")
    (d / "session-12.id").write_text("x")
    (d / "pending-12").mkdir()
    startup.scrub_pane_files(tmp_path, "12")
    assert startup.pane_files_absent(tmp_path, "12") is True
