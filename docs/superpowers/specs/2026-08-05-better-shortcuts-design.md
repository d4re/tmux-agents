# Better shortcuts — design

Date: 2026-08-05
Branch: `feat/better-shortcuts`

## Problem

Two ergonomic complaints about the current keybindings:

1. **The prefix lands on different physical keys across machines.** The user
   runs tmux-agents on macOS (Terminal.app) and Windows/WSL (Windows
   Terminal) with Ctrl↔Cmd swapped at the OS level on the Mac. Any
   Ctrl-based prefix (`Ctrl-Space` today) is therefore a different physical
   gesture on each machine. No universally-better default exists: the
   Win/Cmd key never reaches terminals, AltGr and the Menu key are not
   forwardable, Alt combos need one-time terminal settings on both OSes,
   and F-keys/backtick have their own costs. Conclusion: keep the default,
   make the prefix (and anything else) **user-overridable**.
2. **Every command binding requires Shift** (`N K B R E L V T`). Lowercase
   is one less key per invocation.

## Decisions

### 1. Prefix stays `Ctrl-Space`; add a user-override hook

`agents.conf` gains as its **last line**:

```tmux
source-file -q ~/.config/tmux-agents/local.conf
```

- `-q` silently skips a missing file — zero cost for users without one.
- tmux config is last-write-wins, so `local.conf` can override the prefix
  (or any other option/binding). Command bindings live in the prefix table,
  not tied to a specific prefix key, so they keep working under any prefix.
- `local.conf` is never written by `install.sh` or `make conf-sync`, so
  customizations survive updates.
- README documents the file with a worked example: switching to `Alt-Space`
  (`unbind C-Space` / `set -g prefix M-Space` / `bind M-Space send-prefix`),
  including the two one-time terminal settings that combo needs
  (Terminal.app "Use Option as Meta"; Windows Terminal: unbind `alt+space`
  from `openSystemMenu`).

### 2. Lowercase command bindings, uppercase kept as aliases

| Action | Old | New | Rationale |
|---|---|---|---|
| new agent | `N` | `a` | `n` stays default next-window (user navigates with it when window numbers overflow); `c` stays default new-window; "a for agent" |
| kill agent | `K` | `k` | free key |
| rebuild | `B` | `b` | free key |
| restore | `R` | `r` | overrides default refresh-client (throwaway) |
| rename | `E` | `e` | free key |
| vscode | `V` | `v` | free key |
| terminal popup | `T` | `t` | overrides default clock-mode (throwaway) |
| layout toggle | `L` | `L` (unchanged) | rarely used; lowercase `l` stays default last-window |

- Old uppercase keys `N K B R E V T` remain bound to the same commands as
  silent aliases (graceful muscle-memory migration; may be removed later).
- Untouched tmux defaults: `n`/`p` (next/prev window), `l` (last-window),
  `c` (new window), `0` `w` `s` `z` `d` `[` and everything else.

### 3. Overview TUI mirrors the new keys

`overview.py::handle_key` currently accepts `N K R E`. It will accept the
new lowercase keys `a k r e` **and** keep `N K R E` as aliases (same
graceful-migration logic as the conf).

### 4. Prefix label becomes dynamic (no more hardcoded "Ctrl-Space")

With the prefix user-overridable, hardcoded "Ctrl-Space" hint strings would
lie. A new helper `tmux.py::prefix_label()`:

- runs `tmux show-options -gv prefix` (inside the agents server),
- humanizes the result (`C-Space` → `Ctrl-Space`, `M-Space` → `Alt-Space`,
  other keys passed through, e.g. `F12`),
- falls back to `"Ctrl-Space"` on any error (e.g. not inside tmux).

Consumers (resolve once per process start, not per frame/tick):

- `overview.py` footer strings (`_FOOTER_FULL`, `_FOOTER_SHORT`) and the
  restore alert — these become functions/lazy values instead of
  import-time constants, and their key letters change to the new lowercase
  set (`a new  k kill  r restore  e rename`).
- Recovery-hint heredocs in `commands/new.py` and `commands/restore.py`
  ("remove this window with Ctrl-Space K" → dynamic prefix + lowercase `k`).

### 5. Documentation updates

- **README**: keybinding table and all prose mentions switch to lowercase;
  new "Customizing keys" section documenting `local.conf` with the
  Alt-Space worked example.
- **docs/ARCHITECTURE.md**: the `Ctrl-Space <letter>` mentions in the CLI
  table and overview/restore sections updated; `local.conf` added where the
  config flow is described.
- **BACKLOG.md**: new item — investigate layout breaking on terminal
  resize (surfaced during this design; out of scope here).

## Non-goals

- Changing the default prefix. `Ctrl-Space` remains correct for users
  without a modifier swap.
- OS detection / per-OS conditional bindings — the override file makes
  this unnecessary.
- Fixing the layout-breaks-on-resize behavior (BACKLOG item).
- Removing the uppercase aliases (revisit later once muscle memory has
  migrated).

## Testing

- `tests/test_overview_tui.py`: existing footer/alert assertions updated to
  the lowercase letters; new tests for `prefix_label()` humanization and
  fallback (monkeypatch the tmux call, per existing test conventions);
  handle_key accepts both cases.
- Conf-level behavior (`local.conf` sourcing, new binds) verified on a
  scratch tmux server per the `verify` skill — never the live server.
- Gate: `make check` (ruff + format + pytest).
