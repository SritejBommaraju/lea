"""BLOB conformance (M.BLOB.0, BUILD.md §3.3): byte-exact round trips, content addressing, hash-checked resumable
upload, presign expiry.

    pytest backend/tests/conformance/blob [--impl local]
"""
from __future__ import annotations

import hashlib
import os
import time
from urllib.parse import parse_qs, urlparse

import pytest

from repairos.slots.blob import BlobNotFound, BlobStore, HashMismatch, OffsetMismatch, ref_of, sha_of


@pytest.mark.parametrize("n", [0, 5 * 1024 * 1024, 50 * 1024 * 1024], ids=["0B", "5MB", "50MB"])
def test_byte_exact(impl, n):
    data = os.urandom(n)
    ref = impl.put(data, "application/octet-stream")
    assert ref == ref_of(hashlib.sha256(data).hexdigest())
    assert impl.get(ref) == data and impl.size(ref) == n
    with impl.open(ref) as f:
        assert f.read() == data


def test_protocol_and_dedupe_and_delete(impl):
    assert isinstance(impl, BlobStore)
    a = impl.put(b"same bytes", "image/jpeg")
    assert impl.put(b"same bytes", "image/jpeg") == a and impl.content_type(a) == "image/jpeg"
    impl.delete(a)
    assert not impl.exists(a)
    with pytest.raises(BlobNotFound):
        impl.get(a)


def test_resumable_upload_hash_checked(impl):
    data = os.urandom(3 * 1024 * 1024 + 17)
    sha = hashlib.sha256(data).hexdigest()
    assert impl.upload_offset(sha) == 0
    assert impl.upload_append(sha, 0, data[:1_000_000]) == 1_000_000
    with pytest.raises(OffsetMismatch) as e:  # a resent chunk at the wrong offset is refused
        impl.upload_append(sha, 0, data[:10])
    assert e.value.expected == 1_000_000
    impl.upload_append(sha, impl.upload_offset(sha), data[1_000_000:])
    ref = impl.upload_commit(sha, "application/octet-stream")
    assert sha_of(ref) == sha and impl.get(ref) == data and impl.upload_offset(sha) == 0


def test_upload_with_wrong_bytes_is_refused(impl):
    sha = hashlib.sha256(b"expected").hexdigest()
    impl.upload_append(sha, 0, b"tampered")
    with pytest.raises(HashMismatch):
        impl.upload_commit(sha)
    assert not impl.exists(ref_of(sha)) and impl.upload_offset(sha) == 0


def test_presign_expiry(impl):
    ref = impl.put(b"x")
    q = parse_qs(urlparse(impl.presign(ref, 60)).query)
    sha, exp, sig = sha_of(ref), int(q["exp"][0]), q["sig"][0]
    assert impl.verify_presign(sha, exp, sig)
    assert not impl.verify_presign(sha, exp + 1, sig)
    q = parse_qs(urlparse(impl.presign(ref, -1)).query)
    assert not impl.verify_presign(sha, int(q["exp"][0]), q["sig"][0])
    assert int(q["exp"][0]) < time.time()
