# Sandbox mode: Docker Sandboxes (sbx) as a first-class project mode

Design doc for adding sandbox mode as the third project backend alongside
`container` / `devcontainer = true`. Written 2026-08-26 from the
`aiop-compliance-gateway` pilot; revised same day after Codex review
(12 findings incorporated, see Review notes at the end). Every mechanism was
verified live unless marked open. Companion state: the `[acg-sbx]` stanza in
the user's projects.toml is the hand-rolled version of what this doc
automates.

> **2026-11 update — sbx >= 0.51 / v3 kits.** sbx 0.51 turned the built-in
> agents into published v3 kits (`docker.io/sbx/<agent>`) and stopped
> composing v1/v2 kits with them. Since then every sandbox is composed as
> **workload + agent mixins + state kit + project kits** (see "Sandbox
> composition" below). That change dissolved two pilot-era compromises:
> Codex now gets a proxy-managed login exactly like Claude (via
> `sbx/codex-mixin`), and rebuild no longer exports/imports agent state
> (persistent `volume@1` storage, keyed by sandbox name, survives
> `sbx rm`). Sections below are updated to that design; the pilot-era
> reasoning is kept where it still explains a constraint.

## Why

- **Credential posture**: the sbx host proxy injects Claude's credential
  into outbound requests; the agent can spend but never read it. In
  devcontainers today, Claude's OAuth token sits readable in the
  claude-config volume. Since sbx 0.51 the same holds for Codex:
  `sbx/codex-mixin` declares the OpenAI credential (API key or ChatGPT
  OAuth, `sbx secret set openai --oauth` on the host), so the VM only ever
  sees `oai-*-proxy-managed` placeholders.
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
| `agent-kill` guest cleanup | `docker exec` session dies with the pane | `reap_pane_agents`: killing the pane only kills the host `sbx exec` client — the guest agent survives — so kill terminates it in the VM (see `sandbox.py`) |

`forward_ssh_agent` is defined as a no-op-with-warning in sandbox mode: sbx
forwards the host agent natively, and the Docker SSH pump must never spawn.

## projects.toml schema

```toml
[acg]
repo         = "/Users/me/dev/Chatbot/aiop-compliance-gateway-service"
sandbox      = true
# All optional:
sbx_workload = "shell"                     # default; or a v3 workload kit ref
sbx_kits     = ["git+https://github.com/rmabon/dotfiles.git#ref=master"]  # v3 mixins: dirs, ZIPs, OCI refs, git URLs
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
- `sbx_template` (pilot era: a `-t` image replacing the built-in claude
  template) is rejected with a migration hint: a `-t` image can't carry the
  v3 agent mixins. Project tooling goes into a v3 kit recipe instead.
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
  `create_argv` (see Sandbox composition: workload + pinned agent mixins
  + state kit + project kits, then repo + `sbx_mounts`).
- `status(name)` — `sbx ls --json` status (`running`/`stopped`/…, None
  when absent).
- `reap_pane_agents(name, pane_ids)` — `agent-kill`'s guest-side cleanup.
  Killing a pane kills only the host's `sbx exec` client; the guest
  `claude`/`codex` keeps running, holds its worktree (which the guest then
  keeps listing after the host deleted it), and is invisible to tmux.
  Matches guest processes carrying `TMUX_AGENTS_AGENT=1` + `TMUX_PANE=%<id>`
  (every exec template passes both; children inherit them; the
  `agent-terminal` shell carries neither marker), SIGTERM then SIGKILL after
  a 3 s grace. Skips a sandbox that isn't running — `sbx exec` would boot it.
- `path_visible(name, path)` — True/False/None (inconclusive) probe of
  whether the guest sees a host directory; `agent-new` fails a sandbox
  spawn whose fresh worktree is invisible (see "Stale workspace entries").
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

## Sandbox composition (sbx >= 0.51)

`sandbox.create_argv` builds every sandbox the same way:

```
sbx create --name <project> [-m <sbx_memory>] \
  --kit sbx/claude-mixin:<pinned> --kit sbx/codex-mixin:<pinned> \
  --kit <tmux-agents state kit> --kit <sbx_kits…> \
  <sbx_workload> <repo> <sbx_mounts…>
```

- **Workload** (`sbx_workload`, default `shell`): Docker's Debian 13 shell
  workload (docker engine, git, gh, uv, python, socat for the ssh-agent
  forwarder). Exactly one per composition. A project that needs a heavier
  base can point this at its own v3 workload kit, but the three migrated
  projects all fit in a *mixin* on `shell` (kubectl, node, mark, d2,
  databricks CLI … baked by the mixin's `spec.dockerfile` recipe).
- **Agent mixins**, pinned by tag in `sandbox.CLAUDE_MIXIN` /
  `sandbox.CODEX_MIXIN` so a rebuild never silently changes agent
  versions. Each declares its agent's `credential@1` (proxy-managed API key
  or OAuth, sentinels in the VM) and network allows. Don't put
  `sbx/claude-mixin` on the `claude` workload — two providers of `claude`
  fail composition.
- **State kit** (`sbx_kits/state/spec.yaml`, package data): persistent
  volumes for agent state, see Rebuild semantics.
- **Project kits** (`sbx_kits`): v3 mixins only. Ordering between kits is
  the dependency graph, never flag order — a kit whose hooks must run
  after an agent mixin's (the dotfiles kit editing codex's `config.toml`)
  declares `integrates: ["claude", "codex"]`.

Pilot-era history (0.38–0.49, still why some rules exist): a mixin could
not declare OAuth ("only allowed on agent kits"), a custom agent kit
extending `claude` silently broke both injections (#344 family), and a
second codex sandbox per project doubled VMs — so codex logged in *inside*
the claude sandbox (`codex login` over an `ssh -L 1455` tunnel) and its
token lived in the VM. Docker's own published `codex-mixin` (provenance-
authorized) removed all of that; never reintroduce an in-VM codex login.

Codex hook provisioning is unchanged: `codex_hooks.ensure_sandbox(name)`
(home discovery via `exec_capture`, foreign-hook-preserving merge, digest
comparison, atomic delivery) runs in `agent-new`, `agent-other`, restore
and rebuild. It writes `~/.codex/hooks.json`; the mixin owns
`config.toml`.

## Session restore integration

Restore must handle a sandbox that is **gone**, not merely stopped —
`sbx exec` auto-start does not cover deletion. Per project group, the
worker runs: `ensure_daemon` (once, before the parallel wave) →
`ensure_up` → `codex_hooks.ensure_sandbox` → pane respawns. A sandbox
deleted outside tmux-agents is thereby recreated — and because the state
kit's volumes outlive `sbx rm` and logins are host-side, every slot
resumes with its saved `--resume` id as if nothing happened. Sandbox
absence is **never** a reason to forget window mappings; mappings outlive
sandboxes exactly as they outlive containers.

## Rebuild semantics

### When rebuild is even called

Recreation is needed only when a **create-time input** changes, because sbx
fixes those at creation: the workload or a kit's content (project
toolchain update — the main recurring trigger), the pinned agent mixins,
`sbx_mounts`, `sbx_memory`, newly added **global** secrets (bindings are
fixed at creation; a secret added later never reaches existing sandboxes —
upstream #477 makes even sandbox-scoped updates unreliable), and the
occasional corrupted-sandbox reset or sbx upgrade that demands it.

### Agent state on persistent volumes

`volume@1` block storage is keyed on (owner, **sandbox name**, path) — not
on any kit's identity. `sbx rm` retains it; a create under the same name
reattaches every still-declared path, whatever the rest of the
composition looks like. So `agent-rebuild` is just `sandbox.recreate`
(rm → create in one locked critical section) + codex hooks + gh auth +
respawn with the saved resume ids. Nothing is exported, nothing can be
half-imported, and there is no destructive fallback mode.

The state kit declares one volume per piece of irreplaceable state:

| Path | Why |
|---|---|
| `~/.claude/projects` | sessions (`--resume`) + auto-memory |
| `~/.claude/file-history`, `todos`, `plans` | resume context |
| `/tmp/claude-1000` | per-session scratchpads (lost on every recreate before) |
| `~/.codex/sessions` | codex sessions (`codex resume`) |
| `~/.local/state/tmux-agents` | `~/.claude/history.jsonl` + `~/.codex/history.jsonl`, symlinked into place by a startup hook (volumes are directories) |

Deliberately not on a volume: `~/.claude/skills` (sbx's shared skills
mount; sbx rejects volumes that would hide it — which is also why
`~/.claude` can't be one volume), kit-regenerated config
(`settings.json`, `.claude.json`, codex `config.toml`), codex `auth.json`
(a proxy placeholder now), the gh login (synced from the host).

Rules:

- **Sizes are permanent per sandbox name.** An unsized volume gets
  ~512 MB (hit live: 4.6 GB of scratchpads filled it). sbx 0.55 silently
  reattaches a retained volume at its *original* size when the declared
  size changes (the spec says it should refuse), and offers no local way
  to resize or delete retained storage (`sbx volume` is cloud-only). So
  every state-kit volume declares a generous size up front, and a project
  that needs fresh volumes gets a new sandbox name via `sbx_name` — the
  old name's storage stays on disk until sbx can remove it. Adding a *new*
  path is fine (fresh storage). Ownership is fixed in a root startup hook
  instead of `mode`, because a fresh mount may come up root-owned.
- Project kits use the same mechanism for their own per-sandbox
  credentials caches (`~/.databricks` token cache, `~/.kube` ServiceAccount
  kubeconfig).
- Volumes outlive the sandbox *name*'s use: removing a project for good
  should also remove its retained storage (`sbx df -v` lists it).
- The in-VM docker store (`dockerd@1`) is NOT a volume — it ends with the
  sandbox, so compose deps stacks are brought up again after a rebuild.

## agent-vscode

In v1 (user requirement: attaching to the *actual* environment for
inspection matters — host-side file views can't show the sandbox's
toolchain, venvs, or running processes). Sandbox projects attach via
Remote-SSH instead of the container attach: `sbx setup ssh` provisions
hostnames (`{name}.sbx`); `agent-vscode` runs
`code --folder-uri vscode-remote://ssh-remote+{name}.sbx{workdir}`. Caveats to build around:

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
- Egress: Remote-SSH (exec-server mode) scp's only its small CLI; the
  CLI then downloads the VS Code server from
  `update.code.visualstudio.com` / `vscode.download.prss.microsoft.com`
  from *inside* the VM, with no client-side fallback for that step. Under
  deny-all the window opens but the server never installs (empty
  explorer). Those two hosts must be allowed — ideally in the project's
  kit so recreates keep them; `agent-vscode` preflights with
  `sbx policy check network` and prints the `sbx policy allow` line.
  Extension installs need nothing extra (they fall back to a local
  download + transfer).
- Orphans: the sbx SSH session does not kill the remote install script
  when the client disconnects. A reconnect within seconds can race the
  orphan for the scp'd tarball, leaving the new attempt looping forever
  on "Copying VS Code Server to host with scp". Kill stale
  `~/.vscode-server/code-* command-shell` / `sh` trees in the VM and
  reconnect once.

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
   kit repos), `sbx secret set anthropic --oauth` (or `/login` inside the
   first claude pane — stores the anthropic OAuth host-side), and
   `sbx secret set openai --oauth` (ChatGPT OAuth for the codex mixin,
   browser flow on the host). No per-sandbox logins.
