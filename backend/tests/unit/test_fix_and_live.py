"""Demo (2026-10-24): "Fix it" parts and prices (repairos.references.parts, Bright Data SERP API), the live session
list (GET /v1/sessions), session ids for a pending scan's rows, and one live card per place in the Spacetime mirror.

Bright Data is never called: its HTTP request is replaced by a function that records the request and answers with a
payload in the shape Bright Data documents for the parsed JSON (docs.brightdata.com/products/serp-api/
parsed-json-results). The example values below are format fixtures, not a recorded response."""
from __future__ import annotations

import asyncio
import json
import urllib.parse

import pytest

from repairos.api.app import _named_event
from repairos.contracts import validate
from repairos.live import spacetime_bridge as sb
from repairos.pipeline.run import Context, attach_fix, run_id_for
from repairos.references import brightdata as bd
from repairos.references import parts
from repairos.slots.state_sqlite import SqliteState
from test_api_gateway import ex, gw, new_session  # noqa: F401 (fixture)
from test_pipeline import env, run, upd  # noqa: F401 (fixture)
from test_report_continuation import _publish
from test_report_fixes import TEST_SUMMARY

CREDS = ("key-not-real", "serp_zone")


def _report(**over) -> dict:
    """The report fields parts.part_for reads (object, most_likely, findings)."""
    rep = {"object": {"category": "kitchen appliance", "confidence_word": "medium", "label_visible": False},
           "most_likely": ["f_top"],
           "findings": [{"finding_id": "f_other", "issue": "worn", "part_class": "knob"},
                        {"finding_id": "f_top", "issue": "damaged", "part_class": "power_cord",
                         "part_words": "the cord near the back"}]}
    rep.update(over)
    return rep


def _shopping_payload() -> dict:
    return {"general": {"search_type": "shopping"},
            "shopping": [
                {"global_rank": 1, "title": "Cord A", "price": "$12.99", "shop": "Shop One",
                 "link": "https://shop-one.example/a", "image": "https://shop-one.example/a.jpg"},
                {"global_rank": 2, "title": "Cord B", "price": "from $5", "shop": "Shop Two",
                 "link": "https://shop-two.example/b"},                         # not a plain price: skipped
                {"global_rank": 3, "title": "Cord C", "price": "$1,049.50", "shop": "Shop One",
                 "link": "https://shop-one.example/c"},                         # same seller again: skipped
                {"global_rank": 4, "title": "Cord D", "price": "£8.00", "shop": "Shop Three",
                 "link": "https://shop-three.example/d", "description": "page text is never kept"}],
            "top_pla": [{"global_rank": 5, "title": "All results", "view_all": True, "price": "$1",
                         "link": "https://x.example/all"},
                        {"global_rank": 6, "title": "Cord E", "price": "$20.00", "shop": "Shop Four",
                         "link": "https://shop-four.example/e"}]}


class FakeBrightData:
    """Records each request body; answers the Shopping search with `shop`, the purchase-intent web search with `buy`
    and the guide search with `web` (bytes are sent as they are: b"" is the empty body Bright Data sometimes sends)."""

    def __init__(self, shop=None, web=None, buy=None):
        self.bodies: list[dict] = []
        self.shop = shop if shop is not None else _shopping_payload()
        self.web = web if web is not None else {"organic": [{"title": "Guide", "link": "https://guides.example/g"}]}
        self.buy = buy if buy is not None else {"organic": []}

    def __call__(self, body: dict, key: str, timeout: float) -> bytes:
        self.bodies.append(body)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(body["url"]).query)
        out = self.shop if q.get("tbm") == ["shop"] else self.buy if q["q"][0].startswith("buy ") else self.web
        return out if isinstance(out, bytes) else json.dumps(out).encode()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("REPAIROS_DATA_DIR", str(tmp_path))
    return tmp_path


