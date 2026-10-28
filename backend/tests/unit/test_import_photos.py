"""Plain-photo importer (tools/import_photos.py, repairos.importers.photos): metadata stripping, orientation,
intrinsics from EXIF, HEIC through sips, folder handling, the session it writes and the still contract change
(a still without a pose only when pose_reliable is false).

The photos here are generated gradients with hand-written EXIF (including GPS): format fixtures for the metadata
and folder code, not captures of any product. They never go into samples/.
"""
from __future__ import annotations

import importlib.util
import io
import json
import math
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from repairos import contracts
from repairos.importers import images, photos
from repairos.importers.images import ImportFormatError
from repairos.importers.photos import (
    ToolMissing, capture_time, estimate_intrinsics, import_photos, jpeg_metadata_segments, list_photos,
)
from repairos.importers.session import CaptureMeta
from repairos.sessions.reader import open_session
from repairos.sessions.validate import validate_session

ROOT = Path(__file__).resolve().parents[3]
GPS = {1: "N", 2: (37.0, 46.0, 30.0), 3: "W", 4: (122.0, 25.0, 10.0), 6: 12.5}
HAS_SIPS = sys.platform == "darwin" and shutil.which("sips") is not None


def gradient(w: int = 64, h: int = 48) -> np.ndarray:
    """An asymmetric colour gradient: every 90 degree turn of it looks different."""
    x, y = np.meshgrid(np.linspace(0, 1, w), np.linspace(0, 1, h))
    return (np.stack([x, y, (x * y) ** 0.5], axis=-1) * 255).astype(np.uint8)


def write_photo(path: Path, rgb: np.ndarray | None = None, *, orientation: int = 1, taken: str | None = None,
                offset: str | None = "+02:00", f35: float | None = 24, gps: bool = True, model: str = "iPhone 15 Pro",
                software: str = "18.1", comment: bytes | None = b"a private note") -> Path:
    ex = Image.Exif()
    ex[photos.T_MAKE], ex[photos.T_MODEL], ex[photos.T_SOFTWARE] = "Apple", model, software
    ex[photos.T_ORIENTATION] = orientation
    ifd = ex.get_ifd(photos.IFD_EXIF)
    ifd[photos.T_FOCAL_LENGTH] = 6.765
    ifd[photos.T_LENS_MODEL] = "back camera 6.765mm f/1.78"
    if f35:
        ifd[photos.T_FOCAL_35MM] = f35
    if taken:
        ifd[photos.T_DATETIME_ORIGINAL] = taken
        if offset:
            ifd[photos.T_OFFSET_ORIGINAL] = offset
    if gps:
        ex.get_ifd(photos.IFD_GPS).update(GPS)
    kw = {"exif": ex, "quality": 95}
    if comment:
        kw["comment"] = comment
    Image.fromarray(gradient() if rgb is None else rgb).save(path, "JPEG", **kw)
    return path


def read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "photos"
    d.mkdir()
    return d


# --- privacy -----------------------------------------------------------------------------------------------------

def test_gps_exif_and_comments_are_stripped_from_every_written_jpeg(folder, tmp_path):
    src = write_photo(folder / "IMG_0001.JPG", taken="2026:10:24 10:11:12")
    with Image.open(src) as im:   # the fixture really carries what must go
        assert im.getexif().get_ifd(photos.IFD_GPS) and im.info.get("comment")
    assert any(s.startswith("APP1 Exif") for s in jpeg_metadata_segments(src.read_bytes())) and \
        "COM" in jpeg_metadata_segments(src.read_bytes())

    res = import_photos(folder, tmp_path / "out" / "sess_20261024_test")
    assert res.problems == []
    out = res.path / "stills" / "s1.jpg"
    data = out.read_bytes()
    assert jpeg_metadata_segments(data) == []
    with Image.open(out) as im:
        assert len(im.getexif()) == 0 and "comment" not in im.info and "exif" not in im.info
    assert b"Exif\x00\x00" not in data and b"iPhone" not in data and b"private note" not in data

    prov = read_json(res.path / "import.json")
    rec = prov["stills"][0]
    assert rec["gps_removed"] is True and {"EXIF", "GPS", "comment"} <= set(rec["metadata_removed"])
    assert prov["privacy"]["photos_with_gps_removed"] == 1
    assert "GPS" in prov["privacy"]["metadata_removed"]
    assert "37" not in json.dumps(rec["exif_read"])   # no coordinate is copied into the JSON either