5. `sbx skills import` to seed the shared skills store (mounted rw at
   `~/.claude/skills` in every sandbox — install scripts must never
   replace/back up entries there, only add; the dotfiles repo handles this).

## Project tooling: v3 kit recipes, not template images

Pilot-era projects shipped a custom `-t` template image (user `agent`,
docker engine, `com.docker.sandboxes.*` labels, trixie base, claude and
codex baked in). With v3 that whole layer is gone: the `shell` workload
already satisfies the runtime's conventions (`sbx@1`), the agents come
from the mixins, and project tools go into the project mixin's recipe:

- `spec.yaml` (`# syntax=docker/sandbox-kit:3`, `kind: mixin`) declares
  capabilities: `network-policy@1` (install/runtime allow lists),
  `volume@1`, `lifecycle@1` (install hooks once at create — root unless
  `user:` says otherwise; startup hooks every boot; `files`).
- `spec.dockerfile` (same stem) is the recipe: fetch + checksum-verify
  binaries in a build stage, then `FROM scratch` + `COPY` into the
  overlay; `ENV` for environment variables (v3 has no `environment`
  block).
- Reference kits: `.devcontainer/sbx/kit/` in aiop-compliance-gateway,
  ai-product-enrichment and packalyzer-decision-model; personal dotfiles
  kit in `rmabon/dotfiles` (`spec.yaml` + `spec.dockerfile`).
