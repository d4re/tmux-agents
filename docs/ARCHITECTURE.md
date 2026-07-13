# Architecture

Current-state map of `tmux-agents`: how the pieces fit, where the data flows,
and what each module owns. User-facing setup lives in `README.md`. This file
should be kept in sync as the code evolves — see CLAUDE.md.

## What it is

A Python package plus a tmux config and a hook bundle that lets one user run
4–6 concurrent Claude Code agents in a single tmux session, each typically
running inside a project's devcontainer. The package ships eleven CLI entry
points (`agents`, `agent-new`, `agent-kill`, `agent-rebuild`, `agent-state`,
`agent-overview`, `agent-rename`, `agent-layout`, `agent-restore`,
`agent-vscode`, `agent-terminal`)
that the tmux config wires into keybindings, hooks, and the status line.

## Isolation model

Everything runs on a dedicated tmux socket and config:

- Socket: `tmux -L agents`
- Config: `~/.config/tmux-agents/agents.conf`
- Volatile state: `/tmp/tmux-agents/` (override with `TMUX_AGENTS_STATE_DIR`)

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

State is the most non-obvious part of the system. It moves through three
layers:

```
Claude lifecycle hooks               host-side tick                tmux + overview
─────────────────────────            ──────────────────             ───────────────
SessionStart, UserPromptSubmit,      agent-state                    agent-overview
Stop, Notification,                  (run every ~1s from            (curses TUI in the
PostToolUse[ScheduleWakeup/           status-right format)           split-layout pane)
CronCreate/CronDelete/Agent/Bash],                                  (compact: summary chunk
SessionEnd                                                           in status-right)
   │                                    │
   ▼                                    ▼
<worktree>/.local/.tmux-agents/    read window mapping ────►  tmux per-window options
  state-<pane>.json   ────────►    read state JSON +          @state_code (R/W/B<N>/
  pending-<pane>/<kind>__<id>      scan pending registry      Z<N>/I/X/S) + @state_fg,
                                   derive letter              read by agent-overview
                                   set @state_code/@state_fg  via `list-windows -F`
                                   per window
```

Per-step:

1. **Claude hooks** (provisioned by `agent-new` into
   `<worktree>/.claude/settings.local.json` from `src/tmux_agents/hooks/agents.json`)
   dispatch on every lifecycle event to a tiny helper script,
   `<worktree>/.local/.tmux-agents/write-state.sh` (also provisioned, from
   `src/tmux_agents/hooks/write-state.sh`). The script writes a JSON state
   file *per pane* (the `phase`) and maintains a per-pane registry directory
   `pending-<pane>/` of self-expiring marker files — one per backgrounded or
   scheduled thing. `add-`/`del-` subcommands are keyed off the
   `ScheduleWakeup`, `CronCreate`/`CronDelete`, `Agent`, and `Bash` matchers.
   Background subagent/Bash markers are reaped two ways: a `clear-completed`
   subcommand on `UserPromptSubmit` (fast path — when a `<task-notification>`
   completion arrives as a fresh prompt), and a `reconcile` subcommand on
   `Stop`/`StopFailure` that diffs the markers against the `background_tasks`
   live-task set carried in the stop payload (authoritative — also catches
   completions delivered mid-turn as an attachment, which fire no
   `UserPromptSubmit`).
   The pane id is `${TMUX_PANE#%}` —
   agents must run with `-e TMUX_PANE` exposed so the env var survives
   `docker exec`. The hook commands themselves are one-liners of the form
   `sh "$CLAUDE_PROJECT_DIR/.local/.tmux-agents/write-state.sh" <action>`;
   all shell logic lives in the script.
2. **Host-side tick** — `agent-state`, invoked from the tmux status-right
   format (so it runs every status interval, ~1s), enumerates live
   windows, looks up each via the `windows/<window_id>.json` mapping
   written at agent-new time, reads the per-pane JSON phase + scans the
   `pending-<pane>/` registry (`registry.scan`, which GCs expired markers and
   returns live background/sleeping counts), derives a single letter via
   `phase.derive_letter`, and publishes it as the per-window `@state_code`
   tmux option (along with `@state_fg`/`@state_selected_fg` for colors),
   batched into a single `tmux source-file -` invocation and skipped
   entirely on ticks where the (window-id, name, code) fingerprint —
   `code` includes the B/Z overlay count, so a `B2`→`B3` change still
   re-publishes — is unchanged from the prior tick (cached at
   `<state_dir>/tick.cache`). Finally, `agent-state`
   prints the summary chunk on stdout, so a single `#(agent-state)`
   substitution in `status-right` fills the status bar (no second
   `agent-overview` call on the hot path).
