"""Execute each hook command against a provisioned worktree and assert the
resulting state.json + pending-<pane>/ markers evolve as expected. The hook
commands all dispatch to write-state.sh, which provisioning copies into
the worktree from package data."""

import json
import os
import shutil
import subprocess
from importlib import resources
from pathlib import Path
import pytest

from tmux_agents import provisioning

TEMPLATE = resources.files("tmux_agents.hooks") / "agents.json"
SH = shutil.which("sh") or "/bin/sh"


@pytest.fixture
def project(tmp_path):
    """Worktree with the helper script provisioned."""
    provisioning.provision_settings(tmp_path, template_path=TEMPLATE)
    return tmp_path


def _hook_cmd(
    template: dict, event: str, matcher: str | None = None, index: int = -1
) -> str:
    """Return the LAST command under (event, matcher). -1 matches 'state-writing' hook
    when a notification event also contains the bell."""
    for group in template["hooks"][event]:
        if matcher is not None and group.get("matcher") != matcher:
            continue
        return group["hooks"][index]["command"]
    raise AssertionError(f"no hook command for {event} matcher={matcher}")


def _hook_cmd_containing(template: dict, event: str, needle: str) -> str:
    """Return the (matcher-less) hook command under `event` whose body contains
    `needle` — used when an event has several sibling hooks."""
    for group in template["hooks"][event]:
        for h in group["hooks"]:
            if needle in h["command"]:
                return h["command"]
    raise AssertionError(f"no hook command containing {needle!r} for {event}")


def _run(sh_command: str, env: dict) -> subprocess.CompletedProcess:
    # Build a clean env: start from os.environ, then overlay the provided env.
    # Explicitly remove TMUX_PANE when the caller did not include it, so that
    # an ambient tmux session in the outer shell doesn't leak into the subprocess.
    merged = {**os.environ, **env}
    if "TMUX_PANE" not in env:
        merged.pop("TMUX_PANE", None)
    return subprocess.run(
        [SH, "-c", sh_command], env=merged, capture_output=True, text=True
    )


def _env(project_dir: Path, pane: str = "%23") -> dict:
    return {"CLAUDE_PROJECT_DIR": str(project_dir), "TMUX_PANE": pane}


def _load_state(project_dir: Path, pane_stripped: str = "23") -> dict:
    p = project_dir / ".local" / ".tmux-agents" / f"state-{pane_stripped}.json"
    return json.loads(p.read_text())


def _pending(project_dir: Path, pane_stripped: str = "23") -> Path:
    return project_dir / ".local" / ".tmux-agents" / f"pending-{pane_stripped}"


def _run_with_input(
    sh_command: str, env: dict, payload: str
) -> subprocess.CompletedProcess:
    merged = {**os.environ, **env}
    if "TMUX_PANE" not in env:
        merged.pop("TMUX_PANE", None)
    return subprocess.run(
        [SH, "-c", sh_command],
        env=merged,
        input=payload,
        capture_output=True,
        text=True,
    )


def test_session_start_writes_idle_state(project):
    t = json.loads(TEMPLATE.read_text())
    r = _run(_hook_cmd(t, "SessionStart"), _env(project))
    assert r.returncode == 0, r.stderr
    s = _load_state(project)
    assert s["phase"] == "idle"
    assert "active_crons" not in s


def test_post_tool_use_writes_running_after_permission_prompt(project):
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run(_hook_cmd(t, "Notification", matcher="permission_prompt"), _env(project))
    assert _load_state(project)["phase"] == "waiting"
    r = _run(_hook_cmd(t, "PostToolUse"), _env(project))
    assert r.returncode == 0, r.stderr
    assert _load_state(project)["phase"] == "running"


def test_post_tool_use_failure_writes_running_after_permission_prompt(project):
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run(_hook_cmd(t, "Notification", matcher="permission_prompt"), _env(project))
    r = _run(_hook_cmd(t, "PostToolUseFailure"), _env(project))
    assert r.returncode == 0, r.stderr
    assert _load_state(project)["phase"] == "running"


def test_permission_denied_writes_running(project):
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run(_hook_cmd(t, "Notification", matcher="permission_prompt"), _env(project))
    r = _run(_hook_cmd(t, "PermissionDenied"), _env(project))
    assert r.returncode == 0, r.stderr
    assert _load_state(project)["phase"] == "running"


