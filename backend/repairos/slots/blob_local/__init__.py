"""BLOB=local (M.BLOB.1): content-addressed files under <data>/blobs/<2 hex>/<sha256>.

<data> is REPAIROS_DATA_DIR, else the repo's data/ (gitignored). Presigned urls are HMAC-signed gateway paths; the
key is generated once into <data>/blobs/.presign_key (never leaves the machine).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import BinaryIO

from repairos.config import ROOT
from repairos.registry import register
from repairos.slots.blob import BlobNotFound, HashMismatch, OffsetMismatch, is_sha256, ref_of, sha_of

_CHUNK = 1 << 20


def data_dir() -> Path:
    return Path(os.environ.get("REPAIROS_DATA_DIR") or ROOT / "data")


class LocalBlobStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else data_dir() / "blobs"
        self.staging = self.root / ".staging"
        self.staging.mkdir(parents=True, exist_ok=True)
        key_file = self.root / ".presign_key"
        if not key_file.exists():
            key_file.write_bytes(secrets.token_bytes(32))
            key_file.chmod(0o600)
        self._key = key_file.read_bytes()

    def _path(self, ref: str) -> Path:
        sha = sha_of(ref)
        return self.root / sha[:2] / sha

    def _meta(self, ref: str) -> Path:
        p = self._path(ref)
        return p.with_name(p.name + ".json")

    def _install(self, tmp: Path, sha: str, content_type: str) -> str:
        ref = ref_of(sha)
        dest = self._path(ref)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            tmp.unlink(missing_ok=True)
        else:
            os.replace(tmp, dest)
        if not self._meta(ref).exists():
            self._meta(ref).write_text(json.dumps({"content_type": content_type}), encoding="utf-8")
        return ref

    def put(self, data: bytes, content_type: str = "application/octet-stream") -> str:
        sha = hashlib.sha256(data).hexdigest()
        if self.exists(ref_of(sha)):
            return ref_of(sha)
        tmp = self.staging / f"put-{secrets.token_hex(8)}"
        tmp.write_bytes(data)
        return self._install(tmp, sha, content_type)

    def exists(self, ref: str) -> bool:
        return self._path(ref).is_file()

    def _need(self, ref: str) -> Path:
        p = self._path(ref)
        if not p.is_file():
            raise BlobNotFound(ref)
        return p

    def get(self, ref: str) -> bytes:
        return self._need(ref).read_bytes()

    def open(self, ref: str) -> BinaryIO:
        return self._need(ref).open("rb")

    def size(self, ref: str) -> int:
        return self._need(ref).stat().st_size

    def content_type(self, ref: str) -> str:
        self._need(ref)
        try:
            return json.loads(self._meta(ref).read_text(encoding="utf-8"))["content_type"]
        except (OSError, ValueError, KeyError):
            return "application/octet-stream"

    def delete(self, ref: str) -> None:
        self._path(ref).unlink(missing_ok=True)
        self._meta(ref).unlink(missing_ok=True)

    def _sig(self, sha: str, exp: int) -> str:
        return hmac.new(self._key, f"{sha}:{exp}".encode(), hashlib.sha256).hexdigest()

    def presign(self, ref: str, ttl_s: int) -> str:
        sha = sha_of(self._need(ref).name)
        exp = int(time.time()) + int(ttl_s)
        return f"/v1/blobs/{sha}?exp={exp}&sig={self._sig(sha, exp)}"

    def verify_presign(self, sha256: str, exp: int, sig: str) -> bool:
        return is_sha256(sha256) and exp >= time.time() and hmac.compare_digest(self._sig(sha256, exp), sig)

    # resumable upload
    def _stage(self, sha256: str) -> Path:
        sha_of(sha256)
        return self.staging / sha256

    def upload_offset(self, sha256: str) -> int:
        p = self._stage(sha256)
        return p.stat().st_size if p.exists() else 0

    def upload_append(self, sha256: str, offset: int, data: bytes) -> int:
        current = self.upload_offset(sha256)
        if offset != current:
            raise OffsetMismatch(current, offset)
        with self._stage(sha256).open("ab") as f:
            f.write(data)
        return current + len(data)

    def upload_commit(self, sha256: str, content_type: str = "application/octet-stream") -> str:
        p = self._stage(sha256)
        if self.exists(ref_of(sha256)):
            p.unlink(missing_ok=True)
            return ref_of(sha256)
        h = hashlib.sha256()
        if p.exists():
            with p.open("rb") as f:
                while chunk := f.read(_CHUNK):
                    h.update(chunk)
        if h.hexdigest() != sha256:
            p.unlink(missing_ok=True)
            raise HashMismatch(f"staged bytes hash to {h.hexdigest()}, upload named {sha256}")
        if not p.exists():
            p.touch()
        return self._install(p, sha256, content_type)


@register("BLOB", "local")
def _factory() -> LocalBlobStore:
    return LocalBlobStore()
