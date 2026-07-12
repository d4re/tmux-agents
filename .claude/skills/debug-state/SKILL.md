---
name: debug-state
description: Debug the tmux-agents state pipeline — why a window shows the wrong letter (stale B, stuck I, unexpected X/S), where each layer's data lives on disk, and the exact read-only commands to inspect a live system.
---

# Debugging the state pipeline

The letter a window shows is derived from three layers. Debug by walking
them in order; all inspection below is read-only and safe against the
user's live server.

```
hooks (in worktree)  →  host tick (agent-state)  →  @state_code option  →  overview
```

Priority: `X > W > R > B > Z > I > S` (`phase.derive_letter`).

## 1. What does tmux think right now?

```bash
tmux -L agents list-windows -F '#{window_id} #{window_name} #{@state_code} #{@state_fg}'
tmux -L agents list-panes -s -F '#{window_id} #{pane_id} #{pane_dead}'
```

## 2. Which worktree/pane does the window map to?

```bash
cat ~/.config/tmux-agents/windows/@<N>.json   # → host_worktree, pane_id, phase_hint
```

## 3. What did the hooks write? (in that worktree)

```bash
d=<host_worktree>/.local/.tmux-agents
cat $d/state-<pane>.json          # {phase, updated_at} — running/waiting/idle/errored
ls -la $d/pending-<pane>/         # B/Z markers: wakeup, cron-oneshot__<id>,
                                  # cron-recur__<id>, subagent__<id>, bg-shell__<id>
cat $d/session-<pane>.id          # Claude session id (for --resume)
```

Marker file *content* is the expiry signal (wakeup `scheduledFor` ms, cron
expr); expiry policy lives host-side in `registry.py`.

## 4. Logs

```bash
tail -50 /tmp/tmux-agents/tmux-agents.log     # unified rotating log
cat /tmp/tmux-agents/spawn-@<N>.log           # per-window spawn progress
cat /tmp/tmux-agents/tick.cache               # last tick's fingerprint (skip cache)
```

`TMUX_AGENTS_LOG_LEVEL=DEBUG agent-state` runs one verbose tick by hand.

## Symptom table

| Symptom | Likely layer | Check |
|---|---|---|
| Always `I`, never `R`/`W` | Hooks never fire in container | `exec_cmd` missing `-e TMUX_PANE`; is `state-<pane>.json` updating at all? |
| Stale `B<N>` after work finished | Marker not reaped | `ls pending-<pane>/` — leftover `bg-shell__`/`subagent__`? Reaped by `clear-completed` (UserPromptSubmit) or `reconcile` (Stop); TTL backstop ≈30 min |
| `Z<N>` that never clears | Cron/wakeup marker expiry | Marker content vs `registry.py` policy (recurring cron = 7-day backstop; not re-registered on `--resume`) |
| Unexpected `X` | Pane died, or phase=errored | `list-panes -s` (mapping's pane_id gone?), `state-<pane>.json` phase |
| Stuck `S` | Provision worker never finished | `spawn-@<N>.log`, mapping's `phase_hint` |
| `R`/`B` flicker while subagent runs | Subagent filter regressed | `write-state.sh::is_subagent` — payload `agent_id`/`agent_type` gating |
| Letter right, color/summary wrong | Rendering | tmux-format branch in `overview.render_summary`; final print in `state_tick.main` |

## Remember

- Plain `tmux <cmd>` talks to the wrong server — always `-L agents`.
- The installed tool ≠ this checkout. If you changed code,
  `make reinstall` before trusting manual runs (see the
  `verify` skill).
- Don't mutate the live server (kill/respawn/set-option) while debugging;
  reproduce on a scratch socket instead (pattern in `tests/test_smoke.py`).
