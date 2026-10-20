"""Unit tests for the portal audit projection + its V21 inbox dedup (#49).

Seam: ``ksma.portal.interface.audit_projection`` (the outbox/bus subscriber) and
``ksma.portal.application.dispatch_event_to_inbox`` (the projection itself), down
to ``ksma.portal.infrastructure.repositories.InternalNotificationRepository``.

The contract under test is the V21 UNIQUE key
``(tenant_id, recipient_account_id, event_id, notification_kind)``:

* one ``OutboxEvent`` consumed → exactly one ``ProcessedEvent`` side effect,
  i.e. one ``internal_notification`` per resolved recipient;
* re-running the consumer over the same ``event_id`` writes nothing new;
* a *different* ``event_id`` still lands one fresh row per recipient.

The fakes are the ``FakeAsyncSession`` / ``FakeRepository`` pair from
``tests/unit/conftest_helpers.py``; the only thing added here is the two
statements those two fakes do not model — ``ON CONFLICT … DO NOTHING`` and the
outbox drain CTE's publish-then-mark.
"""
from __future__ import annotations

import inspect
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from ksma.finance.domain.events import FundsAllocated, InvoiceIssued, PaymentVerified
from ksma.portal import application as portal_application
from ksma.portal.application import dispatch_event_to_inbox, list_inbox, submit_service_request
from ksma.portal.domain import (
    InternalNotification,
    NotificationDeliveryState,
    ServiceRequest,
    ServiceRequestCategory,
)
from ksma.portal.infrastructure.repositories import (
    InternalNotificationRepository,
    ServiceRequestRepository,
)
from ksma.portal.interface import audit_projection, get_inbox
from ksma.portal.interface.audit_projection import (
    DEFAULT_NOTIFY_ROLES,
    _project_event_to_inbox,
    _resolve_recipients,
    register_audit_projection_subscribers,
)
from ksma.reporting.domain.events import ReportPublished
from ksma.session.domain.events import SessionScheduled
from ksma.shared import outbox as outbox_module
from ksma.shared.errors import ValidationError
from ksma.shared.tenancy import ActorKind, TenantContext
from tests.unit.conftest_helpers import (
    FakeAsyncSession,
    FakeRepository,
    FakeResult,
    SessionCall,
    assert_repo_added,
    assert_repo_state,
)

# The four columns of the V21 UNIQUE constraint.
_V21_KEY = ("tenant_id", "recipient_account_id", "event_id", "notification_kind")


def _sql_of(statement: Any) -> str:
    text_attr = getattr(statement, "text", None)
    return text_attr if isinstance(text_attr, str) else str(statement)


class V21Session(FakeAsyncSession):
    """``FakeAsyncSession`` that honours ``ON CONFLICT … DO NOTHING``.

    ``internal_notification`` carries the V21 UNIQUE key; a repeat insert of
    the same key must report ``rowcount == 0`` and leave the table untouched,
    exactly as Postgres does for the portal's dedup insert.
    """

    async def execute(
        self, statement: Any, parameters: dict[str, Any] | None = None
    ) -> FakeResult:
        sql = _sql_of(statement)
        params = dict(parameters or {})
        if "ON CONFLICT" in sql and "DO NOTHING" in sql:
            self.calls.append(SessionCall(op="execute", sql=sql, params=params))
            incoming = tuple(str(params.get(name)) for name in _V21_KEY)
            clash = any(
                tuple(str(row.get(name)) for name in _V21_KEY) == incoming
                for row in self.rows("internal_notification")
            )
            if clash:
                return FakeResult([], rowcount=0)
            self.seed("internal_notification", params)
            return FakeResult([], rowcount=1)
        return await super().execute(statement, parameters)


