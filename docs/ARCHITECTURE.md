# Architecture

Current-state map of `tmux-agents`: how the pieces fit, where the data flows,
and what each module owns. User-facing setup lives in `README.md`. This file
should be kept in sync as the code evolves — see CLAUDE.md.

## What it is

A Python package plus a tmux config and hook bundles that lets one user run
4–6 concurrent coding agents in a single tmux session — Claude Code and/or
OpenAI Codex CLI, one or two per window — each typically running inside a
project's devcontainer. The package ships twelve CLI entry points (`agents`,
`agent-new`, `agent-kill`, `agent-rebuild`, `agent-state`, `agent-overview`,
`agent-rename`, `agent-layout`, `agent-restore`, `agent-vscode`,
`agent-terminal`, `agent-other`) that the tmux config wires into
keybindings, hooks, and the status line.

## Isolation model

Everything runs on a dedicated tmux socket and config:

- Socket: `tmux -L agents`
- Config: `~/.config/tmux-agents/agents.conf`
- Volatile state: `/tmp/tmux-agents/` (override with `TMUX_AGENTS_STATE_DIR`)

`agents.conf` sources `~/.config/tmux-agents/local.conf` as its last line
(silently skipped if absent) as a user-override hook: tmux config is
last-write-wins, so anything there beats the shipped defaults, and neither
`install.sh` nor `make conf-sync` ever touch it, so overrides (e.g. a
different prefix key) survive updates.

Nothing is written under `~/.config/tmux/`, so a user's existing tmux setup
(z4h auto-tmux, default-socket sessions) is untouched. The `agents`
launcher detects whether an `agents` tmux session is
already live (attach), whether a stale snapshot exists from a previous
session (prompt the user, then move it aside, start tmux detached, and
spawn `agent-restore --background` before attaching), or neither (fresh
`tmux new-session -A`). See the "Restore" subsection below for details.

The session is named `agents`. Window `0` is `ctrl` (a plain host shell);
every other window is one agent.

## Data flow — the state pipeline

State is the most non-obvious part of the system. A window's mapping has one
or two **agent slots** (Section "Window mapping — schema 2" below); each
live slot runs the *same* three-layer pipeline independently, and the host
tick joins their letters into one window-level display. The layers:

```
agent lifecycle hooks                host-side tick                tmux + overview
─────────────────────────            ──────────────────             ───────────────
Claude: SessionStart,                agent-state                    agent-overview
  UserPromptSubmit, Stop,            (run every ~1s from             (curses TUI in the
  Notification,                       status-right format)           split-layout pane)
  PostToolUse[ScheduleWakeup/                                       (compact: summary chunk
  CronCreate/CronDelete/Agent/Bash],                                 in status-right)
  SessionEnd
Codex: SessionStart, UserPromptSubmit,
  PermissionRequest, PreToolUse,
  PostToolUse, Stop
   │                                    │
   ▼                                    ▼
<worktree>/.local/.tmux-agents/     for each live slot:      ┐
  state-<pane>.json    ─────────►   read state JSON +        │  tmux per-window options
  pending-<pane>/<kind>__<id>       scan pending registry    │  @state_code = joined
                                     derive letter            │  per-slot codes ("R|I"),
                                    then, per window:         │  @state_fg = color of the
                                     join codes, pick the     │  highest-priority letter,
                                     combined-priority color  ┘  read by agent-overview
                                     set @state_code/@state_fg   via `list-windows -F`
```

Per-step:

1. **Lifecycle hooks.** Claude's hooks (provisioned per worktree by
   `agent-new` into `<worktree>/.claude/settings.local.json` from
   `src/tmux_agents/hooks/agents.json`) dispatch to
   `<worktree>/.local/.tmux-agents/write-state.sh` (also provisioned, from
   `src/tmux_agents/hooks/write-state.sh`). Codex's hooks are provisioned
   once at the **user level** (`~/.codex/hooks.json`, host and container
   home) and dispatch to a package-owned script kept *outside* every
   workspace — `codex_hooks.py` / `hooks/codex-hook.sh` — see "Codex hook
   provisioning" below; the two hook families are otherwise independent
   (different script, different provisioning path, different event set).
   Both scripts write the same shape of file: a JSON state *per pane*
   (the `phase`) at `state-<pane>.json`. Only `write-state.sh` also
   maintains the `pending-<pane>/` marker registry (Section "State
   classification & overview" below) — Codex has no background/scheduled
   tracking, so a Codex slot's `pending-<pane>/` is always empty. Both
   scripts key off `${TMUX_PANE#%}` for the pane id — agents must run with
   `-e TMUX_PANE` exposed so the env var survives `docker exec` (every
   default exec template already does this).
2. **Host-side tick** — `agent-state`, invoked from the tmux status-right
   format (so it runs every status interval, ~1s), enumerates live windows,
   looks up each via the `windows/<window_id>.json` mapping, and for each
   **live slot** (`pane_id is not None`) reads that pane's JSON phase +
   scans its `pending-<pane>/` registry (`registry.scan`), then derives a
   letter via `phase.derive_letter` (unchanged per-slot logic — a dead
   secondary's registry is empty, so it always resolves to no B/Z). Per
   window, the tick joins live slots' letters with `|` into `@state_code`
   (`"R"` for a single-agent window, `"R|I"`/`"B2|R"` for a dual one; a dead
   secondary contributes nothing) and sets `@state_fg`/`@state_selected_fg`
   to the color of the **highest-priority** letter across live slots
   (`phase.combined_letter`, same `X > W > R > B > Z > I > S` order). Option
   writes are batched into one `tmux source-file -` and skipped entirely on
   ticks where the fingerprint (which includes the *full* joined `code`, so
   a slot joining/leaving or a `B2`→`B3` overlay change still re-publishes)
   is unchanged from the prior tick (cached at `<state_dir>/tick.cache`).
   The same tick also does the mapping-mutating housekeeping described in
   "Lock discipline" below (dead-secondary marking, cleanup-pointer sweep,
   session-id merge) and finally prints the summary chunk on stdout — the
   status-bar counts increment once per **live slot**, so a dual-agent
   window contributes to two buckets.
3. **Renderer** — `agent-overview` runs a curses TUI in the bottom pane
   under the split layout; it parses each window's `@state_code` option
   (`overview.parse_state_code`, one `SlotState` per `|`-segment) and
   renders each slot's letter in its own state color, joined by a dim `|`.
   The status-line summary is rendered inside `agent-state` (using counts
   already collected during the tick).

**State letters.** Claude slots: `R`, `W`, `B`, `Z`, `I`, `X`, `S` (running,
waiting, background, sleeping, idle, errored, starting) as before. **Codex
slots only ever show `R`/`W`/`I`/`X`/`S`** — no `B`/`Z`: Codex's hooks don't
maintain the `pending-<pane>/` registry (out of scope per the design spec),
so `registry.scan` always returns zero counts for a Codex pane and
`derive_letter` never emits `B`/`Z` for it. `B` = work executing now while
otherwise idle (background subagent / background Bash, Claude only); `Z` =
nothing running now but it will resume on its own (self-paced wakeup,
one-shot or recurring cron, Claude only). Both carry a count suffix
rendered as `background·N` / `sleeping·N`. Priority `X > W > R > B > Z > I
> S` lives in `phase.derive_letter` (per slot) and `phase.combined_letter`
(across a window's live slots). `S` is the lowest-priority letter, used
while a placeholder pane is awaiting its container/agent.

**`phase_hint`** is a host-side phase field on the `WindowMapping`
(`str | None`), consulted only for **slot 0** (the default agent) when its
per-worktree state file does not yet exist — i.e. during the interval
between "window created" and "the default agent's `SessionStart` (or
equivalent) hook has fired." It is set to `"starting"` when the interactive
`agent-new` writes the initial mapping, drives the `S` letter during
pre-worktree startup, and is cleared to `None` once `_provision` confirms
the real worktree path. On a fatal failure before the worktree exists, it
is set to `"errored"` to show `X`. A present worktree state file always
wins over `phase_hint`. A **secondary** slot with no state file yet always
derives `S` regardless of `phase_hint` (`phase_hint` is slot-0-only,
per-mapping, not per-slot).

**Mapping GC is two-phase.** When the tick sees a `windows/<id>.json`
whose window is no longer live, it does *not* delete it: it stamps
`orphaned_at` on the mapping and moves on. Only once the window has been
absent for `_ORPHAN_GRACE_SECONDS` (90s) does `windows.forget` delete the
mapping plus that pane's `state-`/`session-`/`pending-` files. The delay
exists because the mapping set *is* the restore snapshot: tmux closes
every window a moment before the server exits, so an eager prune deletes
the whole snapshot during shutdown and the next `agents` finds nothing to
restore. Ticks stop when the server dies, so a tombstone written during
shutdown is never followed by a delete. A deliberate `agent-kill` calls
`windows.forget` directly, so killed agents drop out immediately and never
show up in a restore prompt. A mapping that is unreadable or malformed is
still deleted on sight — there's nothing restorable in it.

Pane-dead detection is host-side: one batched `tmux list-panes -s`
call per tick via `tmux.window_pane_map(session)` returns the set of
live pane ids per window. **Pane death is asymmetric by slot**: the
*default* slot's pane dying flags the whole window `X` (today's
single-agent behavior, unchanged; restore/revive fixes it) — either because
the window itself has no live panes, or because the mapping's slot-0
`pane_id` is absent from the window's live set (an overview pane, or a live
secondary, keeps the window alive after the default agent pane exits). A
*secondary* slot's pane dying does **not** flag the window `X`; it is
handled by the dead-slot transaction in "Lock discipline" below.

## Window mapping — schema 2

`WindowMapping` (`windows.py`) is `schema: 2`: it carries an `agents: list[AgentSlot]`
instead of a single flat `pane_id`/`claude_session_id` pair. Slot 0 is the
project's **default agent** (`Project.agent`, `claude` or `codex`); slot 1,
when present, is the **secondary** started by `Ctrl-Space O`.

