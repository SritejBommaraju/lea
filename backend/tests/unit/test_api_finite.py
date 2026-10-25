"""GET .../report must never 500 on a stored non-finite stat (a 2026-10-25 scan's report carried an inf)."""
import json
import math

from repairos.api.app import finite_json


def test_non_finite_floats_become_null_and_the_rest_is_kept():
    report = {"mesh": {"triangles": 3, "ratio": math.inf, "shift": -math.inf, "share": math.nan, "size_m": 0.068},
              "list": [1.5, math.inf, {"x": math.nan}], "name": "UNO R3", "ok": True}
    out = finite_json(report)
    json.dumps(out, allow_nan=False)
    assert out["mesh"] == {"triangles": 3, "ratio": None, "shift": None, "share": None, "size_m": 0.068}
    assert out["list"] == [1.5, None, {"x": None}]
    assert out["name"] == "UNO R3" and out["ok"] is True