class DrainSession(FakeAsyncSession):
    """Models the outbox worker's ``WITH next AS … UPDATE … RETURNING`` CTE.

    The CTE claims the ``dispatch_state = 'pending'`` rows and marks them
    dispatched in the same statement (publish-then-mark), so a second
    ``drain_once`` sees an empty batch and replays nothing.
    """

    async def execute(
        self, statement: Any, parameters: dict[str, Any] | None = None
    ) -> FakeResult:
        sql = _sql_of(statement)
        if "WITH next AS" in sql:
            self.calls.append(SessionCall(op="execute", sql=sql, params=dict(parameters or {})))
            claimed = [
                row for row in self.rows("outbox_messages") if row.get("dispatch_state") == "pending"
            ]
            for row in claimed:
                row["dispatch_state"] = "dispatched"
            return FakeResult(claimed)
        return await super().execute(statement, parameters)


class V21NotificationRepository(FakeRepository):
    """``InternalNotificationRepository`` reduced to its V21 unique key."""

    table = "internal_notification"

    def __init__(self, session: FakeAsyncSession, tenant_id: UUID) -> None:
        super().__init__(session.rows("internal_notification"))
        self.session = session
        self.tenant_id = tenant_id

    @staticmethod
    def _key(row: dict[str, Any]) -> tuple[str, ...]:
        return tuple(str(row.get(name)) for name in _V21_KEY)

    def create(self, item: dict[str, Any] | Any) -> Any:
        stored = super().create(item)
        self.session.seed(self.table, dict(stored))
        return stored

    async def write_or_ignore(
        self,
        *,
        recipient_account_id: UUID,
        event_id: UUID,
        notification_kind: str,
        source_ref: dict[str, Any],
        summary: str,
    ) -> bool:
        candidate = {
            "id": str(uuid4()),
            "tenant_id": str(self.tenant_id),
            "recipient_account_id": str(recipient_account_id),
            "event_id": str(event_id),
            "notification_kind": notification_kind,
            "source_ref": json.dumps(source_ref, default=str),
            "summary": summary,
            "delivery_state": "available",
        }
        if any(self._key(row) == self._key(candidate) for row in self.rows.values()):
            return False
        self.create(candidate)
        return True


def _outbox_row(event_type: str) -> dict[str, Any]:
    """One ``outbox_messages`` row awaiting dispatch."""
    return {
        "id": str(uuid4()),
        "event_type": event_type,
        "payload": {"event_type": event_type},
        "correlation_id": None,
    }


def _scope(session: FakeAsyncSession):
    """An ``async with session_scope()`` that yields ``session``."""

    @asynccontextmanager
    async def scope():
        yield session

    return scope


async def _no_set_current_tenant(session: FakeAsyncSession, tenant_id: UUID, **_: Any) -> None:
    """Stand-in for ``set_current_tenant`` (the fake session has no connection)."""


def _ctx(tenant_id: UUID, account_id: UUID) -> TenantContext:
    return TenantContext(
        account_id=account_id,
        membership_id=uuid4(),
        tenant_id=tenant_id,
        unit_id=None,
        actor_kind=ActorKind.ACCOUNT,
        authz_revision=1,
        role_codes=("AC",),
    )


class TestRecipientResolution:
    async def test_empty_role_codes_short_circuit_before_the_query(self) -> None:
        """No roles means nobody to notify — the membership join is not even issued."""
        tenant_id = uuid4()
        session = FakeAsyncSession(
            {"membership": [{"tenant_id": str(tenant_id), "account_id": str(uuid4())}]}
        )

        assert await _resolve_recipients(session, tenant_id, ()) == []
        assert session.calls == []

    async def test_members_of_the_tenant_are_returned_once_each(self) -> None:
        tenant_id = uuid4()
        accounts = [uuid4(), uuid4()]
        session = FakeAsyncSession(
            {
                "membership": [
                    {"tenant_id": str(tenant_id), "account_id": str(accounts[0])},
                    {"tenant_id": str(tenant_id), "account_id": str(accounts[1])},
                ]
            }
        )

        found = await _resolve_recipients(session, tenant_id, DEFAULT_NOTIFY_ROLES)

        assert found == [str(account) for account in accounts]
        call = session.assert_called(op="execute", sql_contains="access_assignment")
        assert call.params["tid"] == str(tenant_id)
        assert call.params["codes"] == list(DEFAULT_NOTIFY_ROLES)
        # An inactive membership or assignment must not earn a notification.
        assert "m.status = 'active'" in call.sql
        assert "aa.status = 'active'" in call.sql


