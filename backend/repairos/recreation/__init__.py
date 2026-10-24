"""Photo-real recreation of the scanned object for the report's 3D hero.

- `texture`: the object's LiDAR mesh textured from the 12 MP stills (and the keyframes where no still sees a face):
  per face the best view (projected size, sharpness, visibility), smoothed so neighbours share a view, one atlas made
  of the source photos' own pixels, exposure levelled across seams. CPU only (numpy + Open3D), seconds.
- `usdz`: that mesh as a USDZ (SceneKit, Model I/O and Quick Look load it as is).
- `splat`: a Gaussian splat trained from the same posed stills on the GPU pod (gsplat, Apache-2.0), cropped to the
  object box, in the scan's frame. Needs CUDA; the textured mesh is the result everywhere else.
- `service`: builds the recreation once per scan revision and records it on the run row (`recreation`, the live
  event `recreation_ready`).

Everything stays in the scan's ARKit world frame (metres, Y up), the frame of every finding's `location.point_m`.
"""
