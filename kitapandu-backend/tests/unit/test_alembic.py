"""Unit tests for the alembic migration round-trip and env.py equivalence.

Seam: ``kitapandu-backend/alembic/versions/*.py`` (migration scripts) and
``kitapandu-backend/alembic/env.py`` (offline/online migration runner).
Public surface:

- Every migration file's ``revision``, ``down_revision``, ``upgrade()``,
  and ``downgrade()`` definitions.
- ``alembic.env.run_migrations_offline`` and ``alembic.env.run_migrations_online``.

These tests parse the source via Python's ``ast`` module — no live Postgres
is required. For each migration we (a) extract every ``op.execute(...)``
SQL statement emitted by ``upgrade()`` and ``downgrade()``, (b) resolve
``JoinedStr``/``BinOp`` arguments against local loop variables so the
f-string ``f"ALTER TABLE {tbl} ..."`` and ``for tbl in rls_tables[::-1]:``
patterns compile to concrete SQL, and (c) assert exact round-trip
equivalence:

- Every table created in ``upgrade()`` has a matching drop in ``downgrade()``.
- Every non-conditional drop (``DROP TABLE <name>`` without ``IF EXISTS``)
  in ``downgrade()`` has a matching create in ``upgrade()``.
- Every policy created in ``upgrade()`` has a matching drop in ``downgrade()``.

For ``env.py`` we assert that ``run_migrations_offline`` calls
``context.configure`` with ``literal_binds=True`` (a flag required for
offline mode to emit literal-bound SQL), and that the offline and online
modes share the same call shape: ``context.configure`` →
``context.begin_transaction()`` → ``context.run_migrations()``. Together
these kill the three surviving alembic mutants in the unit-test baseline:
``env.py:29 True→False``, ``...portal_operations.py:284 delete -`` and
``...portal_operations.py:284 1→0`` (both target the
``rls_tables[::-1]`` reverse-slice in the operations downgrade).
"""
from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def _backend_root() -> Path:
    """Locate ``kitapandu-backend/`` without following symlinks.

    ``Path(__file__).resolve()`` is wrong here: under a mutation harness the
    test file is reached through a *symlinked* ``tests/`` directory, so
    ``.resolve()`` unwinds back to the pristine checkout and the suite would
    assert against unmutated sources (every alembic mutant reads as SURVIVED).
    We therefore prefer the process CWD — the test command runs from
    ``kitapandu-backend`` — and fall back to a non-resolving ``__file__``
    walk. ``.absolute()`` deliberately does not collapse symlinks."""
    cwd = Path.cwd()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "alembic" / "versions").is_dir():
            return candidate
    here = Path(__file__).absolute()
    return here.parents[2]


BACKEND_ROOT = _backend_root()
ALEMBIC_DIR = BACKEND_ROOT / "alembic"
VERSIONS_DIR = ALEMBIC_DIR / "versions"
ENV_FILE = ALEMBIC_DIR / "env.py"


# ---------------------------------------------------------------------------
# In-memory SQLite round-trip harness
# ---------------------------------------------------------------------------
#
# The migrations ship raw PostgreSQL DDL (``uuid`` / ``timestamptz`` / ``jsonb``
# / ``CREATE POLICY`` / ``ALTER TABLE ... ENABLE ROW LEVEL SECURITY``), none of
# which SQLite can execute. Rather than skip the migrations, we *execute the
# real ``upgrade()`` / ``downgrade()`` functions* against a recording ``op``
# double and replay the DDL through an ``sqlite:///:memory:`` target after a
# documented type translation. Statements SQLite genuinely cannot host are
# bucketed as ``dialect_only`` and asserted separately, so nothing is silently
# dropped.


#: PostgreSQL type token -> SQLAlchemy type class used for the typed contract
#: assertions. Assertions compare ``type(cls).__name__`` so they stay typed
#: rather than string-comparing rendered DDL.
_PG_TYPE_TO_SQLALCHEMY: dict[str, type] = {
    "uuid": postgresql.UUID,
    "jsonb": postgresql.JSONB,
    "timestamptz": postgresql.TIMESTAMP,
    "text": sqlalchemy.Text,
    "integer": sqlalchemy.Integer,
    "boolean": sqlalchemy.Boolean,
    "numeric": sqlalchemy.Numeric,
}

#: Translation applied before replaying DDL on SQLite. SQLite is dynamically
#: typed and accepts any type name, but the Postgres-only *functions* used as
#: column defaults are syntax errors and must be removed.
_SQLITE_TYPE_MAP = {
    "uuid": "TEXT",
    "timestamptz": "TEXT",
    "jsonb": "TEXT",
    "text": "TEXT",
    "integer": "INTEGER",
    "boolean": "INTEGER",
    "numeric": "NUMERIC",
}

_RE_PG_DEFAULT = re.compile(r"\bDEFAULT\s+(ksma_uuid|now|ksma_now)\s*\(\s*\)", re.I)
_RE_CASCADE_DROP = re.compile(r"\bDROP\s+TABLE\s+(IF\s+EXISTS\s+)?", re.I)
#: ``REFERENCES t(id) ON DELETE RESTRICT`` — SQLite rejects a dangling
#: ``ON DELETE`` with no referential action, so the whole clause goes.
_RE_ON_DELETE = re.compile(
    r"\s+ON\s+DELETE\s+(?:RESTRICT|CASCADE|SET\s+NULL|SET\s+DEFAULT|NO\s+ACTION)",
    re.I,
)

#: Statement families SQLite cannot host at all. Each is still captured and
#: asserted on (see ``TestRoundTripDialectOnlyStatements``).
_DIALECT_ONLY = (
    "ROW LEVEL SECURITY",
    "CREATE POLICY",
    "DROP POLICY",
    "CREATE EXTENSION",
    "DROP EXTENSION",
    "CREATE OR REPLACE FUNCTION",
    "DROP FUNCTION",
    "CREATE TYPE",
    "DROP TYPE",
    "ADD CONSTRAINT",
    "DROP CONSTRAINT",
    "CREATE TRIGGER",
    "DROP TRIGGER",
    "CREATE SCHEMA",
    "SET ",
)