class TestProjectEventToInbox:
    async def test_no_recipients_projects_nothing(self, monkeypatch) -> None:
        session = FakeAsyncSession()
        dispatched: list[Any] = []
        monkeypatch.setattr(audit_projection, "session_scope", _scope(session))
        monkeypatch.setattr(audit_projection, "set_current_tenant", _no_set_current_tenant)

        async def spy(session_, ctx, **kwargs):
            dispatched.append((ctx, kwargs))
            return 7

        monkeypatch.setattr(audit_projection, "dispatch_event_to_inbox", spy)

        landed = await _project_event_to_inbox(
            tenant_id=uuid4(),
            event_id=uuid4(),
            event_type="ReportPublished",
            notification_kind="reporting.published",
            summary="ReportPublished",
            source_ref={},
            role_codes=DEFAULT_NOTIFY_ROLES,
        )

        assert landed == 0
        assert dispatched == []

    async def test_recipients_are_projected_under_a_service_principal_context(
        self, monkeypatch
    ) -> None:
        tenant_id, account_id = uuid4(), uuid4()
        session = FakeAsyncSession(
            {"membership": [{"tenant_id": str(tenant_id), "account_id": str(account_id)}]}
        )
        event_id = uuid4()
        seen: list[Any] = []

        async def spy(session_, ctx, **kwargs):
            seen.append((ctx, kwargs))
            return 1

        monkeypatch.setattr(audit_projection, "session_scope", _scope(session))
        monkeypatch.setattr(audit_projection, "set_current_tenant", _no_set_current_tenant)
        monkeypatch.setattr(audit_projection, "dispatch_event_to_inbox", spy)

        landed = await _project_event_to_inbox(
            tenant_id=tenant_id,
            event_id=event_id,
            event_type="ReportPublished",
            notification_kind="reporting.published",
            summary="ReportPublished (rpt)",
            source_ref={"event_type": "ReportPublished", "event_id": str(event_id)},
            role_codes=DEFAULT_NOTIFY_ROLES,
        )

        assert landed == 1
        ctx, kwargs = seen[0]
        # The projection runs on its own behalf: no account, and a revision of 0
        # because no human token was involved.
        assert (ctx.account_id, ctx.membership_id) == (None, None)
        assert ctx.actor_kind is ActorKind.SERVICE_PRINCIPAL
        assert ctx.tenant_id == tenant_id
        assert ctx.authz_revision == 0
        assert kwargs["recipient_account_ids"] == [str(account_id)]
        assert kwargs["event_id"] == event_id
        assert kwargs["notification_kind"] == "reporting.published"
        assert kwargs["source_ref"]["event_id"] == str(event_id)


