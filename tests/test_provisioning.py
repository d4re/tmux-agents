import json
from importlib import resources
from importlib.metadata import version as _pkg_version
from tmux_agents import provisioning

TEMPLATE = resources.files("tmux_agents.hooks") / "agents.json"
PKG_VERSION = _pkg_version("tmux-agents")

def test_provision_creates_file_when_missing(tmp_path):
    target = tmp_path / ".claude" / "settings.local.json"
    changed = provisioning.provision_settings(tmp_path, template_path=TEMPLATE)
    assert changed is True
    t = json.loads(target.read_text())
    tpl = json.loads(TEMPLATE.read_text())
    assert t["_tmux_agents_version"] == PKG_VERSION
    assert t["tui"] == tpl["tui"]
    assert t["hooks"] == tpl["hooks"]

def test_provision_skip_when_up_to_date(tmp_path):
    provisioning.provision_settings(tmp_path, template_path=TEMPLATE)
    changed = provisioning.provision_settings(tmp_path, template_path=TEMPLATE)
    assert changed is False

def test_provision_preserves_user_keys(tmp_path):
    target = tmp_path / ".claude" / "settings.local.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "env": {"MY_VAR": "1"},
        "permissions": {"allow": ["Bash(ls *)"]},
    }))
    provisioning.provision_settings(tmp_path, template_path=TEMPLATE)
    got = json.loads(target.read_text())
    assert got["env"] == {"MY_VAR": "1"}
    assert got["permissions"] == {"allow": ["Bash(ls *)"]}
    assert got["tui"] == "fullscreen"
    assert "SessionStart" in got["hooks"]

def test_provision_merges_existing_hooks(tmp_path):
    # User already had a hook of their own; we must not clobber it.
    target = tmp_path / ".claude" / "settings.local.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": "echo user-hook"}]}
            ]
        }
    }))
    provisioning.provision_settings(tmp_path, template_path=TEMPLATE)
    got = json.loads(target.read_text())
    stop_cmds = [h["command"] for g in got["hooks"]["Stop"] for h in g["hooks"]]
    assert "echo user-hook" in stop_cmds
    # Our state-writer is also present:
    assert any("write-state.sh" in c for c in stop_cmds)

def test_provision_upgrades_stale_version(tmp_path):
    target = tmp_path / ".claude" / "settings.local.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "_tmux_agents_version": "0.0.1",
        "tui": "inline",
        "hooks": {"SessionStart": [{"hooks": [{"type": "command",
                                                "command": "echo stale"}]}]}
    }))
    changed = provisioning.provision_settings(tmp_path, template_path=TEMPLATE)
    assert changed is True
    got = json.loads(target.read_text())
    assert got["_tmux_agents_version"] == PKG_VERSION
    assert got["tui"] == "fullscreen"
    sstart = [h["command"] for g in got["hooks"]["SessionStart"] for h in g["hooks"]]
    assert any("write-state.sh" in c and "init" in c for c in sstart)
    assert "echo stale" not in " ".join(sstart)  # our keys replaced stale ones

def test_provision_writes_executable_script(tmp_path):
    provisioning.provision_settings(tmp_path, template_path=TEMPLATE)
    script = tmp_path / ".local" / ".tmux-agents" / "write-state.sh"
    assert script.exists()
    assert script.stat().st_mode & 0o111  # executable
    # Re-running provisioning rewrites the script even if settings is up-to-date.
    script.write_text("# clobbered")
    provisioning.provision_settings(tmp_path, template_path=TEMPLATE)
    assert script.read_text().startswith("#!/usr/bin/env sh")
