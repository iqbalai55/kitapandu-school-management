# ADR-0004: Action Registry and Column-Masking Mechanics

- **Status**: Accepted
- **Date**: 2026-10-20
- **Supersedes**: none
- **See also**: ADR-0002 (aggregate boundaries), GLOSSARY (Roles, Built-in prohibitions).

## Context

The access matrix defines 73 actions across 13 roles with whole-grant evaluation (R02),
column masking (X-A01, X-A02, X-A06), and reason-required corrections (BR07).

## Decision

### Action registry

`app/core/access/registry.py` exposes `ACTION_REGISTRY: dict[str, ActionDef]` where:

```python
class ScopeKind(StrEnum):
    TENANT = "tenant"
    UNIT = "unit"
    BATCH = "batch"
    SESSION = "session"
    PARTICIPANT = "participant"
    OBJECT = "object"  # generic, e.g. attachment, invoice

class ActionDef(BaseModel):
    code: str                       # e.g. "REG-A04"
    module: ModuleCode              # "PLT" | "REG" | "CLS" | "SES" | "ASM" | "FIN" | "PRT" | "OPS" | "X"
    default_role_grants: set[RoleCode]
    scope_kind: ScopeKind
    requires_reason: bool
    requires_column_mask: bool
    description: str
```

`ActionDef` rows are seeded at first boot from the access matrix document and also written to a
shadow `action_registry` SQL table for SQL-level FK checks (per ADR-0002 Q24).

### Grant resolver

```python
def can(actor: ActorContext, action_id: str, target: TargetRef) -> Decision: ...
```

- Loads all active `AccessAssignment` rows for `actor.membership_id`.
- For each, evaluates `(assignment.role_code in action.default_role_grants
  AND target.unit_id in assignment.scope.unit_ids
  AND (assignment.scope.participant_id is None OR assignment.scope.participant_id == target.participant_id)
  AND assignment.validity.contains(now))`.
- Returns `Decision.ALLOW` if ANY single grant passes all conditions (whole-grant, R02).
- Returns `Decision.ALLOW_WITH_MASK(masked_columns=scope.column_mask)` if any passing grant has
  `column_mask` set.
- Returns `Decision.DENY(reason)` otherwise.

### Column masking

- `Scope.column_mask: set[str]` lives in `AccessAssignment.scope` JSONB column.
- Response layer helper `mask_response(payload: BaseModel, actor, object_ref) -> BaseModel`
  drops fields whose names appear in `column_mask` for the matching role.
- Per-role response schema variants (`FinanceInvoicePublic` vs `FinanceInvoiceInternal`) cover the
  common cases; `mask_response` covers the rest.

### Reason-required actions

`F08`-style corrections (`FIN-A08`, `SES-A06`, etc.) have `requires_reason=True`. The
decorator `@audited("FIN-A08", reason_required=True)` rejects the call with 400 if `reason` is
empty.

## Consequences

- Single source of truth for permission codes (code + DB). Drift is detected by CI.
- Whole-grant evaluation is expressible in one function — readable and testable.
- Per-action defaults are explicit; adding a new role-action grant is `INSERT INTO
  AccessAssignment` only.
- The dashboard's "permission summary" view (UX F07 step 3) reads
  `AccessAssignment` rows + registry defaults.

## Deferred

- Q08 decisions on auth method (MFA, password rotation, recovery token lifetime) are tracked in
  ADR-0006 (auth) which is to be written before the `identity` BC routes are implemented.