# ------------------------------------------------------------------------------------------------- the query
def test_the_query_is_the_object_and_the_top_findings_generic_part():
    obj, part, f = parts.part_for(_report())
    assert (obj, part, f["finding_id"]) == ("kitchen appliance", "power cord", "f_top")
    named = _report(object={"category": "drill", "brand": "Acme", "model": "X1", "confidence_word": "high",
                            "label_visible": True})
    assert parts.part_for(named)[0] == "Acme X1"
    assert parts.part_for(_report(object={"category": "drill", "brand": "Acme", "confidence_word": "medium",
                                          "label_visible": False}))[0] == "Acme drill"
    assert parts.part_for(_report(most_likely=[])) is None                     # nothing to fix
    assert parts.part_for(_report(object={"category": "unknown", "confidence_word": "low",
                                          "label_visible": False})) is None    # nothing generic to search
    whole = _report(findings=[{"finding_id": "f_top", "issue": "damaged", "part_class": "whole photo"}])
    assert parts.part_for(whole) is None
    shop, buy, guide = parts.queries("kitchen appliance", "power cord")
    assert shop == "kitchen appliance power cord replacement" and buy.startswith("buy kitchen appliance")
    assert guide.endswith("replacement guide")


def test_request_asks_for_parsed_us_english_results():
    body = parts.request_body("a b", "serp_zone", shopping=True)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(body["url"]).query)
    assert body == {"zone": "serp_zone", "url": body["url"], "format": "raw"}   # parsed via brd_json=1 in the URL
    assert q == {"q": ["a b"], "hl": ["en"], "gl": ["us"], "num": ["10"], "tbm": ["shop"], "brd_json": ["1"]}
    web = parts.request_body("a b", "z", shopping=False)
    assert "tbm" not in web["url"] and "brd_json" not in web["url"] and web["data_format"] == "parsed"


def test_prices_parse_only_when_plain():
    assert parts.parse_price("$1,299.99") == (1299.99, "USD")
    assert parts.parse_price("£8.00") == (8.0, "GBP")
    assert parts.parse_price("US$ 4") == (4.0, "USD")
    for bad in ("from $5", "Free", "", None, "$0.00"):
        assert parts.parse_price(bad) is None


def test_options_keep_five_fields_one_per_seller_at_most_three():
    opts = parts.options_from(json.dumps(_shopping_payload()))
    assert opts == [
        {"title": "Cord A", "price": 12.99, "currency": "USD", "seller": "Shop One", "link": "https://shop-one.example/a"},
        {"title": "Cord D", "price": 8.0, "currency": "GBP", "seller": "Shop Three",
         "link": "https://shop-three.example/d"},
        {"title": "Cord E", "price": 20.0, "currency": "USD", "seller": "Shop Four",
         "link": "https://shop-four.example/e"}]
    wrapped = {"status_code": 200, "body": json.dumps({"organic": [
        {"title": "A page", "link": "https://www.seller.example/p", "description": "x"},
        {"title": "Same host", "link": "https://seller.example/q"},
        {"title": "", "link": "https://other.example"}, {"title": "Bad", "link": "javascript:alert(1)"}]})}
    assert parts.options_from(wrapped) == [{"title": "A page", "seller": "seller.example",
                                            "link": "https://www.seller.example/p"}]   # no price: a web result


# ------------------------------------------------------------------------------------------------- search + cache
def test_search_is_cached_per_object_and_part_and_off_without_a_key(data_dir, monkeypatch):
    fake = FakeBrightData()
    monkeypatch.setattr(parts, "_post", fake)
    monkeypatch.setattr(parts, "credentials", lambda: None)
    assert parts.find_fix(_report()) == (None, {"status": "off"}) and fake.bodies == []

    fix, rec = parts.find_fix(_report(), creds=CREDS)
    assert rec["status"] == "done" and rec["cached"] is False and len(fake.bodies) == 3
    assert fix["part"] == "power cord" and len(fix["options"]) == 3 and fix["guide_link"] == "https://guides.example/g"
    validate("report", {**ex("report"), "fix": fix})
    stored = json.loads(next((data_dir / "cache" / "fix").glob("*.json")).read_text())
    assert set(stored["fix"]) == {"part", "options", "guide_link", "searched_at"}
    assert "page text" not in json.dumps(stored) and "a.jpg" not in json.dumps(stored)   # no page content, no image

    again, rec2 = parts.find_fix(_report(), creds=CREDS)
    assert again == fix and rec2["cached"] is True and len(fake.bodies) == 3           # within 24 h: no request
    assert parts.cached("kitchen appliance", "power cord", now=__import__("time").time() + 25 * 3600) is None


