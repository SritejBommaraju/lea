#!/usr/bin/env python3
"""Check the contracts in spec/contracts/ against our examples and, when present, the private Grain fixtures.

Run:  python3 -I spec/contracts/check.py            (from the repo root or anywhere; Windows: python -I ...)
      python3 -I spec/contracts/check.py --fallback (force the built-in validator even if jsonschema is installed)

What it does, in order:
1. Every `*.schema.json` is draft 2020-12 and has at least one example `examples/<name>.json` (more may sit at
   `examples/<name>.<label>.json`). Every example must validate. An example with no schema is a failure. An
   example labelled `target` (`<name>.target.json`) shows the shape once Grain ships the targets, so it must also
   meet every x-target-required / x-target-checks entry of its schema (keeps the ledger's targets coherent).
2. Every `examples/invalid/*.json` is `{"schema": "<name>", "why": "...", "instance": {...}}`, or
   `{"schema", "why", "base": "<example file>", "patch": {...}}` (an RFC 7386 merge patch on a valid example: null
   deletes a key, arrays are replaced whole). It must FAIL validation.
3. PRIVATE Grain material (Grain is proprietary; `private/` is gitignored, teammates get it from Sritej):
   `private/grain/examples/` (more examples, plus `invalid/`) is checked exactly like steps 1-2, and every fixture in
   `private/grain/observed/` (recorded Grain responses) is listed in its `MAP.json`: `{"body": <schema>, "request": <schema>, "request_must_fail": <schema>}` or
   `{"skip": "<reason>"}`. Mapped bodies and requests must validate (a failure here is drift: Grain's real shape
   no longer matches our contract). An unlisted fixture is a failure. When `private/grain/` is absent this prints
   one line, "private Grain fixtures not present; skipped", and the public checks still decide the exit code.
4. The GAP LEDGER: a schema may declare what Grain should send once a target (T1-T13, SPEC.md §10) lands:
       "x-target-required": [{"path": "readings[*].verdict", "target": "T2"}]
       "x-target-checks":   [{"check": "range_verdict", "path": "readings[*]", "target": "T1"}]
   Paths are dot-separated keys from the document root; `[*]` means every element of an array. A null or absent
   parent means "nothing to check here". `x-target-required` asks that the last key be present; a check is a named
   semantic test in CHECKS below. Each recorded fixture is measured; the ledger prints `gap` (Grain does not do it
   yet) or `met` (Grain now does it) per target. Gaps never fail the run.

Validator: the `jsonschema` package when it is installed (with `referencing` for cross-file $refs), else a small
stdlib-only validator covering what these contracts use: type, properties, required, additionalProperties, items,
minItems, maxItems, enum, const, minimum, maximum, exclusiveMinimum, exclusiveMaximum, minLength, pattern, $ref
(local `#/...` and sibling files `other.schema.json#/...`), allOf, anyOf, oneOf, not, if/then/else. `format` and
`x-*` annotations are not checked.

Exit code 0 when everything above holds; 1 otherwise.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE / "examples"
INVALID = EXAMPLES / "invalid"
PRIVATE = HERE.parent.parent / "private" / "grain"      # gitignored; from Sritej
PRIVATE_EXAMPLES = PRIVATE / "examples"
OBSERVED = PRIVATE / "observed"
MAP_FILE = OBSERVED / "MAP.json"
DRAFT = "https://json-schema.org/draft/2020-12/schema"

TARGETS = {
    "T1": "voltage judged against [min, max]",
    "T2": "a verdict on every reading, joined to its finding",
    "T3": "structured fix subjects and sources",
    "T4": "RepairOS findings in /call",
    "T5": "photos by reference, with metadata",
    "T6": "long work detached",
    "T7": "pin positions in the design frame; one pin naming",
    "T8": "expected values everywhere the meter goes",
    "T9": "scan frame and gravity",
    "T10": "versioned, retry-safe",
    "T11": "project copy and cache validation",
    "T12": "diagnose holds on real phone scans",
    "T13": "a removed part is never ok at phone noise",
}


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


SCHEMA_FILES: dict[str, dict] = {p.name: _read(p) for p in sorted(HERE.glob("*.schema.json"))}


# ----------------------------------------------------------------------------- fallback validator

class SchemaError(Exception):
    pass


def _type_ok(value, t: str) -> bool:
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)) or (isinstance(value, float) and value.is_integer())
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "null":
        return value is None
    raise SchemaError(f"unsupported type keyword: {t}")


def _pointer(doc: dict, frag: str, ref: str):
    node = doc
    if frag in ("", "/"):
        return node
    if not frag.startswith("/"):
        raise SchemaError(f"only JSON-pointer fragments are supported, got {ref}")
    for part in frag[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise SchemaError(f"unresolvable ref {ref}")
        node = node[part]
    return node


def _resolve_ref(ref: str, root: dict):
    """Returns (subschema, its document). Supports '#/...' and 'sibling.schema.json#/...'."""
    file, _, frag = ref.partition("#")
    if file:
        if file not in SCHEMA_FILES:
            raise SchemaError(f"ref to unknown schema file {file!r} ({ref})")
        root = SCHEMA_FILES[file]
    return _pointer(root, frag, ref), root


def _validate(value, schema, root: dict, path: str, errors: list[str]) -> None:
    if schema is True:
        return
    if schema is False:
        errors.append(f"{path}: schema is false")
        return
    if not isinstance(schema, dict):
        raise SchemaError(f"{path}: schema must be an object")

    if "$ref" in schema:
        sub, sub_root = _resolve_ref(schema["$ref"], root)
        _validate(value, sub, sub_root, path, errors)
        rest = {k: v for k, v in schema.items() if k != "$ref"}
        if rest:
            _validate(value, rest, root, path, errors)
        return

    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(value, t) for t in types):
            errors.append(f"{path}: expected type {types}, got {type(value).__name__}")
            return

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum {schema['enum']}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: {value} <= exclusiveMinimum {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: {value} >= exclusiveMaximum {schema['exclusiveMaximum']}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: string shorter than minLength {schema['minLength']}")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(f"{path}: {value!r} does not match pattern {schema['pattern']!r}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: {len(value)} items < minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: {len(value)} items > maxItems {schema['maxItems']}")
        if "items" in schema:
            for i, item in enumerate(value):
                _validate(item, schema["items"], root, f"{path}[{i}]", errors)

    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required property {key!r}")
        for key, sub in props.items():
            if key in value:
                _validate(value[key], sub, root, f"{path}.{key}", errors)
        addl = schema.get("additionalProperties", True)
        extra = [k for k in value if k not in props]
        if addl is False and extra:
            errors.append(f"{path}: unexpected properties {extra}")
        elif isinstance(addl, dict):
            for k in extra:
                _validate(value[k], addl, root, f"{path}.{k}", errors)

    def ok(sub) -> bool:
        sub_errors: list[str] = []
        _validate(value, sub, root, path, sub_errors)
        return not sub_errors

    for combinator in ("allOf", "anyOf", "oneOf"):
        if combinator not in schema:
            continue
        if combinator == "allOf":
            for sub in schema["allOf"]:
                _validate(value, sub, root, path, errors)
            continue
        passed = sum(ok(sub) for sub in schema[combinator])
        if combinator == "anyOf" and passed == 0:
            errors.append(f"{path}: anyOf: no branch matched")
        elif combinator == "oneOf" and passed != 1:
            errors.append(f"{path}: oneOf: {passed} branches matched, expected exactly 1")

    if "not" in schema and ok(schema["not"]):
        errors.append(f"{path}: matches a schema it must not match")

    if "if" in schema:
        branch = schema.get("then") if ok(schema["if"]) else schema.get("else")
        if branch is not None:
            _validate(value, branch, root, path, errors)


def validate_fallback(instance, schema: dict) -> list[str]:
    errors: list[str] = []
    _validate(instance, schema, schema, "$", errors)
    return errors


def pick_validator(force_fallback: bool):
    if not force_fallback:
        try:
            import jsonschema  # type: ignore
            try:
                from referencing import Registry, Resource  # type: ignore

                registry = Registry().with_resources(
                    (s.get("$id", name), Resource.from_contents(s)) for name, s in SCHEMA_FILES.items())

                def make(schema):
                    return jsonschema.Draft202012Validator(schema, registry=registry)
            except ImportError:  # older jsonschema: RefResolver with a store of sibling files
                store = {s.get("$id", name): s for name, s in SCHEMA_FILES.items()}

                def make(schema):
                    resolver = jsonschema.RefResolver(base_uri=schema.get("$id", ""), referrer=schema, store=store)
                    return jsonschema.Draft202012Validator(schema, resolver=resolver)

            def validate(instance, schema):
                return [f"{'/'.join(str(p) for p in e.absolute_path) or '$'}: {e.message}"
                        for e in make(schema).iter_errors(instance)]

            try:
                from importlib.metadata import version
                ver = version("jsonschema")
            except Exception:
                ver = "?"
            return validate, f"jsonschema {ver}"
        except ImportError:
            pass
    return validate_fallback, "built-in subset validator" + ("" if force_fallback else " (jsonschema not installed)")


# ----------------------------------------------------------------------------- gap ledger

def select(doc, path: str):
    """All (location, node) reached by a path like 'electrical.readings[*]'. '' is the root."""
    toks = re.findall(r"\[\*\]|[^.\[\]]+", path)
    found = [("$", doc)]
    for t in toks:
        nxt = []
        for loc, node in found:
            if t == "[*]":
                if isinstance(node, list):
                    nxt += [(f"{loc}[{i}]", x) for i, x in enumerate(node)]
            elif isinstance(node, dict) and node.get(t) is not None:
                nxt.append((f"{loc}.{t}", node[t]))
        found = nxt
    return found


def required_at(doc, path: str):
    """For 'a[*].b': one (location, present) per element of a."""
    parent, _, key = path.rpartition(".")
    if "[" in key:
        raise SchemaError(f"x-target-required path must end in a key: {path}")
    return [(f"{loc}.{key}", isinstance(node, dict) and key in node) for loc, node in select(doc, parent)]


def _check_range_verdict(row):
    """T1: a voltage inside [min - tol, max + tol] is ok, outside is off (tol = 0.25 V or 5 %, the larger)."""
    exp = row.get("expected") if isinstance(row, dict) else None
    if not (isinstance(row.get("volts"), (int, float)) and isinstance(exp, dict)
            and isinstance(exp.get("min"), (int, float)) and isinstance(exp.get("max"), (int, float))):
        return None
    v, lo, hi = row["volts"], exp["min"], exp["max"]
    tol = max(0.25, 0.05 * max(abs(lo), abs(hi)))
    want = "ok" if lo - tol <= v <= hi + tol else "off"
    got = row.get("verdict")
    return got == want, f"{row.get('net')} {v} V in [{lo:.3g}, {hi:.3g}] should be {want!r}, Grain says {got!r}"


def _check_power_nets_expected(doc):
    """T8: every net marked power (other than ground, the meter's reference) has an expected voltage."""
    exp = doc.get("expected_volts") or {}
    return [(n["name"] in exp, f"power net {n['name']} has no expected_volts")
            for n in doc.get("nets") or []
            if isinstance(n, dict) and n.get("power") and str(n.get("name")).upper() not in ("GND", "0V", "VSS")]


CHECKS = {
    "range_verdict": lambda node: [r] if (r := _check_range_verdict(node)) else [],
    "power_nets_expected": _check_power_nets_expected,
}


def ledger_for(schema: dict, doc) -> list[dict]:
    """One row per declared target item: {target, what, total, missing, examples}."""
    rows = []
    for item in schema.get("x-target-required", []):
        hits = required_at(doc, item["path"])
        miss = [loc for loc, present in hits if not present]
        rows.append({"target": item["target"], "what": item["path"], "verb": "has it", "total": len(hits), "missing": len(miss),
                     "examples": miss[:2]})
    for item in schema.get("x-target-checks", []):
        fn = CHECKS.get(item["check"])
        if fn is None:
            raise SchemaError(f"unknown x-target-check {item['check']!r}")
        results = []
        for loc, node in select(doc, item["path"]):
            results += [(loc, ok, msg) for ok, msg in fn(node)]
        bad = [f"{loc}: {msg}" for loc, ok, msg in results if not ok]
        rows.append({"target": item["target"], "what": f"{item['check']} @ {item['path'] or '$'}", "verb": "passes", "total": len(results),
                     "missing": len(bad), "examples": bad[:3]})
    return rows


def collect_targets(node, out: set[str]) -> None:
    if isinstance(node, dict):
        t = node.get("x-target")
        if isinstance(t, str):
            out.add(t)
        for v in node.values():
            collect_targets(v, out)
    elif isinstance(node, list):
        for v in node:
            collect_targets(v, out)


def merge_patch(target, patch):
    """RFC 7386 JSON merge patch."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


# ----------------------------------------------------------------------------- driver

def main(argv: list[str]) -> int:
    validate, engine = pick_validator("--fallback" in argv)
    failures = 0

    def run(instance, schema) -> list[str]:
        try:
            return validate(instance, schema)
        except SchemaError as exc:
            return [f"schema error: {exc}"]

    def schema_named(name: str):
        return SCHEMA_FILES.get(f"{name}.schema.json")

    print(f"validator: {engine}")

    have_private = PRIVATE.is_dir()
    example_roots = [(EXAMPLES, "")] + ([(PRIVATE_EXAMPLES, "private:")] if PRIVATE_EXAMPLES.is_dir() else [])

    def check_example(ex: Path, schema: dict, tag: str) -> int:
        bad = 0
        errors = run(_read(ex), schema)
        label = tag + ex.name.removesuffix(".json")
        if errors:
            bad += 1
            print(f"FAIL {label}")
            for e in errors[:20]:
                print(f"     {e}")
        else:
            print(f"ok   {label}")
        if ex.name.endswith(".target.json"):
            for r in (r for r in ledger_for(schema, _read(ex)) if r["missing"]):
                bad += 1
                print(f"FAIL {label}: target {r['target']} not met: {r['what']} {r['examples']}")
        return bad

    # 1. schemas and their examples (the public examples alone must cover every schema)
    print("\n-- examples (must validate)")
    for file, schema in SCHEMA_FILES.items():
        name = file.removesuffix(".schema.json")
        if schema.get("$schema") != DRAFT:
            print(f"FAIL {name}: $schema is not draft 2020-12")
            failures += 1
            continue
        for root, tag in example_roots:
            examples = sorted(root.glob(f"{name}.json")) + sorted(root.glob(f"{name}.*.json"))
            if not examples and not tag:
                print(f"FAIL {name}: no example at examples/{name}.json")
                failures += 1
            for ex in examples:
                failures += check_example(ex, schema, tag)
    for root, tag in example_roots:
        orphans = sorted(p.name for p in root.glob("*.json") if p.name.split(".")[0] + ".schema.json" not in SCHEMA_FILES)
        if orphans:
            failures += 1
            print(f"FAIL {tag}examples without a schema: {orphans}")

    # 2. negative examples
    print("\n-- examples/invalid (must fail)")
    for root, tag in example_roots:
        for neg in sorted((root / "invalid").glob("*.json")):
            case = _read(neg)
            schema = schema_named(case.get("schema", ""))
            base = next((r / case["base"] for r in (root, EXAMPLES) if "base" in case and (r / case["base"]).exists()), None)
            if base is not None:
                case["instance"] = merge_patch(_read(base), case.get("patch", {}))
            if schema is None or "instance" not in case:
                failures += 1
                print(f"FAIL {tag}{neg.name}: needs a known 'schema' and an 'instance' (or an existing 'base' + 'patch')")
                continue
            errors = run(case["instance"], schema)
            if errors:
                print(f"ok   {tag}{neg.name} rejected by {case['schema']}: {errors[0]}")
            else:
                failures += 1
                print(f"FAIL {tag}{neg.name}: {case['schema']} accepted it ({case.get('why', '')})")

    # 3. recorded Grain responses (private)
    ledger: dict[str, list[tuple[str, dict]]] = {}
    if not (have_private and OBSERVED.is_dir()):
        print("\nprivate Grain fixtures not present; skipped")
    else:
        print("\n-- private Grain fixtures (recorded responses; a failure is drift)")
        mapping = _read(MAP_FILE) if MAP_FILE.exists() else {}
        for fx in sorted(OBSERVED.glob("*.json")):
            if fx.name == MAP_FILE.name:
                continue
            entry = mapping.get(fx.name)
            if entry is None:
                failures += 1
                print(f"FAIL {fx.name}: not listed in MAP.json")
                continue
            if "skip" in entry:
                print(f"skip {fx.name}: {entry['skip']}")
                continue
            rec = _read(fx)
            for part, key, must_fail in (("body", "body", False), ("request", "request", False),
                                         ("request", "request_must_fail", True)):
                name = entry.get(key)
                if not name:
                    continue
                schema = schema_named(name)
                if schema is None:
                    failures += 1
                    print(f"FAIL {fx.name}: MAP names unknown schema {name!r}")
                    continue
                errors = run(rec.get(part), schema)
                tag = f"{fx.name} {part} [{rec.get('_status')}] -> {name}"
                if must_fail:
                    if errors:
                        print(f"ok   {tag} rejects it, as Grain did: {errors[0]}")
                    else:
                        failures += 1
                        print(f"FAIL {tag}: Grain rejected this request but our schema accepts it")
                    continue
                if errors:
                    failures += 1
                    print(f"FAIL {tag}")
                    for e in errors[:20]:
                        print(f"     {e}")
                else:
                    print(f"ok   {tag}")
                if part == "body":
                    try:
                        for row in ledger_for(schema, rec["body"]):
                            ledger.setdefault(row["target"], []).append((fx.name, row))
                    except SchemaError as exc:
                        failures += 1
                        print(f"FAIL {name}: {exc}")
        unknown = sorted(set(mapping) - {p.name for p in OBSERVED.glob("*.json")} - {"_comment"})
        if unknown:
            failures += 1
            print(f"FAIL MAP.json lists fixtures that do not exist: {unknown}")

    # 4. gap ledger
    print("\n-- GAP LEDGER (target fields Grain does not send yet; gaps never fail the run)")
    measured = set(ledger)
    gaps = 0
    for t in sorted(ledger, key=lambda k: int(k[1:])):
        print(f"{t:4} {TARGETS.get(t, '')}")
        by_what: dict[str, list[tuple[str, dict]]] = {}
        for fixture, row in ledger[t]:
            by_what.setdefault(row["what"], []).append((fixture, row))
        for what, hits in by_what.items():
            hits = [(f, r) for f, r in hits if r["total"]]           # fixtures with something to measure
            if not hits:
                print(f"     n/a  {what}  (no recorded fixture exercises it)")
                continue
            bad = [(f, r) for f, r in hits if r["missing"]]
            state = "gap " if bad else "met "
            gaps += bool(bad)
            counts = ", ".join(f"{f.removesuffix('.json')} {r['total'] - r['missing']}/{r['total']}" for f, r in hits)
            print(f"     {state} {what:<38} {hits[0][1]['verb']}: {counts}")
            if bad:
                print(f"            e.g. {'; '.join(bad[0][1]['examples'])}")
    declared: set[str] = set()
    for s in SCHEMA_FILES.values():
        collect_targets(s, declared)
    rest = sorted(declared - measured, key=lambda k: int(k[1:]))
    if rest:
        why = "request-side, or no field to observe" if OBSERVED.is_dir() else "private Grain fixtures not present"
        print(f"     not measured from responses ({why}): "
              + ", ".join(f"{t} ({TARGETS.get(t, '')})" for t in rest))

    print(f"\n{len(SCHEMA_FILES)} schemas, {failures} failure(s), {gaps} open gap(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
