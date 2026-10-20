"""Integration tests for the Operations BC (data dictionary V19–V24 + V20).

Per ADR-0001 + ADR-0002 + ticket 09-operations-slice acceptance:

- V19: attachment rights re-check refuses ``quarantined`` and ``rejected``
  state at fetch time, even when the URL is leaked (R16).
- V20: 5-row import with one cross-tenant row ⇒ 0 rows committed (R18);
  ImportBatch stays in 'failed'.
- V23: OperationalRun with operation_kind='restore' and environment='production'
  is refused.
- V24: compute_metric with denominator == 0 returns
  ``status='not_applicable'``, NOT ``ratio=0``.

Seam: the application services in ``ksma.operations.application`` running
against real Postgres 16 with RLS. We call the application functions directly
so the test exercises the same code path the FastAPI routers do.
"""
from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator
from datetime import UTC
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from ksma.operations.application import (
    commit_import,
    compute_metric,
    define_metric,
    finish_operational_run,
    preview_import,
    queue_export,
    read_attachment_bytes,
    reset_storage_singleton,
    run_export,
    scan_attachment,
    start_operational_run,
    upload_attachment,
)
from ksma.operations.domain import (
    AttachmentCheckStatus,
    ExportStatus,
    MetricTimeBasis,
    OperationalEnvironment,
    OperationalRunStatus,
    OperationKind,
)
from ksma.operations.infrastructure.repositories import (
    ImportRepository,
)
from ksma.shared.errors import OperationalError, StateConflictError
from ksma.shared.tenancy import ActorKind, TenantContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def ops_ctx(migrated_db: AsyncEngine) -> AsyncIterator[dict[str, Any]]:
    """Bootstrap tenant + service_principal + account; return ctx dict.

    Returns:
        dict with keys: engine, TestSession, open_session, session_for, tenant_id,
        account_id, service_principal_id, ctx
    """
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    sp_id = uuid.uuid4()

    TestSession = async_sessionmaker(migrated_db, expire_on_commit=False)

    # Use a superuser engine to bypass RLS for seeding.
    import os as _os

    super_dsn = _os.environ["KITAPANDU_POSTGRES_DSN"].replace(
        "ksma_app:ksma_app@", "test:test@", 1
    )
    from sqlalchemy.ext.asyncio import create_async_engine

    super_engine = create_async_engine(super_dsn, pool_pre_ping=True)
    try:
        async with super_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenant (id, display_name, service_status, default_timezone, "
                    "created_at, created_by, updated_at, updated_by) "
                    "VALUES (:tid, 'Ops Test Tenant', 'active', 'Asia/Jakarta', "
                    "now(), 'seed', now(), 'seed')"
                ),
                {"tid": str(tenant_id)},
            )
            await conn.execute(
                text(
                    "INSERT INTO account "
                    "(id, auth_subject, auth_namespace, display_name, status, authz_revision, "
                    " created_at, created_by, updated_at, updated_by) "
                    "VALUES (:aid, :subj, 'default', 'Ops Test User', 'active', 1, "
                    " now(), 'seed', now(), 'seed')"
                ),
                {"aid": str(account_id), "subj": f"ops-{account_id.hex[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO service_principal "
                    "(id, principal_code, purpose, environment, credential_ref, status, created_at) "
                    "VALUES (:spid, :code, 'ops-runner', 'development', 'cred:stub', 'active', now())"
                ),
                {"spid": str(sp_id), "code": f"ops-runner-{sp_id.hex[:8]}"},
            )
            await conn.commit()
    finally:
        await super_engine.dispose()

    # Local attachment storage root — use a per-test tempdir.
    import tempfile

    tmp_root = tempfile.mkdtemp(prefix="ksma-ops-test-")
    _os.environ["KITAPANDU_LOCAL_ATTACHMENT_DIR"] = tmp_root
    _os.environ["KITAPANDU_USE_LOCAL_STORAGE"] = "1"
    reset_storage_singleton()

    ctx = TenantContext(
        account_id=account_id,
        membership_id=uuid.uuid4(),
        tenant_id=tenant_id,
        unit_id=None,
        actor_kind=ActorKind.ACCOUNT,
        authz_revision=1,
        role_codes=("OP",),
    )

    async def _session_for(tid: uuid.UUID | None = None):
        target = tid or tenant_id
        async with TestSession() as session:
            await session.execute(text("SET LOCAL app.bypass_rls = 'false'"))
            await session.execute(
                text(f"SET LOCAL app.current_tenant = '{target}'")
            )
            yield session

    async def _open_session(tid: uuid.UUID | None = None) -> Any:
        """Open a fresh tenant-scoped session. Commits must be called explicitly."""
        target = tid or tenant_id
        sess = TestSession()
        await sess.execute(text("SET LOCAL app.bypass_rls = 'false'"))
        await sess.execute(
            text(f"SET LOCAL app.current_tenant = '{target}'")
        )
        return sess

    yield {
        "engine": migrated_db,
        "TestSession": TestSession,
        "session_for": _session_for,
        "open_session": _open_session,
        "tenant_id": tenant_id,
        "account_id": account_id,
        "service_principal_id": sp_id,
        "ctx": ctx,
        "storage_root": tmp_root,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _png_payload() -> bytes:
    """Minimal 1x1 PNG so the storage has real bytes."""
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4"
        b"\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa3"
        b"\x35\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82"
    )