def test_user_prompt_submit_writes_running(project):
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    r = _run(_hook_cmd(t, "UserPromptSubmit"), _env(project))
    assert r.returncode == 0, r.stderr
    assert _load_state(project)["phase"] == "running"


def test_stop_writes_idle(project):
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run(_hook_cmd(t, "UserPromptSubmit"), _env(project))
    assert _load_state(project)["phase"] == "running"
    r = _run(_hook_cmd(t, "Stop"), _env(project))
    assert r.returncode == 0, r.stderr
    assert _load_state(project)["phase"] == "idle"


def test_stop_failure_writes_idle(project):
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run(_hook_cmd(t, "UserPromptSubmit"), _env(project))
    r = _run(_hook_cmd(t, "StopFailure"), _env(project))
    assert r.returncode == 0, r.stderr
    assert _load_state(project)["phase"] == "idle"


def test_notification_writes_waiting(project):
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    r = _run(_hook_cmd(t, "Notification", matcher="permission_prompt"), _env(project))
    assert r.returncode == 0, r.stderr
    assert _load_state(project)["phase"] == "waiting"


# ---- registry marker behaviour ----

CRON_RECUR_PAYLOAD = '{"tool_name":"CronCreate","tool_input":{"cron":"0 9 1 1 *","recurring":true},"tool_response":{"id":"aa7dd6dd","humanSchedule":"Every Jan 1 at 9am","recurring":true,"durable":false}}'
CRON_ONESHOT_PAYLOAD = '{"tool_name":"CronCreate","tool_input":{"cron":"*/2 * * * *","recurring":false},"tool_response":{"id":"c8ae5c19","humanSchedule":"Every 2 minutes","recurring":false,"durable":false}}'
CRON_DELETE_PAYLOAD = '{"tool_name":"CronDelete","tool_input":{"id":"aa7dd6dd"},"tool_response":{"id":"aa7dd6dd"}}'
WAKEUP_PAYLOAD = '{"tool_name":"ScheduleWakeup","tool_input":{"delaySeconds":60},"tool_response":{"scheduledFor":1780327440000,"clampedDelaySeconds":60,"wasClamped":false}}'
BG_SUBAGENT_PAYLOAD = '{"tool_name":"Agent","tool_input":{"run_in_background":true},"tool_response":{"isAsync":true,"agentId":"ac7ea5fc7db584902"}}'
FG_SUBAGENT_PAYLOAD = '{"tool_name":"Agent","tool_input":{"run_in_background":false},"tool_response":{"agentId":"deadbeef"}}'
BG_SHELL_PAYLOAD = '{"tool_name":"Bash","tool_input":{"command":"x","run_in_background":true},"tool_response":{"backgroundTaskId":"ba861qi9a"}}'
FG_SHELL_PAYLOAD = '{"tool_name":"Bash","tool_input":{"command":"x"},"tool_response":{"stdout":"","backgroundTaskId":""}}'
# A subagent's own tool-uses fire the parent pane's hooks (it inherits
# TMUX_PANE). Such PostToolUse payloads carry `agent_id`/`agent_type`;
# main-agent payloads never do. These are used to verify the is_subagent filter.
MAIN_BASH_PAYLOAD = '{"tool_name":"Bash","tool_input":{"command":"echo hi"},"tool_response":{"stdout":"hi"}}'
SUBAGENT_BASH_PAYLOAD = '{"tool_name":"Bash","agent_id":"a676ff53b544d7565","agent_type":"general-purpose","tool_input":{"command":"echo hi"},"tool_response":{"stdout":"hi"}}'
SUBAGENT_BG_SHELL_PAYLOAD = '{"tool_name":"Bash","agent_id":"a676ff53b544d7565","agent_type":"general-purpose","tool_input":{"command":"sleep 1","run_in_background":true},"tool_response":{"backgroundTaskId":"subsh1"}}'
SUBAGENT_BG_AGENT_PAYLOAD = '{"tool_name":"Agent","agent_id":"a676ff53b544d7565","agent_type":"general-purpose","tool_input":{"run_in_background":true},"tool_response":{"isAsync":true,"agentId":"nested1"}}'
# Background-task completion (subagent AND background Bash) arrives as a
# UserPromptSubmit whose prompt is a <task-notification> carrying the launch id
# (backgroundTaskId / agentId). This is the single teardown signal for both.
TASK_NOTIFICATION_BGSHELL = '{"hook_event_name":"UserPromptSubmit","prompt":"<task-notification> <task-id>ba861qi9a</task-id> <status>completed</status> </task-notification>"}'
TASK_NOTIFICATION_SUBAGENT = '{"hook_event_name":"UserPromptSubmit","prompt":"<task-notification> <task-id>ac7ea5fc7db584902</task-id> <status>completed</status> </task-notification>"}'


