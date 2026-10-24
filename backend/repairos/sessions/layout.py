"""Where each capture file of a contract object lives in a session folder (samples/README.md).

`map_files(kind, data, fn)` returns a copy of a ScanPackage or Still with every file field passed through
`fn(value, base)`, where `base` is the field's place in the session folder without an extension (for example
`scans/<scan_id>/kf_000`). The writer uses it to turn blob refs into files, replay to turn files into blob refs.
"""
from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

FileFn = Callable[[str, str], str]
EXT_BY_TYPE = {"image/jpeg": ".jpg", "image/png": ".png", "application/x-ply": ".ply", "application/ply": ".ply"}
TYPE_BY_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".ply": "application/x-ply"}


def ext_for(data: bytes, content_type: str = "") -> str:
    """File extension for a capture file: from its magic bytes, else its content type, else .bin."""
    if data.startswith(b"ply\n"):
        return ".ply"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data.startswith(b"\x89PNG"):
        return ".png"
    return EXT_BY_TYPE.get(content_type, ".bin")


def map_files(kind: str, data: dict[str, Any], fn: FileFn) -> dict[str, Any]:
    d = copy.deepcopy(data)
    if kind == "scan-package":
        root = f"scans/{d['scan_id']}"
        if d.get("points_file"):  # absent when depth_source is none (no-LiDAR capture, A8)
            d["points_file"] = fn(d["points_file"], f"{root}/points_arkit_world")
        if d.get("mesh_file"):
            d["mesh_file"] = fn(d["mesh_file"], f"{root}/mesh_arkit_world")
        for kf in d.get("keyframes", []):
            k = kf["keyframe_id"]
            kf["image_file"] = fn(kf["image_file"], f"{root}/{k}")
            if kf.get("depth_file"):
                kf["depth_file"] = fn(kf["depth_file"], f"{root}/{k}_depth")
            if kf.get("confidence_file"):
                kf["confidence_file"] = fn(kf["confidence_file"], f"{root}/{k}_conf")
        for seg in d.get("segments", []):  # later capture segments (object flipped), each with its own cloud
            if seg.get("points_file"):
                seg["points_file"] = fn(seg["points_file"], f"{root}/points_segment{seg['segment']}_arkit_world")
        al = d.get("alignment")
        if al and al.get("design_points_file"):
            al["design_points_file"] = fn(al["design_points_file"], f"{root}/points_design")
    elif kind == "still":
        s = d["still_id"]
        d["image_file"] = fn(d["image_file"], f"stills/{s}")
        if d.get("depth_file"):
            d["depth_file"] = fn(d["depth_file"], f"stills/{s}_depth")
        if d.get("confidence_file"):
            d["confidence_file"] = fn(d["confidence_file"], f"stills/{s}_conf")
    return d


def file_values(kind: str, data: dict[str, Any]) -> list[str]:
    """Every file field value of a ScanPackage or Still."""
    out: list[str] = []
    map_files(kind, data, lambda v, _b: out.append(v) or v)
    return out
