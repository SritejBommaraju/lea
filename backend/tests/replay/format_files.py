"""Format-only test files (not captures): a tiny binary PLY and a header-only JPEG."""
from __future__ import annotations

import struct


def ply_bytes(points: list[tuple[float, float, float]]) -> bytes:
    head = ("ply\nformat binary_little_endian 1.0\nelement vertex %d\nproperty float x\nproperty float y\n"
            "property float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n"
            % len(points)).encode()
    return head + b"".join(struct.pack("<fffBBB", *p, 0, 0, 0) for p in points)


def jpeg_header(width: int, height: int) -> bytes:
    """SOI + SOF0 + EOI: enough for the size check, not an image."""
    sof = b"\x08" + struct.pack(">HH", height, width) + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof + b"\xff\xd9"
