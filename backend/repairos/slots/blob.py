"""BLOB slot interface (BUILD.md §3.1, M.BLOB.0). Implementations live in repairos.slots.blob_<impl>.

Content-addressed: a blob's ref is `blob://sha256:<64 hex>` of its bytes, so the same bytes stored twice are one
object. Resumable uploads stage bytes under the expected sha256 and only become a blob when the hash matches.
"""
from __future__ import annotations

import re
from typing import BinaryIO, Protocol, runtime_checkable

REF_PREFIX = "blob://sha256:"
_SHA = re.compile(r"^[0-9a-f]{64}$")


class BlobError(Exception):
    pass


class BlobNotFound(BlobError, KeyError):
    pass


class HashMismatch(BlobError, ValueError):
    """The bytes received do not hash to the sha256 the upload named."""


class OffsetMismatch(BlobError, ValueError):
    """A resumable chunk did not start where the staged bytes end."""

    def __init__(self, expected: int, got: int):
        self.expected, self.got = expected, got
        super().__init__(f"chunk starts at {got}, staged bytes end at {expected}")


def is_sha256(s: str) -> bool:
    return bool(_SHA.match(s))


def ref_of(sha256: str) -> str:
    if not is_sha256(sha256):
        raise BlobError(f"not a lower-case sha256 hex digest: {sha256!r}")
    return REF_PREFIX + sha256


def sha_of(ref: str) -> str:
    """The sha256 of a `blob://sha256:<hex>` ref (or of a bare hex digest)."""
    sha = ref[len(REF_PREFIX):] if ref.startswith(REF_PREFIX) else ref
    if not is_sha256(sha):
        raise BlobError(f"not a blob ref: {ref!r}")
    return sha


@runtime_checkable
class BlobStore(Protocol):
    def put(self, data: bytes, content_type: str = "application/octet-stream") -> str: ...
    def get(self, ref: str) -> bytes: ...
    def open(self, ref: str) -> BinaryIO: ...
    def exists(self, ref: str) -> bool: ...
    def size(self, ref: str) -> int: ...
    def content_type(self, ref: str) -> str: ...
    def delete(self, ref: str) -> None: ...
    def presign(self, ref: str, ttl_s: int) -> str:
        """A gateway path (`/v1/blobs/<sha>?exp=&sig=`) that serves the blob until `ttl_s` seconds from now."""
    def verify_presign(self, sha256: str, exp: int, sig: str) -> bool: ...
    # Resumable upload, keyed by the sha256 the client says the whole blob has.
    def upload_offset(self, sha256: str) -> int:
        """Bytes staged so far (0 when nothing is staged)."""
    def upload_append(self, sha256: str, offset: int, data: bytes) -> int:
        """Append `data` at `offset` (must equal upload_offset, else OffsetMismatch); returns the new offset."""
    def upload_commit(self, sha256: str, content_type: str = "application/octet-stream") -> str:
        """Hash the staged bytes; on a match the blob exists and its ref is returned, else HashMismatch (staging
        is discarded)."""
