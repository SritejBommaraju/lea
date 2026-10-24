"""The person's own guess is a lead, not a fact (2026-10-24): POST /v1/sessions/{id}/story, the hypothesis, research
leads (Bright Data replaced by a function returning results in its documented organic shape), live `lead` events,
inspection order, and the report's `user_lead`. The vision model and Claude are never called: their two text calls
are replaced by functions returning answers in the schema the real calls enforce, and the crop judge used for the
ordering test records what it was asked. The example words are format fixtures, not recorded answers."""
from __future__ import annotations

import asyncio
import json

import pytest

from repairos.api.app import _named_event
from repairos.contracts import validate
from repairos.pipeline import story as S
from repairos.pipeline.run import Context, run_id_for
from repairos.references import brightdata as bd
from repairos.references import leads as L
from repairos.references import parts
from repairos.slots.detector import Detection
from repairos.slots.vlm import CropState
from repairos.slots.vlm_anthropic import STORY_SCHEMA, AnthropicVisionJudge
from repairos.vision import inspect as insp
from test_api_gateway import ex, gw, new_session  # noqa: F401 (fixture)
from test_report_fixes import TEST_SUMMARY

HYP = {"suspect": "USB port", "symptom": "does not charge", "event": "drop", "confidence_from_user": "think",
       "object_words": "charger"}


def run(coro):
    return asyncio.run(coro)


class StoryVLM:
    """The VLM slot's two text calls, answering in their schemas; records what it was given."""

    def __init__(self, hyp=None, leads=None):
        self.hyp, self.leads, self.calls = hyp or HYP, leads, []

    def story_hypothesis(self, text, identity=None):
        self.calls.append(("hypothesis", text))
        return {**self.hyp, "model_id": "quick"}

    def story_leads(self, hypothesis, identity, sources):
        self.calls.append(("leads", [s["title"] for s in sources]))
        return self.leads if self.leads is not None else [{"area": "charging board", "why": "Drops often crack it.",
                                                           "source": 0}]


# ------------------------------------------------------------------------------------------------- extraction
def test_the_hypothesis_call_quotes_the_story_and_keeps_only_allowed_words(monkeypatch):
    judge = AnthropicVisionJudge(models=["m-big"], effort={"m-big": "medium"}, checklists=False)
    seen = {}

    def ask(model, system, content, schema):
        seen.update(model=model, text=content[0]["text"], schema=schema)
        return {"answer": {"suspect": "  USB   port ", "symptom": "does not charge", "event": "dropped",
                           "confidence_from_user": "think", "object_words": "charger"}}
    monkeypatch.setattr(judge, "_ask", ask)
    out = judge.story_hypothesis("I think the <b>USB port</b> broke when I dropped it", {"category": "charger"})
    assert out["suspect"] == "USB port" and out["event"] == "unknown"       # not one of the allowed events
    assert out["confidence_from_user"] == "think" and seen["schema"] is STORY_SCHEMA
    assert "<data>" in seen["text"] and "<b>" not in seen["text"] and seen["model"] == judge.quick_model
    assert set(STORY_SCHEMA["properties"]["event"]["enum"]) == {"drop", "liquid", "heat", "wear", "unknown"}


def test_research_leads_cite_their_source_and_bad_indices_are_dropped(monkeypatch):
    judge = AnthropicVisionJudge(models=["m"], effort={"m": "low"}, checklists=False)
    monkeypatch.setattr(judge, "_ask", lambda *a: {"answer": {"leads": [
        {"area": "charging board", "why": "Drops crack it.", "source": 1},
        {"area": "cable", "why": "x", "source": 9}, {"area": "", "why": "y", "source": 0}]}})
    out = judge.story_leads(HYP, None, [{"title": "a", "link": "l0"}, {"title": "b", "link": "l1"}])
    assert out == [{"area": "charging board", "why": "Drops crack it.", "source": 1}]


