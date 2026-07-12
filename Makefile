# Developer entry points. CI runs the same checks as `make check`
# (.github/workflows/test.yml: ruff check, ruff format --check, pytest).

.PHONY: check test lint format reinstall conf-sync

check: lint test  ## Everything CI gates on — run before pushing

test:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .

# CAUTION: the two targets below mutate GLOBAL state (the single installed
# uv tool / the single live config), not just this checkout. With several
# agents working in different worktrees at once, whoever ran them last wins —
# only run them when this worktree's code is what should be live.

# The install is a uv tool — editing source does NOT update the installed
# executables. --no-cache is load-bearing: --reinstall alone can ship a
# stale wheel from uv's build cache (see CLAUDE.md "Dev loop").
reinstall:
	uv tool install --reinstall --no-cache .

# Push this checkout's agents.conf to the live config and reload a running server.
conf-sync:
	cp agents.conf $(HOME)/.config/tmux-agents/agents.conf
	tmux -L agents source-file $(HOME)/.config/tmux-agents/agents.conf 2>/dev/null || true
