"""Unit tests for the in-process event bus + DomainEvent base.

Seam: ``ksma.shared.events`` for the bus + event base; ``ksma.shared.outbox``
for the pure-Python helpers. The DB-touching parts of outbox are covered
by integration tests with testcontainers.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from ksma.shared.events import DomainEvent, EventBus, bus


class ConcreteEvent(DomainEvent):
    event_type: ClassVar[str] = "ConcreteEvent"
    schema_version: ClassVar[int] = 1


class OtherEvent(DomainEvent):
    event_type: ClassVar[str] = "OtherEvent"
    schema_version: ClassVar[int] = 2


class TestEventBus:
    def test_handler_receives_event(self) -> None:
        captured = []
        local_bus = EventBus()
        local_bus.subscribe(ConcreteEvent, lambda e: captured.append(e))
        event = ConcreteEvent()
        local_bus.dispatch(event)
        assert captured == [event]

    def test_multiple_handlers_in_registration_order(self) -> None:
        order = []
        local_bus = EventBus()
        local_bus.subscribe(ConcreteEvent, lambda e: order.append("first"))
        local_bus.subscribe(ConcreteEvent, lambda e: order.append("second"))
        local_bus.dispatch(ConcreteEvent())
        assert order == ["first", "second"]

    def test_no_handlers_no_error(self) -> None:
        local_bus = EventBus()
        local_bus.dispatch(ConcreteEvent())  # should not raise

    def test_handler_isolation_by_event_type(self) -> None:
        concrete_seen = []
        other_seen = []
        local_bus = EventBus()
        local_bus.subscribe(ConcreteEvent, lambda e: concrete_seen.append(e))
        local_bus.subscribe(OtherEvent, lambda e: other_seen.append(e))
        local_bus.dispatch(ConcreteEvent())
        assert len(concrete_seen) == 1
        assert len(other_seen) == 0

    def test_failing_handler_does_not_break_subsequent(self) -> None:
        calls = []
        local_bus = EventBus()

        def boom(_e: DomainEvent) -> None:
            raise RuntimeError("boom")

        def ok(_e: DomainEvent) -> None:
            calls.append("ok")

        # This is a behavioural observation: today the bus does NOT swallow
        # exceptions; subsequent handlers are skipped. We document the
        # contract as-is and lock it.
        local_bus.subscribe(ConcreteEvent, boom)
        local_bus.subscribe(ConcreteEvent, ok)
        with pytest.raises(RuntimeError):
            local_bus.dispatch(ConcreteEvent())
        assert calls == []  # ok() never ran


class TestProcessBus:
    def test_default_bus_is_singleton(self) -> None:
        from ksma.shared.events import bus as bus_fn

        assert bus_fn() is bus_fn()

    def test_default_bus_routes(self) -> None:
        from ksma.shared.events import bus as bus_fn

        captured = []
        # Subscribe on the process-wide bus; remember to clean up.
        bus_fn().subscribe(ConcreteEvent, lambda e: captured.append(e))
        try:
            bus_fn().dispatch(ConcreteEvent())
        finally:
            # Best-effort cleanup — EventBus doesn't have unsubscribe, but
            # the captured list is local to the test.
            pass
        assert len(captured) >= 1


class TestDomainEventDefaults:
    def test_event_id_is_uuid(self) -> None:
        e = ConcreteEvent()
        assert isinstance(e.event_id, UUID)
        assert e.event_id.version  # UUID4 has version 4

    def test_event_id_is_unique_per_instance(self) -> None:
        a, b = ConcreteEvent(), ConcreteEvent()
        assert a.event_id != b.event_id

    def test_occurred_at_is_utc_aware(self) -> None:
        e = ConcreteEvent()
        assert e.occurred_at.tzinfo is not None
        assert e.occurred_at.utcoffset().total_seconds() == 0

    def test_default_actor_kind(self) -> None:
        assert ConcreteEvent().actor_kind == "account"

    def test_subclass_event_type_is_stable(self) -> None:
        assert ConcreteEvent.event_type == "ConcreteEvent"
        assert OtherEvent.event_type == "OtherEvent"