```json
{
  "project": "api", "branch": "feature-x", "host_worktree": "/…", "pane_id": "12",
  "schema": 2,
  "agents": [
    {"kind": "claude", "pane_id": "12", "session_id": "uuid-…"},
    {"kind": "codex",  "pane_id": null, "last_pane_id": "15", "session_id": "0199-…"}
  ]
}
```

Each `AgentSlot` is `{kind, pane_id, session_id?, last_pane_id?}`, pane ids
stored **stripped** (no `%`), matching the existing convention. Terminology:
a **dead slot** means exactly one thing — a *secondary* slot persisted with
`pane_id: null`. `last_pane_id` is an orthogonal, optional
cleanup-pending pointer (which pane's per-pane files still need deleting);
it can outlive the death that set it (crash recovery) and a revive
deliberately does not clear it. A default (slot 0) pane dying is **not** a
dead slot — it renders `X` and is fixed by restore/revive, exactly as
before schema 2. **A mapping with only slot 0 is the normal single-agent
window**; the absence of a secondary never implies repair is needed.

**Back-compat.** `WindowMapping.__post_init__` synthesizes a single
`claude` slot from the legacy flat `pane_id`/`claude_session_id` fields
when `agents` is absent, so old on-disk snapshots restore untouched.
`to_dict()` keeps writing the legacy `pane_id` mirrored from slot 0, and
mirrors `claude_session_id` only when slot 0's kind is `claude` — a
downgrade to a pre-schema-2 build must never `claude --resume` a Codex
session id. Documented limitation: downgrading past a codex-default
window makes the old version start a fresh Claude session there instead
of resuming anything.

## Lock discipline

Five locks, never nested in more than one order:

- **Two `fcntl` locks** (`locks.py`) used for window mapping and per-worktree
  cleanup (detailed below).
- **`<config_dir>/codex-hooks.lock`** — guards host-side Codex hook
  (re)provisioning in `codex_hooks.ensure_host()`. This lock never nests with
  the other two; it's held only during `ensure_host()`'s atomic script +
  hooks.json writes.
- **`<state_dir>/sbx-daemon.lock`** (`paths.sbx_daemon_lock`) — serializes
  sbx daemon startup (`sandbox.ensure_daemon`): status probe → detached
  start → bounded readiness poll as one critical section. Never held
  together with any other lock.
- **`<state_dir>/sbx-create-<name>.lock`** (`paths.sbx_create_lock`) —
  per-sandbox-name lifecycle lock: `sandbox.ensure_up`'s inspect → create →
  re-inspect AND `sandbox.recreate`'s rm → create each run as one critical
  section, closing the check-then-create race `container.ensure_up` has
  and keeping rebuild's recreate from interleaving with a concurrent
  `ensure_up` (whose create would then receive the state import). Never
  held together with any other lock (in particular, `ensure_daemon` is
  always called *before* `ensure_up`/`recreate`, never inside them).

The `fcntl` locks are:

- **`<window_id>.json.lock`** (`paths.window_mapping_lock`) — a stable
  sibling of `windows/<window_id>.json`, never the JSON file itself (its
  inode is replaced by every atomic write, which would let a second
  locker in). Taken **only** inside `windows.update_mapping` /
  `windows.delete_mapping`, each call self-contained: read, apply the
  callback, tmp+rename. No caller holds it across other work.
- **`<worktree>/.local/.tmux-agents/.cleanup.lock`** (`paths.worktree_cleanup_lock`)
  — guards **destructive per-pane file cleanup and slot-liveness
  publication** for one worktree; deliberately *not* the lifecycle hooks'
  own state/session writes or the registry's marker GC, which stay
  lock-free.

**Global order: cleanup lock first, mapping lock second, always.** Because
the mapping lock is acquired only inside the self-contained
`update_mapping`/`delete_mapping` calls, no code path holds it while trying
to acquire the cleanup lock, and nothing calls `update_mapping`
re-entrantly — so the inversion deadlock (one worker cleanup→mapping,
another mapping→cleanup) can't happen by construction.

Holders, in order of how often they run:

- **The tick's dead-secondary transaction** (`state_tick._mark_secondary_dead`):
  when a secondary's pane has died, takes the cleanup lock, then a single
  guarded `update_mapping` call that re-reads the mapping and mutates the
  slot **only if it still holds the observed `(kind, pane_id)`** (a
  compare-and-set) — if `agent-other` revived it in between, this is a
  no-op. On a hit: merge the pane's `session-<pane>.id` into the slot,
  set `pane_id: null`, `last_pane_id: <old pane>`, then (cleanup lock still
  held) delete that pane's per-pane files.
- **The tick's cleanup-pointer sweep** (`_sweep_cleanup_pointers`): for
  every slot still carrying `last_pane_id` (live or dead), re-derives its
  collision inputs fresh under the lock and resolves each pending pointer
  one of three ways. **Mapped elsewhere** — `last_pane_id` is now claimed
  by any slot of the same worktree, live or dead (a second window can map
  the same worktree): ownership of the on-disk files already transferred
  to that slot, so the sweep clears ONLY its own pointer via
  `update_mapping` (a compare-and-set — an overlapping older sweep can't
  clear a newer death's pointer) and deletes nothing. **Unmapped but
  tmux-alive** — genuinely ambiguous (the liveness isn't explained by any
  mapping yet): skip entirely, retry next tick. **Otherwise** — scrub, then
  verify via `startup.pane_files_absent` that the state/session/pending
  files are actually gone before clearing the pointer; a `scrub_pane_files`
  that silently left a survivor (e.g. `rmtree` swallowing a permission
  error) keeps its pointer for a retry instead of losing it. The
  complementary half of the aliasing guard: every pane-spawn path
  (`agent-new`, `agent-other`, restore) scrubs stale per-pane files for its
  *assigned* pane id against the resolved worktree path immediately before
  launching into it — so even a missed sweep can't leave a recycled pane id
  pointing at a dead agent's stale files. (`rebuild` is deliberately not in
  that list — see its own section below.)
- **The tick's dead-window prune** (`_prune_windows_and_worktree_files`):
  for each dead window's mapping, takes that worktree's cleanup lock and
  re-derives the same protected set fresh under it — pane ids still
  claimed by a live window's mapping of the same worktree, union every
  currently-alive tmux pane id session-wide — before scrubbing each slot's
  `pane_id` and `last_pane_id`. A pane id in the protected set survives
  pruning even though the dead window's own mapping file is still removed
  (via `windows.delete_mapping`, not a bare unlink).
- **`agent-other`'s start branch** (Section "Start/switch — `agent-other`"
  below): holds the cleanup lock across eligibility re-check, split,
  scrub, respawn, and publish (a single `update_mapping` call as the last
  step) — so two near-simultaneous invocations serialize; the loser
  re-reads a mapping whose secondary is now live and falls through to a
  focus-jump instead of splitting a second pane.

## Spawn flow — `agent-new`

`agent-new` is a **two-mode entry point**: the interactive mode (popup)
returns immediately after creating the window; all slow work runs in a
detached `--provision` worker, mirroring restore's cheap-pre-create /
slow-activate split.

### Interactive mode (popup)

Steps in `commands/new.py` order:

1. **Parse args.** `<project>` and `<branch>` are both optional. If
   `<project>` is missing, fzf-pick from `projects.toml`; then fzf-pick
   `<branch>` from the project's existing worktrees (`worktree.list_existing`),
   with a sentinel `[no branch — use repo root]` pinned at the top and
   an `(open)` suffix on worktrees that already have a live agent window
   (`windows.live_branches_for`). Typed input that doesn't match a
   candidate is treated as a new branch (validated by `git
   check-ref-format`).
2. **Validate.** `git check-ref-format --branch` rejects malformed
   branch names early. Unknown project → exit 2.
3. **Ensure session.** Create the `agents` tmux session if it doesn't
   exist (window 0 = `ctrl`).
4. **Placeholder window.** `tmux.new_window` creates the window
   immediately with a placeholder pane running
   `startup.placeholder_command(window_id)` — a `tail -F` on the per-window
   spawn log (`paths.spawn_log(window_id)`, i.e.
   `<state_dir>/spawn-<window_id>.log`), pre-created empty so tail never
   prints "cannot open …" noise while the worker starts. Window name is `<project>` (no
   branch) or `<project>:<branch>`. To keep same-project windows
   contiguous, `_last_sibling_window_id` passes the highest-indexed
   sibling as `after_target`; `renumber-windows on` collapses the
   resulting indices.
5. **Window mapping.** `windows.write_mapping` records
   `(window_id → project, branch, host_worktree=proj.repo, pane_id,
   phase_hint="starting")`. The provisional `host_worktree` is the
   repo root (worktree path is not yet known); `phase_hint` drives the
   `S` letter until the worktree state file is written.
6. **Bottom pane** *(split layout only).*
   `overview.attach_overview_pane` adds the `agent-overview` pane (initial
   25% split; the TUI immediately re-fits it to content — see Layouts)
   and tags it `@role=overview`.
7. **Switch.** `tmux.select_window` makes the new window active.
8. **Spawn worker.** `_spawn_worker` launches `agent-new --provision …` via
   `tmux.run_shell_bg` (`run-shell -b`, i.e. parented by the long-lived tmux
   server) and returns — the popup closes immediately. It must NOT use
   `subprocess.Popen` from inside the popup: tmux tears down the popup's
   process tree on close and would kill the worker (even with
   `start_new_session=True`) before it does any work, stranding the window
   in `S`.

### `--provision` worker (detached)

The worker forks, calls `os.setsid()`, detaches stdio via
`startup._detach_stdio()`, then runs `_provision()`. Progress is written
to the spawn log (`paths.spawn_log(window_id)`); the placeholder pane's
`tail -F` shows it live.