def test_add_wakeup_marker(project):
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    r = _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="ScheduleWakeup"),
        _env(project),
        WAKEUP_PAYLOAD,
    )
    assert r.returncode == 0, r.stderr
    m = _pending(project) / "wakeup"
    assert m.exists() and m.read_text().strip() == "1780327440000"


def test_add_cron_recurring_then_oneshot(project):
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="CronCreate"),
        _env(project),
        CRON_RECUR_PAYLOAD,
    )
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="CronCreate"),
        _env(project),
        CRON_ONESHOT_PAYLOAD,
    )
    assert (_pending(project) / "cron-recur__aa7dd6dd").exists()
    oneshot = _pending(project) / "cron-oneshot__c8ae5c19"
    # Marker content is the machine-readable cron expr (tool_input.cron), NOT
    # the human-readable humanSchedule — croniter needs the real expression.
    assert oneshot.exists() and oneshot.read_text().strip() == "*/2 * * * *"


def test_del_cron_removes_marker(project):
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="CronCreate"),
        _env(project),
        CRON_RECUR_PAYLOAD,
    )
    assert (_pending(project) / "cron-recur__aa7dd6dd").exists()
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="CronDelete"),
        _env(project),
        CRON_DELETE_PAYLOAD,
    )
    assert not (_pending(project) / "cron-recur__aa7dd6dd").exists()


def test_add_subagent_only_when_background(project):
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="Agent"), _env(project), FG_SUBAGENT_PAYLOAD
    )
    assert not (_pending(project) / "subagent__deadbeef").exists()
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="Agent"), _env(project), BG_SUBAGENT_PAYLOAD
    )
    assert (_pending(project) / "subagent__ac7ea5fc7db584902").exists()


def test_add_bgshell_only_when_background(project):
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="Bash"), _env(project), FG_SHELL_PAYLOAD
    )
    if _pending(project).exists():
        assert not list(_pending(project).glob("bg-shell__*"))
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="Bash"), _env(project), BG_SHELL_PAYLOAD
    )
    assert (_pending(project) / "bg-shell__ba861qi9a").exists()


def test_clear_completed_removes_bgshell_marker(project):
    """A background Bash has no completion hook of its own; its completion
    arrives as a UserPromptSubmit <task-notification> carrying the launch id."""
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="Bash"), _env(project), BG_SHELL_PAYLOAD
    )
    assert (_pending(project) / "bg-shell__ba861qi9a").exists()
    cmd = _hook_cmd_containing(t, "UserPromptSubmit", "clear-completed")
    _run_with_input(cmd, _env(project), TASK_NOTIFICATION_BGSHELL)
    assert not (_pending(project) / "bg-shell__ba861qi9a").exists()


def test_clear_completed_removes_subagent_marker(project):
    """The same <task-notification> teardown covers background subagents
    (the sole removal signal — no SubagentStop hook is wired)."""
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="Agent"), _env(project), BG_SUBAGENT_PAYLOAD
    )
    assert (_pending(project) / "subagent__ac7ea5fc7db584902").exists()
    cmd = _hook_cmd_containing(t, "UserPromptSubmit", "clear-completed")
    _run_with_input(cmd, _env(project), TASK_NOTIFICATION_SUBAGENT)
    assert not (_pending(project) / "subagent__ac7ea5fc7db584902").exists()


def test_running_skipped_for_subagent_tooluse(project):
    """A subagent's tool-use fires the catch-all `running` hook on the parent
    pane, but carries agent_id — the pane must stay idle (track main agent only),
    no B<->R flicker."""
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run_with_input(_hook_cmd(t, "PostToolUse"), _env(project), SUBAGENT_BASH_PAYLOAD)
    assert _load_state(project)["phase"] == "idle"


def test_running_still_fires_for_main_agent_tooluse(project):
    """Control: a main-agent tool-use (no agent_id) DOES set running."""
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run_with_input(_hook_cmd(t, "PostToolUse"), _env(project), MAIN_BASH_PAYLOAD)
    assert _load_state(project)["phase"] == "running"


