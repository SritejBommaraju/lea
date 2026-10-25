"""Real capture exports through the importers (B12). Put an export folder (or a NeRFCapture zip) under
samples/imports/<name>/; each one is imported into a temporary folder, must pass the session validator
(`make sessions-check`), and a box at a still's centre must locate on the scan (or, without depth, say why not).

No export there = the tests skip with HUMAN ACTION NEEDED. Nothing synthetic stands in for a real export.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from repairos.importers import detect_format, import_capture
from repairos.importers.images import ImportFormatError
from repairos.importers.video import ToolMissing
from repairos.locate import Location, NotLocated, try_locate
from repairos.sessions.reader import open_session

IMPORTS = Path(__file__).resolve().parents[3] / "samples" / "imports"
ASK = {"stray": "HUMAN ACTION NEEDED: record a Stray Scanner scan (LiDAR iPhone, App Store 'Stray Scanner') of any "
                "table-top object and put the exported folder in samples/imports/",
       "nerfcapture": "HUMAN ACTION NEEDED: take a NeRFCapture offline capture (any ARKit iPhone) of any table-top "
                      "object and put the unzipped folder (or the zip) in samples/imports/"}


def _exports() -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {"stray": [], "nerfcapture": []}
    if IMPORTS.is_dir():
        for p in sorted(IMPORTS.iterdir()):
            if p.name.startswith("."):
                continue
            try:
                found[detect_format(p)].append(p)
            except ImportFormatError:
                pass
    return found


EXPORTS = _exports()


def _cases(fmt: str):
    return EXPORTS[fmt] or [pytest.param(None, marks=pytest.mark.skip(reason=ASK[fmt]))]


def _import(export: Path, tmp_path: Path):
    try:
        return import_capture(export, tmp_path)
    except ToolMissing as e:
        pytest.skip(str(e))


@pytest.mark.parametrize("export", _cases("stray"))
def test_real_stray_export_imports_validates_and_locates(export, tmp_path):
    res = _import(export, tmp_path)
    assert res.problems == [], res.problems
    s = open_session(res.path)
    pkg, stills = s.scans()[0], s.stills()
    assert pkg["depth_source"] == "lidar" and pkg["point_count"] > 0 and stills
    st = stills[len(stills) // 2]
    w, h = st["width_px"], st["height_px"]
    loc = try_locate(pkg, st, (0.45 * w, 0.45 * h, 0.55 * w, 0.55 * h), base_dir=s.dir)
    assert isinstance(loc, Location), loc.reason
    assert 0.05 < loc.distance_m < 5.0


@pytest.mark.parametrize("export", _cases("nerfcapture"))
def test_real_nerfcapture_export_imports_without_depth(export, tmp_path):
    res = _import(export, tmp_path)
    assert res.problems == [], res.problems
    s = open_session(res.path)
    pkg, stills = s.scans()[0], s.stills()
    assert pkg["depth_source"] == "none" and "points_file" not in pkg and stills
    st = stills[0]
    res2 = try_locate(pkg, st, (0, 0, st["width_px"] / 2, st["height_px"] / 2))
    assert isinstance(res2, NotLocated) and "triangulate" in res2.reason
