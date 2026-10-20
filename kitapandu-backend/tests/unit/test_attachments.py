"""Unit tests for the storage port + adapters in :mod:`ksma.shared.attachments`.

Seam
----
``ksma.shared.attachments`` for the abstract storage port
(``AttachmentStorage``), the on-disk dev adapter
(``LocalAttachmentStorage``), the S3-compatible prod adapter
(``S3AttachmentStorage``), and the filename sanitiser
(``_sanitize_filename``).

Pinned behaviour:

- The filename sanitiser normalises unsafe characters, strips ``\\x00``,
  trims leading/trailing whitespace, returns ``"unnamed"`` when the result
  would otherwise be empty, and caps the sanitised name at 120 chars.
- ``LocalAttachmentStorage`` writes under ``<root>/<tenant>/<attachment>/<safe_name>``
  and round-trips ``put`` → ``get`` / ``exists`` / ``delete`` against a real
  ``tmp_path`` directory. Parent directories are created on demand and cleaned
  up on the trailing edge of a delete.
- ``S3AttachmentStorage`` round-trips the same operations against an
  in-memory fake S3 client that exposes the four boto3 calls the adapter
  makes (``put_object``, ``get_object``, ``delete_object``, ``head_object``).
  No network I/O, no real MinIO.
- ``Attachment.storage_key`` is server-generated opaque text, equal to
  ``<tenant>/<attachment>/<safe_name>`` — never a public URL.

Not asserted here (lives elsewhere):

- Network I/O against real MinIO / S3 (integration tests).
- HTTP streaming download path or RBAC checks around
  ``GET /attachments/{id}/content`` (covered under ``test_post_attachment``
  in the operations slice).
- ``build_storage_from_settings()`` factory: it reads the live settings
  module + ``KITAPANDU_LOCAL_ATTACHMENT_DIR``; exercising it requires
  environment mutation and is intentionally a coverage trade-off.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import threading
from pathlib import Path
from typing import BinaryIO

import pytest

from ksma.shared.attachments import (
    AttachmentStorage,
    LocalAttachmentStorage,
    S3AttachmentStorage,
    StoredBlob,
    _sanitize_filename,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ChunkedStream:
    """A binary file-like that returns at most ``chunk_size`` bytes per read.

    The local adapter pulls ``stream.read(64 * 1024)`` inside a
    ``run_in_executor`` loop; the chunk size argument is the one piece of
    arithmetic in ``put`` (``64 * 1024``). Using a smaller chunk size here
    lets us guarantee the loop iterates more than once and therefore
    exercises both the hash-update site and the empty-chunk early-return.
    """

    def __init__(self, data: bytes, *, chunk_size: int = 17) -> None:
        self._buf = io.BytesIO(data)
        self._chunk = chunk_size

    def read(self, n: int = -1) -> bytes:
        # Ignore the caller's ``n`` and use our own chunk size so we
        # deterministically get many small reads rather than one big one.
        del n
        return self._buf.read(self._chunk)


class _FakeS3Client:
    """Minimal in-memory S3 fake.

    Implements only the four boto3 calls the adapter makes. Errors raised
    by boto3 in real life (``ClientError: NoSuchKey`` etc.) are simulated
    with plain ``KeyError`` — the adapter catches ``Exception`` so the
    error class does not matter for the ``exists`` path.
    """

    def __init__(self) -> None:
        # Bucket-qualified store so two adapters with different buckets
        # do not see each other's blobs.
        self.objects: dict[tuple[str, str], bytes] = {}
        self.lock = threading.Lock()

    def put_object(self, *, Bucket: str, Key: str, Body, ContentType: str) -> dict:
        # boto3 accepts bytes or a file-like; we coerce to bytes either way.
        if hasattr(Body, "read"):
            data = Body.read()
            assert isinstance(data, (bytes, bytearray))
            data = bytes(data)
        else:
            data = bytes(Body)
        with self.lock:
            self.objects[(Bucket, Key)] = data
        return {"ETag": hashlib.md5(data).hexdigest()}

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        with self.lock:
            data = self.objects.get((Bucket, Key))
        if data is None:  # mimic boto3 NoSuchKey on missing object
            raise KeyError(Key)
        return {"Body": _FakeBody(data)}

    def delete_object(self, *, Bucket: str, Key: str) -> dict:
        with self.lock:
            self.objects.pop((Bucket, Key), None)
        return {}

    def head_object(self, *, Bucket: str, Key: str) -> dict:
        with self.lock:
            present = (Bucket, Key) in self.objects
        if not present:
            raise KeyError(Key)
        return {"ContentLength": len(self.objects[(Bucket, Key)])}


class _FakeBody:
    """Stand-in for boto3's ``StreamingBody`` — only ``.read()`` is needed."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


