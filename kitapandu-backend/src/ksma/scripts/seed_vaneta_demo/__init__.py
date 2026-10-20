"""Idempotent dev-DB seed for the KSMA pilot.

Run::

    python -m ksma.scripts.seed_vaneta_demo

Populates ``localhost:5432/ksma`` with the full pilot demo state:

- 1 bootstrap ``seed-bootstrap`` ``ServicePrincipal`` (used as ``created_by``
  for every seeded row),
- 2 tenants: ``Vaneta DEMO`` (``Vaneta Maju Berdaya (DEMO)``, slug
  ``vaneta``) and ``Lembaga Uji DEMO`` (``Lembaga Uji Demo (PENGGANTI)``,
  slug ``lembaga-uji``),
- 1 ``EducationUnit`` per tenant; Vaneta's unit_code is ``VANETA-UNIT-A``,
  Lembaga's is ``LEMBAGA-UNIT-A``; both ``lk_lkp`` with education level
  ``['TK']``,
- 13 demo ``Account`` rows per tenant (one per role code in
  ``op/am/ow/ac/fi/in/as/ap/au/st/gu/py/sv``), Argon2id-hashed password
  ``Demo!2345``,
- 1 ``Membership`` per (tenant, account),
- 1 published ``PolicyVersion`` per domain (access, finance, enrollment,
  session, assessment, certificate),
- 1 ``AccessAssignment`` per membership with the role's default action set,
- For Vaneta only: 6 ``Participant`` rows ``P-0001…P-0006`` (incl. P-0001
  and P-0006 with a shared family contact so the duplicate-suspect V02
  heuristic lights up at search time), 1 ``Program`` + 1 published
  ``ProgramVersion``, 1 ``Batch`` ``EN-A1-Okt2026`` capacity 10
  ``delivery_mode='onsite'`` opened, 3 ``Session`` rows scheduled at
  now+7d/14d/21d, 2 issued ``Invoice`` rows
  ``INV-VANETA-0001``/``INV-VANETA-0002``, 1 ``PaymentSubmission`` → 1
  ``VerifiedPayment`` → 1 ``PaymentAllocation``, 1 ``Attendance`` row,
- 1 ``OperationalRun`` of ``operation_kind='seed'`` that increments
  ``attempt_count`` on every re-run (DB CHECK constraint is widened at seed
  time to accept 'seed').

Idempotency: every row is looked up by natural key first; only inserted if
absent. Re-running the script leaves the row counts unchanged.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ksma.batch.application import (
    create_application,
    create_batch,
    decide_application,
    open_batch,
    place_enrollment,
    submit_application,
)
from ksma.batch.domain import DeliveryMode
from ksma.finance.application import (
    allocate_funds,
    create_invoice,
    issue_invoice,
    submit_payment,
    verify_payment,
)
from ksma.identity.application import (
    activate_account,
    activate_membership,
    activate_tenant,
    create_account,
    create_education_unit,
    create_tenant,
    grant_access,
    invite_membership,
    publish_policy,
)
from ksma.identity.domain import TenantServiceStatus
from ksma.identity.infrastructure.models import ServicePrincipalRow  # noqa: F401  (kept for editor jumps; SP is inserted via raw SQL)
from ksma.participant.application import (
    CreateParticipantCommand,
    create_participant,
)
from ksma.program.application import (
    create_program,
    create_program_version,
    publish_program_version,
)
from ksma.program.domain import Curriculum
from ksma.session.application import (
    add_revision,
    create_session,
    record_attendance,
)
from ksma.session.domain import AttendanceCode
from ksma.shared.persistence import (
    session_scope,
    set_current_tenant,
)
from ksma.shared.rbac import ROLE_DEFAULT_ACTIONS, RoleCode
from ksma.shared.tenancy import ActorKind, TenantContext


# ---------------------------------------------------------------------------
# Constants — what we seed
# ---------------------------------------------------------------------------

DEMO_PASSWORD = "Demo!2345"  # Argon2id-hashed via `create_account`

BOOTSTRAP_SP_CODE = "seed-bootstrap"
BOOTSTRAP_SP_ENV = "development"
BOOTSTRAP_SP_PURPOSE = "KSMA dev seed"


@dataclass(frozen=True, slots=True)
class TenantCfg:
    code: str              # short identifier (VAN, LMB)
    slug: str              # email/inbox subdomain (no dots, no special chars)
    display_name: str      # tenant.display_name
    unit_code: str         # education_unit.unit_code (UNIQUE per tenant)
    institution_type: str  # education_unit.institution_type
    seed_vaneta_data: bool  # whether to seed the Vaneta-only program + finance stack


TENANT_CFGS: tuple[TenantCfg, ...] = (
    TenantCfg(
        code="VAN",
        slug="vaneta",
        display_name="Vaneta Maju Berdaya (DEMO)",
        unit_code="VANETA-UNIT-A",
        institution_type="lk_lkp",
        seed_vaneta_data=True,
    ),
    TenantCfg(
        code="LMB",
        slug="lembaga-uji",
        display_name="Lembaga Uji Demo (PENGGANTI)",
        unit_code="LEMBAGA-UNIT-A",
        institution_type="lk_lkp",
        seed_vaneta_data=False,
    ),
)


# Per ticket P3.1: 12-13 role codes; we seed a demo account for each so the
# pilot login matrix is complete. (Ticket says 11 + lists 12; we accept both
# the 13-role matrix in the RBAC module + the ticket's spelled-out list.)
DEMO_ROLE_CODES: tuple[str, ...] = (
    "op", "am", "ow", "ac", "fi", "in",
    "as", "ap", "au", "st", "gu", "py", "sv",
)

POLICY_DOMAINS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("access", {"version": "pilot-v1", "grants": "default"}),
    ("finance", {
        "version": "pilot-v1",
        "currency": "IDR",
        "rounding": "ROUND_HALF_EVEN",
        "default_installment_count": 1,
    }),
    ("enrollment", {"version": "pilot-v1", "pause_supported": True}),
    ("session", {
        "version": "pilot-v1",
        "attendance_required_for_closure": True,
    }),
    ("assessment", {"version": "pilot-v1", "rubric_required": True}),
    ("certificate", {"version": "pilot-v1", "numbering_scope": "default"}),
)


def _vaneta_participants() -> list[dict[str, Any]]:
    """Demo participants. P-0001 and P-0006 share a family email so the
    duplicate-suspect (V02) heuristic lights up at search time.
    """
    shared_email = "keluarga.sari@vaneta.id"
    return [
        {
            "participant_no": "P-0001",
            "full_name": "Sari Wulandari",
            "birth_date": date(2018, 4, 12),
            "contact": {"email": shared_email, "phone": "+62-811-0001-0001"},
        },
        {
            "participant_no": "P-0002",
            "full_name": "Budi Santoso",
            "birth_date": date(2017, 9, 30),
            "contact": {"email": "ortu.budi@vaneta.id", "phone": "+62-811-0002-0002"},
        },
        {
            "participant_no": "P-0003",
            "full_name": "Dewi Lestari",
            "birth_date": date(2018, 1, 22),
            "contact": {"email": "ortu.dewi@vaneta.id", "phone": "+62-811-0003-0003"},
        },
        {
            "participant_no": "P-0004",
            "full_name": "Andi Pratama",
            "birth_date": date(2017, 6, 8),
            "contact": {"email": "ortu.andi@vaneta.id", "phone": "+62-811-0004-0004"},
        },
        {
            "participant_no": "P-0005",
            "full_name": "Rina Kusuma",
            "birth_date": date(2018, 11, 15),
            "contact": {"email": "ortu.rina@vaneta.id", "phone": "+62-811-0005-0005"},
        },
        {
            "participant_no": "P-0006",
            "full_name": "Sari Rahmawati",
            "birth_date": date(2018, 5, 3),
            "contact": {"email": shared_email, "phone": "+62-811-0006-0006"},
        },
    ]


# ---------------------------------------------------------------------------
# Helpers — idempotent lookup-or-create on every table.
# ---------------------------------------------------------------------------


async def _ensure_service_principal(session: AsyncSession) -> UUID:
    """Look up by ``principal_code`` (UNIQUE) or insert via raw SQL.

    We use raw SQL because the SQLAlchemy ORM mapping for
    ``ServicePrincipalRow`` doesn't pick up the DB-level ``DEFAULT ksma_uuid()``
    on ``id`` or ``DEFAULT now()`` on ``created_at`` and would emit NULLs.
    """
    row = (
        await session.execute(
            text("SELECT id FROM service_principal WHERE principal_code = :c"),
            {"c": BOOTSTRAP_SP_CODE},
        )
    ).first()
    if row is not None:
        return UUID(str(row.id))
    sp_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO service_principal
              (id, principal_code, purpose, environment, credential_ref,
               status, created_at)
            VALUES
              (:id, :code, :purpose, :env, :cred, :status, now())
            """
        ),
        {
            "id": str(sp_id),
            "code": BOOTSTRAP_SP_CODE,
            "purpose": BOOTSTRAP_SP_PURPOSE,
            "env": BOOTSTRAP_SP_ENV,
            "cred": "local://dev",
            "status": "active",
        },
    )
    await session.flush()
    return sp_id


