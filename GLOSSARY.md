# GLOSSARY — KitaPandu School Management (KSMA)

Canonical terms taken verbatim from the KSMA v1.0 documents
(PRD, Architecture & Data Model, Access Control Matrix, Data Dictionary & Validation, UX Blueprint).
This glossary uses the Indonesian/PascalCase vocabulary from the source documents; it is the single
source of truth for naming in the codebase.

> Status legend: **[confirmed in docs]** = term defined and consistent across the v1.0 documents.
> **[deferred Q01–Q14]** = term is defined but operational policy is pending.

---

## Top-level scope

- **Pelanggan** (a.k.a. **Tenant**, **Lembaga**, **Customer**) — Root isolation boundary for all data. Two
  tenants' data must NEVER cross. Pilot tenants: `Vaneta DEMO` and `Lembaga Uji DEMO`. **[confirmed in docs]**
- **Satuan** (a.k.a. **EducationUnit**, **Unit**) — Operating education unit within a tenant. Has its own
  `unit_code`, `institution_type`, `education_levels`, `timezone`, `status`. **[confirmed in docs]**
- **Jenjang** — Education level: `PAUD`, `TK`, `SD`, `SMP`, `SMA`, `SLB`. Stored separately from `institution_type`. **[confirmed in docs]**
- **Jenis satuan / institution_type** — Type of education unit (PAUD/TK, SD, SMP, SMA, SLB, LK/LKP, LPK, PKBM). Distinct from `jenjang`. **[confirmed in docs]**

## Identity & access

- **Akun / Account** — Global login identity (G scope). Has `auth_subject`, `auth_namespace`, `status`
  (`pending`/`active`/`disabled`), `authz_revision`. Does NOT auto-unify participant profiles across tenants. **[confirmed in docs]**
- **Membership** — Verified link from `Account` to `Tenant` with `validity` interval and `status`
  (`invited`/`active`/`suspended`/`revoked`). One logical membership per `(tenant_id, account_id)`. **[confirmed in docs]**
- **AccessAssignment / Grant** — Per-membership permission grant with `role_code`, `action_ids` (CodeList),
  `scope`, `validity`, `policy_id`, `authorization` decision. Whole-grant decision only — do NOT splice
  actions from one grant with scope of another (rule R02). **[confirmed in docs]**
- **ServicePrincipal / Identitas layanan teknis (SV)** — Non-interactive global identity for backup,
  retention, audit jobs. Bounded by purpose, target, environment. Not for human portal use. **[confirmed in docs]**
- **SupportGrant / Akses dukungan platform** — Time-bound operator-assistance grant on customer content
  with `operator_account_id`, `ticket_ref`, `scope`, `approved_by`, `validity`, `status`. **[confirmed in docs]**
- **PolicyVersion** — Versioned operational configuration with `policy_domain`, `unit_scope_id`,
  `program_scope_id`, `version_no`, `effective_range`, `ruleset`, `approval`, `status`. **[confirmed in docs]**
- **Roles (13)** — `OW` Pimpinan, `AM` Pengelola akses, `AC` Admin akademik, `FI` Keuangan, `IN` Pengajar,
  `AS` Penilai, `AP` Pengesah akademik, `AU` Pemeriksa, `ST` Peserta, `GU` Wali akademik, `PY` Pembayar,
  `OP` Operator platform, `SV` Identitas layanan teknis. **[confirmed in docs]**
- **Built-in prohibitions** — Cross-tenant, draft-status, active revocations cannot be bypassed by
  role-combining. **[confirmed in docs]**
- **Default-deny** — OWASP-aligned: unclear permission or unavailable context → DENY. **[confirmed in docs]**

## Participant & relations

- **Peserta / Participant** — Tenant-local profile. `participant_no`, `full_name`, optional `birth_date`,
  `contact`, `identity_reference`, `status` (`active`/`inactive`/`archived`). **[confirmed in docs]**
- **RelatedParty** — A party related to a participant: guardian, payer, organization sponsor. `party_type`
  `person` or `organization`. **[confirmed in docs]**
- **ParticipantAccountLink** — Verified bridge from Account to either `Participant` or `RelatedParty`.
  Mutually exclusive. Same name/email/NIK is NOT automatic link. **[confirmed in docs]**