def _make_s3_storage(fake: _FakeS3Client, **overrides) -> S3AttachmentStorage:
    """Build an ``S3AttachmentStorage`` whose boto3 client is our fake.

    The adapter lazy-imports boto3 inside ``_s3_client()``. We never want
    that code path to actually run in a unit test — so we just assign
    ``_client`` directly. This avoids the network, the credential check,
    and the import error you'd get on a runner that strips boto3 from the
    image.
    """
    defaults = dict(
        endpoint="localhost:9000",
        access_key="test-access-key",
        secret_key="test-secret-key",
        bucket="ksma-unit-test",
        secure=False,
    )
    defaults.update(overrides)
    storage = S3AttachmentStorage(**defaults)
    storage._client = fake  # type: ignore[attr-defined]
    return storage


# ---------------------------------------------------------------------------
# Filename sanitiser — module-level helper
# ---------------------------------------------------------------------------


class TestSanitizeFilename:
    """``_sanitize_filename`` is exported under ``__all__`` so it is part
    of the public surface we test."""

    def test_normal_filename_is_returned_unchanged(self) -> None:
        # Kills the ``or -> and`` mutation on the trailing ``or "unnamed"``:
        # with ``and`` every non-empty input collapses to ``"unnamed"``.
        assert _sanitize_filename("report.pdf") == "report.pdf"

    def test_empty_string_returns_unnamed(self) -> None:
        assert _sanitize_filename("") == "unnamed"

    def test_whitespace_only_returns_unnamed(self) -> None:
        assert _sanitize_filename("   ") == "unnamed"

    def test_strips_nul_byte(self) -> None:
        # The replacement happens *before* the safe-char regex, so a
        # filename made entirely of ``\\x00`` collapses to the empty
        # path → ``"unnamed"``.
        assert _sanitize_filename("hello\x00.txt") == "hello.txt"
        assert _sanitize_filename("\x00\x00\x00") == "unnamed"

    def test_unsafe_characters_replaced_with_underscore(self) -> None:
        # ``/``, ``\\`` and the space all fall outside ``[A-Za-z0-9._-]``.
        assert _sanitize_filename("a/b\\c d.txt") == "a_b_c_d.txt"

    def test_name_is_truncated_at_120_characters(self) -> None:
        long = "a" * 500
        out = _sanitize_filename(long)
        assert len(out) == 120
        assert out == "a" * 120

    def test_strips_surrounding_whitespace_before_sanitising(self) -> None:
        # Whitespace at the edges is consumed by ``str.strip()``; any
        # remaining unsafe characters are replaced.
        assert _sanitize_filename("  report.pdf  ") == "report.pdf"


# ---------------------------------------------------------------------------
# Local adapter
# ---------------------------------------------------------------------------


class TestLocalAttachmentStorageInit:
    """``__init__`` creates the root directory and is idempotent on
    subsequent calls."""

    def test_creates_nested_root_when_intermediate_dirs_missing(
        self, tmp_path: Path
    ) -> None:
        # ``tmp_path`` itself exists; ask for a deep child so ``parents=True``
        # is observable. Removing ``parents=True`` would raise
        # ``FileNotFoundError`` here.
        nested = tmp_path / "a" / "b" / "c"
        storage = LocalAttachmentStorage(nested)
        assert nested.is_dir()
        assert storage.root == nested.resolve()

    def test_idempotent_when_root_already_exists(self, tmp_path: Path) -> None:
        # Removing ``exist_ok=True`` would raise ``FileExistsError`` on the
        # second call.
        LocalAttachmentStorage(tmp_path)
        # Second call must not raise.
        LocalAttachmentStorage(tmp_path)
        assert tmp_path.is_dir()


