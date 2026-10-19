# 03: Participant + enrollment slice — registration to placement with capacity race

**What to build:** A complete registration-to-placement flow: AC searches/creates participants, verifies guardian/payer relations, drafts/submits/decides an Application, then places the approved applicant. The placement endpoint demonstrates the capacity race (V05) and the overlap rejection (V06). After this ticket, you can demo V02 (duplicate candidates) and V05/V06.

**Blocked by:** 00, 01, 02 (identity for tenant context + membership + grants)

**Status:** ready-for-agent

- [ ] `POST /api/v1/participants` creates a `Participant` with `participant_no` (unique per tenant), `full_name`, optional `birth_date`, `contact`, optional `identity_reference`; returns the canonical read view.
- [ ] `GET /api/v1/participants?q=<name|contact>` returns candidates with `duplicate_suspect: true` flag when name + contact fuzzy-match score > 0.8 (V02); never auto-merges.
- [ ] `POST /api/v1/related-parties` creates a `RelatedParty` (person or organization).
- [ ] `POST /api/v1/participants/{id}/account-link` verifies an `Account` ↔ `Participant` (or `RelatedParty`) bridge with explicit `verification` (officer + time + evidence ref); rejects same-name/email/NIK as proof.
- [ ] `POST /api/v1/participants/{id}/relations` creates a `ParticipantRelation` with `relation_kind` ∈ `{academic_guardian, payer}`, optional `financial_scope`, `verification`, validity; rejects if `relation_kind='payer'` without a `financial_scope`.
- [ ] `POST /api/v1/relations/{id}/revoke` sets `status='revoked'` and bumps the relation's account's `authz_revision`.
- [ ] `POST /api/v1/applications` creates a `Application` in `status='draft'` with a `completeness` checklist.
- [ ] `POST /api/v1/applications/{id}/submit` transitions `draft → submitted`.
- [ ] `POST /api/v1/applications/{id}/decide` transitions `submitted → approved | rejected` with a `decision` (actor + reason); an approved application does NOT auto-create an enrollment or an invoice.
- [ ] `POST /api/v1/batches/{id}/placements` creates an `Enrollment` for an approved application; runs `SELECT FOR UPDATE` on the batch row, counts effective enrollments, returns 409 `CAPACITY_EXCEEDED` with fresh `current_capacity` / `occupied` (V05); on success returns the enrollment row + emits `EnrollmentChanged` outbox event.
- [ ] The DB-level `btree_gist` exclusion constraint on `enrollment` rejects overlapping ranges on the same `(participant, batch)` (R07, V06); application layer surfaces it as 409.
- [ ] `POST /api/v1/enrollments/{id}/transfer` creates `EnrollmentChange(change_kind='transfer', source=id, target=new_enrollment_id)`; source `status='transferred'`; emits outbox event for the portal/finance handoff.
- [ ] `POST /api/v1/enrollments/{id}/pause`, `.../resume`, `.../withdraw` complete the lifecycle; each emits the corresponding outbox event.
- [ ] Integration tests for: V02 duplicate-candidate flag (no auto-merge), V05 concurrent-placements race (exactly one succeeds), V06 overlapping enrollment rejection, transfer → emits outbox event with `source_version` snapshot, relation revoke → portal sees no draft reports after the change.