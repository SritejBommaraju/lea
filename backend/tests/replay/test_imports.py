"""Real capture exports through the importers (B12). Put an export folder (or a NeRFCapture zip) under
samples/imports/<name>/; each one is imported into a temporary folder, must pass the session validator
(`make sessions-check`).

No export there = the tests skip with HUMAN ACTION NEEDED. Nothing synthetic stands in for a real export.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from repairos.importers import detect_format, import_capture
from repairos.importers.images import ImportFormatError
from repairos.importers.video import ToolMissing
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
def test_real_stray_export_imports_and_validates(export, tmp_path):
    res = _import(export, tmp_path)
    assert res.problems == [], res.problems
    s = open_session(res.path)
    pkg, stills = s.scans()[0], s.stills()
    assert pkg["depth_source"] == "lidar" and pkg["point_count"] > 0 and stills


@pytest.mark.parametrize("export", _cases("nerfcapture"))
def test_real_nerfcapture_export_imports_without_depth(export, tmp_path):
    res = _import(export, tmp_path)
    assert res.problems == [], res.problems
    s = open_session(res.path)
    pkg, stills = s.scans()[0], s.stills()
    assert pkg["depth_source"] == "none" and "points_file" not in pkg and stills