# Identity BC ORM models lack Python-side ``default=`` for ``created_at``,
# ``updated_at`` etc. The DB-level ``DEFAULT now()`` saves the column if it's
# omitted from INSERT, but SQLAlchemy's ORM emits the column explicitly with
# NULL. We bypass the ORM and use raw SQL for the identity bootstrap so the
# seed remains portable against any future tweak of the ORM mapping.


async def _ensure_tenant(
    session: AsyncSession,
    *,
    display_name: str,
    default_timezone: str,
) -> UUID:
    """Lookup by ``display_name`` (no UNIQUE on tenant.display_name; assume
    one tenant per exact display_name)."""
    row = (
        await session.execute(
            text(
                "SELECT id, service_status FROM tenant WHERE display_name = :n "
                "ORDER BY created_at LIMIT 1"
            ),
            {"n": display_name},
        )
    ).first()
    if row is not None:
        tenant_id = UUID(str(row.id))
        if row.service_status != TenantServiceStatus.ACTIVE.value:
            await session.execute(
                text(
                    "UPDATE tenant SET service_status = 'active', "
                    "activated_at = now(), updated_at = now(), updated_by = :u "
                    "WHERE id = :id"
                ),
                {"id": str(tenant_id), "u": "system"},
            )
            await session.flush()
        return tenant_id

    tenant_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO tenant
              (id, display_name, service_status, default_timezone,
               created_at, created_by, updated_at, updated_by)
            VALUES
              (:id, :dn, 'setup', :tz, now(), 'system', now(), 'system')
            """
        ),
        {"id": str(tenant_id), "dn": display_name, "tz": default_timezone},
    )
    await session.flush()
    await session.execute(
        text(
            "UPDATE tenant SET service_status = 'active', activated_at = now(), "
            "updated_at = now(), updated_by = 'system' WHERE id = :id"
        ),
        {"id": str(tenant_id)},
    )
    await session.flush()
    return tenant_id


async def _ensure_unit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    unit_code: str,
    name: str,
    institution_type: str,
    education_levels: list[str],
    timezone: str,
) -> UUID:
    """Lookup by (tenant_id, unit_code) UNIQUE."""
    row = (
        await session.execute(
            text(
                "SELECT id FROM education_unit "
                "WHERE tenant_id = :tid AND unit_code = :uc"
            ),
            {"tid": str(tenant_id), "uc": unit_code},
        )
    ).first()
    if row is not None:
        return UUID(str(row.id))
    unit_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO education_unit
              (id, tenant_id, unit_code, name, institution_type, education_levels,
               status, timezone, created_at, created_by, updated_at, updated_by)
            VALUES
              (:id, :tid, :uc, :nm, :it, :lvl, 'active', :tz,
               now(), 'system', now(), 'system')
            """
        ),
        {
            "id": str(unit_id),
            "tid": str(tenant_id),
            "uc": unit_code,
            "nm": name,
            "it": institution_type,
            "lvl": list(education_levels),
            "tz": timezone,
        },
    )
    await session.flush()
    return unit_id


