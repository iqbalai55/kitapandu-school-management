"""KSMA Participant bounded context.

Owns Participant, RelatedParty, ParticipantAccountLink, ParticipantRelation.

Design notes
------------
- Participant is a tenant-local profile (ADR04). Account is global; link is verified.
- Guardian, payer, sponsor are DISTINCT relationships per BR02. Two relations can
  exist between the same Participant and RelatedParty (one academic_guardian + one payer).
- Duplicate-suspect detection is computed at search time, NOT stored (V02).
- Profile merging requires a separate rule (R17); we expose the endpoint but gate
  it behind Q02 policy publication.
- ``support_needs_note`` is encrypted-at-rest and only visible to grants that
  include X-A02.
"""