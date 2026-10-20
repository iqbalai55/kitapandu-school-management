"""Meta-test for the shared test toolkit in ``tests/unit/conftest_helpers.py``.

Seam: ``tests.unit.conftest_helpers`` — the public surface every later
mutation-killing slice imports. What is asserted: each helper's observable
contract (recorded calls, dict-backed state, Pydantic round-trip, generated
parametrize ids). What is NOT asserted: any domain
rule of ``ksma.*`` — this file deliberately contains no production logic, and
it never opens a DB socket, a Docker daemon, or a network connection.
"""
from __future__ import annotations


class TestFakeRowAndResult:
    def test_row_reads_by_key_and_attribute(self) -> None:
        from tests.unit.conftest_helpers import FakeAsyncSession  # noqa: PLC0415

        session = FakeAsyncSession({"participant": [{"id": "p1", "full_name": "Sari"}]})

        row = session.rows("participant")[0]

        assert row["full_name"] == "Sari"
        assert row.full_name == "Sari"
        assert row.id == "p1"
        assert dict(row) == {"id": "p1", "full_name": "Sari"}

    def test_result_exposes_the_sqlalchemy_reader_surface(self) -> None:
        from tests.unit.conftest_helpers import FakeAsyncSession, FakeResult  # noqa: PLC0415

        rows = FakeAsyncSession({"participant": [{"id": "p1"}, {"id": "p2"}]}).rows("participant")

        result = FakeResult(rows)
        assert result.first() is rows[0]
        assert result.all() == rows
        assert list(result) == rows

    def test_result_of_empty_select_has_no_first(self) -> None:
        from tests.unit.conftest_helpers import FakeResult  # noqa: PLC0415

        result = FakeResult([])

        assert result.first() is None
        assert result.all() == []
        assert result.one_or_none() is None

    def test_seeding_after_construction_appends(self) -> None:
        from tests.unit.conftest_helpers import FakeAsyncSession  # noqa: PLC0415

        session = FakeAsyncSession()
        session.seed("participant", {"id": "p1"})
        session.seed("participant", [{"id": "p2"}])

        assert [r.id for r in session.rows("participant")] == ["p1", "p2"]


class TestFakeAsyncSessionExecute:
    async def test_select_returns_the_matching_seeded_row(self) -> None:
        from sqlalchemy import text  # noqa: PLC0415

        from tests.unit.conftest_helpers import FakeAsyncSession  # noqa: PLC0415

        session = FakeAsyncSession(
            {"participant": [{"id": "p1", "status": "active"}, {"id": "p2", "status": "archived"}]}
        )

        result = await session.execute(
            text("SELECT id, status FROM participant WHERE id = :id AND status = :status"),
            {"id": "p2", "status": "archived"},
        )

        assert result.first() is not None
        assert result.first().id == "p2"

    async def test_select_that_matches_nothing_is_empty(self) -> None:
        from sqlalchemy import text  # noqa: PLC0415

        from tests.unit.conftest_helpers import FakeAsyncSession  # noqa: PLC0415

        session = FakeAsyncSession({"participant": [{"id": "p1"}]})

        result = await session.execute(
            text("SELECT id FROM participant WHERE id = :id"), {"id": "nope"}
        )

        assert result.all() == []

    async def test_select_without_equality_predicates_returns_every_row(self) -> None:
        from tests.unit.conftest_helpers import FakeAsyncSession  # noqa: PLC0415

        session = FakeAsyncSession({"participant": [{"id": "p1"}, {"id": "p2"}]})

        result = await session.execute("SELECT id FROM participant ORDER BY id")

        assert [r.id for r in result] == ["p1", "p2"]

    async def test_insert_appends_the_row_and_reports_one(self) -> None:
        from sqlalchemy import text  # noqa: PLC0415

        from tests.unit.conftest_helpers import FakeAsyncSession  # noqa: PLC0415

        session = FakeAsyncSession()

        result = await session.execute(
            text(
                "INSERT INTO participant (id, status) VALUES (:id, :status)"
            ),
            {"id": "p9", "status": "active"},
        )

        assert result.rowcount == 1
        assert [r.id for r in session.rows("participant")] == ["p9"]

    async def test_update_changes_the_stored_row(self) -> None:
        from sqlalchemy import text  # noqa: PLC0415

        from tests.unit.conftest_helpers import FakeAsyncSession  # noqa: PLC0415

        session = FakeAsyncSession({"participant": [{"id": "p1", "status": "active"}]})

        result = await session.execute(
            text("UPDATE participant SET status = :status WHERE id = :id"),
            {"status": "archived", "id": "p1"},
        )

        assert result.rowcount == 1
        assert session.rows("participant")[0].status == "archived"

    async def test_delete_removes_the_stored_row(self) -> None:
        from sqlalchemy import text  # noqa: PLC0415

        from tests.unit.conftest_helpers import FakeAsyncSession  # noqa: PLC0415

        session = FakeAsyncSession({"participant": [{"id": "p1"}, {"id": "p2"}]})

        result = await session.execute(
            text("DELETE FROM participant WHERE id = :id"), {"id": "p1"}
        )

        assert result.rowcount == 1
        assert [r.id for r in session.rows("participant")] == ["p2"]

    async def test_rollback_drops_rows_written_since_the_last_commit(self) -> None:
        from sqlalchemy import text  # noqa: PLC0415

        from tests.unit.conftest_helpers import FakeAsyncSession  # noqa: PLC0415

        session = FakeAsyncSession({"participant": [{"id": "p1"}]})
        await session.execute(
            text("INSERT INTO participant (id) VALUES (:id)"), {"id": "p2"}
        )

        await session.rollback()

        assert [r.id for r in session.rows("participant")] == ["p1"]


