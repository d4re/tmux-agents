import json
import subprocess
from pathlib import Path

from tmux_agents import codex_hooks, container


SP = Path("/opt/tmux-agents/codex-hook.sh")


def test_owned_command_quotes_path():
    sp = Path("/weird path/codex-hook.sh")
    assert codex_hooks.owned_command(sp, "init") == (
        "sh '/weird path/codex-hook.sh' init"
    )


def test_is_owned_exact_form_only():
    ok = codex_hooks.owned_command(SP, "idle")
    assert codex_hooks.is_owned(ok, SP)
    # any old/renamed action word still owned (migration-safe)
    assert codex_hooks.is_owned(f"sh {SP} obsolete-action", SP)
    # wrappers / backups / containment are NOT owned
    assert not codex_hooks.is_owned(f"logger sh {SP} idle", SP)
    assert not codex_hooks.is_owned(f"sh {SP}.backup idle", SP)
    assert not codex_hooks.is_owned(f"sh {SP} idle && echo hi", SP)


def test_merge_preserves_foreign_entries_in_shared_group():
    existing = {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": codex_hooks.owned_command(SP, "idle"),
                    },
                    {"type": "command", "command": "my-own-thing"},
                ]
            }
        ]
    }
    out = codex_hooks.merge(existing, SP)
    stop_cmds = [h["command"] for g in out["Stop"] for h in g["hooks"]]
    assert "my-own-thing" in stop_cmds
    assert stop_cmds.count(codex_hooks.owned_command(SP, "idle")) == 1


def test_merge_removes_misfiled_and_duplicate_owned_entries():
    existing = {
        "PreCompact": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": codex_hooks.owned_command(SP, "idle"),
                    }
                ]
            }
        ],
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": codex_hooks.owned_command(SP, "idle"),
                    }
                ]
            },
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": codex_hooks.owned_command(SP, "idle"),
                    }
                ]
            },
        ],
    }
    out = codex_hooks.merge(existing, SP)
    assert "PreCompact" not in out
    assert len(out["Stop"]) == 1


def test_ensure_host_installs_then_noops(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUX_AGENTS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("TMUX_AGENTS_CODEX_HOME", str(tmp_path / "codex"))
    assert codex_hooks.ensure_host() is True
    script = tmp_path / "cfg" / "codex-hook.sh"
    assert script.exists() and script.stat().st_mode & 0o111
    hooks = json.loads((tmp_path / "codex" / "hooks.json").read_text())
    assert set(codex_hooks.HOOK_EVENTS) <= set(hooks["hooks"])
    assert codex_hooks.ensure_host() is False  # canonical → no rewrite


def test_ensure_host_heals_mutated_script_and_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUX_AGENTS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("TMUX_AGENTS_CODEX_HOME", str(tmp_path / "codex"))
    codex_hooks.ensure_host()
    (tmp_path / "cfg" / "codex-hook.sh").write_text("tampered")
    hj = tmp_path / "codex" / "hooks.json"
    data = json.loads(hj.read_text())
    del data["hooks"]["Stop"]
    hj.write_text(json.dumps(data))
    assert codex_hooks.ensure_host() is True
    assert "Stop" in json.loads(hj.read_text())["hooks"]
    assert "tampered" not in (tmp_path / "cfg" / "codex-hook.sh").read_text()


def test_ensure_host_preserves_foreign_hooks_and_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUX_AGENTS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("TMUX_AGENTS_CODEX_HOME", str(tmp_path / "codex"))
    home = tmp_path / "codex"
    home.mkdir(parents=True)
    (home / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [{"hooks": [{"type": "command", "command": "user-thing"}]}]
                },
                "unrelated": True,
            }
        )
    )
    codex_hooks.ensure_host()
    data = json.loads((home / "hooks.json").read_text())
    assert data["unrelated"] is True
    cmds = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
    assert "user-thing" in cmds