def test_add_bgshell_skipped_for_subagent(project):
    """A Bash backgrounded BY a subagent must not pollute the parent's pending
    dir (the parent never sees its completion notification)."""
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="Bash"),
        _env(project),
        SUBAGENT_BG_SHELL_PAYLOAD,
    )
    assert not (_pending(project) / "bg-shell__subsh1").exists()


def test_add_subagent_skipped_for_nested_subagent(project):
    """A subagent launching its own (nested) background subagent must not
    register under the parent pane."""
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="Agent"),
        _env(project),
        SUBAGENT_BG_AGENT_PAYLOAD,
    )
    assert not (_pending(project) / "subagent__nested1").exists()


# The Stop / SubagentStop payload carries `background_tasks` — the session's
# live (status running|pending, backgrounded) task registry, each entry shaped
# {id, type, status, description, ...}. A task that has completed is filtered
# out by the producer, so it simply drops off this list. `reconcile` reaps any
# bg-shell__/subagent__ marker whose id is absent from this set — the
# authoritative teardown for completions that never surfaced as a
# UserPromptSubmit (e.g. delivered mid-turn as an attachment).
STOP_NO_TASKS = '{"hook_event_name":"Stop","stop_hook_active":false,"background_tasks":[],"session_crons":[]}'
STOP_BGSHELL_RUNNING = '{"hook_event_name":"Stop","stop_hook_active":false,"background_tasks":[{"id":"ba861qi9a","type":"local_bash","status":"running","description":"x","command":"x"}],"session_crons":[]}'
STOP_SUBAGENT_RUNNING = '{"hook_event_name":"Stop","stop_hook_active":false,"background_tasks":[{"id":"ac7ea5fc7db584902","type":"local_agent","status":"running","description":"x","agent_type":"general-purpose"}],"session_crons":[]}'
STOP_NO_FIELD = '{"hook_event_name":"Stop","stop_hook_active":false}'


def test_stop_reconcile_removes_completed_bgshell_marker(project):
    """A background Bash whose completion arrived mid-turn (as an attachment, not
    a UserPromptSubmit) is reaped on Stop: its id is absent from background_tasks."""
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="Bash"), _env(project), BG_SHELL_PAYLOAD
    )
    assert (_pending(project) / "bg-shell__ba861qi9a").exists()
    cmd = _hook_cmd_containing(t, "Stop", "reconcile")
    _run_with_input(cmd, _env(project), STOP_NO_TASKS)
    assert not (_pending(project) / "bg-shell__ba861qi9a").exists()


def test_stop_reconcile_removes_completed_subagent_marker(project):
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="Agent"), _env(project), BG_SUBAGENT_PAYLOAD
    )
    assert (_pending(project) / "subagent__ac7ea5fc7db584902").exists()
    cmd = _hook_cmd_containing(t, "Stop", "reconcile")
    _run_with_input(cmd, _env(project), STOP_NO_TASKS)
    assert not (_pending(project) / "subagent__ac7ea5fc7db584902").exists()


def test_stop_reconcile_keeps_running_bgshell_marker(project):
    """A genuinely still-running background Bash (agent backgrounded it then
    ended its turn) stays counted — its id is present in background_tasks."""
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="Bash"), _env(project), BG_SHELL_PAYLOAD
    )
    cmd = _hook_cmd_containing(t, "Stop", "reconcile")
    _run_with_input(cmd, _env(project), STOP_BGSHELL_RUNNING)
    assert (_pending(project) / "bg-shell__ba861qi9a").exists()


def test_stop_reconcile_keeps_running_subagent_marker(project):
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="Agent"), _env(project), BG_SUBAGENT_PAYLOAD
    )
    cmd = _hook_cmd_containing(t, "Stop", "reconcile")
    _run_with_input(cmd, _env(project), STOP_SUBAGENT_RUNNING)
    assert (_pending(project) / "subagent__ac7ea5fc7db584902").exists()


def test_stop_reconcile_skips_when_background_tasks_absent(project):
    """If the payload carries no background_tasks field (older client, or no
    app state), reconcile must NOT reap — fall back to the TTL backstop rather
    than blindly clearing markers for tasks that may still be running."""
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="Bash"), _env(project), BG_SHELL_PAYLOAD
    )
    cmd = _hook_cmd_containing(t, "Stop", "reconcile")
    _run_with_input(cmd, _env(project), STOP_NO_FIELD)
    assert (_pending(project) / "bg-shell__ba861qi9a").exists()


