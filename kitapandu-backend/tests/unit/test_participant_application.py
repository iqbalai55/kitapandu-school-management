"""Unit tests for ``ksma.participant.application`` — use cases.

These tests are pure-Python (no DB, no Docker, no network). ``AsyncMock``
from ``unittest.mock`` drives the repositories that the application
services consume (``ParticipantRepository`` and friends); ``session.execute``
is stubbed via a small async dispatcher to discriminate the
duplicate-suspect SQLs in ``search_participants``.

Public surface under test (per P46 / ``integration/kill-mutants`` — 19
survivors on ``ksma/participant/application/__init__.py``):

- ``update_participant``     — line 116: ``actor or str(ctx.account_id)``
- ``merge_participants``     — line 172: ``source is None or target is None``
- ``request_account_link``   — line 267: XOR on ``participant_id`` / ``related_party_id``
- ``request_relation``       — line 343: PAYER requires ``financial_scope``
- ``search_participants``    — lines 408, 416, 435-437, 447-449, 474:
  default ``offset``, query-length threshold, same-name suspect branch,
  same-email suspect branch.

Each test names the operator site it pins in its docstring so a
mutation-tool diff can map "this test asserts X" to "this operator at
line N produced a mutant that broke X".
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from ksma.participant.application import (
    merge_participants,
    request_account_link,
    request_relation,
    search_participants,
    update_participant,
)
from ksma.participant.domain import (
    FinancialScope,
    Participant,
    ParticipantRelation,
    ParticipantStatus,
    RelationKind,
)
from ksma.participant.infrastructure.repositories import (
    ParticipantAccountLinkRepository,
    ParticipantRelationRepository,
    ParticipantRepository,
)
from ksma.shared.errors import ValidationError

from tests.unit.conftest_helpers import FakeTenantContext
from ksma.shared.rbac import RoleCode


# ---------------------------------------------------------------------------
# Fakes + helpers
# ---------------------------------------------------------------------------


def _ctx(account_id: UUID | None = None) -> Any:
    """ACCOUNT-scoped ``TenantContext`` for the test session."""
    builder = FakeTenantContext(RoleCode.AC)
    if account_id is not None:
        builder = builder.with_account(account_id, uuid4())
    return builder.build()


def _part(
    *,
    id: UUID | None = None,
    full_name: str = "Alice",
    contact: dict[str, Any] | None = None,
) -> Participant:
    """A test ``Participant`` with sensible defaults."""
    return Participant(
        id=id or uuid4(),
        tenant_id=uuid4(),
        participant_no="P001",
        full_name=full_name,
        contact=contact,
        identity_reference=None,
        status=ParticipantStatus.ACTIVE,
    )


class _Row:
    """One row returned from a (mocked) ``session.execute(text(...))`` call."""

    __slots__ = ("id",)

    def __init__(self, row_id: UUID) -> None:
        self.id = row_id


class _Result:
    """The reader surface for a single SELECT result — ``.all()`` only."""

    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def all(self) -> list[_Row]:
        return list(self._rows)


def _session_execute(
    *, same_name_rows: list[_Row], same_email_rows: list[_Row]
) -> AsyncMock:
    """Build an ``AsyncMock`` for ``session.execute`` that dispatches by SQL text.

    The application fires two SELECTs during duplicate-suspect detection
    that share the ``session.execute`` call shape; we discriminate by the
    SQL fragment that uniquely identifies each query.

    The ``side_effect`` is bound to ``stub.execute`` (the attribute the
    application code calls), NOT to ``stub`` itself — those are two
    distinct ``AsyncMock`` instances under AsyncMock's automatic attribute
    delegation, and assigning to the outer binding silently loses the
    side_effect on the inner one that ``search_participants`` actually
    invokes.
    """
    stub = AsyncMock()

    async def _route(statement: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = getattr(statement, "text", "") or ""
        if "LOWER(full_name)" in sql:
            return _Result(same_name_rows)
        if "contact->>'email'" in sql:
            return _Result(same_email_rows)
        return _Result([])

    stub.execute.side_effect = _route
    return stub


# ===========================================================================
# 1. search_participants
# ===========================================================================


class TestSearchParticipants:
    """Duplicate-suspect behaviour of ``search_participants`` (V02).

    Pinned mutant sites in ``search_participants``:
    - line 408 (``offset: int = 0``)
    - line 416 (``if query and len(query.strip()) >= 3:``)
    - lines 435-437 (``if len(ids) > 1``, ``ids[:1]``, ``x != primary``)
    - lines 447-449 (``participants[0].id`` / ``.contact`` / ``.contact.get("email")``)
    - line 474 (``DuplicateSuspect(participant_id=participants[0].id, ...)``)
    """

    async def test_default_offset_is_zero(self) -> None:
        """``offset: int = 0`` — kills ``0 -> 1`` on line 408.

        Calling ``search_participants`` without an ``offset`` must invoke
        ``repo.search(..., offset=0)``; the mutated default would pass 1.
        """
        repo_mock = AsyncMock()
        repo_mock.search.return_value = ([], [])
        session = _session_execute(same_name_rows=[], same_email_rows=[])
        with patch(
            "ksma.participant.application.ParticipantRepository", return_value=repo_mock
        ):
            result = await search_participants(session, _ctx())
        assert repo_mock.search.await_args.kwargs["offset"] == 0
        assert result.duplicate_suspects == []

    async def test_two_char_query_skips_duplicate_detection(self) -> None:
        """``if query and len(...) >= 3:`` — kills ``and -> or`` on line 416.

        A two-char query must NOT enter the suspect branch. With ``or`` the
        short-circuit evaluates ``"ab" or 2 >= 3`` -> True, the same-name
        SQL fires, and (in the no-match fixture) the assertion below
        catches the spurious call.
        """
        repo_mock = AsyncMock()
        repo_mock.search.return_value = ([_part()], [])
        session = _session_execute(same_name_rows=[_Row(uuid4())], same_email_rows=[])
        with patch(
            "ksma.participant.application.ParticipantRepository", return_value=repo_mock
        ):
            result = await search_participants(session, _ctx(), query="al")
        assert result.duplicate_suspects == []
        # No session.execute call fired — the duplicate branch is gated.
        assert session.execute.await_count == 0

    async def test_three_char_query_runs_same_name_sql(self) -> None:
        """``len(query.strip()) >= 3`` — kills ``>= -> >`` on line 416.

        A three-char query MUST pass the threshold and issue the same-name
        SQL. With ``> 3`` the check fails and no SELECT is issued.
        """
        p = _part()
        name_match_id = uuid4()
        repo_mock = AsyncMock()
        # One same-name row -> boundary check on line 435 collapses to False,
        # so no suspect is produced — but the SQL itself must have run.
        repo_mock.search.return_value = ([p], [])
        session = _session_execute(
            same_name_rows=[_Row(name_match_id)], same_email_rows=[]
        )
        with patch(
            "ksma.participant.application.ParticipantRepository", return_value=repo_mock
        ):
            await search_participants(session, _ctx(), query="ali")
        # Same-name SQL fired exactly once with the lowercased query.
        same_name_calls = [
            c for c in session.execute.await_args_list
            if "LOWER(full_name)" in c.args[0].text
        ]
        assert len(same_name_calls) == 1
        assert same_name_calls[0].args[1]["q"] == "ali"

    async def test_two_same_name_rows_yield_one_suspect_pointing_at_first(self) -> None:
        """Lines 435 (``len(ids) > 1``), 436 (``ids[:1]``), 437 (``x != primary``).

        With two same-name rows, exactly one suspect is produced; the
        primary is the FIRST id and the matched set contains only the
        SECOND id (never the primary itself). All three boundary flips
        break at least one of these assertions.
        """
        p = _part()
        first_id = uuid4()
        second_id = uuid4()
        repo_mock = AsyncMock()
        repo_mock.search.return_value = ([p], [])
        session = _session_execute(
            same_name_rows=[_Row(first_id), _Row(second_id)], same_email_rows=[]
        )
        with patch(
            "ksma.participant.application.ParticipantRepository", return_value=repo_mock
        ):
            result = await search_participants(session, _ctx(), query="ali")
        same_name_suspects = [
            s for s in result.duplicate_suspects if s.reason == "same_name"
        ]
        assert len(same_name_suspects) == 1
        s = same_name_suspects[0]
        assert s.participant_id == first_id
        assert s.matched_participant_ids == (second_id,)
        # The primary must NOT appear in its own matched list.
        assert first_id not in s.matched_participant_ids
        # And only ONE entry — not both ids (catches slicing ``0 -> 1``).
        assert len(s.matched_participant_ids) == 1

    async def test_one_same_name_match_yields_no_suspect(self) -> None:
        """``len(ids) > 1`` — kills ``1 -> 0`` and ``> -> >=`` on line 435.

        With exactly one same-name row, the block is skipped entirely. The
        mutated ``> 0`` (or ``>= 1``) would still emit a suspect.
        """
        p = _part()
        only_id = uuid4()
        repo_mock = AsyncMock()
        repo_mock.search.return_value = ([p], [])
        session = _session_execute(same_name_rows=[_Row(only_id)], same_email_rows=[])
        with patch(
            "ksma.participant.application.ParticipantRepository", return_value=repo_mock
        ):
            result = await search_participants(session, _ctx(), query="ali")
        assert result.duplicate_suspects == []

    async def test_participant_with_email_fires_same_email_sql_with_pid(self) -> None:
        """Lines 447, 449: ``participants[0].id`` / ``.contact.get("email")``.

        With a single returned participant carrying an email, the same-email
        SELECT is issued with the participant's id as the ``pid`` exclude
        parameter and the email as ``em``. ``0 -> 1`` flips on either
        line raise ``IndexError`` and abort the call.
        """
        p = _part(contact={"email": "alice@example.com"})
        other_id = uuid4()
        repo_mock = AsyncMock()
        repo_mock.search.return_value = ([p], [])
        session = _session_execute(
            same_name_rows=[], same_email_rows=[_Row(other_id)]
        )
        with patch(
            "ksma.participant.application.ParticipantRepository", return_value=repo_mock
        ):
            await search_participants(session, _ctx(), query="ali")
        email_calls = [
            c for c in session.execute.await_args_list
            if "contact->>'email'" in c.args[0].text
        ]
        assert len(email_calls) == 1
        params = email_calls[0].args[1]
        assert params["pid"] == str(p.id)
        assert params["em"] == "alice@example.com"

    async def test_no_contact_skips_same_email_sql(self) -> None:
        """Line 448: ``participants[0].contact and (email_lookup)``.

        When ``contact`` is ``None`` the lookup short-circuits to ``None``
        and the email SQL must NOT fire. With ``and -> or`` the lookup
        runs against ``None`` (``None.get('email')`` -> ``AttributeError``)
        and with ``0 -> 1`` an ``IndexError`` escapes — both fatal.

        The same-name SELECT still runs (the query has three chars) but
        returns no rows, so no suspects. We only assert the EMAIL-SELECT
        branch was skipped.
        """
        p = _part(contact=None)
        repo_mock = AsyncMock()
        repo_mock.search.return_value = ([p], [])
        session = _session_execute(same_name_rows=[], same_email_rows=[])
        with patch(
            "ksma.participant.application.ParticipantRepository", return_value=repo_mock
        ):
            result = await search_participants(session, _ctx(), query="ali")
        assert result.duplicate_suspects == []
        # No email-SQL select fired — the same-email branch is gated on contact.
        email_calls = [
            c for c in session.execute.await_args_list
            if "contact->>'email'" in c.args[0].text
        ]
        assert email_calls == []

    async def test_same_email_suspect_primary_is_first_participant(self) -> None:
        """Line 474: ``DuplicateSuspect(participant_id=participants[0].id, ...)``.

        The suspect's primary id must equal the only returned participant's
        id; with ``0 -> 1`` and a one-participant fixture an ``IndexError``
        escapes and the call fails before the assertion runs.
        """
        p = _part(contact={"email": "alice@example.com"})
        matched_id = uuid4()
        repo_mock = AsyncMock()
        repo_mock.search.return_value = ([p], [])
        session = _session_execute(
            same_name_rows=[], same_email_rows=[_Row(matched_id)]
        )
        with patch(
            "ksma.participant.application.ParticipantRepository", return_value=repo_mock
        ):
            result = await search_participants(session, _ctx(), query="ali")
        email_suspects = [
            s for s in result.duplicate_suspects if s.reason == "same_contact_email"
        ]
        assert len(email_suspects) == 1
        assert email_suspects[0].participant_id == p.id
        assert email_suspects[0].matched_participant_ids == (matched_id,)


# ===========================================================================
# 2. update_participant
# ===========================================================================


class TestUpdateParticipant:
    """``update_participant`` — ``actor or str(ctx.account_id)`` on line 116.

    The function writes ``participant.updated_by`` and the row is later
    promoted by the repository. We pin the IN-MEMORY value here so the
    short-circuit operator flip is caught.
    """

    async def test_explicit_actor_wins_over_account_id(self) -> None:
        """`actor or str(ctx.account_id)` — kills the ``or -> and`` mutation.

        With ``actor="explicit"`` the resolved ``updated_by`` MUST echo the
        explicit value; mutating ``or -> and`` returns the right-hand side
        whenever both are truthy, swapping the two and breaking this
        assertion.
        """
        session = AsyncMock()
        ctx_account = uuid4()
        ctx = _ctx(account_id=ctx_account)
        part_id = uuid4()
        repo_mock = AsyncMock()
        repo_mock.get.return_value = _part(id=part_id)
        with patch(
            "ksma.participant.application.ParticipantRepository", return_value=repo_mock
        ):
            result = await update_participant(
                session, ctx, part_id, full_name="Bob", actor="explicit"
            )
        assert result.updated_by == "explicit"
        assert result.updated_by != str(ctx_account)


# ===========================================================================
# 3. request_account_link
# ===========================================================================


class TestRequestAccountLink:
    """``request_account_link`` — XOR check on line 267.

    `(participant_id is None) == (related_party_id is None)` rejects BOTH
    set and BOTH unset. Mutating ``== -> !=`` lets each case slip through.
    """

    async def test_both_pid_and_rpid_provided_raises(self) -> None:
        """Both ids set -> ``ValidationError``; no link is created."""
        session = AsyncMock()
        ctx = _ctx()
        link_repo = AsyncMock()
        with patch(
            "ksma.participant.application.ParticipantAccountLinkRepository",
            return_value=link_repo,
        ):
            with pytest.raises(ValidationError):
                await request_account_link(
                    session,
                    ctx,
                    account_id=uuid4(),
                    participant_id=uuid4(),
                    related_party_id=uuid4(),
                    evidence={"v": "x"},
                )
        assert link_repo.create.await_count == 0

    async def test_neither_pid_nor_rpid_provided_raises(self) -> None:
        """Both ids None -> ``ValidationError``; no link is created."""
        session = AsyncMock()
        ctx = _ctx()
        link_repo = AsyncMock()
        with patch(
            "ksma.participant.application.ParticipantAccountLinkRepository",
            return_value=link_repo,
        ):
            with pytest.raises(ValidationError):
                await request_account_link(
                    session,
                    ctx,
                    account_id=uuid4(),
                    participant_id=None,
                    related_party_id=None,
                    evidence={"v": "x"},
                )
        assert link_repo.create.await_count == 0


# ===========================================================================
# 4. request_relation
# ===========================================================================


class TestRequestRelation:
    """``request_relation`` — PAYER requires ``financial_scope`` (line 343).

    The condition ``relation_kind is RelationKind.PAYER and not
    financial_scope.all_approved and not financial_scope.invoices``
    has two ``and`` operators and two ``not`` operators; flipping any of
    them changes what counts as "valid PAYER scope". The two tests below
    pin both sides of the gate.
    """

    async def test_payer_with_empty_scope_raises(self) -> None:
        """PAYER + default ``FinancialScope()`` -> ``ValidationError``.

        Kills the two ``delete not`` flips: dropping either ``not`` makes
        the mutated condition ``False and False / False and True`` on
        this input, so the error never fires.
        """
        session = AsyncMock()
        ctx = _ctx()
        rel_repo = AsyncMock()
        with patch(
            "ksma.participant.application.ParticipantRelationRepository",
            return_value=rel_repo,
        ):
            with pytest.raises(ValidationError):
                await request_relation(
                    session,
                    ctx,
                    participant_id=uuid4(),
                    related_party_id=uuid4(),
                    relation_kind=RelationKind.PAYER,
                    financial_scope=FinancialScope(),
                    evidence={"v": "x"},
                )
        assert rel_repo.create.await_count == 0

    async def test_payer_with_all_approved_scope_creates_relation(self) -> None:
        """PAYER + ``FinancialScope(all_approved=True)`` -> repo.create is called.

        Kills the three flips that change the gate to truthy on this
        input — ``(and -> or)`` on the first clause and the second clause,
        plus the first ``delete not`` — each of which would spuriously
        raise ``ValidationError`` on a valid call.
        """
        session = AsyncMock()
        ctx = _ctx()
        rel_repo = AsyncMock()
        rel_repo.create.return_value = ParticipantRelation(
            tenant_id=ctx.tenant_id,
            participant_id=uuid4(),
            related_party_id=uuid4(),
            relation_kind=RelationKind.PAYER,
            financial_scope=FinancialScope(all_approved=True),
        )
        with patch(
            "ksma.participant.application.ParticipantRelationRepository",
            return_value=rel_repo,
        ):
            await request_relation(
                session,
                ctx,
                participant_id=uuid4(),
                related_party_id=uuid4(),
                relation_kind=RelationKind.PAYER,
                financial_scope=FinancialScope(all_approved=True),
                evidence={"v": "x"},
            )
        assert rel_repo.create.await_count == 1


# ===========================================================================
# 5. merge_participants
# ===========================================================================


class TestMergeParticipants:
    """``merge_participants`` — ``source is None or target is None`` (line 172).

    The Q02 ``enrollment`` policy gate fires FIRST; we stub ``session.execute``
    to return a policy row so the validation branch is reached. We then
    arrange ``repo.get(target_id)`` -> ``None`` and assert the call raises
    ``ValidationError``. The mutated ``and`` would only raise when BOTH
    source and target are missing.
    """

    async def test_missing_target_raises_when_target_only(self) -> None:
        """`source is None or target is None` — kills ``or -> and`` on line 172."""
        session = AsyncMock()

        # Q02 gate: ``session.execute(...).first()`` must return a non-None row.
        # Production calls ``.first()`` without await and uses the result as a
        # truthiness probe; a plain ``MagicMock`` (not ``AsyncMock``) returns
        # the ``return_value`` synchronously and avoids the auto-created
        # coroutine on ``.first()`` that AsyncMock would otherwise yield.
        from unittest.mock import MagicMock

        class _PolicyResult:
            @staticmethod
            def first() -> tuple[str, ...]:
                return ("policy",)

        session.execute = AsyncMock(return_value=_PolicyResult())

        ctx = _ctx()
        source_id = uuid4()
        target_id = uuid4()
        repo_mock = AsyncMock()

        def _get(pk: UUID) -> Participant | None:
            return _part(id=source_id) if pk == source_id else None

        repo_mock.get.side_effect = _get
        with patch(
            "ksma.participant.application.ParticipantRepository", return_value=repo_mock
        ):
            with pytest.raises(ValidationError):
                await merge_participants(
                    session,
                    ctx,
                    source_id=source_id,
                    target_id=target_id,
                    reason="test-merge",
                )
        # The target was looked up, the source was too, and no archive/update
        # happened afterwards because the validation fired first.
        assert repo_mock.get.await_count == 2
        assert repo_mock.archive.await_count == 0