def test_ensure_container_writes_script_and_hooks(monkeypatch):
    fs: dict[str, str] = {}

    def fake_exec(name, user, script, *, stdin=None):
        assert name == "cont" and user == "dev"
        # emulate: home resolution, cat-read, mkdir+mktemp+mv write
        if "printf %s" in script and "$HOME" in script:
            return "/home/dev"
        if script.startswith("cat "):
            path = script.split()[-1]
            if path in fs:
                return fs[path]
            raise subprocess.CalledProcessError(1, script)
        if "mv " in script and stdin is not None:
            target = script.rsplit(" ", 1)[-1].strip("'\"")
            fs[target] = stdin
            return ""
        raise AssertionError(f"unexpected script: {script}")

    monkeypatch.setattr(container, "exec_capture", fake_exec)
    wrote = codex_hooks.ensure_container("cont", "dev")
    assert wrote is True
    assert "/home/dev/.codex/tmux-agents/codex-hook.sh" in fs
    assert (
        fs["/home/dev/.codex/tmux-agents/codex-hook.sh"]
        == codex_hooks.packaged_script()
    )
    hooks = json.loads(fs["/home/dev/.codex/hooks.json"])
    stop = hooks["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert stop == "sh /home/dev/.codex/tmux-agents/codex-hook.sh idle"


def test_ensure_container_noops_when_already_canonical(monkeypatch):
    script_path = Path("/home/dev/.codex/tmux-agents/codex-hook.sh")
    fs: dict[str, str] = {
        str(script_path): codex_hooks.packaged_script(),
        "/home/dev/.codex/hooks.json": json.dumps(
            {"hooks": codex_hooks.canonical_hooks(script_path)}
        ),
    }
    writes = 0

    def fake_exec(name, user, script, *, stdin=None):
        nonlocal writes
        if "printf %s" in script and "$HOME" in script:
            return "/home/dev"
        if script.startswith("cat "):
            path = script.split()[-1]
            if path in fs:
                return fs[path]
            raise subprocess.CalledProcessError(1, script)
        if "mv " in script and stdin is not None:
            writes += 1
            target = script.rsplit(" ", 1)[-1].strip("'\"")
            fs[target] = stdin
            return ""
        raise AssertionError(f"unexpected script: {script}")

    monkeypatch.setattr(container, "exec_capture", fake_exec)
    wrote = codex_hooks.ensure_container("cont", "dev")
    assert wrote is False
    assert writes == 0


def test_ensure_host_heals_array_hooks_json(tmp_path, monkeypatch):
    """Hooks.json containing [] instead of object heals on next ensure_host."""
    monkeypatch.setenv("TMUX_AGENTS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("TMUX_AGENTS_CODEX_HOME", str(tmp_path / "codex"))
    home = tmp_path / "codex"
    home.mkdir(parents=True)
    (home / "hooks.json").write_text("[]")
    assert codex_hooks.ensure_host() is True
    data = json.loads((home / "hooks.json").read_text())
    assert isinstance(data, dict)
    assert set(codex_hooks.HOOK_EVENTS) <= set(data["hooks"])


def test_ensure_host_heals_empty_hooks_dict(tmp_path, monkeypatch):
    """Hooks.json with empty hooks dict heals by restoring canonical entries."""
    monkeypatch.setenv("TMUX_AGENTS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("TMUX_AGENTS_CODEX_HOME", str(tmp_path / "codex"))
    home = tmp_path / "codex"
    home.mkdir(parents=True)
    (home / "hooks.json").write_text(json.dumps({"hooks": []}))
    assert codex_hooks.ensure_host() is True
    data = json.loads((home / "hooks.json").read_text())
    assert set(codex_hooks.HOOK_EVENTS) <= set(data["hooks"])


def test_ensure_container_heals_array_hooks_json(monkeypatch):
    """Container hooks.json containing [] instead of object heals on next ensure_container."""
    fs: dict[str, str] = {"/home/dev/.codex/hooks.json": "[]"}

    def fake_exec(name, user, script, *, stdin=None):
        if "printf %s" in script and "$HOME" in script:
            return "/home/dev"
        if script.startswith("cat "):
            path = script.split()[-1]
            if path in fs:
                return fs[path]
            raise subprocess.CalledProcessError(1, script)
        if "mv " in script and stdin is not None:
            target = script.rsplit(" ", 1)[-1].strip("'\"")
            fs[target] = stdin
            return ""
        raise AssertionError(f"unexpected script: {script}")

    monkeypatch.setattr(container, "exec_capture", fake_exec)
    wrote = codex_hooks.ensure_container("cont", "dev")
    assert wrote is True
    data = json.loads(fs["/home/dev/.codex/hooks.json"])
    assert isinstance(data, dict)
    assert set(codex_hooks.HOOK_EVENTS) <= set(data["hooks"])


def test_merge_tolerate_string_groups():
    """merge with non-list groups value treats it as empty and appends canonical."""
    existing = {"Stop": "garbage"}
    out = codex_hooks.merge(existing, SP)
    # canonical Stop appended, no crash
    assert "Stop" in out
    assert codex_hooks.owned_command(SP, "idle") in [
        h["command"]
        for g in out["Stop"]
        if isinstance(g, dict)
        for h in g.get("hooks", [])
        if isinstance(h, dict)
    ]


def test_merge_tolerate_bad_group_dict():
    """merge with non-dict groups in list preserves them as foreign data."""
    existing = {"Stop": ["bad-group"]}
    out = codex_hooks.merge(existing, SP)
    # canonical Stop appended, "bad-group" preserved
    assert "Stop" in out
    assert "bad-group" in out["Stop"]
    assert codex_hooks.owned_command(SP, "idle") in [
        h["command"]
        for g in out["Stop"]
        if isinstance(g, dict)
        for h in g.get("hooks", [])
        if isinstance(h, dict)
    ]


def test_merge_tolerate_hooks_not_list():
    """merge with group that has hooks=non-list treats it as empty."""
    existing = {"Stop": [{"hooks": "nope"}]}
    out = codex_hooks.merge(existing, SP)
    # canonical Stop appended, empty group dropped, no crash
    assert "Stop" in out
    assert codex_hooks.owned_command(SP, "idle") in [
        h["command"] for g in out["Stop"] for h in g.get("hooks", [])
    ]


def test_merge_tolerate_hook_entry_not_dict():
    """merge with non-dict hook entry preserves it as foreign data."""
    existing = {
        "Stop": [
            {
                "hooks": [
                    "bad-entry",
                    {"type": "command", "command": "user-thing"},
                ]
            }
        ]
    }
    out = codex_hooks.merge(existing, SP)
    stop_entries = [
        h for g in out["Stop"] if isinstance(g, dict) for h in g.get("hooks", [])
    ]
    assert "bad-entry" in stop_entries  # preserved
    assert any(
        h.get("command") == "user-thing" for h in stop_entries if isinstance(h, dict)
    )


def test_owned_subset_tolerate_bad_groups():
    """_owned_subset ignores non-dict groups."""
    hooks = {
        "Stop": [
            "bad-group",
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": codex_hooks.owned_command(SP, "idle"),
                    }
                ]
            },
        ]
    }
    subset = codex_hooks._owned_subset(hooks, SP)
    assert (codex_hooks.owned_command(SP, "idle"), "command", None) in subset.get(
        "Stop", []
    )