3. **Renderer** — `agent-overview` runs a curses TUI in the bottom pane
   under the split layout; it reads each window's `@state_code` option
   (carried on the `list-windows -F` row) to render the per-window rows.
   The status-line summary is rendered inside `agent-state` (using counts
   already collected during the tick).

State letters: `R`, `W`, `B`, `Z`, `I`, `X`, `S` (running, waiting,
background, sleeping, idle, errored, starting). `B` = work executing now
while otherwise idle (background subagent / background Bash); `Z` = nothing
running now but it will resume on its own (self-paced wakeup, one-shot or
recurring cron). Both carry a count suffix rendered as `background·N` /
`sleeping·N`. Priority `X > W > R > B > Z > I > S` lives in
`phase.derive_letter`. `S` is the lowest-priority letter,
used by `agent-new` and `agent-restore` while a placeholder pane is awaiting its
container/Claude.

**`phase_hint`** is a host-side phase field on the `WindowMapping`
(`str | None`). The state tick consults it only when the per-worktree
state file (`state-<pane>.json`) does not yet exist — i.e. during the
interval between "window created" and "Claude's `SessionStart` hook has
fired." It is set to `"starting"` when the interactive `agent-new` writes
the initial mapping, drives the `S` letter during pre-worktree startup, and
is cleared to `None` once `_provision` confirms the real worktree path. On
a fatal failure before the worktree exists, it is set to `"errored"` to
show `X`. A present worktree state file always wins over `phase_hint`.

Pane-dead detection is host-side: one batched `tmux list-panes -s`
call per tick via `tmux.window_pane_map(session)` returns the set of
live pane ids per window. A window is flagged `X` either when its
window id has no live panes (the full-window-dead case) or when the
mapping's recorded `pane_id` is absent from the live set for its
window (the pane-level death case — an overview pane keeps the window
alive after the agent pane exits).

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
   `overview.attach_overview_pane` adds the 25% `agent-overview` pane
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
3. **Worktree.** `worktree.resolve` returns `<repo>` if no branch, else
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
   fresh worktree — no-branch mode (runs Claude in `<repo>` as-is) and
   reuse of an existing `.worktrees/<branch>` — instead run
   `worktree.check_freshness`: a best-effort `git fetch origin <default>`
   + `git rev-list --count HEAD..origin/<default>` that emits a stage
   **warning** (holding the pane for Enter) when the checkout is behind,
   so a stale base is surfaced rather than silently inherited. It never
   modifies the working tree and degrades to an info line offline.
   **After resolve**, the mapping is rewritten with the real
   `host_worktree` and `phase_hint=None` (the worktree state file now
   takes over).
4. **Provision hooks.** `provisioning.provision_settings` merges
   `hooks/agents.json` into `<worktree>/.claude/settings.local.json`
   (idempotent; non-fatal on failure — emits a warning to the log).