async def _ensure_account(
    session: AsyncSession,
    *,
    auth_subject: str,
    display_name: str,
    password: str,
) -> UUID:
    """Lookup by (auth_namespace, auth_subject) UNIQUE."""
    from ksma.shared.auth import hash_password

    row = (
        await session.execute(
            text(
                "SELECT id, status FROM account "
                "WHERE auth_namespace = 'default' AND auth_subject = :s"
            ),
            {"s": auth_subject},
        )
    ).first()
    if row is not None:
        account_id = UUID(str(row.id))
        if row.status == "pending":
            await session.execute(
                text(
                    "UPDATE account SET status = 'active', "
                    "updated_at = now(), updated_by = :u WHERE id = :id"
                ),
                {"id": str(account_id), "u": "system"},
            )
            await session.flush()
        return account_id

    account_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO account
              (id, auth_subject, auth_namespace, display_name, password_hash,
               status, authz_revision, created_at, created_by,
               updated_at, updated_by)
            VALUES
              (:id, :sub, 'default', :dn, :ph, 'active', 1,
               now(), 'system', now(), 'system')
            """
        ),
        {
            "id": str(account_id),
            "sub": auth_subject,
            "dn": display_name,
            "ph": hash_password(password),
        },
    )
    await session.flush()
    return account_id


async def _ensure_membership(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    account_id: UUID,
) -> UUID:
    """Lookup by (tenant_id, account_id) UNIQUE."""
    row = (
        await session.execute(
            text(
                "SELECT id, status FROM membership "
                "WHERE tenant_id = :tid AND account_id = :aid"
            ),
            {"tid": str(tenant_id), "aid": str(account_id)},
        )
    ).first()
    if row is not None:
        membership_id = UUID(str(row.id))
        if row.status == "invited":
            await session.execute(
                text(
                    "UPDATE membership SET status = 'active', "
                    "updated_at = now(), updated_by = :u WHERE id = :id"
                ),
                {"id": str(membership_id), "u": "system"},
            )
            # Bump authz_revision on the account too so JWT holders re-resolve.
            await session.execute(
                text(
                    "UPDATE account SET authz_revision = authz_revision + 1, "
                    "updated_at = now() WHERE id = :id"
                ),
                {"id": str(account_id)},
            )
            await session.flush()
        return membership_id

    membership_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO membership
              (id, tenant_id, account_id, status, invitation_ref,
               created_at, created_by, updated_at, updated_by)
            VALUES
              (:id, :tid, :aid, 'active', :ref,
               now(), 'system', now(), 'system')
            """
        ),
        {
            "id": str(membership_id),
            "tid": str(tenant_id),
            "aid": str(account_id),
            "ref": f"seed-{auth_subject_short(account_id)}",
        },
    )
    await session.flush()
    return membership_id


def auth_subject_short(account_id: UUID) -> str:
    return str(account_id)[:8]


async def _ensure_policy(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    domain: str,
    ruleset: dict[str, Any],
) -> UUID:
    """Idempotent insert of one PolicyVersion row at version_no=1 per (tenant, domain)."""
    row = (
        await session.execute(
            text(
                "SELECT id FROM policy_version "
                "WHERE tenant_id = :tid AND policy_domain = :d AND version_no = 1"
            ),
            {"tid": str(tenant_id), "d": domain},
        )
    ).first()
    if row is not None:
        return UUID(str(row.id))
    pv_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO policy_version
              (id, tenant_id, policy_domain, version_no, effective_range,
               ruleset, status, approval, created_at, created_by,
               updated_at, updated_by)
            VALUES
              (:id, :tid, :d, 1, tstzrange(now(), 'infinity'),
               CAST(:rs AS JSONB), 'published',
               CAST('{"actor":"seed"}' AS JSONB),
               now(), 'system', now(), 'system')
            """
        ),
        {
            "id": str(pv_id),
            "tid": str(tenant_id),
            "d": domain,
            "rs": json.dumps(ruleset),
        },
    )
    await session.flush()
    return pv_id


async def _ensure_access_assignment(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    membership_id: UUID,
    role_code: str,
    action_ids: list[str],
    policy_id: UUID,
) -> UUID:
    """Insert if absent. Matches on (membership_id, role_code, status='active')."""
    row = (
        await session.execute(
            text(
                "SELECT id FROM access_assignment "
                "WHERE membership_id = :mid AND role_code = :rc AND status = 'active' "
                "LIMIT 1"
            ),
            {"mid": str(membership_id), "rc": role_code},
        )
    ).first()
    if row is not None:
        return UUID(str(row.id))
    grant_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO access_assignment
              (id, tenant_id, membership_id, role_code, action_ids, scope,
               valid_from, valid_until, status, policy_id, "authorization",
               created_at, created_by, updated_at, updated_by)
            VALUES
              (:id, :tid, :mid, :rc, :aids, CAST('{}' AS JSONB),
               now(), NULL, 'active', :pid, CAST('{"actor":"seed"}' AS JSONB),
               now(), 'system', now(), 'system')
            """
        ),
        {
            "id": str(grant_id),
            "tid": str(tenant_id),
            "mid": str(membership_id),
            "rc": role_code,
            "aids": list(action_ids),
            "pid": str(policy_id),
        },
    )
    # Bump authz_revision so JWT holders re-resolve grants.
    await session.execute(
        text(
            "UPDATE account SET authz_revision = authz_revision + 1, "
            "updated_at = now() "
            "WHERE id = (SELECT account_id FROM membership WHERE id = :m)"
        ),
        {"m": str(membership_id)},
    )
    await session.flush()
    return grant_id


