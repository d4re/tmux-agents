"""SSH agent forwarding wire framing.

Used by both the host-side pump and the in-container relay.
Importable by tests; also concatenated as source onto the inlined
`python3 -c` invocations at runtime.
"""

import struct
import threading

SENTINEL = b"\x00\x00\x00\x00"  # length-prefix 0 = "raw side closed"

# Relay → pump signal: another relay already owns the in-container UDS.
# Pump uses this to decide "defer" vs "retry" when the relay process exits.
EXIT_DUPLICATE = 75


class FrameError(Exception):
    pass


def encode_frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


def encode_sentinel() -> bytes:
    return SENTINEL


def read_frame(stream) -> bytes | None:
    """Read one frame from `stream`.

    Returns:
        - `b""` on sentinel (length-prefix 0).
        - payload bytes on a normal frame.
        - `None` on clean EOF (no bytes available at all).

    Raises `FrameError` on truncation mid-frame.
    """
    head = _read_exact(stream, 4)
    if head is None:
        return None
    if len(head) < 4:
        raise FrameError(f"truncated length prefix: {len(head)} bytes")
    length = struct.unpack(">I", head)[0]
    if length == 0:
        return b""
    payload = _read_exact(stream, length)
    if payload is None or len(payload) < length:
        got = 0 if payload is None else len(payload)
        raise FrameError(f"truncated payload: {got}/{length} bytes")
    return payload


def _read_exact(stream, n: int) -> bytes | None:
    """Read exactly n bytes; return None if 0 bytes read (clean EOF);
    return whatever was read on partial-EOF (caller decides if that's an error)."""
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return buf if buf else None
        buf += chunk
    return buf


def splice(raw_sock, framed_in, framed_out, *, buf_size: int = 16384) -> None:
    """Bidirectional pump: raw byte socket <-> framed stream pair.

    - Bytes read from `raw_sock` are wrapped in a frame and written to `framed_out`.
    - Frames read from `framed_in` are unwrapped and written to `raw_sock`.
    - On `raw_sock` EOF or error: write sentinel to `framed_out`, return.
    - On sentinel from `framed_in`: shut `raw_sock` down, return.
    - Errors are printed to stderr; the caller cleans up `raw_sock`.

    Returns when either side has signalled close. Does not close `raw_sock`.
    """
    import sys

    done = threading.Event()

    def raw_to_framed():
        try:
            while not done.is_set():
                buf = raw_sock.recv(buf_size)
                if not buf:
                    return
                framed_out.write(encode_frame(buf))
                framed_out.flush()
        except OSError as e:
            print(f"splice raw->framed: {e}", file=sys.stderr)
        finally:
            # Always emit sentinel so the peer's framed reader unblocks,
            # whether we exited via EOF, error, or done-flag short-circuit.
            # ValueError catches "I/O operation on closed file" when the peer
            # tears down framed_out while we're still in the loop.
            try:
                framed_out.write(encode_sentinel())
                framed_out.flush()
            except (OSError, ValueError):
                pass
            done.set()

    def framed_to_raw():
        try:
            while not done.is_set():
                payload = read_frame(framed_in)
                if payload is None or payload == b"":
                    return
                raw_sock.sendall(payload)
        except (FrameError, OSError) as e:
            print(f"splice framed->raw: {e}", file=sys.stderr)
        finally:
            done.set()

    t1 = threading.Thread(target=raw_to_framed, daemon=True)
    t2 = threading.Thread(target=framed_to_raw, daemon=True)
    t1.start()
    t2.start()
    done.wait()
    import socket

    try:
        raw_sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    # Threads may still be blocked on read; let them die with the process.