# ---------------------------------------------------------------------------
# V19 — attachment rights re-check refuses quarantined + rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v19_quarantined_attachment_cannot_be_downloaded(ops_ctx) -> None:
    """V19: even with a known attachment_id, fetching bytes after the row's
    check_status flips to 'quarantined' or 'rejected' must fail.

    We use the application services directly so we exercise the same code
    path the FastAPI GET /attachments/{id}/content router does.
    """
    ctx = ops_ctx["ctx"]
    open_session = ops_ctx["open_session"]

    # Upload — auto-pending.
    sess = await open_session()
    try:
        result = await upload_attachment(
            sess,
            ctx,
            owner_ref={"kind": "participant", "id": str(uuid.uuid4())},
            original_name="avatar.png",
            media_type="image/png",
            stream=io.BytesIO(_png_payload()),
        )
        attachment_id = result.attachment_id
        await sess.commit()
    finally:
        await sess.close()

    # Manually mark the row quarantined using a SQL update.
    sess = await open_session()
    try:
        await sess.execute(
            text(
                "UPDATE attachment SET check_status='quarantined' "
                "WHERE id=:id AND tenant_id=:tid"
            ),
            {"id": str(attachment_id), "tid": str(ops_ctx["tenant_id"])},
        )
        await sess.commit()
    finally:
        await sess.close()

    # Reread the row and verify the rights check refuses.
    sess = await open_session()
    try:
        with pytest.raises(StateConflictError) as exc_info:
            await read_attachment_bytes(sess, ctx, attachment_id=attachment_id)
        assert "quarantined" in str(exc_info.value).lower()
    finally:
        await sess.close()


@pytest.mark.asyncio
async def test_v19_rejected_and_pending_and_deleted_are_refused(ops_ctx) -> None:
    """V19: rejected / pending / deleted are all non-downloadable."""
    ctx = ops_ctx["ctx"]
    open_session = ops_ctx["open_session"]

    for bad_status in ("rejected", "deleted", "pending"):
        sess = await open_session()
        try:
            result = await upload_attachment(
                sess,
                ctx,
                owner_ref={"kind": "participant", "id": str(uuid.uuid4())},
                original_name=f"bad-{bad_status}.png",
                media_type="image/png",
                stream=io.BytesIO(_png_payload()),
            )
            aid = result.attachment_id
            await sess.commit()
        finally:
            await sess.close()

        sess = await open_session()
        try:
            await sess.execute(
                text(
                    "UPDATE attachment SET check_status=:s "
                    "WHERE id=:id AND tenant_id=:tid"
                ),
                {"s": bad_status, "id": str(aid), "tid": str(ops_ctx["tenant_id"])},
            )
            await sess.commit()
        finally:
            await sess.close()

        sess = await open_session()
        try:
            with pytest.raises(StateConflictError):
                await read_attachment_bytes(sess, ctx, attachment_id=aid)
        finally:
            await sess.close()


