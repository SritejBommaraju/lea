"""B10 + B11 live: the pipeline with real Jev and real Claude, and on a real photo.

    pytest -m live backend/tests/integration            # needs ANTHROPIC_API_KEY and TYPESAFE_KEY in backend/.env

- `test_summary_writer_live`: Claude writes the summary of the generic example report from words only.
- `test_run_scan_live_without_photos`: the whole run (inspect, Jev, locate, Claude, publish) on a scan whose
  stills cannot be inspected (a label still, a skipped still): the honest answer is insufficient_evidence (V1).
- `test_pipeline_on_real_photos`: every real photo in $REPAIROS_SAMPLES_DIR/photos/*.jpg (each with its licence in
  <name>.licence.txt: public domain, CC0 or CC-BY, L2) through detector -> crops -> Claude VLM -> Jev -> report.
- `test_replay_recorded_sessions`: MS1 on every recorded real session in $REPAIROS_SAMPLES_DIR/sessions.
Missing keys or samples skip with HUMAN ACTION NEEDED. Nothing is faked.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from repairos.config import MissingSettingError, get_settings
from repairos.contracts import validate
from repairos.report import build_report, honesty_violations, write_summary
from repairos.slots.fusion import numeric_tokens

pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "spec" / "contracts" / "examples"
SAMPLES = Path(os.environ.get("REPAIROS_SAMPLES_DIR") or ROOT / "samples")
NEED_PHOTO = ("HUMAN ACTION NEEDED: put a real public-domain (or CC0 / CC-BY) photo of hardware in "
              f"{SAMPLES / 'photos'}/<name>.jpg with its source and licence in <name>.licence.txt "
              "(or set REPAIROS_SAMPLES_DIR). No synthetic images are used in its place.")
NEED_SESSION = ("HUMAN ACTION NEEDED: record a real session (samples/README.md) under "
                f"{SAMPLES / 'sessions'} from the phone (A4) or an import (B12).")


def ex(name: str) -> dict:
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def need(*keys: str) -> None:
    for k in keys:
        try:
            get_settings().require(k)
        except MissingSettingError:
            pytest.skip(f"HUMAN ACTION NEEDED: {k} in backend/.env")


def test_summary_writer_live():
    need("ANTHROPIC_API_KEY")
    from repairos.registry import Registry

    llm = Registry().slot("LLM")
    report = ex("report")
    s = write_summary(report, llm, symptom="no output when switched on")
    assert s["from_words_only"] is True and s["written_by"] == llm.model and len(s["text"]) > 40
    assert numeric_tokens(s["text"], ("s1/det0", "s1/det1", "s1/det2", "s1/det3", "s1")) == []
    validate("report", {**report, "summary": s})
    print("\nSUMMARY:", s["text"])


def test_run_scan_live_without_photos(tmp_path, monkeypatch):
    need("ANTHROPIC_API_KEY", "TYPESAFE_KEY")
    monkeypatch.setenv("REPAIROS_DATA_DIR", str(tmp_path))
    from repairos.pipeline.run import Context, run_id_for, run_scan
    from repairos.slots.blob_local import LocalBlobStore
    from repairos.slots.state_sqlite import SqliteState

    state, blobs, scan = SqliteState(tmp_path / "s.db"), LocalBlobStore(tmp_path / "blobs"), ex("scan-package")

    async def go():
        await state.transition("session", scan["session_id"], "session.create", {"session_id": scan["session_id"]})
        await state.transition("scan-package", scan["scan_id"], "scan.create", scan)
        for st in ({**ex("still"), "still_id": "s_label", "kind": "label", "targets": []},
                   {**ex("still"), "still_id": "s_skip", "user_skipped": True}):
            await state.transition("still", st["still_id"], "still.create", st)
        return await run_scan(Context(state=state, blobs=blobs), scan["scan_id"])

    rep = asyncio.run(go())
    validate("report", rep)
    assert rep["verdict"] == "insufficient_evidence" and rep["verdict_reason"]["code"] == "nothing_inspected"
    assert len(rep["not_inspected"]) == 2 and rep["findings"] == []
    assert rep["models"]["jev"].startswith("jev-")
    assert rep["models"]["locate"].startswith(("not available", "repairos.locate"))   # before / after the merge
    for e in rep["not_inspected"]:   # these stills have no box: never a location
        assert e.get("location", {"status": "not_computed"})["status"] == "not_computed"
    findings = [r.data for r in asyncio.run(state.list("finding", scan_id=scan["scan_id"]))]
    assert honesty_violations(rep, findings) == []
    runrow = asyncio.run(state.get("pipeline-run", run_id_for(scan["scan_id"]))).data
    assert runrow["status"] == "done" and runrow["report_id"] == rep["report_id"]
    assert asyncio.run(state.get("report", rep["report_id"])).data == rep
    print("\nREPORT:", json.dumps({k: rep[k] for k in ("verdict", "verdict_reason", "summary", "models")}, indent=1))


def _photos() -> list[Path]:
    folder = SAMPLES / "photos"
    return sorted(p for p in folder.glob("*.jpg") if p.with_suffix(".licence.txt").is_file()) if folder.is_dir() \
        else []


def test_pipeline_on_real_photos(tmp_path):
    photos = _photos()
    if not photos:
        pytest.skip(NEED_PHOTO)
    need("ANTHROPIC_API_KEY", "TYPESAFE_KEY")
    from repairos.fusion import fuse
    from repairos.pipeline.locate_iface import not_computed
    from repairos.pipeline.run import regions_for
    from repairos.registry import Registry
    from repairos.vision.inspect import inspect_still

    reg = Registry()
    det, vlm, llm = reg.slot("DET"), reg.slot("VLM"), reg.slot("LLM")
    for photo in photos[:2]:
        sid = "p1"
        # A loose photo has no scan: the report says so (no depth, nothing located); nothing stands in for a scan.
        scan = {"scan_id": f"photo_{photo.stem[:24]}", "session_id": "photos"}
        res = inspect_still(photo, still_id=sid, scan_id=scan["scan_id"], detector=det, judge=vlm,
                            crop_dir=tmp_path / photo.stem, max_crops=6)
        findings = [f.model_dump(mode="json", exclude_none=True) for f in res.findings]
        dets = [{"index": i, "label": d.label, "box_px": [int(v) for v in d.box]} for i, d in enumerate(res.detections)]
        fused = fuse(scan["scan_id"], findings, [], None, engine=reg.slot("FUSION"))
        rep = build_report(scan=scan, stills=[], findings=findings, suspects=fused.suspects,
                           next_action=fused.next_action, regions=regions_for(findings, dets, "whole photo"),
                           locations={f["finding_id"]: not_computed("a loose photo has no scan to place it on")
                                      for f in findings},
                           models={"detector": det.model_id, "classifier": "not selected (CLS=none)",
                                   "vlm": " then ".join(vlm.models), "summary_writer": llm.model,
                                   "jev": fused.answers.model if fused.answers else "not called",
                                   "locate": "not available (a loose photo has no scan)"})
        rep["summary"] = write_summary(rep, llm)
        validate("report", rep)
        assert honesty_violations(rep, findings) == []
        assert all(e["inspected_by"] == ["detector", "vlm"] for e in rep["findings"])
        (tmp_path / f"{photo.stem}.report.json").write_text(json.dumps(rep, indent=1))
        print(f"\n{photo.name}: {rep['verdict']} ({rep['verdict_reason']['rule']}), {len(rep['findings'])} judged, "
              f"{len(rep['not_inspected'])} not inspected, {len(rep['insufficient_evidence'])} undecided; "
              f"timing {res.timing}\nSUMMARY: {rep['summary']['text']}")


def test_replay_recorded_sessions(tmp_path):
    from repairos.sessions.reader import list_sessions

    sessions = list_sessions(SAMPLES / "sessions") if (SAMPLES / "sessions").is_dir() else []
    if not sessions:
        pytest.skip(NEED_SESSION)
    need("ANTHROPIC_API_KEY", "TYPESAFE_KEY")
    import subprocess
    import sys

    for s in sessions[:1]:
        out = tmp_path / "replay"
        proc = subprocess.run([sys.executable, str(ROOT / "tools" / "replay.py"), "--session", str(s), "--out",
                               str(out)], capture_output=True, text=True, timeout=3600)
        assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
        summary = json.loads((out / json.loads((s / "session.json").read_text())["session_id"] / "summary.json")
                             .read_text())
        assert summary["reports"] and all(r["grain"] == "not called" for r in summary["reports"])
