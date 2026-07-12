#!/usr/bin/env bash
set -euo pipefail

# tmux-agents installer. Idempotent — safe to re-run.
#
# Everything is self-contained under ~/.config/tmux-agents/ and a dedicated
# tmux socket (-L agents). This installer does NOT touch ~/.config/tmux/ or
# any other tmux configuration you may already have.

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_AGENTS="$HOME/.config/tmux-agents"
PYTHON="${PYTHON:-python3}"

echo "==> tmux-agents installer"
echo "    repo:   $REPO_DIR"
echo "    target: $CONFIG_AGENTS"

# 1. Verify requirements
echo "==> checking requirements"
command -v tmux  >/dev/null || { echo "error: tmux not found (brew install tmux)"; exit 1; }
command -v git   >/dev/null || { echo "error: git not found"; exit 1; }
command -v "$PYTHON" >/dev/null || { echo "error: python3 not found"; exit 1; }

PY_VERSION=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then
  echo "error: python 3.11+ required (found $PY_VERSION)"
  exit 1
fi

if ! command -v uv >/dev/null; then
  echo "==> installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installer drops the binary in ~/.local/bin; pick it up for this run
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null || { echo "error: uv install failed — see https://docs.astral.sh/uv/"; exit 1; }
fi

# 1b. devcontainer CLI — only needed for projects.toml entries that use
# `devcontainer = true` / `container = ...` (up_cmd defaults to
# `devcontainer up ...`); host-only projects don't need it. Optional, so a
# missing/failed install only warns, it never aborts the installer.
if ! command -v devcontainer >/dev/null; then
  if command -v npm >/dev/null; then
    echo "==> installing @devcontainers/cli"
    npm install -g @devcontainers/cli || echo "⚠️  @devcontainers/cli install failed — devcontainer-based projects won't work until you install it manually."
  else
    echo ""
    echo "⚠️  'devcontainer' CLI not found and npm isn't available to install it."
    echo "   Only needed for devcontainer-based projects.toml entries."
    echo "   Install Node.js, then: npm install -g @devcontainers/cli"
  fi
fi

# 2. Install the Python package
# --reinstall (rather than just --force) guarantees a fresh build from the
# current source; --force alone can reuse a stale build cache.
echo "==> installing tmux-agents via uv"
uv tool install --reinstall --python "$PYTHON" "$REPO_DIR"

# 3. agents.conf (dedicated config for the `-L agents` socket)
echo "==> installing agents.conf"
mkdir -p "$CONFIG_AGENTS"
cp "$REPO_DIR/agents.conf" "$CONFIG_AGENTS/agents.conf"

# 3b. clipboard-copy — cross-platform clipboard sink agents.conf's copy
# bindings shell out to (picks pbcopy / clip.exe / wl-copy / xclip / xsel).
cp "$REPO_DIR/config/clipboard-copy" "$CONFIG_AGENTS/clipboard-copy"
chmod +x "$CONFIG_AGENTS/clipboard-copy"

# 4. Example projects.toml (never overwrite an edited one)
if [[ ! -f "$CONFIG_AGENTS/projects.toml" ]]; then
  cp "$REPO_DIR/config/projects.toml.example" "$CONFIG_AGENTS/projects.toml"
  echo "    created $CONFIG_AGENTS/projects.toml (sample, edit to add your projects)"
fi

# 4b. theme.toml.example (always refresh so users see new keys/comments;
#     never overwrites an actual theme.toml the user created).
cp "$REPO_DIR/config/theme.toml.example" "$CONFIG_AGENTS/theme.toml.example"

# 5. PATH check
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$HOME/.local/bin"; then
  echo ""
  echo "⚠️  ~/.local/bin is not on your PATH."
  echo "   Add this line to your ~/.zshrc (or shell rc):"
  echo "     export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# 6. Cheat sheet
cat <<'EOF'

==> installed.

Launch:
  agents                    start or attach to the agents tmux session
                            (uses a dedicated socket; won't touch other tmux setups)

Cheat sheet (inside the agents session):
  Ctrl-Space N              spawn a new agent (prompted)
  Ctrl-Space R              restore dead agent panes
  Ctrl-Space E              rename current window's branch part
  Ctrl-Space L              toggle split/compact layout
  Ctrl-Space <N>            jump to window N (shown in overview)
  Ctrl-Space z              zoom/unzoom the focused pane
  Ctrl-Space d              detach (session keeps running)

Next:
  1. Edit ~/.config/tmux-agents/projects.toml to define your projects.
  2. Run `agents`, then Ctrl-Space N. (Claude lifecycle hooks are
     provisioned into each worktree automatically by agent-new.)

EOF
