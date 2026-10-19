"""Action registry + whole-grant resolver.

Per ADR0001 §4 + Q15: 73 actions, 13 roles, whole-grant evaluation (R02).

Every protected route calls ``require_action(ctx, action_id, target)`` via a
FastAPI dependency. The dependency:

1. Loads the active ``AccessAssignment`` rows for the current membership.
2. Filters to rows where ``action_id in action_ids`` AND ``validity covers now``
   AND ``status='active'`` AND ``policy_id.status='published'``.
3. Evaluates the JSONB ``scope`` against the target object: tenant, unit,
   batch, session, participant, column.
4. Returns the first grant that matches **all** required dimensions (R02).
   Splicing scope from one grant with action from another is forbidden.

If no grant matches, raises ``AccessDeniedError`` with no object payload
(no participant name / object existence leaked).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID


# Action code enum — 73 codes from KSMA Access Control Matrix v1.0.
# Names follow the doc: ``{MODULE}-A{NN}``.
class ActionCode(str, enum.Enum):
    # --- PLT ---
    PLT_A01 = "PLT-A01"  # Create customer & unit
    PLT_A02 = "PLT-A02"  # Switch institution context
    PLT_A03 = "PLT-A03"  # View profile & brand
    PLT_A04 = "PLT-A04"  # ! Change profile & brand
    PLT_A05 = "PLT-A05"  # ! Invite & change permissions
    PLT_A06 = "PLT-A06"  # ! Revoke & end permissions
    PLT_A07 = "PLT-A07"  # ! Read audit log
    PLT_A08 = "PLT-A08"  # Write audit log (SV)

    # --- REG ---
    REG_A01 = "REG-A01"  # View participant profile
    REG_A02 = "REG-A02"  # Create/correct profile
    REG_A03 = "REG-A03"  # View registration files
    REG_A04 = "REG-A04"  # Decide registration
    REG_A05 = "REG-A05"  # View placement
    REG_A06 = "REG-A06"  # Set placement
    REG_A07 = "REG-A07"  # View transfer history
    REG_A08 = "REG-A08"  # ! Authorize transfer
    REG_A09 = "REG-A09"  # Create/continue draft

    # --- CLS ---
    CLS_A01 = "CLS-A01"  # View program versions
    CLS_A02 = "CLS-A02"  # Create/publish program
    CLS_A03 = "CLS-A03"  # View class & capacity
    CLS_A04 = "CLS-A04"  # Create class & capacity
    CLS_A05 = "CLS-A05"  # View session schedule
    CLS_A06 = "CLS-A06"  # Create/change schedule
    CLS_A07 = "CLS-A07"  # Propose session changes
    CLS_A08 = "CLS-A08"  # Decide substitute schedule

    # --- SES ---
    SES_A01 = "SES-A01"  # View attendance
    SES_A02 = "SES-A02"  # Record attendance
    SES_A03 = "SES-A03"  # View session journal
    SES_A04 = "SES-A04"  # Write session journal
    SES_A05 = "SES-A05"  # Close session
    SES_A06 = "SES-A06"  # ! Correct closed session
    SES_A07 = "SES-A07"  # View substitute assignment
    SES_A08 = "SES-A08"  # ! Set substitute instructor

    # --- ASM ---
    ASM_A01 = "ASM-A01"  # View assessment rubric
    ASM_A02 = "ASM-A02"  # ! Create rubric version
    ASM_A03 = "ASM-A03"  # Read drafts & evidence
    ASM_A04 = "ASM-A04"  # Record/revise grades
    ASM_A05 = "ASM-A05"  # View issued report
    ASM_A06 = "ASM-A06"  # ! Publish & replace report
    ASM_A07 = "ASM-A07"  # View completion & certificate
    ASM_A08 = "ASM-A08"  # ! Authorize & revoke certificate

    # --- FIN ---
    FIN_A01 = "FIN-A01"  # View invoice & installments
    FIN_A02 = "FIN-A02"  # Create/publish invoice
    FIN_A03 = "FIN-A03"  # Submit payment evidence
    FIN_A04 = "FIN-A04"  # ! Verify or reject evidence
    FIN_A05 = "FIN-A05"  # Allocate payment
    FIN_A06 = "FIN-A06"  # View receipt & allocation
    FIN_A07 = "FIN-A07"  # Request finance correction
    FIN_A08 = "FIN-A08"  # ! Authorize transaction reversal

    # --- PRT ---
    PRT_A01 = "PRT-A01"  # Open academic portal
    PRT_A02 = "PRT-A02"  # Open finance portal
    PRT_A03 = "PRT-A03"  # View participant relations
    PRT_A04 = "PRT-A04"  # ! Verify/revoke relation
    PRT_A05 = "PRT-A05"  # Submit service request
    PRT_A06 = "PRT-A06"  # Handle requests & notifications
    PRT_A07 = "PRT-A07"  # Change communication preferences
    PRT_A08 = "PRT-A08"  # ! Create manual messages

    # --- OPS ---
    OPS_A01 = "OPS-A01"  # Review & map import
    OPS_A02 = "OPS-A02"  # ! Commit import
    OPS_A03 = "OPS-A03"  # View export preview
    OPS_A04 = "OPS-A04"  # ! Create & retrieve export
    OPS_A05 = "OPS-A05"  # Monitor backup jobs
    OPS_A06 = "OPS-A06"  # Run backup job (SV)
    OPS_A07 = "OPS-A07"  # View indicators & queue
    OPS_A08 = "OPS-A08"  # Change personal indicator filter

    # --- X (cross-module restricted) ---
    X_A01 = "X-A01"  # ! Open/download private file
    X_A02 = "X-A02"  # ! Access individual support data
    X_A03 = "X-A03"  # ! Approve platform assistance
    X_A04 = "X-A04"  # ! Run platform assistance
    X_A05 = "X-A05"  # ! Authorize recovery
    X_A06 = "X-A06"  # ! Authorize fee exception
    X_A07 = "X-A07"  # ! Execute retention & deletion
    X_A08 = "X-A08"  # ! Change indicator definition


# Role codes (13 roles) — same names as the Access Matrix.
class RoleCode(str, enum.Enum):
    OW = "OW"  # Pimpinan
    AM = "AM"  # Pengelola akses
    AC = "AC"  # Admin akademik
    FI = "FI"  # Keuangan
    IN = "IN"  # Pengajar
    AS = "AS"  # Penilai
    AP = "AP"  # Pengesah akademik
    AU = "AU"  # Pemeriksa
    ST = "ST"  # Peserta
    GU = "GU"  # Wali akademik
    PY = "PY"  # Pembayar
    OP = "OP"  # Operator platform
    SV = "SV"  # Identitas layanan teknis


# Role → default action grants (the matrix in section §2 of the access control doc).
# `None` means "action requires explicit named grant" (the `!` prefix in the doc).
# Roles not listed for an action are NOT granted that action.
ROLE_DEFAULT_ACTIONS: dict[RoleCode, set[ActionCode]] = {
    RoleCode.OW: {
        ActionCode.PLT_A02,
        ActionCode.PLT_A03,
        ActionCode.OPS_A03,
        ActionCode.OPS_A04,
        ActionCode.OPS_A05,
        ActionCode.OPS_A07,
        ActionCode.OPS_A08,
    },
    RoleCode.AM: {
        ActionCode.PLT_A02,
        ActionCode.PLT_A03,
        ActionCode.PLT_A05,
        ActionCode.PLT_A06,
        ActionCode.X_A03,
    },
    RoleCode.AC: {
        ActionCode.PLT_A02,
        ActionCode.PLT_A03,
        ActionCode.REG_A01,
        ActionCode.REG_A02,
        ActionCode.REG_A03,
        ActionCode.REG_A04,
        ActionCode.REG_A05,
        ActionCode.REG_A06,
        ActionCode.REG_A07,
        ActionCode.REG_A08,
        ActionCode.REG_A09,
        ActionCode.CLS_A01,
        ActionCode.CLS_A02,
        ActionCode.CLS_A03,
        ActionCode.CLS_A04,
        ActionCode.CLS_A05,
        ActionCode.CLS_A06,
        ActionCode.CLS_A08,
        ActionCode.SES_A01,
        ActionCode.SES_A03,
        ActionCode.SES_A06,
        ActionCode.SES_A07,
        ActionCode.SES_A08,
        ActionCode.ASM_A01,
        ActionCode.ASM_A02,
        ActionCode.ASM_A05,
        ActionCode.ASM_A07,
        ActionCode.PRT_A03,
        ActionCode.PRT_A04,
        ActionCode.PRT_A06,
        ActionCode.PRT_A08,
        ActionCode.OPS_A01,
        ActionCode.OPS_A02,
        ActionCode.OPS_A03,
        ActionCode.OPS_A04,
        ActionCode.OPS_A07,
        ActionCode.OPS_A08,
        ActionCode.X_A01,
    },
    RoleCode.FI: {
        ActionCode.PLT_A02,
        ActionCode.PLT_A03,
        ActionCode.REG_A01,
        ActionCode.FIN_A01,
        ActionCode.FIN_A02,
        ActionCode.FIN_A03,
        ActionCode.FIN_A04,
        ActionCode.FIN_A05,
        ActionCode.FIN_A06,
        ActionCode.FIN_A07,
        ActionCode.FIN_A08,
        ActionCode.PRT_A06,
        ActionCode.PRT_A08,
        ActionCode.OPS_A01,
        ActionCode.OPS_A02,
        ActionCode.OPS_A03,
        ActionCode.OPS_A04,
        ActionCode.OPS_A07,
        ActionCode.OPS_A08,
        ActionCode.X_A01,
        ActionCode.X_A06,
    },
    RoleCode.IN: {
        ActionCode.PLT_A02,
        ActionCode.PLT_A03,
        ActionCode.REG_A01,
        ActionCode.REG_A05,
        ActionCode.CLS_A01,
        ActionCode.CLS_A03,
        ActionCode.CLS_A05,
        ActionCode.CLS_A07,
        ActionCode.SES_A01,
        ActionCode.SES_A02,
        ActionCode.SES_A03,
        ActionCode.SES_A04,
        ActionCode.SES_A05,
        ActionCode.SES_A06,
        ActionCode.SES_A07,
        ActionCode.ASM_A01,
        ActionCode.ASM_A03,
        ActionCode.ASM_A05,
        ActionCode.ASM_A07,
        ActionCode.PRT_A05,
        ActionCode.OPS_A07,
        ActionCode.OPS_A08,
        ActionCode.X_A01,
        ActionCode.X_A02,
    },
    RoleCode.AS: {
        ActionCode.PLT_A02,
        ActionCode.PLT_A03,
        ActionCode.REG_A01,
        ActionCode.REG_A05,
        ActionCode.CLS_A01,
        ActionCode.CLS_A03,
        ActionCode.CLS_A05,
        ActionCode.SES_A01,
        ActionCode.SES_A03,
        ActionCode.ASM_A01,
        ActionCode.ASM_A02,
        ActionCode.ASM_A03,
        ActionCode.ASM_A04,
        ActionCode.ASM_A05,
        ActionCode.ASM_A07,
        ActionCode.OPS_A07,
        ActionCode.OPS_A08,
        ActionCode.X_A01,
    },
    RoleCode.AP: {
        ActionCode.PLT_A02,
        ActionCode.PLT_A03,
        ActionCode.REG_A01,
        ActionCode.REG_A05,
        ActionCode.CLS_A01,
        ActionCode.CLS_A03,
        ActionCode.CLS_A05,
        ActionCode.SES_A01,
        ActionCode.SES_A03,
        ActionCode.ASM_A01,
        ActionCode.ASM_A03,
        ActionCode.ASM_A05,
        ActionCode.ASM_A06,
        ActionCode.ASM_A07,
        ActionCode.ASM_A08,
        ActionCode.OPS_A07,
        ActionCode.OPS_A08,
        ActionCode.X_A01,
        ActionCode.X_A02,
    },
    RoleCode.AU: {
        ActionCode.PLT_A02,
        ActionCode.PLT_A03,
        ActionCode.PLT_A07,
        ActionCode.OPS_A03,
        ActionCode.OPS_A04,
        ActionCode.OPS_A05,
        ActionCode.OPS_A07,
        ActionCode.OPS_A08,
        ActionCode.X_A01,
    },
    RoleCode.ST: {
        ActionCode.PLT_A02,
        ActionCode.PLT_A03,
        ActionCode.REG_A01,
        ActionCode.REG_A03,
        ActionCode.REG_A05,
        ActionCode.REG_A07,
        ActionCode.CLS_A01,
        ActionCode.CLS_A05,
        ActionCode.CLS_A07,
        ActionCode.SES_A01,
        ActionCode.SES_A03,
        ActionCode.ASM_A05,
        ActionCode.ASM_A07,
        ActionCode.FIN_A01,
        ActionCode.FIN_A03,
        ActionCode.FIN_A06,
        ActionCode.FIN_A07,
        ActionCode.PRT_A01,
        ActionCode.PRT_A03,
        ActionCode.PRT_A05,
        ActionCode.PRT_A07,
        ActionCode.X_A01,
    },
    RoleCode.GU: {
        ActionCode.PLT_A02,
        ActionCode.PLT_A03,
        ActionCode.REG_A01,
        ActionCode.REG_A03,
        ActionCode.REG_A05,
        ActionCode.REG_A07,
        ActionCode.CLS_A01,
        ActionCode.CLS_A05,
        ActionCode.CLS_A07,
        ActionCode.SES_A01,
        ActionCode.SES_A03,
        ActionCode.ASM_A05,
        ActionCode.ASM_A07,
        ActionCode.FIN_A01,
        ActionCode.FIN_A03,
        ActionCode.FIN_A06,
        ActionCode.FIN_A07,
        ActionCode.PRT_A01,
        ActionCode.PRT_A03,
        ActionCode.PRT_A05,
        ActionCode.PRT_A07,
        ActionCode.X_A01,
    },
    RoleCode.PY: {
        ActionCode.PLT_A02,
        ActionCode.PLT_A03,
        ActionCode.REG_A01,
        ActionCode.FIN_A01,
        ActionCode.FIN_A03,
        ActionCode.FIN_A06,
        ActionCode.FIN_A07,
        ActionCode.PRT_A02,
        ActionCode.PRT_A03,
        ActionCode.PRT_A05,
        ActionCode.PRT_A07,
        ActionCode.X_A01,
    },
    RoleCode.OP: {
        ActionCode.PLT_A01,
        ActionCode.OPS_A05,
        ActionCode.X_A04,
        ActionCode.X_A05,
    },
    RoleCode.SV: {
        ActionCode.PLT_A08,
        ActionCode.OPS_A06,
        ActionCode.X_A07,
    },
}


# ---------------------------------------------------------------------------
# Scope shape (matches Q15(a))
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Scope:
    """Per-grant scope. All fields optional. Empty == unrestricted within tenant."""

    units: tuple[UUID, ...] = ()
    programs: tuple[UUID, ...] = ()
    batches: tuple[UUID, ...] = ()
    sessions: tuple[UUID, ...] = ()
    participants: tuple[UUID, ...] = ()
    columns: tuple[str, ...] = ()
    exclude_participants: tuple[UUID, ...] = ()

    def to_json(self) -> dict[str, list[str]]:
        def _ids(xs: tuple[UUID, ...]) -> list[str]:
            return [str(x) for x in xs]

        out: dict[str, list[str]] = {}
        if self.units:
            out["units"] = _ids(self.units)
        if self.programs:
            out["programs"] = _ids(self.programs)
        if self.batches:
            out["batches"] = _ids(self.batches)
        if self.sessions:
            out["sessions"] = _ids(self.sessions)
        if self.participants:
            out["participants"] = _ids(self.participants)
        if self.columns:
            out["columns"] = list(self.columns)
        if self.exclude_participants:
            out["exclude_participants"] = _ids(self.exclude_participants)
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> "Scope":
        if not data:
            return cls()

        def _uuids(key: str) -> tuple[UUID, ...]:
            raw = data.get(key) or []
            return tuple(UUID(x) for x in raw)

        return cls(
            units=_uuids("units"),
            programs=_uuids("programs"),
            batches=_uuids("batches"),
            sessions=_uuids("sessions"),
            participants=_uuids("participants"),
            columns=tuple(data.get("columns") or ()),
            exclude_participants=_uuids("exclude_participants"),
        )

    def covers_target(
        self,
        *,
        unit_id: UUID | None = None,
        program_id: UUID | None = None,
        batch_id: UUID | None = None,
        session_id: UUID | None = None,
        participant_id: UUID | None = None,
        column: str | None = None,
    ) -> bool:
        if participant_id and participant_id in self.exclude_participants:
            return False
        if self.participants and participant_id and participant_id not in self.participants:
            return False
        if self.batches and batch_id and batch_id not in self.batches:
            return False
        if self.sessions and session_id and session_id not in self.sessions:
            return False
        if self.programs and program_id and program_id not in self.programs:
            return False
        if self.units and unit_id and unit_id not in self.units:
            return False
        if self.columns and column and column not in self.columns:
            return False
        return True


# ---------------------------------------------------------------------------
# Resolver input / output
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GrantRow:
    """One ``AccessAssignment`` row, hydrated for in-memory evaluation."""

    id: UUID
    role_code: RoleCode
    action_ids: frozenset[ActionCode]
    scope: Scope
    valid_from: Any  # datetime
    valid_until: Any | None  # datetime or None
    status: str  # 'pending' | 'active' | 'expired' | 'revoked'
    policy_id: UUID


@dataclass(frozen=True, slots=True)
class Target:
    """The object the caller wants to act on. Each field is optional."""

    unit_id: UUID | None = None
    program_id: UUID | None = None
    batch_id: UUID | None = None
    session_id: UUID | None = None
    participant_id: UUID | None = None
    column: str | None = None


@dataclass(frozen=True, slots=True)
class GrantDecision:
    """Result of evaluating one request against the user's active grants."""

    allowed: bool
    grant_id: UUID | None = None
    role_code: RoleCode | None = None
    reason: str | None = None


