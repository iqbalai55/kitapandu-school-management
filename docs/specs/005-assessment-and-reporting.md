# Spec 005 — Assessment and Reporting Bounded Context

> `Assessment`, `AssessmentResult`, `AssessmentEvidence`, `ReportPublication`,
> `CompletionDecision`, `Certificate`. PRD module codes ASM01–04, BR09, BR10,
> R10, R11, V10, V11, V12.

## Problem Statement

A school needs to grade against a rubric that evolves, publish reports that
free at the moment of issuance, decide completion based on evidence (not just
payment or attendance, BR09), and issue internal certificates whose number
preserves history (BR10). The assessment is a draft, then submitted, then
reviewed; revisions create new versions superseding the old.

## Solution

Two adjacent BCs in the existing layout (per ADR-0002 + the previous session):
- `ksma.assessment` owns `Assessment`, `AssessmentResult`, `AssessmentEvidence`.
- `ksma.reporting` owns `ReportPublication`, `CompletionDecision`, `Certificate`.

This split exists because `reporting` has its own approval workflow and
certificate numbering rules that don't belong inside `assessment`. The seam
is: assessment produces source rows; reporting freezes them into publications
and decisions.

## User Stories

### Assessment
1. As **AS**, I want to create an `Assessment` for a participant's enrollment
   against a published `RubricVersion` so that the result is pinned to a rubric.
2. As **AS**, I want to set `AssessmentResult` rows per indicator with exactly
   one value type matching the indicator's `value_kind` (V10) — numeric in
   range, category in list, narrative within bounds.
3. As **AS**, I want to attach `AssessmentEvidence` (file via `Attachment`) with
   a caption so that the result has supporting evidence.
4. As **AS**, I want to submit an assessment (`status='draft' → 'submitted'`)
   once all required indicators are filled (V11).
5. As **AC**, I want to review a submitted assessment (`status='reviewed'`),
   recording the decision.
6. As **AS**, I want to supersede an assessment with a new one pointing at
   the old via `supersedes_id`, so that corrections are versioned, not silent
   (R11).

### Reporting
7. As **AC**, I want to draft a `ReportPublication` with `enrollment_id`,
   `source_assessment_ids[]`, `snapshot`, and a publication metadata blob.
8. As **AP**, I want to publish a report (`status='published'`); the snapshot
   freezes at publish time, with `publisher` and `published_at` recorded (R11).
9. As **AP**, I want to revise a published report by issuing a new version
   that links back via `supersedes_id`; the old version is kept with
   `status='superseded'` (V12).
10. As **AP**, I want to revoke a report (`status='revoked'`) with a `reason`;
    the old version is preserved with `revocation` JSONB (R11).
11. As any **portal user**, I want the portal to show only published reports
    that the reader is entitled to (R11, R17).

### Completion
12. As **AP**, I want to record a `CompletionDecision` with `outcome`
    ('completed'|'remedial_required'|'not_completed'), `evidence_refs[]`,
    `policy_id`, and a `decision` blob (actor + time + reason).
13. As **AP**, I want completion to require at least one evidence_ref
    (BR09) — money or session count alone is NOT sufficient (V09 of the data
    dictionary).
14. As **AP**, I want `evidence_refs` to be typed (`kind='report_publication'`
    or `kind='assessment'`), so that bad refs are rejected at write-time.
15. As **AP**, I want to supersede a completion decision via `supersedes_id`
    so that revised decisions keep history.

### Certificate
16. As **AP**, I want to issue a `Certificate` on a `CompletionDecision`
    with `outcome='completed'`, with `numbering_scope`, `certificate_no`,
    `snapshot`, and `status='issued'`.
17. As **AP**, I want certificate numbering to be unique per
    `(tenant_id, numbering_scope, certificate_no)`, sequential within scope.
19. As **AP**, I want to replace a certificate (`status='replaced'`) with a
    new one linked via `replacement_id`; the new row keeps the original's
    `completion_id` and `numbering_scope`, the original's number is preserved.
20. As **AP**, I want to revoke a certificate with a `reason`; the row's
    `status='revoked'` and `revocation` JSONB preserve the trail (BR10).
21. As **AP**, I want issuance to NOT claim external certification; the
    pilot keeps certificates internal only (Q12 pending).

## Implementation Decisions

### Schema (already migrated, see `alembic/versions/0008_assessment_reporting.py`)
- `assessment(id, tenant_id, unit_id, enrollment_id, rubric_version_id, assessor_id,
  assessment_date, status ('draft'|'submitted'|'reviewed'|'superseded'),
  supersedes_id?, …)`. CHECK `assessment_date <= current_date`.
- `assessment_result(id, tenant_id, assessment_id, indicator_id, numeric_value?,
  category_code?, narrative_value?, result_state ('not_assessed'|'assessed'|'not_applicable'),
  follow_up?, …)`. Unique `(assessment_id, indicator_id)`. CHECK: at most one of
  `numeric_value`, `category_code`, `narrative_value` is set (R10).