# ------------------------------------------------------------------------------------------------- research
def test_queries_use_the_object_symptom_event_and_suspect():
    assert L.queries(HYP, {"category": "USB wall charger", "confidence": "medium"}) == [
        "USB wall charger does not charge drop common cause", "USB wall charger USB port failure"]
    assert L.queries(HYP, None)[1] == "charger USB port failure"                     # the person's own word for it
    assert L.queries({**HYP, "object_words": ""}, None) == []                       # nothing to search on
    assert L.queries(HYP, {"brand": "Acme", "model": "Q2", "category": "x", "confidence": "high"})[1] == \
        "Acme Q2 USB port failure"


def test_research_is_cached_and_charged_and_stops_at_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("REPAIROS_DATA_DIR", str(tmp_path))
    asked = []

    charged, real_charge = [], bd.charge
    monkeypatch.setattr(bd, "charge", lambda q: charged.append(q))

    def search(q, key, zone, timeout=0, charged_=False, **kw):
        asked.append(q)
        return [{"title": f"result for {q}", "link": f"https://r.example/{len(asked)}", "description": "d"}]
    monkeypatch.setattr(bd, "search", search)
    vlm = StoryVLM()
    out, rec = L.research(HYP, None, vlm, ("k", "z"))
    assert rec["status"] == "done" and len(asked) == 2
    assert out == [{"area": "charging board", "why": "Drops often crack it.", "from": "web",
                    "source_title": "result for charger does not charge drop common cause",
                    "source_link": "https://r.example/1"}]
    again, rec2 = L.research(HYP, None, vlm, ("k", "z"))
    assert again == out and rec2["cached"] is True and len(asked) == 2              # a day: no search, no charge
    assert charged == asked
    assert L.research(HYP, None, vlm, None) == ([], {"status": "off"})
    monkeypatch.setattr(bd, "charge", real_charge)   # the real guard again, for the cap below

    import sys
    import types

    class SpendLimit(RuntimeError):
        pass
    mod = types.ModuleType("repairos.spend")

    def charge(service, units=1.0, *, usd=None, key=None):
        raise SpendLimit("cap")
    mod.charge, mod.SpendLimit = charge, SpendLimit
    monkeypatch.setitem(sys.modules, "repairos.spend", mod)
    import repairos
    monkeypatch.setattr(repairos, "spend", mod, raising=False)
    out, rec = L.research({**HYP, "symptom": "hums"}, None, vlm, ("k", "z"))
    assert out == [] and rec["status"] == "limited" and len(asked) == 2            # skipped, no retry


def test_one_failed_search_leaves_the_other(tmp_path, monkeypatch):
    monkeypatch.setenv("REPAIROS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bd, "charge", lambda q: None)

    def search(q, key, zone, timeout=0, **kw):
        if "common cause" in q:
            raise json.JSONDecodeError("empty body", "", 0)
        return [{"title": "only this one", "link": "https://o.example", "description": "d"}]
    monkeypatch.setattr(bd, "search", search)
    out, rec = L.research(HYP, None, StoryVLM(), ("k", "z"))
    assert rec["status"] == "done" and out[0]["source_title"] == "only this one"
    monkeypatch.setattr(bd, "search", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("t")))
    out, rec = L.research({**HYP, "symptom": "buzzes"}, None, StoryVLM(), ("k", "z"))
    assert out == [] and rec["status"] == "failed" and rec["error"] == "TimeoutError"


# ------------------------------------------------------------------------------------------------- never a finding
def test_leads_only_reorder_regions():
    dets = [Detection(box=(0, 0, 10, 10), label="screw", score=0.9),
            Detection(box=(0, 0, 10, 10), label="usb port", score=0.5),
            Detection(box=(0, 0, 10, 10), label="button", score=0.7)]
    assert insp.prioritise([0, 2, 1], dets, ["the USB port"]) == [1, 0, 2]
    assert insp.prioritise([0, 2, 1], dets, None) == [0, 2, 1]
    assert sorted(insp.prioritise([0, 2, 1], dets, ["ports", "button"])) == [0, 1, 2]


