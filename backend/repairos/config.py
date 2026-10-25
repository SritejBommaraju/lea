"""Settings and secrets (PLAN.md task B3).

One typed Settings object, loaded from the environment and backend/.env (environment wins). Keys are SecretStr:
they never show in repr, logs or errors. A missing key stays missing; `require()` names it and stops.

Release lockout: with REPAIROS_RELEASE=1 the dev-only providers are refused (GRAIN=replay, HOST=local_mac) and so
are REPAIROS_SLOT_* overrides (GEN3). The phase1 profile uses HOST=local_mac, so it can never run as a release.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from functools import cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / "backend" / ".env"
PROVIDERS_FILE = ROOT / "config" / "providers.yaml"


class MissingSettingError(RuntimeError):
    """A required setting is not set. The message names the key, never a value."""

    def __init__(self, key: str, why: str = ""):
        self.key = key
        super().__init__(f"{key} is not set (backend/.env or the environment){': ' + why if why else ''}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True,
                                      case_sensitive=False)

    # Grain (GRAIN slot, Phase 2)
    grain_url: str | None = None
    grain_key: SecretStr | None = None
    grain_test_project: str | None = None
    grain_interface_version: str | None = None
    # Jev / TypeSafe (FUSION=jev)
    typesafe_key: SecretStr | None = None
    # LLM / VLM slots
    anthropic_api_key: SecretStr | None = None
    # Reference hints (optional; no key = off): Bright Data SERP API (repairos.references)
    brightdata_api_key: SecretStr | None = None
    brightdata_serp_zone: str | None = None
    # Lea's voice (optional; no key = the phone's own voice): Cartesia TTS/STT (repairos.voice)
    cartesia_api_key: SecretStr | None = None
    # RepairOS runtime
    repairos_pairing_code: SecretStr | None = None  # the phone pairs with this code (gateway, B5)
    repairos_release: bool = False
    repairos_providers_path: Path = PROVIDERS_FILE

    def require(self, key: str, why: str = "") -> str:
        """The value of `key` (e.g. "GRAIN_KEY"), or MissingSettingError naming the key."""
        value = getattr(self, key.lower())
        if value is None or (isinstance(value, SecretStr) and not value.get_secret_value()) or value == "":
            raise MissingSettingError(key.upper(), why)
        return value.get_secret_value() if isinstance(value, SecretStr) else str(value)

    def redact(self) -> dict[str, object]:
        """All settings for logs: secrets as '***' when set, None when not."""
        out: dict[str, object] = {}
        for name in type(self).model_fields:
            v = getattr(self, name)
            out[name.upper()] = ("***" if v.get_secret_value() else None) if isinstance(v, SecretStr) else v
        return out


@cache
def get_settings() -> Settings:
    return Settings()


def release_problems(slots: Mapping[str, str], environ: Mapping[str, str] | None = None) -> list[str]:
    """Why these resolved slots may not run in a release build ([] = allowed)."""
    environ = os.environ if environ is None else environ
    problems = []
    if slots.get("GRAIN") == "replay":
        problems.append("GRAIN=replay serves recorded responses; dev and CI only")
    if slots.get("HOST") == "local_mac":
        problems.append("HOST=local_mac is the dev laptop runtime; dev only")
    overrides = sorted(k for k in environ if k.startswith("REPAIROS_SLOT_"))
    if overrides:
        problems.append("REPAIROS_SLOT_* overrides are dev only: " + ", ".join(overrides))
    return problems


def enforce_release(slots: Mapping[str, str], release: bool, environ: Mapping[str, str] | None = None) -> None:
    """SystemExit with the reasons when a release build selects dev-only providers."""
    if not release:
        return
    problems = release_problems(slots, environ)
    if problems:
        raise SystemExit("REPAIROS_RELEASE=1 refuses dev-only providers (config/providers.yaml):\n  - "
                         + "\n  - ".join(problems))
