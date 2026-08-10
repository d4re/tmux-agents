# Codex agent support — design

Date: 2026-07-17
Status: approved design, pre-implementation.
Revision 10 (final) — the ninth external-review pass returned **no
high-severity findings** and declared the design ready for
implementation planning; this revision folds in its three
clarifications (caught publication failure cleans up like any other
caught failure; the cleanup lock guards destructive cleanup and
slot-liveness publication, not hook writes; crash-left files are
"not permanently leaked — scrubbed on id reuse").

Prior revision history:
Revision 9 — eighth external-review pass (gpt-5.6-sol via Codex CLI).
Rev 9 deltas: a **global lock order** (per-worktree cleanup lock →
per-window mapping lock; the mapping lock is taken only inside
self-contained `update_mapping` calls, never held while acquiring
anything) replaces rev 8's inverted orders, which could deadlock
`agent-other` against the sweep; the start branch serializes on the
cleanup lock instead of the mapping lock; `agent-other` publishes the
slot **last** (split → scrub → respawn → publish), so caught failures
kill the placeholder without any mapping rollback and a mid-branch
crash can only leave the already-accepted *unmapped* visible pane —
never a mapped inert placeholder misread as healthy.

Prior revision history:
Revision 8 — seventh external-review pass (gpt-5.6-sol via Codex CLI).
Rev 8 deltas: restore gains a **global session-id harvest barrier**
(all slots' disk ids merged before any pane is created or scrubbed —
per-slot merging could destroy a not-yet-processed slot's latest id via
a recycled pane id); a **per-worktree cleanup lock** synchronizes the
`last_pane_id` sweep with every spawn path's assigned-id scrub (closes
the TOCTOU where a sweep's stale collision check deletes a freshly
spawned agent's files); `agent-other` spawns **placeholder-first**
(split inert → scrub → publish → respawn into the agent), making
scrub-before-launch enforceable; the spawn scrub is specified as the
full set (state + session + pending) against the resolved worktree
path, correcting the "agent-new already does this" over-claim.

Prior revision history:
Revision 7 — sixth external-review pass (gpt-5.6-sol via Codex CLI).
Rev 7 deltas: pane-id aliasing guard (the sweep skips a `last_pane_id`
matching a live mapped pane of the worktree; every spawn path clears
stale per-pane files for its assigned id — `agent-new` already did);
`agent-other`'s rollback also deletes the killed pane's per-pane files
and the crash-orphan limitation covers its files; `SessionStart`
`source=startup` no longer overwrites an existing differing pin (nested
CLI defense; nested launches documented unsupported); the accepted
limitations are listed in the planned ARCHITECTURE.md update.

