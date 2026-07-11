import json
from importlib import resources

TEMPLATE = resources.files("tmux_agents.hooks") / "agents.json"


def test_template_has_required_hooks_and_matchers():
    t = json.loads(TEMPLATE.read_text())
    required = {"SessionStart", "UserPromptSubmit", "Stop", "StopFailure",
                "Notification", "PermissionDenied",
                "PostToolUse", "PostToolUseFailure", "SessionEnd"}
    assert required.issubset(t["hooks"].keys())
    assert "permission_prompt" in [h.get("matcher") for h in t["hooks"]["Notification"]]
    post = [h.get("matcher") for h in t["hooks"]["PostToolUse"]]
    assert "CronCreate" in post and "CronDelete" in post


def test_every_command_dispatches_to_helper_script():
    """Hook commands no longer carry shell logic — they invoke
    write-state.sh, which guards TMUX_PANE itself. The bell is exempt."""
    t = json.loads(TEMPLATE.read_text())
    for group in t["hooks"].values():
        for entry in group:
            for h in entry["hooks"]:
                c = h["command"]
                if c == "printf '\\a'":
                    continue
                assert "write-state.sh" in c, f"command not dispatched to script: {c!r}"
