"""B3: settings load from .env and the environment; secrets never leak; release lockout."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from repairos.config import MissingSettingError, Settings, enforce_release, release_problems

ROOT = Path(__file__).resolve().parents[3]
SECRET = "sk-test-not-a-real-key-7f3a"
PHASE1 = {"DET": "owlv2", "FUSION": "jev", "STATE": "sqlite", "HOST": "local_mac"}
RELEASE_OK = {"DET": "owlv2", "FUSION": "jev", "STATE": "sqlite", "HOST": "server"}


@pytest.fixture
def clean_env(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    return monkeypatch


def test_loads_from_env_file_and_environment_wins(tmp_path, clean_env):
    env = tmp_path / ".env"
    env.write_text(f"GRAIN_URL=http://from-file:8000\nGRAIN_KEY={SECRET}\nTYPESAFE_KEY=\n", encoding="utf-8")
    clean_env.setenv("GRAIN_URL", "http://from-env:8000")
    s = Settings(_env_file=env)
    assert s.grain_url == "http://from-env:8000"
    assert s.require("GRAIN_KEY") == SECRET
    assert s.typesafe_key is None  # empty value = missing, not ""


def test_missing_key_is_a_typed_error_naming_the_key(tmp_path, clean_env):
    s = Settings(_env_file=tmp_path / "absent.env")
    with pytest.raises(MissingSettingError) as err:
        s.require("TYPESAFE_KEY", "FUSION=jev")
    assert err.value.key == "TYPESAFE_KEY"
    assert "TYPESAFE_KEY" in str(err.value)


def test_secrets_never_in_repr_logs_or_errors(tmp_path, clean_env, caplog):
    env = tmp_path / ".env"
    env.write_text(f"GRAIN_KEY={SECRET}\nANTHROPIC_API_KEY={SECRET}\nTYPESAFE_KEY={SECRET}\n", encoding="utf-8")
    s = Settings(_env_file=env)
    with caplog.at_level(logging.INFO):
        logging.getLogger("repairos.test").info("settings %s %r %s", s, s, s.redact())
    texts = [repr(s), str(s), str(s.redact()), caplog.text, s.model_dump_json()]
    try:
        s.require("GRAIN_URL")
    except MissingSettingError as e:
        texts.append(str(e))
    for t in texts:
        assert SECRET not in t
    assert s.redact()["GRAIN_KEY"] == "***"
    assert s.redact()["GRAIN_URL"] is None


def test_release_with_replay_grain_exits_with_a_clear_message():
    with pytest.raises(SystemExit) as err:
        enforce_release({**RELEASE_OK, "GRAIN": "replay"}, release=True, environ={})
    assert "GRAIN=replay" in str(err.value.code)


def test_release_refuses_the_phase1_profile_and_slot_overrides():
    assert any("HOST=local_mac" in p for p in release_problems(PHASE1, environ={}))
    assert any("REPAIROS_SLOT_DET" in p for p in release_problems(RELEASE_OK, environ={"REPAIROS_SLOT_DET": "gdino"}))
    assert release_problems(RELEASE_OK, environ={}) == []
    enforce_release(PHASE1, release=False, environ={"REPAIROS_SLOT_DET": "gdino"})  # dev build: allowed


def test_env_example_lists_every_setting_and_every_doctor_key():
    example = (ROOT / "backend" / ".env.example").read_text(encoding="utf-8")
    keys = re.findall(r"^([A-Z][A-Z0-9_]*)=$", example, flags=re.M)
    assert len(keys) == len(set(keys))
    assert set(keys) == {n.upper() for n in Settings.model_fields}
    doctor = (ROOT / "build" / "_lib.py").read_text(encoding="utf-8")
    doctor_keys = {k for group in re.findall(r"_env_check\(env, \[([^\]]*)\]", doctor)
                   for k in re.findall(r'"([A-Z0-9_]+)"', group)}
    assert doctor_keys, "doctor env checks not found"
    assert doctor_keys <= set(keys), f"missing from .env.example: {sorted(doctor_keys - set(keys))}"


def test_backend_starts_without_grain_keys(tmp_path, clean_env):
    """Phase 1 has no Grain calls: missing GRAIN_* must not stop startup; only Grain-using code fails, when called."""
    from repairos.registry import Registry

    s = Settings(_env_file=tmp_path / "absent.env")
    reg = Registry(ROOT / "config" / "providers.yaml", environ={}, release=False)
    assert "GRAIN" not in reg.slots
    with pytest.raises(MissingSettingError, match="GRAIN_KEY"):
        s.require("GRAIN_KEY", "GRAIN=live")
