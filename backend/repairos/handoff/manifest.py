"""The capture manifest Lea writes into the Grain project as src/LEA_CAPTURE.json (schema_version 1), and the first
design message.

    {"schema_version": 1, "handoff_id", "lea_session_id", "lea_scan_id", "source_revision",
     "grain": {"project_id", "scan_id"},
     "geometry": {"file", "kind": "triangle_mesh"|"point_cloud", "units": "m", "frame": "arkit_world", "sha256",
                  "source": "gateway_mesh"|"gateway_cloud", "quality": {...}} | null,
     "photos": [{"n", "still_id", "sha256", "original_sha256", "finding_ids"}],
     "findings": [{"id", "state", "subject", "confidence", "observation", "evidence_photos", "location_m"}],
     "dimensions": [{"name", "kind", "value_mm", "uncertainty_mm", "method", "verified", "evidence_still_ids"}],
     "fit": {"tolerance_mm", "spacings": [{"between": [a, b], "distance_mm"}]} | null,
     "measurement_check": {...},
     "object": {...}, "design_objective", "unknowns": [...]}

Rules (S5, I3): a fit requirement is only a verified distance the person marked as a hole spacing; an unverified one is
listed under `unknowns` with its value, never sent as a constraint. `location_m` is null unless Lea located the finding
on the scan. A photo-only handoff has `geometry: null`. Every file sent carries the SHA-256 of what was sent and of the
original it came from; the originals stay in Lea's blob store.
"""
from __future__ import annotations

import hashlib
import io
from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA_VERSION = 1
MAX_PHOTOS = 8                     # Grain keeps at most eight photos per scan
PHOTO_MAX_PX = 2048                # the derivative sent; Grain stores at most 1536 px a side anyway
MAX_SCAN_BYTES = 50 * 1024 * 1024
CAPTURE_PATH = "src/LEA_CAPTURE.json"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def findings(report: Mapping[str, Any] | None) -> list[dict]:
    """The report's defects (not `none`, not setup) in its ranked order, in the manifest's shape."""
    out = []
    for f in (report or {}).get("findings") or []:
        if f.get("issue") in (None, "none") or f.get("category") == "setup":
            continue
        loc = f.get("location") or {}
        crops = [f.get("crop") or {}] + list(f.get("crops") or [])
        out.append({"id": f["finding_id"], "state": f["issue"],
                    "subject": f.get("part_words") or f.get("part_class") or (f.get("subject") or {}).get("ref"),
                    "confidence": f.get("confidence_word"),
                    "observation": "; ".join(f.get("evidence") or [])[:600],
                    "evidence_still_ids": [c["still_id"] for c in crops if c.get("still_id")],
                    "location_m": loc.get("point_m") if loc.get("status") == "computed" else None})
    return out


def choose_photos(finds: Sequence[Mapping], stills: Sequence[Mapping], measurement: Mapping | None,
                  limit: int = MAX_PHOTOS) -> list[str]:
    """Up to `limit` still ids: the findings' evidence first (ranked), then the stills the measurements used, then
    stills from view cells not yet shown (so the agent sees the sides and mounting surfaces). Label stills last."""
    known = {s["still_id"]: s for s in stills}
    order: list[str] = []

    def add(sid):
        if sid in known and sid not in order and len(order) < limit:
            order.append(sid)
    for f in finds:
        for sid in f.get("evidence_still_ids") or []:
            add(sid)
    for d in (measurement or {}).get("dimensions") or []:
        for sid in d.get("evidence_still_ids") or []:
            add(sid)
    cells = {known[s].get("view_cell") for s in order}
    for s in stills:
        if s.get("kind") != "label" and s.get("view_cell") not in cells:
            add(s["still_id"])
            cells.add(s.get("view_cell"))
    for s in sorted(stills, key=lambda s: s.get("kind") == "label"):
        add(s["still_id"])
    return order