def test_owned_subset_tolerate_hooks_not_list():
    """_owned_subset ignores groups with hooks that aren't a list."""
    hooks = {"Stop": [{"hooks": "nope"}]}
    subset = codex_hooks._owned_subset(hooks, SP)
    assert subset == {}


def test_owned_subset_tolerate_hook_entry_not_dict():
    """_owned_subset ignores non-dict hook entries."""
    hooks = {
        "Stop": [
            {
                "hooks": [
                    "bad-entry",
                    {
                        "type": "command",
                        "command": codex_hooks.owned_command(SP, "idle"),
                    },
                ]
            }
        ]
    }
    subset = codex_hooks._owned_subset(hooks, SP)
    assert (codex_hooks.owned_command(SP, "idle"), "command", None) in subset.get(
        "Stop", []
    )


def test_owned_subset_captures_type_and_matcher():
    """_owned_subset's descriptor includes the entry's type and the
    containing group's matcher, not just the command."""
    hooks = {
        "Stop": [
            {
                "matcher": "some-matcher",
                "hooks": [
                    {
                        "type": "shell",
                        "command": codex_hooks.owned_command(SP, "idle"),
                    }
                ],
            }
        ]
    }
    subset = codex_hooks._owned_subset(hooks, SP)
    assert subset["Stop"] == [
        (codex_hooks.owned_command(SP, "idle"), "shell", "some-matcher")
    ]