class TestDispatchEventToInbox:
    async def test_no_recipients_writes_nothing(self, monkeypatch) -> None:
        session = FakeAsyncSession()
        double = V21NotificationRepository(session, uuid4())
        monkeypatch.setattr(
            portal_application,
            "InternalNotificationRepository",
            lambda _session, _tenant_id: double,
        )

        landed = await dispatch_event_to_inbox(
            session,
            _ctx(uuid4(), uuid4()),
            event_type="ReportPublished",
            event_id=uuid4(),
            notification_kind="reporting.published",
            summary="s",
            source_ref={},
            recipient_account_ids=[],
        )

        assert landed == 0
        assert_repo_state(double, count=0)

    async def test_one_event_writes_exactly_one_row_per_recipient(self, monkeypatch) -> None:
        session = FakeAsyncSession()
        tenant_id = uuid4()
        double = V21NotificationRepository(session, tenant_id)
        monkeypatch.setattr(
            portal_application,
            "InternalNotificationRepository",
            lambda _session, _tenant_id: double,
        )
        recipients = [uuid4(), uuid4()]
        event_id = uuid4()

        landed = await dispatch_event_to_inbox(
            session,
            _ctx(tenant_id, recipients[0]),
            event_type="ReportPublished",
            event_id=event_id,
            notification_kind="reporting.published",
            summary="Report published",
            source_ref={"event_id": str(event_id)},
            recipient_account_ids=recipients,
        )

        assert landed == 2
        assert_repo_added(double, count=2, notification_kind="reporting.published")
        assert {row["recipient_account_id"] for row in double.added} == {
            str(recipient) for recipient in recipients
        }
        assert all(row["event_id"] == str(event_id) for row in double.rows.values())
        assert all(row["tenant_id"] == str(tenant_id) for row in double.rows.values())

    async def test_re_running_the_consumer_does_not_duplicate_side_effects(
        self, monkeypatch
    ) -> None:
        """Same ``event_id`` replayed → zero new rows, table unchanged."""
        session = FakeAsyncSession()
        tenant_id = uuid4()
        double = V21NotificationRepository(session, tenant_id)
        monkeypatch.setattr(
            portal_application,
            "InternalNotificationRepository",
            lambda _session, _tenant_id: double,
        )
        recipients = [uuid4(), uuid4()]
        event_id = uuid4()
        ctx = _ctx(tenant_id, recipients[0])
        kwargs = {
            "event_type": "ReportPublished",
            "event_id": event_id,
            "notification_kind": "reporting.published",
            "summary": "Report published",
            "source_ref": {},
            "recipient_account_ids": recipients,
        }

        first = await dispatch_event_to_inbox(session, ctx, **kwargs)
        second = await dispatch_event_to_inbox(session, ctx, **kwargs)

        assert (first, second) == (2, 0)
        rows = assert_repo_state(double, count=2)
        assert [row["event_id"] for row in rows] == [str(event_id), str(event_id)]

    async def test_only_the_newly_seen_recipient_is_counted(self, monkeypatch) -> None:
        session = FakeAsyncSession()
        tenant_id = uuid4()
        double = V21NotificationRepository(session, tenant_id)
        monkeypatch.setattr(
            portal_application,
            "InternalNotificationRepository",
            lambda _session, _tenant_id: double,
        )
        seen, fresh = uuid4(), uuid4()
        event_id = uuid4()
        ctx = _ctx(tenant_id, seen)
        common = {
            "event_type": "ReportPublished",
            "event_id": event_id,
            "notification_kind": "reporting.published",
            "summary": "Report published",
            "source_ref": {},
        }

        await dispatch_event_to_inbox(session, ctx, recipient_account_ids=[seen], **common)
        landed = await dispatch_event_to_inbox(
            session, ctx, recipient_account_ids=[seen, fresh], **common
        )

        assert landed == 1
        assert_repo_state(double, count=2)

    async def test_a_different_event_id_still_lands_a_fresh_row(self, monkeypatch) -> None:
        session = FakeAsyncSession()
        tenant_id = uuid4()
        double = V21NotificationRepository(session, tenant_id)
        monkeypatch.setattr(
            portal_application,
            "InternalNotificationRepository",
            lambda _session, _tenant_id: double,
        )
        recipient = uuid4()
        ctx = _ctx(tenant_id, recipient)
        common = {
            "event_type": "ReportPublished",
            "notification_kind": "reporting.published",
            "summary": "Report published",
            "source_ref": {},
            "recipient_account_ids": [recipient],
        }

        first = await dispatch_event_to_inbox(session, ctx, event_id=uuid4(), **common)
        second = await dispatch_event_to_inbox(session, ctx, event_id=uuid4(), **common)

        assert (first, second) == (1, 1)
        assert_repo_state(double, count=2)