def test_clean_jpeg_keeps_only_the_icc_profile():
    from PIL import ImageCms

    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    data = photos.clean_jpeg(gradient(), icc)
    assert jpeg_metadata_segments(data) == []
    with Image.open(io.BytesIO(data)) as im:
        assert im.info.get("icc_profile") == icc


def test_metadata_segment_scanner_names_xmp_and_rejects_non_jpeg():
    buf = io.BytesIO()
    Image.fromarray(gradient()).save(buf, "JPEG", xmp=b"<x:xmpmeta>place</x:xmpmeta>")
    assert any(s.startswith("APP1 http://ns") for s in jpeg_metadata_segments(buf.getvalue()))
    with pytest.raises(ValueError):
        jpeg_metadata_segments(b"\x89PNG....")


# --- pixels, orientation, intrinsics, time -----------------------------------------------------------------------

def test_orientation_is_applied_and_full_resolution_kept(folder, tmp_path):
    rgb = gradient(80, 60)
    write_photo(folder / "a.jpg", rgb, orientation=6, taken="2026:10:24 10:00:00")   # stored sideways
    res = import_photos(folder, tmp_path / "sess_20261024_rot")
    st = open_session(res.path).stills()[0]
    assert (st["width_px"], st["height_px"]) == (60, 80)
    with Image.open(res.path / st["image_file"]) as im:
        got = np.asarray(im.convert("RGB"), dtype=float)
    upright = np.rot90(rgb, k=-1).astype(float)          # orientation 6: turn 90 degrees clockwise to view
    assert np.abs(got - upright).mean() < 3
    assert read_json(res.path / "import.json")["stills"][0]["orientation_applied"] is True


def test_intrinsics_from_the_35mm_equivalent_focal_length(folder, tmp_path):
    write_photo(folder / "a.jpg", gradient(64, 48), f35=24, taken="2026:10:24 10:00:00")
    res = import_photos(folder, tmp_path / "sess_20261024_intr")
    st = open_session(res.path).stills()[0]
    f = 24 * math.hypot(64, 48) / math.hypot(36, 24)
    assert st["intrinsics"]["fx"] == pytest.approx(f, abs=1e-3) == st["intrinsics"]["fy"]
    assert (st["intrinsics"]["cx"], st["intrinsics"]["cy"]) == (32.0, 24.0)
    rec = read_json(res.path / "import.json")["stills"][0]
    assert rec["intrinsics_source"] == "exif" and "35 mm-equivalent" in rec["intrinsics_method"]


def test_focal_plane_resolution_fallback_and_unknown():
    exif = {photos.T_FOCAL_LENGTH: 5.0, photos.T_FPLANE_XRES: 400.0, photos.T_FPLANE_UNIT: 4,
            photos.T_PIXEL_X: 2000, photos.T_PIXEL_Y: 1500}
    intr, how = estimate_intrinsics(exif, 1000, 750)     # resized to half: the focal length in pixels halves too
    assert intr["fx"] == pytest.approx(5.0 * 400.0 / 2) and "focal-plane" in how
    assert estimate_intrinsics({photos.T_FOCAL_LENGTH: 5.0}, 1000, 750) == (None, None)


def test_photo_without_exif_has_no_intrinsics_and_still_validates(folder, tmp_path):
    Image.fromarray(gradient()).save(folder / "plain.jpg", "JPEG", quality=95)
    when = datetime(2026, 10, 20, 8, 30, tzinfo=UTC).timestamp()
    os.utime(folder / "plain.jpg", (when, when))
    res = import_photos(folder, tmp_path / "sess_20261024_noexif")
    assert res.problems == []
    st = open_session(res.path).stills()[0]
    assert "intrinsics" not in st and "camera" not in st
    assert st["captured_at"] == "2026-10-20T08:30:00.000Z"
    rec = read_json(res.path / "import.json")["stills"][0]
    assert rec["intrinsics_source"] == "unknown" and rec["captured_at_source"].startswith("file modification")


