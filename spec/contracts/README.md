# spec/contracts

JSON Schemas (draft 2020-12) for every object RepairOS stores or exchanges. They are the cross-language source of
truth; `check.py` keeps them honest against our examples.

```
python3 -I spec/contracts/check.py            # Windows: python -I spec/contracts/check.py
python3 -I spec/contracts/check.py --fallback # force the stdlib validator even if jsonschema is installed
```

Exit 0 means: every example validates and every negative example fails.

## Schemas

| Files | Rules |
|---|---|
| `finding`, `suspect`, `meter-reading`, `next-action`, `still`, `scan-package`, `report`, `repair-plan`, `part-on-hand`, `shopping-quote` | Ours. `additionalProperties: false`. Examples stay generic (invariant GEN1: no product names). |

## Files

- `examples/<name>.json`, plus optional `examples/<name>.<label>.json`, must validate.
- `examples/invalid/*.json` must fail: `{"schema", "why", "instance"}` or `{"schema", "why", "base", "patch"}` (an
  RFC 7386 merge patch on a valid example; `null` deletes a key).

## Units and frames

The design frame is the reference model's frame: **millimetres, Z up**. Every `*_mm` field is in it. ARKit is metres,
Y up: `*_m` fields and `frame: arkit_world`. `still.camera_design` is `position_mm`;
`scan-package.alignment.transform_arkit_world_to_design` maps ARKit metres to design millimetres (it includes the
Y-up to Z-up turn). Board placement is in board mm, not in the design frame.
