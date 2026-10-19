# ADR-0002: DDD Aggregate Boundaries and Cross-BC Contracts

- **Status**: Accepted
- **Date**: 2026-10-20
- **Supersedes**: none
- **See also**: ADR-0001 (pilot-acceptance scope, DDD layout)

## Decision

### Aggregate roots per BC

```
participant / Application              → root: Application
participant / Participant              → root: Participant (own invariant: dedup candidate scoring)
participant / RelatedParty             → root: RelatedParty
participant / ParticipantRelation      → root: ParticipantRelation (references Participant, RelatedParty by ID)
participant / ParticipantAccountLink   → root: ParticipantAccountLink (verification bridge)
participant / Enrollment               → root: Enrollment (transfers cross batches; R07)
participant / EnrollmentChange         → owned by Enrollment aggregate

program / Program                      → root: Program
program / ProgramVersion               → owned by Program aggregate
program / Batch                        → root: Batch (capacity invariant R06)
program / AcademicPeriod               → root: AcademicPeriod
program / TeachingAssignment           → root: TeachingAssignment (scope = Batch OR Session)

session / Session                      → root: Session (one-effective-revision R08)
session / SessionRevision              → owned by Session aggregate
session / Attendance                   → root: Attendance (unique per Session+Enrollment, R09)
session / TeachingJournal              → owned by Session aggregate
session / SessionCompletion            → owned by Session aggregate (supersedes chain)

assessment / RubricVersion             → root: RubricVersion
assessment / RubricIndicator           → owned by RubricVersion aggregate
assessment / Assessment                → root: Assessment
assessment / AssessmentResult          → owned by Assessment aggregate
assessment / AssessmentEvidence        → owned by Assessment aggregate
assessment / ReportPublication         → root: ReportPublication (immutable per publish)
assessment / CompletionDecision        → root: CompletionDecision
assessment / Certificate               → root: Certificate

finance / BillingParty                 → root: BillingParty
finance / Invoice                      → root: Invoice
finance / InvoiceLine + Installment    → owned by Invoice aggregate
finance / PaymentSubmission            → root: PaymentSubmission
finance / VerifiedPayment              → root: VerifiedPayment
finance / PaymentAllocation            → root: PaymentAllocation (separate aggregate so V16 races cleanly)
finance / Receipt                      → owned by VerifiedPayment aggregate (snapshot at issue)
finance / FinancialAdjustment          → root: FinancialAdjustment (correction primitive)

platform / Tenant                      → root: Tenant (ROOT scope)
platform / EducationUnit               → root: EducationUnit
platform / Account                     → root: Account (G scope)
platform / Membership                  → root: Membership (Tenant-scoped)
platform / AccessAssignment            → root: AccessAssignment (R02 whole-grant)
platform / ServicePrincipal            → root: ServicePrincipal (G scope)
platform / SupportGrant                → root: SupportGrant
platform / PolicyVersion               → root: PolicyVersion

portal / ServiceRequest                → root: ServiceRequest (also reused as WorkItem, see ADR-0003)
portal / InternalNotification          → root: InternalNotification
portal / CommunicationPreference       → root: CommunicationPreference

operations / Attachment                → root: Attachment
operations / ImportBatch + RowResult   → root: ImportBatch
operations / ExportJob                 → root: ExportJob
operations / MetricDefinition          → root: MetricDefinition
operations / OperationalRun            → root: OperationalRun

shared / OutboxEvent                   → root: OutboxEvent (write-only enqueued by every BC's repo)
shared / BackgroundJob                 → root: BackgroundJob
shared / ProcessedEvent                → root: ProcessedEvent
shared / IdempotencyRecord             → root: IdempotencyRecord
shared / AuditRecord                   → root: AuditRecord (append-only)
```

### Cross-BC consistency: outbox + domain events + eventual consistency

- Every BC exposes `DomainEvent` names in `app/<bc>/events.py`.
- Every cross-BC invariant (e.g., "placement spawns invoice-prep work item") is satisfied by:
  1. the source BC writes its own business row + an `OutboxEvent` in one transaction;
  2. the worker delivers the event to ACL ports in target BCs (`app/core/acl/contracts.py`);
  3. the target BC writes its own row, marks the event processed via `ProcessedEvent`.
- No BC may import another BC's models directly. Enforced by `import-linter` rule
  `contracts = BoundedContextContractList(...)`.

### Audit trail: service-layer decorator

- `app/core/audit/decorator.py` exposes `@audited(action_id, *, reason_required=False, change_ref_builder=None)`.
- Records `AuditRecord(actor, action_id, object_ref, occurred_at, change_ref, reason, authorization_ref, correlation_id)`
  in the **same transaction** as the business mutation.
- `change_ref_builder` is a callable that returns a `ChangeRef` snapshot of the prior row version.
- Operators cannot mutate `AuditRecord` through any service method; only the system
  `ServicePrincipal` can mark retention deletes after `OperationalRun` evidence is recorded.

### Reference ownership

- Shared kernel value object `core.refs.ObjectRef(kind: str, id: UUID)`.
- `core.refs.KIND_REGISTRY` lists the allowed source-table per `kind` (e.g., `enrollment` →
  `enrollment` table, `payment` → `verified_payment`).
- Cross-BC reads go through ACL ports (`acl.contracts.<verb>_<noun>(actor, ref_id)`), not direct
  table joins.

### Database naming

- Singular PascalCase table names matching the data dictionary's entity names exactly.
- `snake_case` columns.
- PK `id UUID NOT NULL DEFAULT gen_random_uuid()`.
- FK `<referenced_table_singular>_id`.
- Postgres 15 minimum.

### Time zone handling

- DB: `TIMESTAMPTZ` for `Instant`-typed columns, `DATE` for `Date`-typed columns.
- Domain: `core.time.Instant` value object (Pydantic `AwareDatetime`), `core.time.Zone` (IANA string).
- Tenant carries `default_timezone`; unit carries its own `timezone`.
- `Installment.due_date` is `Date` interpreted in `Installment.timezone`.

### Test strategy

- `tests/unit/` — pure-Python domain tests (no I/O).
- `tests/integration/` — Postgres in Docker via `testcontainers-python`, full SQLAlchemy + httpx.
- `tests/acceptance/` — `V01–V24` from the data dictionary, each a pytest scenario.
- Two seeded tenants (`Vaneta DEMO`, `Lembaga Uji DEMO`) for cross-tenant isolation tests.

### Deployment & dev loop

- `docker-compose.yml` services: `postgres:15`, `kitapandu-api`, `kitapandu-outbox-worker`,
  `mailhog` (for P0 manual-delivery simulation).
- `Makefile` targets: `up`, `down`, `migrate`, `seed`, `lint`, `test`, `test-acceptance`,
  `openapi`, `worker`.

## Consequences

- The PRD's module codes (PLT, REG, CLS, SES, ASM, FIN, PRT, OPS) and the data dictionary's 55
  entities each map to exactly one BC + aggregate root.
- R02 (whole-grant decision) and R07/R14/R16 (concurrent invariants) are expressible in SQLAlchemy
  with `SELECT … FOR UPDATE` and unique indexes.
- V01 (cross-tenant reference rejected) and V05/V16 (capacity/allocation race) are reproducible in
  integration tests.
- Adding a new BC is bounded: implement the models, schemas, service, router, ACL port, and events;
  add an import-linter rule.

## Deferred decisions

- ADR-0003: `WorkItem` reuse via `ServiceRequest` (decided inline, ADR to follow).
- ADR-0004: Action registry shape (decided inline, ADR to follow).
- ADR-0005: Seeding strategy for two pilot tenants.