5. **Respawn.** Once the log file is closed:
   - No warnings → `startup._respawn_with_retry` swaps the pane into
     the real `exec_cmd` (Claude).
   - Non-fatal warning → `startup.hold_pane_then_exec` shows the log
     plus a "press Enter to launch Claude" prompt (pane state shows `W`),
     then `exec`s into Claude on Enter.

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
| `paths.py` | All filesystem locations. Env-overridable via `TMUX_AGENTS_CONFIG_DIR` / `TMUX_AGENTS_STATE_DIR`. Every path used elsewhere goes through this. |
| `state.py` | The seven display-letter constants (`R`/`W`/`B`/`Z`/`I`/`X`/`S`). |
| `phase.py` | Bridges hook-written `phase` JSON + registry background/sleeping counts + `pane_alive` → display letter, applying the priority rule. |
| `registry.py` | Scans a pane's `pending-<pane>/` marker dir, computes each marker's effective expiry (exact from `scheduledFor`/cron-expr where possible, heuristic timeout otherwise), GCs expired ones, returns live background/sleeping counts. Uses `croniter` for one-shot cron next-fire (host-side, local TZ). |
| `theme.py` | Color palette. Dark + light defaults, optional `theme.toml` overrides, derived ANSI/tmux/contrast variants for active-row inversion. Cached per-process. |
| `tmux.py` | Sole module that shells out to `tmux -L agents`. Window/pane listings, capture, rename, split, kill, option setters. |
| `windows.py` | The `<config_dir>/windows/<window_id>.json` mapping that lets the host tick translate a tmux window into the worktree path + pane id its hooks write under. |
| `config.py` | `projects.toml` loader. Resolves `container` vs `devcontainer = true`, fills in defaults (`up_cmd`, `exec_cmd`, `container_workdir`, `user`, `forward_ssh_agent`). The optional `base_branch` field is stored on `Project` and forwarded to `worktree.resolve` as `base_override`. |
| `container.py` | Docker probes: `is_running`, `current_name` (by name OR `devcontainer.local_folder` label), `ensure_up` (runs `up_cmd` once if down), and `rebuild` (force-recreate: devcontainer projects append `--remove-existing-container` [+ `--build-no-cache`] to `up_cmd`; named-container projects `docker rm -f` then re-run `up_cmd`). |
| `exec_cmd.py` | Shared builder `build(proj, *, branch, claude_session_id, container_name, label)` for the pane launch command, injecting ` --resume <id>` via the `{resume_args}` placeholder. Used by both `agent-restore` and `agent-rebuild` so resume semantics stay identical. |
| `worktree.py` | `git worktree add/remove`. `_resolve_base()` determines the commit-ish for new worktrees (fetch `origin/<default>` → cached ref → HEAD fallback). For container projects, runs git via `docker exec` so the worktree's internal `.git` pointers are container paths. |
| `provisioning.py` | Idempotent merge of `hooks/agents.json` into `<worktree>/.claude/settings.local.json`. Versioned by package version so upgrades replace stale hook groups. |
| `hooks/agents.json` | Package data: the hook *dispatch* table (`tui: fullscreen` + per-event invocation of `write-state.sh`). Shipped, not generated. |
| `hooks/write-state.sh` | Package data: the actual shell body the hooks invoke. Provisioned per worktree at `<worktree>/.local/.tmux-agents/write-state.sh`. Single source for the phase-JSON write + the registry `add-`/`del-` marker subcommands (extracting ids/signals from the hook payload via constrained sed). All counting/expiry/cron-parsing is host-side in `registry.py`. |
| `pickers.py` | fzf-backed primitives (`pick_one`, `prompt_yes_no`, `pick_or_create`, `prompt_free_text`) plus `NO_BRANCH_SENTINEL`. Used by `agent-new` / `agent-kill` / `agent-rebuild`. No tmux/project knowledge. |
| `overview.py` | Row model (header / agent), `format_line_plain` / `format_header`, the status-line summary renderer (`render_summary`, called from `state_tick`), fold persistence, and the curses TUI for the split-layout bottom pane: cursor model, state-colored rendering, click hit-testing, keyboard dispatch (↑↓ ↵ N K R), and `attach_overview_pane` (`@role=overview`). The TUI auto-tracks the active window unless the user moved the cursor. |
| `ssh_forward.py` | Probes + pump spawn for SSH agent forwarding. Spawns the pump as `python -m tmux_agents._ssh_pump_script`; the pump delivers the relay into the container as plain files (no inlining). |
| `_ssh_framing.py` | Wire framing (4-byte length prefix + payload, `\x00\x00\x00\x00` sentinel) and the bidirectional `splice()` between a raw UDS socket and a framed stream pair. |
| `_ssh_pump_script.py` | Host-side pump. For each in-container SSH op, opens a fresh connection to the host's `$SSH_AUTH_SOCK` and splices it. |
| `_ssh_relay_script.py` | In-container relay. Bind-or-exit dedup at `/tmp/tmux-agents-ssh.sock`, accepts client connections, splices each through stdin/stdout to the pump. |
| `startup.py` | Shared spawn/restore primitives used by both `agent-new` and `agent-restore`: `placeholder_command` (build the `tail -F` pane command), `_respawn_with_retry` (fork-safe respawn with backoff), `_detach_stdio` (redirect fds 0/1/2 to `/dev/null` in a backgrounded worker), `_write_pane_state` (write a `phase=…` state JSON), `show_static_text` (respawn pane into a static heredoc), `hold_pane_then_exec` (show log + "press Enter" prompt, then exec). |
| `progress.py` | Per-stage progress display. `Reporter` writes to a single output stream; `MultiReporter` fans out to N reporters for events shared across restore's project groups. Symbols: `▸` info / `✓` success / `!` warning (non-fatal) / `✗` fatal failure. Both `agent-new --provision` and `agent-restore` write each window's progress to `<state_dir>/spawn-<window_id>.log` (`paths.spawn_log`); the placeholder pane runs `tail -F <log>` and is replaced by `respawn-pane` when the worker finishes. |
| `commands/restore.py` | The `agent-restore` worker. Snapshot reading + validation, project grouping, placeholder pre-creation, container ensure-up + per-entry respawn, failure logging + error display. Imports shared primitives from `startup.py`. |
| `commands/rebuild.py` | `agent-rebuild`. Interactive half (popup): eligible-project picker with live agent tallies, tiered confirm (default-No when any agent is `R`/`W`/`B`), then fires the detached worker via `tmux run-shell -b`. `--worker` half (parented to the server): fork/setsid/detach-stdio (same as `agent-new --provision` — otherwise tmux paints the worker's output, e.g. `devcontainer up` JSON, over the active pane in view mode), then show `tail -F` progress in each affected pane, `container.rebuild`, respawn the SSH pump, then re-exec each pane via `exec_cmd.build` (`claude --resume <id>`). Per-pane failures isolated; container-rebuild failure marks every pane `X`. |
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
| `agent-new [<project> [<branch>]]` | `commands/new.py` | Two-mode entry point. **Interactive** (popup): fzf-pick project/branch, create window immediately with a placeholder pane tailing the spawn log (`spawn-<id>.log`), write mapping with `phase_hint="starting"`, attach overview pane, select window, spawn detached `--provision` worker, return. **`--provision` worker**: fork/setsid/detach-stdio, then container ensure-up + SSH pump + worktree resolve (or a `check_freshness` base-staleness check in no-branch mode) + hooks provision, writing progress to the spawn log; respawn the placeholder into Claude on success, hold for Enter on warning (`W`, e.g. a checkout behind `origin/<default>`), show error pane on fatal failure (`X`). |
| `agent-kill [<window>] [--prune-worktree] [--force]` | `commands/kill.py` | fzf picker by default; can target by `--window-id`. Optional `git worktree remove` (interactive force-retry on dirty). |
| `agent-rebuild [<project>] [--project N] [--no-cache] [--yes] [--worker]` | `commands/rebuild.py` | Rebuild a project's shared container and resume its agents. **Interactive** (popup): fzf-pick an eligible project (devcontainer, or named container with `up_cmd`) showing its live agent tally, warn+confirm (default-No when an agent is actively working), then spawn the detached `--worker` via `run-shell -b` so it survives the popup closing. **`--worker`**: show `tail -F` progress in each affected pane, `container.rebuild` (force-recreate), respawn the SSH pump, `respawn-pane` each pane into `claude --resume <id>`. Bound to `Ctrl-Space B`. |
| `agent-state` | `commands/state_tick.py` | Single tick of the host poll. Wired into `status-right` so tmux runs it every status interval. |
| `agent-overview` | `commands/overview.py` | Curses TUI for the split-layout bottom pane. The status-line summary is emitted inline by `agent-state` — `render_summary` is called as a function, not via this CLI. |
| `agent-rename --window-id <id> [--from-hook] <name>` | `commands/rename.py` | Replace the `:branch` half of `<repo>:<branch>`. Explicit (non-hook) renames set the `@pinned` window option; `agent-new` and `agent-restore` also set it when a branch is supplied at creation. `--from-hook` is the `pane-title-changed` mode that silently no-ops on ctrl/`@pinned`/unknown windows or empty names — so the hook keeps tracking Claude's titles on unpinned windows but never overwrites a branch label. |
| `agent-layout` | `commands/layout.py` | Toggle persistent layout file (`<state_dir>/layout`) between `split` and `compact`; rebuilds existing windows accordingly. |
| `agent-restore [--background]` | `commands/restore.py` | Read snapshot, pre-create placeholder windows (with overview pane in split layout), run devcontainer `up_cmd`s in parallel, spawn the SSH pump per container project, `respawn-pane` each pane with `claude --resume <id>`. Triggered automatically by the launcher; runnable manually for partial-failure retry or dead-pane recovery, bound to `Ctrl-Space R`. |
| `agent-vscode --window-id <id>` | `commands/vscode.py` | Open the current agent's worktree in VS Code. Host projects → `code <host_worktree>`. Container / devcontainer projects → `code --folder-uri vscode-remote://attached-container+<hex>/<container_workdir>`, reattaching to the running container resolved by `container.current_name` (no rebuild, no second container). Resolves the `code` binary via `shutil.which` first, then falls back to a top-level `code_path` in `projects.toml` (default: `/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code`). Bound to `Ctrl-Space V`. |
| `agent-terminal --window-id <id>` | `commands/terminal.py` | Pop up a shell in the active agent's context. Host projects → `os.chdir(host_worktree)` then `exec $SHELL -l` (fallback `/bin/bash`). Container / devcontainer projects → `os.execvp("docker", ["exec", "-it", "-e", "TERM", "-e", "COLORTERM", "-e", "TMUX_PANE", "-u", user, "-w", workdir, name, "bash", "-il"])`, with `-e SSH_AUTH_SOCK=/tmp/tmux-agents-ssh.sock` added when `forward_ssh_agent=True`. Container resolved via `container.current_name` (same as `agent-vscode`). Bound to `Ctrl-Space T` via `display-popup -E`. |

## Supported features

### Project types

`projects.toml` supports three modes (see `config.py`):

- **Named container.** `container = "name"` — `current_name` checks if it's
  running; `up_cmd` is required (no default).
- **Image / Dockerfile devcontainer.** `devcontainer = true` — resolved by
  the `devcontainer.local_folder=<repo>` label that VS Code's Dev
  Containers extension and the `devcontainer` CLI stamp. `up_cmd`,
  `exec_cmd`, and `container_workdir` (=`/workspaces/<repo-basename>`)
  default to the canonical devcontainer-CLI invocations.
- **Host-only.** No container fields. `exec_cmd` is optional; the default
  is `cd {workdir} && exec claude{resume_args}`.

Substitutions: `{repo}` → host repo path, `{container}` → resolved name,
`{workdir}` → host path or container path with `.worktrees/<branch>`
appended, `{resume_args}` → empty for fresh agents, ` --resume <session_id>`
(with leading space) when `agent-restore` is reviving a previous Claude
conversation.

### Layouts

- **Split (default).** Each agent window has a top pane (Claude) and a
  bottom 25% pane running `agent-overview`. The bottom pane is
  identical across windows, so the global overview is always visible. The
  pane is tagged `@role=overview` so the `MouseDown1Pane` binding
  forwards clicks to it without stealing pane focus from the agent.
- **Compact.** No splits. The status-right is `#(agent-state)` — a single
  format substitution. `agent-state` runs the host tick AND emits the
  summary chunk on stdout (in tmux-format, not ANSI: the substitution
  treats output as tmux format markup, so raw ANSI would render as
  literal escape codes). State letters get `#[fg=#…]` codes from the
  palette.

Layout choice persists at `<state_dir>/layout` (read by `agent-new` so new
windows match) and is toggled with `agent-layout` (Ctrl-Space L).

### State classification & overview

Letter set + derivation rule above. The overview groups windows by repo
(prefix before `:` in window name), shows a fold marker per repo, and
suffixes `B`/`Z` with their live item counts (rendered as `background·N` /
`sleeping·N`). Folds persist at `<state_dir>/overview-folds.json`. Active-row
coloring inverts the fg/bg using `selected_fg` (chosen from perceived
luminance for AAA contrast against light state colors). The TUI cursor
auto-tracks the active tmux window unless the user has moved it.

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

Honest limitations: a background `Bash` whose session ends before it completes
falls back to the TTL; crons restored on `--resume` don't re-fire `CronCreate`,
so they won't re-register.

Subagent isolation: a subagent inherits its parent's `TMUX_PANE`, so its own
tool-uses fire the *parent* pane's hooks. Left unfiltered this would flip the
parent `B`↔`R` while a subagent works and register a subagent-backgrounded Bash
under the parent's pending dir (where it would never see a completion
notification). `write-state.sh::is_subagent` filters these out: a subagent's
PostToolUse payload carries `agent_id`/`agent_type` (a main-agent payload never
does), so the `running`, `add-subagent`, and `add-bgshell` hooks skip when those
fields are present — the pane tracks the main agent only. The check gates on
*presence*, so a Claude Code build that drops the field degrades to the old
unfiltered behaviour rather than breaking. The main agent launching a background
subagent still registers `B` (that PostToolUse is the *parent's*, with no
`agent_id`).

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
launcher detects the orphaned `windows/<window_id>.json` snapshot and
prompts the user (`Restore N previous agents? [Y/n]`, 5-second
default-Y timer). On consent it moves the snapshot to
`windows.previous/`, starts tmux detached, spawns
`agent-restore --background`, and `execvp`s into `tmux attach`. The
worker pre-creates all windows up front (each with a `phase_hint="starting"`,
yielding the `S` state — lowest priority in `derive_letter` chain),
provisions the per-worktree hook script, and attaches the bottom overview
pane in split layout.
It groups entries by project, fires devcontainer `up_cmd`s in parallel
(one per project group, max 4 concurrent), and `respawn-pane`s each
placeholder into the real `claude` invocation as its container becomes
ready. `--resume <session_id>` is injected via the `{resume_args}`
substitution placeholder; the id was captured by the `SessionStart`
Claude hook (`write-state.sh init` → `session-<pane>.id`) and merged
into the window mapping by the state tick.

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
classifies each snapshot entry as `skip` / `revive` / `fresh` via
`classify_entry(entry, live_panes)`. `revive` is the new case: a
window is alive but its agent pane is gone (only the overview pane
survives). `pre_create_windows` splits a fresh agent pane above the
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

