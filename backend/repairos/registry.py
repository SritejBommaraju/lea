"""Provider registry (BUILD.md §3.2, B0.9). Config picks the implementation of every slot; code never does.

    from repairos.registry import slot
    grain = slot("GRAIN")        # the configured GrainClient

Resolution (same as build/doctor.py): profile = REPAIROS_PROFILE > `profile:` in config/providers.yaml > hackathon;
then REPAIROS_SLOT_<SLOT>=<impl> overrides. An implementation lives in `repairos.slots.<slot>_<impl>` (lower case)
and registers a factory with `@register("<SLOT>", "<impl>")`. Only the selected package is imported, so an
unselected provider's dependencies are never loaded. There is no fallback wrapper for any slot (owner, 2026-10-24).
"""
from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

from repairos.config import enforce_release, get_settings

DEFAULT_PROFILE = "hackathon"
_FACTORIES: dict[tuple[str, str], Callable[[], Any]] = {}


class RegistryError(LookupError):
    pass


def register(slot_id: str, impl: str):
    """Class or factory decorator: `@register("<SLOT>", "<impl>")`. Called with no arguments when first selected."""
    def deco(factory):
        _FACTORIES[(slot_id, impl)] = factory
        return factory
    return deco


def resolve_slots(path: Path, environ: Mapping[str, str] | None = None) -> tuple[str, dict[str, str]]:
    """(profile name, {SLOT: impl}) for config file `path` and the environment."""
    environ = os.environ if environ is None else environ
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    profiles = data.get("profiles") or {}
    name = environ.get("REPAIROS_PROFILE") or data.get("profile") or DEFAULT_PROFILE
    if name not in profiles:
        raise RegistryError(f"unknown provider profile {name!r}; {path} has: {', '.join(sorted(profiles))}")
    slots = {k: str(v) for k, v in (profiles[name] or {}).items()}
    for k, v in environ.items():
        if k.startswith("REPAIROS_SLOT_"):
            slots[k[len("REPAIROS_SLOT_"):]] = v
    return name, slots


class Registry:
    def __init__(self, providers_path: Path | None = None, environ: Mapping[str, str] | None = None,
                 package: str = "repairos.slots", release: bool | None = None):
        self.environ = os.environ if environ is None else environ
        self.path = providers_path or get_settings().repairos_providers_path
        self.package = package
        self.profile, self.slots = resolve_slots(self.path, self.environ)
        enforce_release(self.slots, get_settings().repairos_release if release is None else release, self.environ)
        self._instances: dict[str, Any] = {}

    def selected(self, slot_id: str) -> str:
        if slot_id not in self.slots:
            raise RegistryError(f"slot {slot_id!r} is not in profile {self.profile!r} of {self.path}")
        return self.slots[slot_id]

    def load(self, slot_id: str, impl: str) -> Any:
        """A new instance of a named implementation (the conformance runner uses this for --impl)."""
        module = f"{self.package}.{slot_id.lower()}_{impl}"
        if (slot_id, impl) not in _FACTORIES:
            try:
                importlib.import_module(module)
            except ModuleNotFoundError as e:
                if e.name is None or not module.startswith(e.name):
                    raise
        if (slot_id, impl) not in _FACTORIES:
            known = sorted(i for s, i in _FACTORIES if s == slot_id)
            raise RegistryError(f"no implementation {impl!r} registered for slot {slot_id} (expected package "
                                f"{module}); registered: {known or 'none'}. Change `{slot_id}:` in {self.path} "
                                f"or REPAIROS_SLOT_{slot_id}.")
        return _FACTORIES[(slot_id, impl)]()

    def slot(self, slot_id: str) -> Any:
        """The configured implementation for `slot_id`, one instance per registry."""
        if slot_id not in self._instances:
            self._instances[slot_id] = self.load(slot_id, self.selected(slot_id))
        return self._instances[slot_id]


_default: Registry | None = None


def default_registry() -> Registry:
    global _default
    if _default is None:
        _default = Registry()
    return _default


def slot(slot_id: str) -> Any:
    return default_registry().slot(slot_id)
