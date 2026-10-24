"""Settings and secrets (BUILD.md B0.6).

One typed Settings object, loaded from the environment and backend/.env (environment wins). Keys are SecretStr:
they never show in repr, logs or errors. A missing key stays missing; `require()` names it and stops.

Release lockout: with REPAIROS_RELEASE=1 the dev-only providers are refused (GRAIN=replay, HOST=local_mac,
STATE=spacetimedb without STATE_TARGET) and so are REPAIROS_SLOT_* overrides (GEN3). The hackathon profile uses
HOST=local_mac, so it can never run as a release.
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

    # Grain (GRAIN slot)
    grain_url: str | None = None
    grain_key: SecretStr | None = None
    grain_test_project: str | None = None
    grain_interface_version: str | None = None
    # Jev / TypeSafe (FUSION=jev)
    typesafe_key: SecretStr | None = None
    # LLM / VLM slots
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    openai_compatible_url: str | None = None
    openai_compatible_key: SecretStr | None = None
    # STATE=spacetimedb (full profile)
    spacetimedb_url: str | None = None
    spacetimedb_db: str | None = None
    spacetimedb_service_token: SecretStr | None = None
    # TRAIN / HOST (full profile)
    runpod_api_key: SecretStr | None = None
    lambda_api_key: SecretStr | None = None
    host_ssh: str | None = None
    # BLOB=s3
    s3_endpoint: str | None = None
    s3_bucket: str | None = None
    aws_access_key_id: SecretStr | None = None
    aws_secret_access_key: SecretStr | None = None
    # SUPPLIER=nexar / digikey
    nexar_client_id: SecretStr | None = None
    nexar_client_secret: SecretStr | None = None
    digikey_client_id: SecretStr | None = None
    digikey_client_secret: SecretStr | None = None
    # RepairOS runtime
    repairos_pairing_code: SecretStr | None = None  # the phone pairs with this code (S2.1)
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
    if slots.get("STATE") == "spacetimedb" and not slots.get("STATE_TARGET"):
        problems.append("STATE=spacetimedb needs STATE_TARGET (local | maincloud)")
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