class TestFakeAsyncSessionCallLog:
    async def test_every_call_is_recorded_in_order(self) -> None:
        from sqlalchemy import text  # noqa: PLC0415

        from tests.unit.conftest_helpers import FakeAsyncSession  # noqa: PLC0415

        session = FakeAsyncSession({"participant": [{"id": "p1"}]})
        await session.execute(text("SELECT id FROM participant WHERE id = :id"), {"id": "p1"})
        await session.execute(
            text("UPDATE participant SET status = :s WHERE id = :id"), {"s": "archived", "id": "p1"}
        )
        await session.commit()

        assert [call.op for call in session.calls] == ["execute", "execute", "commit"]

    async def test_recorded_call_keeps_the_statement_and_parameters(self) -> None:
        from sqlalchemy import text  # noqa: PLC0415

        from tests.unit.conftest_helpers import FakeAsyncSession  # noqa: PLC0415

        session = FakeAsyncSession()
        await session.execute(text("DELETE FROM audit WHERE id = :id"), {"id": "a1"})

        call = session.calls[0]
        assert call.op == "execute"
        assert "DELETE FROM audit" in call.sql
        assert call.params == {"id": "a1"}

    async def test_orm_style_add_and_delete_are_recorded(self) -> None:
        from tests.unit.conftest_helpers import FakeAsyncSession  # noqa: PLC0415

        session = FakeAsyncSession()
        row = {"id": "p1"}

        await session.add(row)
        await session.flush()
        await session.delete(row)
        await session.refresh(row)

        assert [call.op for call in session.calls] == ["add", "flush", "delete", "refresh"]

    async def test_assert_called_finds_the_matching_call(self) -> None:
        from sqlalchemy import text  # noqa: PLC0415

        from tests.unit.conftest_helpers import FakeAsyncSession  # noqa: PLC0415

        session = FakeAsyncSession({"participant": [{"id": "p1"}]})
        await session.execute(
            text("UPDATE participant SET status = :s WHERE id = :id"), {"s": "x", "id": "p1"}
        )

        session.assert_called(op="execute", sql_contains="UPDATE participant", params_contains="p1")

    async def test_assert_called_reports_a_missing_call(self) -> None:
        from tests.unit.conftest_helpers import FakeAsyncSession  # noqa: PLC0415

        session = FakeAsyncSession()

        try:
            session.assert_called(op="commit")
        except AssertionError as exc:
            assert "commit" in str(exc)
        else:  # pragma: no cover - the call above must raise
            raise AssertionError("assert_called must raise when no call matches")