def test_capture_time_uses_the_offset():
    t, src = capture_time({photos.T_DATETIME_ORIGINAL: "2026:10:24 10:11:12", photos.T_OFFSET_ORIGINAL: "-07:00",
                           photos.T_SUBSEC_ORIGINAL: "250"}, Path(__file__))
    assert t == datetime(2026, 10, 24, 17, 11, 12, 250000, tzinfo=UTC) and "OffsetTimeOriginal" in src


def test_strip_sharpness_equals_whole_image_sharpness(monkeypatch):
    rgb = (np.random.default_rng(1).random((37, 29, 3)) * 255).astype(np.uint8)
    monkeypatch.setattr(photos, "SHARPNESS_STRIP_ROWS", 4)
    assert photos.sharpness(rgb) == pytest.approx(images.sharpness(rgb), rel=1e-9)


# --- the session --------------------------------------------------------------------------------------------------

def test_session_has_no_poses_no_depth_and_passes_the_validator(folder, tmp_path):
    write_photo(folder / "b.jpg", taken="2026:10:24 10:00:05")
    write_photo(folder / "a.jpg", gradient(48, 64), taken="2026:10:24 10:00:09")
    res = import_photos(folder, tmp_path / "sess_20261024_full", CaptureMeta(symptom="won't turn on"))
    assert res.problems == [] and validate_session(res.path) == [] and res.stills == 2
    s = open_session(res.path)
    (pkg,) = s.scans()
    assert pkg["depth_source"] == "none" and pkg["keyframes"] == [] and "points_file" not in pkg
    assert pkg["symptom"] == "won't turn on" and s.meta["symptom"] == "won't turn on"
    assert s.meta["source"] == "converter" and s.meta["device"] == {"model": "iPhone 15 Pro", "ios": "18.1"}
    stills = s.stills()
    for st in stills:
        assert st["kind"] == "unguided" and st["pose_reliable"] is False and "camera" not in st
        assert st["targets"] == [] and st["tracking_state"] == "unknown" and st["scan_id"] == pkg["scan_id"]
        assert st["sharpness"] > 0
    prov = read_json(res.path / "import.json")
    assert [r["source_file"] for r in prov["stills"]] == ["b.jpg", "a.jpg"]       # by capture time, not name
    assert pkg["captured_at"] == stills[0]["captured_at"] == "2026-10-24T08:00:05.000Z"
    assert any("location" in a for a in prov["assumptions"]) and prov["depth_source"] == "none"


def test_still_contract_allows_no_pose_only_when_pose_is_unreliable():
    ex = read_json(ROOT / "spec" / "contracts" / "examples" / "still.photo.json")
    contracts.validate("still", ex)
    ex.pop("intrinsics")
    contracts.validate("still", ex)
    for bad in ({**ex, "pose_reliable": True}, {k: v for k, v in ex.items() if k != "pose_reliable"}):
        with pytest.raises(Exception, match="camera|intrinsics"):
            contracts.validate("still", bad)


# --- folder handling ----------------------------------------------------------------------------------------------

def test_folder_skips_non_photos_hidden_files_subfolders_and_duplicates(folder, tmp_path):
    write_photo(folder / "IMG_1.JPG", taken="2026:10:24 10:00:00")
    shutil.copy(folder / "IMG_1.JPG", folder / "IMG_1 copy.jpeg")
    (folder / "IMG_1.MOV").write_bytes(b"\x00\x00\x00\x18ftypqt  ")
    (folder / "notes.txt").write_text("x")
    (folder / ".DS_Store").write_bytes(b"\x00")
    (folder / "._IMG_1.JPG").write_bytes(b"\x00")
    (folder / "sub").mkdir()
    write_photo(folder / "sub" / "IMG_9.JPG")
    found, skipped = list_photos(folder)
    assert [p.name for p in found] == ["IMG_1 copy.jpeg", "IMG_1.JPG"]
    assert {s["file"] for s in skipped} == {"IMG_1.MOV", "notes.txt", "sub"}

    res = import_photos(folder, tmp_path / "sess_20261024_skip")
    assert res.stills == 1 and res.problems == []
    prov = read_json(res.path / "import.json")
    assert prov["photos"]["found"] == 2 and prov["photos"]["imported"] == 1
    assert {"file": "IMG_1.JPG", "reason": "the same file as IMG_1 copy.jpeg"} in prov["photos"]["skipped"]


