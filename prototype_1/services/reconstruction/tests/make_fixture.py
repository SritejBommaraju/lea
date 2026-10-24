"""Writes the SYNTHETIC bracket fixture (reference in mm + a scan with the boss removed, arbitrary units).
Usage: python tests/make_fixture.py OUT_DIR"""
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parent.parent)]
import numpy as np
import open3d as o3d
from test_qa import bracket

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
o3d.io.write_triangle_mesh(str(out / "reference.ply"), bracket(True))
scan = bracket(False)
scan.rotate(o3d.geometry.get_rotation_matrix_from_xyz((0.4, -1.1, 2.0)), center=(0, 0, 0)).translate((3.0, -7.0, 11.0)).scale(0.00113, center=(0, 0, 0))
o3d.io.write_triangle_mesh(str(out / "scan.obj"), scan)
print(out)