class TestPydanticRoundTrip:
    def test_a_model_survives_the_write_read_cycle(self) -> None:
        from pydantic import BaseModel  # noqa: I001, PLC0415
        from uuid import UUID, uuid4  # noqa: PLC0415

        from tests.unit.conftest_helpers import FakeAsyncSession  # noqa: PLC0415

        class AssessmentRow(BaseModel):
            id: UUID
            version_no: int
            status: str

        session = FakeAsyncSession()
        model = AssessmentRow(id=uuid4(), version_no=3, status="active")

        stored = session.insert_model("rubric_version", model)

        assert stored == model
        assert session.get_model(AssessmentRow, "rubric_version", model.id) == model

    def test_round_trip_of_a_production_model_keeps_every_field(self) -> None:
        from ksma.participant.interface import ParticipantOut  # noqa: I001, PLC0415
        from uuid import uuid4  # noqa: PLC0415

        from tests.unit.conftest_helpers import FakeAsyncSession  # noqa: PLC0415

        session = FakeAsyncSession()
        participant = ParticipantOut(
            id=uuid4(),
            participant_no="P-A-0001",
            full_name="Sari Tenant A",
            status="active",
            contact={"email": "sari@example.test"},
        )

        session.insert_model("participant", participant)
        loaded = session.get_model(ParticipantOut, "participant", participant.id)

        assert loaded == participant
        assert loaded.full_name == "Sari Tenant A"
        assert loaded.contact == {"email": "sari@example.test"}

    def test_the_stored_row_is_plain_json_types(self) -> None:
        from ksma.participant.interface import ParticipantOut  # noqa: I001, PLC0415
        from uuid import uuid4  # noqa: PLC0415

        from tests.unit.conftest_helpers import FakeAsyncSession  # noqa: PLC0415

        session = FakeAsyncSession()
        participant = ParticipantOut(
            id=uuid4(), participant_no="P-A-0002", full_name="Budi", status="active"
        )

        session.insert_model("participant", participant)

        row = session.rows("participant")[0]
        assert isinstance(row["id"], str)
        assert isinstance(row["full_name"], str)

    def test_seeding_a_model_does_not_mark_the_row_as_added(self) -> None:
        from ksma.participant.interface import ParticipantOut  # noqa: I001, PLC0415
        from uuid import uuid4  # noqa: PLC0415

        from tests.unit.conftest_helpers import FakeAsyncSession  # noqa: PLC0415

        session = FakeAsyncSession()
        participant = ParticipantOut(
            id=uuid4(), participant_no="P-A-0003", full_name="Citra", status="archived"
        )

        session.seed_model("participant", participant)

        assert session.get_model(ParticipantOut, "participant", participant.id) == participant
        assert len(session.rows("participant")) == 1

    def test_get_model_of_an_unknown_row_is_none(self) -> None:
        from ksma.participant.interface import ParticipantOut  # noqa: I001, PLC0415
        from uuid import uuid4  # noqa: PLC0415

        from tests.unit.conftest_helpers import FakeAsyncSession  # noqa: PLC0415

        session = FakeAsyncSession()

        assert session.get_model(ParticipantOut, "participant", uuid4()) is None


