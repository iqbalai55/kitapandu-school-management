"""Unit tests for ``ksma.participant.infrastructure.repositories``.

Seam
----
``ksma.participant.infrastructure.repositories``
(``src/ksma/participant/infrastructure/repositories.py``). Public surface pinned
here:

- ``ParticipantRepository.search`` (:98) — the pagination window sent to
  PostgreSQL: ``LIMIT :limit OFFSET :offset`` with defaults ``50`` / ``0``
  (page 1 starts at offset 0), plus the optional status / free-text filters.
- ``ParticipantRepository.get`` + ``_to_participant`` (:439) — the row →
  ``Participant`` mapper, including the legacy positional fallback used when a
  projection carries no ``tenant_id`` column.
- ``ParticipantRelationRepository.list_for_participant`` + ``_to_relation``
  (:495) — the ``financial_scope`` JSONB column decoded through
  ``FinancialScope.from_json``.
- ``update_financial_scope`` — the scope is written back as a JSONB literal
  (``CAST(:fs AS JSONB)``) via ``_json_dumps``.

No DB, no Docker, no network: ``session.execute(text(...), params)`` is served
by a file-local ``_FakeSession`` that records every ``(sql, params)`` pair.

What is NOT asserted here (lives in the integration tier):
- RLS GUC scoping, real JSONB coercion, index behaviour, Alembic round-trips.
- The duplicate-detection heuristic (see ``test_participant_application.py``).
"""
from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest

from ksma.participant.domain import (
    FinancialScope,
    ParticipantStatus,
    RelationKind,
    RelationStatus,
)
from ksma.participant.infrastructure.repositories import (
    ParticipantRelationRepository,
    ParticipantRepository,
)

TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT_ID = UUID("99999999-9999-9999-9999-999999999999")
PARTICIPANT_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
RELATED_PARTY_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
RELATION_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
INVOICE_A = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
INVOICE_B = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResult:
    """Reader surface for one ``session.execute`` — ``.all()`` and ``.first()``."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Records ``(sql, params)`` per call and replays a queue of row sets."""

    def __init__(self, *row_sets: list[Any]) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._queue: list[list[Any]] = list(row_sets)

    async def execute(
        self, statement: Any, params: dict[str, Any] | None = None
    ) -> _FakeResult:
        self.calls.append((str(statement), params))
        rows = self._queue.pop(0) if self._queue else []
        return _FakeResult(rows)

    # -- assertion helpers -------------------------------------------------

    def params_of(self, index: int) -> dict[str, Any]:
        return dict(self.calls[index][1] or {})

    def sql_of(self, index: int) -> str:
        return self.calls[index][0]


class _Row:
    """A row double exposing the mapped columns as attributes."""

    _COLUMNS = (
        "id",
        "tenant_id",
        "participant_no",
        "full_name",
        "birth_date",
        "contact",
        "identity_reference",
        "status",
        "support_needs_note_enc",
        "created_at",
        "created_by",
        "updated_at",
        "updated_by",
    )

    def __init__(self, **overrides: Any) -> None:
        values: dict[str, Any] = {name: None for name in self._COLUMNS}
        values.update(
            {
                "id": PARTICIPANT_ID,
                "tenant_id": TENANT_ID,
                "participant_no": "P-0001",
                "full_name": "Alice Wibowo",
                "birth_date": date(2014, 5, 17),
                "contact": {"email": "alice@example.test"},
                "identity_reference": {"nik": "3201011505170001"},
                "status": ParticipantStatus.ACTIVE.value,
                "support_needs_note_enc": None,
                "created_at": None,
                "created_by": "seed",
                "updated_at": None,
                "updated_by": "seed",
            }
        )
        for key, value in overrides.items():
            values[key] = value

        for name, value in values.items():
            object.__setattr__(self, name, value)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_Row(id={self.id!r}, tenant_id={self.tenant_id!r})"


