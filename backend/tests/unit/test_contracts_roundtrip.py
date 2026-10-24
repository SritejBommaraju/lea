"""B0.2: generated contract models load every example, reject every invalid case, and decode in Swift."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import jsonschema
import pydantic
import pytest

from repairos.contracts import MODELS, validate

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "spec" / "contracts"
# Public examples, plus the private Grain examples when present (private/ is gitignored; Grain is proprietary).
EXAMPLE_DIRS = [d for d in (CONTRACTS / "examples", ROOT / "private" / "grain" / "examples") if d.is_dir()]
EXAMPLES = sorted(p for d in EXAMPLE_DIRS for p in d.glob("*.json"))
INVALID = sorted((p for d in EXAMPLE_DIRS for p in (d / "invalid").glob("*.json")), key=lambda p: p.name)
SWIFT = ROOT / "ios" / "RepairOS" / "Contracts" / "Contracts.swift"

# Rules the generated Pydantic models cannot carry: JSON Schema `if/then` conditionals. validate() catches them.
MODEL_CANNOT_EXPRESS = {
    "meter-reading-continuity-without-pins.json",
    "meter-reading-resistance-with-volts.json",
    "meter-reading-voltage-with-continuity-value.json",
    "repair-plan-power-on-step-without-gate.json",
    "repair-plan-power-path-step-without-gate.json",
}


def schema_of(example: Path) -> str:
    return example.name.split(".")[0]


def merge_patch(base, patch):
    """RFC 7386, as in spec/contracts/check.py."""
    if not isinstance(patch, dict):
        return patch
    out = dict(base) if isinstance(base, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


def invalid_instance(path: Path) -> tuple[str, object]:
    case = json.loads(path.read_text(encoding="utf-8"))
    if "instance" in case:
        return case["schema"], case["instance"]
    for d in EXAMPLE_DIRS:
        if (d / case["base"]).exists():
            base = json.loads((d / case["base"]).read_text(encoding="utf-8"))
            return case["schema"], merge_patch(base, case["patch"])
    pytest.skip(f"base example {case['base']} not present (private Grain examples absent?)")


def test_every_schema_has_a_model():
    names = {p.name[: -len(".schema.json")] for p in CONTRACTS.glob("*.schema.json")}
    assert names == set(MODELS)


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
def test_example_round_trips(example):
    data = json.loads(example.read_text(encoding="utf-8"))
    model = validate(schema_of(example), data)
    assert model.model_dump(mode="json", by_alias=True, exclude_unset=True) == data


@pytest.mark.parametrize("case", INVALID, ids=lambda p: p.name)
def test_invalid_case_is_rejected(case):
    name, instance = invalid_instance(case)
    with pytest.raises((jsonschema.ValidationError, pydantic.ValidationError)):
        validate(name, instance)
    if case.name in MODEL_CANNOT_EXPRESS:
        MODELS[name].model_validate(instance)  # documents the gap: only validate() rejects it
    else:
        with pytest.raises(pydantic.ValidationError):
            MODELS[name].model_validate(instance)


def test_codegen_has_no_drift():
    res = subprocess.run(["python3", "-I", str(ROOT / "tools" / "codegen" / "codegen.py"), "--check"],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.skipif(shutil.which("swiftc") is None, reason="swiftc not installed (Xcode, human step `xcode`)")
def test_examples_decode_in_swift(tmp_path):
    titles = {p.name[: -len(".schema.json")]: json.loads(p.read_text(encoding="utf-8"))["title"]
              for p in CONTRACTS.glob("*.schema.json")}
    lines = ["import Foundation", "let d = JSONDecoder()", "d.dateDecodingStrategy = .iso8601", "var failed = 0",
             "@MainActor func check<T: Decodable>(_ t: T.Type, _ path: String) {",
             "  do { _ = try d.decode(t, from: try Data(contentsOf: URL(fileURLWithPath: path))) }",
             '  catch { print("FAIL \\(path): \\(error)"); failed += 1 }', "}"]
    lines += [f'check({titles[schema_of(e)]}.self, "{e}")' for e in EXAMPLES]
    lines += ["exit(failed == 0 ? 0 : 1)"]
    main = tmp_path / "main.swift"
    main.write_text("\n".join(lines), encoding="utf-8")
    exe = tmp_path / "decode"
    build = subprocess.run(["swiftc", "-swift-version", "6", str(SWIFT), str(main), "-o", str(exe)],
                           capture_output=True, text=True)
    assert build.returncode == 0, build.stderr[-4000:]
    res = subprocess.run([str(exe)], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout


def test_hand_edited_generated_file_is_drift(tmp_path, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("codegen", ROOT / "tools" / "codegen" / "codegen.py")
    codegen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(codegen)
    py, swift = tmp_path / "py", tmp_path / "swift"
    shutil.copytree(codegen.PY_OUT, py, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(codegen.SWIFT_OUT, swift)
    (py / "finding_schema.py").write_text((py / "finding_schema.py").read_text() + "# hand edit\n")
    monkeypatch.setattr(codegen, "PY_OUT", py)
    monkeypatch.setattr(codegen, "SWIFT_OUT", swift)
    monkeypatch.setattr(codegen, "ROOT", tmp_path)
    assert codegen.check() == 1
    assert "py/finding_schema.py" in capsys.readouterr().out