class TestFakeRepository:
    def test_create_stores_the_item_and_records_the_add(self) -> None:
        from tests.unit.conftest_helpers import FakeRepository  # noqa: PLC0415

        repo = FakeRepository(table="participant")
        item = {"id": "p1", "status": "active"}

        created = repo.create(item)

        assert created is item
        assert repo.rows == {"p1": {"id": "p1", "status": "active"}}
        assert repo.added == [item]

    def test_create_accepts_a_pydantic_model(self) -> None:
        from ksma.participant.interface import ParticipantOut  # noqa: I001, PLC0415
        from uuid import uuid4  # noqa: PLC0415

        from tests.unit.conftest_helpers import FakeRepository  # noqa: PLC0415

        repo = FakeRepository(table="participant")
        participant = ParticipantOut(
            id=uuid4(), participant_no="P-A-0001", full_name="Sari", status="active"
        )

        repo.create(participant)

        stored = repo.rows[str(participant.id)]
        assert stored["full_name"] == "Sari"

    def test_seeded_rows_start_the_table(self) -> None:
        from tests.unit.conftest_helpers import FakeRepository  # noqa: PLC0415

        repo = FakeRepository([{"id": "p1"}, {"id": "p2"}], table="participant")

        assert sorted(repo.rows) == ["p1", "p2"]
        assert repo.added == []

    def test_get_records_the_load_and_returns_the_row(self) -> None:
        from tests.unit.conftest_helpers import FakeRepository  # noqa: PLC0415

        repo = FakeRepository([{"id": "p1", "status": "active"}], table="participant")

        found = repo.get("p1")

        assert found == {"id": "p1", "status": "active"}
        assert repo.loaded == ["p1"]

    def test_get_of_an_unknown_key_is_none(self) -> None:
        from tests.unit.conftest_helpers import FakeRepository  # noqa: PLC0415

        repo = FakeRepository(table="participant")

        assert repo.get("nope") is None

    def test_save_inserts_then_upserts(self) -> None:
        from tests.unit.conftest_helpers import FakeRepository  # noqa: PLC0415

        repo = FakeRepository(table="participant")
        repo.save({"id": "p1", "status": "active"})
        repo.save({"id": "p1", "status": "archived"})

        assert repo.rows["p1"]["status"] == "archived"
        assert len(repo.added) == 1
        assert len(repo.updated) == 1

    def test_update_changes_the_fields_in_place(self) -> None:
        from tests.unit.conftest_helpers import FakeRepository  # noqa: PLC0415

        repo = FakeRepository([{"id": "p1", "status": "active"}], table="participant")

        repo.update("p1", status="archived")

        assert repo.rows["p1"] == {"id": "p1", "status": "archived"}

    def test_delete_removes_the_row_and_records_it(self) -> None:
        from tests.unit.conftest_helpers import FakeRepository  # noqa: PLC0415

        repo = FakeRepository([{"id": "p1"}, {"id": "p2"}], table="participant")

        repo.delete("p1")

        assert list(repo.rows) == ["p2"]
        assert repo.deleted == ["p1"]

    def test_a_subclass_binds_its_own_table(self) -> None:
        from tests.unit.conftest_helpers import FakeRepository  # noqa: PLC0415

        class InvoiceRepository(FakeRepository):
            table = "invoice"

        repo = InvoiceRepository([{"id": "i1", "total": 1000}])

        assert repo.table == "invoice"
        assert repo.rows["i1"]["total"] == 1000


class TestRepoAssertions:
    def test_assert_repo_added_accepts_the_recorded_add(self) -> None:
        from tests.unit.conftest_helpers import FakeRepository, assert_repo_added  # noqa: PLC0415

        repo = FakeRepository(table="invoice")
        repo.create({"id": "i1", "total": 1000})

        assert_repo_added(repo, count=1, total=1000)

    def test_assert_repo_added_rejects_a_wrong_field(self) -> None:
        from tests.unit.conftest_helpers import FakeRepository, assert_repo_added  # noqa: PLC0415

        repo = FakeRepository(table="invoice")
        repo.create({"id": "i1", "status": "issued"})

        try:
            assert_repo_added(repo, status="draft")
        except AssertionError as exc:
            assert "draft" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("assert_repo_added must reject a non-matching add")

    def test_assert_repo_added_rejects_the_wrong_count(self) -> None:
        from tests.unit.conftest_helpers import FakeRepository, assert_repo_added  # noqa: PLC0415

        repo = FakeRepository(table="invoice")

        try:
            assert_repo_added(repo, count=2)
        except AssertionError as exc:
            assert "2" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("assert_repo_added must reject the wrong count")

    def test_assert_repo_state_pins_count_keys_and_columns(self) -> None:
        from tests.unit.conftest_helpers import FakeRepository, assert_repo_state  # noqa: PLC0415

        repo = FakeRepository(
            [{"id": "i1", "status": "issued"}, {"id": "i2", "status": "issued"}],
            table="invoice",
        )

        assert_repo_state(repo, count=2, keys=["i1", "i2"], status="issued")

    def test_assert_repo_state_rejects_a_missing_row(self) -> None:
        from tests.unit.conftest_helpers import FakeRepository, assert_repo_state  # noqa: PLC0415

        repo = FakeRepository([{"id": "i1"}], table="invoice")

        try:
            assert_repo_state(repo, count=2)
        except AssertionError as exc:
            assert "1" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("assert_repo_state must reject the wrong row count")

    def test_assert_repo_deleted_requires_the_removal(self) -> None:
        from tests.unit.conftest_helpers import FakeRepository, assert_repo_deleted  # noqa: PLC0415

        repo = FakeRepository([{"id": "i1"}, {"id": "i2"}], table="invoice")
        repo.delete("i1")

        assert_repo_deleted(repo, "i1")