- Validate locally with `docker buildx build <kit> -f <kit>/spec.yaml`
  (the `docker/sandbox-kit:3` frontend parses the descriptor) or
  `sbx kit validate <kit>` against a running daemon.

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
- Kits and SSH are experimental. Pin the sbx version in the docs and treat
  upgrades as events: 0.51 broke every v2 kit (v3 only with the published
  agent kits), 0.54 needs `sbx setup ssh` re-run.
- **Stale workspace entries** (observed on v0.42.0; **gone in v0.49.2**, re-tested 2026-10-23).
  sbx serves the workspace through its own virtiofs server (sailor), not
  Apple's. If anything in the guest looks a path up before the host creates
  it, the guest keeps getting ENOENT for that name indefinitely — measured
  >5 min, unaffected by guest `drop_caches`, host rename away/back, or host
  rmdir+mkdir; names never looked up before are visible at once. Hit in
  practice as a fresh `agent-new` worktree the sandbox couldn't `cd` into
  (pane died, window `X`). Mitigation here is detection only:
  `agent-new` probes `path_visible` and fails with the remedy (restart the
  sandbox; daemon restart if that doesn't clear it). Related upstream:
  #533 (still-provisional inodes served from the server's model), #328
  (moved dir vanishes / stale cwd, daemon restart recovers), #629 (missing
  readdir name, fixed 0.48). On 0.49.2 the same probes pass (a name
  created after a guest miss is visible at once; host deletions leave
  no ghosts), so nothing was filed; the `path_visible` guard stays as a
  cheap tripwire for older installs or a regression.
- **`sbx ls --json` shape changed in 0.49**: a bare list instead of
  `{"sandboxes": [...]}`. `sandbox.status` accepts both.
- Multiple agents' OAuth bindings per sandbox: solved upstream by the
  published `sbx/claude-mixin` + `sbx/codex-mixin` (docker/sbx-releases#823).
- **#791** (open) — sandbox DNS answers SERVFAIL for SRV/TXT; the reason
  this setup tracks 0.55 release candidates.

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

- Automating `sbx login` / `sbx secret set` (interactive by nature;
  surface clear errors instead).
- Kit authoring/management (kits are referenced, not generated —
  the dotfiles kit lives in the dotfiles repo).
- The devcontainer modes: unchanged, remain the default.

## Review notes (2026-08-26, pilot era — superseded where the 2026-11 update says so)

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
