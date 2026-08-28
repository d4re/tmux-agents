# Sandbox mode: Docker Sandboxes (sbx) as a first-class project mode

Design doc for adding sandbox mode as the third project backend alongside
`container` / `devcontainer = true`. Written 2026-08-26 from the
`aiop-compliance-gateway` pilot; revised same day after Codex review
(12 findings incorporated, see Review notes at the end). Every mechanism was
verified live unless marked open. Companion state: the `[acg-sbx]` stanza in
the user's projects.toml is the hand-rolled version of what this doc
automates.

## Why

- **Credential posture**: the sbx host proxy injects Claude's credential
  into outbound requests; the agent can spend but never read it. In
  devcontainers today, Claude's OAuth token sits readable in the
  claude-config volume. (Codex is the deliberate exception — see below —
  until sbx supports multiple agent OAuth bindings per sandbox.)
- **Per-project isolation**: each sandbox is its own microVM (own kernel,
  own Docker daemon). A devcontainer escape lands in the one shared Docker
  Desktop/colima VM that holds every project's containers and host mounts.
- **Egress the agent can't touch**: deny-by-default policy enforced on the
  host, replacing the in-container iptables firewall (which lives where the
  agent lives).
- **Docker restored**: `docker`/`compose` inside the sandbox is contained by
  the VM — the reason it was removed from devcontainers (host socket
  exposure) does not apply.

Honest limits, conceded during the pilot: dynamic infra credentials
(Azure AD / kubectl / Databricks OAuth) cannot be proxy-injected, and the
egress allowlist still contains viable exfiltration channels (github.com).
For those credentials the sandbox is at parity with a devcontainer, not
better. sbx upstream issue #223 (self-refreshing exec-credential secrets)
is the eventual fix to watch.

## Backend model (not a config flag bolted on)

`sandbox = true` is the external spelling, but internally this is a third
**backend**, not a boolean: every current consumer of `Project.is_container`
makes a Docker-vs-host assumption that is wrong for sandboxes in a
different way. Introduce an explicit backend enum
(`host | container | sandbox`) on `Project` and dispatch on it. Exhaustive
behavior matrix to settle before coding:

