"""Known lengths: a reference corrects the scale, a holdout tests it (S5: numbers compared in code).

    factor = reference true length / its measured length
    every dimension x factor; its sigma is its shape sigma (the scan's size error cancels against the reference's)
    plus the factor's relative sigma
    holdout: |corrected holdout - its true length| <= tolerance  ->  the scan's dimensions whose own sigma is within
             the tolerance are `verified`

A reference or holdout length is the person's (calipers, a ruler, a datasheet they name). It is reported as such
(`method: "given"`) and never re-derived from the scan. Without a holdout nothing is verified, whatever the sigmas say.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

LARGE_CORRECTION = 0.05   # a factor further from 1 than this usually means a mis-placed mark; said, never hidden


def _shape(d: Mapping[str, Any]) -> float:
    """A dimension's sigma without the size's (its total when the caller gave no split)."""
    return float(d["shape_uncertainty_mm"] if d.get("shape_uncertainty_mm") is not None else d["uncertainty_mm"])


def apply(dims: list[dict], reference: Mapping[str, Any] | None, holdout: Mapping[str, Any] | None,
          tolerance_mm: float) -> tuple[list[dict], dict]:
    """(dimensions corrected and marked, the check record). `dims` items carry name, value_mm, uncertainty_mm."""
    by = {d["name"]: d for d in dims}
    check: dict[str, Any] = {"tolerance_mm": tolerance_mm, "factor": 1.0, "sigma_factor": 0.0}
    notes: list[str] = []
    if reference:
        ref = by.get(reference["dimension"])
        if ref is None or not ref.get("value_mm"):
            raise ValueError(f"reference {reference['dimension']!r} was not measured")
        true, u_true = float(reference["true_mm"]), float(reference.get("uncertainty_mm") or 0.0)
        f = true / ref["value_mm"]
        rel = ((_shape(ref) / ref["value_mm"]) ** 2 + (u_true / true) ** 2) ** 0.5
        check.update(factor=f, sigma_factor=f * rel, reference=reference["dimension"],
                     reference_measured_mm=ref["value_mm"], reference_true_mm=true)
        if abs(f - 1) > LARGE_CORRECTION:
            notes.append(f"the reference changes the scale by {100 * (f - 1):+.1f}%: check its marks")
    f, sf = check["factor"], check["sigma_factor"]
    out = []
    for d in dims:
        # with a reference the scan's own size error cancels (the reference has it too): shape sigma + the factor's
        u0 = _shape(d) if reference else d["uncertainty_mm"]
        v, u = d["value_mm"] * f, ((u0 * f) ** 2 + (d["value_mm"] * sf) ** 2) ** 0.5
        out.append({**d, "value_mm": v, "uncertainty_mm": u, "measured_mm": d["value_mm"], "verified": False})
    passed = False
    if holdout:
        h = next((d for d in out if d["name"] == holdout["dimension"]), None)
        if h is None:
            raise ValueError(f"holdout {holdout['dimension']!r} was not measured")
        if reference and holdout["dimension"] == reference["dimension"]:
            raise ValueError("the holdout must be a different length from the reference")
        true = float(holdout["true_mm"])
        err = h["value_mm"] - true
        passed = abs(err) <= tolerance_mm
        check.update(holdout=holdout["dimension"], holdout_true_mm=true, holdout_measured_mm=h["value_mm"],
                     holdout_error_mm=err, holdout_sigma_mm=h["uncertainty_mm"], passed=passed,
                     sigma_covers_error=abs(err) <= 2 * max(h["uncertainty_mm"], 1e-9))
        if not check["sigma_covers_error"]:
            notes.append("the holdout error is outside twice its stated uncertainty: the uncertainties are too small "
                         "on this scan")
        if not passed:
            notes.append(f"the holdout is off by {err:+.2f} mm (tolerance {tolerance_mm} mm): nothing is verified")
    else:
        notes.append("no holdout length: nothing is verified")
    for d in out:
        given = (reference and d["name"] == reference["dimension"]) or (holdout and d["name"] == holdout["dimension"])
        if given:
            spec = reference if reference and d["name"] == reference["dimension"] else holdout
            d.update(value_mm=float(spec["true_mm"]), uncertainty_mm=float(spec.get("uncertainty_mm") or 0.0),
                     method="given", verified=True)
        else:   # a passed holdout vouches for the scan, not for a dimension whose own sigma exceeds the tolerance
            d["verified"] = passed and d["uncertainty_mm"] <= tolerance_mm
    check["notes"] = notes
    return out, check
