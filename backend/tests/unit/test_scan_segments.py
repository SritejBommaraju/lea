"""Capture segments (object flipped for its underside): their point files are blob/file fields like points_file."""
import json
from pathlib import Path

from repairos import contracts
from repairos.sessions.layout import file_values, map_files

EXAMPLES = Path(__file__).resolve().parents[3] / "spec" / "contracts" / "examples"


def _pkg():
    pkg = json.loads((EXAMPLES / "scan-package.json").read_text())
    pkg["segments"] = [{"segment": 1, "reason": "flipped", "points_file": "scans/x/points_segment1.ply",
                        "point_count": 10,
                        "crop_box": {"frame": "arkit_world", "center_m": [0, 0.03, 0], "extent_m": [0.1, 0.06, 0.1],
                                     "quaternion_xyzw": [0, 0, 0, 1]}}]
    return pkg


def test_segment_package_validates():
    contracts.validate("scan-package", _pkg())


def test_segment_points_file_is_a_file_field():
    pkg = _pkg()
    assert "scans/x/points_segment1.ply" in file_values("scan-package", pkg)
    mapped = map_files("scan-package", pkg, lambda v, base: base)
    assert mapped["segments"][0]["points_file"] == f"scans/{pkg['scan_id']}/points_segment1_arkit_world"


def test_still_segment_field_validates():
    still = json.loads((EXAMPLES / "still.json").read_text())
    still.update({"segment": 1, "pose_reliable": False, "view_cell": "r1s03", "low_light": True})
    contracts.validate("still", still)


def test_the_merge_gate_keeps_a_good_bottom_and_refuses_a_bad_one():
    """A registered bottom joins the model only when the merged object makes sense: resting on the table with the
    top's outline. Floating over the table, sunk under it, or another outline: the top scan alone."""
    import numpy as np

    from repairos.pipeline.segments import merge_gate
    from repairos.recon.segment_align import ObjectCloud

    rng = np.random.default_rng(0)
    top = ObjectCloud(np.c_[rng.uniform(-0.05, 0.05, 3000), rng.uniform(0.01, 0.03, 3000),
                            rng.uniform(-0.08, 0.08, 3000)], None)
    good = ObjectCloud(np.c_[rng.uniform(-0.05, 0.05, 3000), rng.uniform(0.0, 0.02, 3000),
                             rng.uniform(-0.08, 0.08, 3000)], None)
    eye = np.eye(4)
    assert merge_gate(top, good, [], [], eye, 0.0)["ok"]
    floating = ObjectCloud(good.points + [0, 0.03, 0], None)
    g = merge_gate(top, floating, [], [], eye, 0.0)
    assert not g["ok"] and "floats" in g["why"]
    sunk = ObjectCloud(good.points - [0, 0.03, 0], None)
    assert "sinks" in merge_gate(top, sunk, [], [], eye, 0.0)["why"]
    turned = ObjectCloud(good.points[:, [2, 1, 0]] + [0.06, 0, 0], None)   # a quarter turn, beside the top
    g = merge_gate(top, turned, [], [], eye, 0.0)
    assert not g["ok"] and "outline" in g["why"]


def test_a_merge_the_gate_refused_stays_out_of_the_model_and_its_photos_stay_photos():
    """The record keeps the registration (aligned, transform) but the gate's no: the mesh and recreation fuse only
    merged segments, its photos are not placed, and the report says the segment is not aligned, with why."""
    import numpy as np

    from repairos.pipeline.segments import merged_transforms, report_segments, unplaced_segments

    t = list(np.eye(4).reshape(-1))
    rec = {"segments": [{"segment": 1, "aligned": True, "transform": t, "merge": {"ok": False, "why": "the bottom "
                                                                                          "floats 30 mm over the table"}},
                        {"segment": 2, "aligned": True, "transform": t, "merge": {"ok": True}},
                        {"segment": 3, "aligned": True, "transform": t}]}   # recorded before the gate: merged
    pkg = {"segments": [{"segment": k, "reason": "flipped", "object_moved": True} for k in (1, 2, 3)]}
    assert set(merged_transforms(rec)) == {2, 3}
    assert unplaced_segments(pkg, rec) == {1}
    rows = {r["segment"]: r for r in report_segments(pkg, rec, {})}
    assert rows[1]["aligned"] is False and "floats 30 mm" in rows[1]["why_not"]
    assert rows[2]["aligned"] and rows[3]["aligned"]
