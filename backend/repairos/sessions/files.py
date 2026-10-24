"""Cheap, dependency-free checks of the capture files: binary PLY header and JPEG size (no decoding)."""
from __future__ import annotations

from typing import BinaryIO

_PLY_SIZES = {"char": 1, "uchar": 1, "int8": 1, "uint8": 1, "short": 2, "ushort": 2, "int16": 2, "uint16": 2,
              "int": 4, "uint": 4, "int32": 4, "uint32": 4, "float": 4, "float32": 4, "double": 8, "float64": 8}
_FLOATS = {"float", "float32", "double", "float64"}
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


class CaptureFileError(ValueError):
    pass


def ply_header(f: BinaryIO, size: int) -> dict:
    """Parse and check a PLY header: binary little-endian, a vertex element with float x, y, z, and enough bytes
    for every vertex. Returns {vertex_count, properties, header_bytes, stride}."""
    head = f.read(65536)
    end = head.find(b"end_header\n")
    if not head.startswith(b"ply\n") or end < 0:
        raise CaptureFileError("not a PLY file (no 'ply' magic or no end_header)")
    lines = head[:end].decode("ascii", "replace").splitlines()
    if "format binary_little_endian 1.0" not in lines:
        raise CaptureFileError("PLY must be 'format binary_little_endian 1.0'")
    count, props, in_vertex, others = None, [], False, False
    for line in lines:
        parts = line.split()
        if parts[:1] == ["element"]:
            in_vertex = parts[1] == "vertex"
            if in_vertex:
                count = int(parts[2])
            elif int(parts[2]) > 0:
                others = True
        elif parts[:1] == ["property"] and in_vertex:
            if parts[1] == "list":
                raise CaptureFileError("PLY vertex element may not hold list properties")
            if parts[1] not in _PLY_SIZES:
                raise CaptureFileError(f"PLY property type {parts[1]!r} unknown")
            props.append((parts[2], parts[1]))
    if count is None:
        raise CaptureFileError("PLY has no vertex element")
    types = dict(props)
    for axis in "xyz":
        if types.get(axis) not in _FLOATS:
            raise CaptureFileError(f"PLY vertex property {axis} must be float")
    stride = sum(_PLY_SIZES[t] for _, t in props)
    header_bytes = end + len(b"end_header\n")
    need = header_bytes + count * stride
    if size < need or (size != need and not others):
        raise CaptureFileError(f"PLY body is {size - header_bytes} bytes; {count} vertices x {stride} B = "
                               f"{count * stride} B")
    return {"vertex_count": count, "properties": [p for p, _ in props], "header_bytes": header_bytes,
            "stride": stride}


def jpeg_size(f: BinaryIO) -> tuple[int, int]:
    """(width, height) from the JPEG frame header; CaptureFileError when it is not a baseline/progressive JPEG."""
    if f.read(2) != b"\xff\xd8":
        raise CaptureFileError("not a JPEG (no SOI marker)")
    while True:
        b = f.read(1)
        if not b:
            raise CaptureFileError("JPEG ended before a frame header")
        if b != b"\xff":
            continue
        marker = f.read(1)
        while marker == b"\xff":
            marker = f.read(1)
        if not marker:
            raise CaptureFileError("JPEG ended before a frame header")
        m = marker[0]
        if m in (0x01, 0xD8) or 0xD0 <= m <= 0xD7:
            continue
        if m in (0xD9, 0xDA):
            raise CaptureFileError("JPEG has no frame header before the image data")
        seg = f.read(2)
        if len(seg) < 2:
            raise CaptureFileError("truncated JPEG segment")
        length = int.from_bytes(seg, "big")
        body = f.read(length - 2)
        if m in _SOF:
            if len(body) < 5:
                raise CaptureFileError("truncated JPEG frame header")
            return int.from_bytes(body[3:5], "big"), int.from_bytes(body[1:3], "big")