- **ParticipantRelation** — Relation between Participant and RelatedParty with `relation_kind`
  (`academic_guardian` or `payer`), optional `financial_scope`, `verification`, `validity`, `status`
  (`pending`/`active`/`expired`/`revoked`). Academic relation does NOT auto-open invoices; payer does NOT
  open grades. **[confirmed in docs]**

## Program & class

- **Program** — Unit-owned definition: `program_code`, `name`, `service_group` (`language`/`health`/
  `technology`/config), `status` (`draft`/`active`/`inactive`). **[confirmed in docs]**
- **ProgramVersion** — Versioned curriculum: `program_id`, `version_no`, `learning_objectives`,
  `curriculum`, optional `effective_range`, `status` (`draft`/`published`/`retired`). **[confirmed in docs]**
- **AcademicPeriod** — `period_code`, `name`, `period_kind` (`course_window`/`academic_year`/`semester`/config),
  `date_range`, `status` (`planned`/`active`/`closed`). **[confirmed in docs]**
- **Batch** — Class cohort: `batch_code`, `program_version_id`, optional `period_id`, `capacity`,
  `delivery_mode` (`onsite`/`online`/`hybrid`), `service_range`, `status` (`planned`/`open`/`running`/
  `closed`/`cancelled`). One batch references ONE `ProgramVersion`. **[confirmed in docs]**
- **Application / Permohonan** — Admission request: `participant_id`, `program_id`, `completeness`,
  `status` (`draft`/`submitted`/`under_review`/`approved`/`rejected`/`withdrawn`), `decision`. **[confirmed in docs]**
- **Enrollment** — Active participation in a batch: `participant_id`, `batch_id`, optional `application_id`,
  `effective_range`, `status` (`planned`/`active`/`paused`/`completed`/`withdrawn`/`transferred`), `policy_id`.
  Duplicate active enrollment on same `(participant, batch)` with overlapping range is rejected (R07). **[confirmed in docs]**
- **EnrollmentChange** — Decision: `source_enrollment_id`, optional `target_enrollment_id`, `change_kind`
  (`transfer`/`pause`/`resume`/`withdraw`), `effective_at`, `decision`, `status`. **[confirmed in docs]**
- **TeachingAssignment** — Per-batch or per-session assignment: `membership_id`, `batch_id`,
  optional `session_id`, `assignment_kind` (`instructor`/`substitute`/`assessor`/`approver`),
  optional `component_codes`, `validity`, `status`. **[confirmed in docs]**

## Session & learning delivery

- **Session / Sesi / Pertemuan** — Logical meeting identity: `batch_id`, `session_no`, optional
  `effective_revision_id`, `lifecycle_status` (`planned`/`scheduled`/`cancelled`/`closed`), optional
  `closure_id`, optional `reschedule_reason`. Cancelled sessions do NOT count as completed (BR04). **[confirmed in docs]**
- **SessionRevision** — Actual schedule version of one session: `session_id`, `revision_no`, `starts_at`,
  `ends_at`, `timezone`, optional `location`, `change_reason`, optional `published_at`. One effective revision
  per session (R08). **[confirmed in docs]**
- **Attendance / Absensi** — Per-session per-enrollment: `session_id`, `enrollment_id`, optional
  `attendance_code` (empty = `belum_diisi`, NOT absent), optional `note`, optional `recorded_at`,
  optional `recorded_by`, optional `correction_reason`. Unique on `(tenant_id, session_id, enrollment_id)`. **[confirmed in docs]**
- **Attendance codes** — `hadir`, `izin`, `sakit`, `tidak_hadir`, `terlambat`; `belum_diisi` is the
  data-completeness state (BR05). **[confirmed in docs]**
- **TeachingJournal / Jurnal mengajar** — Per-session journal: `session_id`, `actual_instructor_id`,
  `topic`, optional `activities`, optional `internal_note` (restricted), optional `portal_summary`,
  optional `follow_up`. **[confirmed in docs]**
- **SessionCompletion / Pengesahan pelaksanaan** — Closure decision: `session_id`, `completion_no`,
  `decision`, `checklist_snapshot`, `policy_id`, optional `supersedes_id`, optional `correction_reason`. **[confirmed in docs]**

