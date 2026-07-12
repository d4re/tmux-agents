---
name: verify
description: Verify a tmux-agents change end-to-end — run the CI gates, reinstall the uv tool if the change affects installed behavior, and drive a scratch tmux server (never the user's live agents session) to observe the change working.
---

# Verifying a change in tmux-agents

## 1. Always: run what CI runs

```bash
make check    # ruff check + ruff format --check + pytest
```

The suite runs in under a second. CI (`.github/workflows/test.yml`) gates on
exactly these, across Python 3.11–3.13.

## 2. Decide whether runtime verification applies

| Change touches | Runtime check needed |
|---|---|
| `src/tmux_agents/**` (any CLI/module) | Yes — reinstall first, then drive it |
| `agents.conf` | Yes — sync conf first, then reload |
| `hooks/agents.json` / `hooks/write-state.sh` | Usually covered by `test_hook_snippets.py`; runtime check only for payload-shape assumptions |
| Docs, tests only | No — `make check` suffices |

## 3. Reinstall before observing (the #1 footgun)

The install is a `uv tool`; editing source does **not** change the installed
executables. Any manual run of `agents`, `agent-new`, `agent-state`, … without
this step exercises **old code**:

```bash
make reinstall    # uv tool install --reinstall --no-cache .  (--no-cache is load-bearing)
```

Note this swaps the **single global** installed tool to this worktree's code.
If other agents are working in sibling worktrees, coordinate — whoever
reinstalled last wins (same for `make conf-sync`).

For `agents.conf` changes:

```bash
make conf-sync    # cp to ~/.config/tmux-agents/ + source-file on the live server
```

If a command still behaves like old code after reinstall, read the installed
copy at `~/.local/share/uv/tools/tmux-agents/lib/python3.12/site-packages/tmux_agents/`
to confirm before debugging elsewhere.

## 4. Drive it on a scratch server, not the user's session

The user's real agents live on the `-L agents` socket. **Never** kill, restart,
or respawn panes on that server to test a change. Instead, isolate:

```bash
export TMUX_AGENTS_STATE_DIR=$(mktemp -d)
export TMUX_AGENTS_CONFIG_DIR=$(mktemp -d)
cp agents.conf "$TMUX_AGENTS_CONFIG_DIR/agents.conf"
tmux -L agents-verify -f "$TMUX_AGENTS_CONFIG_DIR/agents.conf" new-session -d -s agents
# ... drive agent-state / agent-overview / etc. against -L agents-verify ...
tmux -L agents-verify kill-server
```

`tests/test_smoke.py` does exactly this pattern — copy from it. Read-only
inspection of the live server (`tmux -L agents list-windows`, `show-options`)
is fine; mutation is not.

## 5. Where to look for evidence

- Unified log: `/tmp/tmux-agents/tmux-agents.log` (or `$TMUX_AGENTS_STATE_DIR/tmux-agents.log`).
  `TMUX_AGENTS_LOG_LEVEL=DEBUG` for verbose traces.
- Per-window state: `tmux -L <socket> list-windows -F '#{window_id} #{window_name} #{@state_code}'`.
- Spawn progress: `<state_dir>/spawn-<window_id>.log`.

For deeper state-pipeline debugging, see the `debug-state` skill.
