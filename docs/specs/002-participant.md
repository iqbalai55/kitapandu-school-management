# Spec 002 — Participant Bounded Context

> `Participant`, `RelatedParty`, `ParticipantAccountLink`, `ParticipantRelation`,
> `Application`, `Enrollment`, `EnrollmentChange`.
> PRD module codes REG01–04, BR02, BR07, R04, R06, R07, R17, V02, V05, V06.

## Problem Statement

A school needs to register and identify who is learning. Each `Peserta` is a real
person with their own identity, possibly conflicting with another "Sari" in the same
system. Each `Peserta` has relationships (guardian, payer, sponsor) that come and
go. Their participation in classes is not a one-time event — they transfer, pause,
withdraw, and come back. Money and academic records must follow these decisions
without losing history.

The existing `participant/` and `batch/` BCs already implement most of this. This
spec records the contract that the implementation must satisfy and clarifies the
seam between participant (identity) and batch (participation).

## Solution

A `participant` bounded context with two distinct responsibility slices:

1. **Identity slice** (`ksma.participant`): `Participant`, `RelatedParty`,
   `ParticipantAccountLink`, `ParticipantRelation`. Owns who the person is, who
   they relate to, and what portal access each relation grants.
2. **Application slice** (`ksma.participant.application`): `Application` (admission
   request) and decision flow. Status transitions are governed by data dictionary
   §32.

And a `batch` bounded context (separate per existing layout):

3. **Participation slice** (`ksma.batch`): `Enrollment`, `EnrollmentChange`,
   `TeachingAssignment`. Owns the participant's effective presence in a class.

The split keeps `Enrollment`'s capacity invariants (R06, V05, V06) local to one
aggregate; the participant slice doesn't have to lock against the batch slice.

## User Stories

### Participant identity
1. As **AC**, I want to create a `Participant` profile with `participant_no`,
   `full_name`, `birth_date?`, `contact`, optional `identity_reference`, so that
   the person can be enrolled in programs.
2. As **AC**, I want `participant_no` to be unique per tenant (not globally), so
   that two tenants can both have "P-0001".
3. As **AC**, I want two participants with the same name + same family contact to
   be flagged as **duplicate candidates**, NOT auto-merged (V02), so that I can
   review before deciding to merge.
4. As **AC**, I want to correct a participant's name or contact, so that typos get
   fixed; the change is audited and the old value is preserved.
5. As **AC**, I want to archive a participant (`status='archived'`) but never delete
   them, so that historical invoices, reports, and certificates still attribute.

### Related parties and relations
6. As **AC**, I want to create a `RelatedParty` (person or organization) so that
   I can attach a guardian, payer, or sponsor to a participant.
7. As **AC**, I want to verify a guardian relation by capturing minimum evidence
   (`verification.evidence_ref`) and the actor/time of approval, so that BR02
   (guardian, payer, sponsor, account are different relations) is auditable.
9. As **AC**, I want a guardian (`academic_guardian`) relation to grant portal
   access to the academic portal, but I want a separate `payer` relation to be
   required before invoices are visible — so that the access matrix's
   GU/PY separation is enforced.
10. As **AC**, I want a payer relation's `financial_scope` to be either
    `all_approved_obligations` or an explicit `invoice_id[]`, so that a payer
    can be scoped narrowly to one obligation.
11. As **AC**, I want to revoke a relation (`status='revoked'`) when the
    relationship changes (divorce, child turns adult, etc.), so that the next
    portal request uses fresh authority (R03).

### Account linking
12. As a **participant**, I want to link my login account to my participant profile
    via a verified `ParticipantAccountLink`, so that the portal recognises me.
13. As **AC**, I want a `ParticipantAccountLink` to require `verification` (officer,
    time, evidence ref) before `status='active'`, so that I can't impersonate.
14. As any **user**, I want shared name/email/NIK to NOT be enough to auto-link,
    so that I can't accidentally gain access to someone else's record.

### Application (admission request)
15. As **AC**, I want to create a `Application` for a participant against a
    program, with a `completeness` checklist, so that I can collect missing
    data incrementally.
16. As **AC**, I want to submit a draft application (`status='draft' → 'submitted'`),
    so that I can save and return later without losing the data.
17. As **AC**, I want to mark a submitted application `under_review`, then
    `approved` or `rejected` with a `decision` (actor + reason), so that the
    outcome is traceable.
18. As **AC**, I want to withdraw a draft or submitted application
    (`status='withdrawn'`) without losing history.
19. As **AC**, I want my approval to create an `Enrollment` (separate status, not
    automatic per UX F01), so that approval and invoicing are independent events.

### Enrollment
20. As **AC**, I want to place an approved participant on a `Batch` with a
    `effective_range` and `status='planned'`, then activate on the start date.
21. As **AC**, I want a capacity check at placement that uses effective
    occupancy at `effective_range.start`, so that a batch doesn't overflow
    (R06, V05).
22. As **AC**, I want two concurrent placements on the last seat to be safe:
    exactly one succeeds, the other gets 409 with the fresh capacity (V05).
23. As **AC**, I want to pause an enrollment (`status='paused'`) without losing
    its place; the participant resumes on the same batch.
24. As **AC**, I want to transfer an enrollment to another batch in the same
    tenant with `EnrollmentChange(change_kind='transfer')`; the source enrollment
    closes with `status='transferred'` and points at the new one.
25. As **AC**, I want overlapping enrollments on the same `(participant, batch)`
    pair to be rejected (R07, V06), so that a participant cannot be doubly placed.

### Teaching assignments
26. As **AC**, I want to assign an `instructor` (or `assessor`, `approver`,
    `substitute`) to a batch or to a specific session, so that the right person
    is on the schedule.
