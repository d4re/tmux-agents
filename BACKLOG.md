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
## Config-tunable registry TTLs

`tmux_agents/registry.py` hardcodes `WAKEUP_GRACE`, `CRON_ONESHOT_GRACE`,
`SUBAGENT_TTL`, `BG_SHELL_TTL` (and the 7-day `CRON_RECUR_TTL` backstop).
Expose these as a top-level `[registry]` table in an existing config file
(projects.toml) rather than a new file, per the reuse-config preference.
Only `bg-shell` and the `subagent` backstop are genuine heuristics — the
rest derive real values (wakeup `scheduledFor`, one-shot cron next-fire via
croniter, recurring cron 7-day expiry), so the knobs that most warrant
tuning are `BG_SHELL_TTL` and `SUBAGENT_TTL`.