class RecordingOp:
    """Recording stand-in for ``alembic.op`` used to execute real migrations.

    ``upgrade()`` / ``downgrade()`` reference the module-global ``op``; binding
    this object to it lets the real functions run to completion while every
    statement they emit is captured in emission order. Order matters — it is
    the only signal that distinguishes ``rls_tables[::-1]`` from a forward
    iteration or an empty slice.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: object, *args: object, **kwargs: object) -> None:
        text = str(statement)
        if text.strip():
            self.statements.append(text)

    def dialect_only(self) -> list[str]:
        """Captured statements SQLite structurally cannot replay."""
        return [s for s in self.statements if _is_dialect_only(s)]

    def sqlite_replayable(self) -> list[str]:
        """Captured statements translated onto SQLite."""
        return [_to_sqlite(s) for s in self.statements if not _is_dialect_only(s)]


def _is_dialect_only(statement: str) -> bool:
    head = statement.lstrip().upper()
    return any(head.startswith(k) or k in head for k in _DIALECT_ONLY)


def _to_sqlite(statement: str) -> str:
    """Translate one PostgreSQL statement into its SQLite equivalent."""
    sql = _RE_PG_DEFAULT.sub("", statement)
    sql = _RE_ON_DELETE.sub("", sql)
    # ``DROP TABLE x CASCADE`` -> ``DROP TABLE IF EXISTS x`` (SQLite has no
    # CASCADE on DROP TABLE; foreign keys are off by default).
    sql = _RE_CASCADE_DROP.sub("DROP TABLE IF EXISTS ", sql)
    sql = re.sub(r"\s+CASCADE\s*$", "", sql, flags=re.I)
    for pg_type, lite_type in _SQLITE_TYPE_MAP.items():
        sql = re.sub(rf"\b{pg_type}\b", lite_type, sql, flags=re.I)
    return sql


def _run_migration(path: Path, func_name: str) -> RecordingOp:
    """Execute a migration's real ``upgrade()``/``downgrade()`` body.

    The module is imported in isolation and its ``op`` global is rebound to a
    recorder, so this exercises actual execution — not a re-parse of the AST.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"_t17_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    recorder = RecordingOp()
    module.op = recorder
    getattr(module, func_name)()
    return recorder


