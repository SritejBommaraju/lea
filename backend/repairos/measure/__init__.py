"""Measurements in millimetres from a scan's posed stills: pixels for where edges are, LiDAR and camera motion for
how far away they are (recon/PHOTO_LIDAR_FUSION.md steps 0-1).

    views.py     a still as a measuring view: grey image, ARKit pose, pinhole, depth grid
    tracks.py    the same surface point found across stills (seeded by pose + depth, pyramidal KLT on patches warped
                 by the local surface, checked both ways)
    adjust.py    bundle adjustment: still poses and track points, LiDAR depths as a loose prior with one offset,
                 ARKit poses as a weak prior (metric: visual-inertial odometry); lens terms only on request
    size.py      how sure the scene's size is: one scale against thousands of LiDAR depths, and whether ARKit's
                 camera motion agrees
    dimension.py a marked point in 3D (ray onto a local surface, or parallax over two or more stills), distances
                 and hole diameters with an uncertainty
    reference.py a known length corrects the scale; a second known length is the holdout that decides `verified`
    run.py       measure(stills, ..., request) -> the measurement record the Grain handoff carries; tracking and
                 adjustment run twice (nearest stills, then every still that looks the same way)

Numbers are compared in code (S5). A dimension is `verified` only when a holdout length passed on the same scan;
otherwise it carries its uncertainty and `verified: false`. Numpy and Pillow only (L2: no OpenCV wheels).
"""
