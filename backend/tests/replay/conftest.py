"""Replay fixtures (task B6).

- `session(id)`: open a recorded session from samples/sessions/ (held-out sessions raise: only `make eval` reads
  them, M5).
- `recorded_session`: parametrised over every non-held-out real session in samples/sessions/ (none yet = no cases).
- `format_session`: a minimal folder built from the public contract examples with placeholder ids and tiny
  format-only files (a 4-point PLY, a header-only JPEG). It tests the format code; it is not a capture and never
  goes into samples/.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from format_files import jpeg_header, ply_bytes
from repairos.sessions.reader import SAMPLES, list_sessions, open_session

EXAMPLES = Path(__file__).resolve().parents[3] / "spec" / "contracts" / "examples"


def pytest_configure(config):
    config.addinivalue_line("markers", "replay: runs on recorded real sessions in samples/sessions/")


def pytest_generate_tests(metafunc):
    if "recorded_session" in metafunc.fixturenames:
        paths = list_sessions(SAMPLES)
        metafunc.parametrize("recorded_session", paths, ids=[p.name for p in paths])


@pytest.fixture
def session():
    return lambda sid: open_session(sid)


def _ex(name: str) -> dict:
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def format_session(tmp_path) -> Path:
    pkg, still, reading = _ex("scan-package"), _ex("still"), _ex("meter-reading")
    sid = pkg["session_id"]
    d = tmp_path / "sessions" / sid
    scan_dir = d / "scans" / pkg["scan_id"]
    scan_dir.mkdir(parents=True)
    pts = [(0.0, 0.0, -0.4), (0.01, 0.0, -0.4), (0.0, 0.01, -0.41), (0.01, 0.01, -0.42)]
    (scan_dir / "points_arkit_world.ply").write_bytes(ply_bytes(pts))
    pkg["points_file"], pkg["point_count"] = f"scans/{pkg['scan_id']}/points_arkit_world.ply", len(pts)
    pkg.pop("alignment")
    for kf in pkg["keyframes"]:
        rel = f"scans/{pkg['scan_id']}/{kf['keyframe_id']}.jpg"
        (d / rel).write_bytes(jpeg_header(kf["intrinsics"]["width_px"], kf["intrinsics"]["height_px"]))
        kf["image_file"] = rel
        for fld in ("depth_file", "confidence_file"):
            if fld in kf:
                rel = f"scans/{pkg['scan_id']}/{kf['keyframe_id']}{'_depth' if fld == 'depth_file' else '_conf'}.png"
                (d / rel).write_bytes(b"\x89PNG format fixture")
                kf[fld] = rel
    (scan_dir / "scan_package.json").write_text(json.dumps(pkg), encoding="utf-8")
    (d / "stills").mkdir()
    still["image_file"] = f"stills/{still['still_id']}.jpg"
    (d / still["image_file"]).write_bytes(jpeg_header(still["width_px"], still["height_px"]))
    for fld in ("depth_file", "confidence_file"):
        if fld in still:
            still[fld] = f"stills/{still['still_id']}{'_depth' if fld == 'depth_file' else '_conf'}.png"
            (d / still[fld]).write_bytes(b"\x89PNG format fixture")
    (d / "stills" / f"{still['still_id']}.json").write_text(json.dumps(still), encoding="utf-8")
    (d / "readings").mkdir()
    (d / "readings" / f"{reading['reading_id']}.json").write_text(json.dumps(reading), encoding="utf-8")
    finding = _ex("finding")
    truth = {"faults": [{"subject": finding["subject"], "fault": finding["state"]}],
             "not_inspected_expected": [], "negatives": False}
    (d / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
    meta = {"session_id": sid, "device": {"model": pkg["device"]["model"]}, "split": "train", "source": "converter"}
    (d / "session.json").write_text(json.dumps(meta), encoding="utf-8")
    return d
