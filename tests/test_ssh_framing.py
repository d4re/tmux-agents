import io
import os
import socket
import threading

import pytest

from tmux_agents import _ssh_framing as f

def test_encode_frame_prefixes_big_endian_length():
    assert f.encode_frame(b"abc") == b"\x00\x00\x00\x03abc"

def test_encode_frame_empty_payload_is_sentinel():
    assert f.encode_frame(b"") == b"\x00\x00\x00\x00"
    assert f.encode_frame(b"") == f.SENTINEL

def test_encode_sentinel_constant():
    assert f.encode_sentinel() == b"\x00\x00\x00\x00"

def test_read_frame_round_trip_payload():
    buf = io.BytesIO(f.encode_frame(b"hello world"))
    assert f.read_frame(buf) == b"hello world"

def test_read_frame_sentinel_returns_empty_bytes():
    buf = io.BytesIO(f.SENTINEL)
    assert f.read_frame(buf) == b""

def test_read_frame_clean_eof_returns_none():
    buf = io.BytesIO(b"")
    assert f.read_frame(buf) is None

def test_read_frame_truncated_length_prefix_raises():
    buf = io.BytesIO(b"\x00\x01")
    with pytest.raises(f.FrameError):
        f.read_frame(buf)

def test_read_frame_truncated_payload_raises():
    buf = io.BytesIO(b"\x00\x00\x00\x05abc")
    with pytest.raises(f.FrameError):
        f.read_frame(buf)

def test_read_frame_large_payload():
    payload = b"x" * 70_000
    buf = io.BytesIO(f.encode_frame(payload))
    assert f.read_frame(buf) == payload


def _pipe_pair():
    """Return (read_fileobj, write_fileobj) for an os.pipe()."""
    r, w = os.pipe()
    return os.fdopen(r, "rb", buffering=0), os.fdopen(w, "wb", buffering=0)


def test_splice_forwards_raw_to_framed_and_back():
    raw_a, raw_b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    framed_in_r, framed_in_w = _pipe_pair()
    framed_out_r, framed_out_w = _pipe_pair()

    # `splice` reads framed-in from peer and writes framed-out to peer.
    # raw_a is the "splice's raw side". raw_b is what we (the test) drive.
    t = threading.Thread(
        target=f.splice,
        args=(raw_a, framed_in_r, framed_out_w),
        daemon=True,
    )
    t.start()

    # Test peer -> raw side: write a frame to framed_in_w; expect raw_b.recv() yields payload.
    framed_in_w.write(f.encode_frame(b"hello"))
    framed_in_w.flush()
    assert raw_b.recv(16) == b"hello"

    # raw side -> test peer: send bytes via raw_b; expect framed_out_r yields encoded frame.
    raw_b.sendall(b"world")
    head = framed_out_r.read(4)
    length = int.from_bytes(head, "big")
    payload = framed_out_r.read(length)
    assert payload == b"world"

    # Sentinel from peer side closes splice and shuts raw down.
    framed_in_w.write(f.encode_sentinel())
    framed_in_w.flush()
    t.join(timeout=2)
    assert not t.is_alive()
    raw_a.close(); raw_b.close()
    framed_in_w.close(); framed_out_r.close()


def test_splice_raw_eof_emits_sentinel_and_returns():
    raw_a, raw_b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    framed_in_r, framed_in_w = _pipe_pair()
    framed_out_r, framed_out_w = _pipe_pair()

    t = threading.Thread(
        target=f.splice,
        args=(raw_a, framed_in_r, framed_out_w),
        daemon=True,
    )
    t.start()

    raw_b.shutdown(socket.SHUT_WR)  # signal raw EOF to splice
    raw_b.close()
    # splice should write sentinel and return.
    head = framed_out_r.read(4)
    assert head == f.SENTINEL
    t.join(timeout=2)
    assert not t.is_alive()
    raw_a.close()
    framed_in_w.close(); framed_out_r.close()
