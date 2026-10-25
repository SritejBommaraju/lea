"""A textured mesh as a USDZ: one ASCII USD layer (a Mesh with `st` coordinates and a UsdPreviewSurface whose diffuse
colour is the photo atlas) plus the atlas JPEG, in an uncompressed zip whose every file starts on a 64-byte boundary
(the USDZ rules: https://openusd.org/release/spec_usdz.html). Standard library only. SceneKit, Model I/O, RealityKit
and AR Quick Look load it as is; the points stay in the scan's ARKit world (metres, Y up), so the findings' points
land on it without a transform.
"""
from __future__ import annotations

import io
import struct
import zlib

import numpy as np

ALIGN = 64


def usda_text(vertices: np.ndarray, uv: np.ndarray, faces: np.ndarray, texture: str = "texture.jpg") -> str:
    """The USD layer. `uv` has its origin at the atlas image's top-left (v down); USD's `st` has t up, so t = 1 - v."""
    v = np.asarray(vertices, np.float64)
    st = np.column_stack([uv[:, 0], 1.0 - uv[:, 1]])
    f = np.asarray(faces, np.int64).reshape(-1)
    pts = ", ".join(f"({x:.5f}, {y:.5f}, {z:.5f})" for x, y, z in v)
    sts = ", ".join(f"({a:.6f}, {b:.6f})" for a, b in st)
    idx = ", ".join(map(str, f.tolist()))
    counts = ", ".join(["3"] * (len(f) // 3))
    lo, hi = v.min(0), v.max(0)
    m = "/Recreation/Materials/Photo"
    return f"""#usda 1.0
(
    defaultPrim = "Recreation"
    metersPerUnit = 1
    upAxis = "Y"
    doc = "repairos recreation: the scanned object, textured from its photos, in the scan's ARKit world frame"
)

def Xform "Recreation" (
    kind = "component"
)
{{
    def Mesh "Object"
    {{
        uniform bool doubleSided = 1
        float3[] extent = [({lo[0]:.5f}, {lo[1]:.5f}, {lo[2]:.5f}), ({hi[0]:.5f}, {hi[1]:.5f}, {hi[2]:.5f})]
        int[] faceVertexCounts = [{counts}]
        int[] faceVertexIndices = [{idx}]
        point3f[] points = [{pts}]
        texCoord2f[] primvars:st = [{sts}] (
            interpolation = "vertex"
        )
        uniform token subdivisionScheme = "none"
        rel material:binding = <{m}>
    }}

    def Scope "Materials"
    {{
        def Material "Photo"
        {{
            token outputs:surface.connect = <{m}/Surface.outputs:surface>

            def Shader "Surface"
            {{
                uniform token info:id = "UsdPreviewSurface"
                color3f inputs:diffuseColor.connect = <{m}/Texture.outputs:rgb>
                float inputs:metallic = 0
                float inputs:roughness = 0.9
                token outputs:surface
            }}

            def Shader "Texture"
            {{
                uniform token info:id = "UsdUVTexture"
                asset inputs:file = @{texture}@
                float2 inputs:st.connect = <{m}/St.outputs:result>
                token inputs:wrapS = "clamp"
                token inputs:wrapT = "clamp"
                float3 outputs:rgb
            }}

            def Shader "St"
            {{
                uniform token info:id = "UsdPrimvarReader_float2"
                token inputs:varname = "st"
                float2 outputs:result
            }}
        }}
    }}
}}
"""


def _zip_stored(files: list[tuple[str, bytes]]) -> bytes:
    """An uncompressed zip with each file's data 64-byte aligned (padding in the local header's extra field)."""
    out = io.BytesIO()
    central = []
    for name, data in files:
        nb = name.encode()
        offset = out.tell()
        pad = (-(offset + 30 + len(nb) + 4)) % ALIGN
        extra = struct.pack("<HH", 0x1986, pad) + b"\0" * pad       # an unregistered padding block
        crc = zlib.crc32(data) & 0xFFFFFFFF
        out.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, 0, 0, 0x21, crc, len(data), len(data), len(nb),
                              len(extra)))
        out.write(nb + extra)
        assert out.tell() % ALIGN == 0
        out.write(data)
        central.append((nb, crc, len(data), offset))
    cd_start = out.tell()
    for nb, crc, size, offset in central:
        out.write(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0, 0, 0, 0x21, crc, size, size, len(nb), 0, 0,
                              0, 0, 0, offset))
        out.write(nb)
    cd_size = out.tell() - cd_start
    out.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(central), len(central), cd_size, cd_start, 0))
    return out.getvalue()


def usdz_bytes(vertices: np.ndarray, uv: np.ndarray, faces: np.ndarray, texture_jpeg: bytes) -> bytes:
    layer = usda_text(vertices, uv, faces, "texture.jpg").encode("utf-8")
    return _zip_stored([("recreation.usda", layer), ("texture.jpg", texture_jpeg)])
