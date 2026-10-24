"""Validate a session folder: session.json and truth.json against samples/session.schema.json, every contract
object against its schema, cross-references, and the capture files themselves (binary PLY in metres with the
declared point count, JPEG stills at their declared full resolution). Returns problems as strings ([] = valid).
"""
from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import jsonschema
from referencing import Registry, Resource

from repairos.config import ROOT
from repairos.contracts import SCHEMA_DIR
from repairos.sessions import files
from repairos.sessions.reader import Session, SessionError

SESSION_SCHEMA = ROOT / "samples" / "session.schema.json"


@cache
def _registry() -> Registry:
    docs = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in SCHEMA_DIR.glob("*.schema.json")}
    docs["session.schema.json"] = json.loads(SESSION_SCHEMA.read_text(encoding="utf-8"))
    return Registry().with_resources((n, Resource.from_contents(d)) for n, d in docs.items())


@cache
def _validator(ref: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator({"$ref": ref}, registry=_registry())


def _schema_errors(v: jsonschema.Draft202012Validator, data, where: str) -> list[str]:
    return [f"{where} /{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in v.iter_errors(data)]


def _contract_errors(kind: str, data, where: str) -> list[str]:
    return _schema_errors(_validator(f"{kind}.schema.json"), data, where)


def validate_session(path: Path) -> list[str]:
    try:
        s = Session(path)
    except SessionError as e:
        return [str(e)]
    session_v, truth_v = _validator("session.schema.json"), _validator("session.schema.json#/$defs/truth")
    problems = _schema_errors(session_v, s.meta, "session.json")
    if s.meta.get("session_id") != s.dir.name:
        problems.append(f"session.json session_id {s.meta.get('session_id')!r} != folder name {s.dir.name!r}")
    try:
        if s.truth is not None:
            problems += _schema_errors(truth_v, s.truth, "truth.json")
    except SessionError as e:
        problems.append(str(e))

    def exists(rel: str, where: str) -> Path | None:
        try:
            p = s.file(rel)
        except SessionError as e:
            problems.append(f"{where}: {e}")
            return None
        if not p.is_file():
            problems.append(f"{where}: file {rel} is missing")
            return None
        return p

    def jpeg(rel: str, where: str, size: tuple[int, int] | None = None) -> None:
        p = exists(rel, where)
        if p is None:
            return
        try:
            with p.open("rb") as f:
                got = files.jpeg_size(f)
        except files.CaptureFileError as e:
            problems.append(f"{where}: {rel}: {e}")
            return
        if size and got != size:
            problems.append(f"{where}: {rel} is {got[0]}x{got[1]} px, declared {size[0]}x{size[1]}")

    scan_ids = set()
    for p in sorted(s.dir.glob("scans/*/scan_package.json")):
        where = str(p.relative_to(s.dir))
        pkg = json.loads(p.read_text(encoding="utf-8"))
        errs = _contract_errors("scan-package", pkg, where)
        problems += errs
        if errs:
            continue
        scan_ids.add(pkg["scan_id"])
        if pkg["session_id"] != s.session_id:
            problems.append(f"{where}: session_id {pkg['session_id']!r} != {s.session_id!r}")
        if p.parent.name != pkg["scan_id"]:
            problems.append(f"{where}: folder {p.parent.name!r} != scan_id {pkg['scan_id']!r}")
        ply = exists(pkg["points_file"], where)
        if ply is not None:
            try:
                with ply.open("rb") as f:
                    h = files.ply_header(f, ply.stat().st_size)
                if h["vertex_count"] != pkg["point_count"]:
                    problems.append(f"{where}: PLY has {h['vertex_count']} vertices, point_count says "
                                    f"{pkg['point_count']}")
            except files.CaptureFileError as e:
                problems.append(f"{where}: {pkg['points_file']}: {e}")
        for kf in pkg["keyframes"]:
            jpeg(kf["image_file"], f"{where} keyframe {kf['keyframe_id']}",
                 (kf["intrinsics"]["width_px"], kf["intrinsics"]["height_px"]))
            for fld in ("depth_file", "confidence_file"):
                if kf.get(fld):
                    exists(kf[fld], f"{where} keyframe {kf['keyframe_id']}")
    if not scan_ids:
        problems.append("no scans/<scan_id>/scan_package.json")

    for p in sorted(s.dir.glob("stills/*.json")):
        where = str(p.relative_to(s.dir))
        st = json.loads(p.read_text(encoding="utf-8"))
        errs = _contract_errors("still", st, where)
        problems += errs
        if errs:
            continue
        if st["scan_id"] not in scan_ids:
            problems.append(f"{where}: scan_id {st['scan_id']!r} is not a scan in this session")
        if st["session_id"] != s.session_id:
            problems.append(f"{where}: session_id {st['session_id']!r} != {s.session_id!r}")
        if p.stem != st["still_id"]:
            problems.append(f"{where}: file name != still_id {st['still_id']!r}")
        jpeg(st["image_file"], where, (st["width_px"], st["height_px"]))
        for fld in ("depth_file", "confidence_file"):
            if st.get(fld):
                exists(st[fld], where)

    for pattern, kind in (("readings/*.json", "meter-reading"), ("parts/*.json", "part-on-hand")):
        for p in sorted(s.dir.glob(pattern)):
            where = str(p.relative_to(s.dir))
            data = json.loads(p.read_text(encoding="utf-8"))
            errs = _contract_errors(kind, data, where)
            problems += errs
            if not errs and kind == "meter-reading" and data["scan_id"] not in scan_ids:
                problems.append(f"{where}: scan_id {data['scan_id']!r} is not a scan in this session")
    return problems