def test_an_empty_shopping_answer_falls_back_to_the_purchase_search(data_dir, monkeypatch):
    buy = {"shopping": [{"title": "Cord Z", "price": "$9.99", "shop": "Shop Nine", "link": "https://nine.example/z"}],
           "organic": [{"title": "A listing", "link": "https://listing.example/l"}]}
    monkeypatch.setattr(parts, "_post", FakeBrightData(shop=b"", buy=buy))
    fix, rec = parts.find_fix(_report(), creds=CREDS)
    assert rec["status"] == "done" and [o["seller"] for o in fix["options"]] == ["Shop Nine"]
    monkeypatch.setattr(parts, "_post", FakeBrightData(shop=b"", buy={"organic": buy["organic"]}))
    fix, _ = parts.find_fix(_report(object={"category": "fan", "confidence_word": "high", "label_visible": False}),
                            creds=CREDS)
    assert fix["options"] == [{"title": "A listing", "seller": "listing.example", "link": "https://listing.example/l"}]


class SpendLimit(RuntimeError):
    def __init__(self, service, reason="cap reached", retry_after_s=3600):
        super().__init__(reason)
        self.service, self.reason, self.retry_after_s = service, reason, retry_after_s


@pytest.fixture
def spend(monkeypatch):
    """A stand-in for repairos.spend (another task adds it): records charges; `limit` makes the next charge raise."""
    import sys
    import types

    mod = types.ModuleType("repairos.spend")
    mod.calls, mod.limit = [], False
    mod.SpendLimit = SpendLimit

    def charge(service, units=1.0, *, usd=None, key=None):
        if mod.limit:
            raise SpendLimit(service)
        mod.calls.append((service, key))
    mod.charge = charge
    monkeypatch.setitem(sys.modules, "repairos.spend", mod)
    import repairos
    monkeypatch.setattr(repairos, "spend", mod, raising=False)
    return mod


def test_every_request_is_charged_first_and_the_cache_never_is(data_dir, monkeypatch, spend):
    fake = FakeBrightData()
    monkeypatch.setattr(parts, "_post", fake)
    fix, rec = parts.find_fix(_report(), creds=CREDS)
    assert rec["status"] == "done" and len(spend.calls) == 3 == len(fake.bodies)
    assert {s for s, _ in spend.calls} == {"brightdata"} and len({k for _, k in spend.calls}) == 3   # per query
    assert "kitchen" not in json.dumps(spend.calls)                                                  # a hash
    parts.find_fix(_report(), creds=CREDS)
    assert len(spend.calls) == 3                                                    # cached: not charged again


def test_at_the_spend_cap_the_search_is_skipped_not_retried(data_dir, monkeypatch, spend):
    fake = FakeBrightData()
    monkeypatch.setattr(parts, "_post", fake)
    spend.limit = True
    fix, rec = parts.find_fix(_report(), creds=CREDS)
    assert fix is None and rec["status"] == "limited" and fake.bodies == []
    assert not (data_dir / "cache" / "fix").exists() or not list((data_dir / "cache" / "fix").glob("*.json"))
    spend.limit = False
    assert parts.find_fix(_report(), creds=CREDS)[1]["status"] == "done"            # not cached as "nothing"
    with pytest.raises(SpendLimit):
        spend.limit = True
        bd.search("q", *CREDS)