| Consumer | container today | sandbox behavior |
|---|---|---|
| `agent-new` provisioning | `container.ensure_up` + SSH pump | `sandbox.ensure_daemon` + `ensure_up`; **no SSH pump** (sbx forwards the agent natively) |
| `agent-other` codex preflight | `command -v` in container | probe inside sandbox via `sandbox.exec_capture`, never on the host |
| `agent-terminal` | shell into container | `sbx exec -it {name} bash` — a host shell would be a silent isolation hole |
| `agent-vscode` | Dev Containers attach | Remote-SSH (`{name}.sbx`), host-folder fallback |
| `agent-restore` | `up_cmd` per project group | `ensure_daemon` once, then `ensure_up` per project group (see Restore) |
| `agent-rebuild` | rebuild container | destructive sandbox reset (see Rebuild) |
| codex hook provisioning | `ensure_container` (docker exec) | `ensure_sandbox` (sbx exec, same guarantees) |
| worktree resolve/prune | container paths | host paths (worktrees are host-side; passthrough preserves them) |
| exec-cmd preflight | container `command -v` | in-sandbox `command -v` |
| `agent-kill` / idle lifecycle | container keeps running | **nothing to build**: sbx auto-stops idle sandboxes on its own (upstream #450 documents the feature racing starts) and `sbx exec` auto-starts on next use — idle cost self-manages |

`forward_ssh_agent` is defined as a no-op-with-warning in sandbox mode: sbx
forwards the host agent natively, and the Docker SSH pump must never spawn.

## projects.toml schema

```toml
[acg]
repo         = "/Users/me/dev/Chatbot/aiop-compliance-gateway-service"
sandbox      = true
# All optional:
sbx_template = "acg-sbx-template:0.3"      # default: stock claude template; avoid :latest
sbx_kits     = ["https://github.com/rmabon/dotfiles"]  # ordered; dirs, ZIPs, OCI refs, git URLs
sbx_mounts   = ["~/.kube:ro", "~/.databricks"]         # extra workspaces, ":ro" honored
sbx_memory   = "8g"                        # passed as `-m`; sbx default is 50% of host RAM
```

Sandbox name = project name (host-global; a name-collision override key can
be added if it ever bites — deferred as YAGNI).

Validation rules:

- `sandbox` is mutually exclusive with `container`, `devcontainer`, `user`,
  `container_workdir`, and `up_cmd` (hard errors, same pattern as the
  existing container/devcontainer check).
- `sbx_*` keys without `sandbox = true` are errors.
- `sbx_mounts`: expand `~` in Python (subprocess argv gets no shell
  expansion), canonicalize, reject duplicates and non-existent paths,
  parse the `:ro` suffix. **Documentation recommends `:ro` wherever the
  workflow allows** — writable global credential dirs are a major
  capability grant; rw is needed only where token caches live in the
  mounted dir (kubelogin, az).
- Strict types (a string where a bool is expected is an error, not truthy).

### `sbx_mounts` (the "keep kubectl/databricks configured once" feature)

Extra positional workspaces on `sbx create` (`PATH[:ro]`), absolute paths
preserved inside the VM — `~/.kube` mounted behaves exactly like the
devcontainer bind mount does today: configure once on the host, visible in
every sandbox. This is a deliberate parity feature, not a security feature:
anything mounted is readable by the agent, same as the devcontainer. Choose
per project; omit for projects whose agents don't need infra credentials.

Mounts are **create-time only** — changing `sbx_mounts` requires sandbox
recreation (`agent-rebuild`, below). Found-by-name is trusted; drift is
not auto-detected (see Validation of existing sandboxes — container-mode
parity).

## Exec command templates

Both agent kinds get an sbx default template in `config.py::_default_exec_cmd`
(third branch, dispatched on the backend enum):

```
sbx exec -it -e TERM -e COLORTERM -e TMUX_PANE -e TMUX_AGENTS_AGENT=1 \
    {sandbox} bash -lc 'cd {workdir} && exec claude{resume_args}'
```

(codex identical with `exec codex{resume_args}`). Verified properties this
relies on:

- `sbx exec` auto-starts a **stopped** sandbox (not a deleted one — restore
  handles that). Only the daemon must already run.
- `-e TMUX_PANE` forwards the pane id, so `write-state.sh` works unmodified
  and its writes land on the host through the passthrough mount (worktree
  paths are identical on both sides). The whole state pipeline works with
  zero code changes.
- `{workdir}` is the host-side worktree path: worktrees are created
  host-side by `agent-new` exactly as today, and are fully valid inside the
  sandbox (verified: worktree add + commit inside, visible on host).
- **Pitfall (hit live)**: the default must exist for BOTH kinds. With only
  `exec_cmd` overridden, the codex slot fell back to the host-only default
  and ran codex on the host.

## New module: `sandbox.py`

Mirror of `container.py` in role (the only module that shells out to `sbx`)
but not in weakness — the current `container.ensure_up` has a
check-then-create race that this module must not copy.

- `exec_capture(name, cmd, stdin=None)` — the primitive everything else
  builds on (home discovery, preflight probes, atomic file delivery).
- `deliver(name, path, content, mode)` — unique-mktemp + atomic-rename
  write via `exec_capture`, matching the guarantees
  `codex_hooks._container_deliver` provides today. A bare `cat > path` is
  not acceptable for hook provisioning.
- `ensure_daemon()` — global daemon-start lock (reuse `locks.py`), then
  `sbx daemon status`; start detached (`sbx daemon start -d`) if down and
  poll readiness with a bounded timeout. The daemon does not auto-start at
  boot. Restore parallelizes project groups, so this runs once before the
  wave.
- `ensure_up(project)` — per-sandbox-name lock around
  inspect → create → re-inspect (second existence check after acquiring the
  lock). Creation:
  `sbx create --name {sbx_name} [-t {sbx_template}] [--kit …]…
  [-m {sbx_memory}] claude {repo} {sbx_mounts…}`. Agent positional is
  always `claude` (see Codex section).
- **Actionable errors, bounded time.** Three failures the user can fix get
  first-class detection and an exact remediation command in the spawn log:
  sbx not installed, Docker login expired (`run: sbx login` — sessions
  expire ~2-weekly and this can surface on ANY call, so the mapping is
  shared, not create-only), daemon not running/ready. Everything else is a
  generic failure with sbx's own stderr passed through — a deeper taxonomy
  can grow from real incidents instead of speculation. All calls get
  bounded timeouts so a placeholder pane can never hang forever on a stuck
  `sbx`.
- **Noninteractive guard, minimal.** `agent-new` runs in a background
  worker where an interactive `sbx` prompt = an invisible hang, so
  `sbx create` runs with stdin closed (prompt → immediate failure →
  logged remediation) after the login/daemon preflight above. First-time
  kit approvals (credential bindings, kit trust) are documented as a
  one-time interactive `sbx create` per new kit — not machinery. The
  no-`credentials[]` rule for kits (upstream #344) stays a documented
  rule; it is unenforceable by us either way.

### Validation of existing sandboxes: container-mode parity

Deliberately simplified after review: devcontainer projects have exactly
the same drift problem today (edit devcontainer.json → the running
container is stale until you run `agent-rebuild`) and tmux-agents has
never fingerprint-checked those. Sandbox mode inherits the same contract:
found-by-name is trusted; changing any `sbx_*` create-time key is
documented as "run `agent-rebuild`" — which the state-preserving rebuild
makes cheap. Inspect/fingerprint drift detection is a possible v2 nicety,
not v1 machinery.

## Codex in the same sandbox (single-VM dual agent)

Settled architecture after testing every alternative:

| Option | Verdict |
|---|---|
| Mixin kit declaring openai OAuth | Hard-rejected: "oauth credential … only allowed on agent kits" |
| Custom agent kit `extends: claude` + openai OAuth | **Trap**: validates and creates, then silently breaks BOTH injections (only built-in kits pass provenance authorization; see upstream #344 for the confirmed sibling bug) |
| Second codex sandbox per project | Works (verified) but doubles VMs/compose stacks and breaks claude→codex interop — rejected |
| Clone host `~/.codex/auth.json` and keep using both | **Never**: codex rotates the refresh token on rewrite; two live copies of one lineage invalidate each other. (A one-time transfer where the host copy is retired is viable — but then in-sandbox login is simpler anyway) |
| **In-sandbox `codex login` (chosen)** | Sandbox-local, independent token lineage, revocable. Login transport below |

Login transport, in preference order:

1. `codex login --device-auth` — no ports, no bridge. Requires device-code
   auth enabled in ChatGPT personal/workspace settings; **currently
   disabled for this user's Enterprise workspace**, hence path 2 exists.
   Worth requesting from the workspace admin — it also removes the port
   machinery for every colleague.
2. Standard `codex login` with the localhost callback bridged via SSH:
   `ssh -N -L 1455:127.0.0.1:1455 {name}.sbx` (prereq: `sbx setup ssh`),
   login in a pane, ctrl-c the tunnel after. `ssh -L` originates the
   connection inside the VM, so it reaches codex's loopback-bound login
   server — `sbx ports --publish` cannot (connection reset; the publish
   path doesn't reach loopback-bound in-VM services). Works for every
   subscription tier. The fixed host port collides if two sandboxes log
   in simultaneously, so treat it as a scoped, temporary bridge.

Consequences the implementation must own:

- **Codex hook provisioning**: `codex_hooks.ensure_sandbox(name)` — twin of
  `ensure_container` with the SAME guarantees (home discovery via
  `exec_capture`, foreign-hook-preserving merge, digest comparison, atomic
  delivery), wired into `agent-new`, `agent-other`, **and restore/rebuild**
  (rebuild deletes the installed hooks by construction).
- **Codex login state is per-sandbox and lost on `sbx rm`** (see Rebuild).
- The codex token is readable inside the VM (egress-limited) — codex's
  devcontainer status quo, accepted; claude stays fully proxy-managed.

### Experimental, unsupported: mounting the host `~/.codex`

`sbx_mounts = ["~/.codex"]` shares one token lineage (no divergent-copy
invalidation) and gives configure-once ergonomics — but concurrent refresh
from host-codex and sandbox-codex is a read-modify-write race on one file;
a lost race forces re-login, and atomic file replacement does not serialize
two already-running clients. Further wrinkles: the host may keep tokens in
the OS keyring (auth.json absent or stale), shared `config.toml` may select
keyring storage the VM lacks, and all codex history/config becomes
VM-visible. **Contract if used anyway: exclusive use — host codex must not
run while a sandbox uses the shared store.** Not the default, not
supported, documented only so nobody rediscovers it as a "simple" idea.

## Session restore integration

Restore must handle a sandbox that is **gone**, not merely stopped —
`sbx exec` auto-start does not cover deletion. Per project group, the
worker runs: `ensure_daemon` (once, before the parallel wave) →
`ensure_up` → `codex_hooks.ensure_sandbox` → pane respawns. A sandbox
deleted outside tmux-agents is thereby recreated (fresh, needing logins —
panes show the standard errored-placeholder with the runbook hint).
Sandbox absence is **never** a reason to forget window mappings; mappings
outlive sandboxes exactly as they outlive containers.

- `--resume {session_id}` works through `{resume_args}` (verified
  substitution end-to-end) — but only for sandboxes that still hold the
  session files; after recreation the tick's stale session ids must be
  cleared rather than passed to `claude --resume` (which would error).
- Open item: Claude session survival across host reboot (VM state on disk)
  is assumed but not yet verified.

## Rebuild semantics

### When rebuild is even called

Recreation is needed only when a **create-time input** changes, because sbx
fixes those at creation: the template (project toolchain update — the main
recurring trigger), `sbx_mounts`, `sbx_memory`, kit list/content where it
matters at create-time, newly added **global** secrets (bindings are fixed
at creation; a secret added later never reaches existing sandboxes —
upstream #477 makes even sandbox-scoped updates unreliable), and the
occasional corrupted-sandbox reset or sbx upgrade that demands it. In
steady state this is rare (the pilot needed six recreations on day one and
none since the template stabilized) — but "rare" is not "never", so
rebuild must not cost conversation history.

### State-preserving recreate

Agent state lives in the sandbox home only because sbx offers nowhere
better (it deliberately ignores host `~/.claude`, upstream #113; and
redirecting via `CLAUDE_CONFIG_DIR` onto a mounted host dir is blocked by
upstream #67 — credential injection ignores custom config dirs). So
rebuild carries the state across explicitly:

1. **Export** before `sbx rm`. Principle: the export carries only what
   cannot be recreated — sessions, history, memory, the codex login.
   Anything easily recoverable stays out (VS Code server: reinstalls on
   next attach; caches, venvs: rebuilt by tooling) so the export stays
   small and the tar list stays auditable. Concretely, via
   `sandbox.exec_capture` (or `sbx cp`, whose copy-out escape was fixed in
   0.38): `~/.claude` **excluding** `~/.claude/skills` (that's the shared
   store mount — must never be tarred) and minus kit-regenerated files
   (`settings.json`, `.claude.json`); `~/.codex` sessions/history **plus
   `auth.json`** — a one-time transfer whose source is destroyed with the
   sandbox is lineage-safe (this is exactly the "copy is fine if the
   source is retired" case), so the codex login survives; codex
   `config.toml` is NOT restored (kits regenerate it — restoring an old
   copy would clobber a new template/kit's config).
2. `sbx rm --force` + `ensure_up` (new template/mounts/memory take
   effect).
3. **Import** the tar back, re-provision codex hooks, respawn panes. Saved
   `--resume` ids stay valid because the session files came along.

Failure containment: if the export fails, abort the rebuild (never delete
what couldn't be saved) unless the user passes an explicit
`--discard-state`. If the import fails, the sandbox is fresh-but-working:
clear stale session ids and hold codex panes on a "login required"
placeholder — the destructive-reset behavior, now the fallback rather than
the design.

External deletion (`sbx rm` outside tmux-agents) still loses state — out
of scope; restore recreates fresh in that case.

## agent-vscode

In v1 (user requirement: attaching to the *actual* environment for
inspection matters — host-side file views can't show the sandbox's
toolchain, venvs, or running processes). Sandbox projects attach via
Remote-SSH instead of the container attach: `sbx setup ssh` provisions
hostnames (`{name}.sbx`); `agent-vscode` runs
`code --remote ssh-remote+{name}.sbx {workdir}`. Caveats to build around:

- SSH support is upstream-experimental — degrade gracefully: if
  `sbx setup ssh` hasn't run or the managed SSH config is broken, print
  the fix (`sbx setup ssh`) rather than failing opaquely, and keep the
  host-side "open folder locally" fallback behind a flag (passthrough
  means host paths are the same files, useful when SSH is down).
- Known upstream wrinkles: a macOS reconnect loop on some VS Code
  versions (fix: disable `remote.SSH.useLocalServer`) and host-key
  breakage after config regeneration (`sbx setup ssh` re-heals).
- First attach installs the VS Code server inside the sandbox (slow once,
  cached after — but gone after rebuild/recreate, so expect the one-time
  cost again).

## One-time host setup (documented + preflight-checked, not automated)

1. Install sbx (no brew needed): unpack `DockerSandboxes-darwin.tar.gz` from
   `docker/sbx-releases` under `~/.local/share/docker-sandboxes/<ver>/`,
   symlink `bin/sbx` into `~/.local/bin` (the `libexec/` VMM tree must stay
   with the binary). Manual updates; treat upgrades as events (see Known
   issues).
2. `sbx login` (free Docker account; re-login every ~2 weeks — surfaced by
   the error taxonomy whenever it lapses).
3. `sbx daemon start -d --policy balanced`.
4. Secrets: `gh auth token | sbx secret set github` (required for private
   kit repos), `/login` inside the first claude pane (stores the anthropic
   OAuth host-side), per-sandbox codex login as above.
5. `sbx skills import` to seed the shared skills store (mounted rw at
   `~/.claude/skills` in every sandbox — install scripts must never
   replace/back up entries there, only add; the dotfiles repo handles this).

## Custom templates (per-project images)

Requirements discovered the hard way (all load-bearing, none documented
upstream):

- User must be named `agent`, uid 1000, groups `sudo` + `docker`;
  `~/.claude` pre-created agent-owned; default `USER agent`.
- Docker Engine must be in the image (the in-sandbox daemon is
  template-provided), and the OCI labels
  `com.docker.sandboxes.start-docker="true"` +
  `com.docker.sandboxes.flavor` are what make the runtime start it
  privileged — without them dockerd dies with nftables EPERM.
- Base must be trixie-or-newer: bookworm's iptables 1.8.9 fails against the
  nerdbox kernel (nftables-only, no legacy xtables).
- Flow: `docker build` on host → `docker save` → `sbx template load <tar>`
  → reference by a **versioned tag** in `sbx_template` (`:latest` defeats
  the drift detection above). Reference Dockerfile:
  `aiop-compliance-gateway-service/.devcontainer/sbx/Dockerfile`.

## Known upstream issues that shape this design

- **#344** — a kit `credentials[]` entry that resolves flips the built-in
  anthropic credential to api-key mode (our dual-kit trap; maintainer
  confirmed). Rule: project kits must never declare `credentials[]` — and
  since we cannot enforce that for remote kits, the preflight + docs carry
  the warning.
- **#477** — sandbox-scoped secrets don't actually apply to running
  sandboxes despite docs.
- **#208** — asks sbx to integrate device-code OpenAI OAuth into its
  host-proxy credential flow (the CLI itself already has `--device-auth`;
  the issue is about sbx-managed injection).
- **#223** — self-refreshing host-side credential providers (would cover
  Azure/kubectl/Databricks properly; until then `sbx_mounts` is the parity
  answer).
- **#506** — 0.39 reportedly enables cross-sandbox agent communication by
  default; review before upgrading past 0.38.
- Kits and SSH are experimental; the kit schema (v2) is weeks old. Pin the
  sbx version in the docs and treat upgrades as events.
- Unfiled (ours to file): multiple built-in agent kits' OAuth bindings per
  sandbox; the silent-breakage bug distilled from the #344 family.

## Implementation order

1. Backend enum + exhaustive command behavior matrix (the table above,
   settled as code-level decisions).
2. Config parsing/validation + tests: new keys, incompatible-key errors,
   `~`-expansion and mount normalization, strict types.
3. `sandbox.py`: actionable errors, bounded timeouts, daemon readiness
   under a global lock, per-name create locks, `exec_capture`, atomic
   `deliver`.
4. One complete `agent-new` vertical slice, including preflight failures
   surfacing in the spawn log with exact remediation commands.
5. `agent-other` + `agent-terminal` (verify neither probes nor executes on
   the host), `codex_hooks.ensure_sandbox`.
6. Grouped restore: `ensure_daemon` before the wave, `ensure_up` +
   hook-healing per group, deleted-sandbox recovery, stale-session-id
   clearing.
7. `agent-vscode` Remote-SSH branch (in v1 by requirement; graceful
   degradation per its section).
8. Rebuild with state-preserving export/import (the export tar path is
   also the building block for a future backup command if ever wanted).
9. Second pilot (`ai-product-enrichment` per its assessment: compose deps
   move inside the sandbox, shared Dockerfile goes trixie + arm64-aware
   pins) + explicit concurrency/failure gates: parallel `agent-new`,
   daemon-down start, expired login, externally deleted sandbox,
   restore-after-deletion, hook-merge preservation, state-preserving
   rebuild with both slots (history + codex login survive), VS Code attach
   after rebuild (server reinstall path).
10. README + `docs/ARCHITECTURE.md` updated **alongside each step they
    describe** (repo documentation rule), not as a final cleanup.

Deferred from v1: drift detection (container-mode parity instead),
`sbx_name` override, any deeper error taxonomy.

## Out of scope

- Automating `sbx login` / codex login / secret creation (interactive by
  nature; surface clear errors instead).
- Kit authoring/management (kits are referenced, not generated —
  the dotfiles kit lives in the dotfiles repo).
- The devcontainer modes: unchanged, remain the default.

## Review notes (2026-08-26)

Codex review (12 ranked findings) incorporated throughout: restore
recreates deleted sandboxes; backend enum instead of an `is_container`
bolt-on; locking + actionable errors in `sandbox.py`; `~/.codex` mount
demoted to experimental with an exclusive-use contract; "never copy"
corrected to "never clone-and-keep-both"; atomic delivery for hooks
(+ wired into restore/rebuild); schema normalization rules; `:latest`
discouraged; opening credential claim scoped to Claude. Deviations from
the review, deliberate:

- **Device-auth** is documented as preferred but the port-1455 bridge is
  the working default — device-code auth is disabled in the user's
  workspace, a fact the reviewer didn't have.
- **Rebuild** went further than the review's "call it a destructive
  reset": since recreation triggers are legitimate but conversation
  history must survive them, rebuild is specified as a state-preserving
  export/recreate/import (the review's third option), with destructive
  reset demoted to the failure fallback. The codex `auth.json` round-trip
  is deliberate, not casual: the source lineage is destroyed with the old
  sandbox.
- **Drift detection** (finding 8) was simplified to container-mode parity:
  devcontainers have the same drift today with no fingerprinting, and
  cheap state-preserving rebuilds lower the cost of the honor system.
- The nine-way **error taxonomy** (finding 9) was trimmed to the three
  user-actionable failures + passthrough; depth can grow from real
  incidents.