1. **Container up** *(container projects only).* `container.ensure_up`
   resolves the running container (by name OR `devcontainer.local_folder`
   label); runs `up_cmd` if down.
2. **SSH pump** *(container projects with `forward_ssh_agent`, default).*
   `ssh_forward.maybe_spawn_pump` probes host `$SSH_AUTH_SOCK` and
   `python3` in the container; if both present, launches a detached host
   pump that `docker exec`s the relay as `-u {user}`.
3. **gh auth** *(container and sandbox projects with `share_gh_auth`,
   default).* `gh_auth.maybe_sync_gh_auth` (docker exec) /
   `maybe_sync_gh_auth_sandbox` (`sandbox.exec_capture`) reads the host's
   gh token (`gh auth token`, keyring-backed) and pipes it via stdin into
   `gh auth login --with-token` in the target. The sandbox variant first
   probes for a runtime-injected `GH_TOKEN` (the sbx runtime provisions a
   proxy-managed one, and `gh auth login` refuses to run while it's set)
   and short-circuits to "already authenticated" when present. Non-fatal:
   any missing prerequisite (gh on host, host login, gh in the target) or
   sync failure emits a stage warning and the agent starts without it. The same stage
   runs in `agent-restore` and `agent-rebuild`, so a rebuilt container or
   recreated sandbox (which loses its `~/.config/gh/hosts.yml`) is
   re-authed automatically.
4. **Worktree.** `worktree.resolve` returns `<repo>` if no branch, else
   `<repo>/.worktrees/<branch>`. If the worktree dir doesn't exist,
   `git worktree add -B <branch> <target> <commit-ish>` is run. The
   commit-ish comes from `_resolve_base()`: by default it fetches
   `origin/<default>` where `<default>` is read from `origin/HEAD`
   (falling back to `git remote set-head origin -a` on first run, then
   to `init.defaultBranch` + local HEAD as last resorts). An optional
   `base_branch` field in `projects.toml` overrides auto-detection.
   Offline runs degrade to the cached `origin/<base>` with a warning.
   All git invocations run via `docker exec` for container projects.
   The two paths that hand an agent a checkout **without** creating a
   fresh worktree — no-branch mode (runs the default agent in `<repo>`
   as-is) and reuse of an existing `.worktrees/<branch>` — instead run
   `worktree.check_freshness`: a best-effort `git fetch origin <default>`
   + `git rev-list --count HEAD..origin/<default>` that emits a stage
   **warning** (holding the pane for Enter) when the checkout is behind,
   so a stale base is surfaced rather than silently inherited. It never
   modifies the working tree and degrades to an info line offline.
   **After resolve**, the mapping is rewritten with the real
   `host_worktree` and `phase_hint=None` (the worktree state file now
   takes over).
5. **Provision Claude hooks.** `provisioning.provision_settings` merges
   `hooks/agents.json` into `<worktree>/.claude/settings.local.json`
   (idempotent; non-fatal on failure — emits a warning to the log).
6. **Provision Codex hooks.** `codex_hooks.ensure_host()` (host projects)
   or `codex_hooks.ensure_container(container_name, user)` (container
   projects) — idempotent, non-fatal on failure. Runs **regardless of the
   project's default agent kind**, so the secondary is already provisioned
   the first time `Ctrl-Space O` is pressed. Digest + canonical-structure
   check, not presence: a mutated script or hand-edited `hooks.json` heals
   on the next call rather than needing a version sidecar.
7. **Respawn.** Once the log file is closed:
   - No warnings → `startup._respawn_with_retry` swaps the pane into the
     real `exec_cmd.build(...)` for slot 0's kind (`proj.agent`).
   - Non-fatal warning → `startup.hold_pane_then_exec` shows the log
     plus a "press Enter to launch" prompt (pane state shows `W`), then
     `exec`s into the agent on Enter.

Failure modes:
- **Fatal before worktree exists** → `startup.show_static_text` replaces
  the placeholder with an error message; the mapping's `phase_hint` is
  set to `"errored"` so the overview shows `X`.
- **Fatal after worktree exists** → same static error pane, but
  `startup._write_pane_state` writes `phase=errored` to the worktree
  state file (which then takes precedence over the hint).
- Config error (exit 2), container error (exit 4).

## Module map

`src/tmux_agents/` — one responsibility per file. Add new tmux/docker
shell-outs to the dedicated module rather than inline.

| Module | Owns |
|---|---|
| `paths.py` | All filesystem locations. Env-overridable via `TMUX_AGENTS_CONFIG_DIR` / `TMUX_AGENTS_STATE_DIR`. Every path used elsewhere goes through this, including the two lock paths (`window_mapping_lock`, `worktree_cleanup_lock`). |
| `state.py` | The seven display-letter constants (`R`/`W`/`B`/`Z`/`I`/`X`/`S`). |
| `phase.py` | Bridges hook-written `phase` JSON + registry background/sleeping counts + `pane_alive` → per-slot display letter (`derive_letter`), plus `combined_letter` (highest-priority letter across a window's live slots), applying the same `X>W>R>B>Z>I>S` priority rule at both levels. |
| `agent_kind.py` | The two agent kinds (`CLAUDE`, `CODEX`) and per-kind knowledge: `executable(kind)`, `resume_args(kind, session_id)` (claude: ` --resume <id>` flag; codex: ` resume <id>` subcommand), `other(kind)` (the opposite kind), and the `AGENT_MARKER` (`TMUX_AGENTS_AGENT`) env-var name exported by every default exec template. Nothing else hardcodes `"claude"`/`"codex"`. |
| `locks.py` | The single `locked(path)` `fcntl` context manager for the two `fcntl` locks (window mapping and per-worktree cleanup). Docstring states the global order: cleanup lock first, mapping lock second. `codex-hooks.lock` is also a `fcntl` lock but is never held alongside the other two. |
| `registry.py` | Scans a pane's `pending-<pane>/` marker dir, computes each marker's effective expiry (exact from `scheduledFor`/cron-expr where possible, heuristic timeout otherwise), GCs expired ones, returns live background/sleeping counts. Uses `croniter` for one-shot cron next-fire (host-side, local TZ). Claude-only in practice — Codex slots never populate this directory. |
| `theme.py` | Color palette. Dark + light defaults, optional `theme.toml` overrides, derived ANSI/tmux/contrast variants for active-row inversion. Cached per-process. |
| `tmux.py` | Sole module that shells out to `tmux -L agents`. Window/pane listings, capture, rename, kill, option setters, `prefix_label` (humanized, process-cached prefix name for hint strings), and `split_window(target, *, percent, command, before=False, horizontal=False, full_size=False)` — `horizontal` → `-h` (agent-other's 50/50 side-by-side), `full_size` → `-f` (the overview's full-width bottom split under a dual-agent window). |
| `windows.py` | `WindowMapping`/`AgentSlot` — the `<config_dir>/windows/<window_id>.json` mapping, schema 2 (`agents: list[AgentSlot]`, slot 0 = default agent; see "Window mapping — schema 2" above). `update_mapping(window_id, fn)` / `delete_mapping` are the only mutators, each taking `window_mapping_lock` internally. `__post_init__` (triggered via `from_dict` construction) synthesizes a legacy single-claude-slot mapping when `agents` is absent. `forget(window_id)` tears down mapping + slot files together; it removes an agent from the restore snapshot, so only `agent-kill` and the tick's grace-period GC may call it. |
| `config.py` | `projects.toml` loader. Resolves the **backend enum** (`Project.backend` ∈ `BACKEND_HOST`/`BACKEND_CONTAINER`/`BACKEND_SANDBOX`; `is_container` is derived from it) from `container` vs `devcontainer = true` vs `sandbox = true` (sandbox is mutually exclusive with the container keys + `user`/`container_workdir`/`up_cmd`), fills in defaults (`up_cmd`, `exec_cmd`, `codex_exec_cmd`, `container_workdir`, `user`, `forward_ssh_agent`, `share_gh_auth`) per backend. Sandbox extras: `sbx_template`/`sbx_kits`/`sbx_memory` (strict-typed) and `sbx_mounts` (normalized to canonical `path[:ro]` argv strings — `~` expanded in Python, resolved, duplicates/missing rejected); `sandbox_name` = project name. Reads top-level `default_agent` and per-project `agent`/`codex_exec_cmd` (both validated against `agent_kind.KINDS`, `ConfigError`/exit 2 otherwise); `Project.exec_cmd_for(kind)` and `exec_cmd_explicit`/`codex_exec_cmd_explicit` (the latter pair tells `agent-other` whether its executable pre-flight is meaningful); `forward_ssh_agent_explicit` (sandbox mode warns only on an explicit key). The optional `base_branch` field is stored on `Project` and forwarded to `worktree.resolve` as `base_override`. |
| `container.py` | Docker probes: `is_running`, `current_name` (by name OR `devcontainer.local_folder` label), `ensure_up` (runs `up_cmd` once if down), `exec_capture` (run a command inside the container as a given user and capture stdout — used by `agent-other`'s executable pre-flight and by `codex_hooks.ensure_container`), and `rebuild` (force-recreate: devcontainer projects append `--remove-existing-container` [+ `--build-no-cache`] to `up_cmd`; named-container projects `docker rm -f` then re-run `up_cmd`). |
| `sandbox.py` | Sole module that shells out to `sbx` (Docker Sandboxes). Primitives: `exec_capture(name, script, stdin=…)` (auto-starts a stopped sandbox) and atomic `deliver` (unique mktemp + rename, same guarantees as the container delivery). Lifecycle: `daemon_running`/`ensure_daemon` (global `sbx-daemon.lock`, bounded readiness poll — the daemon doesn't auto-start at boot), `is_present` (parses `sbx ls -q`; no inspect verb exists), `ensure_up(proj)` (per-name `sbx-create-<name>.lock` around inspect → create → re-inspect — no check-then-create race; returns True iff CREATED, which callers treat as "fresh VM: logins/sessions gone"), `recreate` (rm → create as ONE critical section under the same per-name lock), `remove` (`rm --force`). State carry for rebuild: `export_state`/`import_state` (base64'd tar of `~/.claude` minus the shared `skills` mount and kit-regenerated `settings.json`, plus codex sessions/history/auth.json — never codex `config.toml`; the tar is staged in a temp file so its own exit status gates the base64 — sh has no pipefail, and a truncated archive must never look like a saved one). `CODEX_LOGIN_RUNBOOK` is the held-pane text for codex slots landing in a fresh sandbox (shared by rebuild + restore). Every call has a bounded timeout and stdin closed unless piping; the three user-fixable failures (not installed / `sbx login` expired / daemon down) raise `SandboxError` with the exact remediation, everything else passes sbx stderr through. |
| `exec_cmd.py` | Shared builder `build(proj, *, branch, session_id, container_name, kind=agent_kind.CLAUDE, label="")` for the pane launch command, injecting the kind's resume snippet via the `{resume_args}` placeholder (`agent_kind.resume_args`). Used by `agent-new`, `agent-other`, `agent-restore`, and `agent-rebuild` so resume semantics stay identical across every spawn path. |
| `worktree.py` | `git worktree add/remove`. `_resolve_base()` determines the commit-ish for new worktrees (fetch `origin/<default>` → cached ref → HEAD fallback). For container projects, runs git via `docker exec` so the worktree's internal `.git` pointers are container paths. `remove` classifies git failures: dirty → `DirtyWorktreeError` (force-retryable), "is not a working tree" → `NotAWorktreeError` (a husk dir git doesn't know — only leftover provisioned files; `remove_leftover` rmtrees it host-side). |
| `provisioning.py` | Idempotent merge of `hooks/agents.json` into `<worktree>/.claude/settings.local.json`. Versioned by package version so upgrades replace stale hook groups. Claude-only; Codex's user-level provisioning is `codex_hooks.py`. |
| `codex_hooks.py` | User-layer Codex hook provisioning: `ensure_host()` / `ensure_container(container_name, user)` / `ensure_sandbox(name)` (the container/sandbox twins share `_ensure_remote`, parameterized by the backend's cat/deliver primitives). Writes the package-owned `codex-hook.sh` **outside every workspace** (`~/.config/tmux-agents/codex-hook.sh` host, `<container home>/.codex/tmux-agents/codex-hook.sh` container) and merges owned entries into `~/.codex/hooks.json` (host and container home) by exact structural command match (`sh '<abs path>/codex-hook.sh' <action>` — matches regardless of which action word, so a renamed/removed action from an older package version is still recognized and cleaned up). "Ensure" verifies script digest + canonical owned-subset structure, not mere presence, so a mutated script or hand-edited hooks file self-heals on the next call; container writes go through a unique-`mktemp`-then-rename helper (no lock needed — content is deterministic per package version). |
| `hooks/agents.json` | Package data: the hook *dispatch* table (`tui: fullscreen` + per-event invocation of `write-state.sh`). Shipped, not generated. |
| `hooks/write-state.sh` | Package data: the actual shell body the *Claude* hooks invoke. Provisioned per worktree at `<worktree>/.local/.tmux-agents/write-state.sh`. Single source for the phase-JSON write + the registry `add-`/`del-` marker subcommands (extracting ids/signals from the hook payload via constrained sed). All counting/expiry/cron-parsing is host-side in `registry.py`. |
| `hooks/codex-hook.sh` | Package data: the shell body the *Codex* hooks invoke (provisioned by `codex_hooks.py`, not per-worktree). Phase writes (`running`/`waiting`/`idle`) + `init` (session-id pin) + bell on `waiting`; no registry markers. No-ops unless `TMUX_PANE` is set, `$PWD/.local/.tmux-agents` exists, and `TMUX_AGENTS_AGENT=1` is exported — the last guard is what stops a manual `codex` run inside `agent-terminal` from corrupting a pane's phase/pin. |
| `pickers.py` | fzf-backed primitives (`pick_one`, `prompt_yes_no`, `pick_or_create`, `prompt_free_text`) plus `NO_BRANCH_SENTINEL`. Used by `agent-new` / `agent-kill` / `agent-rebuild`. No tmux/project knowledge. |
| `overview.py` | Row model (header / agent, with `slots: list[SlotState]` per agent row), `parse_state_code` (splits a `|`-joined `@state_code` into one `SlotState` per live slot), `format_line_plain` / `format_header`, the status-line summary renderer (`render_summary`, called from `state_tick`), fold persistence, and the curses TUI for the split-layout bottom pane: cursor model, per-slot state-colored rendering (letters joined by a dim `|`), click hit-testing, keyboard dispatch (↑↓ ↵ a/k/r/e/o, uppercase aliases), `attach_overview_pane` (`@role=overview`, idempotent), and content-fit auto-resize (`desired_pane_height` + `refit_self_pane`, publishing `@overview_rows` for the window-resized hook's `overview-refit` script). The TUI auto-tracks the active window unless the user moved the cursor. Repo headers count **live slots**, not windows (`format_header`'s `N agent(s)`). |
| `ssh_forward.py` | Probes + pump spawn for SSH agent forwarding. Spawns the pump as `python -m tmux_agents._ssh_pump_script`; the pump delivers the relay into the container as plain files (no inlining). |
| `gh_auth.py` | One-shot host→container/sandbox gh token sync (`maybe_sync_gh_auth` via docker exec, `maybe_sync_gh_auth_sandbox` via `sandbox.exec_capture`). Reads the host token via `gh auth token` (keyring-backed), pipes it via stdin into `gh auth login --with-token` in the target — never on argv or host disk. Always overwrites — except the sandbox variant, which short-circuits to `already_authenticated` when the sbx runtime injected its own `GH_TOKEN` (gh refuses `auth login` while it's set). Every failure is a non-fatal `SyncResult` mapped onto a progress stage. Gated by the per-project `share_gh_auth` flag (default on). |
| `_ssh_framing.py` | Wire framing (4-byte length prefix + payload, `\x00\x00\x00\x00` sentinel) and the bidirectional `splice()` between a raw UDS socket and a framed stream pair. |
| `_ssh_pump_script.py` | Host-side pump. For each in-container SSH op, opens a fresh connection to the host's `$SSH_AUTH_SOCK` and splices it. |
| `_ssh_relay_script.py` | In-container relay. Bind-or-exit dedup at `/tmp/tmux-agents-ssh.sock`, accepts client connections, splices each through stdin/stdout to the pump. |
| `startup.py` | Shared spawn/restore primitives used by `agent-new`, `agent-other`, and `agent-restore`: `placeholder_command` (build the `tail -F` pane command), `_respawn_with_retry` (fork-safe respawn with backoff), `_detach_stdio` (redirect fds 0/1/2 to `/dev/null` in a backgrounded worker), `_write_pane_state` (write a `phase=…` state JSON), `show_static_text` (respawn pane into a static heredoc), `hold_pane_then_exec` (show log + "press Enter" prompt, then exec), `scrub_pane_files(worktree, pane_id)` (delete a pane's state/session/pending files under the resolved worktree — required by every spawn path, and by the tick's dead-slot sweep, before (re)launching an agent into a pane id that may have been recycled). |
| `progress.py` | Per-stage progress display. `Reporter` writes to a single output stream; `MultiReporter` fans out to N reporters for events shared across restore's project groups. Symbols: `▸` info / `✓` success / `!` warning (non-fatal) / `✗` fatal failure. Both `agent-new --provision` and `agent-restore` write each window's progress to `<state_dir>/spawn-<window_id>.log` (`paths.spawn_log`); the placeholder pane runs `tail -F <log>` and is replaced by `respawn-pane` when the worker finishes. |
| `commands/restore.py` | The `agent-restore` worker. Snapshot reading + validation, per-slot plan (`EntryKind` for slot 0, independent `SlotAction` for an existing secondary), a global session-id harvest barrier across every slot of every entry before any pane is touched, project grouping, placeholder pre-creation (dual split for two-slot entries), backend-dispatched bring-up + per-entry respawn (kind-aware, via `exec_cmd.build`): container ensure-up + SSH pump + gh auth sync for container projects; for sandbox projects `sandbox.ensure_daemon` once before the parallel wave, then per-group `sandbox.ensure_up` + gh auth sync — a DELETED sandbox is recreated fresh (exec auto-start only covers stopped), and when that happened the session-id clearing is PERSISTED into each mapping (not just omitted from the respawn) and codex slots are held on `sandbox.CODEX_LOGIN_RUNBOOK` (errored) instead of launching codex into an auth error loop. Codex hook ensure-provisioned per backend, failure logging + error display. Imports shared primitives from `startup.py`. |
| `commands/rebuild.py` | `agent-rebuild`. Interactive half (popup): eligible-project picker with live agent tallies (eligible = sandbox, devcontainer, or named container with explicit `up_cmd`), tiered confirm (default-No when **any live slot** is `R`/`W`/`B`; sandbox phrasing notes state is exported/restored), then fires the detached worker via `tmux run-shell -b`. `--worker` half (parented to the server): fork/setsid/detach-stdio (same as `agent-new --provision` — otherwise tmux paints the worker's output, e.g. `devcontainer up` JSON, over the active pane in view mode), then show `tail -F` progress in each affected pane and dispatch on backend. Container: `container.rebuild`, respawn the SSH pump + gh auth sync, Codex hook ensure-provisioned, re-exec **every live slot's** pane via `exec_cmd.build` (kind-aware resume). Sandbox (`_run_sandbox_worker`): **state-preserving recreate** — daemon first (so a daemon-down failure never suggests `--discard-state`), `sandbox.export_state` (abort without deleting anything on failure, unless `--discard-state`) persisted to `paths.sbx_rebuild_backup(name)` (0600, fsync'd) BEFORE the rm so a worker crash can't lose it, `sandbox.recreate` (rm → create as one locked critical section; new template/mounts/memory take effect), `import_state` (success deletes the backup; failure keeps it for manual recovery and degrades to fresh: session ids cleared in the mappings, codex slots held on `sandbox.CODEX_LOGIN_RUNBOOK` marked `X`), `codex_hooks.ensure_sandbox`, gh auth sync (the recreate wiped the sandbox's gh login — deliberately not in the state export), respawn; `--no-cache` warns and is ignored. Per-pane failures isolated; a bring-up failure marks every pane `X`. Deliberately does **not** scrub pane files: every slot it touches is a live, still-mapped pane being resumed in place, not a possibly-recycled id, so its state/session files must survive the rebuild. |
| `commands/other.py` | `agent-other`. One smart action on the active window (Section "Start/switch — `agent-other`" below): notice-and-stop when there's no mapping or the default pane is dead; focus-jump when both slots are live; otherwise ensure-provisioned + executable pre-flight + placeholder-first split + scrub + respawn + publish-last, all under the per-worktree cleanup lock. |
| `commands/*.py` | Thin CLI orchestrators (one per `[project.scripts]` entry). Logic lives in the modules above. |

### How `_ssh_*.py` reach the container

Tests import them as `tmux_agents._ssh_*`. The host pump runs as `python -m
tmux_agents._ssh_pump_script` (package importable on the host). For the
container, the pump delivers `_ssh_framing.py` + `_ssh_relay_script.py`
verbatim into `/tmp/tmux-agents-relay/` (piped via `docker exec … cat >`) and
runs `python3 …/_ssh_relay_script.py`; the relay finds framing as a sibling on
`sys.path[0]` via its import fallback (`from tmux_agents._ssh_framing import …`
→ `from _ssh_framing import …`). No source is spliced — no regex strip, no
`RELAY_SCRIPT_SOURCE` injection. `test_ssh_relay.py` exercises the
delivered-file import path under `python -E -S`.

## CLI entry points

| Command | Owner | Purpose |
|---|---|---|
| `agents` | `commands/launcher.py` | Probe live session / snapshot, prompt user on stale snapshot, orchestrate restore handoff (`agent-restore --background`) before `execvp` into `tmux attach`. Falls through to plain `new-session -A` when no snapshot exists. Primary entry point. |
| `agent-new [<project> [<branch>]]` | `commands/new.py` | Two-mode entry point. **Interactive** (popup): fzf-pick project/branch, create window immediately with a placeholder pane tailing the spawn log (`spawn-<id>.log`), write mapping with `phase_hint="starting"` and slot 0's kind (`proj.agent`), attach overview pane, select window, spawn detached `--provision` worker, return. **`--provision` worker**: fork/setsid/detach-stdio, then backend bring-up (container ensure-up + SSH pump + gh auth sync; or sandbox `ensure_daemon` + `ensure_up` + gh auth sync, no pump — sbx forwards the host agent natively) + worktree resolve (or a `check_freshness` base-staleness check in no-branch mode; host-side for sandbox — passthrough) + Claude hooks provision + Codex hooks ensure-provisioned per backend (always, regardless of `proj.agent` — readies the secondary before it's ever requested), writing progress to the spawn log; respawn the placeholder into the default agent on success, hold for Enter on warning (`W`, e.g. a checkout behind `origin/<default>`), show error pane on fatal failure (`X`, with sbx remediation text verbatim for sandbox failures). |
| `agent-kill [<window>] [--prune-worktree] [--force]` | `commands/kill.py` | fzf picker by default; can target by `--window-id`. Kills the whole window (every live slot). Optional `git worktree remove` (interactive force-retry on dirty; a husk dir git doesn't know offers delete-folder-or-keep, then kills either way instead of wedging). |
| `agent-rebuild [<project>] [--project N] [--no-cache] [--discard-state] [--yes] [--worker]` | `commands/rebuild.py` | Rebuild a project's shared container/sandbox and resume its agents. **Interactive** (popup): fzf-pick an eligible project (sandbox, devcontainer, or named container with `up_cmd`) showing its live agent tally, warn+confirm (default-No when any live slot is actively working — `R`/`W`/`B`), then spawn the detached `--worker` via `run-shell -b` so it survives the popup closing. **`--worker`**, container: show `tail -F` progress in each affected pane, `container.rebuild` (force-recreate), respawn the SSH pump + gh auth sync, Codex hooks ensure-provisioned, `respawn-pane` **every live slot's** pane via `exec_cmd.build` (kind-aware resume). Sandbox: state-preserving export → recreate → import (destructive only as fallback or with `--discard-state`; `--no-cache` warns + is ignored). Bound to `Ctrl-Space b`. |
| `agent-state` | `commands/state_tick.py` | Single tick of the host poll. Wired into `status-right` so tmux runs it every status interval. |
| `agent-overview` | `commands/overview.py` | Curses TUI for the split-layout bottom pane. The status-line summary is emitted inline by `agent-state` — `render_summary` is called as a function, not via this CLI. |
| `agent-rename --window-id <id> [--from-hook] <name>` | `commands/rename.py` | Replace the `:branch` half of `<repo>:<branch>`. Explicit (non-hook) renames set the `@pinned` window option; `agent-new` and `agent-restore` also set it when a branch is supplied at creation. `--from-hook` is the `pane-title-changed` mode that silently no-ops on ctrl/`@pinned`/unknown windows or empty names — so the hook keeps tracking Claude's titles on unpinned windows but never overwrites a branch label. |
| `agent-layout` | `commands/layout.py` | Toggle persistent layout file (`<state_dir>/layout`) between `split` and `compact`; rebuilds existing windows accordingly — kills only `@role=overview` panes going to `compact` (never by pane index), so a dual-agent window's two agent panes both survive. |
| `agent-restore [--background]` | `commands/restore.py` | Read snapshot, harvest every slot's on-disk session id (barrier, before any pane is touched), pre-create placeholder windows (dual split for two-slot entries; overview pane in split layout), run devcontainer `up_cmd`s in parallel, spawn the SSH pump + gh auth sync per container project (sandbox projects instead get `ensure_daemon` once before the wave + per-group `ensure_up`, recreating deleted sandboxes and clearing stale resume ids), ensure Codex hooks per backend, `respawn-pane` each slot with its own kind's resume command. Triggered automatically by the launcher; runnable manually for partial-failure retry, dead-pane recovery, or a secondary-only repair, bound to `Ctrl-Space r`. |
| `agent-vscode --window-id <id> [--local]` | `commands/vscode.py` | Open the current agent's worktree in VS Code. Host projects → `code <host_worktree>`. Container / devcontainer projects → `code --folder-uri vscode-remote://attached-container+<hex>/<container_workdir>`, reattaching to the running container resolved by `container.current_name` (no rebuild, no second container). Sandbox projects → Remote-SSH: `code --remote ssh-remote+<name>.sbx <workdir>`, preflighted via `ssh -G` against the `sbx setup ssh` managed config (unconfigured → exact fix printed); `--local` opens the host-side worktree instead (passthrough — same files). Resolves the `code` binary via `shutil.which` first, then falls back to a top-level `code_path` in `projects.toml` (default: `/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code`). Bound to `Ctrl-Space v`. |
| `agent-terminal --window-id <id>` | `commands/terminal.py` | Pop up a shell in the active agent's context. Host projects → `os.chdir(host_worktree)` then `exec $SHELL -l` (fallback `/bin/bash`). Container / devcontainer projects → `os.execvp("docker", ["exec", "-it", "-e", "TERM", "-e", "COLORTERM", "-e", "TMUX_PANE", "-u", user, "-w", workdir, name, "bash", "-il"])`, with `-e SSH_AUTH_SOCK=/tmp/tmux-agents-ssh.sock` added when `forward_ssh_agent=True`. Sandbox projects → `sbx exec -it … <name> bash -lc 'cd <workdir> && exec bash -il'` — never a host shell (that would be a silent isolation hole). Container resolved via `container.current_name` (same as `agent-vscode`). Bound to `Ctrl-Space t` via `display-popup -E`. Note: this shell carries `TMUX_PANE` but **not** the `TMUX_AGENTS_AGENT` marker, which is deliberate — see "Honest limitations" below. |
| `agent-other --window-id <id>` | `commands/other.py` | Start, revive, or focus-switch the window's **secondary** agent (the kind other than slot 0's — Claude↔Codex). No mapping / default slot dead → `display-message` notice, no-op. Both slots live → focus-jump between the two agent panes. Secondary absent or dead → ensure-provisioned + executable pre-flight (skipped for a custom `exec_cmd`/`codex_exec_cmd`) + placeholder-first split/scrub/respawn/publish-last, under the per-worktree cleanup lock. Bound to `Ctrl-Space o` (and `o` in the focused overview pane; uppercase aliases work too). |

## Supported features

### Project types

`projects.toml` supports four modes, resolved to the backend enum
(`host | container | sandbox`) in `config.py`:

- **Named container.** `container = "name"` — `current_name` checks if it's
  running; `up_cmd` is required (no default).
- **Image / Dockerfile devcontainer.** `devcontainer = true` — resolved by
  the `devcontainer.local_folder=<repo>` label that VS Code's Dev
  Containers extension and the `devcontainer` CLI stamp. `up_cmd`,
  `exec_cmd`/`codex_exec_cmd`, and `container_workdir`
  (=`/workspaces/<repo-basename>`) default to the canonical
  devcontainer-CLI invocations.
- **Host-only.** No container fields. `exec_cmd`/`codex_exec_cmd` are
  optional; the defaults are `cd {workdir} && TMUX_AGENTS_AGENT=1 exec
  claude{resume_args}` / `... exec codex{resume_args}`.
- **Sandbox (Docker Sandboxes / sbx).** `sandbox = true` — each project
  gets its own microVM, created by `agent-new`/`agent-restore` via
  `sandbox.ensure_up` (`sbx create --name <project> … claude <repo>
  <sbx_mounts…>`; the agent positional is always `claude` — the codex slot
  logs in INSIDE the claude sandbox). Optional `sbx_template`, `sbx_kits`,
  `sbx_mounts` (`path[:ro]`, create-time only), `sbx_memory`. Default exec
  templates run `sbx exec -it … {sandbox} bash -lc 'cd {workdir} && exec
  <agent>{resume_args}'` for BOTH kinds; worktrees stay host-side
  (passthrough keeps host paths valid in the VM), the SSH pump never
  spawns, and the state pipeline works unchanged via `-e TMUX_PANE`. See
  `docs/SANDBOX-MODE.md` for the design and threat model.

Substitutions: `{repo}` → host repo path, `{container}` → resolved name,
`{sandbox}` → the project's sandbox name (empty for non-sandbox projects),
`{workdir}` → host path or container path with `.worktrees/<branch>`
appended (host path for sandbox projects), `{resume_args}` → empty for a
fresh agent, a kind-specific resume snippet (leading space included) when
reviving a conversation — ` --resume <session_id>` for Claude,
` resume <session_id>` for Codex (`agent_kind.resume_args`).

### Agent kinds

Top-level `default_agent = "claude" | "codex"` (absent → `"claude"`,
zero-migration) and per-project `agent` override which kind is the
project's **default** (slot 0). Invalid values fail config loading (exit
2). `agent_kind.py` is the single place that owns the two kind names,
their executable name (`claude`/`codex`, identical to the kind name), and
their resume-arg spelling — nothing else hardcodes either string. A
project's **secondary** agent (started by `Ctrl-Space O`) is always the
other kind; there is no third kind, though the design leaves room for one.

### Layouts

- **Split (default).** Each agent window has a top pane (one or two agent
  panes, side by side if both are live) and a bottom pane running
  `agent-overview`. `attach_overview_pane` always makes it a **full-width**
  bottom split (`-f -v`), so it spans both agent panes in a dual window
  instead of nesting under one of them; for a single-agent window this is
  the same result as before. The bottom pane is identical across windows,
  so the global overview is always visible. The pane is tagged
  `@role=overview` so the `MouseDown1Pane` binding forwards clicks to it
  without stealing pane focus from the agent, and `attach_overview_pane`
  is idempotent (no-op if the window already has one) so a layout toggle
  or restore re-attach can never leave a window with two.

  The overview pane auto-sizes to its content: `min(rows + footer, a
  quarter of the window height)`, floored at 2 (the sizing rule is
  `overview.desired_pane_height`). Two triggers keep it fitted, because
  tmux itself forgets the split ratio at creation time and crushes the
  bottom pane first when the terminal shrinks:
  - **Content changes** — the TUI publishes its desired content height as
    the `@overview_rows` pane option and resizes its own pane
    (`overview.refit_self_pane`, called each loop pass, no-op until the
    row count changes).
  - **Window resizes** — the `window-resized` hook in agents.conf runs
    `~/.config/tmux-agents/overview-refit` (shipped from
    `config/overview-refit`, installed next to `clipboard-copy`), which
    re-caps the published `@overview_rows` against the new window height.
    `resize-pane` changes only the layout, never the window size, so the
    hook cannot re-trigger itself. Manual pane resizes survive until the
    next content change or window resize.
- **Compact.** No splits. The status-right is `#(agent-state)` — a single
  format substitution. `agent-state` runs the host tick AND emits the
  summary chunk on stdout (in tmux-format, not ANSI: the substitution
  treats output as tmux format markup, so raw ANSI would render as
  literal escape codes). State letters get `#[fg=#…]` codes from the
  palette; a dual-agent window's letters are `|`-joined in the same format.

Layout choice persists at `<state_dir>/layout` (read by `agent-new` so new
windows match) and is toggled with `agent-layout` (Ctrl-Space L), which
kills only `@role=overview` panes on the way to `compact` — never by pane
index, so a window's one or two agent panes both survive the toggle.

### Start/switch — `agent-other`

`Ctrl-Space O` (and `O` in the focused overview pane) runs `agent-other
--window-id <id>`, one smart action on the active window (spec Section 4):

- **No mapping** (includes the `ctrl` window) → `display-message` notice,
  no-op.
- **Default slot's pane gone** → notice pointing at `Ctrl-Space R`
  (restore) — there's no live agent pane to split from.
- **Both slots live** → focus-jump between the two agent panes
  (`tmux.active_pane_id` + `tmux.select_pane`; defaults to the secondary
  if the active pane is neither). Wrapped so a `TmuxError` from a pane
  dying mid-jump becomes a friendly notice, not a traceback.
- **Secondary absent or dead** → *start it*:
  1. Ensure-provisioned (Codex hooks, if the other kind is Codex).
  2. Executable pre-flight (`command -v <exe>` on host or via
     `container.exec_capture`) — skipped when the relevant `exec_cmd`/
     `codex_exec_cmd` is a custom command (a fixed-name check proves
     nothing about an arbitrary one). Missing → friendly notice, no pane
     created. Steps 1–2 are idempotent, worktree-state-free checks and
     deliberately run BEFORE the per-worktree cleanup lock — nothing
     about them needs serializing, and running `ensure_host` (which
     holds `codex-hooks.lock`; `ensure_container` is deliberately
     lock-free) under the cleanup lock would nest the two locks.
  3. Acquire the per-worktree cleanup lock (serializes a start-vs-start
     race — the loser re-reads a now-live secondary and falls through to
     focus-jump instead of splitting a second pane); re-read the mapping
     and re-check eligibility.
  4. `tmux.split_window(default_pane, percent=50, horizontal=True,
     command=startup.placeholder_command(...))` — an inert placeholder,
     the same idiom `agent-new` uses, so "scrub before launch" is
     enforceable (a split's command starts before the pane id is even
     returned).
  5. `startup.scrub_pane_files` the new pane id against the resolved
     worktree.
  6. `exec_cmd.build(..., kind=other_kind)` — with the dead slot's
     retained `session_id` (resume) if it had one, fresh otherwise — then
     `tmux.respawn_pane` into it.
  7. **Publish last**: a single `windows.update_mapping` call appends or
     revives the slot. Because publication is the last step, any
     exception up to and including the publish itself just kills the
     pane it created and scrubs its files (`_rollback`) — the mapping is
     never left pointing at a broken pane, and no caught failure needs a
     rollback of the mapping itself.
  Steps 3–7 are the only part still under the cleanup lock (see "Lock
  discipline" above).

Closing the secondary needs no command: quit the agent, the pane dies, the
tick's dead-secondary transaction marks the slot dead (resume identity
retained for next time). `agent-kill` still kills the whole window. `Ctrl-Space z`
zooms/unzooms whichever pane has focus — no `agent-other`-specific code for
full-pane view.

### State classification & overview

Letter set + derivation rule above (per slot: `derive_letter`; per window:
`combined_letter`). The overview groups windows by repo (prefix before `:`
in window name), shows a fold marker per repo, and renders a dual-agent
window's slots as separate colored letters joined by a dim `|`
(`overview.parse_state_code` → `SlotState` list), each suffixing `B`/`Z`
with its live item count (`background·N` / `sleeping·N` — Codex slots never
show these, see "Data flow" above). Repo headers count **live slots**, not
windows, so a repo with one dual-agent window reads `(2 agents)`. Folds
persist at `<state_dir>/overview-folds.json`. Active-row coloring inverts
the fg/bg using `selected_fg` (chosen from perceived luminance for AAA
contrast against light state colors), keyed off the window's
combined-priority letter. The TUI cursor auto-tracks the active tmux window
unless the user has moved it.

Self-healing: each pending marker carries an expiry, so the counts can't drift
the way the old `loop·N` cron-counter did. Every kind has a *precise* removal
path, with the per-kind TTL as a backstop (session-ends-before-completion /
payload-format drift):
- **wakeups** self-expire at the exact `scheduledFor` time.
- **one-shot crons** compute their real next-fire via `croniter` (from
  `tool_input.cron`, the machine-readable expression — *not* `humanSchedule`,
  which can be prose like "Every 2 minutes").
- **recurring crons** use the documented 7-day backstop, removed early by
  `CronDelete`.
- **background subagents and background `Bash`** have no dedicated completion
  hook, so they are reaped two complementary ways. (1) Fast path —
  `clear-completed` on `UserPromptSubmit`: completion usually surfaces as a
  `<task-notification>` whose `<task-id>` equals the launch id (`agentId` /
  `backgroundTaskId`), and the hook reaps either marker type from it. (2)
  Authoritative — `reconcile` on `Stop`/`StopFailure`: the stop payload carries
  `background_tasks`, the session's live (status `running`|`pending`,
  backgrounded) task registry; `reconcile` reaps any marker whose id is absent
  from that set. The fast path alone misses completions delivered *mid-turn*: a
  background task that finishes while the agent is still working has its
  notification injected as an **attachment**, which fires no `UserPromptSubmit`,
  so its marker would otherwise linger until the TTL (showing a stale
  `background·N` once the pane goes idle, since `B` outranks `I`). `reconcile`
  closes that gap at the moment the pane goes idle — exactly when the stale
  count would become visible. (A `SubagentStop` hook is unnecessary: its payload
  carries the same `background_tasks`, so the `Stop` reconcile already covers
  subagent markers.)

Honest limitations (Claude): a background `Bash` whose session ends before
it completes falls back to the TTL; crons restored on `--resume` don't
re-fire `CronCreate`, so they won't re-register.

Subagent isolation (Claude): a subagent inherits its parent's `TMUX_PANE`,
so its own tool-uses fire the *parent* pane's hooks. Left unfiltered this
would flip the parent `B`↔`R` while a subagent works and register a
subagent-backgrounded Bash under the parent's pending dir (where it would
never see a completion notification). `write-state.sh::is_subagent` filters
these out: a subagent's PostToolUse payload carries `agent_id`/`agent_type`
(a main-agent payload never does), so the `running`, `add-subagent`, and
`add-bgshell` hooks skip when those fields are present — the pane tracks the
main agent only. The check gates on *presence*, so a Claude Code build that
drops the field degrades to the old unfiltered behaviour rather than
breaking. The main agent launching a background subagent still registers
`B` (that PostToolUse is the *parent's*, with no `agent_id`).

### Codex hooks & session-id pinning

Codex's hooks are provisioned once at the **user level** — see
`codex_hooks.py` in the module map above — not per-worktree, because a
sandboxed Codex session must never be trusted to run code from inside its
own (potentially agent-writable) workspace, and because a known Codex bug
([codex#27133]) silently ignores project-layer hooks in linked git
worktrees. The script guards on three conditions before writing anything:
`TMUX_PANE` set, `$PWD/.local/.tmux-agents` existing (cwd = worktree root —
every default exec template `cd`s there first), and `TMUX_AGENTS_AGENT=1`
exported. The third is the load-bearing one: without it, a manual `codex`
run inside `agent-terminal`'s popup shell (which does propagate `TMUX_PANE`
and does `cd` into the worktree, by design, so it behaves like a normal
shell there) would otherwise corrupt the pane's phase and its session-id
pin. `agent-terminal` deliberately does **not** export the marker, so those
shells stay inert to the hook. The same latent exposure exists for *Claude*
run manually there (its hooks key off `TMUX_PANE` alone, no marker check)
— recorded in `BACKLOG.md` rather than fixed here, since retrofitting the
marker into every already-provisioned `write-state.sh` would go dark for
live panes spawned before the change.

`session-<pane>.id` doubles as **pin**: `init` (`SessionStart`) overwrites
it on every `source` except `startup` against an existing, differing pin
(a nested Codex CLI launched by the root's own shell tool inherits
`TMUX_PANE`/cwd/marker and would otherwise hijack the pin on its own
`startup`; `/new`/`clear` still re-pin legitimately). Every other action
compares the payload's `session_id` against the pin first: mismatch → the
event writes nothing. **Nested Codex CLI launches inside an agent pane are
documented as unsupported** — a nested `codex resume` is indistinguishable
from a legitimate one, and this is the one gap the pin rule doesn't close.

[codex#27133]: https://github.com/openai/codex/issues/27133

**Honest limitations (dual-agent, not yet closed by more code):**
- **Denied-permission staleness.** Codex has no `PermissionDenied` event, so
  a slot the user just declined a permission prompt in stays `W` until its
  next mapped event fires — worst case, `Stop`. Not a bug; there's no event
  to hook.
- **Subagent flicker / pinning, to be confirmed.** The session-id pin
  defends against a Codex subagent's turn events overwriting the root
  pin, but its guarantee depends on real Codex subagent-payload behavior
  (does `SessionStart` fire only for the root session? do child turns carry
  a distinct `session_id`?) that the design's release gate calls out as
  needing E2E observation before it can be stated unconditionally. **This
  is marked to-be-confirmed pending that verification pass** — until then,
  treat "a subagent's activity might flicker the parent pane's letter" as a
  possible, not-yet-ruled-out limitation for Codex slots (mirroring the
  pre-filter behavior Claude once had, before `is_subagent` above).
- **Mid-`agent-other`-crash visible unmapped pane.** `agent-other` publishes
  the slot as its *last* step specifically so every *caught* failure needs
  no mapping rollback (see "Start/switch — `agent-other`" above). The one
  gap that's left: the process dying **outside** any try/except — between
  creating the pane and publishing it — leaves a real, visible, unmapped
  pane in the window. This is deliberately tolerated, not reconciled: it's
  fully visible (nothing silent), closing it is one keystroke, and the next
  `agent-other` invocation finds the slot absent/dead and starts normally.
  Its per-pane files aren't permanently leaked either — they sit
  unreferenced until that pane id gets recycled, at which point the next
  spawn's mandatory scrub-before-launch (the aliasing guard in "Lock
  discipline" above) cleans them up.
- **Nested Codex CLI unsupported**, per the pinning section above.

### SSH agent forwarding

Container projects forward the host's `$SSH_AUTH_SOCK` into every agent
pane by default. Architecture:

```
host:
   ssh-agent  ←─ host UDS (SSH_AUTH_SOCK)
        ▲
        │ open per-op
   pump (python -m tmux_agents._ssh_pump_script)
        │ stdio framed
        │ docker exec -i -u {user} python3 /tmp/tmux-agents-relay/_ssh_relay_script.py
        ▼
container:
   relay (bind-or-exit at /tmp/tmux-agents-ssh.sock, mode 0600,
          owned by {user})
        ▲
        │ accept(1) per op
   client (git, ssh inside container; SSH_AUTH_SOCK env points at the UDS)
```

Per-container; multiple agents in the same container share one relay (the
relay does a connect-existing-or-bind dedup at start-up). The pump spawns
detached (`start_new_session=True`) and reparents to launchd. The pump
self-supervises: when its `docker exec` stdio EOFs (container stop/
restart) or its splice errors on framing desync, the supervise loop
re-spawns the relay with exponential backoff (1s → 30s cap). It exits
cleanly only when (a) the container is no longer running per
`docker inspect`, (b) the relay reports `EXIT_DUPLICATE` (75) — meaning
another pump owns the in-container UDS — or (c) the host's
`SSH_AUTH_SOCK` is unset. `agent-new` complements this with a
pre-flight health check: probes the in-container UDS via
`ssh-add -l` with a short timeout, kills any stale pump processes for
the container if the probe fails, and respawns. So a broken-but-listening
pump can't shield itself from replacement, and re-running `agent-new`
on a healthy container is idempotent (no zombie pile-up).

Opt out: `forward_ssh_agent = false` per project. Default `exec_cmd`
templates set `SSH_AUTH_SOCK=/tmp/tmux-agents-ssh.sock` only when
forwarding is on.

### GitHub CLI auth sharing

`gh_auth.maybe_sync_gh_auth(container, user)` runs as a stage right after
the SSH pump wherever a container comes up (`agent-new`, `agent-restore`,
`agent-rebuild`). One-shot, no daemon: the host's `gh auth token` (keyring-
backed — a rebuilt container can't carry its own `hosts.yml` login, and the
host token isn't in a mountable file) is piped via stdin into
`docker exec -i -u {user} … gh auth login --with-token --hostname github.com`.
The token never appears on argv, in tmux command strings, or on host disk;
in the container it lands where a manual `gh auth login` would put it. The
sync always overwrites (a probe can't detect a revoked token; re-login is
~200ms) and every subprocess call is time-boxed so a hung docker can't
stall a spawn. All failure modes (`gh` missing on host, host not logged in,
`gh` missing in container, login rejected) map to a `SyncResult` rendered
as a non-fatal stage warning — in `agent-new` that trips the hold-on-warning
pane so it's visible; the agent always starts regardless.

Opt out: `share_gh_auth = false` per project.

### Theming

Dark + light palettes in `theme.py`. Optional override file
`<config_dir>/theme.toml` with `mode = "dark"|"light"` and a `[colors]`
table of per-state hex overrides. Each `Palette` carries fg, ANSI fg, ANSI
bg, contrast `selected_fg` (for active-row inversion), and ANSI selected
fg. Curses uses the closest xterm-256 cube index (`_hex_to_xterm256`).

### Provisioning

`provisioning.provision_settings` merges three top-level keys
(`_tmux_agents_version`, `tui`, `hooks`) into
`<worktree>/.claude/settings.local.json`, leaving everything else
untouched. The version is the installed package's
`importlib.metadata.version("tmux-agents")` — bumping it forces
re-provision on the next `agent-new` so updated hooks supersede stale
ones in existing worktrees. User-authored hooks on the same events are
preserved when the file has no prior tmux-agents marker; once the marker
is set, our own groups are replaced wholesale on upgrade.

### Persistence

After an `agents` server restart (laptop reboot, manual kill), the
surviving `windows/<window_id>.json` mappings *are* the snapshot — which
is why the tick's GC tombstones before deleting (see "Mapping GC is
two-phase" above). The launcher detects the orphaned snapshot and
prompts the user (`Restore N previous agents? [Y/n]`, 5-second
default-Y timer). On consent it moves the snapshot to
`windows.previous/`, starts tmux detached, spawns
`agent-restore --background`, and `execvp`s into `tmux attach`.

Before touching any pane, `harvest_session_ids` reads every slot's
`session-<pane>.id` off disk for **every** entry in the snapshot and merges
the ids into the plan — a barrier, not a per-slot step, because on a fresh
server a freshly-assigned pane id can equal a *different* slot's old pane
id, and that slot's pre-launch scrub would destroy the id file before the
other slot was ever processed. The worker pre-creates all windows up front
(each with a `phase_hint="starting"`, yielding the `S` state — lowest
priority in `derive_letter` chain; a dual-agent entry gets a dual split),
provisions the per-worktree Claude hook script and (idempotently) the
user-level Codex hooks, and attaches the bottom overview pane in split
layout. It groups entries by project, fires devcontainer `up_cmd`s in
parallel (one per project group, max 4 concurrent), and `respawn-pane`s
each placeholder into the real agent invocation — each slot's own kind, via
`exec_cmd.build` — as its container becomes ready. `{resume_args}` is
injected kind-aware (` --resume <id>` Claude, ` resume <id>` Codex); the id
was captured by that agent's `SessionStart` hook (`write-state.sh init` /
`codex-hook.sh init` → `session-<pane>.id`) and merged into the window
mapping by the state tick or the harvest barrier above.

Failures per entry are isolated: logged to `tmux-agents.log` (see
Logging below), and the failed pane is replaced with a heredoc that
prints the reason and recovery instructions, kept alive with a sleep
loop. The per-pane state is set to `phase=errored` so the overview
shows `X` for that window.

Both restore respawns (placeholder pre-create + activation) go through
`startup._respawn_with_retry`, which retries transient `fork failed`
errors (macOS can briefly refuse a pane spawn during the burst of
`devcontainer up` + ssh pump + 14 panes all forking at once) up to
`_FORK_RETRY_ATTEMPTS` times with a short backoff before falling back to
the per-entry failure handling above. The retry keys off stderr now
surfaced by `tmux.TmuxError` (a `CalledProcessError` subclass whose
`str()` appends tmux's stderr — the base class reports only the exit
code); non-fork failures re-raise immediately.

A manual `agent-restore` rerun against a still-live session now
classifies each snapshot entry as `skip` / `revive` / `fresh` /
`reactivate` via `classify_entry(entry, live_panes)`. `revive` is a
window alive but its agent pane is gone (only the overview pane
survives). `reactivate` handles a **failed restore retry**: the previous
run left the placeholder pane alive but at `phase=errored` (e.g. Docker
was down, so `up_cmd` failed and the pane was replaced with the error
heredoc). Because the launcher deletes `windows.previous/` at the end of
every run — success or failure — a retry (`agent-restore` /
`Ctrl-Space r`) reads the live `windows/` mappings, and `classify_entry`
reads the pane's per-pane state file: an alive-but-`errored` pane is
`reactivate` rather than `skip`. `_pre_create_reactivate` reuses that
window+pane in place — respawns it back into the `tail -F` placeholder,
resets its state to `starting`, and returns a `Placeholder` pointing at
the same pane — so `execute_plan` re-runs the container bring-up and
respawns the default agent into it. No new window is created, so retries
never accumulate duplicates, and the retry is idempotent while the
underlying cause (Docker down) persists.

The window-level `EntryKind` above (`skip`/`revive`/`fresh`/`reactivate`)
only ever describes **slot 0**. An **existing secondary** slot gets its own
independent `SlotAction` (`classify_secondary`): `none` (pane alive,
healthy), `reactivate` (pane alive but `phase=errored` — a failed earlier
`agent-other` attempt left the placeholder), or `revive` (`pane_id: null`,
or its recorded pane is gone) — with the slot's retained `session_id` as
resume args. A mapping with no secondary slot is a normal single-agent
window and is never "repaired" into a dual one. An entry drops out of the
plan only when the window action is `skip` **and** the secondary action is
`none` — so a healthy default with a dead secondary still gets planned
(secondary-only repair), and a dead default with a healthy secondary
revives only the default, never touching the already-healthy sibling pane.

`pre_create_windows` splits a fresh agent pane above the
surviving overview at 75/25 (`tmux.split_window(target=surviving_pane_id,
before=True, percent=75)`), rewrites the window mapping with the new
pane id, and cleans the stale per-pane files (`state-<old>.json`,
`pending-<old>/`, `session-<old>.id`) under the worktree's
`.local/.tmux-agents/` directory. When more than one pane survives — a
window wedged into a duplicate-overview state — `_pre_create_revive`
keeps one `@role=overview` pane (`tmux.overview_pane_ids`) as the split
target and reaps the extras; it bails only when nothing survives or no
survivor is an overview pane.

That duplicate-overview state is prevented at the source:
`overview.attach_overview_pane` is idempotent (no-op when the window
already has an overview pane), so a layout toggle or restore re-attaching
to an already-agent-dead window can't add a second overview.

This manual rerun is wired to a recovery shortcut: `Ctrl-Space r` (and
`r` in the focused overview pane) run `agent-restore --background` via
`run-shell -b`, which revives every dead-pane window and reactivates every
errored placeholder in one pass, and skips healthy live ones. `--background` forks + `setsid`s and then redirects fd 0/1/2 to
`/dev/null` (`startup._detach_stdio`); without that detach the backgrounded
worker keeps `run-shell`'s capture pipe as stdout and tmux paints its output
(e.g. `devcontainer up` JSON) over the active pane. The
overview surfaces the affordance — when any window is errored, the curses
TUI footer is replaced by a right-aligned recovery alert
(`overview._restore_alert`), `⚠ N agent(s) down — press Ctrl-Space r to
restore`, in the errored color. Footers spell the full prefix chord via
`tmux.prefix_label()` — a process-cached read of the live server's `prefix`
option (falling back to `"Ctrl-Space"` when the server is unreachable, e.g.
tests) — so a `local.conf` prefix override is reflected in the hint text,
not just the shipped default; the bare keys still work while the overview
pane itself is focused. All command keys are now lowercase (`a`/`k`/`b`/`r`/
`e`/`v`/`t`; layout stays uppercase `L`); the pre-lowercase uppercase keys
remain bound as silent aliases. `agent-rename` is bound to `Ctrl-Space e` /
`e` (and the alias `Ctrl-Space E` / `E`).

### Logging

All diagnostics route through Python's `logging` module via
`tmux_agents.logging_setup.setup_logging()`. Output goes to a single
rotated file at `paths.state_dir() / "tmux-agents.log"` (~20 MB cap:
5 MB × 3 backups). Default level is INFO; set
`TMUX_AGENTS_LOG_LEVEL=DEBUG` for verbose traces. CLI errors are also
printed to stderr (for direct-terminal use); popup and background
invocations rely on the log file. The SSH pump runs as `python -m
tmux_agents._ssh_pump_script`; it does its own minimal logging setup
(format/rotation duplicated from `logging_setup`) keyed off the
`TMUX_AGENTS_LOG_FILE` env var that the host pump-spawn function sets.

### Copy / paste

Mouse-drag copies via `config/clipboard-copy` (vi `MouseDragEnd1Pane →
copy-pipe-and-cancel`), installed to `~/.config/tmux-agents/clipboard-copy`
alongside `agents.conf`. It's a small shell dispatcher, not a single tool:
tries `pbcopy` (macOS), then `clip.exe` (WSL), then `wl-copy` (Wayland),
then `xclip`/`xsel` (X11), falling back to discarding input if none are
present. A `pane-set-clipboard` hook bridges OSC 52 through the same script
for terminals like Apple Terminal that drop OSC 52 — required when Claude
is inside a devcontainer and has no host clipboard tool of its own. Cross-pane
selection requires holding Option (iTerm2/Ghostty/Alacritty) or Fn
(Terminal.app) to bypass tmux's mouse capture.

## On-disk layout

```
~/.config/tmux-agents/                ← TMUX_AGENTS_CONFIG_DIR
  agents.conf                         tmux config (loaded via -f)
  projects.toml                       user-edited project definitions
  theme.toml(.example)                optional palette overrides
  codex-hook.sh                       package-owned Codex hook script (host),
                                      mode 0755; provisioned by codex_hooks.py
  codex-hooks.lock                    fcntl lock for codex-hook.sh +
                                      hooks.json provisioning
  windows/<window_id>.json            window→worktree mapping (host-side),
                                      schema 2 (agents: [AgentSlot, …])
  windows/<window_id>.json.lock       stable sibling lock for the mapping
                                      file above (never the JSON itself)
  windows.previous/<window_id>.json   transient; populated by the
                                      launcher on fresh-server restore,
                                      consumed by agent-restore, removed
                                      at end of restore

~/.codex/hooks.json                   ← user-level Codex config (host);
                                      merged by codex_hooks.py, owned
                                      entries only (foreign entries survive)
<container home>/.codex/hooks.json    same, inside every container
<container home>/.codex/tmux-agents/codex-hook.sh   container twin of the
                                      host script above

/tmp/tmux-agents/                     ← TMUX_AGENTS_STATE_DIR
  layout                              "split" | "compact"
  overview-folds.json                 repo header fold state
  tick.cache                          last tick's per-window fingerprint
  tmux-agents.log                     unified rotating log (all components)
  (the derived letter is the per-window @state_code tmux option, not a file)

<worktree>/.local/.tmux-agents/       ← per-worktree, written by agent hooks
  write-state.sh                      Claude hook helper (provisioned, mode 0755)
  .cleanup.lock                       per-worktree fcntl lock: destructive
                                      per-pane cleanup + slot-liveness
                                      publication (Claude and Codex hooks
                                      write their own files lock-free)
  state-<pane>.json                   {phase, updated_at} — written by
                                      write-state.sh (Claude) or codex-hook.sh
  pending-<pane>/<kind>__<id>         self-expiring B/Z markers (registry;
                                      Claude slots only — Codex never writes
                                      here)
  session-<pane>.id                   UUID/session id written by SessionStart
                                      (init action, either hook script) —
                                      doubles as Codex's subagent-defense pin
<worktree>/.claude/settings.local.json   tui:fullscreen + lifecycle hooks
                                      (Claude only; Codex has no per-worktree
                                      file — see ~/.codex/hooks.json above)
```

## Testing

`uv run pytest -q` (a few seconds). Tests freely monkey-patch the `tmux`
module rather than driving a real server; `tests/conftest.py` provides
`tmp_state_dir` and `fixtures_dir`. The SSH relay sibling-import test
(`test_ssh_relay.py`) checks the delivered-file import path; the hook-snippets
test compiles each shell hook body to catch quoting drift.
Codex-support tests: `test_agent_kind.py` (kind constants, resume-arg
spelling), `test_codex_hooks.py` (host/container provisioning, digest +
canonical-structure ensure, merge preserving foreign entries), `test_codex_hook.py`
(the shell script itself compiles and its guard/pin behavior), `test_locks.py`
(sibling-lock serialization, lock-order/no-deadlock), `test_other.py`
(`agent-other`'s branches: dead-default message, start, revive-dead,
focus-jump, no-mapping, preflight-skipped-on-custom-command, rollback on
respawn/publication failure), plus schema-2/dead-slot/`last_pane_id` cases
folded into `test_windows.py` and multi-slot tick/rendering cases folded
into `test_state_tick.py` / `test_overview.py`.
