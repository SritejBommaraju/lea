"""B1: the scaffold exists, the Makefile has its targets, the package imports."""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TARGETS = {"bootstrap", "codegen", "codegen-check", "check", "test", "replay", "eval", "run-gateway", "run-workers",
           "doctor", "next", "status", "hooks", "guard", "contracts", "sessions-check"}


def test_makefile_targets_exist():
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert TARGETS <= set(re.findall(r"^([a-z][a-z-]*):", text, flags=re.M))


def test_repairos_imports():
    res = subprocess.run([sys.executable, "-c", "import repairos; print(repairos.__version__)"], capture_output=True,
                         text=True, cwd="/")
    assert res.returncode == 0, res.stderr


def test_license_is_apache_2_and_notice_exists():
    # sha256 of https://www.apache.org/licenses/LICENSE-2.0.txt (byte-identical copy)
    digest = hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest()
    assert digest == "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
    assert (ROOT / "NOTICE").read_text(encoding="utf-8").startswith("RepairOS")


def test_secrets_are_gitignored():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in ("backend/.env", "ios/Config/Signing.xcconfig", "data/", ".venv/"):
        assert entry in ignored
