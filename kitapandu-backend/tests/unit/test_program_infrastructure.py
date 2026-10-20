"""Unit tests for the program SQL repositories (#49).

Seam: ``ksma.program.infrastructure.repositories`` — ``AcademicPeriodRepository``,
``ProgramRepository``, ``ProgramVersionRepository`` and ``RubricRepository``.

The session is the ``FakeAsyncSession`` from ``tests/unit/conftest_helpers.py``,
so a test reads as a domain statement ("a max version_no of 5 yields the next
number 6") while still pinning the SQL parameters that were sent and the JSONB
adapters that come back out of a row.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from ksma.program.domain import (
    AcademicPeriod,
    Curriculum,
    IndicatorValueKind,
    PeriodKind,
    PeriodStatus,
    Program,
    ProgramStatus,
    ProgramVersion,
    ProgramVersionStatus,
    RubricIndicator,
    RubricVersion,
    Scale,
)
from ksma.program.infrastructure.repositories import (
    AcademicPeriodRepository,
    ProgramRepository,
    ProgramVersionRepository,
    RubricRepository,
)
from tests.unit.conftest_helpers import FakeAsyncSession


def _last_execute(session: FakeAsyncSession, fragment: str) -> Any:
    """The most recent ``execute`` whose SQL mentions ``fragment``."""
    calls = [
        call
        for call in session.calls
        if call.op == "execute" and fragment in call.sql
    ]
    assert calls, f"no execute mentioning {fragment!r}; recorded: {[c.sql[:60] for c in session.calls]}"
    return calls[-1]


class TestProgramRepository:
    async def test_create_writes_every_column_and_leaves_version_null(self) -> None:
        session = FakeAsyncSession()
        program = Program(
            tenant_id=uuid4(),
            unit_id=uuid4(),
            program_code="P-001",
            name="Kriya Bengali A1",
            service_group="language",
            status=ProgramStatus.ACTIVE,
            created_by="actor-1",
            updated_by="actor-2",
        )

        await ProgramRepository(session, program.tenant_id).create(program)

        call = session.assert_called(op="execute", params_contains="P-001")
        assert call.params["tid"] == str(program.tenant_id)
        assert call.params["uid"] == str(program.unit_id)
        assert call.params["name"] == "Kriya Bengali A1"
        assert call.params["sg"] == "language"
        assert call.params["status"] == "active"
        assert call.params["cb"] == "actor-1"
        assert call.params["ub"] == "actor-2"
        # A brand-new program has no current version yet — the column is NULL,
        # never a synthesised id.
        assert "NULL," in call.sql

    async def test_get_returns_none_when_the_row_is_absent(self) -> None:
        session = FakeAsyncSession({"program": [{"program_code": "P-001"}]})

        assert await ProgramRepository(session, uuid4()).get(uuid4()) is None

    async def test_list_defaults_to_first_page(self) -> None:
        """``offset`` defaults to 0, not 1 — the first page starts at row zero."""
        session = FakeAsyncSession()

        await ProgramRepository(session, uuid4()).list()

        params = _last_execute(session, "FROM program\n").params
        assert params["limit"] == 50
        assert params["offset"] == 0

    async def test_list_without_filters_has_no_extra_predicates(self) -> None:
        session = FakeAsyncSession()

        await ProgramRepository(session, uuid4()).list()

        call = _last_execute(session, "FROM program\n")
        assert call.sql.count("AND") == 0
        assert "uid" not in call.params
        assert "status" not in call.params

    async def test_list_with_unit_and_status_joins_both_predicates(self) -> None:
        session = FakeAsyncSession()
        tenant_id, unit_id = uuid4(), uuid4()

        await ProgramRepository(session, tenant_id).list(
            unit_id=unit_id, status=ProgramStatus.DRAFT, limit=5, offset=10
        )

        call = _last_execute(session, "FROM program\n")
        assert call.sql.count("AND") == 2
        assert call.params["uid"] == str(unit_id)
        assert call.params["status"] == "draft"
        assert (call.params["limit"], call.params["offset"]) == (5, 10)

    async def test_set_status_and_set_current_version_are_tenant_scoped(self) -> None:
        session = FakeAsyncSession()
        tenant_id, program_id, version_id = uuid4(), uuid4(), uuid4()
        repo = ProgramRepository(session, tenant_id)

        await repo.set_status(program_id, status=ProgramStatus.INACTIVE, actor="ops-1")
        status_call = session.calls[-1]
        await repo.set_current_version(program_id, version_id, "ops-2")
        version_call = session.calls[-1]

        assert "SET status=:s" in status_call.sql
        assert status_call.params["s"] == "inactive"
        assert status_call.params["u"] == "ops-1"
        assert status_call.params["tid"] == str(tenant_id)
        assert "SET current_version_id=:v" in version_call.sql
        assert version_call.params["v"] == str(version_id)
        assert version_call.params["tid"] == str(tenant_id)


class TestProgramVersionRepository:
    async def test_create_serialises_the_curriculum_to_jsonb(self) -> None:
        session = FakeAsyncSession()
        version = ProgramVersion(
            tenant_id=uuid4(),
            unit_id=uuid4(),
            program_id=uuid4(),
            version_no=2,
            learning_objectives="Read a short story",
            curriculum=Curriculum(
                components=[{"code": "L1", "name": "Listening", "hours": 12}],
                duration_unit="sessions",
            ),
            status=ProgramVersionStatus.PUBLISHED,
            created_by="actor-1",
        )

        await ProgramVersionRepository(session, version.tenant_id).create(version)

        call = session.assert_called(op="execute", sql_contains="CAST(:cu AS JSONB)")
        assert json.loads(call.params["cu"]) == {
            "components": [{"code": "L1", "name": "Listening", "hours": 12}],
            "duration_unit": "sessions",
        }
        assert call.params["vn"] == 2
        assert call.params["status"] == "published"

    async def test_curriculum_round_trips_out_of_a_jsonb_column(self) -> None:
        """The adapter must rebuild the stored document, not fall back to ``{}``."""
        session = FakeAsyncSession()
        stored = {
            "components": [{"code": "G1", "name": "Grammar", "hours": 8}],
            "duration_unit": "minutes",
        }
        version_id, tenant_id = uuid4(), uuid4()
        session.seed(
            "program_version",
            {
                "id": str(version_id),
                "tenant_id": str(tenant_id),
                "unit_id": str(uuid4()),
                "program_id": str(uuid4()),
                "version_no": 3,
                "learning_objectives": "Explain a rule",
                "curriculum": stored,
                "created_at": None,
                "updated_at": None,
                "effective_from": None,
                "effective_until": None,
                "status": "published",
                "created_by": "actor-1",
                "updated_by": "actor-1",
            },
        )

        found = await ProgramVersionRepository(session, tenant_id).get(version_id)

        assert found is not None
        assert found.version_no == 3
        assert found.curriculum.to_json() == stored

    async def test_curriculum_of_an_empty_document_stays_a_blank_curriculum(self) -> None:
        session = FakeAsyncSession()
        version_id, tenant_id = uuid4(), uuid4()
        session.seed(
            "program_version",
            {
                "id": str(version_id),
                "tenant_id": str(tenant_id),
                "unit_id": str(uuid4()),
                "program_id": str(uuid4()),
                "version_no": 1,
                "learning_objectives": "",
                "curriculum": None,
                "created_at": None,
                "updated_at": None,
                "effective_from": None,
                "effective_until": None,
                "status": "draft",
                "created_by": "",
                "updated_by": "",
            },
        )

        found = await ProgramVersionRepository(session, tenant_id).get(version_id)

        assert found is not None
        assert found.curriculum == Curriculum()

    async def test_list_for_program_is_tenant_and_program_scoped(self) -> None:
        session = FakeAsyncSession()
        tenant_id, program_id = uuid4(), uuid4()

        await ProgramVersionRepository(session, tenant_id).list_for_program(program_id)

        call = _last_execute(session, "FROM program_version")
        assert call.params["tid"] == str(tenant_id)
        assert call.params["pid"] == str(program_id)
        assert "ORDER BY version_no DESC" in call.sql

    async def test_next_version_no_steps_past_the_current_maximum(self) -> None:
        tenant_id, program_id = uuid4(), uuid4()
        session = FakeAsyncSession(
            {
                "program_version": [
                    {"tenant_id": str(tenant_id), "program_id": str(program_id), "n": 5}
                ]
            }
        )

        assert await ProgramVersionRepository(session, tenant_id).next_version_no(program_id) == 6

    async def test_next_version_no_of_an_empty_history_starts_at_one(self) -> None:
        tenant_id, program_id = uuid4(), uuid4()
        for value in (0, None):
            session = FakeAsyncSession(
                {
                    "program_version": [
                        {"tenant_id": str(tenant_id), "program_id": str(program_id), "n": value}
                    ]
                }
            )
            repo = ProgramVersionRepository(session, tenant_id)
            assert await repo.next_version_no(program_id) == 1


class TestAcademicPeriodRepository:
    async def test_create_writes_the_period_kind_and_status(self) -> None:
        session = FakeAsyncSession()
        period = AcademicPeriod(
            tenant_id=uuid4(),
            unit_id=uuid4(),
            period_code="2025-S1",
            name="Semester 1 2025",
            period_kind=PeriodKind.SEMESTER,
            status=PeriodStatus.ACTIVE,
            created_by="registrar",
        )

        await AcademicPeriodRepository(session, period.tenant_id).create(period)

        call = session.assert_called(op="execute", params_contains="2025-S1")
        assert call.params["kind"] == "semester"
        assert call.params["status"] == "active"

    async def test_list_returns_only_the_requested_unit_and_status(self) -> None:
        session = FakeAsyncSession()
        tenant_id, unit_id, other_unit = uuid4(), uuid4(), uuid4()
        shared = {
            "tenant_id": str(tenant_id),
            "period_kind": "semester",
            "name": "Semester",
            "start_date": None,
            "end_date": None,
            "created_at": None,
            "updated_at": None,
            "created_by": "registrar",
            "updated_by": "registrar",
        }
        session.seed(
            "academic_period",
            [
                {
                    "id": str(uuid4()),
                    "unit_id": str(unit_id),
                    "period_code": "mine",
                    "status": "active",
                    **shared,
                },
                {
                    "id": str(uuid4()),
                    "unit_id": str(other_unit),
                    "period_code": "theirs",
                    "status": "active",
                    **shared,
                },
                {
                    "id": str(uuid4()),
                    "unit_id": str(unit_id),
                    "period_code": "closed",
                    "status": "closed",
                    **shared,
                },
            ],
        )

        found = await AcademicPeriodRepository(session, tenant_id).list(
            unit_id=unit_id, status=PeriodStatus.ACTIVE
        )

        assert [p.period_code for p in found] == ["mine"]
        call = _last_execute(session, "FROM academic_period")
        assert call.sql.count("AND") == 2
        assert call.params["uid"] == str(unit_id)

    async def test_list_unfiltered_keeps_only_the_tenant_predicate(self) -> None:
        session = FakeAsyncSession()

        await AcademicPeriodRepository(session, uuid4()).list()

        call = _last_execute(session, "FROM academic_period")
        assert call.sql.count("AND") == 0
        assert set(call.params) == {"tid"}

    async def test_set_status_updates_one_column(self) -> None:
        session = FakeAsyncSession()
        tenant_id, period_id = uuid4(), uuid4()

        await AcademicPeriodRepository(session, tenant_id).set_status(
            period_id, status=PeriodStatus.CLOSED, actor="registrar"
        )

        call = session.calls[-1]
        assert call.params["s"] == "closed"
        assert call.params["id"] == str(period_id)
        assert call.params["tid"] == str(tenant_id)


class TestRubricRepository:
    async def test_create_version_serialises_approval_and_leaves_publication_null(self) -> None:
        session = FakeAsyncSession()
        version = RubricVersion(
            tenant_id=uuid4(),
            unit_id=uuid4(),
            program_version_id=uuid4(),
            version_no=1,
            name="Rubric v1",
            approval={"approved_by": "head-of-school"},
            created_by="actor-1",
        )

        await RubricRepository(session, version.tenant_id).create_version(version)

        call = session.assert_called(op="execute", sql_contains="CAST(:approval AS JSONB)")
        assert json.loads(call.params["approval"]) == {"approved_by": "head-of-school"}
        assert "NULL," in call.sql

    async def test_create_version_without_approval_stores_null(self) -> None:
        session = FakeAsyncSession()
        version = RubricVersion(tenant_id=uuid4(), approval=None)

        await RubricRepository(session, version.tenant_id).create_version(version)

        assert session.assert_called(op="execute").params["approval"] is None

    async def test_publish_version_stamps_status_and_actor(self) -> None:
        session = FakeAsyncSession()
        tenant_id, rubric_id = uuid4(), uuid4()

        await RubricRepository(session, tenant_id).publish_version(rubric_id, "approver-1")

        call = session.calls[-1]
        assert "status='published'" in call.sql
        assert "published_at=now()" in call.sql
        assert call.params["u"] == "approver-1"
        assert call.params["tid"] == str(tenant_id)

    async def test_next_version_no_steps_past_the_current_maximum(self) -> None:
        tenant_id, program_version_id = uuid4(), uuid4()
        session = FakeAsyncSession(
            {
                "rubric_version": [
                    {"tenant_id": str(tenant_id), "program_version_id": str(program_version_id), "n": 3}
                ]
            }
        )

        assert await RubricRepository(session, tenant_id).next_version_no(program_version_id) == 4

    async def test_next_version_no_of_an_empty_history_starts_at_one(self) -> None:
        tenant_id, program_version_id = uuid4(), uuid4()
        session = FakeAsyncSession(
            {
                "rubric_version": [
                    {
                        "tenant_id": str(tenant_id),
                        "program_version_id": str(program_version_id),
                        "n": None,
                    }
                ]
            }
        )

        assert await RubricRepository(session, tenant_id).next_version_no(program_version_id) == 1

    async def test_add_indicator_serialises_the_scale(self) -> None:
        session = FakeAsyncSession()
        indicator = RubricIndicator(
            tenant_id=uuid4(),
            rubric_version_id=uuid4(),
            indicator_code="I-1",
            label="Pronunciation",
            value_kind=IndicatorValueKind.CATEGORICAL,
            scale=Scale(categories=["A", "B", "C"]),
            weight=0.25,
            is_required=True,
            created_by="assessor-1",
        )

        await RubricRepository(session, indicator.tenant_id).add_indicator(indicator)

        call = session.assert_called(op="execute", sql_contains="CAST(:sc AS JSONB)")
        assert json.loads(call.params["sc"]) == {"categories": ["A", "B", "C"]}
        assert call.params["vk"] == "categorical"
        assert call.params["req"] is True

    async def test_scale_round_trips_out_of_a_jsonb_column(self) -> None:
        """The scale adapter must rebuild the stored document, not ``{}``."""
        session = FakeAsyncSession()
        stored = {"min": 0.0, "max": 100.0}
        tenant_id, rubric_id = uuid4(), uuid4()
        session.seed(
            "rubric_indicator",
            {
                "id": str(uuid4()),
                "tenant_id": str(tenant_id),
                "rubric_version_id": str(rubric_id),
                "indicator_code": "I-2",
                "label": "Fluency",
                "value_kind": "numeric",
                "scale": stored,
                "weight": 0.5,
                "is_required": True,
                "created_at": None,
                "updated_at": None,
                "created_by": "assessor-1",
                "updated_by": "assessor-1",
            },
        )

        found = await RubricRepository(session, tenant_id).list_indicators(rubric_id)

        assert [i.scale.to_json() for i in found] == [stored]
        assert found[0].weight == 0.5
        assert found[0].value_kind is IndicatorValueKind.NUMERIC

    async def test_list_versions_and_indicators_are_parent_scoped(self) -> None:
        session = FakeAsyncSession()
        tenant_id, program_version_id = uuid4(), uuid4()
        repo = RubricRepository(session, tenant_id)

        await repo.list_versions(program_version_id)
        version_call = _last_execute(session, "FROM rubric_version")
        assert version_call.params["pvid"] == str(program_version_id)
        assert version_call.params["tid"] == str(tenant_id)

        await repo.list_indicators(program_version_id)
        indicator_call = _last_execute(session, "FROM rubric_indicator")
        assert indicator_call.params["rvid"] == str(program_version_id)
        assert indicator_call.params["tid"] == str(tenant_id)