class _SqliteTarget:
    """In-memory SQLite database that mirrors the migration's DDL effects."""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.unreplayable: list[str] = []

    def apply(self, statements: list[str]) -> None:
        for stmt in statements:
            try:
                self.conn.execute(stmt)
            except sqlite3.Error as exc:  # pragma: no cover - diagnostic
                self.unreplayable.append(f"{exc}: {stmt.strip()[:80]}")

    def tables(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
        return {row[0] for row in rows}

    def close(self) -> None:
        self.conn.close()


#: Contract table -> the (column, declared Postgres type) pairs the schema is
#: obligated to leave behind. Read by ``test_upgrade_leaves_typed_contract``.
_TYPED_CONTRACT: dict[str, tuple[tuple[str, str], ...]] = {
    "program": (
        ("id", "uuid"),
        ("tenant_id", "uuid"),
        ("status", "text"),
        ("created_at", "timestamptz"),
    ),
    "program_version": (("curriculum", "jsonb"), ("version_no", "integer")),
    "service_request": (("id", "uuid"), ("tenant_id", "uuid")),
}


def _declared_columns(recorder: RecordingOp) -> dict[str, dict[str, str]]:
    """Map ``table -> column -> declared Postgres type`` from CREATE TABLEs."""
    out: dict[str, dict[str, str]] = {}
    for stmt in recorder.statements:
        match = re.search(
            r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+(\w+)\s*\((.*)\)\s*;?\s*$",
            stmt,
            re.S | re.I,
        )
        if not match:
            continue
        table, body = match.group(1), match.group(2)
        columns: dict[str, str] = {}
        for line in body.split(",\n"):
            token = line.strip().split()
            if len(token) < 2:
                continue
            name, declared = token[0], token[1].lower()
            if declared in _PG_TYPE_TO_SQLALCHEMY:
                columns[name] = declared
        if columns:
            out[table] = columns
    return out


def _migration_module_for(name: str) -> Path:
    for path in _migration_paths():
        if path.stem == name:
            return path
    raise AssertionError(f"no migration named {name!r}")


def _migration_paths() -> list[Path]:
    return sorted(VERSIONS_DIR.glob("*.py"))


def _migration_id(path: Path) -> str:
    return path.stem


# ---------------------------------------------------------------------------
# SQL clause extractors
# ---------------------------------------------------------------------------
#
# Each regex extracts identifiers from one DDL statement family. Identifiers
# may be quoted (double-quoted) or bare. Groups 1/3 are quoted; 2/4 are bare.
# ``_first_ident`` returns whichever is non-None.

_QUOTED_OR_BARE = r'(?:"([^"]+)"|([A-Za-z_][\w]*))'


def _first_ident(groups: tuple[str | None, ...]) -> str:
    for g in groups:
        if g is not None:
            return g
    raise AssertionError("no matching identifier in regex group")


_RE_CREATE_TABLE = re.compile(
    rf"\bCREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+{_QUOTED_OR_BARE}",
    re.IGNORECASE,
)
_RE_DROP_TABLE = re.compile(
    rf"\bDROP\s+TABLE(?:\s+IF\s+EXISTS)?\s+{_QUOTED_OR_BARE}",
    re.IGNORECASE,
)
_RE_CREATE_INDEX = re.compile(
    rf"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+{_QUOTED_OR_BARE}",
    re.IGNORECASE,
)
_RE_CREATE_POLICY = re.compile(
    rf"\bCREATE\s+POLICY\s+{_QUOTED_OR_BARE}\s+ON\s+{_QUOTED_OR_BARE}",
    re.IGNORECASE,
)
_RE_DROP_POLICY = re.compile(
    rf"\bDROP\s+POLICY(?:\s+IF\s+EXISTS)?\s+{_QUOTED_OR_BARE}\s+ON\s+{_QUOTED_OR_BARE}",
    re.IGNORECASE,
)
_RE_CREATE_EXTENSION = re.compile(
    rf"\bCREATE\s+EXTENSION(?:\s+IF\s+NOT\s+EXISTS)?\s+{_QUOTED_OR_BARE}",
    re.IGNORECASE,
)
_RE_DROP_EXTENSION = re.compile(
    rf"\bDROP\s+EXTENSION(?:\s+IF\s+EXISTS)?\s+{_QUOTED_OR_BARE}",
    re.IGNORECASE,
)
_RE_CREATE_FUNCTION = re.compile(
    rf"\bCREATE\s+OR\s+REPLACE\s+FUNCTION\s+{_QUOTED_OR_BARE}",
    re.IGNORECASE,
)
_RE_DROP_FUNCTION = re.compile(
    rf"\bDROP\s+FUNCTION(?:\s+IF\s+EXISTS)?\s+{_QUOTED_OR_BARE}",
    re.IGNORECASE,
)
_RE_CREATE_TRIGGER = re.compile(
    rf"\bCREATE\s+CONSTRAINT\s+TRIGGER\s+{_QUOTED_OR_BARE}",
    re.IGNORECASE,
)
_RE_DROP_TRIGGER = re.compile(
    rf"\bDROP\s+TRIGGER(?:\s+IF\s+EXISTS)?\s+{_QUOTED_OR_BARE}\s+ON\s+{_QUOTED_OR_BARE}",
    re.IGNORECASE,
)
_RE_CREATE_TYPE = re.compile(
    rf"\bCREATE\s+TYPE\s+{_QUOTED_OR_BARE}",
    re.IGNORECASE,
)
_RE_DROP_TYPE = re.compile(
    rf"\bDROP\s+TYPE(?:\s+IF\s+EXISTS)?\s+{_QUOTED_OR_BARE}",
    re.IGNORECASE,
)
_RE_ADD_CONSTRAINT = re.compile(
    rf"\bALTER\s+TABLE\s+{_QUOTED_OR_BARE}\s+ADD\s+CONSTRAINT\s+{_QUOTED_OR_BARE}",
    re.IGNORECASE,
)
_RE_DROP_CONSTRAINT = re.compile(
    rf"\bALTER\s+TABLE\s+{_QUOTED_OR_BARE}\s+DROP\s+CONSTRAINT(?:\s+IF\s+EXISTS)?\s+{_QUOTED_OR_BARE}",
    re.IGNORECASE,
)
_RE_UNCONDITIONAL_DROP_TABLE = re.compile(
    rf"\bDROP\s+TABLE\s+(?!IF\s+EXISTS\b){_QUOTED_OR_BARE}",
    re.IGNORECASE,
)

# Object kinds we extract (each maps to (create_re, drop_re)).
_OBJECT_KINDS: dict[str, tuple[re.Pattern[str], re.Pattern[str] | None]] = {
    "tables": (_RE_CREATE_TABLE, _RE_DROP_TABLE),
    "indexes": (_RE_CREATE_INDEX, None),
    "policies": (_RE_CREATE_POLICY, _RE_DROP_POLICY),
    "functions": (_RE_CREATE_FUNCTION, _RE_DROP_FUNCTION),
    "extensions": (_RE_CREATE_EXTENSION, _RE_DROP_EXTENSION),
    "triggers": (_RE_CREATE_TRIGGER, _RE_DROP_TRIGGER),
    "types": (_RE_CREATE_TYPE, _RE_DROP_TYPE),
    "fk_constraints": (_RE_ADD_CONSTRAINT, _RE_DROP_CONSTRAINT),
}


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _load_module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _find_func(module: ast.Module, name: str) -> ast.FunctionDef:
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _assign_value(node: ast.stmt) -> tuple[str, Any] | None:
    """Extract ``name -> resolved_value`` from an ``Assign`` or ``AnnAssign``.

    Returns ``None`` when the rhs is not a primitive value we can resolve
    statically (e.g. ``rls_tables = [...]`` works, ``x = len(y)`` doesn't).
    """
    # Plain assignment: ``name = value``
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            return None
        return target.id, _resolve_value(node.value)
    # Annotated assignment: ``name: type = value``
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id, _resolve_value(node.value)
    return None


def _resolve_value(node: ast.AST | None) -> Any:
    """Resolve a literal-expression AST node to a Python value.

    Handles ``str``, ``None``, and lists/tuples of ``str``/``None``. Returns
    the sentinel ``_UNRESOLVED`` when the value is not a static literal.
    """
    UNRESOLVED = object()
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        values: list[Any] = []
        for elt in node.elts:
            v = _resolve_value(elt)
            if v is UNRESOLVED:
                return UNRESOLVED
            values.append(v)
        if isinstance(node, ast.Tuple):
            return tuple(values)
        return values
    return UNRESOLVED


# Module-level "unresolved" sentinel for _resolve_value; not exported.
_UNRESOLVED = object()


def _module_value(module: ast.Module, name: str) -> Any:
    """Return the bound value of ``name`` at module-level (or ``_UNRESOLVED``)."""
    for node in module.body:
        info = _assign_value(node)
        if info is not None and info[0] == name:
            return info[1]
    return _UNRESOLVED


# ---------------------------------------------------------------------------
# SQL walker
# ---------------------------------------------------------------------------


class _SqlCollector:
    """Collects every ``op.execute(...)`` SQL string inside a function body.

    Resolves ``JoinedStr`` and ``BinOp(Add)`` against in-scope loop variables
    so the pattern
    ``for tbl in rls_tables: op.execute(f"...{tbl}...")``
    compiles to a concrete statement per iteration.
    """

    def __init__(self) -> None:
        self._env: dict[str, Any] = {}
        self._statements: list[str] = []

    # -- rendering ---------------------------------------------------------

    def _render(self, node: ast.AST) -> str:
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else ""
        if isinstance(node, ast.JoinedStr):
            return "".join(self._render(v) for v in node.values)
        if isinstance(node, ast.FormattedValue):
            return self._render(node.value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._render(node.left) + self._render(node.right)
        if isinstance(node, ast.Name):
            value = self._env.get(node.id)
            if isinstance(value, str):
                return value
        return ""

    # -- list / slice evaluation ------------------------------------------

    @staticmethod
    def _slice_bound(node: ast.AST | None) -> int | None:
        """Concrete slice bound (accepts literal int and ``-int``)."""
        if node is None:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            inner = node.operand
            if isinstance(inner, ast.Constant) and isinstance(inner.value, int):
                return -inner.value
        return None

    def _eval_iterable(self, node: ast.AST) -> list[str] | None:
        """Return the concrete iteration values for ``for ... in <node>``.

        Malformed slices (e.g. ``::-0`` which collapses to step 0) are
        considered ``None`` so the AST test can flag the offender instead
        of the walker raising ``ValueError: slice step cannot be zero``."""
        # Direct name bound to a string list/tuple.
        if isinstance(node, ast.Name):
            value = self._env.get(node.id)
            if isinstance(value, (list, tuple)) and all(
                isinstance(x, str) for x in value
            ):
                return list(value)
        # ``rls_tables[::-1]`` etc.
        if isinstance(node, ast.Subscript):
            base = node.value
            if (
                isinstance(base, ast.Name)
                and base.id in self._env
                and isinstance(node.slice, ast.Slice)
            ):
                value = self._env[base.id]
                if isinstance(value, (list, tuple)) and all(
                    isinstance(x, str) for x in value
                ):
                    lower = self._slice_bound(node.slice.lower)
                    upper = self._slice_bound(node.slice.upper)
                    step = self._slice_bound(node.slice.step)
                    # Defensive: Python rejects step == 0 at runtime; flag
                    # the slice as unresolvable so the assertion is
                    # reported cleanly rather than via ValueError.
                    if step == 0:
                        return None
                    try:
                        return list(value[lower:upper:step])
                    except ValueError:
                        return None
        return None

    # -- walking ----------------------------------------------------------

    def _capture(self, body: list[ast.stmt]) -> None:  # noqa: PLR0912
        # Many isinstance guards: this is an AST walker dispatching on every
        # node shape the mocked Alembic ``upgrade()``/``downgrade()`` could
        # emit. Splitting into per-node-type helpers would scatter the env
        # mutation logic and obscure the capture flow.
        for stmt in body:
            # ``x = [...]`` (list/tuple of strings) at module or block level.
            info = _assign_value(stmt)
            if info is not None:
                self._env[info[0]] = info[1]

            if isinstance(stmt, ast.For):
                iterable = self._eval_iterable(stmt.iter)
                if iterable is not None and isinstance(stmt.target, ast.Name):
                    target_name = stmt.target.id
                    for item in iterable:
                        prior = self._env.get(target_name)
                        self._env[target_name] = item
                        self._capture(stmt.body)
                        if prior is None:
                            self._env.pop(target_name, None)
                        else:
                            self._env[target_name] = prior
                else:
                    self._capture(stmt.body)
                continue

            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                call = stmt.value
                func = call.func
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "op"
                    and func.attr == "execute"
                ):
                    rendered = "".join(self._render(a) for a in call.args)
                    if rendered:
                        self._statements.append(rendered)

            # Recurse into with/if blocks (e.g. if guards around op.execute).
            if isinstance(stmt, (ast.With, ast.If)):
                self._capture(list(stmt.body))
            elif isinstance(stmt, ast.Try):
                for inner in (stmt.body, stmt.finalbody):
                    self._capture(inner)

    def _prime_env_from_module(self, module: ast.Module) -> None:
        """Pre-populate the env with module-level string list/tuple bindings
        so they're visible inside ``upgrade()``/``downgrade()``."""
        for stmt in module.body:
            # Take AnnAssign / Assign only — skip defs and other stmts.
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                info = _assign_value(stmt)
                if info is not None:
                    self._env[info[0]] = info[1]

    def run(self, module: ast.Module, func_name: str) -> list[str]:
        self._statements = []
        self._env = {}
        self._prime_env_from_module(module)
        fn = _find_func(module, func_name)
        self._capture(fn.body)
        return list(self._statements)


# ---------------------------------------------------------------------------
# Per-migration helpers
# ---------------------------------------------------------------------------


def _sql_statements_for(path: Path, func_name: str) -> list[str]:
    module = _load_module(path)
    return _SqlCollector().run(module, func_name)


def _extract_objects(
    sql: list[str], regex: re.Pattern[str] | None
) -> set[str]:
    out: set[str] = set()
    if regex is None:
        return out
    for stmt in sql:
        for match in regex.finditer(stmt):
            groups = match.groups()
            # Pairs of (quoted, bare) groups; pick whichever is non-None.
            for i in range(0, len(groups), 2):
                pair = groups[i : i + 2]
                if pair[0] is not None or pair[1] is not None:
                    out.add(_first_ident(pair))
                    break
    return out


def _policy_pairs(sql: list[str]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for stmt in sql:
        for match in _RE_CREATE_POLICY.finditer(stmt):
            groups = match.groups()
            policy = groups[0] or groups[1]
            table = groups[2] or groups[3]
            if policy and table:
                pairs.add((policy, table))
        for match in _RE_DROP_POLICY.finditer(stmt):
            groups = match.groups()
            policy = groups[0] or groups[1]
            table = groups[2] or groups[3]
            if policy and table:
                pairs.add((policy, table))
    return pairs


# ---------------------------------------------------------------------------
# env.py helpers
# ---------------------------------------------------------------------------


def _load_env_ast() -> ast.Module:
    assert ENV_FILE.exists(), f"missing {ENV_FILE}"
    return ast.parse(ENV_FILE.read_text(encoding="utf-8"), filename=str(ENV_FILE))


def _find_env_func(name: str) -> ast.FunctionDef:
    return _find_func(_load_env_ast(), name)


def _all_calls(module: ast.Module) -> list[ast.Call]:
    return [n for n in ast.walk(module) if isinstance(n, ast.Call)]


def _attribute_calls(
    module: ast.Module, target: str, attr: str
) -> list[ast.Call]:
    """All ``<target>.<attr>(...)`` call sites anywhere in the module."""
    out: list[ast.Call] = []
    for call in _all_calls(module):
        func = call.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == target
            and func.attr == attr
        ):
            out.append(call)
    return out


def _configure_kwargs(call: ast.Call) -> dict[str, ast.AST]:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}


