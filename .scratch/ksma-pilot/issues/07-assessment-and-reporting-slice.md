# 07: Assessment + reporting slice — rubric, assessment, report, completion, certificate

**What to build:** A complete academic lifecycle: AS creates an assessment against a published rubric, fills results per indicator with exactly one value type matching `value_kind`, submits, AC drafts + AP publishes the report (snapshot frozen at publish), AP issues a completion decision (BR09 — money alone is insufficient evidence), AP issues a certificate with sequential numbering. After this ticket, V10/V11/V12 from the data dictionary are demoable.

**Blocked by:** 00, 01, 02, 03, 04 (rubric + enrollment), 05 (session-attendance context)

**Status:** ready-for-agent

- [ ] `POST /api/v1/assessments` creates an `Assessment` against a published `RubricVersion`; rejects if the rubric version is `draft` or `retired`.
- [ ] `POST /api/v1/assessments/{id}/results` upserts an `AssessmentResult` per indicator with `numeric_value?`, `category_code?`, `narrative_value?`; rejects if more than one is set (R10, V10); rejects if the type doesn't match `value_kind`; rejects if numeric value is out of `scale` range; rejects if category isn't in the scale list; sets `result_state = 'assessed'` when exactly one value is set, `'not_assessed'` otherwise.
- [ ] `POST /api/v1/assessments/{id}/evidence` attaches an `Attachment` (file via `attachment_id`) with optional `caption`; the assessment-result reference is optional.
- [ ] `POST /api/v1/assessments/{id}/submit` checks that all `is_required` indicators are `assessed`; transitions `draft → submitted`; emits outbox event.
- [ ] `POST /api/v1/assessments/{id}/review` transitions `submitted → reviewed` with a `review_decision`.
- [ ] `POST /api/v1/assessments/{id}/supersede` creates a new `Assessment` with `supersedes_id=<old>` and marks the old `status='superseded'`.
- [ ] `POST /api/v1/reports` drafts a `ReportPublication` with `enrollment_id`, `source_assessment_ids[]`, `snapshot` JSONB, `publication` JSONB.
- [ ] `POST /api/v1/reports/{id}/publish` (ASM-A06) freezes the snapshot and appends `publisher` + `published_at` to the `publication` blob; rejects if `source_assessment_ids` reference rows from a different enrollment/unit (R01, V11).
- [ ] `POST /api/v1/reports/{id}/supersede` creates a new report version with `supersedes_id=<old>`; old `status='superseded'` (V12); the prior snapshot is preserved.
- [ ] `POST /api/v1/reports/{id}/revoke` sets `status='revoked'`, appends a `revocation` JSONB (actor + reason + at).
- [ ] `POST /api/v1/completions` records a `CompletionDecision` with `outcome ∈ {completed, remedial_required, not_completed}`, `evidence_refs[]` (BR09); rejects if `evidence_refs` is empty; rejects if any ref's `kind ∉ {report_publication, assessment}` (V11).
- [ ] `POST /api/v1/certificates` (ASM-A08) issues a `Certificate` on a `CompletionDecision(outcome='completed')` with `numbering_scope`; allocates a sequential `certificate_no` (`CERT-{n:06d}`) inside a transaction with `SELECT FOR UPDATE` on the `policy_version` for `policy_domain='certificate'`; uniqueness on `(tenant_id, numbering_scope, certificate_no)`; emits outbox event.
- [ ] `POST /api/v1/certificates/{id}/replace` issues a new certificate with `replacement_id=<old>`, marks old `status='replaced'` (BR10).
- [ ] `POST /api/v1/certificates/{id}/revoke` sets `status='revoked'` with `revocation.reason` (BR10); the original number remains queryable.
- [ ] Integration tests for: V10 indicator-type validation, V11 indicator-scope mismatch, V12 report-version chain (v2 supersedes v1, v1 snapshot intact), BR09 completion with empty evidence rejected, BR10 sequential numbering safe under two concurrent issuances (FOR UPDATE).