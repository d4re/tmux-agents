"""Per-stage progress output for `agent-new` (popup) and `agent-restore`
(placeholder logs).

`Reporter` owns one output stream; `Stage` is its context-manager helper.
`MultiReporter` fans out to N reporters for the restore broadcast case."""

from __future__ import annotations
import os
import sys
import time
from typing import Callable, TextIO

from tmux_agents import state, theme

_SYM_INFO = "▸"
_SYM_OK = "✓"
_SYM_WARN = "!"
_SYM_FAIL = "✗"

_RESET = "\x1b[0m"

_SYM_TO_PALETTE = {
    _SYM_INFO: state.STARTING,
    _SYM_OK: state.RUNNING,
    _SYM_WARN: state.WAITING,
    _SYM_FAIL: state.ERRORED,
}


def _format_elapsed(seconds: float) -> str:
    """Format elapsed time per spec: omit <1s, `(N.Ns)` 1-60s, `(Nm Ns)` ≥60s."""
    if seconds < 1.0:
        return ""
    total_s = int(round(seconds))
    if total_s < 60:
        return f"({seconds:.1f}s)"
    minutes, rem = divmod(total_s, 60)
    return f"({minutes}m {rem}s)"


class Stage:
    """Context manager returned by `Reporter.stage(name)`. Tracks elapsed
    time and which exit line (if any) to emit on `__exit__`."""

    def __init__(self, reporter: "Reporter", name: str) -> None:
        self._r = reporter
        self.name = name
        self._start: float = 0.0
        self._suppress_ok = False  # set by skip/warn
        self._pending_warn: str | None = (
            None  # deferred; emitted in __exit__ with timing
        )

    def info(self, detail: str) -> None:
        # In-progress line — no timing, emit immediately.
        self._r._emit(_SYM_INFO, f"{self.name} — {detail}")

    def skip(self, detail: str) -> None:
        # No timing on skip lines per spec — emit immediately, suppress ✓.
        self._r._emit(_SYM_INFO, f"{self.name} — {detail}")
        self._suppress_ok = True

    def warn(self, detail: str) -> None:
        # Defer to __exit__ so timing is appended.
        self._pending_warn = detail
        self._suppress_ok = True
        self._r._had_warning = True

    def __enter__(self) -> "Stage":
        self._start = self._r._clock()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        elapsed = self._r._clock() - self._start
        timing = _format_elapsed(elapsed)
        if exc is not None:
            msg = f"{self.name} — {exc_type.__name__}"
            if str(exc):
                msg += f": {exc}"
            if timing:
                msg += f" {timing}"
            self._r._emit(_SYM_FAIL, msg)
            return False  # re-raise
        if self._pending_warn is not None:
            msg = f"{self.name} — {self._pending_warn}"
            if timing:
                msg += f" {timing}"
            self._r._emit(_SYM_WARN, msg)
            return False
        if not self._suppress_ok:
            line = f"{self.name} {timing}" if timing else self.name
            self._r._emit(_SYM_OK, line)
        return False


class Reporter:
    """Single-stream stage reporter. Writes to `out` (defaults to stderr),
    coloring symbols via theme.get_palette() when `color=True`. Pass
    `clock` for deterministic timing in tests."""

    def __init__(
        self,
        out: TextIO = sys.stderr,
        *,
        color: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._out = out
        self._color = (
            color
            if color is not None
            else (out.isatty() and os.environ.get("NO_COLOR") is None)
        )
        self._clock = clock
        self._had_warning = False

    @property
    def had_warning(self) -> bool:
        return self._had_warning

    def banner(self, title: str) -> None:
        self._out.write(f"\n{title}\n\n")
        self._out.flush()

    def stage(self, name: str) -> Stage:
        return Stage(self, name)

    def _emit(self, symbol: str, body: str) -> None:
        if self._color:
            code = _SYM_TO_PALETTE[symbol]
            ansi = theme.get_palette().ansi_fg[code]
            self._out.write(f"{ansi}{symbol}{_RESET} {body}\n")
        else:
            self._out.write(f"{symbol} {body}\n")
        self._out.flush()


class MultiStage:
    """Fan-out of `Stage` over N underlying reporters."""

    def __init__(self, stages: list[Stage]) -> None:
        self._stages = stages

    def info(self, detail: str) -> None:
        for s in self._stages:
            s.info(detail)

    def skip(self, detail: str) -> None:
        for s in self._stages:
            s.skip(detail)

    def warn(self, detail: str) -> None:
        for s in self._stages:
            s.warn(detail)

    def __enter__(self) -> "MultiStage":
        for s in self._stages:
            s.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        # Each underlying Stage.__exit__ handles its own line emission
        # (✓ on clean, ! on pending_warn, ✗ on exception).
        for s in self._stages:
            s.__exit__(exc_type, exc, tb)
        return False  # re-raise exceptions


class MultiReporter:
    """Broadcasts banner / stage events to N underlying Reporters."""

    def __init__(self, reporters: list[Reporter]) -> None:
        self._reporters = reporters

    @property
    def had_warning(self) -> bool:
        return any(r.had_warning for r in self._reporters)

    def banner(self, title: str) -> None:
        for r in self._reporters:
            r.banner(title)

    def stage(self, name: str) -> MultiStage:
        return MultiStage([r.stage(name) for r in self._reporters])