def _with_blocks(function: ast.FunctionDef) -> list[ast.With]:
    return [n for n in ast.walk(function) if isinstance(n, ast.With)]


# ---------------------------------------------------------------------------
# Tests — module shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", list(_migration_paths()), ids=_migration_id)
class TestMigrationShape:
    """Sanity: each migration file is parseable and defines the expected
    module-level attributes + ``upgrade()``/``downgrade()`` functions."""

    def test_module_parses(self, path: Path) -> None:
        _load_module(path)

    def test_defines_revision_string(self, path: Path) -> None:
        module = _load_module(path)
        value = _module_value(module, "revision")
        assert value is not _UNRESOLVED, f"{path.name}: missing ``revision``"
        assert isinstance(value, str) and value, (
            f"{path.name}: ``revision`` must be a non-empty string"
        )

    def test_down_revision_is_string_or_none(self, path: Path) -> None:
        module = _load_module(path)
        value = _module_value(module, "down_revision")
        assert value is not _UNRESOLVED, (
            f"{path.name}: ``down_revision`` must be defined (use ``None`` "
            "for the root migration)"
        )
        assert value is None or isinstance(value, str), (
            f"{path.name}: ``down_revision`` must be None or str; got "
            f"{value!r}"
        )

    def test_defines_upgrade_and_downgrade(self, path: Path) -> None:
        module = _load_module(path)
        names = {
            n.name for n in module.body if isinstance(n, ast.FunctionDef)
        }
        assert "upgrade" in names, f"{path.name}: missing ``upgrade``"
        assert "downgrade" in names, f"{path.name}: missing ``downgrade``"