class RecordingJudge:
    models = ("m",)

    def __init__(self):
        self.asked = []

    def inspect_crop(self, crop, context=None, part_hint=None, **kw):
        self.asked.append((part_hint, sorted(kw)))
        return CropState(part_class=part_hint or "part", state="ok", evidence_text="looks intact",
                         confidence_word="medium", model="m", latency_ms=1, severity="none", part_words="the part")


def test_a_lead_changes_what_is_looked_at_first_never_what_is_found():
    from PIL import Image

    im = Image.new("RGB", (1200, 900), (120, 120, 120))
    dets = [Detection(box=(100, 100, 400, 400), label="screw", score=0.9),
            Detection(box=(700, 300, 1000, 600), label="usb port", score=0.4)]
    out = {}
    for focus in (None, ["USB port"]):
        judge = RecordingJudge()
        res = insp.inspect_still(im, still_id="s1", scan_id="scan_t", detector=object(), judge=judge,
                                 detections=dets, max_crops=1, classifier=False, with_context=False, focus=focus)
        out[bool(focus)] = (judge.asked, {f.finding_id: f.state.value for f in res.findings})
    assert out[False][0] == [("screw", ["family"])] and out[True][0] == [("usb port", ["family"])]
    assert all("focus" not in kw and "hints" not in kw for asked in (out[False][0], out[True][0]) for _, kw in asked)
    for states in (out[False][1], out[True][1]):        # the same regions, one judged ok, one over budget
        assert sorted(states.values()) == ["not_inspected", "ok"]


# ------------------------------------------------------------------------------------------------- report section
def _rep(**over):
    rep = {"most_likely": ["f_top"], "findings": [
        {"finding_id": "f_top", "issue": "damaged", "part_class": "plug prongs", "part_words": "bent prong"},
        {"finding_id": "f_port", "issue": "none", "part_class": "usb port", "part_words": "the port on the side"}],
        "cosmetic": [], "insufficient_evidence": [], "not_inspected": []}
    rep.update(over)
    return rep


def _story(suspect="USB port", leads=None):
    return {"session_id": "s", "said": [{"text": "I think the USB port broke when I dropped it", "source": "voice"}],
            "hypothesis": {**HYP, "suspect": suspect, "status": "done"}, "leads": leads or []}


def test_user_lead_states():
    ul = L.user_lead(_story(), _rep())
    assert ul["checked"] == "not_seen" and ul["evidence_finding_ids"] == ["f_port"]
    assert ul["note"] == ("You thought the USB port was the problem. The close photo shows no visible issue there; "
                          "the more likely problem is the plug prongs (damaged).")
    broken = _rep(findings=[{"finding_id": "f_port", "issue": "broken", "part_class": "usb port"}], most_likely=["f_port"])
    ul = L.user_lead(_story(), broken)
    assert ul["checked"] == "confirmed" and ul["note"].endswith("A photo shows it broken.")
    unclear = _rep(findings=[_rep()["findings"][0]],
                   insufficient_evidence=[{"finding_id": "f_blur", "part_class": "port", "reason": "blurred"}])
    ul = L.user_lead(_story(), unclear)
    assert ul["checked"] == "could_not_check" and ul["evidence_finding_ids"] == ["f_blur"]
    nothing = L.user_lead(_story(suspect=""), _rep())
    assert nothing["checked"] == "could_not_check" and "suspect" not in nothing
    web = [{"area": "charging board", "why": "w", "from": "web", "source_title": "T", "source_link": "https://t.example"}]
    assert L.user_lead(_story(leads=web), _rep())["sources"] == [{"title": "T", "link": "https://t.example"}]
    assert L.user_lead({"said": []}, _rep()) is None
    for ul in (L.user_lead(_story(), _rep()), L.user_lead(_story(), broken), nothing):
        validate("report", {**ex("report"), "summary": TEST_SUMMARY, "user_lead": ul})