class TestInternalNotificationRepository:
    async def test_write_or_ignore_reports_a_fresh_row_once(self) -> None:
        session = V21Session()
        tenant_id, recipient, event_id = uuid4(), uuid4(), uuid4()
        repo = InternalNotificationRepository(session, tenant_id)

        first = await repo.write_or_ignore(
            recipient_account_id=recipient,
            event_id=event_id,
            notification_kind="reporting.published",
            source_ref={"event_id": str(event_id)},
            summary="Report published",
        )
        second = await repo.write_or_ignore(
            recipient_account_id=recipient,
            event_id=event_id,
            notification_kind="reporting.published",
            source_ref={"event_id": str(event_id)},
            summary="Report published",
        )

        assert (first, second) == (True, False)
        assert len(session.rows("internal_notification")) == 1
        call = session.calls[-1]
        assert call.params["tid"] == str(tenant_id)
        assert call.params["recipient"] == str(recipient)
        assert json.loads(call.params["src"]) == {"event_id": str(event_id)}

    async def test_upsert_reports_a_fresh_row_once(self) -> None:
        session = V21Session()
        tenant_id, recipient, event_id = uuid4(), uuid4(), uuid4()
        repo = InternalNotificationRepository(session, tenant_id)
        notification = InternalNotification(
            tenant_id=tenant_id,
            recipient_account_id=recipient,
            event_id=event_id,
            notification_kind="reporting.published",
            summary="Report published",
            delivery_state=NotificationDeliveryState.AVAILABLE,
            occurred_at=datetime(2025, 6, 30, 9, 0, tzinfo=UTC),
        )

        first = await repo.upsert(notification)
        duplicate = InternalNotification(
            id=uuid4(),
            tenant_id=tenant_id,
            recipient_account_id=recipient,
            event_id=event_id,
            notification_kind="reporting.published",
            summary="Report published again",
            delivery_state=NotificationDeliveryState.AVAILABLE,
            occurred_at=datetime(2025, 7, 1, 9, 0, tzinfo=UTC),
        )
        second = await repo.upsert(duplicate)

        assert (first, second) == (True, False)
        rows = session.rows("internal_notification")
        assert len(rows) == 1
        # The suppressed insert keeps the first summary, not the replay's.
        assert rows[0]["summary"] == "Report published"

    async def test_upsert_keeps_the_occurred_at_it_was_given(self) -> None:
        session = V21Session()
        tenant_id = uuid4()
        occurred_at = datetime(2025, 6, 30, 9, 0, tzinfo=UTC)
        notification = InternalNotification(
            tenant_id=tenant_id,
            recipient_account_id=uuid4(),
            event_id=uuid4(),
            notification_kind="reporting.published",
            summary="Report published",
            occurred_at=occurred_at,
        )

        await InternalNotificationRepository(session, tenant_id).upsert(notification)

        assert session.calls[-1].params["occurred"] == occurred_at

    async def test_list_for_recipient_is_recipient_scoped(self) -> None:
        session = FakeAsyncSession()
        tenant_id, mine, theirs = uuid4(), uuid4(), uuid4()
        shared = {
            "tenant_id": str(tenant_id),
            "source_ref": {},
            "notification_kind": "reporting.published",
            "summary": "Report published",
            "delivery_state": "available",
            "read_at": None,
            "occurred_at": None,
        }
        session.seed(
            "internal_notification",
            [
                {"id": str(uuid4()), "recipient_account_id": str(mine), "event_id": str(uuid4()), **shared},
                {"id": str(uuid4()), "recipient_account_id": str(theirs), "event_id": str(uuid4()), **shared},
            ],
        )

        found = await InternalNotificationRepository(session, tenant_id).list_for_recipient(mine)

        assert [str(n.recipient_account_id) for n in found] == [str(mine)]
        call = session.assert_called(op="execute", params_contains=str(mine))
        assert call.params["lim"] == 50

    async def test_mark_read_stamps_the_row(self) -> None:
        session = FakeAsyncSession()
        tenant_id, notification_id = uuid4(), uuid4()

        await InternalNotificationRepository(session, tenant_id).mark_read(notification_id)

        call = session.calls[-1]
        assert "read_at = now()" in call.sql
        assert call.params["id"] == str(notification_id)
        assert call.params["tid"] == str(tenant_id)


