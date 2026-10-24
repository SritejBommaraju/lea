"""Pipeline speed (2026-10-24): which stills are inspected (pipeline/select.py), one judgment per place on the scan
(pipeline/regions.py), the speed knobs (config/pipeline.yaml) and the worker's job order.

Regression facts from the opened-appliance scan of 2026-10-24 (22 stills of 12 MP, scan_799d9ab8d44e0e2b): the
ARKit poses, intrinsics, sharpness and subject distances of six of its stills are copied below as numbers. No photo
and no depth file is committed: each still's depth grid is a flat plane at its measured median subject distance.
Measured on that scan: stills 1-3 are one view (one to inspect), 12 and 13 one side, 22 another side."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future

import numpy as np
import pytest

from repairos.pipeline.config import load_pipeline_config
from repairos.pipeline.regions import SameRegions, box_in_world
from repairos.pipeline.select import near_duplicate, select_stills
from repairos.slots.detector import Detection
from repairos.vision.inspect import finding_from_other_still

W, H = 4032, 3024
# still number: (position_m, quaternion_xyzw, fx, cx, cy, sharpness, median subject distance m)
FACTS = {
    1: ([-0.0745, -0.1399, 0.0501], [-0.1967, -0.2186, -0.6512, 0.6996], 2894.1, 2018.1, 1511.5, 686, 0.48),
    2: ([0.0007, -0.1341, 0.0568], [-0.246, -0.1723, -0.6316, 0.7147], 2883.1, 2017.9, 1512.1, 1433, 0.52),
    3: ([0.0442, -0.1269, 0.0539], [-0.2842, -0.1235, -0.6209, 0.7201], 2883.1, 2017.8, 1512.1, 1292, 0.54),
    12: ([0.1457, 0.2819, -0.2613], [-0.6723, 0.0674, -0.1609, 0.7194], 2842.6, 2010.0, 1510.7, 836, 0.91),
    13: ([0.161, 0.2712, -0.3948], [-0.6694, 0.2182, -0.0269, 0.7096], 2841.5, 2011.2, 1512.6, 729, 0.86),
    22: ([-0.3468, 0.2771, -0.4201], [-0.3225, 0.6729, 0.6647, -0.0374], 2836.7, 2012.9, 1510.7, 1097, 0.96),
}


def still(n: int, *, segment: int = 0, kind: str = "unguided", pose: bool = True) -> dict:
    pos, q, fx, cx, cy, sharp, _ = FACTS[n]
    s = {"still_id": f"s{n}", "scan_id": "scan_t", "session_id": "sess_t", "kind": kind, "sharpness": sharp,
         "width_px": W, "height_px": H, "pose_reliable": pose, "captured_at": f"2026-10-24T17:36:{40 + n:02d}Z"}
    if pose:
        s["camera"] = {"frame": "arkit_world", "position_m": pos, "quaternion_xyzw": q}
        s["intrinsics"] = {"fx": fx, "fy": fx, "cx": cx, "cy": cy}
    if segment:
        s["segment"] = segment
    return s


def depth(s: dict) -> np.ndarray | None:
    n = int(s["still_id"][1:])
    return np.full((192, 256), FACTS[n][6], dtype=np.float32) if s.get("camera") else None


# ---------------------------------------------------------------------------------------------- still selection
def test_near_identical_stills_are_one_view_and_the_sharpest_is_inspected():
    sel = select_stills([still(n) for n in (1, 2, 3, 12, 13, 22)], depth, max_stills=10)
    assert "s2" in sel.kept and "s1" not in sel.kept and "s3" not in sel.kept
    assert sel.covered["s1"]["by"] == "s2" and sel.covered["s3"]["by"] == "s2"
    assert "near-identical view" in sel.covered["s1"]["why"] and "still s2" in sel.covered["s1"]["why"]
    assert {"s22"} <= set(sel.kept)                         # another side of the object: a new viewpoint
    assert set(sel.kept) | set(sel.covered) == {"s1", "s2", "s3", "s12", "s13", "s22"}


def test_every_covered_still_names_a_still_that_is_inspected():
    stills = [still(n) for n in (1, 2, 3, 12, 13, 22)]
    for cap in (1, 2, 3, 10):
        sel = select_stills(stills, depth, max_stills=cap)
        assert all(c["by"] in sel.kept for c in sel.covered.values()), cap
        assert not set(sel.kept) & set(sel.covered)


def test_the_cap_keeps_new_viewpoints_and_close_ups():
    sel = select_stills([still(n) for n in (1, 2, 3, 12, 13, 22)], depth, max_stills=1)
    assert "s22" in sel.kept and "s2" in sel.kept       # neither is seen half by any other kept view
    guided = select_stills([still(12), still(13, kind="guided")], depth, max_stills=1)
    assert "s13" in guided.kept                        # a deliberate close-up is never dropped by the cap


def test_stills_that_arrive_during_the_scan_are_decided_against_earlier_ones():
    kept: list[dict] = []
    decided = {}
    for n in (1, 2, 3, 12, 13, 22):                  # arrival order = capture order
        sel = select_stills([still(n)], depth, max_stills=10, already=kept)
        decided[n] = "kept" if sel.kept else sel.covered[f"s{n}"]["by"]
        if sel.kept:
            kept.append(still(n))
    assert decided[1] == "kept"
    assert decided[2] == "kept"     # twice as sharp as the kept near-duplicate: inspected as well
    assert decided[3] == "s2"       # nearly the same view as the sharper still 2
    assert decided[22] == "kept"


def test_a_still_without_pose_or_from_another_segment_is_never_called_covered():
    sel = select_stills([still(2), still(1, pose=False)], depth, max_stills=10)
    assert set(sel.kept) == {"s1", "s2"} and sel.unposed == {"s1": "no reliable pose"}
    a, b = still(2), still(1, segment=1)
    sel = select_stills([a, b], depth, max_stills=10)
    assert set(sel.kept) == {"s1", "s2"}               # poses of different segments are not compared


def test_near_duplicate_needs_the_same_scale():
    from repairos.pipeline.select import build_view

    from repairos.locate.camera import quat_to_matrix

    close = build_view(still(2), depth(still(2)))
    back = np.array(FACTS[2][0]) - quat_to_matrix(FACTS[2][1]) @ np.array([0.0, 0.0, -1.0]) * 0.7
    st = still(2)
    st = {**st, "still_id": "s2b", "camera": {**st["camera"], "position_m": [float(v) for v in back]}}
    far = build_view(st, np.full((192, 256), 0.52 + 0.7, dtype=np.float32))
    assert near_duplicate(close, build_view(still(3), depth(still(3))))
    assert not near_duplicate(close, far)              # same direction, from much farther: different detail


# ------------------------------------------------------------------------------------- one judgment per place
def test_a_box_is_placed_from_the_stills_own_depth():
    s = still(2)
    point, size = box_in_world(s, depth(s), (1918, 1412, 2118, 1612))   # 200 px at the image centre
    cam = np.array(FACTS[2][0])
    assert abs(np.linalg.norm(point - cam) - 0.52) < 0.01
    assert abs(size - 200 / FACTS[2][2] * 0.52) < 1e-3
    assert box_in_world(still(1, pose=False), None, (0, 0, 10, 10)) is None


def test_the_same_place_in_two_stills_is_judged_once():
    reg = SameRegions(0.02)
    s2, s3 = still(2), still(3)
    a, b = reg.for_still(s2, depth(s2)), reg.for_still(s3, depth(s3))
    e, mine = a.claim("s2", 0, (1918, 1412, 2118, 1612), register=True)
    assert mine
    placed = box_in_world(s2, depth(s2), (1918, 1412, 2118, 1612))
    other, mine = reg.claim("s3", 4, placed, register=True)
    assert other is e and not mine                       # same world point, same size, another still
    far, mine = b.claim("s3", 5, (100, 100, 300, 300), register=True)
    assert mine and far is not e                         # elsewhere in the photo: its own judgment
    tiny = (placed[0], placed[1] / 5)
    assert reg.claim("s3", 6, tiny, register=False) == (None, False)   # a much smaller part at the same spot
    assert reg.claim("s9", 7, placed, register=False, segment=1) == (None, False)   # other segment: never matched
    assert reg.claim("s3", 8, None, register=True) == (None, False)    # unplaced: never matched or registered


def _kept_finding(state: str = "insufficient_evidence") -> dict:
    return {"finding_id": "f_kept", "scan_id": "scan_t", "subject": {"kind": "part", "ref": "s2/det0"},
            "state": state, "source": "repairos.vision", "confidence": 0.35, "is_proof": False,
            "evidence": ["could not judge the wire in still s2", "vision model (m): blurred", "vision suspect, not proof"],
            "evidence_refs": {"still_ids": ["s2"], "crop_box_px": [1800, 1300, 2200, 1700]}, "design_trust": "low",
            "created_at": "2026-10-24T17:40:00Z"}


def test_a_reused_answer_says_where_it_was_judged_and_a_failed_one_is_not_inspected():
    reg = SameRegions(0.02)
    s2 = still(2)
    e, _ = reg.for_still(s2, depth(s2)).claim("s2", 0, (1918, 1412, 2118, 1612), register=True)
    e.resolve(_kept_finding())
    det = Detection(box=(1900, 1400, 2100, 1600), label="wire", score=0.4)
    f = finding_from_other_still(e, scan_id="scan_t", still_id="s3", index=2, det=det)
    assert f["state"] == "insufficient_evidence" and f["subject"]["ref"] == "s3/det2"
    assert f["finding_id"] != "f_kept" and "judged once, in the crop of s2/det0" in f["evidence"][1]
    bad = SameRegions(0.02)
    e2, _ = bad.for_still(s2, depth(s2)).claim("s2", 0, (1918, 1412, 2118, 1612), register=True)
    e2.fail(RuntimeError("Claude unreachable"))
    g = finding_from_other_still(e2, scan_id="scan_t", still_id="s3", index=2, det=det)
    assert g["state"] == "not_inspected" and "could not judge it" in g["evidence"][0]


def test_an_entry_answers_once():
    reg = SameRegions(0.02)
    s2 = still(2)
    e, _ = reg.for_still(s2, depth(s2)).claim("s2", 0, (1918, 1412, 2118, 1612), register=True)
    e.resolve({"state": "ok"})
    e.fail(RuntimeError("late"))                      # a second outcome is ignored
    assert isinstance(e.future, Future) and e.result(0) == {"state": "ok"}


# ------------------------------------------------------------------------------------------ knobs and worker
def test_speed_knobs_come_from_config(tmp_path, monkeypatch):
    cfg = load_pipeline_config()
    assert cfg["max_stills"] >= 1 and 1 <= cfg["vlm_calls_at_once"] <= 8 and cfg["detector_tile_px"] >= 1600
    bad = tmp_path / "p.yaml"
    bad.write_text("worker_jobs: 0\n")
    monkeypatch.setenv("REPAIROS_PIPELINE_CONFIG", str(bad))
    with pytest.raises(ValueError, match="worker_jobs"):
        load_pipeline_config()


def test_the_worker_claims_a_waiting_report_before_more_stills(tmp_path):
    from repairos.pipeline.worker import Worker
    from repairos.slots.blob_local import LocalBlobStore
    from repairos.slots.state import Job
    from repairos.slots.state_sqlite import SqliteState

    state = SqliteState(tmp_path / "state.sqlite3")

    async def go():
        for i in range(3):
            await state.enqueue(Job(kind="detect", job_id=f"job_detect_{i}", inputs={"kind": "still", "id": f"s{i}"}))
        await state.enqueue(Job(kind="pipeline", job_id="job_pipeline_x", inputs={"scan_id": "scan_x"}))
        w = Worker(state, LocalBlobStore(tmp_path / "blobs"), name="w", jobs_at_once=2)
        first, second = await w._claim(), await w._claim()
        return w, first, second
    w, first, second = asyncio.run(go())
    assert w.jobs_at_once == 2
    assert first.kind == "pipeline" and second.job_id == "job_detect_0"
