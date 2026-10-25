"""Build the report (task B11) from what the pipeline stored: findings, suspects, the next action, capture facts,
locations and provenance. Pure: no I/O, no model calls (the summary is written by repairos.report.summary).

Every stored finding of the scan lands in exactly one bucket (MS1):
  findings               Finding.state ok (issue `none`) or an issue state: a model judged it
  not_inspected          not_inspected / not_seen_in_photo: nobody looked (I3), with the reason
  insufficient_evidence  insufficient_evidence / unsure: looked at, could not judge (I4), with the reason
Numbers never become user-facing confidence: Finding.confidence becomes a word (I6).

A still whose not-inspected rows cover GROUP_AT or more detector regions gets one grouped row ("N regions in this
photo were not inspected: ..."), with `finding_ids` (each finding still in exactly one row) and the exact region
count in `regions` (a roll-up finding from vision.inspect stands for many regions: `regions[fid]["regions"]`).

Judged rows that are one physical issue collapse into one row (collapse_rows): the same still and the same issue
with crops overlapping at IoU >= MERGE_IOU, or different stills, the same part family and issue, and located regions
on the scan that intersect. Setup and assembly states (Finding.category setup: a battery missing, a cable unplugged,
a cover not clipped) go to `setup` with their plain fix, unless they are a hazard (severity safety) or a meter
reading settled them; rows the vision model judged cosmetic go to `cosmetic`; the rest stay in `findings`, ranked
most likely problem first (rank_key), and the first MOST_LIKELY issue rows are `most_likely`. Neither `setup` nor
`cosmetic` makes the verdict potential_issues on its own (report.verdict).
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from repairos.fusion import words
from repairos.report.safety import limits, safety_notes
from repairos.report.verdict import caveat, decide, facts_of
from repairos.slots.blob import REF_PREFIX
from repairos.vision.generic_classes import GENERIC_CLASSES
from repairos.vision.ranking import MERGE_IOU
from repairos.vision.tiling import iou

NOT_LOOKED_AT = {"not_inspected", "not_seen_in_photo"}
GROUP_AT = 3   # not-inspected regions in one still from which they become one row
UNDECIDED = {"insufficient_evidence", "unsure"}
NOT_PROOF = "vision suspect, not proof"
JUDGES = {"vlm", "classifier", "meter"}
MOST_LIKELY = 3   # issue rows the report and the app lead with

# Ranking (owner, 2026-10-24): meter proof first, then safety > functional > cosmetic, then issues Jev judges as
# explaining the symptom, then confidence, then how many photos show it. Absent severity = not judged = functional.
PROOF_ORDER = {"confirmed_by_meter": 0, "not_measured": 1, "ruled_out_by_meter": 2}
SEVERITY_ORDER = {"safety": 0, "functional": 1, "cosmetic": 2}
EXPLAINS_ORDER = {"yes": 0, "unsure": 1, "no": 2}   # no Jev answer counts as unsure
CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
EXPLAINS_WORD = {"yes": "yes", "unsettled": "unsure", "no": "no"}   # fusion.questions.band -> judgment word


class ReportError(RuntimeError):
    """The report cannot be built honestly from what is stored (a pipeline bug, never papered over)."""


def confidence_word(confidence: float) -> str:
    """Finding.confidence (the source's own, uncalibrated) as a word. Bands sit between the values
    vision/inspect.py stores for the VLM's low / medium / high (0.35 / 0.6 / 0.8)."""
    return "low" if confidence < 0.5 else "medium" if confidence < 0.7 else "high"


def report_id_for(scan_id: str) -> str:
    return f"rep_{scan_id}"


def _photo(f: Mapping[str, Any], need_box: bool) -> dict[str, Any] | None:
    refs = f.get("evidence_refs") or {}
    still_ids = refs.get("still_ids") or []
    if not still_ids:
        if need_box:
            raise ReportError(f"finding {f['finding_id']}: judged, but it names no still")
        return None
    out: dict[str, Any] = {"still_id": still_ids[0]}
    if refs.get("crop_box_px"):
        out["box_px"] = [int(v) for v in refs["crop_box_px"]]
    elif need_box:
        raise ReportError(f"finding {f['finding_id']}: judged, but it has no crop box")
    if str(refs.get("crop_file", "")).startswith(REF_PREFIX):
        out["crop_ref"] = refs["crop_file"]
    return out


def _reason(f: Mapping[str, Any]) -> str:
    text = "; ".join(s.strip() for s in f.get("evidence") or [] if s.strip() and s.strip() != NOT_PROOF)
    if not text:
        raise ReportError(f"finding {f['finding_id']} is {f['state']} without a reason in its evidence (I4)")
    return text


def _proof(suspect: Mapping[str, Any] | None) -> dict[str, Any]:
    if suspect and suspect.get("proof_reading_id"):
        if suspect["verdict"] == "confirmed":
            return {"status": "confirmed_by_meter", "reading_id": suspect["proof_reading_id"]}
        if suspect["verdict"] == "cleared":
            return {"status": "ruled_out_by_meter", "reading_id": suspect["proof_reading_id"]}
    return {"status": "not_measured"}


def capture_summary(scan: Mapping[str, Any] | None, stills: list[Mapping[str, Any]],
                    judged_stills: set[str]) -> dict[str, Any]:
    kfs = (scan or {}).get("keyframes") or []
    hint = (scan or {}).get("coverage_hint")
    out: dict[str, Any] = {
        # the scan package's depth_source (absent = lidar, the contract); a loose photo has no scan, so no depth
        "depth_source": ((scan.get("depth_source") or "lidar") if scan and scan.get("keyframes") is not None
                         else "none"),
        "stills": len(stills),
        "stills_inspected": len(judged_stills & {s["still_id"] for s in stills}),
        "keyframes": len(kfs),
        "coverage_words": words.band("coverage", hint) if hint is not None else "not measured",
    }
    if scan and scan.get("point_count") is not None:
        out["scan_points"] = int(scan["point_count"])
    from repairos.report.coverage import view_coverage

    if (views := view_coverage(scan, stills)) is not None:
        out["views"] = views
    return out


def severity(e: Mapping[str, Any]) -> str:
    return e.get("severity") or "functional"


def photos_of(e: Mapping[str, Any]) -> set[str]:
    return {c["still_id"] for c in [e["crop"], *(e.get("crops") or [])]}


def rank_key(e: Mapping[str, Any]) -> tuple:
    """Sort key for judged rows: issues before `none`, then PROOF_ORDER, SEVERITY_ORDER, EXPLAINS_ORDER,
    CONFIDENCE_ORDER, more photos first, Jev's rank, finding id (deterministic)."""
    j = e.get("judgment") or {}
    return (e["issue"] == "none", PROOF_ORDER[e["proof"]["status"]], SEVERITY_ORDER[severity(e)],
            EXPLAINS_ORDER.get(j.get("explains_symptom"), 1), CONFIDENCE_ORDER[e["confidence_word"]],
            -len(photos_of(e)), j.get("rank", 1 << 30), e["finding_id"])


def part_family(part_class: str) -> str:
    """The longest generic class name (vision/generic_classes.py) inside the VLM's part name, else the name itself:
    "black insulated wire" and "wire" are one family, "connector" and "wire" are not."""
    name = " ".join(part_class.lower().replace("_", " ").split())
    hits = [g for g in GENERIC_CLASSES if re.search(rf"(?<![a-z]){re.escape(g)}(?![a-z])", name)]
    return max(hits, key=len) if hits else name


def _boxes_meet(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return all(a["region_min_m"][k] <= b["region_max_m"][k] and b["region_min_m"][k] <= a["region_max_m"][k]
               for k in range(3))


def same_issue(a: Mapping[str, Any], b: Mapping[str, Any], merge_iou: float = MERGE_IOU) -> str | None:
    """Why two judged rows are one physical issue, or None:
      same_photo_overlap  same still, same issue, crop boxes at IoU >= merge_iou (two distinct neighbouring parts
                          overlap less: measured 0.42 for adjacent terminals; a merged duplicate is 1.0);
      same_place_on_scan  different stills, same issue and part family, both located on the scan, and the located
                          regions (each location's own extent) intersect.
    Containment (intersection over the smaller box) is not used: it would merge a prong with its whole housing."""
    if a["issue"] != b["issue"]:
        return None
    sa, sb = a["crop"]["still_id"], b["crop"]["still_id"]
    if sa == sb:
        return "same_photo_overlap" if iou(a["crop"]["box_px"], b["crop"]["box_px"]) >= merge_iou else None
    la, lb = a.get("location") or {}, b.get("location") or {}
    if la.get("status") == lb.get("status") == "computed" and part_family(a["part_class"]) == \
            part_family(b["part_class"]) and _boxes_meet(la, lb):
        return "same_place_on_scan"
    return None


def collapse_rows(rows: list[dict[str, Any]], merge_iou: float = MERGE_IOU,
                  stable: frozenset[str] | set[str] = frozenset()) -> list[dict[str, Any]]:
    """Judged rows -> one row per physical issue (same_issue, transitively). The best-ranked row of a group keeps its
    fields, unless the group holds a row of the previous report revision (`stable`: its row ids): that row keeps its
    place, so a finding seen again in a later photo of the same scan is one row with the same id, not a new one. The
    group's `finding_ids` (the kept row first), `regions`, the other `crops`, the union of the evidence, the worst
    severity and `merged_by` are added. Rows that collapse with nothing are returned unchanged."""
    parent = list(range(len(rows)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(rows)):
        for k in range(i + 1, len(rows)):
            if root(i) != root(k) and same_issue(rows[i], rows[k], merge_iou):
                parent[root(k)] = root(i)
    groups: dict[int, list[dict[str, Any]]] = {}
    for i, e in enumerate(rows):
        groups.setdefault(root(i), []).append(e)
    out = []
    for group in groups.values():
        if len(group) == 1:
            out.append(group[0])
            continue
        group = sorted(group, key=lambda e: (e["finding_id"] not in stable, rank_key(e)))
        top = dict(group[0])
        top["finding_ids"] = [e["finding_id"] for e in group]
        top["regions"] = len(group)
        top["crops"] = [e["crop"] for e in group[1:]]
        top["evidence"] = list(dict.fromkeys(s for e in group for s in e["evidence"]))
        if any(e.get("severity") for e in group):
            top["severity"] = min((severity(e) for e in group), key=SEVERITY_ORDER.__getitem__)
        top["merged_by"] = "same_photo_overlap" if len({e["crop"]["still_id"] for e in group}) == 1 \
            else "same_place_on_scan"
        out.append(top)
    return out


def _same_place(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Two rows from different photos showing one place: both located on the scan, regions meeting, same part
    family (the rule same_issue uses across stills)."""
    pa, pb = (a.get("photo") or a.get("crop") or {}).get("still_id"), (b.get("photo") or b.get("crop") or {}).get(
        "still_id")
    la, lb = a.get("location") or {}, b.get("location") or {}
    return (pa != pb and la.get("status") == lb.get("status") == "computed" and _boxes_meet(la, lb)
            and part_family(a["part_class"]) == part_family(b["part_class"]))


def merge_undecided(undecided: list[dict[str, Any]], judged: list[dict[str, Any]],
                    stable: frozenset[str] | set[str] = frozenset()) -> list[dict[str, Any]]:
    """Could-not-judge rows of a place that is judged in another photo become `superseded_ids` of that judged row
    (the place is judged now, e.g. from a closer photo added later); could-not-judge rows of one place seen in
    several photos become one row (`finding_ids`, a row of the previous revision first when there is one).
    `judged` rows are updated in place; the remaining insufficient_evidence rows are returned."""
    rest = []
    for u in undecided:
        j = next((j for j in judged if _same_place(u, j) and u["photo"]["still_id"] not in photos_of(j)), None) \
            if u.get("photo") else None
        if j is None:
            rest.append(u)
        else:
            j.setdefault("superseded_ids", []).append(u["finding_id"])
    parent = list(range(len(rest)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(rest)):
        for k in range(i + 1, len(rest)):
            if root(i) != root(k) and _same_place(rest[i], rest[k]):
                parent[root(k)] = root(i)
    groups: dict[int, list[dict[str, Any]]] = {}
    for i, e in enumerate(rest):
        groups.setdefault(root(i), []).append(e)
    out = []
    for group in groups.values():
        group = sorted(group, key=lambda e: (e["finding_id"] not in stable, e["finding_id"]))
        top = dict(group[0])
        if len(group) > 1:
            top["finding_ids"] = [e["finding_id"] for e in group]
            top["reason"] = "; ".join(dict.fromkeys(e["reason"] for e in group))
        out.append(top)
    return out


def is_setup(e: Mapping[str, Any]) -> bool:
    """A row for the `setup` section: a setup or assembly state (category setup), not a broken part, unless it is a
    hazard (severity safety: a cell in backwards, a loose mains wire) or a meter reading confirmed or ruled out a fault
    there; those stay in `findings` (and a hazard in `most_likely`)."""
    return e["issue"] != "none" and e.get("category") == "setup" and e.get("severity") != "safety" and \
        e["proof"]["status"] == "not_measured"


def is_cosmetic(e: Mapping[str, Any]) -> bool:
    """A row for the `cosmetic` section: an issue the VLM judged cosmetic, unless a meter reading confirmed a fault
    there (then it is a proven fault and stays in `findings` and `most_likely`, whatever it looks like). A setup row is
    in `setup`, whatever its severity."""
    return e["issue"] != "none" and e.get("severity") == "cosmetic" and \
        e["proof"]["status"] != "confirmed_by_meter" and not is_setup(e)


def _is_issue(e: Mapping[str, Any]) -> bool:
    return e["issue"] != "none" and e["proof"]["status"] != "ruled_out_by_meter"


def bottom_not_placed(report: Mapping[str, Any], segments: list[Mapping[str, Any]],
                      locations: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The "Bottom (not placed on the model)" list: report rows (findings, cosmetic, insufficient_evidence) built
    from findings of a flipped segment that was not aligned, each with its photo crop and why it has no 3D point."""
    want = {fid: int(sg["segment"]) for sg in segments if not sg.get("aligned")
            for fid in sg.get("findings_not_placed") or []}
    if not want:
        return []
    out = []
    for bucket in ("findings", "setup", "cosmetic", "insufficient_evidence"):
        for row in report.get(bucket) or []:
            ids = [row.get("finding_id"), *(row.get("finding_ids") or [])]
            hit = next((i for i in ids if i in want), None)
            photo = row.get("crop") or row.get("photo")
            if hit is None or not photo:
                continue
            loc = locations.get(hit) or {}
            e = {"finding_id": row["finding_id"], "segment": want[hit],
                 "photo": {k: photo[k] for k in ("still_id", "box_px", "crop_ref") if k in photo},
                 "reason": str(loc.get("reason") or "the flipped segment was not lined up with the main scan")}
            if row.get("subject"):
                e["subject"] = row["subject"]
            if row.get("part_class"):
                e["part_class"] = row["part_class"]
            if row.get("issue"):
                e["issue"] = row["issue"]
            out.append(e)
    return out


def build_report(*, scan: Mapping[str, Any], stills: list[Mapping[str, Any]], findings: list[Mapping[str, Any]],
                 suspects: list[Mapping[str, Any]], next_action: Mapping[str, Any] | None,
                 regions: Mapping[str, Mapping[str, Any]], locations: Mapping[str, Mapping[str, Any]],
                 models: Mapping[str, str], now: datetime | None = None,
                 explains: Mapping[tuple[str, str], str] | None = None,
                 previous: Mapping[str, Any] | None = None, identity: Mapping[str, Any] | None = None,
                 mesh: Mapping[str, Any] | None = None, hints: Mapping[str, Any] | None = None,
                 segments: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """The report without `summary` (repairos.report.summary adds it). `regions[finding_id]` gives the region's
    `part_class` and `inspected_by` (the stages that looked); `locations[finding_id]` its report location;
    `explains[(kind, ref)]` Jev's banded "explains the symptom" judgment (yes | unsettled | no) per subject.
    `previous`: the scan's report before this one (more photos were added): its row ids are kept for the same place
    (collapse_rows, merge_undecided), so the app sees the same finding again, updated, not a duplicate.
    `segments`: the flipped segments' registration (shown as `capture.segments`).
    `identity`, `mesh`, `hints`: the run row's records (pipeline/run.py), shown as `object`, `mesh` and
    `reference_hints` when they are done."""
    explains = explains or {}
    by_finding: dict[str, Mapping[str, Any]] = {}
    for s in suspects:
        for fid in s.get("finding_ids", []):
            by_finding[fid] = s

    judged, not_inspected, undecided = [], [], []
    judged_stills: set[str] = set()
    for f in sorted(findings, key=lambda f: f["finding_id"]):
        fid = f["finding_id"]
        info = regions.get(fid)
        if info is None:
            raise ReportError(f"finding {fid}: the pipeline recorded no region for it (part class, stages)")
        base = {"finding_id": fid, "subject": dict(f["subject"]), "part_class": info["part_class"]}
        loc = locations.get(fid)
        if f["state"] in NOT_LOOKED_AT:
            e = {**base, "reason": _reason(f)}
            if (p := _photo(f, need_box=False)):
                e["photo"] = p
            if loc:
                e["location"] = dict(loc)
            not_inspected.append(e)
        elif f["state"] in UNDECIDED:
            e = {**base, "reason": _reason(f), "inspected_by": list(info["inspected_by"])}
            if (p := _photo(f, need_box=False)):
                e["photo"] = p
            if loc:
                e["location"] = dict(loc)
            undecided.append(e)
        else:
            if not JUDGES & set(info["inspected_by"]):
                raise ReportError(f"finding {fid} is {f['state']!r} but no model judged it (I3)")
            sus = by_finding.get(fid)
            evidence = [s for s in f.get("evidence") or [] if s.strip()]
            if not evidence or loc is None:
                raise ReportError(f"finding {fid}: judged, but it has no evidence text or no location entry")
            e = {**base, "issue": "none" if f["state"] == "ok" else f["state"],
                 "confidence_word": confidence_word(f["confidence"]),
                 "evidence": evidence,
                 "inspected_by": list(info["inspected_by"]), "crop": _photo(f, need_box=True),
                 "location": dict(loc),
                 "proof": _proof(sus)}
            if f.get("severity"):
                e["severity"] = f["severity"]
            if f.get("part_words"):
                e["part_words"] = f["part_words"]
            if f.get("category"):
                e["category"] = f["category"]
            if f.get("fix"):
                e["fix"] = f["fix"]
            if sus:
                e["judgment"] = {"rank": sus["rank"], "likelihood_word": sus["p_fault_word"],
                                 "verdict": sus["verdict"]}
                key = (sus["subject"]["kind"], sus["subject"]["ref"])
                if key in explains:
                    e["judgment"]["explains_symptom"] = EXPLAINS_WORD[explains[key]]
            judged.append(e)
            judged_stills.add(e["crop"]["still_id"])

    not_inspected = group_not_inspected(not_inspected, {fid: int(r.get("regions", 1)) for fid, r in regions.items()},
                                        {f["finding_id"]: f for f in findings})
    stable = frozenset(e["finding_id"] for b in ("findings", "setup", "cosmetic", "insufficient_evidence")
                       for e in (previous or {}).get(b, []))
    judged = sorted(collapse_rows(judged, stable=stable), key=rank_key)
    undecided = merge_undecided(undecided, judged, stable)
    setup = [e for e in judged if is_setup(e)]
    cosmetic = [e for e in judged if is_cosmetic(e)]
    judged = [e for e in judged if not is_cosmetic(e) and not is_setup(e)]
    across = len({s for e in judged + setup + cosmetic for s in photos_of(e)}) > 1 and not any(
        (e.get("location") or {}).get("status") == "computed" for e in judged + setup + cosmetic)
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    report: dict[str, Any] = {
        "report_id": report_id_for(scan["scan_id"]),
        "scan_id": scan["scan_id"],
        "session_id": scan["session_id"],
        "created_at": stamp,
        "findings": judged,
        "most_likely": [e["finding_id"] for e in judged if _is_issue(e)][:MOST_LIKELY],
        "cosmetic": cosmetic,
        "setup": setup,
        "not_inspected": not_inspected,
        "insufficient_evidence": undecided,
        "capture": capture_summary(scan, stills, judged_stills),
        "models": dict(models),
        "grain": "not called",
    }
    if segments:   # flipped segments and how they were registered (pipeline/segments.py report_segments)
        report["capture"]["segments"] = [dict(x) for x in segments]
        if (bottom := bottom_not_placed(report, segments, locations)):
            report["bottom_not_placed"] = bottom
    v = decide(facts_of(report))
    report["verdict"] = v.verdict
    report["verdict_reason"] = v.as_reason()
    if v.verdict in ("no_problems_seen", "insufficient_evidence") and (c := caveat(report)):
        report["verdict_reason"]["caveat"] = c   # what was not checked: the caveat list, never the headline
    if (obj := object_header(identity)):
        report["object"] = obj
    if mesh and mesh.get("status") == "done":
        report["mesh"] = {"url": mesh_url(scan["scan_id"], int(mesh["rev"])), "rev": int(mesh["rev"]),
                          "format": mesh["format"], "frame": "arkit_world", "triangles": int(mesh["triangles"])}
        if mesh.get("cloud_ref"):
            report["mesh"].update(cloud_url=cloud_url(scan["scan_id"], int(mesh["rev"])),
                                  cloud_points=int(mesh.get("cloud_points") or 0))
        if mesh.get("hero"):
            report["mesh"].update(hero=mesh["hero"], hero_reason=mesh.get("hero_reason") or mesh["hero"])
    if hints and hints.get("status") == "done" and hints.get("hints"):
        report["reference_hints"] = {"hints": list(hints["hints"])[:3],
                                     "sources": [{"title": x.get("title", ""), "link": x["link"]}
                                                 for x in (hints.get("sources") or [])[:5] if x.get("link")]}
    report["safety"] = safety_notes(judged, cosmetic, [*undecided, *setup])
    report["limits"] = limits(scan.get("symptom"), judged, unmerged_photos=across)
    if next_action:
        step = {"action_id": next_action["action_id"], "kind": next_action["kind"],
                "instruction": next_action["instruction"]}
        if next_action.get("reason"):
            step["reason"] = next_action["reason"]
        report["next_step"] = step
    return report


def mesh_url(scan_id: str, rev: int) -> str:
    return f"/v1/scans/{scan_id}/mesh?rev={rev}"


def cloud_url(scan_id: str, rev: int) -> str:
    return f"/v1/scans/{scan_id}/cloud?rev={rev}"


def object_header(identity: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The report's `object` from the run row's identity (vision model's answer), or None before one is done."""
    if not identity or identity.get("status") != "done":
        return None
    out: dict[str, Any] = {"category": identity.get("category") or "unknown",
                           "confidence_word": identity.get("confidence") or "low",
                           "label_visible": bool(identity.get("label_visible"))}
    for k in ("brand", "model", "label_text"):
        if identity.get(k):
            out[k] = identity[k]
    if identity.get("from_still_ids"):
        out["from_still_ids"] = list(identity["from_still_ids"])
    return out


def _why(f: Mapping[str, Any]) -> str:
    """The reason a finding was not inspected, without its subject: the text after "not inspected: "."""
    lead = next((s.strip() for s in f.get("evidence") or [] if s.strip() and s.strip() != NOT_PROOF), "")
    _, sep, tail = lead.partition("not inspected: ")
    return tail.strip() if sep and tail.strip() else _reason(f)


def group_not_inspected(rows: list[dict[str, Any]], counts: Mapping[str, int],
                        findings: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per still for stills whose not-inspected rows cover GROUP_AT or more regions; other rows unchanged
    (a single roll-up row gets its exact `regions`). Rows with no still keep their own row."""
    by_still: dict[str, list[dict[str, Any]]] = {}
    out: list[dict[str, Any]] = []
    for e in rows:
        sid = (e.get("photo") or {}).get("still_id")
        (by_still.setdefault(sid, []) if sid else out).append(e)
    for sid in sorted(by_still):
        group = by_still[sid]
        total = sum(counts.get(e["finding_id"], 1) for e in group)
        if len(group) == 1 or total < GROUP_AT:
            for e in group:
                if counts.get(e["finding_id"], 1) > 1:
                    e = {**e, "regions": counts[e["finding_id"]]}
                out.append(e)
            continue
        reasons: dict[str, int] = {}
        for e in group:
            why = _why(findings[e["finding_id"]])
            reasons[why] = reasons.get(why, 0) + counts.get(e["finding_id"], 1)
        parts = "; ".join(f"{n} {'region' if n == 1 else 'regions'}: {why}"
                          for why, n in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])))
        ids = [e["finding_id"] for e in group]
        out.append({"finding_id": ids[0], "subject": {"kind": "part", "ref": f"{sid}/not_inspected"},
                    "part_class": "regions in this photo", "photo": {"still_id": sid},
                    "reason": f"{total} regions in this photo were not inspected: {parts}",
                    "regions": total, "finding_ids": ids})
    return out


def honesty_violations(report: Mapping[str, Any], findings: list[Mapping[str, Any]]) -> list[str]:
    """MS1: every stored finding in exactly one bucket; nothing shown judged that no model looked at."""
    seen: dict[str, list[str]] = {}
    for bucket in ("findings", "setup", "cosmetic", "not_inspected", "insufficient_evidence"):
        for e in report.get(bucket, []):
            for fid in (e.get("finding_ids") or [e["finding_id"]]) + list(e.get("superseded_ids") or []):
                seen.setdefault(fid, []).append(bucket)
    out = []
    state = {f["finding_id"]: f["state"] for f in findings}
    for e in report.get("findings", []) + report.get("setup", []) + report.get("cosmetic", []):
        for fid in e.get("superseded_ids") or []:
            if state.get(fid) not in UNDECIDED:
                out.append(f"row {e['finding_id']} supersedes {fid}, which is {state.get(fid)!r}, not a finding that "
                           "could not be judged")
    for e in report.get("insufficient_evidence", []):
        ids = e.get("finding_ids")
        if ids and (ids[0] != e["finding_id"] or any(state.get(x) not in UNDECIDED for x in ids)):
            out.append(f"grouped row {e['finding_id']}: finding_id is not its first finding, or it groups a finding "
                       "that was judged")
    for f in findings:
        where = seen.pop(f["finding_id"], [])
        if len(where) != 1:
            out.append(f"finding {f['finding_id']} ({f['state']}) is in {where or 'no bucket'}")
    out += [f"report lists {fid}, which is not a stored finding of the scan" for fid in seen]
    for e in report.get("not_inspected", []):
        ids = e.get("finding_ids")
        if ids and (ids[0] != e["finding_id"] or e.get("regions", 1) < len(ids)):
            out.append(f"grouped row {e['finding_id']}: finding_id is not its first finding, or it counts fewer "
                       "regions than it groups")
    for e in report.get("findings", []) + report.get("setup", []) + report.get("cosmetic", []):
        if not JUDGES & set(e["inspected_by"]):
            out.append(f"finding {e['finding_id']} shown as judged but no model looked at it")
        ids = e.get("finding_ids")
        if ids and (ids[0] != e["finding_id"] or e.get("regions") != len(ids)
                    or len(e.get("crops", [])) != len(ids) - 1):
            out.append(f"collapsed row {e['finding_id']}: finding_id is not its first finding, or its regions or "
                       "crops do not match the findings it stands for")
    for e in report.get("cosmetic", []):
        if not is_cosmetic(e):
            out.append(f"row {e['finding_id']} is listed as cosmetic but is not a cosmetic issue")
    for e in report.get("findings", []):
        if is_cosmetic(e):
            out.append(f"row {e['finding_id']} is a cosmetic issue listed among the findings")
        if is_setup(e):
            out.append(f"row {e['finding_id']} is a setup state listed among the findings")
    for e in report.get("setup", []):
        if not is_setup(e) or not e.get("fix"):
            out.append(f"row {e['finding_id']} is listed as setup but is not a setup state with a fix")
    issues = [e["finding_id"] for e in report.get("findings", []) if _is_issue(e)]
    if "most_likely" in report and report["most_likely"] != issues[:MOST_LIKELY]:
        out.append("most_likely is not the first issue rows of the findings")
    return out
