"""Unit tests for ``ksma.operations.infrastructure.repositories``.

Seam: the write-parameter shaping of ``ImportRepository.create`` plus the
row→domain mappers ``_to_attachment`` / ``_to_import_batch`` / ``_to_export``
/ ``_to_metric`` / ``_to_op_run``.

Every mapper normalises a nullable JSONB column with ``row.<col> or {}``
so downstream code always receives a dict. These tests pin both halves of
that boundary: a populated column passes through **verbatim**, and a NULL
column becomes ``{}``. Dropping either half leaves an ``or -> and`` mutant
undetected (the populated column would silently become ``{}``).

The JSONB default-counts fallback on ``ImportRepository.create`` is pinned
by asserting on the bound ``counts`` INSERT parameter, not on literal SQL.
No DB, no Docker.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from ksma.operations.domain import (
    AttachmentCheckStatus,
    ExportStatus,
    ImportStatus,
    MetricTimeBasis,
    OperationalEnvironment,
    OperationalRunStatus,
    OperationKind,
)
from ksma.operations.infrastructure.repositories import (
    ImportRepository,
    _to_attachment,
    _to_export,
    _to_import_batch,
    _to_metric,
    _to_op_run,
)
from tests.unit.conftest_helpers import FakeAsyncSession


def _row(**kwargs: Any) -> SimpleNamespace:
    """Build a mapper input row with sensible defaults for unset columns."""
    base: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "created_at": datetime(2026, 10, 20, tzinfo=UTC),
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


class TestImportRepositoryCreateCounts:
    """L202 — the ``counts`` bind-param default."""

    async def test_populated_counts_are_serialised_verbatim(self) -> None:
        session = FakeAsyncSession()
        repo = ImportRepository(session, uuid4())
        batch = _import_batch(counts={"valid": 7, "invalid": 2, "total": 9})
        await repo.create(batch)
        params = _insert_params(session, "import_batch")
        assert json.loads(params["counts"]) == {"valid": 7, "invalid": 2, "total": 9}

    @pytest.mark.parametrize("empty", [None, {}])
    async def test_missing_counts_default_to_zeroes(self, empty: Any) -> None:
        """The three ``0`` literals in the default must all stay ``0``."""
        session = FakeAsyncSession()
        repo = ImportRepository(session, uuid4())
        await repo.create(_import_batch(counts=empty))
        params = _insert_params(session, "import_batch")
        assert json.loads(params["counts"]) == {
            "valid": 0,
            "invalid": 0,
            "duplicate_candidate": 0,
        }


def _insert_params(session: FakeAsyncSession, table: str) -> dict[str, Any]:
    for call in session.calls:
        if f"INSERT INTO {table}" in (call.sql or ""):
            return dict(call.params)
    raise AssertionError(f"no INSERT INTO {table} recorded")


def _import_batch(*, counts: Any) -> Any:
    from ksma.operations.domain import ImportBatch  # noqa: PLC0415

    return ImportBatch(
        id=uuid4(),
        tenant_id=uuid4(),
        requested_by=uuid4(),
        dataset_kind="participant",
        source_attachment_id=uuid4(),
        mapping={"dedupe_key": "id"},
        scope={"unit_id": "u1"},
        status=ImportStatus.VALIDATING,
        commit_request_id=None,
        counts=counts,
        created_at=None,
        updated_at=None,
    )


class TestAttachmentMapper:
    """L648 — ``owner_ref`` JSONB normalisation."""

    def test_null_owner_ref_becomes_empty_dict(self) -> None:
        row = _row(
            owner_ref=None,
            storage_key="k/1",
            original_name="a.csv",
            media_type="text/plain",
            size_bytes=10,
            content_hash=None,
            check_status=AttachmentCheckStatus.PENDING.value,
            retention_policy_id=None,
            created_by="tester",
        )
        assert _to_attachment(row).owner_ref == {}

    def test_populated_owner_ref_passes_through(self) -> None:
        row = _row(
            owner_ref={"kind": "participant", "id": "p1"},
            storage_key="k/1",
            original_name="a.csv",
            media_type="text/plain",
            size_bytes=10,
            content_hash="abc",
            check_status=AttachmentCheckStatus.APPROVED.value,
            retention_policy_id=None,
            created_by="tester",
        )
        assert _to_attachment(row).owner_ref == {"kind": "participant", "id": "p1"}


class TestImportBatchMapper:
    """L668 / L669 / L672 — ``mapping`` / ``scope`` / ``counts`` normalisation."""

    def _row(self, **kwargs: Any) -> SimpleNamespace:
        base: dict[str, Any] = {
            "id": uuid4(),
            "tenant_id": uuid4(),
            "requested_by": uuid4(),
            "dataset_kind": "participant",
            "source_attachment_id": None,
            "mapping": None,
            "scope": None,
            "status": ImportStatus.READY.value,
            "commit_request_id": None,
            "counts": None,
            "updated_at": None,
        }
        base.update(kwargs)
        return _row(**base)

    def test_null_columns_become_empty_dicts(self) -> None:
        mapped = _to_import_batch(self._row())
        assert mapped.mapping == {}
        assert mapped.scope == {}
        assert mapped.counts == {}

    def test_populated_columns_pass_through(self) -> None:
        mapping = {"dedupe_key": "id", "columns": ["id"]}
        scope = {"unit_id": "u1"}
        counts = {"valid": 3, "total": 3}
        mapped = _to_import_batch(
            self._row(mapping=mapping, scope=scope, counts=counts)
        )
        assert mapped.mapping == mapping
        assert mapped.scope == scope
        assert mapped.counts == counts
        assert mapped.status is ImportStatus.READY


class TestExportMapper:
    """L698 / L699 — ``scope`` / ``filters`` normalisation."""

    def _row(self, **kwargs: Any) -> SimpleNamespace:
        base: dict[str, Any] = {
            "id": uuid4(),
            "tenant_id": uuid4(),
            "requester_account_id": uuid4(),
            "dataset_kind": "participant",
            "scope": None,
            "filters": None,
            "as_of": datetime(2026, 10, 20, tzinfo=UTC),
            "status": ExportStatus.QUEUED.value,
            "result_attachment_id": None,
            "expires_at": None,
            "updated_at": None,
        }
        base.update(kwargs)
        return _row(**base)

    def test_null_columns_become_empty_dicts(self) -> None:
        mapped = _to_export(self._row())
        assert mapped.scope == {}
        assert mapped.filters == {}

    def test_populated_columns_pass_through(self) -> None:
        scope = {"unit_id": "u1"}
        filters = {"status": "active"}
        mapped = _to_export(self._row(scope=scope, filters=filters))
        assert mapped.scope == scope
        assert mapped.filters == filters
        assert mapped.status is ExportStatus.QUEUED


class TestMetricMapper:
    """L716 / L722 — ``source_spec`` / ``approval`` normalisation."""

    def _row(self, **kwargs: Any) -> SimpleNamespace:
        base: dict[str, Any] = {
            "id": uuid4(),
            "tenant_id": uuid4(),
            "metric_code": "M-01",
            "version_no": 1,
            "label": "Attendance",
            "source_spec": None,
            "time_basis": MetricTimeBasis.SERVICE_DATE.value,
            "effective_from": datetime(2026, 1, 1, tzinfo=UTC),
            "effective_until": None,
            "approval": None,
        }
        base.update(kwargs)
        return _row(**base)

    def test_null_columns_become_empty_dicts(self) -> None:
        mapped = _to_metric(self._row())
        assert mapped.source_spec == {}
        assert mapped.approval == {}

    def test_populated_columns_pass_through(self) -> None:
        source_spec = {"formula": "a/b", "timezone": "Asia/Jakarta"}
        approval = {"role": "MGR", "approved_at": "2026-01-01"}
        mapped = _to_metric(
            self._row(source_spec=source_spec, approval=approval)
        )
        assert mapped.source_spec == source_spec
        assert mapped.approval == approval
        assert mapped.time_basis is MetricTimeBasis.SERVICE_DATE


class TestOperationalRunMapper:
    """L734 — ``authorization_ref`` normalisation."""

    def _row(self, **kwargs: Any) -> SimpleNamespace:
        base: dict[str, Any] = {
            "id": uuid4(),
            "tenant_id": uuid4(),
            "operation_kind": OperationKind.RESTORE.value,
            "environment": OperationalEnvironment.TEST.value,
            "execution_subject": uuid4(),
            "authorization_ref": None,
            "started_at": datetime(2026, 10, 20, tzinfo=UTC),
            "finished_at": None,
            "status": OperationalRunStatus.RUNNING.value,
            "evidence": None,
        }
        base.update(kwargs)
        return _row(**base)

    def test_null_authorization_ref_becomes_empty_dict(self) -> None:
        mapped = _to_op_run(self._row())
        assert mapped.authorization_ref == {}
        assert mapped.status is OperationalRunStatus.RUNNING

    def test_populated_authorization_ref_passes_through(self) -> None:
        authz = {"ticket": "OPS-1", "approver": "admin"}
        mapped = _to_op_run(self._row(authorization_ref=authz))
        assert mapped.authorization_ref == authz
        assert mapped.operation_kind is OperationKind.RESTORE
        assert mapped.environment is OperationalEnvironment.TEST
        assert isinstance(mapped.id, UUID)