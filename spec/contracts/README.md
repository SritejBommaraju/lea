# spec/contracts

JSON Schemas (draft 2020-12) for every object RepairOS stores or exchanges with Grain. They are the cross-language
source of truth; `check.py` keeps them honest against our examples and, when you have them, the private Grain
fixtures.

```
python3 -I spec/contracts/check.py            # Windows: python -I spec/contracts/check.py
python3 -I spec/contracts/check.py --fallback # force the stdlib validator even if jsonschema is installed
```

Exit 0 means: every example validates, every negative example fails, and (with `private/grain/`) every recorded
Grain response matches its contract. Without `private/grain/` it prints "private Grain fixtures not present;
skipped" and the public checks decide. The **gap ledger** at the end lists the target fields Grain does not send
yet (measured only from the private fixtures). Gaps never fail the run.

**Grain is proprietary.** Only the API surface we call lives here: endpoints, field shapes, units and frames.
Recorded Grain responses, the recorder and the Grain spec sheet are in `private/grain/` (gitignored; ask Sritej).

## Two kinds of schema

| Kind | Files | Rules |
|---|---|---|
| RepairOS objects | `finding`, `suspect`, `meter-reading`, `next-action`, `still`, `scan-package`, `reference-design`, `repair-plan`, `part-on-hand`, `shopping-quote` | Ours. `additionalProperties: false`. Examples stay generic (invariant GEN1: no product names). |
| Grain exchanges | `grain-scan-record`, `grain-diagnose-response`, `grain-look-response`, `grain-electrical-reference`, `grain-electrical-check-request`, `grain-electrical-check-response`, `grain-call-request`, `grain-call-response`, `grain-error` | Grain owns the shape. Each one matches the private recorded Grain responses, plus target fields marked `"x-target": "T<n>"` (Grain targets, tracked privately). Grain's real names are never renamed. `additionalProperties: true`. |

Nested Grain shapes are reused across files rather than copied: for example `grain-call-response` points
`diagnosis`, `electrical`, `photos` and `scan` at the endpoint schemas (`"$ref": "grain-diagnose-response.schema.json"`),
and `meter-reading.grain_row` points at `grain-electrical-check-response.schema.json#/$defs/row`.

## Annotations (ignored by validators, read by check.py)

| Keyword | Where | Meaning |
|---|---|---|
| `x-target` | any property | The field is a target addition (T1–T12), optional until Grain ships it. |
| `x-target-values` | an enum | Enum values that are targets; the others exist now. |
| `x-target-required` | schema root | `[{"path": "readings[*].verdict", "target": "T2"}]`: Grain should send this once the target lands. Paths are dot-separated keys from the root; `[*]` means every array element; a null or absent parent means "nothing to check". |
| `x-target-checks` | schema root | `[{"check": "range_verdict", "path": "readings[*]", "target": "T1"}]`: a named semantic test from `CHECKS` in check.py. |

## Files

- `examples/<name>.json`, plus optional `examples/<name>.<label>.json`, must validate. Public `grain-*` examples
  show the API shape only: built from the schema with generic names, never copied from a recorded response. A
  `*.target.json` example is the shape once the targets land; it must also meet every `x-target-required` and
  `x-target-checks` entry.
- `examples/invalid/*.json` must fail: `{"schema", "why", "instance"}` or `{"schema", "why", "base", "patch"}` (an
  RFC 7386 merge patch on a valid example; `null` deletes a key).
- Private (not in the repo): `private/grain/examples/` (more examples and `invalid/`, checked the same way) and
  `private/grain/observed/` (recorded Grain responses plus a `MAP.json` that maps each fixture to the schema for its
  `body` and `request`, to `request_must_fail`, or to `skip` with a reason; an unlisted fixture fails). Sritej
  re-records them after any Grain change.

## Units and frames

The design frame is the reference `out/model.glb`: **millimetres, Z up**. Every `*_mm` field from Grain is in it. ARKit
is metres, Y up: `*_m` fields and `frame: arkit_world`. `still.camera_design` is `position_mm`;
`scan-package.alignment.transform_arkit_world_to_design` maps ARKit metres to design millimetres (it includes the
Y-up to Z-up turn). Board placement (`main_placement.csv`) is in board mm, not in the design frame.