def test_a_shared_modifier_is_not_the_same_part():
    # seen live 2026-10-24: "USB port" must not match "two metal prongs on the white USB charger plug"
    assert not L.matches("USB port", "plug prongs", "two metal prongs on the white USB charger plug, upper one bent")
    assert L.matches("USB port", "USB port opening") and L.matches("the cord near the back", "power cord")
    assert L.head("power switch area") == "switch" and L.head("plug prongs") == "prong"
    rep = _rep(findings=[{"finding_id": "f_top", "issue": "damaged", "part_class": "plug prongs",
                          "part_words": "two metal prongs on the white USB charger plug"}],
               insufficient_evidence=[{"finding_id": "f_open", "part_class": "USB port opening"}])
    ul = L.user_lead(_story(), rep)
    assert ul["checked"] == "could_not_check" and ul["evidence_finding_ids"] == ["f_open"]
    assert ul["note"].endswith("the more likely problem is the plug prongs (damaged).")


def test_user_lead_cites_findings_and_never_adds_one():
    rep = _rep()
    before = json.dumps(rep, sort_keys=True)
    ul = L.user_lead(_story(), rep)
    assert json.dumps(rep, sort_keys=True) == before
    ids = {f["finding_id"] for k in ("findings", "cosmetic", "insufficient_evidence", "not_inspected") for f in rep[k]}
    assert set(ul["evidence_finding_ids"]) <= ids


# ------------------------------------------------------------------------------------------------- API + worker
def test_story_endpoint_records_the_words_and_queues_one_job(gw):  # noqa: F811
    sid = new_session(gw)
    assert gw.req("POST", f"/v1/sessions/{sid}/story", {"text": "x"}, auth=False)[0] == 401
    assert gw.req("POST", "/v1/sessions/sess_nope/story", {"text": "x"})[0] == 404
    status, out, _ = gw.req("POST", f"/v1/sessions/{sid}/story", {"text": "it smells burnt near the switch",
                                                                  "source": "voice"})
    assert status == 202 and out["said"] == 1
    again = gw.req("POST", f"/v1/sessions/{sid}/story", {"text": "it smells burnt near the switch", "source": "voice"})
    assert again[1]["said"] == 1 and again[1]["job"] == out["job"]                 # a retried post: nothing new
    row = run(gw.state.get("story", sid))
    assert row.data["said"][0]["text"] == "it smells burnt near the switch" and row.session_id == sid
    job = run(gw.state.get_job(out["job"]))
    assert job.kind == "story" and job.inputs == {"kind": "story", "id": sid}
    assert gw.req("POST", f"/v1/sessions/{sid}/story", {"text": ""})[0] == 422


def test_every_new_utterance_or_photo_queues_its_own_job_past_the_kept_lists(gw):  # noqa: F811
    sid = new_session(gw)
    jobs = [gw.req("POST", f"/v1/sessions/{sid}/story", {"text": f"detail {i}"})[1]["job"] for i in range(S.MAX_SAID + 3)]
    assert len(set(jobs)) == len(jobs)                                  # past MAX_SAID kept utterances too
    photos = [gw.upload(f"photo {i}".encode(), "image/jpeg") for i in range(5)]
    shown = [gw.req("POST", f"/v1/sessions/{sid}/identify", {"image": p})[1]["job"] for p in photos]
    assert len(set(shown + jobs)) == len(shown) + len(jobs)             # past the three kept photos too
    assert gw.req("POST", f"/v1/sessions/{sid}/identify", {"image": photos[-1]})[1]["job"] == shown[-1]   # a retry
    row = run(gw.state.get("story", sid)).data
    assert len(row["said"]) == S.MAX_SAID and S.said_seq(row) == S.MAX_SAID + 3
    assert len(row["photos"]) == 3 and S.photo_seq(row) == 5