async def _ensure_participant(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    participant_no: str,
    full_name: str,
    birth_date: date | None,
    contact: dict[str, Any] | None,
) -> UUID:
    """Lookup by (tenant_id, participant_no) UNIQUE."""
    row = (
        await session.execute(
            text(
                "SELECT id FROM participant "
                "WHERE tenant_id = :tid AND participant_no = :pn"
            ),
            {"tid": str(tenant_id), "pn": participant_no},
        )
    ).first()
    if row is not None:
        return UUID(str(row.id))
    # _ctx_for needs account_id=None (svc-principal). create_participant
    # writes created_by=str(ctx.account_id) -> "None"; we avoid that by
    # constructing a SYNTHETIC actor UUID just for audit strings.
    audit_actor = UUID("00000000-0000-0000-0000-000000000001")
    cmd = CreateParticipantCommand(
        actor=TenantContext(
            account_id=audit_actor,
            membership_id=None,
            tenant_id=tenant_id,
            unit_id=None,
            actor_kind=ActorKind.SERVICE_PRINCIPAL,
            authz_revision=1,
            role_codes=(),
        ),
        participant_no=participant_no,
        full_name=full_name,
        birth_date=birth_date,
        contact=contact,
    )
    p = await create_participant(session, cmd)
    await session.flush()
    return p.id


def _ctx_for(
    *,
    tenant_id: UUID,
    unit_id: UUID | None,
    actor_id: UUID,
) -> TenantContext:
    """A TenantContext that satisfies application services in this script.

    Application services write ``created_by = str(ctx.account_id)`` (and
    same for ``updated_by``), so we always pass a concrete UUID here.
    """
    return TenantContext(
        account_id=actor_id,
        membership_id=None,
        tenant_id=tenant_id,
        unit_id=unit_id,
        actor_kind=ActorKind.SERVICE_PRINCIPAL,
        authz_revision=1,
        role_codes=("SV",),
    )


# ---------------------------------------------------------------------------
# OperationalRun bootstrap
# ---------------------------------------------------------------------------


async def _ensure_operational_run_seed_constraint(session: AsyncSession) -> None:
    """Widen the ``operational_run.operation_kind`` CHECK to include
    ``'seed'`` so the bootstrap row can be inserted. Also ensure the
    ``attempt_count`` column exists (the original 0009 migration only
    defined columns; another ticket owns the formal schema work for
    operations, but we need attempt_count to satisfy the pilot smoke
    assertions).
    """
    # Defensive: idempotent ALTER for attempt_count.
    await session.execute(
        text(
            """
            ALTER TABLE operational_run
              ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0
              CHECK (attempt_count >= 0)
            """
        )
    )
    await session.execute(
        text(
            """
            DO $$
            DECLARE cn text;
            BEGIN
              -- Drop any existing CHECK whose definition mentions ``operation_kind``.
              SELECT con.conname INTO cn
              FROM pg_constraint con
              JOIN pg_class rel ON rel.oid = con.conrelid
              WHERE rel.relname = 'operational_run'
                AND con.contype = 'c'
                AND pg_get_constraintdef(con.oid) LIKE '%operation_kind%'
              LIMIT 1;
              IF cn IS NOT NULL THEN
                EXECUTE format('ALTER TABLE operational_run DROP CONSTRAINT %I', cn);
              END IF;
              ALTER TABLE operational_run
                ADD CONSTRAINT ck_op_run_kind_seed
                CHECK (operation_kind IN (
                  'backup','restore_test','restore','retention','reconciliation','seed'
                ));
            END $$;
            """
        )
    )


async def _bootstrap_operational_run(
    session: AsyncSession,
    *,
    sp_id: UUID,
) -> UUID:
    """Upsert an ``OperationalRun`` row keyed on (operation_kind='seed',
    execution_subject=sp_id). ``attempt_count`` is bumped on each re-run.
    """
    await _ensure_operational_run_seed_constraint(session)

    row = (
        await session.execute(
            text(
                """
                SELECT id, attempt_count FROM operational_run
                WHERE operation_kind = 'seed' AND execution_subject = :sp
                ORDER BY started_at DESC LIMIT 1
                """
            ),
            {"sp": str(sp_id)},
        )
    ).first()
    if row is not None:
        new_attempt = int(row.attempt_count or 0) + 1
        await session.execute(
            text(
                """
                UPDATE operational_run
                SET attempt_count = :n,
                    finished_at = now(),
                    status = 'succeeded',
                    evidence = CAST(:ev AS JSONB)
                WHERE id = :id
                """
            ),
            {
                "n": new_attempt,
                "ev": json.dumps({"seed_event": f"attempt {new_attempt}"}),
                "id": str(row.id),
            },
        )
        return UUID(str(row.id))

    now = datetime.now(timezone.utc)
    await session.execute(
        text(
            """
            INSERT INTO operational_run
              (id, tenant_id, operation_kind, environment, execution_subject,
               authorization_ref, started_at, finished_at, status, evidence,
               created_at)
            VALUES
              (ksma_uuid(), NULL, 'seed', 'development', :sp,
               CAST('{"actor":"seed-bootstrap"}' AS JSONB), :ts, :ts, 'succeeded',
               CAST('{"seed_event":"attempt 1"}' AS JSONB), now())
            """
        ),
        {"sp": str(sp_id), "ts": now},
    )
    await session.flush()
    new_row = (
        await session.execute(
            text(
                "SELECT id FROM operational_run WHERE operation_kind = 'seed' "
                "AND execution_subject = :sp ORDER BY started_at DESC LIMIT 1"
            ),
            {"sp": str(sp_id)},
        )
    ).first()
    assert new_row is not None
    return UUID(str(new_row.id))


# ---------------------------------------------------------------------------
# Tenant seed
# ---------------------------------------------------------------------------