class TestFakeTenantContext:
    def test_the_builder_covers_the_thirteen_role_enum(self) -> None:
        from ksma.shared.rbac import RoleCode  # noqa: I001, PLC0415

        from tests.unit.conftest_helpers import ALL_ROLES  # noqa: PLC0415

        assert len(ALL_ROLES) == 13
        assert ALL_ROLES == tuple(RoleCode)  # noqa: SIM300

    def test_the_thirteen_role_codes_match_the_access_matrix(self) -> None:
        from tests.unit.conftest_helpers import ALL_ROLES  # noqa: PLC0415

        assert [role.value for role in ALL_ROLES] == [
            "OW", "AM", "AC", "FI", "IN", "AS", "AP", "AU", "ST", "GU", "PY", "OP", "SV",
        ]

    def test_every_role_builds_a_context_that_carries_it(self) -> None:
        from tests.unit.conftest_helpers import ALL_ROLES, tenant_context  # noqa: PLC0415

        for role in ALL_ROLES:
            ctx = tenant_context(role)
            assert ctx.has_role(role.value) is True, role

    def test_every_role_builds_a_context_with_a_tenant_and_a_revision(self) -> None:
        from uuid import UUID  # noqa: PLC0415

        from tests.unit.conftest_helpers import ALL_ROLES, tenant_context  # noqa: PLC0415

        for role in ALL_ROLES:
            ctx = tenant_context(role)
            assert isinstance(ctx.tenant_id, UUID), role
            assert ctx.authz_revision == 1, role

    def test_the_builder_is_reusable_and_keeps_its_tenant(self) -> None:
        from tests.unit.conftest_helpers import FakeTenantContext  # noqa: PLC0415

        builder = FakeTenantContext()
        first = builder.build()

        assert builder.build().tenant_id == first.tenant_id

    def test_an_explicit_tenant_is_carried_by_every_build(self) -> None:
        from uuid import uuid4  # noqa: PLC0415

        from tests.unit.conftest_helpers import FakeTenantContext  # noqa: PLC0415

        tenant = uuid4()
        builder = FakeTenantContext().with_tenant(tenant)

        assert builder.build().tenant_id == tenant

    def test_the_authz_revision_toggles(self) -> None:
        from tests.unit.conftest_helpers import FakeTenantContext  # noqa: PLC0415

        ctx = FakeTenantContext().with_authz_revision(7).build()

        assert ctx.authz_revision == 7

    def test_service_identities_carry_no_account(self) -> None:
        from ksma.shared.tenancy import ActorKind  # noqa: I001, PLC0415

        from tests.unit.conftest_helpers import FakeTenantContext  # noqa: PLC0415

        ctx = FakeTenantContext().as_service().build()

        assert ctx.actor_kind is ActorKind.SERVICE_PRINCIPAL
        assert ctx.is_service() is True
        assert ctx.account_id is None
        assert ctx.membership_id is None

    def test_support_operators_carry_no_account(self) -> None:
        from ksma.shared.tenancy import ActorKind  # noqa: I001, PLC0415

        from tests.unit.conftest_helpers import FakeTenantContext  # noqa: PLC0415

        ctx = FakeTenantContext().as_support().build()

        assert ctx.actor_kind is ActorKind.SUPPORT_OPERATOR
        assert ctx.is_support() is True
        assert ctx.account_id is None

    def test_the_service_role_defaults_to_a_service_principal(self) -> None:
        from ksma.shared.rbac import RoleCode  # noqa: I001, PLC0415
        from ksma.shared.tenancy import ActorKind  # noqa: PLC0415

        from tests.unit.conftest_helpers import tenant_context  # noqa: PLC0415

        assert tenant_context(RoleCode.SV).actor_kind is ActorKind.SERVICE_PRINCIPAL

    def test_the_platform_operator_role_defaults_to_a_support_operator(self) -> None:
        from ksma.shared.rbac import RoleCode  # noqa: I001, PLC0415
        from ksma.shared.tenancy import ActorKind  # noqa: PLC0415

        from tests.unit.conftest_helpers import tenant_context  # noqa: PLC0415

        assert tenant_context(RoleCode.OP).actor_kind is ActorKind.SUPPORT_OPERATOR

    def test_an_account_role_defaults_to_an_account(self) -> None:
        from ksma.shared.rbac import RoleCode  # noqa: I001, PLC0415
        from ksma.shared.tenancy import ActorKind  # noqa: PLC0415

        from tests.unit.conftest_helpers import tenant_context  # noqa: PLC0415

        ctx = tenant_context(RoleCode.FI)
        assert ctx.actor_kind is ActorKind.ACCOUNT
        assert ctx.account_id is not None
        assert ctx.membership_id is not None

    def test_unit_scope_can_be_set_and_cleared(self) -> None:
        from uuid import uuid4  # noqa: PLC0415

        from tests.unit.conftest_helpers import FakeTenantContext  # noqa: PLC0415

        builder = FakeTenantContext().with_unit(uuid4())

        assert builder.build().unit_id is not None
        assert builder.without_unit().build().unit_id is None

    def test_extra_roles_accumulate_after_the_primary_role(self) -> None:
        from ksma.shared.rbac import RoleCode  # noqa: I001, PLC0415

        from tests.unit.conftest_helpers import FakeTenantContext  # noqa: PLC0415

        ctx = FakeTenantContext(RoleCode.AC).with_extra_roles(RoleCode.FI).build()

        assert ctx.role_codes == ("AC", "FI")
        assert ctx.has_role("FI") is True
        assert ctx.has_role("AU") is False