def test_stop_reconcile_leaves_scheduled_markers(project):
    """reconcile only governs background work (bg-shell/subagent). Scheduled
    markers (wakeup/cron) expire by their own fire time and must be untouched."""
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="ScheduleWakeup"),
        _env(project),
        WAKEUP_PAYLOAD,
    )
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="Bash"), _env(project), BG_SHELL_PAYLOAD
    )
    cmd = _hook_cmd_containing(t, "Stop", "reconcile")
    _run_with_input(cmd, _env(project), STOP_NO_TASKS)
    assert (_pending(project) / "wakeup").exists()
    assert not (_pending(project) / "bg-shell__ba861qi9a").exists()


def test_clear_completed_ignores_normal_prompt(project):
    """A real user prompt (no task-notification) must not disturb markers."""
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="Bash"), _env(project), BG_SHELL_PAYLOAD
    )
    cmd = _hook_cmd_containing(t, "UserPromptSubmit", "clear-completed")
    _run_with_input(
        cmd,
        _env(project),
        '{"hook_event_name":"UserPromptSubmit","prompt":"hello there"}',
    )
    assert (_pending(project) / "bg-shell__ba861qi9a").exists()


def test_init_clears_pending(project):
    t = json.loads(TEMPLATE.read_text())
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="ScheduleWakeup"),
        _env(project),
        WAKEUP_PAYLOAD,
    )
    assert (_pending(project) / "wakeup").exists()
    _run(_hook_cmd(t, "SessionStart"), _env(project))
    assert not (_pending(project) / "wakeup").exists()


def test_session_end_cleans_up(project):
    t = json.loads(TEMPLATE.read_text())
    payload = '{"session_id":"01234567-89ab-cdef-0123-456789abcdef","source":"startup","cwd":"/x"}'
    _run_with_input(_hook_cmd(t, "SessionStart"), _env(project), payload)
    _run_with_input(
        _hook_cmd(t, "PostToolUse", matcher="ScheduleWakeup"),
        _env(project),
        WAKEUP_PAYLOAD,
    )
    base = project / ".local" / ".tmux-agents"
    assert (base / "session-23.id").exists()
    _run(_hook_cmd(t, "SessionEnd"), _env(project))
    assert not (base / "state-23.json").exists()
    assert not (base / "pending-23").exists()
    assert not (base / "session-23.id").exists()


def test_missing_tmux_pane_noops(project):
    t = json.loads(TEMPLATE.read_text())
    base = project / ".local" / ".tmux-agents"
    for f in base.iterdir():
        if f.name != "write-state.sh":
            if f.is_dir():
                shutil.rmtree(f)
            else:
                f.unlink()
    env = {"CLAUDE_PROJECT_DIR": str(project)}
    r = _run(_hook_cmd(t, "UserPromptSubmit"), env)
    assert r.returncode == 0
    assert not (base / "state-23.json").exists()


def test_session_start_extracts_session_id_from_stdin(project):
    t = json.loads(TEMPLATE.read_text())
    payload = '{"session_id":"01234567-89ab-cdef-0123-456789abcdef","source":"startup","cwd":"/x"}'
    r = _run_with_input(_hook_cmd(t, "SessionStart"), _env(project), payload)
    assert r.returncode == 0, r.stderr
    sid_file = project / ".local" / ".tmux-agents" / "session-23.id"
    assert sid_file.exists()
    assert sid_file.read_text().strip() == "01234567-89ab-cdef-0123-456789abcdef"


def test_session_start_with_garbage_stdin_writes_no_id(project):
    t = json.loads(TEMPLATE.read_text())
    r = _run_with_input(_hook_cmd(t, "SessionStart"), _env(project), "not json")
    assert r.returncode == 0, r.stderr
    assert not (project / ".local" / ".tmux-agents" / "session-23.id").exists()


def test_session_start_with_payload_missing_session_id_writes_no_id(project):
    t = json.loads(TEMPLATE.read_text())
    r = _run_with_input(
        _hook_cmd(t, "SessionStart"), _env(project), '{"source":"startup","cwd":"/x"}'
    )
    assert r.returncode == 0, r.stderr
    assert not (project / ".local" / ".tmux-agents" / "session-23.id").exists()
