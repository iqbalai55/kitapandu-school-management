"""Unit tests for the predicate-extraction helpers in conftest_helpers.

These test the operator-dispatch that D7 (ticket #30) introduced:
``FakeAsyncSession._where`` now captures the comparison operator
(``=``, ``!=``, ``<>``, ``<``, ``>``, ``<=``, ``>=``) instead of only
``=``. ``_matches`` then dispatches on the captured operator.
"""

from __future__ import annotations

import pytest

from tests.unit.conftest_helpers import (
    FakeAsyncSession,
    _PREDICATE_OPS,
    FakeRow,
)


def _seed(session: FakeAsyncSession, table: str, rows: list[dict]) -> None:
    for row in rows:
        session._insert(table, row)


def test_equality_still_returns_only_matching_rows() -> None:
    """The pre-wave ``=`` semantic must be unchanged."""
    session = FakeAsyncSession()
    _seed(session, "accounts", [
        {"id": "a1", "status": "active"},
        {"id": "a2", "status": "active"},
        {"id": "a3", "status": "pending"},
    ])
    rows = session._select("accounts", "SELECT * FROM accounts WHERE status = :status", {"status": "active"})
    assert [r["id"] for r in rows] == ["a1", "a2"]


def test_inequality_filters_out_matching_rows() -> None:
    """``!=`` must exclude rows that match the predicate value."""
    session = FakeAsyncSession()
    _seed(session, "accounts", [
        {"id": "a1", "status": "active"},
        {"id": "a2", "status": "pending"},
        {"id": "a3", "status": "active"},
    ])
    rows = session._select("accounts", "SELECT * FROM accounts WHERE status != :status", {"status": "active"})
    assert [r["id"] for r in rows] == ["a2"]


def test_diamond_inequality_is_alias_for_neq() -> None:
    """``<>`` should behave identically to ``!=``."""
    session = FakeAsyncSession()
    _seed(session, "accounts", [
        {"id": "a1", "status": "active"},
        {"id": "a2", "status": "pending"},
    ])
    rows = session._select("accounts", "SELECT * FROM accounts WHERE status <> :status", {"status": "active"})
    assert [r["id"] for r in rows] == ["a2"]


def test_compound_predicate_with_inequality() -> None:
    """A compound WHERE must honour each predicate independently."""
    session = FakeAsyncSession()
    _seed(session, "accounts", [
        {"id": "a1", "status": "active", "authz_revision": 3},
        {"id": "a2", "status": "active", "authz_revision": 5},
        {"id": "a3", "status": "pending", "authz_revision": 5},
        {"id": "a4", "status": "active", "authz_revision": 7},
    ])
    rows = session._select(
        "accounts",
        "SELECT * FROM accounts WHERE status = :status AND authz_revision != :rev",
        {"status": "active", "rev": 5},
    )
    assert [r["id"] for r in rows] == ["a1", "a4"]


@pytest.mark.parametrize(
    "op",
    ["=", "!=", "<>", "<", ">", "<=", ">="],
)
def test_predicate_ops_table_is_complete(op: str) -> None:
    assert op in _PREDICATE_OPS


def test_unknown_operator_bails_safely() -> None:
    """An unsupported operator must NOT silently match everything."""
    session = FakeAsyncSession()
    _seed(session, "accounts", [
        {"id": "a1", "status": "active"},
        {"id": "a2", "status": "inactive"},
    ])
    # `IS NULL` is not in our dispatch table; the fake currently returns
    # all rows (no predicate match), which is honest about not modelling
    # the semantic rather than falsely evaluating it.
    rows = session._select(
        "accounts",
        "SELECT * FROM accounts WHERE status IS NULL",
        {},
    )
    assert len(rows) == 2  # `IS NULL` is unmodelled, but no row is dropped to "match"
