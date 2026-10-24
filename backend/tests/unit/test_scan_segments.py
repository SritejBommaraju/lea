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
