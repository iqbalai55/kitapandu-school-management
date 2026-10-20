"""Shared test toolkit for the mutation-killing unit suite (#35 / P5.1).

Every helper here is pure Python: no DB, no Docker, no network, no SQLAlchemy
runtime. ``FakeAsyncSession`` stores rows in dicts, ``FakeRepository`` stores
items in a dict-backed table, and the two assertion helpers turn a call log
into a domain-shaped assertion.
"""
from __future__ import annotations

import inspect
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from ksma.shared.rbac import RoleCode
from ksma.shared.tenancy import ActorKind, TenantContext

ModelT = TypeVar("ModelT", bound=BaseModel)


class FakeRow(dict):
    """A stored row that reads like a SQLAlchemy ``Row``.

    Repository code reaches for both styles — ``row["status"]`` and
    ``row.status`` — so the fake supports both.
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:  # pragma: no cover - defensive, mirrors AttributeError
            raise AttributeError(name) from exc


@dataclass(frozen=True, slots=True)
class SessionCall:
    """One recorded call against :class:`FakeAsyncSession`."""

    op: str
    sql: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)
    value: Any = None


class FakeResult:
    """The reader surface of a SQLAlchemy ``Result`` over a fixed row list."""

    def __init__(self, rows: list[FakeRow], *, rowcount: int = 0) -> None:
        self._rows = list(rows)
        self.rowcount = rowcount

    def __iter__(self):
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def first(self) -> FakeRow | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[FakeRow]:
        return list(self._rows)

    def one_or_none(self) -> FakeRow | None:
        if len(self._rows) > 1:
            raise AssertionError(f"expected at most one row, got {len(self._rows)}")
        return self.first()

    def scalar_one_or_none(self) -> Any:
        row = self.one_or_none()
        return None if row is None else next(iter(row.values()))

    def scalars(self) -> list[Any]:
        return [next(iter(row.values())) for row in self._rows]


_INSERT_RE = re.compile(r"INSERT\s+INTO\s+([A-Za-z_][\w.]*)", re.IGNORECASE)
_UPDATE_RE = re.compile(r"UPDATE\s+([A-Za-z_][\w.]*)", re.IGNORECASE)
_DELETE_RE = re.compile(r"DELETE\s+FROM\s+([A-Za-z_][\w.]*)", re.IGNORECASE)
_SELECT_RE = re.compile(r"FROM\s+([A-Za-z_][\w.]*)", re.IGNORECASE)
_EQUALITY_RE = re.compile(r"([A-Za-z_]\w*)\s*=\s*:(\w+)")
_SET_ASSIGNMENT_RE = re.compile(r"([A-Za-z_]\w*)\s*=\s*:(\w+)")
_WHERE_RE = re.compile(r"\bWHERE\b(.*)", re.IGNORECASE | re.DOTALL)
_SET_RE = re.compile(r"\bSET\b(.*?)(?:\bWHERE\b|$)", re.IGNORECASE | re.DOTALL)


def _plain_table(name: str) -> str:
    """``"ksma.participant"`` / ``'"participant"'`` -> ``"participant"``."""
    return name.strip('"').rsplit(".", 1)[-1]


def _sql_of(statement: Any) -> str:
    """The SQL text of ``text(...)``, a plain string, or any statement object."""
    if isinstance(statement, str):
        return statement
    text_attr = getattr(statement, "text", None)
    if isinstance(text_attr, str):
        return text_attr
    return str(statement)


def _same(left: Any, right: Any) -> bool:
    """Compare a stored row value with a query parameter.

    Repositories pass ``str(row["id"])`` while seeded rows may hold a ``UUID``,
    so equality is decided on the string form as well.
    """
    if left == right:
        return True
    return str(left) == str(right)


class FakeAsyncSession:
    """In-memory stand-in for ``AsyncSession`` backed by dict tables.

    Supported statements are the four the repositories actually issue —
    ``SELECT``/``INSERT``/``UPDATE``/``DELETE`` — dispatched on their leading
    keyword. ``SELECT`` filters on ``col = :param`` equality predicates joined
    by ``AND``; a predicate it cannot parse falls through to "every row of the
    table", which keeps the fake useful on queries that do not need filtering.
    """

    def __init__(self, rows: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._tables: dict[str, list[FakeRow]] = {}
        self.calls: list[SessionCall] = []
        self.committed = False
        for table, seeded in (rows or {}).items():
            self.seed(table, seeded)
        self._snapshot()

    # --- seeding ------------------------------------------------------------

    def seed(
        self, table: str, rows: dict[str, Any] | list[dict[str, Any]]
    ) -> FakeAsyncSession:
        """Append one row (mapping) or several rows (iterable of mappings)."""
        if isinstance(rows, dict):
            self._tables.setdefault(table, []).append(FakeRow(rows))
            return self
        self._tables.setdefault(table, []).extend(FakeRow(row) for row in rows)
        return self

    def rows(self, table: str) -> list[FakeRow]:
        """Every stored row of ``table``, in insertion order."""
        return list(self._tables.get(table, []))

    # --- Pydantic round-trip -------------------------------------------------

    def insert_model(self, table: str, model: BaseModel) -> BaseModel:
        """Store ``model`` as a JSON-mode row and return it unchanged.

        ``mode="json"`` mirrors what the repositories persist — ``str(uuid)``,
        ISO dates — so a model validated back out of the row is equal to the
        one that went in.
        """
        self._tables.setdefault(table, []).append(FakeRow(model.model_dump(mode="json")))
        return model

    def seed_model(self, table: str, model: BaseModel) -> BaseModel:
        """Seed a pre-existing row; reads exactly like :meth:`insert_model`."""
        return self.insert_model(table, model)

    def get_model(
        self, model_type: type[ModelT], table: str, pk: Any, *, key: str = "id"
    ) -> ModelT | None:
        """Validate the row whose ``key`` column equals ``pk``, else ``None``."""
        for row in self._tables.get(table, []):
            if _same(row.get(key), pk):
                return model_type.model_validate(row)
        return None


    # --- call log -----------------------------------------------------------

    def _record(self, call: SessionCall) -> SessionCall:
        self.calls.append(call)
        return call

    def assert_called(
        self,
        *,
        op: str,
        sql_contains: str | None = None,
        params_contains: str | None = None,
    ) -> SessionCall:
        """Assert a recorded call matches, and return it (fails with the log)."""
        for call in self.calls:
            if call.op != op:
                continue
            if sql_contains is not None and sql_contains not in call.sql:
                continue
            if params_contains is not None and not any(
                params_contains in str(value) for value in call.params.values()
            ):
                continue
            return call
        raise AssertionError(
            f"no {op!r} call matching sql_contains={sql_contains!r} "
            f"params_contains={params_contains!r}; recorded: "
            + (", ".join(f"{c.op}:{c.sql[:60]}" for c in self.calls) or "<none>")
        )

    # --- transaction --------------------------------------------------------

    def _snapshot(self) -> None:
        self._committed = {table: [FakeRow(row) for row in rows] for table, rows in self._tables.items()}

    async def commit(self) -> None:
        self.committed = True
        self._record(SessionCall(op="commit"))
        self._snapshot()

    async def rollback(self) -> None:
        """Restore every table to its state at the last commit."""
        self.committed = False
        self._record(SessionCall(op="rollback"))
        self._tables = {
            table: [FakeRow(row) for row in rows] for table, rows in self._committed.items()
        }

    async def flush(self) -> None:
        self._record(SessionCall(op="flush"))

    async def refresh(self, instance: Any) -> None:
        self._record(SessionCall(op="refresh", value=instance))

    async def add(self, instance: Any) -> None:
        self._record(SessionCall(op="add", value=instance))

    async def delete(self, instance: Any) -> None:
        self._record(SessionCall(op="delete", value=instance))

    # --- statements ---------------------------------------------------------

    async def execute(
        self, statement: Any, parameters: Mapping[str, Any] | None = None
    ) -> FakeResult:
        sql = _sql_of(statement)
        params = dict(parameters or {})
        self._record(SessionCall(op="execute", sql=sql, params=params))

        insert = _INSERT_RE.search(sql)
        if insert:
            return self._insert(_plain_table(insert.group(1)), params)

        delete = _DELETE_RE.search(sql)
        if delete:
            return self._delete(_plain_table(delete.group(1)), sql, params)

        update = _UPDATE_RE.search(sql)
        if update:
            return self._update(_plain_table(update.group(1)), sql, params)

        select = _SELECT_RE.search(sql)
        if select:
            return FakeResult(self._select(_plain_table(select.group(1)), sql, params))

        return FakeResult([])

    def _where(self, sql: str) -> list[tuple[str, str]]:
        """``col = :param`` pairs from the WHERE clause only.

        The clause is sliced from the original-cased SQL so bind-parameter
        names keep their case (``:id``, not ``:ID``).
        """
        found = _WHERE_RE.search(sql)
        if found is None:
            return []
        return [(col.lower(), param) for col, param in _EQUALITY_RE.findall(found.group(1))]

    def _matches(
        self, row: FakeRow, predicates: Iterable[tuple[str, str]], params: Mapping[str, Any]
    ) -> bool:
        return all(
            column in row and param in params and _same(row[column], params[param])
            for column, param in predicates
        )

    def _select(
        self, table: str, sql: str, params: Mapping[str, Any]
    ) -> list[FakeRow]:
        predicates = self._where(sql)
        rows = self._tables.get(table, [])
        if not predicates:
            return list(rows)
        return [row for row in rows if self._matches(row, predicates, params)]

    def _insert(self, table: str, params: Mapping[str, Any]) -> FakeResult:
        self._tables.setdefault(table, []).append(FakeRow(dict(params)))
        return FakeResult([], rowcount=1)

    def _update(self, table: str, sql: str, params: Mapping[str, Any]) -> FakeResult:
        found = _SET_RE.search(sql)
        assignments = (
            [(col.lower(), param) for col, param in _SET_ASSIGNMENT_RE.findall(found.group(1))]
            if found
            else []
        )
        predicates = self._where(sql)
        updated = 0
        for row in self._tables.get(table, []):
            if not self._matches(row, predicates, params):
                continue
            for column, param in assignments:
                if param in params:
                    row[column] = params[param]
            updated += 1
        return FakeResult([], rowcount=updated)

    def _delete(self, table: str, sql: str, params: Mapping[str, Any]) -> FakeResult:
        predicates = self._where(sql)
        rows = self._tables.get(table, [])
        kept = [row for row in rows if not self._matches(row, predicates, params)]
        removed = len(rows) - len(kept)
        self._tables[table] = kept
        return FakeResult([], rowcount=removed)


def _row_of(item: Any) -> dict[str, Any]:
    """Normalise a stored item to a plain dict (model, mapping, or object)."""
    if isinstance(item, BaseModel):
        return dict(item.model_dump(mode="json"))
    if isinstance(item, Mapping):
        return dict(item)
    return dict(vars(item))


class FakeRepository:
    """Dict-backed table with a recorded call log per verb.

    A subclass binds its table by name — ``class InvoiceRepository(FakeRepository):
    table = "invoice"`` — and adds whatever named query the bounded context
    needs (``list_for_assessment``, ``search``, …) on top of the CRUD verbs
    below. Rows are normalised to plain dicts on the way in, so a Pydantic
    model and a hand-written dict are interchangeable at this seam.
    """

    table: str = "item"
    key: str = "id"

    def __init__(
        self,
        rows: Iterable[dict[str, Any] | BaseModel] | None = None,
        *,
        table: str | None = None,
        key: str | None = None,
    ) -> None:
        self.table = table if table is not None else self.table
        self.key = key if key is not None else self.key
        self.rows: dict[Any, dict[str, Any]] = {
            _row_of(row)[self.key]: _row_of(row) for row in (rows or [])
        }
        self.added: list[Any] = []
        self.updated: list[Any] = []
        self.deleted: list[Any] = []
        self.loaded: list[Any] = []

    def __len__(self) -> int:
        return len(self.rows)

    def create(self, item: dict[str, Any] | BaseModel) -> Any:
        """Insert ``item`` and record the add. Returns ``item`` unchanged."""
        row = _row_of(item)
        self.rows[row[self.key]] = row
        self.added.append(item)
        return item

    add = create

    def get(self, pk: Any) -> dict[str, Any] | None:
        """Read one row, recording the load. ``None`` when absent."""
        self.loaded.append(pk)
        found = self.rows.get(pk)
        return dict(found) if found is not None else None

    def all(self) -> list[dict[str, Any]]:
        """Every row, in insertion order."""
        return [dict(row) for row in self.rows.values()]

    def save(self, item: dict[str, Any] | BaseModel) -> Any:
        """Upsert: a new key is an add, an existing key is an update."""
        row = _row_of(item)
        pk = row[self.key]
        if pk in self.rows:
            self.rows[pk].update(row)
            self.updated.append(item)
        else:
            return self.create(item)
        return item

    def update(self, pk: Any, **fields: Any) -> dict[str, Any]:
        """Merge ``fields`` into an existing row."""
        if pk not in self.rows:
            raise AssertionError(f"cannot update {pk!r}: not in {self.table}")
        self.rows[pk].update(fields)
        self.updated.append(fields)
        return dict(self.rows[pk])

    def delete(self, pk: Any) -> dict[str, Any] | None:
        """Remove a row, recording the deletion."""
        removed = self.rows.pop(pk, None)
        if removed is not None:
            self.deleted.append(pk)
        return removed


def assert_repo_added(
    repo: FakeRepository, *, count: int = 1, **fields: Any
) -> Any:
    """Assert ``repo`` recorded the expected adds, and return the last one."""
    assert len(repo.added) == count, (
        f"expected {count} added item(s), got {len(repo.added)}: {repo.added!r}"
    )
    if not fields:
        return repo.added[-1]
    target = repo.added[-1]
    row = _row_of(target)
    for name, expected in fields.items():
        actual = row.get(name, "<missing>")
        assert actual == expected, (
            f"added item {name}={actual!r}, expected {expected!r}"
        )
    return target


def assert_repo_state(
    repo: FakeRepository,
    *,
    count: int | None = None,
    keys: Iterable[Any] | None = None,
    match: Callable[[dict[str, Any]], bool] | None = None,
    **fields: Any,
) -> list[dict[str, Any]]:
    """Assert the dict-backed table itself, not the call log.

    ``count`` pins the row count, ``keys`` the primary keys, ``match`` a
    predicate over each row, and ``**fields`` column values every row must
    carry. With no argument at all the table must still be non-empty.
    """
    rows = [dict(row) for row in repo.rows.values()]
    if count is not None:
        assert len(rows) == count, f"expected {count} row(s), got {len(rows)} in {repo.table}"
    if keys is not None:
        expected_keys = sorted(str(key) for key in keys)
        actual_keys = sorted(str(key) for key in repo.rows)
        assert actual_keys == expected_keys, (
            f"expected keys {expected_keys}, got {actual_keys} in {repo.table}"
        )
    if match is not None:
        assert any(match(row) for row in rows), f"no row of {repo.table} matched"
    for name, expected in fields.items():
        for row in rows:
            actual = row.get(name, "<missing>")
            assert actual == expected, (
                f"{repo.table} row {row.get(repo.key)!r} has {name}={actual!r}, "
                f"expected {expected!r}"
            )
    if count is None and keys is None and match is None and not fields:
        assert rows, f"{repo.table} is empty"
    return rows


def assert_repo_deleted(repo: FakeRepository, pk: Any) -> None:
    """Assert ``pk`` was recorded as deleted and is gone from the table."""
    assert pk in repo.deleted, f"expected {pk!r} in deleted={repo.deleted!r}"
    assert pk not in repo.rows, f"expected {pk!r} gone from {repo.table}, still present"


ALL_ROLES: tuple[RoleCode, ...] = tuple(RoleCode)
"""The 13 roles of the Access Control Matrix (GLOSSARY §"Roles (13)")."""

SERVICE_ROLE = RoleCode.SV
"""Service identity for backup/retention/audit jobs (no human portal use)."""

SUPPORT_ROLE = RoleCode.OP
"""Platform operator — assists customer content under a time-bound grant."""


class FakeTenantContext:
    """Builder for a valid ``TenantContext`` at any of the 13 roles.

    The invariants the value object documents are applied here rather than
    left to each test: a service principal or a support operator carries no
    ``account_id`` / ``membership_id``, and every context has a ``tenant_id``
    (the ROOT boundary) even when it works tenant-wide.

        >>> ctx = FakeTenantContext(RoleCode.FI).with_unit(unit_id).build()

    ``authz_revision`` is the Q08 token-cache value the auth dependency
    compares against the JWT; ``with_authz_revision`` is the toggle a test
    flips to simulate a revoked membership.
    """

    def __init__(
        self,
        role: RoleCode | str = RoleCode.AC,
        *,
        tenant_id: UUID | None = None,
        unit_id: UUID | None = None,
        account_id: UUID | None = None,
        membership_id: UUID | None = None,
        actor_kind: ActorKind | None = None,
        authz_revision: int = 1,
        extra_roles: Iterable[RoleCode | str] = (),
    ) -> None:
        self._role = role
        self._tenant_id = tenant_id if tenant_id is not None else uuid4()
        self._unit_id = unit_id
        self._account_id = account_id
        self._membership_id = membership_id
        self._actor_kind = actor_kind if actor_kind is not None else _default_actor_kind(role)
        self._authz_revision = authz_revision
        self._extra_roles = list(extra_roles)

    def _clone(self) -> FakeTenantContext:
        return FakeTenantContext(
            self._role,
            tenant_id=self._tenant_id,
            unit_id=self._unit_id,
            account_id=self._account_id,
            membership_id=self._membership_id,
            actor_kind=self._actor_kind,
            authz_revision=self._authz_revision,
            extra_roles=self._extra_roles,
        )

    def with_role(self, role: RoleCode | str) -> FakeTenantContext:
        clone = self._clone()
        clone._role = role
        return clone

    def with_extra_roles(self, *roles: RoleCode | str) -> FakeTenantContext:
        clone = self._clone()
        clone._extra_roles.extend(roles)
        return clone

    def with_tenant(self, tenant_id: UUID) -> FakeTenantContext:
        clone = self._clone()
        clone._tenant_id = tenant_id
        return clone

    def with_unit(self, unit_id: UUID) -> FakeTenantContext:
        clone = self._clone()
        clone._unit_id = unit_id
        return clone

    def without_unit(self) -> FakeTenantContext:
        """Drop to tenant-wide scope (``unit_id=None``)."""
        clone = self._clone()
        clone._unit_id = None
        return clone

    def with_account(self, account_id: UUID, membership_id: UUID) -> FakeTenantContext:
        clone = self._clone()
        clone._account_id = account_id
        clone._membership_id = membership_id
        return clone

    def with_authz_revision(self, revision: int) -> FakeTenantContext:
        """Set the Q08 token-cache revision — the authz-version toggle."""
        clone = self._clone()
        clone._authz_revision = revision
        return clone

    def as_service(self) -> FakeTenantContext:
        clone = self._clone()
        clone._actor_kind = ActorKind.SERVICE_PRINCIPAL
        return clone

    def as_support(self) -> FakeTenantContext:
        clone = self._clone()
        clone._actor_kind = ActorKind.SUPPORT_OPERATOR
        return clone

    def as_account(self) -> FakeTenantContext:
        clone = self._clone()
        clone._actor_kind = ActorKind.ACCOUNT
        return clone

    def build(self) -> TenantContext:
        """Build the frozen value object, applying the actor-kind invariants."""
        is_account = self._actor_kind is ActorKind.ACCOUNT
        role_codes = (_code_of(self._role),) + tuple(_code_of(role) for role in self._extra_roles)
        return TenantContext(
            account_id=(self._account_id if self._account_id is not None else uuid4())
            if is_account
            else None,
            membership_id=(self._membership_id if self._membership_id is not None else uuid4())
            if is_account
            else None,
            tenant_id=self._tenant_id,
            unit_id=self._unit_id,
            actor_kind=self._actor_kind,
            authz_revision=self._authz_revision,
            role_codes=role_codes,
        )


def _code_of(role: RoleCode | str) -> str:
    """The role's wire code (``RoleCode.FI`` -> ``"FI"``)."""
    return role.value if isinstance(role, RoleCode) else str(role)


