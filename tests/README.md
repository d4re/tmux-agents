# Tests

`uv run pytest -q`. Suite is fast (under a second) — run before committing.

## Conventions

- Unit tests freely monkey-patch the `tmux_agents.tmux` module rather than
  driving a real tmux server. Shelling out isn't the thing under test.
- Filesystem-touching tests use the `tmp_state_dir` / `tmp_config_dir`
  fixtures from `conftest.py`, which set `TMUX_AGENTS_STATE_DIR` /
  `TMUX_AGENTS_CONFIG_DIR` so `paths.py` redirects automatically. New
  code that touches disk should go through `paths.py` for the same
  reason.
- The `_reset_theme_cache` autouse fixture clears the theme palette
  cache between tests; tests that load custom theme files don't need to
  do this themselves.

## Shared fixtures (`conftest.py`)

- `agent_new_env` — default-stubs the tmux + container plumbing
  exercised by `agent-new` and returns a `SimpleNamespace` with capture
  lists (`made`, `splits`, `selected`, `ensured`). Used by
  `test_new.py`. Tests can override any individual stub with a fresh
  `monkeypatch.setattr` — the last assignment wins.
- `kill_env` — writes a one-project (api host-only) `projects.toml`,
  stubs `tmux.list_windows` to return a single `api:feat-x` window, and
  captures `tmux.kill_window` calls. Returns `.repo` and `.killed`.
  Used by `test_kill.py`.
- `tmux_agents_caplog` — `caplog` variant that attaches to the
  `tmux_agents` logger directly, bypassing the `propagate=False` that
  `setup_logging()` sets. Use this when a test triggers an entry-point
  `main()` and also needs to read `caplog.records`.

## Fixtures (`tests/fixtures/`)

- `projects_example.toml` — minimal valid `projects.toml` (one
  container project, one host-only) used by `test_config.py` and
  anything that needs a parsed `Project`.

## Load-bearing contract guards

A few tests aren't checking application behavior — they're protecting
non-obvious source-level contracts. If you change one of these areas,
expect to update the corresponding test:

- **`test_ssh_relay.py::test_relay_imports_framing_as_sibling_without_package`**
  — the in-container relay is delivered as a plain file alongside
  `_ssh_framing.py` and run as `python3 <dir>/_ssh_relay_script.py`, so it must
  import framing as a sibling. This test writes both files to a tmp dir and
  imports the relay under `python -E -S` (no env, no site, no `tmux_agents`
  package) to force the `from _ssh_framing import …` fallback. Keep the relay's
  two-branch import (`from tmux_agents._ssh_framing` → `from _ssh_framing`)
  intact.
- **`test_hook_snippets.py`** — runs each shell command from
  `src/tmux_agents/hooks/agents.json` against a tmp `CLAUDE_PROJECT_DIR`
  and asserts the resulting `state-<pane>.json` and `crons-<pane>.count`
  files match expectations across the full lifecycle (SessionStart →
  Notification → PostToolUse / PostToolUseFailure / PermissionDenied →
  Stop / StopFailure → SessionEnd, plus CronCreate/Delete counting and
  the floor-at-zero rule). Catches quoting drift, missing handlers, and
  state-machine regressions in the hook bodies.
- **`test_agents_hooks_template.py`** — schema/structure check on the
  hooks JSON template (matchers, top-level keys, no extra/missing
  events).

## Smoke (`test_smoke.py`)

Spins up an ephemeral tmux session under the `-L agents` socket and
runs `agent-state` + `agent-overview` against real windows. Skipped if
`tmux` isn't on `$PATH`. Useful for catching regressions where the
unit tests' monkey-patches mask real-tmux behavior.
