"""B6: session format reader, validator, writer round trip, held-out guard and the replay runner."""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from repairos.sessions import files, ids
from repairos.sessions.reader import HeldoutSession, open_session
from repairos.sessions.validate import validate_session
from repairos.sessions.writer import SessionWriter

from format_files import jpeg_header, ply_bytes

ROOT = Path(__file__).resolve().parents[3]


def test_reader_yields_contract_objects(format_session):
    s = open_session(format_session)
    assert s.session_id == format_session.name and s.split == "train"
    [pkg] = s.scans()
    assert pkg["point_count"] == 4 and s.file(pkg["points_file"]).is_file()
    assert [st["still_id"] for st in s.stills()] == ["s7"]
    assert len(s.readings()) == 1 and s.parts() == []
    assert s.truth["negatives"] is False


def test_valid_format_session_has_no_problems(format_session):
    assert validate_session(format_session) == []


def test_heldout_session_raises(format_session):
    meta = json.loads((format_session / "session.json").read_text())
    (format_session / "session.json").write_text(json.dumps({**meta, "split": "heldout"}))
    with pytest.raises(HeldoutSession):
        open_session(format_session)
    assert open_session(format_session, allow_heldout=True).split == "heldout"


@pytest.mark.parametrize("break_it, expect", [
    (lambda d: (d / "scans/scan_7f3a/points_arkit_world.ply").write_bytes(ply_bytes([(0, 0, 0)])), "vertices"),
    (lambda d: (d / "scans/scan_7f3a/points_arkit_world.ply").write_bytes(
        ply_bytes([(0, 0, 0)] * 4).replace(b"binary_little_endian", b"ascii")), "binary_little_endian"),
    (lambda d: (d / "stills/s7.jpg").write_bytes(jpeg_header(1920, 1440)), "declared 4032x3024"),
    (lambda d: (d / "stills/s7.jpg").unlink(), "missing"),
    (lambda d: (d / "stills/s7.jpg").write_bytes(b"\x89PNG not a jpeg"), "not a JPEG"),
    (lambda d: (d / "truth.json").write_text('{"faults": [], "negatives": true}'), "not_inspected_expected"),
    (lambda d: (d / "readings/rd_0003.json").write_text('{"reading_id": "rd_0003"}'), "readings/rd_0003.json"),
])
def test_validator_catches(format_session, break_it, expect):
    break_it(format_session)
    problems = validate_session(format_session)
    assert any(expect in p for p in problems), problems


def test_writer_round_trip(format_session, tmp_path):
    src = open_session(format_session)
    w = SessionWriter(tmp_path / "out", src.session_id)
    fetch = lambda rel: (src.file(rel).read_bytes(), Path(rel).suffix)  # noqa: E731
    w.write_session(src.meta)
    for pkg in src.scans():
        w.write_scan(pkg, fetch)
    for st in src.stills():
        w.write_still(st, fetch)
    for rd in src.readings():
        w.write_reading(rd)
    w.write_truth(src.truth)
    assert validate_session(w.dir) == []
    assert open_session(w.dir).scans() == src.scans()


def test_capture_file_checks():
    assert files.jpeg_size(io.BytesIO(jpeg_header(4032, 3024))) == (4032, 3024)
    data = ply_bytes([(0, 0, 0), (1, 1, 1)])
    assert files.ply_header(io.BytesIO(data), len(data))["vertex_count"] == 2
    with pytest.raises(files.CaptureFileError):
        files.ply_header(io.BytesIO(data[:-1]), len(data) - 1)


def test_ids_are_opaque():
    assert ids.is_session_id(ids.new_session_id())
    assert ids.OBJECT_RE.match(ids.new_object_id()) and ids.new_scan_id().startswith("scan_")


def test_replay_runs_offline(format_session, tmp_path):
    out = tmp_path / "replay"
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "replay.py"), "--session", str(format_session),
                        "--out", str(out)], capture_output=True, text=True, timeout=60)
    # The fixture's files are format-only (a JPEG header, no picture): the pipeline (B10) cannot inspect the still,
    # so the run fails at that stage, clearly, and no report is made up in its place.
    assert r.returncode == 1, r.stdout + r.stderr
    summary = json.loads((out / format_session.name / "summary.json").read_text())
    assert summary["counts"] == {"scans": 1, "stills": 1, "readings": 1, "parts": 0}
    assert [s["stage"] for s in summary["stages"]] == ["validate", "ingest", "pipeline"]
    pipe = summary["stages"][2]
    assert pipe["status"] == "failed" and "stage inspect (still s7)" in pipe["error"]
    assert summary["reports"] == []
    assert summary["outcome"]["missed"]  # nothing has looked at it yet: the induced fault is missed, not invented
    check = subprocess.run([sys.executable, str(ROOT / "tools" / "replay.py"), "--check", "--session",
                            str(format_session)], capture_output=True, text=True, timeout=60)
    assert check.returncode == 0, check.stdout


def test_replay_refuses_an_invalid_session(format_session, tmp_path):
    shutil.rmtree(format_session / "stills")
    (format_session / "scans/scan_7f3a/points_arkit_world.ply").unlink()
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "replay.py"), "--session", str(format_session),
                        "--out", str(tmp_path / "replay")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "missing" in r.stdout


@pytest.mark.replay
def test_recorded_session_is_valid(recorded_session):
    assert validate_session(recorded_session) == []
