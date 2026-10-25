"""The Lea -> Grain handoff's own rules, without calling Grain: only verified hole spacings become fit requirements
(everything else is an unknown with its number), findings keep Lea's words and no made-up location, photos follow
the evidence, geometry falls back mesh -> cloud -> none, the app never sees blob refs, and the client refuses what
Grain would refuse before sending it."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image

from repairos.handoff import manifest as mf
from repairos.handoff.service import payload
from repairos.slots.grain import GrainError
from repairos.slots.grain_http import MAX_PHOTO_BYTES, MAX_SCAN_BYTES, GrainHTTP

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api_gateway import gw, make_scan, make_still, new_session  # noqa: E402,F401  (the real gateway)

MEASURED = {"dimensions": [
    {"name": "spacing_x", "kind": "distance", "between": ["h1", "h2"], "value_mm": 48.26, "uncertainty_mm": 0.2,
     "verified": True, "method": "parallax", "evidence_still_ids": ["s3"], "fit": "spacing"},
    {"name": "spacing_y", "kind": "distance", "between": ["h1", "h3"], "value_mm": 40.6, "uncertainty_mm": 0.9,
     "verified": False, "method": "surface", "evidence_still_ids": ["s4"], "fit": {"role": "spacing"}},
    {"name": "width", "kind": "distance", "between": ["a", "b"], "value_mm": 68.6, "uncertainty_mm": 0.0,
     "verified": True, "method": "given", "evidence_still_ids": ["s1"]}],
    "failed": [{"name": "hole_d", "kind": "diameter", "error": "a diameter needs at least three rim points"}],
    "check": {"tolerance_mm": 0.4, "passed": True, "notes": ["x"]}}

REPORT = {"object": {"category": "circuit board", "confidence_word": "high", "label_visible": False},
          "findings": [
              {"finding_id": "f1", "issue": "broken", "part_words": "the corner mounting tab", "part_class": "bracket",
               "confidence_word": "medium", "evidence": ["tab snapped at the hole"],
               "crop": {"still_id": "s2", "box_px": [0, 0, 9, 9]},
               "location": {"status": "computed", "point_m": [0.1, 0.0, -0.2]}},
              {"finding_id": "f2", "issue": "none", "part_class": "board", "confidence_word": "high",
               "evidence": ["looked fine"], "crop": {"still_id": "s5", "box_px": [0, 0, 9, 9]}},
              {"finding_id": "f3", "issue": "loose", "category": "setup", "part_class": "cable",
               "confidence_word": "low", "evidence": ["unplugged"], "crop": {"still_id": "s6", "box_px": [0, 0, 9, 9]}},
              {"finding_id": "f4", "issue": "worn", "part_class": "connector", "confidence_word": "low",
               "evidence": ["pins dull"], "crop": {"still_id": "s7", "box_px": [0, 0, 9, 9]},
               "location": {"status": "not_computed", "reason": "no depth"}}]}

STILLS = [{"still_id": f"s{k}", "kind": "label" if k == 0 else "guided", "view_cell": f"r1s{k % 4:02d}"}
          for k in range(12)]


def test_only_verified_spacings_become_fit_requirements():
    fit, unknowns = mf.fit_and_unknowns(MEASURED, ["the screw size"])
    assert fit == {"tolerance_mm": 0.4,
                   "spacings": [{"between": ["h1", "h2"], "distance_mm": 48.26, "name": "spacing_x"}]}
    assert unknowns[0] == "the screw size"
    assert any("spacing_y (measured 40.6 ± 0.9 mm, not verified" in u for u in unknowns)
    assert any(u.startswith("hole_d (could not be measured") for u in unknowns)
    assert mf.fit_and_unknowns(None) == (None, [])


def test_findings_keep_leas_words_and_no_made_up_location():
    f = mf.findings(REPORT)
    assert [x["id"] for x in f] == ["f1", "f4"]          # no `none`, no setup finding
    assert f[0]["subject"] == "the corner mounting tab" and f[0]["location_m"] == [0.1, 0.0, -0.2]
    assert f[1]["subject"] == "connector" and f[1]["location_m"] is None


def test_photos_follow_the_evidence_then_the_sides():
    picked = mf.choose_photos(mf.findings(REPORT), STILLS, MEASURED)
    assert picked[:4] == ["s2", "s7", "s3", "s4"] and len(picked) == mf.MAX_PHOTOS
    assert "s0" not in picked                            # the label still comes last, and eight are already chosen
    assert len(set(picked)) == len(picked)


def test_the_manifest_maps_evidence_to_photo_numbers():
    photos = [{"n": 1, "still_id": "s2", "sha256": "a" * 64, "original_sha256": "b" * 64},
              {"n": 2, "still_id": "s7", "sha256": "c" * 64, "original_sha256": "d" * 64}]
    m = mf.build(handoff_id="gh_x_1", scan={"scan_id": "x", "session_id": "sess"}, report=REPORT, revision=1,
                 objective="a mounting plate", geometry=None, photos=photos, measurement=MEASURED,
                 grain_project_id="p1", grain_scan_id="g1", unknowns=[])
    assert m["schema_version"] == 1 and m["geometry"] is None and m["grain"] == {"project_id": "p1", "scan_id": "g1"}
    assert [f["evidence_photos"] for f in m["findings"]] == [[1], [2]]
    assert m["fit"]["spacings"][0]["distance_mm"] == 48.26 and len(m["fit"]["spacings"]) == 1
    assert {d["name"] for d in m["dimensions"]} == {"spacing_x", "spacing_y", "width"}
    assert "notes" not in m["measurement_check"] and m["object"] == {"category": "circuit board"}
    msg = mf.first_message(m)
    assert "inspect_repair_capture with scan_id 'g1'" in msg and "2 verified dimension(s)" in msg
    assert "spacing_y" in msg                            # its unknown is said up front


def test_geometry_falls_back_from_mesh_to_cloud_to_none():
    mesh = {"status": "done", "ref": "blob://m", "format": "ply", "cloud_ref": "blob://c"}
    assert mf.geometry_choice(mesh, lambda r: 10) == ("blob://m", "triangle_mesh", "gateway_mesh")
    assert mf.geometry_choice(mesh, lambda r: MAX_SCAN_BYTES + 1 if r == "blob://m" else 10)[1] == "point_cloud"
    assert mf.geometry_choice({"status": "unavailable"}, lambda r: 10) is None
    assert mf.geometry_choice(None, lambda r: 10) is None


def test_a_photo_is_sent_smaller_and_as_jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (4032, 3024), "red").save(buf, format="PNG")
    out = mf.photo_derivative(buf.getvalue())
    with Image.open(io.BytesIO(out)) as im:
        assert im.format == "JPEG" and max(im.size) == mf.PHOTO_MAX_PX


def test_the_app_sees_revisions_without_blob_refs():
    row = {"scan_id": "x", "handoff_id": "gh_x_1", "objective": "o", "stage": "done", "grain": {"project_id": "p"},
           "revisions": [{"n": 1, "build_ok": True, "files": {"out/model.step": "blob://sha256:" + "a" * 64}}]}
    p = payload(row)
    assert p["revisions"][0]["files"] == ["out/model.step"] and "blob://" not in str(p)


def test_the_client_refuses_what_grain_would_refuse_before_sending():
    g = GrainHTTP(base_url="http://127.0.0.1:9", key="")       # nothing listens there; nothing must be sent
    with pytest.raises(GrainError) as e:
        g.upload_scan("p", "s.ply", b"x" * (MAX_SCAN_BYTES + 1), "m")
    assert e.value.status == 413
    with pytest.raises(GrainError) as e:
        g.upload_photo("p", None, "a.jpg", b"x" * (MAX_PHOTO_BYTES + 1))
    assert e.value.status == 413
    with pytest.raises(ValueError):
        g.write_file("p", "out/model.step", "x")
    with pytest.raises(GrainError) as e:
        g.project("p")
    assert e.value.status is None and e.value.transient


def test_the_gateway_refuses_a_design_it_cannot_send(gw, monkeypatch):
    monkeypatch.delenv("REPAIROS_PROFILE", raising=False)
    monkeypatch.delenv("GRAIN_URL", raising=False)
    pkg = make_scan(gw, new_session(gw))
    gw.req("POST", "/v1/scans", pkg)
    status, out, _ = gw.req("POST", f"/v1/scans/{pkg['scan_id']}/grain", {"objective": "a mounting plate"})
    assert status == 503 and "HUMAN ACTION NEEDED" in out["detail"]["reason"]
    assert gw.req("GET", f"/v1/scans/{pkg['scan_id']}/grain")[0] == 404          # nothing was queued


def test_the_gateway_queues_one_design_per_objective_and_follows_it(gw, monkeypatch):
    monkeypatch.setenv("REPAIROS_PROFILE", "phase2")
    monkeypatch.setenv("GRAIN_URL", "http://127.0.0.1:9")
    pkg = make_scan(gw, new_session(gw))
    assert gw.req("POST", "/v1/scans", pkg)[0] in (200, 201)
    sid = pkg["scan_id"]
    assert gw.req("GET", f"/v1/scans/{sid}/grain")[0] == 404
    status, out, _ = gw.req("POST", f"/v1/scans/{sid}/grain", {"objective": "a mounting plate", "unknowns": ["screws"]})
    assert status == 202 and out["stage"] == "queued" and out["job"].startswith("job_grain_")
    again = gw.req("POST", f"/v1/scans/{sid}/grain", {"objective": "a mounting plate"})[1]
    assert again["handoff_id"] == out["handoff_id"]                       # the same objective is the same design
    other = gw.req("POST", f"/v1/scans/{sid}/grain", {"objective": "a new enclosure"})[1]
    assert other["handoff_id"] != out["handoff_id"]
    assert gw.req("GET", f"/v1/scans/{sid}/grain")[1]["objective"] == "a new enclosure"
    status, out, _ = gw.req("POST", f"/v1/scans/{sid}/grain/answer", {"text": "M3"})
    assert status == 409                                                    # Grain is not asking
    assert gw.req("GET", f"/v1/scans/{sid}/grain/revisions/1/model.step")[0] == 404


def test_measuring_says_why_when_no_still_can_measure(gw):
    pkg = make_scan(gw, new_session(gw))
    gw.req("POST", "/v1/scans", pkg)
    st = make_still(gw, pkg)
    assert gw.req("POST", "/v1/stills", st)[0] in (200, 201, 202)
    mark = [{"still_id": st["still_id"], "uv": [10, 10]}]
    req = {"points": {"a": mark, "b": mark}, "dimensions": [{"name": "d", "kind": "distance", "between": ["a", "b"]}]}
    status, out, _ = gw.req("POST", f"/v1/scans/{pkg['scan_id']}/measure", req)
    assert status == 422 and "no still can measure" in out["detail"]["reason"]


@pytest.mark.parametrize("bad", [
    {"points": {"a": [{"still_id": "s"}]}, "dimensions": [{"name": "d", "between": ["a", "a"]}]},       # no uv
    {"points": {"a": [{"still_id": "s", "uv": [1, 2]}]}, "dimensions": [{"name": "d", "between": ["a"]}]},
    {"points": {"a": [{"still_id": "s", "uv": [1, 2]}], "b": [{"still_id": "s", "uv": [3, 4]}]},
     "dimensions": [{"name": "d", "between": ["a", "b"]}], "reference": {"dimension": "d", "true_mm": 0}},
    {"points": {"a": [{"still_id": "s", "uv": [1, 2]}], "b": [{"still_id": "s", "uv": [3, 4]}]},
     "dimensions": [{"name": "d", "between": ["a", "b"], "value_mm": 48.0}]},                 # numbers are computed
    {"points": {"a": [{"still_id": "s", "uv": [1, 2]}]}, "dimensions": [{"name": "d", "between": ["a", "z"]}]},
])
def test_a_malformed_measuring_request_is_refused_before_anything_runs(gw, bad):
    pkg = make_scan(gw, new_session(gw))
    gw.req("POST", "/v1/scans", pkg)
    assert gw.req("POST", f"/v1/scans/{pkg['scan_id']}/measure", bad)[0] == 422


# ---------------------------------------------------------------- the durable row, on Lea's real store (no Grain call)
class _Ctx:
    """The worker context the handoff reads: the real store; any Grain call fails the test."""

    def __init__(self, state):
        self.state = state

    def slot(self, name):
        raise AssertionError(f"{name} must not be called here")


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_a_failed_design_resumes_at_the_step_that_failed_under_a_new_attempt(tmp_path):
    from repairos.handoff import service
    from repairos.slots.state_sqlite import SqliteState
    st = SqliteState(tmp_path / "s.sqlite3")
    scan = {"scan_id": "scan_r", "session_id": None}
    row = _run(service.create(st, scan, "a plate", None, []))
    first = service.job_key(row)
    r = _run(st.get(service.KIND, row["handoff_id"]))
    failed = {**r.data, "stage": "failed", "resume_stage": "project", "error": "Grain 400: bad scan",
              "grain": {"project_id": "p1"}}
    _run(st.put(service.KIND, failed, expect_rev=r.rev))
    again = _run(service.create(st, scan, "a plate", None, []))
    assert again["stage"] == "project" and again["error"] is None and again["attempt"] == 2
    assert service.job_key(again) != first                         # a new job, so the queue runs it
    assert _run(service.create(st, scan, "a plate", None, []))["attempt"] == 2   # running: the same attempt


def test_a_live_lease_keeps_a_second_run_away_from_grain(tmp_path):
    import time

    from repairos.handoff import service
    from repairos.slots.state_sqlite import SqliteState
    st = SqliteState(tmp_path / "s.sqlite3")
    row = _run(service.create(st, {"scan_id": "scan_l"}, "a plate", None, []))
    r = _run(st.get(service.KIND, row["handoff_id"]))
    _run(st.put(service.KIND, {**r.data, "lease": {"token": "other", "until": time.time() + 60}}, expect_rev=r.rev))
    out = _run(service.run_handoff(_Ctx(st), row["handoff_id"]))   # _Ctx fails on any Grain call
    assert out["skipped"] and out["stage"] == "queued"


def test_a_finished_design_is_not_run_again(tmp_path):
    from repairos.handoff import service
    from repairos.slots.state_sqlite import SqliteState
    st = SqliteState(tmp_path / "s.sqlite3")
    row = _run(service.create(st, {"scan_id": "scan_d"}, "a plate", None, []))
    r = _run(st.get(service.KIND, row["handoff_id"]))
    _run(st.put(service.KIND, {**r.data, "stage": "done"}, expect_rev=r.rev))
    assert _run(service.run_handoff(_Ctx(st), row["handoff_id"]))["stage"] == "done"