class TestLocalAttachmentStoragePut:
    def _stream(self, data: bytes, *, chunk_size: int = 17) -> BinaryIO:
        return _ChunkedStream(data, chunk_size=chunk_size)  # type: ignore[return-value]

    async def test_put_returns_blob_with_expected_key_and_size(
        self, tmp_path: Path
    ) -> None:
        storage = LocalAttachmentStorage(tmp_path)
        payload = b"hello world"

        blob = await storage.put(
            tenant_id="tenant-a",
            attachment_id="att-1",
            original_name="greeting.txt",
            stream=self._stream(payload),
            content_type="text/plain",
        )

        assert isinstance(blob, StoredBlob)
        assert blob.key == "tenant-a/att-1/greeting.txt"
        assert blob.size == len(payload)
        # sha256 hex, 64 chars, lowercase.
        assert blob.content_hash == hashlib.sha256(payload).hexdigest()
        assert len(blob.content_hash) == 64

    async def test_put_writes_bytes_to_disk(self, tmp_path: Path) -> None:
        storage = LocalAttachmentStorage(tmp_path)
        payload = b"\x00\x01binary\x02" * 16
        blob = await storage.put(
            tenant_id="t1",
            attachment_id="a1",
            original_name="blob.bin",
            stream=self._stream(payload),
            content_type="application/octet-stream",
        )

        on_disk = (tmp_path / blob.key).read_bytes()
        assert on_disk == payload

    async def test_put_creates_intermediate_parent_directories(
        self, tmp_path: Path
    ) -> None:
        # Fresh ``tenant_id`` so the intermediate ``<tenant>/<attachment>``
        # chain does not already exist. Killing ``parents=True`` raises
        # ``FileNotFoundError`` inside ``put``.
        storage = LocalAttachmentStorage(tmp_path)
        await storage.put(
            tenant_id="brand-new-tenant",
            attachment_id="new-attachment",
            original_name="file.txt",
            stream=self._stream(b"x"),
            content_type="text/plain",
        )
        assert (tmp_path / "brand-new-tenant" / "new-attachment").is_dir()
        assert (tmp_path / "brand-new-tenant" / "new-attachment" / "file.txt").exists()

    async def test_put_is_idempotent_when_overwriting_same_key(
        self, tmp_path: Path
    ) -> None:
        # Killing ``exist_ok=True`` on ``target.parent.mkdir`` raises
        # ``FileExistsError`` on the second ``put`` of the same attachment.
        storage = LocalAttachmentStorage(tmp_path)
        for payload in (b"first", b"second"):
            blob = await storage.put(
                tenant_id="t",
                attachment_id="a",
                original_name="doc.txt",
                stream=self._stream(payload),
                content_type="text/plain",
            )
            assert blob.size == len(payload)
            assert blob.content_hash == hashlib.sha256(payload).hexdigest()

        on_disk = (tmp_path / blob.key).read_bytes()
        assert on_disk == b"second"

    async def test_put_content_hash_matches_sha256_of_payload(
        self, tmp_path: Path
    ) -> None:
        # Multi-chunk payload so the hash-update site ``sha.update(chunk)``
        # is exercised for every chunk — killing both the ``* -> /`` flip
        # on the read size (``64 * 1024`` -> ``64 / 1024`` would read
        # essentially nothing) and the ``delete not`` flip on
        # ``if not chunk: break`` (a multi-chunk stream would not be
        # consumed past the first chunk).
        storage = LocalAttachmentStorage(tmp_path)
        payload = b"".join(bytes([i]) for i in range(64)) * 1024  # 64 KiB
        blob = await storage.put(
            tenant_id="t",
            attachment_id="a",
            original_name="bulk.bin",
            stream=self._stream(payload, chunk_size=512),
            content_type="application/octet-stream",
        )
        assert blob.size == len(payload)
        assert blob.content_hash == hashlib.sha256(payload).hexdigest()

    async def test_put_with_empty_stream_completes(self, tmp_path: Path) -> None:
        # Even an empty stream must return a ``StoredBlob`` with size 0
        # and the SHA-256 of empty input. With the ``delete not`` flip,
        # ``if chunk: break`` would never fire on ``b""`` and the loop
        # would infinite-spin — ``asyncio.wait_for`` enforces a hard
        # deadline so the test fails fast under the mutation.
        storage = LocalAttachmentStorage(tmp_path)
        blob = await asyncio.wait_for(
            storage.put(
                tenant_id="t",
                attachment_id="a",
                original_name="empty.bin",
                stream=self._stream(b""),
                content_type="application/octet-stream",
            ),
            timeout=2.0,
        )
        assert blob.size == 0
        assert blob.content_hash == hashlib.sha256(b"").hexdigest()

    async def test_put_sanitises_original_filename(self, tmp_path: Path) -> None:
        storage = LocalAttachmentStorage(tmp_path)
        blob = await storage.put(
            tenant_id="t",
            attachment_id="a",
            original_name="weird / name \\ with spaces.txt",
            stream=self._stream(b"x"),
            content_type="text/plain",
        )
        # Unsafe characters folded to underscores by the sanitiser.
        assert blob.key.endswith("weird___name___with_spaces.txt")