class _PositionalRow:
    """Row double for a legacy projection that carries no ``tenant_id`` column.

    Every mapped column is still attribute-addressable, so the mapper only
    falls back to the positional read (``row[0]``) for the tenant id, which
    the driver appends first in the raw tuple. ``hasattr(row, "tenant_id")``
    is therefore ``False``.
    """

    def __init__(self, sequence: list[Any]) -> None:
        self._sequence = sequence
        base = _Row()
        for name in _Row._COLUMNS:
            if name == "tenant_id":
                continue
            object.__setattr__(self, name, getattr(base, name))

    def __getitem__(self, index: int) -> Any:
        return self._sequence[index]

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - guard
        raise AttributeError(name)


# ===========================================================================
# ParticipantRepository.search — :98 (default offset)
# ===========================================================================


class TestSearchPaginationWindow:
    """``search`` — the LIMIT/OFFSET window per docs/specs/002 listing."""

    async def test_defaults_to_the_first_page_window(self) -> None:
        """Kills :98 ``offset=0 -> offset=1``.

        Listing page 1 of participants must start at offset 0; sliding to 1
        would silently skip the first (newest-ordered) row on every default
        request.
        """
        session = _FakeSession([])
        repo = ParticipantRepository(session, TENANT_ID)

        participants, suspects = await repo.search()

        assert participants == []
        assert suspects == []
        params = session.params_of(0)
        assert params["offset"] == 0
        assert params["limit"] == 50
        assert params["tid"] == str(TENANT_ID)
        assert "LIMIT :limit OFFSET :offset" in session.sql_of(0)

    async def test_explicit_window_is_forwarded_verbatim(self) -> None:
        session = _FakeSession([])
        repo = ParticipantRepository(session, TENANT_ID)

        await repo.search(limit=25, offset=100)

        params = session.params_of(0)
        assert params["limit"] == 25
        assert params["offset"] == 100

    async def test_first_page_explicit_offset_zero_matches_the_default(self) -> None:
        """Boundary pair for :98 — the on-boundary value is 0."""
        session = _FakeSession([])
        repo = ParticipantRepository(session, TENANT_ID)

        await repo.search(offset=0)

        assert session.params_of(0)["offset"] == 0

    async def test_status_filter_is_bound_to_the_where_clause(self) -> None:
        session = _FakeSession([])
        repo = ParticipantRepository(session, TENANT_ID)

        await repo.search(status=ParticipantStatus.ARCHIVED)

        sql = session.sql_of(0)
        assert "status = :status" in sql
        assert session.params_of(0)["status"] == ParticipantStatus.ARCHIVED.value

    async def test_text_query_binds_lowercase_contains_and_exact_no(self) -> None:
        session = _FakeSession([])
        repo = ParticipantRepository(session, TENANT_ID)

        await repo.search(query="Ali")

        params = session.params_of(0)
        assert params["q"] == "%ali%"
        assert params["qexact"] == "Ali"
        assert "LOWER(full_name) LIKE :q OR participant_no = :qexact" in session.sql_of(0)

    async def test_suspects_are_skipped_without_a_query(self) -> None:
        session = _FakeSession([])
        repo = ParticipantRepository(session, TENANT_ID)

        _participants, suspects = await repo.search()

        assert suspects == []
        assert len(session.calls) == 1

    async def test_rows_are_mapped_to_typed_participants(self) -> None:
        session = _FakeSession([_Row()])
        repo = ParticipantRepository(session, TENANT_ID)

        participants, _ = await repo.search()

        assert len(participants) == 1
        assert participants[0].id == PARTICIPANT_ID
        assert participants[0].full_name == "Alice Wibowo"
        assert participants[0].birth_date == date(2014, 5, 17)
        assert participants[0].status is ParticipantStatus.ACTIVE


# ===========================================================================
# _to_participant — :439 (positional tenant fallback)
# ===========================================================================