@pytest.mark.asyncio
async def test_v19_approved_attachment_can_be_downloaded(ops_ctx) -> None:
    """V19 (positive control): approved attachment serves bytes."""
    ctx = ops_ctx["ctx"]
    open_session = ops_ctx["open_session"]
    payload = _png_payload()

    sess = await open_session()
    try:
        result = await upload_attachment(
            sess,
            ctx,
            owner_ref={"kind": "participant", "id": str(uuid.uuid4())},
            original_name="ok.png",
            media_type="image/png",
            stream=io.BytesIO(payload),
        )
        attachment_id = result.attachment_id
        await sess.commit()
    finally:
        await sess.close()

    sess = await open_session()
    try:
        await scan_attachment(sess, ctx, attachment_id=attachment_id)
        await sess.commit()
    finally:
        await sess.close()

    sess = await open_session()
    try:
        att, body = await read_attachment_bytes(
            sess, ctx, attachment_id=attachment_id
        )
        assert att.check_status is AttachmentCheckStatus.APPROVED
        assert body == payload
    finally:
        await sess.close()


# ---------------------------------------------------------------------------
# V20 — atomic import rejects cross-tenant row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v20_atomic_commit_rejects_cross_tenant_row(ops_ctx) -> None:
    """V20: 5-row import with one cross-tenant row ⇒ 0 rows committed."""
    ctx = ops_ctx["ctx"]
    open_session = ops_ctx["open_session"]
    other_tid = uuid.uuid4()

    # Upload the source attachment with 5 JSON rows on its own lines.
    payload = b"\n".join(
        f'{{"id":"a{i}","name":"row {i}","value":{i}}}'.encode() for i in range(5)
    ) + b"\n"

    sess = await open_session()
    try:
        up = await upload_attachment(
            sess,
            ctx,
            owner_ref={"kind": "indicator_import", "id": str(uuid.uuid4())},
            original_name="rows.json",
            media_type="text/plain",
            stream=io.BytesIO(payload),
        )
        source_id = up.attachment_id
        await scan_attachment(sess, ctx, attachment_id=source_id)
        await sess.commit()
    finally:
        await sess.close()

    sess = await open_session()
    try:
        batch, _ = await preview_import(
            sess,
            ctx,
            dataset_kind="indicator",
            mapping={"required": ["id"], "columns": ["id", "name", "value"], "dedupe_key": "id"},
            source_attachment_id=source_id,
        )
        await sess.commit()
    finally:
        await sess.close()

    # Read the rows in a fresh session.
    sess = await open_session()
    try:
        rows = (
            await sess.execute(
                text(
                    "SELECT row_no, target_ref FROM import_row_result "
                    "WHERE import_batch_id = :bid ORDER BY row_no"
                ),
                {"bid": str(batch.id)},
            )
        ).all()
    finally:
        await sess.close()

    # Mutate row 3's target_ref to carry a different tenant_id.
    cross_row_no = rows[2].row_no
    sess = await open_session()
    try:
        await sess.execute(
            text(
                "UPDATE import_row_result "
                "SET target_ref = jsonb_set(COALESCE(target_ref, '{}'::jsonb), "
                "                          '{tenant_id}', to_jsonb(CAST(:other AS text))) "
                "WHERE import_batch_id = :bid AND row_no = :rn"
            ),
            {"other": str(other_tid), "bid": str(batch.id), "rn": cross_row_no},
        )
        await sess.commit()
    finally:
        await sess.close()

    # Now commit ⇒ must raise OperationalError, leave batch non-committed.
    raised = False
    sess = await open_session()
    try:
        try:
            await commit_import(
                sess,
                ctx,
                import_batch_id=batch.id,
                commit_request_id=uuid.uuid4(),
            )
        except OperationalError as e:
            assert "v20" in str(e).lower() or "cross-tenant" in str(e).lower()
            raised = True
    finally:
        await sess.close()
    assert raised, "commit must raise OperationalError on cross-tenant row (V20)"

    # Batch must NOT have advanced to ``committed``.
    sess = await open_session()
    try:
        irepo = ImportRepository(sess, ops_ctx["tenant_id"])
        b2 = await irepo.get(batch.id)
        assert b2 is not None
        assert b2.status.value != "committed", (
            f"V20 violated: batch unexpectedly committed; status={b2.status.value}"
        )

        # No row should be COMMITTED.
        rows_after = await irepo.list_row_results(batch.id)
        committed = [r for r in rows_after if r.outcome.value == "committed"]
        assert committed == [], (
            f"V20 violated: {len(committed)} row(s) committed; expected 0"
        )
    finally:
        await sess.close()


