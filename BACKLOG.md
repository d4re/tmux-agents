# Backlog

Potential additions — none are currently planned. Keep this file honest: if
an item is being worked on, delete it from here.

## Dev-link helper

A small `make dev-link` (or `scripts/dev-link.sh`) that symlinks
`~/.config/tmux-agents/agents.conf` to the repo copy and `uv tool install`s
in editable mode. Goal: edits to `agents.conf` or Python source are live
without re-running `install.sh`.

Sketch:
```makefile
dev-link:
	ln -sf $(PWD)/agents.conf $(HOME)/.config/tmux-agents/agents.conf
	uv tool install --reinstall --editable .
```

Tradeoff: convenient for development, meaningless for end users. Keep it
opt-in and document it as a developer-only target.

**Multi-worktree hazard (why this stayed in the backlog when the
Makefile was added):** with several agents working in different
`.worktrees/` checkouts at once, each `dev-link` run silently repoints the
single live symlink and the single editable install at *that* checkout —
whoever ran it last hijacks the live setup, and the symlink keeps tracking
that worktree's future edits. The copy-based `make reinstall` /
`make conf-sync` have a one-shot version of the same problem but at least
stop tracking. Any implementation needs an answer for this (e.g. refuse to
run from inside `.worktrees/`).

## Devcontainer for this repo

Planned: develop tmux-agents itself inside a devcontainer like the other
projects. When building it, the image needs the dev-loop tools: `make`
(the `Makefile` is the canonical command surface), `uv`, `tmux` (for
`test_smoke.py`), and git. Note the wrinkle that `make reinstall` /
`make conf-sync` mutate *host* state (the installed uv tool, the live
`~/.config/tmux-agents/`) — from inside a container those either need the
relevant paths mounted or must stay host-only steps.

## Cleaner window-status-format

The default tmux window list shows raw window names, which can get long when
Claude's pane title is verbose (e.g. `pricing:* Claude Code`). If this gets
noisy in practice, customize `window-status-format` /
`window-status-current-format` in `agents.conf` to truncate or reformat.

Only worth doing once there's real in-use pain — premature tuning is likely
to be wrong.

## Session recovery on container restart

Today, container restart kills the agent's tmux pane and the SSH agent pump
together; user runs `agent-new` again. Investigate re-attaching existing agent
panes and re-spawning the SSH pump automatically when a container comes back up.

## SSH agent forwarding over non-docker exec transports

The current `ssh_forward` module hardcodes `docker exec` as the wire.
Investigate parameterising the transport so podman, `kubectl exec`
(for remote k8s dev pods), or other exec-capable runtimes can be
plugged in without forking the relay/pump scripts.

## `splice()` leftover-thread race when raw side EOFs first

When an in-container client closes its UDS connection before the host pump
responds, the relay's `framed_to_raw` thread stays blocked in
`read_frame(stdin)` until the peer's eventual return-sentinel arrives.
If a *new* in-container client connects within that window
(milliseconds), the leftover thread can race the new splice's
`framed_to_raw` for the host pump's response sentinel; the leftover
wins, eats the bytes, and tries to `sendall` to the now-closed prior
socket. In practice this requires two SSH agent ops within a few ms
of each other in the same container; queueing at `accept(1)` makes
this rare. Fix: wait for the peer-sentinel round-trip before
`splice()` returns, or allocate a fresh per-op pipe pair so the
leftover thread is forcibly EOF'd.
## Per-agent context/token telemetry in the overview

marmonitor and codex-hud show that context-remaining and token usage are
readable from Claude/Codex on-disk session files (Claude project JSONL
transcripts; Codex `~/.codex` rollout JSONL). Since hooks already capture
each pane's `session_id`, we know the exact file — none of the
process-to-session binding fragility those tools fight. A "context 73%"
column in the overview would be genuinely useful with 6 agents. Poll cost:
one file stat/tail per pane per tick (or on a slower cadence).

## On-state-change user hook

ccmanager fires a user-configured command on session state transitions
(e.g. desktop notification when an agent flips to `W`). The state tick
already computes transitions (the tick.cache fingerprint); exposing an
optional `on_state_change` command (projects.toml top-level) that runs
with old/new letter + window name as args would be cheap.

## Bell/sound on turn-complete

tmux-agent-status plays a sound when an agent finishes a turn. We already
`printf '\a'` on permission prompts; an opt-in bell (or command) on
`Stop` → idle would let the user look away from the screen entirely.

## Agent-launch marker for Claude hooks (`write-state.sh`)

The Codex hook script (codex-support spec) requires `TMUX_AGENTS_AGENT=1`
in the environment so a manual `codex` run inside `agent-terminal`
(`Ctrl-Space T`) — which deliberately propagates `TMUX_PANE` and sits in
the worktree — can't write phases under the focused agent pane or
overwrite its session pin. The same latent exposure exists for a manual
`claude` run in that popup: worktree hooks key off `TMUX_PANE` alone.
Extending the marker to `write-state.sh` closes it, but existing live
panes were spawned without the variable and would go dark until
respawned — needs a migration story (e.g. gate only when the variable
is *present but not "1"*, then flip to hard-require a release later).

## Config-tunable registry TTLs

`tmux_agents/registry.py` hardcodes `WAKEUP_GRACE`, `CRON_ONESHOT_GRACE`,
`SUBAGENT_TTL`, `BG_SHELL_TTL` (and the 7-day `CRON_RECUR_TTL` backstop).
Expose these as a top-level `[registry]` table in an existing config file
(projects.toml) rather than a new file, per the reuse-config preference.
Only `bg-shell` and the `subagent` backstop are genuine heuristics — the
rest derive real values (wakeup `scheduledFor`, one-shot cron next-fire via
croniter, recurring cron 7-day expiry), so the knobs that most warrant
tuning are `BG_SHELL_TTL` and `SUBAGENT_TTL`.

## Layout breaks on terminal resize

Resizing the outer terminal can leave the split layout wrong (user report,
2026-08-05: layout toggle is essentially only needed after a resize broke
things). Investigate whether a `client-resized` hook re-running
`agent-layout`'s split logic would keep panes proportional, and whether
that plays nicely with the compact layout's status-line overview.
