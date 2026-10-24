"""Stills posted while the scan goes on (a pending scan), and more photos added to a scan from its report (the scan
package posted again). Through the real gateway on a local port, as test_api_gateway does; bodies are the public
contract examples, files are format-only fixtures (not captures)."""
from __future__ import annotations

import asyncio
import copy

import pytest

from repairos.pipeline.run import Context, inspect_still_stage, run_id_for
from repairos.slots.state import InvariantViolation
from test_api_gateway import ex, gw, make_scan, make_still, new_session  # noqa: F401 (fixture)


def _finding_for(still: dict, n: int = 0) -> dict:
    return {**ex("finding"), "finding_id": f"f_early_{n}", "scan_id": still["scan_id"],
            "evidence_refs": {"still_ids": [still["still_id"]]}}


def test_a_still_before_its_scan_package_is_a_pending_scan_and_is_inspected_at_once(gw):  # noqa: F811
    sid = new_session(gw)
    pkg = make_scan(gw, sid)                      # files uploaded, package not posted yet (the scan is going on)
    st = make_still(gw, pkg)
    status, out, _ = gw.req("POST", "/v1/stills", st)
    assert status == 201 and out["scan_pending"] is True and out["jobs"]
    job = asyncio.run(gw.state.get_job(out["jobs"][0]))
    assert job.kind == "detect" and job.inputs == {"kind": "still", "id": st["still_id"]}
    # inspection may store findings and claim the still before the package exists ...
    asyncio.run(gw.state.transition("finding", "f_early_0", "finding.create", _finding_for(st)))
    asyncio.run(gw.state.transition("pipeline-run", run_id_for(pkg["scan_id"]), "pipeline.claim_still",
                                    {"still_id": st["still_id"], "worker": "w", "scan_id": pkg["scan_id"],
                                     "session_id": sid}))
    # ... but nothing that needs the scan: fusion output, readings and the report still need the package (REF)
    sus = {**ex("suspect"), "scan_id": pkg["scan_id"]}
    with pytest.raises(InvariantViolation, match="REF"):
        asyncio.run(gw.state.transition("suspect", sus["suspect_id"], "suspect.update_from_fusion", sus))
    rd = {**ex("meter-reading"), "session_id": sid, "scan_id": pkg["scan_id"]}
    assert gw.req("POST", "/v1/readings", rd)[0] == 422
    assert gw.req("POST", f"/v1/scans/{pkg['scan_id']}/complete")[0] == 404   # no report run before the package
    # the package arrives at Done: the still is linked by its scan_id
    status, posted, _ = gw.req("POST", "/v1/scans", pkg)
    assert status == 201 and posted["updated"] is False
    status, got, _ = gw.req("GET", f"/v1/scans/{pkg['scan_id']}")
    assert status == 200 and [s["still_id"] for s in got["stills"]] == [st["still_id"]]
    assert gw.req("POST", f"/v1/scans/{pkg['scan_id']}/complete")[0] == 202
    st2 = make_still(gw, pkg)
    st2["still_id"] = "still_after_done"
    assert gw.req("POST", "/v1/stills", st2)[1]["scan_pending"] is False


def test_a_pending_still_is_validated_in_full_and_needs_its_session(gw):  # noqa: F811
    sid = new_session(gw)
    pkg = make_scan(gw, sid)
    small = make_still(gw, pkg, size=(1920, 1440))
    assert gw.req("POST", "/v1/stills", small)[0] == 422            # still checks are unchanged
    bad = make_still(gw, pkg)
    bad.pop("width_px")
    assert gw.req("POST", "/v1/stills", bad)[0] == 422              # contract validation unchanged
    orphan = make_still(gw, pkg)
    orphan["session_id"] = "sess_nobody"
    assert gw.req("POST", "/v1/stills", orphan)[0] == 404           # a pending scan needs a real session
    # a finding of a pending scan must name a still of that scan
    with pytest.raises(InvariantViolation, match="REF"):
        asyncio.run(gw.state.transition("finding", "f_x", "finding.create",
                                        {**ex("finding"), "finding_id": "f_x", "scan_id": pkg["scan_id"],
                                         "evidence_refs": {"still_ids": ["no_such_still"]}}))


def test_the_package_must_come_under_the_session_its_stills_were_posted_in(gw):  # noqa: F811
    sid, other = new_session(gw), new_session(gw)
    pkg = make_scan(gw, sid)
    st = make_still(gw, pkg)
    assert gw.req("POST", "/v1/stills", st)[0] == 201
    moved = {**pkg, "session_id": other}
    status, out, _ = gw.req("POST", "/v1/scans", moved)
    assert status == 422 and "posted for this scan under session" in str(out)


def test_inspection_of_a_pending_still_runs_before_the_package(gw, tmp_path):  # noqa: F811
    """The worker's inspect stage on a still whose scan package is not posted: a label still (the OCR stage, no
    model needed) ends with its findings and a done claim on the run row of the pending scan."""
    sid = new_session(gw)
    pkg = make_scan(gw, sid)
    st = make_still(gw, pkg)
    st.update(still_id="s_label", kind="label", targets=[])
    assert gw.req("POST", "/v1/stills", st)[1]["scan_pending"] is True
    ctx = Context(state=gw.state, blobs=gw.blobs, root=tmp_path / "pipeline")
    stage = asyncio.run(inspect_still_stage(ctx, "s_label", decide=True))
    assert stage["status"] == "done" and stage["finding_ids"]
    run = asyncio.run(gw.state.get("pipeline-run", run_id_for(pkg["scan_id"]))).data
    assert run["stills"]["s_label"]["status"] == "done" and run["session_id"] == sid


def test_posting_the_scan_package_again_adds_to_the_scan(gw):  # noqa: F811
    sid = new_session(gw)
    pkg = make_scan(gw, sid)
    status, first, _ = gw.req("POST", "/v1/scans", pkg)
    assert status == 201 and first["rev"] == 1 and first["updated"] is False
    status, retry, _ = gw.req("POST", "/v1/scans", copy.deepcopy(pkg))
    assert status == 201 and retry["rev"] == 1                       # an identical retry changes nothing
    more = copy.deepcopy(pkg)
    more["keyframes"] = more["keyframes"] + [{**more["keyframes"][0], "keyframe_id": "kf_more"}]
    more["segments"] = [{"segment": 1, "reason": "continued", "aligned": False}]
    status, again, _ = gw.req("POST", "/v1/scans", more)
    assert status == 200 and again["updated"] is True and again["rev"] == 2
    assert gw.req("GET", f"/v1/scans/{pkg['scan_id']}")[1]["scan"] == more
    assert gw.req("POST", "/v1/scans", {**more, "session_id": new_session(gw)})[0] == 422   # REF: another session
    st = make_still(gw, more)
    st.update(still_id="s_more", segment=1)
    status, out, _ = gw.req("POST", "/v1/stills", st)
    assert status == 201 and out["scan_pending"] is False
    # complete again: a new pipeline job for the new set of stills, the report is re-made
    status, c1, _ = gw.req("POST", f"/v1/scans/{pkg['scan_id']}/complete")
    assert status == 202 and c1["stills"] == 1


def test_photos_of_an_unaligned_segment_are_not_placed_on_the_scan():
    from repairos.pipeline.run import NOT_ALIGNED, unaligned_segments

    scan = {"segments": [{"segment": 1, "reason": "continued", "aligned": False},
                         {"segment": 2, "reason": "continued"}]}
    assert unaligned_segments(scan) == {1} and "not placed on the scan" in NOT_ALIGNED
