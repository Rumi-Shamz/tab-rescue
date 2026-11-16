"""Pure-Python LZ4 *block* decompressor plus the Mozilla ``mozLz40`` container."""

from __future__ import annotations

import struct

MOZLZ4_MAGIC = b"mozLz40\0"


class LZ4Error(ValueError):
    pass


def decompress_block(src: bytes | bytearray | memoryview, uncompressed_size: int | None = None) -> bytes:
    """Decompress a raw LZ4 block (no frame header).

    ``uncompressed_size`` is only used as a sanity limit / pre-allocation hint.
    """
    src = bytes(src)
    n = len(src)
    out = bytearray()
    i = 0
    limit = uncompressed_size if uncompressed_size is not None else None
    while i < n:
        token = src[i]
        i += 1
        lit_len = token >> 4
        if lit_len == 15:
            while True:
                if i >= n:
                    raise LZ4Error("truncated literal length")
                b = src[i]
                i += 1
                lit_len += b
                if b != 255:
                    break
        if i + lit_len > n:
            raise LZ4Error("truncated literals")
        out += src[i:i + lit_len]
        i += lit_len
        if i >= n:
            break  # the last sequence has literals only
        if i + 2 > n:
            raise LZ4Error("truncated match offset")
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0 or offset > len(out):
            raise LZ4Error("invalid match offset")
        match_len = token & 15
        if match_len == 15:
            while True:
                if i >= n:
                    raise LZ4Error("truncated match length")
                b = src[i]
                i += 1
                match_len += b
                if b != 255:
                    break
        match_len += 4
        start = len(out) - offset
        if offset >= match_len:
            out += out[start:start + match_len]
        else:  # overlapping copy: repeat the pattern
            pattern = out[start:]
            reps, rem = divmod(match_len, offset)
            out += pattern * reps + pattern[:rem]
        if limit is not None and len(out) > limit:
            raise LZ4Error("output exceeds declared size")
    if uncompressed_size is not None and len(out) != uncompressed_size:
        raise LZ4Error(f"size mismatch: got {len(out)}, expected {uncompressed_size}")
    return bytes(out)


def compress_literals(data: bytes) -> bytes:
    """Encode ``data`` as a valid LZ4 block made of a single literal run.

    Not a real compressor; used to build test fixtures.
    """
    n = len(data)
    if n < 15:
        return bytes([n << 4]) + data
    out = bytearray([0xF0])
    rest = n - 15
    while rest >= 255:
        out.append(255)
        rest -= 255
    out.append(rest)
    return bytes(out) + data


def read_mozlz4(data: bytes) -> bytes:
    """Decode the contents of a ``*.jsonlz4`` / ``*.baklz4`` Mozilla file."""
    if not data.startswith(MOZLZ4_MAGIC):
        raise LZ4Error("not a mozLz40 file")
    if len(data) < 12:
        raise LZ4Error("truncated mozLz40 header")
    (size,) = struct.unpack_from("<I", data, 8)
    return decompress_block(data[12:], size)