def evaluate(
    *,
    actor: "TenantContextLike",
    action: ActionCode,
    target: Target,
    now: Any,
    grants: tuple[GrantRow, ...],
) -> GrantDecision:
    """Whole-grant evaluator (R02 / R03).

    Looks at every active grant for the actor's membership. The action is allowed
    only if there exists ONE grant whose role covers the action AND whose scope
    covers the target AND whose validity covers ``now`` AND whose status is
    ``active``. Splicing is forbidden.
    """
    for g in grants:
        if g.status != "active":
            continue
        if g.valid_from > now:
            continue
        if g.valid_until is not None and g.valid_until <= now:
            continue
        if action not in g.action_ids:
            continue
        if not g.scope.covers_target(
            unit_id=target.unit_id,
            program_id=target.program_id,
            batch_id=target.batch_id,
            session_id=target.session_id,
            participant_id=target.participant_id,
            column=target.column,
        ):
            continue
        return GrantDecision(allowed=True, grant_id=g.id, role_code=g.role_code)
    return GrantDecision(
        allowed=False,
        reason=f"No grant covers action {action.value} for the given target",
    )


# Forward reference so we don't import tenancy here (avoid circular import).
# Marker Protocol; the actual ``TenantContext`` lives in ``ksma.shared.tenancy``.
class TenantContextLike(Protocol):
    pass