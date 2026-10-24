"""Flatten a GLB scene (every node's transform applied) into one triangle mesh, written as binary PLY in the same
units. Used to turn a design's model.glb into a reference mesh Open3D reads reliably.
Usage: python -m lea_geometry.glb_to_ply in.glb out.ply"""
import json
import sys

import trimesh


def main(src: str, dst: str) -> int:
    scene = trimesh.load(src, force="scene")
    mesh = scene.to_geometry()
    if mesh.is_empty or len(mesh.faces) == 0:
        print(json.dumps({"error": "the GLB has no triangles"}))
        return 2
    mesh.export(dst, file_type="ply")
    print(json.dumps({"faces": int(len(mesh.faces)), "extents": [round(float(x), 3) for x in mesh.extents]}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