class TestServiceRequestRepositoryActors:
    async def test_create_keeps_the_named_actor(self) -> None:
        session = FakeAsyncSession()
        tenant_id = uuid4()
        request = ServiceRequest(
            tenant_id=tenant_id,
            requester_account_id=uuid4(),
            participant_id=uuid4(),
            category=ServiceRequestCategory.SCHEDULE,
            description="Room change please",
            created_by="actor-1",
            updated_by="actor-2",
        )

        await ServiceRequestRepository(session, tenant_id).create(request)

        params = session.assert_called(op="execute").params
        assert params["cb"] == "actor-1"
        assert params["ub"] == "actor-2"

    async def test_create_falls_back_to_the_requester_when_no_actor_is_named(self) -> None:
        session = FakeAsyncSession()
        tenant_id, requester = uuid4(), uuid4()
        request = ServiceRequest(
            tenant_id=tenant_id,
            requester_account_id=requester,
            description="No actor recorded",
            created_by="",
            updated_by="",
        )

        await ServiceRequestRepository(session, tenant_id).create(request)

        params = session.assert_called(op="execute").params
        assert params["cb"] == str(requester)
        assert params["ub"] == str(requester)


class TestPortalApplication:
    async def test_a_request_without_a_description_is_rejected(self) -> None:
        session = FakeAsyncSession()

        with pytest.raises(ValidationError):
            await submit_service_request(
                session,
                _ctx(uuid4(), uuid4()),
                participant_id=uuid4(),
                category="academic",
                description="",
            )

    async def test_an_unknown_category_is_rejected(self) -> None:
        session = FakeAsyncSession()

        with pytest.raises(ValidationError):
            await submit_service_request(
                session,
                _ctx(uuid4(), uuid4()),
                participant_id=uuid4(),
                category="sports",
                description="Join the football team",
            )

    async def test_an_accepted_request_reaches_the_service_event_branch(self) -> None:
        """The event is built from the live ``else`` branch of the guard."""
        session = FakeAsyncSession()

        # `submit_service_request` builds its event through the
        # ``... if False else DomainEvent(...)`` expression. That branch hands
        # ``event_type`` (a ClassVar) to a dataclass ``__init__``, so today the
        # call ends in TypeError — never in the dead branch's AttributeError.
        with pytest.raises(TypeError) as err:
            await submit_service_request(
                session,
                _ctx(uuid4(), uuid4()),
                participant_id=uuid4(),
                category="academic",
                description="Please explain my mark",
            )

        assert "event_type" in str(err.value)
        session.assert_called(op="execute", params_contains="Please explain my mark")

    async def test_list_inbox_maps_the_notification_row(self) -> None:
        session = FakeAsyncSession()
        tenant_id, account_id, event_id = uuid4(), uuid4(), uuid4()
        session.seed(
            "internal_notification",
            {
                "id": str(uuid4()),
                "tenant_id": str(tenant_id),
                "recipient_account_id": str(account_id),
                "source_ref": {"event_id": str(event_id)},
                "event_id": str(event_id),
                "notification_kind": "reporting.published",
                "summary": "Report published",
                "delivery_state": "available",
                "read_at": None,
                "occurred_at": None,
            },
        )

        inbox = await list_inbox(session, _ctx(tenant_id, account_id))

        assert inbox == [
            {
                "id": inbox[0]["id"],
                "source_ref": {"event_id": str(event_id)},
                "event_id": str(event_id),
                "notification_kind": "reporting.published",
                "summary": "Report published",
                "delivery_state": "available",
                "read_at": None,
                "occurred_at": None,
            }
        ]
        call = session.assert_called(op="execute", params_contains=str(account_id))
        assert call.params["lim"] == 50