class TestToParticipantTenantMapping:
    """``_to_participant`` — tenant id resolution from a mapped row."""

    async def test_column_row_uses_the_tenant_id_attribute(self) -> None:
        session = _FakeSession([_Row(tenant_id=TENANT_ID)])
        repo = ParticipantRepository(session, TENANT_ID)

        participant = await repo.get(PARTICIPANT_ID)

        assert participant is not None
        assert participant.tenant_id == TENANT_ID

    async def test_legacy_row_without_tenant_column_falls_back_to_position_zero(
        self,
    ) -> None:
        """Kills :439 ``row[0] -> row[1]``.

        Pre-tenancy projections expose the tenant id as the first raw tuple
        slot. Reading slot 1 instead would attach the participant to a
        neighbouring row's tenant — a cross-tenant data leak.
        """
        legacy = _PositionalRow([str(TENANT_ID), str(PARTICIPANT_ID)])
        session = _FakeSession([legacy])
        repo = ParticipantRepository(session, TENANT_ID)

        participant = await repo.get(PARTICIPANT_ID)

        assert participant is not None
        assert not hasattr(legacy, "tenant_id")
        assert participant.tenant_id == TENANT_ID
        assert participant.tenant_id != OTHER_TENANT_ID
        assert participant.id == PARTICIPANT_ID

    async def test_legacy_fallback_still_maps_every_other_column(self) -> None:
        legacy = _PositionalRow([str(OTHER_TENANT_ID), str(PARTICIPANT_ID)])
        session = _FakeSession([legacy])
        repo = ParticipantRepository(session, TENANT_ID)

        participant = await repo.get(PARTICIPANT_ID)

        assert participant is not None
        assert participant.tenant_id == OTHER_TENANT_ID
        assert participant.participant_no == "P-0001"
        assert participant.identity_reference == {"nik": "3201011505170001"}

    async def test_missing_row_maps_to_none(self) -> None:
        session = _FakeSession([])
        repo = ParticipantRepository(session, TENANT_ID)

        assert await repo.get(PARTICIPANT_ID) is None

    async def test_get_is_tenant_scoped(self) -> None:
        session = _FakeSession([_Row()])
        repo = ParticipantRepository(session, TENANT_ID)

        await repo.get(PARTICIPANT_ID)

        params = session.params_of(0)
        assert params["id"] == str(PARTICIPANT_ID)
        assert params["tid"] == str(TENANT_ID)
        assert "WHERE id = :id AND tenant_id = :tid" in session.sql_of(0)


# ===========================================================================
# _to_relation — :495 (financial_scope JSONB decode)
# ===========================================================================


class _RelationRow:
    """Row double for ``participant_relation``."""

    _COLUMNS = (
        "id",
        "tenant_id",
        "participant_id",
        "related_party_id",
        "relation_kind",
        "financial_scope",
        "verification",
        "valid_from",
        "valid_until",
        "status",
        "created_at",
        "created_by",
        "updated_at",
        "updated_by",
    )

    def __init__(self, **overrides: Any) -> None:
        values: dict[str, Any] = {name: None for name in self._COLUMNS}
        values.update(
            {
                "id": RELATION_ID,
                "tenant_id": TENANT_ID,
                "participant_id": PARTICIPANT_ID,
                "related_party_id": RELATED_PARTY_ID,
                "relation_kind": RelationKind.PAYER.value,
                "financial_scope": None,
                "verification": {"document_ref": "doc-1"},
                "status": RelationStatus.ACTIVE.value,
                "created_by": "seed",
                "updated_by": "seed",
            }
        )
        values.update(overrides)
        for name, value in values.items():
            object.__setattr__(self, name, value)