def test_the_story_is_read_again_after_the_kept_list_is_full():
    said = [{"text": f"d{i}"} for i in range(S.MAX_SAID)]
    read_at_full = {"said_count": S.MAX_SAID, "said_seq": S.MAX_SAID}
    assert S.already_read({"said": said, "said_seq": S.MAX_SAID, "hypothesis": read_at_full})
    assert not S.already_read({"said": said, "said_seq": S.MAX_SAID + 1, "hypothesis": read_at_full})   # same length
    assert S.already_read({"said": [{"text": "a"}], "hypothesis": {"said_count": 1}})   # a row from before the counter


def test_the_story_job_sends_the_persons_lead_first_then_research_leads(gw, monkeypatch):  # noqa: F811
    sid = new_session(gw)
    st = gw.state
    run(st.transition("pipeline-run", run_id_for("scan_live"), "pipeline.update",
                      {"scan_id": "scan_live", "session_id": sid, "status": "inspecting"}))
    run(S.record_story(st, sid, "I think the USB port broke when I dropped it", "voice"))
    monkeypatch.setattr(parts, "credentials", lambda: ("k", "z"))
    monkeypatch.setenv("REPAIROS_DATA_DIR", str(gw.tmp))
    monkeypatch.setattr(bd, "search", lambda q, key, zone, timeout=0, **kw: [
        {"title": "Why chargers stop", "link": "https://w.example/a", "description": "d"}])
    ctx = Context(state=st, blobs=gw.blobs, root=gw.tmp / "pipeline")
    ctx._slots["VLM"] = StoryVLM()
    out = run(S.run_story(ctx, sid))
    assert out["leads"] == 2 and out["announced"] == 2 and out["research"] == "done"
    events = run(st.events_since(sid))
    named = [n[1] for n in (_named_event(e) for e in events) if n and n[0] == "lead"]
    assert named == [
        {"scan_id": "scan_live", "area": "USB port", "why": "you mentioned it (does not charge)", "source_title": "",
         "from": "you"},
        {"scan_id": "scan_live", "area": "charging board", "why": "Drops often crack it.",
         "source_title": "Why chargers stop", "from": "web"}]
    assert run(S.run_story(ctx, sid))["skipped"] == "already read"                 # same words: no second call
    runrow = run(st.get("pipeline-run", run_id_for("scan_live"))).data
    assert [x["area"] for x in runrow["leads"]["items"]] == ["USB port", "charging board"]
    assert not run(st.list("finding", scan_id="scan_live"))                         # a lead makes no finding
    # a scan that starts later gets the same leads, once each
    later = {"scan_id": "scan_later", "session_id": sid}
    assert run(S.apply_story(ctx, later)) == 2 and run(S.apply_story(ctx, later)) == 0


# ------------------------------------------------------------------------------------------------- pre-scan brief
class BriefVLM(StoryVLM):
    """Adds the photo identify call and the brief call (answers in their schemas)."""

    def __init__(self, label_brand="Acme", brief_brand="Acme", brief_model="Z9 Pro"):
        super().__init__()
        self.label_brand, self.brief_brand, self.brief_model = label_brand, brief_brand, brief_model

    def identify(self, images):
        self.calls.append(("identify", len(images)))
        return {"category": "electric trimmer", "brand": self.label_brand, "model": "", "confidence": "medium",
                "label_visible": True, "label_text": f"{self.label_brand} Z9".upper(), "model_id": "m"}

    def product_brief(self, identity, said, sources):
        self.calls.append(("brief", [s["title"] for s in sources]))
        return {"name": f"{self.brief_brand} {self.brief_model} trimmer", "category": "electric trimmer",
                "brand": self.brief_brand, "model": self.brief_model,
                "known_issues": [{"area": "blade guard", "why": "It cracks when dropped.", "source": 0},
                                 {"area": "charging port", "why": "Its contacts wear.", "source": 1}]}


def _results(q, key=None, zone=None, timeout=0, **kw):
    return [{"title": f"{q} result {i}", "link": f"https://b.example/{abs(hash(q)) % 999}/{i}", "description": "d"}
            for i in range(2)]