- `assessment_evidence(id, tenant_id, assessment_id, result_id?, attachment_id, caption?,
  visibility ('internal_assessment'), …)`.
- `report_publication(id, tenant_id, unit_id, enrollment_id, publication_no, source_assessment_ids[],
  snapshot JSONB, publication JSONB, attachment_id?, supersedes_id?, status
  ('published'|'superseded'|'revoked'), …)`. Unique `(enrollment_id, publication_no)`.
- `completion_decision(id, tenant_id, unit_id, enrollment_id, outcome ('completed'|'remedial_required'|'not_completed'),
  evidence_refs JSONB, policy_id?, decision JSONB, supersedes_id?, …)`.
- `certificate(id, tenant_id, unit_id, completion_id, numbering_scope, certificate_no,
  snapshot JSONB, attachment_id?, status ('issued'|'replaced'|'revoked'), replacement_id?,
  revocation JSONB?, …)`. Unique `(tenant_id, numbering_scope, certificate_no)`.

### One-valued-result invariant (R10, V10)
- Application layer enforces `set_values == 1` and value-kind match (existing
  `assessment.application.set_result` does this).
- DB CHECK is the belt-and-braces: `((numeric_value IS NOT NULL)::int +
  (category_code IS NOT NULL)::int + (narrative_value IS NOT NULL)::int) <= 1`.

### Publish freezes the snapshot (R11)
- `report.publish()` does NOT mutate `snapshot`; it appends to `publication` JSONB:
  `{"publisher": actor, "published_at": now}`.
- All `report_publication.source_assessment_ids` are validated against the
  `enrollment_id`'s `unit_id` so cross-unit publication is impossible (R01).

### Completion requires evidence (BR09, V11)
- `completion_decision.evidence_refs` must be non-empty.
- Each ref must have `kind ∈ {'report_publication', 'assessment'}` and an `id` UUID.
- Validation cross-checks the referenced row exists in the same tenant.

### Certificate numbering (BR10, R11)
- `CertificateRepository.next_certificate_no(scope)` reads `MAX` of numeric suffix,
  returns `CERT-{n+1:06d}`. Q12 will replace with the agreed numbering policy.
- Issuance runs in a transaction with `SELECT … FOR UPDATE` on the `policy_version`
  for `policy_domain='certificate'` (when Q12 is settled).
- Replacement: write new certificate first, then `UPDATE old SET status='replaced',
  replacement_id=new.id`.

### Seams
- **Inbound**: portal reads `report_publication` (only `status='published'` and
  relation-entitled); finance reads nothing.
- **Outbound**: assessment imports `program.domain.RubricVersion`,
  `participant.domain.Enrollment`; reporting imports `participant.domain.Enrollment`,
  `identity.domain.PolicyVersion` (for `policy_id`).
- **ACL exports**: `assessment.acl.assessments_for_enrollment(enrollment_id, requester_id)`
  for the portal.

## Testing Decisions

What makes a good test:
- V10: `set_result` with two value kinds at once → 400; numeric out of range → 400;
  zero is accepted when allowed by scale.
- V11: assessment referencing an indicator from another rubric → 409 `INDICATOR_SCOPE_MISMATCH`.
- V12: publishing v2 of a report keeps v1's snapshot intact; v1's `status='superseded'`.
- BR09: `decide_completion` with empty `evidence_refs` → 400; with `kind='payment'` →
  400 `INVALID_EVIDENCE_KIND`.
- BR10: certificate numbering within scope is sequential and unique across two
  parallel `issue_certificate` calls (use a transaction with `FOR UPDATE` on the
  `policy_version` row).
- Revocation: revoke a certificate, the row's `status='revoked'` and `revocation`
  JSONB has actor+reason+at; the prior `status='issued'` is preserved on the
  revision chain (via `supersedes_id`).

## Out of Scope

- Public certificate verification link (Q12 pending; pilot keeps certificates private).
- Cross-rubric indicator reuse (each `rubric_version` owns its indicators).
- Auto-completion based on attendance count (BR09 forbids).
- Report templates (PRD allows but pilot uses snapshot dict directly).

## Further Notes

- The `reporting` BC in the existing codebase is missing `repositories.py`,
  `application/__init__.py`, and `interface/__init__.py`. The domain entities
  are present. This is the single biggest gap in the codebase and should be
  filled before any other testing.
- The `assessment/application` and `assessment/infrastructure` layers are
  complete; `reporting` needs to mirror that structure.
- Remaining work:
  1. Implement `reporting/infrastructure/repositories.py` (use the
     existing pattern from `assessment/infrastructure/repositories.py`).
  2. Move the reporting-specific functions out of `assessment/application/__init__.py`
     into `reporting/application/__init__.py` so the BC is self-contained.
  3. Implement `reporting/interface/__init__.py` with `/reports/{id}/publish`,
     `/reports/{id}/revoke`, `/completions`, `/certificates`, etc.
  4. Add unit tests for `Scale.validate_value` (already referenced from
     `assessment.application.set_result`).
  5. Add integration tests for V10, V11, V12.