# ---------------------------------------------------------------------------
# V23 — operational run refuses restore outside non-prod
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v23_restore_in_production_is_refused(ops_ctx) -> None:
    """V23: operation_kind='restore' with environment='production' raises."""
    ctx = ops_ctx["ctx"]
    open_session = ops_ctx["open_session"]
    sp_id = ops_ctx["service_principal_id"]

    sess = await open_session()
    try:
        with pytest.raises(StateConflictError) as exc_info:
            await start_operational_run(
                sess,
                ctx,
                operation_kind=OperationKind.RESTORE,
                environment=OperationalEnvironment.PRODUCTION,
                execution_subject=sp_id,
                authorization_ref={"ticket": "INC-123", "approver": "ops"},
            )
        assert "v23" in str(exc_info.value).lower() or "production" in str(exc_info.value).lower()
    finally:
        await sess.close()


@pytest.mark.asyncio
async def test_v23_restore_in_development_is_allowed(ops_ctx) -> None:
    """V23 (positive control): restore in development proceeds."""
    ctx = ops_ctx["ctx"]
    open_session = ops_ctx["open_session"]
    sp_id = ops_ctx["service_principal_id"]

    sess = await open_session()
    op_run_id = None
    try:
        op_run = await start_operational_run(
            sess,
            ctx,
            operation_kind=OperationKind.RESTORE,
            environment=OperationalEnvironment.DEVELOPMENT,
            execution_subject=sp_id,
            authorization_ref={"ticket": "INC-123", "approver": "ops"},
        )
        assert op_run.status is OperationalRunStatus.RUNNING
        op_run_id = op_run.id
        await sess.commit()
    finally:
        await sess.close()

    sess = await open_session()
    try:
        finished = await finish_operational_run(
            sess,
            ctx,
            op_run_id=op_run_id,
            status=OperationalRunStatus.SUCCEEDED,
            evidence={"rows_restored": 100, "duration_seconds": 12},
        )
        assert finished.status is OperationalRunStatus.SUCCEEDED
    finally:
        await sess.close()


