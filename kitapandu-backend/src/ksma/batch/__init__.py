"""KSMA Batch bounded context.

Owns:
- Batch (cohort with capacity, period, mode)
- Application (admission request; approval ≠ enrollment activation)
- Enrollment (active participation; race-safe via FOR UPDATE + exclusion constraint)
- EnrollmentChange (transfer/pause/resume/withdraw decision)
- TeachingAssignment (instructor/substitute/assessor/approver, batch or session scope)

The capacity-race contract (R06 + V05) is enforced at two layers:
1. App-layer SELECT … FOR UPDATE on the Batch row + active-enrollment count
2. Postgres exclusion constraint on Enrollment as a backstop

R07 (no overlapping active enrollment on same (participant, batch)) is enforced
primarily by the same exclusion constraint; V06 reproduces this scenario.
"""