def test_no_results_and_errors_add_nothing(data_dir, monkeypatch):
    monkeypatch.setattr(parts, "_post", FakeBrightData(shop={"shopping": []}, web={"organic": []}, buy=b""))
    assert parts.find_fix(_report(), creds=CREDS) == (None, {"status": "none", "cached": False,
                                                            "seconds": pytest.approx(0, abs=1)})

    def boom(body, key, timeout):
        raise TimeoutError("timed out")
    monkeypatch.setattr(parts, "_post", boom)
    fix, rec = parts.find_fix(_report(object={"category": "lamp", "confidence_word": "high",
                                              "label_visible": False}), creds=CREDS)
    assert fix is None and rec["status"] == "failed" and rec["error"] == "TimeoutError"
    assert "key-not-real" not in json.dumps(rec)


def test_a_search_under_way_is_shared_not_repeated(data_dir, monkeypatch):
    import threading

    gate, calls = threading.Event(), []

    def slow(body, key, timeout):
        calls.append(body)
        gate.wait(5)
        return FakeBrightData()(body, key, timeout)
    monkeypatch.setattr(parts, "_post", slow)
    assert parts.prewarm("kitchen appliance", "power cord", CREDS) is True
    assert parts.prewarm("kitchen appliance", "power cord", CREDS) is False            # already under way
    got = {}
    t = threading.Thread(target=lambda: got.update(r=parts.find_fix(_report(), creds=CREDS)))
    t.start()
    __import__("time").sleep(0.2)
    gate.set()
    t.join(5)
    fix, rec = got["r"]
    assert len(calls) == 3 and rec.get("shared") is True and len(fix["options"]) == 3
    assert parts.prewarm("kitchen appliance", "power cord", CREDS) is False            # cached now


def test_the_live_top_part_is_the_most_pressing_issue_so_far():
    from repairos.pipeline.run import live_top_part

    run_row = {"stills": {"s1": {"regions": {"f1": {"part_class": "knob"}, "f2": {"part_class": "power_cord"},
                                             "f3": {"part_class": "whole photo"}, "f4": {"part_class": "lid"}}}}}
    fs = [{"finding_id": "f1", "state": "worn", "severity": "functional", "confidence": 0.9},
          {"finding_id": "f2", "state": "damaged", "severity": "safety", "confidence": 0.5},
          {"finding_id": "f3", "state": "damaged", "severity": "safety", "confidence": 0.9},
          {"finding_id": "f4", "state": "ok", "severity": "safety"}]
    assert live_top_part(fs, run_row) == "power cord"
    assert live_top_part(fs[:1], run_row) == "knob" and live_top_part(fs[2:], run_row) is None


# ------------------------------------------------------------------------------------------------- pipeline
def _done_task(value):
    async def f():
        return value
    return asyncio.ensure_future(f())


def test_a_ready_fix_is_added_to_the_published_report_with_a_live_event(env, tmp_path):  # noqa: F811
    state, blobs, scan = env
    from repairos.pipeline.run import inspect_still_stage

    ctx = Context(state=state, blobs=blobs, root=tmp_path / "pipeline")
    for s in ("s_label", "s_skip"):
        run(inspect_still_stage(ctx, s))
    published = _publish(state, scan)
    fix = {"part": "power cord", "searched_at": "2026-10-24T20:00:00Z",
           "options": [{"title": "Cord A", "price": 12.99, "currency": "USD", "seller": "Shop One",
                        "link": "https://shop-one.example/a"}]}

    async def go():
        return await attach_fix(ctx, scan, published, _done_task((fix, {"status": "done", "cached": False,
                                                                        "seconds": 3.2})))
    out = run(go())
    assert out == {"scan_id": scan["scan_id"], **fix}
    row = run(state.get("report", published.id))
    assert row.rev == published.rev + 1 and row.data["fix"] == fix
    assert {k: v for k, v in row.data.items() if k != "fix"} == published.data          # nothing else changed
    events = run(state.events_since(scan["session_id"]))
    named = [n for n in (_named_event(e) for e in events) if n and n[0] == "fix_ready"]
    assert named == [("fix_ready", {"scan_id": scan["scan_id"], **fix, "status": "done"})]
    runrow = run(state.get("pipeline-run", run_id_for(scan["scan_id"]))).data
    assert runrow["fix"]["status"] == "done" and runrow["announce"] == "fix_ready"


