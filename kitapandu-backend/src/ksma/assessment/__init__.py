"""KSMA Assessment bounded context.

Owns Assessment, AssessmentResult, AssessmentEvidence.

Invariants (R10, R11, §3.28–§3.30):
- Assessment references a specific ``rubric_version_id`` (no silent switch)
- ``AssessmentResult`` has exactly one correctly-typed value matching
  ``RubricIndicator.value_kind`` (numeric / categorical / narrative)
- Empty value ≠ zero (R10)
- Evidence is private (``visibility='internal_assessment'``); portal reads the
  report snapshot, not raw evidence
- ``Assessment.supersedes_id`` chains revisions; old versions stay queryable

The reporting context reads ``Assessment`` IDs to build ``ReportPublication``;
the two contexts share a single migration in 0008 since they have a tight
read dependency.
"""