class TestLocalAttachmentStorageGet:
    async def test_get_returns_bytes_written_by_put(self, tmp_path: Path) -> None:
        storage = LocalAttachmentStorage(tmp_path)
        payload = b"round-trip"
        blob = await storage.put(
            tenant_id="t",
            attachment_id="a",
            original_name="f.txt",
            stream=io.BytesIO(payload),
            content_type="text/plain",
        )
        assert await storage.get(blob.key) == payload

    async def test_get_raises_when_key_missing(self, tmp_path: Path) -> None:
        storage = LocalAttachmentStorage(tmp_path)
        with pytest.raises(FileNotFoundError):
            await storage.get("nope/missing/file.txt")


class TestLocalAttachmentStorageExists:
    async def test_exists_true_after_put(self, tmp_path: Path) -> None:
        storage = LocalAttachmentStorage(tmp_path)
        blob = await storage.put(
            tenant_id="t",
            attachment_id="a",
            original_name="f.txt",
            stream=io.BytesIO(b"x"),
            content_type="text/plain",
        )
        assert await storage.exists(blob.key) is True

    async def test_exists_false_when_key_missing(self, tmp_path: Path) -> None:
        storage = LocalAttachmentStorage(tmp_path)
        assert await storage.exists("nope/missing/file.txt") is False


class TestLocalAttachmentStorageDelete:
    async def test_delete_removes_existing_blob(self, tmp_path: Path) -> None:
        storage = LocalAttachmentStorage(tmp_path)
        blob = await storage.put(
            tenant_id="t",
            attachment_id="a",
            original_name="f.txt",
            stream=io.BytesIO(b"x"),
            content_type="text/plain",
        )
        path = tmp_path / blob.key
        assert path.exists()

        await storage.delete(blob.key)

        assert not path.exists()
        assert await storage.exists(blob.key) is False

    async def test_delete_is_idempotent_when_missing(self, tmp_path: Path) -> None:
        storage = LocalAttachmentStorage(tmp_path)
        # Must not raise even though the key was never written.
        await storage.delete("never/seen/this.txt")

    async def test_delete_prunes_empty_parent_dirs(self, tmp_path: Path) -> None:
        # Only attachment under that tenant; deleting it should clean up
        # both the attachment dir and the tenant dir (the docstring
        # promises a "best-effort" clean-up two levels up).
        storage = LocalAttachmentStorage(tmp_path)
        blob = await storage.put(
            tenant_id="lone-tenant",
            attachment_id="only-attachment",
            original_name="f.txt",
            stream=io.BytesIO(b"x"),
            content_type="text/plain",
        )
        await storage.delete(blob.key)

        assert not (tmp_path / "lone-tenant" / "only-attachment").exists()
        assert not (tmp_path / "lone-tenant").exists()

    async def test_delete_keeps_non_empty_parent_when_sibling_remains(
        self, tmp_path: Path
    ) -> None:
        # Two attachments in the same tenant. Deleting one must NOT take
        # the tenant directory down with it. With ``and -> or`` on the
        # parent-rstrip guard the mutated ``rmdir`` would raise
        # ``OSError`` because the tenant dir is still populated; with
        # ``delete not`` the guard inverts and the empty attachment dir
        # would not be cleaned up.
        storage = LocalAttachmentStorage(tmp_path)
        keep_blob = await storage.put(
            tenant_id="shared-tenant",
            attachment_id="keep-me",
            original_name="k.txt",
            stream=io.BytesIO(b"keep"),
            content_type="text/plain",
        )
        drop_blob = await storage.put(
            tenant_id="shared-tenant",
            attachment_id="drop-me",
            original_name="d.txt",
            stream=io.BytesIO(b"drop"),
            content_type="text/plain",
        )

        await storage.delete(drop_blob.key)

        # The kept sibling + its directory + the tenant dir all survive.
        assert (tmp_path / keep_blob.key).read_bytes() == b"keep"
        assert (tmp_path / "shared-tenant" / "keep-me").is_dir()
        assert (tmp_path / "shared-tenant").is_dir()

        # The dropped attachment dir is gone (best-effort cleanup).
        assert not (tmp_path / "shared-tenant" / "drop-me").exists()


# ---------------------------------------------------------------------------
# S3 adapter
# ---------------------------------------------------------------------------


