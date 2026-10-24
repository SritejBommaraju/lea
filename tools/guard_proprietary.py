#!/usr/bin/env python3
"""Refuse commits that would leak Grain internals. Stdlib only.

Grain is proprietary: only the API surface RepairOS calls (endpoints, field shapes, units, frames) may be committed.
This guard fails on
  - any path under private/, any file named like the private Grain spec, status or recorded-fixture folder,
    and secrets files (backend/.env, *.env other than .env.example);
  - text that names Grain source modules or commands, the model Grain calls, or protected terms for Grain's
    algorithms and parameters. The protected terms are stored as SHA-256 digests so this file does not publish them;
    Sritej can add more, unhashed, in private/grain/guard-patterns.txt (one regex per line, never committed).

Run:  python3 -I tools/guard_proprietary.py              staged files (git diff --cached), what the pre-commit hook runs
      python3 -I tools/guard_proprietary.py --tracked    every file git tracks or has staged (make check)
      python3 -I tools/guard_proprietary.py PATH ...     files or folders (folders are walked)
Exit 0 when clean, 1 with the hits listed.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRA = ROOT / "private" / "grain" / "guard-patterns.txt"
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", "DerivedData", ".pytest_cache"}

# Grain source paths and commands, and the model it calls (written so they do not match this file).
PATTERNS = [
    ("Grain source path", re.compile(r"\bserver/[a-z_./]+", re.I)),
    ("Grain server command", re.compile(r"python3?\s+-m\s+server\b", re.I)),
    ("model Grain calls", re.compile(r"\bgpt-[0-9]", re.I)),
]

# sha256 of lowercase terms (single tokens or two-token phrases) that describe Grain internals.
PROTECTED_DIGESTS = {
    "8105c7723ec3451b7e6b5d9a82ff4c9c4400d29cce6eb9b930a5439c6e9457c0",
    "1ecce11c3e1c10751dcf33eec24b0d1473c73718363c39add38f732b659f6ef0",
    "5d6b706a46e6ea4b5e7d1eefbb17ad2972c96d8d2c7868567ea3185a363889ef",
    "798ae47915177d9c76d45d4111f7a51ced28396605cb2df81141f68cdb676c6f",
    "486b60eed469c2a1c10f5a1dd2452b862d21e54034bb1ef7346d7a89a0e00c88",
    "385d4775e81aadae6327d8709d540915f00dc81af42e9a18d815143b90618e01",
    "31fe0d741e9003e4c7c9f998ec6249fd0495a043916a81ee602d30967ff8c7f8",
    "7194045246691eeaca259a943c5056e981b9246cba25a1440a9c6c8da7ca715a",
    "11953e2549938dcaf15f85f2582a998cf88069dc9b2b21b93477dda33a21dccf",
    "1f473b6f51612fc9ad389a2b61a2c9ecf2d9fa463526e8247d1141e76e41099a",
    "f19cc9647345ffac58f11f7238b6ed93055efb3015868a10f0262880b7b9fe52",
    "88d9f3952277ce419b65fe9801e9f17ced42486938ed5edacaac2427fdedcc97",
}
PATH_RULES = [
    ("private folder", re.compile(r"(^|/)private/")),
    ("private Grain doc", re.compile(r"grain_(spec|status)", re.I)),
    ("recorded Grain fixtures", re.compile(r"grain[-_]observ", re.I)),
    ("secrets file", re.compile(r"(^|/)(backend/)?\.env$|(^|/)(?!\.env\.example$)[^/]*\.env$")),
]

TOKEN = re.compile(r"[a-z0-9_.+\-]+")


def _digest(term: str) -> str:
    return hashlib.sha256(term.encode()).hexdigest()


def _candidates(line: str):
    """Tokens of a line, their parts split on '-' and '/', and adjacent two-token phrases."""
    toks = [t.strip(".-_") for t in TOKEN.findall(line.lower())]
    toks = [t for t in toks if t]
    for i, t in enumerate(toks):
        yield t
        for part in re.split(r"[-/]", t):
            if part and part != t:
                yield part
        if i + 1 < len(toks):
            yield f"{t} {toks[i + 1]}"


def _extra_patterns():
    if not EXTRA.is_file():
        return []
    out = []
    for raw in EXTRA.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw and not raw.startswith("#"):
            out.append(("private pattern", re.compile(raw, re.I)))
    return out


def scan_text(text: str, protected: set[str], patterns) -> list[tuple[int, str, str]]:
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for name, rx in patterns:
            m = rx.search(line)
            if m:
                hits.append((n, name, m.group(0)))
        for cand in _candidates(line):
            if _digest(cand) in protected:
                hits.append((n, "protected Grain term", cand))
                break
    return hits


def scan_path(rel: str) -> list[str]:
    rel = rel.replace("\\", "/")
    if Path(rel).is_absolute():
        return []  # outside the repo: path rules are about repo paths
    return [f"{rel}: {name}" for name, rx in PATH_RULES if rx.search(rel)]


def _in_git() -> bool:
    r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return r.returncode == 0 and Path(r.stdout.strip()).resolve() == ROOT


def staged_files(tracked: bool = False) -> list[tuple[str, str | None]]:
    cmd = ["ls-files", "-c", "-z"] if tracked else ["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]
    r = subprocess.run(["git", "-C", str(ROOT), *cmd], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"guard: git {cmd[0]} failed: {r.stderr.strip()}")
    out = []
    for rel in filter(None, r.stdout.split("\0")):
        blob = subprocess.run(["git", "-C", str(ROOT), "show", f":{rel}"], capture_output=True)
        out.append((rel, _decode(blob.stdout) if blob.returncode == 0 else None))
    return out


def _decode(data: bytes) -> str | None:
    if b"\0" in data[:8192]:
        return None  # binary
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def given_files(args: list[str]) -> list[tuple[str, str | None]]:
    out = []
    for a in args:
        p = Path(a)
        if not p.exists():
            sys.exit(f"guard: no such path: {a}")
        files = [p] if p.is_file() else [f for f in sorted(p.rglob("*"))
                                          if f.is_file() and not SKIP_DIRS & set(f.parts)]
        for f in files:
            try:
                rel = f.resolve().relative_to(ROOT).as_posix()
            except ValueError:
                rel = f.as_posix()
            out.append((rel, _decode(f.read_bytes())))
    return out


def main(argv: list[str]) -> int:
    protected = set(PROTECTED_DIGESTS)
    patterns = PATTERNS + _extra_patterns()
    tracked = "--tracked" in argv
    argv = [a for a in argv if a != "--tracked"]
    if not argv and not _in_git():
        print("guard_proprietary: not a git repository, nothing staged; pass paths to scan files")
        return 0
    files = given_files(argv) if argv else staged_files(tracked)
    hits = []
    for rel, text in files:
        hits += scan_path(rel)
        if text is not None:
            hits += [f"{rel}:{n}: {name}: {found}" for n, name, found in scan_text(text, protected, patterns)]
    if hits:
        print("guard_proprietary: Grain internals or private files would be committed:", file=sys.stderr)
        for h in hits:
            print("  " + h, file=sys.stderr)
        print("Rewrite as an API-level need or a pointer to private/grain/ (ask Sritej); "
              "keep private files out of git.", file=sys.stderr)
        return 1
    print(f"guard_proprietary: {len(files)} file(s) clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