async def _seed_one_tenant(
    session: AsyncSession,
    *,
    cfg: TenantCfg,
    sp_id: UUID,
) -> dict[str, Any]:
    """Seed everything for a single tenant. Returns the per-tenant state dict
    needed by the Vaneta-only branches.
    """
    tenant_id = await _ensure_tenant(
        session,
        display_name=cfg.display_name,
        default_timezone="Asia/Jakarta",
    )
    print(f"[seed] tenant {cfg.code} {cfg.display_name} -> {tenant_id}")

    unit_id = await _ensure_unit(
        session,
        tenant_id=tenant_id,
        unit_code=cfg.unit_code,
        name=f"{cfg.display_name} HQ",
        institution_type=cfg.institution_type,
        education_levels=["TK"],
        timezone="Asia/Jakarta",
    )

    # Policies (access policy is needed before any grant).
    policy_ids: dict[str, UUID] = {}
    for domain, ruleset in POLICY_DOMAINS:
        pid = await _ensure_policy(
            session, tenant_id=tenant_id, domain=domain, ruleset=ruleset
        )
        policy_ids[domain] = pid
    print(f"[seed]   {cfg.code} published {len(policy_ids)} policies")

    # Demo accounts + memberships + role grants.
    demo: dict[str, dict[str, UUID]] = {}
    for code in DEMO_ROLE_CODES:
        auth_subject = f"{code}.{cfg.slug}"
        display_name = f"{cfg.code} {code.upper()} Demo"
        account_id = await _ensure_account(
            session,
            auth_subject=auth_subject,
            display_name=display_name,
            password=DEMO_PASSWORD,
        )
        membership_id = await _ensure_membership(
            session, tenant_id=tenant_id, account_id=account_id
        )
        # Action set for this role, as per the RoleCode matrix.
        role_enum = RoleCode(code.upper())
        actions = sorted({a.value for a in ROLE_DEFAULT_ACTIONS.get(role_enum, set())})
        await _ensure_access_assignment(
            session,
            tenant_id=tenant_id,
            membership_id=membership_id,
            role_code=code.upper(),
            action_ids=actions,
            policy_id=policy_ids["access"],
        )
        demo[code] = {"account_id": account_id, "membership_id": membership_id}
    print(f"[seed]   {cfg.code} seeded {len(DEMO_ROLE_CODES)} accounts + grants")

    ctx = _ctx_for(tenant_id=tenant_id, unit_id=unit_id, actor_id=sp_id)

    out: dict[str, Any] = {
        "tenant_id": tenant_id,
        "unit_id": unit_id,
        "ctx": ctx,
        "demo": demo,
        "policy_ids": policy_ids,
    }

    if cfg.seed_vaneta_data:
        await _seed_vaneta(session, cfg=cfg, **out)

    return out


# ---------------------------------------------------------------------------
# Vaneta-only seed: program, batch, sessions, enrollments, invoices, payments,
# attendance.
# ---------------------------------------------------------------------------


async def _seed_vaneta(
    session: AsyncSession,
    *,
    cfg: TenantCfg,
    tenant_id: UUID,
    unit_id: UUID,
    ctx: TenantContext,
    demo: dict[str, dict[str, UUID]],
    policy_ids: dict[str, UUID],
) -> None:
    print(f"[seed]   {cfg.code} seeding program + batch + sessions + finance")

    # 6 demo participants.
    participants: dict[str, UUID] = {}
    for pdict in _vaneta_participants():
        pid = await _ensure_participant(
            session,
            tenant_id=tenant_id,
            participant_no=pdict["participant_no"],
            full_name=pdict["full_name"],
            birth_date=pdict["birth_date"],
            contact=pdict["contact"],
        )
        participants[pdict["participant_no"]] = pid
    print(f"[seed]     {cfg.code} seeded {len(participants)} participants")

    # 1 Program + 1 published ProgramVersion.
    program_id = await _ensure_program(
        session,
        ctx=ctx,
        unit_id=unit_id,
        program_code="EN-A1",
        name="English A1 (Vaneta Demo)",
        service_group="language",
    )
    pv_id = await _ensure_published_program_version(
        session,
        ctx=ctx,
        program_id=program_id,
    )

    # 1 Batch EN-A1-Okt2026 (capacity 10, onsite), opened.
    batch_id = await _ensure_batch(
        session,
        ctx=ctx,
        unit_id=unit_id,
        program_version_id=pv_id,
        batch_code="EN-A1-Okt2026",
        capacity=10,
    )
    if await _batch_status(session, ctx, batch_id) == "planned":
        await open_batch(session, ctx, batch_id)

    # 3 Sessions at now+7d/14d/21d.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    session_ids: list[UUID] = []
    for offset_days in (7, 14, 21):
        sid = await _ensure_session(
            session,
            ctx=ctx,
            batch_id=batch_id,
            ordinal=len(session_ids) + 1,
            starts_at=now + timedelta(days=offset_days, hours=1),
            ends_at=now + timedelta(days=offset_days, hours=3),
        )
        session_ids.append(sid)
    print(f"[seed]     {cfg.code} seeded {len(session_ids)} sessions")

    # 2 enrollments (P-0001, P-0002) — needed so the batch shows occupied=2.
    for pno in ("P-0001", "P-0002"):
        await _ensure_enrollment_for_batch(
            session,
            ctx=ctx,
            batch_id=batch_id,
            participant_id=participants[pno],
            unit_id=unit_id,
        )

    # 2 invoices: P-0001 (1 installment), P-0002 (2 installments).
    inv1_id = await _ensure_invoice_with_payment_alloc(
        session,
        ctx=ctx,
        unit_id=unit_id,
        participant_id=participants["P-0001"],
        target_invoice_no="INV-VANETA-0001",
        lines=[
            {
                "description": "EN-A1 Tuition — Sari Wulandari",
                "quantity": 1,
                "unit_price": 1_000_000,
                "discount_amount": 0,
            },
        ],
        installments=[
            (1, now.date(), 1_000_000),
        ],
        allocation_amount=1_000_000,
    )
    inv2_id = await _ensure_invoice_with_payment_alloc(
        session,
        ctx=ctx,
        unit_id=unit_id,
        participant_id=participants["P-0002"],
        target_invoice_no="INV-VANETA-0002",
        lines=[
            {
                "description": "EN-A1 Tuition (Term 1) — Budi Santoso",
                "quantity": 1,
                "unit_price": 600_000,
                "discount_amount": 0,
            },
            {
                "description": "EN-A1 Tuition (Term 2) — Budi Santoso",
                "quantity": 1,
                "unit_price": 400_000,
                "discount_amount": 0,
            },
        ],
        installments=[
            (1, now.date(), 600_000),
            (2, now.date(), 400_000),
        ],
        # Do NOT allocate against INV-0002 (per ticket: it stays unpaid).
        allocation_amount=None,
    )
    print(
        f"[seed]     {cfg.code} seeded 2 invoices (INV-VANETA-0001={inv1_id}, "
        f"INV-VANETA-0002={inv2_id}); 1 verified payment + 1 allocation"
    )

    # 1 attendance row for P-0001 in the first session.
    if session_ids:
        await _ensure_attendance(
            session,
            ctx=ctx,
            session_id=session_ids[0],
            batch_id=batch_id,
            participant_id=participants["P-0001"],
            code=AttendanceCode.HADIR,
        )
        print(f"[seed]     {cfg.code} seeded 1 attendance row")

    # Surface the seed events into the portal inbox + audit log so the
    # smoke-pilot endpoints return meaningful rows (the real outbox worker
    # would do this; the seed short-circuits that).
    allocation_info = await _allocation_row_for_first_invoice(
        session, tenant_id=tenant_id, invoice_id=inv1_id
    )
    await _seed_portal_inbox_and_audit(
        session,
        tenant_id=tenant_id,
        st_account_id=demo["st"]["account_id"],
        invoice_id=inv1_id,
        payment_allocation=allocation_info,
    )