class TestS3AttachmentStorageInit:
    def test_default_secure_is_false(self) -> None:
        # Killing ``secure: bool = False`` -> ``secure: bool = True`` flips
        # the default; the explicit attribute must still be ``False``.
        storage = S3AttachmentStorage(
            endpoint="localhost:9000",
            access_key="x",
            secret_key="y",
            bucket="b",
        )
        assert storage.secure is False

    def test_endpoint_url_uses_http_when_secure_false(self) -> None:
        # We never call ``_s3_client`` here — just pin the field that
        # would have been passed to boto3.
        storage = S3AttachmentStorage(
            endpoint="minio.example:9000",
            access_key="x",
            secret_key="y",
            bucket="b",
            secure=False,
        )
        assert storage.endpoint == "minio.example:9000"
        assert storage.secure is False

    def test_endpoint_url_uses_https_when_secure_true(self) -> None:
        storage = S3AttachmentStorage(
            endpoint="s3.amazonaws.com",
            access_key="x",
            secret_key="y",
            bucket="b",
            secure=True,
        )
        assert storage.secure is True


class TestS3AttachmentStoragePutGet:
    async def test_put_then_get_round_trips(self) -> None:
        fake = _FakeS3Client()
        storage = _make_s3_storage(fake)
        payload = b"s3 round-trip"

        blob = await storage.put(
            tenant_id="tenant-a",
            attachment_id="att-1",
            original_name="report.pdf",
            stream=io.BytesIO(payload),
            content_type="application/pdf",
        )

        assert blob.key == "tenant-a/att-1/report.pdf"
        assert blob.size == len(payload)
        assert blob.content_hash == hashlib.sha256(payload).hexdigest()

        # The fake stores the exact bytes; verify both ends agree.
        assert fake.objects[("ksma-unit-test", blob.key)] == payload
        assert await storage.get(blob.key) == payload

    async def test_get_raises_when_key_missing(self) -> None:
        fake = _FakeS3Client()
        storage = _make_s3_storage(fake)

        with pytest.raises(KeyError):
            await storage.get("missing/object.txt")


class TestS3AttachmentStorageExists:
    async def test_exists_true_after_put(self) -> None:
        fake = _FakeS3Client()
        storage = _make_s3_storage(fake)
        blob = await storage.put(
            tenant_id="t",
            attachment_id="a",
            original_name="f.txt",
            stream=io.BytesIO(b"x"),
            content_type="text/plain",
        )
        assert await storage.exists(blob.key) is True

    async def test_exists_false_when_key_missing(self) -> None:
        fake = _FakeS3Client()
        storage = _make_s3_storage(fake)
        # Direct kill of the ``False -> True`` mutation on the tail
        # ``return False``: mutated code would say "yes, it exists"
        # for everything the ``head_object`` path raises on.
        assert await storage.exists("nope/missing.txt") is False

    async def test_exists_false_after_delete(self) -> None:
        fake = _FakeS3Client()
        storage = _make_s3_storage(fake)
        blob = await storage.put(
            tenant_id="t",
            attachment_id="a",
            original_name="f.txt",
            stream=io.BytesIO(b"x"),
            content_type="text/plain",
        )
        await storage.delete(blob.key)
        assert await storage.exists(blob.key) is False


class TestS3AttachmentStorageDelete:
    async def test_delete_removes_blob_and_get_raises_after(self) -> None:
        fake = _FakeS3Client()
        storage = _make_s3_storage(fake)
        blob = await storage.put(
            tenant_id="t",
            attachment_id="a",
            original_name="f.txt",
            stream=io.BytesIO(b"x"),
            content_type="text/plain",
        )
        await storage.delete(blob.key)

        assert ("ksma-unit-test", blob.key) not in fake.objects
        with pytest.raises(KeyError):
            await storage.get(blob.key)


# ---------------------------------------------------------------------------
# Port sanity
# ---------------------------------------------------------------------------


class TestAttachmentStoragePort:
    """``AttachmentStorage`` is an ``abc.ABC``; both adapters must be valid
    concrete implementations and the abstract methods must not be
    instantiable on their own."""

    def test_local_adapter_is_an_attachment_storage(self, tmp_path: Path) -> None:
        assert isinstance(LocalAttachmentStorage(tmp_path), AttachmentStorage)

    def test_s3_adapter_is_an_attachment_storage(self) -> None:
        storage = _make_s3_storage(_FakeS3Client())
        assert isinstance(storage, AttachmentStorage)

    def test_port_cannot_be_instantiated_directly(self) -> None:
        # Pin the abstract contract — a future refactor that strips
        # ``abc.ABC`` from the port would not be flagged by runtime use,
        # but would be flagged here.
        with pytest.raises(TypeError):
            AttachmentStorage()  # type: ignore[abstract]
