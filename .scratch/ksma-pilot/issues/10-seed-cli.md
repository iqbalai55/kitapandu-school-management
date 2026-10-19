# 10: Seed CLI — two-tenant idempotent demo data

**What to build:** A single CLI command `python -m ksma.scripts.seed_vaneta_demo` that populates the dev DB with two demo tenants (`Vaneta DEMO`, `Lembaga Uji DEMO`), 11 demo users per tenant, 6 demo participants (including two "Sari" duplicates for V02), one program + one version + one batch + three sessions + two invoices + one verified payment + one allocation + one attendance row. Records an `OperationalRun` for the seed. Re-runnable without duplicates.

**Blocked by:** 00, 01, 02, 03, 04, 05, 06, 07, 08, 09 (every BC needs to exist)

**Status:** ready-for-agent

- [ ] `ksma.scripts.seed_vaneta_demo` is a `python -m` CLI that sets `app.bypass_rls='on'` for its bootstrap `ServicePrincipal` and runs idempotent upserts.
- [ ] Tenant `Vaneta DEMO` (`display_name='Vaneta Maju Berdaya (DEMO)'`, `default_timezone='Asia/Jakarta'`, `service_status='active'`) and Tenant `Lembaga Uji DEMO` (`display_name='Lembaga Uji Demo (PENGGANTI)'`) are created or upserted.
- [ ] One `EducationUnit` per tenant (`unit_code='VANETA-UNIT-A'`, `institution_type='lk_lkp'`, `education_levels=['TK']`, `timezone='Asia/Jakarta'`); the same for the second tenant.
- [ ] A bootstrap `ServicePrincipal(principal_code='seed-bootstrap', environment='development', purpose='KSMA dev seed')` is created once and used as `created_by` for every seeded row.
- [ ] Per tenant, the 13 demo users are created with Argon2id-hashed password `Demo!2345`: `op`, `am`, `ow`, `ac`, `fi`, `in`, `as`, `ap`, `au`, `st`, `gu`, `py` (and `sv` for completeness), with email `<code>@<tenant-slug>.id`.
- [ ] For Vaneta: 6 demo participants including `Sari Wulandari (P-0001)`, `Budi Santoso (P-0002)`, `Dewi Lestari (P-0003)`, `Andi Pratama (P-0004)`, `Rina Kusuma (P-0005)`, and `Sari Rahmawati (P-0006)` (intentional V02 duplicate-candidate target with same family contact).
- [ ] For Vaneta: one `Program(EN-A1, service_group='language')` + one published `ProgramVersion(v1)` with `learning_objectives='Can introduce self and ask basic questions'`, one `Batch(EN-A1-Okt2026, capacity=10, delivery_mode='onsite', status='open')`.
- [ ] Three `Session` rows for that batch, scheduled at `now + 7d`, `now + 14d`, `now + 21d`, each with one `SessionRevision(starts_at, ends_at, timezone='Asia/Jakarta')`.
- [ ] Two `Invoice` rows issued: `INV-VANETA-0001` for P-0001 (one installment Rp 1,000,000), `INV-VANETA-0002` for P-0002 (two installments Rp 600,000 + Rp 400,000). Both `document_status='issued'`.
- [ ] One `PaymentSubmission` (P-0001, claimed_amount=1,000,000) → one `VerifiedPayment(recognized_amount=1,000,000)` → one `PaymentAllocation(amount=1,000,000)` against INV-VANETA-0001's installment; emits outbox events for the portal inbox.
- [ ] One `Attendance` row for P-0001 in the first scheduled session with `attendance_code='hadir'`.
- [ ] An `OperationalRun(operation_kind='seed', environment='development', execution_subject_id=<seed-bootstrap>)` row records the entire seed run; re-running increments `attempt_count`.
- [ ] Integration test: run the seed twice, assert no duplicates and the same row counts.