def test_folder_errors(tmp_path, folder):
    with pytest.raises(ImportFormatError, match="no JPEG or HEIC"):
        import_photos(folder, tmp_path / "sess_20261024_empty")
    (folder / "x.txt").write_text("x")
    with pytest.raises(ImportFormatError, match="not a folder"):
        import_photos(folder / "x.txt", tmp_path / "sess_20261024_file")
    write_photo(folder / "a.jpg")
    with pytest.raises(ImportFormatError, match="sess_"):
        import_photos(folder, tmp_path / "my-radio")
    taken = tmp_path / "sess_20261024_taken"
    taken.mkdir()
    (taken / "session.json").write_text("{}")
    with pytest.raises(FileExistsError):
        import_photos(folder, taken)


def test_unreadable_photo_fails_and_leaves_no_half_written_session(folder, tmp_path):
    write_photo(folder / "a.jpg")
    (folder / "b.jpg").write_bytes(b"\xff\xd8 not really a jpeg")
    out = tmp_path / "sess_20261024_broken"
    with pytest.raises(ImportFormatError, match="b.jpg"):
        import_photos(folder, out)
    assert not out.exists()


def test_default_out_is_a_new_session_under_out_root(folder, tmp_path):
    write_photo(folder / "a.jpg")
    res = import_photos(folder, None, out_root=tmp_path / "sessions")
    assert res.path.parent == tmp_path / "sessions" and res.session_id.startswith("sess_")


# --- HEIC ---------------------------------------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_SIPS, reason="HEIC conversion needs macOS sips")
def test_heic_is_converted_with_sips_and_stripped(folder, tmp_path):
    jpg = write_photo(tmp_path / "src.jpg", gradient(64, 48), orientation=6, taken="2026:10:24 10:00:00")
    os.system(f"sips -s format heic '{jpg}' --out '{folder / 'IMG_0002.HEIC'}' >/dev/null 2>&1")
    assert (folder / "IMG_0002.HEIC").is_file()
    res = import_photos(folder, tmp_path / "sess_20261024_heic")
    assert res.problems == []
    st = open_session(res.path).stills()[0]
    assert (st["width_px"], st["height_px"]) == (48, 64) and "intrinsics" in st
    data = (res.path / st["image_file"]).read_bytes()
    assert data.startswith(b"\xff\xd8") and jpeg_metadata_segments(data) == []
    rec = read_json(res.path / "import.json")["stills"][0]
    assert rec["source_format"] == "heic" and rec["gps_removed"] is True and "sips" in rec["converted_with"]


def test_heic_without_sips_says_what_to_do(folder, tmp_path, monkeypatch):
    (folder / "IMG_0003.HEIC").write_bytes(b"\x00\x00\x00\x18ftypheic")
    monkeypatch.setattr(photos.shutil, "which", lambda _name: None)
    with pytest.raises(ToolMissing, match="sips.*JPEG"):
        import_photos(folder, tmp_path / "sess_20261024_nosips")
    assert not (tmp_path / "sess_20261024_nosips").exists()


# --- the command --------------------------------------------------------------------------------------------------

def _cli():
    spec = importlib.util.spec_from_file_location("import_photos_cli", ROOT / "tools" / "import_photos.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_imports_and_prints_the_replay_command(folder, tmp_path, capsys):
    write_photo(folder / "a.jpg", taken="2026:10:24 10:00:00")
    out = tmp_path / "sess_20261024_cli"
    assert _cli().main([str(folder), "--out", str(out), "--symptom", "won't turn on"]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("ok ") and f"make replay SESSION={out}" in printed
    assert read_json(out / "session.json")["symptom"] == "won't turn on"


def test_cli_reports_a_bad_folder(tmp_path, capsys):
    assert _cli().main([str(tmp_path / "nope"), "--out", str(tmp_path / "sess_20261024_x")]) == 1
    assert "import failed" in capsys.readouterr().err