## Assessment, reports & completion

- **RubricVersion** — Versioned rubric tied to `ProgramVersion`: `version_no`, `name`, `status`, `published_at`,
  `approval`. **[confirmed in docs]**
- **RubricIndicator** — One typed indicator: `indicator_code`, `label`, `value_kind`
  (`numeric`/`categorical`/`narrative`), `scale`, optional `weight`, `is_required`. **[confirmed in docs]**
- **Assessment** — One assessment event: `enrollment_id`, `rubric_version_id`, `assessor_id`,
  `assessment_date`, `status` (`draft`/`submitted`/`reviewed`/`superseded`), optional `supersedes_id`. **[confirmed in docs]**
- **AssessmentResult** — One indicator's value: `assessment_id`, `indicator_id`, exactly one of
  `numeric_value`/`category_code`/`narrative_value`, `result_state` (`not_assessed`/`assessed`/`not_applicable`),
  optional `follow_up`. Empty value ≠ zero. **[confirmed in docs]**
- **AssessmentEvidence / Bukti** — Private evidence link: `assessment_id`, optional `result_id`,
  `attachment_id`, optional `caption`, `visibility` (`internal_assessment`). **[confirmed in docs]**
- **ReportPublication / Laporan** — Frozen published version: `enrollment_id`, `publication_no`,
  `source_assessment_ids`, `snapshot`, `publication`, optional `attachment_id`, optional `supersedes_id`,
  `status` (`published`/`superseded`/`revoked`). Drafts never appear as `published`. **[confirmed in docs]**
- **CompletionDecision / Penyelesaian program** — Outcome: `enrollment_id`, `outcome`
  (`completed`/`remedial_required`/`not_completed`), `evidence_refs`, `policy_id`, `decision`,
  optional `supersedes_id`. Money/attendance count alone does NOT yield `completed` (BR09). **[confirmed in docs]**
- **Certificate / Sertifikat internal** — Internal completion certificate: `completion_id`, `numbering_scope`,
  `certificate_no`, `snapshot`, optional `attachment_id`, `status` (`issued`/`replaced`/`revoked`),
  optional `replacement_id`, optional `revocation`. Unique per `(tenant, numbering_scope)`. **[confirmed in docs]**

## Finance

- **BillingParty** — Party to be billed / paying. **[confirmed in docs]**
- **Invoice / Tagihan** — `participant_id`, optional `invoice_no` (required on `issued`, unique per tenant),
  `currency`, `document_status` (`draft`/`issued`/`void`), optional `issue_date`, `total_amount`, `policy_id`,
  optional `payer_party_id`. **Paid / overdue is a projection of balance + due date, NOT a `document_status`**. **[confirmed in docs]**
- **InvoiceLine** — `invoice_id`, `line_no`, `description`, optional `enrollment_id`, `quantity`,
  `unit_price`, `discount_amount`, `net_amount`. **`net_amount` non-negative**. **[confirmed in docs]**
- **Installment / Cicilan** — `invoice_id`, `sequence_no`, `due_date`, `due_amount`, `timezone`.
  Sum of installments = issued total. **[confirmed in docs]**
- **PaymentSubmission / Bukti transfer** — Payer claim: `participant_id`, optional `invoice_id`,
  `submitted_by`, `claimed_amount`, optional `transfer_at`, optional `proof_attachment_id`, optional
  `transfer_reference`, `status` (`submitted`/`under_review`/`verified`/`rejected`), optional
  `review_decision`. Upload does NOT reduce invoice (BR06). **[confirmed in docs]**
- **VerifiedPayment / Pembayaran terverifikasi** — Funds recognized: `submission_id`, `participant_id`,
  `recognized_amount`, `currency`, `verification`, `received_date`, `reversal_total`. Original receipt
  value not overwritten. **[confirmed in docs]**
- **PaymentAllocation / Alokasi pembayaran** — `payment_id`, `installment_id`, `amount`, `allocated_at`,
  `allocated_by`, `idempotency_id`. Amount ≤ available funds AND ≤ installment remainder (R14). **[confirmed in docs]**