# ---------------------------------------------------------------------------
# V24 — denominator zero ⇒ "not_applicable", not "0"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v24_metric_denominator_zero_is_not_applicable(ops_ctx) -> None:
    """V24: numerator=10, denominator=0 must return status='not_applicable',
    ratio=None — NOT ratio=0.
    """
    ctx = ops_ctx["ctx"]
    open_session = ops_ctx["open_session"]

    sess = await open_session()
    metric_id = None
    try:
        metric = await define_metric(
            sess,
            ctx,
            metric_code="v24_test",
            version_no=1,
            label="V24 test indicator",
            source_spec={"timezone": "Asia/Jakarta"},
            time_basis=MetricTimeBasis.RECORD_DATE,
            effective_from=None,
            effective_until=None,
            approval={"approver": "seed", "approved_at": "2026-01-01T00:00:00Z"},
        )
        metric_id = metric.id
        await sess.commit()
    finally:
        await sess.close()

    sess = await open_session()
    try:
        comp = await compute_metric(
            sess, ctx, metric_id=metric_id, numerator=10, denominator=0
        )
    finally:
        await sess.close()
    assert comp.status == "not_applicable", (
        f"V24 violated: expected 'not_applicable', got {comp.status!r}"
    )
    assert comp.ratio is None, (
        f"V24 violated: ratio must not be 0 for zero denominator; got {comp.ratio!r}"
    )
    assert comp.numerator == 10
    assert comp.denominator == 0
    assert comp.timezone == "Asia/Jakarta"


@pytest.mark.asyncio
async def test_v24_metric_normal_computation(ops_ctx) -> None:
    """V24 (positive control): numerator=10, denominator=4 ⇒ status='ok',
    ratio=2.5."""
    ctx = ops_ctx["ctx"]
    open_session = ops_ctx["open_session"]

    sess = await open_session()
    metric_id = None
    try:
        metric = await define_metric(
            sess,
            ctx,
            metric_code="v24_normal",
            version_no=1,
            label="V24 normal",
            source_spec={"timezone": "Asia/Jakarta"},
            time_basis=MetricTimeBasis.RECORD_DATE,
            effective_from=None,
            effective_until=None,
            approval={"approver": "seed"},
        )
        metric_id = metric.id
        await sess.commit()
    finally:
        await sess.close()

    sess = await open_session()
    try:
        comp = await compute_metric(
            sess, ctx, metric_id=metric_id, numerator=10, denominator=4
        )
    finally:
        await sess.close()
    assert comp.status == "ok"
    assert comp.ratio == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# Smoke test for upload/scan/upload-complete happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_scan_approve_round_trip(ops_ctx) -> None:
    """Sanity: upload + scan + read returns the original bytes."""
    ctx = ops_ctx["ctx"]
    open_session = ops_ctx["open_session"]
    payload = b"this is a plain text document\nwith two lines.\n"

    sess = await open_session()
    aid = None
    try:
        result = await upload_attachment(
            sess,
            ctx,
            owner_ref={"kind": "incident", "id": str(uuid.uuid4())},
            original_name="incident.txt",
            media_type="text/plain",
            stream=io.BytesIO(payload),
        )
        aid = result.attachment_id
        assert result.check_status is AttachmentCheckStatus.PENDING
        await sess.commit()
    finally:
        await sess.close()

    sess = await open_session()
    try:
        att = await scan_attachment(sess, ctx, attachment_id=aid)
        assert att.check_status is AttachmentCheckStatus.APPROVED
        await sess.commit()
    finally:
        await sess.close()

    sess = await open_session()
    try:
        _, body = await read_attachment_bytes(sess, ctx, attachment_id=aid)
        assert body == payload
    finally:
        await sess.close()


# ---------------------------------------------------------------------------
# Smoke test for export queue → run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_queue_then_run(ops_ctx) -> None:
    """Sanity: queue an export then run it produces a downloadable result."""
    ctx = ops_ctx["ctx"]
    open_session = ops_ctx["open_session"]
    from datetime import datetime

    sess = await open_session()
    job_id = None
    try:
        job = await queue_export(
            sess,
            ctx,
            dataset_kind="participants",
            scope={"unit_id": None},
            filters={"status": "active"},
            as_of=datetime.now(UTC),
        )
        assert job.status is ExportStatus.QUEUED
        job_id = job.id
        await sess.commit()
    finally:
        await sess.close()

    sess = await open_session()
    try:
        finished = await run_export(
            sess, ctx, export_job_id=job_id, body=b"id,name\n1,alice\n"
        )
        assert finished.status is ExportStatus.READY
        assert finished.result_attachment_id is not None
    finally:
        await sess.close()
