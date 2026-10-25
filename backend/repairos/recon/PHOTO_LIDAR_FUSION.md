# Photos + LiDAR: an accurate 3D reconstruction for hardware

Research note, 2026-10-24. Goal: put the 12 MP stills and the LiDAR model in one metric frame accurately enough
to pin a finding to a solder pad, and use the stills to sharpen the geometry LiDAR alone cannot resolve.

## What exists on this branch

| Piece | Where | What it does |
|---|---|---|
| LiDAR TSDF | `recon/tsdf.py`, `pipeline/mesh.py`, `recreation/surface.py` | Depth + raw ARKit poses -> mesh, 3-5 mm voxels |
| Texturing | `recreation/texture.py` | Waechter-style view selection, one atlas, seam levelling. Colour only; geometry stays LiDAR |
| Flipped segment | `recon/segment_align.py` (server), `recon/merge.py` (Mac) | FPFH / SIFT bridge + ICP of the two segments' LiDAR clouds |
| Apple photogrammetry | `recon/build.py`, `recon/align.py` | PhotogrammetrySession mesh, sim(3) onto ARKit camera centres. Mac only |
| Gaussian splat | `recreation/splat.py`, `rohan/splat-test` | gsplat trained photometric only (L1 + D-SSIM), poses fixed, no depth loss |

## Why it is not accurate enough yet

1. **Poses are never refined.** Every still keeps its ARKit pose, which drifts by up to a few mm or more over a scan.
   B13 (backend pose refinement) is not built. A still projected onto the mesh lands mm off.
2. **No lens distortion.** `locate/camera.py` is a pure pinhole; near the image edges that is several pixels.
3. **Geometry is LiDAR-limited.** 256 x 192 depth with about 1 cm of noise; objects under about 8 cm are blobs
   (PLAN.md decision log). The stills (about 15 px/mm at 20 cm) carry the detail but only Apple's Mac path uses
   them for geometry.
4. **No ground truth.** `recon/bench.py` measures distance to Apple's mesh, not to the real object. We cannot
   state an accuracy in mm.

## Plan

### Step 0: measure

- Scan reference objects with known geometry: a 3D print of known dimensions, a ChArUco board beside a real PCB,
  features measured with calipers.
- Add to `bench.py`: mesh error in mm against the reference (Chamfer, median / p90) and still reprojection error
  in px (how far each still lands from the mesh). Every later step is judged by these numbers.

### Step 1: refine poses and intrinsics (B13, the biggest lever)

- Features across all stills and keyframes: SIFT (already in `merge.py`), or ALIKED (BSD-3) + LightGlue
  (Apache-2.0). Not SuperPoint: its weights are non-commercial (invariant L2).
- COLMAP (BSD-3) seeded with the ARKit poses: triangulate, then bundle-adjust poses, focal length and distortion.
- LiDAR depth anchors metric scale (depth residuals on matched keypoints, or a sim(3) back onto the ARKit
  camera centres as `align.py` already does).
- Output: refined still poses in the same `arkit_world` frame, so `locate` and findings need no contract change.

### Step 2: sharpen geometry from the stills (compare two arms)

- **A, MVS + LiDAR TSDF (baseline):** COLMAP PatchMatch on the pod (CUDA) gives per-still depth at full
  resolution; reject depth inconsistent with LiDAR; fuse both into one TSDF at about 1 mm inside the object box,
  LiDAR for low frequency, MVS for detail. Deterministic and debuggable.
- **B, depth-supervised Gaussian surface (extends `rohan/splat-test`):** add a LiDAR depth loss, gsplat's 2DGS
  mode (Apache-2.0) with normal consistency, and pose optimisation; extract the mesh by TSDF of rendered depths.
  Possibly sharper, harder to trust.

Pick by the Step 0 numbers, not by renders.

### Step 3: ship honestly

- The shipped mesh stays in the findings' frame; `recon.json` records the measured error and which arm built it.
- Avoid depth-prompting models that take iPhone LiDAR as input (e.g. Prompt Depth Anything) unless their weights
  are verified licence-clean.

## First experiment

Step 0 + Step 1 on an already-recorded session, backend only (`recon/**`), no app change. Report reprojection
error and reference-object error before and after refinement.
