"""SYNTHETIC fixture: an asymmetric bracket built in code. It proves the QA arithmetic and registration;
it is not evidence that a real photo reconstruction passes."""
import numpy as np
import open3d as o3d
import pytest

from lea_geometry.qa import run_qa


def box(size, at):
    b = o3d.geometry.TriangleMesh.create_box(*size)
    b.translate(at)
    return b


def bracket(with_boss=True):
    # 80 x 30 base, 42 mm upright at one end, a 10 mm boss near the other end: no symmetry.
    parts = [box((80, 30, 4), (0, 0, 0)), box((6, 30, 42), (0, 0, 4)), box((12, 8, 3), (50, 18, 4))]
    if with_boss:
        parts.append(o3d.geometry.TriangleMesh.create_cylinder(radius=5, height=10).translate((66, 10, 9)))
    m = parts[0]
    for p in parts[1:]:
        m += p
    return m.subdivide_midpoint(2)


@pytest.fixture
def files(tmp_path):
    ref = bracket(True)
    o3d.io.write_triangle_mesh(str(tmp_path / "ref.ply"), ref)
    scan = bracket(False)                       # boss snapped off
    rng = np.random.default_rng(7)
    rot = o3d.geometry.get_rotation_matrix_from_xyz((0.4, -1.1, 2.0))
    scan.rotate(rot, center=(0, 0, 0)).translate((3.0, -7.0, 11.0)).scale(0.00113, center=(0, 0, 0))
    v = np.asarray(scan.vertices)
    scan.vertices = o3d.utility.Vector3dVector(v + rng.normal(0, 0.0002 * 0.00113 * 1000, v.shape))
    o3d.io.write_triangle_mesh(str(tmp_path / "scan.obj"), scan)
    return tmp_path


def test_scale_holdout_fit_and_missing_boss(files):
    r, _ = run_qa(files / "scan.obj", files / "ref.ply", 1.0, "longest", 80.0, "middle", 1.0, "synthetic")
    assert r.scale_factor == pytest.approx(1 / 0.00113, rel=0.01)
    assert abs(r.measured_holdout_mm - 46.0) < 1.0     # 4 mm base + 42 mm upright
    assert r.fit_rms_mm < 0.6 and r.fit_inlier_share > 0.95
    assert 0.8 < r.coverage < 0.99                    # the boss is unreached reference surface
    missing = [g for g in r.regions if g.kind == "missing_material"]
    assert missing, r.regions
    c = np.array(missing[0].centroid_mm)
    assert np.linalg.norm(c[:2] - (66, 10)) < 6 and c[2] > 8


def test_wrong_anchor_measurement_shows_in_holdout(files):
    r, _ = run_qa(files / "scan.obj", files / "ref.ply", 1.0, "longest", 88.0, "middle", 1.0, "synthetic")
    assert abs(r.measured_holdout_mm - 46.0) > 3.5     # 10% anchor error appears as holdout error


def test_same_axis_for_anchor_and_holdout_is_refused(files):
    with pytest.raises(ValueError, match="different dimension"):
        run_qa(files / "scan.obj", files / "ref.ply", 1.0, "longest", 80.0, "longest", 1.0, "synthetic")


def test_no_reference_reports_unmeasured_fit(files):
    r, _ = run_qa(files / "scan.obj", None, 1.0, "longest", 80.0, "middle", 1.0, "synthetic")
    assert not r.reference_present and r.coverage == 0.0 and r.fit_inlier_share == 0.0


def test_near_equal_sides_are_not_trusted(tmp_path):
    # 60 x 58 x 10 plate: longest and middle are within 5%, shortest is distinct.
    plate = o3d.geometry.TriangleMesh.create_box(60, 58, 10).subdivide_midpoint(2)
    o3d.io.write_triangle_mesh(str(tmp_path / "plate.ply"), plate)
    r, _ = run_qa(tmp_path / "plate.ply", None, 1.0, "longest", 60.0, "shortest", 1.0, "synthetic")
    assert not r.scale_trusted and r.holdout_trusted
    r, _ = run_qa(tmp_path / "plate.ply", None, 1.0, "shortest", 10.0, "middle", 1.0, "synthetic")
    assert r.scale_trusted and not r.holdout_trusted


def perforated_plate(w=80.0, d=30.0, t=4.0, holes=((15.0, 15.0, 2.5), (65.0, 15.0, 2.5)), cell=0.5):
    """SYNTHETIC plate with two 5 mm through-holes: top and bottom faces as quads, cells inside a hole left out."""
    verts, tris = [], []
    for z, flip in ((0.0, True), (t, False)):
        for i in range(int(w / cell)):
            for j in range(int(d / cell)):
                cx, cy = (i + 0.5) * cell, (j + 0.5) * cell
                if any((cx - hx) ** 2 + (cy - hy) ** 2 < r * r for hx, hy, r in holes):
                    continue
                k = len(verts)
                verts += [(i * cell, j * cell, z), ((i + 1) * cell, j * cell, z), ((i + 1) * cell, (j + 1) * cell, z), (i * cell, (j + 1) * cell, z)]
                tris += [(k, k + 2, k + 1), (k, k + 3, k + 2)] if flip else [(k, k + 1, k + 2), (k, k + 2, k + 3)]
    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(np.array(verts)), o3d.utility.Vector3iVector(np.array(tris)))
    return m


def test_features_find_plate_thickness_and_holes(tmp_path):
    from lea_geometry.features import extract
    o3d.io.write_triangle_mesh(str(tmp_path / "plate.ply"), perforated_plate())
    f = extract(str(tmp_path / "plate.ply"))
    assert any(abs(w["thickness_mm"] - 4.0) < 0.3 for w in f["walls"]), f["walls"]
    holes = f["holes"]
    assert len(holes) == 2, holes      # each through-hole once, and no sampling gaps
    for h in holes:
        assert abs(h["diameter_mm"] - 5.0) < 0.8 and h["round"]
    xs = sorted(h["centre_mm"][0] for h in holes)
    assert any(abs(x - 15) < 1 for x in xs) and any(abs(x - 65) < 1 for x in xs)


def test_features_find_a_boss_and_a_slot(tmp_path):
    from lea_geometry.features import extract
    plate = perforated_plate(holes=((15.0, 15.0, 2.5),))
    # an elongated slot: two overlapping circles cut from the same plate builder
    slot = perforated_plate(holes=tuple((x, 15.0, 2.0) for x in np.arange(55.0, 69.0, 0.5)))
    boss = o3d.geometry.TriangleMesh.create_cylinder(radius=5, height=10, resolution=96, split=8).translate((40, 15, 9))
    for name, m in (("boss.ply", plate + boss), ("slot.ply", slot)):
        o3d.io.write_triangle_mesh(str(tmp_path / name), m)
    f = extract(str(tmp_path / "boss.ply"))
    assert any(abs(c["diameter_mm"] - 10) < 0.8 and abs(c["length_mm"] - 10) < 1.5 for c in f["cylinders"]), f["cylinders"]
    s = extract(str(tmp_path / "slot.ply"))
    slots = [h for h in s["holes"] if h["kind"] == "slot"]
    assert slots and max(slots[0]["extent_mm"]) > 15 and min(slots[0]["extent_mm"]) < 5.5, s["holes"]