# ---------------------------------------------------------------------------
# Vaneta-domain idempotent helpers
# ---------------------------------------------------------------------------


async def _ensure_program(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    unit_id: UUID,
    program_code: str,
    name: str,
    service_group: str,
) -> UUID:
    row = (
        await session.execute(
            text(
                "SELECT id FROM program "
                "WHERE tenant_id = :tid AND unit_id = :uid AND program_code = :c"
            ),
            {"tid": str(ctx.tenant_id), "uid": str(unit_id), "c": program_code},
        )
    ).first()
    if row is not None:
        return UUID(str(row.id))
    p = await create_program(
        session,
        ctx,
        unit_id=unit_id,
        program_code=program_code,
        name=name,
        service_group=service_group,
    )
    await session.flush()
    return p.id


async def _ensure_published_program_version(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    program_id: UUID,
) -> UUID:
    row = (
        await session.execute(
            text(
                "SELECT id, status FROM program_version "
                "WHERE tenant_id = :tid AND program_id = :pid AND version_no = 1"
            ),
            {"tid": str(ctx.tenant_id), "pid": str(program_id)},
        )
    ).first()
    if row is not None:
        return UUID(str(row.id))
    pv = await create_program_version(
        session,
        ctx,
        program_id=program_id,
        learning_objectives=(
            "EN-A1 Starter: basic English vocabulary, classroom routines, "
            "simple sentences; 32 hours over 8 weeks."
        ),
        curriculum=Curriculum(
            components=[
                {"code": "greetings", "name": "Greetings & routines", "hours": 8},
                {"code": "alphabet", "name": "Alphabet & phonics", "hours": 8},
                {"code": "vocab", "name": "Daily vocabulary", "hours": 8},
                {"code": "sentences", "name": "Simple sentences", "hours": 8},
            ],
            duration_unit="hours",
        ),
    )
    await session.flush()
    await publish_program_version(session, ctx, version_id=pv.id, make_current=True)
    await session.flush()
    return pv.id


async def _ensure_batch(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    unit_id: UUID,
    program_version_id: UUID,
    batch_code: str,
    capacity: int,
) -> UUID:
    row = (
        await session.execute(
            text(
                "SELECT id, status, capacity FROM batch "
                "WHERE tenant_id = :tid AND unit_id = :uid AND batch_code = :c"
            ),
            {"tid": str(ctx.tenant_id), "uid": str(unit_id), "c": batch_code},
        )
    ).first()
    if row is not None:
        return UUID(str(row.id))
    b = await create_batch(
        session,
        ctx,
        unit_id=unit_id,
        batch_code=batch_code,
        program_version_id=program_version_id,
        capacity=capacity,
        delivery_mode=DeliveryMode.ONSITE,
    )
    await session.flush()
    return b.id


async def _batch_status(session: AsyncSession, ctx: TenantContext, batch_id: UUID) -> str:
    r = (
        await session.execute(
            text("SELECT status FROM batch WHERE id = :i AND tenant_id = :t"),
            {"i": str(batch_id), "t": str(ctx.tenant_id)},
        )
    ).first()
    return str(r.status) if r else "planned"


async def _ensure_session(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    batch_id: UUID,
    ordinal: int,
    starts_at: datetime,
    ends_at: datetime,
) -> UUID:
    row = (
        await session.execute(
            text(
                "SELECT id FROM session "
                "WHERE tenant_id = :tid AND batch_id = :bid AND session_no = :n"
            ),
            {"tid": str(ctx.tenant_id), "bid": str(batch_id), "n": ordinal},
        )
    ).first()
    if row is not None:
        existing_id = UUID(str(row.id))
        # Ensure the session has an effective revision (R09).
        rev_row = (
            await session.execute(
                text(
                    "SELECT id FROM session_revision WHERE session_id = :sid "
                    "AND revision_no = 1"
                ),
                {"sid": str(existing_id)},
            )
        ).first()
        if rev_row is None:
            await add_revision(
                session,
                ctx,
                session_id=existing_id,
                starts_at=starts_at,
                ends_at=ends_at,
                timezone_str="Asia/Jakarta",
                change_reason="Pilot seed schedule",
                location={"venue": "Vaneta HQ — Ruang A"},
                make_effective=True,
            )
            await session.flush()
        return existing_id

    s = await create_session(session, ctx, batch_id=batch_id)
    await session.flush()
    await add_revision(
        session,
        ctx,
        session_id=s.id,
        starts_at=starts_at,
        ends_at=ends_at,
        timezone_str="Asia/Jakarta",
        change_reason="Pilot seed schedule",
        location={"venue": "Vaneta HQ — Ruang A"},
        make_effective=True,
    )
    await session.flush()
    return s.id


