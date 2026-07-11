#!/usr/bin/env sh
# tmux-agents per-pane state writer + background/scheduled marker registry.
# Provisioned into <worktree>/.local/.tmux-agents/ by `agent-new`. Invoked by
# the Claude Code hooks defined in this project's settings.local.json.
#
# Usage:
#   write-state.sh <phase>      # idle | running | waiting  (writes state JSON)
#   write-state.sh init         # clear pending markers, write idle, grab session id
#   write-state.sh cleanup      # remove state + session id + pending markers
#   write-state.sh add-wakeup   # PostToolUse ScheduleWakeup: marker from scheduledFor
#   write-state.sh add-cron     # PostToolUse CronCreate: oneshot/recur marker
#   write-state.sh del-cron     # PostToolUse CronDelete: remove cron marker
#   write-state.sh add-subagent # PostToolUse Agent: marker iff run_in_background
#   write-state.sh add-bgshell  # PostToolUse Bash: marker iff run_in_background
#   write-state.sh clear-completed # UserPromptSubmit: reap markers on <task-notification>
#   write-state.sh reconcile    # Stop/StopFailure: reap bg markers absent from background_tasks
#
# The add-/del- commands read the Claude hook JSON payload on stdin and extract
# ids/signals with constrained sed (no jq). All counting, expiry, and cron
# parsing happen host-side in tmux_agents.registry — this script just writes
# and removes marker files.
set -eu