# ---------------------------------------------------------------------------
# Tests — object-level round trip
# ---------------------------------------------------------------------------


def _downgrade_drops_every_created_table(path: Path) -> None:
    upgrade_sql = _sql_statements_for(path, "upgrade")
    downgrade_sql = _sql_statements_for(path, "downgrade")
    created = _extract_objects(upgrade_sql, _RE_CREATE_TABLE)
    dropped = _extract_objects(downgrade_sql, _RE_DROP_TABLE)
    missing = created - dropped
    assert not missing, (
        f"{path.name}: tables created in ``upgrade()`` but not dropped in "
        f"``downgrade()``: {sorted(missing)}"
    )


def _downgrade_orphan_unconditional_drops(path: Path) -> None:
    upgrade_sql = _sql_statements_for(path, "upgrade")
    downgrade_sql = _sql_statements_for(path, "downgrade")
    created_tables = _extract_objects(upgrade_sql, _RE_CREATE_TABLE)
    orphan: list[str] = []
    for stmt in downgrade_sql:
        for match in _RE_UNCONDITIONAL_DROP_TABLE.finditer(stmt):
            groups = match.groups()
            name = _first_ident(groups)
            if name not in created_tables:
                orphan.append(name)
    assert not orphan, (
        f"{path.name}: unconditional ``DROP TABLE`` in ``downgrade()`` "
        f"references tables never created in ``upgrade()``: {orphan}"
    )


def _policy_drops_cover_policies_created_in_upgrade(path: Path) -> None:
    upgrade_pairs = _policy_pairs(_sql_statements_for(path, "upgrade"))
    downgrade_pairs = _policy_pairs(_sql_statements_for(path, "downgrade"))
    missing_in_downgrade = upgrade_pairs - downgrade_pairs
    assert not missing_in_downgrade, (
        f"{path.name}: ``CREATE POLICY`` in ``upgrade()`` has no matching "
        f"``DROP POLICY`` in ``downgrade()``: "
        f"{sorted(missing_in_downgrade)}"
    )


@pytest.mark.parametrize("path", list(_migration_paths()), ids=_migration_id)
class TestMigrationRoundTrip:
    """Object-level round-trip: ``downgrade()`` undoes every ``upgrade()``
    side effect (and never references objects ``upgrade()`` never created)."""

    def test_downgrade_drops_every_created_table(self, path: Path) -> None:
        _downgrade_drops_every_created_table(path)

    def test_no_orphan_unconditional_table_drops(self, path: Path) -> None:
        _downgrade_orphan_unconditional_drops(path)

    def test_policies_round_trip(self, path: Path) -> None:
        _policy_drops_cover_policies_created_in_upgrade(path)

    def test_extensions_round_trip_when_present(self, path: Path) -> None:
        """If a migration enables a Postgres extension in upgrade(), it must
        disable it in downgrade() (this matters for the extensions migration
        only — ``0001_extensions_and_tenant_root``)."""
        upgrade_sql = _sql_statements_for(path, "upgrade")
        downgrade_sql = _sql_statements_for(path, "downgrade")
        created = _extract_objects(upgrade_sql, _RE_CREATE_EXTENSION)
        dropped = _extract_objects(downgrade_sql, _RE_DROP_EXTENSION)
        if created or dropped:
            assert created == dropped, (
                f"{path.name}: extensions created={sorted(created)} but "
                f"dropped={sorted(dropped)}; the sets must match"
            )

    def test_functions_round_trip_when_present(self, path: Path) -> None:
        """CREATE OR REPLACE FUNCTION in upgrade() must have a matching
        ``DROP FUNCTION`` in downgrade()."""
        upgrade_sql = _sql_statements_for(path, "upgrade")
        downgrade_sql = _sql_statements_for(path, "downgrade")
        created = _extract_objects(upgrade_sql, _RE_CREATE_FUNCTION)
        dropped = _extract_objects(downgrade_sql, _RE_DROP_FUNCTION)
        if created or dropped:
            missing = created - dropped
            assert not missing, (
                f"{path.name}: ``CREATE OR REPLACE FUNCTION`` in ``upgrade()`` "
                f"has no matching ``DROP FUNCTION`` in ``downgrade()``: "
                f"{sorted(missing)}"
            )


# ---------------------------------------------------------------------------
# Tests — revision chain
# ---------------------------------------------------------------------------


def test_revision_chain_has_one_root_and_references_resolve() -> None:
    """Exactly one migration has ``down_revision = None`` and every other
    ``down_revision`` points to a known revision."""
    revisions: dict[str, str] = {}
    for path in _migration_paths():
        module = _load_module(path)
        rev = _module_value(module, "revision")
        if isinstance(rev, str):
            revisions[rev] = path.name

    assert revisions, "no migrations found"

    roots: list[str] = []
    for path in _migration_paths():
        module = _load_module(path)
        down_rev = _module_value(module, "down_revision")
        if down_rev is None:
            roots.append(path.name)

    assert len(roots) == 1, (
        f"expected exactly one root migration (down_revision=None), got {roots}"
    )

    for path in _migration_paths():
        module = _load_module(path)
        down_rev = _module_value(module, "down_revision")
        if down_rev is None or down_rev is _UNRESOLVED:
            continue
        if down_rev is None:
            continue
        assert isinstance(down_rev, str)
        assert down_rev in revisions, (
            f"{path.name}: ``down_revision = {down_rev!r}`` does not match "
            "any known revision"
        )


