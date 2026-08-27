"""User-layer Codex hook provisioning: a package-owned codex-hook.sh
installed OUTSIDE every workspace + owned entries in ~/.codex/hooks.json.
Ownership = exact structural command match (quoted script path as the
sole first argument + one action word) — migration-safe without a ledger,
and immune to matching user wrappers/backups. ensure_* validates content
(script digest + canonical owned structure), not presence."""

from __future__ import annotations
import json
import os
import re
import shlex
import subprocess
from importlib import resources
from pathlib import Path, PurePosixPath

from tmux_agents import container, locks, paths

_SCRIPT_NAME = "codex-hook.sh"

HOOK_EVENTS = {
    "SessionStart": "init",
    "UserPromptSubmit": "running",
    "PermissionRequest": "waiting",
    "PreToolUse": "running",  # general activity signal, not approval-granted
    "PostToolUse": "running",
    "Stop": "idle",
}


def packaged_script() -> str:
    return (resources.files("tmux_agents.hooks") / _SCRIPT_NAME).read_text()


def codex_home() -> Path:
    env = os.environ.get("TMUX_AGENTS_CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def host_script_path() -> Path:
    return paths.config_dir() / _SCRIPT_NAME


def owned_command(script_path: Path, action: str) -> str:
    return f"sh {shlex.quote(str(script_path))} {action}"


def _owned_re(script_path: Path) -> re.Pattern:
    return re.compile(
        rf"^sh {re.escape(shlex.quote(str(script_path)))} [A-Za-z0-9_-]+$"
    )


# The generic shape of any command we have EVER generated: `sh` + one
# absolute path whose basename is codex-hook.sh (quoted or bare) + exactly
# one action word. Matching by shape (not just the current install path)
# lets ensure/merge reclaim entries stranded at obsolete locations — a
# moved config dir, or a leak from a run with redirected paths. Wrappers
# (`logger sh …`), trailing shell (`… && rm`), and `codex-hook.sh.backup`
# still never match: the anchors, the single action word, and the
# `/codex-hook.sh` basename suffix rule them out.
_ANY_OWNED_RE = re.compile(
    r"^sh (?:'/[^']*/codex-hook\.sh'|/[^\s'\"]*/codex-hook\.sh) [A-Za-z0-9_-]+$"
)


def is_owned(command: str, script_path: Path) -> bool:
    return bool(_owned_re(script_path).match(command) or _ANY_OWNED_RE.match(command))


def _as_dict(v) -> dict:
    """Normalize value to dict; non-dict becomes {}."""
    return v if isinstance(v, dict) else {}


def canonical_hooks(script_path: Path) -> dict:
    return {
        event: [
            {
                "hooks": [
                    {"type": "command", "command": owned_command(script_path, action)}
                ]
            }
        ]
        for event, action in HOOK_EVENTS.items()
    }


def merge(existing: dict, script_path: Path) -> dict:
    """Strip every owned entry wherever it is (wrong event, duplicated),
    drop emptied groups/events, then append the canonical set. Tolerate
    malformed group-level shapes: non-list groups, non-dict entries,
    non-list hooks values, non-string commands."""
    out: dict = {}
    for event, v in (existing or {}).items():
        # Normalize groups to list; non-list values are foreign garbage
        groups = v if isinstance(v, list) else []

        kept_groups = []
        for g in groups:
            # Skip non-dict groups (foreign garbage we can't parse);
            # preserve as-is per conservative "foreign entries survive" policy
            if not isinstance(g, dict):
                kept_groups.append(g)
                continue

            # Normalize hooks list; non-list is treated as empty
            hooks_list = g.get("hooks")
            hooks_list = hooks_list if isinstance(hooks_list, list) else []

            kept = []
            for h in hooks_list:
                # Skip non-dict entries (foreign/malformed); preserve as-is
                if not isinstance(h, dict):
                    kept.append(h)
                    continue

                cmd = h.get("command", "")
                # Only skip owned commands that are strings; preserve malformed
                if isinstance(cmd, str) and is_owned(cmd, script_path):
                    continue
                kept.append(h)

            if kept:
                kept_groups.append({**g, "hooks": kept})

        if kept_groups:
            out[event] = kept_groups

    for event, groups in canonical_hooks(script_path).items():
        out.setdefault(event, [])
        out[event] = out[event] + groups
    return out


def _owned_subset(hooks_table: dict, script_path: Path) -> dict:
    """Extract only the owned entries from a hooks table, normalized to a
    `(command, type, matcher)` descriptor per entry — not just the command —
    so `ensure_*` validates the FULL owned structure: right events, right
    matcher, right command, right type, exact multiplicity. `matcher` is
    the containing group's matcher value (`None` when absent, so a missing
    key and an explicit `null` compare equal). Tolerate malformed
    group-level shapes: non-list groups, non-dict entries, non-list hooks
    values, non-string commands."""
    subset: dict = {}
    for event, v in (hooks_table or {}).items():
        # Normalize groups to list; non-list is ignored
        groups = v if isinstance(v, list) else []

        entries = []
        for g in groups:
            # Skip non-dict groups (can't extract hooks from them)
            if not isinstance(g, dict):
                continue

            matcher = g.get("matcher")
            # Normalize hooks list; non-list is treated as empty
            hooks_list = g.get("hooks")
            hooks_list = hooks_list if isinstance(hooks_list, list) else []

            for h in hooks_list:
                # Skip non-dict entries
                if not isinstance(h, dict):
                    continue

                cmd = h.get("command", "")
                # Only include owned commands that are strings
                if isinstance(cmd, str) and is_owned(cmd, script_path):
                    entries.append((cmd, h.get("type"), matcher))

        if entries:
            # Sort by a string key: entries can carry malformed, mutually
            # unorderable `type`/`matcher` values (e.g. a dict), so sorting
            # the tuples directly could raise TypeError.
            subset[event] = sorted(entries, key=repr)
    return subset


def _canonical_subset(script_path: Path) -> dict:
    return {
        e: [(owned_command(script_path, a), "command", None)]
        for e, a in HOOK_EVENTS.items()
    }


def ensure_host() -> bool:
    """Idempotent: verify script digest + canonical owned hooks structure;
    (re)provision on any deviation. Returns True if anything was written."""
    with locks.locked(paths.config_dir() / "codex-hooks.lock"):
        wrote = False
        script = host_script_path()
        want = packaged_script()
        if not script.exists() or script.read_text() != want:
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text(want)
            script.chmod(0o755)
            wrote = True
        hooks_file = codex_home() / "hooks.json"
        data = _as_dict(paths.read_json_or(hooks_file, {}) or {})
        table = _as_dict(data.get("hooks"))
        if _owned_subset(table, script) != _canonical_subset(script):
            data["hooks"] = merge(table, script)
            paths.atomic_write_json(hooks_file, data, indent=2)
            wrote = True
        return wrote


def _container_cat(container_name: str, user: str, path: PurePosixPath) -> str:
    """`cat <path>` inside the container; absence -> ""."""
    try:
        return container.exec_capture(
            container_name, user, f"cat {shlex.quote(str(path))}"
        )
    except subprocess.CalledProcessError:
        return ""


def _container_deliver(
    container_name: str,
    user: str,
    path: PurePosixPath,
    content: str,
    *,
    mode: str | None = None,
) -> None:
    """Write `content` to `path` inside the container as `user`, via a
    unique mktemp + atomic rename so concurrent writers converge without
    a lock (content is deterministic host-side)."""
    directory = shlex.quote(str(path.parent))
    chmod = f' && chmod {mode} "$t"' if mode else ""
    script = (
        f"mkdir -p {directory} && "
        f"t=$(mktemp {directory}/.codex-hook.XXXXXX) && "
        f'cat > "$t"{chmod} && mv "$t" {shlex.quote(str(path))}'
    )
    container.exec_capture(container_name, user, script, stdin=content)


def _ensure_remote(cat, deliver_fn, home: str) -> bool:
    """Shared body of ensure_container/ensure_sandbox: same canonical
    script + owned hooks.json entries, delivered via the backend's own
    exec/deliver primitives. `cat(path) -> str` returns "" for a missing
    file; `deliver_fn(path, content, mode=None)` must be unique-mktemp +
    atomic-rename. Returns True iff anything was written."""
    script = PurePosixPath(home) / ".codex" / "tmux-agents" / _SCRIPT_NAME
    hooks_file = PurePosixPath(home) / ".codex" / "hooks.json"

    wrote = False

    want = packaged_script()
    if cat(script) != want:
        deliver_fn(script, want, mode="755")
        wrote = True

    raw = cat(hooks_file)
    try:
        data = _as_dict(json.loads(raw) if raw.strip() else {})
    except ValueError:
        data = {}
    table = _as_dict(data.get("hooks"))
    if _owned_subset(table, script) != _canonical_subset(script):
        data["hooks"] = merge(table, script)
        deliver_fn(hooks_file, json.dumps(data, indent=2))
        wrote = True

    return wrote


def ensure_container(container_name: str, user: str) -> bool:
    """Container-side twin of `ensure_host`: same canonical script +
    owned hooks.json entries, provisioned inside the container via
    `docker exec -u <user>`. No lock — writes are a unique mktemp +
    atomic rename with deterministic content, so concurrent callers
    converge. Returns True iff anything was written."""
    home = container.exec_capture(container_name, user, 'printf %s "$HOME"').strip()

    def cat(path: PurePosixPath) -> str:
        return _container_cat(container_name, user, path)

    def deliver_fn(path: PurePosixPath, content: str, mode: str | None = None) -> None:
        _container_deliver(container_name, user, path, content, mode=mode)

    return _ensure_remote(cat, deliver_fn, home)


def ensure_sandbox(name: str) -> bool:
    """Sandbox twin of `ensure_container`: same guarantees (home discovery
    via exec_capture, foreign-hook-preserving merge, digest comparison,
    atomic delivery), through `sbx exec` instead of `docker exec`. Raises
    sandbox.SandboxError when sbx itself fails. Imported lazily — sandbox
    imports config, and keeping this module import-light avoids a cycle."""
    from tmux_agents import sandbox

    home = sandbox.exec_capture(name, 'printf %s "$HOME"').strip()

    absent = "__tmux_agents_absent__"

    def cat(path: PurePosixPath) -> str:
        # Sentinel-based absence probe: only a CONFIRMED missing file reads
        # as "" (fresh provisioning). A transport/read failure must raise —
        # treating it as missing would make the merge below overwrite
        # hooks.json from scratch and clobber foreign hook entries.
        q = shlex.quote(str(path))
        out = sandbox.exec_capture(
            name, f"if [ -e {q} ]; then cat {q}; else printf %s {absent}; fi"
        )
        return "" if out == absent else out

    def deliver_fn(path: PurePosixPath, content: str, mode: str | None = None) -> None:
        sandbox.deliver(name, path, content, mode=mode)

    return _ensure_remote(cat, deliver_fn, home)
