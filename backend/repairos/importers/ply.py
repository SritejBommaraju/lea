"""Write the session's point cloud: binary little-endian PLY, arkit_world metres, `float x, y, z` +
`uchar red, green, blue` + optional `uchar confidence, views` (samples/README.md)."""
from __future__ import annotations

import numpy as np


def ply_bytes(xyz: np.ndarray, rgb: np.ndarray, *, confidence: np.ndarray | None = None,
              views: np.ndarray | None = None, comments: tuple[str, ...] = ()) -> bytes:
    xyz = np.asarray(xyz, dtype="<f4").reshape(-1, 3)
    rgb = np.asarray(rgb, dtype=np.uint8).reshape(-1, 3)
    n = len(xyz)
    if len(rgb) != n:
        raise ValueError(f"{n} points but {len(rgb)} colours")
    fields = [("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("red", "u1"), ("green", "u1"), ("blue", "u1")]
    extra = {}
    for name, arr in (("confidence", confidence), ("views", views)):
        if arr is not None:
            arr = np.asarray(arr).reshape(-1)
            if len(arr) != n:
                raise ValueError(f"{n} points but {len(arr)} {name} values")
            fields.append((name, "u1"))
            extra[name] = np.clip(arr, 0, 255).astype(np.uint8)
    body = np.empty(n, dtype=np.dtype(fields))
    body["x"], body["y"], body["z"] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    body["red"], body["green"], body["blue"] = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    for name, arr in extra.items():
        body[name] = arr
    head = ["ply", "format binary_little_endian 1.0", *(f"comment {c}" for c in comments), f"element vertex {n}"]
    head += [f"property {'float' if t == '<f4' else 'uchar'} {name}" for name, t in fields]
    head.append("end_header")
    return ("\n".join(head) + "\n").encode("ascii") + body.tobytes()


def mesh_ply_bytes(vertices: np.ndarray, faces: np.ndarray, rgb: np.ndarray | None = None,
                   comments: tuple[str, ...] = ()) -> bytes:
    """Triangle mesh as binary little-endian PLY: float x, y, z (+ uchar red, green, blue) vertices and a
    `uchar int vertex_indices` face list (the scan package's `mesh_file`)."""
    v = np.asarray(vertices, dtype="<f4").reshape(-1, 3)
    f = np.asarray(faces, dtype="<i4").reshape(-1, 3)
    if len(f) and (f.min() < 0 or f.max() >= len(v)):
        raise ValueError("face index out of range")
    vfields = [("x", "<f4"), ("y", "<f4"), ("z", "<f4")]
    if rgb is not None:
        vfields += [("red", "u1"), ("green", "u1"), ("blue", "u1")]
    vb = np.empty(len(v), dtype=np.dtype(vfields))
    vb["x"], vb["y"], vb["z"] = v[:, 0], v[:, 1], v[:, 2]
    if rgb is not None:
        c = np.asarray(rgb, dtype=np.uint8).reshape(-1, 3)
        vb["red"], vb["green"], vb["blue"] = c[:, 0], c[:, 1], c[:, 2]
    fb = np.empty(len(f), dtype=np.dtype([("n", "u1"), ("i", "<i4", (3,))]))
    fb["n"], fb["i"] = 3, f
    head = ["ply", "format binary_little_endian 1.0", *(f"comment {c}" for c in comments), f"element vertex {len(v)}"]
    head += [f"property {'float' if t == '<f4' else 'uchar'} {name}" for name, t in vfields]
    head += [f"element face {len(f)}", "property list uchar int vertex_indices", "end_header"]
    return ("\n".join(head) + "\n").encode("ascii") + vb.tobytes() + fb.tobytes()