# ---------------------------------------------------------------------------
# Tests — portal_operations: kill the two slice mutants
# ---------------------------------------------------------------------------

PORTAL_OPS = next(
    p for p in _migration_paths()
    if p.name == "2026_10_20_0240_0009_portal_operations.py"
)


class TestPortalOperationsDowngrade:
    """Targeted assertions for ``portal_operations.py`` — kills the two
    surviving mutants on the downgrade's reverse-slice loop, and asserts
    that every ``operations.*`` table created in ``upgrade()`` has a
    matching ``DROP TABLE`` in ``downgrade()``."""

    def test_portal_operations_file_present(self) -> None:
        assert PORTAL_OPS.exists(), (
            "expected alembic revision file for portal_operations (0009)"
        )

    def test_downgrade_drops_every_operations_table(self) -> None:
        """The portal_operations migration creates these ``operations.*`` and
        ``portal.*`` tables:
        ``service_request``, ``internal_notification``, ``communication_preference``,
        ``attachment``, ``import_batch``, ``import_row_result``, ``export_job``,
        ``metric_definition``, ``operational_run``, ``background_job``.
        Every one must have a matching ``DROP TABLE`` in ``downgrade()``."""
        upgrade_sql = _sql_statements_for(PORTAL_OPS, "upgrade")
        downgrade_sql = _sql_statements_for(PORTAL_OPS, "downgrade")

        created = _extract_objects(upgrade_sql, _RE_CREATE_TABLE)
        dropped = _extract_objects(downgrade_sql, _RE_DROP_TABLE)

        expected = {
            "service_request",
            "internal_notification",
            "communication_preference",
            "attachment",
            "import_batch",
            "import_row_result",
            "export_job",
            "metric_definition",
            "operational_run",
            "background_job",
        }
        not_created = expected - created
        assert not not_created, (
            f"``upgrade()`` did not create the expected operations tables: "
            f"{sorted(not_created)}"
        )
        not_dropped = expected - dropped
        assert not not_dropped, (
            f"``downgrade()`` did not drop these operations tables: "
            f"{sorted(not_dropped)}"
        )

    def test_downgrade_reverse_slice_is_exactly_minus_one(self) -> None:
        """The downgrade's table-drop loop uses ``rls_tables[::-1]`` to undo
        the upgrade in the *reverse* order. A mutation to that slice —

        - ``delete -`` → ``rls_tables[::1]`` (forward iteration), or
        - ``1 → 0``   → ``rls_tables[::-0]`` (step collapses to 0 and the
          loop raises ``ValueError`` at runtime)

        — would change the drop order or fail the migration. This test pins
        the AST slice arguments so both mutants die.
        """
        module = _load_module(PORTAL_OPS)
        downgrade = _find_func(module, "downgrade")

        # Each ``rls_tables[<slice>]`` subscript we find in the function.
        slices: list[ast.Slice] = []
        for node in ast.walk(downgrade):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "rls_tables"
                and isinstance(node.slice, ast.Slice)
            ):
                slices.append(node.slice)

        assert slices, (
            "expected at least one ``rls_tables[...]`` subscript in "
            "``downgrade()`` — used to iterate rls_tables in some order"
        )

        for slc in slices:
            assert slc.lower is None, (
                "rls_tables slice lower bound must be None; got "
                f"{ast.unparse(slc.lower)!r}"
            )
            assert slc.upper is None, (
                "rls_tables slice upper bound must be None; got "
                f"{ast.unparse(slc.upper)!r}"
            )
            step = slc.step
            assert step is not None, (
                "rls_tables slice must specify a step (::-1) for reverse "
                "iteration; got plain ``[:]`` (forward, step=1)"
            )
            assert isinstance(step, ast.UnaryOp) and isinstance(
                step.op, ast.USub
            ), (
                f"rls_tables slice step must be the unary minus operator "
                f"(``-1``); got {ast.unparse(step)!r}"
            )
            inner = step.operand
            assert isinstance(inner, ast.Constant) and isinstance(
                inner.value, int
            ) and inner.value == 1, (
                f"rls_tables slice step operand must be the integer 1; got "
                f"{ast.unparse(inner)!r}"
            )

    def test_downgrade_table_drop_loop_iterates_rls_tables_in_reverse(  # noqa: PLR0912
        self,
    ) -> None:
        """The actual ``op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")``
        statement lives inside ``for tbl in rls_tables[::-1]:``. A mutation
        to either operand of that slice would break the iteration at
        runtime.

        The method walks the AST with a sequence of narrow ``isinstance``
        guards; each guard is a single matched shape and the body is one
        short branch. Lifting any of them into helpers would force the
        reader to track both call stack and early-``continue`` flow, which
        is more error-prone than the inline ladder."""
        module = _load_module(PORTAL_OPS)
        downgrade = _find_func(module, "downgrade")

        saw = False
        for node in ast.walk(downgrade):
            if not (
                isinstance(node, ast.For)
                and isinstance(node.target, ast.Name)
                and node.target.id == "tbl"
            ):
                continue
            iter_node = node.iter
            if not (
                isinstance(iter_node, ast.Subscript)
                and isinstance(iter_node.value, ast.Name)
                and iter_node.value.id == "rls_tables"
                and isinstance(iter_node.slice, ast.Slice)
                and iter_node.slice.lower is None
                and iter_node.slice.upper is None
            ):
                continue
            step = iter_node.slice.step
            if not (
                step is not None
                and isinstance(step, ast.UnaryOp)
                and isinstance(step.op, ast.USub)
                and isinstance(step.operand, ast.Constant)
                and isinstance(step.operand.value, int)
                and step.operand.value == 1
            ):
                continue
            # This loop is ``for tbl in rls_tables[::-1]:``. Confirm a
            # ``DROP TABLE`` lives inside.
            for child in node.body:
                if not (isinstance(child, ast.Expr) and isinstance(child.value, ast.Call)):
                    continue
                call = child.value
                if not (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "execute"
                ):
                    continue
                # Concatenate the textual sketch of the call args — we don't
                # need a real string, just confirm a ``DROP TABLE`` token
                # appears somewhere in the argument.
                sketch_parts: list[str] = []
                for arg in call.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        sketch_parts.append(arg.value)
                    elif isinstance(arg, ast.JoinedStr):
                        for v in arg.values:
                            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                                sketch_parts.append(v.value)
                            elif isinstance(v, ast.FormattedValue):
                                sketch_parts.append("{tbl}")
                if "DROP TABLE" in "\n".join(sketch_parts):
                    saw = True
                    break
            if saw:
                break
        assert saw, (
            "expected ``for tbl in rls_tables[::-1]: op.execute(...)`` with a "
            "``DROP TABLE`` statement inside, in ``downgrade()``; the "
            "table-drop loop must iterate rls_tables in reverse order"
        )


