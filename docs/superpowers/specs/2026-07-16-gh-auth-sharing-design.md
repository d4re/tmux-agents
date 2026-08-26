# gh auth sharing — design

Date: 2026-07-16
Status: approved

## Problem

`gh` inside a devcontainer stores its login in the container's own
`~/.config/gh/hosts.yml`, which is lost on every container rebuild — so the
user keeps having to `gh auth login`. On the host (macOS), the token lives in
the keyring, not in `hosts.yml`, so mounting/copying the config dir cannot
carry the login across.

Scope: `gh` CLI inside containers only. HTTPS git auth and host-side gh are
explicitly out of scope (ssh remotes already work via the ssh agent pump).

## Approach (chosen: one-shot token sync at container-up)

A new module `src/tmux_agents/gh_auth.py`, deliberately shaped like
`ssh_forward.py`:

- `host_gh_token() -> str | None` — runs `gh auth token --hostname github.com`
  on the host (reads the keyring; we never touch the keychain ourselves).
  `None` when `gh` is missing or not logged in.
- `has_gh_in_container(container, user) -> bool` — `docker exec … gh --version`
  probe, same pattern as `has_python3_in_container`.
- `maybe_sync_gh_auth(container, user) -> SyncResult` — the idempotent entry
  point. Pipes the token via **stdin** to
  `docker exec -i -u {user} {container} gh auth login --with-token --hostname
  github.com`. The token never appears on argv, in tmux command strings, or on
  disk host-side. Always overwrites (no probe — a probe can't detect a revoked
  token; always-sync is self-healing and costs ~200ms). All subprocess calls
  carry timeouts so a hung docker can't stall agent spawn.
- `SyncResult` — frozen dataclass mirroring `PumpResult`: outcomes
  `disabled_no_host_gh`, `disabled_not_logged_in`, `disabled_no_container_gh`,
  `synced`, `failed`, with the duck-typed `render(stage)` mapping onto
  `progress.Stage`.

## Config

Per-project `share_gh_auth = true` flag (default on), parsed in `config.py`
exactly like `forward_ssh_agent`. Only meaningful for container projects.

## Call sites

A `gh auth` stage right after the `ssh pump` stage everywhere containers come
up: `commands/new.py`, `commands/restore.py`, `commands/rebuild.py`. Gated on
`proj.is_container and proj.share_gh_auth`. `agent-terminal` needs no change —
the synced login lives in the container's `~/.config/gh/hosts.yml`, so every
shell sees it; it survives container restarts and is re-synced on rebuild.

## Error handling / visibility

Nothing is fatal: every failure renders as a `stage.warn(...)` and logs to the
unified log; the agent still spawns and gh simply asks for login as today.
Warnings get first-class visibility for free in `agent-new`: `stage.warn` sets
`reporter.had_warning`, which holds the pane open (state `W`) with the log on
screen until a keypress. Restore/rebuild don't hold — acceptable, since the
persistent failure modes (no gh in image, host not logged in) surface on the
first `agent-new` for the project.

## Rejected alternatives

- `GH_TOKEN` env injection into exec_cmd: leaks the token into `ps`, tmux
  respawn command strings, and restore snapshots.
- Live credential-helper relay (ssh-pump style UDS): most secure but heavy
  machinery; gh needs a static long-lived token, not a live socket.

## Deliberate scope cuts

No `gh auth setup-git` (https git was not the pain), no multi-host/GHE
support, no token-refresh loop (device-flow `gho_` tokens are long-lived).

## Testing

Unit tests in the existing style (monkey-patched subprocess, no real docker):
outcome mapping per prerequisite failure, token delivered via stdin and absent
from argv, `share_gh_auth` parsing default/override, call-site gating.

## Docs

`docs/ARCHITECTURE.md` (new module + new `projects.toml` key), README
(`share_gh_auth` in the project options).
