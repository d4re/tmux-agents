# CLAUDE.md

Project-specific notes for future Claude sessions. The user-facing docs are
`README.md` (usage), `docs/ARCHITECTURE.md` (current-state map of components,
data flow, features), and `BACKLOG.md` (deferred ideas).

## Keep `docs/ARCHITECTURE.md` honest

When reviewing a change (your own or someone else's), check whether
`docs/ARCHITECTURE.md` still matches reality. Things that should
trigger an update: a new module under `src/tmux_agents/`, a new CLI
entry point, a change in the state-pipeline shape (hooks → tick →
overview), a new on-disk path under `paths.py`, a new `projects.toml`
key, or a feature added/removed from the supported-features list.
Touch-ups belong in the same change; if a refactor leaves the doc
stale, update it before merging.

## What this is

A Python-packaged toolkit around a dedicated tmux socket/config that lets the
user run 4–6 concurrent Claude Code agents, each typically inside a project's
devcontainer. `agents` is the user's entry point; everything else is internal
helpers wired into the status line and keybindings.

## Isolation model (non-obvious)

This setup runs on a **dedicated tmux socket and config**, not the default
ones:

- Socket: `tmux -L agents ...`
- Config: `~/.config/tmux-agents/agents.conf`

Nothing is written under `~/.config/tmux/`. The isolation is there so the
user's zsh4humans (z4h) auto-started tmux doesn't pick up this config. When
driving tmux from the shell for debugging, always pass `-L agents` — a plain
`tmux list-sessions` will look at the wrong server.

The `agents` command (`src/tmux_agents/commands/launcher.py`) is a
4-path orchestrator: live-session attach, no-snapshot legacy
`tmux new-session -A`, snapshot+consent restore handoff (rename
`windows/` → `windows.previous/`, start tmux detached, spawn
`agent-restore --background`, execvp into `tmux attach`),
snapshot+decline clean slate. The 5-second default-Y prompt is shown in
the user's current terminal before tmux comes up. See the "Session
restore" section below for the full flow.

## Dev loop — read this before making changes

Common commands are `Makefile` targets — prefer them over retyping raw
invocations:

| Target | Does |
|---|---|
| `make check` | Everything CI gates on: `ruff check` + `ruff format --check` + `pytest`. Run before pushing — tests alone are not the whole CI gate. |
| `make test` / `make lint` / `make format` | The individual pieces. |
| `make reinstall` | Reinstall the uv tool from this checkout (see below). |
| `make conf-sync` | Copy `agents.conf` to the live config + reload the server. |

`reinstall` and `conf-sync` mutate **global** state (the one installed
tool, the one live config) — with multiple agents in sibling worktrees,
whoever ran them last wins. Only run them when this worktree's code
should be live.

The install uses `uv tool install` from the repo. Editing source does **not**
update the installed executables. After a code change:

```
uv tool install --reinstall --no-cache .   # = make reinstall
```

`--no-cache` is load-bearing: `--reinstall` alone can pull a stale wheel from
uv's build cache and silently ship old code, even when the source changed.
Don't use `--force` either — same hazard. If an installed command still
behaves like the old code after a reinstall, read
`~/.local/share/uv/tools/tmux-agents/lib/python3.12/site-packages/tmux_agents/commands/<cmd>.py`
directly to confirm the install is the culprit before debugging elsewhere.

Edits to `agents.conf` in the repo also don't take effect until they reach
`~/.config/tmux-agents/agents.conf`. `make conf-sync` does the copy +
live-server reload in one step.

There's a `BACKLOG.md` item for a `dev-link` helper that would symlink
the conf and `uv tool install --editable`. Deliberately not built — see
the multi-worktree hazard noted on that item.

Tests run with `uv run pytest -q`. The suite is fast (under a second); run it
before committing. CI also gates on `ruff check` and `ruff format --check`,
so `make check` is the real pre-push bar.

## Agent support files

- `AGENTS.md` is a symlink to this file (for non-Claude tooling); edit
  CLAUDE.md only.
- Project skills live in `.claude/skills/`: `verify` (how to verify a
  change end-to-end without touching the user's live server) and
  `debug-state` (state-pipeline introspection commands + symptom table).
- `.claude/settings.json` (checked in) pre-allows the safe read-only
  commands: `make check/test/lint/format`, `uv run pytest`/`ruff`, and
  the read-only `tmux -L agents list-*`/`show-options` calls.
- `.claude/settings.local.json` here is **provisioned by tmux-agents
  itself** (this repo is dogfooded); never edit it by hand — it's
  regenerated from `hooks/agents.json`.

## Worktrees are managed externally

Feature branches live under `.worktrees/` and are created/removed by the
user's own tooling (typically `agent-new`). When finishing work in a
worktree, don't `git worktree remove` it yourself — the user handles
cleanup. Doing it from inside the worktree also strands Bash on a dead
CWD for the rest of the session. Merge / push as usual and stop there.

## State classification — hook-driven

`src/tmux_agents/state.py` just defines the seven display-letter constants
(`R`, `W`, `B`, `Z`, `I`, `X`, `S`). The actual classification is:

- **Phase** (`running` / `waiting` / `idle`) is written by Claude lifecycle
  hooks to `<worktree>/.local/.tmux-agents/state-<pane>.json`. The hook
  bodies live in `src/tmux_agents/hooks/agents.json` (shipped as package
  data) and are provisioned into each worktree's
  `.claude/settings.local.json` by `agent-new`.
- **Background/scheduled items** are tracked as a *set of self-expiring
  marker files* under `<worktree>/.local/.tmux-agents/pending-<pane>/`,
  one file `<kind>__<id>` per pending/running thing. `write-state.sh`
  `add-`/`del-` subcommands (keyed off the `ScheduleWakeup`, `CronCreate`,
  `CronDelete`, `Agent`, `Bash` matchers) write/remove these, extracting ids
  + signals from the hook payload via constrained sed. The host-side
  `src/tmux_agents/registry.py::scan` computes each marker's expiry, GCs the
  dead ones, and returns live background/sleeping counts. Because every
  marker self-expires, the counts can't drift the way the old cron counter
  did. `B` = work executing now (background subagent / background Bash);
  `Z` = will resume on its own (wakeup / one-shot cron / recurring cron).
  Expiry is exact where a signal exists (wakeup `scheduledFor`; one-shot cron
  next-fire via `croniter` from the machine-readable `tool_input.cron`,
  host-side in local TZ; recurring cron = 7-day backstop). Background
  subagents and background Bash are removed two complementary ways. (1) Fast
  path: a `clear-completed` hook on `UserPromptSubmit` — completion usually
  surfaces as a `<task-notification>` whose `<task-id>` equals the launch id
  (`agentId` / `backgroundTaskId`). (2) Authoritative: a `reconcile` hook on
  `Stop`/`StopFailure` — the stop payload carries `background_tasks` (the live
  running|pending backgrounded task registry, each entry `{id,type,status,…}`),
  and `reconcile` reaps any `bg-shell__`/`subagent__` marker whose id is absent
  from it. The fast path alone misses *mid-turn* completions: a task finishing
  while the agent is still working has its notification injected as an
  **attachment**, which fires no `UserPromptSubmit`, so the marker would linger
  to the TTL and show a stale `B` once the pane goes idle (`B` outranks `I`).
  `reconcile` closes that at the instant the pane stops. The per-kind TTL is now
  only a last-resort backstop (session ends before `Stop`, or payload drift). No
  `SubagentStop` hook is wired — its payload carries the same `background_tasks`,
  so the `Stop` reconcile already covers subagent markers. A subagent
  inherits the parent's `TMUX_PANE`, so its tool-uses fire the parent's hooks;
  `write-state.sh::is_subagent` drops those (subagent payloads carry
  `agent_id`/`agent_type`; main-agent payloads don't) so the pane tracks the
  main agent only.
- **Pane-dead override** is host-side: one batched `tmux list-panes -s`
  per tick in `src/tmux_agents/tmux.py::dead_window_ids`.
- **Letter derivation** lives in `src/tmux_agents/phase.py::derive_letter`
  with priority `X > W > R > B > Z > I > S`. `S` is lowest — used by
  `agent-restore` while a placeholder pane is awaiting its container/Claude.
  `B` and `Z` carry a suffixed count (`B2`, `Z3`, …) that the overview
  renders as `background·N` / `sleeping·N`.

The host-side tick (`src/tmux_agents/commands/state_tick.py`) reads the
JSON via `windows.read_mapping(window_id)`, scans the pending registry, and
publishes the derived letter (or `B<N>`/`Z<N>`) as the per-window
`@state_code` tmux option, which `agent-overview` reads off the
`list-windows -F` row (no separate `.state` file). No `capture-pane` call
remains on the state path — the screen-scraping layer was removed when
Claude moved to the alternate screen buffer (fullscreen) which has no tmux
scrollback.

## Status line rendering

tmux's `#(...)` interpolation treats its output as a **tmux format string**,
not as ANSI. `agent-state` emits `#[fg=green]R#[default]`-style codes
(via `overview.render_summary(tmux_format=True)`) as the last thing it
prints, after running the state tick — one Python invocation per status
interval covers both jobs. Raw ANSI (`\x1b[32m...`) would render as
literal `[32mR[0m` in the status bar. If colors look wrong in the
status line, check `state_tick.main`'s final `print(...)` call and the
tmux-format branch in `overview.render_summary`.

Per-tick `set-option @state_fg` calls are batched (`tmux.apply_commands`
→ `source-file -`) and skipped on ticks where the (window-id, name,
letter) fingerprint matches the cache at `<state_dir>/tick.cache` —
most ticks emit zero subprocess calls for window options.

## Layout of the source

- `src/tmux_agents/theme.py` — state-color palette. Defaults (dark +
  light) and optional overrides loaded from `~/.config/tmux-agents/theme.toml`.
  Call `theme.get_palette()` from anywhere that renders a state color;
  do not re-hardcode hex values.
- `src/tmux_agents/paths.py` — all filesystem locations. Every path that's
  not test-scoped should come from here so `TMUX_AGENTS_CONFIG_DIR` /
  `TMUX_AGENTS_STATE_DIR` overrides work for tests.
- `src/tmux_agents/tmux.py` — the only module that shells out to `tmux`. Add
  new tmux calls here, not inline.
- `src/tmux_agents/commands/*.py` — one file per CLI entry point, registered
  in `pyproject.toml` under `[project.scripts]`.
- `tests/conftest.py` provides `tmp_state_dir` and `fixtures_dir` fixtures;
  most tests monkey-patch `tmux.capture_pane` / `tmux.list_windows` rather
  than actually driving tmux.

## SSH agent forwarding (non-obvious bits)

`src/tmux_agents/_ssh_*.py` reach the container as real files, not inlined source:

- **Host pump**: spawned as `python -m tmux_agents._ssh_pump_script <container>
  <user>` (via `sys.executable`, so the installed tool's venv resolves the
  package). It imports `_ssh_framing` directly.
- **In-container relay**: the pump delivers `_ssh_framing.py` +
  `_ssh_relay_script.py` verbatim into `/tmp/tmux-agents-relay/` (piped through
  `docker exec … sh -c 'mkdir -p … && cat > …'`) on every (re)spawn, then runs
  `python3 /tmp/tmux-agents-relay/_ssh_relay_script.py`. The relay imports
  framing as a sibling on `sys.path[0]` via a real two-branch fallback:
  `from tmux_agents._ssh_framing import …` → `from _ssh_framing import …`.

There is **no source splicing** — no regex strip, no `RELAY_SCRIPT_SOURCE`
injection, no `assemble_*` helpers. The relay's two-branch import is the only
contract; `test_ssh_relay.py::test_relay_imports_framing_as_sibling_without_package`
exercises the delivered-file path under `python -E -S` (no env, no site, no
`tmux_agents` package).

The host pump is launched with `start_new_session=True` and reparents
to launchd. It self-supervises (`_ssh_pump_script.supervise`):
re-spawns the in-container relay with backoff (1s→30s) on docker-exec
EOF or framing desync. It exits cleanly only when the container is
gone (`docker inspect`), the relay returns `EXIT_DUPLICATE` (75 — set
in `_ssh_framing.py`, meaning another pump owns the UDS), or
`SSH_AUTH_SOCK` is unset on the host. `_maybe_spawn_ssh_pump` in
`commands/new.py` also probes pump health with `ssh-add -l` (1.5s
timeout) before deciding to spawn — broken-but-listening pumps get
SIGTERMed and replaced; healthy ones are left alone (idempotent
`agent-new`).

## Things that are deliberately not there

- No daemon. State is refreshed on the tmux 2-second status-interval tick by
  running `agent-state` as a format substitution.
- No mock of the database / tmux in integration tests — but unit tests freely
  monkey-patch the `tmux` module because shelling out isn't the thing under
  test.
- No container lifecycle management beyond "run `up_cmd` if container is
  down". Deliberately out of scope.
- No `/loop` manual-toggle fallback. Automatic detection works.

## Session restore

`agents` runs `agent-restore` automatically on fresh-server start when
`~/.config/tmux-agents/windows/` has stale entries. The launcher prompts,
moves the snapshot to `windows.previous/`, starts tmux detached, spawns
`agent-restore --background`, then `execvp`s into `tmux attach`. The
worker pre-creates placeholder panes (visible in the overview as the
new `S` state — palette + `derive_letter` priority below `I`), runs
`up_cmd`s in parallel grouped by project, attaches the overview pane
in split layout, and `respawn-pane`s each pane with
`claude --resume <session_id>` once its container is ready. `--resume`
is injected via the `{resume_args}` substitution placeholder added to
`Project.substitute()`; the session id is captured by the
`SessionStart` hook (`write-state.sh init`) into
`<worktree>/.local/.tmux-agents/session-<pane>.id` and merged into the
window mapping by the state tick.

Failures per entry are isolated: logged to
`/tmp/tmux-agents/tmux-agents.log`, and the placeholder pane is replaced
with a heredoc displaying the reason + recovery hint. The per-pane
state is set to `phase=errored` (which `derive_letter` promotes to
`X`).

Host-only projects now have a smart default `exec_cmd`
(`cd {workdir} && exec claude{resume_args}`) symmetric with container
projects, so users no longer need to hand-write the placeholder.