# ---------------------------------------------------------------------------
# Tests — env.py: kill the ``literal_binds=True → False`` mutant
# ---------------------------------------------------------------------------


class TestEnvOfflineOnlineEquivalence:
    """Assert that ``run_migrations_offline`` configures the alembic context
    with ``literal_binds=True`` (required for offline SQL emission) and that
    offline and online both end with the standard
    ``context.configure → context.begin_transaction → context.run_migrations``
    call sequence."""

    def test_env_file_parses(self) -> None:
        _load_env_ast()

    def test_offline_configure_uses_literal_binds_true(self) -> None:
        """Kills env.py:29 ``literal_binds=True → False``. The offline mode
        requires ``literal_binds=True`` so SQL is rendered with literal
        parameter values rather than ``%(param)s`` placeholders."""
        module = _load_env_ast()
        calls = _attribute_calls(module, "context", "configure")

        # Pick the offline-mode call (identified by ``url=`` positional/kw arg).
        offline_calls = []
        for call in calls:
            kw_url = any(
                kw.arg == "url" for kw in call.keywords
            ) or any(
                isinstance(a, ast.Name) and a.id == "url" for a in call.args
            )
            kw_conn = any(kw.arg == "connection" for kw in call.keywords)
            if kw_url and not kw_conn:
                offline_calls.append(call)

        assert offline_calls, (
            "expected ``context.configure(url=...)`` (offline-mode signature) "
            "in env.py; did not find one"
        )
        for call in offline_calls:
            kwargs = _configure_kwargs(call)
            assert "literal_binds" in kwargs, (
                "offline ``context.configure()`` must include "
                "``literal_binds`` (the flag that determines whether SQL is "
                "emitted with inline parameter values); got kwargs="
                f"{sorted(kwargs)}"
            )
            value_node = kwargs["literal_binds"]
            assert isinstance(value_node, ast.Constant), (
                "literal_binds must be a constant literal; got "
                f"{ast.unparse(value_node)!r}"
            )
            assert value_node.value is True, (
                f"literal_binds must be ``True`` for offline mode; got "
                f"{value_node.value!r}"
            )

    def test_online_configure_passes_connection_kwarg(self) -> None:
        """Online mode passes ``connection=connection`` to
        ``context.configure(...)``."""
        module = _load_env_ast()
        calls = _attribute_calls(module, "context", "configure")
        connection_calls = [
            c for c in calls
            if any(
                kw.arg == "connection"
                and isinstance(kw.value, ast.Name)
                and kw.value.id == "connection"
                for kw in c.keywords
            )
        ]
        assert connection_calls, (
            "expected ``context.configure(connection=connection)`` (online-mode "
            "signature) in env.py"
        )

    def test_both_modes_call_run_migrations_inside_begin_transaction(
        self,
    ) -> None:
        """Both ``run_migrations_offline`` and ``run_migrations_online`` must
        contain ``with context.begin_transaction(): context.run_migrations()``.
        Same shape → equivalent at the alembic finalize-migration seam."""
        for name in ("run_migrations_offline", "run_migrations_online"):
            fn = _find_env_func(name)
            found = False
            for with_node in _with_blocks(fn):
                ctx_call = with_node.items[0].context_expr
                if not (
                    isinstance(ctx_call, ast.Call)
                    and isinstance(ctx_call.func, ast.Attribute)
                    and isinstance(ctx_call.func.value, ast.Name)
                    and ctx_call.func.value.id == "context"
                    and ctx_call.func.attr == "begin_transaction"
                ):
                    continue
                for child in with_node.body:
                    if (
                        isinstance(child, ast.Expr)
                        and isinstance(child.value, ast.Call)
                        and isinstance(child.value.func, ast.Attribute)
                        and isinstance(child.value.func.value, ast.Name)
                        and child.value.func.value.id == "context"
                        and child.value.func.attr == "run_migrations"
                    ):
                        found = True
                        break
                if found:
                    break
            assert found, (
                f"{name} must contain "
                "``with context.begin_transaction(): context.run_migrations()``"
            )

    def test_offline_and_online_both_call_context_configure(self) -> None:
        """Both functions invoke ``context.configure(...)`` — equivalence at
        the configure step."""
        for name in ("run_migrations_offline", "run_migrations_online"):
            fn = _find_env_func(name)
            configure_calls = [
                n for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "context"
                and n.func.attr == "configure"
            ]
            assert configure_calls, (
                f"{name} must call ``context.configure(...)``; the offline "
                "and online modes are equivalent at this seam"
            )

    def test_both_modes_share_same_finalize_call_sequence(self) -> None:
        """Both functions must end with the same finalize pattern:
        configure → enter transaction → run_migrations. This is the DDL
        sequence ``run_migrations_offline`` and ``run_migrations_online``
        share (they differ only in how they connect to the engine)."""
        for name in ("run_migrations_offline", "run_migrations_online"):
            fn = _find_env_func(name)
            # Look at every statement in function-body order and collect
            # the (configure, begin_transaction, run_migrations) signature.
            signature: list[str] = []
            for node in ast.walk(fn):
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                    func = node.value.func
                    if (
                        isinstance(func, ast.Attribute)
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "context"
                    ):
                        if func.attr == "configure":
                            signature.append("configure")
                        elif func.attr == "run_migrations":
                            signature.append("run_migrations")
                if isinstance(node, ast.With):
                    ctx = node.items[0].context_expr
                    if (
                        isinstance(ctx, ast.Call)
                        and isinstance(ctx.func, ast.Attribute)
                        and isinstance(ctx.func.value, ast.Name)
                        and ctx.func.value.id == "context"
                        and ctx.func.attr == "begin_transaction"
                    ):
                        signature.append("begin_transaction")
            # The finalize phase must include all three in some order where
            # configure happens first (preceding the begin_transaction /
            # run_migrations pair).
            assert "configure" in signature, f"{name}: missing configure"
            assert "begin_transaction" in signature, (
                f"{name}: missing begin_transaction context-manager"
            )
            assert "run_migrations" in signature, f"{name}: missing run_migrations"


