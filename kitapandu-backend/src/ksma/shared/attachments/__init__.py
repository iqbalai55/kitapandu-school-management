"""Attachment storage port + Local and S3 (MinIO) adapters.

Per ADR-0001 §Q25 + NFR07 + R16: ``Attachment.storage_key`` is server-generated
opaque text, NEVER a public URL. Downloads are streamed through
``GET /attachments/{id}/content`` which re-checks rights + check_status at
fetch time.

The port is intentionally minimal: put + get + delete + exists + size. Adapters
keep the binary out of the database.
"""
from __future__ import annotations

import abc
import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True)
class StoredBlob:
    """Result of a ``put`` operation. ``key`` is the server-generated
    ``Attachment.storage_key`` that will be persisted on the DB row."""

    key: str
    size: int
    content_hash: str


class AttachmentStorage(abc.ABC):
    """The port every adapter implements."""

    @abc.abstractmethod
    async def put(
        self,
        *,
        tenant_id: str,
        attachment_id: str,
        original_name: str,
        stream: BinaryIO,
        content_type: str,
    ) -> StoredBlob:
        """Persist a new attachment. Returns the storage key + size + hash."""

    @abc.abstractmethod
    async def get(self, key: str) -> bytes:
        """Read the bytes for an attachment. Raises ``KeyError`` if missing."""

    @abc.abstractmethod
    async def delete(self, key: str) -> None:
        """Remove the bytes. Idempotent."""

    @abc.abstractmethod
    async def exists(self, key: str) -> bool: ...


# --- Local filesystem adapter --------------------------------------------


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_filename(name: str) -> str:
    name = name.strip().replace("\x00", "")
    if not name:
        return "unnamed"
    safe = _SAFE_NAME_RE.sub("_", name)
    return safe[:120] or "unnamed"


class LocalAttachmentStorage(AttachmentStorage):
    """Writes under ``root_dir/<tenant_id>/<attachment_id>/<sanitized_name>``."""

    def __init__(self, root_dir: str | Path) -> None:
        self.root = Path(root_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # ``key`` is built by put(); we trust the format ``<tenant>/<id>/<name>``
        # because we generate it ourselves. We resolve and verify it stays
        # under ``self.root`` to defend against accidental key corruption.
        full = (self.root / key).resolve()
        if not str(full).startswith(str(self.root)):
            raise ValueError(f"storage key escapes root: {key}")
        return full

    async def put(
        self,
        *,
        tenant_id: str,
        attachment_id: str,
        original_name: str,
        stream: BinaryIO,
        content_type: str,
    ) -> StoredBlob:
        import hashlib

        safe_name = _sanitize_filename(original_name)
        rel_key = f"{tenant_id}/{attachment_id}/{safe_name}"
        target = self._path(rel_key)
        target.parent.mkdir(parents=True, exist_ok=True)

        sha = hashlib.sha256()
        size = 0
        loop = asyncio.get_running_loop()
        # Stream into the file and the hash without loading the whole file.
        with target.open("wb") as out:
            while True:
                chunk = await loop.run_in_executor(None, stream.read, 64 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                sha.update(chunk)
                size += len(chunk)
        return StoredBlob(key=rel_key, size=size, content_hash=sha.hexdigest())

    async def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    async def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()
        # Best-effort: remove empty parent dirs up to the tenant dir.
        for parent in [path.parent, path.parent.parent]:
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()


# --- S3-compatible adapter (MinIO in dev) --------------------------------


class S3AttachmentStorage(AttachmentStorage):
    """S3-compatible object storage adapter.

    The ``boto3`` client is lazy-imported so the dev image can run without
    AWS credentials when using the local adapter. The connection details
    come from ``ksma.settings``.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
    ) -> None:
        self.endpoint = endpoint
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket = bucket
        self.secure = secure
        self._client: object | None = None

    def _s3_client(self):  # pragma: no cover - requires MinIO/S3 at runtime
        if self._client is None:
            try:
                import boto3  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "S3AttachmentStorage requires boto3. Install with `uv pip install boto3`."
                ) from exc
            self._client = boto3.client(
                "s3",
                endpoint_url=f"{'https' if self.secure else 'http'}://{self.endpoint}",
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
            )
        return self._client

    async def put(
        self,
        *,
        tenant_id: str,
        attachment_id: str,
        original_name: str,
        stream: BinaryIO,
        content_type: str,
    ) -> StoredBlob:
        import hashlib

        safe_name = _sanitize_filename(original_name)
        key = f"{tenant_id}/{attachment_id}/{safe_name}"
        sha = hashlib.sha256()
        body = stream.read()
        sha.update(body)
        loop = asyncio.get_running_loop()
        client = self._s3_client()
        await loop.run_in_executor(
            None,
            lambda: client.put_object(
                Bucket=self.bucket, Key=key, Body=body, ContentType=content_type
            ),
        )
        return StoredBlob(key=key, size=len(body), content_hash=sha.hexdigest())

    async def get(self, key: str) -> bytes:
        loop = asyncio.get_running_loop()
        client = self._s3_client()
        resp = await loop.run_in_executor(
            None, lambda: client.get_object(Bucket=self.bucket, Key=key)
        )
        return resp["Body"].read()

    async def delete(self, key: str) -> None:
        loop = asyncio.get_running_loop()
        client = self._s3_client()
        await loop.run_in_executor(
            None, lambda: client.delete_object(Bucket=self.bucket, Key=key)
        )

    async def exists(self, key: str) -> bool:
        loop = asyncio.get_running_loop()
        client = self._s3_client()
        try:
            await loop.run_in_executor(
                None,
                lambda: client.head_object(Bucket=self.bucket, Key=key),
            )
            return True
        except Exception:
            return False


# --- Factory --------------------------------------------------------------


def build_storage_from_settings() -> AttachmentStorage:
    """Return the configured adapter based on ``ksma.settings``. Local when
    ``KITAPANDU_LOCAL_ATTACHMENT_DIR`` is set, S3 otherwise."""
    from ksma.settings import get_settings

    settings = get_settings()
    local_dir = os.environ.get("KITAPANDU_LOCAL_ATTACHMENT_DIR")
    if local_dir:
        return LocalAttachmentStorage(local_dir)
    return S3AttachmentStorage(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key.get_secret_value(),
        bucket=settings.s3_bucket,
        secure=settings.minio_secure,
    )


__all__ = [
    "AttachmentStorage",
    "StoredBlob",
    "LocalAttachmentStorage",
    "S3AttachmentStorage",
    "build_storage_from_settings",
    "_sanitize_filename",
]