"""Unit tests for the ``ksma.operations.interface`` upload endpoint.

Seam: ``ksma.operations.interface.post_attachment`` — the multipart
``POST /api/v1/attachments`` handler (OPS-A01-adjacent). It enforces three
NFR07 / allowlist guards *before* the application layer ever sees the blob:

- ``owner_ref`` must be JSON (else 422).
- ``file.content_type`` must be in ``ALLOWED_MEDIA_TYPES`` (else 422).
- The body is read with a hard ``cap = MAX_ATTACHMENT_BYTES + 1`` and any
  surplus is rejected with 413 — so a file of *exactly* the cap is accepted
  and a file of ``cap + 1`` is not.

The application service is replaced with a recorder so the assertions are
about the router's own guards and response shaping, not about storage or
the DB. No FastAPI app, no DB, no network.
"""
from __future__ import annotations

import io
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException

from ksma.operations import interface as _iface
from ksma.operations.application import MAX_ATTACHMENT_BYTES, UploadResult
from ksma.operations.domain import AttachmentCheckStatus
from tests.unit.conftest_helpers import FakeAsyncSession, FakeTenantContext
from ksma.shared.rbac import RoleCode


class _FakeUploadFile:
    """Duck-typed stand-in for ``fastapi.UploadFile`` (direct handler call)."""

    def __init__(
        self,
        body: bytes,
        *,
        filename: str | None = "report.csv",
        content_type: str | None = "text/plain",
    ) -> None:
        self._body = body
        self.filename = filename
        self.content_type = content_type
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._body[:size] if size and size > 0 else self._body


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``session_scope`` + ``upload_attachment`` with local doubles."""
    session = FakeAsyncSession()

    @asynccontextmanager
    async def _scope():
        yield session

    calls: list[dict[str, Any]] = []

    async def _upload(_session: Any, ctx: Any, **kwargs: Any) -> UploadResult:
        calls.append(kwargs)
        return UploadResult(
            attachment_id=uuid4(),
            check_status=AttachmentCheckStatus.PENDING,
            storage_key=f"key/{kwargs['original_name']}",
            size_bytes=len(kwargs["stream"].getvalue()),
            content_hash="deadbeef",
        )

    monkeypatch.setattr(_iface, "session_scope", _scope)
    monkeypatch.setattr(_iface, "upload_attachment", _upload)
    return {"session": session, "calls": calls}


def _ctx(tenant_id: Any = None) -> Any:
    return FakeTenantContext(RoleCode.AC, tenant_id=tenant_id).with_account(
        uuid4(), uuid4()
    ).build()


class TestPostAttachmentGuards:
    """L235 (read cap) + L237 (413 boundary)."""

    async def test_rejects_invalid_owner_ref_json(self, recorder: Any) -> None:
        with pytest.raises(HTTPException) as exc:
            await _iface.post_attachment(
                "not-json", _FakeUploadFile(b"x"), _ctx()  # type: ignore[arg-type]
            )
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "INVALID_VALUE"
        assert recorder["calls"] == []

    async def test_rejects_disallowed_media_type(self, recorder: Any) -> None:
        with pytest.raises(HTTPException) as exc:
            await _iface.post_attachment(
                '{"kind":"participant","id":"p1"}',
                _FakeUploadFile(b"x", content_type="application/x-msdownload"),
                _ctx(),  # type: ignore[arg-type]
            )
        assert exc.value.status_code == 422
        assert "not allowed" in exc.value.detail["message"]
        assert recorder["calls"] == []

    async def test_reads_at_most_one_byte_over_the_cap(self, recorder: Any) -> None:
        """L235: the read cap is ``MAX + 1`` — never the whole file."""
        upload = _FakeUploadFile(b"z" * (MAX_ATTACHMENT_BYTES + 4096))
        with pytest.raises(HTTPException) as exc:
            await _iface.post_attachment(
                '{"kind":"participant","id":"p1"}', upload, _ctx()  # type: ignore[arg-type]
            )
        assert upload.read_sizes == [MAX_ATTACHMENT_BYTES + 1]
        assert exc.value.status_code == 413

    async def test_one_byte_over_the_cap_is_rejected(self, recorder: Any) -> None:
        """L235 + L237: ``MAX + 1`` bytes must fail closed with 413."""
        upload = _FakeUploadFile(b"z" * (MAX_ATTACHMENT_BYTES + 1))
        with pytest.raises(HTTPException) as exc:
            await _iface.post_attachment(
                '{"kind":"participant","id":"p1"}', upload, _ctx()  # type: ignore[arg-type]
            )
        assert exc.value.status_code == 413
        assert str(MAX_ATTACHMENT_BYTES) in exc.value.detail["message"]
        assert recorder["calls"] == []

    async def test_exactly_at_the_cap_is_accepted(self, recorder: Any) -> None:
        """L237 ``> -> >=``: a body of exactly MAX bytes must still be accepted."""
        upload = _FakeUploadFile(b"z" * MAX_ATTACHMENT_BYTES)
        out = await _iface.post_attachment(
            '{"kind":"participant","id":"p1"}', upload, _ctx()  # type: ignore[arg-type]
        )
        assert out.size_bytes == MAX_ATTACHMENT_BYTES
        assert out.media_type == "text/plain"
        assert len(recorder["calls"]) == 1


class TestPostAttachmentResponseShaping:
    """L252 / L253 / L262 / L263 — ``or``-fallback defaults on the response."""

    async def test_missing_filename_falls_back_to_upload(
        self, recorder: Any
    ) -> None:
        """L252 + L262: both the service kwarg and the response use ``'upload'``."""
        out = await _iface.post_attachment(
            '{"kind":"participant","id":"p1"}',
            _FakeUploadFile(b"a,b\n1,2\n", filename=None),
            _ctx(),  # type: ignore[arg-type]
        )
        assert out.original_name == "upload"
        assert recorder["calls"][0]["original_name"] == "upload"

    async def test_missing_media_type_falls_back_to_octet_stream(
        self, recorder: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """L253 + L263: a falsy (allow-listed) media type defaults to octet-stream.

        ``ALLOWED_MEDIA_TYPES`` is a module constant consulted by the guard at
        :224, so it is narrowed here to the empty string — the only way a
        falsy ``content_type`` can reach the ``or`` fallback at all.
        """
        monkeypatch.setattr(_iface, "ALLOWED_MEDIA_TYPES", {""})
        out = await _iface.post_attachment(
            '{"kind":"participant","id":"p1"}',
            _FakeUploadFile(b"a,b\n1,2\n", content_type=""),
            _ctx(),  # type: ignore[arg-type]
        )
        assert out.media_type == "application/octet-stream"
        assert recorder["calls"][0]["media_type"] == "application/octet-stream"

    async def test_typed_response_carries_owner_ref_and_hash(
        self, recorder: Any
    ) -> None:
        owner_ref = '{"kind":"participant","id":"p1"}'
        out = await _iface.post_attachment(
            owner_ref, _FakeUploadFile(b"hello"), _ctx()  # type: ignore[arg-type]
        )
        assert out.owner_ref == {"kind": "participant", "id": "p1"}
        assert out.content_hash == "deadbeef"
        assert out.check_status == AttachmentCheckStatus.PENDING.value
        assert out.storage_key == "key/report.csv"
        assert isinstance(out.id, object)