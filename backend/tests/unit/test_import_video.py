"""repairos.importers.video against ffmpeg itself: frame counts, scaling and selection by index. The clip is
ffmpeg's own generated test pattern (lavfi testsrc2), a decoder fixture, not a capture. Skips without ffmpeg."""
from __future__ import annotations

import shutil
import subprocess

import numpy as np
import pytest

from repairos.importers import video

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="HUMAN ACTION NEEDED: install ffmpeg (brew install ffmpeg)")


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    path = tmp_path_factory.mktemp("video") / "pattern.mp4"
    for codec in ("libx264", "mpeg4"):
        r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=60:duration=0.2",
                            "-c:v", codec, "-pix_fmt", "yuv420p", "-y", str(path)], capture_output=True)
        if r.returncode == 0:
            return path
    pytest.skip("ffmpeg has no libx264 or mpeg4 encoder")


def test_probe_and_every_frame_once(clip):
    info = video.probe(clip)
    assert (info["width"], info["height"]) == (64, 48) and len(info["pts"]) == 12
    full = list(video.frames(clip))
    assert [k for k, _ in full] == list(range(12)) and full[0][1].shape == (48, 64, 3)
    small = list(video.frames(clip, size=(32, 24)))
    assert len(small) == 12 and small[3][1].shape == (24, 32, 3)


def test_selected_frames_are_the_same_frames(clip):
    full = dict(video.frames(clip))
    picked = list(video.frames(clip, select=[7, 2]))
    assert [k for k, _ in picked] == [2, 7]
    for k, img in picked:
        assert np.array_equal(img, full[k])
    assert not np.array_equal(full[2], full[7])