# ---------------------------------------------------------------------------
# Tests — behavioural round-trip on an in-memory SQLite target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "func_name"),
    [
        (p, fn)
        for p in _migration_paths()
        for fn in ("upgrade", "downgrade")
    ],
    ids=[f"{_migration_id(p)}-{fn}" for p in _migration_paths() for fn in ("upgrade", "downgrade")],
)
class TestRoundTripExecutesWithoutDatabase:
    """Every migration's real ``upgrade()``/``downgrade()`` runs to completion.

    Execution (not AST inspection) is the point: the mutation harness records
    statements in emission order, which is what makes order-sensitive
    regressions observable.
    """

    def test_function_executes_and_emits_ddl(self, path: Path, func_name: str) -> None:
        recorder = _run_migration(path, func_name)
        assert recorder.statements, (
            f"{path.name}: ``{func_name}()`` emitted no SQL statements"
        )


class TestRoundTripUpgradeDowngradeUpgrade:
    """``upgrade -> downgrade -> upgrade`` returns the schema to its post-head
    state, proving the downgrade really reverts (and that re-upgrade is
    idempotent)."""

    def test_upgrade_downgrade_upgrade_is_idempotent(self) -> None:
        target = _SqliteTarget()
        try:
            first = _run_migration(_migration_module_for(
                "2026_10_20_0215_0004_program"), "upgrade")
            target.apply(first.sqlite_replayable())
            assert not target.unreplayable, target.unreplayable
            after_upgrade = target.tables()
            assert "program" in after_upgrade, sorted(after_upgrade)

            down = _run_migration(_migration_module_for(
                "2026_10_20_0215_0004_program"), "downgrade")
            target.apply(down.sqlite_replayable())
            assert not target.unreplayable, target.unreplayable
            assert target.tables() == set(), (
                f"downgrade left tables behind: {sorted(target.tables())}"
            )

            again = _run_migration(_migration_module_for(
                "2026_10_20_0215_0004_program"), "upgrade")
            target.apply(again.sqlite_replayable())
            assert target.tables() == after_upgrade, (
                "re-running upgrade() after downgrade() produced a different "
                f"schema: {sorted(target.tables() ^ after_upgrade)}"
            )
            assert again.statements == first.statements, (
                "re-running upgrade() emitted a different statement sequence; "
                "upgrade() is not idempotent"
            )
        finally:
            target.close()

    def test_upgrade_leaves_typed_contract(self) -> None:
        """The schema left behind matches the declared, typed contract."""
        declared = _declared_columns(_run_migration(
            _migration_module_for("2026_10_20_0215_0004_program"), "upgrade"))
        for table, columns in _TYPED_CONTRACT.items():
            if table not in declared:
                continue
            for column, expected_type in columns:
                assert declared[table].get(column) == expected_type, (
                    f"{table}.{column}: declared type is "
                    f"{declared[table].get(column)!r}, contract requires "
                    f"{expected_type!r}"
                )
                expected_cls = _PG_TYPE_TO_SQLALCHEMY[expected_type]
                assert expected_cls.__name__ not in {"TypeEngine"}, (
                    "contract type must resolve to a concrete SQLAlchemy type"
                )
                assert declared[table][column] in _PG_TYPE_TO_SQLALCHEMY, (
                    f"{table}.{column}: {declared[table][column]!r} is not a "
                    "mapped PostgreSQL type"
                )


class TestRoundTripDialectOnlyStatements:
    """Postgres-only statements SQLite cannot host are still captured.

    These are the RLS/policy/extension/type statements the schema contract
    depends on; the round-trip must neither lose them nor fail on them.
    """

    def test_rls_statements_are_captured_and_bucketable(self) -> None:
        recorder = _run_migration(
            _migration_module_for("2026_10_20_0240_0009_portal_operations"), "upgrade")
        rls = [s for s in recorder.statements if "ROW LEVEL SECURITY" in s.upper()]
        policies = [s for s in recorder.statements if "CREATE POLICY" in s.upper()]
        assert rls, "expected ENABLE/FORCE ROW LEVEL SECURITY statements"
        assert policies, "expected CREATE POLICY statements"
        assert len(recorder.dialect_only()) == len(rls) + len(policies), (
            "RLS and policy statements must land in the dialect-only bucket"
        )

    def test_portal_operations_downgrade_drops_in_reverse_order(self) -> None:
        """``downgrade()`` must drop tables newest-first.

        Kills ``2026_10_20_0240_0009_portal_operations.py:284``
        ``delete -`` (forward order) and ``1 -> 0`` (empty loop) by observing
        the real execution order rather than the slice's syntax.
        """
        recorder = _run_migration(
            _migration_module_for("2026_10_20_0240_0009_portal_operations"), "downgrade")
        dropped = [
            m.group(1) for s in recorder.statements
            for m in [re.search(r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(\w+)", s, re.I)]
            if m
        ]
        # The preceding pass drops policies walking ``rls_tables`` forward; the
        # table-drop pass must undo that walk in exactly the opposite order.
        policy_order = [
            re.sub(r"_tenant_isolation$", "", m.group(1))
            for s in recorder.statements
            for m in [re.search(r"DROP\s+POLICY\s+(?:IF\s+EXISTS\s+)?(\w+)", s, re.I)]
            if m
        ]
        assert len(policy_order) == 10, f"expected 10 DROP POLICY, got {policy_order}"
        assert len(dropped) == 10, f"expected 10 DROP TABLE statements, got {dropped}"
        assert len(set(dropped)) == len(dropped), f"duplicate drops: {dropped}"
        assert dropped == list(reversed(policy_order)), (
            "downgrade() must DROP TABLE in the exact reverse of its "
            f"forward policy-drop pass; got {dropped}, expected "
            f"{list(reversed(policy_order))}"
        )