[ -n "${TMUX_PANE:-}" ] || exit 0
[ $# -ge 1 ] || exit 0

p="${TMUX_PANE#%}"
d="$CLAUDE_PROJECT_DIR/.local/.tmux-agents"
state="$d/state-$p.json"
pending="$d/pending-$p"

mkdir -p "$d"

write_state() {
  t=$(mktemp "$d/.tmp.XXXXXX")
  printf '{"phase":"%s","updated_at":"%s"}\n' "$1" "$(date -u +%FT%TZ)" > "$t"
  mv "$t" "$state"
}

# write_marker <name> <signal> — create the registry marker file <name> in the
# pending dir, holding <signal> as content (empty file when no signal). The
# file's mtime is its creation time; tmux_agents.registry reads both.
write_marker() {
  mkdir -p "$pending"
  printf '%s' "$2" > "$pending/$1"
}

# Read all of stdin (PostToolUse payloads can exceed 4 KiB and put
# tool_response last), flattened to one line so the field extractors match
# regardless of pretty-printing.
read_payload() { cat 2>/dev/null | tr '\n' ' '; }

# Extract a string field whose value is alphanumeric (ids, agentId, etc).
extract_str() {
  printf '%s' "$1" | \
    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\([0-9a-zA-Z_-]\{1,\}\)\".*/\1/p"
}
# Extract an integer field (e.g. scheduledFor epoch ms).
extract_num() {
  printf '%s' "$1" | \
    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p"
}
# Extract the machine-readable 5-field cron expression (tool_input.cron, NOT
# the human-readable humanSchedule, which can be prose like "Every 2 minutes").
# Uses | as the sed delimiter so the literal / inside the value needs no escaping.
extract_cron() {
  printf '%s' "$1" | \
    sed -n 's|.*"cron"[[:space:]]*:[[:space:]]*"\([0-9*/, -]\{1,\}\)".*|\1|p'
}
is_background() {
  printf '%s' "$1" | grep -q '"run_in_background"[[:space:]]*:[[:space:]]*true'
}
# A subagent inherits the parent's TMUX_PANE, so its own tool-uses fire the
# parent pane's hooks. Those PostToolUse payloads carry `agent_id`/`agent_type`;
# main-agent payloads never do. Skip them so the pane tracks the MAIN agent only
# (no B<->R flicker, no subagent-initiated markers polluting the parent). Gate on
# presence so a build without the field degrades to current behaviour.
is_subagent() {
  printf '%s' "$1" | grep -q '"agent_id"[[:space:]]*:'
}
is_recurring() {
  printf '%s' "$1" | grep -q '"recurring"[[:space:]]*:[[:space:]]*true'
}

case "$1" in
  init)
    rm -rf "$pending"
    write_state idle
    # Capture Claude's session_id from the stdin payload (top-level UUID).
    input=$(dd bs=4096 count=1 2>/dev/null) || input=""
    session_id=$(printf '%s' "$input" | \
      sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([0-9a-fA-F-]\{36\}\)".*/\1/p')
    if [ -n "$session_id" ]; then
      printf '%s\n' "$session_id" > "$d/session-$p.id"
    fi
    ;;
  cleanup)
    rm -rf "$pending"
    rm -f "$state" "$d/session-$p.id"
    ;;
  add-wakeup)
    payload=$(read_payload)
    sf=$(extract_num "$payload" scheduledFor)
    [ -n "$sf" ] || exit 0
    write_marker wakeup "$sf"
    ;;
  add-cron)
    payload=$(read_payload)
    id=$(extract_str "$payload" id)
    [ -n "$id" ] || exit 0
    sched=$(extract_cron "$payload")
    if is_recurring "$payload"; then kind=cron-recur; else kind=cron-oneshot; fi
    write_marker "${kind}__${id}" "$sched"
    ;;
  del-cron)
    payload=$(read_payload)
    id=$(extract_str "$payload" id)
    [ -n "$id" ] || exit 0
    rm -f "$pending/cron-oneshot__${id}" "$pending/cron-recur__${id}"
    ;;
  add-subagent)
    payload=$(read_payload)
    is_subagent "$payload" && exit 0   # nested subagent — don't track under parent
    is_background "$payload" || exit 0
    aid=$(extract_str "$payload" agentId)
    [ -n "$aid" ] || exit 0
    write_marker "subagent__${aid}" ""
    ;;
  add-bgshell)
    payload=$(read_payload)
    is_subagent "$payload" && exit 0   # subagent's bg shell — parent never sees its completion
    is_background "$payload" || exit 0
    bid=$(extract_str "$payload" backgroundTaskId)
    [ -n "$bid" ] || exit 0
    write_marker "bg-shell__${bid}" ""
    ;;
  clear-completed)
    # Background subagents AND background Bash have no dedicated completion
    # hook; both signal completion via a UserPromptSubmit whose prompt is a
    # <task-notification> carrying the launch id (== agentId / backgroundTaskId).
    # One handler reaps either marker type. rm -f is idempotent, so unrelated
    # ids no-op.
    payload=$(read_payload)
    case "$payload" in *"<task-notification>"*) ;; *) exit 0 ;; esac
    ids=$(printf '%s' "$payload" \
      | grep -o '<task-id>[A-Za-z0-9_-]\{1,\}</task-id>' \
      | sed 's|<task-id>||; s|</task-id>||')
    for id in $ids; do
      rm -f "$pending/bg-shell__${id}" "$pending/subagent__${id}"
    done
    ;;
  reconcile)
    # Stop / StopFailure carry `background_tasks`: the session's live (status
    # running|pending, backgrounded) task registry, each entry shaped
    # {id,type,status,...}. A task that has completed is filtered out by the
    # producer, so it simply drops off the list. Reap any bg-shell__/subagent__
    # marker whose id is absent from this set — the authoritative teardown for a
    # completion that never surfaced as a UserPromptSubmit (e.g. delivered
    # mid-turn as an attachment, which fires no hook). Scheduled markers
    # (wakeup/cron) are time-based and deliberately untouched here.
    payload=$(read_payload)
    # Only reconcile when the live set is actually present. An absent field means
    # "unknown" (older client / no app state) — fall back to the TTL backstop
    # rather than clearing markers for tasks that may still be running.
    case "$payload" in *'"background_tasks"'*) ;; *) exit 0 ;; esac
    [ -d "$pending" ] || exit 0
    # Collect every task id present in the payload. A superset (e.g. a cron id,
    # or an "id" inside a command string) is harmless: it can only *retain* a
    # marker, never wrongly drop a live one — every live task carries its "id".
    live=$(printf '%s' "$payload" \
      | grep -o '"id"[[:space:]]*:[[:space:]]*"[0-9a-zA-Z_-]\{1,\}"' \
      | sed 's/.*"\([0-9a-zA-Z_-]\{1,\}\)"$/\1/' | tr '\n' ' ')
    for f in "$pending"/bg-shell__* "$pending"/subagent__*; do
      [ -e "$f" ] || continue          # glob did not match — skip the literal
      id=${f##*__}
      case " $live " in *" $id "*) ;; *) rm -f "$f" ;; esac
    done
    ;;
  *)
    # PostToolUse `running` also fires for a subagent's tool-uses (it shares the
    # parent's pane); skip those so the pane reflects the main agent's phase.
    is_subagent "$(read_payload)" && exit 0
    write_state "$1"
    ;;
esac