- **Receipt / Kuitansi** — Frozen-on-issue receipt: `payment_id`, `allocation_ids`, `receipt_no`,
  `snapshot`, optional `attachment_id`, `status` (`issued`/`replaced`/`void`), optional `supersedes_id`. **[confirmed in docs]**
- **FinancialAdjustment / Koreksi** — `adjustment_kind` (`invoice_credit`/`allocation_reversal`/
  `payment_reversal`), `source_ref`, optional `installment_id`, `amount`, `decision`, `effective_at`,
  `idempotency_id`, `policy_id`. **[confirmed in docs]**

## Portal, communication & requests

- **ServiceRequest / Permintaan layanan** — Ticket: `requester_account_id`, `participant_id`, `category`
  (`academic`/`schedule`/`identity`/`finance`), optional `source_ref`, `description`, optional
  `assigned_membership_id`, `status` (`open`/`in_review`/`approved`/`rejected`/`closed`), optional `resolution`.
  Does NOT directly mutate source. **[confirmed in docs]**
- **InternalNotification** — In-app message: `recipient_account_id`, `source_ref`, `event_id`,
  `notification_kind`, `summary`, `delivery_state` (`pending`/`available`/`suppressed`), optional `read_at`.
  Unique on `(event, recipient, kind)`. **[confirmed in docs]**
- **CommunicationPreference** — Opt-in per `(account, optional participant, category, channel)`. P0 channels
  are `internal` and `manual_template`. **[confirmed in docs]**
- **Peserta dewasa** — Adult participant not required to have GU. Sponsor PY retains only financial scope. **[confirmed in docs]**

## Operations

- **ImportBatch** — `requested_by`, `dataset_kind`, `source_attachment_id`, `mapping`, `scope`, `status`
  (`uploaded`/`validating`/`needs_correction`/`ready`/`committed`/`failed`), optional `commit_request_id`,
  `counts`. Atomic commit: all rows ready or no commit effect (R18). **[confirmed in docs]**
- **ImportRowResult** — Per-row: `import_batch_id`, `row_no`, `row_fingerprint`, `outcome`
  (`valid`/`invalid`/`duplicate_candidate`/`committed`), optional `errors`, optional `target_ref`,
  optional `source_reference`. **[confirmed in docs]**
- **ExportJob** — `requester_account_id`, `dataset_kind`, `scope`, `filters`, `as_of`, `status`
  (`queued`/`running`/`ready`/`failed`/`cancelled`/`expired`), optional `result_attachment_id`, optional
  `expires_at`. Rights re-checked at request, execution, download (R19). **[confirmed in docs]**
- **MetricDefinition** — Versioned KPI: `metric_code`, `version_no`, `label`, `source_spec`, `time_basis`,
  `effective_range`, `approval`. No free user query expressions. **[confirmed in docs]**
- **OperationalRun** — Ops evidence: `operation_kind` (`backup`/`restore_test`/`restore`/`retention`/
  `reconciliation`), `environment`, `execution_subject`, `authorization_ref`, `started_at`, optional
  `finished_at`, `status`, optional `evidence`. **[confirmed in docs]**

## Shared & infrastructure

- **Attachment** — Private file: `owner_ref`, `storage_key`, `original_name`, `media_type`, `size_bytes`,
  optional `content_hash`, `check_status` (`pending`/`quarantined`/`approved`/`rejected`/`deleted`),
  optional `retention_policy_id`. Initial 10 MB limit (Q11). **[confirmed in docs]**
- **OutboxEvent** — Event committed with source transaction: `event_type`, `source_ref`, `source_version`,
  `payload`, `occurred_at`, `dispatch_state`, `correlation_id`. Drafted with ADR06. **[confirmed in docs]**
- **BackgroundJob** — Retriable async work: optional `event_id`, `job_kind`, `execution_subject`,
  `scope`, `status` (`queued`/`running`/`succeeded`/`retry_wait`/`failed`/`cancelled`), `attempt_count`,
  optional `next_attempt_at`, optional `result_ref`. **[confirmed in docs]**
- **ProcessedEvent** — Consumer-side dedup: `consumer_code`, `event_id`, `effect_ref`, `processed_at`.
  Unique on `(tenant_id, consumer_code, event_id)` (R20). **[confirmed in docs]**