def _default_actor_kind(role: RoleCode | str) -> ActorKind:
    """SV is a service identity and OP assists tenants; the rest are accounts."""
    if role == SERVICE_ROLE:
        return ActorKind.SERVICE_PRINCIPAL
    if role == SUPPORT_ROLE:
        return ActorKind.SUPPORT_OPERATOR
    return ActorKind.ACCOUNT


def tenant_context(role: RoleCode | str = RoleCode.AC, **overrides: Any) -> TenantContext:
    """One-shot context for ``role`` — ``tenant_context(RoleCode.FI)``."""
    return FakeTenantContext(role, **overrides).build()


def _slug(value: Any) -> str:
    """The canonical table-cell name for one boundary value."""
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        # Enum cells keep their wire spelling: RoleCode.OW -> "OW", not "ow".
        member = value.value
        if isinstance(member, str):
            return re.sub(r"[^0-9a-zA-Z]+", "_", member).strip("_") or "empty"
        return _slug(member)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^0-9a-zA-Z]+", "_", value).strip("_").lower()
        return cleaned or "empty"
    return re.sub(r"[^0-9a-zA-Z]+", "_", type(value).__name__).strip("_").lower()


def boundary_ids(
    values: Iterable[Any], idfn: Callable[[Any], str] | None = None
) -> list[str]:
    """Canonical, unique pytest ids for a boundary table.

    ``RoleCode.OW`` -> ``OW``, ``True`` -> ``true``, ``None`` -> ``none``,
    ``-1`` -> ``-1``, ``"draft status"`` -> ``draft_status``. Repeated cells
    get a ``_2`` / ``_3`` suffix so pytest never collapses two rows of the
    table into one node.
    """
    ids: list[str] = []
    seen: dict[str, int] = {}
    for value in values:
        base = idfn(value) if idfn is not None else _slug(value)
        seen[base] = seen.get(base, 0) + 1
        ids.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return ids


