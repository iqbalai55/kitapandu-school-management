# ADR-0003: Work Items reuse ServiceRequest with a system-origin flag

- **Status**: Accepted
- **Date**: 2026-10-20
- **Supersedes**: none
- **See also**: ADR-0002 (aggregate boundaries), GLOSSARY (`ServiceRequest`).

## Context

PRD and UX blueprint repeatedly mention "work items" handed from one party to another:

- F01 step 5: "transfer records origin, destination, reason, effective date, and spawns a
  billing-review work item for finance".
- F04 step 4: "approver reviews preview, recipients, version; publishes".
- F08: "import preview, problematic rows, commit, results".
- OPS-A07: "work queue (unchecked evidence, incomplete attendance, reports awaiting approval,
  open requests)".

The data dictionary defines `ServiceRequest` with `requester_account_id`, `participant_id`,
`category`, `source_ref`, `description`, `assigned_membership_id`, `status`, `resolution`.

## Decision

Reuse `ServiceRequest` as the work-item primitive, with two additions:

1. `requester_kind` column: `account` (default) or `service`. When `service`, the
   `requester_account_id` is NULL and a new column `system_event_id` points at the originating
   `OutboxEvent`.
2. View `ops_work_queue` in the operations BC joins `ServiceRequest` and `InternalNotification`
   to give the dashboard query in OPS-A07:

```sql
SELECT id, category, status, age_since_submission, assigned_role, source_ref
FROM ops_work_queue
WHERE status IN ('open', 'in_review')
ORDER BY age_since_submission DESC;
```

Categories stay the data dictionary's four: `academic`, `schedule`, `identity`, `finance`. The
system-generated work items for cross-BC handoffs use the same category enum and add a
`system_event_id` for traceability.

## Consequences

- No new aggregate root.
- Dashboard query is a single SQL view.
- Operator UX is identical whether the work item came from a user (`ST`, `GU`, `PY`) or from the
  system (placement, import result, assessment awaiting approval).
- Audit trail: the originating `OutboxEvent` is the system actor's reason; the `ServiceRequest`
  carries the resolved human action with its own audit.

## Deferred

- If "work items" turn out to need richer fields (priority, due_at, SLA), we may extract a
  separate `WorkItem` entity in a future ADR. For the pilot, the four-category enum + status
  flow covers it.