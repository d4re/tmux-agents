#!/usr/bin/env sh
# tmux-agents Codex hook. Provisioned OUTSIDE every workspace (host:
# ~/.config/tmux-agents/, container: <home>/.codex/tmux-agents/) and
# referenced from user-level ~/.codex/hooks.json — the trusted hook
# definition never executes workspace-controlled code.
#
# Usage: codex-hook.sh <init|running|waiting|idle>   (payload JSON on stdin)
#
# Guards: TMUX_PANE set, $PWD/.local/.tmux-agents exists (exec templates
# `cd {workdir}` first, so cwd = worktree root), and TMUX_AGENTS_AGENT=1
# (exported only by agent exec templates — a manual codex inside
# agent-terminal must not touch agent state).
set -eu

[ "${TMUX_AGENTS_AGENT:-}" = "1" ] || exit 0
[ -n "${TMUX_PANE:-}" ] || exit 0
d="$PWD/.local/.tmux-agents"
[ -d "$d" ] || exit 0
[ $# -ge 1 ] || exit 0

p="${TMUX_PANE#%}"
state="$d/state-$p.json"
pin="$d/session-$p.id"

payload=$(cat 2>/dev/null | tr '\n' ' ')

extract() {
  printf '%s' "$payload" | \
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([0-9a-zA-Z_-]\{1,\}\)\".*/\1/p"
}

sid=$(extract session_id)

write_state() {
  t=$(mktemp "$d/.tmp.XXXXXX")
  printf '{"phase":"%s","updated_at":"%s"}\n' "$1" "$(date -u +%FT%TZ)" > "$t"
  mv "$t" "$state"
}

pinned=""
[ -f "$pin" ] && pinned=$(cat "$pin" 2>/dev/null || printf '')

case "$1" in
  init)
    src=$(extract source)
    # A root re-`startup` is impossible in an exec'd pane (exit kills the
    # pane) — a differing-pin startup can only be a nested CLI. Drop it.
    if [ "$src" = "startup" ] && [ -n "$pinned" ] && [ "$pinned" != "$sid" ]; then
      exit 0
    fi
    [ -n "$sid" ] && printf '%s\n' "$sid" > "$pin"
    # compact fires SessionStart mid-turn: pin only, never fake idle.
    case "$src" in
      startup|new|clear|resume) write_state idle ;;
      *) : ;;
    esac
    ;;
  running|waiting|idle)
    # Session-id pinning: a mismatching event belongs to a subagent or
    # stale session — it may corrupt neither phase nor pin.
    if [ -n "$pinned" ] && [ -n "$sid" ] && [ "$pinned" != "$sid" ]; then
      exit 0
    fi
    # Absent-pin capture (release-gate decision: ENABLED, observed in the
    # field): the first attributable event after a trust-skipped
    # SessionStart adopts that event's session id as the pin, so tracking
    # starts mid-session instead of waiting for the next SessionStart.
    # Residual risk if that first event is subagent-owned with a distinct
    # id: the child id gets pinned and root events drop until the next
    # SessionStart re-pins (visible as a stuck letter; /new heals it).
    # An event with no session id at all stays unattributable: write
    # nothing rather than track blind.
    if [ -z "$pinned" ]; then
      [ -n "$sid" ] || exit 0
      printf '%s\n' "$sid" > "$pin"
    fi
    [ "$1" = "waiting" ] && printf '\a'
    write_state "$1"
    ;;
esac
exit 0