def test_brand_and_model_are_kept_only_as_read_or_said(tmp_path, monkeypatch):
    monkeypatch.setenv("REPAIROS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bd, "charge", lambda q: None)
    monkeypatch.setattr(bd, "search", _results)
    ident = {"category": "electric trimmer", "brand": "Acme", "label_text": "ACME Z9"}
    brief, rec = L.product_brief(ident, {}, "my acme trimmer", BriefVLM(), ("k", "z"))
    assert rec["status"] == "done" and brief["brand"] == "Acme"
    assert "model" not in brief      # "Z9 Pro" is neither on the label ("ACME Z9") nor said: left out, never invented
    assert [k["area"] for k in brief["known_issues"]] == ["blade guard", "charging port"]
    assert brief["known_issues"][0]["source_title"].endswith("result 0") and len(brief["sources"]) <= 5
    said = L.product_brief({"category": "electric trimmer"}, {"object_words": "Acme Z9 Pro trimmer"},
                           "it's an Acme Z9 Pro trimmer", BriefVLM(), ("k", "z"))[0]
    assert said["brand"] == "Acme" and said["model"] == "Z9 Pro"                       # said by the person
    assert L.product_name(None, {"object_words": ""}) == "" and L.product_brief(None, {}, "", BriefVLM(), ("k", "z"))[1] \
        == {"status": "nothing_to_search"}
    again = L.product_brief(ident, {}, "my acme trimmer", BriefVLM(), ("k", "z"))
    assert again[1]["cached"] is True


def test_a_session_with_no_scan_gets_a_product_brief_and_its_leads_steer_the_scan(gw, monkeypatch):  # noqa: F811
    import io

    from PIL import Image

    sid = new_session(gw)
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (90, 90, 90)).save(buf, "JPEG")
    ref = gw.upload(buf.getvalue(), "image/jpeg")
    assert gw.req("POST", f"/v1/sessions/{sid}/identify", {"image": "blob://sha256:" + "0" * 64})[0] == 422
    status, out, _ = gw.req("POST", f"/v1/sessions/{sid}/identify", {"image": ref})
    assert status == 202 and out["photos"] == 1
    assert gw.req("POST", f"/v1/sessions/{sid}/story", {"text": "it stopped cutting"})[0] == 202
    monkeypatch.setattr(parts, "credentials", lambda: ("k", "z"))
    monkeypatch.setenv("REPAIROS_DATA_DIR", str(gw.tmp))
    monkeypatch.setattr(bd, "charge", lambda q: None)
    monkeypatch.setattr(bd, "search", _results)
    ctx = Context(state=gw.state, blobs=gw.blobs, root=gw.tmp / "pipeline")
    vlm = BriefVLM()
    vlm.hyp = {**HYP, "suspect": "", "symptom": "stopped cutting", "event": "unknown", "object_words": ""}
    ctx._slots["VLM"] = vlm
    res = run(S.run_story(ctx, sid))
    assert res["brief"] == "done"
    assert [c[0] for c in vlm.calls][:3] == ["hypothesis", "identify", "brief"]
    named = [n for n in (_named_event(e) for e in run(gw.state.events_since(sid))) if n and n[0] == "product_brief"]
    assert len(named) == 1
    b = named[0][1]
    assert b["session_id"] == sid and b["brand"] == "Acme" and "model" not in b and b["category"] == "electric trimmer"
    assert [k["area"] for k in b["known_issues"]] == ["blade guard", "charging port"]
    # the scan starts later: the product's failure points arrive as leads, and make no finding
    assert run(S.apply_story(ctx, {"scan_id": "scan_new", "session_id": sid})) == 3
    leads = [n[1] for n in (_named_event(e) for e in run(gw.state.events_since(sid))) if n and n[0] == "lead"]
    assert [(x["area"], x["from"]) for x in leads] == [("blade guard", "product"), ("charging port", "product"),
                                                       ("charging board", "web")]   # the symptom's research lead
    assert not run(gw.state.list("finding", scan_id="scan_new"))
    assert run(S.run_story(ctx, sid))["skipped"] == "already read"                 # once per product