def test_ensure_host_reprovisions_when_owned_entry_type_mutated(tmp_path, monkeypatch):
    """A hand-edit (or a foreign tool) that mutates an owned entry's `type`
    to something other than "command" must be detected and healed, even
    though the command string itself is still exactly ours."""
    monkeypatch.setenv("TMUX_AGENTS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("TMUX_AGENTS_CODEX_HOME", str(tmp_path / "codex"))
    codex_hooks.ensure_host()
    hj = tmp_path / "codex" / "hooks.json"
    data = json.loads(hj.read_text())
    data["hooks"]["Stop"][0]["hooks"][0]["type"] = "shell"
    hj.write_text(json.dumps(data))

    assert codex_hooks.ensure_host() is True

    healed = json.loads(hj.read_text())
    script = tmp_path / "cfg" / "codex-hook.sh"
    assert codex_hooks._owned_subset(
        healed["hooks"], script
    ) == codex_hooks._canonical_subset(script)
    assert codex_hooks.ensure_host() is False  # now canonical -> no rewrite


def test_ensure_host_reprovisions_when_matcher_added_to_owned_group(
    tmp_path, monkeypatch
):
    """A matcher added to our owned group (e.g. by hand-editing) must be
    detected and healed, even though event/command/type are all still
    exactly ours."""
    monkeypatch.setenv("TMUX_AGENTS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("TMUX_AGENTS_CODEX_HOME", str(tmp_path / "codex"))
    codex_hooks.ensure_host()
    hj = tmp_path / "codex" / "hooks.json"
    data = json.loads(hj.read_text())
    data["hooks"]["Stop"][0]["matcher"] = "unexpected"
    hj.write_text(json.dumps(data))

    assert codex_hooks.ensure_host() is True

    healed = json.loads(hj.read_text())
    script = tmp_path / "cfg" / "codex-hook.sh"
    assert codex_hooks._owned_subset(
        healed["hooks"], script
    ) == codex_hooks._canonical_subset(script)
    assert codex_hooks.ensure_host() is False  # now canonical -> no rewrite


def test_ensure_host_heals_with_malformed_groups(tmp_path, monkeypatch):
    """ensure_host heals hooks.json with malformed group-level shapes."""
    monkeypatch.setenv("TMUX_AGENTS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("TMUX_AGENTS_CODEX_HOME", str(tmp_path / "codex"))
    home = tmp_path / "codex"
    home.mkdir(parents=True)
    # Pre-seed with malformed groups
    (home / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        "bad-group",  # non-dict in list
                        {"hooks": "nope"},  # hooks not a list
                    ]
                }
            }
        )
    )
    assert codex_hooks.ensure_host() is True
    data = json.loads((home / "hooks.json").read_text())
    # Canonical SessionStart should be present
    assert "SessionStart" in data["hooks"]
    script_path = tmp_path / "cfg" / "codex-hook.sh"
    expected_cmd = codex_hooks.owned_command(script_path, "init")
    assert any(
        h.get("command") == expected_cmd
        for g in data["hooks"]["SessionStart"]
        if isinstance(g, dict)
        for h in g.get("hooks", [])
        if isinstance(h, dict)
    )
    # Bad group should be preserved as foreign data
    assert "bad-group" in data["hooks"]["SessionStart"]


def test_is_owned_recognizes_obsolete_script_locations():
    """Entries stranded at an old install path (moved config dir, or a leak
    from a redirected-paths run) are still ours to reclaim."""
    stale = "sh /tmp/pytest-of-x/test_y0/config/codex-hook.sh running"
    assert codex_hooks.is_owned(stale, SP)
    quoted = "sh '/weird dir/codex-hook.sh' idle"
    assert codex_hooks.is_owned(quoted, SP)
    # Shape guards still hold.
    assert not codex_hooks.is_owned("logger sh /a/codex-hook.sh idle", SP)
    assert not codex_hooks.is_owned("sh /a/codex-hook.sh.backup idle", SP)
    assert not codex_hooks.is_owned("sh /a/codex-hook.sh idle && echo hi", SP)


def test_ensure_host_reclaims_entries_at_stale_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUX_AGENTS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("TMUX_AGENTS_CODEX_HOME", str(tmp_path / "codex"))
    home = tmp_path / "codex"
    home.mkdir(parents=True)
    stale_cmd = "sh /tmp/pytest-of-x/test_y0/config/codex-hook.sh running"
    (home / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"type": "command", "command": stale_cmd}]}
                    ],
                    "Stop": [{"hooks": [{"type": "command", "command": "user-thing"}]}],
                }
            }
        )
    )
    assert codex_hooks.ensure_host() is True
    data = json.loads((home / "hooks.json").read_text())
    all_cmds = [
        h["command"]
        for groups in data["hooks"].values()
        for g in groups
        for h in g["hooks"]
    ]
    assert stale_cmd not in all_cmds  # zombie reclaimed and dropped
    assert "user-thing" in all_cmds  # genuine foreign hook preserved
    script = str(tmp_path / "cfg" / "codex-hook.sh")
    assert f"sh {script} running" in all_cmds  # canonical set present