async def _ensure_application_for_participant(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    batch_id: UUID,
    participant_id: UUID,
    unit_id: UUID,
) -> UUID:
    """Look up an existing application for the participant or create + submit +
    decide one. Application is linked indirectly via ``program_id`` (which is
    looked up from the batch → program_version → program chain)."""
    row = (
        await session.execute(
            text(
                """
                SELECT a.id, a.status FROM application a
                WHERE a.tenant_id = :tid AND a.participant_id = :pid
                ORDER BY a.created_at DESC LIMIT 1
                """
            ),
            {"tid": str(ctx.tenant_id), "pid": str(participant_id)},
        )
    ).first()

    if row is not None and row.status in {"submitted", "approved", "under_review"}:
        app_id = UUID(str(row.id))
        if row.status == "submitted" or row.status == "under_review":
            await decide_application(
                session, ctx, application_id=app_id, decision="approve",
                reason="Pilot seed approval",
            )
            await session.flush()
        return app_id

    pv_row = (
        await session.execute(
            text("SELECT program_version_id FROM batch WHERE id = :i"),
            {"i": str(batch_id)},
        )
    ).first()
    program_id = (
        await session.execute(
            text("SELECT program_id FROM program_version WHERE id = :i"),
            {"i": str(pv_row.program_version_id)},
        )
    ).scalar_one()

    a = await create_application(
        session,
        ctx,
        unit_id=unit_id,
        participant_id=participant_id,
        program_id=UUID(str(program_id)),
    )
    await session.flush()
    await submit_application(session, ctx, a.id)
    await session.flush()
    await decide_application(
        session, ctx, application_id=a.id, decision="approve",
        reason="Pilot seed approval",
    )
    await session.flush()
    return a.id


async def _ensure_enrollment_for_batch(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    batch_id: UUID,
    participant_id: UUID,
    unit_id: UUID,
) -> UUID:
    row = (
        await session.execute(
            text(
                """
                SELECT id FROM enrollment
                WHERE tenant_id = :tid AND batch_id = :bid AND participant_id = :pid
                  AND status IN ('planned','active','running')
                LIMIT 1
                """
            ),
            {"tid": str(ctx.tenant_id), "bid": str(batch_id), "pid": str(participant_id)},
        )
    ).first()
    if row is not None:
        return UUID(str(row.id))
    application_id = await _ensure_application_for_participant(
        session, ctx=ctx, batch_id=batch_id,
        participant_id=participant_id, unit_id=unit_id,
    )
    e = await place_enrollment(
        session,
        ctx,
        participant_id=participant_id,
        batch_id=batch_id,
        application_id=application_id,
        effective_from=datetime.now(timezone.utc),
    )
    await session.flush()
    return e.id


async def _ensure_invoice_with_payment_alloc(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    unit_id: UUID,
    participant_id: UUID,
    target_invoice_no: str,
    lines: list[dict[str, Any]],
    installments: list[tuple[int, date, int]],
    allocation_amount: int | None,
) -> UUID:
    """Idempotent invoice creation: lookup by ``invoice_no``; if absent, create
    + issue + OVERWRITE invoice_no to our requested tag. If allocation_amount
    is set, also submit + verify + allocate one installment (first one).
    """
    row = (
        await session.execute(
            text(
                "SELECT id FROM invoice "
                "WHERE tenant_id = :tid AND invoice_no = :n"
            ),
            {"tid": str(ctx.tenant_id), "n": target_invoice_no},
        )
    ).first()
    if row is not None:
        return UUID(str(row.id))

    installment_dicts = [
        {"sequence_no": s, "due_date": d, "due_amount": amt}
        for (s, d, amt) in installments
    ]
    inv = await create_invoice(
        session,
        ctx,
        unit_id=unit_id,
        participant_id=participant_id,
        lines=lines,
        installments=installment_dicts,
    )
    await session.flush()
    issued = await issue_invoice(session, ctx, invoice_id=inv.id)
    await session.flush()

    # Overwrite the auto-generated invoice_no with our target tag.
    await session.execute(
        text(
            "UPDATE invoice SET invoice_no = :n WHERE id = :i AND tenant_id = :t"
        ),
        {"n": target_invoice_no, "i": str(issued.id), "t": str(ctx.tenant_id)},
    )
    invoice_id = issued.id

    if allocation_amount is None:
        return invoice_id

    inst_row = (
        await session.execute(
            text(
                "SELECT id FROM installment "
                "WHERE invoice_id = :iid AND sequence_no = 1"
            ),
            {"iid": str(invoice_id)},
        )
    ).first()
    if inst_row is None:
        return invoice_id
    installment_id = UUID(str(inst_row.id))

    sub = await submit_payment(
        session,
        ctx,
        unit_id=unit_id,
        participant_id=participant_id,
        invoice_id=invoice_id,
        claimed_amount=allocation_amount,
        transfer_at=datetime.now(timezone.utc),
        proof_attachment_id=None,
        transfer_reference=f"SEED-{target_invoice_no}",
    )
    await session.flush()

    verified = await verify_payment(
        session,
        ctx,
        submission_id=sub.id,
        recognized_amount=allocation_amount,
        reason="Pilot seed verification",
    )
    await session.flush()

    # Deterministic idempotency_id per invoice: SHA256 of the invoice_no,
    # truncated to 32 hex chars. This way re-running the seed does not double-
    # allocate (the allocator returns the existing allocation).
    import hashlib

    idem_hex = hashlib.sha256(target_invoice_no.encode()).hexdigest()[:32]
    alloc, _receipt = await allocate_funds(
        session,
        ctx,
        payment_id=verified.id,
        installment_id=installment_id,
        amount=allocation_amount,
        idempotency_id=UUID(hex=idem_hex),
        issue_receipt=True,
    )
    await session.flush()
    return invoice_id