def parametrize_boundary(
    values: Iterable[Any],
    *,
    argname: str = "value",
    idfn: Callable[[Any], str] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Parametrize a test over a boundary table with canonical cell names.

    One mutation per failing operator per table row is the goal, so the test
    id has to name the cell: ``test_scale[OW]`` rather than ``test_scale[0]``.

        @parametrize_boundary([RoleCode.OW, RoleCode.AC], argname="role")
        def test_role_is_in_the_context(role): ...
    """
    cells = list(values)

    def decorate(test: Callable[..., Any]) -> Callable[..., Any]:
        return pytest.mark.parametrize(argname, cells, ids=boundary_ids(cells, idfn))(test)

    return decorate


_VERDICT_RE = re.compile(r"^(KILLED|SURVIVED)\s+(\S+):(\d+)\s+(.*\S)\s*$")


@dataclass(frozen=True, slots=True)
class MutantOutcome:
    """One ``KILLED``/``SURVIVED`` line from a mutator run."""

    path: str
    line: int
    mutation: str
    verdict: str

    @property
    def killed(self) -> bool:
        return self.verdict == "killed"


@dataclass(frozen=True, slots=True)
class MutatorRun:
    """The exit status and combined output of one ``mutator`` invocation."""

    exit_code: int
    output: str


def parse_mutator_output(output: str) -> list[MutantOutcome]:
    """Pull the per-mutant verdicts out of mutator's stdout.

    Only ``KILLED``/``SURVIVED`` lines are read; the trailing function/score
    table is not a verdict and is ignored.
    """
    outcomes: list[MutantOutcome] = []
    for line in output.splitlines():
        found = _VERDICT_RE.match(line.strip())
        if found is None:
            continue
        verdict, path, line_no, mutation = found.groups()
        outcomes.append(
            MutantOutcome(
                path=path, line=int(line_no), mutation=mutation, verdict=verdict.lower()
            )
        )
    return outcomes


def mutator_command(
    *, root: str, source: str, line: int, test_command: str
) -> list[str]:
    """The mutator invocation that re-runs exactly one mutation site.

    ``--mutate-all`` is what makes this a gate rather than a report: without
    it a site the previous snapshot already killed is trusted instead of
    re-executed.
    """
    return [
        "mutator",
        "--root",
        str(root),
        str(source),
        "--lines",
        str(line),
        "--test-command",
        test_command,
        "--mutate-all",
        "--no-coverage",
        "--max-workers",
        "1",
    ]


def run_mutator(command: list[str], *, timeout: float | None = None) -> MutatorRun:
    """Run ``mutator`` and capture its verdict lines."""
    if shutil.which(command[0]) is None:
        raise RuntimeError(
            f"{command[0]!r} is not on PATH; install the mutation toolchain before "
            "gating a function with assert_function_killed"
        )
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        command, capture_output=True, text=True, timeout=timeout, check=False
    )
    return MutatorRun(exit_code=completed.returncode, output=completed.stdout + completed.stderr)


def project_root(start: Path | None = None) -> Path:
    """The repository root that contains ``kitapandu-backend/``."""
    backend = start or Path(__file__).resolve().parents[2]
    for candidate in (backend, *backend.parents):
        if candidate.name == "kitapandu-backend" and candidate.is_dir():
            return candidate.parent
    return backend


def _normalise_mutation(mutation: str) -> str:
    """``"+->-"`` and ``"+ -> -"`` name the same flip."""
    return re.sub(r"\s+", "", mutation)


def _test_command(fixture: Any, source: str) -> str:
    """The ``--test-command`` for the mutated run.

    ``fixture`` is the pytest fixture (or any zero-argument callable) that
    supplies the command; a plain string is accepted for one-off use.
    """
    if callable(fixture):
        command = fixture()
        if not isinstance(command, str):
            raise TypeError(
                f"fixture {fixture!r} must return the test command as a str, "
                f"got {type(command).__name__}"
            )
        return command
    if isinstance(fixture, str):
        return fixture
    raise TypeError(f"fixture must be a str or a callable returning a str, got {fixture!r}")


def assert_function_killed(
    fn: Callable[..., Any],
    *,
    line: int,
    mutation: str,
    fixture: Any,
    runner: Callable[[list[str]], MutatorRun] | None = None,
    root: Path | None = None,
    timeout: float | None = None,
) -> MutantOutcome:
    """Gate one function: mutate ``line`` and require the flip to be killed.

    ``fn`` locates the source file, ``line``/``mutation`` name the operator
    site (``"1 -> 0"``, ``"and -> or"``, ``"delete not"``), and ``fixture``
    supplies the pytest command mutator runs. Returns the passing
    :class:`MutantOutcome`; raises ``AssertionError`` when the mutant
    survived, when the site never ran, or when the baseline itself is red.
    """
    repo_root = Path(root) if root is not None else project_root()
    source_file = inspect.getsourcefile(fn)
    if source_file is None:
        raise AssertionError(f"cannot locate the source file of {fn!r}")
    source = Path(source_file).resolve()
    try:
        relative_source = str(source.relative_to(repo_root))
    except ValueError:  # pragma: no cover - source outside the repo root
        relative_source = str(source)

    command = mutator_command(
        root=str(repo_root),
        source=relative_source,
        line=line,
        test_command=_test_command(fixture, relative_source),
    )
    run = (runner or (lambda argv: run_mutator(argv, timeout=timeout)))(command)

    if run.exit_code == 2:
        raise AssertionError(f"mutator baseline failed for {relative_source}:\n{run.output}")
    if run.exit_code == 1:
        raise AssertionError(f"mutator rejected the command {command}:\n{run.output}")

    wanted = _normalise_mutation(mutation)
    matching = [
        outcome
        for outcome in parse_mutator_output(run.output)
        if outcome.line == line
        and _normalise_mutation(outcome.mutation) == wanted
        and Path(outcome.path).name == source.name
    ]
    if any(outcome.killed for outcome in matching):
        return matching[0]
    if matching:
        raise AssertionError(
            f"mutant {mutation!r} at {relative_source}:{line} survived — "
            f"no test pins that operator"
        )
    raise AssertionError(
        f"no mutant ran for {mutation!r} at {relative_source}:{line}; "
        f"mutator reported:\n{run.output}"
    )