class TestRelationFinancialScopeDecode:
    """``_to_relation`` — the ``financial_scope`` JSONB column (Q33(a))."""

    async def test_blanket_scope_column_is_decoded_as_blanket(self) -> None:
        """Kills :495 ``or -> and``.

        A populated ``financial_scope`` must be forwarded to
        ``FinancialScope.from_json``; collapsing it to ``{}`` would downgrade
        a blanket payer grant to the conservative default.
        """
        session = _FakeSession(
            [_RelationRow(financial_scope={"all_approved": True})]
        )
        repo = ParticipantRelationRepository(session, TENANT_ID)

        relations = await repo.list_for_participant(PARTICIPANT_ID)

        assert len(relations) == 1
        assert relations[0].financial_scope.all_approved is True
        assert relations[0].financial_scope.invoices == ()

    async def test_allow_list_scope_column_is_decoded_into_uuids(self) -> None:
        session = _FakeSession(
            [
                _RelationRow(
                    financial_scope={"invoices": [str(INVOICE_A), str(INVOICE_B)]}
                )
            ]
        )
        repo = ParticipantRelationRepository(session, TENANT_ID)

        relations = await repo.list_for_participant(PARTICIPANT_ID)

        assert relations[0].financial_scope.all_approved is False
        assert relations[0].financial_scope.invoices == (INVOICE_A, INVOICE_B)
        assert relations[0].financial_scope.covers_invoice(INVOICE_A) is True
        assert relations[0].financial_scope.covers_invoice(uuid4()) is False

    @pytest.mark.parametrize(
        "raw_scope",
        [
            pytest.param(None, id="null_jsonb_column"),
            pytest.param({}, id="empty_jsonb_object"),
        ],
    )
    async def test_absent_scope_column_decodes_to_the_conservative_default(
        self, raw_scope: dict[str, object] | None
    ) -> None:
        """Boundary pair for :495 — the ``or {}`` fallback side."""
        session = _FakeSession([_RelationRow(financial_scope=raw_scope)])
        repo = ParticipantRelationRepository(session, TENANT_ID)

        relations = await repo.list_for_participant(PARTICIPANT_ID)

        assert relations[0].financial_scope.all_approved is False
        assert relations[0].financial_scope.invoices == ()

    async def test_relation_enums_and_scope_are_all_typed(self) -> None:
        session = _FakeSession([_RelationRow(financial_scope={"all_approved": True})])
        repo = ParticipantRelationRepository(session, TENANT_ID)

        relations = await repo.list_for_participant(PARTICIPANT_ID)

        rel = relations[0]
        assert rel.id == RELATION_ID
        assert rel.tenant_id == TENANT_ID
        assert rel.participant_id == PARTICIPANT_ID
        assert rel.related_party_id == RELATED_PARTY_ID
        assert rel.relation_kind is RelationKind.PAYER
        assert rel.status is RelationStatus.ACTIVE
        assert rel.verification == {"document_ref": "doc-1"}

    async def test_list_is_tenant_and_participant_scoped(self) -> None:
        session = _FakeSession([])
        repo = ParticipantRelationRepository(session, TENANT_ID)

        await repo.list_for_participant(PARTICIPANT_ID)

        params = session.params_of(0)
        assert params["tid"] == str(TENANT_ID)
        assert params["pid"] == str(PARTICIPANT_ID)
        assert "WHERE tenant_id = :tid AND participant_id = :pid" in session.sql_of(0)


class TestUpdateFinancialScope:
    """``update_financial_scope`` — the scope is written as a JSONB literal."""

    async def test_blanket_scope_is_serialised_to_the_shorthand_form(self) -> None:
        session = _FakeSession()
        repo = ParticipantRelationRepository(session, TENANT_ID)

        await repo.update_financial_scope(
            RELATION_ID, scope=FinancialScope(all_approved=True), actor="admin-1"
        )

        sql = session.sql_of(0)
        params = session.params_of(0)
        assert "CAST(:fs AS JSONB)" in sql
        assert params["fs"] == '{"all_approved": true}'
        assert params["id"] == str(RELATION_ID)
        assert params["tid"] == str(TENANT_ID)
        assert params["u"] == "admin-1"

    async def test_allow_list_scope_is_serialised_with_string_uuids(self) -> None:
        session = _FakeSession()
        repo = ParticipantRelationRepository(session, TENANT_ID)

        await repo.update_financial_scope(
            RELATION_ID,
            scope=FinancialScope(invoices=(INVOICE_A, INVOICE_B)),
            actor="admin-1",
        )

        assert session.params_of(0)["fs"] == (
            '{"invoices": ["%s", "%s"]}' % (INVOICE_A, INVOICE_B)
        )