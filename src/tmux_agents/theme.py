"""State-color palette — defaults, config loading, ANSI/tmux derivations."""
from __future__ import annotations
import logging
import re
import tomllib
from dataclasses import dataclass
from tmux_agents import state
from tmux_agents import paths

logger = logging.getLogger(__name__)

DARK_DEFAULTS: dict[str, str] = {
    state.RUNNING:    "#87af5f",
    state.WAITING:    "#ffd75f",
    state.IDLE:       "#5fafff",
    state.BACKGROUND: "#5fd7d7",
    state.SLEEPING:   "#c678dd",
    state.ERRORED:    "#ff5f5f",
    state.STARTING:   "#666666",
}

LIGHT_DEFAULTS: dict[str, str] = {
    state.RUNNING:    "#4a7a2a",
    state.WAITING:    "#b8860b",
    state.IDLE:       "#0068a0",
    state.BACKGROUND: "#008787",
    state.SLEEPING:   "#7a3b9a",
    state.ERRORED:    "#c73030",
    state.STARTING:   "#999999",
}


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def _contrast_fg(h: str) -> str:
    r, g, b = _hex_to_rgb(h)
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    return "#000000" if lum > 0.6 else "#ffffff"


def _ansi_fg(h: str) -> str:
    r, g, b = _hex_to_rgb(h)
    return f"\x1b[38;2;{r};{g};{b}m"


def _ansi_bg(h: str) -> str:
    r, g, b = _hex_to_rgb(h)
    return f"\x1b[48;2;{r};{g};{b}m"


@dataclass(frozen=True)
class Palette:
    fg: dict[str, str]
    ansi_fg: dict[str, str]
    ansi_bg: dict[str, str]
    selected_fg: dict[str, str]
    ansi_selected_fg: dict[str, str]

    @classmethod
    def from_hex(cls, colors: dict[str, str]) -> "Palette":
        selected = {code: _contrast_fg(h) for code, h in colors.items()}
        return cls(
            fg=dict(colors),
            ansi_fg={code: _ansi_fg(h) for code, h in colors.items()},
            ansi_bg={code: _ansi_bg(h) for code, h in colors.items()},
            selected_fg=selected,
            ansi_selected_fg={code: _ansi_fg(h) for code, h in selected.items()},
        )


_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

_COLOR_KEYS: dict[str, str] = {
    "running":    state.RUNNING,
    "waiting":    state.WAITING,
    "idle":       state.IDLE,
    "background": state.BACKGROUND,
    "sleeping":   state.SLEEPING,
    "errored":    state.ERRORED,
    "starting":   state.STARTING,
}


def _resolve_overrides(raw_colors: dict, base: dict[str, str]) -> dict[str, str]:
    resolved = dict(base)
    for key, value in raw_colors.items():
        code = _COLOR_KEYS.get(key)
        if code is None:
            logger.warning("unknown [colors] key %r; ignoring", key)
            continue
        if not isinstance(value, str) or not _HEX_RE.match(value):
            logger.warning("%s: invalid hex %r; keeping default", key, value)
            continue
        resolved[code] = value.lower()
    return resolved


_BASE_BY_MODE = {"dark": DARK_DEFAULTS, "light": LIGHT_DEFAULTS}


def load() -> Palette:
    path = paths.theme_toml()
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        return Palette.from_hex(DARK_DEFAULTS)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("failed to parse %s: %s; using dark defaults", path, e)
        return Palette.from_hex(DARK_DEFAULTS)

    mode = raw.get("mode", "dark")
    base = _BASE_BY_MODE.get(mode)
    if base is None:
        logger.warning("unknown mode %r; using dark", mode)
        base = DARK_DEFAULTS
    colors = raw.get("colors", {})
    if not isinstance(colors, dict):
        logger.warning("'colors' must be a table; ignoring")
        colors = {}
    return Palette.from_hex(_resolve_overrides(colors, base))


_cached: Palette | None = None


def get_palette() -> Palette:
    global _cached
    if _cached is None:
        _cached = load()
    return _cached


def reset_cache() -> None:
    global _cached
    _cached = None