def test_a_fix_for_an_older_report_is_dropped_and_none_is_not_announced(env, tmp_path):  # noqa: F811
    state, blobs, scan = env
    from repairos.pipeline.run import inspect_still_stage

    ctx = Context(state=state, blobs=blobs, root=tmp_path / "pipeline")
    for s in ("s_label", "s_skip"):
        run(inspect_still_stage(ctx, s))
    first = _publish(state, scan)
    run(state.transition("report", first.id, "report.publish", {**first.data, "created_at": "2026-10-24T21:00:00Z"}))
    fix = {"part": "knob", "searched_at": "2026-10-24T20:00:00Z",
           "options": [{"title": "Knob", "seller": "seller.example", "link": "https://seller.example/k"}]}

    async def go(value):
        return await attach_fix(ctx, scan, first, _done_task(value))
    assert run(go((fix, {"status": "done"}))) is None
    assert "fix" not in run(state.get("report", first.id)).data
    assert run(state.get("pipeline-run", run_id_for(scan["scan_id"]))).data["fix"]["status"] == "stale"
    assert run(go((None, {"status": "limited"}))) is None
    named = [n for n in (_named_event(e) for e in run(state.events_since(scan["session_id"]))) if n and n[0] == "fix_ready"]
    assert named == [("fix_ready", {"scan_id": scan["scan_id"], "status": "limited", "options": []})]   # ends the wait
    assert "fix" not in run(state.get("report", first.id)).data


def test_report_schema_takes_a_fix_with_or_without_prices():
    rep = {**ex("report"), "summary": TEST_SUMMARY}
    validate("report", {**rep, "fix": {"part": "p", "searched_at": "2026-10-24T20:00:00Z",
                                       "options": [{"title": "t", "seller": "s", "link": "https://s.example"}]}})
    import jsonschema
    with pytest.raises(jsonschema.ValidationError):   # a price needs its currency
        validate("report", {**rep, "fix": {"part": "p", "searched_at": "2026-10-24T20:00:00Z",
                                           "options": [{"title": "t", "seller": "s", "link": "l", "price": 3}]}})
    with pytest.raises(jsonschema.ValidationError):   # at most three
        validate("report", {**rep, "fix": {"part": "p", "searched_at": "2026-10-24T20:00:00Z",
                                           "options": [{"title": "t", "seller": "s", "link": "l"}] * 4}})


# ------------------------------------------------------------------------------------------------- (a) pending scan
def test_rows_of_a_pending_scan_carry_the_stills_session(tmp_path):
    state = SqliteState(tmp_path / "state.sqlite3")
    still = {**ex("still"), "scan_id": "scan_pending1"}
    sid = still["session_id"]

    async def go():
        await state.transition("session", sid, "session.create", {"session_id": sid})
        await state.transition("still", still["still_id"], "still.create", still)
        assert await state.get("scan-package", "scan_pending1") is None      # the scan package is not posted yet
        f = {**ex("finding"), "finding_id": "f_pending", "scan_id": "scan_pending1",
             "evidence_refs": {"still_ids": [still["still_id"]], "crop_box_px": [0, 0, 10, 10]}}
        await state.transition("finding", "f_pending", "finding.create", f)
        return await state.get("finding", "f_pending"), await state.events_since(sid)
    row, events = run(go())
    assert row.session_id == sid
    assert [e.id for e in events if e.kind == "finding"] == ["f_pending"]