27. As **AC**, I want a `TeachingAssignment` to be scoped to either the whole
    batch (no `session_id`) OR a specific session (`session_id` filled, must
    belong to the same batch).
28. As **AC**, I want to revoke a `TeachingAssignment` (`status='revoked'`) and
    have the instructor's session-level access drop on the next request.

## Implementation Decisions

### Schema (already migrated, see `alembic/versions/0003_participant.py` and `0005_batch.py`)
- `participant(id, tenant_id, participant_no, full_name, birth_date?, contact?, identity_reference?,
  status, support_needs_note?, …)`. Unique `(tenant_id, participant_no)`.
- `related_party(id, tenant_id, party_type, display_name, contact?, organization_ref?, status, …)`.
- `participant_account_link(id, tenant_id, account_id, participant_id?, related_party_id?,
  verification JSONB, validity, status, …)`. Exactly one of `participant_id`/`related_party_id`
  is set.
- `participant_relation(id, tenant_id, participant_id, related_party_id, relation_kind
  ('academic_guardian'|'payer'), financial_scope?, verification JSONB, validity, status, …)`.
  Unique `(participant_id, related_party_id, relation_kind)`.
- `application(id, tenant_id, participant_id, program_id, requested_start?, completeness JSONB,
  status, decision?, source_ref?, …)`.
- `enrollment(id, tenant_id, participant_id, batch_id, application_id?, effective_range
  tstzrange, status, policy_id, …)`. Unique `(tenant_id, participant_id, batch_id)` plus
  exclusion constraint preventing overlapping `effective_range` per (participant, batch)
  using `btree_gist`.
- `enrollment_change(id, tenant_id, source_enrollment_id, target_enrollment_id?,
  change_kind ('transfer'|'pause'|'resume'|'withdraw'), effective_at, decision JSONB,
  status, …)`.
- `teaching_assignment(id, tenant_id, membership_id, batch_id, session_id?,
  assignment_kind ('instructor'|'substitute'|'assessor'|'approver'), component_codes?,
  validity tstzrange, status, …)`. Check: if `session_id` filled, it must belong to `batch_id`.

### Capacity invariant (R06)
- Placement runs inside a single transaction:
  1. `SELECT capacity FROM batch WHERE id=:bid AND tenant_id=:tid FOR UPDATE`.
  2. `SELECT count(*) FROM enrollment WHERE batch_id=:bid AND tenant_id=:tid
     AND status IN ('planned','active') AND effective_range && tstzrange(now(), infinity)`.
  3. If `count >= capacity` → 409 `CAPACITY_EXCEEDED` with `current_capacity`, `occupied`.
  4. INSERT enrollment row.

### Duplication candidates (V02)
- `participant_search(q)` returns participants whose normalised `full_name` or
  `contact` matches `q`. The response includes `duplicate_suspect: true` if
  `phonetic_match_score > 0.8` AND same `contact_normalised`. The API does not
  auto-merge; it returns candidate IDs for the admin to review.

### Cross-BC evidence: portal UX F01 step 5
- When `EnrollmentChange(change_kind='transfer'|'pause'|'resume'|'withdraw')`
  commits, the participant slice emits `EnrollmentChanged` outbox event.
- The portal BC subscribes and creates a `ServiceRequest(category='finance',
  source_ref={kind:'enrollment_change', id:…}, description='Placement changed —
  finance review needed')` for finance (FI) to action.
- FI then creates the invoice via the finance BC (separate status, not automatic).

### Seams
- **Inbound**: every BC that names a participant imports the `ObjectRef(kind='participant',
  id=…)` value object — never a direct row.
- **Outbound**: participant imports `program.domain.Program` for application validation,
  `batch.domain.Batch` for enrollment placement, and `shared.tenancy.TenantContext`.
- **ACL exports**: `participant.acl.allowed_unit_ids(account_id) -> set[UUID]`
  for portal filtering.

## Testing Decisions

What makes a good test:
- Asserts **DB-visible state** after the API call (the row was created, the audit row
  was written, the outbox event was emitted).
- V02 (duplicate candidates): assert that creating two participants with same name +
  same contact returns both candidates with `duplicate_suspect=true`, not a single row.
- V05 (capacity race): spawn 2 concurrent placements; one succeeds, the other gets
  409 with `current_capacity`/`occupied`.
- V06 (overlapping enrollments): second `enrollment.create` with overlapping range
  on same `(participant, batch)` returns 409 `STATE_CONFLICT`.
- R02 (whole grant): AC at unit A can't read participant at unit B → 403 empty body.
- Transfer: `EnrollmentChange(transfer)` closes source with `status='transferred'`,
  links `target_enrollment_id`, and emits `EnrollmentChanged` outbox event.

Existing tests: none. This spec implies `tests/integration/test_participant_*.py`
and `tests/integration/test_enrollment_*.py`.

## Out of Scope

- Automated profile merge (BR02 forbids; manual review only).
- Cross-tenant profile reuse (ADR-0004: profile is per-tenant, no cross-tenant reuse).
- Birth-date-influencing enrolment age bands (Q02 pending).
- Self-service application submission (PRD allows it but defers; admin-assisted for now).

## Further Notes

- The existing `src/ksma/participant/` and `src/ksma/batch/` folders together
  contain ~6,700 lines and cover the full REG + CLS placement flows. This spec
  documents that contract. Remaining work:
  1. Add `btree_gist` extension migration (already in `0001_extensions_and_tenant_root.py`).
  2. Add the exclusion constraint for overlapping enrollments.
  3. Verify that placement uses `FOR UPDATE` lock semantics (current code may rely on
     count-then-insert without locking — needs review against V05).
  4. Add unit + integration tests covering V02, V05, V06.
  5. Wire the `EnrollmentChanged` → `ServiceRequest` outbox handler.