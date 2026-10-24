"""STATE conformance (task B4): the same assertions for every implementation.

    pytest backend/tests/conformance/state [--impl sqlite]

Rows are the public contract examples (spec/contracts/examples, generic placeholder ids). No product data.
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path

import pytest

from repairos.slots.state import InvariantViolation, Job, JobQueue, LiveEvents, NotFound, RevConflict, StateStore

EXAMPLES = Path(__file__).resolve().parents[4] / "spec" / "contracts" / "examples"
ORDER = ["scan-package", "still", "meter-reading", "finding", "finding.partly", "suspect", "next-action",
         "repair-plan", "part-on-hand", "reference-design", "shopping-quote"]
KIND_OF = {"finding.partly": "finding"}
ID = {"scan-package": "scan_id", "still": "still_id", "meter-reading": "reading_id", "finding": "finding_id",
      "suspect": "suspect_id", "next-action": "action_id", "repair-plan": "plan_id", "part-on-hand": "item_id",
      "reference-design": "product_id", "shopping-quote": "quote_id"}


def run(coro):
    return asyncio.run(coro)


def ex(name: str) -> dict:
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


async def seed(s, upto: str | None = None) -> dict[str, dict]:
    """Session row, then contract examples in dependency order (stops after `upto`)."""
    sess = ex("scan-package")["session_id"]
    await s.transition("session", sess, "session.create", {"session_id": sess})
    out = {}
    for name in ORDER:
        d = ex(name)
        kind = KIND_OF.get(name, name)
        await s.put(kind, d)
        out[name] = d
        if name == upto:
            break
    return out


def test_implements_protocols(impl):
    assert isinstance(impl, StateStore) and isinstance(impl, JobQueue) and isinstance(impl, LiveEvents)


def test_round_trip_every_contract_example(impl):
    async def go():
        rows = await seed(impl)
        for name, d in rows.items():
            kind = KIND_OF.get(name, name)
            got = await impl.get(kind, d[ID[kind]])
            assert got is not None and got.data == d and got.rev == 1, name
        assert {r.id for r in await impl.list("finding", scan_id="scan_7f3a")} == {ex("finding")["finding_id"], ex("finding.partly")["finding_id"]}
        sess = ex("scan-package")["session_id"]
        assert len(await impl.list("still", session_id=sess)) == 1
    run(go())


def test_rev_conflict(impl):
    async def go():
        await seed(impl, upto="scan-package")
        d = ex("scan-package")
        with pytest.raises(RevConflict):
            await impl.put("scan-package", d, expect_rev=0)
        assert await impl.put("scan-package", {**d, "symptom": "x"}, expect_rev=1) == 2
        with pytest.raises(RevConflict):
            await impl.put("scan-package", d, expect_rev=1)
    run(go())


def test_schema_and_reference_rejects(impl):
    async def go():
        bad = ex("scan-package")
        del bad["units"]
        with pytest.raises(InvariantViolation) as e:
            await impl.put("scan-package", bad)
        assert e.value.rule == "SCHEMA"
        with pytest.raises(InvariantViolation) as e:  # no session row yet
            await impl.put("scan-package", ex("scan-package"))
        assert e.value.rule == "REF"
    run(go())


def test_create_is_idempotent_and_conflicts_on_different_content(impl):
    async def go():
        await seed(impl, upto="scan-package")
        st = ex("still")
        a = await impl.transition("still", st["still_id"], "still.create", st)
        b = await impl.transition("still", st["still_id"], "still.create", copy.deepcopy(st))
        assert a.rev == b.rev == 1
        with pytest.raises(RevConflict):
            await impl.transition("still", st["still_id"], "still.create", {**st, "sharpness": 1.0})
    run(go())


def test_i1_is_proof_only_from_the_meter(impl):
    async def go():
        await seed(impl, upto="meter-reading")
        f = ex("finding")
        with pytest.raises(InvariantViolation) as e:
            await impl.transition("finding", f["finding_id"], "finding.create", {**f, "is_proof": True})
        assert e.value.rule == "I1"
        meter = {**f, "finding_id": "f_m1", "source": "repairos.meter", "is_proof": True, "evidence_refs": {}}
        with pytest.raises(InvariantViolation):  # a meter finding must point at a stored reading
            await impl.transition("finding", "f_m1", "finding.create", meter)
        meter["evidence_refs"] = {"reading_id": ex("meter-reading")["reading_id"]}
        assert (await impl.transition("finding", "f_m1", "finding.create", meter)).data["is_proof"] is True
    run(go())


def test_i2_electrical_confirm_needs_a_proof_reading(impl):
    async def go():
        await seed(impl, upto="meter-reading")
        sus = ex("suspect")  # electrical subject, verdict suspect
        assert sus["subject"]["kind"] in ("wire", "net")
        with pytest.raises(InvariantViolation) as e:
            await impl.transition("suspect", sus["suspect_id"], "suspect.update_from_fusion", {**sus, "verdict": "confirmed"})
        assert e.value.rule == "I2"
        with pytest.raises(InvariantViolation) as e:
            await impl.put("suspect", {**sus, "verdict": "cleared", "proof_reading_id": None})
        assert e.value.rule == "I2"
        await impl.transition("suspect", sus["suspect_id"], "suspect.update_from_fusion", sus)
        rd = ex("meter-reading")  # verdict disagrees: proves the fault
        with pytest.raises(InvariantViolation):
            await impl.transition("suspect", sus["suspect_id"], "suspect.clear", {"reading_id": rd["reading_id"]})
        row = await impl.transition("suspect", sus["suspect_id"], "suspect.confirm", {"reading_id": rd["reading_id"]})
        assert row.data["verdict"] == "confirmed" and row.data["proof_reading_id"] == rd["reading_id"]
        # A reading overrides any engine answer: a later fusion pass cannot undo it.
        again = await impl.transition("suspect", sus["suspect_id"], "suspect.update_from_fusion",
                                      {**sus, "verdict": "suspect", "p_fault": 0.1})
        assert again.data["verdict"] == "confirmed" and again.data["proof_reading_id"] == rd["reading_id"]
        with pytest.raises(InvariantViolation):
            await impl.put("suspect", {**again.data, "verdict": "suspect", "proof_reading_id": None})
    run(go())


def test_i2_unknown_reading_cannot_confirm(impl):
    async def go():
        await seed(impl, upto="meter-reading")
        rd = {**ex("meter-reading"), "reading_id": "rd_unk", "verdict": "unknown"}
        await impl.transition("meter-reading", "rd_unk", "reading.record", rd)
        sus = ex("suspect")
        await impl.transition("suspect", sus["suspect_id"], "suspect.update_from_fusion", sus)
        with pytest.raises(InvariantViolation) as e:
            await impl.transition("suspect", sus["suspect_id"], "suspect.confirm", {"reading_id": "rd_unk"})
        assert e.value.rule == "I4"
        with pytest.raises(NotFound):
            await impl.transition("suspect", sus["suspect_id"], "suspect.confirm", {"reading_id": "rd_missing"})
    run(go())


def test_i2_plan_electrical_root_cause_needs_proof(impl):
    async def go():
        await seed(impl, upto="next-action")
        plan = ex("repair-plan")
        wire = next(rc for rc in plan["root_causes"] if rc["subject"]["kind"] in ("wire", "net"))
        wire["proof_reading_id"] = None
        with pytest.raises(InvariantViolation) as e:
            await impl.transition("repair-plan", plan["plan_id"], "plan.create", plan)
        assert e.value.rule == "I2"
        wire["verdict"] = "suspect"  # stated as unproven: allowed
        row = await impl.transition("repair-plan", plan["plan_id"], "plan.create", plan)
        assert row.status == "open"
        assert (await impl.transition("repair-plan", plan["plan_id"], "plan.confirm", {})).status == "closed"
        assert (await impl.transition("repair-plan", plan["plan_id"], "plan.reopen", {})).status == "open"
    run(go())


def test_i3_not_inspected_never_becomes_ok(impl):
    async def go():
        await seed(impl, upto="meter-reading")
        f = {**ex("finding"), "state": "not_inspected"}
        await impl.transition("finding", f["finding_id"], "finding.create", f)
        with pytest.raises(InvariantViolation) as e:
            await impl.put("finding", {**f, "state": "ok"}, expect_rev=1)
        assert e.value.rule == "I3"
        part = {**ex("suspect"), "suspect_id": "sus_p", "subject": {"kind": "part", "ref": "P1"}, "verdict": "not_inspected"}
        await impl.transition("suspect", "sus_p", "suspect.update_from_fusion", part)
        with pytest.raises(InvariantViolation) as e:
            await impl.transition("suspect", "sus_p", "suspect.update_from_fusion", {**part, "verdict": "cleared"})
        assert e.value.rule == "I3"
    run(go())


def test_i4_insufficient_evidence_needs_a_reason(impl):
    async def go():
        await seed(impl, upto="scan-package")
        f = {**ex("finding"), "state": "insufficient_evidence", "evidence": []}
        with pytest.raises(InvariantViolation) as e:
            await impl.transition("finding", f["finding_id"], "finding.create", f)
        assert e.value.rule == "I4"
        na = {**ex("next-action"), "kind": "report_insufficient_evidence", "power_state": "n/a"}
        na.pop("insufficient_evidence_reason", None)
        with pytest.raises(InvariantViolation) as e:
            await impl.transition("next-action", na["action_id"], "next_action.issue", na)
        assert e.value.rule == "I4"
        na["insufficient_evidence_reason"] = "blur"
        await impl.transition("next-action", na["action_id"], "next_action.issue", na)
    run(go())


def test_i5_and_s4(impl):
    async def go():
        await seed(impl, upto="next-action")
        plan = {**ex("repair-plan"), "summary": "Swap verified by simulation."}
        with pytest.raises(InvariantViolation) as e:
            await impl.transition("repair-plan", plan["plan_id"], "plan.create", plan)
        assert e.value.rule == "I5"
        na = {**ex("next-action"), "action_id": "na_v", "kind": "run_voltage_test", "power_state": "off"}
        with pytest.raises(InvariantViolation) as e:
            await impl.transition("next-action", "na_v", "next_action.issue", na)
        assert e.value.rule == "S4"
        with pytest.raises(InvariantViolation):
            await impl.transition("next-action", "na_v", "next_action.issue", {**na, "power_state": "on", "safety_notes": []})
    run(go())


def test_scan_alignment_and_still_skip(impl):
    async def go():
        rows = await seed(impl, upto="still")
        align = rows["scan-package"]["alignment"]
        r = await impl.transition("scan-package", "scan_7f3a", "scan.set_alignment", {"alignment": {**align, "fitness": 0.9}})
        assert r.rev == 2 and r.data["alignment"]["fitness"] == 0.9
        st = rows["still"]
        r = await impl.transition("still", st["still_id"], "still.mark_not_inspected", {})
        assert r.data["user_skipped"] is True
    run(go())


def test_job_queue_exclusive_claim_under_10_claimers(impl):
    async def go():
        ids = [await impl.enqueue(Job(kind="detect", inputs={"n": i})) for i in range(10)]
        claims = await asyncio.gather(*(impl.claim(f"w{i}", ["detect"]) for i in range(10)))
        got = [j.job_id for j in claims if j]
        assert sorted(got) == sorted(ids) and len(set(got)) == 10
        assert await impl.claim("w", ["detect"]) is None
        j = claims[0]
        await impl.heartbeat(j.job_id)
        await impl.fail(j.job_id, "transient", retry=True)
        assert (await impl.get_job(j.job_id)).status == "queued"
        again = await impl.claim("w2", ["detect"])
        assert again.job_id == j.job_id and again.attempts == 2
        await impl.complete(again.job_id, {"ok": True})
        done = await impl.get_job(again.job_id)
        assert done.status == "done" and done.result == {"ok": True}
        await impl.fail(claims[1].job_id, "fatal", retry=False)
        assert (await impl.get_job(claims[1].job_id)).status == "failed"
        assert await impl.claim("w", ["render"]) is None
    run(go())


def test_subscriber_sees_an_insert_within_2_s(impl):
    async def go():
        await seed(impl, upto="meter-reading")
        sess = ex("scan-package")["session_id"]
        start = (await impl.events_since(sess))[-1].event_id
        stream = impl.subscribe(sess, after=start)
        f = ex("finding")

        async def write():
            await asyncio.sleep(0.3)
            await impl.transition("finding", f["finding_id"], "finding.create", f)
            return time.monotonic()

        task = asyncio.create_task(write())
        ev = await asyncio.wait_for(anext(stream), timeout=5)
        seen = time.monotonic()
        written = await task
        assert ev.kind == "finding" and ev.id == f["finding_id"] and ev.op == "insert" and ev.row == f
        assert seen - written <= 2.0
        await stream.aclose()
    run(go())
