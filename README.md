# tmux-agents

A tmux-based runner for 4–6 concurrent Claude Code agents, each typically babysitting a PR
with `/loop` inside its own devcontainer (or on the host for non-containerized projects).

## Install

```bash
git clone https://github.com/d4re/tmux-agents.git ~/dev/tmux-agents
cd ~/dev/tmux-agents
./install.sh
```

`install.sh` installs [uv](https://docs.astral.sh/uv/) (via the official install script) and the
`tmux-agents` Python package (as a `uv tool`).
Everything lives under `~/.config/tmux-agents/` and runs on a dedicated tmux socket (`-L agents`) —
it does **not** touch `~/.config/tmux/` or interact with any other tmux sessions you may already run.

## Configure projects

Edit `~/.config/tmux-agents/projects.toml`. Examples:

```toml
# Devcontainer project with a stable name (e.g. docker-compose-based)
[api]
repo = "/Users/you/dev/api"
container = "api-devcontainer"
container_workdir = "/work"
up_cmd = "cd {repo} && devcontainer up --workspace-folder ."
exec_cmd = "docker exec -it {container} bash -lc 'cd {workdir} && claude'"

# Image/Dockerfile devcontainer (no stable Docker name — resolved by label)
[webapp]
repo = "/Users/you/dev/webapp-service"
devcontainer = true

# Host-only project (exec_cmd is optional — see below)
[scripts]
repo = "/Users/you/dev/scripts"
```

Placeholders: `{repo}`, `{container}`, `{workdir}`, `{resume_args}`. For container projects
`{workdir}` resolves inside the container; for host-only it resolves on the host.
`{resume_args}` expands to empty for a fresh agent and to a resume snippet (leading space
included) when reviving a previous conversation — ` --resume <session_id>` for Claude,
` resume <session_id>` for Codex (Codex resumes via a subcommand, not a flag).

`devcontainer = true` looks up the container by the `devcontainer.local_folder={repo}` label
that VS Code's Dev Containers extension and the `devcontainer` CLI stamp on every container,
so image/Dockerfile devcontainers (which Docker names randomly, e.g. `brave_benz`) survive
rebuilds. In this mode several fields take canonical defaults:

- `container_workdir` → `/workspaces/<repo-basename>` (the tooling's mount path; override if
  your `devcontainer.json` sets a custom `workspaceFolder`).
- `up_cmd` → `cd {repo} && devcontainer up --workspace-folder .`
- `exec_cmd` → `docker exec -it -e TERM -e COLORTERM -e TMUX_PANE -e TMUX_AGENTS_AGENT=1
  -u vscode {container} bash -lc 'export SSH_AUTH_SOCK=/tmp/tmux-agents-ssh.sock && cd {workdir}
  && exec claude{resume_args}'` (the `SSH_AUTH_SOCK` export is only present when
  `forward_ssh_agent` is on, the default — see "SSH agent forwarding" below).

`-u vscode` matches the `remoteUser` set by most Microsoft Dev Containers feature-based
templates (Python, Java, Kubernetes). If your image uses a different user — e.g. the Node
template's `remoteUser: node` — override `exec_cmd` explicitly.

For host-only projects, `exec_cmd` is now optional (defaults to
`cd {workdir} && TMUX_AGENTS_AGENT=1 exec claude{resume_args}`). For container
projects, it also has a default that uses `docker exec` (above). Override
only if you need a custom invocation; if you do, include `{resume_args}`
after `claude` to enable conversation resume on restore, and keep exporting
`TMUX_AGENTS_AGENT=1` before the agent starts — Codex's hook script (below)
refuses to write state without it, and a future custom `exec_cmd` losing it
degrades silently to an untracked-but-functional pane rather than an error.

### Running Codex alongside Claude

Every project also has a **Codex** agent kind available as a second, optional
agent in the same window/worktree — started on demand with `Ctrl-Space O`,
not spawned automatically. Two keys control which agent is primary and which
custom command launches the secondary:

```toml
# Global default (optional; "claude" if omitted)
default_agent = "claude"

[api]
repo = "/Users/you/dev/api"
container = "api-devcontainer"
up_cmd = "cd {repo} && devcontainer up --workspace-folder ."
# This project runs Codex as its PRIMARY agent instead of the global default.
agent = "codex"
# Optional override for the secondary/primary Codex launch command (same
# placeholders as exec_cmd; defaults mirror exec_cmd's shape with `codex`
# instead of `claude` and Codex's `resume <id>` subcommand spelling).
codex_exec_cmd = "docker exec -it -e TERM -e COLORTERM -e TMUX_PANE -e TMUX_AGENTS_AGENT=1 \
  -u vscode {container} bash -lc 'cd {workdir} && exec codex{resume_args}'"
```

⚠️ *Note: the example above is a custom override. The default `codex_exec_cmd`
also exports `SSH_AUTH_SOCK` to forward SSH agent credentials — copying this
example verbatim will lose SSH forwarding. Omit `codex_exec_cmd` to use the
automatic default, or include the SSH_AUTH_SOCK export if you customize it.*

- `default_agent` (top-level, optional) — `"claude"` (default) or `"codex"`.
  Sets which kind every project starts as its primary agent unless overridden.
- `agent` (per-project, optional) — overrides `default_agent` for one project.
- `codex_exec_cmd` (per-project, optional) — Codex's twin of `exec_cmd`, used
  whichever slot (primary or secondary) is running Codex. Defaults mirror the
  `exec_cmd` defaults exactly, substituting `codex` for `claude` and Codex's
  `resume <id>` subcommand for `{resume_args}` instead of Claude's `--resume <id>`
  flag.

**Custom `codex_exec_cmd` contract.** If you write your own instead of taking
the default, it must, like a custom `exec_cmd`: (1) `cd {workdir}` before
launching Codex — Codex's project-layer session cwd must be the worktree
root, and (2) export `TMUX_AGENTS_AGENT=1` — the marker the provisioned
Codex hooks require before writing any state for that pane. Missing either
one doesn't crash anything; it just leaves that pane's Codex session
untracked (no state letter beyond whatever `phase_hint` left behind, no
session-id capture for resume).

Starting the other agent: `Ctrl-Space O` (also bound to `O` in the focused
overview pane) is one smart action on the active window's agent(s):

- Neither started yet but the default is alive → splits the window 50/50
  and starts the other kind alongside it (idempotent Codex-hook provisioning
  runs first; a missing `codex`/`claude` binary shows a friendly error
  instead of a broken pane).
- Both already running → jumps focus between the two agent panes.
- The secondary was started before and then quit → **revives** it,
  resuming the same conversation (its session id is retained even after the
  pane closes) rather than starting fresh; run `/new` inside it for a clean
  slate instead.
- The default agent's pane is dead → a message pointing at `Ctrl-Space R`
  (restore) instead, since there's no live pane to split from.
- No agent window (e.g. the `ctrl` host-shell window) → a no-op message.

Once both agents are running, `Ctrl-Space z` zooms/unzooms whichever pane
has focus, same as any pane. Quitting an agent (exiting `claude`/`codex` in
its pane) closes that pane; `agent-kill` still kills the whole window
(both agents). `agent-restore` brings back both agents after a server
restart, each resuming its own conversation.

**Codex hook provisioning and the one-time `/hooks` approval.** Codex state
tracking (the `R`/`W`/`I`/`X`/`S` letters — no background/sleeping tracking
for Codex, see "State colors" below) works the same way Claude's does: a
small hook script writes phase transitions as Codex's lifecycle events fire.
Unlike Claude's per-worktree hooks, Codex's hooks are registered once at the
**user level** (`~/.codex/hooks.json`, both on the host and inside every
container's home directory) and reference a package-owned script kept
*outside* any workspace — `~/.config/tmux-agents/codex-hook.sh` on the host,
`<container home>/.codex/tmux-agents/codex-hook.sh` in a container — so a
sandboxed Codex session can't tamper with the script it's trusted to run.
`agent-new`, `agent-restore`, `agent-rebuild`, and `Ctrl-Space O` all
(re-)provision this idempotently, so run any one of them once and you're
set for every project. `agent-new`/`agent-restore`/`agent-rebuild` treat a
provisioning failure as non-fatal — they warn and continue starting the
project's default agent regardless; `Ctrl-Space O` treats it as a hard
stop for that keypress (a friendly message, no pane created) since
provisioning the *other* kind's hooks is the whole point of that action.

The first time tmux-agents writes `~/.codex/hooks.json`, Codex will prompt
for a **one-time `/hooks` approval** (Codex trust-gates every hook
definition by exact hash) — approve it once and every subsequent session
uses the hooks silently. Upgrading tmux-agents to a version that changes the
hook script or its registered commands re-triggers that same one-time
prompt, since the definition hash changed.

One visible consequence: the session that showed the approval prompt sits
at `starting` until you approve — its `SessionStart` fired *before* the
approval, so it was skipped and never replayed. The first hook event after
approving (your next prompt, a tool call) adopts that session's id and
tracking starts mid-session. In the unlikely case the pane's letter looks
stuck after that, `/new` inside Codex re-pins cleanly.

**Codex inside a container — prerequisites.** tmux-agents does not install
or authenticate Codex for you: the `codex` binary and a working
`~/.codex` auth directory must already exist wherever the agent runs — on
the host for host-only projects, baked into the devcontainer image (or
mounted) for container projects. A missing binary shows the standard
error pane (`X`) with the failing command; `Ctrl-Space O` additionally
pre-flights the binary's presence before splitting a pane (skipped when
`codex_exec_cmd`/`exec_cmd` is a custom command, since a fixed executable
name check proves nothing about an arbitrary one).

**Override the base branch for new worktrees:**

`base_branch` (optional, string) — override the auto-detected remote default
branch. New worktrees are created from `origin/<base>` after a fresh fetch.
When unset (the recommended default), the remote default is detected from
`origin/HEAD`. Use this only when the integration branch differs from the
remote default (e.g. `develop`).

```toml
[myproject]
repo        = "/Users/you/dev/myproject"
devcontainer = true
base_branch = "develop"
```

Offline: if fetch fails but a cached `origin/<base>` ref exists, `agent-new`
warns on stderr and uses the cached ref. If no remote ref is reachable at
all, it falls back to creating the worktree from HEAD (with a warning).

## Theming

State colors (R/W/B/Z/I/X/S) default to a dark palette tuned for truecolor
terminals. To switch to a light palette or override individual states,
copy the installed example and edit:

```bash
cp ~/.config/tmux-agents/theme.toml.example ~/.config/tmux-agents/theme.toml
$EDITOR ~/.config/tmux-agents/theme.toml
```

Keys:

- `mode` — `"dark"` (default) or `"light"`.
- `[colors]` table — per-state hex overrides. Each key is optional and
  falls back to the mode default. Values must be `#rrggbb`.

Reload an existing session after editing:

```bash
tmux -L agents source-file ~/.config/tmux-agents/agents.conf
```

(State colors will pick up the new palette on the next status-line tick,
within 2 seconds.)

## Claude fullscreen + state hooks

`agent-new` automatically provisions `<worktree>/.claude/settings.local.json`
on first use, which enables Claude's fullscreen TUI and registers the
lifecycle hooks that feed the overview. Nothing to do per-project.

If you want to run Claude manually in a worktree (without `agent-new`) and
keep state tracking working, re-run `agent-new <project> [branch]` once
against that worktree; the provisioning step is idempotent.

**Migration from older tmux-agents versions:** if your `projects.toml`
devcontainer `exec_cmd` does not already pass `-e TMUX_PANE`, add it:

```
exec_cmd = "docker exec -it -e TMUX_PANE {container} bash -lc 'cd {workdir} && claude'"
```

Without this flag the hooks no-op inside the container (state stays `I`
for every agent). Host-only `exec_cmd`s need no change.

If you have a custom `exec_cmd` and want SSH agent forwarding, also
add `export SSH_AUTH_SOCK=/tmp/tmux-agents-ssh.sock && ` before
`cd {workdir} && exec claude` (and keep `forward_ssh_agent = true`,
the default). See "SSH agent forwarding" below.

Data written by the hooks lives under `<worktree>/.local/.tmux-agents/`.
Add `.local/` to your project's `.gitignore` the first time you see it
(one-off per project).

**Background & scheduled work (`background·N` / `sleeping·N`).** Each
backgrounded subagent / background shell and each scheduled wakeup or cron
drops a self-expiring marker file under
`<worktree>/.local/.tmux-agents/pending-<pane>/`; the host tick counts the
live ones. Because every marker carries an expiry, the counts self-heal —
they can't drift the way the old cron counter did. Removal is precise:
wakeups/crons expire at their real fire time, and background subagents and
shells both clear the moment their completion notification arrives. A
per-kind timeout (≈30 min) is only a backstop for the rare case where the
session ends before a background task reports completion.

## SSH agent forwarding

Container projects forward the host's SSH agent into every agent pane
by default. `git push`, `ssh -T git@github.com`, and SSH-key commit
signing inside the container all use your host's running SSH agent —
no host port exposed, no iptables holes.

How it works: `agent-new` spawns a detached Python pump on the host
plus a `docker exec -i python3` relay inside the container. The relay
binds a UDS at `/tmp/tmux-agents-ssh.sock` (mode 0600) owned by the
**agent user** — the same user as `-u {user}` in your `exec_cmd`
template (default `vscode`, override with `user` in `projects.toml`).
The pump runs the relay's `docker exec` with `-u {user}` so the bind
owner matches the agent. The bootstrap exports `SSH_AUTH_SOCK` to that
path. Multiple agents in the same container share one relay
(bind-or-exit dedup).

**Requirements:**
- `python3` on PATH inside the container.
- `$SSH_AUTH_SOCK` set on the host (default for macOS / launchd).

**Opt out per project:**

```toml
[isolated-thing]
devcontainer      = true
forward_ssh_agent = false
```

**Custom `exec_cmd`** (you manage your own template):

```toml
[api]
container = "api-devcontainer"
exec_cmd  = """docker exec -it -e TERM -e COLORTERM -e TMUX_PANE -u vscode {container} \
  bash -lc 'export SSH_AUTH_SOCK=/tmp/tmux-agents-ssh.sock && cd {workdir} && exec claude'"""
```

The `forward_ssh_agent` flag still controls whether the pump spawns
when you use a custom `exec_cmd` — set it to `false` to skip the pump
even if your template references the env var.

**Troubleshooting:**

- Unified log: `$TMUX_AGENTS_STATE_DIR/tmux-agents.log` (default `/tmp/tmux-agents/tmux-agents.log`); pump entries are tagged with the container name in the `[component]` field.
- Live pump processes: `ps -ef | grep tmux-agents-ssh-pump`.
- Inside the container: `ls -la /tmp/tmux-agents-ssh.sock` to confirm the relay bound.
- Container restart breaks the channel; `Ctrl-Space b` (rebuild) recreates the container and brings the agents back, or recreate a single agent with `Ctrl-Space k` then `Ctrl-Space a`.

## Daily use

```
agents
```

`agents` is a wrapper that runs `tmux -L agents -f ~/.config/tmux-agents/agents.conf new-session -A -s agents`.
It creates the session on first run and attaches on subsequent runs. To detach from inside, press `Ctrl-Space d`.

Cheat sheet:

| Keys                | Action                                           |
|---------------------|--------------------------------------------------|
| `Ctrl-Space a`      | New agent — prompts for `<project> [branch]`     |
| `Ctrl-Space k`      | Kill agent — picker for window + worktree removal |
| `Ctrl-Space r`      | Restore dead agent panes (revives every window whose Claude pane died) |
| `Ctrl-Space b`      | Rebuild a project's container and resume its agents (warns first if any are busy) |
| `Ctrl-Space e`      | Rename current window's branch part              |
| `Ctrl-Space L`      | Toggle layout: split (vertical) ↔ compact (horizontal) |
| `Ctrl-Space v`      | Open the current agent's worktree in VS Code     |
| `Ctrl-Space t`      | Open a shell in the current agent's worktree (popup) |
| `Ctrl-Space o`      | Start/switch to the window's other agent (Claude↔Codex); see "Running Codex alongside Claude" |
| `Ctrl-Space <num>`  | Jump to window by number (shown in overview)     |
| `Ctrl-Space w`      | Arrow-key window picker (`choose-tree`)          |
| `Ctrl-Space z`      | Zoom/unzoom the focused pane                     |
| `Ctrl-Space 0`      | Jump to the `ctrl` (host shell) window           |
| `Ctrl-Space d`      | Detach (session keeps running)                   |
| `Ctrl-Space <old uppercase>` | The pre-lowercase keys (`N K B R E V T`) still work as aliases |

### Restore across server restarts

`agents` keeps a per-window snapshot under `~/.config/tmux-agents/windows/`.
After a tmux server restart (laptop reboot, manual kill), running `agents`
detects the snapshot and prompts:

```
Restore 4 previous agents? [Y/n] (5s, default Y):
```

Hit Enter (or wait the timer out) to restore; type `n` to start fresh.
On restore, all previous agent windows reappear in the overview in a
greyed-out `S` (starting) state and fill in with their actual Claude
sessions as their devcontainers come up — typically a few seconds for
warm containers, ~30s–2min for cold ones. New windows pop in without
stealing focus, so you can interact with already-restored agents while
the slow ones are still booting.

`claude --resume <session_id>` is invoked automatically using the
session id captured at agent start time, so each conversation continues
exactly where it left off.

When an agent's Claude pane dies, the overview shows `X` for that window
and its footer turns into a recovery hint (`⚠ N agent(s) down — press
Ctrl-Space r to restore`). `Ctrl-Space r` (or `r` in the overview) re-runs
`agent-restore` to revive every dead pane; it's idempotent, so live
windows are left alone.

If something fails (e.g., Docker not running), the broken window
displays a clear error message and the overview shows `X` for it; fix
the underlying issue and re-run `agent-restore` (`Ctrl-Space r`), or kill
the window with Ctrl-Space k.

## Customizing keys

`agents.conf` sources `~/.config/tmux-agents/local.conf` as its last step
(silently skipped if absent). tmux config is last-write-wins, so anything
you put there overrides the shipped defaults, and it is never touched by
`install.sh` or `make conf-sync`. All command keys live in the prefix
table, so they keep working under whatever prefix you choose.

Example — move the prefix to Alt-Space (useful if Ctrl sits on different
physical keys on your machines, e.g. an OS-level Ctrl/Cmd swap on macOS):

```tmux
# ~/.config/tmux-agents/local.conf
unbind C-Space
set -g prefix M-Space
bind M-Space send-prefix
```

Alt-Space needs two one-time terminal settings:

- **macOS Terminal.app**: Settings → Profiles → Keyboard → check
  "Use Option as Meta key".
- **Windows Terminal**: unbind `alt+space` from the window system menu —
  in `settings.json`: `{ "command": "unbound", "keys": "alt+space" }`.

The overview footer and recovery hints read the prefix from the server,
so they display your override, not the default.

## Copy / paste

Mouse-drag inside a pane copies to the system clipboard on release.
Cross-platform: tries `pbcopy` (macOS), `clip.exe` (WSL), `wl-copy`
(Wayland), then `xclip`/`xsel` (X11) — see `~/.config/tmux-agents/clipboard-copy`.
Keyboard: enter copy-mode with `Ctrl-Space [`, `v` starts a
selection, `C-v` toggles rectangle, `y` copies and exits.

To select text that spans two panes (tmux's copy-mode is pane-bound),
hold **Option** while dragging (iTerm2, Ghostty, Alacritty) or **Fn**
(macOS Terminal.app). That bypasses tmux's mouse capture and gives you
native terminal selection, then ⌘C to copy.

## State colors

In the bottom-pane overview:

- green = running (Claude is working)
- yellow = waiting (permission prompt — needs you)
- cyan = background (work running now while otherwise idle — background subagent or background shell; shown as `background·N`)
- purple = sleeping (nothing running now, but it will resume on its own — a self-paced `/loop` wakeup or a cron; shown as `sleeping·N`)
- blue = idle (waiting for your input)
- red = errored
- grey = starting (window pre-created by `agent-restore`, container not yet ready)

A window running both agents shows one letter per agent, separated by a dim
`|` (e.g. `R|I`) — the window's tab color follows whichever letter has the
highest priority. Codex agents only ever show `R`/`W`/`I`/`X`/`S`: Codex has
no background-subagent/scheduled-wakeup tracking, so `background`/`sleeping`
never apply to a Codex slot. A denied Codex permission prompt has no
dedicated "denied" event, so that pane can stay `waiting` (yellow) until its
next tracked event (worst case, until it goes idle) even after you've
declined the prompt inside Codex itself — expected today, not a bug.

## Uninstall

```bash
./uninstall.sh
```

Keeps `~/.config/tmux-agents/projects.toml`.

## Layout

- **Split (default, vertical screens):** every window is split top = Claude, bottom = overview. The overview pane auto-sizes to its content (all rows + footer, at most a quarter of the window) and re-fits itself when the terminal is resized.
- **Compact (horizontal screens):** no splits; overview collapses into the tmux status line.

Toggle with `Ctrl-Space L`. The choice persists in `/tmp/tmux-agents/layout`.

### Overview pane interactions

In the split layout, the bottom overview pane is interactive:

- **Click an agent row** (from any pane) — switch to that agent's window.
- **Click a repo header** — fold or unfold the repo group; state persists
  across `agents` restarts (saved alongside other state under
  `$TMUX_AGENTS_STATE_DIR`, default `/tmp/tmux-agents/`).
- **Click blank space** to the right of text or below the rows — focus
  the overview pane.
- With the overview pane focused: `↑` / `↓` walk visible rows, `↵`
  activates (window-switch on agent row, fold-toggle on header), and
  `a` / `k` / `r` / `e` mirror the prefix bindings (new / kill / restore /
  rename) — `a`, `k`, `e` act on the cursor's agent; `r` restores all dead
  panes. The old uppercase keys still work.

A dim hint at the bottom-right of the pane shows the available keys, spelled
as the full prefix chord (your `local.conf` prefix override is reflected
here) so the prefix is discoverable. When an agent pane is dead this is
replaced by a recovery alert (`⚠ N agent(s) down — press Ctrl-Space r to
restore`) in the errored color.

## Developer notes

- Python 3.11+ required (stdlib `tomllib`).
- `make check` runs everything CI gates on (`ruff check`, `ruff format
  --check`, `pytest`); individual targets: `make test`, `make lint`,
  `make format`. The `Makefile` also has the dev-loop targets
  (`reinstall`, `conf-sync`) — see its comments for caveats.
- Layout: `src/tmux_agents/` has one module per responsibility
  (`config`, `state`, `tmux`, `worktree`, `container`, `paths`, `overview`) plus
  thin command orchestrators under `commands/`. `docs/ARCHITECTURE.md`
  has the full map.