def photo_derivative(jpeg_or_image: bytes) -> bytes:
    """The JPEG sent to Grain: at most PHOTO_MAX_PX a side, quality 90, in the stored pixel orientation."""
    from PIL import Image

    with Image.open(io.BytesIO(jpeg_or_image)) as im:
        im = im.convert("RGB")
        im.thumbnail((PHOTO_MAX_PX, PHOTO_MAX_PX))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def geometry_choice(mesh: Mapping[str, Any] | None, size_of) -> tuple[str, str, str] | None:
    """(blob ref, kind, source) of the geometry to send: the reconstructed triangle mesh when it is built and under
    Grain's size cap, else the cleaned object cloud, else None (a photo-only handoff)."""
    if not mesh or mesh.get("status") != "done":
        return None
    if mesh.get("ref") and mesh.get("format") == "ply" and size_of(mesh["ref"]) <= MAX_SCAN_BYTES:
        return mesh["ref"], "triangle_mesh", "gateway_mesh"
    if mesh.get("cloud_ref") and size_of(mesh["cloud_ref"]) <= MAX_SCAN_BYTES:
        return mesh["cloud_ref"], "point_cloud", "gateway_cloud"
    return None


def fit_and_unknowns(measurement: Mapping | None, unknowns: Sequence[str] = ()) -> tuple[dict | None, list[str]]:
    """Hole spacings Grain must hold (verified distances marked `fit: "spacing"`) and what is still unknown."""
    left = list(unknowns)
    if not measurement:
        return None, left
    spacings = []
    for d in measurement.get("dimensions") or []:
        role = d.get("fit") if isinstance(d.get("fit"), str) else (d.get("fit") or {}).get("role")
        if role != "spacing" or d.get("kind", "distance") != "distance" or len(d.get("between") or []) != 2:
            continue
        if d.get("verified"):
            spacings.append({"between": list(d["between"]), "distance_mm": d["value_mm"], "name": d["name"]})
        else:
            left.append(f"{d['name']} (measured {d['value_mm']:g} ± {d['uncertainty_mm']:g} mm, not verified: "
                        "needs a caliper reading)")
    for f in measurement.get("failed") or []:
        left.append(f"{f['name']} (could not be measured: {f['error']})")
    tol = float((measurement.get("check") or {}).get("tolerance_mm") or 0.5)
    return ({"tolerance_mm": tol, "spacings": spacings} if spacings else None), left


def build(*, handoff_id: str, scan: Mapping[str, Any], report: Mapping[str, Any] | None, revision: int,
          objective: str, geometry: dict | None, photos: Sequence[dict], measurement: Mapping | None,
          grain_project_id: str | None, grain_scan_id: str | None, unknowns: Sequence[str] = ()) -> dict:
    """The manifest (see module doc). `geometry` and `photos` already carry their hashes; nothing here reads a file."""
    finds = findings(report)
    photo_of = {p["still_id"]: p["n"] for p in photos}
    for f in finds:
        f["evidence_photos"] = [photo_of[s] for s in f.pop("evidence_still_ids") if s in photo_of]
    fit, left = fit_and_unknowns(measurement, unknowns)
    dims = [{k: d.get(k) for k in ("name", "kind", "value_mm", "uncertainty_mm", "method", "verified",
                                    "evidence_still_ids", "between")} for d in (measurement or {}).get("dimensions") or []]
    return {"schema_version": SCHEMA_VERSION, "handoff_id": handoff_id, "lea_session_id": scan.get("session_id"),
            "lea_scan_id": scan["scan_id"], "source_revision": revision,
            "grain": {"project_id": grain_project_id, "scan_id": grain_scan_id},
            "geometry": geometry, "photos": list(photos), "findings": finds, "dimensions": dims, "fit": fit,
            "measurement_check": {k: v for k, v in ((measurement or {}).get("check") or {}).items() if k != "notes"}
            or None,
            "object": {k: v for k, v in ((report or {}).get("object") or {}).items()
                       if k in ("category", "brand", "model")},
            "design_objective": objective, "unknowns": left}


def first_message(m: Mapping[str, Any]) -> str:
    """The design turn's opening message: what to make, where the evidence is, and the rules that matter."""
    sid = (m.get("grain") or {}).get("scan_id")
    n_ver = sum(1 for d in m.get("dimensions") or [] if d.get("verified"))
    lines = [f"Lea capture for a repair: {m.get('design_objective')}.",
             f"First call inspect_repair_capture with scan_id {sid!r} and study the photos and the scan "
             f"(src/{CAPTURE_PATH.split('/', 1)[1]} has the findings and {n_ver} verified dimension(s)).",
             "Design an editable parametric part for the objective from the verified dimensions; the build checks the "
             "capture's hole spacings. If a dimension the part needs is unknown or unverified, ask me before relying "
             "on it, or make a labelled concept and say what must be measured."]
    if m.get("unknowns"):
        lines.append("Known unknowns: " + "; ".join(m["unknowns"]) + ".")
    return "\n".join(lines)
