"""B3: settings load from .env and the environment; secrets never leak; release lockout."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from repairos.config import MissingSettingError, Settings, enforce_release, release_problems

ROOT = Path(__file__).resolve().parents[3]
SECRET = "sk-test-not-a-real-key-7f3a"
PHASE1 = {"STATE": "sqlite", "BLOB": "local", "HOST": "local_mac"}
RELEASE_OK = {"STATE": "sqlite", "BLOB": "local", "HOST": "server"}


@pytest.fixture
def clean_env(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    return monkeypatch


def test_loads_from_env_file_and_environment_wins(tmp_path, clean_env):
    env = tmp_path / ".env"
    env.write_text(f"GRAIN_PROJECT=from-file\nANTHROPIC_API_KEY={SECRET}\nGRAIN_API_KEY=\n", encoding="utf-8")
    clean_env.setenv("GRAIN_PROJECT", "from-env")
    s = Settings(_env_file=env)
    assert s.grain_project == "from-env"
    assert s.require("ANTHROPIC_API_KEY") == SECRET
    assert s.grain_api_key is None  # empty value = missing, not ""


def test_missing_key_is_a_typed_error_naming_the_key(tmp_path, clean_env):
    s = Settings(_env_file=tmp_path / "absent.env")
    with pytest.raises(MissingSettingError) as err:
        s.require("GRAIN_API_KEY", "the design engine")
    assert err.value.key == "GRAIN_API_KEY"
    assert "GRAIN_API_KEY" in str(err.value)


def test_secrets_never_in_repr_logs_or_errors(tmp_path, clean_env, caplog):
    env = tmp_path / ".env"
    env.write_text(f"CARTESIA_API_KEY={SECRET}\nANTHROPIC_API_KEY={SECRET}\nGRAIN_API_KEY={SECRET}\n", encoding="utf-8")
    s = Settings(_env_file=env)
    with caplog.at_level(logging.INFO):
        logging.getLogger("repairos.test").info("settings %s %r %s", s, s, s.redact())
    texts = [repr(s), str(s), str(s.redact()), caplog.text, s.model_dump_json()]
    try:
        s.require("GRAIN_PROJECT")
    except MissingSettingError as e:
        texts.append(str(e))
    for t in texts:
        assert SECRET not in t
    assert s.redact()["GRAIN_API_KEY"] == "***"
    assert s.redact()["GRAIN_PROJECT"] is None


def test_release_with_the_dev_host_exits_with_a_clear_message():
    with pytest.raises(SystemExit) as err:
        enforce_release({**RELEASE_OK, "HOST": "local_mac"}, release=True, environ={})
    assert "HOST=local_mac" in str(err.value.code)


def test_release_refuses_the_phase1_profile_and_slot_overrides():
    assert any("HOST=local_mac" in p for p in release_problems(PHASE1, environ={}))
    assert any("REPAIROS_SLOT_STATE" in p for p in release_problems(RELEASE_OK, environ={"REPAIROS_SLOT_STATE": "other"}))
    assert release_problems(RELEASE_OK, environ={}) == []
    enforce_release(PHASE1, release=False, environ={"REPAIROS_SLOT_STATE": "other"})  # dev build: allowed


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
