"""B3: config picks the implementation; only the selected package is imported; release lockout applies."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from repairos import registry
from repairos.registry import Registry, RegistryError, resolve_slots

ROOT = Path(__file__).resolve().parents[3]

IMPL = '''
from repairos.registry import register

@register("{slot}", "{impl}")
class Impl:
    name = "{impl}"
'''


@pytest.fixture
def toy(tmp_path, monkeypatch):
    """A package `toyslots` with det_gdino and det_florence2 implementations, and a providers file."""
    pkg = tmp_path / "toyslots"
    for impl in ("gdino", "florence2"):
        (pkg / f"det_{impl}").mkdir(parents=True)
        (pkg / f"det_{impl}" / "__init__.py").write_text(IMPL.format(slot="DET", impl=impl))
    (pkg / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(registry, "_FACTORIES", {})
    yield tmp_path
    for m in [m for m in sys.modules if m.startswith("toyslots")]:
        del sys.modules[m]


def providers(tmp_path: Path, det: str) -> Path:
    p = tmp_path / "providers.yaml"
    p.write_text(f"profile: phase1\nprofiles:\n  phase1:\n    DET: {det}  # comment\n    HOST: local_mac\n"
                 f"  other:\n    DET: gdino\n    HOST: server\n")
    return p


def test_config_selects_one_and_the_other_is_never_imported(toy):
    reg = Registry(providers(toy, "gdino"), environ={}, package="toyslots", release=False)
    assert reg.slot("DET").name == "gdino"
    assert reg.slot("DET") is reg.slot("DET")
    assert "toyslots.det_gdino" in sys.modules
    assert "toyslots.det_florence2" not in sys.modules


def test_swapping_config_changes_the_class_with_no_code_change(toy):
    assert Registry(providers(toy, "florence2"), environ={}, package="toyslots", release=False).slot("DET").name == "florence2"
    env = {"REPAIROS_SLOT_DET": "gdino"}
    assert Registry(providers(toy, "florence2"), environ=env, package="toyslots", release=False).slot("DET").name == "gdino"


def test_profile_from_environment(toy):
    name, slots = resolve_slots(providers(toy, "florence2"), environ={"REPAIROS_PROFILE": "other"})
    assert (name, slots["DET"]) == ("other", "gdino")
    with pytest.raises(RegistryError, match="unknown provider profile"):
        resolve_slots(providers(toy, "gdino"), environ={"REPAIROS_PROFILE": "nope"})


def test_unknown_impl_lists_registered_ones_and_the_config_key(toy):
    reg = Registry(providers(toy, "owlv2"), environ={}, package="toyslots", release=False)
    reg.load("DET", "gdino")
    with pytest.raises(RegistryError) as err:
        reg.slot("DET")
    msg = str(err.value)
    assert "owlv2" in msg and "gdino" in msg and "REPAIROS_SLOT_DET" in msg


def test_missing_dependency_inside_an_impl_is_not_hidden(toy):
    (toy / "toyslots" / "det_heavy").mkdir()
    (toy / "toyslots" / "det_heavy" / "__init__.py").write_text("import a_dependency_that_is_not_installed\n")
    reg = Registry(providers(toy, "heavy"), environ={}, package="toyslots", release=False)
    with pytest.raises(ModuleNotFoundError, match="a_dependency_that_is_not_installed"):
        reg.slot("DET")


def test_release_build_refuses_dev_only_profile(toy):
    with pytest.raises(SystemExit, match="HOST=local_mac"):
        Registry(providers(toy, "gdino"), environ={}, package="toyslots", release=True)


def test_real_providers_file_resolves_phase1_by_default():
    name, slots = resolve_slots(ROOT / "config" / "providers.yaml", environ={})
    assert name == "phase1"
    assert slots == {"STATE": "sqlite", "BLOB": "local", "HOST": "local_mac"}


def test_registry_matches_build_tools_resolution():
    sys.path.insert(0, str(ROOT / "build"))
    try:
        import _lib
    finally:
        sys.path.pop(0)
    env = {"REPAIROS_SLOT_BLOB": "other"}
    assert resolve_slots(ROOT / "config" / "providers.yaml", environ=env)[1] == _lib.load_providers(environ=env)["slots"]


def _conformance_conftest():
    spec = importlib.util.spec_from_file_location("conformance_conftest", ROOT / "backend/tests/conformance/conftest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_conformance_runner_names_missing_human_prerequisites():
    conf = _conformance_conftest()
    assert conf.missing_prerequisites("STATE", "sqlite", env={}) == []
    assert conf.slot_of(ROOT / "backend/tests/conformance/state/test_x.py") == "STATE"


LAZY_IMPL = '''
from repairos.registry import register


@register("DET", "lazy")
class Impl:
    model_id = "toy"

    def __init__(self):
        import heavy_ml_lib  # stands in for torch/transformers: imported only when the impl is built

    def detect(self, image, prompts, min_score=None):
        return [prompts[0]]
'''


def test_det_impl_heavy_imports_are_lazy(toy):
    (toy / "toyslots" / "det_lazy").mkdir()
    (toy / "toyslots" / "det_lazy" / "__init__.py").write_text(LAZY_IMPL)
    (toy / "heavy_ml_lib.py").write_text("")
    reg = Registry(providers(toy, "lazy"), environ={}, package="toyslots", release=False)
    assert "heavy_ml_lib" not in sys.modules
    det = reg.slot("DET")
    assert "heavy_ml_lib" in sys.modules
    assert det.detect(None, ["connector"]) == ["connector"]
    del sys.modules["heavy_ml_lib"]


def test_backend_registry_never_imports_torch_or_transformers():
    import subprocess

    code = ("import sys; from repairos.registry import Registry; r = Registry(); r.selected('STATE'); "
            "assert 'torch' not in sys.modules and 'transformers' not in sys.modules")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert res.returncode == 0, res.stderr