class TestParametrizeBoundary:
    def test_the_decorator_attaches_the_values_and_the_ids(self) -> None:
        from ksma.shared.rbac import RoleCode  # noqa: I001, PLC0415

        from tests.unit.conftest_helpers import parametrize_boundary  # noqa: PLC0415

        @parametrize_boundary([RoleCode.OW, RoleCode.SV])
        def _case(role):  # pragma: no cover - never executed, only introspected
            return role

        (mark,) = _case.pytestmark
        assert mark.name == "parametrize"
        assert mark.args == ("value", [RoleCode.OW, RoleCode.SV])
        assert mark.kwargs["ids"] == ["OW", "SV"]

    def test_the_decorator_honours_an_explicit_argument_name(self) -> None:
        from tests.unit.conftest_helpers import parametrize_boundary  # noqa: PLC0415

        @parametrize_boundary([1, 2], argname="amount")
        def _case(amount):  # pragma: no cover - only introspected
            return amount

        (mark,) = _case.pytestmark
        assert mark.args == ("amount", [1, 2])

    def test_the_decorator_honours_a_custom_id_function(self) -> None:
        from tests.unit.conftest_helpers import parametrize_boundary  # noqa: PLC0415

        @parametrize_boundary(["x", "y"], idfn=lambda value: f"cell_{value}")
        def _case(value):  # pragma: no cover - only introspected
            return value

        (mark,) = _case.pytestmark
        assert mark.kwargs["ids"] == ["cell_x", "cell_y"]

    def test_repeated_cells_get_unique_suffixes(self) -> None:
        from tests.unit.conftest_helpers import parametrize_boundary  # noqa: PLC0415

        @parametrize_boundary(["a", "a", "a"])
        def _case(value):  # pragma: no cover - only introspected
            return value

        (mark,) = _case.pytestmark
        assert mark.kwargs["ids"] == ["a", "a_2", "a_3"]
