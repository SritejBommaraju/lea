#!/usr/bin/env python3
"""Check the contracts in spec/contracts/ against our examples.

Run:  python3 -I spec/contracts/check.py            (from the repo root or anywhere; Windows: python -I ...)
      python3 -I spec/contracts/check.py --fallback (force the built-in validator even if jsonschema is installed)

What it does, in order:
1. Every `*.schema.json` is draft 2020-12 and has at least one example `examples/<name>.json` (more may sit at
   `examples/<name>.<label>.json`). Every example must validate. An example with no schema is a failure.
2. Every `examples/invalid/*.json` is `{"schema": "<name>", "why": "...", "instance": {...}}`, or
   `{"schema", "why", "base": "<example file>", "patch": {...}}` (an RFC 7386 merge patch on a valid example: null
   deletes a key, arrays are replaced whole). It must FAIL validation.

Validator: the `jsonschema` package when it is installed (with `referencing` for cross-file $refs), else a small
stdlib-only validator covering what these contracts use: type, properties, required, additionalProperties, items,
minItems, maxItems, contains, uniqueItems, enum, const, minimum, maximum, exclusiveMinimum, exclusiveMaximum, minLength, pattern, $ref
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
DRAFT = "https://json-schema.org/draft/2020-12/schema"


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
        if "contains" in schema:
            def matches(item) -> bool:
                sub_errors: list[str] = []
                _validate(item, schema["contains"], root, path, sub_errors)
                return not sub_errors
            if not any(matches(item) for item in value):
                errors.append(f"{path}: no item matches contains")
        if schema.get("uniqueItems"):
            seen = [json.dumps(v, sort_keys=True) for v in value]
            if len(set(seen)) != len(seen):
                errors.append(f"{path}: items are not unique")

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

    example_roots = [(EXAMPLES, "")]

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

    print(f"\n{len(SCHEMA_FILES)} schemas, {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