# ------------------------------------------------------------------------------------------------- (b) sessions
def test_sessions_list_is_newest_activity_first_and_needs_a_device(gw):  # noqa: F811
    older, newer = new_session(gw), new_session(gw)
    assert gw.req("GET", "/v1/sessions", auth=False)[0] == 401
    status, out, _ = gw.req("GET", "/v1/sessions?limit=5")
    assert status == 200 and [s["session_id"] for s in out["sessions"]][:2] == [newer, older]
    run(gw.state.transition("pipeline-run", "run_scan_x", "pipeline.update",
                            {"scan_id": "scan_x", "session_id": older, "status": "inspecting"}))   # older is active
    out = gw.req("GET", "/v1/sessions?limit=1")[1]
    assert [s["session_id"] for s in out["sessions"]] == [older]
    assert out["sessions"][0]["last_event_id"] > 0 and out["sessions"][0]["last_event_at"]
    assert gw.req("GET", "/v1/sessions?limit=0")[0] == 422


def test_bridge_discovers_sessions_from_the_gateway_only(gw, monkeypatch):  # noqa: F811
    sid = new_session(gw)
    monkeypatch.setenv("REPAIROS_DEVICE_TOKEN", gw.token)
    disc = sb.Discovery(sb.Gateway(f"http://127.0.0.1:{gw.port}", gw.tmp / "nowhere"), lookback_s=600)
    assert disc.start() == [(sid, 0)]
    assert disc.poll() == []
    run(gw.state.transition("pipeline-run", "run_scan_y", "pipeline.update",
                            {"scan_id": "scan_y", "session_id": sid, "status": "inspecting"}))
    other = new_session(gw)
    assert set(disc.poll()) == {sid, other}


# ------------------------------------------------------------------------------------------------- (c) one card a place
def _finding_event(fid: str, still: str, box: list[int], state: str = "damaged", severity: str = "functional"):
    return {"kind": "finding", "row": {"finding_id": fid, "scan_id": "scan_d", "state": state, "severity": severity,
                                       "part_words": f"words {fid}",
                                       "evidence_refs": {"still_ids": [still], "crop_box_px": box}}}


def test_the_mirror_shows_one_live_card_per_place():
    out = sb.Outbox()
    m = sb.Mirror(out, None, 8)
    m.handle("sess", {"kind": "still", "row": {"still_id": "s2", "scan_id": "scan_d"}, "at": "t0"})
    m.handle("sess", _finding_event("f_a", "s2", [10, 10, 50, 50]))
    m.handle("sess", _finding_event("f_b", "s2", [10, 10, 50, 50], severity="safety"))  # reused in another still
    m.handle("sess", _finding_event("f_c", "s2", [60, 10, 90, 50]))                     # another place
    keys = [k for k in out._items if k.startswith("f:")]
    assert keys == ["f:scan_d:f_a", "f:scan_d:f_c"]
    card = out._items["f:scan_d:f_a"][1][2]
    assert card["finding_id"] == "f_a" and card["severity"] == "safety" and card["words"] == "words f_a"
    assert sb.place_key({"finding_id": "f_x", "evidence_refs": {}}) == "id:f_x"


def test_report_rows_another_row_stands_for_are_left_out():
    out = sb.Outbox()
    m = sb.Mirror(out, None, 8)
    rep = {"report_id": "rep_scan_d", "verdict": "potential_issues", "findings": [
        {"finding_id": "f_a", "issue": "damaged", "finding_ids": ["f_a", "f_b"]},
        {"finding_id": "f_b", "issue": "damaged"},
        {"finding_id": "f_c", "issue": "worn", "superseded_ids": ["f_d"]},
        {"finding_id": "f_d", "issue": "worn"},
        {"finding_id": "f_e", "issue": "none"}]}
    m.handle("sess", {"kind": "report", "row": {**rep, "scan_id": "scan_d"}, "rev": 1, "at": "t1"})
    rows = out._items["rep:scan_d"][1][12]
    assert [r["finding_id"] for r in rows] == ["f_a", "f_c"]