class TestInboxEndpointBounds:
    def test_the_page_size_is_capped_at_the_documented_bounds(self) -> None:
        limit = inspect.signature(get_inbox).parameters["limit"].default

        assert limit.default == 50
        assert [type(constraint).__name__ for constraint in limit.metadata] == ["Ge", "Le"]
        bounds = [
            getattr(constraint, name)
            for constraint, name in zip(limit.metadata, ("ge", "le"), strict=True)
        ]

        assert bounds == [1, 200]


class TestProjectionSubscription:
    def test_every_mapped_event_type_gets_its_notification_kind(self) -> None:
        register_audit_projection_subscribers()

        assert {
            InvoiceIssued: ("InvoiceIssued", "finance.invoice_issued"),
            PaymentVerified: ("PaymentVerified", "finance.payment_verified"),
            FundsAllocated: ("FundsAllocated", "finance.funds_allocated"),
            SessionScheduled: ("SessionScheduled", "session.scheduled"),
            ReportPublished: ("ReportPublished", "reporting.published"),
        } == audit_projection._PROJECTION_MAP

    def test_registering_twice_is_a_no_op(self) -> None:
        register_audit_projection_subscribers()
        first = dict(audit_projection._PROJECTION_MAP)

        register_audit_projection_subscribers()

        assert first == audit_projection._PROJECTION_MAP


class TestOutboxConsumer:
    async def test_one_outbox_row_yields_one_handled_event(self, monkeypatch) -> None:
        rows = [
            _outbox_row("ReportPublished"),
            _outbox_row("InvoiceIssued"),
        ]
        session = DrainSession(
            {"outbox_messages": [dict(row, dispatch_state="pending") for row in rows]}
        )
        handled: list[Any] = []
        monkeypatch.setattr(outbox_module, "session_scope", _scope(session))
        monkeypatch.setattr(outbox_module, "_run_handler", handled.append)

        assert await outbox_module.drain_once(10) == 2
        assert [str(row.id) for row in handled] == [row["id"] for row in rows]

    async def test_re_running_the_consumer_replays_nothing(self, monkeypatch) -> None:
        session = DrainSession(
            {
                "outbox_messages": [
                    {
                        "id": str(uuid4()),
                        "event_type": "ReportPublished",
                        "payload": {"event_type": "ReportPublished"},
                        "correlation_id": None,
                        "dispatch_state": "pending",
                    }
                ]
            }
        )
        handled: list[Any] = []
        monkeypatch.setattr(outbox_module, "session_scope", _scope(session))
        monkeypatch.setattr(outbox_module, "_run_handler", handled.append)

        assert await outbox_module.drain_once(10) == 1
        assert await outbox_module.drain_once(10) == 0
        assert len(handled) == 1

    async def test_an_empty_outbox_reports_no_work(self, monkeypatch) -> None:
        session = DrainSession()
        monkeypatch.setattr(outbox_module, "session_scope", _scope(session))
        monkeypatch.setattr(outbox_module, "_run_handler", lambda row: None)

        assert await outbox_module.drain_once(10) == 0

    async def test_a_failing_handler_is_logged_but_still_counted(self, monkeypatch) -> None:
        session = DrainSession(
            {
                "outbox_messages": [
                    {
                        "id": str(uuid4()),
                        "event_type": "ReportPublished",
                        "payload": {"event_type": "ReportPublished"},
                        "correlation_id": None,
                        "dispatch_state": "pending",
                    }
                ]
            }
        )

        def boom(row: Any) -> None:
            raise RuntimeError("consumer exploded")

        monkeypatch.setattr(outbox_module, "session_scope", _scope(session))
        monkeypatch.setattr(outbox_module, "_run_handler", boom)

        assert await outbox_module.drain_once(10) == 1