async def _ensure_attendance(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    session_id: UUID,
    batch_id: UUID,
    participant_id: UUID,
    code: AttendanceCode,
) -> UUID | None:
    """Idempotent: locate the (participant, batch) enrollment, then insert one
    attendance row (UNIQUE session_id, enrollment_id).
    """
    erow = (
        await session.execute(
            text(
                """
                SELECT id FROM enrollment
                WHERE tenant_id = :tid AND batch_id = :bid AND participant_id = :pid
                  AND status IN ('planned','active')
                ORDER BY created_at DESC LIMIT 1
                """
            ),
            {"tid": str(ctx.tenant_id), "bid": str(batch_id), "pid": str(participant_id)},
        )
    ).first()
    if erow is None:
        return None
    enrollment_id = UUID(str(erow.id))
    arow = (
        await session.execute(
            text(
                "SELECT id FROM attendance WHERE session_id = :sid AND enrollment_id = :eid"
            ),
            {"sid": str(session_id), "eid": str(enrollment_id)},
        )
    ).first()
    if arow is not None:
        return UUID(str(arow.id))
    a = await record_attendance(
        session,
        ctx,
        session_id=session_id,
        enrollment_id=enrollment_id,
        attendance_code=code,
    )
    await session.flush()
    return a.id


# ---------------------------------------------------------------------------
# Portal-inbox + audit seeding (so the smoke pilot sees notifications)
# ---------------------------------------------------------------------------


async def _allocation_row_for_first_invoice(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    invoice_id: UUID,
) -> dict[str, Any] | None:
    """Return a small dict identifying the first allocation against
    ``invoice_id`` so the seed can emit a matching ``audit_record`` row.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT pa.id, pa.amount, i.sequence_no, pa.allocated_by
                FROM payment_allocation pa
                JOIN installment i ON i.id = pa.installment_id
                WHERE pa.tenant_id = :tid AND i.invoice_id = :iid
                  AND pa.reversed_at IS NULL
                ORDER BY pa.allocated_at LIMIT 1
                """
            ),
            {"tid": str(tenant_id), "iid": str(invoice_id)},
        )
    ).first()
    if row is None:
        return None
    return {
        "id": str(row.id),
        "amount": int(row.amount),
        "sequence_no": int(row.sequence_no),
        "allocated_by": str(row.allocated_by),
    }


async def _seed_portal_inbox_and_audit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    st_account_id: UUID,
    invoice_id: UUID,
    payment_allocation: dict[str, Any] | None,
) -> None:
    """Idempotent: write a ``PaymentVerified`` notification for ``st_account_id``
    and one ``FIN-A05`` audit_record row so the smoke pilot can assert on
    portal/inbox + /me/audit=FIN.
    """
    notification_event_id = uuid4()
    # Internal notification for ST (idempotent on the UNIQUE composite key).
    already = (
        await session.execute(
            text(
                """
                SELECT id FROM internal_notification
                WHERE tenant_id = :tid
                  AND recipient_account_id = :acct
                  AND notification_kind = 'PaymentVerified'
                LIMIT 1
                """
            ),
            {"tid": str(tenant_id), "acct": str(st_account_id)},
        )
    ).first()
    if already is None:
        await session.execute(
            text(
                """
                INSERT INTO internal_notification
                  (id, tenant_id, recipient_account_id, source_ref, event_id,
                   notification_kind, summary, delivery_state, occurred_at)
                VALUES
                  (ksma_uuid(), :tid, :acct,
                   CAST(:src AS JSONB),
                   :eid, 'PaymentVerified',
                   'Pembayaran INV-VANETA-0001 telah diverifikasi.',
                   'available', now())
                """
            ),
            {
                "tid": str(tenant_id),
                "acct": str(st_account_id),
                "src": json.dumps(
                    {"kind": "verified_payment", "invoice_id": str(invoice_id)}
                ),
                "eid": str(notification_event_id),
            },
        )

    # Audit row for the allocation event (idempotent on (action_id, object_ref)).
    if payment_allocation is not None:
        already_audit = (
            await session.execute(
            text(
                """
                SELECT id FROM audit_record
                WHERE tenant_id = :tid
                  AND action_id = 'FIN-A05'
                  AND object_ref->>'id' = :pid
                LIMIT 1
                """
            ),
            {"tid": str(tenant_id), "pid": payment_allocation["id"]},
            )
        ).first()
        if already_audit is None:
            await session.execute(
                text(
                    """
                    INSERT INTO audit_record
                      (id, tenant_id, actor, action_id, object_ref,
                       occurred_at, change_ref, reason, authorization_ref,
                       correlation_id)
                    VALUES
                      (ksma_uuid(), :tid,
                       CAST('{"kind":"service_principal","id":"seed-bootstrap"}' AS JSONB),
                       'FIN-A05',
                       CAST(:oref AS JSONB),
                       now(), NULL, 'Pilot seed allocation',
                       CAST('{"actor":"seed"}' AS JSONB),
                       'seed-pilot')
                    """
                ),
                {
                    "tid": str(tenant_id),
                    "oref": json.dumps(
                        {
                            "kind": "payment_allocation",
                            "id": payment_allocation["id"],
                        }
                    ),
                },
            )

    await session.flush()





async def main() -> None:
    async with session_scope() as session:
        # Bypass RLS for the platform-wide bootstrap; otherwise writes to
        # multiple tenants get filtered by tenant_id GUC.
        await set_current_tenant(
            session,
            tenant_id=UUID("00000000-0000-0000-0000-000000000000"),
            bypass=True,
        )

        sp_id = await _ensure_service_principal(session)
        print(f"[seed] service principal seed-bootstrap -> {sp_id}")

        for cfg in TENANT_CFGS:
            await _seed_one_tenant(session, cfg=cfg, sp_id=sp_id)
            await session.flush()

        run_id = await _bootstrap_operational_run(session, sp_id=sp_id)
        await session.flush()
        print(f"[seed] operational_run {run_id} (attempt_count bumped)")

    print("[seed] done.")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
