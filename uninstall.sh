#!/usr/bin/env bash
set -euo pipefail

CONFIG_AGENTS="$HOME/.config/tmux-agents"

echo "==> tmux-agents uninstaller"

# 1. Kill the agents server (dedicated socket -L agents, independent of any
# other tmux you may run).
if tmux -L agents list-sessions 2>/dev/null | grep -q .; then
  echo "    killing agents tmux server (-L agents)"
  tmux -L agents kill-server
fi

# 2. uv tool (removes `agents`, `agent-new`, etc. from ~/.local/bin)
if command -v uv >/dev/null && uv tool list 2>/dev/null | grep -q '^tmux-agents '; then
  echo "    removing uv tool"
  uv tool uninstall tmux-agents
fi

# 3. Config (preserve projects.toml — user content)
if [[ -f "$CONFIG_AGENTS/agents.conf" ]]; then
  echo "    removing $CONFIG_AGENTS/agents.conf"
  rm "$CONFIG_AGENTS/agents.conf"
fi
# Legacy dirs from installs that predate the TPM/resurrect removal.
if [[ -d "$CONFIG_AGENTS/plugins" ]]; then
  echo "    removing $CONFIG_AGENTS/plugins/ (legacy)"
  rm -rf "$CONFIG_AGENTS/plugins"
fi
if [[ -d "$CONFIG_AGENTS/resurrect" ]]; then
  echo "    removing $CONFIG_AGENTS/resurrect/ (legacy)"
  rm -rf "$CONFIG_AGENTS/resurrect"
fi
if [[ -f "$CONFIG_AGENTS/projects.toml" ]]; then
  echo "    NOTE: $CONFIG_AGENTS/projects.toml left in place (remove manually if desired)"
fi

# 4. Runtime state
if [[ -d /tmp/tmux-agents ]]; then
  echo "    clearing /tmp/tmux-agents/"
  rm -rf /tmp/tmux-agents
fi

echo "==> uninstalled."
