"""KSMA Program bounded context.

Owns:
- Program (unit-owned definition)
- ProgramVersion (versioned curriculum; batches reference one version per ADR05)
- AcademicPeriod (course window / academic year / semester)
- RubricVersion + RubricIndicator (versioned rubric per ProgramVersion)

Does NOT own PolicyVersion — that lives in ``identity`` per ADR08; this context
references ``PolicyVersion.id`` via the optional FK on Program/ProgramVersion
when the policy is scoped to a program.
"""