Prior revision history:
Revision 6 — fifth external-review pass (gpt-5.6-sol via Codex CLI).
Rev 6 deltas: the hook guard requires a `TMUX_AGENTS_AGENT=1`
agent-launch marker exported only by exec templates (a manual `codex`
inside `agent-terminal` can no longer corrupt a pane's phase or pin;
Claude's symmetric latent case → BACKLOG); `last_pane_id` became an
orthogonal cleanup-pending field — revival keeps it, the sweep covers
live and dead slots and clears it only after verified deletion via
compare-and-set; `agent-other` rolls back its created pane when mapping
publication fails (process-death-mid-operation leaves a visible pane —
documented honest limitation, no reconciliation protocol); atomic
writes use unique temp names (container provisioning has no lock to
hide behind).

Prior revision history:
Revision 5 — fourth external-review pass (gpt-5.6-sol via Codex CLI).
Rev 5 deltas: `resume` writes `idle` (only `compact`/unknown sources are
pin-only); the disabled-capture consequence is stated as possible loss
of automatic resume (with manual `codex resume` as recovery), not a
bounded gap; hooks.json ownership matches the structural form (quoted
path + one action word — migration-safe without a ledger) and
ensure-provisioned validates the canonical owned structure incl.
multiplicity; `agent-other`'s start branch runs under the window mapping
lock (start-vs-start race) and gains a dead-default branch + custom-exec
preflight skip; the tick sweeps dead slots' leftover files via
`last_pane_id`; the restore window action explicitly owns slot 0.

Prior revision history:
Revision 4 — third external-review pass (gpt-5.6-sol via Codex CLI).
Rev 3 established the structure: a **package-owned hook script outside
every workspace** (no trust bypass), **session-id pinning** for subagent
defense, a **sibling lock file** for mapping writes, no `@role=agent`
(mapping slots are the source of truth; only the already-universal
`@role=overview` is used), and idempotent **ensure-provisioned** checks.
Rev 4 hardens the details: pin capture gated on root-ownership
observation and `init` phase writes keyed on `SessionStart.source`;
ensure-provisioned verifies script digest + expected hook entries (not
presence); hooks.json ownership by exact command match, removed per
entry; dead-slot marking is a compare-and-set with `last_pane_id` for
precise cleanup; restore plans one action per slot (window + slots, not
a single verdict); `tmux.split_window` gains direction/full-size
parameters; the custom `codex_exec_cmd` cwd contract, live-slot header
counts, and a narrow "dead slot" definition are documented.

## Goal

Support OpenAI Codex CLI as a second agent kind alongside Claude Code. A
project has a **default agent** (claude or codex) that `agents` / `agent-new`
start and track exactly as today. A keybinding starts the **other** agent in
the same window/worktree or switches focus between the two once both run.
Window state reflects both agents (`R|I`-style codes, combined-priority
color); the status-bar summary counts each live agent.

Out of scope (deliberately): a third agent kind (the design leaves the door
open but builds nothing for it), Codex background/scheduled tracking (B/Z
letters), per-agent context/token telemetry (backlogged).

## Research grounding

Surveyed trackers: [tmux-agent-status], [tmux-agent-indicator], [ccmanager],
[Claude Squad], [marmonitor], [codex-hud], [codex-cli-farm].

- Every tool with hook access uses hooks; all non-hook approaches
  (PTY output-pattern matching, session-file polling, `capture-pane`
  scraping) self-describe as heuristic/best-effort. Hook-first is confirmed.
- Codex's hooks engine is stable (v0.124.0+) and Claude-Code-shaped:
  hooks configurable at user and project layers, payload on stdin with
  `session_id` / `hook_event_name` / `tool_name` / `tool_input` (every
  event carries `session_id`), events `SessionStart`, `UserPromptSubmit`,
  `PreToolUse`, `PostToolUse`, `PermissionRequest`, `Stop`,
  `SubagentStart/Stop`, `PreCompact/PostCompact`. Hooks are **enabled by
  default**; `codex_hooks` is a deprecated alias of `[features] hooks` —
  no feature flag is provisioned. `codex resume <id>` exists.
- **Trust model**: every non-managed command hook — user layer included —
  is trust-gated per exact definition hash and approved via `/hooks`; new
  or changed definitions are skipped (not queued) until approved. The
  expected UX for this design is a **one-time `/hooks` approval** per
  provisioned-hook version; E2E verification only confirms that UX.
- An open Codex bug reports project-layer `.codex/hooks.json` silently
  ignored in linked git worktrees ([codex#27133]) — one reason this design
  avoids the project layer entirely.
- Codex subagent turn hooks may fire without a stable root/subagent
  discriminator ([codex#20675]) — addressed by session-id pinning
  (Section 2) with an observation gate.
- marmonitor's four phases (permission/thinking/tool/done) map onto the
  existing `W`/`R`/`I` letters — our vocabulary is already a superset of the
  most complete trackers.
- No surveyed tool handles two agents per worktree/window; samleeney's
  per-pane files → priority-combined session rollup confirms the
  decomposition (per-pane state, combined display).

[tmux-agent-status]: https://github.com/samleeney/tmux-agent-status
[tmux-agent-indicator]: https://github.com/accessd/tmux-agent-indicator
[ccmanager]: https://github.com/kbwo/ccmanager
[Claude Squad]: https://github.com/smtg-ai/claude-squad
[marmonitor]: https://github.com/mjjo16/marmonitor
[codex-hud]: https://github.com/fwyc0573/codex-hud
[codex-cli-farm]: https://github.com/waskosky/codex-cli-farm
[codex#27133]: https://github.com/openai/codex/issues/27133
[codex#20675]: https://github.com/openai/codex/issues/20675

## Decisions (user-confirmed)

1. **Default scope**: top-level `default_agent` in projects.toml with
   per-project `agent` override.
2. **Dual layout**: side-by-side split of the agent area + tmux native zoom
   for full-pane; pane navigation / the toggle key switches focus.
3. **Summary counts**: each **live** agent slot counts individually (a
   `R|I` window adds 1 to R and 1 to I; dead slots are not counted and
   render nothing).
4. **Restore**: both agents come back, each resuming its own conversation.

## Section 1 — Configuration & agent model

`projects.toml`:

- Top-level `default_agent = "claude" | "codex"` (absent → `"claude"`;
  zero-migration).
- Per-project `agent = "claude" | "codex"` overrides the global default.
- Invalid values fail config loading (exit 2).

**Agent kinds**: a small module (`agent_kind.py` or constants in
`config.py`) owning the two kind names and per-kind knowledge: executable
name, default exec templates, resume-arg spelling. Nothing else hardcodes
`claude`.

**Exec commands**: existing `exec_cmd` keeps meaning "launch Claude"
(full back-compat). New optional `codex_exec_cmd` is its Codex twin.
Defaults mirror the Claude ones exactly:

- host-only: `cd {workdir} && exec codex{resume_args}`
- container: the same `docker exec … -e TMUX_PANE …` shape with
  `export SSH_AUTH_SOCK=…` when forwarding, body ending
  `exec codex{resume_args}`.

`{resume_args}` is reused unchanged: Claude → ` --resume <id>` (flag),
Codex → ` resume <id>` (subcommand; works because the placeholder follows
the executable). `Project.substitute()` gains an `agent` parameter to pick
the template; `exec_cmd.build(...)` gains the slot kind.

Every default exec template `cd {workdir}` first, so the Codex
**session cwd is always the worktree root** for panes spawned by
tmux-agents. Section 2's hook script depends on this invariant to locate
the pane's state dir. For a **custom** `codex_exec_cmd` this is a
documented two-part contract: the command must establish `{workdir}` as
the agent's cwd (exactly as a custom `exec_cmd` already must for
Claude's project-layer hooks to load) and must export
`TMUX_AGENTS_AGENT=1` (the agent-launch marker, Section 2) — README
states both, and a violation degrades to an untracked-but-functional
pane, not a crash.

**Prerequisite (documented, not enforced)**: codex must be installed and
authenticated where the agent runs — on the host for host-only projects,
inside the container (including `~/.codex` auth) for container projects.
Missing binary at spawn time → standard error pane (`X`) with the failing
command; `agent-other` additionally pre-flights the executable
(Section 4).

## Section 2 — Codex hooks & the state pipeline

### A package-owned hook script, referenced from user-level hooks

New package data `hooks/codex-hook.sh`: a small, self-contained POSIX
script handling only the Codex needs — phase writes (`running` /
`waiting` / `idle`), `init`, session-id pinning (below), and the bell on
`waiting`. No registry markers, no payload sed beyond `session_id` and
`SessionStart.source`.
**`write-state.sh` stays Claude-only and unchanged.**

It is provisioned to **user-owned paths outside every workspace**:

- host: `~/.config/tmux-agents/codex-hook.sh`
- container: `<home>/.codex/tmux-agents/codex-hook.sh`, where `<home>` is
  resolved *inside* the container (`docker exec -u {user} sh -c
  'printf %s "$HOME"'`), written via `docker exec -u {user}` so ownership
  is correct — the same delivery pattern as the SSH relay files.

`~/.codex/hooks.json` (host and container home) gets hook groups whose
commands reference that absolute path: `sh <abs>/codex-hook.sh <action>`.
Because the script lives outside the workspace, a sandboxed
(workspace-write) agent cannot modify it, and the trusted hook definition
never executes workspace-controlled code — the one-time `/hooks` approval
covers code only tmux-agents (or the user) can change.

Script guards: exit 0 unless **all three** hold — `TMUX_PANE` is set,
`$PWD/.local/.tmux-agents` exists (cwd = worktree root, the Section 1
invariant), and `TMUX_AGENTS_AGENT=1` is in the environment. The third
is the **agent-launch marker**: every default exec template exports it
(host: in the `cd {workdir} && …` line; container: an extra `-e` on the
`docker exec`), and nothing else does. Without it, `TMUX_PANE` + cwd
are spoofable *by accident*: `agent-terminal` (`Ctrl-Space T`)
deliberately opens a shell in the worktree with `TMUX_PANE` propagated,
so a manual `codex` run there — including the manual `codex resume`
recovery this spec itself suggests — would otherwise write phases under
the focused agent pane and, worse, `init`-overwrite its session pin.
With the marker, hooks from such shells no-op (tested). Custom
`codex_exec_cmd`s must carry the marker themselves — folded into the
existing cwd-contract documentation (Section 1); a violation again
degrades to an untracked-but-functional pane. The same latent exposure
exists today for *Claude* run manually inside `agent-terminal` (its
worktree hooks key off `TMUX_PANE` alone); extending the marker to
`write-state.sh` is deliberately **not** done here — existing live
panes were spawned without the variable and would go dark until
respawned — and is recorded in BACKLOG.md instead.

### Session-id pinning (subagent defense + resume capture in one rule)

`session-<pane>.id` is the **pin**:

- `init` (`SessionStart`) **overwrites** the pin with the payload's
  `session_id` — with one exception: `source=startup` **does not
  overwrite an existing, differing pin**. In this pane topology the
  agent is `exec`'d, so its exit kills the pane; a legitimate second
  process-start (`startup`) in a pinned pane cannot happen — but a
  *nested* Codex CLI launched by the root's own shell tool inherits
  `TMUX_PANE`, cwd, and the launch marker, and its `startup`
  `SessionStart` would otherwise hijack the pin. (`new`/`clear` must
  still overwrite: `/new` inside the root is a legitimate re-pin.) A
  nested `codex resume` remains indistinguishable from a legitimate
  one; nested CLI launches inside an agent pane are **documented as
  unsupported**. Its *phase* write depends on the payload's `source`:
  `startup` / `new` / `clear` / `resume` → `idle` (a fresh or resumed
  process sits at the prompt — without this, a restored/rebuilt pane
  whose `resume` waits for input would stay `S` forever); `compact` →
  pin only, **no phase write** — compaction fires `SessionStart`
  mid-turn, and writing `idle` there would falsely idle an in-progress
  turn. An unknown `source` value is treated as `compact` (pin-only, the
  safe direction); the actual source vocabulary is confirmed in E2E.
- Every other action first compares the payload's `session_id` to the
  pin: **mismatch → exit without writing anything** — a mismatching
  event may corrupt neither the phase nor the pin. **Absent pin →
  capture** is the trust-skip recovery path (a first session whose early
  events were skipped pending `/hooks` approval); it is enabled only if
  the release gate confirms it is safe (below), otherwise the action
  no-ops until the next `SessionStart` re-pins.

The one rule replaces revision 2's depth counter (no shared mutable
counter, no lock, no stuck-depth failure mode). Its guarantees are
conditional on the release gate — the spec deliberately makes no
unconditional "a child can never overwrite the root id" claim.

**Release gate (not a spike)**: observe real Codex payloads for subagent
turns and answer three questions before shipping:

1. Is `SessionStart` emitted **only** by the root session? If subagents
   emit it too, `init` needs a root discriminator (e.g. `source`, a
   parent/agent field, or thread-id comparison) before overwriting the
   pin.
2. Do child turn events carry a **distinct** `session_id`? If yes, the
   pin filters them completely. If they carry the root id, phase can
   flicker during subagent activity (child `Stop` → `idle` while the
   root works) — ship only with that documented as an honest limitation,
   mirroring the pre-filter Claude behavior the codebase once had.
3. Is the **first** event after trust approval guaranteed root-owned?
   Only then is absent-pin capture enabled. Otherwise it stays off, and
   the consequence is documented honestly: if that first session ends
   (pane closed, server dies) before any later in-pane `SessionStart`
   fires, its id is never captured and **automatic** resume of that one
   conversation is lost — not a "bounded gap", a loss. Manual recovery
   still exists (`codex resume` inside the pane lists recent sessions),
   and every session after the one-time trust approval pins normally.

> **Gate outcome (2026-08-10, decided by the maintainer during the
> Task-18 field test):** absent-pin capture is **enabled** ahead of the
> formal question-3 observation — live usage is the chosen instrument
> for answering it. The residual risk (a subagent-owned first event
> pinning a child id, dropping root events until the next
> `SessionStart`) is visible as a stuck letter and heals with `/new`;
> if it is observed in practice, the capture reverts to gated-off.
> Questions 1–2 remain under observation.

### Event mapping

Codex has no `Notification` / `SessionEnd` / `StopFailure` /
`PermissionDenied`:

| Codex event | codex-hook.sh action | Claude equivalent |
|---|---|---|
| `SessionStart` | `init` (pin session id) | same |
| `UserPromptSubmit` | `running` | same |
| `PermissionRequest` | `waiting` + `printf '\a'` | `Notification[permission_prompt]` |
| `PreToolUse` | `running` | — (not wired) |
| `PostToolUse` | `running` | same |
| `Stop` | `idle` | same |

`PreToolUse → running` is a **general activity signal**, not an
approval-granted transition — it fires *before* any permission request
for that tool, and `PermissionRequest` may set `W` again afterwards;
`PostToolUse` resets to `R` only after execution.

**Known limitation (documented)**: a *denied* permission request leaves
`W` standing until the next mapped event (worst case `Stop`) — Codex has
no `PermissionDenied` event.

Registry markers are Claude-tool-specific and not wired for Codex:
`pending-<pane>/` stays empty, so a Codex slot's letters are **R/W/I/X/S**
(no B/Z). `derive_letter` is unchanged (empty registry ⇒ zero counts). No
`SessionEnd` ⇒ no `cleanup`; stale per-pane files are handled by the
existing pane-death/restore cleanup paths.

### Provisioning `~/.codex/hooks.json` (host + container)

- **Ownership by exact structural match, per entry**: a hook *entry* is
  tmux-agents-owned iff its command matches the exact structural form
  `sh '<abs>/codex-hook.sh' <word>` — the shell-quoted absolute script
  path as the sole first argument plus exactly one action word. Matching
  the *form* rather than a fixed action list makes upgrades
  migration-safe (a renamed/removed action from an older version is
  still recognized and cleaned up — no ledger of historical commands
  needed), while still never matching user wrappers, loggers, or
  `codex-hook.sh.backup` (those don't have the quoted path as the
  entire first argument). The merge removes owned **entries**
  individually, removes a group only when that leaves it empty, and
  appends the current owned group; foreign entries sharing a group with
  ours survive. No extra keys in `hooks.json`, so no schema risk.
- **Concurrency**: writes are tmp + atomic rename, with **unique
  (`mktemp`-style) temporary names** — a fixed sibling `.tmp` path would
  let two concurrent writers corrupt each other mid-write even when
  their final content is identical. Host-side the merge additionally
  runs under the same sibling-lock helper built for mappings
  (Section 3); container-side there is no lock, and convergence relies
  on unique-temp atomic renames plus content that is deterministic per
  package version (tested with two concurrent provisioners).
- **Ensure-provisioned = canonical structure, not presence**: the
  idempotent check run by `agent-other` and the provision/restore/rebuild
  workers verifies (a) the installed script's **digest** matches the
  packaged script for this version, and (b) the **owned subset** of
  `hooks.json` — every owned entry extracted by the structural match —
  equals the canonical generated structure exactly: right events, right
  matchers, right commands, exact multiplicity (no duplicates, nothing
  owned under a wrong event). Any deviation → re-provision (strip all
  owned entries, append the canonical set). No version sidecar file —
  the digest subsumes it. A user- or Codex-mutated script, a truncated
  write, hand-edited or event-misfiled hooks all heal at the next use;
  there is no durable failure state to maintain.
- Mutating user-global Codex config is stated explicitly in the README,
  along with the one-time `/hooks` approval it entails (re-approval when
  the provisioned commands change between versions).

### Verification items (E2E, each with a stated fallback)

1. **Subagent payloads** — the release gate above.
2. **`TMUX_PANE` in the hook environment**, host and container (docs
   guarantee cwd, not env inheritance; child-process inheritance makes it
   expected). Verify both paths in E2E.
3. **`/hooks` one-time approval UX** — confirm wording/flow for the
   README; behavior itself is documented fact, not in question.

## Section 3 — Mapping schema, tick, rendering

**`WindowMapping`** gains `schema: 2` and an `agents` list, slot 0 = the
project's default agent. Pane ids are stored **stripped** (no `%`),
matching the existing convention:

```json
"schema": 2,
"agents": [
  {"kind": "claude", "pane_id": "12", "session_id": "uuid-…"},
  {"kind": "codex",  "pane_id": null, "last_pane_id": "15", "session_id": "0199-…"}
]
```

**Terminology**: "dead slot" means exactly one thing — a **secondary**
slot persisted with `pane_id: null`. `last_pane_id` is an **optional,
orthogonal cleanup-pending field** (which pane's files still need
deleting): a dead slot loses it once its sweep completes, and a revived
(live) slot can still carry it until the sweep finishes. A default
(slot 0) pane that dies is *not* a dead slot: it renders
`X`, is counted in the summary, and is fixed by restore/revive — today's
behavior, unchanged. Back-compat:
`from_dict` synthesizes a single claude slot from legacy
`pane_id`/`claude_session_id` when `agents` is absent (old snapshots
restore untouched); writes keep the legacy `pane_id` mirroring slot 0,
and `claude_session_id` mirrored only when slot 0 is a claude slot (a
downgraded reader must never `claude --resume` a Codex session id).
Documented limitation: new→old downgrade of a codex-default window makes
the old version start a fresh Claude session there. `phase_hint` stays
window-level (pre-worktree only). **A mapping with only slot 0 is the
normal single-agent window** — the absence of a secondary slot never
implies anything needs repair.

**Serialized mapping writes**: all mutations go through a new
`windows.update_mapping(window_id, fn)`. The lock is an `fcntl` lock on a
**stable sibling** `<window_id>.json.lock` — never on the JSON file
itself, whose inode is replaced by every atomic write. Holding the lock:
re-read, apply `fn`, tmp + rename. Mapping deletion takes the same lock.
Users: the tick's session-id/window-index merge, `agent-other`'s slot
append/revive, the tick's dead-slot marking, restore/rebuild pane-id
rewrites.

**State tick**: loops live slots; each runs the existing per-pane
machinery (`state-<pane>.json`, `pending-<pane>/`, `derive_letter`)
unchanged. Then:

- `@state_code` = joined per-slot codes (`"R|I"`, `"B2|R"`, single code
  for single-agent windows; dead slots render nothing). tick.cache
  fingerprint uses the full string.
- `@state_fg` = color of the highest-priority letter across live slots
  (priority `X > W > R > B > Z > I > S`).
- **Pane death is asymmetric**: the default slot's pane dying shows `X`
  (today's behavior; restore/revive fixes it). A *secondary* slot's pane
  dying is handled in **one guarded `update_mapping` transaction** —
  a compare-and-set: the callback re-reads the mapping under the lock
  and mutates the slot **only if it still holds the observed
  (kind, pane_id)**; if `agent-other` revived the slot to a new pane in
  between, the tick's stale death observation is a no-op. When the guard
  holds: read `session-<observed pane>.id` from disk, merge it into the
  slot's `session_id`, set `pane_id: null` and
  `last_pane_id: <observed pane>`, write. Only then delete that old
  pane's per-pane files. `last_pane_id` makes the cleanup precise and
  covers the crash window: **the tick sweeps every slot carrying
  `last_pane_id`** — live or dead — retrying the per-pane file deletion
  and clearing the field only **after verified deletion**, via a
  compare-and-set on the observed value (an overlapping older sweep can
  never clear a newer death's pointer). Revival deliberately does *not*
  clear it: a crash between death-marking and deletion followed by an
  immediate revive would otherwise orphan the old files with no pointer
  left. **Aliasing guard**: tmux pane ids are server-local and are
  reused after a server restart, so a stale `last_pane_id` can name a
  *newly live* pane's id. Two complementary rules make this safe:
  (a) the sweep **skips** (does not delete, does not clear) any
  `last_pane_id` that equals a live mapped pane id of the same
  worktree — deferring until the collision clears; and (b) **every**
  pane-spawn path deletes stale per-pane files — state, session id,
  *and* `pending-<pane>/` — for its assigned pane id against the
  **resolved worktree path**, immediately before launching the agent.
  (Today's `agent-new` scrub is *not* sufficient as-is: it clears only
  state/session files and only under `proj.repo` before the worktree is
  resolved — the worker must repeat the full scrub against `wt_path`.)
  **Sweep and scrub are synchronized** by a stable **per-worktree
  cleanup lock** (`<worktree>/.local/.tmux-agents/.cleanup.lock`,
  `fcntl`) guarding **destructive cleanup/scrubbing and slot-liveness
  publication** — deliberately *not* the lifecycle hooks' own
  state/session writes or the registry's marker GC, which stay
  lock-free (no retrofitting the host-side lock into container-side
  hooks): the sweep holds it across collision-check + delete +
  CAS-clear, the tick's death-marking holds it (it deletes files), and
  every spawn path holds it from assigned-id scrub through launch and
  publication — without it, a sweep that passed its collision check
  could delete files a concurrently spawned agent just wrote (the
  per-window mapping lock can't cover this: two windows can share a
  worktree). **Global lock order: cleanup lock first, mapping lock
  second, always.** The mapping lock is acquired *only* inside
  `windows.update_mapping` (and the read helper), each call
  self-contained — no code path holds the mapping lock while acquiring
  the cleanup lock, and nothing calls `update_mapping` re-entrantly, so
  the inversion deadlock (one worker cleanup→mapping, another
  mapping→cleanup) is impossible by construction; a deterministic
  lock-inversion test pauses the sweep and a spawn after their first
  acquisition to prove it. No worktree-wide orphan scan, no silent
  leak. The dead slot **retains its resume identity**:
  crash and quit are indistinguishable at the tmux level, so instead of
  guessing, `agent-other` revives it resuming the same conversation, and
  restore does the same. Want a fresh start? `/new` inside the revived
  agent.
- A live slot with no state file derives `S` until its `SessionStart`
  (or first captured event) fires.

**Overview + summary**: `_parse_state_code` is replaced by a parser
returning `list[SlotState]` (letter + overlay count per slot); `Row`
carries the list so per-slot colors/counts survive rendering. Rows render
each letter in its own state color separated by a dim `|`;
sorting/active-row color uses the combined-priority letter. The summary
counts each **live** slot (a dead default pane still counts as `X` — it
is not a dead slot, per the terminology above). The overview's repo
headers switch their count unit to **live slots** and their label to
match — a repo with one dual-agent window reads `2 agents`, keeping the
label truthful. Window tabs need no conf change (they consume only
`@state_fg`).

**Theme**: no new colors; separator uses default/dim fg.

## Section 4 — Start/switch UX, layout, restore/rebuild, errors, testing

**Pane identification — no new roles.** The mapping's slots (pane ids)
are the source of truth for which panes are agents. The only pane role
remains `@role=overview`, which `attach_overview_pane` has always set —
so every overview pane, legacy included, already carries it and **no
role backfill is needed**.

**New CLI entry point `agent-other`** (keybinding `Ctrl-Space O`, plus `O`
in the focused overview pane); one smart action on the active window:

- Default pane dead → no split target and nothing sane to toggle to:
  `display-message` pointing at the recovery path
  (`Ctrl-Space R` restore) and stop.
- Secondary absent or dead → run the ensure-provisioned check
  (Section 2), pre-flight the **other kind's** executable
  (`command -v <exe>`, via `docker exec` for container projects; friendly
  `display-message` error if missing — skipped when a custom
  `codex_exec_cmd`/`exec_cmd` overrides the default template, since the
  fixed executable name proves nothing about an arbitrary command),
  then start **placeholder-first**, the codebase's existing spawn idiom,
  with **publication last**: split the agent pane 50/50 left/right with
  an inert placeholder command, obtain the new pane id, scrub its
  per-pane files, `respawn-pane` into `exec_cmd.build(kind=other)` —
  **with resume args when the slot is dead** (retained `session_id`),
  fresh otherwise — and finally publish the slot through
  `windows.update_mapping`. The placeholder is what makes "scrub before
  launch" enforceable at all (`tmux.split_window` starts its command
  before returning the pane id, so launching the agent directly in the
  split could beat the scrub); publishing last means **no caught
  failure ever needs a mapping rollback** — a failed split/scrub/
  respawn just kills the placeholder and deletes its files, the mapping
  never having changed — and a mid-branch process death can never
  strand a *mapped* inert placeholder that the tick, `agent-other`, and
  restore would all misread as a healthy live slot.
- Both live → jump focus between the two agent panes.
- Full-pane = native zoom (`Ctrl-Space z`); no code.
- On the ctrl window or any window without a mapping, `agent-other`
  no-ops with a `display-message` explaining why.

**Start-vs-start race**: the whole start branch — eligibility check,
split, scrub, respawn, publication — runs while holding the
**per-worktree cleanup lock** (Section 3; the eligibility read is
stable under it because every slot liveness transition takes that
lock), so two near-simultaneous invocations serialize: the loser
re-reads a mapping whose secondary is now live and falls through to
the focus-jump branch. No third pane, no untracked pane. (The lock is
held across a few tmux calls — milliseconds; the tick just waits its
turn. The mapping lock is only ever taken inside `update_mapping`,
after the cleanup lock — the Section 3 lock order.) **Caught failures
need no mapping rollback**: publication is the last step, so **any**
exception up to and including the publication itself just kills the
created pane (placeholder or respawned agent) and deletes its per-pane
files before releasing the lock — a failed atomic publication means
the mapping never changed (tested for both the respawn-failure and
publication-failure cases). The one remaining hole — the process dying
mid-branch, before publication — leaves an *unmapped* pane (inert
placeholder or just-respawned agent), which is deliberately tolerated
rather than reconciled: it is fully visible in the window (nothing
here is silent), closing it is one keystroke, and the next
`agent-other` still finds the slot absent/dead and starts normally.
Its per-pane files are not permanently leaked: they sit unreferenced
by any mapping until that pane id is recycled, at which point the
spawn-clears-assigned-id rule (Section 3's aliasing guard) scrubs them. A reservation/
reconciliation protocol to auto-reap the pane would outweigh the
failure it guards against; recorded as an honest limitation (also
listed in the ARCHITECTURE.md limitations update).

No provision worker is needed: the container is up, the worktree exists,
and the hook setup is a cheap idempotent ensure-provisioned call.

Closing the secondary needs no command: quit the agent, the pane dies,
the tick marks the slot dead (resume identity retained). `agent-kill`
still kills the whole window.

**`agent-new`**: unchanged UX; spawns the default agent only, records its
kind in slot 0. The `--provision` worker adds the Codex
ensure-provisioned step (host or container as appropriate) so the
secondary is ready before it's ever requested.

**`agent-layout`**: compact-mode transition kills exactly the panes
tagged `@role=overview` — never by index (replacing today's
kill-all-but-index-0 loop, which would kill a secondary agent) — so
agent panes (one or two) always survive a layout toggle. Split-mode
transition re-attaches the overview via `attach_overview_pane`, which
switches to a **full-width bottom split**: identical result for
single-agent windows, and it spans both agents in a dual window instead
of nesting under one of them. The attach stays idempotent.

**`tmux.split_window` grows the parameters this needs** (today it can
only emit a plain vertical split): a direction (`-v`/`-h`) and a
`full_size` flag (`-f`). The overview attach uses `-f -v` (full-width
bottom); `agent-other` uses `-h` at 50% on the default agent pane. Unit
tests assert the exact argv for both shapes.

**Restore** (`agent-restore`): snapshot entries carry slots; both come
back. The plan model becomes **one window-level action that owns
slot 0, plus one independent action per existing secondary slot** —
slot 0 is never described twice, and classification is no longer a
single value that drops an entry wholesale:

- The window action stays keyed on the default slot exactly as today
  (`skip`/`reactivate`/`revive`/`fresh`) and is the *only* thing that
  acts on slot 0 (and on window existence/layout).
- Each **existing secondary** slot gets its own action (a mapping
  without a secondary slot is a normal single-agent window and is never
  "repaired" into a dual one): pane alive and healthy → `none`; pane
  alive but `phase=errored` (a failed earlier attempt left the
  placeholder) → `reactivate` in place; slot dead (`pane_id: null`) or
  its recorded pane gone → `revive` with the slot's resume args.
- **An entry is dropped from the plan only when the window action is
  `skip` and every secondary action is `none`.** A healthy default with
  a dead secondary stays in the plan (secondary-only repair); a dead
  default with a healthy secondary revives only the default — actions
  never touch a healthy sibling pane, so no duplicate spawns.

**Harvest barrier**: before restore creates or scrubs *any* pane, a
global harvest phase reads `session-<old pane>.id` from disk for
**every** slot of **every** snapshot entry and merges the ids into the
plan — covering ids captured after the snapshot but before the server
died. This must be a barrier, not a per-slot step: on a fresh server,
slot A's newly assigned pane id can equal slot B's *old* pane id, and
A's pre-launch scrub would destroy `session-<id>.id` before B was ever
processed. Only after the harvest may pre-creation assign and scrub
recycled ids. (The tick's death-marking keeps its inline merge — it
runs against a live server where the observed pane id is current.)
Pre-creation builds the dual split for two-slot entries (role-aware
around the optional overview pane, using the new split parameters);
activation respawns each pane with its own kind's resume command and
rewrites each slot's `pane_id` (stripped form) through
`windows.update_mapping`, preserving both session ids.
**Rebuild** (`agent-rebuild`): re-execs every live slot pane via
`exec_cmd.build`, kind-aware; its "actively working" tally counts a
window busy when **any** live slot is `R`/`W`/`B`.

**Testing** (monkey-patched `tmux`, as today): config parsing/validation
of new keys; mapping round-trip incl. legacy synthesis, `schema: 2`,
dead slots, and `last_pane_id`; `update_mapping` sibling-lock
serialization (two writers) and locked deletion; the dead-slot
compare-and-set — including the race where `agent-other`'s revival wins
before the tick's death-marking acquires the lock (must no-op) — and
the start-vs-start race (two concurrent `agent-other` starts → one
pane, loser focus-jumps); session id merged from disk before per-pane
file cleanup; the dead-slot sweep (tick starting from an already-null
secondary with surviving per-pane files deletes them and clears
`last_pane_id`); multi-slot tick →
`@state_code` join + combined `@state_fg`; slot-list state-code parser
(`R|I`, `B2|R`, single, empty); live-slot-only summary and header
counts; `exec_cmd.build` resume-arg shaping per kind and dead-slot
revive; `tmux.split_window` exact argv for `-f -v` (overview) and `-h`
50% (`agent-other`); hooks.json merge (removes owned entries by exact
structural match only — including obsolete action words from older
versions — preserves foreign entries sharing a group, drops only
emptied groups); ensure-provisioned digest + canonical-structure
verification (mutated script, hand-edited hooks, duplicated or
event-misfiled owned entries → re-provision);
restore's per-slot plan (healthy default + dead secondary stays planned;
dead default + healthy secondary revives only the default);
codex-hook.sh compile test + guard
behavior — including that hooks no-op without the `TMUX_AGENTS_AGENT`
marker (the `agent-terminal` scenario) — + session-id pin rules (init
pins on every source except a `startup` with an existing differing pin;
phase written for `startup`/`new`/`clear`/`resume`, pin-only for
`compact` and unknown sources; mismatch drops; absent-pin capture per
gate outcome); restore's harvest barrier (slot A assigned slot B's old
pane id, latest session id only on disk → B still resumes correctly);
the sweep-vs-spawn cleanup lock (sweep passes its collision check, a
concurrent spawn scrubs+publishes the same id → the new agent's files
survive); the lock-inversion test (sweep and a spawn each paused after
their first acquisition → no deadlock, per the cleanup→mapping order);
publication-last failure handling (respawn fails → placeholder killed,
files deleted, mapping untouched; publication fails → agent killed,
files deleted, mapping unchanged); the sweep's compare-and-set clear
(crash → revive-before-sweep keeps the pointer; an old sweep cannot
clear a newer death's pointer) and its aliasing guard (fresh-server
scenario where a stale `last_pane_id` equals a newly assigned live
pane id → sweep skips; spawn paths clear stale files for their
assigned id); the `source=startup` pin exception (existing differing
pin survives a nested startup, `new`/`clear` still re-pin); concurrent
container provisioning (unique temp names); `agent-other`'s branches
(dead-default message / start / revive-dead / focus-jump / no-mapping /
preflight skipped on custom exec command / rollback on publication
failure kills the pane and deletes its per-pane files); layout kills
only `@role=overview`. The E2E pass additionally confirms
the `SessionStart.source` vocabulary and that a restored (`resume`)
pane transitions `S → I` while sitting at the prompt. End-to-end via the
`verify` skill against a scratch server — including a **real linked
worktree** with user-layer Codex hooks, the one-time `/hooks` approval
UX, subagent payload observation (the release gate), and `TMUX_PANE`
presence in hook payload handling on host and in a container.

**Docs**: README (config keys, `Ctrl-Space O`, dual-agent workflow, the
user-layer `~/.codex/hooks.json` + `codex-hook.sh` provisioning and its
one-time `/hooks` approval, codex-in-container prerequisites),
ARCHITECTURE.md (mapping schema v2, pipeline, `agent-other`, per-kind
letter vocabulary, honest-limitations additions: denied-permission `W`
staleness, subagent-flicker caveat per release-gate outcome, the
split-before-publication crash window — visible unmapped pane, manual
close, next `agent-other` unaffected — and nested Codex CLI launches
inside an agent pane being unsupported), BACKLOG.md (three
research-derived items added with revision 1, plus the Claude
agent-launch-marker item from revision 6).
