"""A view-ready copy of a mesh for the phone: at most `max_tris` triangles (quadric decimation), written as
little-endian float32 xyz positions (same units as the input) followed by uint32 triangle indices.
Prints {"vertices": n, "triangles": m}. Usage: python -m lea_geometry.mesh_view in.ply out.bin [max_tris]"""
import json
import sys

import numpy as np
import open3d as o3d


def main(src: str, dst: str, max_tris: int = 150_000) -> int:
    mesh = o3d.io.read_triangle_mesh(src)
    if mesh.is_empty() or len(mesh.triangles) == 0:
        print(json.dumps({"error": "no triangles"}))
        return 2
    if len(mesh.triangles) > max_tris:
        mesh = mesh.simplify_quadric_decimation(target_number_of_triangles=max_tris)
    v = np.asarray(mesh.vertices, dtype="<f4")
    t = np.asarray(mesh.triangles, dtype="<u4")
    with open(dst, "wb") as f:
        f.write(v.tobytes())
        f.write(t.tobytes())
    print(json.dumps({"vertices": int(len(v)), "triangles": int(len(t))}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 150_000))