- **IdempotencyRecord** — Anti-repeat: `request_subject`, `action_id`, `key_fingerprint`, `request_hash`,
  `status`, optional `result_ref`, `expires_at`. Unique on `(tenant, requester, action, key)`. Same key +
  different input hash is rejected (R21). **[confirmed in docs]**
- **AuditRecord / Jejak perubahan** — Append-only trail: `actor`, `action_id`, `object_ref`,
  `occurred_at`, optional `change_ref`, optional `reason`, `authorization_ref`, `correlation_id`. No
  passwords/tokens/file content in audit. **[confirmed in docs]**

## Money formulas (R12, R14, R15)

- **Effective allocation** = original allocation − valid allocation reversals.
- **Available funds** = `recognized_amount` − `payment_reversal` − effective allocations.
- **Effective installment obligation** = `due_amount` − `invoice_credit` on that installment.
- **Installment remainder** = effective obligation − effective allocations to that installment.
- **Tunggakan (arrears)** = sum of installment remainders that have `due_date ≤ today`. NOT total invoice
  balance. **[confirmed in docs]**
- **Bank refund is NOT an automatic effect of `payment_reversal`.** (Q04) **[deferred Q01–Q14]**

## Key business rules (BR01–BR12)

- **BR01** — Customer + unit always checked; context switching gives no auto-access.
- **BR02** — Guardian, payer, sponsor, participant account are DIFFERENT relationships.
- **BR03** — One active enrollment = one seat in a batch per its effective date.
- **BR04** — Cancelled session ≠ completed service; doesn't auto-use session rights.
- **BR05** — Empty attendance ≠ absence; closure needs authorized action.
- **BR06** — Payments reduce billed amount after verification AND allocation.
- **BR07** — Money, grades, published docs corrected via traceable trail.
- **BR08** — Arrears do NOT auto-stop learning or change grades.
- **BR09** — Completion not by payment alone; academic decision with evidence.
- **BR10** — Certificate stores publisher and document type + active/revoked.
- **BR11** — Internal message, manual delivery, provider confirmation are different statuses.
- **BR12** — Import/export have source, mapping, filter, time, actor.

## Open decisions (deferred policy — DO NOT invent values)

- **Q01** — Official identity, unit, location, Vaneta `jenis satuan` for pilot.
- **Q02** — Age groups, mandatory data, guardian/payer grants and revocation.
- **Q03** — First programs, capacity, period, curriculum, mode, placement rules.
- **Q04** — Tariff, discount, installment, due date, transfer check, variance, arrears.
- **Q05** — Leave, cancellation, substitute, expiry, learning-credit usage.
- **Q06** — Attendance status, mandatory journal data, session closure, correction rights.
- **Q07** — Rubric, scale, evidence, completion, remedial, report approver.
- **Q08** — Real roles, role merging, correction authorization, support access, authentication.
- **Q09** — Channel, recipients, preferences, template, delivery evidence.
- **Q10** — Import source, data owner, cutoff date, volume, quality.
- **Q11** — Environment, load, file size, backup, recovery, retention, support.
- **Q12** — Certificate format, numbering, publisher, revocation, verification.
- **Q13** — School/SLB/PKBM representatives, official integration access.
- **Q14** — Honor, sponsor, internship, job placement, external certification.

## Conventions

- **Money** — integer in smallest unit + currency (pilot: IDR, scale 0). NO floating point.
- **Timezone** — explicit IANA name (pilot: `Asia/Jakarta`). Instants carry offsets.
- **Date vs Instant** — `Date` is calendar date (no time-of-day); `Instant` is point in time with zone.
- **Null** — Optional unfilled values stored as "unknown" or "not applicable", never as `0`, `false`,
  fabricated name, or default date.
- **Tenant = ROOT** — `Tenant.id` is the tenant identity. Every T/U entity carries `tenant_id`.
- **Whole-grant decision** — R02. Do NOT splice actions from Grant A with scope/relation from Grant B.
- **Default-deny** — Unclear permission or unavailable context → 403 with no payload leakage.
- **Multi-language field names** — Documents use Indonesian canonical terms. Code identifiers may use
  English (snake_case) for portability, but the model classes preserve the Indonesian concept names.