This manual rerun is wired to a recovery shortcut: `Ctrl-Space R` (and
`R` in the focused overview pane) run `agent-restore --background` via
`run-shell -b`, which revives every dead-pane window in one pass and skips
live ones. `--background` forks + `setsid`s and then redirects fd 0/1/2 to
`/dev/null` (`startup._detach_stdio`); without that detach the backgrounded
worker keeps `run-shell`'s capture pipe as stdout and tmux paints its output
(e.g. `devcontainer up` JSON) over the active pane. The
overview surfaces the affordance — when any window is errored, the curses
TUI footer is replaced by a right-aligned recovery alert
(`overview._restore_alert`), `⚠ N agent(s) down — press Ctrl-Space R to
restore`, in the errored color. Footers spell the full `Ctrl-Space` chord
(`overview._PREFIX`) so the prefix is discoverable to new users; the bare
keys still work while the overview pane itself is focused. Because R moved
to restore, `agent-rename` is bound to `Ctrl-Space E` / `E`.

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
  windows/<window_id>.json            window→worktree mapping (host-side)
  windows.previous/<window_id>.json   transient; populated by the
                                      launcher on fresh-server restore,
                                      consumed by agent-restore, removed
                                      at end of restore

/tmp/tmux-agents/                     ← TMUX_AGENTS_STATE_DIR
  layout                              "split" | "compact"
  overview-folds.json                 repo header fold state
  tick.cache                          last tick's per-window fingerprint
  tmux-agents.log                     unified rotating log (all components)
  (the derived letter is the per-window @state_code tmux option, not a file)

<worktree>/.local/.tmux-agents/       ← per-worktree, written by Claude hooks
  write-state.sh                      hook helper (provisioned, mode 0755)
  state-<pane>.json                   {phase, updated_at}
  pending-<pane>/<kind>__<id>         self-expiring B/Z markers (registry)
  session-<pane>.id                   UUID written by SessionStart hook
                                      (init action)
<worktree>/.claude/settings.local.json   tui:fullscreen + lifecycle hooks
```

## Testing

`uv run pytest -q` (under a second). Tests freely monkey-patch the `tmux`
module rather than driving a real server; `tests/conftest.py` provides
`tmp_state_dir` and `fixtures_dir`. The SSH relay sibling-import test
(`test_ssh_relay.py`) checks the delivered-file import path; the hook-snippets
test compiles each shell hook body to catch quoting drift.
